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
            d = server.compute_data("all", 50)
            self.assertEqual(d["total_requests"], 8)
            cold_offline = next(g for g in d["groups"] if g["startup"] == "cold" and g["persistence"] == "offline")
            self.assertEqual(cold_offline["n"], 5)
            self.assertEqual(cold_offline["latest_ms"], 2050)
            self.assertEqual(cold_offline["mean_ms"], 2906.8)
            warm_offline = next(g for g in d["groups"] if g["startup"] == "warm" and g["persistence"] == "offline")
            self.assertEqual(warm_offline["latest_ms"], 153)
            self.assertEqual(warm_offline["divergence_pct"], 0.0)
        finally:
            server.CONFIG = original
            server.invalidate_cache()


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
            self.assertEqual(len(server.parse_file_cached(target, source)), 2)

    def test_incomplete_requests_excluded(self):
        text = "Kernel Authenticating Application License... | TimeNow: 2026-11-20 12:00:00.000 | File: BaseEntity.cs\n"
        source = {"path": "test", "persistence": "direct", "startup": "auto"}
        self.assertEqual(server.extract_events(text, source, "test.log"), [])


if __name__ == "__main__":
    unittest.main()