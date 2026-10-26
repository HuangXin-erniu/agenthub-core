from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import shutil
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
    "README.md", "README.zh-CN.md", "SECURITY.md", "docs/PROJECT_REGISTRATION.md", "CHANGELOG.md",
]


class PublicationTests(unittest.TestCase):
    def test_unregistered_core_passes_audit(self):
        if list((ROOT / "projects").glob("*/project.json")):
            self.skipTest("private registration present; run this test in an isolated core checkout")
        result = audit.audit()
        self.assertTrue(result["ok"], result["findings"])
        self.assertEqual(result["projects"], 0)

    def test_version_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in ("README.md", "README.zh-CN.md", "SECURITY.md",
                             "docs/PROJECT_REGISTRATION.md", "CHANGELOG.md"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            self.assertEqual(audit.version_findings(root, hub.VERSION), [])
            readme = root / "README.md"
            readme.write_text(readme.read_text(encoding="utf-8").replace(hub.VERSION, "0.0.0", 1),
                              encoding="utf-8")
            self.assertIn("version:README.md", audit.version_findings(root, hub.VERSION))

    def test_private_registration_audit_accepts_generic_role_and_device_names(self):
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
            config = {"schema_version": 1, "agenthub_version": hub.VERSION, "project_id": "audit_project",
                      "devices": {"windows": {}},
                      "agents": {"administrator": {"roles": ["administrator"], "devices": ["windows"]}},
                      "state_authority": {"host": "windows", "database": str(root / "private-state.db"), "policy": "single_authority_local"},
                      "execution": {"repository": str(root / "repository"), "worktree_root": str(root / "worktrees")},
                      "messaging_transports": [], "lark_workflow": "hard_disabled"}
            (project / "project.json").write_text(json.dumps(config))
            result = audit.audit(root)
            self.assertTrue(result["ok"], result["findings"])
            self.assertEqual(result["projects"], 1)

            (project / ".zcode" / "plans").mkdir(parents=True)
            (project / ".zcode" / "plans" / "plan.md").write_text("local plan")
            (project / "__pycache__").mkdir()
            (project / "__pycache__" / "cache.pyc").write_bytes(b"cache")
            (project / ".DS_Store").write_bytes(b"metadata")
            result = audit.audit(root)
            self.assertTrue(result["ok"], result["findings"])

            (project / "unexpected.md").write_text("real mirror")
            result = audit.audit(root)
            self.assertFalse(result["ok"])
            self.assertEqual(result["findings"],
                             ["mirror:unexpected:projects/audit_project/unexpected.md"])
            (project / "unexpected.md").unlink()

            template_path = root / "templates/project.example.json"
            template = json.loads(template_path.read_text())
            template["messaging_transports"] = ["unreviewed"]
            template_path.write_text(json.dumps(template))
            result = audit.audit(root)
            self.assertFalse(result["ok"])
            self.assertIn("template:transport", result["findings"])

    def test_audit_rejects_a_catalogue_promising_unusable_phases(self):
        """enum_description is label text, so only the audit can catch a contradictory one."""
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
                "devices": {"authority": {}}, "agents": {"administrator": {"roles": ["administrator"], "devices": ["authority"]}},
                "state_authority": {"host": "authority", "database": str(root / "private-state.db"), "policy": "single_authority_local"},
                "execution": {"repository": str(root / "repository"), "worktree_root": str(root / "worktrees")},
                "messaging_transports": [], "lark_workflow": "hard_disabled",
                "task_naming": {
                    "task_pattern": r"(?P<phase>P\d\d)-(?P<domain>[A-Z]+)-(?P<type>[A-Z]+)-(?P<seq>\d{3})",
                    "chain_pattern": r"CHN-(?P<seq>\d{4})", "seq_digits": 3,
                    "enum_description": {"PHASE": "P00-P02/P99", "DOMAIN": "GOV/TASK/SEC/DATA",
                                         "TYPE": "FIX/AUDIT/FEAT"},
                    "phase_domains": {"P00": ["GOV", "TASK", "SEC"], "P99": ["GOV", "TASK", "SEC"]},
                    "domain_types": {"GOV": ["FIX", "AUDIT"], "TASK": ["FIX", "AUDIT", "FEAT"], "SEC": ["FIX", "AUDIT"]},
                },
            }
            (project / "project.json").write_text(json.dumps(config))
            findings = audit.audit(root)["findings"]
            self.assertIn("naming:phase:projects/audit_project/project.json:['P01', 'P02']", findings)
            self.assertIn("naming:domain:projects/audit_project/project.json:['DATA']", findings)

            config["task_naming"]["enum_description"]["PHASE"] = "P00/P99"
            config["task_naming"]["enum_description"]["DOMAIN"] = "GOV/TASK/SEC"
            config["task_naming"]["seq_offset"] = 0
            (project / "project.json").write_text(json.dumps(config))
            findings = audit.audit(root)["findings"]
            self.assertNotIn("naming:phase:projects/audit_project/project.json:[]", findings)
            self.assertEqual([item for item in findings if item.startswith("naming:")],
                             ["naming:seq-offset-ignored:projects/audit_project/project.json"])

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
