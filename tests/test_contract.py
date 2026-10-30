from __future__ import annotations

import json
import unittest

from support import HubFixture, git, hub


class ContractTests(HubFixture):
    def close_task(self, task_id: str = "T-1") -> None:
        self.submit_completed(task_id)
        self.call("--device", "client_primary", "review", "admin_agent", task_id,
                  "--action", "approve", "--note", "ok")
        git(self.repo, "merge", "--ff-only", f"agenthub/{task_id}")
        merge = git(self.repo, "rev-parse", "HEAD")
        self.call("--device", "client_primary", "close", "admin_agent", task_id,
                  "--merge-commit", merge, "--target-branch", "main", "--note", "done")

    def test_minimal_contract_and_defaults(self):
        self.decide()
        self.post()
        task = self.call("--device", "client_primary", "show", "admin_agent", "T-1")["task"]
        contract = task["contract"]
        self.assertEqual(contract["forbidden_paths"], [".agenthub/**"])
        self.assertEqual(contract["depends_on"], [])
        self.assertEqual(contract["gates"], [])
        self.assertEqual(contract["artifacts"], [])
        self.assertEqual(contract["priority"], 50)
        self.assertEqual(contract["chain_id"], "")

    def test_required_arrays_and_unknown_fields(self):
        self.decide()
        for field in ("instructions", "acceptance", "allowed_paths"):
            packet = self.packet()
            packet.pop(field)
            with self.assertRaises(hub.HubError) as raised:
                self.post(packet)
            self.assertEqual(raised.exception.code, "contract.invalid")
        with self.assertRaises(hub.HubError) as raised:
            self.post(self.packet(scope=["legacy"]))
        self.assertEqual(raised.exception.code, "contract.invalid")
        with self.assertRaises(hub.HubError) as raised:
            self.post(self.packet(constraints=["legacy"]))
        self.assertEqual(raised.exception.code, "contract.invalid")

    def test_contract_budget(self):
        self.configure(max_contract_bytes=4096)
        self.decide()
        packet = self.packet(objective="x" * 3500,
                             instructions=[f"step {index} " + "y" * 180 for index in range(10)])
        with self.assertRaises(hub.HubError) as raised:
            self.post(packet)
        self.assertEqual(raised.exception.code, "contract.budget")

    def test_gates_are_computed_by_the_core(self):
        self.decide()
        gate = {"id": "acc", "metric": "accuracy", "operator": ">=", "threshold": 0.9}
        self.post(self.packet(gates=[gate]))
        self.claim("T-1")
        commit = self.commit_work("T-1")
        base_result = {"outcome": "completed", "summary": "done", "commit": commit,
                       "tests": ["unit"], "evidence": [f"commit:{commit}"]}
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "submit", "worker_a", "T-1",
                      "--result", json.dumps(base_result))
        self.assertEqual(raised.exception.code, "contract.invalid")
        failed = dict(base_result, gate_results={"acc": 0.8})
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "submit", "worker_a", "T-1",
                      "--result", json.dumps(failed))
        self.assertEqual(raised.exception.code, "gate.failed")
        blocked = {"outcome": "blocked", "summary": "below target", "blocker": "accuracy",
                   "decision_needed": "accept lower accuracy", "gate_results": {"acc": 0.8}}
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", json.dumps(blocked))
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-1")
        self.assertEqual(shown["task"]["status"], "blocked")
        self.assertEqual(shown["task"]["gate_status"], {"total": 1, "passed": 0})
        self.call("--device", "client_primary", "review", "admin_agent", "T-1",
                  "--action", "rework", "--note", "retry")
        self.call("--device", "authority", "claim", "worker_a", "T-1")
        commit = self.commit_work("T-1", text="base\nchange2\n")
        passed = {"outcome": "completed", "summary": "done", "commit": commit,
                  "tests": ["unit"], "evidence": [f"commit:{commit}"],
                  "gate_results": {"acc": 0.95}}
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", json.dumps(passed))
        self.call("--device", "client_primary", "review", "admin_agent", "T-1",
                  "--action", "approve", "--note", "ok")
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-1")
        self.assertEqual(shown["task"]["status"], "approved")
        self.assertEqual(shown["task"]["gate_status"], {"total": 1, "passed": 1})

    def test_approve_recomputes_instead_of_trusting_the_payload(self):
        self.decide()
        gate = {"id": "acc", "metric": "accuracy", "operator": ">=", "threshold": 0.9}
        self.post(self.packet(gates=[gate]))
        self.claim("T-1")
        commit = self.commit_work("T-1")
        result = {"outcome": "completed", "summary": "done", "commit": commit,
                  "tests": ["unit"], "evidence": [f"commit:{commit}"],
                  "gate_results": {"acc": 0.95}}
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", json.dumps(result))
        conn = self.database()
        try:
            row = conn.execute("SELECT submission_id,result_json FROM submissions WHERE task_id='T-1'").fetchone()
            tampered = json.loads(row["result_json"])
            tampered["gate_results"] = {"acc": 0.1}
            conn.execute("UPDATE submissions SET result_json=? WHERE submission_id=?",
                         (json.dumps(tampered), row["submission_id"]))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "review", "admin_agent", "T-1",
                      "--action", "approve", "--note", "ok")
        self.assertEqual(raised.exception.code, "gate.failed")

    def test_artifacts_require_root_and_existence(self):
        self.decide()
        packet = self.packet(artifacts=["reports/result.txt"])
        with self.assertRaises(hub.HubError) as raised:
            self.post(packet)
        self.assertEqual(raised.exception.code, "contract.invalid")
        artifact_root = self.root / "artifacts"
        artifact_root.mkdir()
        self.configure(artifact_root=str(artifact_root))
        self.post(packet)
        self.claim("T-1")
        commit = self.commit_work("T-1")
        result = {"outcome": "completed", "summary": "done", "commit": commit,
                  "tests": ["unit"], "evidence": [f"commit:{commit}"]}
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "submit", "worker_a", "T-1",
                      "--result", json.dumps(result))
        self.assertEqual(raised.exception.code, "artifact.missing")
        (artifact_root / "reports").mkdir()
        (artifact_root / "reports/result.txt").write_text("ok", encoding="utf-8")
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", json.dumps(result))

    def test_artifact_paths_cannot_escape(self):
        self.configure(artifact_root=str(self.root / "artifacts"))
        self.decide()
        for value in ("../x", "/abs/x", "C:/x", "a/*.txt"):
            with self.assertRaises(hub.HubError) as raised:
                self.post(self.packet(artifacts=[value]))
            self.assertEqual(raised.exception.code, "contract.invalid")

    def test_dependency_blocks_claim_until_closed(self):
        self.decide()
        self.post(self.packet("T-A"))
        self.claim("T-A")
        self.close_task("T-A")
        new_base = git(self.repo, "rev-parse", "HEAD")
        self.post(self.packet("T-B", base_commit=new_base, depends_on=["T-A"]))
        claimed = self.claim("T-B")
        self.assertEqual(claimed["task"]["id"], "T-B")
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-B")
        self.assertTrue(shown["task"]["dependencies"]["satisfied"])
        self.assertEqual(shown["task"]["dependencies"]["stale_base"], [])

    def test_dependency_must_exist_and_not_be_cancelled(self):
        self.decide()
        with self.assertRaises(hub.HubError) as raised:
            self.post(self.packet(depends_on=["MISSING"]))
        self.assertEqual(raised.exception.code, "dependency.unmet")
        self.post(self.packet("T-C"))
        self.call("--device", "client_primary", "cancel", "admin_agent", "T-C", "--reason", "drop")
        with self.assertRaises(hub.HubError) as raised:
            self.post(self.packet("T-D", depends_on=["T-C"]))
        self.assertEqual(raised.exception.code, "dependency.unmet")

    def test_path_overlap_is_rejected_and_can_be_acknowledged(self):
        self.decide()
        self.post(self.packet("T-1", allowed_paths=["src/**"], allow_path_overlap=False))
        serial = self.post(self.packet("T-2", allowed_paths=["src/a.py"], allow_path_overlap=False,
                                       depends_on=["T-1"]))
        self.assertEqual(serial["warnings"], [])
        serial = self.post(self.packet("T-3", allowed_paths=["src/b.py"], allow_path_overlap=False,
                                       depends_on=["T-2"]))
        self.assertEqual(serial["warnings"], [])
        with self.assertRaises(hub.HubError) as raised:
            self.post(self.packet("T-4", allowed_paths=["src/a.py"], allow_path_overlap=False))
        self.assertEqual(raised.exception.code, "path.overlap")
        ack = self.post(self.packet("T-5", allowed_paths=["src/a.py"], allow_path_overlap=True))
        self.assertEqual(ack["warnings"][0]["code"], "path.overlap.ack")

    def test_task_naming_is_generic_and_monotonic(self):
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["task_naming"] = {
            "task_pattern": r"TASK-(?P<seq>\d{3})",
            "chain_pattern": r"CHN-(?P<seq>\d{4})",
            "seq_digits": 3,
            "enum_description": {"PHASE": "ignored"},
            "phase_domains": {"P00": ["GOV"]},
            "domain_types": {"GOV": ["FIX"]},
        }
        self.write_config(value)
        self.decide()
        self.post(self.packet("TASK-001", chain_id="CHN-0001"))
        with self.assertRaises(hub.HubError) as raised:
            self.post(self.packet("TASK-003", chain_id="CHN-0002"))
        self.assertEqual(raised.exception.code, "contract.invalid")
        self.post(self.packet("TASK-002", chain_id="CHN-0002"))
        with self.assertRaises(hub.HubError) as raised:
            self.post(self.packet("TASK-002", chain_id="CHN-0003"))
        self.assertEqual(raised.exception.code, "contract.invalid")

    def test_schema_is_compact_and_has_one_contract_column(self):
        self.decide()
        self.post()
        conn = self.database()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            metadata = dict(conn.execute("SELECT key,value FROM metadata"))
        finally:
            conn.close()
        self.assertEqual(len(columns), 26)
        self.assertIn("contract_json", columns)
        self.assertIn("approved_submission_id", columns)
        for removed in ("result_json", "objective", "scope_json", "gates_json", "depends_on_json"):
            self.assertNotIn(removed, columns)
        self.assertEqual(metadata["schema_version"], "5")
        self.assertEqual(metadata["agenthub_version"], hub.VERSION)

    def test_status_filters_by_decision_and_rejects_unknown_keys(self):
        self.decide("D-1")
        self.decide("D-2")
        self.post(self.packet("T-1", decision_key="D-1"))
        self.post(self.packet("T-2", decision_key="D-2"))
        filtered = self.call("--device", "client_primary", "status", "admin_agent", "--decision", "D-2")
        self.assertEqual([task["id"] for task in filtered["tasks"]], ["T-2"])
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "status", "admin_agent", "--decision", "NOPE")
        self.assertEqual(raised.exception.code, "contract.invalid")

    def test_close_verifies_artifacts_before_cleanup(self):
        artifact_root = self.root / "artifacts"
        artifact_root.mkdir()
        self.configure(artifact_root=str(artifact_root))
        self.decide()
        self.post(self.packet(artifacts=["result.txt"]))
        self.claim("T-1")
        (artifact_root / "result.txt").write_text("ok", encoding="utf-8")
        commit = self.commit_work("T-1")
        result = {"outcome": "completed", "summary": "done", "commit": commit,
                  "tests": ["unit"], "evidence": [f"commit:{commit}"]}
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", json.dumps(result))
        self.call("--device", "client_primary", "review", "admin_agent", "T-1",
                  "--action", "approve", "--note", "ok")
        git(self.repo, "merge", "--ff-only", "agenthub/T-1")
        merge = git(self.repo, "rev-parse", "HEAD")
        (artifact_root / "result.txt").unlink()
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                      "--merge-commit", merge, "--target-branch", "main", "--note", "done")
        self.assertEqual(raised.exception.code, "artifact.missing")
        shown = self.call("--device", "client_primary", "show", "admin_agent", "T-1")
        self.assertEqual(shown["task"]["status"], "approved")
        (artifact_root / "result.txt").write_text("ok", encoding="utf-8")
        self.call("--device", "client_primary", "close", "admin_agent", "T-1",
                  "--merge-commit", merge, "--target-branch", "main", "--note", "done")



if __name__ == "__main__":
    unittest.main()
