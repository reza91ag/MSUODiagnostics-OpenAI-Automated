# MSUO Community Edition License Manager

ASP.NET Core 9 service on `http://127.0.0.1:5107`, intended public hostname `https://license.msuo-runtime.io`.

## Flow
Visitor submits **email only**. No passwords, Firebase Authentication, or Firebase Web API key.
The backend normalizes the address and uses its SHA-256 as the Firestore document ID inside `CommunityEdition`. A new address receives a signed RSA-PSS/SHA-256 license; duplicate addresses do not create another license. Browser responses are generic, and the license is delivered by **Resend email**, never shown on the page. Pending emails are retried (up to 10 attempts).

## Required runner configuration
Place these locally on the Windows deployment server (the deployment workflow does not copy them):

- `C:\MSUO\Secrets\firebase-service-account.json` — supplied Firebase service account
- `C:\MSUO\Secrets\MSUO_signing_private.pem` — generated MSUO RSA private signing key
- `C:\MSUO\Secrets\resend.json` — Resend configuration of the following shape:
```json
{
  "Mail-Service": { "Provider": "Resend", "Token": "your-resend-api-key" },
  "From": "MSUO <license@msuo-runtime.io>"
}
```
The `From` address must belong to a sending domain verified in Resend (or another Resend-authorized sender). The domain is a proposed address, not verified here.

The runner reads this file into `RESEND_API_KEY` and `RESEND_FROM` for the .NET service. The Firebase service-account JSON goes into `GOOGLE_APPLICATION_CREDENTIALS`. No API credentials belong in the public repository.

## Build
`dotnet publish license-manager/MSUO.LicenseManager.csproj -c Release`

## Smoke test
`GET http://127.0.0.1:5107/health` — after the server is running.

This service is independent of MSUO Diagnostics, which uses its own port.
