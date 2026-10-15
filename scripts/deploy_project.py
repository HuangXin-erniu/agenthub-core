#!/usr/bin/env python3
"""Configuration-driven, hash-verified deployment for an AgentHub project adapter."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def ps_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def load_entries(project_dir: Path, config: dict) -> list[dict[str, str | Path]]:
    raw_entries = config.get("deployment", {}).get("managed_files")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SystemExit("project.json deployment.managed_files must be a non-empty list")
    entries: list[dict[str, str | Path]] = []
    for index, raw in enumerate(raw_entries):
        source = (project_dir / raw["source"]).resolve()
        if not source.is_relative_to(ROOT) or not source.is_file():
            raise SystemExit(f"invalid managed source: {source}")
        destination = raw.get("destination")
        if not isinstance(destination, str) or not destination.strip() or ".." in destination.replace("\\", "/").split("/"):
            raise SystemExit(f"invalid managed destination: {destination}")
        entries.append({
            "stage": f"{index:02d}-{source.name}", "source": source,
            "destination": destination, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        })
    return entries


def deploy_windows(host: str, root: str, staging: str, entries: list[dict[str, str | Path]], reset_state: bool, state: str, local: bool) -> None:
    directories = sorted({str(PureWindowsPath(root) / PureWindowsPath(str(e["destination"])).parent) for e in entries})
    prepare = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        f"Remove-Item -Recurse -Force -ErrorAction SilentlyContinue '{staging}'; "
        + "; ".join(f"New-Item -ItemType Directory -Force -Path '{value}' | Out-Null" for value in [staging, *directories])
    )
    if local:
        run(["powershell", "-NoProfile", "-EncodedCommand", ps_encoded(prepare)])
    else:
        run(["ssh", host, "powershell", "-NoProfile", "-EncodedCommand", ps_encoded(prepare)])
    for entry in entries:
        if local:
            shutil.copy2(entry["source"], PureWindowsPath(staging) / str(entry["stage"]))
        else:
            run(["scp", str(entry["source"]), f"{host}:{staging.replace(chr(92), '/')}/{entry['stage']}"])
    checks = "\n".join(
        f"if((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $staging '{e['stage']}')).Hash.ToLower() -ne '{e['sha256']}') {{ throw 'hash mismatch: {e['stage']}' }}"
        for e in entries
    )
    moves = "\n".join(
        f"Move-Item -Force (Join-Path $staging '{e['stage']}') '{root}\\{e['destination']}'" for e in entries
    )
    reset = f"Remove-Item -Force -ErrorAction SilentlyContinue '{state}*'" if reset_state else ""
    activate = f"$ErrorActionPreference='Stop'\n$ProgressPreference='SilentlyContinue'\n$staging='{staging}'\n{checks}\n{reset}\n{moves}\nRemove-Item -Recurse -Force -ErrorAction SilentlyContinue $staging\nWrite-Output 'AgentHub activation complete'"
    remote_script = root + "\\.agenthub\\activate-v23.ps1"
    with tempfile.TemporaryDirectory(prefix="agenthub-deploy-") as temp:
        local_script = Path(temp) / "activate-v23.ps1"
        local_script.write_text(activate, encoding="utf-8-sig")
        if local:
            shutil.copy2(local_script, PureWindowsPath(remote_script))
        else:
            run(["scp", str(local_script), f"{host}:{remote_script.replace(chr(92), '/')}"])
    if local:
        run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", remote_script])
    else:
        run(["ssh", host, "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", remote_script])
    cleanup = f"Remove-Item -Force -ErrorAction SilentlyContinue '{remote_script}'"
    if local:
        run(["powershell", "-NoProfile", "-EncodedCommand", ps_encoded(cleanup)])
    else:
        run(["ssh", host, "powershell", "-NoProfile", "-EncodedCommand", ps_encoded(cleanup)])


def is_local_windows_target(host: str) -> bool:
    """Use local file operations when a Windows authority invokes its own adapter."""
    return os.name == "nt" and host.casefold() in {"windows", "localhost", "127.0.0.1", "::1"}


def deploy_posix(host: str, root: str, staging: str, entries: list[dict[str, str | Path]], reset_state: bool, state: str) -> None:
    directories = sorted({str(Path(root) / Path(str(e["destination"])).parent) for e in entries})
    prepare = "set -eu; " + "; ".join(f"mkdir -p -- {shlex.quote(value)}" for value in [staging, *directories])
    run(["ssh", host, "sh", "-c", shlex.quote(prepare)])
    for entry in entries:
        run(["scp", str(entry["source"]), f"{host}:{staging}/{entry['stage']}"])
    commands = ["set -eu"]
    commands.extend(
        f"test \"$(sha256sum {shlex.quote(staging + '/' + str(e['stage']))} | cut -d' ' -f1)\" = {shlex.quote(str(e['sha256']))}"
        for e in entries
    )
    if reset_state:
        commands.append(f"rm -f -- {shlex.quote(state)} {shlex.quote(state + '-wal')} {shlex.quote(state + '-shm')}")
    commands.extend(
        f"mv -f -- {shlex.quote(staging + '/' + str(e['stage']))} {shlex.quote(root + '/' + str(e['destination']))}"
        for e in entries
    )
    commands.append(f"rmdir -- {shlex.quote(staging)}")
    run(["ssh", host, "sh", "-c", shlex.quote("; ".join(commands))])


def verify_posix(host: str, root: str, entries: list[dict[str, str | Path]]) -> list[dict[str, object]]:
    """Compare the registry sources against what is deployed, without writing anything.

    A deployed copy that has drifted from its source is invisible to the structural audit,
    which is local and static; this is the read-only half of the deployment contract.
    """
    script = "; ".join(
        f"sha256sum {shlex.quote(root + '/' + str(entry['destination']))}" for entry in entries
    )
    result = subprocess.run(
        ["ssh", host, "sh", "-c", shlex.quote(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    deployed_by_path: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            deployed_by_path[fields[-1]] = fields[0]
    report: list[dict[str, object]] = []
    for entry in entries:
        expected = str(entry["sha256"])
        deployed = deployed_by_path.get(root + "/" + str(entry["destination"]))
        report.append({
            "destination": str(entry["destination"]),
            "registry": expected,
            "deployed": deployed,
            "match": deployed == expected,
        })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    project_dir = ROOT / "projects" / args.project
    config_path = project_dir / "project.json"
    if not config_path.is_file():
        raise SystemExit(f"unknown project registration: {args.project}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("project_id") != args.project:
        raise SystemExit("project directory and project_id mismatch")
    remote = config.get("remote", {})
    host, project_root = remote.get("ssh_host"), remote.get("project_root")
    shell = remote.get("shell")
    if not host or not project_root or shell not in {"windows_cmd", "posix_sh"}:
        raise SystemExit("remote ssh_host/project_root/shell is incomplete")
    expected_adapter = {"windows_cmd": "ssh_atomic_windows", "posix_sh": "ssh_atomic_posix"}[shell]
    if config.get("deployment", {}).get("adapter") != expected_adapter:
        raise SystemExit(f"deployment.adapter must be {expected_adapter}")
    entries = load_entries(project_dir, config)
    if args.dry_run:
        print(json.dumps({
            "dry_run": True, "project": args.project, "host": host, "shell": shell,
            "files": [{"source": str(e["source"]), "destination": e["destination"], "sha256": e["sha256"]} for e in entries],
        }, ensure_ascii=False, indent=2))
        return 0
    separator = "\\" if shell == "windows_cmd" else "/"
    staging = project_root + separator + ".agenthub" + separator + "staging-v23"
    state = config["state_authority"]["database"]
    if args.verify:
        if shell != "posix_sh":
            raise SystemExit("--verify is implemented for posix_sh alone; compare the deployed "
                             "files on a windows_cmd authority by hand")
        report = verify_posix(host, project_root, entries)
        ok = all(item["match"] for item in report)
        print(json.dumps({
            "verify": True, "ok": ok, "project": args.project, "host": host, "files": report,
        }, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    if shell == "windows_cmd":
        deploy_windows(host, project_root, staging, entries, args.reset_state, state, is_local_windows_target(host))
    else:
        deploy_posix(host, project_root, staging, entries, args.reset_state, state)
    print(json.dumps({
        "deployed": True, "project": args.project, "host": host, "shell": shell,
        "files": [{"destination": e["destination"], "sha256": e["sha256"]} for e in entries],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
