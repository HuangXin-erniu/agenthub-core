# AgentHub Core 2.3.4

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

```text
decision -> post -> claim -> checkpoint / handoff -> submit -> review
                                                           | rework -> claim
                                                           | approve -> integration -> close
```

Approval does not merge code. Integrate through your normal reviewed Git process before `close` verifies the merge and removes the task worktree. Cancellation and replacement preserve task history. SQLite state belongs to one authority host; each task has one active device and each executor one active task.

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
```

For a local authority with an explicit private configuration, use `--local --config <config_file>`. Always declare the executing device. Version 2.3.4 retains the 2.3.1 CLI parameters and configuration format.

| Commands | Purpose |
| --- | --- |
| init, status, context, next, show, audit | Initialize or inspect authorized workflow state |
| decision, post, claim | Record a decision, publish its contract and claim assigned work |
| checkpoint, handoff, submit | Save progress, transfer active device and submit evidence |
| review, close, cancel, supersede | Review, verify integration or preserve a cancellation/replacement |

Each subcommand supports `--help`. JSON arguments accept inline JSON or `@<file>` through `--spec`, `--progress` and `--result`. Task contracts bind assignee, decision key, base commit, allowed/forbidden paths, acceptance, validation, deliverables and evidence requirements. Contracts using `task_naming` also require the configured chain ID and exact next sequence. Packets are limited to 16,384 UTF-8 bytes.

## Workflow integrity and routing

- **Contracts:** `post` and `supersede` apply the same required fields, array, priority, budget, naming, decision and commit checks. Sequence numbers are global within a project, include cancelled tasks and cannot be reused. A replacement can retain its old task's chain ID, but cannot conflict with another active task. Replacement and cancellation commit atomically.
- **Routing:** with remote access enabled, a registered device other than `state_authority.host` proxies through SSH on Windows and POSIX alike. The Authority, disabled remote access and explicit `--local` use local execution. SSH errors do not fall back to a local database.
- **Handoff:** the current executor must save a checkpoint on the active device after the most recent claim or handoff. A different administrator can recover an unavailable device through the existing handoff command with a non-empty explanation; recovery records whether a valid checkpoint was missing. Event IDs determine order, including events occurring in the same second.
- **Paths:** submission checks every changed path; a rename checks both source and destination. Forbidden patterns take precedence. NUL-separated Git output preserves Unicode, spaces and supported special filenames; paths that cannot be decoded reliably as UTF-8 are rejected. Existing `fnmatch` semantics remain: `src/*` also matches `src/nested/file.py`, and case normalization follows the host OS.

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

## Upgrade from 2.3.1

1. Pause Authority commands and back up its SQLite state using an SQLite-aware backup or a stopped Authority. Nothing else needs to be kept aside for rollback: a deployment retains the generation it replaces, and `--rollback` restores it.
2. Review the [changelog](CHANGELOG.md), install the 2.3.4 runtime and update your private configuration's `agenthub_version` to `2.3.4` so the structural audit agrees with the runtime.
3. Configuration schema remains **1** and database schema remains **4**. Existing task and checkpoint history is preserved; no new database columns or destructive migration are introduced.
4. Save a current checkpoint before ordinary handoff. Previously accepted incomplete replacement contracts, repeated sequences and renames involving forbidden paths are now rejected. Historical contracts are not rewritten.
5. Run the structural audit, tests and isolated smoke check; validate the real launcher under your deployment process before resuming tasks.

This public release does not deploy or modify any existing private project automatically.

## Contributing and license

Read [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md) and [SECURITY.md](SECURITY.md). Licensed under [MIT](LICENSE).
