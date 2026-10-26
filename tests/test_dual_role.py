from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

import test_agenthub as fixtures

hub = fixtures.hub


class DualRoleTests(unittest.TestCase):
    """One Agent ID may hold decision and execution roles at the same time."""

    def setUp(self):
        fixtures.AgentHubV23Tests.setUp(self)
        self.add_agent("solo_agent", ["administrator", "decider", "executor"])
        self.add_agent("hybrid_agent", ["decider", "executor"])
        self.decision_ready = False

    tearDown = fixtures.AgentHubV23Tests.tearDown
    git = fixtures.AgentHubV23Tests.git
    call = fixtures.AgentHubV23Tests.call
    packet = fixtures.AgentHubV23Tests.packet
    decide = fixtures.AgentHubV23Tests.decide

    def add_agent(self, agent, roles):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["agents"][agent] = {
            "roles": roles,
            "devices": ["authority", "client_primary", "client_secondary"],
        }
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def set_self_review(self, agent, policy):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["agents"][agent]["self_review"] = policy
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def configure_concurrency(self, limit):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["execution"]["concurrency"] = "multiple_tasks_per_executor"
        config["execution"]["max_active_tasks_per_executor"] = limit
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def decide_as_solo(self):
        if self.decision_ready:
            return
        self.call("--device", "client_primary", "decision", "solo_agent",
                  "--key", "D-1", "--summary", "route", "--rationale", "evidence")
        self.decision_ready = True

    def post(self, task_id="T-1", assignee="solo_agent", poster="solo_agent"):
        self.decide_as_solo()
        self.call("--device", "client_primary", "post", poster, "--spec", self.packet(task_id, assignee))

    def claim(self, task_id="T-1", agent="solo_agent", session=None):
        args = ["claim", agent, task_id]
        if session:
            args = ["--session", session, *args]
        return json.loads(self.call("--device", "authority", *args))

    def commit_work(self, task_id, worktree):
        path = Path(worktree) / "README.md"
        path.write_text(path.read_text(encoding="utf-8") + f"{task_id}\n", encoding="utf-8")
        self.git("add", "README.md", cwd=worktree)
        self.git("commit", "-m", f"work {task_id}", cwd=worktree)
        return self.git("rev-parse", "HEAD", cwd=worktree)

    def submit_completed(self, task_id, agent, claimed, session=None):
        worktree = claimed["worktree_path"]
        commit = self.commit_work(task_id, worktree)
        result = json.dumps({
            "outcome": "completed", "summary": f"done {task_id}", "commit": commit,
            "tests": ["unit"], "evidence": [f"commit:{commit}"],
        })
        args = ["submit", agent, task_id, "--result", result]
        if session:
            args = ["--session", session, *args]
        self.call("--device", "authority", *args)

    def submit_blocked(self, task_id, agent, session=None):
        result = json.dumps({
            "outcome": "blocked", "summary": "blocked", "blocker": "dependency",
            "decision_needed": "choose a path",
        })
        args = ["submit", agent, task_id, "--result", result]
        if session:
            args = ["--session", session, *args]
        self.call("--device", "authority", *args)

    def review(self, task_id, agent="solo_agent", action="approve", note="ok"):
        self.call("--device", "client_primary", "review", agent, task_id,
                  "--action", action, "--note", note)

    def show(self, task_id, agent="solo_agent", device="client_primary"):
        return json.loads(self.call("--device", device, "show", agent, task_id))

    def context(self, agent="solo_agent", session=None, device="client_primary"):
        args = ["context", agent]
        if session:
            args = ["--session", session, *args]
        return json.loads(self.call("--device", device, *args))

    def test_context_is_a_unified_workbench_for_a_dual_role_agent(self):
        self.post()
        context = self.context()
        self.assertEqual(context["role_mode"], "hybrid")
        self.assertEqual(context["tasks"][0]["id"], "T-1")
        self.assertEqual(context["tasks"][0]["status"], "ready")
        self.assertEqual(context["overview"][0]["id"], "T-1")
        self.assertEqual(context["review_queue"], [])
        self.assertEqual(context["self_review"], {"policy": "allow", "capable": True})

        claimed = self.claim()
        self.submit_completed("T-1", "solo_agent", claimed)
        context = self.context()
        self.assertEqual(context["tasks"][0]["status"], "review")
        self.assertEqual(context["review_queue"][0]["id"], "T-1")
        self.assertEqual(context["review_queue"][0]["status"], "review")
        self.assertTrue(context["review_queue"][0]["self_review"])

    def test_dual_role_agent_can_show_another_executors_task(self):
        self.post(assignee="worker_a")
        shown = self.show("T-1", "hybrid_agent")
        self.assertEqual(shown["task"]["assignee"], "worker_a")
        with self.assertRaises(hub.HubError):
            self.show("T-1", "worker_b")

    def test_self_review_is_recorded_in_the_review_packet_and_audit(self):
        self.post()
        claimed = self.claim()
        self.submit_completed("T-1", "solo_agent", claimed)
        self.review("T-1", "solo_agent")

        shown = self.show("T-1")["task"]
        self.assertEqual(shown["status"], "approved")
        self.assertTrue(shown["latest_review"]["self_review"])
        audit = json.loads(self.call("--device", "client_primary", "audit", "solo_agent", "--limit", "20"))
        approved = next(event for event in audit if event["action"] == "task.review.approve")
        self.assertTrue(approved["detail"]["self_review"])

    def test_self_review_forbid_blocks_approval_until_an_independent_reviewer(self):
        self.set_self_review("solo_agent", "forbid")
        self.assertEqual(self.context()["self_review"]["policy"], "forbid")
        self.post()
        claimed = self.claim()
        self.submit_completed("T-1", "solo_agent", claimed)

        with self.assertRaises(hub.HubError):
            self.review("T-1", "solo_agent")
        self.assertEqual(self.show("T-1", "admin_agent")["task"]["status"], "review")

        self.review("T-1", "admin_agent")
        self.assertEqual(self.show("T-1", "admin_agent")["task"]["status"], "approved")

    def test_self_review_forbid_blocks_rework_of_own_submission(self):
        self.set_self_review("solo_agent", "forbid")
        self.post()
        self.claim()
        self.submit_blocked("T-1", "solo_agent")

        with self.assertRaises(hub.HubError):
            self.review("T-1", "solo_agent", action="rework")
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
        self.assertEqual(window_a["tasks"][0]["id"], "T-1")
        self.assertEqual(window_b["tasks"][0]["id"], "T-2")
        self.assertEqual({task["id"] for task in window_b["overview"]}, {"T-1", "T-2"})

        self.submit_completed("T-1", "solo_agent", first, session="win-a")
        window_b = self.context(session="win-b")
        self.assertEqual(window_b["tasks"][0]["id"], "T-2")
        self.assertEqual(window_b["review_queue"][0]["id"], "T-1")
        self.assertTrue(window_b["review_queue"][0]["self_review"])
        window_a = self.context(session="win-a")
        self.assertEqual(window_a["tasks"][0]["id"], "T-1")
        self.assertEqual(window_a["tasks"][0]["status"], "review")

    def test_close_clears_the_claim_and_keeps_the_executor_in_the_event(self):
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-1", "worker_a"))
        claimed = self.claim("T-1", "worker_a")
        self.submit_completed("T-1", "worker_a", claimed)
        self.review("T-1", "admin_agent")
        self.git("merge", "--ff-only", "agenthub/T-1")
        self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                  "--merge-commit", self.git("rev-parse", "HEAD"),
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
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["agents"]["solo_agent"]["self_review"] = "sometimes"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_primary", "status", "solo_agent")

        config["agents"]["solo_agent"]["self_review"] = "allow"
        config["agents"]["solo_agent"]["roles"] = "executor"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_primary", "status", "solo_agent")

    def test_config_rejects_self_review_on_a_single_role_agent(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["agents"]["worker_a"]["self_review"] = "forbid"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_primary", "status", "admin_agent")


if __name__ == "__main__":
    unittest.main()
