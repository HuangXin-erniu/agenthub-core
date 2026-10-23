#!/usr/bin/env python3
"""AgentHub v2: a small, decision-first multi-agent control plane.

The canonical runtime and SQLite state live on the project execution host.
Other devices are thin clients and proxy commands through SSH.

Reading task state:
  * `submissions[0].result` in `show` carries the submitted payload; `task.commit` is not set.
  * `worktree_path` is where that task's worktree lives and is kept as history after the task
    closes. The live state is the `worktree` block, which appears only while the directory
    exists ({head, dirty_entries}); its absence means the worktree is gone.
  * `close` removes the worktree recursively and `prune` reclaims the ones cancelled and
    superseded tasks leave behind.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import json
import math
import operator as operator_module
import os
import platform
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


VERSION = "2.4.2"
MAX_PACKET_BYTES = 16_384
ROOT = Path(__file__).resolve().parent
COMMANDS = {"init", "status", "context", "next", "show", "post", "claim", "claim-next", "handoff", "checkpoint", "submit", "heartbeat", "review", "close", "decision", "audit", "rpc", "cancel", "supersede", "release", "prune"}
TERMINAL_STATUSES = {"closed", "cancelled"}
CONCURRENCY_ONE = "one_active_task_per_executor"
CONCURRENCY_MULTI = "multiple_tasks_per_executor"
CONCURRENCY_MODES = {CONCURRENCY_ONE, CONCURRENCY_MULTI}
ISOLATION_MODES = {"git_worktree_per_task"}
SESSION_POLICIES = {"optional", "required"}
SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_ACTIVE_TASKS_CEILING = 32
MAX_LEASE_MINUTES = 10080
GATE_OPERATORS = {">=", ">", "<=", "<"}
GATE_COMPARE = {
    ">=": operator_module.ge, ">": operator_module.gt,
    "<=": operator_module.le, "<": operator_module.lt,
}
PERMISSIONS = {
    "administrator": {"init", "post", "review", "close", "handoff", "show", "decision", "audit", "status", "context", "cancel", "supersede", "release", "prune", "heartbeat"},
    "decider": {"post", "review", "show", "decision", "audit", "status", "context"},
    "executor": {"claim", "claim-next", "handoff", "checkpoint", "submit", "heartbeat", "show", "status", "context", "next", "release"},
}


class HubError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def session_value(raw: str | None) -> str | None:
    """A window identifier declared by the caller; it is workflow metadata, not a credential."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if not SESSION_PATTERN.fullmatch(value):
        raise HubError("session 必须是 1-64 位字母、数字、点、下划线或连字符，且以字母或数字开头")
    return value


def session_policy(config: dict[str, Any]) -> str:
    return config.get("execution", {}).get("session_policy", "optional")


def lease_seconds_argument(minutes: int | None) -> int:
    if minutes is None:
        return 0
    if type(minutes) is not int or not 0 <= minutes <= MAX_LEASE_MINUTES:
        raise HubError(f"lease_minutes 必须是 0..{MAX_LEASE_MINUTES} 的整数")
    return minutes * 60


def lease_state(row: sqlite3.Row) -> dict[str, Any]:
    seconds = row["lease_seconds"] if "lease_seconds" in row.keys() else 0
    last = row["last_heartbeat_at"] if "last_heartbeat_at" in row.keys() else None
    expires_at: str | None = None
    remaining: int | None = None
    expired = False
    if seconds and last:
        try:
            expiry = dt.datetime.fromisoformat(last) + dt.timedelta(seconds=seconds)
            expires_at = expiry.isoformat()
            remaining = int((expiry - dt.datetime.now(dt.timezone.utc)).total_seconds())
            expired = remaining <= 0
        except ValueError:
            expires_at = None
    return {
        "last_heartbeat_at": last,
        "lease_seconds": seconds,
        "expires_at": expires_at,
        "remaining_seconds": remaining,
        "expired": expired,
    }


def load_state_json(raw: str, label: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HubError(f"状态数据损坏 ({label}): {exc}") from exc


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_json_argument(raw: str, label: str) -> dict[str, Any]:
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > MAX_PACKET_BYTES:
        raise HubError(f"{label} 超过 {MAX_PACKET_BYTES} 字节；请只提交摘要和证据指针")
    try:
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise HubError(f"{label} 必须是 JSON 对象或 @文件路径: {exc}") from exc
    if not isinstance(value, dict):
        raise HubError(f"{label} 必须是 JSON 对象")
    return value


def expand_at_files(argv: list[str]) -> list[str]:
    expanded = list(argv)
    for flag in ("--spec", "--progress", "--result"):
        if flag in expanded:
            index = expanded.index(flag) + 1
            if index < len(expanded) and expanded[index].startswith("@"):
                expanded[index] = Path(expanded[index][1:]).read_text(encoding="utf-8")
    return expanded


def config_path(project: str | None, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    project = project or os.environ.get("AGENTHUB_PROJECT")
    if not project:
        candidates = list((ROOT / "projects").glob("*/project.json"))
        if len(candidates) == 1:
            return candidates[0]
        raise HubError("请使用 --project 指定项目")
    return ROOT / "projects" / project / "project.json"


def validate_execution_config(execution: dict[str, Any]) -> None:
    concurrency = execution.get("concurrency", CONCURRENCY_ONE)
    if concurrency not in CONCURRENCY_MODES:
        raise HubError(f"execution.concurrency 仅支持 {sorted(CONCURRENCY_MODES)}")
    isolation = execution.get("isolation", "git_worktree_per_task")
    if isolation not in ISOLATION_MODES:
        raise HubError(f"execution.isolation 仅支持 {sorted(ISOLATION_MODES)}")
    session_policy_value = execution.get("session_policy", "optional")
    if session_policy_value not in SESSION_POLICIES:
        raise HubError(f"execution.session_policy 仅支持 {sorted(SESSION_POLICIES)}")
    maximum = execution.get("max_active_tasks_per_executor")
    if concurrency == CONCURRENCY_MULTI:
        if maximum is None:
            raise HubError("启用多任务时必须声明 execution.max_active_tasks_per_executor")
        if type(maximum) is not int or not 1 <= maximum <= MAX_ACTIVE_TASKS_CEILING:
            raise HubError(f"execution.max_active_tasks_per_executor 必须是 1..{MAX_ACTIVE_TASKS_CEILING} 的整数")
    elif maximum is not None and (type(maximum) is not int or maximum != 1):
        raise HubError("one_active_task_per_executor 的 execution.max_active_tasks_per_executor 只能为 1")
    artifact_root = execution.get("artifact_root")
    if artifact_root is not None:
        if not isinstance(artifact_root, str) or not artifact_root.strip():
            raise HubError("execution.artifact_root 必须是非空字符串")
        worktree_root = execution.get("worktree_root")
        if worktree_root:
            artifacts = Path(artifact_root).expanduser().resolve()
            worktrees = Path(worktree_root).expanduser().resolve()
            if artifacts == worktrees or artifacts.is_relative_to(worktrees):
                raise HubError("execution.artifact_root 不能位于 execution.worktree_root 之内；close 与 prune 会删除该目录")


def project_task_limit(config: dict[str, Any]) -> int:
    execution = config.get("execution", {})
    if execution.get("concurrency", CONCURRENCY_ONE) == CONCURRENCY_ONE:
        return 1
    return execution["max_active_tasks_per_executor"]


def task_limit(config: dict[str, Any], agent: str) -> int:
    return config["agents"].get(agent, {}).get("max_active_tasks", project_task_limit(config))


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HubError(f"无法读取项目配置 {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise HubError("项目配置必须是 JSON 对象")
    required = {"schema_version", "project_id", "agents", "devices", "state_authority"}
    missing = sorted(required - config.keys())
    if missing:
        raise HubError("项目配置缺少字段: " + ", ".join(missing))
    for section in ("state_authority", "execution", "remote", "deployment", "task_naming"):
        if section in config and not isinstance(config[section], dict):
            raise HubError(f"项目配置的 {section} 必须是 JSON 对象")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise HubError("不支持的项目配置版本")
    workflow = config.get("workflow")
    if workflow is not None and workflow != "decision_first":
        raise HubError("workflow 仅支持 decision_first")
    project_id = config.get("project_id")
    if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", project_id):
        raise HubError("project_id 只能包含字母、数字、点、下划线和连字符")
    if not isinstance(config.get("devices"), dict) or not config["devices"]:
        raise HubError("项目必须声明至少一个设备")
    if not isinstance(config.get("agents"), dict) or not config["agents"]:
        raise HubError("项目必须声明至少一个 Agent")
    for agent_id, info in config["agents"].items():
        if not isinstance(info, dict) or not info.get("roles") or not info.get("devices"):
            raise HubError(f"Agent {agent_id} 必须声明 roles 和 devices")
        unknown_roles = sorted(set(info["roles"]) - set(PERMISSIONS))
        if unknown_roles:
            raise HubError(f"Agent {agent_id} 包含未知角色: {unknown_roles}")
        unknown_devices = sorted(set(info["devices"]) - set(config["devices"]))
        if unknown_devices:
            raise HubError(f"Agent {agent_id} 引用了未知设备: {unknown_devices}")
    authority = config.get("state_authority", {})
    if authority.get("host") not in config["devices"] or not authority.get("database"):
        raise HubError("state_authority 必须声明已注册 host 和 database")
    execution = config.get("execution", {})
    if not execution.get("repository") or not execution.get("worktree_root"):
        raise HubError("execution 必须声明 repository 和 worktree_root")
    validate_execution_config(execution)
    limit = project_task_limit(config)
    for agent_id, info in config["agents"].items():
        override = info.get("max_active_tasks")
        if override is None:
            continue
        if "executor" not in info["roles"]:
            raise HubError(f"Agent {agent_id} 不是执行者，不能声明 max_active_tasks")
        if type(override) is not int or not 1 <= override <= MAX_ACTIVE_TASKS_CEILING:
            raise HubError(f"Agent {agent_id} 的 max_active_tasks 必须是 1..{MAX_ACTIVE_TASKS_CEILING} 的整数")
        if override > limit:
            raise HubError(f"Agent {agent_id} 的 max_active_tasks 不能超过项目上限 {limit}")
    remote = config.get("remote", {})
    if remote.get("enabled"):
        if not remote.get("ssh_host") or not remote.get("project_root") or not remote.get("launcher"):
            raise HubError("远端项目必须声明 ssh_host、project_root 和 launcher")
        if remote.get("shell") not in {"windows_cmd", "posix_sh"}:
            raise HubError("remote.shell 仅允许 windows_cmd 或 posix_sh")
    naming = config.get("task_naming", {})
    if naming:
        task_pattern = naming.get("task_pattern")
        chain_pattern = naming.get("chain_pattern")
        seq_digits = naming.get("seq_digits")
        enum_description = naming.get("enum_description")
        phase_domains = naming.get("phase_domains", {})
        domain_types = naming.get("domain_types", {})
        if not isinstance(task_pattern, str) or not isinstance(chain_pattern, str):
            raise HubError("task_naming 必须包含 task_pattern 和 chain_pattern 字符串")
        if not isinstance(seq_digits, int) or seq_digits < 3 or seq_digits > 6:
            raise HubError("task_naming.seq_digits 必须是 3-6 的整数")
        if not isinstance(enum_description, dict) or not enum_description:
            raise HubError("task_naming.enum_description 必须是非空字典")
        try:
            task_re = re.compile(task_pattern)
            chain_re = re.compile(chain_pattern)
        except re.error as exc:
            raise HubError(f"task_naming 正则无效: {exc}") from exc
        if "seq" not in task_re.groupindex:
            raise HubError("task_pattern 必须包含命名捕获组 (?P<seq>...)")
        if "seq" not in chain_re.groupindex:
            raise HubError("chain_pattern 必须包含命名捕获组 (?P<seq>...)")
        if not isinstance(phase_domains, dict):
            raise HubError("task_naming.phase_domains 必须是字典")
        if not isinstance(domain_types, dict):
            raise HubError("task_naming.domain_types 必须是字典")
    deployment = config.get("deployment", {})
    expected_adapter = {"windows_cmd": "ssh_atomic_windows", "posix_sh": "ssh_atomic_posix"}.get(remote.get("shell"))
    if remote.get("enabled") and deployment.get("adapter") != expected_adapter:
        raise HubError(f"deployment.adapter 必须与 remote.shell 匹配: {expected_adapter}")
    managed = deployment.get("managed_files")
    if remote.get("enabled") and (not isinstance(managed, list) or not managed):
        raise HubError("远端项目必须声明 deployment.managed_files")
    return config


def command_suffix(argv: list[str]) -> list[str]:
    for index, value in enumerate(argv):
        if value in COMMANDS:
            return argv[index:]
    raise HubError("缺少命令")


def proxy_to_authority(config: dict[str, Any], device: str, argv: list[str], session: str | None = None) -> int:
    remote = config.get("remote") or {}
    host = remote.get("ssh_host")
    root = remote.get("project_root")
    launcher = remote.get("launcher", "agenthub.cmd")
    if not host or not root:
        raise HubError("项目未配置远端状态权威主机")
    payload = json_dump({
        "device": device, "session": session,
        "argv": command_suffix(expand_at_files(argv)),
    })
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    shell = remote.get("shell")
    if shell == "windows_cmd":
        remote_command = f'cmd /d /c "cd /d {root} && {launcher} rpc {encoded}"'
    elif shell == "posix_sh":
        remote_command = f"cd -- {shlex.quote(root)} && {shlex.quote('./' + launcher)} rpc {shlex.quote(encoded)}"
    else:
        raise HubError("项目未配置受支持的 remote.shell")
    ssh_options = remote.get("ssh_options", [
        "BatchMode=yes", "StrictHostKeyChecking=yes", "ConnectTimeout=10"
    ])
    ssh_command = ["ssh"]
    for option in ssh_options:
        ssh_command.extend(["-o", option])
    ssh_command.extend([host, remote_command])
    return subprocess.run(ssh_command, check=False).returncode


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            objective TEXT NOT NULL,
            instructions_json TEXT NOT NULL,
            acceptance_json TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 50,
            assignee TEXT NOT NULL,
            decision_key TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            branch TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            constraints_json TEXT NOT NULL,
            allowed_paths_json TEXT NOT NULL,
            forbidden_paths_json TEXT NOT NULL,
            validation_json TEXT NOT NULL,
            deliverables_json TEXT NOT NULL,
            evidence_requirements_json TEXT NOT NULL,
            blocked_protocol TEXT NOT NULL,
            context_budget INTEGER NOT NULL DEFAULT 6000,
            status TEXT NOT NULL,
            created_by TEXT NOT NULL,
            claimed_by TEXT,
            active_device TEXT,
            claim_session TEXT,
            last_heartbeat_at TEXT,
            lease_seconds INTEGER NOT NULL DEFAULT 0,
            claim_generation INTEGER NOT NULL DEFAULT 0,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS decisions (
            decision_key TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            rationale TEXT NOT NULL,
            decided_by TEXT NOT NULL,
            device TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS decision_revisions (
            revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_key TEXT NOT NULL,
            summary TEXT NOT NULL,
            rationale TEXT NOT NULL,
            decided_by TEXT NOT NULL,
            device TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            device TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            detail_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            device TEXT NOT NULL,
            summary TEXT NOT NULL,
            next_step TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id)
        );
        CREATE TABLE IF NOT EXISTS submissions (
            submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            device TEXT NOT NULL,
            outcome TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority, created_at);
        CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, checkpoint_id);
        CREATE INDEX IF NOT EXISTS idx_submissions_task ON submissions(task_id, submission_id);
        CREATE INDEX IF NOT EXISTS idx_decision_revisions_key ON decision_revisions(decision_key, revision_id);
        """
    )
    ensure_column(conn, "tasks", "active_device", "TEXT")
    ensure_column(conn, "tasks", "claim_session", "TEXT")
    ensure_column(conn, "tasks", "last_heartbeat_at", "TEXT")
    ensure_column(conn, "tasks", "lease_seconds", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "tasks", "claim_generation", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "checkpoints", "generation", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "tasks", "chain_id", "TEXT")
    ensure_column(conn, "tasks", "supersedes", "TEXT")
    ensure_column(conn, "tasks", "superseded_by", "TEXT")
    ensure_column(conn, "tasks", "cancelled_at", "TEXT")
    ensure_column(conn, "tasks", "cancelled_by", "TEXT")
    ensure_column(conn, "tasks", "cancel_reason", "TEXT")
    ensure_column(conn, "tasks", "depends_on_json", "TEXT NOT NULL DEFAULT '[]'")
    ensure_column(conn, "tasks", "gates_json", "TEXT NOT NULL DEFAULT '[]'")
    ensure_column(conn, "tasks", "artifacts_json", "TEXT NOT NULL DEFAULT '[]'")
    ensure_column(conn, "tasks", "allow_path_overlap", "INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_chain ON tasks(chain_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_decision ON tasks(decision_key)")
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version','4')")
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('agenthub_version',?)", (VERSION,))
    conn.commit()


def record(conn: sqlite3.Connection, agent: str, device: str, action: str, entity: str, detail: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO events(occurred_at,agent_id,device,action,entity_id,detail_json) VALUES(?,?,?,?,?,?)",
        (now(), agent, device, action, entity, json_dump(detail)),
    )


def identity(config: dict[str, Any], agent: str, device: str, action: str) -> dict[str, Any]:
    info = config["agents"].get(agent)
    if not info or not info.get("enabled", True):
        raise HubError(f"未启用的 Agent: {agent}")
    if device not in config["devices"]:
        raise HubError(f"未知设备: {device}")
    if device not in info.get("devices", []):
        raise HubError(f"Agent {agent} 不允许在设备 {device} 操作")
    roles = set(info.get("roles", []))
    allowed = set().union(*(PERMISSIONS.get(role, set()) for role in roles))
    if action not in allowed:
        raise HubError(f"Agent {agent} 的角色 {sorted(roles)} 无权执行 {action}")
    return info


def require_session(config: dict[str, Any], row: sqlite3.Row, session: str | None, action: str) -> None:
    """Fence an executor action to the window that owns the claim.

    An unbound claim is legacy state (claimed before sessions existed) or a claim made under
    `session_policy=optional`. `required` projects must bind it with `heartbeat` before any
    further mutation, so a different window cannot adopt it by accident.
    """
    bound = row["claim_session"]
    if bound:
        if session != bound:
            raise HubError(
                f"任务 {row['task_id']} 由会话 {bound} 持有；当前会话 {session or '(未声明)'} 不得 {action}"
            )
        return
    if session_policy(config) == "required":
        raise HubError(
            f"任务 {row['task_id']} 尚未绑定会话；请先在活动设备执行 "
            f"heartbeat {row['claimed_by'] or '<agent>'} {row['task_id']} --session <id>"
        )


def active_task_ids(conn: sqlite3.Connection, agent: str) -> list[str]:
    rows = conn.execute(
        "SELECT task_id FROM tasks WHERE claimed_by=? AND status='in_progress' "
        "ORDER BY priority, created_at, task_id", (agent,)
    ).fetchall()
    return [row["task_id"] for row in rows]


def ensure_capacity(conn: sqlite3.Connection, config: dict[str, Any], agent: str) -> None:
    limit = task_limit(config, agent)
    active = active_task_ids(conn, agent)
    if len(active) >= limit:
        raise HubError(
            f"执行者 {agent} 已达到并发上限 {limit}；当前任务: {', '.join(active)}；"
            "请先 submit、release 或调整配置"
        )


def required_text(value: dict[str, Any], field: str, max_length: int = 4000) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise HubError(f"缺少非空字段: {field}")
    result = result.strip()
    if len(result) > max_length:
        raise HubError(f"字段 {field} 超过 {max_length} 字符")
    return result


def required_list(value: dict[str, Any], field: str) -> list[Any]:
    result = value.get(field)
    if not isinstance(result, list) or not result:
        raise HubError(f"缺少非空数组: {field}")
    if len(result) > 20 or any(not isinstance(item, str) or not item.strip() for item in result):
        raise HubError(f"字段 {field} 必须由不超过 20 个非空字符串组成")
    return result


def optional_string_list(value: dict[str, Any], field: str, max_length: int = 500) -> list[str]:
    result = value.get(field, [])
    if result is None:
        return []
    if not isinstance(result, list):
        raise HubError(f"字段 {field} 必须是数组")
    if len(result) > 20:
        raise HubError(f"字段 {field} 最多 20 项")
    for item in result:
        if not isinstance(item, str) or not item.strip():
            raise HubError(f"字段 {field} 的每一项必须是非空字符串")
        if len(item) > max_length:
            raise HubError(f"字段 {field} 的单项超过 {max_length} 字符")
    return result


def finite_number(value: Any) -> bool:
    """True for a JSON number that can be compared without NaN/inf surprises."""
    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value)


def validate_depends_on(spec: dict[str, Any], task_id: str) -> list[str]:
    result = optional_string_list(spec, "depends_on", max_length=120)
    if len(set(result)) != len(result):
        raise HubError("depends_on 不能包含重复任务 ID")
    if task_id in result:
        raise HubError("depends_on 不能包含任务自身")
    return result


def validate_gates(spec: dict[str, Any]) -> list[dict[str, Any]]:
    raw = spec.get("gates", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HubError("gates 必须是数组")
    if len(raw) > 20:
        raise HubError("gates 最多 20 项")
    gates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HubError(f"gates[{index}] 必须是对象")
        unknown = sorted(set(item) - {"id", "metric", "operator", "threshold", "source"})
        if unknown:
            raise HubError(f"gates[{index}] 含未知字段: {unknown}")
        gate_id = item.get("id")
        if not isinstance(gate_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", gate_id):
            raise HubError(f"gates[{index}].id 必须是 1-64 位字母、数字、点、下划线或连字符")
        if gate_id in seen:
            raise HubError(f"gates id 重复: {gate_id}")
        seen.add(gate_id)
        metric = item.get("metric")
        if not isinstance(metric, str) or not metric.strip() or len(metric) > 200:
            raise HubError(f"gates[{gate_id}].metric 必须是 1-200 字符的非空字符串")
        operator_value = item.get("operator")
        if not isinstance(operator_value, str) or operator_value not in GATE_OPERATORS:
            raise HubError(f"gates[{gate_id}].operator 仅允许 {sorted(GATE_OPERATORS)}")
        threshold = item.get("threshold")
        if not finite_number(threshold):
            raise HubError(f"gates[{gate_id}].threshold 必须是有限数字")
        source = item.get("source")
        if source is not None and (not isinstance(source, str) or len(source) > 500):
            raise HubError(f"gates[{gate_id}].source 必须是不超过 500 字符的字符串")
        gate = {"id": gate_id, "metric": metric.strip(), "operator": operator_value, "threshold": threshold}
        if source is not None:
            gate["source"] = source
        gates.append(gate)
    return gates


def validate_artifacts(spec: dict[str, Any]) -> list[str]:
    raw = spec.get("artifacts", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HubError("artifacts 必须是数组")
    if len(raw) > 20:
        raise HubError("artifacts 最多 20 项")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item or len(item) > 500:
            raise HubError(f"artifacts[{index}] 必须是 1-500 字符的非空字符串")
        if "\\" in item or item.startswith("/") or re.match(r"^[A-Za-z]:", item):
            raise HubError(f"artifacts[{index}] 必须是 artifact_root 下的相对 POSIX 路径: {item}")
        if any(char in item for char in "*?["):
            raise HubError(f"artifacts[{index}] 不能包含通配符: {item}")
        pure = PurePosixPath(item)
        if pure.is_absolute() or not pure.parts or any(part == ".." for part in pure.parts):
            raise HubError(f"artifacts[{index}] 不能包含 ..: {item}")
        normalized = pure.as_posix()
        if normalized in seen:
            raise HubError(f"artifacts 重复: {normalized}")
        seen.add(normalized)
        result.append(normalized)
    return result


def validate_revision(value: str, label: str) -> str:
    """Reject values Git could interpret as an option before they reach a command line."""
    if not value or value.startswith("-") or any(char.isspace() or ord(char) < 32 for char in value):
        raise HubError(f"{label} 不是合法的 Git revision: {value!r}")
    return value


def validate_branch_name(value: str, label: str) -> str:
    result = subprocess.run(["git", "check-ref-format", "--branch", value], capture_output=True, check=False)
    if result.returncode:
        raise HubError(f"{label} 不是合法的分支名: {value!r}")
    return value


def git_argv(repository: str, *args: str) -> list[str]:
    """Git argv for one directory, with the ownership check pre-cleared for it.

    `safe.directory` accepts an exact path only (never a glob) and is evaluated against the
    directory git is told to operate on, so every call clears the one it is about to use.
    Without this, each command run inside a task worktree fails with "detected dubious
    ownership" wherever the checkout's owner differs from the running uid — an NTFS-3G mount
    with user_id=0, for instance — and nothing in the configuration can make the runtime trust
    that path. Note that clearing the main repository does not cover its worktrees.
    """
    trusted = str(Path(repository).expanduser().resolve())
    return ["git", "-c", f"safe.directory={trusted}", "-C", repository, *args]


def git(config: dict[str, Any], *args: str, cwd: str | None = None, check: bool = True) -> str:
    repository = cwd or config.get("execution", {}).get("repository")
    if not repository:
        raise HubError("项目未配置 execution.repository")
    result = subprocess.run(
        git_argv(repository, *args), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if check and result.returncode:
        raise HubError(f"Git 命令失败: {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def changed_paths(config: dict[str, Any], base: str, commit: str, cwd: str) -> list[str]:
    result = subprocess.run(
        git_argv(cwd, "diff", "--no-renames", "--name-only", "-z", f"{base}..{commit}"),
        capture_output=True, check=False,
    )
    if result.returncode:
        raise HubError("无法读取提交路径差异")
    try:
        raw = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HubError("提交包含无法可靠解码为 UTF-8 的路径") from exc
    if raw and not raw.endswith("\0"):
        raise HubError("Git 路径差异缺少 NUL 分隔符")
    return raw[:-1].split("\0") if raw else []


def git_ok(config: dict[str, Any], *args: str, cwd: str | None = None) -> bool:
    repository = cwd or config.get("execution", {}).get("repository")
    return subprocess.run(
        git_argv(repository, *args), capture_output=True, check=False
    ).returncode == 0


def glob_prefix_parts(pattern: str) -> list[str]:
    """Path segments before the first wildcard; used to compare two glob patterns."""
    parts: list[str] = []
    for part in pattern.removeprefix("./").split("/"):
        if any(char in part for char in "*?["):
            break
        parts.append(part)
    return parts


def path_patterns_overlap(left: str, right: str) -> bool:
    """Conservative test for whether two allowed_paths patterns can match one path.

    Literal patterns are tested with the same fnmatch semantics `submit` uses. When both
    contain wildcards, only the static prefix is compared; that can report two patterns as
    overlapping when they cannot, which is the safe direction for a concurrency guard.
    """
    left = left.removeprefix("./")
    right = right.removeprefix("./")
    if left == right:
        return True
    left_glob = any(char in left for char in "*?[")
    right_glob = any(char in right for char in "*?[")
    if not left_glob and not right_glob:
        return False
    if not left_glob and fnmatch.fnmatch(left, right):
        return True
    if not right_glob and fnmatch.fnmatch(right, left):
        return True
    left_parts = glob_prefix_parts(left)
    right_parts = glob_prefix_parts(right)
    shared = min(len(left_parts), len(right_parts))
    return left_parts[:shared] == right_parts[:shared]


def dependency_state(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    dependencies = load_state_json(row["depends_on_json"], f"tasks[{row['task_id']}].depends_on")
    items: list[dict[str, Any]] = []
    for task_id in dependencies:
        dependency = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        items.append({"task_id": task_id, "status": dependency["status"] if dependency else "missing"})
    unmet = [item for item in items if item["status"] != "closed"]
    return {"satisfied": not unmet, "items": items, "unmet": unmet}


def dependency_closure(conn: sqlite3.Connection, task_ids: list[str]) -> set[str]:
    """Every task reachable through depends_on, including the starting IDs."""
    seen: set[str] = set()
    pending = list(task_ids)
    while pending:
        task_id = pending.pop()
        if task_id in seen:
            continue
        seen.add(task_id)
        row = conn.execute("SELECT depends_on_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row:
            pending.extend(load_state_json(row["depends_on_json"], f"tasks[{task_id}].depends_on"))
    return seen


def dependency_freshness(conn: sqlite3.Connection, config: dict[str, Any], row: sqlite3.Row,
                         state: dict[str, Any]) -> list[dict[str, Any]]:
    """Report closed dependencies whose merge commit is not contained in the task base."""
    stale: list[dict[str, Any]] = []
    for item in state["items"]:
        if item["status"] != "closed":
            continue
        event = conn.execute(
            "SELECT detail_json FROM events WHERE entity_id=? AND action='task.close' "
            "ORDER BY event_id DESC LIMIT 1", (item["task_id"],),
        ).fetchone()
        if not event:
            continue
        detail = load_state_json(event["detail_json"], f"events[{item['task_id']}].close.detail")
        merge_commit = detail.get("merge_commit")
        if not isinstance(merge_commit, str) or not merge_commit:
            continue
        if not git_ok(config, "merge-base", "--is-ancestor", merge_commit, row["base_commit"]):
            stale.append({"task_id": item["task_id"], "merge_commit": merge_commit})
    return stale


def dependents_of(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT task_id,status,depends_on_json FROM tasks "
        "WHERE status NOT IN ('closed','cancelled') AND task_id != ?", (task_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        dependencies = load_state_json(row["depends_on_json"], f"tasks[{row['task_id']}].depends_on")
        if task_id in dependencies:
            result.append({"task_id": row["task_id"], "status": row["status"]})
    return result


def evaluate_gates(gates: list[dict[str, Any]], results: Any, require_all: bool) -> list[dict[str, Any]]:
    if not gates:
        if results:
            raise HubError("任务合同未声明 gates，但结果包含了 gate_results")
        return []
    if results is None:
        if require_all:
            raise HubError("任务声明了 gates，completed 提交必须提供 gate_results")
        return []
    if not isinstance(results, dict):
        raise HubError("gate_results 必须是对象")
    declared = {gate["id"]: gate for gate in gates}
    unknown = sorted(set(results) - set(declared))
    if unknown:
        raise HubError(f"gate_results 含未声明的门禁: {unknown}")
    missing = sorted(set(declared) - set(results))
    if require_all and missing:
        raise HubError(f"gate_results 缺少门禁: {missing}")
    evaluated: list[dict[str, Any]] = []
    for gate in gates:
        if gate["id"] not in results:
            continue
        value = results[gate["id"]]
        if not finite_number(value):
            raise HubError(f"gate_results[{gate['id']}] 必须是有限数字")
        evaluated.append({**gate, "value": value,
                          "passed": bool(GATE_COMPARE[gate["operator"]](value, gate["threshold"]))})
    return evaluated


def verify_artifacts(config: dict[str, Any], artifacts: list[str], label: str) -> None:
    if not artifacts:
        return
    root_value = config.get("execution", {}).get("artifact_root")
    if not root_value:
        raise HubError(f"{label}: 项目未配置 execution.artifact_root，无法校验 artifacts")
    root = Path(root_value).expanduser().resolve()
    missing: list[str] = []
    escaped: list[str] = []
    for relative in artifacts:
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            escaped.append(relative)
        elif not candidate.exists():
            missing.append(relative)
    if escaped or missing:
        parts = []
        if missing:
            parts.append(f"缺失={missing}")
        if escaped:
            parts.append(f"越界={escaped}")
        raise HubError(f"{label}: 交付物校验失败（artifact_root={root}）；" + "；".join(parts))


def repository_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "head": git(config, "rev-parse", "HEAD"),
        "branch": git(config, "branch", "--show-current", check=False),
        "dirty_entries": len(git(config, "status", "--porcelain").splitlines()),
    }


def latest_checkpoint(conn: sqlite3.Connection, task_id: str, generation: int | None = None) -> dict[str, Any] | None:
    statement = "SELECT * FROM checkpoints WHERE task_id=?"
    params: list[Any] = [task_id]
    if generation is not None:
        statement += " AND generation=?"
        params.append(generation)
    row = conn.execute(statement + " ORDER BY checkpoint_id DESC LIMIT 1", params).fetchone()
    if not row:
        return None
    return {
        "summary": row["summary"], "next_step": row["next_step"],
        "evidence": load_state_json(row["evidence_json"], f"checkpoints[{row['checkpoint_id']}].evidence"),
        "device": row["device"], "generation": row["generation"], "created_at": row["created_at"],
    }


def previous_checkpoint(conn: sqlite3.Connection, task_id: str, generation: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM checkpoints WHERE task_id=? AND generation!=? ORDER BY checkpoint_id DESC LIMIT 1",
        (task_id, generation),
    ).fetchone()
    if not row:
        return None
    return {
        "summary": row["summary"], "next_step": row["next_step"],
        "evidence": load_state_json(row["evidence_json"], f"checkpoints[{row['checkpoint_id']}].evidence"),
        "device": row["device"], "generation": row["generation"], "created_at": row["created_at"],
        "note": "上一轮 claim 的 checkpoint，不是本轮进度",
    }


def latest_review(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT action,detail_json,occurred_at,agent_id FROM events WHERE entity_id=? AND (action='task.review.approve' OR action='task.review.rework') ORDER BY event_id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    detail = load_state_json(row["detail_json"], f"events[{task_id}].review.detail")
    return {"action": "approve" if "approve" in row["action"] else "rework", "note": detail.get("note", ""), "agent": row["agent_id"], "occurred_at": row["occurred_at"]}

def task_summary(row: sqlite3.Row) -> dict[str, Any]:
    lease = lease_state(row)
    summary = {
        "id": row["task_id"], "title": row["title"], "priority": row["priority"],
        "status": row["status"], "assignee": row["assignee"],
        "decision_key": row["decision_key"], "claimed_by": row["claimed_by"],
        "active_device": row["active_device"], "claim_session": row["claim_session"],
        "claim_generation": row["claim_generation"], "lease_expired": lease["expired"],
        "lease": lease, "chain_id": row["chain_id"],
        "depends_on": load_state_json(row["depends_on_json"], f"tasks[{row['task_id']}].depends_on"),
        "supersedes": row["supersedes"], "superseded_by": row["superseded_by"],
        "branch": row["branch"], "updated_at": row["updated_at"],
    }
    if row["result_json"]:
        result = load_state_json(row["result_json"], f"tasks[{row['task_id']}].result")
        computed = result.get("gates") if isinstance(result, dict) else None
        if isinstance(computed, list) and computed:
            passed = sum(1 for gate in computed if isinstance(gate, dict) and gate.get("passed") is True)
            summary["gate_status"] = {"total": len(computed), "passed": passed}
    return summary


def task_packet(conn: sqlite3.Connection, row: sqlite3.Row, config: dict[str, Any], session: str | None = None) -> dict[str, Any]:
    generation = row["claim_generation"]
    packet = task_summary(row) | {
        "objective": row["objective"], "scope": load_state_json(row["scope_json"], f"tasks[{row['task_id']}].scope"),
        "instructions": load_state_json(row["instructions_json"], f"tasks[{row['task_id']}].instructions"),
        "constraints": load_state_json(row["constraints_json"], f"tasks[{row['task_id']}].constraints"),
        "allowed_paths": load_state_json(row["allowed_paths_json"], f"tasks[{row['task_id']}].allowed_paths"),
        "forbidden_paths": load_state_json(row["forbidden_paths_json"], f"tasks[{row['task_id']}].forbidden_paths"),
        "acceptance": load_state_json(row["acceptance_json"], f"tasks[{row['task_id']}].acceptance"),
        "validation": load_state_json(row["validation_json"], f"tasks[{row['task_id']}].validation"),
        "deliverables": load_state_json(row["deliverables_json"], f"tasks[{row['task_id']}].deliverables"),
        "evidence_requirements": load_state_json(row["evidence_requirements_json"], f"tasks[{row['task_id']}].evidence_requirements"),
        "blocked_protocol": row["blocked_protocol"],
        "base_commit": row["base_commit"], "worktree_path": row["worktree_path"],
        "context_budget": row["context_budget"],
        "gates": load_state_json(row["gates_json"], f"tasks[{row['task_id']}].gates"),
        "artifacts": load_state_json(row["artifacts_json"], f"tasks[{row['task_id']}].artifacts"),
        "allow_path_overlap": bool(row["allow_path_overlap"]),
        "dependents": dependents_of(conn, row["task_id"]),
        "ownership": {
            "session": row["claim_session"],
            "mine": bool(session and row["claim_session"] == session),
            "session_policy": session_policy(config),
        },
        "checkpoint": latest_checkpoint(conn, row["task_id"], generation),
        "latest_review": latest_review(conn, row["task_id"]),
    }
    dependencies = dependency_state(conn, row)
    dependencies["stale_base"] = dependency_freshness(conn, config, row, dependencies)
    packet["dependencies"] = dependencies
    if config.get("execution", {}).get("artifact_root"):
        packet["artifact_root"] = config["execution"]["artifact_root"]
    older = previous_checkpoint(conn, row["task_id"], generation)
    if older:
        packet["previous_checkpoint"] = older
    if row["result_json"]:
        packet["result"] = load_state_json(row["result_json"], f"tasks[{row['task_id']}].result")
    if Path(row["worktree_path"]).exists():
        try:
            packet["worktree"] = {
                "head": git(config, "rev-parse", "HEAD", cwd=row["worktree_path"]),
                "dirty_entries": len(git(config, "status", "--porcelain", cwd=row["worktree_path"]).splitlines()),
            }
        except (HubError, OSError) as exc:
            packet["worktree"] = {"error": str(exc)}
    return packet


def submission_history(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT submission_id,agent_id,device,outcome,result_json,created_at FROM submissions "
        "WHERE task_id=? ORDER BY submission_id DESC", (task_id,),
    ).fetchall()
    return [
        {
            "submission_id": row["submission_id"], "agent": row["agent_id"],
            "device": row["device"], "outcome": row["outcome"],
            "result": load_state_json(row["result_json"], f"submissions[{row['submission_id']}].result"), "created_at": row["created_at"],
        }
        for row in rows
    ]


def do_status(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, all_tasks: bool,
              as_json: bool, mine: bool = False, assignee: str | None = None,
              decision: str | None = None) -> None:
    identity(config, agent, device, "status")
    target = agent if mine else assignee
    if assignee is not None and assignee not in config["agents"]:
        raise HubError(f"未知 Agent: {assignee}")
    if decision is not None and not conn.execute(
        "SELECT 1 FROM decisions WHERE decision_key=?", (decision,)
    ).fetchone():
        raise HubError(f"未知决策: {decision}")
    clauses: list[str] = []
    params: list[Any] = []
    if not all_tasks:
        clauses.append("status NOT IN ('closed','cancelled')")
    if target:
        clauses.append("assignee=?")
        params.append(target)
    if decision is not None:
        clauses.append("decision_key=?")
        params.append(decision)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM tasks {where} ORDER BY priority ASC, created_at ASC, task_id ASC", params
    ).fetchall()
    decisions = [dict(row) for row in conn.execute(
        "SELECT decision_key,summary,decided_by,updated_at FROM decisions ORDER BY updated_at DESC LIMIT 10"
    )]
    tasks: list[dict[str, Any]] = []
    for row in rows:
        summary = task_summary(row)
        state = dependency_state(conn, row)
        summary["dependency"] = {"satisfied": state["satisfied"], "unmet": state["unmet"]}
        tasks.append(summary)
    result = {
        "project": config["project_id"], "version": VERSION,
        "repository": repository_snapshot(config),
        "filter": {"assignee": target, "decision": decision},
        "tasks": tasks, "decisions": decisions,
    }
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"AgentHub {VERSION} | {config['project_id']} | 活动任务 {len(rows)}")
    for row, summary in zip(rows, tasks):
        session = f" session={row['claim_session']}" if row["claim_session"] else ""
        lease = " lease=expired" if lease_state(row)["expired"] else ""
        chain = f" chain={summary['chain_id']}" if summary["chain_id"] else ""
        decision_value = f" decision={summary['decision_key']}"
        dependencies = f" deps={','.join(summary['depends_on'])}" if summary["depends_on"] else ""
        unmet = summary["dependency"]["unmet"]
        blocked = f" blocked_by={','.join(item['task_id'] for item in unmet)}" if unmet else ""
        gates = ""
        if "gate_status" in summary:
            gates = f" gates={summary['gate_status']['passed']}/{summary['gate_status']['total']}"
        print(
            f"- [{row['status']}] {row['task_id']} {row['title']} (P{row['priority']}){chain}{decision_value}"
            f"{dependencies}{blocked}{gates} -> {row['claimed_by'] or '未认领'}{session}{lease}"
        )
    if decisions:
        print("当前决策:")
        for item in decisions:
            print(f"- {item['decision_key']}: {item['summary']}")


def executor_task_row(
    conn: sqlite3.Connection, config: dict[str, Any], agent: str, session: str | None, available: bool = False,
) -> tuple[sqlite3.Row | None, list[dict[str, Any]]]:
    """Pick one task for a window without ever handing it another window's claim.

    With a session the window first sees the tasks that session owns, then unclaimed ready work.
    Without one, `optional` projects keep the pre-2.4.1 precedence for legacy unbound claims but
    skip every session-bound task; `required` projects only expose ready work. The skipped rows
    are returned so the caller can explain why an in-progress task is not shown.
    """
    def dependency_exclusion(candidate: sqlite3.Row, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": candidate["task_id"], "session": candidate["claim_session"],
            "status": candidate["status"], "reason": "dependencies_unmet",
            "dependencies": state["unmet"],
        }

    if available:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE assignee=? AND status='ready' AND claimed_by IS NULL "
            "ORDER BY priority,created_at,task_id",
            (agent,),
        ).fetchall()
        excluded: list[dict[str, Any]] = []
        for row in rows:
            state = dependency_state(conn, row)
            if not state["satisfied"]:
                excluded.append(dependency_exclusion(row, state))
                continue
            return row, excluded[:10]
        return None, excluded[:10]
    if session:
        row = conn.execute(
            "SELECT * FROM tasks WHERE assignee=? AND claim_session=? "
            "AND status IN ('in_progress','blocked','review') "
            "ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,"
            "priority,created_at,task_id LIMIT 1",
            (agent, session),
        ).fetchone()
        if row:
            return row, []
        rows = conn.execute(
            "SELECT * FROM tasks WHERE assignee=? AND status='ready' AND claimed_by IS NULL "
            "ORDER BY priority,created_at,task_id",
            (agent,),
        ).fetchall()
        excluded = []
        for row in rows:
            state = dependency_state(conn, row)
            if not state["satisfied"]:
                excluded.append(dependency_exclusion(row, state))
                continue
            return row, excluded[:10]
        return None, excluded[:10]
    rows = conn.execute(
        "SELECT * FROM tasks WHERE assignee=? AND status IN ('in_progress','blocked','review','ready') "
        "AND status != 'cancelled' "
        "ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1 WHEN 'review' THEN 2 ELSE 3 END,"
        "priority,created_at,task_id",
        (agent,),
    ).fetchall()
    excluded = []
    required = session_policy(config) == "required"
    for row in rows:
        if row["claim_session"]:
            excluded.append({
                "task_id": row["task_id"], "session": row["claim_session"],
                "status": row["status"], "reason": "session_owned",
            })
            continue
        if required and row["status"] != "ready":
            excluded.append({
                "task_id": row["task_id"], "session": None,
                "status": row["status"], "reason": "session_required",
            })
            continue
        if row["status"] == "ready":
            state = dependency_state(conn, row)
            if not state["satisfied"]:
                excluded.append(dependency_exclusion(row, state))
                continue
        return row, excluded[:10]
    return None, excluded[:10]


def do_context(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, session: str | None) -> None:
    info = identity(config, agent, device, "context")
    roles = set(info["roles"])
    is_executor = "executor" in roles
    limit = task_limit(config, agent) if is_executor else None
    active = len(active_task_ids(conn, agent)) if is_executor else None
    excluded: list[dict[str, Any]] = []
    if is_executor:
        row, excluded = executor_task_row(conn, config, agent, session)
        tasks = [task_packet(conn, row, config, session)] if row else []
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('ready','in_progress','blocked','review','approved') AND status != 'cancelled' ORDER BY priority,created_at,task_id"
        ).fetchmany(25)
        tasks = [task_summary(row) for row in rows]
    decisions = [dict(row) for row in conn.execute(
        "SELECT decision_key,summary,rationale,decided_by,updated_at FROM decisions ORDER BY updated_at DESC LIMIT 10"
    )]
    if is_executor and limit == 1:
        rule = "决策者发布不可变执行合同并复核；执行者一次只推进一个活动任务；换设备必须 checkpoint 后 handoff。"
    elif is_executor:
        rule = f"决策者发布不可变执行合同并复核；执行者最多同时推进 {limit} 个活动任务，用 next --session <id> --available 或 claim-next 领取新任务；换设备必须 checkpoint 后 handoff。"
    else:
        rule = "决策者发布不可变执行合同并复核；执行者按项目并发上限推进任务；换设备必须 checkpoint 后 handoff。"
    payload: dict[str, Any] = {
        "project": config["project_id"], "agent": agent, "roles": info["roles"], "device": device,
        "session": session, "session_policy": session_policy(config),
        "repository": repository_snapshot(config),
        "operating_rule": rule,
        "decisions": decisions, "tasks": tasks, "excluded": excluded,
        "detail_rule": "blocked/review 必须 show 后决策；approved 等待用户确认合并，再由 Codex close。",
    }
    if is_executor:
        payload["concurrency"] = {"active": active, "limit": limit}
        if session_policy(config) == "required" and not session:
            payload["session_required"] = True
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def do_next(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, session: str | None, available: bool = False) -> None:
    identity(config, agent, device, "next")
    row, excluded = executor_task_row(conn, config, agent, session, available)
    packet = task_packet(conn, row, config, session) if row else None
    if packet:
        packet["device_access"] = (
            "active" if not row["active_device"] or row["active_device"] == device
            else f"handoff_required_from:{row['active_device']}"
        )
    payload = {
        "task": packet,
        "concurrency": {"active": len(active_task_ids(conn, agent)), "limit": task_limit(config, agent)},
        "session": session, "session_policy": session_policy(config), "excluded": excluded,
    }
    if session_policy(config) == "required" and not session:
        payload["session_required"] = True
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def do_show(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
            session: str | None = None) -> None:
    info = identity(config, agent, device, "show")
    row = load_task(conn, task_id)
    if "executor" in set(info.get("roles", [])) and row["assignee"] != agent:
        raise HubError("执行者只能查看分配给自己的任务")
    print(json.dumps({
        "task": task_packet(conn, row, config, session),
        "submissions": submission_history(conn, task_id),
    }, ensure_ascii=False, indent=2))


TASK_LIST_FIELDS = (
    "instructions", "scope", "constraints", "allowed_paths", "forbidden_paths",
    "acceptance", "validation", "deliverables", "evidence_requirements",
)


def task_worktree_path(config: dict[str, Any], task_id: str) -> str:
    """Resolve a task's worktree without letting the task ID escape worktree_root."""
    root_value = config.get("execution", {}).get("worktree_root")
    if not root_value:
        raise HubError("项目未配置 execution.worktree_root")
    if task_id in {".", ".."} or task_id.startswith(".") or any(char in task_id for char in "/\\\0"):
        raise HubError(f"任务 id 必须是单一目录名，不能包含路径分隔符或前导点: {task_id!r}")
    root = Path(root_value).expanduser().resolve()
    candidate = (root / task_id).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise HubError(f"任务 worktree 路径越界: {candidate} 不在 {root} 之下")
    return str(candidate)


def validate_task_contract(config: dict[str, Any], raw: str) -> dict[str, Any]:
    spec = read_json_argument(raw, "任务包")
    task_id = required_text(spec, "id", 120)
    chain_id = spec.get("chain_id", "")
    if not isinstance(chain_id, str):
        raise HubError("chain_id 必须是字符串")
    title = required_text(spec, "title", 200)
    objective = required_text(spec, "objective")
    assignee = required_text(spec, "assignee", 80)
    assignee_info = config["agents"].get(assignee, {})
    if "executor" not in assignee_info.get("roles", []) or not assignee_info.get("enabled", True):
        raise HubError(f"assignee 必须是启用的执行者: {assignee}")
    decision_key = required_text(spec, "decision_key", 160)
    base_commit = required_text(spec, "base_commit", 80)
    validate_revision(base_commit, "base_commit")
    instructions = required_list(spec, "instructions")
    scope = required_list(spec, "scope")
    constraints = required_list(spec, "constraints")
    allowed_paths = required_list(spec, "allowed_paths")
    forbidden_paths = required_list(spec, "forbidden_paths")
    acceptance = required_list(spec, "acceptance")
    validation = required_list(spec, "validation")
    deliverables = required_list(spec, "deliverables")
    evidence_requirements = required_list(spec, "evidence_requirements")
    blocked_protocol = required_text(spec, "blocked_protocol", 1000)
    depends_on = validate_depends_on(spec, task_id)
    gates = validate_gates(spec)
    artifacts = validate_artifacts(spec)
    if artifacts and not config.get("execution", {}).get("artifact_root"):
        raise HubError("任务声明了 artifacts，但项目未配置 execution.artifact_root")
    allow_path_overlap = spec.get("allow_path_overlap", False)
    if not isinstance(allow_path_overlap, bool):
        raise HubError("allow_path_overlap 必须是布尔值")
    priority = spec.get("priority", 50)
    if type(priority) is not int or not 1 <= priority <= 99:
        raise HubError("priority 必须是 1..99 的整数")
    context_budget = spec.get("context_budget", 6000)
    if type(context_budget) is not int or not 2000 <= context_budget <= 12000:
        raise HubError("context_budget 必须是 2000..12000 的整数")
    if len(json_dump(spec).encode("utf-8")) > context_budget:
        raise HubError("任务合同超过 context_budget；请压缩描述并只保留必要执行信息")
    naming = config.get("task_naming", {})
    if naming:
        if not re.fullmatch(naming["task_pattern"], task_id):
            raise HubError(f"任务 id 不符合项目命名规则 {naming['task_pattern']}: {task_id}")
        if not chain_id:
            raise HubError("任务必须指定 chain_id")
        if not re.fullmatch(naming["chain_pattern"], chain_id):
            raise HubError(f"chain_id 不符合项目命名规则 {naming['chain_pattern']}: {chain_id}")
        m = re.match(naming["task_pattern"], task_id)
        if m:
            pd = naming.get("phase_domains", {})
            dt = naming.get("domain_types", {})
            gd = m.groupdict()
            if "phase" in gd and pd:
                phase = m.group("phase")
                if phase not in pd:
                    raise HubError(f"阶段 {phase} 不在允许列表 {sorted(pd.keys())} 中")
            if "domain" in gd and pd and "phase" in m.groupdict():
                domain = m.group("domain")
                p = m.group("phase")
                if domain not in pd.get(p, []):
                    raise HubError(f"领域 {domain} 不在阶段 {p} 的允许列表中: {pd.get(p, [])}")
            if "type" in gd and dt and "domain" in m.groupdict():
                ttype = m.group("type")
                d = m.group("domain")
                if ttype not in dt.get(d, []):
                    raise HubError(f"类型 {ttype} 不在领域 {d} 的允许列表中: {dt.get(d, [])}")
    else:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", task_id):
            raise HubError("任务 id 只能包含字母、数字、点、下划线和连字符")
    branch = f"agenthub/{task_id}"
    worktree_path = task_worktree_path(config, task_id)
    return {
        **spec, "id": task_id, "chain_id": chain_id, "title": title, "objective": objective,
        "assignee": assignee, "decision_key": decision_key, "base_commit": base_commit,
        "instructions": instructions, "scope": scope, "constraints": constraints,
        "allowed_paths": allowed_paths, "forbidden_paths": forbidden_paths,
        "acceptance": acceptance, "validation": validation, "deliverables": deliverables,
        "evidence_requirements": evidence_requirements, "blocked_protocol": blocked_protocol,
        "depends_on": depends_on, "gates": gates, "artifacts": artifacts,
        "allow_path_overlap": allow_path_overlap,
        "priority": priority, "context_budget": context_budget,
        "branch": branch, "worktree_path": worktree_path,
    }


def check_task_constraints(conn: sqlite3.Connection, config: dict[str, Any], spec: dict[str, Any],
                           replacing: str | None = None) -> list[dict[str, Any]]:
    task_id, chain_id = spec["id"], spec["chain_id"]
    if conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
        raise HubError(f"任务 ID 已存在: {task_id}")
    naming = config.get("task_naming", {})
    if naming:
        conflict = conn.execute(
            "SELECT 1 FROM tasks WHERE chain_id=? AND status NOT IN ('closed','cancelled') AND task_id != ?",
            (chain_id, replacing or ""),
        ).fetchone()
        if conflict:
            raise HubError(f"active chain_id 已存在: {chain_id}")
        candidate = int(re.fullmatch(naming["task_pattern"], task_id).group("seq"))
        existing_seqs = set()
        for row in conn.execute("SELECT task_id FROM tasks"):
            match = re.fullmatch(naming["task_pattern"], row["task_id"])
            if match:
                existing_seqs.add(int(match.group("seq")))
        if candidate in existing_seqs:
            raise HubError(f"序号 {candidate:0{naming['seq_digits']}d} 已被占用（含已取消任务）")
        expected = max(existing_seqs) + 1 if existing_seqs else 1
        if candidate != expected:
            raise HubError(f"下一可用序号应为 {expected:0{naming['seq_digits']}d}，但收到 {candidate:0{naming['seq_digits']}d}")
    if not conn.execute("SELECT 1 FROM decisions WHERE decision_key=?", (spec["decision_key"],)).fetchone():
        raise HubError(f"任务必须关联已存在的决策: {spec['decision_key']}")
    if replacing and replacing in spec["depends_on"]:
        raise HubError(f"新合同不能依赖正在被替代的任务 {replacing}")
    for dependency in spec["depends_on"]:
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (dependency,)).fetchone()
        if not row:
            raise HubError(f"depends_on 引用了不存在的任务: {dependency}")
        if row["status"] == "cancelled":
            raise HubError(f"depends_on 引用已取消的任务: {dependency}")
    git(config, "rev-parse", "--verify", f"{spec['base_commit']}^{{commit}}")
    closure = dependency_closure(conn, spec["depends_on"])
    active = conn.execute(
        "SELECT task_id,chain_id,allowed_paths_json FROM tasks "
        "WHERE status NOT IN ('closed','cancelled') AND task_id != ?",
        (replacing or "",),
    ).fetchall()
    acknowledgements: list[dict[str, Any]] = []
    for other in active:
        if other["task_id"] in closure:
            continue
        if spec["chain_id"] and other["chain_id"] == spec["chain_id"]:
            continue
        other_paths = load_state_json(other["allowed_paths_json"], f"tasks[{other['task_id']}].allowed_paths")
        overlaps = [
            {"path": left, "other_path": right}
            for left in spec["allowed_paths"]
            for right in other_paths
            if path_patterns_overlap(left, right)
        ]
        if not overlaps:
            continue
        if not spec["allow_path_overlap"]:
            detail = ", ".join(f"{item['path']} ↔ {item['other_path']}" for item in overlaps[:3])
            raise HubError(
                f"任务 {spec['id']} 的 allowed_paths 与活动任务 {other['task_id']} 重叠: {detail}；"
                "如需并发请声明 depends_on，或在合同中显式设置 allow_path_overlap: true"
            )
        acknowledgements.append({"task_id": other["task_id"], "overlaps": overlaps})
    return acknowledgements


def insert_task(conn: sqlite3.Connection, config: dict[str, Any], agent: str, spec: dict[str, Any],
                stamp: str, supersedes: str | None = None) -> list[dict[str, Any]]:
    acknowledgements = check_task_constraints(conn, config, spec, replacing=supersedes)
    values = {
        "task_id": spec["id"], "title": spec["title"], "objective": spec["objective"],
        "priority": spec["priority"], "assignee": spec["assignee"],
        "decision_key": spec["decision_key"], "base_commit": spec["base_commit"],
        "branch": spec["branch"], "worktree_path": spec["worktree_path"],
        "blocked_protocol": spec["blocked_protocol"], "context_budget": spec["context_budget"],
        "status": "ready", "created_by": agent, "created_at": stamp, "updated_at": stamp,
        "chain_id": spec["chain_id"], "supersedes": supersedes,
        "depends_on_json": json_dump(spec["depends_on"]),
        "gates_json": json_dump(spec["gates"]),
        "artifacts_json": json_dump(spec["artifacts"]),
        "allow_path_overlap": 1 if spec["allow_path_overlap"] else 0,
    }
    values.update({field + "_json": json_dump(spec[field]) for field in TASK_LIST_FIELDS})
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    conn.execute(f"INSERT INTO tasks({columns}) VALUES({placeholders})", tuple(values.values()))
    return acknowledgements


def do_post(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, raw: str) -> None:
    identity(config, agent, device, "post")
    spec = validate_task_contract(config, raw)
    acknowledgements: list[dict[str, Any]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        acknowledgements = insert_task(conn, config, agent, spec, now())
        detail = {key: spec[key] for key in ("title", "priority", "assignee", "decision_key", "base_commit")}
        if acknowledgements:
            detail["path_overlap_ack"] = acknowledgements
        record(conn, agent, device, "task.post", spec["id"], detail)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已发布 {spec['id']}")
    if acknowledgements:
        print(json.dumps({"task_id": spec["id"], "path_overlap_ack": acknowledgements},
                         ensure_ascii=False), file=sys.stderr)


def load_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        raise HubError(f"任务不存在: {task_id}")
    return row


def ensure_worktree(config: dict[str, Any], row: sqlite3.Row) -> None:
    path = Path(row["worktree_path"])
    if path.exists():
        if not (path / ".git").exists():
            raise HubError(f"worktree 路径已存在但不是 Git worktree: {path}")
        current = git(config, "branch", "--show-current", cwd=str(path))
        if current != row["branch"]:
            raise HubError(f"worktree 分支不一致: {current} != {row['branch']}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = bool(git(config, "show-ref", "--verify", f"refs/heads/{row['branch']}", check=False))
    args = ["worktree", "add"]
    if not branch_exists:
        args.extend(["-b", row["branch"]])
    args.extend([str(path), row["branch"] if branch_exists else row["base_commit"]])
    git(config, *args)


def claim_task(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, row: sqlite3.Row,
               session: str | None, lease_minutes: int | None) -> dict[str, Any]:
    task_id = row["task_id"]
    if row["assignee"] != agent:
        raise HubError(f"任务 {task_id} 已指定给 {row['assignee']}，{agent} 不得认领")
    if row["status"] != "ready" or row["claimed_by"]:
        raise HubError(f"任务 {task_id} 当前不可认领: {row['status']}")
    if session_policy(config) == "required" and not session:
        raise HubError("execution.session_policy=required：claim/claim-next 必须带 --session <id>")
    dependencies = dependency_state(conn, row)
    if not dependencies["satisfied"]:
        detail = ", ".join(f"{item['task_id']}({item['status']})" for item in dependencies["unmet"])
        raise HubError(f"任务 {task_id} 的依赖尚未全部 closed: {detail}")
    seconds = lease_seconds_argument(lease_minutes)
    ensure_capacity(conn, config, agent)
    ensure_worktree(config, row)
    stamp = now()
    conn.execute(
        "UPDATE tasks SET status='in_progress',claimed_by=?,active_device=?,claim_session=?,"
        "last_heartbeat_at=?,lease_seconds=?,claim_generation=claim_generation+1,updated_at=? WHERE task_id=?",
        (agent, device, session, stamp, seconds, stamp, task_id),
    )
    updated = load_task(conn, task_id)
    record(conn, agent, device, "task.claim", task_id, {
        "worktree": row["worktree_path"], "branch": row["branch"], "session": session,
        "lease_seconds": seconds, "generation": updated["claim_generation"],
    })
    # Read the packet inside the transaction: it is the first thing that touches the new
    # worktree with git, and a failure there must leave the task claimable rather than
    # committed as in_progress with nobody able to reach it.
    packet = task_packet(conn, updated, config, session)
    stale = packet.get("dependencies", {}).get("stale_base", [])
    if stale:
        print(json.dumps({"task_id": task_id, "warning": "base_commit 不包含依赖的合并提交", "stale_base": stale},
                         ensure_ascii=False), file=sys.stderr)
    return packet


def do_claim(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
             session: str | None, lease_minutes: int | None) -> None:
    identity(config, agent, device, "claim")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        packet = claim_task(conn, config, agent, device, row, session, lease_minutes)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(json.dumps(packet, ensure_ascii=False, indent=2))


def do_claim_next(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str,
                  session: str | None, lease_minutes: int | None) -> None:
    identity(config, agent, device, "claim")
    if session_policy(config) == "required" and not session:
        raise HubError("execution.session_policy=required：claim/claim-next 必须带 --session <id>")
    packet: dict[str, Any] | None = None
    skipped: list[dict[str, Any]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE assignee=? AND status='ready' AND claimed_by IS NULL "
            "ORDER BY priority,created_at,task_id",
            (agent,),
        ).fetchall()
        for row in rows:
            state = dependency_state(conn, row)
            if not state["satisfied"]:
                skipped.append({"task_id": row["task_id"], "reason": "dependencies_unmet",
                                "dependencies": state["unmet"]})
                continue
            packet = claim_task(conn, config, agent, device, row, session, lease_minutes)
            break
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(json.dumps({
        "task": packet, "skipped": skipped,
        "concurrency": {"active": len(active_task_ids(conn, agent)), "limit": task_limit(config, agent)},
        "session": session,
    }, ensure_ascii=False, indent=2))


def do_release(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
               reason: str, session: str | None, if_expired: bool = False) -> None:
    info = identity(config, agent, device, "release")
    if not reason.strip():
        raise HubError("释放原因不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if row["status"] != "in_progress":
            raise HubError(f"只有 in_progress 任务可以释放，当前状态: {row['status']}")
        administrator = "administrator" in set(info.get("roles", []))
        forced = row["claimed_by"] != agent
        if forced and not administrator:
            raise HubError("只有当前执行者或管理员可以释放任务")
        lease = lease_state(row)
        if if_expired:
            if not administrator:
                raise HubError("--if-expired 仅管理员可用")
            if not lease["lease_seconds"]:
                raise HubError("任务没有租约；直接 release，或先 heartbeat --lease-minutes 设置租约")
            if not lease["expired"]:
                raise HubError(f"任务租约尚未过期（剩余 {lease['remaining_seconds']} 秒）；拒绝按过期释放")
        if not forced:
            require_session(config, row, session, "release")
        cursor = conn.execute(
            "UPDATE tasks SET status='ready',claimed_by=NULL,active_device=NULL,claim_session=NULL,"
            "last_heartbeat_at=NULL,lease_seconds=0,updated_at=? "
            "WHERE task_id=? AND status='in_progress'",
            (now(), task_id),
        )
        if cursor.rowcount != 1:
            raise HubError("任务状态已被其他命令改变，释放失败")
        record(conn, agent, device, "task.release", task_id, {
            "executor": row["claimed_by"], "previous_status": row["status"],
            "worktree": row["worktree_path"], "branch": row["branch"],
            "session": row["claim_session"], "lease_expired": lease["expired"],
            "reason": reason.strip(), "forced": forced,
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已释放 {task_id} -> ready")


def fresh_checkpoint_event(conn: sqlite3.Connection, task_id: str, executor: str, device: str) -> sqlite3.Row | None:
    boundary = conn.execute(
        "SELECT event_id FROM events WHERE entity_id=? AND action IN ('task.claim','task.handoff','task.handoff.force') ORDER BY event_id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    checkpoint = conn.execute(
        "SELECT event_id,detail_json FROM events WHERE entity_id=? AND action='task.checkpoint' AND agent_id=? AND device=? ORDER BY event_id DESC LIMIT 1",
        (task_id, executor, device),
    ).fetchone()
    if not boundary or not checkpoint or checkpoint["event_id"] <= boundary["event_id"]:
        return None
    return checkpoint


def do_handoff(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
               to_device: str, note: str, session: str | None) -> None:
    info = identity(config, agent, device, "handoff")
    if not note.strip():
        raise HubError("设备交接说明不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if row["status"] != "in_progress" or not row["claimed_by"]:
            raise HubError("只有 in_progress 任务可以进行设备交接")
        executor = row["claimed_by"]
        executor_info = config["agents"].get(executor, {})
        if to_device not in executor_info.get("devices", []):
            raise HubError(f"执行者 {executor} 不允许在设备 {to_device} 工作")
        forced = "administrator" in set(info.get("roles", [])) and agent != executor
        if not forced:
            if agent != executor:
                raise HubError("只有当前执行者或管理员可以交接任务设备")
            if row["active_device"] != device:
                raise HubError(f"任务当前活动设备是 {row['active_device']}，必须从该设备发起交接")
            require_session(config, row, session, "handoff")
        checkpoint = fresh_checkpoint_event(conn, task_id, executor, row["active_device"])
        if not forced and checkpoint is None:
            raise HubError("设备交接前必须由当前执行者在当前活动设备保存本轮 checkpoint；请先执行 checkpoint")
        stamp = now()
        conn.execute(
            "UPDATE tasks SET active_device=?,last_heartbeat_at=?,updated_at=? WHERE task_id=?",
            (to_device, stamp, stamp, task_id),
        )
        record(conn, agent, device, "task.handoff.force" if forced else "task.handoff", task_id, {
            "from_device": row["active_device"], "to_device": to_device, "executor": executor,
            "session": row["claim_session"], "note": note.strip(),
            "checkpoint_missing": checkpoint is None,
            "checkpoint_event_id": checkpoint["event_id"] if checkpoint else None,
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已交接 {task_id}: {row['active_device']} -> {to_device}")


def do_checkpoint(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
                  raw: str, session: str | None) -> None:
    identity(config, agent, device, "checkpoint")
    progress = read_json_argument(raw, "进度 checkpoint")
    summary = required_text(progress, "summary", 1200)
    next_step = required_text(progress, "next_step", 1200)
    evidence = progress.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 20 or any(
        not isinstance(item, str) or not item.strip() for item in evidence
    ):
        raise HubError("checkpoint.evidence 必须是不超过 20 个非空字符串的数组")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if row["claimed_by"] != agent or row["status"] != "in_progress":
            raise HubError("只能为自己正在执行的任务写入 checkpoint")
        if row["active_device"] != device:
            raise HubError(f"任务活动设备是 {row['active_device']}；请先从活动设备执行 handoff")
        require_session(config, row, session, "checkpoint")
        stamp = now()
        conn.execute(
            "INSERT INTO checkpoints(task_id,agent_id,device,summary,next_step,evidence_json,generation,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, agent, device, summary, next_step, json_dump(evidence), row["claim_generation"], stamp),
        )
        conn.execute("UPDATE tasks SET updated_at=?,last_heartbeat_at=? WHERE task_id=?", (stamp, stamp, task_id))
        record(conn, agent, device, "task.checkpoint", task_id, {
            "summary": summary, "next_step": next_step, "generation": row["claim_generation"],
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已保存 checkpoint {task_id}")


def do_heartbeat(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
                 session: str | None, lease_minutes: int | None, note: str = "") -> None:
    """Refresh liveness without writing a checkpoint.

    This is also the only command that binds a session to an already-running legacy claim, so a
    long task can adopt isolation after an upgrade without being released and re-claimed.
    """
    info = identity(config, agent, device, "heartbeat")
    if not session:
        raise HubError("heartbeat 必须带 --session <id>")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if row["status"] != "in_progress":
            raise HubError(f"只有 in_progress 任务可以 heartbeat，当前状态: {row['status']}")
        administrator = "administrator" in set(info.get("roles", []))
        forced = administrator and agent != row["claimed_by"]
        if not forced:
            if row["claimed_by"] != agent:
                raise HubError("只能为自己正在执行的任务 heartbeat")
            if row["active_device"] != device:
                raise HubError(f"任务活动设备是 {row['active_device']}；请先从活动设备执行 handoff")
        if row["claim_session"] and row["claim_session"] != session:
            raise HubError(
                f"任务 {task_id} 已绑定会话 {row['claim_session']}；当前会话 {session} 不得 heartbeat"
            )
        seconds = row["lease_seconds"] if lease_minutes is None else lease_seconds_argument(lease_minutes)
        stamp = now()
        conn.execute(
            "UPDATE tasks SET claim_session=?,last_heartbeat_at=?,lease_seconds=?,updated_at=? WHERE task_id=?",
            (session, stamp, seconds, stamp, task_id),
        )
        if not row["claim_session"]:
            record(conn, agent, device, "task.session.bind", task_id, {
                "session": session, "executor": row["claimed_by"], "forced": forced,
                "active_device": row["active_device"], "note": note.strip(),
            })
        conn.commit()
        updated = load_task(conn, task_id)
    except Exception:
        conn.rollback()
        raise
    print(json.dumps({
        "task_id": task_id, "session": session, "heartbeat_at": stamp,
        "lease": lease_state(updated),
    }, ensure_ascii=False, indent=2))


def do_submit(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
              raw: str, session: str | None) -> None:
    identity(config, agent, device, "submit")
    result = read_json_argument(raw, "执行结果")
    outcome = required_text(result, "outcome")
    if outcome not in {"completed", "blocked"}:
        raise HubError("outcome 仅允许 completed 或 blocked")
    required_text(result, "summary")
    if "gates" in result:
        raise HubError("gates 是核心计算字段；请在 gate_results 中提供实测值")
    if outcome == "completed":
        required_list(result, "evidence")
        required_list(result, "tests")
        commit = required_text(result, "commit", 80)
        validate_revision(commit, "result.commit")
        target = "review"
    else:
        required_text(result, "blocker")
        required_text(result, "decision_needed")
        target = "blocked"
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if row["claimed_by"] != agent or row["status"] != "in_progress":
            raise HubError("只能由当前执行者提交处于 in_progress 的任务")
        if row["active_device"] != device:
            raise HubError(f"任务活动设备是 {row['active_device']}；请先从活动设备执行 handoff")
        require_session(config, row, session, "submit")
        gate_definitions = load_state_json(row["gates_json"], f"tasks[{task_id}].gates")
        evaluated = evaluate_gates(gate_definitions, result.get("gate_results"),
                                   require_all=(outcome == "completed"))
        if outcome == "completed":
            failed = [gate for gate in evaluated if not gate["passed"]]
            if failed:
                detail = "; ".join(
                    f"{gate['id']} ({gate['metric']}): {gate['value']} {gate['operator']} {gate['threshold']}"
                    for gate in failed
                )
                raise HubError(
                    f"合同门禁未通过: {detail}；请核对测量口径，或改用 blocked 提交并把实测值与取舍交回决策者"
                )
        if evaluated:
            result["gates"] = evaluated
        if outcome == "completed":
            worktree = row["worktree_path"]
            branch = git(config, "branch", "--show-current", cwd=worktree)
            if branch != row["branch"]:
                raise HubError(f"worktree 当前分支不符合任务合同: {branch} != {row['branch']}")
            head = git(config, "rev-parse", "HEAD", cwd=worktree)
            if head != commit:
                raise HubError(f"提交证据 commit 与 worktree HEAD 不一致: {commit} != {head}")
            dirty = git(config, "status", "--porcelain", cwd=worktree)
            if dirty:
                raise HubError("worktree 仍有未提交变更；执行者必须先提交代码再申请复核")
            if not git_ok(config, "merge-base", "--is-ancestor", row["base_commit"], commit, cwd=worktree):
                raise HubError("提交 commit 不是任务 base_commit 的后代")
            changed = changed_paths(config, row["base_commit"], commit, worktree)
            allowed = load_state_json(row["allowed_paths_json"], f"tasks[{task_id}].allowed_paths")
            forbidden = load_state_json(row["forbidden_paths_json"], f"tasks[{task_id}].forbidden_paths")
            outside = [path for path in changed if not any(fnmatch.fnmatch(path, pattern) for pattern in allowed)]
            denied = [path for path in changed if any(fnmatch.fnmatch(path, pattern) for pattern in forbidden)]
            if outside or denied:
                raise HubError(f"提交文件超出合同路径边界；未授权={outside}；禁止={denied}")
            verify_artifacts(config, load_state_json(row["artifacts_json"], f"tasks[{task_id}].artifacts"),
                             f"任务 {task_id}")
        stamp = now()
        conn.execute(
            "INSERT INTO submissions(task_id,agent_id,device,outcome,result_json,created_at) VALUES(?,?,?,?,?,?)",
            (task_id, agent, device, outcome, json_dump(result), stamp),
        )
        conn.execute(
            "UPDATE tasks SET status=?,result_json=?,lease_seconds=0,last_heartbeat_at=?,updated_at=? WHERE task_id=?",
            (target, json_dump(result), stamp, stamp, task_id),
        )
        record(conn, agent, device, f"task.submit.{outcome}", task_id, {
            "summary": result["summary"], "session": row["claim_session"],
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已提交 {task_id} -> {target}")


def do_review(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str, action: str, note: str) -> None:
    identity(config, agent, device, "review")
    if not note.strip():
        raise HubError("复核意见不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if action == "approve":
            if row["status"] != "review":
                raise HubError("只有 review 状态可以批准")
            gate_definitions = load_state_json(row["gates_json"], f"tasks[{task_id}].gates")
            if gate_definitions:
                if not row["result_json"]:
                    raise HubError("review 任务缺少执行结果，不能批准")
                submitted = load_state_json(row["result_json"], f"tasks[{task_id}].result")
                evaluated = evaluate_gates(gate_definitions, submitted.get("gate_results"), require_all=True)
                failed = [gate for gate in evaluated if not gate["passed"]]
                if failed:
                    detail = "; ".join(
                        f"{gate['id']} ({gate['metric']}): {gate['value']} {gate['operator']} {gate['threshold']}"
                        for gate in failed
                    )
                    raise HubError(f"合同门禁未通过，不能批准: {detail}")
            target, claimed_by, active_device = "approved", row["claimed_by"], row["active_device"]
            claim_session = row["claim_session"]
        elif action == "rework":
            if row["status"] not in {"review", "blocked"}:
                raise HubError("只有 review/blocked 状态可以退回")
            target, claimed_by, active_device = "ready", None, None
            claim_session = None
        else:
            raise HubError("action 仅允许 approve 或 rework")
        result_value = row["result_json"] if action == "approve" else None
        conn.execute(
            "UPDATE tasks SET status=?,claimed_by=?,active_device=?,claim_session=?,"
            "last_heartbeat_at=NULL,lease_seconds=0,result_json=?,updated_at=? WHERE task_id=?",
            (target, claimed_by, active_device, claim_session, result_value, now(), task_id),
        )
        record(conn, agent, device, f"task.review.{action}", task_id, {"note": note.strip()})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已复核 {task_id} -> {target}")


def do_close(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str, merge_commit: str, target_branch: str, note: str) -> None:
    identity(config, agent, device, "close")
    conn.execute("BEGIN IMMEDIATE")
    worktree = Path("")
    remove_worktree = False
    remove_branch = False
    try:
        row = load_task(conn, task_id)
        if row["status"] != "approved" or not row["result_json"]:
            raise HubError("只有 approved 且保留完成结果的任务可以关闭")
        result = load_state_json(row["result_json"], f"tasks[{task_id}].result")
        task_commit = required_text(result, "commit", 80)
        validate_revision(task_commit, "result.commit")
        if not note.strip():
            raise HubError("关闭说明不能为空")
        validate_revision(merge_commit, "merge_commit")
        validate_branch_name(target_branch, "target_branch")
        git(config, "rev-parse", "--verify", f"{merge_commit}^{{commit}}")
        target_head = git(config, "rev-parse", "--verify", f"refs/heads/{target_branch}^{{commit}}")
        if target_head != merge_commit:
            raise HubError(f"merge_commit 必须是目标分支 {target_branch} 的当前 HEAD")
        if not git_ok(config, "merge-base", "--is-ancestor", task_commit, merge_commit):
            raise HubError("目标分支尚未包含已批准的任务 commit")
        artifacts = load_state_json(row["artifacts_json"], f"tasks[{task_id}].artifacts")
        verify_artifacts(config, artifacts, f"任务 {task_id}")
        worktree = Path(row["worktree_path"])
        if worktree.exists():
            if git(config, "status", "--porcelain", cwd=str(worktree)):
                raise HubError("任务 worktree 仍有未提交变更，不能清理")
            head = git(config, "rev-parse", "HEAD", cwd=str(worktree))
            if head != task_commit:
                raise HubError(f"worktree HEAD 与已批准 commit 不一致: {head} != {task_commit}")
            remove_worktree = True
        remove_branch = (
            row["branch"] != target_branch
            and bool(git(config, "show-ref", "--verify", f"refs/heads/{row['branch']}", check=False))
        )
        cursor = conn.execute(
            "UPDATE tasks SET status='closed',active_device=NULL,claim_session=NULL,"
            "last_heartbeat_at=NULL,lease_seconds=0,updated_at=? WHERE task_id=? AND status='approved'",
            (now(), task_id),
        )
        if cursor.rowcount != 1:
            raise HubError("任务状态已被其他命令改变，关闭失败")
        detail = {
            "task_commit": task_commit, "merge_commit": merge_commit,
            "target_branch": target_branch, "session": row["claim_session"], "note": note.strip(),
        }
        if artifacts:
            detail["artifacts"] = artifacts
        record(conn, agent, device, "task.close", task_id, detail)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # State is durable before anything is destroyed. A failed cleanup leaves a closed task with
    # its worktree or branch behind, which `prune` can reclaim; the reverse order could delete
    # work for a task that a rollback then kept alive.
    cleanup_errors: list[str] = []
    if remove_worktree:
        try:
            git(config, "worktree", "remove", str(worktree))
        except (HubError, OSError) as exc:
            cleanup_errors.append(f"worktree: {exc}")
    if remove_branch:
        try:
            git(config, "branch", "-d", row["branch"])
        except (HubError, OSError) as exc:
            cleanup_errors.append(f"branch: {exc}")
    if cleanup_errors:
        print(json.dumps({"task_id": task_id, "cleanup": "pending", "errors": cleanup_errors,
                          "hint": "run prune --apply after the cause is fixed"}, ensure_ascii=False),
              file=sys.stderr)
    print(f"已关闭 {task_id}；目标 {target_branch}@{merge_commit}")


def do_cancel(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str, reason: str) -> None:
    info = identity(config, agent, device, "cancel")
    if not reason.strip():
        raise HubError("取消原因不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        # in_progress and approved have no other way out: an executor whose claim failed after
        # the state write cannot release the task, and an approved task can be neither reworked
        # nor closed once the merge will not happen. Administrators may cancel both; everyone
        # else keeps the narrow ready/blocked/review rule.
        forced = row["status"] in {"in_progress", "approved"} and "administrator" in set(info.get("roles", []))
        if row["status"] not in {"ready", "blocked", "review"} and not forced:
            raise HubError(
                f"只有 ready/blocked/review 状态可以取消，当前状态: {row['status']}"
                "；in_progress/approved 只能由管理员取消"
            )
        stamp = now()
        conn.execute(
            "UPDATE tasks SET status='cancelled',claimed_by=NULL,active_device=NULL,claim_session=NULL,"
            "last_heartbeat_at=NULL,lease_seconds=0,cancelled_at=?,cancelled_by=?,cancel_reason=?,updated_at=? "
            "WHERE task_id=?",
            (stamp, agent, reason.strip(), stamp, task_id),
        )
        record(conn, agent, device, "task.cancel", task_id, {
            "reason": reason.strip(), "previous_status": row["status"],
            "executor": row["claimed_by"], "forced": forced,
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已取消 {task_id}")
    dependents = dependents_of(conn, task_id)
    if dependents:
        print(json.dumps({"task_id": task_id, "warning": "仍有活动任务依赖该任务，取消后它们无法 claim",
                          "dependents": dependents}, ensure_ascii=False), file=sys.stderr)


def parse_worktree_listing(text: str) -> list[dict[str, Any]]:
    """Parse `git worktree list --porcelain` into one dict per registered worktree."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):], "branch": None, "head": None}
            entries.append(current)
        elif current is not None and line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif current is not None and line.startswith("branch "):
            current["branch"] = line[len("branch "):].removeprefix("refs/heads/")
    return entries


def do_prune(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, apply_changes: bool) -> None:
    """Reclaim worktrees nothing is working in. Cancelled and superseded tasks never release
    theirs, and there is no other way to collect them; this reports first and only removes
    under --apply, never touches a live task or a worktree holding uncommitted changes, and
    never deletes a directory git does not recognise as one of its worktrees."""
    identity(config, agent, device, "prune")
    repository = Path(config["execution"]["repository"]).expanduser().resolve()
    root = Path(config["execution"]["worktree_root"]).expanduser().resolve()
    rows = conn.execute("SELECT * FROM tasks").fetchall()
    by_path = {str(Path(row["worktree_path"]).expanduser().resolve()): row for row in rows}
    report: list[dict[str, Any]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for entry in parse_worktree_listing(git(config, "worktree", "list", "--porcelain")):
            resolved = Path(entry["path"]).expanduser().resolve()
            if resolved == repository:
                continue
            row = by_path.get(str(resolved))
            task_id = row["task_id"] if row else None
            status = row["status"] if row else None
            reason = None
            if row is not None and status not in TERMINAL_STATUSES:
                reason = f"live task ({status})"
            elif row is None and not resolved.is_relative_to(root):
                reason = "not a registered task and outside worktree_root"
            elif not resolved.exists():
                reason = "directory is gone; the stale entry is cleared by worktree prune"
            else:
                try:
                    if git(config, "status", "--porcelain", cwd=str(resolved)):
                        reason = "uncommitted changes"
                except HubError:
                    reason = "git could not read this worktree"
            action = "skipped" if reason else ("removed" if apply_changes else "would-remove")
            item: dict[str, Any] = {"path": str(resolved), "task_id": task_id, "status": status,
                                    "branch": entry["branch"], "action": action}
            if reason:
                item["reason"] = reason
            elif apply_changes:
                git(config, "worktree", "remove", str(resolved))
                branch = entry["branch"]
                if branch and branch.startswith("agenthub/") and git_ok(config, "show-ref", "--verify", f"refs/heads/{branch}"):
                    git(config, "branch", "-D", branch)
                record(conn, agent, device, "worktree.prune", task_id or str(resolved),
                       {"path": str(resolved), "branch": branch, "task_id": task_id})
            report.append(item)
        if apply_changes:
            git(config, "worktree", "prune")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(json.dumps({
        "applied": apply_changes,
        "worktree_root": str(root),
        "candidates": report,
        "removed": sum(1 for item in report if item["action"] == "removed"),
        "skipped": sum(1 for item in report if item["action"] == "skipped"),
        "note": "report only; re-run with --apply to remove" if not apply_changes else "removal applied",
    }, ensure_ascii=False, indent=2))


def do_supersede(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str, new_spec: str, reason: str) -> None:
    identity(config, agent, device, "supersede")
    if not reason.strip():
        raise HubError("替代原因不能为空")
    spec = validate_task_contract(config, new_spec)
    new_id = spec["id"]
    acknowledgements: list[dict[str, Any]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        old_row = load_task(conn, task_id)
        if old_row["status"] not in {"ready", "blocked", "review", "in_progress"}:
            raise HubError(f"只有 ready/blocked/review/in_progress 状态可以被替代，当前状态: {old_row['status']}")
        if old_row["superseded_by"]:
            raise HubError(f"任务 {task_id} 已被 {old_row['superseded_by']} 替代")
        stamp = now()
        acknowledgements = insert_task(conn, config, agent, spec, stamp, supersedes=task_id)
        detail = {"new_task": new_id, "reason": reason.strip()}
        if acknowledgements:
            detail["path_overlap_ack"] = acknowledgements
        record(conn, agent, device, "task.supersede.new_posted", task_id, detail)
        conn.execute(
            "UPDATE tasks SET status='cancelled',claimed_by=NULL,active_device=NULL,claim_session=NULL,"
            "last_heartbeat_at=NULL,lease_seconds=0,cancelled_at=?,cancelled_by=?,cancel_reason=?,superseded_by=?,updated_at=? "
            "WHERE task_id=?",
            (stamp, agent, f"superseded by {new_id}: {reason.strip()}", new_id, stamp, task_id),
        )
        record(conn, agent, device, "task.supersede.old_cancelled", task_id, {
            "new_task": new_id, "reason": reason.strip(), "previous_status": old_row["status"],
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已替代 {task_id} -> {new_id}")
    if acknowledgements:
        print(json.dumps({"task_id": new_id, "path_overlap_ack": acknowledgements},
                         ensure_ascii=False), file=sys.stderr)
    dependents = dependents_of(conn, task_id)
    if dependents:
        print(json.dumps({"task_id": task_id, "warning": "仍有活动任务依赖被替代的任务，需要一并 supersede",
                          "dependents": dependents}, ensure_ascii=False), file=sys.stderr)


def do_decision(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, key: str, summary: str, rationale: str) -> None:
    identity(config, agent, device, "decision")
    if not key.strip() or not summary.strip() or not rationale.strip():
        raise HubError("决策 key、summary、rationale 均不能为空")
    stamp = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO decisions(decision_key,summary,rationale,decided_by,device,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(decision_key) DO UPDATE SET summary=excluded.summary,rationale=excluded.rationale,decided_by=excluded.decided_by,device=excluded.device,updated_at=excluded.updated_at",
            (key, summary, rationale, agent, device, stamp),
        )
        conn.execute(
            "INSERT INTO decision_revisions(decision_key,summary,rationale,decided_by,device,created_at) VALUES(?,?,?,?,?,?)",
            (key, summary, rationale, agent, device, stamp),
        )
        record(conn, agent, device, "decision.set", key, {"summary": summary})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已记录决策 {key}")


def do_audit(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, limit: int) -> None:
    identity(config, agent, device, "audit")
    if limit < 1 or limit > 1000:
        raise HubError("audit --limit 必须是 1..1000 的整数")
    rows = conn.execute("SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (limit,)).fetchall()
    print(json.dumps([
        dict(row) | {"detail": load_state_json(row["detail_json"], f"events[{row['event_id']}].detail")}
        for row in rows
    ], ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="AgentHub v2")
    value.add_argument("--project")
    value.add_argument("--config")
    value.add_argument("--state")
    value.add_argument("--device", default=os.environ.get("AGENTHUB_DEVICE"))
    value.add_argument("--session", default=os.environ.get("AGENTHUB_SESSION"))
    value.add_argument("--local", action="store_true", help=argparse.SUPPRESS)
    subs = value.add_subparsers(dest="command", required=True)
    init = subs.add_parser("init"); init.add_argument("agent")
    status = subs.add_parser("status"); status.add_argument("agent"); status.add_argument("--all", action="store_true"); status.add_argument("--json", action="store_true")
    status_filter = status.add_mutually_exclusive_group(); status_filter.add_argument("--mine", action="store_true"); status_filter.add_argument("--assignee")
    status.add_argument("--decision")
    context = subs.add_parser("context"); context.add_argument("agent")
    next_task = subs.add_parser("next"); next_task.add_argument("agent"); next_task.add_argument("--available", action="store_true")
    show = subs.add_parser("show"); show.add_argument("agent"); show.add_argument("task_id")
    post = subs.add_parser("post"); post.add_argument("agent"); post.add_argument("--spec", required=True)
    claim = subs.add_parser("claim"); claim.add_argument("agent"); claim.add_argument("task_id"); claim.add_argument("--lease-minutes", type=int)
    claim_next = subs.add_parser("claim-next"); claim_next.add_argument("agent"); claim_next.add_argument("--lease-minutes", type=int)
    release = subs.add_parser("release"); release.add_argument("agent"); release.add_argument("task_id"); release.add_argument("--reason", required=True); release.add_argument("--if-expired", action="store_true")
    handoff = subs.add_parser("handoff"); handoff.add_argument("agent"); handoff.add_argument("task_id"); handoff.add_argument("--to-device", required=True); handoff.add_argument("--note", required=True)
    checkpoint = subs.add_parser("checkpoint"); checkpoint.add_argument("agent"); checkpoint.add_argument("task_id"); checkpoint.add_argument("--progress", required=True)
    heartbeat = subs.add_parser("heartbeat"); heartbeat.add_argument("agent"); heartbeat.add_argument("task_id"); heartbeat.add_argument("--lease-minutes", type=int); heartbeat.add_argument("--note", default="")
    submit = subs.add_parser("submit"); submit.add_argument("agent"); submit.add_argument("task_id"); submit.add_argument("--result", required=True)
    review = subs.add_parser("review"); review.add_argument("agent"); review.add_argument("task_id"); review.add_argument("--action", choices=["approve", "rework"], required=True); review.add_argument("--note", required=True)
    close = subs.add_parser("close"); close.add_argument("agent"); close.add_argument("task_id"); close.add_argument("--merge-commit", required=True); close.add_argument("--target-branch", required=True); close.add_argument("--note", required=True)
    cancel = subs.add_parser("cancel"); cancel.add_argument("agent"); cancel.add_argument("task_id"); cancel.add_argument("--reason", required=True)
    prune = subs.add_parser("prune"); prune.add_argument("agent"); prune.add_argument("--apply", action="store_true")
    supersede = subs.add_parser("supersede"); supersede.add_argument("agent"); supersede.add_argument("task_id"); supersede.add_argument("--spec", required=True); supersede.add_argument("--reason", required=True)
    decision = subs.add_parser("decision"); decision.add_argument("agent"); decision.add_argument("--key", required=True); decision.add_argument("--summary", required=True); decision.add_argument("--rationale", required=True)
    audit = subs.add_parser("audit"); audit.add_argument("agent"); audit.add_argument("--limit", type=int, default=50)
    rpc = subs.add_parser("rpc"); rpc.add_argument("payload")
    return value


def rpc_argv(payload: str) -> tuple[str, str | None, list[str]]:
    try:
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise HubError(f"无效 RPC 载荷: {exc}") from exc
    if not isinstance(value.get("argv"), list) or not isinstance(value.get("device"), str):
        raise HubError("RPC 载荷结构无效")
    session = value.get("session")
    if session is not None and not isinstance(session, str):
        raise HubError("RPC session 必须是字符串")
    if value["argv"] and value["argv"][0] == "rpc":
        raise HubError("禁止嵌套 RPC")
    return value["device"], session, value["argv"]


def run(argv: list[str] | None = None) -> int:
    argv = expand_at_files(list(argv if argv is not None else sys.argv[1:]))
    args = parser().parse_args(argv)
    path = config_path(args.project, args.config)
    config = load_config(path)
    device = args.device
    if args.command == "rpc":
        expected_platform = {"windows_cmd": "Windows", "posix_sh": "POSIX"}.get(config.get("remote", {}).get("shell"))
        actual_platform = "Windows" if platform.system() == "Windows" else "POSIX"
        if expected_platform != actual_platform and os.environ.get("AGENTHUB_ALLOW_RPC_ANYWHERE") != "1":
            raise HubError(f"RPC 必须在项目配置的 {expected_platform} 权威主机处理")
        device, session, suffix = rpc_argv(args.payload)
        forwarded = ["--local", "--config", str(path), "--state", args.state or config["state_authority"]["database"], "--device", device]
        if session:
            forwarded.extend(["--session", session])
        forwarded.extend(suffix)
        return run(forwarded)
    session = session_value(args.session)
    if not device:
        raise HubError("必须使用 --device 或 AGENTHUB_DEVICE 声明执行设备")
    if device not in config["devices"]:
        raise HubError(f"未知设备: {device}")
    if not args.local and device != config["state_authority"]["host"] and config.get("remote", {}).get("enabled"):
        return proxy_to_authority(config, device, argv, session)
    state = Path(args.state or config["state_authority"]["database"])
    conn = connect(state)
    try:
        initialize(conn)
        if args.command == "init":
            identity(config, args.agent, device, "init")
            print(f"AgentHub {VERSION} initialized: {state}")
        elif args.command == "status": do_status(conn, config, args.agent, device, args.all, args.json, args.mine, args.assignee, args.decision)
        elif args.command == "context": do_context(conn, config, args.agent, device, session)
        elif args.command == "next": do_next(conn, config, args.agent, device, session, args.available)
        elif args.command == "show": do_show(conn, config, args.agent, device, args.task_id, session)
        elif args.command == "post": do_post(conn, config, args.agent, device, args.spec)
        elif args.command == "claim": do_claim(conn, config, args.agent, device, args.task_id, session, args.lease_minutes)
        elif args.command == "claim-next": do_claim_next(conn, config, args.agent, device, session, args.lease_minutes)
        elif args.command == "release": do_release(conn, config, args.agent, device, args.task_id, args.reason, session, args.if_expired)
        elif args.command == "handoff": do_handoff(conn, config, args.agent, device, args.task_id, args.to_device, args.note, session)
        elif args.command == "checkpoint": do_checkpoint(conn, config, args.agent, device, args.task_id, args.progress, session)
        elif args.command == "heartbeat": do_heartbeat(conn, config, args.agent, device, args.task_id, session, args.lease_minutes, args.note)
        elif args.command == "submit": do_submit(conn, config, args.agent, device, args.task_id, args.result, session)
        elif args.command == "review": do_review(conn, config, args.agent, device, args.task_id, args.action, args.note)
        elif args.command == "close": do_close(conn, config, args.agent, device, args.task_id, args.merge_commit, args.target_branch, args.note)
        elif args.command == "cancel": do_cancel(conn, config, args.agent, device, args.task_id, args.reason)
        elif args.command == "prune": do_prune(conn, config, args.agent, device, args.apply)
        elif args.command == "supersede": do_supersede(conn, config, args.agent, device, args.task_id, args.spec, args.reason)
        elif args.command == "decision": do_decision(conn, config, args.agent, device, args.key, args.summary, args.rationale)
        elif args.command == "audit": do_audit(conn, config, args.agent, device, args.limit)
        return 0
    finally:
        conn.close()


def main() -> None:
    try:
        raise SystemExit(run())
    except (HubError, sqlite3.Error, OSError) as exc:
        print(f"[AgentHub] {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
