# MSUO Observatory: Windows Server deployment

The deployment workflow is prepared for `reza91ag/MSUODiagnostics-OpenAI-Automated`.
The initial deployment completed successfully on 2026-11-20. Pushes affecting application source and deployment scripts automatically trigger deployment, and manual dispatch is also supported.

## 1. Secure the repository

**Keep the repository Private while using a production self-hosted runner.**
Do not put production WatchDog logs, tokens, Cloudflare credentials, or `msuo_live_config.json` in Git.

## 2. Install Python

Install Python **3.10+ for all users** on the Windows Server.
In PowerShell verify:

```powershell
py -3 --version
```

## 3. Register a dedicated Windows runner

In GitHub: **Repository → Settings → Actions → Runners → New self-hosted runner → Windows x64**.
Follow the GitHub-generated commands on the Windows Server.
Use a dedicated runner registered to this repository. Configure it to run as a Windows service for unattended deployments.

The workflow expects the default `self-hosted` runner label.
The runner account must have permission to write `C:\MSUO\Observatory` and control the
`MSUO-Observatory` scheduled task. Do not use a shared runner for untrusted repositories.

## 4. Create the dashboard startup task (one time)

The workflow automatically registers the task on its first deployment if the runner has administrative permissions. To create it manually from a checked-out copy, open **Windows PowerShell as Administrator**:

```powershell
.\scripts\setup_windows_task.ps1
```

This creates a startup scheduled task running `server.py --no-browser` as SYSTEM,
listening on `127.0.0.1:8765`. The task will not run until the app files are deployed.

If Python was installed in a custom location:

```powershell
.\scripts\setup_windows_task.ps1 -PythonExe 'C:\Path\To\python.exe'
```

**Runner permissions:** A non-admin runner service account may not be able to restart
a SYSTEM scheduled task. Grant only the necessary task/directory rights, or use a
trusted dedicated runner service account with sufficient privileges.

## 5. Deploy

The source is already in the repository. Pushes to `main` affecting application or deployment files automatically deploy. You can also open **Actions → Deploy MSUO Observatory (Windows) → Run workflow**.

The workflow tests the parser, copies the two application files to
`C:\MSUO\Observatory`, restarts the scheduled task, and checks the dashboard
on `http://127.0.0.1:8765/`. If the health check fails, it attempts to restore
the previous app files. The live `msuo_live_config.json` is preserved.

The destination can be changed via repository Actions variable `MSUO_DEPLOY_DIR`.
If you change the destination, also pass the same path to `setup_windows_task.ps1`.

## 6. Connect Cloudflare

Add a **Published Application Route** on an existing remotely managed Cloudflare
Tunnel: `observatory.msuo-runtime.io` → `http://localhost:8765`.
Protect the hostname with **Cloudflare Access** before exposing the dashboard.
The current app's configuration endpoints are not independently authenticated.

## 7. Automatic deployment

The workflow already triggers on relevant source changes to `main` and can also be started manually. For a production self-hosted runner, protect `main` or require approval in the GitHub production environment.
