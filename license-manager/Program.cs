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
    options.OnRejected = async (context, ct) => {
        context.HttpContext.Response.StatusCode = StatusCodes.Status429TooManyRequests;
        context.HttpContext.Response.ContentType = "application/json";
        context.HttpContext.Response.Headers.RetryAfter = "900";
        await context.HttpContext.Response.WriteAsJsonAsync(
            new { error = "Too many registration attempts. Please try again in 15 minutes." }, cancellationToken: ct);
    };
    options.AddPolicy("register", ctx => {
        // Cloudflare Tunnel connects to Kestrel from loopback; the socket IP alone
        // would incorrectly count every visitor as one user.
        var tunnelIp = ctx.Request.Headers["CF-Connecting-IP"].FirstOrDefault();
        var visitorIp = System.Net.IPAddress.TryParse(tunnelIp, out var parsed)
            ? parsed.ToString()
            : (ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown");
        return RateLimitPartition.GetFixedWindowLimiter(
            visitorIp,
            _ => new FixedWindowRateLimiterOptions {
                PermitLimit = 5, Window = TimeSpan.FromMinutes(15),
                QueueLimit = 0, AutoReplenishment = true
            });
    });
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

        // Blue (#596A89), transparent PNG converted from the official MSUO logo, embedded via Resend Content-ID for email clients.
        const string logoBase64 = "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";
        var filename = "MSUO-Community-Edition.msuolicense";
        var html = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f2f5f9;font-family:Arial,Helvetica,sans-serif;color:#19243a;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f2f5f9;padding:36px 12px;">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:620px;border:1px solid #e0e5ed;border-radius:18px;background:#ffffff;overflow:hidden;">
<tr><td align="center" style="padding:36px 32px 15px;">
<img alt="MSUO" width="90" height="82" src="cid:msuo-kernel-logo" style="display:block;width:90px;height:82px;object-fit:contain;margin:0 auto 12px;">
<div style="font-size:13px;letter-spacing:2px;font-weight:700;color:#596a89;">MSUO™ APPLICATION ORCHESTRATION KERNEL</div>
</td></tr>
<tr><td style="padding:12px 36px 34px;">
<h1 style="margin:4px 0 18px;font-size:28px;line-height:1.25;color:#111827;">Welcome to MSUO™ Application Orchestration Kernel</h1>
<p style="font-size:16px;line-height:1.7;color:#435169;margin:0 0 20px;">Your <strong>Community Edition</strong> license has been issued and is attached to this email.</p>
<table role="presentation" width="100%" style="background:#f6f8fc;border:1px solid #dbe3f0;border-radius:12px;">
<tr><td style="padding:20px 22px;">
<div style="font-size:12px;font-weight:700;letter-spacing:1px;color:#64748b;">YOUR LICENSE FILE</div>
<div style="font-size:16px;font-weight:700;color:#1b3358;padding-top:10px;overflow-wrap:anywhere;">MSUO-Community-Edition.msuolicense</div>
<p style="font-size:14px;line-height:1.65;color:#4a5970;margin:12px 0 0;">Please download the attachment and <strong>keep it in a safe place</strong>. You may need it when configuring the MSUO SDK or restoring your development environment.</p>
</td></tr></table>
<p style="margin:26px 0 8px;font-size:17px;font-weight:700;color:#182641;">Enjoy the new developer experience.</p>
<p style="font-size:15px;color:#435169;line-height:1.6;margin:0 0 22px;">Build freely, experiment, and explore a different approach to application architecture.</p>
<p style="font-size:15px;line-height:1.6;color:#435169;margin:0;">Best regards,<br><strong>The MSUO™ Application Orchestration Kernel Team</strong></p>
</td></tr>
<tr><td style="padding:22px 36px;background:#f8fafc;border-top:1px solid #e6ebf2;text-align:center;color:#7a889e;font-size:12px;">MSUO™ Application Orchestration Kernel &middot; msuo-runtime.io</td></tr>
</table>
</td></tr>
</table>
</body></html>
""";

        var text = "Welcome to MSUO™ Application Orchestration Kernel Community Edition\n\n"
            + "Your signed Community Edition license file is attached: " + filename + "\n\n"
            + "Please keep this file in a safe place. You may need it for SDK setup or restoring your development environment.\n\n"
            + "Enjoy the new developer experience.\n\nBest regards,\nThe MSUO Team";
        using var request = new HttpRequestMessage(HttpMethod.Post,"https://api.resend.com/emails");
        request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer",apiKey);
        request.Headers.UserAgent.ParseAdd("MSUO-LicenseManager/1.0");
        request.Content = System.Net.Http.Json.JsonContent.Create(new {
            from,
            to = new[] { address },
            subject = "MSUO™ Application Orchestration Kernel — Your Community Edition License",
            html,
            text,
            attachments = new object[] {
                new { filename, content = Convert.ToBase64String(Encoding.UTF8.GetBytes(license)), content_type = "application/octet-stream" },
                new { filename = "msuo-logo.png", content = logoBase64, content_type = "image/png", content_id = "msuo-kernel-logo" }
            }
        });
        using var response=await factory.CreateClient().SendAsync(request,ct);
        if(!response.IsSuccessStatusCode)
            {
            var body = await response.Content.ReadAsStringAsync(ct);
            // Preserve the validation reason, but redact any accidentally echoed personal/secret data.
            body = Regex.Replace(body, @"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", "[email-redacted]");
            body = Regex.Replace(body, @"re_[A-Za-z0-9_\-]+", "[api-key-redacted]");
            body = Regex.Replace(body, @"MSUO1\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", "[license-redacted]");
            throw new HttpRequestException("Resend rejected license email HTTP " + (int)response.StatusCode + ": " + body[..Math.Min(body.Length, 600)]);
        }
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
