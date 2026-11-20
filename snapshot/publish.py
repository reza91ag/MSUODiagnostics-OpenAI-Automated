"""Publish a single updated Observatory PDF into the GitHub Pages checkout.

Called by a self-hosted Windows GitHub Actions runner. Publishes only the
measurement PDF and static viewer, never raw WatchDog logs or configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import Request, urlopen

PDF_NAME = "MSUO-Snapshot.pdf"
STATIC_FILES = {
    "snapshot/index.html": "index.html",
    "web/msuo-logo.svg": "msuo-logo.svg",
}
SIGNATURE_FILES = (
    "snapshot/index.html",
    "snapshot/publish.py",
    "web/msuo-logo.svg",
    "pdf_export.py",
    "server.py",
)
BASE_URL = "http://127.0.0.1:8765"


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    request = Request(url, headers={"User-Agent": "MSUO-Snapshot-Publisher/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def signature(data: dict, source: Path) -> str:
    """Ignore generated_utc so unchanged measurements do not redeploy."""
    stable = dict(data)
    stable.pop("generated_utc", None)
    digest = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    for relative in SIGNATURE_FILES:
        file = source / relative
        digest.update(relative.encode("utf-8"))
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()


def validate_pdf(pdf: bytes) -> None:
    if len(pdf) < 500 or not pdf.startswith(b"%PDF-") or b"%%EOF" not in pdf[-128:]:
        raise ValueError("The Observatory returned an invalid or incomplete PDF")


def write_changed(path: Path, data: bytes) -> None:
    if path.is_file() and path.read_bytes() == data:
        return
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def publish(source: Path, site: Path, base_url: str = BASE_URL) -> bool:
    data = json.loads(fetch_bytes(base_url + "/api/data?operation=all&window=0").decode("utf-8"))
    new_signature = signature(data, source)
    version_path = site / "snapshot-version.txt"
    pdf_path = site / PDF_NAME
    existing = version_path.read_text(encoding="ascii").strip() if version_path.exists() else ""
    static_ready = all((site / destination).is_file() for destination in STATIC_FILES.values())
    if existing == new_signature and pdf_path.is_file() and static_ready:
        print("Snapshot unchanged; no Pages commit needed.")
        return False

    pdf = fetch_bytes(base_url + "/api/export.pdf?operation=all&window=0", timeout=60)
    validate_pdf(pdf)
    site.mkdir(parents=True, exist_ok=True)
    for relative, destination in STATIC_FILES.items():
        write_changed(site / destination, (source / relative).read_bytes())
    write_changed(site / ".nojekyll", b"")
    write_changed(pdf_path, pdf)
    write_changed(version_path, (new_signature + "\n").encode("ascii"))
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    write_changed(site / "snapshot-updated.txt", (timestamp + "\n").encode("ascii"))
    print(f"Snapshot refreshed: {data.get('total_requests', 0)} completed lifecycles; {len(pdf)} PDF bytes.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()
    publish(args.source.resolve(), args.site.resolve(), args.base_url.rstrip("/"))


if __name__ == "__main__":
    main()
