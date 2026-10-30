from __future__ import annotations

import ast
import json
import unittest

from support import ROOT, HubFixture, hub


class CliTests(HubFixture):
    def test_version_and_help_surface(self):
        result = self.cli("--version", config=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "AgentHub 2.4.5")
        help_result = self.cli("--help", config=False)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for name in hub.PUBLIC_COMMANDS:
            self.assertIn(name, help_result.stdout)
        for removed in ("init", "claim-next", "rpc"):
            self.assertNotIn(removed, help_result.stdout)

    def test_envelope_and_text_modes(self):
        raw = self.raw_call("--device", "client_primary", "decision", "admin_agent",
                            "--key", "D-1", "--summary", "s", "--rationale", "r")
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["key"], "D-1")
        text = self.call_text("--device", "client_primary", "context", "admin_agent")
        self.assertIn("role=decider", text)
        self.assertIn("D-1", text)

    def test_error_envelope_and_text(self):
        result = self.cli("--device", "client_primary", "show", "admin_agent", "NOPE")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "task.not_found")
        text_result = self.cli("--text", "--device", "client_primary", "show", "admin_agent", "NOPE")
        self.assertEqual(text_result.returncode, 2)
        self.assertIn("[AgentHub] task.not_found:", text_result.stderr)

    def test_removed_commands_are_not_accepted(self):
        self.assertEqual(self.cli("init", "admin_agent").returncode, 2)
        self.assertEqual(self.cli("claim-next", "worker_a").returncode, 2)

    def test_project_is_never_inferred_from_a_single_registration(self):
        with self.assertRaises(hub.HubError) as raised:
            hub.config_path(None, None)
        self.assertEqual(raised.exception.code, "input.invalid")

    def test_status_visibility_is_role_scoped(self):
        self.decide()
        self.post(self.packet("T-A", "worker_a"))
        self.post(self.packet("T-B", "worker_b"))
        own = self.call("--device", "authority", "status", "worker_a")
        self.assertEqual([task["id"] for task in own["tasks"]], ["T-A"])
        admin = self.call("--device", "client_primary", "status", "admin_agent")
        self.assertEqual({task["id"] for task in admin["tasks"]}, {"T-A", "T-B"})
        with self.assertRaises(hub.HubError) as raised:
            self.call("--device", "authority", "status", "worker_a", "--assignee", "worker_b")
        self.assertEqual(raised.exception.code, "identity.permission")

    def test_claim_without_task_id_takes_one_ready_task(self):
        self.decide()
        self.post(self.packet("T-1"))
        self.post(self.packet("T-2"))
        claimed = self.claim(None, "worker_a", "authority")
        self.assertIsNotNone(claimed["task"])
        self.assertIn(claimed["task"]["id"], {"T-1", "T-2"})
        self.assertEqual(claimed["concurrency"], {"active": 1, "limit": 1})

    def test_status_json_flag_is_a_compatible_noop(self):
        self.decide()
        data = self.call("--device", "client_primary", "status", "admin_agent", "--json")
        self.assertIn("tasks", data)

    def test_error_codes_are_literal_and_documented(self):
        source = (ROOT / "agent_hub.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HubError":
                self.assertGreaterEqual(len(node.args), 2, ast.unparse(node))
                self.assertIsInstance(node.args[0], ast.Constant, ast.unparse(node))
                used.add(node.args[0].value)
        self.assertTrue(used <= hub.ERROR_CODES, used - hub.ERROR_CODES)
        spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
        for code in hub.ERROR_CODES:
            self.assertIn(f"`{code}`", spec, code)


if __name__ == "__main__":
    unittest.main()
