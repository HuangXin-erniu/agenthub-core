# AgentHub Core 2.4.3

[English](README.md) | [简体中文](README.zh-CN.md)

AgentHub is a lightweight, decision-first control plane for AI-agent collaboration across independent projects. Deciders publish compact, immutable task contracts; executors work in isolated Git worktrees and return commits, validation evidence or explicit blockers.

This repository contains only the reusable mechanism, implementation, templates and tests. Actual project registrations, business source, datasets, models, credentials and workflow databases stay outside the published Git history. The code uses the Python standard library; no Python package installation is required.

## Requirements

- Python 3.10 or later and Git with worktree support.
- Windows or a POSIX host. CI covers Windows/Linux with Python 3.10 and 3.13.
- OpenSSH client and a separately configured trusted SSH host for remote clients.
- PowerShell for Windows deployment; sh and sha256sum for POSIX deployment.

Use `python` below, or your platform's `python3` command.

## Architecture

```text
AgentHub core: lifecycle, role permissions, contract validation, SQLite schema
    |
Private project registration: agents, devices, paths, SSH and deployment mapping
    |
Authority host: canonical Git repository + local SQLite state + task worktrees
```

| Role | Responsibilities |
| --- | --- |
| administrator | Governance, initialization, integration close, cancellation and replacement |
| decider | Decisions, task contracts, inspection and evidence review |
| executor | Claim assigned tasks, checkpoint, handoff and submit results |

Roles are additive. One Agent may hold `decider` and `executor` (optionally `administrator`) and use one unified `context`: `role_mode` is `hybrid`, `tasks` is the current execution focus, `overview` lists active work and `review_queue` lists `blocked`/`review` tasks with a `self_review` flag. An executor that also reviews may approve its own submission; the review event and packet record `self_review: true`. Set `agents.<id>.self_review: forbid` to require an independent reviewer; the key is valid only for an Agent that has both an execution role and a review role. Only an Agent that has `executor` and no review role is restricted by `show` to its own assigned tasks.

```text
decision -> post -> claim -> checkpoint / handoff -> submit -> review
                                                           | rework -> claim
                                                           | approve -> integration -> close
```

Approval does not merge code. Integrate through your normal reviewed Git process before `close` verifies the merge and removes the task worktree. Cancellation and replacement preserve task history. SQLite state belongs to one authority host; each task has one active device, and an executor holds one in-progress task by default or up to the project's configured limit when concurrency is enabled.

## Configure a project

1. Copy `templates/project.example.json` into a private `projects/<project_id>/project.json`. Registered project directories are ignored by Git.
2. Replace every angle-bracket placeholder, including JSON object keys. Declare stable project, agent and device IDs; select a canonical Git repository, authority-local state file and separate worktree directory.
3. Set `remote.enabled` to `false` for local use. For remote use choose `windows_cmd` or `posix_sh`, the matching deployment adapter, a trusted SSH alias and strict host-key checking. Keep credentials in SSH configuration or a credential manager.
4. Add private `README.md`, `AGENTS.md` and role files declared in `instructions`. Configure the launcher from the OS-specific template, filling its runtime, interpreter, config and state paths.
5. If deploying, list every required file in `deployment.managed_files`, including role instructions. Audit, preview deployment, then deploy under your own change process.

See [project registration](docs/PROJECT_REGISTRATION.md) and [security boundaries](SECURITY.md). The template is a substitution guide, not an executable instance. No project is bundled or initialized automatically.

## Core commands

```sh
python agent_hub.py --help
python agent_hub.py --project <project_id> --device <device_id> context <decider_agent_id>
python agent_hub.py --project <project_id> --device <device_id> next <executor_agent_id>
python agent_hub.py --project <project_id> --device <device_id> --session <window_id> next <executor_agent_id> --available
```

For a local authority with an explicit private configuration, use `--local --config <config_file>`. Always declare the executing device. Version 2.4.3 retains the 2.3.1 CLI parameters and configuration format; `next --available`, `claim-next`, `release`, `--session`, `heartbeat`, `status --decision`, the contract fields `depends_on`, `gates`, `artifacts` and `allow_path_overlap`, the optional `agents.<id>.self_review` policy, and the additive `context` fields `role_mode`, `overview` and `review_queue` are supported.

| Commands | Purpose |
| --- | --- |
| init, status, context, next, show, audit | Initialize or inspect authorized workflow state |
| decision, post, claim, claim-next | Record a decision, publish its contract and claim assigned work |
| checkpoint, heartbeat, handoff, submit, release | Save progress, refresh liveness, transfer active device, release a task and submit evidence |
| review, close, cancel, supersede | Review, verify integration or preserve a cancellation/replacement |

`--session <id>` (or `AGENTHUB_SESSION`) names the window that owns a claim. It is declared by the caller, like an agent or device ID, and is not a credential. `heartbeat` refreshes a claim's `last_heartbeat_at`; `--lease-minutes` optionally makes expiry visible without ever triggering an automatic release.

Each subcommand supports `--help`. JSON arguments accept inline JSON or `@<file>` through `--spec`, `--progress` and `--result`. Task contracts bind assignee, decision key, base commit, allowed/forbidden paths, acceptance, validation, deliverables and evidence requirements. Contracts using `task_naming` also require the configured chain ID and exact next sequence. Packets are limited to 16,384 UTF-8 bytes.

## Workflow integrity and routing

- **Contracts:** `post` and `supersede` apply the same required fields, array, priority, budget, naming, decision and commit checks. Sequence numbers are global within a project, include cancelled tasks and cannot be reused. A replacement can retain its old task's chain ID, but cannot conflict with another active task. Replacement and cancellation commit atomically.
- **Dependencies:** an optional `depends_on` array names earlier tasks that must be `closed` before the task can be claimed. `claim`, `claim-next`, `next` and `next --available` refuse or skip unsatisfied tasks and report which dependency is unmet. The packet also warns when a closed dependency's merge commit is not contained in the task's `base_commit`, so the executor can merge the target branch or the decider can supersede with a newer base. `chain_id` remains a lineage label, not an ordering mechanism.
- **Path conflicts:** `post` compares the new contract's `allowed_paths` with every active task. Overlapping patterns are rejected unless the tasks are serialized by `depends_on` or the contract sets `allow_path_overlap: true`; an explicit acknowledgement is recorded in the `task.post` event. Glob patterns are compared by their static prefix, which is deliberately conservative.
- **Gates and artifacts:** a contract may declare numeric `gates` (`id`, `metric`, `operator` in `>= > <= <`, `threshold`, optional `source`). A completed submission must supply `gate_results` for every declared gate; the runtime computes pass/fail, rejects a completed submission when a required gate fails (use `blocked` to return the measurement and the trade-off to the decider), and recomputes the gates on `review approve`. A project may set `execution.artifact_root`, and a contract may then declare `artifacts` as paths relative to it; `submit` and `close` verify that every declared artifact exists, and `close` refuses before deleting the worktree.
- **Routing:** with remote access enabled, a registered device other than `state_authority.host` proxies through SSH on Windows and POSIX alike. The Authority, disabled remote access and explicit `--local` use local execution. SSH errors do not fall back to a local database.
- **Handoff:** the current executor must save a checkpoint on the active device after the most recent claim or handoff. A different administrator can recover an unavailable device through the existing handoff command with a non-empty explanation; recovery records whether a valid checkpoint was missing. Event IDs determine order, including events occurring in the same second.
- **Paths:** submission checks every changed path; a rename checks both source and destination. Forbidden patterns take precedence. NUL-separated Git output preserves Unicode, spaces and supported special filenames; paths that cannot be decoded reliably as UTF-8 are rejected. Existing `fnmatch` semantics remain: `src/*` also matches `src/nested/file.py`, and case normalization follows the host OS.
- **Concurrency:** each task keeps its own worktree, branch and active device. Executors default to one in-progress task; a project may enable `multiple_tasks_per_executor` with a bounded `max_active_tasks_per_executor`, optionally lowered per executor. `next --session <id> --available` lists unclaimed work, `claim-next` claims it atomically, and `release` returns a stuck task to `ready` without deleting its worktree. Only `in_progress` tasks consume a slot.
- **Sessions and long tasks:** a claim records the window that made it. `checkpoint`, `submit`, `handoff` and `release` reject a different window of the same executor identity. With `execution.session_policy: required`, claims need a session and an existing unbound claim must be bound once with `heartbeat --session <id>`. `next`/`context` with a session show that session's tasks and unclaimed ready work; without one they skip session-bound tasks and list them under `excluded`. A lease is advisory: `status`/`next` report `lease_expired`, but nothing is released automatically. External shared state (datasets, run outputs) remains project-owned; sessions isolate workflow ownership, not filesystem writes.
- **Dual roles and self-review:** an Agent holding both decision and execution roles receives one `context` workbench instead of choosing between a decider view and an executor view. Its execution focus stays scoped to the declared session, while `overview` and `review_queue` cover the Agent's decision scope, so a second window can review work submitted by the first. Self-review is allowed by default and audited with `self_review: true`; `agents.<id>.self_review: forbid` makes both `approve` and `rework` require a different Agent. `show` is restricted to assigned tasks only for Agents that cannot review.

These checks govern trusted workflow and committed changes. They do not sandbox running agents or prevent filesystem reads.

## Verification and deployment

```sh
python scripts/audit_agenthub.py
python -m unittest discover -s tests -v
python scripts/check_release.py
```

The audit accepts an unregistered core checkout. Once private registrations exist, it also checks their configuration, instructions, deployment sources and isolation. The release checker verifies the explicit public file manifest and scans content without printing matched values. In a development checkout with private registrations, use `--staged` or `--history` to inspect only the intended Git export.

```sh
python scripts/deploy_project.py --project <project_id> --dry-run
python scripts/deploy_project.py --project <project_id>
python scripts/deploy_project.py --project <project_id> --verify
python scripts/deploy_project.py --project <project_id> --rollback
python scripts/smoke_project.py --config <config_file> --runtime agent_hub.py
```

Deployment replaces managed files one by one, so it is not a transaction across the file set. The ordering it does guarantee: every staged file is hash-verified before any file is replaced; the generation being replaced is retained; and after the swap the deployed runtime is made to load the deployed configuration, which is the only pair of managed files that constrain each other. If that self-check fails the previous generation is restored automatically, so an intermediate state either works or is undone. `--rollback` restores the retained generation on demand, and `--verify` compares the deployed files against the registration by hash. Authority state is never part of a deployment: back it up separately in an SQLite-aware way, and avoid `--reset-state` unless intentionally rebuilding it. The smoke script clones the configured repository into a temporary directory and uses separate state and worktrees; it never runs the private launcher or deploys files. It tests generic lifecycle with synthetic task IDs, while unit tests cover naming rules and `tests/test_upgrade.py` covers upgrading with a task in flight. Unit tests create disposable repositories and require no SSH or private registrations.

Messaging transports are disabled. Legacy Feishu/Lark implementations are omitted. Agent identities and role checks organize trusted workflow; they do not authenticate untrusted network users.

## Upgrade from 2.4.2 or earlier

1. Pause Authority commands and back up its SQLite state using an SQLite-aware backup or a stopped Authority. Nothing else needs to be kept aside for rollback: a deployment retains the generation it replaces, and `--rollback` restores it.
2. Review the [changelog](CHANGELOG.md), install the 2.4.3 runtime and update your private configuration's `agenthub_version` to `2.4.3` so the structural audit agrees with the runtime.
3. Configuration schema remains **1** and database schema remains **4**. Version 2.4.3 adds no column and no migration: `self_review` is an optional Agent policy and the audit marker is stored in the existing review event detail. The 2.4.1 session/heartbeat columns and the 2.4.2 `depends_on_json`, `gates_json`, `artifacts_json` and `allow_path_overlap` columns remain as documented; older runtimes ignore them.
4. Configurations without `execution.session_policy` keep `optional`: session-less commands behave as before, and legacy unbound claims remain visible. To isolate long tasks from other windows, set it to `required`, then bind each running claim once with `heartbeat --session <id>`. A bound claim accepts only its own session; an administrator may bind an unbound claim on the executor's behalf.
5. Long tasks may stay in flight across the deploy: deployment never touches SQLite state and `tests/test_upgrade.py` covers the runtime swap. Do not use `--require-idle` for that upgrade. After the deploy, bind the running claim before its next checkpoint or submit.
6. To enable concurrent tasks for one executor, set `execution.concurrency` to `multiple_tasks_per_executor` and `execution.max_active_tasks_per_executor` to a value from 1 to 32. Optionally lower an individual executor's limit with `agents.<id>.max_active_tasks`.
7. Save a current checkpoint before ordinary handoff. Previously accepted incomplete replacement contracts, repeated sequences and renames involving forbidden paths are rejected. Historical contracts are not rewritten.
8. Run the structural audit, tests and isolated smoke check; validate the real launcher under your deployment process before resuming tasks.

For 2.4.3, no contract or state migration is required. Existing Agents keep self-review allowed; set `agents.<id>.self_review: forbid` when an independent reviewer is mandatory. `context` gains `role_mode`, `overview`, `review_queue` and a `self_review` block for dual-role Agents, while `tasks` and `excluded` keep their existing shape. `show` now lets an Agent that also reviews inspect tasks assigned to other executors. Previously accepted 2.4.2 rules still apply: `allowed_paths` overlaps are rejected at `post` unless sequenced with `depends_on` or acknowledged with `allow_path_overlap: true`; `execution.artifact_root` is required only for contracts that declare `artifacts`; gate declarations only affect contracts that use them.

This public release does not deploy or modify any existing private project automatically.

## Contributing and license

Read [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md) and [SECURITY.md](SECURITY.md). Release tagging and GitHub Releases follow [RELEASING.md](RELEASING.md). Licensed under [MIT](LICENSE).
