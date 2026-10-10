#!/usr/bin/env python3
"""Fail-closed public manifest and redacted credential/privacy checks."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "github-token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "aws-access-key": re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "api-token": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "credential-url": re.compile(r"https?://[^\s/@:]+:[^\s/@]+@"),
    "assigned-secret": re.compile(
        r"""(?i)["']?(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|secret[_-]?key)["']?\s*[:=]\s*["']([A-Za-z0-9_+/=-]{12,})["']"""
    ),
    "private-user-path": re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+[A-Za-z0-9_.-]+|/(?:Users|home)/[A-Za-z0-9_.-]+)"),
}
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL_DOMAINS = {"example.invalid", "example.com", "users.noreply.github.com"}


def load_manifest(content: bytes) -> set[str]:
    data = json.loads(content.decode("utf-8"))
    if not isinstance(data, list) or not data or not all(isinstance(item, str) for item in data):
        raise ValueError("invalid manifest")
    result = set(data)
    if len(result) != len(data) or "release-files.json" not in result:
        raise ValueError("invalid manifest")
    for item in result:
        path = PurePosixPath(item)
        if path.is_absolute() or "\\" in item or ".." in path.parts or str(path) != item:
            raise ValueError("invalid manifest path")
        if item.startswith(("projects/",)) and item != "projects/README.md":
            raise ValueError("registrations cannot be published")
    return result


def scan_content(name: str, content: bytes) -> list[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return [f"non-utf8:{name}"]
    if "\0" in text:
        return [f"binary:{name}"]
    findings = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for rule, pattern in RULES.items():
            if pattern.search(line):
                findings.append(f"{rule}:{name}:{line_no}")
        for match in EMAIL.finditer(line):
            domain = match.group().rsplit("@", 1)[-1].casefold()
            if domain not in ALLOWED_EMAIL_DOMAINS:
                findings.append(f"private-email:{name}:{line_no}")
    return findings


def validate_files(files: dict[str, tuple[str, bytes]]) -> list[str]:
    try:
        manifest = load_manifest(files["release-files.json"][1])
    except (KeyError, ValueError, TypeError):
        return ["manifest:missing-or-invalid"]
    findings = []
    for name in sorted(set(files) - manifest):
        findings.append(f"path:not-allowlisted:{name}")
    for name in sorted(manifest - set(files)):
        findings.append(f"path:missing:{name}")
    for name, (mode, content) in sorted(files.items()):
        if mode not in {"100644", "100755"}:
            findings.append(f"path:unsafe-mode:{name}")
        findings.extend(scan_content(name, content))
    return findings


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def tree_files(root: Path, revision: str) -> dict[str, tuple[str, bytes]]:
    result = {}
    for entry in git(root, "ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if not entry:
            continue
        header, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        name = raw_name.decode("utf-8")
        content = git(root, "cat-file", "blob", oid) if kind == "blob" else b""
        result[name] = (mode, content)
    return result


def staged_files(root: Path) -> dict[str, tuple[str, bytes]]:
    result = {}
    for entry in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        header, raw_name = entry.split(b"\t", 1)
        mode, oid, stage = header.decode("ascii").split()
        name = raw_name.decode("utf-8")
        if stage != "0":
            raise ValueError("unmerged index")
        content = b"" if mode == "160000" else git(root, "cat-file", "blob", oid)
        result[name] = (mode, content)
    return result


def disk_files(root: Path) -> dict[str, tuple[str, bytes]]:
    result = {}
    for target in root.rglob("*"):
        relative = target.relative_to(root)
        if ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        if target.is_symlink():
            result[relative.as_posix()] = ("120000", b"")
        elif target.is_file():
            result[relative.as_posix()] = ("100644", target.read_bytes())
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="Check the Git index")
    group.add_argument("--history", action="store_true", help="Check every reachable commit and author email")
    args = parser.parse_args(argv)
    findings: list[str] = []
    checked = 0
    try:
        if args.history:
            revisions = git(ROOT, "rev-list", "--all").decode("ascii").splitlines()
            if not revisions:
                raise ValueError("empty history")
            for revision in revisions:
                files = tree_files(ROOT, revision)
                findings.extend(validate_files(files))
                checked += len(files)
                metadata = git(ROOT, "show", "-s", "--format=%ae%n%ce%n%B", revision)
                findings.extend(scan_content("commit-metadata", metadata))
        else:
            files = staged_files(ROOT) if args.staged else disk_files(ROOT)
            findings = validate_files(files)
            checked = len(files)
    except (OSError, ValueError, subprocess.CalledProcessError):
        findings.append("scan:unavailable-or-invalid-input")
    print(json.dumps({"ok": not findings, "files_checked": checked, "findings": sorted(set(findings))}, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
