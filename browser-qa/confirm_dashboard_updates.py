"""Verify browser actions against the original application logs."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import extract_events, files_in_source, read_log


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def completed_updates(config, app):
    updates = {}
    for source in config.get("sources", []):
        if not isinstance(source, dict) or not source.get("path"):
            continue
        location = Path(source["path"])
        hint = re.sub(r"[^a-z0-9]", "", (str(source.get("name", "")) + str(location)).casefold())
        if app.casefold() not in hint:
            continue
        for log in files_in_source(location, source.get("filenames") or ""):
            try:
                events = extract_events(read_log(log), source, log.name)
            except OSError:
                continue
            for event in events:
                if event["operation"].casefold() in {"updatedashboard", "updatedashboard crud", "updatedashboard read"}:
                    updates[event["id"]] = event
    return list(updates.values())


def check(config, app, windows):
    pending = [
        (window["command"], parse_time(window["sentAt"]), parse_time(window["endAt"]))
        for window in windows
    ]
    used = set()
    for attempt in range(5):
        events = sorted(completed_updates(config, app), key=lambda event: event["start_utc"])
        unmatched = []
        for command, sent, received in pending:
            for event in events:
                if event["id"] in used:
                    continue
                started = parse_time(event["start_utc"])
                ended = parse_time(event["end_utc"])
                if (sent - timedelta(seconds=1) <= started <= ended <= received + timedelta(seconds=1)
                        and ended >= sent):
                    used.add(event["id"])
                    print(f"{app} {command}: confirmed completed UpdateDashboard in app log")
                    break
            else:
                unmatched.append((command, sent, received))
        pending = unmatched
        if not pending:
            return True
        if attempt < 4:
            time.sleep(3)
    for command, _, _ in pending:
        print(f"::error::{app} {command}: no matching completed UpdateDashboard in app log")
    return False


def main():
    root = Path(os.getenv("MSUO_DEPLOY_DIR") or r"C:\MSUO\Observatory")
    config = json.loads((root / "msuo_live_config.json").read_text(encoding="utf-8-sig"))
    ok = True
    for app in ("EntryFlow", "OptiSpec"):
        payload = os.getenv(app.upper() + "_WINDOWS")
        if payload:
            ok = check(config, app, json.loads(payload)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
