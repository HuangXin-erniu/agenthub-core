# Contributing

Keep this repository project-neutral. Do not submit private registrations, business code, databases, logs, datasets, model artifacts, credential files or copies of runtime deployments. Use temporary repositories and fictional identities in tests.

1. Make focused changes in a branch or isolated checkout.
2. Preserve the documented CLI/configuration contract, or document an intentional compatibility change.
3. Add behavioral tests for lifecycle, permissions, isolation or deployment changes.
4. Run `python -m unittest discover -s tests -v` and `python scripts/audit_agenthub.py`.
5. Review staged filenames and run `python scripts/check_release.py --staged` before committing. Run `python scripts/check_release.py --history` before pushing.
6. Keep English and Simplified Chinese READMEs aligned, including language links, commands and version information.
7. Explain the resulting behavior and validation in the pull request.

`release-files.json` is the exact public file allowlist. Update it deliberately when adding reusable source, documentation or tests; the scanner rejects unknown published paths, binary content and detected secrets. Avoid broad exclusions or disabling scanners to publish sensitive content. Git ignore rules are a convenience, not the release boundary.

Use a GitHub noreply email if you do not want to publish your personal address. Follow `.gitattributes` for line endings. CI has read-only repository permissions and runs the same checks on Windows and Linux. Do not run production deployment or authority smoke tests from CI.
