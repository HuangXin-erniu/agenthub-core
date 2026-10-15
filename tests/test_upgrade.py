"""Upgrade behaviour: the deploy's safety properties, and a task surviving a version change.

The deployment replaces managed files one at a time, so its ordering *is* the contract and is
asserted here rather than trusted. The cross-version tests take the runtime from the previous
release out of git history and drive both halves of one task with it, which is what makes
"upgrading does not disturb work in progress" a checked claim instead of an assurance.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_agenthub as fixtures

ROOT = Path(__file__).resolve().parents[1]
hub = fixtures.hub


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


deploy = load("deploy_runtime", "scripts/deploy_project.py")

ROOT_PATH = "authority/project"
STAGING = ROOT_PATH + "/.agenthub/" + deploy.STAGING_NAME
PREVIOUS = ROOT_PATH + "/.agenthub/" + deploy.PREVIOUS_NAME
LAUNCHER = ".agenthub/agenthub.sh"


def entries_fixture() -> list[dict[str, str]]:
    """Three managed files: the runtime, the configuration, and the launcher."""
    return [
        {"stage": "00-agent_hub.py", "source": "agent_hub.py",
         "destination": ".agenthub/agent_hub.py", "sha256": "a" * 64},
        {"stage": "01-project.json", "source": "project.json",
         "destination": ".agenthub/project.json", "sha256": "b" * 64},
        {"stage": "02-agenthub.sh", "source": "agenthub.sh",
         "destination": LAUNCHER, "sha256": "c" * 64},
    ]


def posix_activation() -> str:
    return deploy.activation_script(ROOT_PATH, STAGING, PREVIOUS, entries_fixture(), False,
                                    ROOT_PATH + "/state/agenthub.db", LAUNCHER,
                                    ".agenthub/agent_hub.py", ".agenthub/project.json")


def windows_activation() -> str:
    return deploy.activation_script_windows(ROOT_PATH, ROOT_PATH + "\\.agenthub\\" + deploy.STAGING_NAME,
                                            ROOT_PATH + "\\.agenthub\\" + deploy.PREVIOUS_NAME,
                                            entries_fixture(), False, ROOT_PATH + "\\state\\agenthub.db",
                                            LAUNCHER, ".agenthub/agent_hub.py", ".agenthub/project.json")


def line_of(lines: list[str], predicate, last: bool = False) -> int:
    hits = [index for index, line in enumerate(lines) if predicate(line)]
    if not hits:
        raise AssertionError("no line matched")
    return hits[-1] if last else hits[0]


class DeployScriptTests(unittest.TestCase):
    def test_activation_verifies_every_file_before_replacing_any(self):
        """A corrupt transfer must not be able to replace even one file."""
        lines = posix_activation().splitlines()
        last_check = line_of(lines, lambda l: "sha256sum" in l and "hash mismatch" in l, last=True)
        first_move = line_of(lines, lambda l: l.strip().startswith("mv -f"))
        self.assertLess(last_check, first_move)
        self.assertEqual(lines[0], "set -eu")
        for entry in entries_fixture():
            self.assertIn(f"hash mismatch: {entry['stage']}", posix_activation())

    def test_activation_makes_the_launcher_executable_before_moving_it(self):
        """The remote proxy runs ./agenthub.sh; a launcher landing 0644 breaks every command."""
        lines = posix_activation().splitlines()
        chmod = line_of(lines, lambda l: l.startswith("chmod 755") and deploy.STAGING_NAME in l)
        first_move = line_of(lines, lambda l: l.strip().startswith("mv -f"))
        self.assertLess(chmod, first_move)
        self.assertIn(f"chmod 755 -- {STAGING}/02-agenthub.sh", posix_activation())

    def test_activation_keeps_the_generation_it_replaces(self):
        lines = posix_activation().splitlines()
        keep = [index for index, line in enumerate(lines) if line.startswith(f"[ -f ") and "cp -f --" in line]
        first_move = line_of(lines, lambda l: l.strip().startswith("mv -f"))
        self.assertEqual(len(keep), len(entries_fixture()))
        self.assertLess(max(keep), first_move)
        self.assertIn(f"cp -f -- {ROOT_PATH}/.agenthub/agent_hub.py {PREVIOUS}/00-agent_hub.py",
                      posix_activation())

    def test_activation_restores_the_previous_generation_when_the_self_check_fails(self):
        script = posix_activation()
        self.assertIn("restoring the previous generation", script)
        self.assertIn("self-check ok", deploy.self_check_code("/root/agent_hub.py", "/root/project.json"))
        failure = script.split("restoring the previous generation", 1)[1]
        self.assertIn(f"cp -f -- {PREVIOUS}/00-agent_hub.py", failure)
        self.assertIn("chmod 755", failure)
        self.assertIn("exit 1", failure)
        # the staged copy is kept for inspection, so the removal happens only on success
        self.assertLess(script.index("restoring the previous generation"),
                        script.index(f"rm -rf -- {STAGING}"))

    def test_verify_script_only_reads(self):
        script = deploy.verify_script(ROOT_PATH, entries_fixture())
        self.assertEqual(len(script.splitlines()), len(entries_fixture()))
        for forbidden in ("mv ", "cp ", "rm ", "chmod", ">"):
            self.assertNotIn(forbidden, script)
        self.assertTrue(all(line.startswith("sha256sum ") for line in script.splitlines()))

    def test_rollback_restores_only_from_the_retained_generation(self):
        script = deploy.rollback_script(ROOT_PATH, PREVIOUS, entries_fixture(), LAUNCHER,
                                        ".agenthub/agent_hub.py", ".agenthub/project.json")
        self.assertIn("no retained generation to roll back to", script)
        self.assertIn(f"cp -f -- {PREVIOUS}/00-agent_hub.py {ROOT_PATH}/.agenthub/agent_hub.py", script)
        self.assertNotIn("staging-v23/", script.replace(PREVIOUS, ""))
        self.assertIn("self-check ok", script)

    def test_windows_builders_mirror_the_same_ordering(self):
        lines = windows_activation().splitlines()
        last_check = line_of(lines, lambda l: "Get-FileHash" in l and "throw" in l, last=True)
        first_move = line_of(lines, lambda l: l.strip().startswith("Move-Item -Force (Join-Path $staging"))
        self.assertLess(last_check, first_move)
        self.assertIn("restoring the previous generation", "\n".join(lines))
        verify = deploy.verify_script_windows(ROOT_PATH, entries_fixture())
        self.assertIn("Get-FileHash", verify)
        self.assertNotIn("Move-Item", verify)
        rollback = deploy.rollback_script_windows(ROOT_PATH, PREVIOUS, entries_fixture(), LAUNCHER,
                                                  ".agenthub/agent_hub.py", ".agenthub/project.json")
        self.assertIn("$previous", rollback)
        self.assertIn("no retained generation", rollback)

    def test_powershell_quoting_doubles_embedded_quotes(self):
        self.assertEqual(deploy.ps_quote('a "b" c'), 'a ""b"" c')

    def test_self_check_targets_are_found_by_their_registry_sources(self):
        project_dir = ROOT / "projects" / "project_Merman"
        if not (project_dir / "project.json").is_file():
            self.skipTest("no private registration in this checkout")
        runtime, deployed_config = deploy.self_check_targets(
            project_dir,
            [{"stage": "00", "source": ROOT / "agent_hub.py", "destination": ".agenthub/agent_hub.py", "sha256": "a" * 64},
             {"stage": "01", "source": project_dir / "project.json", "destination": ".agenthub/project.json", "sha256": "b" * 64}])
        self.assertEqual(runtime, ".agenthub/agent_hub.py")
        self.assertEqual(deployed_config, ".agenthub/project.json")
        unmatched = deploy.self_check_targets(project_dir, [
            {"stage": "00", "source": ROOT / "README.md", "destination": ".agenthub/README.md", "sha256": "a" * 64}])
        self.assertEqual(unmatched, ("", ""))

    def test_inflight_tasks_warn_by_default_and_can_be_required_to_be_idle(self):
        inflight = [{"task_id": "T-1", "status": "in_progress", "assignee": "worker_a", "claimed_by": "worker_a"}]
        self.assertEqual(deploy.enforce_idle(None, False), "")
        self.assertEqual(deploy.enforce_idle([], True), "")
        self.assertIn("T-1(in_progress)", deploy.enforce_idle(inflight, False))
        with self.assertRaises(SystemExit):
            deploy.enforce_idle(inflight, True)

    def test_active_tasks_parses_the_state_query(self):
        def answer(returncode: int, stdout: str, stderr: str = ""):
            return subprocess.CompletedProcess([], returncode, stdout, stderr)

        with mock.patch.object(deploy.subprocess, "run", return_value=answer(0, "[]")):
            self.assertEqual(deploy.active_tasks("authority", "/state.db"), ([], ""))
        with mock.patch.object(deploy.subprocess, "run", return_value=answer(0, '[{"task_id": "T-1"}]')):
            tasks, note = deploy.active_tasks("authority", "/state.db")
            self.assertEqual(tasks, [{"task_id": "T-1"}])
            self.assertEqual(note, "")
        with mock.patch.object(deploy.subprocess, "run", return_value=answer(0, "not json")):
            tasks, note = deploy.active_tasks("authority", "/state.db")
            self.assertIsNone(tasks)
            self.assertTrue(note)
        with mock.patch.object(deploy.subprocess, "run", return_value=answer(0, '{"tasks": []}')):
            tasks, note = deploy.active_tasks("authority", "/state.db")
            self.assertIsNone(tasks)
            self.assertIn("unexpected shape", note)
        with mock.patch.object(deploy.subprocess, "run", return_value=answer(2, "", "ssh failed")):
            tasks, note = deploy.active_tasks("authority", "/state.db")
            self.assertIsNone(tasks)
            self.assertEqual(note, "ssh failed")


def previous_runtime_source() -> str | None:
    """`agent_hub.py` as of the release before the checked-out one, or None without history."""
    revisions = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--", "agent_hub.py"],
                               capture_output=True, text=True, encoding="utf-8", check=False)
    hashes = revisions.stdout.split()
    if revisions.returncode or len(hashes) < 2:
        return None
    blob = subprocess.run(["git", "-C", str(ROOT), "show", f"{hashes[1]}:agent_hub.py"],
                          capture_output=True, text=True, encoding="utf-8", check=False)
    return blob.stdout if blob.returncode == 0 and blob.stdout else None


class CrossVersionTests(unittest.TestCase):
    setUp = fixtures.AgentHubV23Tests.setUp
    tearDown = fixtures.AgentHubV23Tests.tearDown
    git = fixtures.AgentHubV23Tests.git
    call = fixtures.AgentHubV23Tests.call
    decide = fixtures.AgentHubV23Tests.decide
    packet = fixtures.AgentHubV23Tests.packet

    def previous_runtime(self) -> Path:
        source = previous_runtime_source()
        if source is None:
            self.skipTest("no earlier revision of agent_hub.py in this checkout")
        path = self.root / "previous_agent_hub.py"
        path.write_text(source, encoding="utf-8")
        return path

    def run_previous(self, runtime: Path, *args: str) -> str:
        result = subprocess.run(
            [sys.executable, str(runtime), *self.base, *args],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_task_claimed_under_the_previous_runtime_finishes_under_this_one(self):
        """The upgrade case: the runtime file is swapped while a task is in flight."""
        runtime = self.previous_runtime()
        self.decide()
        self.run_previous(runtime, "--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-UP"))
        claimed = json.loads(self.run_previous(runtime, "--device", "authority", "claim", "worker_a", "T-UP"))
        worktree = Path(claimed["worktree_path"])
        self.assertEqual(claimed["status"], "in_progress")
        self.assertTrue((worktree / ".git").exists())
        self.run_previous(runtime, "--device", "authority", "checkpoint", "worker_a", "T-UP",
                          "--progress", json.dumps({"summary": "started on the old runtime",
                                                    "next_step": "finish", "evidence": []}))

        # the swap, exactly as the adapter performs it: new runtime bytes over the old file
        first = json.loads(self.call("--device", "authority", "next", "worker_a"))["task"]
        self.assertEqual(first["status"], "in_progress")
        self.assertEqual(first["checkpoint"]["summary"], "started on the old runtime")

        (worktree / "README.md").write_text("base\ncompleted after the upgrade\n", encoding="utf-8")
        self.git("add", "README.md", cwd=worktree)
        self.git("commit", "-m", "finish after upgrade", cwd=worktree)
        commit = self.git("rev-parse", "HEAD", cwd=worktree)
        result = json.dumps({"outcome": "completed", "summary": "finished on the new runtime",
                             "commit": commit, "tests": ["upgrade"], "evidence": [f"commit:{commit}"]})
        self.call("--device", "authority", "submit", "worker_a", "T-UP", "--result", result)
        self.call("--device", "client_primary", "review", "planner_agent", "T-UP", "--action", "approve", "--note", "ok")
        self.git("merge", "--ff-only", "agenthub/T-UP")
        self.call("--device", "client_primary", "close", "admin_agent", "T-UP",
                  "--merge-commit", self.git("rev-parse", "HEAD"), "--target-branch", "main", "--note", "upgraded")

        shown = json.loads(self.call("--device", "client_primary", "show", "admin_agent", "T-UP"))
        self.assertEqual(shown["task"]["status"], "closed")
        self.assertFalse(worktree.exists())
        conn = sqlite3.connect(self.state)
        try:
            metadata = dict(conn.execute("SELECT key,value FROM metadata"))
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(metadata["schema_version"], "4")
        self.assertEqual(metadata["agenthub_version"], hub.VERSION)
        self.assertGreaterEqual(events, 6)

    def test_the_previous_runtime_can_still_read_state_this_one_wrote(self):
        """Downgrade compatibility: the schema policy is additive, and this is what checks it."""
        runtime = self.previous_runtime()
        self.decide()
        self.call("--device", "client_primary", "post", "admin_agent", "--spec", self.packet("T-DOWN"))
        self.call("--device", "authority", "claim", "worker_a", "T-DOWN")
        shown = json.loads(self.run_previous(runtime, "--device", "client_primary", "show", "admin_agent", "T-DOWN"))
        self.assertEqual(shown["task"]["status"], "in_progress")
        status = json.loads(self.run_previous(runtime, "--device", "client_primary", "status", "admin_agent", "--json"))
        self.assertEqual([task["id"] for task in status["tasks"]], ["T-DOWN"])


if __name__ == "__main__":
    unittest.main()
