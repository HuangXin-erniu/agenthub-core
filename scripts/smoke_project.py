#!/usr/bin/env python3
"""Disposable authority-host integration smoke test for any registered project."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


def select_agent(config: dict, role: str, *, require_admin: bool = False) -> str:
    for agent_id, info in config["agents"].items():
        roles = set(info.get("roles", []))
        if role in roles and (not require_admin or "administrator" in roles):
            return agent_id
    raise RuntimeError(f"project has no suitable {role} agent")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime")
    args = parser.parse_args()
    source_config_path = Path(args.config).resolve()
    source_config = json.loads(source_config_path.read_text(encoding="utf-8"))
    runtime = Path(args.runtime).resolve() if args.runtime else source_config_path.parent / "runtime" / "agent_hub.py"
    module_spec = importlib.util.spec_from_file_location("agenthub_authority_smoke", runtime)
    hub = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader
    module_spec.loader.exec_module(hub)

    source_project = Path(source_config["execution"]["repository"]).resolve()
    authority_device = source_config["state_authority"]["host"]
    administrator = select_agent(source_config, "decider", require_admin=True)
    decider = select_agent(source_config, "decider")
    executor = select_agent(source_config, "executor")
    client_devices = [item for item in source_config["agents"][executor]["devices"] if item != authority_device]
    if not client_devices:
        raise RuntimeError("executor needs a second device to test handoff")
    target_device = client_devices[-1]
    admin_device = source_config["agents"][administrator]["devices"][0]
    decider_device = source_config["agents"][decider]["devices"][0]
    task_id = "SMOKE-" + uuid.uuid4().hex[:8]
    branch = f"agenthub/{task_id}"
    session = "smoke"

    with tempfile.TemporaryDirectory(prefix="agenthub-smoke-") as temp_raw:
        temp = Path(temp_raw)
        project = temp / "repository"
        subprocess.run(["git", "clone", "--no-hardlinks", "--", str(source_project), str(project)],
                       check=True, capture_output=True)
        worktree_root = temp / "worktrees"
        state = temp / "state.db"
        config_path = temp / "project.json"
        config = json.loads(json.dumps(source_config))
        config["execution"]["repository"] = str(project)
        config["execution"]["worktree_root"] = str(worktree_root)
        config.pop("task_naming", None)
        config["state_authority"]["database"] = str(state)
        config.setdefault("remote", {})["enabled"] = False
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        base = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, encoding="utf-8",
        ).stdout.strip()

        def call(*call_args: str) -> dict:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = hub.run(["--local", "--config", str(config_path), "--state", str(state), *call_args])
            if code:
                raise RuntimeError(f"AgentHub returned {code}: {call_args}")
            payload = json.loads(output.getvalue())
            if not payload.get("ok"):
                raise RuntimeError(payload)
            return payload["data"]

        worktree: Path | None = None
        try:
            call("--device", admin_device, "decision", administrator, "--key", "SMOKE-D",
                 "--summary", "route", "--rationale", "AgentHub smoke")
            packet = json.dumps({
                "id": task_id, "title": "AgentHub smoke", "objective": "validate configured project flow",
                "assignee": executor, "decision_key": "SMOKE-D", "base_commit": base,
                "instructions": ["do not edit business files"], "allowed_paths": ["AGENTS.md"],
                "acceptance": ["handoff enforced"],
            }, ensure_ascii=False)
            call("--device", admin_device, "post", administrator, "--spec", packet)
            claimed = call("--device", authority_device, "--session", session, "claim", executor, task_id)
            worktree = Path(claimed["task"]["worktree_path"])
            call("--device", authority_device, "--session", session, "heartbeat", executor, task_id,
                 "--lease-minutes", "1")
            launch = subprocess.run(
                [sys.executable, str(runtime), "--local", "--config", str(config_path), "--state", str(state),
                 "--device", authority_device, "--session", session, "next", executor],
                cwd=worktree, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if launch.returncode:
                raise RuntimeError(f"worktree runtime failed: {launch.stderr}")
            progress = json.dumps({"summary": "safe", "next_step": "handoff", "evidence": []})
            call("--device", authority_device, "--session", session, "checkpoint", executor, task_id,
                 "--progress", progress)
            try:
                call("--device", target_device, "--session", session, "checkpoint", executor, task_id,
                     "--progress", progress)
            except hub.HubError:
                pass
            else:
                raise RuntimeError("non-active device checkpoint was not rejected")
            call("--device", authority_device, "--session", session, "handoff", executor, task_id,
                 "--to-device", target_device, "--note", "smoke")
            call("--device", target_device, "--session", session, "checkpoint", executor, task_id,
                 "--progress", progress)
            blocked = json.dumps({
                "outcome": "blocked", "summary": "smoke blocker",
                "blocker": "intentional", "decision_needed": "confirm history",
            })
            call("--device", target_device, "--session", session, "submit", executor, task_id, "--result", blocked)
            shown = call("--device", admin_device, "show", administrator, task_id)
            assert shown["submissions"][0]["result"]["decision_needed"] == "confirm history"
            call("--device", decider_device, "review", decider, task_id, "--action", "rework",
                 "--note", "history verified")
            print(json.dumps({
                "ok": True, "project": config["project_id"], "task": task_id,
                "authority_device": authority_device, "handoff_device": target_device,
                "runtime_from_worktree": True, "isolated_clone": True, "history": True,
                "session": session,
            }, ensure_ascii=False))
        finally:
            if worktree and worktree.exists():
                subprocess.run(["git", "-C", str(project), "worktree", "remove", "--force", str(worktree)],
                               check=False)
            subprocess.run(["git", "-C", str(project), "branch", "-D", branch], check=False, capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
