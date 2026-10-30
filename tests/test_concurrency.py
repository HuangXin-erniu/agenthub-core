from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, HubFixture, git, hub


class ConcurrencyTests(HubFixture):
    def post(self, task_id: str, assignee: str = "worker_a", priority: int | None = None) -> dict:
        packet = self.packet(task_id, assignee)
        if priority is not None:
            packet["priority"] = priority
        return super().post(packet)

    def test_default_config_rejects_a_second_claim_for_the_same_executor(self):
        self.decide()
        self.post("T-1")
        self.post("T-2")
        self.claim("T-1")
        with self.assertRaises(hub.HubError) as raised:
            self.claim("T-2")
        self.assertEqual(raised.exception.code, "task.capacity")
        self.assertIn("并发上限 1", raised.exception.message)

    def test_multiple_tasks_per_executor_run_independently(self):
        self.configure(max_active_tasks_per_executor=2)
        self.decide()
        self.post("T-1")
        self.post("T-2")
        first = self.claim("T-1")
        second = self.claim("T-2")
        self.assertEqual(first["task"]["status"], "in_progress")
        self.assertEqual(second["task"]["status"], "in_progress")
        self.assertNotEqual(first["task"]["worktree_path"], second["task"]["worktree_path"])
        self.assertNotEqual(first["task"]["branch"], second["task"]["branch"])
        for task_id, summary in (("T-1", "first"), ("T-2", "second")):
            progress = json.dumps({"summary": summary, "next_step": "continue", "evidence": []})
            self.call("--device", "authority", "checkpoint", "worker_a", task_id, "--progress", progress)
        shown_first = self.call("--device", "authority", "show", "worker_a", "T-1")
        shown_second = self.call("--device", "authority", "show", "worker_a", "T-2")
        self.assertEqual(shown_first["task"]["checkpoint"]["summary"], "first")
        self.assertEqual(shown_second["task"]["checkpoint"]["summary"], "second")
        self.submit_completed("T-1")
        shown_first = self.call("--device", "authority", "show", "worker_a", "T-1")
        shown_second = self.call("--device", "authority", "show", "worker_a", "T-2")
        self.assertEqual(shown_first["task"]["status"], "review")
        self.assertEqual(shown_second["task"]["status"], "in_progress")

    def test_capacity_limit_and_release_frees_a_slot(self):
        self.configure(max_active_tasks_per_executor=2)
        self.decide()
        for task_id in ("T-1", "T-2", "T-3"):
            self.post(task_id)
        self.claim("T-1")
        self.claim("T-2")
        with self.assertRaises(hub.HubError) as raised:
            self.claim("T-3")
        self.assertEqual(raised.exception.code, "task.capacity")
        self.assertIn("并发上限 2", raised.exception.message)
        self.call("--device", "authority", "release", "worker_a", "T-1", "--reason", "window restarted")
        released = self.call("--device", "authority", "show", "worker_a", "T-1")
        self.assertEqual(released["task"]["status"], "ready")
        self.assertIsNone(released["task"]["claimed_by"])
        self.assertTrue(Path(released["task"]["worktree_path"]).exists())
        claimed = self.claim("T-3")
        self.assertEqual(claimed["task"]["status"], "in_progress")

    def test_release_is_limited_to_the_owner_or_an_administrator(self):
        self.configure(max_active_tasks_per_executor=2)
        self.decide()
        self.post("T-A", "worker_a")
        self.post("T-B", "worker_b")
        self.claim("T-A", "worker_a")
        self.claim("T-B", "worker_b")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "release", "worker_a", "T-B", "--reason", "not mine")
        self.assertEqual(raised.exception.code, "identity.permission")
        self.call("--device", "client_primary", "release", "admin_agent", "T-B", "--reason", "worker_b offline")
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-B")
        self.assertEqual(shown["task"]["status"], "ready")
        self.assertIsNone(shown["task"]["claimed_by"])

    def test_release_rejects_a_task_that_is_not_in_progress(self):
        self.decide()
        self.post("T-READY")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "release", "admin_agent", "T-READY", "--reason", "noop")
        self.assertEqual(raised.exception.code, "task.state")

    def test_next_available_skips_claimed_tasks(self):
        self.configure(max_active_tasks_per_executor=2)
        self.decide()
        self.post("T-1")
        self.post("T-2")
        self.claim("T-1")
        available = self.call("--device", "authority", "next", "worker_a", "--available")
        self.assertEqual(available["task"]["id"], "T-2")
        self.assertEqual(available["concurrency"], {"active": 1, "limit": 2})
        self.claim("T-2")
        empty = self.call("--device", "authority", "next", "worker_a", "--available")
        self.assertIsNone(empty["task"])

    def test_claim_without_task_id_assigns_distinct_tasks_and_reports_empty(self):
        self.configure(max_active_tasks_per_executor=2)
        self.decide()
        self.post("T-1")
        self.post("T-2")
        first = self.claim(None)
        second = self.claim(None)
        third = self.claim(None)
        self.assertEqual({first["task"]["id"], second["task"]["id"]}, {"T-1", "T-2"})
        self.assertIsNone(third["task"])
        self.assertEqual(third["concurrency"], {"active": 2, "limit": 2})
        self.post("T-3")
        with self.assertRaises(hub.HubError) as raised:
            self.claim(None)
        self.assertEqual(raised.exception.code, "task.capacity")

    def test_claim_without_task_id_only_takes_the_callers_tasks(self):
        self.decide()
        self.post("T-A", "worker_a")
        self.post("T-B", "worker_b")
        first = self.claim(None, "worker_a")
        second = self.claim(None, "worker_b")
        self.assertEqual(first["task"]["id"], "T-A")
        self.assertEqual(second["task"]["id"], "T-B")

    def test_per_agent_limit_overrides_the_project_limit(self):
        value = self.configure(max_active_tasks_per_executor=3)
        value["agents"]["worker_b"]["max_active_tasks"] = 1
        self.write_config(value)
        self.decide()
        for task_id in ("T-A1", "T-A2", "T-A3"):
            self.post(task_id, "worker_a")
        for task_id in ("T-B1", "T-B2"):
            self.post(task_id, "worker_b")
        for task_id in ("T-A1", "T-A2", "T-A3"):
            self.claim(task_id, "worker_a")
        self.claim("T-B1", "worker_b")
        with self.assertRaises(hub.HubError) as raised:
            self.claim("T-B2", "worker_b")
        self.assertEqual(raised.exception.code, "task.capacity")
        self.assertIn("并发上限 1", raised.exception.message)

    def test_invalid_concurrency_configuration_is_rejected(self):
        cases = [
            ({"concurrency": "sometimes"}, "concurrency"),
            ({"concurrency": "multiple_tasks_per_executor", "max_active_tasks_per_executor": None}, "max_active_tasks_per_executor"),
            ({"concurrency": "multiple_tasks_per_executor", "max_active_tasks_per_executor": 0},
             "max_active_tasks_per_executor"),
            ({"concurrency": "one_active_task_per_executor", "max_active_tasks_per_executor": 4}, "只能为 1"),
            ({"isolation": "shared_worktree"}, "isolation"),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes):
                value = json.loads(self.config.read_text(encoding="utf-8"))
                value["execution"].update(changes)
                self.config.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(hub.HubError) as raised:
                    hub.load_config(self.config)
                self.assertEqual(raised.exception.code, "config.invalid")
                self.assertIn(message, raised.exception.message)
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["execution"] = {"repository": str(self.repo), "worktree_root": str(self.worktrees),
                              "max_active_tasks_per_executor": 1}
        value["agents"]["admin_agent"]["max_active_tasks"] = 1
        self.config.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(hub.HubError) as raised:
            hub.load_config(self.config)
        self.assertEqual(raised.exception.code, "config.invalid")

    def test_status_filters_by_executor(self):
        self.decide()
        self.post("T-A", "worker_a")
        self.post("T-B", "worker_b")
        mine = self.call("--device", "authority", "status", "worker_a", "--mine")
        self.assertEqual([task["id"] for task in mine["tasks"]], ["T-A"])
        other = self.call("--device", "client_primary", "status", "admin_agent", "--assignee", "worker_b")
        self.assertEqual([task["id"] for task in other["tasks"]], ["T-B"])
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "status", "worker_a", "--assignee", "worker_b")
        self.assertEqual(raised.exception.code, "identity.permission")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "status", "admin_agent", "--assignee", "nobody")
        self.assertEqual(raised.exception.code, "identity.unknown")

    def test_concurrent_claim_processes_do_not_double_claim(self):
        self.configure(max_active_tasks_per_executor=4)
        self.decide()
        for index in range(1, 5):
            self.post(f"T-{index}")
        command = [sys.executable, str(ROOT / "agent_hub.py"), *self.base,
                   "--device", "authority", "claim", "worker_a"]
        processes = [
            subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8")
            for _ in range(4)
        ]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout)["data"])
        claimed = [item["task"]["id"] for item in outputs if item["task"]]
        self.assertEqual(sorted(claimed), ["T-1", "T-2", "T-3", "T-4"])
        status = self.call("--device", "authority", "status", "worker_a", "--mine")
        self.assertEqual({task["status"] for task in status["tasks"]}, {"in_progress"})

    def test_close_rolls_back_when_the_audit_write_fails(self):
        self.decide()
        self.post("T-1")
        self.claim("T-1")
        self.submit_completed("T-1")
        self.call("--device", "client_primary", "review", "admin_agent", "T-1",
                  "--action", "approve", "--note", "ok")
        git(self.repo, "merge", "--ff-only", "agenthub/T-1")
        merged = git(self.repo, "rev-parse", "HEAD")
        with mock.patch.object(hub, "record", side_effect=hub.HubError("git.error", "audit unavailable")):
            with self.assertRaises(hub.HubError):
                self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                          "--merge-commit", merged, "--target-branch", "main", "--note", "integrated")
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-1")
        self.assertEqual(shown["task"]["status"], "approved")

    def test_checkpoint_rolls_back_when_the_audit_write_fails(self):
        self.decide()
        self.post("T-1")
        self.claim("T-1")
        progress = json.dumps({"summary": "half", "next_step": "finish", "evidence": []})
        with mock.patch.object(hub, "record", side_effect=hub.HubError("git.error", "audit unavailable")):
            with self.assertRaises(hub.HubError):
                self.call("--device", "authority", "checkpoint", "worker_a", "T-1", "--progress", progress)
        shown = self.call("--device", "authority", "show", "worker_a", "T-1")
        self.assertIsNone(shown["task"]["checkpoint"])


if __name__ == "__main__":
    unittest.main()
