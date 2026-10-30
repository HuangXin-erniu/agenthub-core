from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agenthub_245", ROOT / "agent_hub.py")
hub = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(hub)


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args], check=True,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


def init_repository(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "AgentHub Test")
    git(path, "config", "user.email", "agenthub@example.invalid")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-qm", "base")
    return git(path, "rev-parse", "HEAD")


class HubFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.base_commit = init_repository(self.repo)
        self.state = self.root / "state.db"
        self.config = self.root / "project.json"
        self.worktrees = self.root / "worktrees"
        self.write_config()

    def tearDown(self):
        self.temp.cleanup()

    def config_value(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_id": "test",
            "devices": {"authority": {}, "client_primary": {}, "client_secondary": {}},
            "agents": {
                "admin_agent": {"roles": ["administrator", "decider"],
                                "devices": ["client_primary", "client_secondary"]},
                "planner_agent": {"roles": ["decider"],
                                  "devices": ["client_primary", "client_secondary"]},
                "worker_a": {"roles": ["executor"],
                             "devices": ["authority", "client_primary", "client_secondary"]},
                "worker_b": {"roles": ["executor"],
                             "devices": ["authority", "client_primary", "client_secondary"]},
            },
            "state_authority": {
                "host": "authority", "database": str(self.state),
                "policy": "single_authority_local",
            },
            "execution": {
                "repository": str(self.repo), "worktree_root": str(self.worktrees),
                "max_active_tasks_per_executor": 1,
            },
        }

    def write_config(self, value: dict[str, Any] | None = None) -> dict[str, Any]:
        value = value or self.config_value()
        self.config.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return value

    def configure(self, **execution: Any) -> dict[str, Any]:
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["execution"].update(execution)
        self.write_config(value)
        return value

    def add_agent(self, agent: str, roles: list[str], devices: list[str] | None = None,
                  **extra: Any) -> dict[str, Any]:
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["agents"][agent] = {
            "roles": roles,
            "devices": devices or ["authority", "client_primary", "client_secondary"],
            **extra,
        }
        self.write_config(value)
        return value

    @property
    def base(self) -> list[str]:
        return ["--local", "--config", str(self.config), "--state", str(self.state)]

    def raw_call(self, *args: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = hub.run([*self.base, *args])
        self.assertEqual(code, 0)
        return output.getvalue()

    def call(self, *args: str) -> dict[str, Any]:
        payload = json.loads(self.raw_call(*args))
        self.assertTrue(payload["ok"], payload)
        return payload["data"]

    def call_text(self, *args: str) -> str:
        return self.raw_call("--text", *args)

    def call_error(self, *args: str) -> hub.HubError:
        with self.assertRaises(hub.HubError) as raised:
            self.raw_call(*args)
        return raised.exception

    def cli(self, *args: str, config: bool = True) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(ROOT / "agent_hub.py")]
        if config:
            command.extend(self.base)
        command.extend(args)
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False)

    def decide(self, key: str = "D-1", summary: str = "route",
               rationale: str = "evidence", agent: str = "admin_agent",
               device: str = "client_primary") -> dict[str, Any]:
        return self.call("--device", device, "decision", agent, "--key", key,
                         "--summary", summary, "--rationale", rationale)

    def packet(self, task_id: str = "T-1", assignee: str = "worker_a", **changes: Any) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": task_id,
            "title": "task",
            "objective": "implement exact change",
            "assignee": assignee,
            "decision_key": "D-1",
            "base_commit": self.base_commit,
            "instructions": ["append one line"],
            "acceptance": ["content exists"],
            "allowed_paths": ["README.md"],
            "forbidden_paths": [".agenthub/**"],
            "allow_path_overlap": True,
        }
        value.update(changes)
        return value

    def post(self, packet: dict[str, Any] | None = None, agent: str = "admin_agent",
             device: str = "client_primary") -> dict[str, Any]:
        return self.call("--device", device, "post", agent, "--spec", json.dumps(packet or self.packet()))

    def claim(self, task_id: str | None = None, agent: str = "worker_a",
              device: str = "authority", session: str | None = None,
              lease_minutes: int | None = None) -> dict[str, Any]:
        args = ["--device", device]
        if session:
            args.extend(["--session", session])
        args.extend(["claim", agent])
        if task_id:
            args.append(task_id)
        if lease_minutes is not None:
            args.extend(["--lease-minutes", str(lease_minutes)])
        return self.call(*args)

    def claim_task(self, task_id: str = "T-1", agent: str = "worker_a",
                   device: str = "authority", session: str | None = None) -> dict[str, Any]:
        return self.claim(task_id, agent, device, session)

    def worktree(self, task_id: str = "T-1") -> Path:
        return self.worktrees / task_id

    def commit_work(self, task_id: str = "T-1", path: str = "README.md",
                    text: str = "base\nchange\n", message: str = "change") -> str:
        worktree = self.worktree(task_id)
        target = worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        git(worktree, "add", path)
        git(worktree, "commit", "-qm", message)
        return git(worktree, "rev-parse", "HEAD")

    def submit_completed(self, task_id: str = "T-1", agent: str = "worker_a",
                         device: str = "authority", session: str | None = None) -> dict[str, Any]:
        commit = self.commit_work(task_id)
        result = {
            "outcome": "completed", "summary": "done", "commit": commit,
            "tests": ["unit"], "evidence": [f"commit:{commit}"],
        }
        args = ["--device", device]
        if session:
            args.extend(["--session", session])
        args.extend(["submit", agent, task_id, "--result", json.dumps(result)])
        return self.call(*args)

    def database(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.state)
        conn.row_factory = sqlite3.Row
        return conn

    def events(self) -> list[sqlite3.Row]:
        conn = self.database()
        try:
            return conn.execute("SELECT * FROM events ORDER BY event_id").fetchall()
        finally:
            conn.close()

    def open_state(self) -> sqlite3.Connection:
        return sqlite3.connect(self.state)


V4_TASKS = """
CREATE TABLE tasks (
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
    updated_at TEXT NOT NULL,
    chain_id TEXT,
    supersedes TEXT,
    superseded_by TEXT,
    cancelled_at TEXT,
    cancelled_by TEXT,
    cancel_reason TEXT,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    gates_json TEXT NOT NULL DEFAULT '[]',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    allow_path_overlap INTEGER NOT NULL DEFAULT 0
)
"""


def create_v4_database(path: Path, base_commit: str, worktree_root: Path) -> None:
    """Build a representative schema-4 database directly, without the old runtime."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE decisions (
                decision_key TEXT PRIMARY KEY, summary TEXT NOT NULL, rationale TEXT NOT NULL,
                decided_by TEXT NOT NULL, device TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE decision_revisions (
                revision_id INTEGER PRIMARY KEY AUTOINCREMENT, decision_key TEXT NOT NULL,
                summary TEXT NOT NULL, rationale TEXT NOT NULL, decided_by TEXT NOT NULL,
                device TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
                agent_id TEXT NOT NULL, device TEXT NOT NULL, action TEXT NOT NULL,
                entity_id TEXT NOT NULL, detail_json TEXT NOT NULL
            );
            """
        )
        conn.execute(V4_TASKS)
        conn.executescript(
            """
            CREATE TABLE checkpoints (
                checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL, device TEXT NOT NULL, summary TEXT NOT NULL,
                next_step TEXT NOT NULL, evidence_json TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );
            CREATE TABLE submissions (
                submission_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL, device TEXT NOT NULL, outcome TEXT NOT NULL,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );
            """
        )
        conn.execute("INSERT INTO metadata(key,value) VALUES('schema_version','4')")
        conn.execute("INSERT INTO metadata(key,value) VALUES('agenthub_version','2.4.3')")
        conn.execute("INSERT INTO decisions(decision_key,summary,rationale,decided_by,device,updated_at) "
                     "VALUES('D-OLD','legacy','legacy','admin_agent','client_primary','2026-10-01T00:00:00+00:00')")
        now = "2026-10-01T00:00:00+00:00"
        common = {
            "objective": "legacy objective",
            "instructions_json": json.dumps(["legacy instruction"]),
            "acceptance_json": json.dumps(["legacy acceptance"]),
            "scope_json": json.dumps(["legacy scope"]),
            "constraints_json": json.dumps(["legacy constraint"]),
            "allowed_paths_json": json.dumps(["README.md"]),
            "forbidden_paths_json": json.dumps(["none/**"]),
            "validation_json": json.dumps(["legacy validation"]),
            "deliverables_json": json.dumps(["legacy deliverable"]),
            "evidence_requirements_json": json.dumps(["legacy evidence"]),
            "blocked_protocol": "legacy blocked protocol",
            "context_budget": 6000,
            "assignee": "worker_a",
            "decision_key": "D-OLD",
            "base_commit": base_commit,
            "branch": "agenthub/T-OLD",
            "worktree_path": str(worktree_root / "T-OLD"),
            "created_by": "admin_agent",
            "created_at": now,
            "updated_at": now,
            "chain_id": "CHN-0001",
            "depends_on_json": "[]",
            "gates_json": "[]",
            "artifacts_json": "[]",
            "allow_path_overlap": 1,
        }
        rows = [
            ("T-CLOSED", "closed task", "closed", "worker_a", "authority", None, json.dumps(
                {"outcome": "completed", "summary": "closed", "commit": base_commit,
                 "tests": ["legacy"], "evidence": ["legacy"]})),
            ("T-APPROVED", "approved task", "approved", "worker_a", "authority", None, json.dumps(
                {"outcome": "completed", "summary": "approved", "commit": base_commit,
                 "tests": ["legacy"], "evidence": ["legacy"]})),
            ("T-ACTIVE", "active task", "in_progress", "worker_a", "authority", "legacy-session", None),
            ("T-BLOCKED", "blocked task", "blocked", "worker_b", "authority", None, json.dumps(
                {"outcome": "blocked", "summary": "blocked", "blocker": "x", "decision_needed": "y"})),
        ]
        for task_id, title, status, claimed_by, active_device, claim_session, result in rows:
            values = dict(common)
            values.update({
                "task_id": task_id,
                "title": title,
                "status": status,
                "priority": 50,
                "claimed_by": claimed_by,
                "active_device": active_device,
                "claim_session": claim_session,
                "last_heartbeat_at": now,
                "lease_seconds": 0,
                "claim_generation": 1,
                "result_json": result,
            })
            columns = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            conn.execute(f"INSERT INTO tasks({columns}) VALUES({placeholders})", tuple(values.values()))
            if result is not None:
                conn.execute(
                    "INSERT INTO submissions(task_id,agent_id,device,outcome,result_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (task_id, claimed_by, active_device, "completed" if "blocked" not in status else "blocked",
                     result, now),
                )
        conn.execute(
            "INSERT INTO checkpoints(task_id,agent_id,device,summary,next_step,evidence_json,generation,created_at) "
            "VALUES('T-ACTIVE','worker_a','authority','legacy checkpoint','next','[]',1,?)", (now,)
        )
        conn.execute(
            "INSERT INTO events(occurred_at,agent_id,device,action,entity_id,detail_json) "
            "VALUES(?,?,?,?,?,?)",
            (now, "worker_a", "authority", "task.claim", "T-ACTIVE", "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def previous_release_runtime() -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "show", "v2.4.3:agent_hub.py"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode or not result.stdout:
        return None
    path = Path(tempfile.mkdtemp(prefix="agenthub-old-runtime-")) / "agent_hub.py"
    path.write_text(result.stdout, encoding="utf-8")
    return path
