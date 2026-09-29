"""Local repository checks: xcconfig versions, commit ancestry, Info.plist keys.

Everything here is read-only. git is called without a shell, with a timeout, and
every user-supplied revision is validated first so it cannot become a git option.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_REV = re.compile(r"^(?!-)[A-Za-z0-9._/@^~{}-]{1,200}$")
_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?\s*=\s*(.*?)\s*;?\s*$")
_INCLUDE = re.compile(r'^\s*#include\??\s+"([^"]+)"')
_SKIP_DIRS = {".git", "node_modules", "DerivedData", "build", ".build", "Pods", ".venv", "artifacts"}


class RepoError(RuntimeError):
    pass


def validate_rev(rev: str) -> str:
    if not _REV.match(rev) or ".." in rev:
        raise RepoError(f"not a valid git revision: {rev!r}")
    return rev


def _git(repo: Path, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                          check=False)


def is_git_repo(repo: Path) -> bool:
    try:
        return _git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def resolve(repo: Path, rev: str) -> str | None:
    out = _git(repo, "rev-parse", "--verify", "--quiet", validate_rev(rev) + "^{commit}")
    return out.stdout.strip() or None if out.returncode == 0 else None


@dataclass
class Ancestry:
    commit: str | None
    ref: str
    ref_sha: str | None
    is_ancestor: bool | None
    note: str


def commit_on_branch(repo: Path, commit: str, refs: list[str]) -> Ancestry:
    """Is `commit` contained in the first ref of `refs` that exists locally?"""
    sha = resolve(repo, commit)
    if not sha:
        return Ancestry(None, refs[0], None, None, f"commit {commit!r} not found in the local repository")
    for ref in refs:
        ref_sha = resolve(repo, ref)
        if not ref_sha:
            continue
        out = _git(repo, "merge-base", "--is-ancestor", sha, ref_sha)
        if out.returncode not in (0, 1):
            return Ancestry(sha, ref, ref_sha, None, "git merge-base failed")
        return Ancestry(sha, ref, ref_sha, out.returncode == 0, "")
    return Ancestry(sha, refs[0], None, None, f"none of {refs} exist locally (fetch first)")


def read_xcconfig(path: Path, _depth: int = 0) -> dict[str, str]:
    """Parse KEY = VALUE lines, following #include like Xcode does (later wins)."""
    if _depth > 8:
        raise RepoError("xcconfig #include nesting too deep")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.split("//", 1)[0].rstrip()
        inc = _INCLUDE.match(stripped)
        if inc:
            target = (path.parent / inc.group(1)).resolve()
            if target.is_file():
                values.update(read_xcconfig(target, _depth + 1))
            continue
        match = _ASSIGN.match(stripped)
        if match and not match.group(2):  # ignore conditional [sdk=...] assignments
            values[match.group(1)] = match.group(3).strip().strip('"')
    return values


def find_files(repo: Path, name: str, max_depth: int = 5) -> list[Path]:
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not entry.name.endswith((".xcarchive", ".app")):
                    walk(entry, depth + 1)
            elif entry.name == name:
                found.append(entry)

    walk(repo, 0)
    return found


def version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value or "")
    return tuple(int(p) for p in parts) or (0,)


def plist_value(path: Path, key: str) -> object | None:
    try:
        with path.open("rb") as fh:
            return plistlib.load(fh).get(key)
    except (OSError, plistlib.InvalidFileException):
        return None
