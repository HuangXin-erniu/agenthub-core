from __future__ import annotations

import json
from contextlib import contextmanager
import os
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_agenthub as fixtures

hub = fixtures.hub


class IntegrityTests(unittest.TestCase):
    setUp = fixtures.AgentHubV23Tests.setUp
    tearDown = fixtures.AgentHubV23Tests.tearDown
    git = fixtures.AgentHubV23Tests.git
    call = fixtures.AgentHubV23Tests.call
    decide = fixtures.AgentHubV23Tests.decide
    packet = fixtures.AgentHubV23Tests.packet

    @contextmanager
    def database(self):
        conn = sqlite3.connect(self.state)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def snapshot(self):
        with self.database() as conn:
            return {table: conn.execute("SELECT * FROM " + table).fetchall()
                    for table in ("tasks", "events", "checkpoints", "submissions")}

    def post_old(self, task="OLD"):
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet(task))

    def naming(self, detailed=False):
        config = json.loads(self.config.read_text())
        naming = {
            "task_pattern": r"(?P<subject>[A-Z]+)-(?P<seq>\d{3})",
            "chain_pattern": r"CHN-(?P<seq>\d{4})", "seq_digits": 3,
            "enum_description": {"SUBJECT": "TEST"},
        }
        if detailed:
            naming.update({
                "task_pattern": r"(?P<phase>P\d\d)-(?P<domain>[A-Z]+)-(?P<type>[A-Z]+)-(?P<subject>[A-Z]+)-(?P<seq>\d{3})",
                "phase_domains": {"P00": ["GOV"]}, "domain_types": {"GOV": ["FIX"]},
            })
        config["task_naming"] = naming
        self.config.write_text(json.dumps(config))

    def named_packet(self, task, chain):
        spec = json.loads(self.packet(task))
        spec["chain_id"] = chain
        return spec

    def assert_rejected_by_both(self, spec):
        before = self.snapshot()
        for command in ("post", "supersede"):
            args = [command, "admin_agent"]
            if command == "supersede":
                args += ["OLD", "--reason", "replace"]
            args += ["--spec", json.dumps(spec)]
            with self.subTest(command=command, spec_fields=list(spec)):
                with self.assertRaises(hub.HubError):
                    self.call("--device", "client_primary", *args)
                self.assertEqual(self.snapshot(), before)

    def test_contract_arrays_and_text_are_shared_and_atomic(self):
        self.post_old()
        for field in hub.TASK_LIST_FIELDS:
            for value in (None, [], "not-an-array", [""] , ["item"] * 21):
                spec = json.loads(self.packet("NEW"))
                if value is None:
                    del spec[field]
                else:
                    spec[field] = value
                self.assert_rejected_by_both(spec)
        for field in ("id", "title", "objective", "assignee", "decision_key", "base_commit", "blocked_protocol"):
            spec = json.loads(self.packet("NEW"))
            del spec[field]
            self.assert_rejected_by_both(spec)
        for field, value in (("assignee", "admin_agent"), ("decision_key", "missing"),
                             ("base_commit", "0" * 40), ("id", "bad/id"),
                             ("priority", 0), ("priority", 100), ("priority", True),
                             ("context_budget", 1), ("context_budget", 12001),
                             ("context_budget", "6000"), ("chain_id", [])):
            spec = json.loads(self.packet("NEW"))
            spec[field] = value
            self.assert_rejected_by_both(spec)

    def test_contract_budget_and_packet_limit_are_shared(self):
        self.post_old()
        spec = json.loads(self.packet("NEW"))
        spec.update(context_budget=2000, objective="x" * 2500)
        self.assert_rejected_by_both(spec)
        spec["objective"] = "x" * 17000
        self.assert_rejected_by_both(spec)

    def test_sequence_chain_and_successful_same_chain_replacement(self):
        self.naming()
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent",
                  "--spec", json.dumps(self.named_packet("OLD-001", "CHN-0001")))
        self.call("--device", "client_primary", "post", "admin_agent",
                  "--spec", json.dumps(self.named_packet("OTHER-002", "CHN-0002")))
        before = self.snapshot()
        for task, chain in (("NEW-001", "CHN-0001"), ("NEW-004", "CHN-0001"),
                            ("NEW-003", "CHN-0002"), ("NEW-003", "invalid")):
            with self.assertRaises(hub.HubError):
                self.call("--device", "client_primary", "supersede", "admin_agent", "OLD-001",
                          "--spec", json.dumps(self.named_packet(task, chain)), "--reason", "replace")
            self.assertEqual(self.snapshot(), before)
        self.call("--device", "client_primary", "supersede", "admin_agent", "OLD-001",
                  "--spec", json.dumps(self.named_packet("NEW-003", "CHN-0001")), "--reason", "replace")
        with self.database() as conn:
            old = conn.execute("SELECT status,superseded_by FROM tasks WHERE task_id='OLD-001'").fetchone()
            new = conn.execute("SELECT status,supersedes,chain_id FROM tasks WHERE task_id='NEW-003'").fetchone()
        self.assertEqual(old, ("cancelled", "NEW-003"))
        self.assertEqual(new, ("ready", "OLD-001", "CHN-0001"))

    def test_supersede_enforces_naming_catalogue(self):
        self.naming(detailed=True)
        self.decide()
        old = "P00-GOV-FIX-OLD-001"
        self.call("--device", "client_primary", "post", "admin_agent",
                  "--spec", json.dumps(self.named_packet(old, "CHN-0001")))
        before = self.snapshot()
        for task in ("P01-GOV-FIX-NEW-002", "P00-DATA-FIX-NEW-002", "P00-GOV-FEAT-NEW-002"):
            with self.assertRaises(hub.HubError):
                self.call("--device", "client_primary", "supersede", "admin_agent", old,
                          "--spec", json.dumps(self.named_packet(task, "CHN-0001")), "--reason", "replace")
            self.assertEqual(self.snapshot(), before)

    def test_failure_after_old_cancellation_rolls_back_all_writes(self):
        self.post_old()
        before = self.snapshot()
        original = hub.record
        def fail_last(conn, agent, device, action, entity, detail):
            if action == "task.supersede.old_cancelled":
                raise RuntimeError("injected final audit failure")
            return original(conn, agent, device, action, entity, detail)
        with patch.object(hub, "record", side_effect=fail_last):
            with self.assertRaises(RuntimeError):
                self.call("--device", "client_primary", "supersede", "admin_agent", "OLD",
                          "--spec", self.packet("NEW"), "--reason", "replace")
        self.assertEqual(self.snapshot(), before)
        self.call("--device", "client_primary", "supersede", "admin_agent", "OLD",
                  "--spec", self.packet("NEW"), "--reason", "retry")

    def test_failed_insert_does_not_cancel_or_consume_sequence(self):
        self.naming()
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent",
                  "--spec", json.dumps(self.named_packet("OLD-001", "CHN-0001")))
        before = self.snapshot()
        with self.database() as conn:
            conn.execute("CREATE TRIGGER reject_new BEFORE INSERT ON tasks WHEN NEW.task_id='NEW-002' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.call("--device", "client_primary", "supersede", "admin_agent", "OLD-001",
                      "--spec", json.dumps(self.named_packet("NEW-002", "CHN-0001")), "--reason", "replace")
        self.assertEqual(self.snapshot(), before)
        with self.database() as conn:
            conn.execute("DROP TRIGGER reject_new")
        self.call("--device", "client_primary", "supersede", "admin_agent", "OLD-001",
                  "--spec", json.dumps(self.named_packet("NEW-002", "CHN-0001")), "--reason", "retry")

    def remote_config(self, shell):
        config = json.loads(self.config.read_text())
        config["remote"] = {"enabled": True, "shell": shell, "ssh_host": "mock-authority",
                            "project_root": str(self.repo), "launcher": "agenthub.cmd" if shell == "windows_cmd" else "agenthub.sh"}
        config["deployment"] = {"adapter": "ssh_atomic_windows" if shell == "windows_cmd" else "ssh_atomic_posix",
                                "managed_files": [{"source": "agent_hub.py", "destination": "agent_hub.py"}]}
        self.config.write_text(json.dumps(config))
        return config

    def test_device_routing_matrix_and_explicit_local(self):
        for shell in ("windows_cmd", "posix_sh"):
            self.remote_config(shell)
            for platform in ("Windows", "Linux"):
                for device, local, expected_proxy in (("client_primary", False, True),
                                                       ("authority", False, False),
                                                       ("client_primary", True, False)):
                    with self.subTest(shell=shell, platform=platform, device=device, local=local):
                        with patch.object(hub.platform, "system", return_value=platform), \
                             patch.object(hub, "proxy_to_authority", return_value=0) as proxy, \
                             patch.object(hub, "connect", side_effect=RuntimeError("local")) as connect:
                            args = ["--config", str(self.config), "--device", device, "status", "worker_a"]
                            if local:
                                args.insert(0, "--local")
                            if expected_proxy:
                                self.assertEqual(hub.run(args), 0)
                            else:
                                with self.assertRaisesRegex(RuntimeError, "local"):
                                    hub.run(args)
                            self.assertEqual(proxy.called, expected_proxy)
                            self.assertEqual(connect.called, not expected_proxy)

    def test_remote_disabled_and_unknown_device(self):
        for device in ("authority", "client_primary"):
            with patch.object(hub, "proxy_to_authority") as proxy, \
                 patch.object(hub, "connect", side_effect=RuntimeError("local")) as connect:
                with self.assertRaises(RuntimeError):
                    hub.run(["--config", str(self.config), "--device", device, "status", "worker_a"])
                self.assertFalse(proxy.called)
                self.assertTrue(connect.called)
        with patch.object(hub, "proxy_to_authority") as proxy, patch.object(hub, "connect") as connect:
            with self.assertRaises(hub.HubError):
                hub.run(["--config", str(self.config), "--device", "unknown", "status", "worker_a"])
            self.assertFalse(proxy.called)
            self.assertFalse(connect.called)

    def test_ssh_failure_never_opens_local_state_and_uses_configured_shell(self):
        for shell in ("windows_cmd", "posix_sh"):
            self.remote_config(shell)
            with patch.object(hub, "connect") as connect, \
                 patch.object(hub.subprocess, "run", return_value=subprocess.CompletedProcess([], 23)) as run:
                self.assertEqual(hub.run(["--config", str(self.config), "--device", "client_primary", "status", "worker_a"]), 23)
                self.assertFalse(connect.called)
                command = run.call_args.args[0]
                self.assertEqual(command[0], "ssh")
                self.assertIn("StrictHostKeyChecking=yes", command)
                self.assertTrue(command[-1].startswith("cmd /d /c") if shell == "windows_cmd" else command[-1].startswith("cd --"))
            with patch.object(hub, "connect") as connect, patch.object(hub.subprocess, "run", side_effect=OSError("unavailable")):
                with self.assertRaises(OSError):
                    hub.run(["--config", str(self.config), "--device", "client_primary", "status", "worker_a"])
                self.assertFalse(connect.called)

    def claim_old(self):
        self.post_old()
        return Path(json.loads(self.call("--device", "authority", "claim", "worker_a", "OLD"))["worktree_path"])

    def checkpoint(self, device="authority"):
        self.call("--device", device, "checkpoint", "worker_a", "OLD",
                  "--progress", json.dumps({"summary": "safe", "next_step": "resume", "evidence": []}))

    def handoff(self, device="authority", target="client_primary", agent="worker_a"):
        return self.call("--device", device, "handoff", agent, "OLD", "--to-device", target, "--note", "handoff")

    def test_handoff_requires_current_checkpoint_and_records_reference(self):
        self.claim_old()
        before = self.snapshot()
        with self.assertRaisesRegex(hub.HubError, "checkpoint"):
            self.handoff()
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(hub.HubError):
            self.checkpoint("client_primary")
        self.checkpoint()
        self.handoff()
        with self.database() as conn:
            checkpoint = conn.execute("SELECT event_id FROM events WHERE action='task.checkpoint' ORDER BY event_id DESC LIMIT 1").fetchone()[0]
            detail = json.loads(conn.execute("SELECT detail_json FROM events WHERE action='task.handoff' ORDER BY event_id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(detail["checkpoint_event_id"], checkpoint)
        self.assertFalse(detail["checkpoint_missing"])

    def test_handoff_round_trip_rejects_previous_phase_checkpoint(self):
        self.claim_old()
        self.checkpoint()
        self.handoff()
        self.checkpoint("client_primary")
        self.handoff("client_primary", "authority")
        with self.assertRaisesRegex(hub.HubError, "checkpoint"):
            self.handoff()
        self.checkpoint()
        self.handoff()

    def test_handoff_reclaim_rejects_checkpoint_from_previous_claim(self):
        self.claim_old()
        self.checkpoint()
        blocked = {"outcome": "blocked", "summary": "blocked", "blocker": "fixture", "decision_needed": "rework"}
        self.call("--device", "authority", "submit", "worker_a", "OLD", "--result", json.dumps(blocked))
        self.call("--device", "client_primary", "review", "admin_agent", "OLD", "--action", "rework", "--note", "retry")
        self.call("--device", "authority", "claim", "worker_a", "OLD")
        with self.assertRaisesRegex(hub.HubError, "checkpoint"):
            self.handoff()

    def test_admin_recovery_missing_checkpoint_and_reason(self):
        self.claim_old()
        before = self.snapshot()
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_primary", "handoff", "admin_agent", "OLD", "--to-device", "client_secondary", "--note", "")
        self.assertEqual(self.snapshot(), before)
        self.handoff("client_primary", "client_secondary", "admin_agent")
        with self.database() as conn:
            detail = json.loads(conn.execute("SELECT detail_json FROM events WHERE action='task.handoff.force'").fetchone()[0])
        self.assertTrue(detail["checkpoint_missing"])
        self.assertIsNone(detail["checkpoint_event_id"])
        self.assertEqual(detail["note"], "handoff")
        with self.assertRaises(hub.HubError):
            self.checkpoint("authority")
        self.checkpoint("client_secondary")
        self.handoff("client_secondary", "authority")

    def test_same_second_event_order_and_transaction_failure(self):
        with patch.object(hub, "now", return_value="2026-01-01T00:00:00+00:00"):
            self.claim_old()
            self.checkpoint()
            before = self.snapshot()
            with patch.object(hub, "record", side_effect=RuntimeError("audit failed")):
                with self.assertRaises(RuntimeError):
                    self.handoff()
            self.assertEqual(self.snapshot(), before)
            self.handoff()
            with self.assertRaisesRegex(hub.HubError, "checkpoint"):
                self.handoff("client_primary", "authority")
            self.checkpoint("client_primary")
            self.handoff("client_primary", "authority")

    def test_schema4_upgrade_preserves_data_and_existing_checkpoint(self):
        self.claim_old()
        self.checkpoint()
        before = self.snapshot()
        with self.database() as conn:
            columns = conn.execute("PRAGMA table_info(tasks)").fetchall()
            conn.execute("UPDATE metadata SET value='2.3.1' WHERE key='agenthub_version'")
        self.call("--device", "client_primary", "show", "admin_agent", "OLD")
        self.assertEqual(self.snapshot(), before)
        with self.database() as conn:
            self.assertEqual(conn.execute("PRAGMA table_info(tasks)").fetchall(), columns)
            self.assertEqual(dict(conn.execute("SELECT key,value FROM metadata"))["schema_version"], "4")
            self.assertEqual(dict(conn.execute("SELECT key,value FROM metadata"))["agenthub_version"], hub.VERSION)
        self.handoff()

    def seeded_claim(self, names, allowed, forbidden):
        for name in names:
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture\n", encoding="utf-8")
        if names:
            self.git("add", "--", *names)
            self.git("commit", "-m", "fixtures")
            self.base_commit = self.git("rev-parse", "HEAD")
        self.decide()
        packet = json.loads(self.packet("PATHS"))
        packet.update(allowed_paths=allowed, forbidden_paths=forbidden)
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(packet))
        return Path(json.loads(self.call("--device", "authority", "claim", "worker_a", "PATHS"))["worktree_path"])

    def submit_paths(self, worktree, accepted):
        self.git("add", "-A", cwd=worktree)
        self.git("commit", "-m", "path fixture", cwd=worktree)
        commit = self.git("rev-parse", "HEAD", cwd=worktree)
        result = {"outcome": "completed", "summary": "paths", "commit": commit, "tests": ["fixture"], "evidence": ["fixture"]}
        args = ("--device", "authority", "submit", "worker_a", "PATHS", "--result", json.dumps(result))
        if accepted:
            self.call(*args)
        else:
            before = self.snapshot()
            with self.assertRaises(hub.HubError):
                self.call(*args)
            self.assertEqual(self.snapshot(), before)

    def test_path_add_modify_delete_and_legacy_recursive_pattern(self):
        worktree = self.seeded_claim(["src/edit.txt", "src/remove.txt"], ["src/*"], ["src/private/*"])
        (worktree / "src/edit.txt").write_text("changed\n")
        (worktree / "src/remove.txt").unlink()
        (worktree / "src/nested").mkdir()
        (worktree / "src/nested/new.txt").write_text("new\n")
        self.submit_paths(worktree, True)

    def test_path_forbidden_overrides_allowed_for_deletion(self):
        worktree = self.seeded_claim(["protected/file.txt"], ["*"], ["protected/*"])
        (worktree / "protected/file.txt").unlink()
        self.submit_paths(worktree, False)

    def test_path_rename_source_is_checked(self):
        worktree = self.seeded_claim(["protected/file.txt"], ["allowed/*"], ["protected/*"])
        (worktree / "allowed").mkdir()
        self.git("mv", "protected/file.txt", "allowed/file.txt", cwd=worktree)
        self.submit_paths(worktree, False)

    def test_path_rename_target_is_checked(self):
        worktree = self.seeded_claim(["allowed/file.txt"], ["allowed/*"], ["protected/*"])
        (worktree / "protected").mkdir()
        self.git("mv", "allowed/file.txt", "protected/file.txt", cwd=worktree)
        self.submit_paths(worktree, False)

    def test_path_rename_inside_allowed_is_accepted(self):
        worktree = self.seeded_claim(["allowed/file.txt"], ["allowed/*"], ["protected/*"])
        self.git("mv", "allowed/file.txt", "allowed/renamed.txt", cwd=worktree)
        self.submit_paths(worktree, True)

    def test_path_unicode_and_spaces_are_preserved(self):
        names = ["allowed/中文.txt", "allowed/ spaced file .txt"]
        worktree = self.seeded_claim([], names, ["protected/*"])
        for name in names:
            target = worktree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture\n", encoding="utf-8")
        self.submit_paths(worktree, True)

    def test_path_unicode_forbidden_is_rejected(self):
        worktree = self.seeded_claim([], ["*"], ["protected/*"])
        (worktree / "protected").mkdir()
        (worktree / "protected/中文.txt").write_text("fixture\n", encoding="utf-8")
        self.submit_paths(worktree, False)

    @unittest.skipIf(os.name == "nt", "Windows does not support these filenames")
    def test_path_control_characters_and_edge_spaces_are_preserved(self):
        names = [" newline\nname ", " tab\tname ", 'quote"name']
        worktree = self.seeded_claim([], names, ["protected/*"])
        for name in names:
            (worktree / name).write_text("fixture\n")
        self.submit_paths(worktree, True)

    @unittest.skipIf(os.name == "nt", "Windows does not support newline filenames")
    def test_path_quoted_forbidden_filename_is_rejected(self):
        worktree = self.seeded_claim([], ["*"], ["protected/*"])
        (worktree / "protected").mkdir()
        (worktree / "protected/line\nname").write_text("fixture\n")
        self.submit_paths(worktree, False)

    def test_path_decoder_fails_closed_and_preserves_whitespace(self):
        with patch.object(hub.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b" leading \0trailing \0", b"")):
            self.assertEqual(hub.changed_paths({}, "base", "head", "repo"), [" leading ", "trailing "])
        for raw in (b"\xff\0", b"missing terminator"):
            with patch.object(hub.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, raw, b"")):
                with self.assertRaises(hub.HubError):
                    hub.changed_paths({}, "base", "head", "repo")


if __name__ == "__main__":
    unittest.main()
