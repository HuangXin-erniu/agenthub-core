# Changelog

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
