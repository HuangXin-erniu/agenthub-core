# AgentHub project registrations

Each `projects/<project_id>/` directory is a lightweight adapter for one independent project. It may contain:

- `project.json`: identities, roles, devices, authority, repository, worktree, SSH and deployment mapping.
- `AGENTS.md` and role files: project-specific operating rules.
- a generated launcher appropriate to the authority operating system.
- lightweight instance documentation and connector-specific instructions.

It must not contain business source mirrors, datasets, models, production state databases, environments, caches or execution outputs.

AgentHub core never discovers project configuration from a business repository or a previous chat. Callers select `--project <project_id>` explicitly. Adding another project means adding another independent registration; it does not require changing core code.
