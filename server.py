#!/usr/bin/env python3
"""MSUO WatchDog Live Benchmark — local-only, dependency-free HTTP dashboard."""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from latex_export import render_latex
from pdf_export import render_pdf

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "msuo_live_config.json"
WEB_PATH = ROOT / "web" / "index.html"
LOGO_PATH = ROOT / "web" / "msuo-logo.svg"
VALID_PERSISTENCE = {"direct", "offline"}
VALID_STARTUP = {"auto", "cold", "warm"}
MAX_FILES_PER_SOURCE = 400
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_SOURCES = 12
# Only the salted PBKDF2 verifier is stored in source control, never the password.
SOURCE_PASSWORD_SALT = bytes.fromhex("a98e9ff0c1f663dcb1d7f66d7b75e8d3")
SOURCE_PASSWORD_HASH = bytes.fromhex("3f0c7bb88fac65ccdd613b3b02e9ad92b2d0ad849362f56d68755eefab1fa377")
SOURCE_PASSWORD_ITERATIONS = 600_000
SOURCE_AUTH_LOCK = threading.Lock()
SOURCE_AUTH_FAILURES = {}
SOURCE_AUTH_MAX_FAILURES = 5
SOURCE_AUTH_WINDOW_SECONDS = 120


def check_source_password(password, client_ip):
    """Validate a source-management password, throttling repeated failures per client."""
    now = time.monotonic()
    with SOURCE_AUTH_LOCK:
        recent = [t for t in SOURCE_AUTH_FAILURES.get(client_ip, ()) if now - t < SOURCE_AUTH_WINDOW_SECONDS]
        SOURCE_AUTH_FAILURES[client_ip] = recent
        if len(recent) >= SOURCE_AUTH_MAX_FAILURES:
            return 429
    valid = (
        isinstance(password, str)
        and 0 < len(password) <= 256
        and hmac.compare_digest(
            hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                SOURCE_PASSWORD_SALT, SOURCE_PASSWORD_ITERATIONS),
            SOURCE_PASSWORD_HASH,
        )
    )
    with SOURCE_AUTH_LOCK:
        if valid:
            SOURCE_AUTH_FAILURES.pop(client_ip, None)
            return 200
        SOURCE_AUTH_FAILURES.setdefault(client_ip, []).append(now)
    return 401


TIME_PATTERN = re.compile(r"TimeNow:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3,6})")
SNAPSHOT_PATTERN = re.compile(r"(?=WATCHDOG SNAPSHOT\s*-\s*\d{4}-\d{2}-\d{2})")
OPERATION_PATTERN = re.compile(r"Metadata\s+([\w-]+)\s+Staged", re.IGNORECASE)
LOGIC_NODE_PATTERN = re.compile(r"Logic Node\s+([\w-]+)\s+discovered\b", re.IGNORECASE)
APPLICATION_ACTION_PATTERN = re.compile(r"\b[\w-]+/([\w-]+):", re.IGNORECASE)
LOG_EXTENSIONS = {".txt", ".log", ".trace", ".out"}
LOG_FILENAMES = {"watchdog-log-public"}  # WatchDog writes an extensionless production log
DEFAULT_OPERATIONS = ("SignUp", "SignIn")  # Fixed dropdown order; visible before first matching request
CONFIG_LOCK = threading.RLock()
CACHE_LOCK = threading.Lock()
CACHE = {"at": 0.0, "records": [], "sources": [], "warnings": []}
FILE_CACHE = {}  # (file, source settings) -> (mtime_ns, size, parsed requests)
FILE_CACHE_LOCK = threading.Lock()


def default_config():
    return {"sources": [], "overrides": {}, "operations": []}


def load_config():
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default_config()
        return {
            "sources": raw.get("sources", []) if isinstance(raw.get("sources"), list) else [],
            "overrides": raw.get("overrides", {}) if isinstance(raw.get("overrides"), dict) else {},
            "operations": raw.get("operations", []) if isinstance(raw.get("operations"), list) else [],
        }
    except (OSError, ValueError):
        return default_config()


CONFIG = load_config()


def save_config():
    with CONFIG_LOCK:
        temporary = CONFIG_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, CONFIG_PATH)
    invalidate_cache()


def invalidate_cache():
    with CACHE_LOCK:
        CACHE["at"] = 0.0


def parse_timestamp(s):
    try:
        return datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def validate_operation_mappings(value):
    """Validate exact log action -> dashboard name mappings (literal text, not regex)."""
    if not isinstance(value, list) or len(value) > 40:
        raise ValueError("Expected up to 40 custom operations")
    cleaned, names, actions = [], set(), set()
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"Operation {i+1}: invalid mapping")
        name, action = item.get("name"), item.get("action")
        if not isinstance(name, str) or not isinstance(action, str):
            raise ValueError(f"Operation {i+1}: name and action must be text")
        name, action = name.strip(), action.strip()
        if (not name or not action or len(name) > 80 or len(action) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in name + action)):
            raise ValueError(f"Operation {i+1}: invalid name or log action")
        if name.casefold() == "all":
            raise ValueError("All is a reserved operation name")
        if name.casefold() in names or action.casefold() in actions:
            raise ValueError(f"Operation {i+1}: duplicate name or log action")
        names.add(name.casefold())
        actions.add(action.casefold())
        cleaned.append({"name": name, "action": action})
    return cleaned


def match_custom_operation(raw_operation, messages, mappings):
    """Match a logged action or literal message within the completed lifecycle."""
    for mapping in mappings:
        if raw_operation.casefold() == mapping["action"].casefold():
            return mapping["name"]
    for mapping in mappings:
        action = mapping["action"]
        if re.fullmatch(r"[\w-]+", action):
            # A token must match as a whole action, not part of another identifier.
            pattern = rf"(?<![\w-]){re.escape(action)}(?![\w-])"
            found = any(re.search(pattern, message, re.IGNORECASE) for message in messages)
        else:
            found = any(action.casefold() in message.casefold() for message in messages)
        if found:
            return mapping["name"]
    return None


def extract_events(text, source, filename, operation_mappings=()):
    """Extract completed requests; never expose raw payloads or identity-bearing log lines."""
    parts = SNAPSHOT_PATTERN.split(text.lstrip("\ufeff"))
    if len(parts) == 1:
        parts = [text]
    events = []
    for part in parts:
        if "FULL LOGS" in part:
            part = part.split("FULL LOGS", 1)[1]
        captures = []
        for line in part.splitlines():
            if "TimeNow:" not in line or "|" not in line:
                continue
            match = TIME_PATTERN.search(line)
            if not match:
                continue
            stamp = parse_timestamp(match.group(1))
            if stamp:
                captures.append((line.split("|", 1)[0].strip(), stamp, line))
        if not captures:
            continue
        begins = [(i, t) for i, (m, t, _) in enumerate(captures)
                  if "Kernel Authenticating Application License" in m]
        finishes = [(i, t) for i, (m, t, _) in enumerate(captures)
                    if "Request Lifecycle Completed" in m]
        if not begins or not finishes:
            continue
        # A snapshot normally holds one lifecycle; this also supports concatenated
        # log blocks without WATCHDOG SNAPSHOT headers.
        for start_index, start in begins:
            finish = next(((j, t) for j, t in finishes if j > start_index), None)
            if not finish:
                continue
            end_index, end = finish
            next_start = next((i for i, _ in begins if i > start_index), None)
            if next_start is not None and end_index >= next_start:
                continue
            ms = round((end - start).total_seconds() * 1000)
            if ms < 0:
                continue
            # The action belongs to this request, not the preceding 12 log lines:
            # concatenated snapshots may contain a different earlier operation.
            messages = [x[0] for x in captures[start_index:end_index + 1]]
            operation = "Unknown"
            for message in messages:
                found = OPERATION_PATTERN.search(message)
                if found:
                    operation = found.group(1)
                    break
            if operation == "Unknown":
                for message in messages:
                    found = LOGIC_NODE_PATTERN.search(message)
                    if found:
                        operation = found.group(1)
                        break
            if operation == "Unknown":
                for message in messages:
                    found = APPLICATION_ACTION_PATTERN.search(message)
                    if found:
                        operation = found.group(1)
                        break
            raw_operation = operation
            lower_messages = "\n".join(messages).lower()
            if operation.lower() == "updatedashboard":
                mutation = any(x in lower_messages for x in (
                    "adding or replacing dashboard data", "crud operation succesfully completed",
                    "crud operation successfully completed", "persisting updated dashboard data",
                ))
                operation = "UpdateDashboard CRUD" if mutation else "UpdateDashboard Read"
            elif operation.lower() == "signin" and "sign-in rejected" in lower_messages:
                operation = "SignIn Rejected"
            has_ctor = any(("method: .ctor" in raw.lower() and "baseentity" in raw.lower())
                           for _, _, raw in captures[:start_index])
            # In an individual snapshot, the constructor is before ingress.
            # Without a snapshot boundary, do not borrow the constructor from a
            # previous lifecycle in the same file.
            if len(parts) == 1 and len(begins) > 1:
                previous_finish = max((i for i, _ in finishes if i < start_index), default=-1)
                has_ctor = any("method: .ctor" in raw.lower() and "baseentity" in raw.lower()
                               for _, _, raw in captures[previous_finish + 1:start_index])
            auto_startup = "cold" if has_ctor else "warm"
            evidence = "BaseEntity constructor observed" if has_ctor else "No constructor capture (heuristic)"
            ctor_gap = None
            if has_ctor:
                constructors = [t for m, t, raw in captures[:start_index]
                                if "method: .ctor" in raw.lower() and "baseentity" in raw.lower()]
                if constructors:
                    ctor_gap = round((start - constructors[-1]).total_seconds() * 1000)
            src_mode = source.get("startup", "auto")
            startup = src_mode if src_mode in {"cold", "warm"} else auto_startup
            if src_mode in {"cold", "warm"}:
                evidence = "Source startup override"
            signature = f"{source['path']}|{start.isoformat()}|{end.isoformat()}|{operation}|{ms}"
            event_id = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]
            # Keep event IDs stable when a user changes a dashboard alias.
            # Search only the active lifecycle, not preceding captures.
            action_messages = [x[0] for x in captures[start_index:end_index + 1]]
            custom_name = match_custom_operation(raw_operation, action_messages, operation_mappings)
            if custom_name is not None:
                operation = custom_name
            events.append({
                "id": event_id, "source": source.get("name") or Path(source["path"]).name,
                "filename": filename, "persistence": source["persistence"],
                "startup": startup, "startup_evidence": evidence,
                "auto_startup": auto_startup, "operation": operation,
                "duration_ms": ms, "start_utc": start.isoformat(),
                "end_utc": end.isoformat(), "constructor_to_ingress_ms": ctor_gap,
            })
    return events


def read_log(path):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size > MAX_FILE_BYTES:
            f.seek(-MAX_FILE_BYTES, os.SEEK_END)
            f.readline()  # discard truncated leading line
        else:
            f.seek(0)
        return f.read().decode("utf-8-sig", errors="replace")


def files_in_source(path, filenames=""):
    """Discover supported logs, or exact comma-separated filenames when configured."""
    selected = {name.strip().casefold() for name in filenames.split(",") if name.strip()}
    if path.is_file():
        return [path] if not selected or path.name.casefold() in selected else []
    found = []
    for base, folders, files in os.walk(path, followlinks=False):
        folders[:] = [x for x in folders if not x.startswith(".")]
        for name in files:
            if selected:
                matches = name.casefold() in selected
            else:
                matches = (Path(name).suffix.lower() in LOG_EXTENSIONS
                           or name.casefold() in LOG_FILENAMES)
            if matches:
                candidate = Path(base) / name
                try:
                    found.append((candidate.stat().st_mtime, candidate))
                except OSError:
                    pass
        if len(found) > MAX_FILES_PER_SOURCE * 3:
            break
    found.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in found[:MAX_FILES_PER_SOURCE]]


def parse_file_cached(file, src, operation_mappings=()):
    """Reparse only when an SMB log file or its operation mapping changes."""
    stat = file.stat()
    mapping_key = tuple((item["name"], item["action"]) for item in operation_mappings)
    cache_key = (str(file), src.get("name"), src["persistence"], src.get("startup", "auto"), mapping_key)
    signature = (stat.st_mtime_ns, stat.st_size)
    with FILE_CACHE_LOCK:
        cached = FILE_CACHE.get(cache_key)
        if cached and cached[:2] == signature:
            return cached[2]
    parsed = extract_events(read_log(file), src, file.name, operation_mappings)
    with FILE_CACHE_LOCK:
        if len(FILE_CACHE) > 1500:
            FILE_CACHE.clear()
        FILE_CACHE[cache_key] = (*signature, parsed)
    return parsed


def scan_all():
    with CONFIG_LOCK:
        sources = [dict(s) for s in CONFIG.get("sources", [])]
        overrides = dict(CONFIG.get("overrides", {}))
        operation_mappings = [dict(item) for item in CONFIG.get("operations", [])
                              if isinstance(item, dict) and "name" in item and "action" in item]
    records, statuses, warnings = [], [], []
    seen = set()
    for src in sources:
        path_str = src.get("path", "")
        path = Path(path_str)
        status = {"name": src.get("name", "Source"), "persistence": src.get("persistence"),
                  "path": path_str, "files": 0, "requests": 0, "ok": False}
        if not path.exists():
            warnings.append(f"{status['name']}: folder/file not accessible: {path_str}")
            statuses.append(status)
            continue
        try:
            paths = files_in_source(path, src.get("filenames", ""))
            status["files"] = len(paths)
            status["ok"] = True
            for file in paths:
                try:
                    parsed = parse_file_cached(file, src, operation_mappings)
                    for original_event in parsed:
                        event = dict(original_event)  # overrides must not mutate cached parse results
                        if event["id"] in seen:
                            continue
                        seen.add(event["id"])
                        override = overrides.get(event["id"])
                        if override in {"cold", "warm"}:
                            event["startup"] = override
                            event["startup_evidence"] = "Per-request reviewer override"
                        records.append(event)
                        status["requests"] += 1
                except (OSError, UnicodeError) as exc:
                    warnings.append(f"{status['name']}: could not read {file.name}: {exc}")
        except OSError as exc:
            status["ok"] = False
            warnings.append(f"{status['name']}: {exc}")
        statuses.append(status)
    records.sort(key=lambda e: e["end_utc"], reverse=True)
    return records, statuses, warnings


def get_scan():
    with CACHE_LOCK:
        if time.monotonic() - CACHE["at"] < 1.5:
            return CACHE["records"], CACHE["sources"], CACHE["warnings"]
    records, statuses, warnings = scan_all()
    with CACHE_LOCK:
        CACHE.update(at=time.monotonic(), records=records, sources=statuses, warnings=warnings)
    return records, statuses, warnings


def percentile(values, q):
    sorted_values = sorted(values)
    if not sorted_values:
        return None
    index = (len(sorted_values) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (index - lo)


def available_operations(records, custom_mappings=()):
    """Built-in SignUp and SignIn first; then observed/custom actions alphabetically."""
    choices = {record["operation"] for record in records}
    choices.update(mapping["name"] for mapping in custom_mappings
                   if isinstance(mapping, dict) and isinstance(mapping.get("name"), str))
    ordered = []
    for builtin in DEFAULT_OPERATIONS:
        # Keep an existing alias's exact spelling so selecting it filters records.
        matching = sorted((name for name in choices if name.casefold() == builtin.casefold()),
                          key=lambda name: (name != builtin, name))
        ordered.append(matching[0] if matching else builtin)
        choices = {name for name in choices if name.casefold() != builtin.casefold()}
    return ordered + sorted(choices, key=str.casefold)


def compute_data(operation="all", window=50):
    records, statuses, warnings = get_scan()
    filtered = records if operation == "all" else [r for r in records if r["operation"] == operation]
    groups = []
    for startup, persistence in (("cold", "offline"), ("cold", "direct"),
                                 ("warm", "direct"), ("warm", "offline")):
        matching = [r for r in filtered if r["startup"] == startup and r["persistence"] == persistence]
        selected = matching if window == 0 else matching[:window]
        values = [r["duration_ms"] for r in selected]
        if values:
            mean = statistics.mean(values)
            latest = values[0]
            delta = latest - mean
            groups.append({
                "startup": startup, "persistence": persistence,
                "label": f"{startup.title()} / {'Direct cloud' if persistence == 'direct' else 'Offline-first'}",
                "n": len(values), "total_n": len(matching),
                "latest_ms": latest, "fastest_ms": min(values), "mean_ms": round(mean, 2),
                "median_ms": round(statistics.median(values), 2),
                "p95_ms": round(percentile(values, .95), 2),
                "stddev_ms": round(statistics.pstdev(values), 2),
                "divergence_ms": round(delta, 2),
                "divergence_pct": round(delta / mean * 100, 2) if mean else None,
                "latest_utc": selected[0]["end_utc"],
            })
        else:
            groups.append({"startup": startup, "persistence": persistence,
                           "label": f"{startup.title()} / {'Direct cloud' if persistence == 'direct' else 'Offline-first'}",
                           "n": 0, "total_n": 0, "latest_ms": None, "fastest_ms": None, "mean_ms": None,
                           "median_ms": None, "p95_ms": None, "stddev_ms": None,
                           "divergence_ms": None, "divergence_pct": None, "latest_utc": None})
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "total_requests": len(records),
        "operation_requests": len(filtered),
        "operations": available_operations(records, CONFIG.get("operations", [])),
        "selected_operation": operation, "window": window,
        "groups": groups, "recent": filtered[:50],
        "sources": statuses, "warnings": warnings,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MSUO-Live/1.0"

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/data"):
            print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_bytes(self, payload, content_type="application/json; charset=utf-8", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status=status)

    def require_source_password(self):
        status = check_source_password(
            self.headers.get("X-MSUO-Source-Password"), self.client_address[0]
        )
        if status == 200:
            return True
        message = ("Too many incorrect attempts. Try again in two minutes."
                   if status == 429 else "Incorrect or missing configuration password.")
        self.send_json({"error": message}, status)
        return False

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 100_000:
            raise ValueError("Invalid request body length")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            self.send_bytes(WEB_PATH.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/assets/msuo-logo.svg":
            self.send_bytes(LOGO_PATH.read_bytes(), "image/svg+xml; charset=utf-8")
        elif url.path == "/api/config":
            if not self.require_source_password():
                return
            with CONFIG_LOCK:
                self.send_json({"sources": CONFIG.get("sources", [])})
        elif url.path == "/api/operations":
            with CONFIG_LOCK:
                self.send_json({"operations": CONFIG.get("operations", [])})
        elif url.path == "/api/data":
            args = parse_qs(url.query)
            operation = args.get("operation", ["all"])[0]
            try:
                window = int(args.get("window", ["50"])[0])
            except ValueError:
                window = 50
            window = window if window in (0, 10, 20, 50, 100, 500) else 50
            self.send_json(compute_data(operation, window))
        elif url.path == "/api/export.csv":
            records, _, _ = get_scan()
            output = io.StringIO()
            cols = ("id", "source", "operation", "startup", "startup_evidence", "persistence",
                    "duration_ms", "start_utc", "end_utc", "constructor_to_ingress_ms")
            writer = csv.DictWriter(output, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
            data = output.getvalue().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="msuo_watchdog_measurements.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif url.path == "/api/export.tex":
            args = parse_qs(url.query)
            operation = args.get("operation", ["all"])[0]
            try:
                window = int(args.get("window", ["50"])[0])
            except ValueError:
                window = 50
            window = window if window in (0, 10, 20, 50, 100, 500) else 50
            payload = render_latex(compute_data(operation, window)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tex; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="msuo_diagnostics_tables.tex"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif url.path == "/api/export.pdf":
            args = parse_qs(url.query)
            operation = args.get("operation", ["all"])[0]
            try:
                window = int(args.get("window", ["50"])[0])
            except ValueError:
                window = 50
            window = window if window in (0, 10, 20, 50, 100, 500) else 50
            payload = render_pdf(compute_data(operation, window))
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="msuo_diagnostics_report.pdf"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif url.path == "/api/health":
            self.send_json({"status": "ok"})
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/api/config" and not self.require_source_password():
            return
        try:
            payload = self.read_json()
            if self.path == "/api/config":
                sources = payload.get("sources")
                if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
                    raise ValueError("Expected a list of up to 12 sources")
                cleaned = []
                for i, src in enumerate(sources):
                    if not isinstance(src, dict):
                        raise ValueError("Invalid source")
                    path = str(src.get("path", "")).strip().strip('"')
                    persistence = src.get("persistence", "")
                    startup = src.get("startup", "auto")
                    filenames = src.get("filenames", "")
                    if not path or persistence not in VALID_PERSISTENCE or startup not in VALID_STARTUP:
                        raise ValueError(f"Source {i+1}: path, persistence, or startup invalid")
                    if not isinstance(filenames, str) or len(filenames) > 2000:
                        raise ValueError(f"Source {i+1}: invalid log filenames")
                    selected = [name.strip() for name in filenames.split(",") if name.strip()]
                    if len(selected) > 20 or any(
                        name in (".", "..") or len(name) > 255
                        or any(char in name for char in '<>:"/\\|?*')
                        for name in selected
                    ):
                        raise ValueError(f"Source {i+1}: enter up to 20 plain filenames, separated by commas")
                    cleaned.append({"name": str(src.get("name", "Source"))[:80],
                                    "path": path, "persistence": persistence, "startup": startup,
                                    "filenames": ", ".join(selected)})
                with CONFIG_LOCK:
                    CONFIG["sources"] = cleaned
                save_config()
                self.send_json({"ok": True})
            elif self.path == "/api/operations":
                operations = validate_operation_mappings(payload.get("operations"))
                with CONFIG_LOCK:
                    CONFIG["operations"] = operations
                save_config()
                self.send_json({"ok": True, "operations": operations})
            elif self.path == "/api/override":
                event_id = str(payload.get("id", ""))
                startup = payload.get("startup")
                if not re.fullmatch(r"[a-f0-9]{20}", event_id):
                    raise ValueError("Invalid request ID")
                if startup not in ("auto", "cold", "warm"):
                    raise ValueError("Startup must be auto, cold, or warm")
                with CONFIG_LOCK:
                    if startup == "auto":
                        CONFIG["overrides"].pop(event_id, None)
                    else:
                        CONFIG["overrides"][event_id] = startup
                save_config()
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "Not found"}, 404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)


def main():
    parser = argparse.ArgumentParser(description="MSUO WatchDog live benchmark dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--direct", help="Direct-cloud log folder or file (UNC or mapped drive)")
    parser.add_argument("--offline", help="Offline-first log folder or file (UNC or mapped drive)")
    parser.add_argument("--demo", action="store_true", help="Use bundled anonymized WatchDog examples")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.demo:
        with CONFIG_LOCK:
            CONFIG["sources"] = [
                {"name": "EntryFlow (example)", "path": str(ROOT / "sample_logs" / "EntryFlow"),
                 "persistence": "offline", "startup": "auto"},
                {"name": "OptiSpec (example)", "path": str(ROOT / "sample_logs" / "OptiSpec"),
                 "persistence": "direct", "startup": "auto"},
            ]
        # The historical 2,935 ms OptiSpec trace was manually labeled Cold* in
        # the paper's diagram, although it lacks constructor evidence. Preserve
        # that explicit research label in demo mode, not as an automatic inference.
        demo_source = CONFIG["sources"][1]
        demo_file = ROOT / "sample_logs" / "OptiSpec" / "optispec_watchdog.txt"
        for sample in extract_events(demo_file.read_text(encoding="utf-8"), demo_source, demo_file.name):
            if sample["duration_ms"] == 2935:
                CONFIG["overrides"][sample["id"]] = "cold"
        invalidate_cache()
    elif args.direct or args.offline:
        new_sources = []
        if args.direct:
            new_sources.append({"name": "Direct cloud", "path": args.direct,
                                "persistence": "direct", "startup": "auto"})
        if args.offline:
            new_sources.append({"name": "Offline-first", "path": args.offline,
                                "persistence": "offline", "startup": "auto"})
        with CONFIG_LOCK:
            CONFIG["sources"] = new_sources
        save_config()
    host = "127.0.0.1"
    url = f"http://{host}:{args.port}"
    httpd = ThreadingHTTPServer((host, args.port), Handler)
    print("\nMSUO WatchDog Live Benchmark")
    print("--------------------------------")
    print(f"Dashboard: {url}")
    print("Connect Tailscale and ensure the Windows share is accessible before adding it.")
    print("Folder access uses your existing Windows credentials; no VPN password is stored.")
    print("Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()