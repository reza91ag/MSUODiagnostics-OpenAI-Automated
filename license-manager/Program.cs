using Google.Cloud.Firestore;
using MSUO.LicenseManager;
using System.Net.Mail;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.RateLimiting;

var builder = WebApplication.CreateBuilder(args);
builder.WebHost.UseUrls(builder.Configuration["BIND_URL"] ?? "http://127.0.0.1:5107");
builder.Services.AddProblemDetails();
builder.Services.AddRateLimiter(options => {
    options.RejectionStatusCode = 429;
    options.AddPolicy("register", ctx => RateLimitPartition.GetFixedWindowLimiter(
        ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown",
        _ => new FixedWindowRateLimiterOptions { PermitLimit = 5, Window = TimeSpan.FromMinutes(15), QueueLimit = 0, AutoReplenishment = true }));
});

var projectId = builder.Configuration["FIREBASE_PROJECT_ID"] ?? "msuo-community-licensemanager";
var keyPath = builder.Configuration["SIGNING_PRIVATE_KEY_PATH"];
if (string.IsNullOrWhiteSpace(keyPath) || !File.Exists(keyPath))
    throw new InvalidOperationException("A signing private key is required at SIGNING_PRIVATE_KEY_PATH.");
// Uses GOOGLE_APPLICATION_CREDENTIALS, which points to the supplied service-account JSON.
// The Google Cloud library performs server-side auth. No Firebase Authentication users are created.
builder.Services.AddSingleton(FirestoreDb.Create(projectId));
builder.Services.AddSingleton(new LicenseSigner(File.ReadAllText(keyPath)));
builder.Services.AddHttpClient();
builder.Services.AddSingleton<LicenseMailer>();
builder.Services.AddHostedService<PendingMailWorker>();

var app = builder.Build();
app.UseExceptionHandler();
app.UseDefaultFiles();
app.UseStaticFiles();
app.UseRateLimiter();

app.MapGet("/health", () => Results.Ok(new { status = "running" }));
app.MapPost("/api/register", async (RegistrationInput input, FirestoreDb db, LicenseSigner signer, LicenseMailer mailer, ILogger<Program> log, CancellationToken ct) => {
    // No passwords, no Firebase Authentication and no browser Firebase API key.
    if (string.IsNullOrWhiteSpace(input.Email) || input.Email.Length > 254)
        return Results.BadRequest(new { error = "Enter a valid email address." });
    var email = input.Email.Trim().Normalize(NormalizationForm.FormKC).ToLowerInvariant();
    try { var parsed = new MailAddress(email); if (!parsed.Address.Equals(email, StringComparison.Ordinal)) throw new FormatException(); }
    catch { return Results.BadRequest(new { error = "Enter a valid email address." }); }
    if (!Regex.IsMatch(email, @"^[^\s@]+@[^\s@]+\.[^\s@]+$"))
        return Results.BadRequest(new { error = "Enter a valid email address." });

    // Deterministic document ID enforces case-insensitive uniqueness, even under concurrent signups.
    var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(email))).ToLowerInvariant();
    var doc = db.Collection("CommunityEdition").Document(hash);
    var license = signer.Issue(hash, email);
    var record = new Dictionary<string, object> {
        ["email"] = email, ["emailHash"] = hash, ["edition"] = "CommunityEdition",
        ["licenseKey"] = license, ["issuedAt"] = Timestamp.GetCurrentTimestamp(),
        ["deliveryStatus"] = "pending", ["deliveryAttempts"] = 0, ["schemaVersion"] = 1
    };
    try { await doc.CreateAsync(record, ct); }
    catch (Grpc.Core.RpcException ex) when (ex.StatusCode == Grpc.Core.StatusCode.AlreadyExists) {
        // Deliberately avoid exposing account existence to an unauthenticated caller.
        return Results.Accepted(value: new { message = "If eligible, a license will be emailed to that address." });
    }
    // The background worker sends the license. No browser ever receives the key.
    return Results.Accepted(value: new { message = "If eligible, a license will be emailed to that address." });
}).RequireRateLimiting("register");

app.Run();

public sealed record RegistrationInput(string Email);

public sealed class LicenseMailer(IConfiguration config, IHttpClientFactory factory) {
    public async Task SendAsync(string address, string license, CancellationToken ct) {
        var apiKey = config["RESEND_API_KEY"] ?? throw new InvalidOperationException("RESEND_API_KEY not configured.");
        var from = config["RESEND_FROM"] ?? throw new InvalidOperationException("RESEND_FROM not configured.");
        using var request = new HttpRequestMessage(HttpMethod.Post, "https://api.resend.com/emails");
        request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", apiKey);
        request.Headers.UserAgent.ParseAdd("MSUO-LicenseManager/1.0");
        request.Content = System.Net.Http.Json.JsonContent.Create(new {
            from,
            to = new[] { address },
            subject = "Your MSUO Community Edition License",
            text = "Thank you for registering for MSUO Community Edition.\\n\\nYour signed license key:\\n\\n" + license +
                "\\n\\nKeep this key for your SDK installation.\\n\\nMSUO"
        });
        using var response = await factory.CreateClient().SendAsync(request, ct);
        if (!response.IsSuccessStatusCode)
            throw new HttpRequestException("Resend rejected the email request with status " + (int)response.StatusCode);
    }
}

public sealed class PendingMailWorker(FirestoreDb db, LicenseMailer mailer, ILogger<PendingMailWorker> log) : BackgroundService {
    protected override async Task ExecuteAsync(CancellationToken stoppingToken) {
        while (!stoppingToken.IsCancellationRequested) {
            try {
                var docs = await db.Collection("CommunityEdition")
                    .WhereEqualTo("deliveryStatus", "pending")
                    .Limit(25).GetSnapshotAsync(stoppingToken);
                foreach (var record in docs.Documents) {
                    if (stoppingToken.IsCancellationRequested) break;
                    var attempts = record.TryGetValue<long>("deliveryAttempts", out var n) ? n : 0L;
                    if (attempts >= 10) { await record.Reference.UpdateAsync(new Dictionary<string, object> { ["deliveryStatus"] = "failed" }, cancellationToken: stoppingToken); continue; }
                    try {
                        await record.Reference.UpdateAsync(new Dictionary<string, object> {
                            ["deliveryAttempts"] = attempts + 1,
                            ["lastAttemptAt"] = Timestamp.GetCurrentTimestamp()
                        }, cancellationToken: stoppingToken);
                        await mailer.SendAsync(record.GetValue<string>("email"), record.GetValue<string>("licenseKey"), stoppingToken);
                        await record.Reference.UpdateAsync(new Dictionary<string, object> {
                            ["deliveryStatus"] = "sent", ["sentAt"] = Timestamp.GetCurrentTimestamp()
                        }, cancellationToken: stoppingToken);
                    } catch (Exception ex) { log.LogWarning(ex, "Queued license delivery failed."); }
                }
            } catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
              catch (Exception ex) { log.LogError(ex, "Email queue processing failed."); }
            try { await Task.Delay(TimeSpan.FromMinutes(3), stoppingToken); }
            catch (OperationCanceledException) { break; }
        }
    }
}
