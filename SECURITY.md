# Security

## Trust boundary

AgentHub is a control plane for agents and devices within a trusted environment. Agent, device and session IDs are caller-declared workflow identifiers, not cryptographic authentication. Git path validation checks submitted changes; it does not sandbox process execution, stop an executor from reading other files or restrict the executor's OS account. Use separate OS accounts, file permissions and sandboxing when needed.

The authority host owns SQLite state and canonical Git/worktree operations. Keep state and worktrees outside synchronized folders; back up state separately. Protect Git, Python, SSH configuration, launchers and project registrations from untrusted modification. Remote RPC relies on SSH access control and host identity. The hidden `rpc` subcommand, `--local` and `--state` are trusted administration interfaces, not public API. Default JSON output can contain task metadata; protect logs and captured output accordingly.

State migration is a privileged write. The runtime copies schema 4 to `state.db.v4-<UTC timestamp>.bak` before migrating to schema 5. That backup contains task contracts, evidence and audit events: protect it like the database, and delete it only after the new state is proven. Schema 5 is not readable by 2.4.3; restore the backup to downgrade.

Treat configurations and task instructions as trusted inputs. Do not use arbitrary remote host names, shell fragments or unreviewed path values: deployment and Windows proxy adapters construct shell commands. Inspect `--dry-run`, use simple administrator-controlled paths, and retain strict SSH host-key checking. Deployment replaces files individually rather than as one transaction, but it verifies every hash before the first replacement, retains the generation it replaces, restores it if the deployed runtime cannot load the deployed configuration, and offers `--rollback`. Never use `--reset-state` on an authority whose history must be preserved.

## Publishing safely

Real project registrations, SSH host configuration, user paths, task state, logs, credentials and private data do not belong in this repository. Keep secrets in a credential manager, environment or separately secured SSH configuration. Do not place tokens in `project.json`, launcher arguments, task packets or evidence.

The release is built from an explicit file allowlist and a fresh Git history. The release scanner checks paths and common credential/privacy patterns in both the tree and history, and emits only rule names, filenames and line numbers. Pattern scanning is not proof that no unknown secret exists: review the full export before publication. Ignore rules cannot protect files already tracked or manually force-added.

## Reporting

Do not put credentials, confidential logs or exploit details in a public issue. Use GitHub's private vulnerability reporting feature when it is enabled on this repository. If it is unavailable, open a minimal issue asking for a private reporting channel without disclosing sensitive details. If a credential is exposed, revoke or rotate it first; deleting a Git file does not remove history or external copies.
