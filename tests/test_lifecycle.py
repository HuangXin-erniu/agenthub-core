from __future__ import annotations

import json
import subprocess
import sys
import unittest

from support import ROOT, HubFixture, git, hub, init_repository


class LifecycleTests(HubFixture):
    def test_claim_checkpoint_and_cross_device_resume(self):
        self.decide()
        self.post()
        claimed = self.claim("T-1")
        self.assertEqual(claimed["task"]["status"], "in_progress")
        self.assertEqual(claimed["concurrency"], {"active": 1, "limit": 1})
        progress = json.dumps({"summary": "safe point", "next_step": "continue", "evidence": []})
        self.call("--device", "authority", "checkpoint", "worker_a", "T-1", "--progress", progress)
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_secondary", "checkpoint", "worker_a", "T-1", "--progress", progress)
        self.assertEqual(raised.exception.code, "task.state")
        self.call("--device", "authority", "handoff", "worker_a", "T-1",
                  "--to-device", "client_secondary", "--note", "move")
        self.call("--device", "client_secondary", "checkpoint", "worker_a", "T-1", "--progress", progress)
        resumed = self.call("--device", "client_secondary", "next", "worker_a")
        self.assertEqual(resumed["task"]["checkpoint"]["summary"], "safe point")

    def test_two_executors_receive_isolated_worktrees(self):
        self.decide()
        self.post(self.packet("T-R", "worker_a"))
        self.post(self.packet("T-C", "worker_b"))
        first = self.claim("T-R", "worker_a")
        second = self.claim("T-C", "worker_b")
        self.assertNotEqual(first["task"]["worktree_path"], second["task"]["worktree_path"])
        self.assertTrue(first["task"]["worktree_path"].endswith("T-R"))
        self.assertTrue(second["task"]["worktree_path"].endswith("T-C"))

    def test_submit_rejects_paths_outside_contract(self):
        self.decide()
        self.post()
        self.claim("T-1")
        worktree = self.worktree("T-1")
        (worktree / "extra.txt").write_text("not allowed\n", encoding="utf-8")
        git(worktree, "add", "extra.txt")
        git(worktree, "commit", "-qm", "extra")
        commit = git(worktree, "rev-parse", "HEAD")
        result = {"outcome": "completed", "summary": "done", "commit": commit,
                  "tests": ["unit"], "evidence": [f"commit:{commit}"]}
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "submit", "worker_a", "T-1",
                      "--result", json.dumps(result))
        self.assertEqual(raised.exception.code, "path.boundary")

    def test_decider_context_is_compact(self):
        self.decide()
        self.post()
        context = self.call("--device", "client_primary", "context", "admin_agent")
        self.assertEqual(context["role_mode"], "decider")
        self.assertIsNone(context["focus"])
        self.assertNotIn("overview", context)
        self.assertNotIn("tasks", context)
        self.assertEqual(set(context["counts"]), {"ready", "in_progress", "blocked", "review", "approved"})
        self.assertEqual(context["decisions"][0]["decision_key"], "D-1")

    def test_role_permissions(self):
        self.decide()
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "post", "worker_a", "--spec", json.dumps(self.packet()))
        self.assertEqual(raised.exception.code, "identity.permission")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "claim", "admin_agent", "T-1")
        self.assertEqual(raised.exception.code, "identity.device")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "claim", "planner_agent", "T-1")
        self.assertEqual(raised.exception.code, "identity.permission")

    def test_unknown_contract_field_is_rejected(self):
        self.decide()
        with self.assertRaises(hub.HubError) as raised:
            self.post(self.packet(scope=["legacy"]))
        self.assertEqual(raised.exception.code, "contract.invalid")
        self.assertIn("未知字段", raised.exception.message)

    def test_close_verifies_merge_and_cleans_worktree(self):
        self.decide()
        self.post()
        self.claim("T-1")
        self.submit_completed("T-1")
        self.call("--device", "client_primary", "review", "admin_agent", "T-1",
                  "--action", "approve", "--note", "ok")
        git(self.repo, "merge", "--ff-only", "agenthub/T-1")
        merge = git(self.repo, "rev-parse", "HEAD")
        closed = self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                           "--merge-commit", merge, "--target-branch", "main", "--note", "done")
        self.assertEqual(closed["status"], "closed")
        self.assertFalse(self.worktree("T-1").exists())
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-1")
        self.assertEqual(shown["task"]["status"], "closed")
        self.assertIsNone(shown["task"]["claimed_by"])

    def test_close_rejects_a_merge_that_is_not_the_target_head(self):
        self.decide()
        self.post()
        self.claim("T-1")
        self.submit_completed("T-1")
        self.call("--device", "client_primary", "review", "admin_agent", "T-1",
                  "--action", "approve", "--note", "ok")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                      "--merge-commit", self.base_commit, "--target-branch", "main", "--note", "bad")
        self.assertEqual(raised.exception.code, "git.error")

    def test_cancel_permissions_and_forced_cancel(self):
        self.decide()
        self.post()
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "cancel", "planner_agent", "T-1", "--reason", "no")
        self.assertEqual(raised.exception.code, "identity.permission")
        self.call("--device", "client_primary", "cancel", "admin_agent", "T-1", "--reason", "drop")
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-1")
        self.assertEqual(shown["task"]["status"], "cancelled")
        self.post(self.packet("T-2"))
        self.claim("T-2")
        self.call("--device", "client_primary", "cancel", "admin_agent", "T-2", "--reason", "forced")
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-2")
        self.assertEqual(shown["task"]["status"], "cancelled")
        self.assertTrue(self.worktree("T-2").exists())

    def test_supersede_preserves_lineage(self):
        self.decide()
        self.post()
        result = self.call("--device", "client_primary", "supersede", "admin_agent", "T-1",
                           "--spec", json.dumps(self.packet("T-2")), "--reason", "better")
        self.assertEqual(result["old_task"], "T-1")
        self.assertEqual(result["new_task"], "T-2")
        old = self.call("--device", "client_primary", "show", "admin_agent", "T-1")["task"]
        new = self.call("--device", "client_primary", "show", "admin_agent", "T-2")["task"]
        self.assertEqual(old["status"], "cancelled")
        self.assertEqual(old["superseded_by"], "T-2")
        self.assertEqual(new["status"], "ready")
        self.assertEqual(new["supersedes"], "T-1")

    def test_rework_preserves_blocked_submission(self):
        self.decide()
        self.post()
        self.claim("T-1")
        blocked = {"outcome": "blocked", "summary": "blocked", "blocker": "x", "decision_needed": "y"}
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", json.dumps(blocked))
        self.call("--device", "client_primary", "review", "admin_agent", "T-1",
                  "--action", "rework", "--note", "evidence needed")
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-1")
        self.assertEqual(shown["task"]["status"], "ready")
        self.assertEqual(shown["submissions"][0]["result"]["decision_needed"], "y")
        self.assertEqual(shown["task"]["latest_review"]["action"], "rework")

    def test_prune_reports_then_removes_terminal_worktree(self):
        self.decide()
        self.post()
        self.claim("T-1")
        self.call("--device", "client_primary", "cancel", "admin_agent", "T-1", "--reason", "drop")
        report = self.call("--device", "client_primary", "prune", "admin_agent")
        self.assertTrue(report["applied"] is False)
        self.assertTrue(any(item["task_id"] == "T-1" for item in report["candidates"]))
        applied = self.call("--device", "client_primary", "prune", "admin_agent", "--apply")
        self.assertEqual(applied["removed"], 1)
        self.assertFalse(self.worktree("T-1").exists())

    def test_second_project_has_independent_state(self):
        second_repo = self.root / "repo-two"
        init_repository(second_repo)
        second_state = self.root / "second.db"
        second_config = self.root / "second.json"
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["project_id"] = "second-project"
        value["state_authority"]["database"] = str(second_state)
        value["execution"] = {"repository": str(second_repo), "worktree_root": str(self.root / "worktrees-two")}
        second_config.write_text(json.dumps(value), encoding="utf-8")
        self.decide()
        first = self.call("--device", "client_primary", "context", "admin_agent")
        second = subprocess.run(
            [sys.executable, str(ROOT / "agent_hub.py"), "--local", "--config", str(second_config),
             "--state", str(second_state), "--device", "client_primary", "decision", "admin_agent",
             "--key", "SECOND", "--summary", "s", "--rationale", "r"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        self.assertTrue(json.loads(second.stdout)["ok"])
        result = subprocess.run(
            [sys.executable, str(ROOT / "agent_hub.py"), "--local", "--config", str(second_config),
             "--state", str(second_state), "--device", "client_primary", "context", "admin_agent"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        context = json.loads(result.stdout)["data"]
        self.assertEqual(context["project"], "second-project")
        self.assertEqual(context["decisions"][0]["decision_key"], "SECOND")
        self.assertEqual(first["project"], "test")
        self.assertEqual(first["decisions"][0]["decision_key"], "D-1")

    def test_smoke_script_uses_disposable_state(self):
        before_refs = git(self.repo, "show-ref")
        before_worktrees = git(self.repo, "worktree", "list", "--porcelain")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/smoke_project.py"),
             "--config", str(self.config), "--runtime", str(ROOT / "agent_hub.py")],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["ok"])
        self.assertTrue(output["isolated_clone"])
        self.assertEqual(git(self.repo, "show-ref"), before_refs)
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain"), before_worktrees)
        self.assertFalse(self.state.exists())

    def test_show_submission_limit(self):
        self.decide()
        self.post()
        self.claim("T-1")
        self.submit_completed("T-1")
        shown = self.call("--device", "authority", "show", "worker_a", "T-1", "--submissions", "0")
        self.assertEqual(shown["submissions"], [])
        shown = self.call("--device", "authority", "show", "worker_a", "T-1")
        self.assertEqual(len(shown["submissions"]), 1)

    def test_audit_filters(self):
        self.decide()
        self.post()
        self.claim("T-1")
        by_entity = self.call("--device", "client_primary", "audit", "admin_agent", "--entity", "T-1")
        self.assertTrue(by_entity["events"])
        self.assertTrue(all(event["entity_id"] == "T-1" for event in by_entity["events"]))
        by_action = self.call("--device", "client_primary", "audit", "admin_agent", "--action", "task.post")
        self.assertEqual([event["action"] for event in by_action["events"]], ["task.post"])

    def test_prune_skips_a_live_task(self):
        self.decide()
        self.post()
        self.claim("T-1")
        report = self.call("--device", "client_primary", "prune", "admin_agent")
        live = [item for item in report["candidates"] if item.get("task_id") == "T-1"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["action"], "skipped")
        self.assertIn("live task", live[0]["reason"])



if __name__ == "__main__":
    unittest.main()
