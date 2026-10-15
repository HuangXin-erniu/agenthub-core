# Changelog

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
