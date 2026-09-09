"""Tests for the Git tool service.

The interesting behaviour is not "does git work" but what happens around it:
output that would otherwise flood the context is trimmed, a commit is not lost
when the machine has no configured identity, and a missing git reports itself
instead of raising. Every test stubs the subprocess boundary - no test needs a
real repository, a network, or a global git config.
"""

from __future__ import annotations

import pytest

from windows_mcp.coding import git_service


def _fake_git(handler):
    """Wrap a per-test handler and record every argv that crossed the boundary."""
    calls: list[list[str]] = []

    def runner(args, cwd, timeout=git_service.DEFAULT_TIMEOUT):
        calls.append(list(args))
        return handler(list(args))

    runner.calls = calls
    return runner


@pytest.fixture
def in_repo(monkeypatch, tmp_path):
    """Pretend *tmp_path* is the root of a repository."""
    monkeypatch.setattr(git_service, "_repo_root", lambda path: str(tmp_path))
    return str(tmp_path)


class TestStatus:
    def test_files_are_grouped_by_stage(self, monkeypatch, in_repo):
        def handler(args):
            if args[0] == "status":
                return (
                    0,
                    "## main...origin/main [ahead 1]\nM  src/staged.py\n M src/dirty.py\n?? notes.txt",
                    "",
                )
            return (0, "abc1234 tidy up (Ann, 2 hours ago)", "")

        monkeypatch.setattr(git_service, "_git", _fake_git(handler))
        out = git_service.status(in_repo)

        assert "Branch: main...origin/main [ahead 1]" in out
        assert "Last commit: abc1234 tidy up (Ann, 2 hours ago)" in out
        assert "Staged (1):" in out
        assert "M src/staged.py" in out
        assert "Unstaged (1):" in out
        assert "M src/dirty.py" in out
        assert "Untracked (1):" in out
        assert "notes.txt" in out

    def test_a_clean_tree_says_so(self, monkeypatch, in_repo):
        monkeypatch.setattr(
            git_service, "_git", _fake_git(lambda args: (0, "## main", "") if args[0] == "status" else (0, "abc1234 x (Ann, now)", ""))
        )
        assert "Working tree clean." in git_service.status(in_repo)

    def test_a_huge_change_set_is_capped(self, monkeypatch, in_repo):
        many = "\n".join(f"?? file{index}.txt" for index in range(60))

        def handler(args):
            if args[0] == "status":
                return (0, f"## main\n{many}", "")
            return (0, "abc1234 x (Ann, now)", "")

        monkeypatch.setattr(git_service, "_git", _fake_git(handler))
        out = git_service.status(in_repo, limit=10)

        assert "Untracked (60):" in out
        assert "... and 50 more" in out


class TestDiff:
    def test_a_long_patch_is_trimmed_with_a_note(self, monkeypatch, in_repo):
        patch = "\n".join(f"+line {index}" for index in range(500))

        def handler(args):
            if "--stat" in args:
                return (0, " src/a.py | 500 +++++", "")
            return (0, patch, "")

        monkeypatch.setattr(git_service, "_git", _fake_git(handler))
        out = git_service.diff(in_repo, max_lines=50)

        assert "src/a.py | 500" in out
        assert "showing 50 of 500 lines" in out

    def test_no_changes_is_not_an_error(self, monkeypatch, in_repo):
        monkeypatch.setattr(git_service, "_git", _fake_git(lambda args: (0, "", "")))
        assert "No unstaged changes." in git_service.diff(in_repo)

    def test_staged_and_file_reach_the_command_line(self, monkeypatch, in_repo):
        runner = _fake_git(lambda args: (0, "patch", ""))
        monkeypatch.setattr(git_service, "_git", runner)

        git_service.diff(in_repo, staged=True, file="src/a.py")

        assert runner.calls[-1] == ["diff", "--staged", "--", "src/a.py"]


class TestLog:
    def test_limit_and_pattern_reach_the_command_line(self, monkeypatch, in_repo):
        runner = _fake_git(lambda args: (0, "abc1234  2 hours ago  Ann  tidy up", ""))
        monkeypatch.setattr(git_service, "_git", runner)

        out = git_service.log(in_repo, limit=5, pattern="fix")

        assert "-n5" in runner.calls[-1]
        assert "--grep" in runner.calls[-1]
        assert "tidy up" in out

    def test_the_limit_is_capped(self, monkeypatch, in_repo):
        runner = _fake_git(lambda args: (0, "abc1234  now  Ann  x", ""))
        monkeypatch.setattr(git_service, "_git", runner)

        git_service.log(in_repo, limit=10_000)

        assert f"-n{git_service.HARD_LOG_LIMIT}" in runner.calls[-1]


class TestCommit:
    def test_a_message_is_required(self, monkeypatch, in_repo):
        monkeypatch.setattr(git_service, "_git", _fake_git(lambda args: (0, "", "")))
        assert git_service.commit(in_repo, message="  ").startswith("Error: mode=commit requires")

    def test_an_empty_index_is_reported(self, monkeypatch, in_repo):
        monkeypatch.setattr(git_service, "_git", _fake_git(lambda args: (0, "", "")))
        assert "nothing staged" in git_service.commit(in_repo, message="tidy up")

    def test_a_normal_commit_reports_what_it_saved(self, monkeypatch, in_repo):
        def handler(args):
            if args[:2] == ["diff", "--cached"]:
                return (0, "src/a.py\nsrc/b.py", "")
            if args[0] == "commit":
                return (0, "[main abc1234] tidy up", "")
            return (0, "", "")

        monkeypatch.setattr(git_service, "_git", _fake_git(handler))
        out = git_service.commit(in_repo, message="tidy up")

        assert "[main abc1234] tidy up" in out
        assert "2 file(s) staged" in out
        assert "not pushed" in out

    def test_a_missing_identity_does_not_lose_the_commit(self, monkeypatch, in_repo):
        """A fresh Windows box has no user.email; the work must still be saved."""

        def handler(args):
            if args[:2] == ["diff", "--cached"]:
                return (0, "src/a.py", "")
            if args[0] == "-c":
                return (0, "[main abc1234] tidy up", "")
            if args[0] == "commit":
                return (
                    128,
                    "",
                    "Author identity unknown\nfatal: unable to auto-detect email address",
                )
            if args[0] == "log":
                return (0, "Ann\x1fann@example.com", "")
            return (0, "", "")

        runner = _fake_git(handler)
        monkeypatch.setattr(git_service, "_git", runner)
        out = git_service.commit(in_repo, message="tidy up")

        assert "[main abc1234] tidy up" in out
        assert "committed as Ann <ann@example.com>" in out
        assert ["-c", "user.name=Ann"] == runner.calls[-1][:2]

    def test_the_invented_identity_is_the_last_resort(self, monkeypatch, in_repo):
        def handler(args):
            if args[:2] == ["diff", "--cached"]:
                return (0, "src/a.py", "")
            if args[0] == "-c":
                return (0, "[main abc1234] first", "")
            if args[0] == "commit":
                return (128, "", "Please tell me who you are")
            if args[0] == "log":
                return (128, "", "fatal: your current branch does not have any commits yet")
            return (0, "", "")

        monkeypatch.setattr(git_service, "_git", _fake_git(handler))
        out = git_service.commit(in_repo, message="first")

        assert git_service.FALLBACK_EMAIL in out

    def test_named_paths_are_staged_instead_of_everything(self, monkeypatch, in_repo):
        def handler(args):
            if args[:2] == ["diff", "--cached"]:
                return (0, "src/a.py", "")
            if args[0] == "commit":
                return (0, "[main abc1234] tidy up", "")
            return (0, "", "")

        runner = _fake_git(handler)
        monkeypatch.setattr(git_service, "_git", runner)
        git_service.commit(in_repo, message="tidy up", paths="src/a.py, src/b.py")

        assert runner.calls[0] == ["add", "--", "src/a.py", "src/b.py"]


class TestBranch:
    def test_branches_are_listed_with_the_current_one_marked(self, monkeypatch, in_repo):
        listing = "* main | origin/main | 2 hours ago\n  spike | | 3 days ago"
        monkeypatch.setattr(git_service, "_git", _fake_git(lambda args: (0, listing, "")))

        out = git_service.branch(in_repo)

        assert "Branches (2)" in out
        assert "* main | origin/main" in out

    def test_create_starts_a_new_branch(self, monkeypatch, in_repo):
        runner = _fake_git(lambda args: (0, "", "Switched to a new branch 'spike'"))
        monkeypatch.setattr(git_service, "_git", runner)

        out = git_service.branch(in_repo, name="spike", create=True)

        assert runner.calls[-1] == ["switch", "-c", "spike"]
        assert "Switched to a new branch" in out

    def test_a_failed_switch_is_reported(self, monkeypatch, in_repo):
        monkeypatch.setattr(
            git_service, "_git", _fake_git(lambda args: (1, "", "fatal: invalid reference: nope"))
        )
        assert "invalid reference" in git_service.branch(in_repo, name="nope")


class TestInfo:
    def test_it_answers_which_repo_and_whether_github_is_connected(
        self, monkeypatch, in_repo
    ):
        def fake_git(args, cwd, timeout=git_service.DEFAULT_TIMEOUT):
            joined = " ".join(args)
            if joined == "config --get user.name":
                return (0, "Ann", "")
            if joined == "config --get user.email":
                return (0, "ann@example.com", "")
            if joined.startswith("rev-parse --abbrev-ref"):
                return (0, "main", "")
            if joined.startswith("log -1"):
                return (0, "abc1234 tidy up (Ann, 2 hours ago)", "")
            if joined.startswith("status"):
                return (0, " M src/a.py", "")
            if joined.startswith("remote"):
                return (
                    0,
                    "origin\thttps://github.com/o/r.git (fetch)\n"
                    "origin\thttps://github.com/o/r.git (push)",
                    "",
                )
            return (0, "", "")

        def fake_exe(exe, args, cwd=None, timeout=git_service.DEFAULT_TIMEOUT):
            if exe == "git":
                return (0, "git version 2.53.0.windows.1", "")
            if args[:2] == ["auth", "status"]:
                return (0, "github.com\n  Logged in to github.com account octocat", "")
            return (0, "gh version 2.92.0", "")

        monkeypatch.setattr(git_service, "_git", fake_git)
        monkeypatch.setattr(git_service, "_run_exe", fake_exe)
        monkeypatch.setattr(
            git_service, "_discover_repos", lambda start, **kwargs: ["C:/code/other"]
        )

        out = git_service.info(in_repo)

        assert "git version 2.53" in out
        assert "Ann <ann@example.com>" in out
        assert "branch: main" in out
        assert "1 changed file(s)" in out
        assert "remote origin: https://github.com/o/r.git" in out
        assert "account octocat" in out
        assert "C:/code/other" in out

    def test_a_missing_gh_is_reported_not_fatal(self, monkeypatch, in_repo):
        def fake_exe(exe, args, cwd=None, timeout=git_service.DEFAULT_TIMEOUT):
            if exe == "gh":
                raise git_service.GitError("gh is not installed or not on PATH")
            return (0, "git version 2.53.0.windows.1", "")

        monkeypatch.setattr(git_service, "_git", _fake_git(lambda args: (0, "", "")))
        monkeypatch.setattr(git_service, "_run_exe", fake_exe)
        monkeypatch.setattr(git_service, "_discover_repos", lambda start, **kwargs: [])

        out = git_service.info(in_repo)

        assert "GitHub CLI: gh is not installed" in out
        assert "No other repositories found nearby." in out


class TestRepoDiscovery:
    def test_it_finds_repositories_and_does_not_descend_into_them(self, tmp_path):
        (tmp_path / "alpha" / ".git").mkdir(parents=True)
        (tmp_path / "alpha" / "nested" / ".git").mkdir(parents=True)
        (tmp_path / "beta").mkdir()
        (tmp_path / "node_modules" / "pkg" / ".git").mkdir(parents=True)

        found = git_service._discover_repos(str(tmp_path), depth=3)

        assert found == [str(tmp_path / "alpha")]

    def test_the_current_repo_is_skipped(self, tmp_path):
        (tmp_path / "alpha" / ".git").mkdir(parents=True)

        found = git_service._discover_repos(str(tmp_path), skip=str(tmp_path / "alpha"))

        assert found == []


class TestFailureModes:
    def test_a_path_that_does_not_exist_is_reported(self, tmp_path):
        missing = str(tmp_path / "nope")
        assert git_service.status(missing) == f"Error: path not found: {missing}"

    def test_a_missing_git_is_reported_not_raised(self, monkeypatch, tmp_path):
        def boom(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(git_service.subprocess, "run", boom)

        assert git_service.status(str(tmp_path)).startswith("Error: git is not installed")

    def test_a_directory_outside_a_repository_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            git_service, "_git", _fake_git(lambda args: (128, "", "fatal: not a git repository"))
        )
        assert "is not inside a git repository" in git_service.status(str(tmp_path))
