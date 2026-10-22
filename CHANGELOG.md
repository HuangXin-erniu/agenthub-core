# Changelog

## 2.4.1 — 2026-10-22

### Added

- An executor window can declare `--session <id>` (or `AGENTHUB_SESSION`). A claim records the session, and `checkpoint`, `submit`, `handoff` and `release` reject a different window of the same executor identity. Session is caller-declared workflow metadata, like agent and device, not a credential.
- `heartbeat <agent> <task_id> [--lease-minutes N]` refreshes `last_heartbeat_at` without writing a checkpoint. It is also the only command that binds a session to an existing `in_progress` claim, so a long task can adopt isolation after an upgrade without being released. An administrator may bind an unbound claim on the executor's behalf; a bound claim still requires the matching session.
- `execution.session_policy` accepts `optional` (default) or `required`. In `required` projects, `claim` and `claim-next` need a session, and an unbound claim must be bound by `heartbeat` before checkpoint/submit/release.
- `next` and `context` accept a session. With one, they show the tasks that session owns and then unclaimed ready work. Without one, `optional` projects keep the old selection for legacy unbound claims but skip every session-bound task and list it under `excluded`; `required` projects show only ready work. A window is never handed another window's in-progress task by default.
- `claim` and `claim-next` accept `--lease-minutes N` (0-10080). A lease expires relative to the last heartbeat; expiration is reported as `lease_expired` and never triggers an automatic release. `release --if-expired` lets an administrator free only an expired claim.
- `status` and task packets expose `claim_session`, `claim_generation`, `last_heartbeat_at` and a `lease` block. A re-claimed task exposes only checkpoints from the current claim as `checkpoint`; earlier rounds move to `previous_checkpoint`.

### Fixed

- `close` now commits the `closed` state and its audit event before removing the worktree or branch. A cleanup failure leaves a closed task for `prune` instead of deleting files and then rolling the state back. It also verifies that the worktree HEAD equals the approved commit.
- Task IDs are resolved as a single directory name under `execution.worktree_root`; `.`, `..`, leading dots and path separators are rejected even when a custom `task_naming` pattern would accept them.
- Corrupt JSON in task, checkpoint, submission, review or audit state is reported as `HubError` with the failing field instead of a `JSONDecodeError` traceback. Invalid configuration section types and invalid naming regexes are rejected at load time.
- `task_packet` reports an unreadable worktree as `worktree: {error: ...}` instead of failing `show`/`next` during a concurrent cleanup.
- `checkpoint.evidence` rejects blank strings; `audit --limit` is bounded to 1-1000; Git revisions and target branches are validated before use.
- `scripts/smoke_project.py` no longer hides the original failure behind an `UnboundLocalError` in its cleanup path.

### Changed

- Version 2.4.1. `templates/project.example.json` documents `execution.session_policy`; the registered Merman project sets it to `required` and uses `p00-download` / `p02-forecast` as window sessions.
- `README.md`, `README.zh-CN.md`, `docs/PROJECT_REGISTRATION.md`, `docs/CONTROL_PLANE_OPERATING_STANDARD.md`, `AGENTS.md` and the project role instructions document session ownership, heartbeat, lease warnings and migration.
- Remote RPC carries the session in its payload so a client window keeps its identity on the authority host.

### Upgrade notes

Configuration schema 1 and database schema 4 are unchanged. New columns (`claim_session`, `last_heartbeat_at`, `lease_seconds`, `claim_generation`, `checkpoints.generation`) are added in place; older runtimes ignore them. Existing configurations without `execution.session_policy` keep `optional`, so session-less commands behave as before.

To make a project session-safe, set `execution.session_policy` to `required`, deploy 2.4.1, and bind each running claim once with `heartbeat --session <id>`. Deploy while long tasks are in flight: deployment does not touch the SQLite state, and the runtime swap is covered by `tests/test_upgrade.py`. Do not use `--require-idle` for that upgrade. Bump the private registration's `agenthub_version` to 2.4.1.

## 2.4.0 — 2026-10-21

### Added

- An executor may hold more than one in-progress task when the project enables it. `execution.concurrency` accepts `one_active_task_per_executor` (default) or `multiple_tasks_per_executor`; the latter requires `execution.max_active_tasks_per_executor` (1-32), and an executor may lower its own limit with `agents.<id>.max_active_tasks`. Only `in_progress` tasks consume a slot; `blocked` and `review` do not.
- `next --available` returns the highest-priority ready task not yet claimed, and `claim-next` selects and claims one atomically. Multiple windows using the same executor identity therefore receive distinct tasks without a separate window ID.
- `release` returns an `in_progress` task to `ready` without deleting its worktree or branch. The current executor may release its own task from any device it is allowed to use; an administrator may release any task. This is the recovery path for a closed or crashed window.
- `status --mine` and `status --assignee <id>` filter the task list by executor.
- `next`, `context` and `claim-next` report `concurrency: {active, limit}`.
- `tests/test_concurrency.py` covers the default guard, multiple tasks, limits and per-agent overrides, `next --available`, `claim-next` (including four concurrent processes), `release` permissions, `status` filters and rollback on audit failure. `tests/test_upgrade.py` adds a cross-version case where an older runtime holds one task while the new runtime claims a second.

### Fixed

- `close` read the task before its write transaction, so a concurrent cancel, supersede or second close could act on stale state. The whole operation now runs under `BEGIN IMMEDIATE` with a conditional `approved` update and explicit rollback.
- `checkpoint`, `submit`, `review` and `decision` now roll back explicitly on failure instead of relying on connection close.
- `next`, `context`, `claim` capacity checks and `status` use `priority, created_at, task_id` ordering, so equal-priority tasks are deterministic.
- `load_config` now validates `execution.concurrency`, `execution.isolation`, `max_active_tasks_per_executor` and per-agent `max_active_tasks`; they were previously ignored.
- The structural audit now checks the template's execution settings, requires every enabled executor to have an `instructions` mapping, and rejects instruction keys that are neither `common` nor a registered agent.

### Changed

- Version 2.4.0. `templates/project.example.json` documents the new execution fields. The registered project enables multiple tasks with a limit of four.
- `README.md`, `README.zh-CN.md`, `docs/PROJECT_REGISTRATION.md`, `docs/CONTROL_PLANE_OPERATING_STANDARD.md`, `AGENTS.md` and the project role instructions describe the new commands and recovery path.
- `tests/test_publication.py::test_unregistered_core_passes_audit` skips when a private registration is present; CI still runs it in a core-only checkout.

### Upgrade notes

Configuration schema 1 and database schema 4 are unchanged: no new columns, no destructive migration. Existing configurations without the new keys keep the one-task-per-executor behavior. To enable concurrency, set `execution.concurrency` to `multiple_tasks_per_executor` and `execution.max_active_tasks_per_executor` to the desired limit; optionally set `agents.<id>.max_active_tasks` to lower an individual executor's limit.

Deploy the 2.4.0 runtime before invoking `claim-next`, `next --available` or `release`; older runtimes do not know those commands and will reject them. Change `agenthub_version` in the private registration to 2.4.0, as the structural audit requires the registration and runtime to agree.

## 2.3.4 — 2026-10-15

### Fixed

- A deployment could land the launcher non-executable and take the authority down with it. The launcher is invoked as `./.agenthub/agenthub.sh`, its mode after a deploy is the source mode masked by the remote umask, and nothing in the repository called `chmod`. The registration's launcher is a local, Git-ignored file — it has no recorded mode — while the only tracked launcher artifact, `templates/agenthub.posix.sh.tmpl`, is `100644`; so rebuilding a registration from the template without an explicit `chmod +x` produced a deployment that broke every proxied command. The staged launcher is now made executable before it is moved, so it lands correct and there is no window.
- The POSIX adapter never cleared its staging directory, unlike the Windows one. A file left by a crashed run is truncated in place by `scp`, which does not reset an existing file's mode, and the following `mv` would carry that stale mode into place.
- A hash mismatch surfaced only as a `CalledProcessError` traceback. The check now names the offending file, and a failed activation reports what the authority said instead of a traceback.

### Added

- The generation being replaced is retained under `staging-v23-previous`, and `--rollback` restores it: a documented manual procedure becomes a command.
- A post-swap compatibility self-check. The deployed runtime is made to load the deployed configuration — the only pair of managed files that constrain each other — and the launcher is confirmed executable. If it fails, the previous generation is restored automatically and the staging directory is left for inspection. That is what makes a file-by-file replacement safe in effect: an intermediate state either works, or is detected and undone.
- A deployment reports in-flight tasks by reading the state database read-only, and `--require-idle` refuses to deploy while any exist. The default is to report and proceed, because a task provably continues across a deployment.
- `deploy_project.py --verify` now covers `windows_cmd` as well as `posix_sh`.
- `tests/test_upgrade.py` asserts the deployment scripts' ordering properties for both adapters — every hash check before every replacement, a read-only verify script, rollback only from the retained generation, the launcher executable before it lands — and adds two cross-version tests that take the previous release's runtime out of Git history, claim a task with it, finish that task with the checked-out runtime, and confirm the previous runtime can still read state the current one wrote.

### Changed

- `--dry-run` also reports the in-flight tasks, so it now contacts the authority. It remains read-only.

### Upgrade notes

Configuration schema 1 and database schema 4 are unchanged: no new columns, no destructive migration, no change to CLI parameters or fnmatch semantics. Update the private configuration's `agenthub_version` to 2.3.4 — the audit requires the registration and the runtime to agree.

A deployment now retains one previous generation, and the next deployment replaces it, so `--rollback` undoes the most recent deployment only. The state database is still never part of a deployment; backing it up remains a separate, SQLite-aware step.

The `windows_cmd` verification path and the Windows activation changes are covered by script-level tests only and have not been exercised against a real Windows authority.

See `docs/PROJECT_REGISTRATION.md`, section "Upgrading an existing registration", for the full runbook.

## 2.3.3 — 2026-10-15

### Fixed

- Cleared the Git ownership check for the directory each command actually uses. On a host whose checkout owner differs from the running uid — an NTFS-3G mount with `user_id=0`, for example — every command inside a task worktree failed with `detected dubious ownership`, and no configuration could make the runtime trust the path: `safe.directory` matches an exact path only, never a glob, and clearing the main repository does not cover its worktrees.
- Made `claim` atomic. It read the new worktree after committing `in_progress` and had no rollback, so a Git failure there left the task permanently claimed, tripped the executor's one-active-task guard and could not be cancelled. The read now happens inside the transaction, and a failure leaves the task `ready` with its worktree reusable by the retry.
- Allowed an administrator to cancel `in_progress` and `approved` tasks. Both states previously had no exit: a wedged `in_progress` task could not be cancelled, and an `approved` task whose merge will not happen could be neither reworked nor closed. A forced cancellation records the previous status, the executor and `forced` in the event log; executors still cannot cancel anything.

### Added

- `prune` (administrator) reclaims the worktrees that cancelled and superseded tasks leave behind, which previously leaked with no way to collect them. It reports candidates by default and removes only under `--apply`; worktrees of live tasks and worktrees holding uncommitted changes are skipped, a directory Git does not recognise as a worktree is never deleted, and stale registry entries are cleared with `git worktree prune`.
- `deploy_project.py --verify` compares the registry sources against the deployed files by hash and exits non-zero on drift, which the local static audit cannot see. Implemented for `posix_sh`; a `windows_cmd` authority reports it as unsupported rather than pretending to check.
- Structural audit checks for the naming catalogue: the `enum_description` `PHASE`, `DOMAIN` and `TYPE` strings must name exactly what `phase_domains` and `domain_types` enforce, and a lingering `seq_offset` — a key the runtime never read — is reported.

### Changed

- `templates/project.example.json` drops `seq_offset`, which implied control over task numbering while the sequence is always `max(existing)+1`, and its `enum_description` now agrees with its own maps (`P00/P99`, `GOV/TASK/SEC`). `domain_types` still lists vocabulary the template does not enable.
- `workflow` is validated; the only supported value is `decision_first`. `project_name` stays descriptive metadata, and configuration-level `instructions` is read by the structural audit rather than the runtime.

### Upgrade notes

Configuration schema 1 and database schema 4 are unchanged: no new columns, no destructive migration, no change to CLI parameters or fnmatch semantics. Update the private configuration's `agenthub_version` to 2.3.3 — the audit requires the registration and the runtime to agree.

The audit is stricter: a registration whose naming catalogue advertises phases or domains its maps do not declare, or that still carries `seq_offset`, now fails it. Existing valid checkpoint events after the latest claim or handoff remain usable.

Cancelling `in_progress`/`approved` is an administrator escape hatch for a task that would otherwise be stuck, not an ordinary workflow step. `prune` changes nothing without `--apply`, so a first run is always a safe report.

Reading task state: a submission's payload is `submissions[0].result`, not `task.commit`. `worktree_path` is where the task's worktree lived and is retained as history after close, while the live state is the `worktree` block, which is absent once the directory is gone.

## 2.3.2 — 2026-10-10

### Fixed

- Unified task contract validation and insertion for post and supersede, including required arrays, priority, context budget, naming catalogues, decisions, base commits, global sequences and active chains.
- Kept replacement insertion, cancellation and audit events within one transaction; failed replacements preserve old tasks and do not consume sequences.
- Routed remote clients by registered device and Authority identity on Windows and POSIX, with no local fallback on SSH failure.
- Checked both ends of renames and preserved UTF-8 paths and special characters using NUL-separated Git output.
- Required a checkpoint in the current execution phase before ordinary device handoff; administrator recovery retains an explicit audit trail.

### Added

- A complete Simplified Chinese README with reciprocal language links.
- Regression tests for invalid contracts, transactional failure, routing, path boundaries, fresh checkpoints and schema-4 state compatibility.

### Upgrade notes

CLI parameters, configuration schema 1, database schema 4 and existing fnmatch semantics remain unchanged. No new database columns or destructive migration are introduced; historical task and checkpoint data is preserved.

Pause Authority activity and back up SQLite state before upgrading. Update the private configuration's agenthub_version to 2.3.2, then run the audit and isolated smoke check. Existing valid checkpoint events after the latest claim or handoff remain usable; otherwise save a new checkpoint before ordinary handoff. Administrators other than the executor may recover unavailable devices with a non-empty handoff note; missing checkpoints are recorded.

Incomplete replacement contracts, reused sequences and renames involving forbidden paths that previously slipped through validation are now rejected. The fixes do not turn the control plane into an OS sandbox. Public releases do not automatically deploy to private projects.

## 2.3.1 — 2026-10-10

- Initial public core-only snapshot: task lifecycle, role/device permissions, isolated Git worktrees, SQLite state, SSH adapters and generic templates.
- Added explicit release allowlisting, redacted publication checks, Gitleaks, Windows/Linux CI and deployment/security guidance.
