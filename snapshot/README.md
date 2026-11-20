# MSUO Live Execution Snapshot (GitHub Pages)

The Windows self-hosted GitHub Actions runner polls the locally running MSUO
Observatory every five minutes, and immediately on relevant source changes or
manual workflow dispatch. It downloads the **all operations / all requests**
PDF only if the computed measurements or report code changed. It overwrites
`MSUO-Snapshot.pdf` in the `gh-pages` branch, and keeps a version marker.

The `gh-pages` branch contains only the static PDF viewer, the PDF, logo,
and version/timestamp files. It does **not** publish raw WatchDog logs, the
server configuration, source code, or secrets.

## Enable GitHub Pages (one-time repository setting)

Open repository **Settings → Pages → Build and deployment** and choose
**Deploy from a branch**, **gh-pages**, **/(root)**, then **Save**.

Default address (once enabled):
`https://reza91ag.github.io/MSUODiagnostics-OpenAI-Automated/`

For `snapshot.msuo-runtime.io`, set **Custom domain** in GitHub Pages and
add a Cloudflare DNS CNAME `snapshot` pointing to `reza91ag.github.io`.
Use DNS-only until GitHub validates the domain and provisions HTTPS. The
`CNAME` file is intentionally not installed until the domain is configured
so the default GitHub Pages URL remains usable.

The viewer follows the MSUO Developer API documentation design, with
inline browser PDF viewer, Open PDF, and Download controls. It checks for
a newer published version once per minute and reloads the PDF when needed.

Notes:
- GitHub Actions scheduled jobs have a minimum five-minute interval and can
  run later under GitHub scheduling load. This is polling, not an instant
  file-system trigger.
- The PDF is publicly accessible on GitHub Pages even when the repository
  source is private. Check that its measurement metadata is intended for
  publication.
- GitHub Pages for a private repository depends on the account's GitHub plan.
- If the Observatory is offline, the job fails without overwriting the last
  published PDF.
