# MSUO Live Execution Observatory

Live WatchDog request-lifecycle diagnostics for EntryFlow and OptiSpec. The dashboard compares momentary and rolling-average request latency, divergence, and startup/persistence configurations.

## Status

This repository is being prepared for deployment to a Windows Server using GitHub Actions and a self-hosted Windows runner. The application source must be added before deployment can run.

## Local application

The application uses Python 3.10+ and runs locally on `127.0.0.1:8765`. Configure the two local WatchDog log directories in the dashboard. Keep production log files, credentials, configuration containing internal paths, and VPN secrets out of Git.

## Deployment plan

1. Add `server.py`, `web/`, Windows launch scripts, and tests from the MSUO Live Benchmark application package.
2. Install a self-hosted GitHub Actions runner on the Windows Server with restricted permissions.
3. Configure the deployment directory and a managed service or scheduled task for the application.
4. Add an approved GitHub Actions deployment workflow.
5. Expose the dashboard via the existing Cloudflare Tunnel with Cloudflare Access authentication.

**Security:** This repository is currently public. Make it private before connecting a production self-hosted runner. Do not upload real WatchDog logs or secrets.
