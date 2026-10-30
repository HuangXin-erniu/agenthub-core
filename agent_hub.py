#!/usr/bin/env python3
"""AgentHub v2.4.5: a small, decision-first multi-agent control plane.

The canonical runtime and SQLite state live on the project execution host.
Other devices are thin clients and proxy commands through SSH.

State model:
  * `tasks.contract_json` is the single source for the task contract.
  * `submissions` is the single source for execution results; a task points at
    the approved submission through `approved_submission_id`.
  * `worktree_path` is history after the task closes. The live `worktree` block
    appears only while the directory exists ({head, dirty_entries}).
  * `close` removes the worktree recursively and `prune` reclaims cancelled and
    superseded worktrees.

Database schema 4 is migrated to schema 5 in place on first open, after writing
`<state>.v4-<UTC timestamp>.bak`. A schema 5 database is not readable by 2.4.3:
restore that backup to downgrade.
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


VERSION = "2.4.5"
SCHEMA_VERSION = 5
CONFIG_SCHEMA = 1
MAX_CONTRACT_BYTES = 16_384
MAX_CONTRACT_BYTES_CEILING = 65_536
MAX_ACTIVE_TASKS_CEILING = 32
MAX_LEASE_MINUTES = 10080
MAX_SUBMISSIONS = 50
ROOT = Path(__file__).resolve().parent
TERMINAL_STATUSES = {"closed", "cancelled"}
ACTIVE_STATUSES = {"ready", "in_progress", "blocked", "review", "approved"}
SESSION_POLICIES = {"optional", "required"}
SELF_REVIEW_POLICIES = {"allow", "forbid"}
SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
GATE_OPERATORS = {">=", ">", "<=", "<"}
GATE_COMPARE = {
    ">=": operator_module.ge, ">": operator_module.gt,
    "<=": operator_module.le, "<": operator_module.lt,
}
PUBLIC_COMMANDS = (
    "status", "context", "next", "show", "post", "claim", "release", "handoff",
    "checkpoint", "heartbeat", "submit", "review", "close", "cancel", "supersede",
    "prune", "decision", "audit",
)
HIDDEN_COMMANDS = {"rpc"}
COMMANDS = set(PUBLIC_COMMANDS) | HIDDEN_COMMANDS
PERMISSIONS = {
    "administrator": {
        "post", "review", "close", "handoff", "show", "decision", "audit", "status",
        "context", "cancel", "supersede", "release", "prune", "heartbeat",
    },
    "decider": {"post", "review", "show", "decision", "audit", "status", "context"},
    "executor": {
        "claim", "handoff", "checkpoint", "submit", "heartbeat", "show", "status",
        "context", "next", "release",
    },
}
CONTRACT_FIELDS = {
    "id", "title", "objective", "assignee", "decision_key", "base_commit",
    "instructions", "acceptance", "allowed_paths", "forbidden_paths", "depends_on",
    "gates", "artifacts", "allow_path_overlap", "priority", "chain_id",
}
ERROR_CODES = frozenset({
    "domain.error", "config.invalid", "identity.unknown", "identity.device",
    "identity.permission", "task.not_found", "task.state", "task.capacity",
    "task.session", "contract.invalid", "contract.budget", "dependency.unmet",
    "path.overlap", "path.boundary", "gate.failed", "artifact.missing", "git.error",
    "state.corrupt", "state.conflict", "state.migration", "input.invalid", "io.error",
    "internal.error",
})
TEXT_MODE = False


class HubError(RuntimeError):
    def __init__(self, code: str, message: str | None = None, details: dict[str, Any] | None = None):
        if message is None:
            code, message = "domain.error", code
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def agent_roles(info: dict[str, Any]) -> set[str]:
    return set(info.get("roles", []))


def has_executor_role(info: dict[str, Any]) -> bool:
    return "executor" in agent_roles(info)


def has_review_role(info: dict[str, Any]) -> bool:
    return bool(agent_roles(info) & {"decider", "administrator"})


def self_review_policy(config: dict[str, Any], agent: str) -> str:
    return config["agents"].get(agent, {}).get("self_review", "allow")


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
        raise HubError("input.invalid", "session 必须是 1-64 位字母、数字、点、下划线或连字符，且以字母或数字开头")
    return value


def session_policy(config: dict[str, Any]) -> str:
    return config.get("execution", {}).get("session_policy", "optional")


def lease_seconds_argument(minutes: int | None) -> int:
    if minutes is None:
        return 0
    if type(minutes) is not int or not 0 <= minutes <= MAX_LEASE_MINUTES:
        raise HubError("input.invalid", f"lease_minutes 必须是 0..{MAX_LEASE_MINUTES} 的整数")
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
        raise HubError("state.corrupt", f"状态数据损坏 ({label}): {exc}") from exc


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_json_argument(raw: str, label: str, limit: int = MAX_CONTRACT_BYTES) -> dict[str, Any]:
    if raw.startswith("@"):
        try:
            raw = Path(raw[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise HubError("input.invalid", f"{label} 无法读取 @文件: {exc}") from exc
    if len(raw.encode("utf-8")) > limit:
        raise HubError("contract.budget", f"{label} 超过 {limit} 字节；请只提交摘要和证据指针")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HubError("input.invalid", f"{label} 必须是 JSON 对象或 @文件路径: {exc}") from exc
    if not isinstance(value, dict):
        raise HubError("input.invalid", f"{label} 必须是 JSON 对象")
    return value


def expand_at_files(argv: list[str]) -> list[str]:
    expanded = list(argv)
    for flag in ("--spec", "--progress", "--result"):
        if flag in expanded:
            index = expanded.index(flag) + 1
            if index < len(expanded) and expanded[index].startswith("@"):
                try:
                    expanded[index] = Path(expanded[index][1:]).read_text(encoding="utf-8")
                except OSError as exc:
                    raise HubError("input.invalid", f"{flag} 无法读取 @文件: {exc}") from exc
    return expanded


def config_path(project: str | None, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    project = project or os.environ.get("AGENTHUB_PROJECT")
    if not project:
        raise HubError("input.invalid", "请使用 --project、AGENTHUB_PROJECT 或 --config 指定项目")
    return ROOT / "projects" / project / "project.json"


def _present(execution: dict[str, Any], key: str) -> bool:
    return key in execution and execution[key] is not None


def validate_execution_config(execution: dict[str, Any]) -> None:
    legacy_concurrency = execution.get("concurrency")
    if legacy_concurrency is not None:
        if not isinstance(legacy_concurrency, str) or legacy_concurrency not in {
            "one_active_task_per_executor", "multiple_tasks_per_executor"
        }:
            raise HubError("config.invalid", "execution.concurrency 仅支持 one_active_task_per_executor 或 multiple_tasks_per_executor")
        if legacy_concurrency == "multiple_tasks_per_executor" and not _present(execution, "max_active_tasks_per_executor"):
            raise HubError(
                "config.invalid",
                "execution.concurrency 已由 execution.max_active_tasks_per_executor 取代；"
                "启用多任务时必须声明后者",
            )
    legacy_isolation = execution.get("isolation")
    if legacy_isolation is not None and legacy_isolation != "git_worktree_per_task":
        raise HubError("config.invalid", "execution.isolation 仅支持 git_worktree_per_task")
    maximum = execution.get("max_active_tasks_per_executor", 1)
    if type(maximum) is not int or not 1 <= maximum <= MAX_ACTIVE_TASKS_CEILING:
        raise HubError("config.invalid", f"execution.max_active_tasks_per_executor 必须是 1..{MAX_ACTIVE_TASKS_CEILING} 的整数")
    if legacy_concurrency == "one_active_task_per_executor" and maximum != 1:
        raise HubError("config.invalid", "one_active_task_per_executor 的 execution.max_active_tasks_per_executor 只能为 1")
    max_contract = execution.get("max_contract_bytes", MAX_CONTRACT_BYTES)
    if type(max_contract) is not int or not 4096 <= max_contract <= MAX_CONTRACT_BYTES_CEILING:
        raise HubError("config.invalid", f"execution.max_contract_bytes 必须是 4096..{MAX_CONTRACT_BYTES_CEILING} 的整数")
    session_policy_value = execution.get("session_policy", "optional")
    if session_policy_value not in SESSION_POLICIES:
        raise HubError("config.invalid", f"execution.session_policy 仅支持 {sorted(SESSION_POLICIES)}")
    artifact_root = execution.get("artifact_root")
    if artifact_root is not None:
        if not isinstance(artifact_root, str) or not artifact_root.strip():
            raise HubError("config.invalid", "execution.artifact_root 必须是非空字符串")
        worktree_root = execution.get("worktree_root")
        if worktree_root:
            artifacts = Path(artifact_root).expanduser().resolve()
            worktrees = Path(worktree_root).expanduser().resolve()
            if artifacts == worktrees or artifacts.is_relative_to(worktrees):
                raise HubError("config.invalid", "execution.artifact_root 不能位于 execution.worktree_root 之内；close 与 prune 会删除该目录")


def project_task_limit(config: dict[str, Any]) -> int:
    return int(config.get("execution", {}).get("max_active_tasks_per_executor", 1))


def task_limit(config: dict[str, Any], agent: str) -> int:
    return config["agents"].get(agent, {}).get("max_active_tasks", project_task_limit(config))


def max_contract_bytes(config: dict[str, Any]) -> int:
    return int(config.get("execution", {}).get("max_contract_bytes", MAX_CONTRACT_BYTES))


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HubError("config.invalid", f"无法读取项目配置 {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise HubError("config.invalid", "项目配置必须是 JSON 对象")
    required = {"schema_version", "project_id", "agents", "devices", "state_authority"}
    missing = sorted(required - config.keys())
    if missing:
        raise HubError("config.invalid", "项目配置缺少字段: " + ", ".join(missing))
    for section in ("state_authority", "execution", "remote", "deployment", "task_naming"):
        if section in config and not isinstance(config[section], dict):
            raise HubError("config.invalid", f"项目配置的 {section} 必须是 JSON 对象")
    if type(config["schema_version"]) is not int or config["schema_version"] != CONFIG_SCHEMA:
        raise HubError("config.invalid", "不支持的项目配置版本")
    project_id = config.get("project_id")
    if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", project_id):
        raise HubError("config.invalid", "project_id 只能包含字母、数字、点、下划线和连字符")
    if not isinstance(config.get("devices"), dict) or not config["devices"]:
        raise HubError("config.invalid", "项目必须声明至少一个设备")
    if not isinstance(config.get("agents"), dict) or not config["agents"]:
        raise HubError("config.invalid", "项目必须声明至少一个 Agent")
    for agent_id, info in config["agents"].items():
        if not isinstance(info, dict):
            raise HubError("config.invalid", f"Agent {agent_id} 必须是 JSON 对象")
        roles = info.get("roles")
        devices = info.get("devices")
        if not isinstance(roles, list) or not roles or any(
            not isinstance(role, str) or not role.strip() for role in roles
        ):
            raise HubError("config.invalid", f"Agent {agent_id} 的 roles 必须是非空字符串数组")
        if not isinstance(devices, list) or not devices or any(
            not isinstance(device, str) or not device.strip() for device in devices
        ):
            raise HubError("config.invalid", f"Agent {agent_id} 的 devices 必须是非空字符串数组")
        unknown_roles = sorted(set(roles) - set(PERMISSIONS))
        if unknown_roles:
            raise HubError("config.invalid", f"Agent {agent_id} 包含未知角色: {unknown_roles}")
        unknown_devices = sorted(set(devices) - set(config["devices"]))
        if unknown_devices:
            raise HubError("config.invalid", f"Agent {agent_id} 引用了未知设备: {unknown_devices}")
        self_review = info.get("self_review", "allow")
        if self_review not in SELF_REVIEW_POLICIES:
            raise HubError("config.invalid", f"Agent {agent_id} 的 self_review 仅支持 {sorted(SELF_REVIEW_POLICIES)}")
        if "self_review" in info and not (has_executor_role(info) and has_review_role(info)):
            raise HubError("config.invalid", f"Agent {agent_id} 的 self_review 仅适用于同时具备执行与复核角色的 Agent")
    authority = config.get("state_authority", {})
    if authority.get("host") not in config["devices"] or not authority.get("database"):
        raise HubError("config.invalid", "state_authority 必须声明已注册 host 和 database")
    if authority.get("policy") not in {None, "single_authority_local"}:
        raise HubError("config.invalid", "state_authority.policy 仅支持 single_authority_local")
    execution = config.get("execution", {})
    if not execution.get("repository") or not execution.get("worktree_root"):
        raise HubError("config.invalid", "execution 必须声明 repository 和 worktree_root")
    validate_execution_config(execution)
    limit = project_task_limit(config)
    for agent_id, info in config["agents"].items():
        override = info.get("max_active_tasks")
        if override is None:
            continue
        if "executor" not in info["roles"]:
            raise HubError("config.invalid", f"Agent {agent_id} 不是执行者，不能声明 max_active_tasks")
        if type(override) is not int or not 1 <= override <= MAX_ACTIVE_TASKS_CEILING:
            raise HubError("config.invalid", f"Agent {agent_id} 的 max_active_tasks 必须是 1..{MAX_ACTIVE_TASKS_CEILING} 的整数")
        if override > limit:
            raise HubError("config.invalid", f"Agent {agent_id} 的 max_active_tasks 不能超过项目上限 {limit}")
    remote = config.get("remote", {})
    if remote.get("enabled"):
        if not remote.get("ssh_host") or not remote.get("project_root") or not remote.get("launcher"):
            raise HubError("config.invalid", "远端项目必须声明 ssh_host、project_root 和 launcher")
        if remote.get("shell") not in {"windows_cmd", "posix_sh"}:
            raise HubError("config.invalid", "remote.shell 仅允许 windows_cmd 或 posix_sh")
    naming = config.get("task_naming", {})
    if naming:
        task_pattern = naming.get("task_pattern")
        chain_pattern = naming.get("chain_pattern")
        seq_digits = naming.get("seq_digits")
        if not isinstance(task_pattern, str) or not isinstance(chain_pattern, str):
            raise HubError("config.invalid", "task_naming 必须包含 task_pattern 和 chain_pattern 字符串")
        if type(seq_digits) is not int or not 3 <= seq_digits <= 6:
            raise HubError("config.invalid", "task_naming.seq_digits 必须是 3-6 的整数")
        try:
            task_re = re.compile(task_pattern)
            chain_re = re.compile(chain_pattern)
        except re.error as exc:
            raise HubError("config.invalid", f"task_naming 正则无效: {exc}") from exc
        if "seq" not in task_re.groupindex:
            raise HubError("config.invalid", "task_pattern 必须包含命名捕获组 (?P<seq>...)")
        if "seq" not in chain_re.groupindex:
            raise HubError("config.invalid", "chain_pattern 必须包含命名捕获组 (?P<seq>...)")
    deployment = config.get("deployment", {})
    expected_adapter = {"windows_cmd": "ssh_atomic_windows", "posix_sh": "ssh_atomic_posix"}.get(remote.get("shell"))
    if remote.get("enabled") and deployment.get("adapter") != expected_adapter:
        raise HubError("config.invalid", f"deployment.adapter 必须与 remote.shell 匹配: {expected_adapter}")
    managed = deployment.get("managed_files")
    if remote.get("enabled") and (not isinstance(managed, list) or not managed):
        raise HubError("config.invalid", "远端项目必须声明 deployment.managed_files")
    return config


def split_command(argv: list[str]) -> list[str]:
    """Return the subcommand suffix of an argv that starts with global options.

    This replaces the old "find the first token that happens to be a command" heuristic:
    a global option value that equals a command name no longer confuses the proxy.
    """
    value_options = {"--project", "--config", "--state", "--device", "--session"}
    flag_options = {"--local", "--text", "--json"}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in value_options:
            index += 2
            continue
        if token in flag_options:
            index += 1
            continue
        if any(token.startswith(option + "=") for option in value_options):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return argv[index:]
    raise HubError("input.invalid", "缺少命令")


def proxy_to_authority(config: dict[str, Any], device: str, argv: list[str], session: str | None,
                       text_mode: bool) -> int:
    remote = config.get("remote") or {}
    host = remote.get("ssh_host")
    root = remote.get("project_root")
    launcher = remote.get("launcher", "agenthub.cmd")
    if not host or not root:
        raise HubError("config.invalid", "项目未配置远端状态权威主机")
    payload = json_dump({
        "device": device, "session": session, "text": text_mode,
        "argv": split_command(argv),
    })
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    shell = remote.get("shell")
    if shell == "windows_cmd":
        remote_command = f'cmd /d /c "cd /d {root} && {launcher} rpc {encoded}"'
    elif shell == "posix_sh":
        remote_command = f"cd -- {shlex.quote(root)} && {shlex.quote('./' + launcher)} rpc {shlex.quote(encoded)}"
    else:
        raise HubError("config.invalid", "项目未配置受支持的 remote.shell")
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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _read_schema_version(conn: sqlite3.Connection) -> int | None:
    if not _table_exists(conn, "metadata"):
        return None
    row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if not row:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _detect_schema(conn: sqlite3.Connection) -> int | None:
    if not _table_exists(conn, "tasks"):
        return None
    columns = _columns(conn, "tasks")
    if "contract_json" in columns:
        return 5
    if "instructions_json" in columns:
        return 4
    raise HubError("state.migration", "无法识别 tasks 表结构")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 50,
            assignee TEXT NOT NULL,
            decision_key TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            branch TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            contract_json TEXT NOT NULL,
            approved_submission_id INTEGER,
            chain_id TEXT,
            supersedes TEXT,
            superseded_by TEXT,
            created_by TEXT NOT NULL,
            claimed_by TEXT,
            active_device TEXT,
            claim_session TEXT,
            last_heartbeat_at TEXT,
            lease_seconds INTEGER NOT NULL DEFAULT 0,
            claim_generation INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            cancelled_at TEXT,
            cancelled_by TEXT,
            cancel_reason TEXT
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
        """
    )
    create_indexes(conn)
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('agenthub_version',?)", (VERSION,))
    conn.commit()


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_decision ON tasks(decision_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_chain ON tasks(chain_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id, event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, checkpoint_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_task ON submissions(task_id, submission_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_revisions_key ON decision_revisions(decision_key, revision_id)")


def prepare_database(conn: sqlite3.Connection, path: Path) -> dict[str, Any] | None:
    version = _read_schema_version(conn)
    if version is None:
        version = _detect_schema(conn)
    if version is None:
        create_schema(conn)
        return None
    if version == SCHEMA_VERSION:
        conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        create_indexes(conn)
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('agenthub_version',?)", (VERSION,))
        conn.commit()
        return None
    if version == 4:
        return migrate_v4_to_v5(conn, path)
    raise HubError(
        "state.migration",
        f"状态数据库 schema {version} 高于当前运行时支持的 {SCHEMA_VERSION}；请使用匹配的运行时",
    )


def _backup_path(path: Path) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.v4-{stamp}.bak")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.v4-{stamp}-{counter}.bak")
        counter += 1
    return candidate


def backup_database(path: Path) -> Path:
    target = _backup_path(path)
    source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _parse_column(row: sqlite3.Row, name: str, default: Any) -> Any:
    if name not in row.keys():
        return default
    raw = row[name]
    if raw is None:
        return default
    return load_state_json(raw, f"tasks[{row['task_id']}].{name}")


def _migrated_contract(row: sqlite3.Row) -> dict[str, Any]:
    legacy = {
        "scope": _parse_column(row, "scope_json", []),
        "constraints": _parse_column(row, "constraints_json", []),
        "validation": _parse_column(row, "validation_json", []),
        "deliverables": _parse_column(row, "deliverables_json", []),
        "evidence_requirements": _parse_column(row, "evidence_requirements_json", []),
        "blocked_protocol": row["blocked_protocol"] if "blocked_protocol" in row.keys() else "",
        "context_budget": row["context_budget"] if "context_budget" in row.keys() else 6000,
    }
    return {
        "id": row["task_id"],
        "title": row["title"],
        "objective": row["objective"],
        "assignee": row["assignee"],
        "decision_key": row["decision_key"],
        "base_commit": row["base_commit"],
        "instructions": _parse_column(row, "instructions_json", []),
        "acceptance": _parse_column(row, "acceptance_json", []),
        "allowed_paths": _parse_column(row, "allowed_paths_json", []),
        "forbidden_paths": _parse_column(row, "forbidden_paths_json", []),
        "depends_on": _parse_column(row, "depends_on_json", []),
        "gates": _parse_column(row, "gates_json", []),
        "artifacts": _parse_column(row, "artifacts_json", []),
        "allow_path_overlap": bool(row["allow_path_overlap"]) if "allow_path_overlap" in row.keys() else False,
        "priority": row["priority"],
        "chain_id": row["chain_id"] or "" if "chain_id" in row.keys() else "",
        "legacy": legacy,
    }


def _synthetic_submission(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    result_raw = row["result_json"] if "result_json" in row.keys() else None
    if not result_raw:
        raise HubError("state.migration", f"任务 {row['task_id']} 缺少结果数据，无法迁移")
    result = load_state_json(result_raw, f"tasks[{row['task_id']}].result")
    outcome = result.get("outcome") if isinstance(result, dict) else None
    if outcome not in {"completed", "blocked"}:
        outcome = "completed"
    agent = row["claimed_by"] or row["created_by"]
    device = row["active_device"] or ""
    cursor = conn.execute(
        "INSERT INTO submissions(task_id,agent_id,device,outcome,result_json,created_at) VALUES(?,?,?,?,?,?)",
        (row["task_id"], agent, device, outcome, result_raw, row["updated_at"]),
    )
    return int(cursor.lastrowid)


def migrate_v4_to_v5(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    backup: Path | None = None
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = _read_schema_version(conn) or _detect_schema(conn)
        if version == SCHEMA_VERSION:
            conn.rollback()
            return None
        if version != 4:
            raise HubError("state.migration", f"无法从 schema {version} 迁移到 {SCHEMA_VERSION}")
        backup = backup_database(path)
        rows = conn.execute("SELECT * FROM tasks").fetchall()
        conn.execute(
            """
            CREATE TABLE tasks_v5 (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 50,
                assignee TEXT NOT NULL,
                decision_key TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                branch TEXT NOT NULL,
                worktree_path TEXT NOT NULL,
                contract_json TEXT NOT NULL,
                approved_submission_id INTEGER,
                chain_id TEXT,
                supersedes TEXT,
                superseded_by TEXT,
                created_by TEXT NOT NULL,
                claimed_by TEXT,
                active_device TEXT,
                claim_session TEXT,
                last_heartbeat_at TEXT,
                lease_seconds INTEGER NOT NULL DEFAULT 0,
                claim_generation INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                cancelled_at TEXT,
                cancelled_by TEXT,
                cancel_reason TEXT
            )
            """
        )
        for row in rows:
            contract = _migrated_contract(row)
            approved: int | None = None
            if row["status"] in {"approved", "closed"}:
                submission = conn.execute(
                    "SELECT submission_id FROM submissions WHERE task_id=? ORDER BY submission_id DESC LIMIT 1",
                    (row["task_id"],),
                ).fetchone()
                if submission:
                    approved = int(submission["submission_id"])
                elif "result_json" in row.keys() and row["result_json"]:
                    approved = _synthetic_submission(conn, row)
            conn.execute(
                "INSERT INTO tasks_v5(task_id,title,status,priority,assignee,decision_key,base_commit,"
                "branch,worktree_path,contract_json,approved_submission_id,chain_id,supersedes,superseded_by,"
                "created_by,claimed_by,active_device,claim_session,last_heartbeat_at,lease_seconds,"
                "claim_generation,created_at,updated_at,cancelled_at,cancelled_by,cancel_reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["task_id"], row["title"], row["status"], row["priority"], row["assignee"],
                    row["decision_key"], row["base_commit"], row["branch"], row["worktree_path"],
                    json_dump(contract), approved,
                    row["chain_id"] if "chain_id" in row.keys() else None,
                    row["supersedes"] if "supersedes" in row.keys() else None,
                    row["superseded_by"] if "superseded_by" in row.keys() else None,
                    row["created_by"], row["claimed_by"],
                    row["active_device"] if "active_device" in row.keys() else None,
                    row["claim_session"] if "claim_session" in row.keys() else None,
                    row["last_heartbeat_at"] if "last_heartbeat_at" in row.keys() else None,
                    row["lease_seconds"] if "lease_seconds" in row.keys() else 0,
                    row["claim_generation"] if "claim_generation" in row.keys() else 0,
                    row["created_at"], row["updated_at"],
                    row["cancelled_at"] if "cancelled_at" in row.keys() else None,
                    row["cancelled_by"] if "cancelled_by" in row.keys() else None,
                    row["cancel_reason"] if "cancel_reason" in row.keys() else None,
                ),
            )
        conn.execute("DROP TABLE tasks")
        conn.execute("ALTER TABLE tasks_v5 RENAME TO tasks")
        create_indexes(conn)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise HubError("state.migration", f"迁移后外键校验失败: {len(violations)} 行")
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('agenthub_version',?)", (VERSION,))
        conn.commit()
        return {"from": 4, "to": SCHEMA_VERSION, "backup": str(backup), "tasks": len(rows)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def record(conn: sqlite3.Connection, agent: str, device: str, action: str, entity: str,
           detail: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO events(occurred_at,agent_id,device,action,entity_id,detail_json) VALUES(?,?,?,?,?,?)",
        (now(), agent, device, action, entity, json_dump(detail)),
    )


def identity(config: dict[str, Any], agent: str, device: str, action: str) -> dict[str, Any]:
    info = config["agents"].get(agent)
    if not info or not info.get("enabled", True):
        raise HubError("identity.unknown", f"未启用的 Agent: {agent}")
    if device not in config["devices"]:
        raise HubError("identity.device", f"未知设备: {device}")
    if device not in info.get("devices", []):
        raise HubError("identity.device", f"Agent {agent} 不允许在设备 {device} 操作")
    roles = set(info.get("roles", []))
    allowed = set().union(*(PERMISSIONS.get(role, set()) for role in roles))
    if action not in allowed:
        raise HubError("identity.permission", f"Agent {agent} 的角色 {sorted(roles)} 无权执行 {action}")
    return info


def require_session(config: dict[str, Any], row: sqlite3.Row, session: str | None, action: str) -> None:
    """Fence an executor action to the window that owns the claim."""
    bound = row["claim_session"]
    if bound:
        if session != bound:
            raise HubError(
                "task.session",
                f"任务 {row['task_id']} 由会话 {bound} 持有；当前会话 {session or '(未声明)'} 不得 {action}",
            )
        return
    if session_policy(config) == "required":
        raise HubError(
            "task.session",
            f"任务 {row['task_id']} 尚未绑定会话；请先在活动设备执行 "
            f"heartbeat {row['claimed_by'] or '<agent>'} {row['task_id']} --session <id>",
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
            "task.capacity",
            f"Agent {agent} 已达到并发上限 {limit}；先 submit 或 release 一个任务",
        )


def required_text(value: dict[str, Any], field: str, max_length: int = 4000) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise HubError("contract.invalid", f"缺少非空字段: {field}")
    result = result.strip()
    if len(result) > max_length:
        raise HubError("contract.invalid", f"字段 {field} 超过 {max_length} 字符")
    return result


def required_list(value: dict[str, Any], field: str) -> list[Any]:
    result = value.get(field)
    if not isinstance(result, list) or not result:
        raise HubError("contract.invalid", f"缺少非空数组: {field}")
    if len(result) > 20 or any(not isinstance(item, str) or not item.strip() for item in result):
        raise HubError("contract.invalid", f"字段 {field} 必须由不超过 20 个非空字符串组成")
    return result


def optional_string_list(value: dict[str, Any], field: str, max_length: int = 500) -> list[str]:
    result = value.get(field, [])
    if result is None:
        return []
    if not isinstance(result, list):
        raise HubError("contract.invalid", f"字段 {field} 必须是数组")
    if len(result) > 20:
        raise HubError("contract.invalid", f"字段 {field} 最多 20 项")
    for item in result:
        if not isinstance(item, str) or not item.strip():
            raise HubError("contract.invalid", f"字段 {field} 的每一项必须是非空字符串")
        if len(item) > max_length:
            raise HubError("contract.invalid", f"字段 {field} 的单项超过 {max_length} 字符")
    return result


def finite_number(value: Any) -> bool:
    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value)


def validate_depends_on(spec: dict[str, Any], task_id: str) -> list[str]:
    raw = spec.get("depends_on", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HubError("contract.invalid", "depends_on 必须是数组")
    if len(raw) > 20:
        raise HubError("contract.invalid", "depends_on 最多 20 项")
    result: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip() or len(item) > 120:
            raise HubError("contract.invalid", f"depends_on[{index}] 必须是 1-120 字符的非空字符串")
        item = item.strip()
        if item == task_id:
            raise HubError("contract.invalid", "任务不能依赖自身")
        if item in result:
            raise HubError("contract.invalid", f"depends_on 重复: {item}")
        result.append(item)
    return result


def validate_gates(spec: dict[str, Any]) -> list[dict[str, Any]]:
    raw = spec.get("gates", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HubError("contract.invalid", "gates 必须是数组")
    if len(raw) > 20:
        raise HubError("contract.invalid", "gates 最多 20 项")
    gates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HubError("contract.invalid", f"gates[{index}] 必须是对象")
        unknown = sorted(set(item) - {"id", "metric", "operator", "threshold", "source"})
        if unknown:
            raise HubError("contract.invalid", f"gates[{index}] 含未知字段: {unknown}")
        gate_id = item.get("id")
        if not isinstance(gate_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", gate_id):
            raise HubError("contract.invalid", f"gates[{index}].id 必须是 1-64 位字母、数字、点、下划线或连字符")
        if gate_id in seen:
            raise HubError("contract.invalid", f"gates id 重复: {gate_id}")
        seen.add(gate_id)
        metric = item.get("metric")
        if not isinstance(metric, str) or not metric.strip() or len(metric) > 200:
            raise HubError("contract.invalid", f"gates[{gate_id}].metric 必须是 1-200 字符的非空字符串")
        operator_value = item.get("operator")
        if not isinstance(operator_value, str) or operator_value not in GATE_OPERATORS:
            raise HubError("contract.invalid", f"gates[{gate_id}].operator 仅允许 {sorted(GATE_OPERATORS)}")
        threshold = item.get("threshold")
        if not finite_number(threshold):
            raise HubError("contract.invalid", f"gates[{gate_id}].threshold 必须是有限数字")
        source = item.get("source")
        if source is not None and (not isinstance(source, str) or len(source) > 500):
            raise HubError("contract.invalid", f"gates[{gate_id}].source 必须是不超过 500 字符的字符串")
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
        raise HubError("contract.invalid", "artifacts 必须是数组")
    if len(raw) > 20:
        raise HubError("contract.invalid", "artifacts 最多 20 项")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item or len(item) > 500:
            raise HubError("contract.invalid", f"artifacts[{index}] 必须是 1-500 字符的非空字符串")
        if "\\" in item or item.startswith("/") or re.match(r"^[A-Za-z]:", item):
            raise HubError("contract.invalid", f"artifacts[{index}] 必须是 artifact_root 下的相对 POSIX 路径: {item}")
        if any(char in item for char in "*?["):
            raise HubError("contract.invalid", f"artifacts[{index}] 不能包含通配符: {item}")
        pure = PurePosixPath(item)
        if pure.is_absolute() or not pure.parts or any(part == ".." for part in pure.parts):
            raise HubError("contract.invalid", f"artifacts[{index}] 不能包含 ..: {item}")
        normalized = pure.as_posix()
        if normalized in seen:
            raise HubError("contract.invalid", f"artifacts 重复: {normalized}")
        seen.add(normalized)
        result.append(normalized)
    return result


def validate_revision(value: str, label: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
        raise HubError("contract.invalid", f"{label} 必须是 7-64 位十六进制提交 ID")
    return value


def validate_branch_name(value: str, label: str) -> str:
    try:
        subprocess.run(
            ["git", "check-ref-format", "--branch", value],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HubError("git.error", f"{label} 不是合法分支名: {value}") from exc
    return value


def git_argv(repository: str, *args: str) -> list[str]:
    return ["git", "-c", "safe.directory=*", "-C", repository, *args]


def git(config: dict[str, Any], *args: str, cwd: str | None = None, check: bool = True) -> str:
    repository = cwd or config["execution"]["repository"]
    result = subprocess.run(git_argv(repository, *args), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    if check and result.returncode:
        raise HubError("git.error", f"git {' '.join(args)} 失败: {result.stderr.strip() or result.returncode}")
    return result.stdout.strip()


def changed_paths(config: dict[str, Any], base: str, commit: str, cwd: str) -> list[str]:
    result = subprocess.run(
        git_argv(cwd, "diff", "--no-renames", "--name-only", "-z", f"{base}..{commit}"),
        capture_output=True, check=False,
    )
    if result.returncode:
        raise HubError("git.error", "无法读取提交路径差异")
    try:
        raw = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HubError("git.error", "提交包含无法可靠解码为 UTF-8 的路径") from exc
    if raw and not raw.endswith("\0"):
        raise HubError("git.error", "Git 路径差异缺少 NUL 分隔符")
    return raw[:-1].split("\0") if raw else []


def git_ok(config: dict[str, Any], *args: str, cwd: str | None = None) -> bool:
    repository = cwd or config["execution"]["repository"]
    result = subprocess.run(git_argv(repository, *args), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    return result.returncode == 0


def glob_prefix_parts(pattern: str) -> list[str]:
    parts: list[str] = []
    for part in pattern.removeprefix("./").split("/"):
        if any(char in part for char in "*?["):
            break
        parts.append(part)
    return parts


def path_patterns_overlap(left: str, right: str) -> bool:
    """Conservative test for whether two allowed_paths patterns can match one path."""
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


def task_contract(row: sqlite3.Row) -> dict[str, Any]:
    contract = load_state_json(row["contract_json"], f"tasks[{row['task_id']}].contract")
    if not isinstance(contract, dict):
        raise HubError("state.corrupt", f"tasks[{row['task_id']}].contract 必须是对象")
    return contract


def dependency_state(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    dependencies = task_contract(row).get("depends_on", [])
    if not isinstance(dependencies, list):
        raise HubError("state.corrupt", f"tasks[{row['task_id']}].depends_on 必须是数组")
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
        row = conn.execute("SELECT contract_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row:
            contract = load_state_json(row["contract_json"], f"tasks[{task_id}].contract")
            dependencies = contract.get("depends_on", []) if isinstance(contract, dict) else []
            if isinstance(dependencies, list):
                pending.extend(item for item in dependencies if isinstance(item, str))
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
        "SELECT task_id,status,contract_json FROM tasks "
        "WHERE status NOT IN ('closed','cancelled') AND task_id != ?", (task_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        contract = load_state_json(row["contract_json"], f"tasks[{row['task_id']}].contract")
        dependencies = contract.get("depends_on", []) if isinstance(contract, dict) else []
        if isinstance(dependencies, list) and task_id in dependencies:
            result.append({"task_id": row["task_id"], "status": row["status"]})
    return result


def evaluate_gates(gates: list[dict[str, Any]], results: Any, require_all: bool) -> list[dict[str, Any]]:
    if not gates:
        if results:
            raise HubError("contract.invalid", "任务合同未声明 gates，但结果包含了 gate_results")
        return []
    if results is None:
        if require_all:
            raise HubError("contract.invalid", "任务声明了 gates，completed 提交必须提供 gate_results")
        return []
    if not isinstance(results, dict):
        raise HubError("contract.invalid", "gate_results 必须是对象")
    declared = {gate["id"]: gate for gate in gates}
    unknown = sorted(set(results) - set(declared))
    if unknown:
        raise HubError("contract.invalid", f"gate_results 含未声明的门禁: {unknown}")
    missing = sorted(set(declared) - set(results))
    if require_all and missing:
        raise HubError("contract.invalid", f"gate_results 缺少门禁: {missing}")
    evaluated: list[dict[str, Any]] = []
    for gate in gates:
        if gate["id"] not in results:
            continue
        value = results[gate["id"]]
        if not finite_number(value):
            raise HubError("contract.invalid", f"gate_results[{gate['id']}] 必须是有限数字")
        evaluated.append({**gate, "value": value,
                          "passed": bool(GATE_COMPARE[gate["operator"]](value, gate["threshold"]))})
    return evaluated


def verify_artifacts(config: dict[str, Any], artifacts: list[str], label: str) -> None:
    if not artifacts:
        return
    root_value = config.get("execution", {}).get("artifact_root")
    if not root_value:
        raise HubError("artifact.missing", f"{label}: 项目未配置 execution.artifact_root，无法校验 artifacts")
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
        raise HubError("artifact.missing", f"{label}: 交付物校验失败（artifact_root={root}）；" + "；".join(parts))


def repository_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "head": git(config, "rev-parse", "HEAD"),
        "branch": git(config, "branch", "--show-current", check=False),
        "dirty_entries": len(git(config, "status", "--porcelain").splitlines()),
    }


def latest_submission(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM submissions WHERE task_id=? ORDER BY submission_id DESC LIMIT 1", (task_id,)
    ).fetchone()


def approved_submission(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
    submission_id = row["approved_submission_id"]
    if not submission_id:
        return None
    return conn.execute("SELECT * FROM submissions WHERE submission_id=?", (submission_id,)).fetchone()


def submission_result(row: sqlite3.Row) -> dict[str, Any] | None:
    result = load_state_json(row["result_json"], f"submissions[{row['submission_id']}].result")
    return result if isinstance(result, dict) else None


def current_submission(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
    return approved_submission(conn, row) or latest_submission(conn, row["task_id"])


def current_result(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any] | None:
    submission = current_submission(conn, row)
    return submission_result(submission) if submission else None


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
        "SELECT action,detail_json,occurred_at,agent_id FROM events WHERE entity_id=? "
        "AND (action='task.review.approve' OR action='task.review.rework') "
        "ORDER BY event_id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    detail = load_state_json(row["detail_json"], f"events[{task_id}].review.detail")
    return {
        "action": "approve" if "approve" in row["action"] else "rework",
        "note": detail.get("note", ""),
        "agent": row["agent_id"],
        "self_review": bool(detail.get("self_review", False)),
        "submission_id": detail.get("submission_id"),
        "occurred_at": row["occurred_at"],
    }


def task_summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    lease = lease_state(row)
    contract = task_contract(row)
    summary = {
        "id": row["task_id"], "title": row["title"], "priority": row["priority"],
        "status": row["status"], "assignee": row["assignee"],
        "decision_key": row["decision_key"], "claimed_by": row["claimed_by"],
        "active_device": row["active_device"], "claim_session": row["claim_session"],
        "claim_generation": row["claim_generation"], "lease_expired": lease["expired"],
        "lease": lease, "chain_id": row["chain_id"],
        "depends_on": contract.get("depends_on", []),
        "supersedes": row["supersedes"], "superseded_by": row["superseded_by"],
        "branch": row["branch"], "base_commit": row["base_commit"], "updated_at": row["updated_at"],
    }
    result = current_result(conn, row)
    computed = result.get("gates") if isinstance(result, dict) else None
    if isinstance(computed, list) and computed:
        passed = sum(1 for gate in computed if isinstance(gate, dict) and gate.get("passed") is True)
        summary["gate_status"] = {"total": len(computed), "passed": passed}
    return summary


def review_queue_entry(row: sqlite3.Row, agent: str) -> dict[str, Any]:
    """A compact action item; the full task stays available through `show`."""
    return {
        "id": row["task_id"], "title": row["title"], "priority": row["priority"],
        "status": row["status"], "assignee": row["assignee"], "claimed_by": row["claimed_by"],
        "claim_session": row["claim_session"], "decision_key": row["decision_key"],
        "self_review": bool(row["claimed_by"] and row["claimed_by"] == agent),
        "updated_at": row["updated_at"],
    }


def task_packet(conn: sqlite3.Connection, row: sqlite3.Row, config: dict[str, Any],
                session: str | None = None) -> dict[str, Any]:
    generation = row["claim_generation"]
    contract = task_contract(row)
    packet = task_summary(conn, row) | {
        "contract": contract,
        "worktree_path": row["worktree_path"],
        "ownership": {
            "session": row["claim_session"],
            "mine": bool(session and row["claim_session"] == session),
            "session_policy": session_policy(config),
        },
        "checkpoint": latest_checkpoint(conn, row["task_id"], generation),
        "latest_review": latest_review(conn, row["task_id"]),
        "dependents": dependents_of(conn, row["task_id"]),
        "result": current_result(conn, row),
    }
    dependencies = dependency_state(conn, row)
    dependencies["stale_base"] = dependency_freshness(conn, config, row, dependencies)
    packet["dependencies"] = dependencies
    if config.get("execution", {}).get("artifact_root"):
        packet["artifact_root"] = config["execution"]["artifact_root"]
    older = previous_checkpoint(conn, row["task_id"], generation)
    if older:
        packet["previous_checkpoint"] = older
    if Path(row["worktree_path"]).exists():
        try:
            packet["worktree"] = {
                "head": git(config, "rev-parse", "HEAD", cwd=row["worktree_path"]),
                "dirty_entries": len(git(config, "status", "--porcelain", cwd=row["worktree_path"]).splitlines()),
            }
        except (HubError, OSError) as exc:
            packet["worktree"] = {"error": str(exc)}
    return packet


def submission_history(conn: sqlite3.Connection, task_id: str, limit: int = 3) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT submission_id,agent_id,device,outcome,result_json,created_at FROM submissions "
        "WHERE task_id=? ORDER BY submission_id DESC LIMIT ?", (task_id, limit),
    ).fetchall()
    return [
        {
            "submission_id": row["submission_id"], "agent": row["agent_id"],
            "device": row["device"], "outcome": row["outcome"],
            "result": load_state_json(row["result_json"], f"submissions[{row['submission_id']}].result"),
            "created_at": row["created_at"],
        }
        for row in rows
    ]



def task_worktree_path(config: dict[str, Any], task_id: str) -> str:
    """Resolve a task's worktree without letting the task ID escape worktree_root."""
    root_value = config.get("execution", {}).get("worktree_root")
    if not root_value:
        raise HubError("config.invalid", "项目未配置 execution.worktree_root")
    if task_id in {".", ".."} or task_id.startswith(".") or any(char in task_id for char in "/\\\0"):
        raise HubError("contract.invalid", f"任务 id 必须是单一目录名，不能包含路径分隔符或前导点: {task_id!r}")
    root = Path(root_value).expanduser().resolve()
    candidate = (root / task_id).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise HubError("contract.invalid", f"任务 worktree 路径越界: {candidate} 不在 {root} 之下")
    return str(candidate)


def validate_task_contract(config: dict[str, Any], raw: str) -> dict[str, Any]:
    limit = max_contract_bytes(config)
    spec = read_json_argument(raw, "任务合同", limit)
    unknown = sorted(set(spec) - CONTRACT_FIELDS)
    if unknown:
        raise HubError("contract.invalid", f"合同含未知字段: {unknown}")
    task_id = required_text(spec, "id", 120)
    chain_id = spec.get("chain_id", "")
    if not isinstance(chain_id, str):
        raise HubError("contract.invalid", "chain_id 必须是字符串")
    title = required_text(spec, "title", 200)
    objective = required_text(spec, "objective")
    assignee = required_text(spec, "assignee", 80)
    assignee_info = config["agents"].get(assignee, {})
    if "executor" not in assignee_info.get("roles", []) or not assignee_info.get("enabled", True):
        raise HubError("contract.invalid", f"assignee 必须是启用的执行者: {assignee}")
    decision_key = required_text(spec, "decision_key", 160)
    base_commit = required_text(spec, "base_commit", 80)
    validate_revision(base_commit, "base_commit")
    instructions = required_list(spec, "instructions")
    acceptance = required_list(spec, "acceptance")
    allowed_paths = required_list(spec, "allowed_paths")
    forbidden_paths = optional_string_list(spec, "forbidden_paths")
    depends_on = validate_depends_on(spec, task_id)
    gates = validate_gates(spec)
    artifacts = validate_artifacts(spec)
    if artifacts and not config.get("execution", {}).get("artifact_root"):
        raise HubError("contract.invalid", "任务声明了 artifacts，但项目未配置 execution.artifact_root")
    allow_path_overlap = spec.get("allow_path_overlap", False)
    if not isinstance(allow_path_overlap, bool):
        raise HubError("contract.invalid", "allow_path_overlap 必须是布尔值")
    priority = spec.get("priority", 50)
    if type(priority) is not int or not 1 <= priority <= 99:
        raise HubError("contract.invalid", "priority 必须是 1..99 的整数")
    naming = config.get("task_naming", {})
    if naming:
        if not re.fullmatch(naming["task_pattern"], task_id):
            raise HubError("contract.invalid", f"任务 id 不符合项目命名规则 {naming['task_pattern']}: {task_id}")
        if not chain_id:
            raise HubError("contract.invalid", "任务必须指定 chain_id")
        if not re.fullmatch(naming["chain_pattern"], chain_id):
            raise HubError("contract.invalid", f"chain_id 不符合项目命名规则 {naming['chain_pattern']}: {chain_id}")
    elif not re.fullmatch(r"[A-Za-z0-9._-]+", task_id):
        raise HubError("contract.invalid", "任务 id 只能包含字母、数字、点、下划线和连字符")
    contract = {
        "id": task_id, "title": title, "objective": objective, "assignee": assignee,
        "decision_key": decision_key, "base_commit": base_commit,
        "instructions": instructions, "acceptance": acceptance,
        "allowed_paths": allowed_paths, "forbidden_paths": forbidden_paths,
        "depends_on": depends_on, "gates": gates, "artifacts": artifacts,
        "allow_path_overlap": allow_path_overlap, "priority": priority, "chain_id": chain_id,
    }
    if len(json_dump(contract).encode("utf-8")) > limit:
        raise HubError("contract.budget", f"任务合同超过 {limit} 字节；请压缩描述并只保留必要执行信息")
    branch = f"agenthub/{task_id}"
    worktree_path = task_worktree_path(config, task_id)
    return contract | {"branch": branch, "worktree_path": worktree_path}


def check_task_constraints(conn: sqlite3.Connection, config: dict[str, Any], spec: dict[str, Any],
                           replacing: str | None = None) -> list[dict[str, Any]]:
    task_id, chain_id = spec["id"], spec["chain_id"]
    if conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
        raise HubError("contract.invalid", f"任务 ID 已存在: {task_id}")
    naming = config.get("task_naming", {})
    if naming:
        conflict = conn.execute(
            "SELECT 1 FROM tasks WHERE chain_id=? AND status NOT IN ('closed','cancelled') AND task_id != ?",
            (chain_id, replacing or ""),
        ).fetchone()
        if conflict:
            raise HubError("contract.invalid", f"active chain_id 已存在: {chain_id}")
        match = re.fullmatch(naming["task_pattern"], task_id)
        if not match or "seq" not in match.groupdict():
            raise HubError("contract.invalid", f"任务 id 缺少序号: {task_id}")
        try:
            candidate = int(match.group("seq"))
        except (TypeError, ValueError) as exc:
            raise HubError("contract.invalid", f"任务 id 的序号不是数字: {task_id}") from exc
        existing_seqs = set()
        for row in conn.execute("SELECT task_id FROM tasks"):
            existing = re.fullmatch(naming["task_pattern"], row["task_id"])
            if existing and "seq" in existing.groupdict():
                try:
                    existing_seqs.add(int(existing.group("seq")))
                except ValueError:
                    continue
        if candidate in existing_seqs:
            raise HubError("contract.invalid", f"序号 {candidate:0{naming['seq_digits']}d} 已被占用（含已取消任务）")
        expected = max(existing_seqs) + 1 if existing_seqs else 1
        if candidate != expected:
            raise HubError(
                "contract.invalid",
                f"下一可用序号应为 {expected:0{naming['seq_digits']}d}，但收到 {candidate:0{naming['seq_digits']}d}",
            )
    if not conn.execute("SELECT 1 FROM decisions WHERE decision_key=?", (spec["decision_key"],)).fetchone():
        raise HubError("contract.invalid", f"任务必须关联已存在的决策: {spec['decision_key']}")
    if replacing and replacing in spec["depends_on"]:
        raise HubError("contract.invalid", f"新合同不能依赖正在被替代的任务 {replacing}")
    for dependency in spec["depends_on"]:
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (dependency,)).fetchone()
        if not row:
            raise HubError("dependency.unmet", f"depends_on 引用了不存在的任务: {dependency}")
        if row["status"] == "cancelled":
            raise HubError("dependency.unmet", f"depends_on 引用已取消的任务: {dependency}")
    git(config, "rev-parse", "--verify", f"{spec['base_commit']}^{{commit}}")
    closure = dependency_closure(conn, spec["depends_on"])
    active = conn.execute(
        "SELECT task_id,chain_id,contract_json FROM tasks "
        "WHERE status NOT IN ('closed','cancelled') AND task_id != ?",
        (replacing or "",),
    ).fetchall()
    acknowledgements: list[dict[str, Any]] = []
    for other in active:
        if other["task_id"] in closure:
            continue
        if spec["chain_id"] and other["chain_id"] == spec["chain_id"]:
            continue
        other_contract = load_state_json(other["contract_json"], f"tasks[{other['task_id']}].contract")
        other_paths = other_contract.get("allowed_paths", []) if isinstance(other_contract, dict) else []
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
                "path.overlap",
                f"任务 {spec['id']} 的 allowed_paths 与活动任务 {other['task_id']} 重叠: {detail}；"
                "如需并发请声明 depends_on，或在合同中显式设置 allow_path_overlap: true",
            )
        acknowledgements.append({"task_id": other["task_id"], "overlaps": overlaps})
    return acknowledgements


def insert_task(conn: sqlite3.Connection, config: dict[str, Any], agent: str, spec: dict[str, Any],
                stamp: str, supersedes: str | None = None) -> list[dict[str, Any]]:
    acknowledgements = check_task_constraints(conn, config, spec, replacing=supersedes)
    contract = {field: spec[field] for field in CONTRACT_FIELDS if field in spec}
    values = {
        "task_id": spec["id"], "title": spec["title"], "priority": spec["priority"],
        "assignee": spec["assignee"], "decision_key": spec["decision_key"],
        "base_commit": spec["base_commit"], "branch": spec["branch"],
        "worktree_path": spec["worktree_path"], "contract_json": json_dump(contract),
        "status": "ready", "created_by": agent, "created_at": stamp, "updated_at": stamp,
        "chain_id": spec["chain_id"], "supersedes": supersedes,
    }
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    conn.execute(f"INSERT INTO tasks({columns}) VALUES({placeholders})", tuple(values.values()))
    return acknowledgements


def load_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        raise HubError("task.not_found", f"任务不存在: {task_id}")
    return row


def ensure_worktree(config: dict[str, Any], row: sqlite3.Row) -> None:
    path = Path(row["worktree_path"])
    if path.exists():
        if not (path / ".git").exists():
            raise HubError("git.error", f"worktree 路径已存在但不是 Git worktree: {path}")
        current = git(config, "branch", "--show-current", cwd=str(path))
        if current != row["branch"]:
            raise HubError("git.error", f"worktree 分支不一致: {current} != {row['branch']}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = bool(git(config, "show-ref", "--verify", f"refs/heads/{row['branch']}", check=False))
    args = ["worktree", "add"]
    if not branch_exists:
        args.extend(["-b", row["branch"]])
    args.extend([str(path), row["branch"] if branch_exists else row["base_commit"]])
    git(config, *args)


def _status_counts(conn: sqlite3.Connection, info: dict[str, Any], agent: str) -> dict[str, int]:
    clauses = ["status IN ('ready','in_progress','blocked','review','approved')"]
    params: list[Any] = []
    if has_executor_role(info) and not has_review_role(info):
        clauses.append("assignee=?")
        params.append(agent)
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE " + " AND ".join(clauses) + " GROUP BY status",
        params,
    ).fetchall()
    counts = {status: 0 for status in ("ready", "in_progress", "blocked", "review", "approved")}
    for row in rows:
        counts[row["status"]] = row["n"]
    return counts


def do_status(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, all_tasks: bool,
              mine: bool = False, assignee: str | None = None, decision: str | None = None) -> dict[str, Any]:
    info = identity(config, agent, device, "status")
    if assignee is not None and assignee not in config["agents"]:
        raise HubError("identity.unknown", f"未知 Agent: {assignee}")
    if not has_review_role(info):
        if assignee is not None and assignee != agent:
            raise HubError("identity.permission", "仅执行角色的 Agent 只能查看分配给自己的任务")
        target = agent
    else:
        target = agent if mine else assignee
    if decision is not None and not conn.execute(
        "SELECT 1 FROM decisions WHERE decision_key=?", (decision,)
    ).fetchone():
        raise HubError("contract.invalid", f"未知决策: {decision}")
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
    tasks: list[dict[str, Any]] = []
    for row in rows:
        summary = task_summary(conn, row)
        state = dependency_state(conn, row)
        summary["dependency"] = {"satisfied": state["satisfied"], "unmet": state["unmet"]}
        tasks.append(summary)
    return {
        "project": config["project_id"], "version": VERSION,
        "repository": repository_snapshot(config),
        "filter": {"assignee": target, "decision": decision},
        "tasks": tasks,
    }


def executor_task_row(
    conn: sqlite3.Connection, config: dict[str, Any], agent: str, session: str | None, available: bool = False,
) -> tuple[sqlite3.Row | None, list[dict[str, Any]]]:
    """Pick one task for a window without ever handing it another window's claim."""
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


def do_context(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str,
               session: str | None) -> dict[str, Any]:
    info = identity(config, agent, device, "context")
    roles = agent_roles(info)
    executor = "executor" in roles
    reviewer = has_review_role(info)
    role_mode = "hybrid" if executor and reviewer else ("executor" if executor else "decider")
    limit = task_limit(config, agent) if executor else None
    active = len(active_task_ids(conn, agent)) if executor else None
    excluded: list[dict[str, Any]] = []
    focus: dict[str, Any] | None = None
    if executor:
        row, excluded = executor_task_row(conn, config, agent, session)
        if row:
            focus = task_packet(conn, row, config, session)
    decisions = [dict(row) for row in conn.execute(
        "SELECT decision_key,summary,rationale,decided_by,updated_at FROM decisions "
        "ORDER BY updated_at DESC LIMIT 5"
    )]
    if role_mode == "hybrid":
        rule = (
            f"该 Agent 同时具备决策与执行角色：最多同时推进 {limit} 个活动任务；"
            f"提交后在 review_queue 复核（self_review={self_review_policy(config, agent)}）；"
            "换设备必须 checkpoint 后 handoff。"
        )
    elif executor:
        if limit == 1:
            rule = "决策者发布不可变执行合同并复核；执行者一次只推进一个活动任务；换设备必须 checkpoint 后 handoff。"
        else:
            rule = (
                f"决策者发布不可变执行合同并复核；执行者最多同时推进 {limit} 个活动任务，"
                "用 claim 或 next --available 领取新任务；换设备必须 checkpoint 后 handoff。"
            )
    else:
        rule = "决策者发布不可变执行合同并复核；执行者按项目并发上限推进任务；换设备必须 checkpoint 后 handoff。"
    payload: dict[str, Any] = {
        "project": config["project_id"], "agent": agent, "roles": info["roles"], "device": device,
        "role_mode": role_mode, "session": session, "session_policy": session_policy(config),
        "repository": repository_snapshot(config),
        "operating_rule": rule,
        "focus": focus,
        "counts": _status_counts(conn, info, agent),
        "decisions": decisions,
        "excluded": excluded,
        "detail_rule": "blocked/review 必须 show 后决策；approved 等待用户确认合并，再由具备 close 权限的 Agent 关闭。",
        "warnings": [],
    }
    if reviewer:
        queue_rows = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('blocked','review') "
            "ORDER BY priority,created_at,task_id LIMIT 25"
        ).fetchall()
        payload["review_queue"] = [review_queue_entry(queue_row, agent) for queue_row in queue_rows]
    if role_mode == "hybrid":
        payload["self_review"] = {"policy": self_review_policy(config, agent), "capable": True}
    if executor:
        payload["concurrency"] = {"active": active, "limit": limit}
        if session_policy(config) == "required" and not session:
            payload["session_required"] = True
    return payload


def do_next(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str,
            session: str | None, available: bool = False) -> dict[str, Any]:
    identity(config, agent, device, "next")
    row, excluded = executor_task_row(conn, config, agent, session, available)
    packet = task_packet(conn, row, config, session) if row else None
    if packet and row is not None:
        packet["device_access"] = (
            "active" if not row["active_device"] or row["active_device"] == device
            else f"handoff_required_from:{row['active_device']}"
        )
    payload: dict[str, Any] = {
        "task": packet,
        "concurrency": {"active": len(active_task_ids(conn, agent)), "limit": task_limit(config, agent)},
        "session": session, "session_policy": session_policy(config), "excluded": excluded,
        "warnings": [],
    }
    if session_policy(config) == "required" and not session:
        payload["session_required"] = True
    return payload


def do_show(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
            session: str | None = None, submissions: int = 3) -> dict[str, Any]:
    info = identity(config, agent, device, "show")
    row = load_task(conn, task_id)
    if has_executor_role(info) and not has_review_role(info) and row["assignee"] != agent:
        raise HubError("identity.permission", "仅执行角色的 Agent 只能查看分配给自己的任务")
    if submissions < 0 or submissions > MAX_SUBMISSIONS:
        raise HubError("input.invalid", f"--submissions 必须是 0..{MAX_SUBMISSIONS} 的整数")
    return {
        "task": task_packet(conn, row, config, session),
        "submissions": submission_history(conn, task_id, submissions),
    }


def do_post(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, raw: str) -> dict[str, Any]:
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
    warnings = []
    if acknowledgements:
        warnings.append({
            "code": "path.overlap.ack",
            "message": "allowed_paths 与其他活动任务重叠，已按 allow_path_overlap 放行",
            "acknowledgements": acknowledgements,
        })
    return {"id": spec["id"], "status": "ready", "warnings": warnings}


def claim_task(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, row: sqlite3.Row,
               session: str | None, lease_minutes: int | None) -> dict[str, Any]:
    task_id = row["task_id"]
    if row["assignee"] != agent:
        raise HubError("identity.permission", f"任务 {task_id} 已指定给 {row['assignee']}，{agent} 不得认领")
    if row["status"] != "ready" or row["claimed_by"]:
        raise HubError("task.state", f"任务 {task_id} 当前不可认领: {row['status']}")
    if session_policy(config) == "required" and not session:
        raise HubError("task.session", "execution.session_policy=required：claim 必须带 --session <id>")
    dependencies = dependency_state(conn, row)
    if not dependencies["satisfied"]:
        detail = ", ".join(f"{item['task_id']}({item['status']})" for item in dependencies["unmet"])
        raise HubError("dependency.unmet", f"任务 {task_id} 的依赖尚未全部 closed: {detail}")
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
    return task_packet(conn, updated, config, session)


def do_claim(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str | None,
             session: str | None, lease_minutes: int | None) -> dict[str, Any]:
    identity(config, agent, device, "claim")
    packet: dict[str, Any] | None = None
    skipped: list[dict[str, Any]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        if task_id:
            packet = claim_task(conn, config, agent, device, load_task(conn, task_id), session, lease_minutes)
        else:
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
    warnings: list[dict[str, Any]] = []
    if packet:
        stale = packet.get("dependencies", {}).get("stale_base", [])
        if stale:
            warnings.append({
                "code": "dependency.stale_base",
                "message": "base_commit 不包含依赖的合并提交",
                "stale_base": stale,
            })
    return {
        "task": packet, "skipped": skipped,
        "concurrency": {"active": len(active_task_ids(conn, agent)), "limit": task_limit(config, agent)},
        "session": session, "warnings": warnings,
    }


def clear_claim(conn: sqlite3.Connection, task_id: str, status: str, stamp: str, **extra: Any) -> None:
    values: dict[str, Any] = {
        "status": status, "claimed_by": None, "active_device": None, "claim_session": None,
        "last_heartbeat_at": None, "lease_seconds": 0, "updated_at": stamp,
    }
    values.update(extra)
    statement = "UPDATE tasks SET " + ",".join(f"{key}=?" for key in values) + " WHERE task_id=?"
    cursor = conn.execute(statement, (*values.values(), task_id))
    if cursor.rowcount != 1:
        raise HubError("state.conflict", "任务状态已被其他命令改变，操作失败")


def touch_claim(conn: sqlite3.Connection, task_id: str, stamp: str, **extra: Any) -> None:
    values: dict[str, Any] = {"last_heartbeat_at": stamp, "updated_at": stamp}
    values.update(extra)
    statement = "UPDATE tasks SET " + ",".join(f"{key}=?" for key in values) + " WHERE task_id=?"
    cursor = conn.execute(statement, (*values.values(), task_id))
    if cursor.rowcount != 1:
        raise HubError("state.conflict", "任务状态已被其他命令改变，操作失败")


def do_release(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
               reason: str, session: str | None, if_expired: bool = False) -> dict[str, Any]:
    info = identity(config, agent, device, "release")
    if not reason.strip():
        raise HubError("input.invalid", "释放原因不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if row["status"] != "in_progress":
            raise HubError("task.state", f"只有 in_progress 任务可以释放，当前状态: {row['status']}")
        administrator = "administrator" in set(info.get("roles", []))
        forced = row["claimed_by"] != agent
        if forced and not administrator:
            raise HubError("identity.permission", "只有当前执行者或管理员可以释放任务")
        lease = lease_state(row)
        if if_expired:
            if not administrator:
                raise HubError("identity.permission", "--if-expired 仅管理员可用")
            if not lease["lease_seconds"]:
                raise HubError("task.state", "任务没有租约；直接 release，或先 heartbeat --lease-minutes 设置租约")
            if not lease["expired"]:
                raise HubError("task.state", f"任务租约尚未过期（剩余 {lease['remaining_seconds']} 秒）；拒绝按过期释放")
        if not forced:
            require_session(config, row, session, "release")
        clear_claim(conn, task_id, "ready", now())
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
    return {"task_id": task_id, "status": "ready", "warnings": []}


def fresh_checkpoint_event(conn: sqlite3.Connection, task_id: str, executor: str, device: str) -> sqlite3.Row | None:
    boundary = conn.execute(
        "SELECT event_id FROM events WHERE entity_id=? AND action IN "
        "('task.claim','task.handoff','task.handoff.force') ORDER BY event_id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    checkpoint = conn.execute(
        "SELECT event_id,detail_json FROM events WHERE entity_id=? AND action='task.checkpoint' "
        "AND agent_id=? AND device=? ORDER BY event_id DESC LIMIT 1",
        (task_id, executor, device),
    ).fetchone()
    if not boundary or not checkpoint or checkpoint["event_id"] <= boundary["event_id"]:
        return None
    return checkpoint


def do_handoff(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
               to_device: str, note: str, session: str | None) -> dict[str, Any]:
    info = identity(config, agent, device, "handoff")
    if not note.strip():
        raise HubError("input.invalid", "设备交接说明不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if row["status"] != "in_progress" or not row["claimed_by"]:
            raise HubError("task.state", "只有 in_progress 任务可以进行设备交接")
        executor = row["claimed_by"]
        executor_info = config["agents"].get(executor, {})
        if to_device not in executor_info.get("devices", []):
            raise HubError("identity.device", f"执行者 {executor} 不允许在设备 {to_device} 工作")
        forced = "administrator" in set(info.get("roles", [])) and agent != executor
        if not forced:
            if agent != executor:
                raise HubError("identity.permission", "只有当前执行者或管理员可以交接任务设备")
            if row["active_device"] != device:
                raise HubError("task.state", f"任务当前活动设备是 {row['active_device']}，必须从该设备发起交接")
            require_session(config, row, session, "handoff")
        checkpoint = fresh_checkpoint_event(conn, task_id, executor, row["active_device"] or "")
        if not forced and checkpoint is None:
            raise HubError("task.state", "设备交接前必须由当前执行者在当前活动设备保存本轮 checkpoint；请先执行 checkpoint")
        stamp = now()
        touch_claim(conn, task_id, stamp, active_device=to_device)
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
    return {"task_id": task_id, "from_device": row["active_device"], "to_device": to_device}


def do_checkpoint(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
                  raw: str, session: str | None) -> dict[str, Any]:
    identity(config, agent, device, "checkpoint")
    progress = read_json_argument(raw, "进度 checkpoint")
    summary = required_text(progress, "summary", 1200)
    next_step = required_text(progress, "next_step", 1200)
    evidence = progress.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 20 or any(
        not isinstance(item, str) or not item.strip() for item in evidence
    ):
        raise HubError("contract.invalid", "checkpoint.evidence 必须是不超过 20 个非空字符串的数组")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if row["claimed_by"] != agent or row["status"] != "in_progress":
            raise HubError("task.state", "只能为自己正在执行的任务写入 checkpoint")
        if row["active_device"] != device:
            raise HubError("task.state", f"任务活动设备是 {row['active_device']}；请先从活动设备执行 handoff")
        require_session(config, row, session, "checkpoint")
        stamp = now()
        cursor = conn.execute(
            "INSERT INTO checkpoints(task_id,agent_id,device,summary,next_step,evidence_json,generation,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, agent, device, summary, next_step, json_dump(evidence), row["claim_generation"], stamp),
        )
        touch_claim(conn, task_id, stamp)
        record(conn, agent, device, "task.checkpoint", task_id, {
            "summary": summary, "next_step": next_step, "generation": row["claim_generation"],
        })
        conn.commit()
        checkpoint_id = int(cursor.lastrowid)
    except Exception:
        conn.rollback()
        raise
    return {"task_id": task_id, "checkpoint_id": checkpoint_id, "generation": row["claim_generation"]}


def do_heartbeat(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
                 session: str | None, lease_minutes: int | None, note: str = "") -> dict[str, Any]:
    info = identity(config, agent, device, "heartbeat")
    if not session:
        raise HubError("task.session", "heartbeat 必须带 --session <id>")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if row["status"] != "in_progress":
            raise HubError("task.state", f"只有 in_progress 任务可以 heartbeat，当前状态: {row['status']}")
        administrator = "administrator" in set(info.get("roles", []))
        forced = administrator and agent != row["claimed_by"]
        if not forced:
            if row["claimed_by"] != agent:
                raise HubError("identity.permission", "只能为自己正在执行的任务 heartbeat")
            if row["active_device"] != device:
                raise HubError("task.state", f"任务活动设备是 {row['active_device']}；请先从活动设备执行 handoff")
        if row["claim_session"] and row["claim_session"] != session:
            raise HubError("task.session", f"任务 {task_id} 已绑定会话 {row['claim_session']}；当前会话 {session} 不得 heartbeat")
        seconds = row["lease_seconds"] if lease_minutes is None else lease_seconds_argument(lease_minutes)
        stamp = now()
        touch_claim(conn, task_id, stamp, claim_session=session, lease_seconds=seconds)
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
    return {
        "task_id": task_id, "session": session, "heartbeat_at": stamp,
        "lease": lease_state(updated),
    }


def do_submit(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
              raw: str, session: str | None) -> dict[str, Any]:
    identity(config, agent, device, "submit")
    result = read_json_argument(raw, "执行结果")
    outcome = required_text(result, "outcome")
    if outcome not in {"completed", "blocked"}:
        raise HubError("contract.invalid", "outcome 仅允许 completed 或 blocked")
    required_text(result, "summary")
    if "gates" in result:
        raise HubError("contract.invalid", "gates 是核心计算字段；请在 gate_results 中提供实测值")
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
            raise HubError("task.state", "只能由当前执行者提交处于 in_progress 的任务")
        if row["active_device"] != device:
            raise HubError("task.state", f"任务活动设备是 {row['active_device']}；请先从活动设备执行 handoff")
        require_session(config, row, session, "submit")
        contract = task_contract(row)
        gate_definitions = contract.get("gates", [])
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
                    "gate.failed",
                    f"合同门禁未通过: {detail}；请核对测量口径，或改用 blocked 提交并把实测值与取舍交回决策者",
                )
        if evaluated:
            result["gates"] = evaluated
        if outcome == "completed":
            worktree = row["worktree_path"]
            branch = git(config, "branch", "--show-current", cwd=worktree)
            if branch != row["branch"]:
                raise HubError("git.error", f"worktree 当前分支不符合任务合同: {branch} != {row['branch']}")
            head = git(config, "rev-parse", "HEAD", cwd=worktree)
            if head != commit:
                raise HubError("git.error", f"提交证据 commit 与 worktree HEAD 不一致: {commit} != {head}")
            dirty = git(config, "status", "--porcelain", cwd=worktree)
            if dirty:
                raise HubError("git.error", "worktree 仍有未提交变更；执行者必须先提交代码再申请复核")
            if not git_ok(config, "merge-base", "--is-ancestor", row["base_commit"], commit, cwd=worktree):
                raise HubError("git.error", "提交 commit 不是任务 base_commit 的后代")
            changed = changed_paths(config, row["base_commit"], commit, worktree)
            allowed = contract.get("allowed_paths", [])
            forbidden = contract.get("forbidden_paths", [])
            outside = [path for path in changed if not any(fnmatch.fnmatch(path, pattern) for pattern in allowed)]
            denied = [path for path in changed if any(fnmatch.fnmatch(path, pattern) for pattern in forbidden)]
            if outside or denied:
                raise HubError(
                    "path.boundary",
                    f"提交文件超出合同路径边界；未授权={outside}；禁止={denied}",
                )
            verify_artifacts(config, contract.get("artifacts", []), f"任务 {task_id}")
        stamp = now()
        cursor = conn.execute(
            "INSERT INTO submissions(task_id,agent_id,device,outcome,result_json,created_at) VALUES(?,?,?,?,?,?)",
            (task_id, agent, device, outcome, json_dump(result), stamp),
        )
        submission_id = int(cursor.lastrowid)
        conn.execute(
            "UPDATE tasks SET status=?,approved_submission_id=NULL,lease_seconds=0,last_heartbeat_at=?,updated_at=? "
            "WHERE task_id=?",
            (target, stamp, stamp, task_id),
        )
        record(conn, agent, device, f"task.submit.{outcome}", task_id, {
            "summary": result["summary"], "session": row["claim_session"], "submission_id": submission_id,
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"task_id": task_id, "status": target, "submission_id": submission_id, "warnings": []}


def do_review(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
              action: str, note: str) -> dict[str, Any]:
    identity(config, agent, device, "review")
    if not note.strip():
        raise HubError("input.invalid", "复核意见不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        if action == "approve":
            if row["status"] != "review":
                raise HubError("task.state", "只有 review 状态可以批准")
            submission = latest_submission(conn, task_id)
            if not submission:
                raise HubError("task.state", "review 任务缺少执行结果，不能批准")
            submitted = submission_result(submission) or {}
            contract = task_contract(row)
            gate_definitions = contract.get("gates", [])
            if gate_definitions:
                evaluated = evaluate_gates(gate_definitions, submitted.get("gate_results"), require_all=True)
                failed = [gate for gate in evaluated if not gate["passed"]]
                if failed:
                    detail = "; ".join(
                        f"{gate['id']} ({gate['metric']}): {gate['value']} {gate['operator']} {gate['threshold']}"
                        for gate in failed
                    )
                    raise HubError("gate.failed", f"合同门禁未通过，不能批准: {detail}")
            target = "approved"
            submission_id = int(submission["submission_id"])
        elif action == "rework":
            if row["status"] not in {"review", "blocked"}:
                raise HubError("task.state", "只有 review/blocked 状态可以退回")
            target = "ready"
            submission_id = None
        else:
            raise HubError("input.invalid", "action 仅允许 approve 或 rework")
        self_executed = bool(row["claimed_by"] and row["claimed_by"] == agent)
        if self_executed and self_review_policy(config, agent) == "forbid":
            raise HubError(
                "identity.permission",
                f"Agent {agent} 的 self_review=forbid：不能复核自己执行的任务 {task_id}；"
                "请由其他具备复核权限的 Agent 处理",
            )
        clear_claim(conn, task_id, target, now(), approved_submission_id=submission_id)
        record(conn, agent, device, f"task.review.{action}", task_id,
               {"note": note.strip(), "self_review": self_executed, "submission_id": submission_id})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"task_id": task_id, "status": target, "submission_id": submission_id, "self_review": self_executed}


def do_close(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
             merge_commit: str, target_branch: str, note: str) -> dict[str, Any]:
    identity(config, agent, device, "close")
    conn.execute("BEGIN IMMEDIATE")
    worktree = Path("")
    remove_worktree = False
    remove_branch = False
    branch = ""
    try:
        row = load_task(conn, task_id)
        if row["status"] != "approved":
            raise HubError("task.state", "只有 approved 任务可以关闭")
        submission = approved_submission(conn, row) or latest_submission(conn, task_id)
        if not submission:
            raise HubError("task.state", "approved 任务缺少已批准的 submission，不能关闭")
        result = submission_result(submission) or {}
        task_commit = required_text(result, "commit", 80)
        validate_revision(task_commit, "result.commit")
        if not note.strip():
            raise HubError("input.invalid", "关闭说明不能为空")
        validate_revision(merge_commit, "merge_commit")
        validate_branch_name(target_branch, "target_branch")
        git(config, "rev-parse", "--verify", f"{merge_commit}^{{commit}}")
        target_head = git(config, "rev-parse", "--verify", f"refs/heads/{target_branch}^{{commit}}")
        if target_head != merge_commit:
            raise HubError("git.error", f"merge_commit 必须是目标分支 {target_branch} 的当前 HEAD")
        if not git_ok(config, "merge-base", "--is-ancestor", task_commit, merge_commit):
            raise HubError("git.error", "目标分支尚未包含已批准的任务 commit")
        contract = task_contract(row)
        verify_artifacts(config, contract.get("artifacts", []), f"任务 {task_id}")
        worktree = Path(row["worktree_path"])
        branch = row["branch"]
        if worktree.exists():
            if git(config, "status", "--porcelain", cwd=str(worktree)):
                raise HubError("git.error", "任务 worktree 仍有未提交变更，不能清理")
            head = git(config, "rev-parse", "HEAD", cwd=str(worktree))
            if head != task_commit:
                raise HubError("git.error", f"worktree HEAD 与已批准 commit 不一致: {head} != {task_commit}")
            remove_worktree = True
        remove_branch = (
            branch != target_branch
            and bool(git(config, "show-ref", "--verify", f"refs/heads/{branch}", check=False))
        )
        clear_claim(conn, task_id, "closed", now())
        detail = {
            "task_commit": task_commit, "merge_commit": merge_commit,
            "executor": submission["agent_id"], "target_branch": target_branch,
            "session": row["claim_session"], "note": note.strip(),
            "submission_id": submission["submission_id"],
        }
        artifacts = contract.get("artifacts", [])
        if artifacts:
            detail["artifacts"] = artifacts
        record(conn, agent, device, "task.close", task_id, detail)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    cleanup_errors: list[str] = []
    if remove_worktree:
        try:
            git(config, "worktree", "remove", str(worktree))
        except (HubError, OSError) as exc:
            cleanup_errors.append(f"worktree: {exc}")
    if remove_branch:
        try:
            git(config, "branch", "-d", branch)
        except (HubError, OSError) as exc:
            cleanup_errors.append(f"branch: {exc}")
    warnings = []
    if cleanup_errors:
        warnings.append({
            "code": "cleanup.pending",
            "message": "任务已关闭，但 worktree/branch 清理未完成；修复原因后运行 prune --apply",
            "errors": cleanup_errors,
        })
    return {
        "task_id": task_id, "status": "closed", "target_branch": target_branch,
        "merge_commit": merge_commit, "warnings": warnings,
    }


def do_cancel(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
              reason: str) -> dict[str, Any]:
    info = identity(config, agent, device, "cancel")
    if not reason.strip():
        raise HubError("input.invalid", "取消原因不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = load_task(conn, task_id)
        forced = row["status"] in {"in_progress", "approved"} and "administrator" in set(info.get("roles", []))
        if row["status"] not in {"ready", "blocked", "review"} and not forced:
            raise HubError(
                "task.state",
                f"只有 ready/blocked/review 状态可以取消，当前状态: {row['status']}；in_progress/approved 只能由管理员取消",
            )
        stamp = now()
        clear_claim(conn, task_id, "cancelled", stamp, cancelled_at=stamp, cancelled_by=agent,
                    cancel_reason=reason.strip())
        record(conn, agent, device, "task.cancel", task_id, {
            "reason": reason.strip(), "previous_status": row["status"],
            "executor": row["claimed_by"], "forced": forced,
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    dependents = dependents_of(conn, task_id)
    warnings = []
    if dependents:
        warnings.append({
            "code": "dependency.cancelled",
            "message": "仍有活动任务依赖该任务，取消后它们无法 claim",
            "dependents": dependents,
        })
    return {"task_id": task_id, "status": "cancelled", "warnings": warnings}


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


def do_prune(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str,
             apply_changes: bool) -> dict[str, Any]:
    """Reclaim worktrees nothing is working in. Reports first; only --apply removes."""
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
                if branch and branch.startswith("agenthub/") and git_ok(
                    config, "show-ref", "--verify", f"refs/heads/{branch}"
                ):
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
    return {
        "applied": apply_changes,
        "worktree_root": str(root),
        "candidates": report,
        "removed": sum(1 for item in report if item["action"] == "removed"),
        "skipped": sum(1 for item in report if item["action"] == "skipped"),
        "note": "report only; re-run with --apply to remove" if not apply_changes else "removal applied",
        "warnings": [],
    }


def do_supersede(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, task_id: str,
                 new_spec: str, reason: str) -> dict[str, Any]:
    identity(config, agent, device, "supersede")
    if not reason.strip():
        raise HubError("input.invalid", "替代原因不能为空")
    spec = validate_task_contract(config, new_spec)
    new_id = spec["id"]
    acknowledgements: list[dict[str, Any]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        old_row = load_task(conn, task_id)
        if old_row["status"] not in {"ready", "blocked", "review", "in_progress"}:
            raise HubError("task.state", f"只有 ready/blocked/review/in_progress 状态可以被替代，当前状态: {old_row['status']}")
        if old_row["superseded_by"]:
            raise HubError("task.state", f"任务 {task_id} 已被 {old_row['superseded_by']} 替代")
        stamp = now()
        acknowledgements = insert_task(conn, config, agent, spec, stamp, supersedes=task_id)
        detail = {"new_task": new_id, "reason": reason.strip()}
        if acknowledgements:
            detail["path_overlap_ack"] = acknowledgements
        record(conn, agent, device, "task.supersede.new_posted", task_id, detail)
        clear_claim(conn, task_id, "cancelled", stamp, cancelled_at=stamp, cancelled_by=agent,
                    cancel_reason=f"superseded by {new_id}: {reason.strip()}", superseded_by=new_id)
        record(conn, agent, device, "task.supersede.old_cancelled", task_id, {
            "new_task": new_id, "reason": reason.strip(), "previous_status": old_row["status"],
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    warnings = []
    if acknowledgements:
        warnings.append({
            "code": "path.overlap.ack",
            "message": "新合同 allowed_paths 与其他活动任务重叠，已按 allow_path_overlap 放行",
            "acknowledgements": acknowledgements,
        })
    dependents = dependents_of(conn, task_id)
    if dependents:
        warnings.append({
            "code": "dependency.cancelled",
            "message": "仍有活动任务依赖被替代的任务，需要一并 supersede",
            "dependents": dependents,
        })
    return {"old_task": task_id, "new_task": new_id, "warnings": warnings}


def do_decision(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, key: str,
                summary: str, rationale: str) -> dict[str, Any]:
    identity(config, agent, device, "decision")
    if not key.strip() or not summary.strip() or not rationale.strip():
        raise HubError("input.invalid", "决策 key、summary、rationale 均不能为空")
    stamp = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO decisions(decision_key,summary,rationale,decided_by,device,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(decision_key) DO UPDATE SET summary=excluded.summary,rationale=excluded.rationale,"
            "decided_by=excluded.decided_by,device=excluded.device,updated_at=excluded.updated_at",
            (key, summary, rationale, agent, device, stamp),
        )
        conn.execute(
            "INSERT INTO decision_revisions(decision_key,summary,rationale,decided_by,device,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (key, summary, rationale, agent, device, stamp),
        )
        record(conn, agent, device, "decision.set", key, {"summary": summary})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"key": key, "summary": summary, "warnings": []}


def do_audit(conn: sqlite3.Connection, config: dict[str, Any], agent: str, device: str, limit: int,
             entity: str | None = None, action: str | None = None) -> dict[str, Any]:
    identity(config, agent, device, "audit")
    if limit < 1 or limit > 1000:
        raise HubError("input.invalid", "audit --limit 必须是 1..1000 的整数")
    clauses: list[str] = []
    params: list[Any] = []
    if entity:
        clauses.append("entity_id=?")
        params.append(entity)
    if action:
        clauses.append("action=?")
        params.append(action)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM events {where} ORDER BY event_id DESC LIMIT ?", (*params, limit)
    ).fetchall()
    return {
        "events": [
            dict(row) | {"detail": load_state_json(row["detail_json"], f"events[{row['event_id']}].detail")}
            for row in rows
        ],
        "limit": limit,
        "warnings": [],
    }


def _task_text(task: dict[str, Any] | None) -> str:
    if not task:
        return "(无任务)"
    contract = task.get("contract", {}) if isinstance(task.get("contract"), dict) else {}
    lines = [
        f"[{task['status']}] {task['id']} {task['title']} (P{task['priority']}) "
        f"assignee={task['assignee']} claimed_by={task.get('claimed_by') or '-'}"
    ]
    if contract.get("objective"):
        lines.append(f"  objective: {contract['objective']}")
    if contract.get("acceptance"):
        lines.append("  acceptance: " + " | ".join(str(item) for item in contract["acceptance"]))
    if contract.get("allowed_paths"):
        lines.append("  allowed: " + ", ".join(str(item) for item in contract["allowed_paths"]))
    if task.get("active_device"):
        lines.append(f"  device: {task['active_device']} session={task.get('claim_session') or '-'}")
    if task.get("checkpoint"):
        lines.append(f"  checkpoint: {task['checkpoint'].get('summary', '')}")
    return "\n".join(lines)


def render_text(command: str, data: dict[str, Any]) -> str:
    if command == "status":
        lines = [f"AgentHub {data['version']} | {data['project']} | 活动任务 {len(data['tasks'])}"]
        for task in data["tasks"]:
            chain = f" chain={task['chain_id']}" if task.get("chain_id") else ""
            deps = f" deps={','.join(task['depends_on'])}" if task.get("depends_on") else ""
            unmet = task.get("dependency", {}).get("unmet", [])
            blocked = f" blocked_by={','.join(item['task_id'] for item in unmet)}" if unmet else ""
            lines.append(
                f"- [{task['status']}] {task['id']} {task['title']} (P{task['priority']})"
                f"{chain}{deps}{blocked} -> {task.get('claimed_by') or '未认领'}"
            )
        return "\n".join(lines)
    if command == "context":
        lines = [
            f"{data['project']} | {data['agent']} | role={data['role_mode']} | "
            f"device={data['device']} | session={data.get('session') or '-'}"
        ]
        if data.get("focus"):
            lines.append("focus:")
            lines.append(_task_text(data["focus"]))
        counts = data.get("counts", {})
        lines.append("counts: " + " ".join(f"{key}={value}" for key, value in counts.items()))
        queue = data.get("review_queue", [])
        if queue:
            lines.append("review_queue:")
            for item in queue:
                mark = " self_review" if item.get("self_review") else ""
                lines.append(f"- [{item['status']}] {item['id']} {item['title']}{mark}")
        if data.get("decisions"):
            lines.append("decisions:")
            for item in data["decisions"]:
                lines.append(f"- {item['decision_key']}: {item['summary']}")
        return "\n".join(lines)
    if command in {"next", "claim"}:
        lines = [_task_text(data.get("task"))]
        if data.get("concurrency"):
            lines.append(
                f"concurrency: active={data['concurrency']['active']} limit={data['concurrency']['limit']}"
            )
        if data.get("excluded"):
            lines.append("excluded: " + ", ".join(item["task_id"] for item in data["excluded"]))
        return "\n".join(lines)
    if command == "show":
        lines = [_task_text(data["task"])]
        lines.append(f"submissions: {len(data.get('submissions', []))}")
        for item in data.get("submissions", []):
            lines.append(f"- #{item['submission_id']} {item['outcome']} by {item['agent']} at {item['created_at']}")
        return "\n".join(lines)
    if command == "post":
        lines = [f"已发布 {data['id']}"]
        lines.extend(f"warning: {item['message']}" for item in data.get("warnings", []))
        return "\n".join(lines)
    if command == "release":
        return f"已释放 {data['task_id']} -> ready"
    if command == "handoff":
        return f"已交接 {data['task_id']}: {data['from_device']} -> {data['to_device']}"
    if command == "checkpoint":
        return f"已保存 checkpoint {data['task_id']} generation={data['generation']}"
    if command == "heartbeat":
        lease = data.get("lease", {})
        return (
            f"heartbeat {data['task_id']} session={data['session']} "
            f"expires_at={lease.get('expires_at') or '-'} expired={lease.get('expired')}"
        )
    if command == "submit":
        return f"已提交 {data['task_id']} -> {data['status']} submission={data['submission_id']}"
    if command == "review":
        return f"已复核 {data['task_id']} -> {data['status']}"
    if command == "close":
        lines = [f"已关闭 {data['task_id']}；目标 {data['target_branch']}@{data['merge_commit']}"]
        lines.extend(f"warning: {item['message']}" for item in data.get("warnings", []))
        return "\n".join(lines)
    if command == "cancel":
        lines = [f"已取消 {data['task_id']}"]
        lines.extend(f"warning: {item['message']}" for item in data.get("warnings", []))
        return "\n".join(lines)
    if command == "supersede":
        lines = [f"已替代 {data['old_task']} -> {data['new_task']}"]
        lines.extend(f"warning: {item['message']}" for item in data.get("warnings", []))
        return "\n".join(lines)
    if command == "prune":
        lines = [
            f"prune applied={data['applied']} removed={data['removed']} skipped={data['skipped']}",
        ]
        for item in data.get("candidates", []):
            lines.append(f"- {item['action']} {item['path']}" + (f" ({item['reason']})" if item.get("reason") else ""))
        return "\n".join(lines)
    if command == "decision":
        return f"已记录决策 {data['key']}"
    if command == "audit":
        return "\n".join(
            f"- {item['occurred_at']} {item['action']} {item['entity_id']} by {item['agent_id']}"
            for item in data.get("events", [])
        )
    return json.dumps(data, ensure_ascii=False, indent=2)


def emit_success(command: str, data: dict[str, Any], text_mode: bool) -> None:
    if text_mode:
        print(render_text(command, data))
        return
    print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2))


def emit_error(error: HubError, text_mode: bool) -> None:
    if text_mode:
        print(f"[AgentHub] {error.code}: {error.message}", file=sys.stderr)
        return
    payload: dict[str, Any] = {"ok": False, "error": {"code": error.code, "message": error.message}}
    if error.details:
        payload["error"]["details"] = error.details
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=f"AgentHub v{VERSION}")
    value.add_argument("--project")
    value.add_argument("--config")
    value.add_argument("--state")
    value.add_argument("--device", default=os.environ.get("AGENTHUB_DEVICE"))
    value.add_argument("--session", default=os.environ.get("AGENTHUB_SESSION"))
    value.add_argument("--local", action="store_true", help=argparse.SUPPRESS)
    value.add_argument("--text", action="store_true", help="human-readable output")
    value.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    value.add_argument("--version", action="version", version=f"AgentHub {VERSION}")
    subs = value.add_subparsers(
        dest="command", required=True, metavar="{" + ",".join(PUBLIC_COMMANDS) + "}"
    )
    status = subs.add_parser("status")
    status.add_argument("agent")
    status.add_argument("--all", action="store_true")
    status.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    status_filter = status.add_mutually_exclusive_group()
    status_filter.add_argument("--mine", action="store_true")
    status_filter.add_argument("--assignee")
    status.add_argument("--decision")
    context = subs.add_parser("context")
    context.add_argument("agent")
    next_task = subs.add_parser("next")
    next_task.add_argument("agent")
    next_task.add_argument("--available", action="store_true")
    show = subs.add_parser("show")
    show.add_argument("agent")
    show.add_argument("task_id")
    show.add_argument("--submissions", type=int, default=3)
    post = subs.add_parser("post")
    post.add_argument("agent")
    post.add_argument("--spec", required=True)
    claim = subs.add_parser("claim")
    claim.add_argument("agent")
    claim.add_argument("task_id", nargs="?")
    claim.add_argument("--lease-minutes", type=int)
    release = subs.add_parser("release")
    release.add_argument("agent")
    release.add_argument("task_id")
    release.add_argument("--reason", required=True)
    release.add_argument("--if-expired", action="store_true")
    handoff = subs.add_parser("handoff")
    handoff.add_argument("agent")
    handoff.add_argument("task_id")
    handoff.add_argument("--to-device", required=True)
    handoff.add_argument("--note", required=True)
    checkpoint = subs.add_parser("checkpoint")
    checkpoint.add_argument("agent")
    checkpoint.add_argument("task_id")
    checkpoint.add_argument("--progress", required=True)
    heartbeat = subs.add_parser("heartbeat")
    heartbeat.add_argument("agent")
    heartbeat.add_argument("task_id")
    heartbeat.add_argument("--lease-minutes", type=int)
    heartbeat.add_argument("--note", default="")
    submit = subs.add_parser("submit")
    submit.add_argument("agent")
    submit.add_argument("task_id")
    submit.add_argument("--result", required=True)
    review = subs.add_parser("review")
    review.add_argument("agent")
    review.add_argument("task_id")
    review.add_argument("--action", choices=["approve", "rework"], required=True)
    review.add_argument("--note", required=True)
    close = subs.add_parser("close")
    close.add_argument("agent")
    close.add_argument("task_id")
    close.add_argument("--merge-commit", required=True)
    close.add_argument("--target-branch", required=True)
    close.add_argument("--note", required=True)
    cancel = subs.add_parser("cancel")
    cancel.add_argument("agent")
    cancel.add_argument("task_id")
    cancel.add_argument("--reason", required=True)
    prune = subs.add_parser("prune")
    prune.add_argument("agent")
    prune.add_argument("--apply", action="store_true")
    supersede = subs.add_parser("supersede")
    supersede.add_argument("agent")
    supersede.add_argument("task_id")
    supersede.add_argument("--spec", required=True)
    supersede.add_argument("--reason", required=True)
    decision = subs.add_parser("decision")
    decision.add_argument("agent")
    decision.add_argument("--key", required=True)
    decision.add_argument("--summary", required=True)
    decision.add_argument("--rationale", required=True)
    audit = subs.add_parser("audit")
    audit.add_argument("agent")
    audit.add_argument("--limit", type=int, default=50)
    audit.add_argument("--entity")
    audit.add_argument("--action")
    rpc = subs.add_parser("rpc")
    rpc.add_argument("payload")
    for action in list(getattr(subs, "_choices_actions", [])):
        if getattr(action, "dest", None) == "rpc":
            subs._choices_actions.remove(action)
    return value


def rpc_argv(payload: str) -> tuple[str, str | None, bool, list[str]]:
    try:
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise HubError("input.invalid", f"无效 RPC 载荷: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("argv"), list) or not isinstance(value.get("device"), str):
        raise HubError("input.invalid", "RPC 载荷结构无效")
    session = value.get("session")
    if session is not None and not isinstance(session, str):
        raise HubError("input.invalid", "RPC session 必须是字符串")
    text = value.get("text", False)
    if not isinstance(text, bool):
        raise HubError("input.invalid", "RPC text 必须是布尔值")
    if value["argv"] and value["argv"][0] == "rpc":
        raise HubError("input.invalid", "禁止嵌套 RPC")
    return value["device"], session, text, value["argv"]


def dispatch(conn: sqlite3.Connection, config: dict[str, Any], args: argparse.Namespace,
             device: str, session: str | None) -> dict[str, Any]:
    if args.command == "status":
        return do_status(conn, config, args.agent, device, args.all, args.mine, args.assignee, args.decision)
    if args.command == "context":
        return do_context(conn, config, args.agent, device, session)
    if args.command == "next":
        return do_next(conn, config, args.agent, device, session, args.available)
    if args.command == "show":
        return do_show(conn, config, args.agent, device, args.task_id, session, args.submissions)
    if args.command == "post":
        return do_post(conn, config, args.agent, device, args.spec)
    if args.command == "claim":
        return do_claim(conn, config, args.agent, device, args.task_id, session, args.lease_minutes)
    if args.command == "release":
        return do_release(conn, config, args.agent, device, args.task_id, args.reason, session, args.if_expired)
    if args.command == "handoff":
        return do_handoff(conn, config, args.agent, device, args.task_id, args.to_device, args.note, session)
    if args.command == "checkpoint":
        return do_checkpoint(conn, config, args.agent, device, args.task_id, args.progress, session)
    if args.command == "heartbeat":
        return do_heartbeat(conn, config, args.agent, device, args.task_id, session, args.lease_minutes, args.note)
    if args.command == "submit":
        return do_submit(conn, config, args.agent, device, args.task_id, args.result, session)
    if args.command == "review":
        return do_review(conn, config, args.agent, device, args.task_id, args.action, args.note)
    if args.command == "close":
        return do_close(conn, config, args.agent, device, args.task_id, args.merge_commit, args.target_branch, args.note)
    if args.command == "cancel":
        return do_cancel(conn, config, args.agent, device, args.task_id, args.reason)
    if args.command == "prune":
        return do_prune(conn, config, args.agent, device, args.apply)
    if args.command == "supersede":
        return do_supersede(conn, config, args.agent, device, args.task_id, args.spec, args.reason)
    if args.command == "decision":
        return do_decision(conn, config, args.agent, device, args.key, args.summary, args.rationale)
    if args.command == "audit":
        return do_audit(conn, config, args.agent, device, args.limit, args.entity, args.action)
    raise HubError("input.invalid", f"未知命令: {args.command}")


def run(argv: list[str] | None = None) -> int:
    global TEXT_MODE
    argv = expand_at_files(list(argv if argv is not None else sys.argv[1:]))
    args = parser().parse_args(argv)
    TEXT_MODE = bool(getattr(args, "text", False))
    path = config_path(args.project, args.config)
    config = load_config(path)
    device = args.device
    if args.command == "rpc":
        expected_platform = {"windows_cmd": "Windows", "posix_sh": "POSIX"}.get(config.get("remote", {}).get("shell"))
        actual_platform = "Windows" if platform.system() == "Windows" else "POSIX"
        if expected_platform != actual_platform and os.environ.get("AGENTHUB_ALLOW_RPC_ANYWHERE") != "1":
            raise HubError("input.invalid", f"RPC 必须在项目配置的 {expected_platform} 权威主机处理")
        device, session, text, suffix = rpc_argv(args.payload)
        forwarded = [
            "--local", "--config", str(path), "--state",
            args.state or config["state_authority"]["database"], "--device", device,
        ]
        if session:
            forwarded.extend(["--session", session])
        if text:
            forwarded.append("--text")
        forwarded.extend(suffix)
        return run(forwarded)
    session = session_value(args.session)
    if not device:
        raise HubError("input.invalid", "必须使用 --device 或 AGENTHUB_DEVICE 声明执行设备")
    if device not in config["devices"]:
        raise HubError("identity.device", f"未知设备: {device}")
    if not args.local and device != config["state_authority"]["host"] and config.get("remote", {}).get("enabled"):
        return proxy_to_authority(config, device, argv, session, bool(args.text))
    state = Path(args.state or config["state_authority"]["database"])
    conn = connect(state)
    try:
        migration = prepare_database(conn, state)
        data = dispatch(conn, config, args, device, session)
        if migration is not None:
            data = data | {"migration": migration}
        emit_success(args.command, data, bool(args.text))
        return 0
    finally:
        conn.close()


def main() -> None:
    try:
        raise SystemExit(run())
    except HubError as exc:
        emit_error(exc, TEXT_MODE)
        raise SystemExit(2)
    except (sqlite3.Error, OSError) as exc:
        emit_error(HubError("io.error", str(exc)), TEXT_MODE)
        raise SystemExit(1)
    except Exception as exc:
        emit_error(HubError("internal.error", str(exc)), TEXT_MODE)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
