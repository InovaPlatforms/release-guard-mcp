"""Local git and xcconfig checks against throwaway repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from release_guard import repo


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def gitrepo(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("1")
    git(root, "add", ".")
    git(root, "commit", "-qm", "one")
    git(root, "checkout", "-qb", "feature")
    (root / "b.txt").write_text("2")
    git(root, "add", ".")
    git(root, "commit", "-qm", "two")
    git(root, "checkout", "-q", "main")
    return root


def test_commit_on_main_true_and_false(gitrepo: Path) -> None:
    main_sha = git(gitrepo, "rev-parse", "main")
    feature_sha = git(gitrepo, "rev-parse", "feature")
    assert repo.commit_on_branch(gitrepo, main_sha, ["origin/main", "main"]).is_ancestor is True
    result = repo.commit_on_branch(gitrepo, feature_sha, ["origin/main", "main"])
    assert result.is_ancestor is False and result.ref == "main"


def test_missing_commit_and_missing_refs(gitrepo: Path) -> None:
    assert repo.commit_on_branch(gitrepo, "deadbeef", ["main"]).is_ancestor is None
    result = repo.commit_on_branch(gitrepo, "HEAD", ["origin/release"])
    assert result.is_ancestor is None and "fetch" in result.note


@pytest.mark.parametrize("bad", ["--upload-pack=evil", "-rf", "a b", "HEAD..main", "x;rm -rf /"])
def test_revisions_cannot_become_git_options(bad: str) -> None:
    with pytest.raises(repo.RepoError):
        repo.validate_rev(bad)


def test_read_xcconfig_follows_includes_and_ignores_comments(tmp_path: Path) -> None:
    (tmp_path / "Config").mkdir()
    (tmp_path / "Config" / "AppVersion.xcconfig").write_text(
        "// versions\nMARKETING_VERSION = 2.88\nCURRENT_PROJECT_VERSION = 4 // bumped\n"
        "OTHER[sdk=iphoneos*] = ignored\n")
    nested = tmp_path / "Core"
    nested.mkdir()
    (nested / "AppVersion.xcconfig").write_text('#include "../Config/AppVersion.xcconfig"\nEXTRA = yes\n')
    values = repo.read_xcconfig(nested / "AppVersion.xcconfig")
    assert values == {"MARKETING_VERSION": "2.88", "CURRENT_PROJECT_VERSION": "4", "EXTRA": "yes"}


def test_find_files_skips_build_output(tmp_path: Path) -> None:
    (tmp_path / "DerivedData").mkdir()
    (tmp_path / "DerivedData" / "AppVersion.xcconfig").write_text("")
    (tmp_path / "App").mkdir()
    (tmp_path / "App" / "AppVersion.xcconfig").write_text("")
    assert [p.relative_to(tmp_path).as_posix() for p in repo.find_files(tmp_path, "AppVersion.xcconfig")] == \
        ["App/AppVersion.xcconfig"]


def test_version_tuple_orders_build_numbers_numerically() -> None:
    assert repo.version_tuple("10") > repo.version_tuple("9")
    assert repo.version_tuple("1.0.10") > repo.version_tuple("1.0.9")
    assert repo.version_tuple("") == (0,)
