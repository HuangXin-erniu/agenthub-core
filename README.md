# AgentHub Core 2.3.1

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

For a local authority with an explicit private configuration, use `--local --config <config_file>`. Always declare the executing device. The runtime retains its 2.3.1 CLI and configuration interfaces.

| Commands | Purpose |
| --- | --- |
| init, status, context, next, show, audit | Initialize or inspect authorized workflow state |
| decision, post, claim | Record a decision, publish its contract and claim assigned work |
| checkpoint, handoff, submit | Save progress, transfer active device and submit evidence |
| review, close, cancel, supersede | Review, verify integration or preserve a cancellation/replacement |

Each subcommand supports `--help`. JSON arguments accept inline JSON or `@<file>` through `--spec`, `--progress` and `--result`. Task contracts bind assignee, decision key, full base commit, allowed/forbidden paths, acceptance, validation, deliverables and evidence requirements. Contracts using `task_naming` also require the configured chain ID and exact next sequence. Packets are limited to 16,384 UTF-8 bytes.

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
python scripts/smoke_project.py --config <config_file> --runtime agent_hub.py
```

Deployment verifies file hashes and replaces individual files; it is not a transaction across all files. Back up authority state separately and avoid `--reset-state` unless intentionally rebuilding it. The smoke script clones the configured repository into a temporary directory and uses separate state and worktrees; it never runs the private launcher or deploys files. It tests generic lifecycle with synthetic task IDs, while unit tests cover naming rules. Unit tests create their own disposable repositories and require no SSH or private registrations.

Messaging transports are disabled. The legacy Feishu/Lark implementations are omitted. Agent identities and role checks organize trusted workflow; they do not provide OS sandboxing or authenticate untrusted network users.

## Contributing and license

Read [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md) and [SECURITY.md](SECURITY.md). Licensed under [MIT](LICENSE).
