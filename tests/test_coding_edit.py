"""Tests for windows_mcp.coding.edit_service (surgical file editing)."""

import pytest

from windows_mcp.coding import edit_service


def write(path, text, newline="\n", encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline=newline) as handle:
        handle.write(text)
    return str(path)


def read(path, encoding="utf-8"):
    with open(path, "r", encoding=encoding, newline="") as handle:
        return handle.read()


@pytest.fixture()
def sample(tmp_path):
    return write(
        tmp_path / "sample.py",
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
    )


@pytest.fixture()
def crlf_file(tmp_path):
    body = "\r\n".join(f"line {index}" for index in range(1, 11)) + "\r\n"
    return write(tmp_path / "crlf.txt", body, newline="")


class TestView:
    def test_reports_metadata(self, crlf_file):
        rendered = edit_service.view(crlf_file)
        assert "Lines 1-10 of 10" in rendered
        assert "EOL CRLF" in rendered
        assert "sha256 " in rendered
        assert "     3| line 3" in rendered

    def test_slice(self, crlf_file):
        assert "Lines 9-10 of 10" in edit_service.view(crlf_file, start=9)

    def test_missing_file(self, tmp_path):
        assert edit_service.view(str(tmp_path / "nope.txt")).startswith("Error:")


class TestReplace:
    def test_ambiguous_match_is_rejected(self, crlf_file):
        before = read(crlf_file)
        report = edit_service.apply_edits([{"file": crlf_file, "old": "line 1", "new": "x"}])
        assert report.startswith("Error:")
        assert read(crlf_file) == before

    def test_count_all(self, crlf_file):
        report = edit_service.apply_edits(
            [{"file": crlf_file, "old": "line ", "new": "row ", "count": "all"}], backup=False
        )
        assert not report.startswith("Error:")
        assert "row 7" in read(crlf_file)

    def test_crlf_is_preserved(self, crlf_file):
        edit_service.apply_edits(
            [{"file": crlf_file, "old": "line 4", "new": "LINE FOUR"}], backup=False
        )
        content = read(crlf_file)
        assert content.count("\r\n") == 10
        assert "\n" not in content.replace("\r\n", "")

    def test_noop_is_reported(self, sample):
        report = edit_service.apply_edits(
            [{"file": sample, "old": "return 1", "new": "return 1"}]
        )
        assert "NO-OP" in report

    def test_unicode_roundtrip(self, tmp_path):
        path = write(tmp_path / "u.txt", "\u041f\u0440\u0438\u0432\u0435\u0442 \u2014 \u043c\u0438\u0440 \U0001f680\n")
        edit_service.apply_edits(
            [{"file": path, "old": "\u043c\u0438\u0440", "new": "world"}], backup=False
        )
        content = read(path)
        assert "world" in content
        assert "\U0001f680" in content and "\u2014" in content


class TestPreconditions:
    def test_correct_digest_is_accepted(self, sample):
        digest = edit_service.file_digest(sample)
        report = edit_service.apply_edits(
            [{"file": sample, "old": "return 1", "new": "return 11", "expected_sha256": digest}],
            backup=False,
        )
        assert not report.startswith("Error:")
        assert "return 11" in read(sample)

    def test_stale_digest_is_rejected(self, sample):
        before = read(sample)
        report = edit_service.apply_edits(
            [{"file": sample, "old": "return 1", "new": "x", "expected_sha256": "deadbeef"}]
        )
        assert report.startswith("Error:")
        assert read(sample) == before

    def test_dry_run_writes_nothing(self, sample):
        before = read(sample)
        report = edit_service.apply_edits(
            [{"file": sample, "old": "return 1", "new": "return 99"}], dry_run=True
        )
        assert "DRY-RUN" in report
        assert read(sample) == before

    def test_batch_is_atomic(self, tmp_path, sample):
        other = write(tmp_path / "other.txt", "keep me\n")
        before_sample, before_other = read(sample), read(other)
        report = edit_service.apply_edits([
            {"file": sample, "old": "return 2", "new": "return 22"},
            {"file": other, "old": "NOT-PRESENT", "new": "x"},
        ])
        assert report.startswith("Error:")
        assert read(sample) == before_sample
        assert read(other) == before_other

    def test_missing_file_is_reported(self, tmp_path):
        report = edit_service.apply_edits(
            [{"file": str(tmp_path / "ghost.txt"), "old": "a", "new": "b"}]
        )
        assert report.startswith("Error:")


class TestModes:
    def test_regex_backreference(self, tmp_path):
        path = write(tmp_path / "a.ts", "const item_1 = 1\nconst item_2 = 2\n")
        report = edit_service.apply_edits(
            [{
                "file": path,
                "mode": "regex",
                "old": r"item_(\d+)",
                "new": r"widget_\1",
                "count": "all",
            }],
            backup=False,
        )
        assert not report.startswith("Error:")
        content = read(path)
        assert "widget_2" in content and "item_" not in content

    def test_regex_count_mismatch(self, tmp_path):
        path = write(tmp_path / "a.ts", "const item_1 = 1\nconst item_2 = 2\n")
        report = edit_service.apply_edits(
            [{"file": path, "mode": "regex", "old": r"item_\d+", "new": "x", "count": 1}]
        )
        assert report.startswith("Error:")

    def test_lines_insert_append(self, sample):
        report = edit_service.apply_edits([
            {"file": sample, "mode": "lines", "start": 1, "end": 1, "new": "def alpha(x):"},
            {"file": sample, "mode": "insert_after", "old": "def alpha(x):", "new": "    # noqa"},
            {"file": sample, "mode": "append", "new": "# tail"},
            {"file": sample, "mode": "prepend", "new": "# head"},
        ], backup=False)
        assert not report.startswith("Error:")
        lines = read(sample).split("\n")
        assert lines[0] == "# head"
        assert lines[1] == "def alpha(x):"
        assert lines[2] == "    # noqa"
        assert read(sample).rstrip().endswith("# tail")

    def test_delete_lines(self, crlf_file):
        edit_service.apply_edits(
            [{"file": crlf_file, "mode": "delete_lines", "start": 1, "end": 2}], backup=False
        )
        rows = [row for row in read(crlf_file).split("\r\n") if row]
        assert rows[0] == "line 3"
        assert "line 1" not in rows and "line 2" not in rows
        assert len(rows) == 8

    def test_create_and_overwrite(self, tmp_path):
        target = str(tmp_path / "nested" / "deep" / "new.py")
        report = edit_service.apply_edits(
            [{"file": target, "mode": "create", "new": "print('hi')\n"}], backup=False
        )
        assert not report.startswith("Error:")
        assert read(target) == "print('hi')\n"

        assert edit_service.apply_edits(
            [{"file": target, "mode": "create", "new": "x"}]
        ).startswith("Error:")

        report = edit_service.apply_edits(
            [{"file": target, "mode": "create", "new": "y\n", "overwrite": True}], backup=False
        )
        assert not report.startswith("Error:")
        assert read(target) == "y\n"

    def test_unknown_mode_is_rejected(self, sample):
        report = edit_service.apply_edits([{"file": sample, "mode": "teleport", "new": "x"}])
        assert report.startswith("Error:")


class TestBackups:
    def test_backup_is_created(self, sample):
        report = edit_service.apply_edits(
            [{"file": sample, "old": "return 1", "new": "return 1000"}], backup=True
        )
        assert not report.startswith("Error:")
        assert "backup" in report.lower()
