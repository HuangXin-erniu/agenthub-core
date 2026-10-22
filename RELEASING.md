# Releasing AgentHub Core

A release is an annotated Git tag plus a GitHub Release. Pushing a commit is not a release, and a tag without a GitHub Release is only half the record.

## Version scheme

- Use `MAJOR.MINOR.PATCH`; the tag is `v` followed by the version, for example `v2.4.1`.
- `agent_hub.py` `VERSION` is the source of truth. It must agree with:
  - `templates/project.example.json` `agenthub_version`
  - the `README.md` and `README.zh-CN.md` titles
  - `SECURITY.md`
  - the `docs/PROJECT_REGISTRATION.md` title
  - a matching `CHANGELOG.md` section
  - every private registration's `agenthub_version` when that registration is upgraded
- Configuration schema and database schema are versioned separately. State any compatibility impact in the changelog and upgrade notes.

The structural audit fails when these surfaces drift, so a partial version bump is detected before release.

## Release commit

1. Start from a clean `main` whose CI passed.
2. Bump the version in every file listed above.
3. Add a `CHANGELOG.md` section `## X.Y.Z — YYYY-MM-DD` with `Added`, `Fixed`, `Changed` and `Upgrade notes` as needed.
4. Update `release-files.json` when the public file set changes.
5. Run:

   ```sh
   python3 scripts/audit_agenthub.py
   python3 -m unittest discover -s tests -v
   python3 scripts/check_release.py --staged
   python3 scripts/check_release.py --history
   ```

   In a development checkout that contains private registrations, run the audit, tests and working-tree release scan in an isolated core copy; use `--staged` or `--history` for the real checkout.
6. Commit with `Release AgentHub Core X.Y.Z`.

## Tag and GitHub Release

```sh
git tag -a vX.Y.Z -m "AgentHub Core vX.Y.Z: <one-line summary>" HEAD
git push origin main
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

## Deployment is separate

A tag or GitHub Release never deploys anything. Deploy the matching runtime and registration with `scripts/deploy_project.py`, then run `--verify` and the isolated smoke test. Deployment never touches the SQLite state database; back state up separately when a release changes schema or requires a migration.
