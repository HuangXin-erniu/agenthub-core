from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_agenthub as fixtures

hub = fixtures.hub


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        fixtures.AgentHubV23Tests.setUp(self)
        self.base_config = json.loads(self.config.read_text(encoding="utf-8"))

    def tearDown(self):
        fixtures.AgentHubV23Tests.tearDown(self)

    git = fixtures.AgentHubV23Tests.git
    call = fixtures.AgentHubV23Tests.call
    decide = fixtures.AgentHubV23Tests.decide
    packet = fixtures.AgentHubV23Tests.packet

    def configure(self, **changes):
        """Rewrite the fixture config from a clean base for each scenario."""
        config = json.loads(json.dumps(self.base_config))
        execution = config["execution"]
        for key in ("concurrency", "max_active_tasks_per_executor", "isolation"):
            if key not in changes:
                continue
            if changes[key] is None:
                execution.pop(key, None)
            else:
                execution[key] = changes[key]
        for agent, value in changes.get("overrides", {}).items():
            config["agents"][agent]["max_active_tasks"] = value
        self.config.write_text(json.dumps(config), encoding="utf-8")
        return config

    def post(self, task_id, assignee="worker_a", priority=None):
        spec = json.loads(self.packet(task_id, assignee))
        if priority is not None:
            spec["priority"] = priority
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(spec))

    def claim(self, task_id, agent="worker_a"):
        return json.loads(self.call("--device", "authority", "claim", agent, task_id))

    def test_default_config_rejects_a_second_claim_for_the_same_executor(self):
        self.decide()
        self.post("T-1")
        self.post("T-2")
        self.claim("T-1")
        with self.assertRaises(hub.HubError) as raised:
            self.claim("T-2")
        self.assertIn("并发上限 1", str(raised.exception))

    def test_multiple_tasks_per_executor_run_independently(self):
        self.configure(concurrency="multiple_tasks_per_executor", max_active_tasks_per_executor=2)
        self.decide()
        self.post("T-1")
        self.post("T-2")
        first = self.claim("T-1")
        second = self.claim("T-2")
        self.assertEqual(first["status"], "in_progress")
        self.assertEqual(second["status"], "in_progress")
        self.assertNotEqual(first["worktree_path"], second["worktree_path"])
        self.assertNotEqual(first["branch"], second["branch"])

        for task_id, summary in (("T-1", "first"), ("T-2", "second")):
            progress = json.dumps({"summary": summary, "next_step": "continue", "evidence": []})
            self.call("--device", "authority", "checkpoint", "worker_a", task_id, "--progress", progress)
        shown_first = json.loads(self.call("--device", "authority", "show", "worker_a", "T-1"))
        shown_second = json.loads(self.call("--device", "authority", "show", "worker_a", "T-2"))
        self.assertEqual(shown_first["task"]["checkpoint"]["summary"], "first")
        self.assertEqual(shown_second["task"]["checkpoint"]["summary"], "second")

        worktree = Path(first["worktree_path"])
        (worktree / "README.md").write_text("base\nfirst\n", encoding="utf-8")
        self.git("add", "README.md", cwd=worktree)
        self.git("commit", "-m", "complete first", cwd=worktree)
        commit = self.git("rev-parse", "HEAD", cwd=worktree)
        result = json.dumps({"outcome": "completed", "summary": "done", "commit": commit,
                             "tests": ["pass"], "evidence": [f"commit:{commit}"]})
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", result)

        shown_first = json.loads(self.call("--device", "authority", "show", "worker_a", "T-1"))
        shown_second = json.loads(self.call("--device", "authority", "show", "worker_a", "T-2"))
        self.assertEqual(shown_first["task"]["status"], "review")
        self.assertEqual(shown_second["task"]["status"], "in_progress")

    def test_capacity_limit_and_release_frees_a_slot(self):
        self.configure(concurrency="multiple_tasks_per_executor", max_active_tasks_per_executor=2)
        self.decide()
        for task_id in ("T-1", "T-2", "T-3"):
            self.post(task_id)
        self.claim("T-1")
        self.claim("T-2")
        with self.assertRaises(hub.HubError) as raised:
            self.claim("T-3")
        self.assertIn("并发上限 2", str(raised.exception))

        self.call("--device", "authority", "release", "worker_a", "T-1", "--reason", "window restarted")
        released = json.loads(self.call("--device", "authority", "show", "worker_a", "T-1"))
        self.assertEqual(released["task"]["status"], "ready")
        self.assertIsNone(released["task"]["claimed_by"])
        self.assertTrue(Path(released["task"]["worktree_path"]).exists())

        claimed = self.claim("T-3")
        self.assertEqual(claimed["status"], "in_progress")

    def test_release_is_limited_to_the_owner_or_an_administrator(self):
        self.configure(concurrency="multiple_tasks_per_executor", max_active_tasks_per_executor=2)
        self.decide()
        self.post("T-A", "worker_a")
        self.post("T-B", "worker_b")
        self.claim("T-A", "worker_a")
        self.claim("T-B", "worker_b")
        with self.assertRaises(hub.HubError):
            self.call("--device", "authority", "release", "worker_a", "T-B", "--reason", "not mine")

        self.call("--device", "client_primary", "release", "admin_agent", "T-B", "--reason", "worker_b offline")
        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-B"))
        self.assertEqual(shown["task"]["status"], "ready")
        self.assertIsNone(shown["task"]["claimed_by"])

    def test_release_rejects_a_task_that_is_not_in_progress(self):
        self.decide()
        self.post("T-READY")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "release", "admin_agent", "T-READY", "--reason", "noop")
        self.assertIn("只有 in_progress", str(raised.exception))

    def test_next_available_skips_claimed_tasks(self):
        self.configure(concurrency="multiple_tasks_per_executor", max_active_tasks_per_executor=2)
        self.decide()
        self.post("T-1")
        self.post("T-2")
        self.claim("T-1")
        available = json.loads(self.call("--device", "authority", "next", "worker_a", "--available"))
        self.assertEqual(available["task"]["id"], "T-2")
        self.assertEqual(available["concurrency"], {"active": 1, "limit": 2})

        self.claim("T-2")
        empty = json.loads(self.call("--device", "authority", "next", "worker_a", "--available"))
        self.assertIsNone(empty["task"])

    def test_claim_next_assigns_distinct_tasks_and_reports_empty(self):
        self.configure(concurrency="multiple_tasks_per_executor", max_active_tasks_per_executor=2)
        self.decide()
        self.post("T-1")
        self.post("T-2")
        first = json.loads(self.call("--device", "authority", "claim-next", "worker_a"))
        second = json.loads(self.call("--device", "authority", "claim-next", "worker_a"))
        third = json.loads(self.call("--device", "authority", "claim-next", "worker_a"))
        self.assertEqual({first["task"]["id"], second["task"]["id"]}, {"T-1", "T-2"})
        self.assertIsNone(third["task"])
        self.assertEqual(third["concurrency"], {"active": 2, "limit": 2})

        self.post("T-3")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "claim-next", "worker_a")
        self.assertIn("并发上限 2", str(raised.exception))

    def test_claim_next_only_takes_the_callers_tasks(self):
        self.decide()
        self.post("T-A", "worker_a")
        self.post("T-B", "worker_b")
        first = json.loads(self.call("--device", "authority", "claim-next", "worker_a"))
        second = json.loads(self.call("--device", "authority", "claim-next", "worker_b"))
        self.assertEqual(first["task"]["id"], "T-A")
        self.assertEqual(second["task"]["id"], "T-B")

    def test_per_agent_limit_overrides_the_project_limit(self):
        self.configure(concurrency="multiple_tasks_per_executor", max_active_tasks_per_executor=3,
                       overrides={"worker_b": 1})
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
        self.assertIn("并发上限 1", str(raised.exception))

    def test_invalid_concurrency_configuration_is_rejected(self):
        cases = [
            ({"concurrency": "sometimes"}, "execution.concurrency"),
            ({"concurrency": "multiple_tasks_per_executor"}, "max_active_tasks_per_executor"),
            ({"concurrency": "multiple_tasks_per_executor", "max_active_tasks_per_executor": 0},
             "max_active_tasks_per_executor"),
            ({"concurrency": "one_active_task_per_executor", "max_active_tasks_per_executor": 4}, "只能为 1"),
            ({"isolation": "shared_worktree"}, "execution.isolation"),
            ({"overrides": {"worker_a": 2}}, "不能超过项目上限 1"),
            ({"overrides": {"admin_agent": 1}}, "不是执行者"),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes):
                self.configure(**changes)
                with self.assertRaises(hub.HubError) as raised:
                    hub.load_config(self.config)
                self.assertIn(message, str(raised.exception))

    def test_status_filters_by_executor(self):
        self.decide()
        self.post("T-A", "worker_a")
        self.post("T-B", "worker_b")
        mine = json.loads(self.call("--device", "client_primary", "status", "worker_a", "--json", "--mine"))
        self.assertEqual([task["id"] for task in mine["tasks"]], ["T-A"])
        other = json.loads(self.call("--device", "client_primary", "status", "worker_a", "--json",
                                     "--assignee", "worker_b"))
        self.assertEqual([task["id"] for task in other["tasks"]], ["T-B"])
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_primary", "status", "worker_a", "--json", "--assignee", "nobody")

    def test_concurrent_claim_next_processes_do_not_double_claim(self):
        self.configure(concurrency="multiple_tasks_per_executor", max_active_tasks_per_executor=4)
        self.decide()
        for index in range(1, 5):
            self.post(f"T-{index}")
        command = [sys.executable, str(fixtures.ROOT / "agent_hub.py"), *self.base,
                   "--device", "authority", "claim-next", "worker_a"]
        processes = [
            subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8")
            for _ in range(4)
        ]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout))
        claimed = [item["task"]["id"] for item in outputs if item["task"]]
        self.assertEqual(sorted(claimed), ["T-1", "T-2", "T-3", "T-4"])

        status = json.loads(self.call("--device", "client_primary", "status", "worker_a", "--json", "--mine"))
        self.assertEqual({task["status"] for task in status["tasks"]}, {"in_progress"})

    def test_close_rolls_back_when_the_audit_write_fails(self):
        self.decide()
        self.post("T-1")
        claimed = self.claim("T-1")
        worktree = Path(claimed["worktree_path"])
        (worktree / "README.md").write_text("base\nclosed\n", encoding="utf-8")
        self.git("add", "README.md", cwd=worktree)
        self.git("commit", "-m", "complete", cwd=worktree)
        commit = self.git("rev-parse", "HEAD", cwd=worktree)
        result = json.dumps({"outcome": "completed", "summary": "done", "commit": commit,
                             "tests": ["pass"], "evidence": [f"commit:{commit}"]})
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", result)
        self.call("--device", "client_primary", "review", "admin_agent", "T-1", "--action", "approve", "--note", "ok")
        self.git("merge", "--ff-only", "agenthub/T-1")
        merged = self.git("rev-parse", "HEAD")

        with mock.patch.object(hub, "record", side_effect=hub.HubError("audit unavailable")):
            with self.assertRaises(hub.HubError):
                self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                          "--merge-commit", merged, "--target-branch", "main", "--note", "integrated")

        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-1"))
        self.assertEqual(shown["task"]["status"], "approved")

    def test_checkpoint_rolls_back_when_the_audit_write_fails(self):
        self.decide()
        self.post("T-1")
        self.claim("T-1")
        progress = json.dumps({"summary": "half", "next_step": "finish", "evidence": []})
        with mock.patch.object(hub, "record", side_effect=hub.HubError("audit unavailable")):
            with self.assertRaises(hub.HubError):
                self.call("--device", "authority", "checkpoint", "worker_a", "T-1", "--progress", progress)

        shown = json.loads(self.call("--device", "authority", "show", "worker_a", "T-1"))
        self.assertIsNone(shown["task"]["checkpoint"])


if __name__ == "__main__":
    unittest.main()
