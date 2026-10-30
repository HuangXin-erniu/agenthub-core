from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load("release_checks", "scripts/check_release.py")
audit = load("publication_audit", "scripts/audit_agenthub.py")
hub = load("agenthub_runtime", "agent_hub.py")

CORE_COPY = [
    "agent_hub.py", "scripts/deploy_project.py", "scripts/smoke_project.py",
    "scripts/check_release.py", "scripts/audit_agenthub.py", "templates/project.example.json",
    "CHANGELOG.md",
]


class PublicationTests(unittest.TestCase):
    def test_repository_audit_passes(self):
        result = audit.audit(ROOT)
        self.assertTrue(result["ok"], result["findings"])

    def test_changelog_version_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copyfile(ROOT / "CHANGELOG.md", root / "CHANGELOG.md")
            self.assertEqual(audit.changelog_findings(root, hub.VERSION), [])
            changelog = root / "CHANGELOG.md"
            changelog.write_text(changelog.read_text(encoding="utf-8").replace(
                f"## {hub.VERSION} —", "## 0.0.0 —", 1), encoding="utf-8")
            self.assertIn("version:CHANGELOG.md", audit.changelog_findings(root, hub.VERSION))

    def test_private_registration_audit_accepts_generic_names_and_legacy_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in CORE_COPY:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            project = root / "projects" / "audit_project"
            project.mkdir(parents=True)
            for name in ("AGENTS.md", "README.md"):
                (project / name).write_text("private adapter")
            config = {
                "schema_version": 1, "agenthub_version": hub.VERSION, "project_id": "audit_project",
                "devices": {"windows": {}},
                "agents": {"administrator": {"roles": ["administrator"], "devices": ["windows"]}},
                "state_authority": {"host": "windows", "database": str(root / "private-state.db"),
                                    "policy": "single_authority_local"},
                "execution": {"repository": str(root / "repository"), "worktree_root": str(root / "worktrees")},
                "messaging_transports": [], "lark_workflow": "hard_disabled",
                "task_naming": {
                    "task_pattern": r"TASK-(?P<seq>\d{3})",
                    "chain_pattern": r"CHN-(?P<seq>\d{4})", "seq_digits": 3,
                    "enum_description": {"PHASE": "ignored"},
                    "phase_domains": {"P00": ["GOV"]},
                    "domain_types": {"GOV": ["FIX"]},
                },
            }
            (project / "project.json").write_text(json.dumps(config))
            result = audit.audit(root)
            self.assertTrue(result["ok"], result["findings"])
            self.assertEqual(result["projects"], 1)
            (project / ".zcode" / "plans").mkdir(parents=True)
            (project / ".zcode" / "plans" / "plan.md").write_text("local plan")
            (project / "__pycache__").mkdir()
            (project / "__pycache__" / "cache.pyc").write_bytes(b"cache")
            (project / ".DS_Store").write_bytes(b"metadata")
            self.assertTrue(audit.audit(root)["ok"])
            (project / "unexpected.md").write_text("real mirror")
            result = audit.audit(root)
            self.assertFalse(result["ok"])
            self.assertEqual(result["findings"], ["mirror:unexpected:projects/audit_project/unexpected.md"])
            (project / "unexpected.md").unlink()

            template_path = root / "templates/project.example.json"
            template = json.loads(template_path.read_text())
            template["phase_domains"] = {"P00": ["GOV"]}
            template_path.write_text(json.dumps(template))
            findings = audit.audit(root)["findings"]
            self.assertIn("template:legacy-key:phase_domains", findings)

    def test_spec_lists_the_public_command_surface(self):
        text = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
        for name in hub.PUBLIC_COMMANDS:
            self.assertIn(f"`{name}`", text)
        self.assertNotIn("| `init`", text)
        self.assertNotIn("| `claim-next`", text)
        self.assertNotIn("| `rpc`", text)

    def test_redaction_and_credential_detection(self):
        token = "ghp_" + "A" * 36
        findings = release.scan_content("fixture.txt", ("access " + token).encode())
        self.assertEqual(findings, ["github-token:fixture.txt:1"])
        self.assertNotIn(token, json.dumps(findings))
        key = "-----BEGIN " + "PRIVATE KEY-----"
        self.assertEqual(release.scan_content("fixture.txt", key.encode()), ["private-key:fixture.txt:1"])
        password = 'password = "' + "a" * 16 + '"'
        self.assertIn("assigned-secret:fixture.txt:1", release.scan_content("fixture.txt", password.encode()))

    def test_privacy_detection_and_safe_test_email(self):
        personal = "person" + "@" + "private" + ".invalid"
        findings = release.scan_content("fixture.txt", personal.encode())
        self.assertEqual(findings, ["private-email:fixture.txt:1"])
        self.assertEqual(release.scan_content("fixture.txt", b"agenthub@example.invalid"), [])
        self.assertEqual(release.scan_content("fixture.txt", b"noreply@github.com"), [])
        private_github = "person" + "@" + "github.com"
        self.assertEqual(release.scan_content("fixture.txt", private_github.encode()), ["private-email:fixture.txt:1"])
        private_path = "C:" + "\\" + "Users" + "\\" + "person"
        self.assertEqual(release.scan_content("fixture.txt", private_path.encode()), ["private-user-path:fixture.txt:1"])

    def test_manifest_rejects_extra_missing_and_symlink_files(self):
        manifest = json.dumps(["release-files.json", "README.md"]).encode()
        files = {"release-files.json": ("100644", manifest), "README.md": ("100644", b"safe")}
        self.assertEqual(release.validate_files(files), [])
        files["private.env"] = ("100644", b"safe")
        self.assertIn("path:not-allowlisted:private.env", release.validate_files(files))
        del files["private.env"]
        files["README.md"] = ("120000", b"elsewhere")
        self.assertIn("path:unsafe-mode:README.md", release.validate_files(files))
        del files["README.md"]
        self.assertIn("path:missing:README.md", release.validate_files(files))

    def test_manifest_matches_the_intended_public_surface(self):
        manifest = set(release.load_manifest((ROOT / "release-files.json").read_bytes()))
        for required in ("agent_hub.py", "docs/SPEC.md", "tests/support.py",
                         "tests/test_cli.py", "tests/test_migration.py"):
            self.assertIn(required, manifest)
        self.assertNotIn("docs/CONTROL_PLANE_OPERATING_STANDARD.md", manifest)
        self.assertNotIn("tests/test_agenthub.py", manifest)
        self.assertNotIn("tests/test_contract_extensions.py", manifest)
        self.assertNotIn("tests/test_dual_role.py", manifest)

    def test_manifest_forbids_instance_registration_and_traversal(self):
        for name in ("projects/private/project.json", "../outside.txt"):
            with self.assertRaises(ValueError):
                release.load_manifest(json.dumps(["release-files.json", name]).encode())

    def test_history_scan_finds_secret_removed_from_head(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

            git("init", "-b", "main")
            git("config", "user.name", "AgentHub Test")
            git("config", "user.email", "agenthub@example.invalid")
            (root / "release-files.json").write_text(json.dumps(["release-files.json", "README.md"]))
            (root / "README.md").write_text("ghp_" + "B" * 36)
            git("add", ".")
            git("commit", "-m", "first")
            (root / "README.md").write_text("safe")
            git("add", ".")
            git("commit", "-m", "clean head")
            self.assertEqual(release.validate_files(release.tree_files(root, "HEAD")), [])
            old_root = release.ROOT
            release.ROOT = root
            try:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = release.main(["--history"])
                self.assertEqual(code, 1)
                self.assertIn("github-token:README.md:1", output.getvalue())
                self.assertNotIn("ghp_" + "B" * 36, output.getvalue())
            finally:
                release.ROOT = old_root


if __name__ == "__main__":
    unittest.main()
