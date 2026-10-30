from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from support import (
    ROOT, HubFixture, create_v4_database, hub, previous_release_runtime,
)


class MigrationTests(HubFixture):
    def prepare_v4(self) -> None:
        create_v4_database(self.state, self.base_commit, self.worktrees)

    def test_v4_migrates_with_backup_and_preserves_rows(self):
        self.prepare_v4()
        data = self.call("--device", "client_primary", "status", "admin_agent")
        migration = data["migration"]
        self.assertEqual(migration["from"], 4)
        self.assertEqual(migration["to"], 5)
        self.assertEqual(migration["tasks"], 4)
        backup = Path(migration["backup"])
        self.assertTrue(backup.exists())
        conn = sqlite3.connect(self.state)
        conn.row_factory = sqlite3.Row
        try:
            metadata = dict(conn.execute("SELECT key,value FROM metadata"))
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            rows = {row["task_id"]: row for row in conn.execute("SELECT * FROM tasks")}
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()
        self.assertEqual(metadata["schema_version"], "5")
        self.assertEqual(metadata["agenthub_version"], hub.VERSION)
        self.assertEqual(len(columns), 26)
        self.assertEqual(set(rows), {"T-CLOSED", "T-APPROVED", "T-ACTIVE", "T-BLOCKED"})
        self.assertIsNotNone(rows["T-CLOSED"]["approved_submission_id"])
        self.assertIsNotNone(rows["T-APPROVED"]["approved_submission_id"])
        self.assertIsNone(rows["T-ACTIVE"]["approved_submission_id"])
        contract = json.loads(rows["T-ACTIVE"]["contract_json"])
        self.assertEqual(contract["legacy"]["scope"], ["legacy scope"])
        self.assertEqual(contract["legacy"]["blocked_protocol"], "legacy blocked protocol")
        self.assertEqual(contract["instructions"], ["legacy instruction"])
        self.assertEqual(rows["T-ACTIVE"]["claim_session"], "legacy-session")
        self.assertEqual(violations, [])

    def test_migration_is_idempotent(self):
        self.prepare_v4()
        first = self.call("--device", "client_primary", "status", "admin_agent")
        self.assertIn("migration", first)
        second = self.call("--device", "client_primary", "status", "admin_agent")
        self.assertNotIn("migration", second)
        backups = list(self.root.glob("state.db.v4-*.bak"))
        self.assertEqual(len(backups), 1)

    def test_corrupt_v4_json_rolls_back_and_keeps_v4(self):
        self.prepare_v4()
        conn = sqlite3.connect(self.state)
        try:
            conn.execute("UPDATE tasks SET scope_json='{' WHERE task_id='T-ACTIVE'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "status", "admin_agent")
        self.assertEqual(raised.exception.code, "state.corrupt")
        conn = sqlite3.connect(self.state)
        try:
            metadata = dict(conn.execute("SELECT key,value FROM metadata"))
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        finally:
            conn.close()
        self.assertEqual(metadata["schema_version"], "4")
        self.assertIn("scope_json", columns)
        self.assertTrue(list(self.root.glob("state.db.v4-*.bak")))

    def test_newer_schema_is_rejected(self):
        self.prepare_v4()
        conn = sqlite3.connect(self.state)
        try:
            conn.execute("UPDATE metadata SET value='6' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "status", "admin_agent")
        self.assertEqual(raised.exception.code, "state.migration")
        self.assertFalse(list(self.root.glob("state.db.v4-*.bak")))

    def test_synthetic_submission_is_created_for_result_without_history(self):
        self.prepare_v4()
        conn = sqlite3.connect(self.state)
        try:
            conn.execute("DELETE FROM submissions WHERE task_id='T-APPROVED'")
            conn.commit()
        finally:
            conn.close()
        self.call("--device", "client_primary", "status", "admin_agent")
        conn = sqlite3.connect(self.state)
        try:
            row = conn.execute("SELECT approved_submission_id FROM tasks WHERE task_id='T-APPROVED'").fetchone()
            submissions = conn.execute("SELECT COUNT(*) FROM submissions WHERE task_id='T-APPROVED'").fetchone()[0]
            result = conn.execute(
                "SELECT result_json FROM submissions WHERE task_id='T-APPROVED' ORDER BY submission_id DESC LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNotNone(row[0])
        self.assertEqual(submissions, 1)
        self.assertEqual(json.loads(result)["summary"], "approved")

    def test_v4_backup_is_readable_by_the_previous_runtime(self):
        runtime = previous_release_runtime()
        if runtime is None:
            self.skipTest("v2.4.3 tag is not available")
        self.prepare_v4()
        data = self.call("--device", "client_primary", "status", "admin_agent")
        backup = Path(data["migration"]["backup"])
        result = subprocess.run(
            [sys.executable, str(runtime), "--local", "--config", str(self.config),
             "--state", str(backup), "--device", "client_primary", "status", "admin_agent", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(sorted(task["id"] for task in payload["tasks"]), ["T-ACTIVE", "T-APPROVED", "T-BLOCKED"])

    def test_migration_preserves_checkpoints_and_events(self):
        self.prepare_v4()
        self.call("--device", "client_primary", "status", "admin_agent")
        conn = sqlite3.connect(self.state)
        try:
            checkpoint = conn.execute("SELECT summary FROM checkpoints WHERE task_id='T-ACTIVE'").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(checkpoint, "legacy checkpoint")
        self.assertGreaterEqual(events, 1)


if __name__ == "__main__":
    unittest.main()
