# AgentHub project registration standard

## Boundary

AgentHub core owns role semantics, lifecycle, permissions, contract validation, state
schema and the JSON envelope. A project registration owns every concrete identity and
piece of infrastructure.

| Core concept | Project-defined value |
|---|---|
| project selector | `project_id` |
| Agent identity | keys under `agents` |
| device identity | keys under `devices` |
| authority host | `state_authority.host` |
| repository and worktrees | `execution.*` |
| SSH and operating system | `remote.*` |
| local role instructions | `instructions.*` |
| deployed adapter files | `deployment.managed_files` |
| task/chain naming | `task_naming` (optional, generic) |

No concrete value from one registration may appear in core source or another
registration.

## New project checklist

1. Copy `templates/project.example.json` to a private
   `projects/<project_id>/project.json`. Registrations are Git-ignored; never force-add
   them to the public repository. Replace every placeholder, including object keys.
2. Choose stable Agent IDs and map each to generic roles: `administrator`, `decider`
   and/or `executor`. Roles are additive.
3. Register every authority/client device and restrict each Agent to allowed devices.
4. Select one authority-local state database and one canonical Git repository.
5. Select a worktree root outside cloud-synchronized control-plane storage. Set
   `execution.artifact_root` only if contracts will declare `artifacts`; it must not
   be inside the worktree root.
6. Configure SSH alias, strict options and `remote.shell` as `windows_cmd` or
   `posix_sh`; set the matching `deployment.adapter`.
7. Create `AGENTS.md`, role files and an OS-specific launcher from `templates/`.
8. Declare every deployed file in `deployment.managed_files`, including README and
   role files.
9. Set `execution.max_active_tasks_per_executor` (default 1) and optionally
   `agents.<id>.max_active_tasks`.
10. Run the structural audit and tests, then deploy without resetting state.
11. Run the authority-host smoke test before production tasks. It clones the
    repository into a temporary directory and uses disposable state and worktrees; it
    never runs the private launcher.

The mirror check ignores local tool and OS metadata under the registration directory:
`.zcode`, `.git`, `__pycache__`, `.DS_Store` and `*.pyc`. Every other undeclared file is
reported as `mirror:unexpected`.

## Configuration

See [the specification](SPEC.md#6-project-configuration) for the canonical key set and
defaults. Removed 2.4.3 keys are ignored for compatibility, but a new registration
must not include them. In particular:

- Use `execution.max_active_tasks_per_executor`; do not use `execution.concurrency`.
- Use the generic `task_naming` keys only:
  `task_pattern`, `chain_pattern`, `seq_digits`. The core does not interpret
  phase/domain/type vocabulary.
- Do not include `agenthub_version`, `project_name`, `workflow`,
  `messaging_transports`, `lark_workflow` or `execution.isolation`.

## Sessions, concurrency and long tasks

`execution.session_policy` defaults to `optional`. Set it to `required` when several
windows share one executor identity and a long task must not be disturbed. In that
mode `claim` requires `--session <id>` (or `AGENTHUB_SESSION`), and an existing
unbound claim must be bound once:

```sh
agenthub --session <window_id> heartbeat <executor> <task_id>
```

Each task keeps its own worktree, branch and active device. `claim <agent>` without a
task ID selects the next available task; `release` returns a stuck in-progress task to
`ready` without deleting its worktree. Only `in_progress` tasks consume a slot.

## Upgrading a registration

1. Back up the authority SQLite database in an SQLite-aware way.
2. Install the runtime and update role instructions for the current command surface
   (`init` and `claim-next` are removed).
3. The first command migrates schema 4 to schema 5 after writing a `v4` backup.
4. Remove the old optional keys listed above.
5. Run the audit, tests and isolated smoke check.
6. Deploy with `scripts/deploy_project.py`, then run `--verify` and the authority smoke
   test. Deployment never touches state.

Schema 5 is not readable by 2.4.3. To roll back, stop the authority, restore the
generated `state.db.v4-<timestamp>.bak`, and run the older runtime.
