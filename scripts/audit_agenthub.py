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


def phase_tokens(value: object) -> set[str]:
    """Expand a PHASE catalogue string such as "P00-P04/P99" into its tokens."""
    tokens: set[str] = set()
    for chunk in str(value).split("/"):
        chunk = chunk.strip()
        span = re.fullmatch(r"(P\d\d)-(P\d\d)", chunk)
        if span:
            low, high = int(span.group(1)[1:]), int(span.group(2)[1:])
            tokens.update(f"P{index:02d}" for index in range(low, high + 1))
        elif chunk:
            tokens.add(chunk)
    return tokens


def catalogue_values(mapping: object) -> set[str]:
    """Every string listed across a phase_domains / domain_types mapping."""
    if not isinstance(mapping, dict):
        return set()
    return {item for value in mapping.values() if isinstance(value, (list, tuple, set))
            for item in value if isinstance(item, str)}


def naming_findings(naming: object, label: str) -> list[str]:
    """The advertised catalogue must name exactly what the runtime enforces.

    The runtime accepts a phase only if it is a phase_domains key, a domain only if some phase
    allows it, and a type only if some domain allows it (agent_hub.py:599-608). enum_description
    is label text the runtime never reads, so without this check a catalogue can advertise
    phases and domains that are impossible to use and still be reported as consistent.
    domain_types may list unused vocabulary; what it must not do is contradict the strings.
    """
    findings: list[str] = []
    if not isinstance(naming, dict) or not naming:
        return findings
    if "seq_offset" in naming:
        findings.append(f"naming:seq-offset-ignored:{label}")
    description = naming.get("enum_description")
    if not isinstance(description, dict):
        return findings
    phase_domains = naming.get("phase_domains") if isinstance(naming.get("phase_domains"), dict) else {}
    domain_types = naming.get("domain_types") if isinstance(naming.get("domain_types"), dict) else {}
    if phase_domains:
        declared = set(phase_domains)
        claimed = phase_tokens(description.get("PHASE", ""))
        if claimed != declared:
            findings.append(f"naming:phase:{label}:{sorted(claimed ^ declared)}")
    domains_allowed = catalogue_values(phase_domains)
    claimed_domains = {chunk.strip() for chunk in str(description.get("DOMAIN", "")).split("/") if chunk.strip()}
    if claimed_domains != domains_allowed:
        findings.append(f"naming:domain:{label}:{sorted(claimed_domains ^ domains_allowed)}")
    types_allowed = catalogue_values(domain_types)
    claimed_types = {chunk.strip() for chunk in str(description.get("TYPE", "")).split("/") if chunk.strip()}
    if claimed_types != types_allowed:
        findings.append(f"naming:type:{label}:{sorted(claimed_types ^ types_allowed)}")
    return findings


def version_findings(root: Path, version: str) -> list[str]:
    """The runtime version is the source of truth; every published surface must agree."""
    findings: list[str] = []
    markers = {
        "README.md": f"# AgentHub Core {version}",
        "README.zh-CN.md": f"# AgentHub Core {version}",
        "SECURITY.md": f"AgentHub {version}",
        "docs/PROJECT_REGISTRATION.md": f"# AgentHub v{version} project registration standard",
    }
    for relative, marker in markers.items():
        try:
            text = (root / relative).read_text(encoding="utf-8")
        except OSError:
            findings.append(f"version:{relative}")
            continue
        if marker not in text:
            findings.append(f"version:{relative}")
    try:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        findings.append("version:CHANGELOG.md")
    else:
        if f"## {version} —" not in changelog:
            findings.append("version:CHANGELOG.md")
    return findings


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
    findings.extend(version_findings(root, hub.VERSION))

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
        hub.validate_execution_config(template["execution"])
        findings.extend(naming_findings(template.get("task_naming"), "template"))
    except (OSError, ValueError, KeyError, TypeError, re.error, hub.HubError):
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
        findings.extend(naming_findings(config.get("task_naming"), label))
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
