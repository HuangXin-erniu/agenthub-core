# AgentHub control-plane rules

This repository is a lightweight, multi-project AgentHub control plane. It must not
contain business-code mirrors, production databases, datasets, models or project
environments. The canonical semantics are in `docs/SPEC.md`.

At every new session:

1. Obtain the target `project_id` explicitly; never infer it from the last chat.
2. Read `projects/<project_id>/project.json` as the authority for Agent identities,
   roles, devices, execution host and paths.
3. Read `projects/<project_id>/AGENTS.md` plus exactly one role instruction declared
   by `project.json.instructions`.
4. Deciders and dual-role Agents start with `context`; executors start with `next`.
   Every concurrent window declares `--session <id>` and starts with
   `--session <id> next <agent> --available` or `--session <id> claim <agent>`.
5. Use only the current project's configured authority and worktree. Never reuse
   paths, state or contracts from another project.

AgentHub core commands and role semantics are project-neutral. Project names, Agent
IDs, device IDs, operating-system paths, SSH aliases and launchers belong only to
project registrations/adapters.

Roles are additive. One Agent may hold `decider` and `executor` (optionally
`administrator`); its `context` is a workbench with `role_mode`, one execution
`focus`, `counts`, `review_queue` and recent `decisions`. Self-review is allowed by
default and recorded as `self_review`; set `agents.<id>.self_review: forbid` to require
an independent reviewer. `show` and `status` restrict an Agent that has `executor`
and no review role to its own tasks.

`claim <agent> [task_id]` claims a named task or the next available one. `init` and
`claim-next` no longer exist. Executors modify only the assigned task's
authority-host worktree and use checkpoint plus handoff before changing device.
Contracts use the 2.0 fields: required `id`, `title`, `objective`, `assignee`,
`decision_key`, `base_commit`, `instructions`, `acceptance`, `allowed_paths`;
optional `forbidden_paths`, `depends_on`, `gates`, `artifacts`,
`allow_path_overlap`, `priority`, `chain_id`. Removed prose fields are not accepted
on new contracts.

Contracts may declare `depends_on`, numeric `gates` and `artifacts`; claim, submit and
close enforce them. `chain_id` is lineage rather than ordering. Only a project that
configures `execution.max_active_tasks_per_executor` above 1 may hold more than one
in-progress task per executor; each task keeps its own worktree, active device and
session. A long task stays owned by its window: keep it alive with `heartbeat` and
never `checkpoint`, `submit`, `handoff` or `release` another session's task. Sessions
do not isolate external shared state such as datasets; the project owns that
convention.

State schema 4 migrates to schema 5 on first open and writes a `v4` backup. Schema 5
is not readable by 2.4.3; restore the backup to downgrade. Messaging transports are
disabled; legacy transport code is not part of this repository.

Do not load full history or infer work from old chat. Ask the user before unclear,
destructive, credential, network, merge/push or external-service actions.

For core maintenance without a registered project, use an isolated checkout and
disposable test configurations. Never create or infer a real registration merely to
run tests. Public commits must contain only files declared in `release-files.json`;
run the structural audit, behavioral tests and release scanner before publishing.
