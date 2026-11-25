# MSUO Community Edition Licensing Portal
ASP.NET Core 9, local port 5107, public hostname intended: https://license.msuo-runtime.io.

## Registration
Only an email is submitted. No Firebase Authentication users, passwords, Web API key, or login.
The server checks Firestore collection `CommunityEdition`. Each normalized email's SHA-256 is the document ID, so case-insensitive duplicates cannot create another license, including during races. A signed RSA-PSS/SHA-256 license is stored under `CommunityEdition/{emailHash}`, and the license key is emailed via SMTP; it is never displayed in the browser. Failures are marked pending and retried by a background worker. Repeated requests return the same generic response without disclosing whether an email exists.

## Required private server configuration (never commit)
- GOOGLE_APPLICATION_CREDENTIALS: absolute path to service-account JSON on the runner/server
- SIGNING_PRIVATE_KEY_PATH: absolute path to the MSUO signing private key PEM
- FIREBASE_PROJECT_ID: msuo-community-licensemanager
- SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM

The server must have the selected SMTP provider account configured. No email can be delivered without a working sender. The app has an unauthenticated email form, so use suitable abuse limits and CAPTCHA for public production rollout.
