# AgentHub v2.3.2 project registration standard

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
