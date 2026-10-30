#!/usr/bin/env python3
"""Read-only structural audit for core checkouts and private registrations."""
from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Local tool and OS metadata is not part of a project registration. Ignoring it keeps the
# mirror check focused on real files while still flagging every other undeclared entry.
IGNORED_PROJECT_DIRS = {".git", ".zcode", "__pycache__"}
IGNORED_PROJECT_FILES = {".DS_Store"}
IGNORED_PROJECT_SUFFIXES = {".pyc"}
FORBIDDEN_TEMPLATE_KEYS = {
    "agenthub_version", "project_name", "workflow", "messaging_transports", "lark_workflow",
    "concurrency", "isolation", "enum_description", "phase_domains", "domain_types",
}
FORBIDDEN_CORE_MARKERS = ("phase_domains", "domain_types", "enum_description",
                          "messaging_transports", "lark_workflow")


def ignored_project_artifact(relative: Path) -> bool:
    if any(part in IGNORED_PROJECT_DIRS for part in relative.parts[:-1]):
        return True
    return relative.name in IGNORED_PROJECT_FILES or relative.suffix in IGNORED_PROJECT_SUFFIXES


def instruction_files(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    if isinstance(value, dict):
        return set().union(*(instruction_files(item) for item in value.values()))
    return set()


def changelog_findings(root: Path, version: str) -> list[str]:
    try:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return ["version:CHANGELOG.md"]
    return [] if f"## {version} —" in changelog else ["version:CHANGELOG.md"]


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
    findings.extend(changelog_findings(root, hub.VERSION))

    try:
        template = json.loads((root / "templates/project.example.json").read_text(encoding="utf-8"))
        for key in sorted(FORBIDDEN_TEMPLATE_KEYS & set(template)):
            findings.append(f"template:legacy-key:{key}")
        if "StrictHostKeyChecking=yes" not in template["remote"]["ssh_options"]:
            findings.append("template:ssh-host-checking")
        naming = template.get("task_naming", {})
        for name in ("task_pattern", "chain_pattern"):
            if name not in naming or "seq" not in re.compile(naming[name]).groupindex:
                findings.append(f"template:{name}")
        hub.validate_execution_config(template["execution"])
    except (OSError, ValueError, KeyError, TypeError, re.error, hub.HubError):
        findings.append("template:invalid")

    for surface in ("agent_hub.py", "templates/project.example.json"):
        try:
            content = (root / surface).read_text(encoding="utf-8")
        except OSError:
            findings.append(f"read:{surface}")
            continue
        for marker in FORBIDDEN_CORE_MARKERS:
            if marker in content:
                findings.append(f"core:forbidden-marker:{surface}:{marker}")

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
        if config["state_authority"].get("policy") != "single_authority_local":
            findings.append(f"config:authority:{label}")
        instructions = config.get("instructions", {})
        if isinstance(instructions, dict) and instructions:
            if "common" not in instructions:
                findings.append(f"instructions:common:{label}")
            for agent_id, info in config["agents"].items():
                if info.get("enabled", True) and "executor" in info.get("roles", []) and agent_id not in instructions:
                    findings.append(f"instructions:executor:{label}:{agent_id}")
            for key in instructions:
                if key != "common" and key not in config["agents"]:
                    findings.append(f"instructions:unknown-agent:{label}:{key}")
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
            if not target.is_file():
                continue
            relative = target.relative_to(project_dir)
            if ignored_project_artifact(relative):
                continue
            if relative.as_posix() not in required:
                findings.append(f"mirror:unexpected:{target.relative_to(root).as_posix()}")
        identities.update([project_id, *(agent for agent in config["agents"] if agent not in hub.PERMISSIONS)])
        identities.update(str(value) for value in (
            config["execution"]["repository"], config["execution"]["worktree_root"],
            config["state_authority"]["database"],
        ))
        for surface in ("agent_hub.py", "scripts/deploy_project.py"):
            content = (root / surface).read_text(encoding="utf-8")
            for marker in identities:
                if len(marker) >= 4 and re.search(
                    r"(?<![A-Za-z0-9_])" + re.escape(marker) + r"(?![A-Za-z0-9_])", content
                ):
                    findings.append(f"boundary:instance-marker:{surface}")

    for target in root.rglob("*.db"):
        if ".git" not in target.parts and target.resolve() not in allowed_states:
            findings.append(f"state:unexpected:{target.relative_to(root).as_posix()}")
    for relative in ("lark_agent_hub.py", "lark_listener.py"):
        if (root / relative).exists():
            findings.append(f"legacy:unexpected:{relative}")
    return {"ok": not findings, "version": hub.VERSION, "projects": len(configs),
            "findings": sorted(set(findings))}


def main() -> int:
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
