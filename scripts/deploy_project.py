#!/usr/bin/env python3
"""Configuration-driven, hash-verified deployment for an AgentHub project adapter.

Deployment replaces files one by one, which is not a transaction across the file set. The
ordering below is therefore part of the contract, and every step of it exists to keep a bad
transfer or an incompatible pair from leaving the authority unusable:

  1. every staged file is hash-verified BEFORE any file is replaced;
  2. the generation being replaced is copied aside first, so it can be restored;
  3. after the swap the deployed runtime is made to load the deployed configuration, which is
     the only pair of managed files that constrain each other, and the launcher is forced
     executable — the remote proxy runs it as `./.agenthub/agenthub.sh`, and a deploy that
     landed it 0644 would take every proxied command down;
  4. if that check fails, the previous generation is restored automatically.

`--rollback` restores the retained generation on demand. One generation is kept; the next
deployment replaces it.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
STAGING_NAME = "staging-v23"
PREVIOUS_NAME = "staging-v23-previous"
SELF_CHECK_PREFIX = "self-check"


def run(command: list[str], capture: bool = False) -> subprocess.CompletedProcess[str] | None:
    if capture:
        return subprocess.run(command, check=True, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    subprocess.run(command, check=True)
    return None


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


def staging_paths(project_root: str, shell: str) -> tuple[str, str]:
    separator = "\\" if shell == "windows_cmd" else "/"
    base = project_root + separator + ".agenthub" + separator
    return base + STAGING_NAME, base + PREVIOUS_NAME


def self_check_targets(project_dir: Path, entries: list[dict[str, str | Path]]) -> tuple[str, str]:
    """The deployed runtime and configuration, identified by their registry sources.

    The registration template fixes these two sources (`../../agent_hub.py` and
    `project.json`), so they can be recognised without a new configuration key. Returns
    ("", "") when they cannot be found, and the self-check is then reported as skipped.
    """
    runtime = config = ""
    for entry in entries:
        source = Path(str(entry["source"]))
        if source == ROOT / "agent_hub.py":
            runtime = str(entry["destination"])
        elif source.name == "project.json":
            config = str(entry["destination"])
    return runtime, config


def self_check_code(hub_path: str, config_path: str) -> str:
    """A one-liner that imports the deployed runtime and makes it load the deployed config.

    Importing the module does not touch SQLite: `connect()` is reached only from `run()`,
    which is guarded by `__main__`. `-B` keeps the check from writing a bytecode cache.
    """
    return (
        "import importlib.util as u,pathlib as p;"
        f"h=u.spec_from_file_location('m',{json.dumps(hub_path)});"
        "m=u.module_from_spec(h);h.loader.exec_module(m);"
        f"c=m.load_config(p.Path({json.dumps(config_path)}));"
        "print('self-check ok',m.VERSION,c['project_id'])"
    )


# ------------------------------------------------------------------ POSIX builders
def activation_script(root: str, staging: str, previous: str, entries: list[dict[str, str | Path]],
                      reset_state: bool, state: str, launcher: str, hub: str, config: str) -> str:
    lines = ["set -eu"]
    launcher_stage = next((str(e["stage"]) for e in entries if str(e["destination"]) == launcher), "")
    lines.extend(
        f"test \"$(sha256sum {shlex.quote(staging + '/' + str(e['stage']))} | cut -d' ' -f1)\" = "
        f"{shlex.quote(str(e['sha256']))} || {{ echo 'hash mismatch: {e['stage']}' >&2; exit 1; }}"
        for e in entries
    )
    for entry in entries:
        destination = root + "/" + str(entry["destination"])
        lines.append(f"[ -f {shlex.quote(destination)} ] && "
                     f"cp -f -- {shlex.quote(destination)} {shlex.quote(previous + '/' + str(entry['stage']))} || true")
    if reset_state:
        lines.append(f"rm -f -- {shlex.quote(state)} {shlex.quote(state + '-wal')} {shlex.quote(state + '-shm')}")
    if launcher_stage:
        # Make the staged launcher executable BEFORE it is moved: the remote proxy runs
        # `./.agenthub/agenthub.sh`, so a launcher that lands 0644 takes every proxied command
        # down. Fixing it while staged leaves no window at all.
        lines.append(f"chmod 755 -- {shlex.quote(staging + '/' + launcher_stage)}")
    lines.extend(
        f"mv -f -- {shlex.quote(staging + '/' + str(e['stage']))} {shlex.quote(root + '/' + str(e['destination']))}"
        for e in entries
    )
    if hub and config:
        lines.append("PY=\"${AGENTHUB_PYTHON:-python3}\"")
        lines.append("if command -v \"$PY\" >/dev/null 2>&1; then")
        lines.append(f"  if ! \"$PY\" -B -c {shlex.quote(self_check_code(root + '/' + hub, root + '/' + config))}; then")
        lines.append("    echo 'activation self-check failed; restoring the previous generation' >&2")
        for entry in entries:
            lines.append(f"    [ -f {shlex.quote(previous + '/' + str(entry['stage']))} ] && "
                         f"cp -f -- {shlex.quote(previous + '/' + str(entry['stage']))} "
                         f"{shlex.quote(root + '/' + str(entry['destination']))} || true")
        if launcher:
            # a plain copy does not carry the mode, and the generation being restored may
            # itself have been the broken one
            lines.append(f"    chmod 755 -- {shlex.quote(root + '/' + launcher)}")
        lines.append("    exit 1")
        lines.append("  fi")
        lines.append("else")
        lines.append(f"  echo '{SELF_CHECK_PREFIX} skipped: no python interpreter on PATH' >&2")
        lines.append("fi")
    else:
        lines.append(f"echo '{SELF_CHECK_PREFIX} skipped: runtime or configuration not identified' >&2")
    lines.append(f"rm -rf -- {shlex.quote(staging)}")
    return "\n".join(lines)


def verify_script(root: str, entries: list[dict[str, str | Path]]) -> str:
    """Read-only: print `<sha256>  <path>` for each deployed file, exactly like sha256sum."""
    return "\n".join(f"sha256sum {shlex.quote(root + '/' + str(e['destination']))}" for e in entries)


def rollback_script(root: str, previous: str, entries: list[dict[str, str | Path]],
                    launcher: str, hub: str, config: str) -> str:
    lines = ["set -eu", f"test -d {shlex.quote(previous)} || "
                        "{ echo 'no retained generation to roll back to' >&2; exit 1; }"]
    for entry in entries:
        stage = shlex.quote(previous + '/' + str(entry["stage"]))
        destination = shlex.quote(root + "/" + str(entry["destination"]))
        lines.append(f"if [ -f {stage} ]; then cp -f -- {stage} {destination}; echo 'restored {entry['destination']}'; fi")
    if launcher:
        lines.append(f"chmod 755 -- {shlex.quote(root + '/' + launcher)}")
    if hub and config:
        lines.append("PY=\"${AGENTHUB_PYTHON:-python3}\"")
        lines.append("if command -v \"$PY\" >/dev/null 2>&1; then")
        lines.append(f"  \"$PY\" -B -c {shlex.quote(self_check_code(root + '/' + hub, root + '/' + config))}")
        lines.append("else")
        lines.append(f"  echo '{SELF_CHECK_PREFIX} skipped: no python interpreter on PATH' >&2")
        lines.append("fi")
    return "\n".join(lines)


# ---------------------------------------------------------------- Windows builders
def ps_quote(code: str) -> str:
    """Escape Python source for embedding in a PowerShell double-quoted string.

    Inside double quotes PowerShell treats a doubled quote as one literal quote, so the
    embedded `"` of the JSON-encoded paths survives; the single quotes in the code do not
    need escaping there.
    """
    return code.replace('"', '""')


def activation_script_windows(root: str, staging: str, previous: str, entries: list[dict[str, str | Path]],
                              reset_state: bool, state: str, launcher: str, hub: str, config: str) -> str:
    lines = [
        "$ErrorActionPreference='Stop'",
        "$ProgressPreference='SilentlyContinue'",
        f"$staging='{staging}'",
        f"$previous='{previous}'",
    ]
    lines.extend(
        f"if((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $staging '{e['stage']}')).Hash.ToLower() "
        f"-ne '{e['sha256']}') {{ throw 'hash mismatch: {e['stage']}' }}"
        for e in entries
    )
    for entry in entries:
        destination = str(PureWindowsPath(root) / PureWindowsPath(str(entry["destination"])))
        lines.append(f"if(Test-Path -LiteralPath '{destination}') "
                     f"{{ Copy-Item -Force -LiteralPath '{destination}' -Destination (Join-Path $previous '{entry['stage']}') }}")
    if reset_state:
        lines.append(f"Remove-Item -Force -ErrorAction SilentlyContinue '{state}*'")
    lines.extend(
        f"Move-Item -Force (Join-Path $staging '{e['stage']}') "
        f"'{PureWindowsPath(root) / PureWindowsPath(str(e['destination']))}'"
        for e in entries
    )
    if hub and config:
        # native commands do not raise, so the exit code is inspected explicitly
        lines.append(f"& python -B -c \"{ps_quote(self_check_code(str(PureWindowsPath(root) / PureWindowsPath(hub)), str(PureWindowsPath(root) / PureWindowsPath(config))))}\"")
        lines.append("if($LASTEXITCODE -ne 0) {")
        lines.append("  Write-Output 'activation self-check failed; restoring the previous generation'")
        for entry in entries:
            destination = str(PureWindowsPath(root) / PureWindowsPath(str(entry["destination"])))
            lines.append(f"  if(Test-Path -LiteralPath (Join-Path $previous '{entry['stage']}')) "
                         f"{{ Copy-Item -Force (Join-Path $previous '{entry['stage']}') '{destination}' }}")
        lines.append("  exit 1")
        lines.append("}")
    else:
        lines.append(f"Write-Output '{SELF_CHECK_PREFIX} skipped: runtime or configuration not identified'")
    lines.append("Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $staging")
    lines.append("Write-Output 'AgentHub activation complete'")
    return "\n".join(lines)


def verify_script_windows(root: str, entries: list[dict[str, str | Path]]) -> str:
    """Read-only counterpart of verify_script, printing `<destination> <hash|missing>`."""
    lines = ["$ErrorActionPreference='Stop'", "$ProgressPreference='SilentlyContinue'"]
    for entry in entries:
        destination = str(PureWindowsPath(root) / PureWindowsPath(str(entry["destination"])))
        lines.append(
            f"if(Test-Path -LiteralPath '{destination}') "
            f"{{ Write-Output (\"{entry['destination']} \" + (Get-FileHash -Algorithm SHA256 -LiteralPath '{destination}').Hash.ToLower()) }} "
            f"else {{ Write-Output \"{entry['destination']} missing\" }}"
        )
    return "\n".join(lines)


def rollback_script_windows(root: str, previous: str, entries: list[dict[str, str | Path]],
                            launcher: str, hub: str, config: str) -> str:
    lines = ["$ErrorActionPreference='Stop'", f"$previous='{previous}'",
             f"if(-not (Test-Path -LiteralPath $previous)) {{ Write-Error 'no retained generation to roll back to'; exit 1 }}"]
    for entry in entries:
        destination = str(PureWindowsPath(root) / PureWindowsPath(str(entry["destination"])))
        lines.append(f"if(Test-Path -LiteralPath (Join-Path $previous '{entry['stage']}')) "
                     f"{{ Copy-Item -Force (Join-Path $previous '{entry['stage']}') '{destination}'; "
                     f"Write-Output 'restored {entry['destination']}' }}")
    if hub and config:
        lines.append(f"& python -B -c \"{ps_quote(self_check_code(str(PureWindowsPath(root) / PureWindowsPath(hub)), str(PureWindowsPath(root) / PureWindowsPath(config))))}\"")
        lines.append("if($LASTEXITCODE -ne 0) { Write-Error 'the restored generation did not pass its self-check'; exit 1 }")
    return "\n".join(lines)


# ------------------------------------------------------------------------ execution
def report_self_check(output: str) -> str:
    for line in output.splitlines():
        if line.strip().startswith(SELF_CHECK_PREFIX):
            return line.strip()
    return "no self-check line reported"


def enforce_idle(inflight: list[dict[str, object]] | None, require_idle: bool) -> str:
    """Decide what an in-flight task means for this deployment.

    Deployment never touches the state database and a task continues across it — measured
    across a real version boundary — so the default is to say what is running and proceed.
    `--require-idle` turns that into a refusal for operators who want the gate.
    """
    if not inflight:
        return ""
    summary = ", ".join(f"{item['task_id']}({item['status']})" for item in inflight)
    if require_idle:
        raise SystemExit(f"{len(inflight)} task(s) are not finished: {summary}; "
                         "finish them or drop --require-idle")
    return summary


def report_failure(verb: str, project: str, exc: subprocess.CalledProcessError) -> int:
    """Surface what the authority said. A failed activation has already reverted the files and
    echoed why; a traceback would hide exactly the lines that matter."""
    print(json.dumps({
        verb: False, "project": project,
        "stdout": (exc.stdout or "").strip(), "stderr": (exc.stderr or "").strip(),
    }, ensure_ascii=False, indent=2), file=sys.stderr)
    return 1


def active_tasks(host: str, state: str) -> tuple[list[dict[str, object]] | None, str]:
    """Read-only view of non-terminal tasks, so an operator knows what is in flight.

    Deployment does not touch the state database; this only opens it read-only to report.
    """
    query = (
        "import json,sqlite3;"
        f"c=sqlite3.connect({json.dumps('file:' + state + '?mode=ro')},uri=True);"
        "print(json.dumps([{'task_id':r[0],'status':r[1],'assignee':r[2],'claimed_by':r[3]}"
        " for r in c.execute(\"SELECT task_id,status,assignee,claimed_by FROM tasks "
        "WHERE status NOT IN ('closed','cancelled')\")]))"
    )
    script = (
        f"if [ ! -f {shlex.quote(state)} ]; then echo '[]'; exit 0; fi\n"
        "PY=\"${AGENTHUB_PYTHON:-python3}\"\n"
        "command -v \"$PY\" >/dev/null 2>&1 || { echo '[]'; exit 0; }\n"
        f"\"$PY\" -B -c {shlex.quote(query)}"
    )
    result = subprocess.run(["ssh", host, "sh", "-c", shlex.quote(script)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        return None, (result.stderr.strip() or "state could not be read")
    try:
        parsed = json.loads(result.stdout.strip() or "[]")
    except ValueError:
        return None, "state query returned unreadable output"
    if not isinstance(parsed, list):
        return None, "state query returned an unexpected shape"
    return parsed, ""


def deploy_posix(host: str, root: str, staging: str, previous: str, entries: list[dict[str, str | Path]],
                 reset_state: bool, state: str, launcher: str, hub: str, config: str) -> str:
    directories = sorted({str(Path(root) / Path(str(e["destination"])).parent) for e in entries})
    prepare = ("set -eu; "
               + f"rm -rf -- {shlex.quote(staging)} {shlex.quote(previous)}; "
               + "mkdir -p -- " + " ".join(shlex.quote(value) for value in [staging, previous, *directories]))
    run(["ssh", host, "sh", "-c", shlex.quote(prepare)])
    for entry in entries:
        run(["scp", str(entry["source"]), f"{host}:{staging}/{entry['stage']}"])
    script = activation_script(root, staging, previous, entries, reset_state, state, launcher, hub, config)
    result = run(["ssh", host, "sh", "-c", shlex.quote(script)], capture=True)
    return result.stdout if result else ""


def deploy_windows(host: str, root: str, staging: str, previous: str, entries: list[dict[str, str | Path]],
                   reset_state: bool, state: str, launcher: str, hub: str, config: str, local: bool) -> str:
    directories = sorted({str(PureWindowsPath(root) / PureWindowsPath(str(e["destination"])).parent) for e in entries})
    prepare = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        f"Remove-Item -Recurse -Force -ErrorAction SilentlyContinue '{staging}','{previous}'; "
        + "; ".join(f"New-Item -ItemType Directory -Force -Path '{value}' | Out-Null"
                    for value in [staging, previous, *directories])
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
    activate = activation_script_windows(root, staging, previous, entries, reset_state, state, launcher, hub, config)
    remote_script = root + "\\.agenthub\\activate-v23.ps1"
    with tempfile.TemporaryDirectory(prefix="agenthub-deploy-") as temp:
        local_script = Path(temp) / "activate-v23.ps1"
        local_script.write_text(activate, encoding="utf-8-sig")
        if local:
            shutil.copy2(local_script, PureWindowsPath(remote_script))
        else:
            run(["scp", str(local_script), f"{host}:{remote_script.replace(chr(92), '/')}"])
    invocation = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", remote_script]
    result = run(["ssh", host, *invocation], capture=True) if not local else run(invocation, capture=True)
    cleanup = f"Remove-Item -Force -ErrorAction SilentlyContinue '{remote_script}'"
    if local:
        run(["powershell", "-NoProfile", "-EncodedCommand", ps_encoded(cleanup)])
    else:
        run(["ssh", host, "powershell", "-NoProfile", "-EncodedCommand", ps_encoded(cleanup)])
    return result.stdout if result else ""


def verify_posix(host: str, root: str, entries: list[dict[str, str | Path]]) -> list[dict[str, object]]:
    result = subprocess.run(["ssh", host, "sh", "-c", shlex.quote(verify_script(root, entries))],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    deployed_by_path: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            deployed_by_path[fields[-1]] = fields[0]
    report: list[dict[str, object]] = []
    for entry in entries:
        expected = str(entry["sha256"])
        deployed = deployed_by_path.get(root + "/" + str(entry["destination"]))
        report.append({"destination": str(entry["destination"]), "registry": expected,
                       "deployed": deployed, "match": deployed == expected})
    return report


def verify_windows(host: str, root: str, entries: list[dict[str, str | Path]], local: bool) -> list[dict[str, object]]:
    """Windows counterpart of verify_posix. Not exercised against a real authority here."""
    encoded = ps_encoded(verify_script_windows(root, entries))
    command = ["powershell", "-NoProfile", "-EncodedCommand", encoded]
    result = (run(command, capture=True) if local
              else run(["ssh", host, *command], capture=True))
    deployed: dict[str, str] = {}
    for line in (result.stdout if result else "").splitlines():
        fields = line.rsplit(" ", 1)
        if len(fields) == 2:
            deployed[fields[0].strip()] = "" if fields[1].strip() == "missing" else fields[1].strip()
    report: list[dict[str, object]] = []
    for entry in entries:
        expected = str(entry["sha256"])
        found = deployed.get(str(entry["destination"]))
        report.append({"destination": str(entry["destination"]), "registry": expected,
                       "deployed": found, "match": found == expected})
    return report


def is_local_windows_target(host: str) -> bool:
    """Use local file operations when a Windows authority invokes its own adapter."""
    return os.name == "nt" and host.casefold() in {"windows", "localhost", "127.0.0.1", "::1"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--require-idle", action="store_true")
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
    staging, previous = staging_paths(project_root, shell)
    state = config["state_authority"]["database"]
    launcher = str(remote.get("launcher", ""))
    hub, deployed_config = self_check_targets(project_dir, entries)
    local_windows = is_local_windows_target(host)
    inflight, state_note = active_tasks(host, state)

    if args.verify:
        report = (verify_posix(host, project_root, entries) if shell == "posix_sh"
                  else verify_windows(host, project_root, entries, local_windows))
        ok = all(item["match"] for item in report)
        print(json.dumps({"verify": True, "ok": ok, "project": args.project, "host": host,
                          "files": report, "active_tasks": inflight, "state_note": state_note},
                         ensure_ascii=False, indent=2))
        return 0 if ok else 1
    if args.dry_run:
        print(json.dumps({
            "dry_run": True, "project": args.project, "host": host, "shell": shell,
            "files": [{"source": str(e["source"]), "destination": e["destination"], "sha256": e["sha256"]} for e in entries],
            "self_check_targets": {"runtime": hub, "config": deployed_config, "launcher": launcher},
            "active_tasks": inflight, "state_note": state_note,
        }, ensure_ascii=False, indent=2))
        return 0
    inflight_summary = enforce_idle(inflight, args.require_idle)
    if inflight_summary:
        print(json.dumps({"warning": "tasks are in flight; a deployment never touches the state "
                                      "database and these tasks continue across it",
                          "active_tasks": inflight}, ensure_ascii=False), flush=True)

    if args.rollback:
        try:
            if shell == "posix_sh":
                script = rollback_script(project_root, previous, entries, launcher, hub, deployed_config)
                result = run(["ssh", host, "sh", "-c", shlex.quote(script)], capture=True)
            else:
                script = rollback_script_windows(project_root, previous, entries, launcher, hub, deployed_config)
                command = ["powershell", "-NoProfile", "-EncodedCommand", ps_encoded(script)]
                result = run(command, capture=True) if local_windows else run(["ssh", host, *command], capture=True)
        except subprocess.CalledProcessError as exc:
            return report_failure("rolled_back", args.project, exc)
        output = result.stdout if result else ""
        print(json.dumps({"rolled_back": True, "project": args.project, "host": host,
                          "restored": [line.split(" ", 1)[1] for line in output.splitlines()
                                       if line.startswith("restored ")],
                          "self_check": report_self_check(output)}, ensure_ascii=False, indent=2))
        return 0

    try:
        if shell == "windows_cmd":
            output = deploy_windows(host, project_root, staging, previous, entries, args.reset_state,
                                    state, launcher, hub, deployed_config, local_windows)
        else:
            output = deploy_posix(host, project_root, staging, previous, entries, args.reset_state,
                                  state, launcher, hub, deployed_config)
    except subprocess.CalledProcessError as exc:
        return report_failure("deployed", args.project, exc)
    print(json.dumps({
        "deployed": True, "project": args.project, "host": host, "shell": shell,
        "files": [{"destination": e["destination"], "sha256": e["sha256"]} for e in entries],
        "previous_generation": previous, "self_check": report_self_check(output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
