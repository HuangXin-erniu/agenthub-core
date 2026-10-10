#!/usr/bin/env python3
"""Read-only structural audit for core checkouts and private registrations."""
from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def instruction_files(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    if isinstance(value, dict):
        return set().union(*(instruction_files(item) for item in value.values()))
    return set()


def audit(root: Path = ROOT) -> dict:
    root = root.resolve()
    findings: list[str] = []
    core_files = ["agent_hub.py", "scripts/deploy_project.py", "scripts/smoke_project.py",
                  "scripts/check_release.py", "scripts/audit_agenthub.py"]
    for relative in core_files:
        try:
            ast.parse((root / relative).read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError):
            findings.append(f"syntax:{relative}")
    if findings:
        return {"ok": False, "projects": 0, "findings": findings}

    spec = importlib.util.spec_from_file_location("agenthub_structural_audit", root / "agent_hub.py")
    hub = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(hub)

    try:
        template = json.loads((root / "templates/project.example.json").read_text(encoding="utf-8"))
        if template["agenthub_version"] != hub.VERSION:
            findings.append("template:version")
        if template.get("messaging_transports") != [] or template.get("lark_workflow") != "hard_disabled":
            findings.append("template:transport")
        if "StrictHostKeyChecking=yes" not in template["remote"]["ssh_options"]:
            findings.append("template:ssh-host-checking")
        for name in ("task_pattern", "chain_pattern"):
            if "seq" not in re.compile(template["task_naming"][name]).groupindex:
                findings.append(f"template:{name}")
    except (OSError, ValueError, KeyError, TypeError, re.error):
        findings.append("template:invalid")

    configs = sorted((root / "projects").glob("*/project.json"))
    identities: set[str] = set()
    seen: dict[tuple[str, str], str] = {}
    allowed_states: set[Path] = set()
    for config_path in configs:
        label = config_path.relative_to(root).as_posix()
        try:
            config = hub.load_config(config_path)
        except Exception:
            findings.append(f"config:invalid:{label}")
            continue
        project_dir = config_path.parent
        project_id = config["project_id"]
        if project_id != project_dir.name:
            findings.append(f"config:id:{label}")
        if config.get("agenthub_version") != hub.VERSION:
            findings.append(f"config:version:{label}")
        if config.get("messaging_transports") != [] or config.get("lark_workflow") != "hard_disabled":
            findings.append(f"config:transport:{label}")
        if config["state_authority"].get("policy") != "single_authority_local":
            findings.append(f"config:authority:{label}")
        allowed_states.add(Path(config["state_authority"]["database"]).resolve())
        for category, value in (
            ("repository", config["execution"]["repository"]),
            ("state", config["state_authority"]["database"]),
            ("worktree", config["execution"]["worktree_root"]),
        ):
            normalized = str(value).replace("\\", "/").rstrip("/").casefold()
            key = (category, normalized)
            if key in seen:
                findings.append(f"isolation:{category}:{label}")
            seen[key] = project_id
        required = {"project.json", "README.md", "AGENTS.md"}
        required.update(instruction_files(config.get("instructions", {})))
        for entry in config.get("deployment", {}).get("managed_files", []):
            source = entry.get("source") if isinstance(entry, dict) else None
            if not isinstance(source, str):
                findings.append(f"deployment:source:{label}")
                continue
            source_path = (project_dir / source).resolve()
            if not source_path.is_relative_to(root) or not source_path.is_file():
                findings.append(f"deployment:source:{label}")
            elif source_path.is_relative_to(project_dir):
                required.add(source_path.relative_to(project_dir).as_posix())
        for relative in sorted(required):
            target = (project_dir / relative).resolve()
            if not target.is_relative_to(project_dir) or not target.is_file():
                findings.append(f"instructions:missing-or-outside:{label}")
        for target in project_dir.rglob("*"):
            if target.is_file() and target.relative_to(project_dir).as_posix() not in required:
                findings.append(f"mirror:unexpected:{target.relative_to(root).as_posix()}")
        identities.update([project_id, *(agent for agent in config["agents"] if agent not in hub.PERMISSIONS)])
        identities.update(str(value) for value in (config["execution"]["repository"], config["execution"]["worktree_root"], config["state_authority"]["database"]))
        for surface in ("agent_hub.py", "scripts/deploy_project.py"):
            content = (root / surface).read_text(encoding="utf-8")
            for marker in identities:
                if len(marker) >= 4 and re.search(r"(?<![A-Za-z0-9_])" + re.escape(marker) + r"(?![A-Za-z0-9_])", content):
                    findings.append(f"boundary:instance-marker:{surface}")

    for target in root.rglob("*.db"):
        if ".git" not in target.parts and target.resolve() not in allowed_states:
            findings.append(f"state:unexpected:{target.relative_to(root).as_posix()}")
    for relative in ("lark_agent_hub.py", "lark_listener.py"):
        if (root / relative).exists():
            findings.append(f"legacy:unexpected:{relative}")
    return {"ok": not findings, "version": hub.VERSION, "projects": len(configs), "findings": sorted(set(findings))}


def main() -> int:
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
