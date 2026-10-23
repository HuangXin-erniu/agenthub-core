# Control-Plane Operating Standard

- Generic core only: no project names, agent IDs, device IDs, absolute paths, business code, data, models, environments, or databases.
- Project registrations define authority host, state DB, repository, worktree root, launcher, device permissions, and naming catalogue.
- Codex alone owns system governance, lifecycle, runtime, deployment, and cross-project policy.
- Executor tasks require immutable decision, full acceptance matrix, visible review note in next/show, acknowledgement checkpoint, and a single complete submission.
- An executor may hold several in-progress tasks only up to the project's configured limit; each task keeps its own active device, worktree, checkpoints and submission. `release` returns a stuck task to ready without deleting its work.
- A long task is owned by the window that claimed it. Declare a stable session per window, keep it alive with `heartbeat`, and never run `checkpoint`, `submit`, `handoff` or `release` against another session's task. Lease expiry is a warning for a human decision, not an automatic release.
- Sessions isolate workflow ownership, not external shared state. Projects that share datasets between a producer and a consumer define atomic publishing and snapshot pinning in their own instructions.
- Contracts may declare `depends_on`, numeric `gates` and `artifacts`; `claim`, `submit` and `close` enforce them. `status --decision <key>` lists a decision line.
- Task IDs are project-configured; sequence is global, monotonic, and never reused. `chain_id` is a lineage label, not an ordering mechanism; `depends_on` expresses ordering.
- cancel and supersede preserve history. Deployment occurs only after user-approved merge and production smoke verification.
