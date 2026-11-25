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
    catch (Google.Cloud.Firestore.V1.FirestoreException ex) when (ex.Status.StatusCode == Grpc.Core.StatusCode.AlreadyExists) {
        // Deliberately avoid exposing account existence to an unauthenticated caller.
        return Results.Accepted(value: new { message = "If eligible, a license will be emailed to that address." });
    }
    // First delivery attempt; if SMTP fails, leave it pending for the background retry worker.
    try {
        await mailer.SendAsync(email, license, ct);
        await doc.UpdateAsync(new Dictionary<string, object>{
            ["deliveryStatus"] = "sent", ["sentAt"] = Timestamp.GetCurrentTimestamp(), ["deliveryAttempts"] = 1
        }, cancellationToken: ct);
    } catch (Exception ex) {
        log.LogError(ex, "License email delivery failed; record remains queued.");
    }
    return Results.Accepted(value: new { message = "If eligible, a license will be emailed to that address." });
}).RequireRateLimiting("register");

app.Run();

public sealed record RegistrationInput(string Email);

public sealed class LicenseMailer(IConfiguration config) {
    public async Task SendAsync(string address, string license, CancellationToken ct) {
        var host = config["SMTP_HOST"] ?? throw new InvalidOperationException("SMTP_HOST missing.");
        var username = config["SMTP_USER"] ?? throw new InvalidOperationException("SMTP_USER missing.");
        var password = config["SMTP_PASSWORD"] ?? throw new InvalidOperationException("SMTP_PASSWORD missing.");
        var from = config["SMTP_FROM"] ?? throw new InvalidOperationException("SMTP_FROM missing.");
        var port = int.TryParse(config["SMTP_PORT"], out var number) ? number : 587;
        using var message = new MailMessage(from, address) {
            Subject = "Your MSUO Community Edition License",
            Body = "Thank you for registering for MSUO Community Edition.\n\nYour signed license key:\n\n" + license + "\n\nKeep this key for your SDK installation.\n\nMSUO",
            IsBodyHtml = false
        };
        using var smtp = new SmtpClient(host, port) {
            EnableSsl = true, Credentials = new System.Net.NetworkCredential(username, password),
            DeliveryMethod = SmtpDeliveryMethod.Network, Timeout = 20000
        };
        ct.ThrowIfCancellationRequested();
        await smtp.SendMailAsync(message, ct);
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
                    if (attempts >= 10) continue;
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
