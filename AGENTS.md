# AgentHub v2.4.3 control-plane rules

This repository is a lightweight, multi-project AgentHub control plane. It must not contain business-code mirrors, production databases, datasets, models or project environments.

At every new session:

1. Obtain the target `project_id`; never infer it from the last chat.
2. Read `projects/<project_id>/project.json` as the authority for Agent identities, roles, devices, execution host and paths.
3. Read `projects/<project_id>/AGENTS.md` plus exactly one role instruction declared by `project.json.instructions`.
4. Deciders and dual-role Agents start with `context`; executors start with `next`. Every concurrent window declares its own `--session <id>` and starts with `next --session <id> --available` or `claim-next --session <id>`. `release` returns a stuck task to `ready`.
5. Use only the current project's configured authority and worktree. Never reuse paths, state or contracts from another project.

AgentHub core commands and role semantics are project-neutral. Project names, Agent IDs, device IDs, operating-system paths, SSH aliases and launchers belong only to project registrations/adapters.

Roles are additive. One Agent may hold `decider` and `executor` (optionally `administrator`); its `context` is a unified workbench with `role_mode`, execution focus, `overview` and `review_queue`. Self-review is allowed by default and recorded as `self_review`; set `agents.<id>.self_review: forbid` to require an independent reviewer. The key applies only when the Agent has both an execution and a review role. `show` restricts only Agents that have `executor` and no review role.

Deciders use `show` before handling blocked/review tasks. Executors modify only the assigned task's authority-host worktree and use checkpoint plus handoff before changing device. Contracts may declare `depends_on`, numeric `gates` and `artifacts`; claim, submit and close enforce them, and `chain_id` is lineage rather than ordering. Only a project that configures concurrency may hold more than one in-progress task per executor; each task keeps its own worktree, active device and session. A long task stays owned by its window: keep it alive with `heartbeat` and never `checkpoint`, `submit`, `handoff` or `release` another session's task. Sessions do not isolate external shared state such as datasets; the project owns that convention. Messaging transports are disabled; legacy transport code is not part of this repository.

Do not load full history or infer work from old chat. Ask the user before unclear, destructive, credential, network, merge/push or external-service actions.

For core maintenance without a registered project, use an isolated checkout and disposable test configurations. Never create or infer a real registration merely to run tests. Public commits must contain only files declared in `release-files.json`; run the structural audit, behavioral tests and release scanner before publishing.
