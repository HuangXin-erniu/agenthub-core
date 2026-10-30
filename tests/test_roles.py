from __future__ import annotations

import json
import sqlite3
import unittest

from support import HubFixture, git, hub


class RoleTests(HubFixture):
    """One Agent ID may hold decision and execution roles at the same time."""

    def setUp(self):
        super().setUp()
        self.add_agent("solo_agent", ["administrator", "decider", "executor"])
        self.add_agent("hybrid_agent", ["decider", "executor"])
        self.decision_ready = False

    def set_self_review(self, agent: str, policy: str) -> None:
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["agents"][agent]["self_review"] = policy
        self.write_config(value)

    def configure_concurrency(self, limit: int) -> None:
        self.configure(max_active_tasks_per_executor=limit)

    def decide_as_solo(self) -> None:
        if self.decision_ready:
            return
        self.call("--device", "client_primary", "decision", "solo_agent",
                  "--key", "D-1", "--summary", "route", "--rationale", "evidence")
        self.decision_ready = True

    def post(self, task_id: str = "T-1", assignee: str = "solo_agent",
             poster: str = "solo_agent") -> dict:
        self.decide_as_solo()
        return self.call("--device", "client_primary", "post", poster,
                         "--spec", json.dumps(self.packet(task_id, assignee)))

    def claim(self, task_id: str = "T-1", agent: str = "solo_agent",
              session: str | None = None) -> dict:
        return super().claim(task_id, agent, "authority", session)

    def submit_completed(self, task_id: str, agent: str, claimed: dict,
                         session: str | None = None) -> dict:
        worktree = claimed["task"]["worktree_path"]
        commit = self.commit_work(task_id)
        result = json.dumps({
            "outcome": "completed", "summary": f"done {task_id}", "commit": commit,
            "tests": ["unit"], "evidence": [f"commit:{commit}"],
        })
        args = ["--device", "authority"]
        if session:
            args.extend(["--session", session])
        args.extend(["submit", agent, task_id, "--result", result])
        return self.call(*args)

    def submit_blocked(self, task_id: str, agent: str, session: str | None = None) -> dict:
        result = json.dumps({
            "outcome": "blocked", "summary": "blocked", "blocker": "dependency",
            "decision_needed": "choose a path",
        })
        args = ["--device", "authority"]
        if session:
            args.extend(["--session", session])
        args.extend(["submit", agent, task_id, "--result", result])
        return self.call(*args)

    def review(self, task_id: str, agent: str = "solo_agent", action: str = "approve",
               note: str = "ok") -> dict:
        return self.call("--device", "client_primary", "review", agent, task_id,
                         "--action", action, "--note", note)

    def show(self, task_id: str, agent: str = "solo_agent", device: str = "client_primary") -> dict:
        return self.call("--device", device, "show", agent, task_id)

    def context(self, agent: str = "solo_agent", session: str | None = None,
                device: str = "client_primary") -> dict:
        args = ["--device", device]
        if session:
            args.extend(["--session", session])
        args.extend(["context", agent])
        return self.call(*args)

    def test_context_is_a_unified_workbench_for_a_dual_role_agent(self):
        self.post()
        context = self.context()
        self.assertEqual(context["role_mode"], "hybrid")
        self.assertEqual(context["focus"]["id"], "T-1")
        self.assertEqual(context["focus"]["status"], "ready")
        self.assertEqual(context["review_queue"], [])
        self.assertEqual(context["self_review"], {"policy": "allow", "capable": True})
        self.assertNotIn("overview", context)
        self.assertNotIn("tasks", context)
        self.assertEqual(context["counts"]["ready"], 1)
        claimed = self.claim()
        self.submit_completed("T-1", "solo_agent", claimed)
        context = self.context()
        self.assertEqual(context["focus"]["status"], "review")
        self.assertEqual(context["review_queue"][0]["id"], "T-1")
        self.assertEqual(context["review_queue"][0]["status"], "review")
        self.assertTrue(context["review_queue"][0]["self_review"])

    def test_dual_role_agent_can_show_another_executors_task(self):
        self.post(assignee="worker_a")
        shown = self.show("T-1", "hybrid_agent")
        self.assertEqual(shown["task"]["assignee"], "worker_a")
        with self.assertRaises(hub.HubError) as raised:
            self.show("T-1", "worker_b")
        self.assertEqual(raised.exception.code, "identity.permission")

    def test_self_review_is_recorded_in_the_review_packet_and_audit(self):
        self.post()
        claimed = self.claim()
        self.submit_completed("T-1", "solo_agent", claimed)
        self.review("T-1", "solo_agent")
        shown = self.show("T-1")["task"]
        self.assertEqual(shown["status"], "approved")
        self.assertTrue(shown["latest_review"]["self_review"])
        audit = self.call("--device", "client_primary", "audit", "solo_agent", "--limit", "20")
        approved = next(event for event in audit["events"] if event["action"] == "task.review.approve")
        self.assertTrue(approved["detail"]["self_review"])

    def test_self_review_forbid_blocks_approval_until_an_independent_reviewer(self):
        self.set_self_review("solo_agent", "forbid")
        self.assertEqual(self.context()["self_review"]["policy"], "forbid")
        self.post()
        claimed = self.claim()
        self.submit_completed("T-1", "solo_agent", claimed)
        with self.assertRaises(hub.HubError) as raised:
            self.review("T-1", "solo_agent")
        self.assertEqual(raised.exception.code, "identity.permission")
        self.assertEqual(self.show("T-1", "admin_agent")["task"]["status"], "review")
        self.review("T-1", "admin_agent")
        self.assertEqual(self.show("T-1", "admin_agent")["task"]["status"], "approved")

    def test_self_review_forbid_blocks_rework_of_own_submission(self):
        self.set_self_review("solo_agent", "forbid")
        self.post()
        self.claim()
        self.submit_blocked("T-1", "solo_agent")
        with self.assertRaises(hub.HubError) as raised:
            self.review("T-1", "solo_agent", action="rework")
        self.assertEqual(raised.exception.code, "identity.permission")
        self.assertEqual(self.show("T-1", "admin_agent")["task"]["status"], "blocked")
        self.review("T-1", "admin_agent", action="rework")
        self.assertEqual(self.show("T-1", "admin_agent")["task"]["status"], "ready")

    def test_windows_of_one_agent_share_the_review_queue_but_not_the_execution_focus(self):
        self.configure_concurrency(2)
        self.post("T-1")
        self.post("T-2")
        first = self.claim("T-1", session="win-a")
        window_a = self.context(session="win-a")
        window_b = self.context(session="win-b")
        self.assertEqual(window_a["focus"]["id"], "T-1")
        self.assertEqual(window_b["focus"]["id"], "T-2")
        self.submit_completed("T-1", "solo_agent", first, session="win-a")
        window_b = self.context(session="win-b")
        self.assertEqual(window_b["focus"]["id"], "T-2")
        self.assertEqual(window_b["review_queue"][0]["id"], "T-1")
        self.assertTrue(window_b["review_queue"][0]["self_review"])
        window_a = self.context(session="win-a")
        self.assertEqual(window_a["focus"]["id"], "T-1")
        self.assertEqual(window_a["focus"]["status"], "review")

    def test_close_clears_the_claim_and_keeps_the_executor_in_the_event(self):
        self.decide()
        self.post("T-1", "worker_a", "admin_agent")
        claimed = super().claim("T-1", "worker_a", "authority")
        self.submit_completed("T-1", "worker_a", claimed)
        self.review("T-1", "admin_agent")
        git(self.repo, "merge", "--ff-only", "agenthub/T-1")
        self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                  "--merge-commit", git(self.repo, "rev-parse", "HEAD"),
                  "--target-branch", "main", "--note", "merged")
        self.assertIsNone(self.show("T-1", "admin_agent")["task"]["claimed_by"])
        conn = sqlite3.connect(self.state)
        try:
            claimed_by = conn.execute("SELECT claimed_by FROM tasks WHERE task_id='T-1'").fetchone()[0]
            close_detail = json.loads(conn.execute(
                "SELECT detail_json FROM events WHERE entity_id='T-1' AND action='task.close'"
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertIsNone(claimed_by)
        self.assertEqual(close_detail["executor"], "worker_a")

    def test_config_rejects_invalid_self_review_and_non_array_roles(self):
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["agents"]["solo_agent"]["self_review"] = "sometimes"
        self.write_config(value)
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "status", "solo_agent")
        self.assertEqual(raised.exception.code, "config.invalid")
        value["agents"]["solo_agent"]["self_review"] = "allow"
        value["agents"]["solo_agent"]["roles"] = "executor"
        self.write_config(value)
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "status", "solo_agent")
        self.assertEqual(raised.exception.code, "config.invalid")

    def test_config_rejects_self_review_on_a_single_role_agent(self):
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["agents"]["worker_a"]["self_review"] = "forbid"
        self.write_config(value)
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "status", "admin_agent")
        self.assertEqual(raised.exception.code, "config.invalid")


if __name__ == "__main__":
    unittest.main()
