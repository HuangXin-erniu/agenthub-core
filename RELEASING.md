# Releasing AgentHub Core

A release is an annotated Git tag plus a GitHub Release. Pushing a commit is not a release, and a tag without a GitHub Release is only half the record.

## Version scheme

- Use `MAJOR.MINOR.PATCH`; the tag is `v` followed by the version, for example `v2.4.5`.
- `agent_hub.py` `VERSION` is the source of truth. It must agree with a matching
  `CHANGELOG.md` section `## X.Y.Z — YYYY-MM-DD`. README, SECURITY, registration docs
  and the template do not repeat the version.
- Configuration schema and database schema are versioned separately. State any
  compatibility impact in the changelog and upgrade notes.

The structural audit fails when `VERSION` has no matching changelog section.

## Release commit

1. Start from a clean `main` whose CI passed.
2. Bump `VERSION` and add a `CHANGELOG.md` section `## X.Y.Z — YYYY-MM-DD` with
   `Breaking`, `Added`, `Fixed`, `Changed`, `Removed` and `Upgrade notes` as needed.
3. Update `release-files.json` when the public file set changes.
4. Run:

   ```sh
   python3 scripts/audit_agenthub.py
   python3 -m unittest discover -s tests -v
   python3 scripts/check_release.py --staged
   python3 scripts/check_release.py --history
   ```

   In a development checkout that contains private registrations, run the audit, tests and working-tree release scan in an isolated core copy; use `--staged` or `--history` for the real checkout.
5. Commit with `Release AgentHub Core X.Y.Z`, push `main`, and wait for CI on that
   commit to pass before tagging.

## Tag and GitHub Release

```sh
git push origin main
# wait for CI on that commit to pass
git tag -a vX.Y.Z -m "AgentHub Core vX.Y.Z: <one-line summary>" HEAD
git push origin vX.Y.Z
```

Create a GitHub Release whose title is `AgentHub Core vX.Y.Z` and whose notes are the matching `CHANGELOG.md` section:

```sh
gh release create vX.Y.Z --title "AgentHub Core vX.Y.Z" --notes-file /tmp/notes-X.Y.Z.md
```

- Use annotated tags only. Never move, delete or force-push a published tag.
- The newest release is `Latest`. When backfilling an older version, pass `--latest=false` so the current release keeps the flag.
- Keep the release notes factual and identical in intent to the changelog. For a version that predates the changelog, write a short summary from the commit and README.

## Historical releases

The public history starts at 2.3.1. Versions before it are not in this repository and cannot be tagged here.

| Tag | Commit | Note |
| --- | --- | --- |
| v2.3.1 | 015ba47 | initial public core-only snapshot |
| v2.3.2 | 0e7e659 | tagged at release time |
| v2.3.3 | 094c68b | backfilled |
| v2.3.4 | 906434c | backfilled |
| v2.4.0 | 5c643f9 | tagged at release time |
| v2.4.1 | 271a736 | tagged at release time |
| v2.4.2 | 19d404c | tagged at release time |
| v2.4.3 | e35b6c5 | tagged at release time |

## Deployment is separate

A tag or GitHub Release never deploys anything. Deploy the matching runtime and registration with `scripts/deploy_project.py`, then run `--verify` and the isolated smoke test. Deployment never touches the SQLite state database; back state up separately when a release changes schema or requires a migration.
