"""Tests for ``Edit mode=patch``: applying unified diffs to real files.

A diff is the cheapest way to express a multi-hunk change, but it is only safe
if hunks are located by their *context* rather than by trusting line numbers,
and if a hunk that does not match aborts the whole batch instead of writing
half of it. These tests pin both properties.
"""

from __future__ import annotations

from windows_mcp.coding import edit_service

BASE = "\n".join(
    [
        "def alpha():",
        '    return "one"',
        "",
        "",
        "def beta():",
        '    return "two"',
        "",
        "",
        "def gamma():",
        '    return "three"',
        "",
    ]
)

ALPHA_HUNK = "\n".join(
    [
        "@@ -1,2 +1,2 @@",
        " def alpha():",
        '-    return "one"',
        '+    return "ONE"',
    ]
)


def _write(tmp_path, name="sample.py", text=BASE):
    target = tmp_path / name
    target.write_text(text, encoding="utf-8", newline="")
    return target


def _apply(target, patch, **kwargs):
    return edit_service.apply_edits(
        [{"file": str(target), "mode": "patch", "patch": patch}], backup=False, **kwargs
    )


class TestApply:
    def test_single_hunk_applies(self, tmp_path):
        target = _write(tmp_path)
        report = _apply(target, ALPHA_HUNK)
        text = target.read_text(encoding="utf-8")
        assert report.startswith("Applied: 1 edit(s)")
        assert 'return "ONE"' in text
        assert 'return "two"' in text

    def test_hunk_survives_drifted_line_numbers(self, tmp_path):
        target = _write(tmp_path)
        patch = "\n".join(
            [
                "@@ -40,2 +40,2 @@",
                " def beta():",
                '-    return "two"',
                '+    return "TWO"',
            ]
        )
        _apply(target, patch)
        assert 'return "TWO"' in target.read_text(encoding="utf-8")

    def test_multiple_hunks_in_one_edit(self, tmp_path):
        target = _write(tmp_path)
        patch = "\n".join(
            [
                "diff --git a/sample.py b/sample.py",
                "--- a/sample.py",
                "+++ b/sample.py",
                "@@ -1,2 +1,3 @@",
                " def alpha():",
                '-    return "one"',
                "+    # first",
                '+    return "ONE"',
                "@@ -9,2 +10,2 @@",
                " def gamma():",
                '-    return "three"',
                '+    return "THREE"',
            ]
        )
        _apply(target, patch)
        text = target.read_text(encoding="utf-8")
        assert "# first" in text
        assert 'return "ONE"' in text
        assert 'return "THREE"' in text
        assert 'return "two"' in text

    def test_context_only_hunk_inserts(self, tmp_path):
        target = _write(tmp_path)
        _apply(target, "\n".join(["@@ -5,1 +5,2 @@", " def beta():", "+    # noqa"]))
        lines = target.read_text(encoding="utf-8").split("\n")
        assert lines[4] == "def beta():"
        assert lines[5] == "    # noqa"

    def test_patch_can_be_passed_as_new(self, tmp_path):
        target = _write(tmp_path)
        report = edit_service.apply_edits(
            [{"file": str(target), "mode": "patch", "new": ALPHA_HUNK}], backup=False
        )
        assert report.startswith("Applied:")
        assert 'return "ONE"' in target.read_text(encoding="utf-8")

    def test_crlf_is_preserved(self, tmp_path):
        target = tmp_path / "crlf.py"
        target.write_bytes(BASE.replace("\n", "\r\n").encode("utf-8"))
        _apply(target, ALPHA_HUNK)
        blob = target.read_bytes()
        assert b'return "ONE"' in blob
        assert b"\r\n" in blob
        assert b"\n" not in blob.replace(b"\r\n", b"")


class TestRefusals:
    def test_mismatch_aborts_the_whole_batch(self, tmp_path):
        first = _write(tmp_path, "a.py")
        second = _write(tmp_path, "b.py")
        bad = "\n".join(
            [
                "@@ -1,2 +1,2 @@",
                " def missing():",
                '-    return "nope"',
                '+    return "NOPE"',
            ]
        )
        report = edit_service.apply_edits(
            [
                {"file": str(first), "mode": "patch", "patch": ALPHA_HUNK},
                {"file": str(second), "mode": "patch", "patch": bad},
            ],
            backup=False,
        )
        assert report.startswith("Error:")
        assert "hunk #1" in report
        assert first.read_text(encoding="utf-8") == BASE
        assert second.read_text(encoding="utf-8") == BASE

    def test_applying_the_same_patch_twice_fails(self, tmp_path):
        target = _write(tmp_path)
        assert _apply(target, ALPHA_HUNK).startswith("Applied:")
        assert _apply(target, ALPHA_HUNK).startswith("Error:")
        assert target.read_text(encoding="utf-8").count('return "ONE"') == 1

    def test_bad_hunk_header_is_rejected(self, tmp_path):
        target = _write(tmp_path)
        report = _apply(target, "@@ nonsense @@\n-x\n+y")
        assert "unrecognized hunk header" in report
        assert target.read_text(encoding="utf-8") == BASE

    def test_patch_without_hunks_is_rejected(self, tmp_path):
        target = _write(tmp_path)
        assert "no '@@' hunks found" in _apply(target, "just some prose, no hunks")

    def test_dry_run_reports_the_diff_without_writing(self, tmp_path):
        target = _write(tmp_path)
        report = _apply(target, ALPHA_HUNK, dry_run=True)
        assert report.startswith("Dry run:")
        assert '+    return "ONE"' in report
        assert target.read_text(encoding="utf-8") == BASE
