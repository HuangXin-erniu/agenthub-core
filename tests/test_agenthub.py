from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agenthub_v23", ROOT / "agent_hub.py")
hub = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(hub)


class AgentHubV23Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "AgentHub Test")
        self.git("config", "user.email", "agenthub@example.invalid")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "base")
        self.base_commit = self.git("rev-parse", "HEAD")
        self.state = self.root / "state.db"
        self.config = self.root / "project.json"
        self.worktrees = self.root / "worktrees"
        self.config.write_text(json.dumps({
            "schema_version": 1, "agenthub_version": hub.VERSION, "project_id": "test",
            "devices": {"authority": {}, "client_primary": {}, "client_secondary": {}},
            "agents": {
                "admin_agent": {"roles": ["administrator", "decider"], "devices": ["client_primary", "client_secondary"]},
                "planner_agent": {"roles": ["decider"], "devices": ["client_primary", "client_secondary"]},
                "worker_a": {"roles": ["executor"], "devices": ["authority", "client_primary", "client_secondary"]},
                "worker_b": {"roles": ["executor"], "devices": ["authority", "client_primary", "client_secondary"]}
            },
            "state_authority": {"host": "authority", "database": str(self.state), "policy": "single_authority_local"},
            "execution": {"repository": str(self.repo), "worktree_root": str(self.worktrees)},
            "messaging_transports": [], "lark_workflow": "hard_disabled"
        }), encoding="utf-8")
        self.base = ["--local", "--config", str(self.config), "--state", str(self.state)]

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-C", str(cwd or self.repo), *args], check=True,
            capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = hub.run([*self.base, *args])
        self.assertEqual(code, 0)
        return output.getvalue()

    def decide(self):
        self.call("--device", "client_primary", "decision", "admin_agent", "--key", "D-1", "--summary", "route", "--rationale", "evidence")

    def packet(self, task_id="T-1", assignee="worker_a"):
        return json.dumps({
            "id": task_id, "title": "task", "objective": "implement exact change",
            "assignee": assignee, "decision_key": "D-1", "base_commit": self.base_commit,
            "scope": ["README only"], "instructions": ["append one line"],
            "constraints": ["do not change route"], "allowed_paths": ["README.md"],
            "forbidden_paths": [".agenthub/**"], "acceptance": ["content exists"],
            "validation": ["git diff --check"], "deliverables": ["one commit"],
            "evidence_requirements": ["commit", "test result"],
            "blocked_protocol": "submit blocked with facts and decision_needed",
            "context_budget": 6000
        })

    def test_assigned_worktree_checkpoint_and_cross_device_resume(self):
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet())
        with self.assertRaises(hub.HubError):
            self.call("--device", "authority", "claim", "worker_b", "T-1")
        claimed = json.loads(self.call("--device", "authority", "claim", "worker_a", "T-1"))
        worktree = Path(claimed["worktree_path"])
        self.assertTrue((worktree / ".git").exists())
        progress = json.dumps({"summary": "half done", "next_step": "commit", "evidence": ["README.md"]})
        self.call("--device", "authority", "checkpoint", "worker_a", "T-1", "--progress", progress)
        resumed = json.loads(self.call("--device", "client_secondary", "next", "worker_a"))["task"]
        self.assertEqual(resumed["checkpoint"]["summary"], "half done")
        self.assertEqual(resumed["worktree_path"], str(worktree))

        (worktree / "README.md").write_text("base\ncompleted\n", encoding="utf-8")
        self.git("add", "README.md", cwd=worktree)
        self.git("commit", "-m", "complete T-1", cwd=worktree)
        commit = self.git("rev-parse", "HEAD", cwd=worktree)
        result = json.dumps({"outcome": "completed", "summary": "done", "commit": commit, "tests": ["git diff --check: pass"], "evidence": [f"commit:{commit}"]})
        self.call("--device", "authority", "handoff", "worker_a", "T-1", "--to-device", "client_primary", "--note", "switch device")
        self.call("--device", "client_primary", "submit", "worker_a", "T-1", "--result", result)
        self.call("--device", "client_secondary", "review", "planner_agent", "T-1", "--action", "approve", "--note", "verified")
        self.assertIsNone(json.loads(self.call("--device", "client_primary", "next", "worker_a"))["task"])

    def test_two_executors_receive_isolated_worktrees(self):
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-R", "worker_a"))
        self.call("--device", "client_secondary", "post", "planner_agent", "--spec", self.packet("T-C", "worker_b"))
        worker_a = json.loads(self.call("--device", "authority", "claim", "worker_a", "T-R"))
        worker_b = json.loads(self.call("--device", "authority", "claim", "worker_b", "T-C"))
        self.assertNotEqual(worker_a["worktree_path"], worker_b["worktree_path"])
        self.assertNotEqual(worker_a["branch"], worker_b["branch"])

    def test_submit_rejects_paths_outside_contract(self):
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet())
        claimed = json.loads(self.call("--device", "authority", "claim", "worker_a", "T-1"))
        worktree = Path(claimed["worktree_path"])
        (worktree / "outside.py").write_text("print('forbidden')\n", encoding="utf-8")
        self.git("add", "outside.py", cwd=worktree)
        self.git("commit", "-m", "outside contract", cwd=worktree)
        commit = self.git("rev-parse", "HEAD", cwd=worktree)
        result = json.dumps({"outcome": "completed", "summary": "done", "commit": commit, "tests": ["pass"], "evidence": [f"commit:{commit}"]})
        with self.assertRaises(hub.HubError):
            self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", result)

    def test_decider_context_is_compact(self):
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet())
        context = json.loads(self.call("--device", "client_secondary", "context", "admin_agent"))
        self.assertNotIn("instructions", context["tasks"][0])
        self.assertEqual(context["tasks"][0]["assignee"], "worker_a")

    def test_executor_cannot_post_and_admin_agent_cannot_run_on_authority(self):
        self.decide()
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_secondary", "post", "worker_b", "--spec", self.packet("T-2", "worker_b"))
        with self.assertRaises(hub.HubError):
            self.call("--device", "authority", "context", "admin_agent")

    def test_lark_is_hard_disabled(self):
        config = json.loads((ROOT / "templates" / "project.example.json").read_text(encoding="utf-8"))
        self.assertEqual(config["messaging_transports"], [])
        self.assertEqual(config["lark_workflow"], "hard_disabled")
        self.assertIn("StrictHostKeyChecking=yes", config["remote"]["ssh_options"])

    def test_smoke_preserves_source_repository_and_state(self):
        before_refs = self.git("show-ref")
        before_worktrees = self.git("worktree", "list", "--porcelain")
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["task_naming"] = {"synthetic": "ignored by generic smoke"}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/smoke_project.py"),
             "--config", str(self.config), "--runtime", str(ROOT / "agent_hub.py")],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        output = json.loads(result.stdout)
        self.assertTrue(output["ok"])
        self.assertTrue(output["isolated_clone"])
        self.assertEqual(self.git("show-ref"), before_refs)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), before_worktrees)
        self.assertFalse(self.state.exists())

    def test_second_project_has_independent_config_state_and_worktrees(self):
        second_repo = self.root / "repo-two"
        second_repo.mkdir()
        subprocess.run(["git", "-C", str(second_repo), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(second_repo), "config", "user.name", "AgentHub Test"], check=True)
        subprocess.run(["git", "-C", str(second_repo), "config", "user.email", "agenthub@example.invalid"], check=True)
        (second_repo / "README.md").write_text("second\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(second_repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(second_repo), "commit", "-m", "base"], check=True, capture_output=True)
        second_state = self.root / "second.db"
        second_config = self.root / "second.json"
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["project_id"] = "second-project"
        value["state_authority"]["database"] = str(second_state)
        value["execution"] = {"repository": str(second_repo), "worktree_root": str(self.root / "worktrees-two")}
        second_config.write_text(json.dumps(value), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            hub.run(["--local", "--config", str(second_config), "--state", str(second_state), "--device", "client_primary", "decision", "admin_agent", "--key", "SECOND", "--summary", "isolated", "--rationale", "separate state"])
        first_context = json.loads(self.call("--device", "client_primary", "context", "admin_agent"))
        with contextlib.redirect_stdout(output := io.StringIO()):
            hub.run(["--local", "--config", str(second_config), "--state", str(second_state), "--device", "client_primary", "context", "admin_agent"])
        second_context = json.loads(output.getvalue())
        self.assertEqual(first_context["project"], "test")
        self.assertEqual(first_context["decisions"], [])
        self.assertEqual(second_context["project"], "second-project")
        self.assertEqual(second_context["decisions"][0]["decision_key"], "SECOND")

    def test_device_handoff_is_enforced(self):
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet())
        self.call("--device", "authority", "claim", "worker_a", "T-1")
        progress = json.dumps({"summary": "safe point", "next_step": "continue", "evidence": []})
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_secondary", "checkpoint", "worker_a", "T-1", "--progress", progress)
        self.call("--device", "authority", "checkpoint", "worker_a", "T-1", "--progress", progress)
        self.call("--device", "authority", "handoff", "worker_a", "T-1", "--to-device", "client_secondary", "--note", "move")
        self.call("--device", "client_secondary", "checkpoint", "worker_a", "T-1", "--progress", progress)

    def test_decider_show_preserves_blocked_submission_after_rework(self):
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet())
        self.call("--device", "authority", "claim", "worker_a", "T-1")
        blocked = json.dumps({
            "outcome": "blocked", "summary": "cannot continue",
            "blocker": "missing authoritative input", "decision_needed": "choose A or B",
        })
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", blocked)
        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-1"))
        self.assertEqual(shown["submissions"][0]["result"]["decision_needed"], "choose A or B")
        self.call("--device", "client_primary", "review", "admin_agent", "T-1", "--action", "rework", "--note", "choose A")
        shown = json.loads(self.call("--device", "client_secondary", "show", "planner_agent", "T-1"))
        self.assertEqual(shown["submissions"][0]["result"]["blocker"], "missing authoritative input")

    def test_status_requires_valid_identity_and_device(self):
        self.call("--device", "client_primary", "status", "admin_agent")
        with self.assertRaises(hub.HubError):
            self.call("--device", "authority", "status", "admin_agent")

    def test_close_verifies_merge_and_cleans_worktree(self):
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet())
        claimed = json.loads(self.call("--device", "authority", "claim", "worker_a", "T-1"))
        worktree = Path(claimed["worktree_path"])
        (worktree / "README.md").write_text("base\nclosed\n", encoding="utf-8")
        self.git("add", "README.md", cwd=worktree)
        self.git("commit", "-m", "complete T-1", cwd=worktree)
        commit = self.git("rev-parse", "HEAD", cwd=worktree)
        result = json.dumps({"outcome": "completed", "summary": "done", "commit": commit, "tests": ["pass"], "evidence": [f"commit:{commit}"]})
        self.call("--device", "authority", "submit", "worker_a", "T-1", "--result", result)
        self.call("--device", "client_primary", "review", "admin_agent", "T-1", "--action", "approve", "--note", "verified")
        context = json.loads(self.call("--device", "client_primary", "context", "admin_agent"))
        self.assertEqual(context["tasks"][0]["status"], "approved")
        self.git("merge", "--ff-only", "agenthub/T-1")
        merged = self.git("rev-parse", "HEAD")
        self.call("--device", "client_primary", "close", "admin_agent", "T-1", "--merge-commit", merged, "--target-branch", "main", "--note", "integrated")
        self.assertFalse(worktree.exists())
        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-1"))
        self.assertEqual(shown["task"]["status"], "closed")



    def test_cancel_rejects_non_administrator(self):
        """Only administrator can cancel."""
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-CANCEL"))
        with self.assertRaises(hub.HubError):
            self.call("--device", "authority", "cancel", "worker_a", "T-CANCEL", "--reason", "no reason")

    def test_cancel_admin_can_release_in_progress_and_approved(self):
        """in_progress and approved have no other exit; only an administrator may force them."""
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-CIP"))
        self.call("--device", "authority", "claim", "worker_a", "T-CIP")
        with self.assertRaises(hub.HubError):
            self.call("--device", "authority", "cancel", "worker_a", "T-CIP", "--reason", "worker may not")
        self.call("--device", "client_primary", "cancel", "admin_agent", "T-CIP", "--reason", "claim wedged")
        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-CIP"))
        self.assertEqual(shown["task"]["status"], "cancelled")
        self.assertIsNone(shown["task"]["claimed_by"])

    def test_cancel_ready_task(self):
        """Cancel a ready task marks it cancelled and removes from next."""
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-CRD"))
        self.call("--device", "client_primary", "cancel", "admin_agent", "T-CRD", "--reason", "requirements changed")
        self.assertIsNone(json.loads(self.call("--device", "authority", "next", "worker_a"))["task"])

    def test_supersede_old_task_cancelled_new_task_ready(self):
        """Supersede cancels old task and posts new one."""
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-SUP"))
        new_spec = json.dumps({
            "id": "T-SUP2", "title": "supersede replacement", "objective": "new scope",
            "assignee": "worker_a", "decision_key": "D-1", "base_commit": self.base_commit,
            "scope": ["full"], "instructions": ["new steps"],
            "constraints": ["none"], "allowed_paths": ["README.md"],
            "forbidden_paths": [".agenthub/**"], "acceptance": ["done"],
            "validation": ["git diff --check"], "deliverables": ["commit"],
            "evidence_requirements": ["commit"], "blocked_protocol": "blocked if fail",
            "context_budget": 6000,
        })
        spec_file = self.root / "supersede_spec.json"
        spec_file.write_text(new_spec, encoding="utf-8")
        self.call("--device", "client_primary", "supersede", "admin_agent", "T-SUP", "--spec", f"@{spec_file}", "--reason", "scope expanded")
        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-SUP"))
        self.assertEqual(shown["task"]["status"], "cancelled")
        shown2 = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-SUP2"))
        self.assertEqual(shown2["task"]["status"], "ready")


    def test_executor_can_read_review_note_without_audit(self):
        """Executor can read review note via next/show without audit permission."""
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-RVN"))
        claimed = json.loads(self.call("--device", "authority", "claim", "worker_a", "T-RVN"))
        worktree = Path(claimed["worktree_path"])
        (worktree / "README.md").write_text("base\nreview-me\n", encoding="utf-8")
        self.git("add", "README.md", cwd=worktree)
        self.git("commit", "-m", "done", cwd=worktree)
        commit = self.git("rev-parse", "HEAD", cwd=worktree)
        result = json.dumps({"outcome": "completed", "summary": "done", "commit": commit, "tests": ["ok"], "evidence": ["commit"]})
        self.call("--device", "authority", "submit", "worker_a", "T-RVN", "--result", result)
        self.call("--device", "client_primary", "review", "planner_agent", "T-RVN", "--action", "rework", "--note", "fix the approach")
        shown = json.loads(self.call("--device", "authority", "show", "worker_a", "T-RVN"))
        self.assertIsNotNone(shown["task"]["latest_review"])
        self.assertEqual(shown["task"]["latest_review"]["note"], "fix the approach")
        self.assertEqual(shown["task"]["latest_review"]["action"], "rework")

    def test_post_rejects_invalid_chain_id(self):
        """Post without chain_id or with invalid chain_id is rejected."""
        naming_config = {"task_naming": {"task_pattern": r"(?P<seq>\d{3})", "chain_pattern": r"CHN-(?P<seq>\d{4})", "seq_digits": 3, "enum_description": {"X": "Y"}, "seq_offset": 0, "phase_domains": {}, "domain_types": {}}}
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["task_naming"] = naming_config["task_naming"]
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.decide()
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("001"))
        spec = json.loads(self.packet("001"))
        spec["chain_id"] = "BAD-FORMAT"
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(spec))
        # Restore config
        config["task_naming"] = ""
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def test_post_requires_exact_next_sequence(self):
        """Post with non-sequential seq is rejected."""
        naming_config = {"task_pattern": r"(?P<seq>\d{3})", "chain_pattern": r"CHN-(?P<seq>\d{4})", "seq_digits": 3, "enum_description": {"X": "Y"}, "seq_offset": 0, "phase_domains": {}, "domain_types": {}}
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["task_naming"] = naming_config
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.decide()
        spec = json.loads(self.packet("002"))
        spec["chain_id"] = "CHN-0001"
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_primary", "post", "admin_agent", "--spec", json.dumps(spec))
        # Restore
        del config["task_naming"]
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def test_supersede_rollback_on_failure(self):
        """supersede does not cancel old task if successor insert fails."""
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-RB"))
        bad_spec = json.dumps({
            "id": "T-RB2", "title": "", "objective": "bad",
            "assignee": "worker_a", "decision_key": "D-1", "base_commit": self.base_commit,
            "scope": ["x"], "instructions": ["x"],
            "constraints": ["x"], "allowed_paths": ["x"],
            "forbidden_paths": ["x"], "acceptance": ["x"],
            "validation": ["x"], "deliverables": ["x"],
            "evidence_requirements": ["x"], "blocked_protocol": "x",
            "context_budget": 2000,
        })
        spec_file = self.root / "bad_spec.json"
        spec_file.write_text(bad_spec, encoding="utf-8")
        with self.assertRaises(hub.HubError):
            self.call("--device", "client_primary", "supersede", "admin_agent", "T-RB", "--spec", f"@{spec_file}", "--reason", "should fail")
        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-RB"))
        self.assertEqual(shown["task"]["status"], "ready")

    def test_cancel_shows_in_status_all(self):
        """Cancelled task appears in status --all but not default status."""
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-CSA"))
        self.call("--device", "client_primary", "cancel", "admin_agent", "T-CSA", "--reason", "obsolete")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            hub.run([*self.base, "--device", "client_primary", "status", "admin_agent"])
        default_out = output.getvalue()
        self.assertNotIn("T-CSA", default_out)
        output2 = io.StringIO()
        with contextlib.redirect_stdout(output2):
            hub.run([*self.base, "--device", "client_primary", "status", "admin_agent", "--all"])
        all_out = output2.getvalue()
        self.assertIn("T-CSA", all_out)


    def test_claim_rolls_back_when_the_new_worktree_cannot_be_read(self):
        """A git failure after the state write must leave the task claimable, not wedged."""
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-RB"))
        with mock.patch.object(hub, "task_packet", side_effect=hub.HubError("git refused")):
            with self.assertRaises(hub.HubError):
                self.call("--device", "authority", "claim", "worker_a", "T-RB")
        resumed = json.loads(self.call("--device", "authority", "next", "worker_a"))["task"]
        self.assertEqual(resumed["status"], "ready")
        self.assertIsNone(resumed["claimed_by"])
        claimed = json.loads(self.call("--device", "authority", "claim", "worker_a", "T-RB"))
        self.assertEqual(claimed["status"], "in_progress")
        self.assertTrue(Path(claimed["worktree_path"]).exists())

    def test_git_argv_clears_ownership_for_the_directory_in_use(self):
        """safe.directory must name the directory of this call, not just the main repository."""
        target = self.worktrees / "T-X"
        argv = hub.git_argv(str(target), "rev-parse", "HEAD")
        self.assertEqual(argv[:2], ["git", "-c"])
        self.assertEqual(argv[2], f"safe.directory={target.resolve()}")
        self.assertEqual(argv[3:6], ["-C", str(target), "rev-parse"])

    def test_prune_reports_first_then_removes_terminal_worktrees(self):
        """A cancelled task keeps its worktree until prune is told to reclaim it."""
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-PR"))
        worktree = Path(json.loads(self.call("--device", "authority", "claim", "worker_a", "T-PR"))["worktree_path"])
        self.call("--device", "client_primary", "cancel", "admin_agent", "T-PR", "--reason", "superseded by hand")
        self.assertTrue(worktree.exists())

        report = json.loads(self.call("--device", "client_primary", "prune", "admin_agent"))
        self.assertFalse(report["applied"])
        self.assertEqual([item["action"] for item in report["candidates"]], ["would-remove"])
        self.assertEqual(report["candidates"][0]["task_id"], "T-PR")
        self.assertTrue(worktree.exists())

        applied = json.loads(self.call("--device", "client_primary", "prune", "admin_agent", "--apply"))
        self.assertEqual(applied["removed"], 1)
        self.assertFalse(worktree.exists())
        self.assertNotIn("agenthub/T-PR", self.git("show-ref"))

    def test_prune_skips_a_worktree_holding_uncommitted_changes(self):
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-PD"))
        worktree = Path(json.loads(self.call("--device", "authority", "claim", "worker_a", "T-PD"))["worktree_path"])
        self.call("--device", "client_primary", "cancel", "admin_agent", "T-PD", "--reason", "stop")
        (worktree / "uncommitted.txt").write_text("work in progress\n", encoding="utf-8")

        report = json.loads(self.call("--device", "client_primary", "prune", "admin_agent", "--apply"))
        self.assertEqual(report["removed"], 0)
        self.assertEqual(report["candidates"][0]["action"], "skipped")
        self.assertEqual(report["candidates"][0]["reason"], "uncommitted changes")
        self.assertTrue(worktree.exists())

    def test_prune_never_touches_a_live_task(self):
        """A live worktree is listed with its reason rather than silently omitted."""
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-PL"))
        worktree = Path(json.loads(self.call("--device", "authority", "claim", "worker_a", "T-PL"))["worktree_path"])
        report = json.loads(self.call("--device", "client_primary", "prune", "admin_agent", "--apply"))
        self.assertEqual(report["removed"], 0)
        self.assertEqual([(item["action"], item["reason"]) for item in report["candidates"]],
                         [("skipped", "live task (in_progress)")])
        self.assertTrue(worktree.exists())
        self.assertIn("agenthub/T-PL", self.git("show-ref"))


if __name__ == "__main__":
    unittest.main()
