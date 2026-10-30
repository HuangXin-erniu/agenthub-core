from __future__ import annotations

import json
import sqlite3
import unittest
from unittest import mock

from support import HubFixture, git, hub


class IntegrityTests(HubFixture):
    def remote_config(self, enabled: bool = True) -> dict:
        value = self.config_value()
        value["remote"] = {
            "enabled": enabled,
            "shell": "posix_sh",
            "ssh_host": "host",
            "project_root": "/repo",
            "launcher": ".agenthub/agenthub.sh",
            "ssh_options": [],
        }
        value["deployment"] = {
            "adapter": "ssh_atomic_posix",
            "managed_files": [{"source": "agent_hub.py", "destination": ".agenthub/agent_hub.py"}],
        }
        self.write_config(value)
        return value

    def commit_file(self, task_id: str, path: str, text: str = "change\n") -> str:
        worktree = self.worktree(task_id)
        target = worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        git(worktree, "add", "--", path)
        git(worktree, "commit", "-qm", f"change {path}")
        return git(worktree, "rev-parse", "HEAD")

    def submit(self, task_id: str, commit: str) -> str:
        return json.dumps({"outcome": "completed", "summary": "done", "commit": commit,
                           "tests": ["unit"], "evidence": [f"commit:{commit}"]})

    def test_device_routing_matrix_and_local_override(self):
        self.remote_config(enabled=True)
        with mock.patch.object(hub.subprocess, "run") as run:
            run.return_value.returncode = 0
            code = hub.run(["--config", str(self.config), "--state", str(self.state),
                            "--device", "client_primary", "status", "admin_agent"])
            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 1)
        self.assertFalse(self.state.exists())
        self.call("--device", "authority", "status", "worker_a")
        self.assertTrue(self.state.exists())

    def test_remote_disabled_and_unknown_device(self):
        self.remote_config(enabled=False)
        self.call("--device", "client_primary", "status", "admin_agent")
        self.assertTrue(self.state.exists())
        with self.assertRaises(hub.HubError) as raised:
            hub.run(["--config", str(self.config), "--state", str(self.state),
                     "--device", "nobody", "status", "admin_agent"])
        self.assertEqual(raised.exception.code, "identity.device")

    def test_ssh_failure_never_opens_local_state(self):
        self.remote_config(enabled=True)
        with mock.patch.object(hub.subprocess, "run") as run:
            run.return_value.returncode = 255
            code = hub.run(["--config", str(self.config), "--state", str(self.state),
                            "--device", "client_primary", "status", "admin_agent"])
        self.assertEqual(code, 255)
        self.assertFalse(self.state.exists())

    def test_split_command_ignores_option_values_that_look_like_commands(self):
        self.assertEqual(hub.split_command(["--config", "post", "--device", "next", "status", "admin"]),
                         ["status", "admin"])
        self.assertEqual(hub.split_command(["--project=post", "next", "worker"]), ["next", "worker"])

    def test_path_add_modify_and_delete(self):
        self.decide()
        self.post(self.packet("T-1", allowed_paths=["README.md", "new.txt"]))
        self.claim("T-1")
        commit = self.commit_file("T-1", "new.txt", "new\n")
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result",
                  self.submit("T-1", commit))
        self.post(self.packet("T-2", allowed_paths=["README.md"]))
        self.claim("T-2")
        worktree = self.worktree("T-2")
        (worktree / "README.md").unlink()
        git(worktree, "add", "-A")
        git(worktree, "commit", "-qm", "delete")
        commit = git(worktree, "rev-parse", "HEAD")
        self.call("--device", "authority", "submit", "worker_a", "T-2", "--result",
                  self.submit("T-2", commit))

    def test_path_forbidden_overrides_allowed(self):
        self.decide()
        self.post(self.packet("T-1", allowed_paths=["*"], forbidden_paths=["README.md"]))
        self.claim("T-1")
        commit = self.commit_file("T-1", "README.md", "changed\n")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "submit", "worker_a", "T-1", "--result",
                      self.submit("T-1", commit))
        self.assertEqual(raised.exception.code, "path.boundary")

    def test_path_rename_checks_source_and_target(self):
        self.configure(max_active_tasks_per_executor=2)
        self.decide()
        self.post(self.packet("T-1", allowed_paths=["docs/README.md"]))
        self.claim("T-1")
        worktree = self.worktree("T-1")
        (worktree / "docs").mkdir()
        git(worktree, "mv", "README.md", "docs/README.md")
        git(worktree, "commit", "-qm", "rename")
        commit = git(worktree, "rev-parse", "HEAD")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "submit", "worker_a", "T-1", "--result",
                      self.submit("T-1", commit))
        self.assertEqual(raised.exception.code, "path.boundary")
        self.post(self.packet("T-2", allowed_paths=["README.md", "docs/README.md"]))
        self.claim("T-2")
        worktree = self.worktree("T-2")
        (worktree / "docs").mkdir()
        git(worktree, "mv", "README.md", "docs/README.md")
        git(worktree, "commit", "-qm", "rename")
        commit = git(worktree, "rev-parse", "HEAD")
        self.call("--device", "authority", "submit", "worker_a", "T-2", "--result",
                  self.submit("T-2", commit))

    def test_path_unicode_and_spaces_are_preserved(self):
        self.decide()
        name = "ünïcode file.txt"
        self.post(self.packet("T-1", allowed_paths=[name]))
        self.claim("T-1")
        commit = self.commit_file("T-1", name, "unicode\n")
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result",
                  self.submit("T-1", commit))

    def test_event_order_is_deterministic(self):
        self.decide()
        self.post(self.packet("T-1"))
        self.claim("T-1")
        events = self.events()
        ids = [event["event_id"] for event in events]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual([event["action"] for event in events],
                         ["decision.set", "task.post", "task.claim"])

    def test_supersede_rollback_keeps_old_task_ready(self):
        self.decide()
        self.post(self.packet("T-1"))
        with mock.patch.object(hub, "record", side_effect=hub.HubError("git.error", "audit unavailable")):
            with self.assertRaises(hub.HubError):
                self.call("--device", "client_primary", "supersede", "admin_agent", "T-1",
                          "--spec", json.dumps(self.packet("T-2")), "--reason", "better")
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-1")
        self.assertEqual(shown["task"]["status"], "ready")
        conn = sqlite3.connect(self.state)
        try:
            self.assertIsNone(conn.execute("SELECT 1 FROM tasks WHERE task_id='T-2'").fetchone())
        finally:
            conn.close()

    def test_failed_insert_does_not_consume_sequence(self):
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["task_naming"] = {"task_pattern": r"TASK-(?P<seq>\d{3})",
                               "chain_pattern": r"CHN-(?P<seq>\d{4})", "seq_digits": 3}
        self.write_config(value)
        self.decide()
        self.post(self.packet("TASK-001", chain_id="CHN-0001"))
        with mock.patch.object(hub, "record", side_effect=hub.HubError("git.error", "audit unavailable")):
            with self.assertRaises(hub.HubError):
                self.post(self.packet("TASK-002", chain_id="CHN-0002"))
        self.post(self.packet("TASK-002", chain_id="CHN-0002"))


if __name__ == "__main__":
    unittest.main()
