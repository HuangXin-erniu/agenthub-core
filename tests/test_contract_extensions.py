from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

import test_agenthub as fixtures

hub = fixtures.hub


class ContractExtensionTests(unittest.TestCase):
    setUp = fixtures.AgentHubV23Tests.setUp
    tearDown = fixtures.AgentHubV23Tests.tearDown
    git = fixtures.AgentHubV23Tests.git
    call = fixtures.AgentHubV23Tests.call
    decide = fixtures.AgentHubV23Tests.decide
    packet = fixtures.AgentHubV23Tests.packet

    def configure(self, **execution):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["execution"].update(execution)
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def spec(self, task_id="T-1", assignee="worker_a", **changes):
        value = json.loads(self.packet(task_id, assignee))
        value.update(changes)
        return value

    def post(self, task_id="T-1", assignee="worker_a", **changes):
        value = self.spec(task_id, assignee, **changes)
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(value))
        return value

    def claim(self, task_id="T-1", agent="worker_a"):
        return json.loads(self.call("--device", "authority", "claim", agent, task_id))

    def commit_work(self, task_id="T-1", agent="worker_a"):
        packet = self.claim(task_id, agent)
        worktree = Path(packet["worktree_path"])
        path = worktree / "README.md"
        path.write_text(path.read_text(encoding="utf-8") + f"{task_id}\n", encoding="utf-8")
        self.git("add", "README.md", cwd=worktree)
        self.git("commit", "-m", f"work {task_id}", cwd=worktree)
        return self.git("rev-parse", "HEAD", cwd=worktree)

    def submit_completed(self, task_id, commit, **extra):
        result = {"outcome": "completed", "summary": f"done {task_id}", "commit": commit,
                  "tests": ["unit"], "evidence": [f"commit:{commit}"]}
        result.update(extra)
        self.call("--device", "authority", "submit", "worker_a", task_id,
                  "--result", json.dumps(result))

    def finish(self, task_id="T-1"):
        commit = self.commit_work(task_id)
        self.submit_completed(task_id, commit)
        self.call("--device", "client_primary", "review", "planner_agent", task_id,
                  "--action", "approve", "--note", "ok")
        self.git("merge", "--ff-only", f"agenthub/{task_id}")
        self.call("--device", "client_primary", "close", "admin_agent", task_id,
                  "--merge-commit", self.git("rev-parse", "HEAD"),
                  "--target-branch", "main", "--note", "merged")

    # Dependencies ---------------------------------------------------------

    def test_dependency_must_be_closed_before_claim(self):
        self.decide()
        self.post("T-1")
        self.post("T-2", depends_on=["T-1"])
        with self.assertRaises(hub.HubError) as raised:
            self.claim("T-2")
        self.assertIn("依赖尚未全部 closed", str(raised.exception))
        self.assertIn("T-1(ready)", str(raised.exception))
        self.finish("T-1")
        claimed = self.claim("T-2")
        self.assertEqual(claimed["status"], "in_progress")
        self.assertTrue(claimed["dependencies"]["satisfied"])

    def test_claim_next_skips_dependency_blocked_task(self):
        self.configure(concurrency="multiple_tasks_per_executor", max_active_tasks_per_executor=2)
        self.decide()
        self.post("T-1", priority=10)
        self.post("T-2", priority=1, depends_on=["T-1"])
        output = json.loads(self.call("--device", "authority", "claim-next", "worker_a"))
        self.assertEqual(output["task"]["id"], "T-1")
        self.assertEqual(output["skipped"][0]["task_id"], "T-2")
        self.assertEqual(output["skipped"][0]["reason"], "dependencies_unmet")

    def test_next_available_excludes_dependency_blocked_task(self):
        self.decide()
        self.post("T-1")
        self.post("T-2", priority=1, depends_on=["T-1"])
        output = json.loads(self.call("--device", "authority", "next", "worker_a", "--available"))
        self.assertEqual(output["task"]["id"], "T-1")
        self.assertEqual(output["excluded"][0]["task_id"], "T-2")
        self.assertEqual(output["excluded"][0]["reason"], "dependencies_unmet")

    def test_dependency_must_exist_and_not_be_cancelled(self):
        self.decide()
        with self.assertRaises(hub.HubError) as raised:
            self.post("T-1", depends_on=["NOPE"])
        self.assertIn("不存在的任务", str(raised.exception))
        self.post("T-2")
        self.call("--device", "client_primary", "cancel", "admin_agent", "T-2", "--reason", "no")
        with self.assertRaises(hub.HubError) as raised:
            self.post("T-3", depends_on=["T-2"])
        self.assertIn("已取消", str(raised.exception))

    def test_replacement_cannot_depend_on_the_task_it_replaces(self):
        self.decide()
        self.post("T-1")
        replacement = self.spec("T-2", depends_on=["T-1"])
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "supersede", "admin_agent", "T-1",
                      "--spec", json.dumps(replacement), "--reason", "change")
        self.assertIn("不能依赖正在被替代的任务", str(raised.exception))

    def test_superseded_dependency_blocks_the_dependent(self):
        self.decide()
        self.post("T-1")
        self.post("T-2", depends_on=["T-1"])
        replacement = self.spec("T-3")
        self.call("--device", "client_primary", "supersede", "admin_agent", "T-1",
                  "--spec", json.dumps(replacement), "--reason", "change")
        with self.assertRaises(hub.HubError) as raised:
            self.claim("T-2")
        self.assertIn("T-1(cancelled)", str(raised.exception))

    def test_claim_reports_a_stale_base_after_the_dependency_merged(self):
        self.decide()
        self.post("T-1")
        self.post("T-2", depends_on=["T-1"])
        self.finish("T-1")
        claimed = self.claim("T-2")
        self.assertTrue(claimed["dependencies"]["stale_base"])
        self.assertEqual(claimed["dependencies"]["stale_base"][0]["task_id"], "T-1")

    # allowed_paths overlap -------------------------------------------------

    def test_path_overlap_is_rejected_without_explicit_ack(self):
        self.decide()
        first = self.spec("T-1")
        first.pop("allow_path_overlap")
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(first))
        second = self.spec("T-2")
        second.pop("allow_path_overlap")
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(second))
        message = str(raised.exception)
        self.assertIn("T-1", message)
        self.assertIn("README.md", message)

    def test_explicit_overlap_ack_is_recorded(self):
        self.decide()
        self.post("T-1")
        self.post("T-2")
        conn = sqlite3.connect(self.state)
        try:
            raw = conn.execute(
                "SELECT detail_json FROM events WHERE action='task.post' AND entity_id='T-2'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(json.loads(raw)["path_overlap_ack"][0]["task_id"], "T-1")

    def test_dependency_serialises_an_overlap(self):
        self.decide()
        first = self.spec("T-1")
        first.pop("allow_path_overlap")
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(first))
        second = self.spec("T-2", depends_on=["T-1"])
        second.pop("allow_path_overlap")
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(second))

    def test_disjoint_paths_do_not_conflict(self):
        self.decide()
        first = self.spec("T-1")
        first["allowed_paths"] = ["docs/a.md"]
        first.pop("allow_path_overlap")
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(first))
        second = self.spec("T-2")
        second["allowed_paths"] = ["docs/b.md"]
        second.pop("allow_path_overlap")
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(second))

    def test_glob_prefixes_are_compared_conservatively(self):
        self.decide()
        first = self.spec("T-1")
        first["allowed_paths"] = ["t2_forecast/**"]
        first.pop("allow_path_overlap")
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(first))
        second = self.spec("T-2")
        second["allowed_paths"] = ["t2_forecast/src/a.py"]
        second.pop("allow_path_overlap")
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(second))

    def test_replacement_does_not_conflict_with_the_task_it_replaces(self):
        self.decide()
        self.post("T-1")
        replacement = self.spec("T-2")
        replacement.pop("allow_path_overlap")
        self.call("--device", "client_primary", "supersede", "admin_agent", "T-1",
                  "--spec", json.dumps(replacement), "--reason", "same path")

    # Gates ----------------------------------------------------------------

    GATES = [{"id": "gain", "metric": "overlap", "operator": ">=", "threshold": 0.005}]

    def test_invalid_gate_declaration_is_rejected(self):
        self.decide()
        with self.assertRaises(hub.HubError):
            self.post("T-1", gates=[{"id": "x", "metric": "m", "operator": "~", "threshold": 1}])
        with self.assertRaises(hub.HubError):
            self.post("T-2", gates=[{"id": "x", "metric": "m", "operator": ">=", "threshold": True}])

    def test_completed_submission_requires_gate_results(self):
        self.decide()
        self.post("T-1", gates=self.GATES)
        commit = self.commit_work("T-1")
        with self.assertRaises(hub.HubError) as raised:
            self.submit_completed("T-1", commit)
        self.assertIn("gate_results", str(raised.exception))

    def test_failed_gate_rejects_completed_and_allows_blocked(self):
        self.decide()
        self.post("T-1", gates=self.GATES)
        commit = self.commit_work("T-1")
        with self.assertRaises(hub.HubError) as raised:
            self.submit_completed("T-1", commit, gate_results={"gain": 0.004})
        self.assertIn("合同门禁未通过", str(raised.exception))
        self.assertIn("blocked", str(raised.exception))
        blocked = {"outcome": "blocked", "summary": "gate failed",
                   "blocker": "gain 0.004 < 0.005",
                   "decision_needed": "drop the arm or relax the gate",
                   "gate_results": {"gain": 0.004}}
        self.call("--device", "authority", "submit", "worker_a", "T-1",
                  "--result", json.dumps(blocked))
        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-1"))
        self.assertEqual(shown["task"]["status"], "blocked")
        self.assertFalse(shown["task"]["result"]["gates"][0]["passed"])

    def test_passing_gate_allows_review_and_approve(self):
        self.decide()
        self.post("T-1", gates=self.GATES)
        commit = self.commit_work("T-1")
        self.submit_completed("T-1", commit, gate_results={"gain": 0.006})
        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-1"))
        self.assertEqual(shown["task"]["status"], "review")
        self.assertTrue(shown["task"]["result"]["gates"][0]["passed"])
        self.call("--device", "client_primary", "review", "planner_agent", "T-1",
                  "--action", "approve", "--note", "ok")
        approved = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-1"))
        self.assertEqual(approved["task"]["status"], "approved")

    def test_approve_recomputes_gates_instead_of_trusting_the_payload(self):
        self.decide()
        self.post("T-1", gates=self.GATES)
        commit = self.commit_work("T-1")
        self.submit_completed("T-1", commit, gate_results={"gain": 0.006})
        conn = sqlite3.connect(self.state)
        try:
            raw = conn.execute("SELECT result_json FROM tasks WHERE task_id='T-1'").fetchone()[0]
            result = json.loads(raw)
            result["gate_results"] = {"gain": 0.001}
            conn.execute("UPDATE tasks SET result_json=? WHERE task_id='T-1'", (json.dumps(result),))
            conn.execute("UPDATE submissions SET result_json=? WHERE task_id='T-1'", (json.dumps(result),))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "review", "planner_agent", "T-1",
                      "--action", "approve", "--note", "ok")
        self.assertIn("门禁未通过", str(raised.exception))

    # Artifacts ------------------------------------------------------------

    def test_artifacts_require_an_artifact_root(self):
        self.decide()
        with self.assertRaises(hub.HubError) as raised:
            self.post("T-1", artifacts=["out/result.json"])
        self.assertIn("artifact_root", str(raised.exception))

    def test_artifact_paths_cannot_escape_or_use_wildcards(self):
        self.configure(artifact_root=str(self.root / "artifacts"))
        self.decide()
        for bad in ("/tmp/x", "../x", "a/../../x", "a\\b", "a/*.json"):
            with self.assertRaises(hub.HubError):
                self.post("T-1", artifacts=[bad])

    def test_artifacts_must_exist_at_submit_and_close(self):
        artifacts_root = self.root / "artifacts"
        artifacts_root.mkdir()
        self.configure(artifact_root=str(artifacts_root))
        self.decide()
        self.post("T-1", artifacts=["out/result.json"])
        commit = self.commit_work("T-1")
        with self.assertRaises(hub.HubError) as raised:
            self.submit_completed("T-1", commit)
        self.assertIn("缺失", str(raised.exception))
        (artifacts_root / "out").mkdir()
        (artifacts_root / "out" / "result.json").write_text("{}", encoding="utf-8")
        self.submit_completed("T-1", commit)
        self.call("--device", "client_primary", "review", "planner_agent", "T-1",
                  "--action", "approve", "--note", "ok")
        self.git("merge", "--ff-only", "agenthub/T-1")
        merge_commit = self.git("rev-parse", "HEAD")
        (artifacts_root / "out" / "result.json").unlink()
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                      "--merge-commit", merge_commit, "--target-branch", "main", "--note", "merged")
        self.assertIn("交付物校验失败", str(raised.exception))
        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-1"))
        self.assertEqual(shown["task"]["status"], "approved")
        (artifacts_root / "out" / "result.json").write_text("{}", encoding="utf-8")
        self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                  "--merge-commit", merge_commit, "--target-branch", "main", "--note", "merged")

    # Lineage and status ---------------------------------------------------

    def test_status_exposes_chain_dependencies_and_gate_state(self):
        self.decide()
        self.post("T-1", chain_id="CHN-0001")
        self.post("T-2", chain_id="CHN-0002", depends_on=["T-1"])
        status = json.loads(self.call("--device", "client_primary", "status", "admin_agent", "--json"))
        tasks = {task["id"]: task for task in status["tasks"]}
        self.assertEqual(tasks["T-1"]["chain_id"], "CHN-0001")
        self.assertEqual(tasks["T-2"]["depends_on"], ["T-1"])
        self.assertFalse(tasks["T-2"]["dependency"]["satisfied"])
        text = self.call("--device", "client_primary", "status", "admin_agent")
        self.assertIn("chain=CHN-0001", text)
        self.assertIn("(P50)", text)
        self.assertIn("blocked_by=T-1", text)

    def test_status_filters_by_decision_and_rejects_unknown_keys(self):
        self.decide()
        self.post("T-1")
        self.call("--device", "client_primary", "decision", "admin_agent",
                  "--key", "D-2", "--summary", "other", "--rationale", "x")
        self.post("T-2", decision_key="D-2")
        status = json.loads(self.call("--device", "client_primary", "status", "admin_agent",
                                      "--json", "--decision", "D-1"))
        self.assertEqual([task["id"] for task in status["tasks"]], ["T-1"])
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "status", "admin_agent", "--decision", "NOPE")
        self.assertIn("未知决策", str(raised.exception))

    def test_schema_adds_contract_columns_in_place(self):
        self.decide()
        conn = sqlite3.connect(self.state)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            metadata = dict(conn.execute("SELECT key,value FROM metadata"))
        finally:
            conn.close()
        self.assertTrue({"depends_on_json", "gates_json", "artifacts_json", "allow_path_overlap"} <= columns)
        self.assertEqual(metadata["schema_version"], "4")
        self.assertEqual(metadata["agenthub_version"], hub.VERSION)
