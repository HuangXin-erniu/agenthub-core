# Control-Plane Operating Standard

- Generic core only: no project names, agent IDs, device IDs, absolute paths, business code, data, models, environments, or databases.
- Project registrations define authority host, state DB, repository, worktree root, launcher, device permissions, and naming catalogue.
- Codex alone owns system governance, lifecycle, runtime, deployment, and cross-project policy.
- Executor tasks require immutable decision, full acceptance matrix, visible review note in next/show, acknowledgement checkpoint, and a single complete submission.
- An executor may hold several in-progress tasks only up to the project's configured limit; each task keeps its own active device, worktree, checkpoints and submission. `release` returns a stuck task to ready without deleting its work.
- Task IDs and chains are project-configured; sequence is global, monotonic, and never reused.
- cancel and supersede preserve history. Deployment occurs only after user-approved merge and production smoke verification.
