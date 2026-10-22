from __future__ import annotations

import base64
import json
import shlex
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import test_agenthub as fixtures

hub = fixtures.hub


class SessionTests(unittest.TestCase):
    def setUp(self):
        fixtures.AgentHubV23Tests.setUp(self)
        self.base_config = json.loads(self.config.read_text(encoding="utf-8"))

    def tearDown(self):
        fixtures.AgentHubV23Tests.tearDown(self)

    git = fixtures.AgentHubV23Tests.git
    call = fixtures.AgentHubV23Tests.call
    decide = fixtures.AgentHubV23Tests.decide
    packet = fixtures.AgentHubV23Tests.packet

    def configure(self, **execution):
        config = json.loads(json.dumps(self.base_config))
        config["execution"].update(execution)
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def post(self, task_id="T-1", assignee="worker_a"):
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet(task_id, assignee))

    def call_session(self, session, *args, device="authority"):
        return self.call("--device", device, "--session", session, *args)

    def claim(self, task_id="T-1", session=None, lease_minutes=None, agent="worker_a"):
        args = ["claim", agent, task_id]
        if session:
            args = ["--session", session, *args]
        if lease_minutes is not None:
            args.extend(["--lease-minutes", str(lease_minutes)])
        return json.loads(self.call("--device", "authority", *args))

    def status_task(self, task_id="T-1"):
        status = json.loads(self.call("--device", "authority", "status", "worker_a", "--json"))
        return next(task for task in status["tasks"] if task["id"] == task_id)

    def execute_state(self, statement, params=()):
        conn = sqlite3.connect(self.state)
        try:
            conn.execute(statement, params)
            conn.commit()
        finally:
            conn.close()

    def test_next_never_returns_another_sessions_task(self):
        self.decide()
        self.post("T-1")
        self.post("T-2")
        self.claim("T-1", "p00-download")

        anonymous = json.loads(self.call("--device", "authority", "next", "worker_a"))
        self.assertEqual(anonymous["task"]["id"], "T-2")
        self.assertEqual(anonymous["excluded"][0]["task_id"], "T-1")
        self.assertEqual(anonymous["excluded"][0]["session"], "p00-download")

        owned = json.loads(self.call_session("p00-download", "next", "worker_a"))
        self.assertEqual(owned["task"]["id"], "T-1")
        self.assertTrue(owned["task"]["ownership"]["mine"])

        other = json.loads(self.call_session("p02-forecast", "next", "worker_a"))
        self.assertEqual(other["task"]["id"], "T-2")

    def test_wrong_session_cannot_mutate_a_claim(self):
        self.decide()
        self.post()
        self.claim("T-1", "p00-download")
        progress = json.dumps({"summary": "half", "next_step": "finish", "evidence": []})

        for args in (
            ("checkpoint", "worker_a", "T-1", "--progress", progress),
            ("submit", "worker_a", "T-1", "--result", json.dumps({
                "outcome": "blocked", "summary": "x", "blocker": "x", "decision_needed": "x"})),
            ("release", "worker_a", "T-1", "--reason", "wrong window"),
            ("handoff", "worker_a", "T-1", "--to-device", "client_primary", "--note", "wrong window"),
        ):
            with self.assertRaisesRegex(hub.HubError, "p00-download"):
                self.call_session("p02-forecast", *args)

        self.call_session("p00-download", "checkpoint", "worker_a", "T-1", "--progress", progress)
        shown = json.loads(self.call("--device", "authority", "show", "worker_a", "T-1"))
        self.assertEqual(shown["task"]["checkpoint"]["summary"], "half")

    def test_heartbeat_binds_legacy_claim_and_tracks_lease(self):
        self.decide()
        self.post()
        self.claim("T-1")  # optional policy: no session, legacy claim

        # An administrator can bind the session of a running claim after an upgrade.
        self.call_session("p00-download", "heartbeat", "admin_agent", "T-1",
                          "--lease-minutes", "10", device="client_primary")
        task = self.status_task()
        self.assertEqual(task["claim_session"], "p00-download")
        self.assertEqual(task["lease"]["lease_seconds"], 600)
        self.assertFalse(task["lease_expired"])

        # The owning window can refresh it; another window cannot.
        self.call_session("p00-download", "heartbeat", "worker_a", "T-1")
        with self.assertRaisesRegex(hub.HubError, "p00-download"):
            self.call_session("p02-forecast", "heartbeat", "worker_a", "T-1")
        with self.assertRaisesRegex(hub.HubError, "活动设备"):
            self.call_session("p00-download", "heartbeat", "worker_a", "T-1", device="client_secondary")

        # Expiry is a warning, never an automatic release.
        old = (hub.dt.datetime.now(hub.dt.timezone.utc) - hub.dt.timedelta(hours=2)).replace(microsecond=0).isoformat()
        self.execute_state("UPDATE tasks SET last_heartbeat_at=? WHERE task_id='T-1'", (old,))
        task = self.status_task()
        self.assertTrue(task["lease_expired"])
        self.assertEqual(task["status"], "in_progress")

    def test_required_policy_rejects_sessionless_claims_and_mutations(self):
        self.configure(session_policy="required")
        self.decide()
        self.post("T-1")
        self.post("T-2")
        with self.assertRaisesRegex(hub.HubError, "session"):
            self.call("--device", "authority", "claim", "worker_a", "T-1")
        with self.assertRaisesRegex(hub.HubError, "session"):
            self.call("--device", "authority", "claim-next", "worker_a")

        self.claim("T-1", "p02-forecast")
        anonymous = json.loads(self.call("--device", "authority", "next", "worker_a"))
        self.assertEqual(anonymous["task"]["id"], "T-2")
        self.assertTrue(anonymous["session_required"])
        self.assertEqual(anonymous["excluded"][0]["task_id"], "T-1")

        progress = json.dumps({"summary": "half", "next_step": "finish", "evidence": []})
        with self.assertRaisesRegex(hub.HubError, "p02-forecast"):
            self.call("--device", "authority", "checkpoint", "worker_a", "T-1", "--progress", progress)

    def test_required_policy_accepts_a_heartbeat_binding_for_an_old_claim(self):
        self.decide()
        self.post()
        self.claim("T-1")
        self.configure(session_policy="required")
        progress = json.dumps({"summary": "half", "next_step": "finish", "evidence": []})
        with self.assertRaisesRegex(hub.HubError, "尚未绑定会话"):
            self.call("--device", "authority", "checkpoint", "worker_a", "T-1", "--progress", progress)
        self.call_session("p00-download", "heartbeat", "worker_a", "T-1")
        self.call_session("p00-download", "checkpoint", "worker_a", "T-1", "--progress", progress)

    def test_reclaim_moves_the_old_checkpoint_to_previous(self):
        self.decide()
        self.post()
        self.claim("T-1", "p00-download")
        progress = json.dumps({"summary": "first round", "next_step": "finish", "evidence": []})
        self.call_session("p00-download", "checkpoint", "worker_a", "T-1", "--progress", progress)
        self.call_session("p00-download", "release", "worker_a", "T-1", "--reason", "window closed")
        self.claim("T-1", "p02-forecast")

        shown = json.loads(self.call("--device", "authority", "show", "worker_a", "T-1"))["task"]
        self.assertIsNone(shown["checkpoint"])
        self.assertEqual(shown["previous_checkpoint"]["summary"], "first round")

        progress = json.dumps({"summary": "second round", "next_step": "finish", "evidence": []})
        self.call_session("p02-forecast", "checkpoint", "worker_a", "T-1", "--progress", progress)
        shown = json.loads(self.call("--device", "authority", "show", "worker_a", "T-1"))["task"]
        self.assertEqual(shown["checkpoint"]["summary"], "second round")
        self.assertEqual(shown["previous_checkpoint"]["summary"], "first round")

    def test_admin_release_can_require_an_expired_lease(self):
        self.decide()
        self.post()
        self.claim("T-1", "p00-download", lease_minutes=10)
        with self.assertRaisesRegex(hub.HubError, "尚未过期"):
            self.call("--device", "client_primary", "release", "admin_agent", "T-1",
                      "--reason", "stale", "--if-expired")

        old = (hub.dt.datetime.now(hub.dt.timezone.utc) - hub.dt.timedelta(hours=2)).replace(microsecond=0).isoformat()
        self.execute_state("UPDATE tasks SET last_heartbeat_at=? WHERE task_id='T-1'", (old,))
        self.call("--device", "client_primary", "release", "admin_agent", "T-1",
                  "--reason", "stale", "--if-expired")
        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-1"))["task"]
        self.assertEqual(shown["status"], "ready")
        self.assertIsNone(shown["claim_session"])

    def test_task_ids_cannot_escape_the_worktree_root(self):
        self.decide()
        for task_id in ("../escape", "a/b", ".", "..", ".hidden"):
            with self.assertRaises(hub.HubError):
                self.post(task_id)

    def test_corrupt_state_json_is_reported_as_a_hub_error(self):
        self.decide()
        self.post()
        self.claim("T-1", "p00-download")
        self.execute_state("UPDATE tasks SET scope_json='{' WHERE task_id='T-1'")
        with self.assertRaisesRegex(hub.HubError, "状态数据损坏"):
            self.call("--device", "authority", "show", "worker_a", "T-1")

    def test_invalid_config_section_type_is_rejected(self):
        config = json.loads(json.dumps(self.base_config))
        config["execution"] = []
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(hub.HubError, "execution 必须是 JSON 对象"):
            hub.run([*self.base, "--device", "authority", "status", "worker_a"])

    def test_invalid_session_policy_is_rejected(self):
        self.configure(session_policy="sometimes")
        with self.assertRaisesRegex(hub.HubError, "session_policy"):
            self.call("--device", "authority", "status", "worker_a")

    def test_audit_limit_is_bounded(self):
        with self.assertRaisesRegex(hub.HubError, "1..1000"):
            self.call("--device", "client_primary", "audit", "admin_agent", "--limit", "-1")

    def test_close_keeps_durable_state_when_cleanup_fails(self):
        self.decide()
        self.post()
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

        real_git = hub.git

        def failing_cleanup(config, *args, **kwargs):
            if args[:2] == ("worktree", "remove"):
                raise hub.HubError("simulated cleanup failure")
            return real_git(config, *args, **kwargs)

        with mock.patch.object(hub, "git", side_effect=failing_cleanup):
            self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                      "--merge-commit", merged, "--target-branch", "main", "--note", "integrated")

        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-1"))["task"]
        self.assertEqual(shown["status"], "closed")
        self.assertTrue(worktree.exists())

    def test_remote_proxy_forwards_the_session(self):
        config = {
            "remote": {
                "enabled": True, "shell": "posix_sh", "ssh_host": "host",
                "project_root": "/repo", "launcher": ".agenthub/agenthub.sh", "ssh_options": [],
            },
            "execution": {}, "state_authority": {},
        }
        with mock.patch.object(hub.subprocess, "run") as run:
            run.return_value.returncode = 0
            hub.proxy_to_authority(
                config, "client", ["--device", "client", "--session", "s1", "next", "worker"], "s1",
            )
        command = run.call_args.args[0]
        tokens = shlex.split(command[-1])
        payload = json.loads(base64.urlsafe_b64decode(tokens[tokens.index("rpc") + 1]))
        self.assertEqual(payload["device"], "client")
        self.assertEqual(payload["session"], "s1")
        self.assertEqual(payload["argv"], ["next", "worker"])


if __name__ == "__main__":
    unittest.main()
