"""Read each app's configured WatchDog files and report QA evidence counts only."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import SNAPSHOT_PATTERN, TIME_PATTERN, files_in_source, parse_timestamp, read_log


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--center", required=True)
    parser.add_argument("--entry-id", required=True)
    parser.add_argument("--device-id", required=True)
    args = parser.parse_args()
    center = datetime.fromisoformat(args.center.replace("Z", "+00:00")).astimezone(timezone.utc)
    window = timedelta(seconds=90)
    root = Path(os.getenv("MSUO_DEPLOY_DIR") or r"C:\MSUO\Observatory")
    config = json.loads((root / "msuo_live_config.json").read_text(encoding="utf-8-sig"))
    for app, identifier in (("EntryFlow", args.entry_id), ("OptiSpec", args.device_id)):
        files = set()
        for source in config.get("sources", []):
            if not isinstance(source, dict) or not source.get("path"):
                continue
            location = Path(source["path"])
            hint = re.sub(r"[^a-z0-9]", "", (str(source.get("name", "")) + str(location)).casefold())
            if app.casefold() in hint:
                files.update(files_in_source(location, source.get("filenames") or ""))
        lifecycle_count = 0
        matching_id = 0
        matching_removal = 0
        completed_removals = 0
        operations = {}
        opened = 0
        for path in files:
            try:
                content = read_log(path)
            except OSError:
                continue
            opened += 1
            for block in SNAPSHOT_PATTERN.split(content):
                if "Request Lifecycle Completed" not in block:
                    continue
                match = TIME_PATTERN.search(block)
                timestamp = parse_timestamp(match.group(1)) if match else None
                if not timestamp or abs(timestamp - center) > window:
                    continue
                lifecycle_count += 1
                operation = re.search(r"Metadata\s+(SignIn|UpdateDashboard|RemoveDevice|RemoveEntry)\s+Staged",
                                      block, re.IGNORECASE)
                if operation:
                    key = operation.group(1)
                    operations[key] = operations.get(key, 0) + 1
                removal = re.search(r"\b(?:removing|deleting|removal|delete)\b", block, re.IGNORECASE)
                success = re.search(r"CRUD Operation succe[s]+fully completed", block, re.IGNORECASE)
                if removal and success:
                    completed_removals += 1
                if re.search(r"(?<![\w-])" + re.escape(identifier) + r"(?![\w-])", block):
                    matching_id += 1
                    matching_removal += int(bool(removal and success))
        print(f"{app}: readable app logs={opened}, completed lifecycles in time window={lifecycle_count}, "
              f"removal success signals={completed_removals}, lifecycles naming the test ID={matching_id}, "
              f"ID-matched successful removal logs={matching_removal}, operations={json.dumps(operations)}")


if __name__ == "__main__":
    main()
