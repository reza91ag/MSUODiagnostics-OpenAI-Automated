import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


class WatchDogParserTests(unittest.TestCase):
    def parse(self, folder, persistence):
        path = next((ROOT / "sample_logs" / folder).glob("*.txt"))
        return server.extract_events(path.read_text(encoding="utf-8"), {
            "name": folder, "path": str(path.parent),
            "persistence": persistence, "startup": "auto",
        }, path.name)

    def test_entryflow_all_six_lifecycles(self):
        events = self.parse("EntryFlow", "offline")
        self.assertEqual(len(events), 6)
        self.assertEqual([e["duration_ms"] for e in events], [4239, 2039, 2081, 4125, 2050, 153])
        self.assertEqual([e["startup"] for e in events], ["cold"] * 5 + ["warm"])
        self.assertEqual(events[-1]["operation"], "UpdateDashboard CRUD")
        self.assertEqual(events[2]["operation"], "SignIn Rejected")
        self.assertEqual(events[0]["constructor_to_ingress_ms"], 8111)

    def test_optispec_both_lifecycles(self):
        events = self.parse("OptiSpec", "direct")
        self.assertEqual(len(events), 2)
        self.assertEqual([e["duration_ms"] for e in events], [2935, 2153])
        self.assertEqual(events[0]["operation"], "UpdateDashboard Read")
        self.assertEqual(events[1]["operation"], "UpdateDashboard CRUD")
        self.assertEqual([e["startup"] for e in events], ["warm", "warm"])

    def test_statistics_and_divergence(self):
        original = server.CONFIG
        try:
            server.CONFIG = {"sources": [
                {"name": "EntryFlow", "path": str(ROOT / "sample_logs" / "EntryFlow"),
                 "persistence": "offline", "startup": "auto"},
                {"name": "OptiSpec", "path": str(ROOT / "sample_logs" / "OptiSpec"),
                 "persistence": "direct", "startup": "auto"},
            ], "overrides": {}}
            server.invalidate_cache()
            d = server.compute_data("all", 50, ignore_outliers=False)
            self.assertEqual(d["total_requests"], 5)
            self.assertEqual(sum(g["n"] for g in d["groups"]), 5)
            self.assertNotIn("UpdateDashboard Read", {r["operation"] for r in d["export_records"]})
            cold_offline = next(g for g in d["groups"] if g["startup"] == "cold" and g["persistence"] == "offline")
            self.assertGreater(cold_offline["n"], 0)
            warm_offline = next(g for g in d["groups"] if g["startup"] == "warm" and g["persistence"] == "offline")
            self.assertEqual(warm_offline["latest_ms"], 153)
            self.assertEqual(warm_offline["divergence_pct"], 0.0)
        finally:
            server.CONFIG = original
            server.invalidate_cache()


    def test_multiple_operation_selection_filters_all_statistics(self):
        original = server.CONFIG
        try:
            server.CONFIG = {"sources": [
                {"name": "EntryFlow", "path": str(ROOT / "sample_logs" / "EntryFlow"), "persistence": "offline", "startup": "auto"},
                {"name": "OptiSpec", "path": str(ROOT / "sample_logs" / "OptiSpec"), "persistence": "direct", "startup": "auto"},
            ], "overrides": {}}
            server.invalidate_cache()
            one = server.compute_data(["UpdateDashboard CRUD"], 50, ignore_outliers=False)
            both = server.compute_data(["UpdateDashboard CRUD", "UpdateDashboard Read"], 50, ignore_outliers=False)
            self.assertEqual(one["operation_requests"], both["operation_requests"])
            self.assertEqual(one["included_count"], one["operation_requests"])
            self.assertEqual(both["included_count"], both["operation_requests"])
            self.assertEqual({r["operation"] for r in both["export_records"]}, {"UpdateDashboard CRUD"})
            self.assertEqual(sum(g["n"] for g in both["groups"]), both["included_count"])
            self.assertEqual(both["selected_operations"], ["UpdateDashboard CRUD", "UpdateDashboard Read"])
        finally:
            server.CONFIG = original
            server.invalidate_cache()

    def test_dashboard_read_excluded_from_benchmarks_and_operations(self):
        from unittest.mock import patch
        records = [
            {"id": "read", "operation": "UpdateDashboard Read", "startup": "warm",
             "persistence": "direct", "duration_ms": 2935,
             "end_utc": "2026-11-20T12:00:03+00:00"},
            {"id": "signup", "operation": "SignUp", "startup": "cold",
             "persistence": "direct", "duration_ms": 4239,
             "end_utc": "2026-11-20T12:00:02+00:00"},
        ]
        self.assertNotIn("UpdateDashboard Read", server.available_operations(
            records, [{"name": "UpdateDashboard Read", "action": "UpdateDashboard"}]))
        with patch.object(server, "get_scan", return_value=(records, [], [])):
            for ignore_outliers in (False, True):
                result = server.compute_data("all", 0, ignore_outliers=ignore_outliers)
                self.assertEqual(result["total_requests"], 1)
                self.assertEqual(result["included_count"], 1)
                self.assertEqual(result["export_records"][0]["operation"], "SignUp")
                self.assertEqual(result["export_records"][0]["duration_ms"], 4239)
                self.assertNotIn("UpdateDashboard Read",
                                 {row["operation"] for row in result["recent"]})

    def test_latency_health_states_and_operation_localization(self):
        from datetime import datetime, timedelta, timezone
        def samples(operations, excluded):
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            rows = [{"id": str(i), "operation": op,
                     "end_utc": (start + timedelta(seconds=i)).isoformat(),
                     "source": "App", "startup": "warm", "persistence": "direct"}
                    for i, op in enumerate(operations)]
            return server.latency_health(rows, {str(i) for i in excluded})
        self.assertEqual(samples(["A"] * 30, [])["status"], "healthy")
        self.assertEqual(samples(["A"] * 30, [2, 13, 27])["status"], "sporadic")
        temporal = samples(["A"] * 30, [15, 16, 18])
        self.assertEqual(temporal["status"], "localized")
        self.assertTrue(temporal["temporal_cluster"])
        operational = samples(["A" if i % 2 == 0 else "B" for i in range(30)],
                              [0, 6, 12, 18])
        self.assertEqual(operational["status"], "localized")
        self.assertTrue(operational["operational_cluster"])
        self.assertFalse(operational["temporal_cluster"])
        self.assertEqual(operational["affected_operations"][0]["operation"], "A")

    def test_outliers_use_one_baseline_across_selected_records(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        records = []
        for i, (duration, operation, startup, persistence) in enumerate([
            (35, "SignIn", "warm", "offline"),
            (51, "Dashboard", "warm", "offline"),
            (67, "SignIn", "warm", "offline"),
            (100, "Dashboard", "warm", "offline"),
            (110, "SignIn", "warm", "offline"),
            (120, "Dashboard", "warm", "offline"),
            (153, "SignIn", "warm", "offline"),
            (2043, "Dashboard", "warm", "offline"),
            (2049, "SignIn", "warm", "offline"),
            (2208, "Dashboard", "warm", "offline"),
            (50000, "Other", "cold", "direct"),
        ]):
            records.append({"id": str(i), "source": "AppA" if i % 2 else "AppB",
                "operation": operation, "startup": startup, "persistence": persistence,
                "duration_ms": duration,
                "end_utc": (start + timedelta(seconds=i)).isoformat()})
        records.reverse()
        with patch.object(server, "get_scan", return_value=(records, [], [])):
            result = server.compute_data(["SignIn", "Dashboard"], 0,
                ignore_outliers=False, configurations=["warm:offline"])
        self.assertEqual(result["selected_count"], 10)
        self.assertEqual(result["health"]["recent_requests"], 10)
        flags = {r["duration_ms"]: r["is_outlier"] for r in result["recent"]}
        self.assertFalse(flags[35])
        self.assertFalse(flags[153])
        for low in flags:
            for high in flags:
                if low < high and flags[low]:
                    self.assertTrue(flags[high])
        self.assertEqual(server.remove_latency_outliers(
            [{"duration_ms": v} for v in [35, 100, 101, 102, 103, 104, 105, 106]])[1], 0)

    def test_ignore_outliers_excludes_health_detected_anomalies(self):
        from unittest.mock import patch
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        records = []
        for startup, persistence, values in [
            ("warm", "direct", [100, 101, 102, 103, 104, 105, 106, 1000]),
            ("cold", "offline", [10000, 10001, 10002, 10003, 10004, 10005, 10006, 10007]),
        ]:
            for value in values:
                i = len(records)
                records.append({"id": str(i), "operation": "SignIn",
                    "startup": startup, "persistence": persistence,
                    "duration_ms": value,
                    "end_utc": (start + timedelta(seconds=i)).isoformat()})
        records.reverse()
        with patch.object(server, "get_scan", return_value=(records, [], [])):
            actual = server.compute_data("all", 0, ignore_outliers=True)
            unfiltered = server.compute_data("all", 0, ignore_outliers=False)
        # Health flags the warm/direct anomaly against its own baseline.
        self.assertEqual(actual["health"]["recent_excluded"], 1)
        self.assertEqual(unfiltered["health"]["recent_excluded"], 1)
        self.assertEqual(actual["health"]["affected_operations"][0]["startup"], "warm")
        self.assertEqual(actual["health"]["affected_operations"][0]["persistence"], "direct")
        # Ignore outliers must exclude exactly the anomalies reported by Health.
        self.assertEqual(actual["excluded_count"], actual["health"]["recent_excluded"])
        self.assertEqual(actual["excluded_count"], 1)
        self.assertEqual(actual["included_count"], 16 - actual["excluded_count"])
        self.assertEqual(unfiltered["included_count"], 16)
        self.assertEqual(unfiltered["excluded_count"], 0)
        self.assertEqual({r["id"] for r in actual["export_records"]},
                         {r["id"] for r in records} - {"7"})

    def test_outlier_detection_uses_full_history_not_display_window(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        records = []
        # Newest 10 are normal; one old anomaly must still be identified globally.
        for i, latency in enumerate([1000] + [100 + (j % 5) for j in range(1, 40)]):
            records.append({"id": str(i), "source": "App", "operation": "SignIn",
                "startup": "warm", "persistence": "direct", "duration_ms": latency,
                "end_utc": (start + timedelta(seconds=i)).isoformat()})
        records.reverse()
        with patch.object(server, "get_scan", return_value=(records, [], [])):
            full = server.compute_data("all", 0, ignore_outliers=True)
            unfiltered = server.compute_data("all", 0, ignore_outliers=False)
            recent = server.compute_data("all", 10, ignore_outliers=True)
        self.assertEqual(full["excluded_count"], 1)
        self.assertEqual(full["included_count"], 39)
        self.assertEqual(unfiltered["included_count"], 40)
        self.assertEqual(recent["excluded_count"], 0)
        self.assertEqual(recent["included_count"], 10)
        self.assertNotIn("0", {r["id"] for r in full["export_records"]})

    def test_outlier_toggle_changes_statistics(self):
        samples = [{"duration_ms": v} for v in (50, 55, 60, 65, 70, 75, 80, 90, 1000)]
        included, excluded = server.remove_latency_outliers(samples)
        self.assertEqual(excluded, 1)
        self.assertEqual([r["duration_ms"] for r in included], [50, 55, 60, 65, 70, 75, 80, 90])
        self.assertEqual(server.remove_latency_outliers(samples[:3])[1], 0)

    def test_cache_reloads_after_file_append(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "watchdog.log"
            source = {"name": "Test", "path": temp, "persistence": "direct", "startup": "auto"}
            sample = """WATCHDOG SNAPSHOT - 2026-11-20 12:00:02.000 UTC
FULL LOGS
Kernel Authenticating Application License... | TimeNow: 2026-11-20 12:00:00.000 | File: BaseEntity.cs
Metadata UpdateDashboard Staged. | TimeNow: 2026-11-20 12:00:00.000 | File: OrchestrationEngine.cs
Request Lifecycle Completed succesfully! | TimeNow: 2026-11-20 12:00:02.000 | File: BaseEntity.cs
"""
            target.write_text(sample)
            self.assertEqual(len(server.parse_file_cached(target, source)), 1)
            target.write_text(sample + sample.replace("12:00:02.000", "12:00:04.000").replace("12:00:00.000", "12:00:03.000"))
            # A recent-only reader sees one lifecycle here. The dashboard scan
            # must also retain the earlier one when the log grows.
            tail = server.read_log(target, max_bytes=len(sample.encode("utf-8")) + 1)
            self.assertEqual(len(server.extract_events(tail, source, target.name)), 1)
            self.assertEqual(len(server.parse_file_cached(target, source)), 2)

    def test_extensionless_watchdog_log_discovered_from_folder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            target = folder / "Watchdog-Log-Public"
            target.write_text("WATCHDOG SNAPSHOT - 2026-11-20 12:00:00.000 UTC\n", encoding="utf-8")
            self.assertEqual(server.files_in_source(folder), [target])
            self.assertEqual(server.files_in_source(target), [target])

    def test_extensionless_watchdog_log_case_insensitive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            target = folder / "WATCHDOG-LOG-PUBLIC"
            target.write_text("WATCHDOG SNAPSHOT - 2026-11-20 12:00:00.000 UTC\n", encoding="utf-8")
            self.assertEqual(server.files_in_source(folder), [target])

    def test_configurable_extensionless_log_filenames(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            names = ("Watchdog-Log-Public", "Watchdog-Log-Debug", "unrelated.log")
            for name in names:
                (folder / name).write_text("sample", encoding="utf-8")
            self.assertEqual(server.files_in_source(folder, "Watchdog-Log-Public"),
                             [folder / "Watchdog-Log-Public"])
            self.assertEqual(server.files_in_source(folder, "watchdog-log-public"),
                             [folder / "Watchdog-Log-Public"])
            self.assertEqual(set(server.files_in_source(folder, "Watchdog-Log-Public, Watchdog-Log-Debug")),
                             {folder / "Watchdog-Log-Public", folder / "Watchdog-Log-Debug"})
            self.assertEqual(server.files_in_source(folder, "nonexistent"), [])

    def test_source_config_applies_filename_filter(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "Watchdog-Log-Public").write_text("sample", encoding="utf-8")
            (folder / "Watchdog-Log-Debug").write_text("sample", encoding="utf-8")
            original = server.CONFIG
            try:
                server.CONFIG = {"sources": [{
                    "name": "Selected", "path": temp, "persistence": "direct",
                    "startup": "auto", "filenames": "Watchdog-Log-Debug",
                }], "overrides": {}}
                server.invalidate_cache()
                _, statuses, _ = server.scan_all()
                self.assertEqual(statuses[0]["files"], 1)
            finally:
                server.CONFIG = original
                server.invalidate_cache()

    def test_signup_from_uploaded_entryflow_watchdog_format(self):
        # Anonymized, timestamp-preserving lines from Watchdog-Log-EntryFlow.
        text = """WATCHDOG SNAPSHOT - 2026-08-04 14:08:09.076 UTC
FULL LOGS
Watchdog AWake! | TimeNow: 2026-08-04 14:07:37.299 | File: BaseEntity.cs | Class: BaseEntity | Method: .ctor
Kernel Authenticating Application License... | TimeNow: 2026-08-04 14:07:37.331 | File: BaseEntity.cs
Metadata SignUp Staged. Now asking for Logic Discovery... | TimeNow: 2026-08-04 14:07:37.410 | File: OrchestrationEngine.cs
Logic Node SignUp discovered at 17363659.Now Executing... | TimeNow: 2026-08-04 14:07:37.410 | File: OrchestrationEngine.cs
Request Lifecycle Completed succesfully! | TimeNow: 2026-08-04 14:08:09.076 | File: BaseEntity.cs
"""
        source = {"path": "test", "persistence": "direct", "startup": "auto"}
        events = server.extract_events(text, source, "Watchdog-Log-EntryFlow")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["operation"], "SignUp")
        self.assertEqual(events[0]["duration_ms"], 31745)

    def test_signup_default_precedes_signin_with_no_records(self):
        self.assertEqual(server.available_operations([], []), ["SignUp", "SignIn"])
        self.assertEqual(server.available_operations(
            [{"operation": "SignIn"}, {"operation": "UpdateDashboard CRUD"},
             {"operation": "SignUp"}, {"operation": "ResetPassword"}], []),
            ["SignUp", "SignIn", "ResetPassword", "UpdateDashboard CRUD"])

    def test_signup_default_respects_existing_custom_spelling(self):
        self.assertEqual(server.available_operations(
            [{"operation": "SignUP"}], [{"name": "SignUP", "action": "signup"}]),
            ["SignUP", "SignIn"])

    def test_signup_does_not_inherit_prior_request_metadata(self):
        text = """WATCHDOG SNAPSHOT - 2026-11-20 12:00:04.000 UTC
FULL LOGS
Kernel Authenticating Application License... | TimeNow: 2026-11-20 12:00:00.000 | File: BaseEntity.cs
Metadata SignIn Staged. Now asking for Logic Discovery... | TimeNow: 2026-11-20 12:00:00.100 | File: OrchestrationEngine.cs
Request Lifecycle Completed succesfully! | TimeNow: 2026-11-20 12:00:01.000 | File: BaseEntity.cs
Kernel Authenticating Application License... | TimeNow: 2026-11-20 12:00:02.000 | File: BaseEntity.cs
Metadata SignUp Staged. Now asking for Logic Discovery... | TimeNow: 2026-11-20 12:00:02.100 | File: OrchestrationEngine.cs
Logic Node SignUp discovered at 42.Now Executing... | TimeNow: 2026-11-20 12:00:02.100 | File: OrchestrationEngine.cs
Request Lifecycle Completed succesfully! | TimeNow: 2026-11-20 12:00:04.000 | File: BaseEntity.cs
"""
        source = {"path": "test", "persistence": "direct", "startup": "auto"}
        events = server.extract_events(text, source, "test.log")
        self.assertEqual([event["operation"] for event in events], ["SignIn", "SignUp"])

    def test_signup_recognized_from_logic_node_without_metadata(self):
        text = """WATCHDOG SNAPSHOT - 2026-11-20 12:00:02.000 UTC
FULL LOGS
Kernel Authenticating Application License... | TimeNow: 2026-11-20 12:00:00.000 | File: BaseEntity.cs
Logic Node SignUP discovered at 42.Now Executing... | TimeNow: 2026-11-20 12:00:00.010 | File: OrchestrationEngine.cs
Request Lifecycle Completed succesfully! | TimeNow: 2026-11-20 12:00:02.000 | File: BaseEntity.cs
"""
        source = {"path": "test", "persistence": "direct", "startup": "auto"}
        events = server.extract_events(text, source, "test.log")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["operation"], "SignUP")

    def test_signup_custom_name_maps_case_insensitive_metadata_action(self):
        text = """WATCHDOG SNAPSHOT - 2026-11-20 12:00:02.000 UTC
FULL LOGS
Kernel Authenticating Application License... | TimeNow: 2026-11-20 12:00:00.000 | File: BaseEntity.cs
Metadata SignUP Staged. | TimeNow: 2026-11-20 12:00:00.010 | File: OrchestrationEngine.cs
Request Lifecycle Completed succesfully! | TimeNow: 2026-11-20 12:00:02.000 | File: BaseEntity.cs
"""
        source = {"path": "test", "persistence": "direct", "startup": "auto"}
        raw = server.extract_events(text, source, "test.log")
        mapped = server.extract_events(text, source, "test.log",
                                       [{"name": "Registration", "action": "signup"}])
        self.assertEqual(raw[0]["operation"], "SignUP")
        self.assertEqual(mapped[0]["operation"], "Registration")
        self.assertEqual(raw[0]["id"], mapped[0]["id"])

    def test_custom_literal_log_phrase_for_unknown_operation(self):
        text = """WATCHDOG SNAPSHOT - 2026-11-20 12:00:02.000 UTC
FULL LOGS
Kernel Authenticating Application License... | TimeNow: 2026-11-20 12:00:00.000 | File: BaseEntity.cs
Routing request to user creation handler. | TimeNow: 2026-11-20 12:00:00.010 | File: User.cs
Request Lifecycle Completed succesfully! | TimeNow: 2026-11-20 12:00:02.000 | File: BaseEntity.cs
"""
        source = {"path": "test", "persistence": "direct", "startup": "auto"}
        events = server.extract_events(text, source, "test.log",
                                       [{"name": "SignUP", "action": "user creation handler"}])
        self.assertEqual(events[0]["operation"], "SignUP")

    def test_custom_operation_validation(self):
        self.assertEqual(server.validate_operation_mappings([{"name": "SignUP", "action": "SignUp"}]),
                         [{"name": "SignUP", "action": "SignUp"}])
        with self.assertRaises(ValueError):
            server.validate_operation_mappings([{"name": "all", "action": "SignUp"}])
        with self.assertRaises(ValueError):
            server.validate_operation_mappings([{"name": "SignUP", "action": "SignUp"},
                                                {"name": "Registration", "action": "signup"}])
        with self.assertRaises(ValueError):
            server.validate_operation_mappings([{"name": "Bad" + chr(10) + "Name", "action": "SignUp"}])

    def test_mapping_cache_changes_without_log_file_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "watchdog.log"
            path.write_text("""WATCHDOG SNAPSHOT - 2026-11-20 12:00:02.000 UTC
FULL LOGS
Kernel Authenticating Application License... | TimeNow: 2026-11-20 12:00:00.000 | File: BaseEntity.cs
Metadata SignUP Staged. | TimeNow: 2026-11-20 12:00:00.010 | File: OrchestrationEngine.cs
Request Lifecycle Completed succesfully! | TimeNow: 2026-11-20 12:00:02.000 | File: BaseEntity.cs
""")
            src = {"name": "Test", "path": temp, "persistence": "direct", "startup": "auto"}
            original = server.CONFIG
            try:
                server.CONFIG = {"sources": [src], "overrides": {},
                                 "operations": [{"name": "Registration", "action": "SignUP"},
                                                {"name": "Future operation", "action": "NotYetLogged"}]}
                server.invalidate_cache()
                data = server.compute_data("Registration", 50)
                self.assertIn("Registration", data["operations"])
                self.assertIn("Future operation", data["operations"])
                self.assertEqual(data["recent"][0]["operation"], "Registration")
                self.assertEqual(data["groups"][2]["n"], 1)
                self.assertEqual(server.compute_data("Future operation", 50)["recent"], [])
                server.CONFIG["operations"] = [{"name": "SignUP", "action": "SignUP"}]
                server.invalidate_cache()
                changed = server.compute_data("SignUP", 50)
                self.assertEqual(changed["recent"][0]["operation"], "SignUP")
                self.assertEqual(data["recent"][0]["id"], changed["recent"][0]["id"])
            finally:
                server.CONFIG = original
                server.invalidate_cache()

    def test_incomplete_requests_excluded(self):
        text = "Kernel Authenticating Application License... | TimeNow: 2026-11-20 12:00:00.000 | File: BaseEntity.cs\n"
        source = {"path": "test", "persistence": "direct", "startup": "auto"}
        self.assertEqual(server.extract_events(text, source, "test.log"), [])


if __name__ == "__main__":
    unittest.main()