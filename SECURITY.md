# Security

## Trust boundary

AgentHub 2.4.1 is a control plane for agents and devices within a trusted environment. Agent and device IDs are caller-declared workflow identifiers, not cryptographic authentication. Git path validation checks submitted changes; it does not sandbox process execution, stop an executor from reading other files or restrict the executor's OS account. Use separate OS accounts, file permissions and sandboxing when needed.

The authority host owns SQLite state and canonical Git/worktree operations. Keep state and worktrees outside synchronized folders; back up state separately. Protect Git, Python, SSH configuration, launchers and project registrations from untrusted modification. Remote RPC relies on SSH access control and host identity. The internal `--local`, `--state` and RPC paths are trusted administration interfaces.

Treat configurations and task instructions as trusted inputs. Do not use arbitrary remote host names, shell fragments or unreviewed path values: deployment and Windows proxy adapters construct shell commands. Inspect `--dry-run`, use simple administrator-controlled paths, and retain strict SSH host-key checking. Deployment replaces files individually rather than as one transaction, but it verifies every hash before the first replacement, retains the generation it replaces, restores it if the deployed runtime cannot load the deployed configuration, and offers `--rollback`. Never use `--reset-state` on an authority whose history must be preserved.

## Publishing safely

Real project registrations, SSH host configuration, user paths, task state, logs, credentials and private data do not belong in this repository. Keep secrets in a credential manager, environment or separately secured SSH configuration. Do not place tokens in `project.json`, launcher arguments, task packets or evidence.

The release is built from an explicit file allowlist and a fresh Git history. The release scanner checks paths and common credential/privacy patterns in both the tree and history, and emits only rule names, filenames and line numbers. Pattern scanning is not proof that no unknown secret exists: review the full export before publication. Ignore rules cannot protect files already tracked or manually force-added.

## Reporting

Do not put credentials, confidential logs or exploit details in a public issue. Use GitHub's private vulnerability reporting feature when it is enabled on this repository. If it is unavailable, open a minimal issue asking for a private reporting channel without disclosing sensitive details. If a credential is exposed, revoke or rotate it first; deleting a Git file does not remove history or external copies.
