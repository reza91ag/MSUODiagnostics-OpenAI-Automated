using Google.Cloud.Firestore;
using MSUO.LicenseManager;
using System.Net.Mail;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Text.Json;
using System.Threading.RateLimiting;

var builder = WebApplication.CreateBuilder(new WebApplicationOptions { Args = args, ContentRootPath = AppContext.BaseDirectory });
builder.WebHost.UseUrls(builder.Configuration["BIND_URL"] ?? "http://127.0.0.1:5107");
builder.Services.AddProblemDetails();
builder.Services.AddRateLimiter(options => {
    options.RejectionStatusCode = 429;
    options.OnRejected = async (context, ct) => {
        context.HttpContext.Response.StatusCode = StatusCodes.Status429TooManyRequests;
        context.HttpContext.Response.ContentType = "application/json";
        context.HttpContext.Response.Headers.RetryAfter = context.HttpContext.Request.Path == "/heart-beat" ? "60" : "900";
        await context.HttpContext.Response.WriteAsJsonAsync(
            new { error = "Too many requests. Please try again later." }, cancellationToken: ct);
    };
    options.AddPolicy("heartbeat", ctx => RateLimitPartition.GetFixedWindowLimiter(
        ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown",
        _ => new FixedWindowRateLimiterOptions {
            PermitLimit = 1200, Window = TimeSpan.FromMinutes(1), QueueLimit = 0, AutoReplenishment = true
        }));
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
builder.Services.AddSingleton(new EulaAgreement(Path.Combine(AppContext.BaseDirectory, "wwwroot", "eula", EulaAgreement.Version)));
builder.Services.AddSingleton<LicenseMailer>();
if (!builder.Configuration.GetValue<bool>("DISABLE_MAIL_WORKER"))
    builder.Services.AddHostedService<PendingMailWorker>();

var app = builder.Build();
app.UseExceptionHandler();
app.Use(async (ctx, next) => {
    var path = ctx.Request.Path.Value ?? "";
    if (path.Equals("/DeveloperPreview", StringComparison.OrdinalIgnoreCase) ||
        path.Equals("/preview", StringComparison.OrdinalIgnoreCase) ||
        path.Equals("/preview.html", StringComparison.OrdinalIgnoreCase) ||
        path.Equals("/app.js", StringComparison.OrdinalIgnoreCase) ||
        path.Equals("/api/eula", StringComparison.OrdinalIgnoreCase) ||
        path.Equals("/api/register", StringComparison.OrdinalIgnoreCase)) {
        ctx.Response.OnStarting(() => {
            ctx.Response.Headers.CacheControl = "no-store, no-cache, must-revalidate, max-age=0";
            ctx.Response.Headers["CDN-Cache-Control"] = "no-store";
            ctx.Response.Headers["Cloudflare-CDN-Cache-Control"] = "no-store";
            return Task.CompletedTask;
        });
    }
    await next();
});
app.UseDefaultFiles();
app.UseStaticFiles();
app.UseRateLimiter();

app.MapGet("/health", () => Results.Ok(new { status = "running" }));
// Stable public PNG endpoint for email clients that do not support CID inline attachments.
app.MapGet("/msuo-email-logo.png", () => Results.File(
    Convert.FromBase64String(EmailLogo.PngBase64), "image/png"));

app.MapGet("/CommunityEdition", () => Results.File(Path.Combine(app.Environment.WebRootPath, "community.html"), "text/html"));
app.MapGet("/DeveloperPreview", () => Results.File(Path.Combine(app.Environment.WebRootPath, "preview.html"), "text/html"));
app.MapGet("/preview", () => Results.Redirect("/DeveloperPreview"));
app.MapGet("/api/eula", (EulaAgreement eula, HttpContext ctx) => {
    ctx.Response.Headers.CacheControl = "no-store";
    return Results.Ok(new { version = EulaAgreement.Version, sha256 = eula.Sha256, text = eula.Text, pdfUrl = $"/eula/{EulaAgreement.Version}/MSUO-EULA.pdf" });
});
app.MapPost("/heart-beat", async (HeartbeatInput input, FirestoreDb db, CancellationToken ct) => {
    if (!HeartbeatCredential.TryResolve(input, out var edition, out var id, out var fingerprint))
        return Results.Unauthorized();
    var doc = db.Collection(edition).Document(id);
    // Compare against the original issued credential before updating; unknown licenses never create records.
    var accepted = await db.RunTransactionAsync(async tx => {
        var record = await tx.GetSnapshotAsync(doc, ct);
        if (!record.Exists || !record.TryGetValue<string>("licenseKey", out var issued) ||
            string.IsNullOrEmpty(issued) ||
            !CryptographicOperations.FixedTimeEquals(SHA256.HashData(Encoding.UTF8.GetBytes(issued)), fingerprint))
            return false;
        tx.Update(doc, new Dictionary<string, object> {
            ["usageCount"] = FieldValue.Increment(1L),
            ["lastUsedUtc"] = FieldValue.ServerTimestamp
        });
        return true;
    }, cancellationToken: ct);
    return accepted ? Results.NoContent() : Results.Unauthorized();
}).RequireRateLimiting("heartbeat");

app.MapPost("/api/register", async (RegistrationInput input, FirestoreDb db, LicenseSigner signer, EulaAgreement eula, CancellationToken ct) => {
    if (!input.EulaAccepted)
        return Results.BadRequest(new { code = "eula_not_accepted", error = "Please accept the End-User License Agreement before requesting a license." });
    if (input.EulaVersion != EulaAgreement.Version || input.EulaSha256 != eula.Sha256)
        return Results.Conflict(new { code = "eula_changed", error = "The agreement has changed. Please review and accept the current agreement." });
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
    if (input.Edition != "DeveloperPreview") return Results.BadRequest(new { error = "Community Edition is coming soon." });
    const string edition = "DeveloperPreview";
    var doc = db.Collection(edition).Document(hash);
    var license = signer.Issue(hash, email, edition);
    var record = new Dictionary<string, object> {
        ["email"] = email, ["emailHash"] = hash, ["edition"] = edition,
        ["licenseKey"] = license, ["issuedAt"] = Timestamp.GetCurrentTimestamp(),
        ["deliveryStatus"] = "pending", ["deliveryAttempts"] = 0, ["schemaVersion"] = 2,
        ["usageCount"] = 0L, ["eulaAccepted"] = true, ["eulaVersion"] = EulaAgreement.Version,
        ["eulaSha256"] = eula.Sha256, ["eulaAcceptedUtc"] = FieldValue.ServerTimestamp
    };
    try { await doc.CreateAsync(record, ct); }
    catch (Grpc.Core.RpcException ex) when (ex.StatusCode == Grpc.Core.StatusCode.AlreadyExists) {
        // Existing keys are preserved. Legacy registrations may explicitly accept the EULA
        // and receive their original key with the newly accepted agreement.
        await db.RunTransactionAsync(async tx => {
            var existing = await tx.GetSnapshotAsync(doc, ct);
            if (!existing.Exists || eula.WasAccepted(existing)) return false;
            tx.Update(doc, new Dictionary<string, object> {
                ["eulaAccepted"] = true, ["eulaVersion"] = EulaAgreement.Version,
                ["eulaSha256"] = eula.Sha256, ["eulaAcceptedUtc"] = FieldValue.ServerTimestamp,
                ["deliveryStatus"] = "pending", ["deliveryAttempts"] = 0
            });
            return true;
        }, cancellationToken: ct);
        return Results.Accepted(value: new { message = "If eligible, a license will be emailed to that address." });
    }
    // The background worker sends the license. No browser ever receives the key.
    return Results.Accepted(value: new { message = "If eligible, a license will be emailed to that address." });
}).RequireRateLimiting("register");

app.Run();

public static class EmailLogo {
    public const string PngBase64 = "iVBORw0KGgoAAAANSUhEUgAAAKAAAACSBAMAAAAwU9y4AAAAMFBMVEVZYJ4AAABZaohZaolZaYhYaYlYZ4pZaohZaohYaohjY40AAP9ZcoyAgIBAgIBVVYDpPC9VAAAAEHRSTlMIAP62bcsULk+NBwEKAgQGQGcWZwAABN9JREFUeNrtWl1oW2UYfs53mhObbl0OKAXFLYahY960hoL1wlUYUpxKHCt2FIa7GTh/KHMlY1KxXrkbDbuSzosDu1IYHsEL2YJT/AGVwgG96DaIuVzB0TKnrj8xXiyJJ8n7fj8nx+lGzk3Ll6/P+zzv977P9yap5SLeR6AH2APsAd7tgFfz9Pq3zP68AtA5SK8nDtHrZwM5YOrRgH7h1VU6zlsKyRMVev2mJ43Tx+ElLzKCH6IJLo3JD2XgJMwEv6w45YNFen2JE9xIOCP5pychJ8II5hg6r0NBhI1DA74SRBTMAF73zARPefLWS+4wFLws7+V+rmKmPI04BOA0UzHOMr3+gSd3m7PnGILMSTkfye2r3uMqImwcodla7USaz+ee3GD3e4aCT8kd21mBHhE2Titg/5tMi3QQqT+nPfmdwlUMK/gT+SXFVsxpTzuO3R+qmMWbrS9mlupEfr8aXrb+bJyg37L9ntU2hlzFgLmuyBMU6lbF/opBHKFsVbaU6DjNK2DrTqZiUo8EJnGaDF+smN3P3BzQAEyeMxTMzQF1wMT9hgOJc1E+fbEVwwqWj3Nc67MDCSe4Dsi1PlBl1n+RD5ybwR03EufcN+IEfHClVJ4fjhHwOoCNyv85h5luAPtiIDU0/dl4qigBNKxKe2kO34/EncNxGeAWholbyB5RticleQ1AItMhfLN8AosjlQinXM1mszs6E5lqkdb2pnFVxrBGm7QVd2Gvcy+kwwxbAO2RoWN8D1tapxyWbP36Bebmzb3xviNuicyhAADfnOGHAIq3xRyqzWTk3ELn1o13szuf8qKYw7ZKCXOd7VI7BVwzK5sgfP5x+OHbIX2x+GF6cML9AchUumDYAvipfaH5+3AQg+RoBqvZy3HeeoOS/TltQHfo2MMzdYMt5B5/hyif2QN7Vr7WPRRRxhx2131qAbhE+dcZoKbLMPFfTV/aDNehbjuvm9bjng0jQMUIlzFhqJ3LYZNetuV7g0nXLQWagNXBCTd9vtFvufJL7xOXSqltNnnBckvPt30IYLkAxLaWtQd+BhJbAOzx+7ZSabqGkcqtn4CVDjk9k7sb3cyNIsqtHu6uosYllakAfyRD4AcAXGFO48sII/HAAoDDAT3spHUdO1Rvv92utxVCydB2C9nD9ZE46hXQ2inlEwgGsQYM7PtR/bfVXdP+3vMah1LdWw0u6LBZfg8LUoabtxZqi/+yY6ci5zBBe/xfkwaHpFPYa6Woktd1biEjhpOu+9WlGAFrJXT93NnfPArmZn+M2Z9XANrPpcn15D56/2hRAVhgCE7N0HEWFJKdWRkRIk5GDmiNQUaEjcMCHvKlRNg4HOC9xUiCWUBxhknEUVUc2m3sZ8eZROTpOClFYc96ZoJDCScBRwtQEWHjkN88MonCdl+dWApwKs8QYcab7Xk5IFcxrOBA7jbOFUPBKbl9cS3HC/blgLuZlrM+ZqbOQG6wo99wgmlztC7LHTv5DPSIsHFaAe2JGUbwE7pxhJZJY5evG0fomDQS39HrJ30pIFsxrODj8muUM2mKCBtHqFsOznGDOEJp0mwm6Djin2udMWm85pvEESqThjNvFEcoTBr2mFkcoTBpzPpGcRqAnEnDKRjFaQCyFdPHEHm6wI4wLgBnPeaBM0a83r8L9gDvTsC/Aa2eK9LUw4SxAAAAAElFTkSuQmCC";
}

public sealed record RegistrationInput(string Email, string? Edition = null,
    bool EulaAccepted = false, string? EulaVersion = null, string? EulaSha256 = null);

public sealed record HeartbeatInput(string? License = null, string? LicenseId = null,
    string? LicenseFingerprint = null, string? Edition = null);

public sealed class EulaAgreement {
    public const string Version = "1.0";
    public string DocumentVersion { get; }
    private readonly EulaAgreement? previous;
    public string Text { get; }
    public string Sha256 { get; }
    public string PdfBase64 { get; }
    public EulaAgreement(string folder, string version = Version) {
        DocumentVersion = version;
        if (version == Version)
            previous = new EulaAgreement(Path.Combine(Path.GetDirectoryName(folder)!, "1.2"), "1.2");
        else if (version == "1.2")
            previous = new EulaAgreement(Path.Combine(Path.GetDirectoryName(folder)!, "1.1"), "1.1");
        var bytes = File.ReadAllBytes(Path.Combine(folder, "LICENSE.txt"));
        Text = Encoding.UTF8.GetString(bytes);
        Sha256 = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
        PdfBase64 = Convert.ToBase64String(File.ReadAllBytes(Path.Combine(folder, "MSUO-EULA.pdf")));
    }
    public bool WasAccepted(DocumentSnapshot record) =>
        record.TryGetValue<bool>("eulaAccepted", out var agreed) && agreed &&
        record.TryGetValue<string>("eulaVersion", out var version) && version == DocumentVersion &&
        record.TryGetValue<string>("eulaSha256", out var hash) && hash == Sha256 &&
        record.TryGetValue<Timestamp>("eulaAcceptedUtc", out _);
    public EulaAgreement? AcceptedAgreement(DocumentSnapshot record) => WasAccepted(record) ? this : previous?.AcceptedAgreement(record);
}

public static class HeartbeatCredential {
    public static bool TryResolve(HeartbeatInput input, out string edition, out string id, out byte[] fingerprint) {
        edition = ""; id = ""; fingerprint = Array.Empty<byte>();
        try {
            if (!string.IsNullOrEmpty(input.License)) {
                if (input.License.Length > 8192) return false;
                var parts = input.License.Split('.');
                if (parts.Length != 3 || parts[0] != "MSUO1") return false;
                var payload = parts[1].Replace('-', '+').Replace('_', '/');
                using var json = JsonDocument.Parse(Convert.FromBase64String(payload + new string('=', (4 - payload.Length % 4) % 4)));
                edition = json.RootElement.GetProperty("edition").GetString() ?? "";
                id = json.RootElement.GetProperty("sub").GetString() ?? "";
                fingerprint = SHA256.HashData(Encoding.UTF8.GetBytes(input.License));
            } else {
                edition = input.Edition ?? "";
                id = input.LicenseId ?? "";
                if (input.LicenseFingerprint is not { Length: 64 }) return false;
                fingerprint = Convert.FromHexString(input.LicenseFingerprint);
            }
            return (edition == "DeveloperPreview" || edition == "CommunityEdition")
                && id.Length == 64 && id.All(c => (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))
                && fingerprint.Length == 32;
        } catch (Exception ex) when (ex is FormatException or JsonException or InvalidOperationException or KeyNotFoundException or ArgumentException) {
            return false;
        }
    }
}

public sealed class LicenseMailer(IConfiguration config, IHttpClientFactory factory) {
    public async Task SendAsync(string address, string license, string edition, EulaAgreement acceptedAgreement, CancellationToken ct) {
        var apiKey = config["RESEND_API_KEY"] ?? throw new InvalidOperationException("RESEND_API_KEY not configured.");
        var from = config["RESEND_FROM"] ?? throw new InvalidOperationException("RESEND_FROM not configured.");

        var filename = "MSUO-Community-Edition.msuolicense";
        var preview = edition == "DeveloperPreview";
        var html = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f2f5f9;font-family:Arial,Helvetica,sans-serif;color:#19243a;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f2f5f9;padding:36px 12px;">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:620px;border:1px solid #e0e5ed;border-radius:18px;background:#ffffff;overflow:hidden;">
<tr><td align="center" style="padding:36px 32px 15px;">
<img alt="MSUO" width="90" height="82" src="https://license.msuo-runtime.io/msuo-email-logo.png" style="display:block;width:90px;height:82px;object-fit:contain;margin:0 auto 12px;">
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
<p style="font-size:15px;line-height:1.6;color:#435169;margin:20px 0;">You received this license after affirmatively agreeing to the MSUO End-User License Agreement (EULA) version EULA_VERSION_PLACEHOLDER, attached as <strong>MSUO-EULA.pdf</strong>. Use the software only within the permissions granted by that agreement. Production use requires separate commercial authorization.</p>
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
            + "You received this license after agreeing to the MSUO EULA version EULA_VERSION_PLACEHOLDER (attached as MSUO-EULA.pdf). Use the software only within its permitted scope. Production use requires separate commercial authorization.\n\n"
            + "Enjoy the new developer experience.\n\nBest regards,\nThe MSUO Team";
        if (preview) {
            html = html.Replace("Community Edition", "Developer Preview").Replace("Community-Edition", "Developer-Preview");
            text = text.Replace("Community Edition", "Developer Preview").Replace("Community-Edition", "Developer-Preview");
            filename = filename.Replace("Community-Edition", "Developer-Preview");
        }
        html = html.Replace("EULA_VERSION_PLACEHOLDER", acceptedAgreement.DocumentVersion);
        text = text.Replace("EULA_VERSION_PLACEHOLDER", acceptedAgreement.DocumentVersion);
        using var request = new HttpRequestMessage(HttpMethod.Post,"https://api.resend.com/emails");
        request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer",apiKey);
        request.Headers.UserAgent.ParseAdd("MSUO-LicenseManager/1.0");
        request.Content = System.Net.Http.Json.JsonContent.Create(new {
            from,
            to = new[] { address },
            subject = "MSUO™ Application Orchestration Kernel — Your " + (preview ? "Developer Preview" : "Community Edition") + " License",
            html,
            text,
            attachments = new[] {
                new { filename, content = Convert.ToBase64String(Encoding.UTF8.GetBytes(license)), content_type = "application/octet-stream" },
                new { filename = "MSUO-EULA.pdf", content = acceptedAgreement.PdfBase64, content_type = "application/pdf" }
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

public sealed class PendingMailWorker(FirestoreDb db, LicenseMailer mailer, EulaAgreement eula, ILogger<PendingMailWorker> log) : BackgroundService {
    protected override async Task ExecuteAsync(CancellationToken stoppingToken) {
        while (!stoppingToken.IsCancellationRequested) {
            try {
                foreach (var edition in new[] { "CommunityEdition", "DeveloperPreview" }) {
                var docs = await db.Collection(edition)
                    .WhereEqualTo("deliveryStatus", "pending")
                    .Limit(25).GetSnapshotAsync(stoppingToken);
                foreach (var record in docs.Documents) {
                    if (stoppingToken.IsCancellationRequested) break;
                    var acceptedAgreement = eula.AcceptedAgreement(record);
                    if (acceptedAgreement is null) {
                        // Never claim a legacy request accepted terms it did not actually accept.
                        // Re-read in a transaction to avoid racing a fresh acceptance.
                        await db.RunTransactionAsync(async tx => {
                            var current = await tx.GetSnapshotAsync(record.Reference, stoppingToken);
                            if (current.Exists && eula.AcceptedAgreement(current) is null)
                                tx.Update(record.Reference, new Dictionary<string, object> { ["deliveryStatus"] = "awaitingEulaAcceptance" });
                            return true;
                        }, cancellationToken: stoppingToken);
                        continue;
                    }
                    var attempts = record.TryGetValue<long>("deliveryAttempts", out var n) ? n : 0L;
                    if (attempts >= 10) { await record.Reference.UpdateAsync(new Dictionary<string, object> { ["deliveryStatus"] = "failed" }, cancellationToken: stoppingToken); continue; }
                    try {
                        await record.Reference.UpdateAsync(new Dictionary<string, object> {
                            ["deliveryAttempts"] = attempts + 1,
                            ["lastAttemptAt"] = Timestamp.GetCurrentTimestamp()
                        }, cancellationToken: stoppingToken);
                        await mailer.SendAsync(record.GetValue<string>("email"), record.GetValue<string>("licenseKey"), edition, acceptedAgreement, stoppingToken);
                        await record.Reference.UpdateAsync(new Dictionary<string, object> {
                            ["deliveryStatus"] = "sent", ["sentAt"] = Timestamp.GetCurrentTimestamp()
                        }, cancellationToken: stoppingToken);
                    } catch (Exception ex) { log.LogWarning(ex, "Queued license delivery failed."); }
                }
                }
            } catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
              catch (Exception ex) { log.LogError(ex, "Email queue processing failed."); }
            try { await Task.Delay(TimeSpan.FromMinutes(3), stoppingToken); }
            catch (OperationCanceledException) { break; }
        }
    }
}
