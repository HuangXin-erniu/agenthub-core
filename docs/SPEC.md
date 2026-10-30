# AgentHub Core Specification

This document is the single source of truth for the current AgentHub Core release. README files,
project registrations and role instructions link here instead of restating the
mechanism.

## 1. Scope and trust boundary

AgentHub is a decision-first control plane for AI-agent collaboration. The authority
host owns the SQLite state, the canonical Git repository and every task worktree.
Other devices are thin clients that proxy commands over SSH.

Agent IDs, device IDs and session IDs are caller-declared workflow identifiers, not
credentials. The control plane organizes work inside a trusted environment; it does
not authenticate network users and does not sandbox process execution. Use OS
accounts, file permissions, SSH host checking and sandboxing where those properties
matter.

The core is project-neutral. Project IDs, agent IDs, device IDs, paths, SSH aliases,
launchers and naming patterns belong to a private registration.

## 2. Roles and permissions

Roles are additive. An Agent may hold `executor`, `decider` and `administrator`.

| Role | Capabilities |
| --- | --- |
| `administrator` | `post`, `review`, `close`, `cancel`, `supersede`, `prune`, `release`, `handoff`, `heartbeat`, `show`, `status`, `context`, `decision`, `audit` |
| `decider` | `post`, `review`, `show`, `status`, `context`, `decision`, `audit` |
| `executor` | `claim`, `next`, `checkpoint`, `submit`, `release`, `handoff`, `heartbeat`, `show`, `status`, `context` |

Visibility:

- An Agent with `executor` and no review role sees only tasks assigned to it in
  `status` and `show`. `--assignee <other>` is rejected.
- An Agent with `decider` or `administrator` sees all tasks and may inspect another
  executor's task.
- `audit` is limited to review roles. `close`, `cancel`, `supersede` and `prune` are
  administrator actions.

Self-review is allowed by default. When an Agent approves or reworks a task it
executed, the review event records `self_review: true`. Set
`agents.<id>.self_review: "forbid"` on a dual-role Agent to require a different
reviewer for both `approve` and `rework`. The key is rejected on a single-role Agent.

## 3. Lifecycle

```text
ready -> in_progress -> review -> approved -> closed
             |  \          |
             |   rework ---+-> ready
             +-> blocked --+-> review|ready (rework)
ready/blocked/review/in_progress -> cancelled (administrator rules)
approved/in_progress -> cancelled (administrator only)
```

- `post` creates an immutable contract in `ready`.
- `claim` assigns it to the executor's device, creates a Git worktree and increments
  the claim generation.
- `checkpoint` records progress without leaving `in_progress`; `heartbeat` refreshes
  liveness and binds a session. Changing device requires a checkpoint then `handoff`.
- `submit` writes one submission and moves to `review` (`completed`) or `blocked`.
- `review approve` recomputes declared gates, stores `approved_submission_id` and
  clears the claim. `review rework` returns the task to `ready`.
- `close` verifies that the target branch contains the approved commit, verifies
  artifacts and removes the worktree.
- `cancel` and `supersede` preserve history. `prune` reclaims terminal worktrees.

A task keeps at most one active device. Concurrency is bounded by
`execution.max_active_tasks_per_executor`; only `in_progress` tasks consume a slot.

## 4. Command-line contract

Global options must precede the command:

```text
agent_hub.py [--project ID] [--config FILE] [--state FILE] [--device ID]
             [--session ID] [--text] [--version] <command> ...
```

`--json` is accepted as a hidden no-op for older scripts. `--local` and `rpc` are
internal/trusted interfaces and are not part of the public command table.

### 4.1 Public commands

| Command | Purpose |
| --- | --- |
| `status` | List authorized task summaries; `--all`, `--mine`, `--assignee`, `--decision` |
| `context` | Unified workbench: focus packet, review queue, counts, recent decisions |
| `next` | Return the next execution focus; `--available` restricts to unclaimed work |
| `show` | Full task packet plus recent submissions; `--submissions N` (0-50) |
| `post` | Publish an immutable task contract |
| `claim` | Claim a named task, or the next available task when the ID is omitted |
| `release` | Return an in-progress task to ready; `--if-expired` requires an expired lease |
| `handoff` | Move the active device after a current checkpoint |
| `checkpoint` | Store progress, next step and evidence |
| `heartbeat` | Refresh liveness, bind a session and optionally set a lease |
| `submit` | Submit `completed` or `blocked` evidence |
| `review` | `--action approve` or `--action rework` with a note |
| `close` | Verify integration and remove the task worktree |
| `cancel` | Cancel a task while preserving history |
| `supersede` | Atomically cancel an old task and publish its replacement |
| `prune` | Report or remove terminal worktrees; `--apply` performs removal |
| `decision` | Record or revise the decision that authorizes contracts |
| `audit` | Read the event log; `--limit`, `--entity`, `--action` |

`rpc` exists only for the SSH proxy and is hidden from help, documentation and tests
of the public surface.

### 4.2 JSON envelope

Every public command prints one envelope on success:

```json
{"ok": true, "data": {"...": "..."}}
```

Domain and input errors print to stderr and exit `2`:

```json
{"ok": false, "error": {"code": "task.not_found", "message": "...", "details": {}}}
```

`--text` prints a human-readable summary or `[AgentHub] <code>: <message>` on error.
Unexpected/infrastructure errors exit `1`. Argparse usage errors remain argparse-style
and exit `2`.

Warnings are data, not a second output stream:

```json
{"ok": true, "data": {"warnings": [{"code": "path.overlap.ack", "message": "..."}]}}
```

### 4.3 Error codes

| Code | Meaning |
| --- | --- |
| `config.invalid` | Registration or execution configuration is invalid |
| `identity.unknown` | Unknown or disabled Agent |
| `identity.device` | Device is unknown or not allowed for this Agent |
| `identity.permission` | Role or ownership does not permit the action |
| `task.not_found` | Task ID does not exist |
| `task.state` | Invalid lifecycle transition or missing prerequisite |
| `task.capacity` | Executor concurrency limit reached |
| `task.session` | Session is missing, mismatched or not yet bound |
| `contract.invalid` | Contract shape, naming or declaration is invalid |
| `contract.budget` | Contract or JSON argument exceeds the configured size |
| `dependency.unmet` | A declared dependency is missing, cancelled or not closed |
| `path.overlap` | `allowed_paths` overlap another active contract |
| `path.boundary` | Submitted changes leave the contract path boundary |
| `gate.failed` | A declared numeric gate did not pass |
| `artifact.missing` | A declared artifact is absent or escapes its root |
| `git.error` | Git command, revision or integration check failed |
| `state.corrupt` | Stored JSON or state is unreadable |
| `state.conflict` | Concurrent state change defeated the transition |
| `state.migration` | Database schema is unsupported or migration failed |
| `input.invalid` | CLI argument, JSON argument or session format is invalid |
| `io.error` | Filesystem or SQLite operation failed unexpectedly |
| `internal.error` | Unexpected runtime failure |
| `domain.error` | Compatibility fallback for an unclassified domain error |

## 5. Task contract 2.0

Required fields:

| Field | Rule |
| --- | --- |
| `id` | Safe directory name; project pattern when `task_naming` is configured |
| `title` | 1-200 characters |
| `objective` | Non-empty text |
| `assignee` | Enabled Agent with `executor` |
| `decision_key` | Existing decision |
| `base_commit` | 7-64 hex characters, existing Git commit |
| `instructions` | Non-empty array, at most 20 non-empty strings |
| `acceptance` | Non-empty array, at most 20 non-empty strings |
| `allowed_paths` | Non-empty array of `fnmatch` patterns |

Optional fields:

| Field | Default |
| --- | --- |
| `forbidden_paths` | `[]`; forbidden patterns win over allowed ones |
| `depends_on` | `[]`; every dependency must be `closed` before claim |
| `gates` | `[]`; each gate is `{id, metric, operator, threshold, source?}` |
| `artifacts` | `[]`; POSIX paths under `execution.artifact_root` |
| `allow_path_overlap` | `false`; explicit acknowledgement of an overlap |
| `priority` | `50`; 1-99 |
| `chain_id` | `""`; lineage label, not ordering |

Unknown top-level fields are rejected. Removed 2.4.3 fields (`scope`, `constraints`,
`validation`, `deliverables`, `evidence_requirements`, `blocked_protocol`,
`context_budget`) are preserved only on migrated contracts under `contract.legacy`.

Minimal example:

```json
{
  "id": "TASK-001",
  "title": "Fix parser",
  "objective": "Reject empty input",
  "assignee": "executor",
  "decision_key": "D-1",
  "base_commit": "0123456789abcdef0123456789abcdef01234567",
  "instructions": ["Add validation", "Add a regression test"],
  "acceptance": ["Empty input exits 2"],
  "allowed_paths": ["src/parser.py", "tests/**"]
}
```

Submission result:

- `completed`: `summary`, `commit`, `tests[]`, `evidence[]`, optional `gate_results`.
- `blocked`: `summary`, `blocker`, `decision_needed`, optional `gate_results`.

`submit` computes `gates`; clients must not send a `gates` field. A completed
submission missing gate results is rejected. A failed gate is rejected for
`completed` and accepted for `blocked`; `review approve` recomputes the gate result
instead of trusting any stored pass flag.

## 6. Project configuration

Configuration schema is `1`. The canonical keys are:

```json
{
  "schema_version": 1,
  "project_id": "<project>",
  "devices": {"<authority>": {}, "<client>": {}},
  "agents": {
    "<admin>": {"roles": ["administrator", "decider"], "devices": ["<client>"]},
    "<executor>": {"roles": ["executor"], "devices": ["<authority>", "<client>"]}
  },
  "state_authority": {"host": "<authority>", "database": "<state>", "policy": "single_authority_local"},
  "execution": {
    "repository": "<repo>", "worktree_root": "<worktrees>",
    "artifact_root": "<artifacts>", "max_active_tasks_per_executor": 1,
    "max_contract_bytes": 16384, "session_policy": "optional"
  },
  "remote": {"enabled": true, "shell": "posix_sh", "ssh_host": "<alias>",
             "project_root": "<root>", "launcher": "<launcher>", "ssh_options": []},
  "instructions": {"common": "AGENTS.md", "<admin>": "<admin.md>", "<executor>": "<executor.md>"},
  "deployment": {"adapter": "ssh_atomic_posix", "managed_files": []},
  "task_naming": {"task_pattern": "TASK-(?P<seq>\\d{3})",
                  "chain_pattern": "CHAIN-(?P<seq>\\d{4})", "seq_digits": 3}
}
```

- `execution.max_active_tasks_per_executor`: 1-32, default 1. Only `in_progress`
  tasks consume a slot. `agents.<id>.max_active_tasks` may lower the limit.
- `execution.max_contract_bytes`: 4096-65536, default 16384.
- `execution.session_policy`: `optional` (default) or `required`.
- `artifact_root` must not be inside `worktree_root`.
- `task_naming` is optional. When present, both patterns must contain a `seq` named
  group; sequence is global, monotonic and never reused. The core does not interpret
  phase/domain/type vocabulary.
- Removed keys (`agenthub_version`, `project_name`, `workflow`,
  `messaging_transports`, `lark_workflow`, `execution.concurrency`,
  `execution.isolation`, `task_naming.enum_description`, `phase_domains`,
  `domain_types`) are ignored so an older private registration still loads. They
  should be removed from the registration at deployment time.
- If an older configuration sets `execution.concurrency` to
  `multiple_tasks_per_executor` without `max_active_tasks_per_executor`, loading fails
  instead of silently reducing concurrency to one.

## 7. State schema 5

`tasks` is 26 columns:

```text
task_id, title, status, priority, assignee, decision_key, base_commit,
branch, worktree_path, contract_json, approved_submission_id,
chain_id, supersedes, superseded_by, created_by, claimed_by,
active_device, claim_session, last_heartbeat_at, lease_seconds,
claim_generation, created_at, updated_at, cancelled_at, cancelled_by,
cancel_reason
```

- `contract_json` is the contract source of truth. `id`, `title`, `assignee`,
  `decision_key`, `base_commit`, `priority` and `chain_id` are denormalized columns
  for queries; insert writes both.
- `submissions` is the execution-result source of truth. `approved_submission_id`
  points at the submission that `review approve` accepted; `close` reads it.
- `checkpoints` stores per-generation progress. `events` is the append-only audit log.
- `decisions` is the current decision snapshot; `decision_revisions` is its history.

### 7.1 Migration

On first open, a schema 4 database is migrated in place:

1. Acquire `BEGIN IMMEDIATE`.
2. Copy the database to `state.db.v4-<UTC timestamp>.bak` through the SQLite backup
   API.
3. Create `tasks_v5`; map each old row to `contract_json`; move removed prose fields
   under `contract.legacy`; set `approved_submission_id` for `approved`/`closed`
   tasks; synthesize a submission when an old `result_json` has no history.
4. Drop/rename the table, recreate indexes, run `PRAGMA foreign_key_check`, update
   metadata and commit. Failure rolls back and leaves schema 4 intact.

Schema 5 is not readable by 2.4.3. To downgrade, stop the authority, restore the
generated v4 backup and start the older runtime. The generated backup is the only
supported rollback path.

## 8. Sessions, leases and concurrency

- A claim records `--session`. `checkpoint`, `submit`, `handoff` and `release` reject
  another window of the same executor identity.
- `execution.session_policy=required` makes a session mandatory for `claim`; an
  unbound legacy claim must be bound once with `heartbeat --session <id>`.
- `next`/`context` with a session show that session's work first, then unclaimed
  ready work. Without a session they skip session-bound tasks and report them under
  `excluded`.
- `--lease-minutes` makes expiry visible. Expiry is a warning; nothing is released
  automatically. An administrator may use `release --if-expired`.
- `next --available` and `claim` without a task ID take only unclaimed ready work for
  the caller and skip dependency-blocked tasks with a reason.

## 9. Routing and deployment

- With remote access enabled, any registered device except `state_authority.host`
  proxies through SSH. `--local` forces local execution.
- SSH failure returns the SSH exit code and never falls back to a local database.
- Deployment replaces managed files one by one, verifies every hash before the first
  replacement, retains the replaced generation, restores it when the deployed runtime
  cannot load the deployed configuration, and supports `--verify` and `--rollback`.
- Deployment never modifies SQLite state. Back up state separately, especially before
  a schema change.

## 10. Version and documentation policy

- `agent_hub.py` `VERSION` is the runtime source of truth.
- `CHANGELOG.md` must contain `## <VERSION> —` for the current runtime.
- README, project registration docs, SECURITY and templates do not repeat the version.
- `release-files.json` is the exact public file allowlist. The release scanner checks
  the manifest, content and history before publication.
