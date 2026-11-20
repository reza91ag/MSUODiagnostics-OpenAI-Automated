"""Publish a single updated Observatory PDF to the gh-pages branch via GitHub REST.

Runs on the existing Windows self-hosted Actions runner without requiring Git
to be installed on the server. Only the PDF and static viewer are published.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote
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


class GithubAPI:
    def __init__(self, repo: str, token: str):
        if not token:
            raise ValueError("GH_TOKEN is required to publish a snapshot")
        self.base = "https://api.github.com/repos/" + repo
        self.headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "MSUO-Snapshot-Publisher/1.0",
        }

    def request(self, method: str, path: str, payload=None, allow_404=False):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=45) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            raise RuntimeError(f"GitHub API {method} {path} failed ({exc.code}): {detail}") from exc

    def current_version(self):
        result = self.request("GET", "/contents/snapshot-version.txt?ref=gh-pages", allow_404=True)
        if not result:
            return ""
        if result.get("encoding") != "base64":
            return ""
        return base64.b64decode(result["content"]).decode("ascii").strip()

    def add_blob(self, content: bytes):
        return self.request("POST", "/git/blobs", {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })["sha"]

    def commit_files(self, files: dict[str, bytes]):
        ref = self.request("GET", "/git/ref/heads/gh-pages")
        parent = ref["object"]["sha"]
        base_tree = self.request("GET", "/git/commits/" + parent)["tree"]["sha"]
        entries = []
        for path, content in files.items():
            entries.append({"path": path, "mode": "100644", "type": "blob",
                            "sha": self.add_blob(content)})
        tree = self.request("POST", "/git/trees", {"base_tree": base_tree, "tree": entries})["sha"]
        commit = self.request("POST", "/git/commits", {
            "message": "Update MSUO execution snapshot [skip ci]",
            "tree": tree,
            "parents": [parent],
        })["sha"]
        self.request("PATCH", "/git/refs/heads/gh-pages", {"sha": commit, "force": False})
        return commit


def publish(source: Path, github: GithubAPI, base_url: str = BASE_URL) -> bool:
    data = json.loads(fetch_bytes(base_url + "/api/data?operation=all&window=0").decode("utf-8"))
    new_signature = signature(data, source)
    if github.current_version() == new_signature:
        print("Snapshot unchanged; existing published PDF retained.")
        return False

    pdf = fetch_bytes(base_url + "/api/export.pdf?operation=all&window=0", timeout=60)
    validate_pdf(pdf)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    files = {
        PDF_NAME: pdf,
        "snapshot-version.txt": (new_signature + "\n").encode("ascii"),
        "snapshot-updated.txt": (timestamp + "\n").encode("ascii"),
        ".nojekyll": b"",
    }
    for relative, destination in STATIC_FILES.items():
        files[destination] = (source / relative).read_bytes()
    commit = github.commit_files(files)
    print(f"Published snapshot {commit}: {data.get('total_requests', 0)} completed lifecycles; {len(pdf)} PDF bytes.")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("."))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()
    if not args.repo or "/" not in args.repo:
        parser.error("Provide --repo owner/name or GITHUB_REPOSITORY")
    github = GithubAPI(args.repo, os.environ.get("GH_TOKEN", ""))
    publish(args.source.resolve(), github, args.base_url.rstrip("/"))


if __name__ == "__main__":
    main()
