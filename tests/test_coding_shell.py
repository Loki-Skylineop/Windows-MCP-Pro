"""Tests for windows_mcp.coding.shell_service: CLIXML stderr and the timeout clamp."""

import pytest

from windows_mcp.coding import shell_service

CLIXML_SAMPLE = (
    "#< CLIXML\r\n"
    '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
    '<S S="Error">exit $global:__wm_code : boom_x000D__x000A_</S>'
    '<S S="Error">    + CategoryInfo : NotSpecified_x000D__x000A_</S>'
    "</Objs>"
)

WRAPPER_SCRIPT = (
    "$ErrorActionPreference = 'Continue'\n"
    "Write-Error 'boom'\n"
    "exit $global:__wm_code"
)


class _Completed:
    def __init__(self, stdout=b"ok", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def captured_run(monkeypatch):
    """Replace the real subprocess call and capture the kwargs it was given."""
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured.update(kwargs)
        return _Completed()

    monkeypatch.setattr(shell_service, "run_with_graceful_timeout", _fake_run)
    return captured


class TestClixmlDecoding:
    def test_plain_text_is_untouched(self):
        assert shell_service._decode_clixml("error: boom") == "error: boom"

    def test_payload_is_decoded(self):
        out = shell_service._decode_clixml(CLIXML_SAMPLE)
        assert "#< CLIXML" not in out
        assert "<Objs" not in out
        assert "<S " not in out
        assert "boom" in out

    def test_escapes_become_newlines(self):
        out = shell_service._decode_clixml(CLIXML_SAMPLE)
        assert "_x000D_" not in out
        assert out.count("\n") == 1

    def test_entities_are_unescaped(self):
        payload = '#< CLIXML\n<Objs><S S="Error">a &lt;b&gt; &amp; c</S></Objs>'
        assert shell_service._decode_clixml(payload) == "a <b> & c"


class TestStderrSanitising:
    def test_wrapper_echo_is_stripped(self):
        out = shell_service._sanitise_stderr(CLIXML_SAMPLE, WRAPPER_SCRIPT, "Write-Error 'boom'")
        assert "boom" in out
        assert "__wm_code" not in out
        assert "ErrorActionPreference" not in out

    def test_user_command_and_message_survive(self):
        script = "$ErrorActionPreference = 'Continue'\nnpm run build\nexit $global:__wm_code"
        stderr = "npm run build : missing script\n+ ~~~~~~~~~~~~~\nAt line:14 char:1"
        out = shell_service._sanitise_stderr(stderr, script, "npm run build")
        assert "missing script" in out
        assert "npm run build" in out
        assert "~~~" not in out
        assert "At line:14" not in out

    def test_empty_stays_empty(self):
        assert shell_service._sanitise_stderr("", WRAPPER_SCRIPT, "whoami") == ""

    def test_never_returns_nothing_when_input_had_content(self):
        """If every line looks like wrapper noise, keep the original text."""
        out = shell_service._sanitise_stderr(
            "exit $global:__wm_code", WRAPPER_SCRIPT, "Write-Error 'boom'"
        )
        assert out.strip()


class TestTimeoutClamp:
    def test_long_timeout_is_clamped(self, monkeypatch, captured_run):
        monkeypatch.delenv("WINDOWS_MCP_CLIENT_TIMEOUT", raising=False)
        result = shell_service.run("echo hi", timeout=600)
        assert captured_run["timeout"] == shell_service.CLIENT_TIMEOUT_CEILING
        assert any("clamped" in note for note in result.notes)
        assert any("Job mode=start" in note for note in result.notes)

    def test_short_timeout_is_untouched(self, monkeypatch, captured_run):
        monkeypatch.delenv("WINDOWS_MCP_CLIENT_TIMEOUT", raising=False)
        result = shell_service.run("echo hi", timeout=10)
        assert captured_run["timeout"] == 10
        assert result.notes == []

    def test_ceiling_can_be_disabled(self, monkeypatch, captured_run):
        monkeypatch.setenv("WINDOWS_MCP_CLIENT_TIMEOUT", "0")
        result = shell_service.run("echo hi", timeout=600)
        assert captured_run["timeout"] == 600
        assert result.notes == []

    def test_ceiling_is_configurable(self, monkeypatch, captured_run):
        monkeypatch.setenv("WINDOWS_MCP_CLIENT_TIMEOUT", "20")
        shell_service.run("echo hi", timeout=600)
        assert captured_run["timeout"] == 20

    def test_garbage_env_falls_back_to_default(self, monkeypatch, captured_run):
        monkeypatch.setenv("WINDOWS_MCP_CLIENT_TIMEOUT", "not-a-number")
        shell_service.run("echo hi", timeout=600)
        assert captured_run["timeout"] == shell_service.CLIENT_TIMEOUT_CEILING


class TestWindowsPowerShellOutputFormat:
    def test_text_output_requested_for_powershell_51(self):
        argv, temp = shell_service._argv("C:\\Windows\\System32\\powershell.exe", "whoami")
        assert temp is None
        assert "-OutputFormat" in argv
        assert argv[argv.index("-OutputFormat") + 1] == "Text"

    def test_pwsh_is_left_alone(self):
        argv, _ = shell_service._argv("C:\\Program Files\\PowerShell\\7\\pwsh.exe", "whoami")
        assert "-OutputFormat" not in argv
