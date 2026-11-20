# MSUO Live Execution Observatory

Live WatchDog request-lifecycle diagnostics for EntryFlow and OptiSpec. The dashboard compares momentary and rolling-average request latency, divergence, and startup/persistence configurations.

## Status

The application source, parser tests, and Windows deployment workflow are committed. A dedicated self-hosted Windows runner deploys source changes to the server after the tests pass.

## Local application

The application uses Python 3.10+ and runs locally on `127.0.0.1:8765`. Configure the two local WatchDog log directories in the dashboard. Each source also has an optional **Log filename(s)** field: enter one or more exact filenames separated by commas (for example, `Watchdog-Log-Public`), including names without extensions. Leaving it blank uses automatic log discovery. Keep production log files, credentials, configuration containing internal paths, and VPN secrets out of Git.

## Deployment plan

1. The self-hosted Windows runner checks out the repository and runs the parser tests.
2. The deployment workflow registers the Observatory startup task if needed and copies the application to `C:\MSUO\Observatory`.
3. The workflow restarts the dashboard and checks `http://127.0.0.1:8765`.
4. For remote access, publish the dashboard through an existing Cloudflare Tunnel with Cloudflare Access authentication.

**Security:** Keep this repository private. Do not upload production WatchDog logs, credentials, or secrets. The included `sample_logs` are anonymized test fixtures.
