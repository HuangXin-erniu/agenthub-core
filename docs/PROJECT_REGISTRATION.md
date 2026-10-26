# AgentHub v2.4.3 project registration standard

## Boundary

AgentHub core owns role semantics, permissions, lifecycle, evidence and state schema. A project registration owns all concrete identities and infrastructure:

| Core concept | Project-defined value |
|---|---|
| project selector | `project_id` |
| Agent identity | keys under `agents` |
| device identity | keys under `devices` |
| authority host | `state_authority.host` |
| repository/worktrees | `execution.*` |
| SSH and operating system | `remote.*` |
| local role instructions | `instructions.*` |
| deployed adapter files | `deployment.managed_files` |

No concrete value from one registration may appear in core source or another registration.

## New project checklist

1. Copy `templates/project.example.json` into a private `projects/<project_id>/project.json`. Registrations are Git-ignored; never force-add them to this public repository. Replace every placeholder, including object keys.
2. Choose stable Agent IDs and map each to generic roles: `administrator`, `decider`, `executor`.
3. Register every authority/client device and restrict each Agent to allowed devices.
4. Select one authority-local state database and one canonical Git repository.
5. Select a worktree root outside cloud-synchronized control-plane storage.
6. Configure SSH alias, strict options and `remote.shell` as `windows_cmd` or `posix_sh`.
7. Create `AGENTS.md`, role files and an OS-specific launcher from `templates/`.
8. Declare every deployed file in `deployment.managed_files`, including README and role files. Extend the minimum template entries for your private adapter.
9. Run structural audit and unit tests.
10. Deploy with `python3 scripts/deploy_project.py --project <project_id>` without resetting state.
11. Run the authority-host smoke test before production tasks. It uses a temporary clone, state and worktree and does not invoke the private launcher. Validate the actual launcher separately under your change process.

## Concurrency

`execution.concurrency` defaults to `one_active_task_per_executor`. Set it to `multiple_tasks_per_executor` and declare `execution.max_active_tasks_per_executor` (1-32) to let one executor hold several in-progress tasks. An optional `agents.<id>.max_active_tasks` lowers the limit for one executor and may not exceed the project value. Only `in_progress` tasks count toward the limit; `blocked` and `review` tasks do not.

Each task keeps its own worktree, branch and active device, so concurrent tasks never share a working directory. `next --session <id> --available` returns the highest-priority ready task that nobody has claimed, `claim-next` selects and claims one atomically, and `release` returns a stuck `in_progress` task to `ready` without deleting its worktree. The current executor may release its own task from any device it is allowed to use; an administrator may release any task.

`execution.isolation` may only be `git_worktree_per_task`. A project may not run two active tasks in the same `chain_id`.

## Dual roles and self-review

Roles are additive: an Agent may list `decider`, `executor` and `administrator` together. A project can therefore model a single-Agent workflow without inventing a second identity. For such an Agent, `context` returns one workbench with `role_mode: hybrid`: `tasks` is the execution focus selected for the declared session, `overview` lists active tasks in the Agent's decision scope, and `review_queue` lists `blocked`/`review` tasks with a `self_review` flag. The decider and executor views are no longer mutually exclusive; a second window of the same Agent can review a task submitted by the first.

Self-review is allowed by default for backward compatibility. When an Agent approves or reworks a task it executed, the review event and `latest_review` record `self_review: true`. To require an independent reviewer, declare:

```json
"agents": {
  "<agent_id>": {
    "roles": ["administrator", "decider", "executor"],
    "devices": ["<authority_device_id>", "<client_device_id>"],
    "self_review": "forbid"
  }
}
```

`self_review` accepts `allow` (default) or `forbid`. With `forbid`, both `approve` and `rework` are rejected when the reviewing Agent is the one that executed the task; another Agent with review permission must act. The policy is agent-scoped, so one dual-role Agent in a mixed project can self-review while another cannot. It is accepted only when the Agent has `executor` and a review role (`decider` or `administrator`); on a single-role Agent it is a configuration error.

`show` restricts an Agent to its own assigned tasks only when it has `executor` and no review role (`decider` or `administrator`). An Agent that can review may inspect tasks assigned to other executors, which is what makes the hybrid workbench and independent review possible.

## Dependencies, gates and artifacts

A task contract may carry four optional fields. They are additive: a contract without them behaves exactly as before.

- `depends_on` is an array of earlier task IDs. `claim` and `claim-next` refuse a task until every dependency is `closed`; `next` and `next --available` skip it and list it under `excluded` with `reason: dependencies_unmet`. A dependency on a cancelled task cannot be posted. When a closed dependency's merge commit is not contained in the task's `base_commit`, the packet reports `dependencies.stale_base`; merge the target branch before working, or supersede with a newer base.
- `gates` declares numeric acceptance gates: `id`, `metric`, `operator` (`>=`, `>`, `<=`, `<`), `threshold`, optional `source`. A completed submission must include `gate_results` keyed by gate ID. The runtime computes pass/fail; a failed required gate rejects `completed`, and the executor must submit `blocked` with the measurement and the trade-off. `review approve` recomputes the gates.
- `artifacts` lists paths relative to `execution.artifact_root`. The project must configure `artifact_root` before a contract may declare artifacts, and the root may not be inside `worktree_root`. Every artifact must exist at `submit` and again at `close`.
- `allow_path_overlap` acknowledges that the contract's `allowed_paths` overlap an active task. `post` rejects an overlap by default; tasks serialized by `depends_on` do not need the flag. The acknowledgement is recorded in the `task.post` event.

`status --decision <key>` filters the task list to one decision, which is the durable grouping key for a line of work. `chain_id` remains a per-task lineage label; it does not order tasks and a chain may hold only one active task.

## Sessions and long tasks

`execution.session_policy` is `optional` by default. In that mode a claim may omit a session and legacy unbound claims keep working, but any task bound to a session is skipped by `next`/`context` calls that do not present that session and is rejected by `checkpoint`, `submit`, `handoff` and `release` from a different window.

Set it to `required` when several windows share one executor identity and a long task must not be disturbed. In that mode `claim` and `claim-next` require `--session <id>` (or `AGENTHUB_SESSION`), and an existing unbound claim must be bound once:

```bash
agent_hub.py --project <project_id> --device <authority_or_allowed_device> \
  --session <window_id> heartbeat <executor> <task_id> --lease-minutes 180
```

`heartbeat` updates `last_heartbeat_at` without writing a checkpoint. An administrator may bind an unbound claim on the executor's behalf; after binding, only the matching session may refresh or mutate it. `--lease-minutes` is optional: expiry is reported as `lease_expired`, never enforced by an automatic release. `release --if-expired` lets an administrator free only an expired claim.

Sessions fence workflow ownership, not external shared state. A project that lets one task write a dataset while another reads it must define its own atomic-publish and snapshot-pinning rules in the project instructions. Task checkpoints are scoped to the current claim: a re-claimed task exposes earlier checkpoints as `previous_checkpoint`, not as the current progress.

## Upgrading an existing registration

A deployment replaces the managed files on the authority. It never touches the state database, and a task that is in flight continues across it — that is asserted by `tests/test_upgrade.py`, which claims a task with the previous release's runtime and finishes it with the checked-out one.

```bash
python3 scripts/deploy_project.py --project <project_id> --dry-run     # sources, hashes, in-flight tasks
python3 scripts/deploy_project.py --project <project_id>              # deploy
python3 scripts/deploy_project.py --project <project_id> --verify     # registry vs deployed, by hash
```

Order of operations when upgrading:

1. Back the state database up in an SQLite-aware way (its `-wal` and `-shm` companions included), or stop the authority. A deployment does not do this for you.
2. Bump `agenthub_version` in the private `project.json` to the new version. The structural audit requires the registration and the runtime to agree, so a partial upgrade fails loudly rather than silently.
3. `--dry-run`. It reports the in-flight tasks as well as the files; add `--require-idle` to the deployment if you want it to refuse while any task is unfinished. In-flight tasks are safe by measurement, so the default is to report and proceed.
4. Deploy. Every staged file is hash-verified before any file is replaced; the generation being replaced is retained; and after the swap the deployed runtime is made to load the deployed configuration. If that check fails, the previous generation is restored automatically and the staging directory is left for inspection. Do not add `--require-idle` when a long task must continue.
5. `--verify` to confirm the deployed files match the registry.
6. If the registration uses `session_policy: required`, bind every in-flight claim before its next mutation: `heartbeat <executor> <task_id> --session <window_id>`. A run that was claimed before the upgrade has no session yet.
7. If the new version turns out to be wrong, `python3 scripts/deploy_project.py --project <project_id> --rollback` restores the retained generation and re-runs the self-check. One generation is retained, so this undoes the most recent deployment; restore state from your own backup if the change involved it.

What is guaranteed across versions, and what is checked: configuration schema 1 and database schema 4 stay compatible, and schema changes are additive only. Version 2.4.1 added the session columns; version 2.4.2 added `depends_on_json`, `gates_json`, `artifacts_json` and `allow_path_overlap`; version 2.4.3 adds no column and stores the self-review marker in the existing review event detail. Older runtimes ignore newer columns. Existing configurations that omit the concurrency and session keys keep the one-task-per-executor and session-optional behavior. `tests/test_upgrade.py` asserts that the previous release's runtime can still read state written by the current one, including state with two tasks in progress, so a release that needs to break that has to say so explicitly.

For 2.4.3, no contract or state migration is required. Existing Agents keep self-review allowed; set `agents.<id>.self_review: forbid` only when independent review is mandatory. `context` gains `role_mode`, `overview`, `review_queue` and a `self_review` block for dual-role Agents, while `tasks` and `excluded` keep their existing shape. `show` now permits an Agent with review permission to inspect another executor's task. The 2.4.2 contract rules still apply: `post` rejects `allowed_paths` overlaps with active tasks unless the contracts are sequenced by `depends_on` or set `allow_path_overlap: true`; `execution.artifact_root` is required only for contracts that declare `artifacts`; gate declarations only affect contracts that use them.

The `windows_cmd` adapter implements the same ordering and the same self-check, but only its generated scripts are tested here; it has not been exercised against a real Windows authority.

## Fictional example

The following is an example, not a built-in project:

```text
project_id: demo_app
authority device: build_host
client device: laptop_a
administrator: chief
executor: worker
authority repository: <authority-repository-path>
worktrees: <authority-worktree-path>
```

Commands from the control plane would be:

```bash
python3 agent_hub.py --project demo_app --device laptop_a context chief
python3 agent_hub.py --project demo_app --device laptop_a next worker
```

This example demonstrates substitution only. It must never be copied into another project's production configuration without validation.
