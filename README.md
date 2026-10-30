# AgentHub Core

[English](README.md) | [简体中文](README.zh-CN.md)

AgentHub is a small, decision-first control plane for AI-agent collaboration across
independent projects. Deciders publish compact, immutable task contracts; executors
work in isolated Git worktrees and return commits, evidence or explicit blockers.

This repository contains only the reusable mechanism, templates and tests. Project
registrations, business source, datasets, models, credentials and workflow databases
stay outside the published history. The runtime uses only the Python standard library.

The full contract, command, configuration and state semantics live in
[docs/SPEC.md](docs/SPEC.md). This README is a quickstart.

## Requirements

- Python 3.10 or later and Git with worktree support.
- Windows or a POSIX host. CI covers Windows/Linux with Python 3.10 and 3.13.
- OpenSSH for remote clients; PowerShell/sh and sha256sum for deployment.

## Architecture

```text
AgentHub core: lifecycle, role permissions, contract validation, SQLite state
    |
Private project registration: agents, devices, paths, SSH and deployment mapping
    |
Authority host: canonical repository + local SQLite state + task worktrees
```

Roles are additive: `administrator`, `decider`, `executor`. One Agent may hold
several roles and use one unified `context`. Self-review is allowed unless
`agents.<id>.self_review` is set to `forbid`.

## Quickstart

```sh
python agent_hub.py --project <project_id> --device <device_id> context <decider_agent_id>
python agent_hub.py --project <project_id> --device <device_id> next <executor_agent_id>
python agent_hub.py --project <project_id> --device <device_id> --session <window_id> \
  next <executor_agent_id> --available
```

Commands before the subcommand are global. For a local authority with an explicit
private configuration, use `--local --config <config_file>`. Always declare the
device. JSON arguments accept inline JSON or `@<file>` for `--spec`, `--progress` and
`--result`.

## Commands

| Command | Purpose |
| --- | --- |
| `status`, `context`, `next`, `show`, `audit` | Inspect authorized state |
| `decision`, `post` | Record a decision and publish its contract |
| `claim`, `release` | Claim work or return it to the queue |
| `checkpoint`, `heartbeat`, `handoff` | Save progress, refresh liveness, move device |
| `submit`, `review`, `close` | Submit evidence, review and integrate |
| `cancel`, `supersede`, `prune` | Preserve cancellation/replacement and reclaim worktrees |

`claim <agent> [task_id]` claims a named task, or the next available task when the ID
is omitted. `init` and `claim-next` are removed in 2.4.5. Every command opens or
migrates the state database; there is no separate initialization command.

All commands print one JSON envelope (`{"ok": true, "data": ...}`) and use
`{"ok": false, "error": {"code": ...}}` on stderr for domain errors. `--text` prints a
human-readable view. See [the command contract](docs/SPEC.md#4-command-line-contract).

## Contract and workflow

A minimal contract requires `id`, `title`, `objective`, `assignee`, `decision_key`,
`base_commit`, `instructions`, `acceptance` and `allowed_paths`. Optional fields add
`forbidden_paths`, `depends_on`, numeric `gates`, `artifacts`, `allow_path_overlap`,
priority and `chain_id`.

```sh
# decision and contract
python agent_hub.py ... decision <admin> --key D-1 --summary "..." --rationale "..."
python agent_hub.py ... post <admin> --spec @task.json

# execution
python agent_hub.py ... --session win-a claim <executor> TASK-001
python agent_hub.py ... --session win-a checkpoint <executor> TASK-001 --progress @progress.json
python agent_hub.py ... --session win-a submit <executor> TASK-001 --result @result.json

# review and integration
python agent_hub.py ... review <decider> TASK-001 --action approve --note "..."
# merge through the project's normal reviewed Git process, then:
python agent_hub.py ... close <admin> TASK-001 --merge-commit <sha> --target-branch main --note "..."
```

`submit` computes declared gates; a failed required gate must be returned as a
`blocked` submission. `close` verifies that the target branch contains the approved
commit and removes the worktree.

## Concurrency, sessions and state

Each task keeps its own worktree, branch and active device. An executor holds one
in-progress task by default; set `execution.max_active_tasks_per_executor` (1-32) to
allow more, optionally lowered per Agent. Only `in_progress` tasks consume a slot.

A claim records the caller-declared session. `checkpoint`, `submit`, `handoff` and
`release` reject another window of the same identity. With
`execution.session_policy: required`, claims need a session and an unbound legacy
claim must be bound once with `--session <id> heartbeat <executor> <task_id>`. A lease is advisory:
expiry is reported but never releases work automatically.

The authority owns SQLite state and worktrees. Keep them outside synchronized
folders; back up state separately. Schema 4 is migrated to schema 5 on first open
after writing a `state.db.v4-<UTC timestamp>.bak` backup. A schema 5 database is not
readable by 2.4.3; restore that backup to downgrade.

## Verification and deployment

```sh
python scripts/audit_agenthub.py
python -m unittest discover -s tests -v
python scripts/check_release.py
```

Deployment replaces managed files individually but verifies every hash before the
first replacement, retains the replaced generation, restores it when the deployed
runtime cannot load the deployed configuration, and supports `--dry-run`, `--verify`
and `--rollback`. It never modifies SQLite state. The smoke script clones the
configured repository into a temporary directory and uses disposable state.

## Upgrade from 2.4.3

1. Stop authority commands and back up SQLite state in an SQLite-aware way.
2. Install the 2.4.5 runtime.
3. The first command migrates schema 4 to schema 5 and writes a v4 backup. Keep it
   until the new state is proven; it is the downgrade path.
4. Remove the old optional keys from the private registration: `agenthub_version`,
   `project_name`, `workflow`, `messaging_transports`, `lark_workflow`,
   `execution.concurrency`, `execution.isolation`, and the task-naming catalogue
   keys. They are ignored by 2.4.5 but should not be carried forward.
5. Update role instructions that still mention `init` or `claim-next`.
6. Run the audit, tests and isolated smoke check before resuming work.

## Contributing and license

Read [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md) and
[SECURITY.md](SECURITY.md). Release tagging and GitHub Releases follow
[RELEASING.md](RELEASING.md). Licensed under [MIT](LICENSE).
