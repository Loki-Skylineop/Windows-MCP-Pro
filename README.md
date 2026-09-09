<div align="center">
  <h1>🪟 Windows-MCP Pro</h1>

  <a href="https://github.com/Loki-Skylineop/Windows-MCP-Pro/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </a>
  <img src="https://img.shields.io/badge/python-3.14%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/platform-Windows%2010%E2%80%9311-blue" alt="Platform">
  <img src="https://img.shields.io/badge/tools-13-blue" alt="13 tools">
  <img src="https://img.shields.io/badge/tests-813%20passing-brightgreen" alt="813 tests passing">
  <img src="https://img.shields.io/badge/SearchPro-9%20modes-blueviolet" alt="SearchPro: 9 modes">
  <img src="https://img.shields.io/badge/GUI%20tools-0-lightgrey" alt="Zero GUI tools">

  <p><b>A coding-agent fork of <a href="https://github.com/CursorTouch/Windows-MCP">CursorTouch/Windows-MCP</a> v0.8.5</b></p>
  <p><i>Same Windows plumbing. A different job - and it does that job without burning your context window.</i></p>

</div>

## What this fork is

**Windows-MCP Pro** is a downstream fork of
[CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP) **v0.8.5**
(upstream base commit `08ddee7`), retargeted from *desktop/UI automation* to
*agentic software engineering on Windows*.

Upstream hands an LLM a mouse and a keyboard. This fork hands it a shell, a
surgical file editor, a code searcher, a background job runner and a web research
stack - and deletes the GUI tools that were quietly eating the context window on
every request.

### Why you want this one instead

Upstream is a fine project for the job it was built for: 2M+ installs of "let the
model click things". Point that same toolset at real engineering work and the
seams open immediately - a failing command that reports `Status Code: 0`, a
`timeout=75` the client aborts at 60 s anyway, an outline tool that floods the
context window and truncates the answer mid-sentence, and a `Scrape` tool whose
default path depends on an MCP feature most clients never implemented. That is
not incompetence; that is a UI-automation server being used as a coding backend.

This fork treats every one of those as a bug with a test attached, not as a quirk
to work around in the system prompt:

- **18 commits** on top of the upstream base, each one shipped with a green suite.
- **813 tests** - 654 test functions across 48 files, 7,038 lines of test code,
  hermetic: no network, no live desktop, no flaky GUI.
- **3,564 lines** of new service code (2,285 in the coding services, 1,279 in the
  out-of-process web stack) instead of prompt-level workarounds.
- **11 GUI tools deleted** - roughly 14,000 characters (~4k tokens) of schema
  that was re-sent on *every single request* and never called.
- Four coding tools added (`Edit`, `Grep`, `Job`, `Git`), `Scrape` replaced
  outright, and every tool tagged `[coding]` / `[web]` / `[windows]` / `[util]`
  so tool choice stops being guesswork.

None of this came from reading upstream's source and guessing. Every failure mode
below was hit in live use, reproduced, fixed, locked down by a test, and then
written into the tool description the model actually reads.

All upstream credit belongs to [CursorTouch](https://github.com/CursorTouch); this
repository keeps the MIT license and tracks upstream as a remote. The
disagreement here is about target workload, not about their engineering.

### What's different from upstream v0.8.5

| | Upstream v0.8.5 | Windows-MCP Pro |
| --- | --- | --- |
| Tools exposed | 23 | **13** |
| Target workload | click / type / screenshot | shell, files, code search, web |
| File editing | `FileSystem write` (whole file) | `Edit` with 10 modes, backups, `expected_sha256`, `dry_run`, unified `patch` |
| Code search | none | `Grep`: `grep` / `map` / `outline` (Python parsed, not pattern-matched), capped output |
| Version control | none | `Git`: `status` / `diff` / `log` / `commit` / `branch` / `info`, trimmed output |
| Long commands | die at the request timeout | `Job`: `start` / `status` / `logs` / `stop` / `list` / `clean` |
| Shell | unbounded timeout, raw CLIXML stderr | 55 s clamp + graceful stop, decoded stderr, persistent sessions |
| Failure reporting | `Write-Error` could return `Status Code: 0` | real exit codes, `isError: true`, sanitised stderr |
| Web | `Scrape` (one URL, via MCP sampling most clients don't implement) | `SearchPro`: 9 modes - metasearch, news, images, videos, books, article extraction, CSS scraping, headless crawl, diagnostics |
| Hostile networks | one transport, one provider, silent failure | provider ladders, second transport, honest `engine=` label |
| Schema cost per request | 23 schemas, GUI set included | ~4k tokens lighter |
| GUI automation | 11 tools | removed on purpose |
| Tests | 676 | **813** |

### Five upstream defects this fork fixes

- **`Grep mode=outline` blew up the context.** It returned every symbol in the
  repository and truncated the model's answer mid-sentence. Now capped (default
  100 symbols, hard max 2000) with an explicit "narrow the pattern" hint.
- **`PowerShell` accepted timeouts it could not honour.** `timeout=75` was
  accepted, then the MCP client aborted the call at ~60 s and the output was lost.
  Now clamped to 55 s (`WINDOWS_MCP_CLIENT_TIMEOUT`) with a note pointing at `Job`.
- **`Write-Error` reported success.** A failing command returned `Status Code: 0`
  and leaked raw `#< CLIXML` payloads plus the internal exit-code probe into the
  answer. stderr is now decoded to plain text and sanitised.
- **`Scrape` was a trap.** Its default path asked the client to summarise the page
  through MCP sampling; clients that don't implement sampling (Notion among them)
  got raw HTML noise. Replaced by `SearchPro`.
- **BOM payloads.** Anything written by PowerShell's `Set-Content -Encoding UTF8`
  carries a BOM that `json.load` rejects - the SearchPro worker reads `utf-8-sig`.

### Hardened after live use, not after reading the code

- **`select` used to burn the whole deadline.** Scrapling got the full budget and
  the call died at 45-55 s. It now gets one attempt on half the budget and falls
  back to urllib: same page, `engine=urllib`, about 20 s instead of a timeout.
- **Selector errors now teach.** `'h1, body=p'` used to fail with a bare parse
  error; the message now states that selector pairs are separated by `;`, because
  the comma belongs to CSS itself.
- **`books` survives a blocked catalogue.** A network that kills TLS to
  `openlibrary.org` (`UNEXPECTED_EOF_WHILE_READING` on urllib, `curl: (28)` on
  curl_cffi) used to mean no answer at all. `books` now walks Open Library ->
  Google Books -> plain web search, splits the budget half / quarter / rest, and
  names the rung that answered - a web page is never dressed up as a catalogue
  record.

### SearchPro in one paragraph

One tool, nine modes, cheapest first: `search` (ddgs metasearch over Brave,
Yandex, DuckDuckGo, Bing and Yahoo, ~1-4 s, no captcha), `news` / `images` /
`videos`, `books` (Open Library -> Google Books -> web fallback), `read`
(trafilatura, URL to markdown, up to five URLs in one call), `select` (scrapling
+ CSS, with a urllib fallback), `crawl` (headless Chromium, only when the page
needs JavaScript) and `env` (diagnostics). It runs in a separate interpreter, so
the crawler's dependency tree can never break the server, and a hung browser dies
with a child process. Output is budgeted, timeouts are clamped, fetches are
cached for ten minutes, the answering engine is named in the header, and
captcha/block pages are reported as such instead of being passed off as content.
Full documentation: [docs/search-pro.md](docs/search-pro.md).

## Updates

- `SearchPro mode=books` gained a provider ladder (Open Library -> Google Books ->
  plain web search), so a blocked catalogue no longer means no answer.
- `select` falls back to urllib when Scrapling is blocked or slow, and selector
  syntax errors now explain the `;` versus `,` rule.
- Every tool description is prefixed with `[coding]` / `[web]` / `[windows]` /
  `[util]` plus a keyword line, so a model picks the right tool on the first try.
- `SearchPro` replaces `Scrape`: metasearch, article extraction, CSS scraping and
  a headless crawler in one tool.
- The 11 GUI-automation tools were removed; `Wait` was kept as a standalone tool.
- Coding tools (`Edit`, `Grep`, `Job`, `Git`, persistent `PowerShell` sessions)
  added and hardened - see the fix list above.
- Suite at **813 tests**, still hermetic.

### Supported Operating Systems

- Windows 10
- Windows 11

Upstream also lists Windows 7 and 8.1; this fork requires Python 3.14, which
does not support them.

## 🎥 What a session looks like

There are no demo videos here, because there is nothing to film: the fork does
not click, type, or take screenshots. A typical loop is text in, text out.

```text
Grep  mode=outline  path=src/windows_mcp/websearch/search_service.py
Edit  mode=apply    edits=[{file: ..., mode: replace, old: ..., new: ...}]
Job   mode=start    command="uv run pytest -q"  name=tests
Job   mode=logs     job_id=...  tail=40  pattern="FAILED|Error"
```

Research works the same way, cheapest step first:

```text
SearchPro mode=search  query="crawl4ai wait_for selector"  backend=yandex
SearchPro mode=read    url=https://docs.crawl4ai.com/...   max_chars=8000
SearchPro mode=crawl   url=https://example.com/spa  wait_for="css:.results"
```

## ✨ Key Features

- **Built for coding agents**
  Thirteen tools: a hardened shell with persistent sessions, surgical file edits,
  code search, a background job runner, and web research. No pixel pushing.

- **Context treated as a budget**
  Tool schemas are re-sent on every single request, so the tool list is kept
  deliberately small. `Grep` caps its own output, `SearchPro` trims page text,
  and both say so instead of silently flooding the window.

- **Timeouts that match reality**
  MCP clients abort a call at ~60 s, so blocking tools clamp to 55 s and point
  at `Job` for anything longer. A slow build no longer throws away the answer.

- **Edits you can verify**
  `Edit` offers replace / regex / line-range / patch modes with optional
  `expected_sha256` guards, dry runs, and automatic backups.

- **Web research without an API key**
  `SearchPro` metasearches Brave, Yandex, DuckDuckGo, Bing and Yahoo through
  `ddgs`, extracts articles with trafilatura, scrapes CSS selectors with
  Scrapling, and drives a headless browser with Crawl4AI - all out of process,
  so those dependencies can never break the server.

- **Tested like a product, not a demo**
  813 tests, 654 test functions across 48 files, 7,038 lines of test code. The
  whole suite runs without a network connection or a live desktop, so a green run
  actually means something.

- **Survives a hostile network**
  Built and verified through a VPN that intermittently kills TLS to individual
  hosts. `books` answers anyway, `select` degrades to a second transport instead
  of timing out, and every reply names the engine that produced it.

- **Honest failures**
  Real exit codes, `isError: true` on real errors, decoded stderr, and error
  messages that state the fix rather than the symptom.

## 🛠️Installation

> **This fork is not on PyPI.** The distribution is named `windows-mcp-pro`, but
> nothing is published under that name yet, and `uvx windows-mcp` fetches
> *upstream* Windows-MCP instead of this repository. Install from git.
>
> The config and data folders keep their original names
> (`~/.windows-mcp/`, `%LOCALAPPDATA%\windows-mcp\`), so an existing install
> keeps its jobs, sessions and config after the rename.

### Prerequisites

- Python 3.14+ (`requires-python = ">=3.14"`; with uv: `uv python install 3.14`)
- [uv](https://docs.astral.sh/uv/) - `pip install uv`
- Windows 10 or 11
- Optional, for `SearchPro`: a second interpreter (3.11-3.13 works well) with the
  search stack installed. It is deliberately *not* a server dependency, so a
  browser-automation package can never break the MCP server:

```shell
pip install ddgs trafilatura "scrapling[fetchers]" crawl4ai
python -m playwright install chromium   # only needed for mode=crawl
```

Run `SearchPro mode=env` afterwards - it prints the interpreter it picked, the
versions it found, and a live probe.

### Install from git

```shell
uv tool install git+https://github.com/Loki-Skylineop/Windows-MCP-Pro
windows-mcp-pro serve
```

Or from a clone, which is what you want if you plan to change anything:

```shell
git clone https://github.com/Loki-Skylineop/Windows-MCP-Pro
cd Windows-MCP-Pro
uv sync
uv run python -m windows_mcp serve
```

### Run at Login

Install it as a background task that starts now and at every login:

```shell
windows-mcp-pro install

# Or choose the HTTP transport and bind address explicitly
windows-mcp-pro install --transport sse --host 127.0.0.1 --port 8000
```

This creates a per-user Scheduled Task named `windows-mcp-server` and a wrapper script at
`~/.windows-mcp/start-server.cmd`. Use `windows-mcp-pro uninstall` to remove it. Logs are written
to `~/.windows-mcp/server.log` and `~/.windows-mcp/server.error.log`.

<details>
  <summary>Install in Claude Desktop</summary>

  1. Install [Claude Desktop](https://claude.ai/download).

```shell
npm install -g @anthropic-ai/mcpb
```

  2. Configure the MCP server.

  **Option A: Install from PyPI (Recommended)**
  
  Use `uvx` to run the latest version directly from PyPI.

  Add this to your `claude_desktop_config.json`:
  ```json
  {
    "mcpServers": {
      "windows-mcp-pro": {
        "command": "uvx",
        "args": [
          "windows-mcp-pro",
          "serve"
        ]
      }
    }
  }
  ```

  **Option B: Install from Source**

  1. Clone the repository:
  ```shell
  git clone https://github.com/CursorTouch/Windows-MCP.git
  cd Windows-MCP
  ```

  2. Add this to your `claude_desktop_config.json`:
  ```json
  {
    "mcpServers": {
      "windows-mcp-pro": {
        "command": "uv",
        "args": [
          "--directory",
          "<path to the windows-mcp-pro directory>",
          "run",
          "windows-mcp-pro",
          "serve"
        ]
      }
    }
  }
  ```
  3. Fully restart Claude Desktop and verify the server appears in the MCP tools list.

  **Claude Desktop MSIX (Windows Store)**

  The MSIX-packaged Claude Desktop (Microsoft Store version) virtualizes `%APPDATA%`. This causes two main issues:
  1. The config file is located at: `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` (not `%APPDATA%\Claude\`).
  2. Automatic installation from the "Claude Directory" will fail because the `${__dirname}` variable resolves to the incorrect (non-virtualized) path.

  **To configure Windows-MCP on the Windows Store version of Claude:**
  
  You must manually edit the configuration file. Note that Electron apps in the MSIX sandbox do not inherit the system `PATH`, so you must use the **full absolute path** to `uvx.exe` (or `uv.exe`).

  **Option A: Using pre-installed executable**

  1. In a terminal, run `uv tool install windows-mcp-pro`.
  2. Use the generated executable in your config:
  ```json
  {
    "mcpServers": {
      "windows-mcp-pro": {
        "command": "C:\\Users\\<user>\\.local\\bin\\windows-mcp-pro.exe",
        "args": ["serve"]
      }
    }
  }
  ```

  **Option B: Using uvx**
  ```json
  {
    "mcpServers": {
      "windows-mcp-pro": {
        "command": "C:\\Users\\<user>\\.local\\bin\\uvx.exe",
        "args": ["windows-mcp-pro", "serve"]
      }
    }
  }
  ```

  **Option C: Install from Source**
  ```json
  {
    "mcpServers": {
      "windows-mcp-pro": {
        "command": "C:\\Users\\<user>\\.local\\bin\\uv.exe",
        "args": [
          "--directory",
          "C:\\path\\to\\Windows-MCP",
          "run",
          "windows-mcp-pro",
          "serve"
        ]
      }
    }
  }
  ```

  Replace `<user>` with your Windows username. To find the correct paths, run `where uvx`, `where windows-mcp-pro`, or `where uv`. Fully quit Claude Desktop (Tray → Quit) and reopen after saving the config.

  For additional Claude Desktop integration troubleshooting, see the [MCP documentation](https://modelcontextprotocol.io/quickstart/server#claude-for-desktop-integration-issues).
</details>

<details>
  <summary>Install in Perplexity Desktop</summary>

  1. Install [Perplexity Desktop](https://apps.microsoft.com/detail/xp8jnqfbqh6pvf).
  2. Open Perplexity Desktop and go to `Settings -> Connectors -> Add Connector -> Advanced`.
  3. Enter the name as `Windows-MCP`, then paste one of the following configs.


  **Option A: Install from PyPI (Recommended)**

  ```json
  {
    "command": "uvx",
    "args": [
      "windows-mcp-pro",
      "serve"
    ]
  }
  ```

  **Option B: Install from Source**

  ```json
  {
    "command": "uv",
    "args": [
      "--directory",
      "<path to the windows-mcp-pro directory>",
      "run",
      "windows-mcp-pro",
      "serve"
    ]
  }
  ```

  4. Click `Save`, then restart Perplexity Desktop if needed.

For additional Claude Desktop integration troubleshooting, see the [Perplexity MCP Support](https://www.perplexity.ai/help-center/en/articles/11502712-local-and-remote-mcps-for-perplexity). The documentation includes helpful tips for checking logs and resolving common issues.
</details>

<details>
  <summary> Install in Gemini CLI</summary>

  1. Install Gemini CLI.

```shell
npm install -g @google/gemini-cli
```

  2. Open `%USERPROFILE%/.gemini/settings.json`.
  3. Add the `windows-mcp-pro` config and save it.

```json
{
  "theme": "Default",
  ...
  "mcpServers": {
    "windows-mcp-pro": {
      "command": "uvx",
      "args": [
        "windows-mcp-pro",
        "serve"
      ]
    }
  }
}
```
*Note: To run from source, replace the command with `uv` and args with `["--directory", "<path>", "run", "windows-mcp-pro", "serve"]`.*

  4. Restart Gemini CLI.
</details>

<details>
  <summary>Install in Qwen Code</summary>
  1. Install Qwen Code.

```shell
npm install -g @qwen-code/qwen-code@latest
```
  2. Open `%USERPROFILE%/.qwen/settings.json`.
  3. Add the `windows-mcp-pro` config and save it.

```json
{
  "mcpServers": {
    "windows-mcp-pro": {
      "command": "uvx",
      "args": [
        "windows-mcp-pro",
        "serve"
      ]
    }
  }
}
```
*Note: To run from source, replace the command with `uv` and args with `["--directory", "<path>", "run", "windows-mcp-pro", "serve"]`.*

  4. Restart Qwen Code.
</details>

<details>
  <summary>Install in Codex CLI</summary>
  1. Install Codex CLI.

```shell
npm install -g @openai/codex
```
  2. Open `%USERPROFILE%/.codex/config.toml`.
  3. Add the `windows-mcp-pro` config and save it.

```toml
[mcp_servers.windows-mcp-pro]
command="uvx"
args=[
  "windows-mcp-pro",
  "serve"
]
```
*Note: To run from source, replace the command with `uv` and args with `["--directory", "<path>", "run", "windows-mcp-pro", "serve"]`.*

  4. Restart Codex CLI.
</details>

<details>
  <summary>Install in Autohand Code</summary>

  Add the published stdio server from a Windows terminal:

  ```shell
  autohand mcp add windows-mcp-pro uvx windows-mcp-pro serve
  ```

  Add `--scope project` after `add` to keep the server configuration in the current project. See [Autohand Code](https://github.com/autohandai/code-cli/) for current installation and CLI details.
</details>

<details>
  <summary>Install in Claude Code</summary>

  1. Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview):

```shell
npm install -g @anthropic-ai/claude-code
```

  2. Configure the server:

  **Option A: Install from PyPI (Recommended)**

  Use `uvx` to run the latest version directly from PyPI.

  ```shell
  claude mcp add --transport stdio windows-mcp-pro -- uvx windows-mcp-pro serve
  ```

  **Option B: Install from Source**

  1. Clone the repository:
  ```shell
  git clone https://github.com/CursorTouch/Windows-MCP.git
  cd Windows-MCP
  ```

  2. Run the following command in your terminal:
  ```shell
  claude mcp add --transport stdio windows-mcp-pro -- uv --directory "<path>" run windows-mcp-pro serve
  ```

  *Note: To make the server available across all projects, add `--scope user` to the command.*

  3. Rerun Claude Code in terminal. Enjoy 🥳

  **Note:** On Windows, if you encounter "Connection closed" errors, use the full path to `uvx.exe`:

  ```shell
  claude mcp add --transport stdio windows-mcp-pro -- C:\Users\<user>\.local\bin\uvx.exe windows-mcp-pro serve
  ```

  To verify the server is registered, run `claude mcp list`. Inside Claude Code, use `/mcp` to check server status.

  **WSL (Windows Subsystem for Linux)**

  If you run Claude Code from WSL, the MCP server must still execute on the Windows side (it needs Windows APIs for UI automation). Use `powershell.exe` as the command to bridge WSL and Windows:

  1. Install `uv` on **Windows** (from a PowerShell terminal):
  ```powershell
  irm https://astral.sh/uv/install.ps1 | iex
  ```

  2. From your **WSL terminal**, register the server:
  ```shell
  claude mcp add windows-mcp-pro --transport stdio -s user -- powershell.exe -Command "C:\Users\<user>\.local\bin\uvx.exe windows-mcp-pro serve"
  ```

  Replace `<user>` with your Windows username. The `-s user` flag makes the server available across all projects.

  3. Restart Claude Code and verify with `/mcp`.
</details>

---

## 🖥️ Running the server

The server runs on your Windows machine and exposes its 13 tools to the
connected MCP client.

```shell
# stdio transport (default)
windows-mcp-pro serve

# Or SSE / Streamable HTTP for network access
windows-mcp-pro serve --transport sse --host localhost --port 8000
windows-mcp-pro serve --transport streamable-http --host localhost --port 8000
```

From a clone without installing the entry point, use
`uv run python -m windows_mcp serve` with the same flags.

Optional environment variables can be set to customize behavior — see [Environment Variables](#-environment-variables) below.

### Security for Remote Access

For network access, enable authentication and TLS:

```shell
windows-mcp-pro serve --transport sse --host 0.0.0.0 \
  --auth-key "your_secret_token" \
  --ip-allowlist "203.0.113.0/24" \
  --ssl-certfile cert.pem --ssl-keyfile key.pem
```

See [🔐 Security & Access Control](#-security--access-control) for all options.

### Transport Options

| Transport | Command | Use Case |
|---|---|---|
| `stdio` (default) | `serve --transport stdio` | Direct connection from MCP clients like Claude Desktop, Cursor, etc. |
| `sse` | `serve --transport sse --host HOST --port PORT` | Network-accessible via Server-Sent Events |
| `streamable-http` | `serve --transport streamable-http --host HOST --port PORT` | Network-accessible via HTTP streaming (recommended for production) |

---

## 🔐 Security & Access Control

### Authentication
```shell
windows-mcp-pro serve --transport sse --host 0.0.0.0 --auth-key "your_token"
```
Requires `Authorization: Bearer your_token` header on all requests.

### IP Allowlist
```shell
windows-mcp-pro serve --auth-key "token" --ip-allowlist "203.0.113.0/24,198.51.100.5"
```
Restricts connections to specified CIDR ranges. Blocks private/loopback IPs by default.

### CORS Origins

By default, **no CORS headers are emitted**. Browsers block cross-origin requests via their own Same-Origin Policy, which means arbitrary websites cannot reach the MCP control plane even if the server is on `localhost`. Host-header validation (DNS rebinding protection) is also applied automatically based on the bind address.

If you need a browser-based MCP client to reach the server, opt in with an explicit origin allowlist:

```shell
windows-mcp-pro serve --cors-origins "https://my-client.example.com,https://other.example.com"
```

Only the listed origins receive `Access-Control-Allow-Origin` headers; all other cross-origin requests are rejected by the browser. The equivalent environment variable is `WINDOWS_MCP_CORS_ORIGINS`.

### Tool Selection
All tools are enabled by default. Use `--tools` to whitelist specific tools, or `--exclude-tools` to block specific ones.

```shell
windows-mcp-pro serve --tools "PowerShell,Edit,Grep,Job"   # Enable only these tools
windows-mcp-pro serve --exclude-tools "PowerShell,Registry" # Disable specific tools
```

### TLS/HTTPS
```shell
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes

windows-mcp-pro serve --ssl-certfile cert.pem --ssl-keyfile key.pem
```

### OAuth 2.0 + PKCE

For MCP clients that use OAuth (e.g. Claude Desktop) instead of a static API key:

```shell
windows-mcp-pro serve --transport streamable-http --host 0.0.0.0 \
  --ssl-certfile ~/.windows-mcp/cert.pem \
  --ssl-keyfile  ~/.windows-mcp/key.pem \
  --oauth-client-id my-client \
  --oauth-client-secret my-secret
```

**Claude Desktop config:**
```json
{
  "mcpServers": {
    "windows-mcp-pro": {
      "type": "http",
      "url": "https://<host>:8000/mcp/",
      "oauth": {
        "clientId": "my-client",
        "clientSecret": "my-secret"
      }
    }
  }
}
```

The OAuth server exposes:
- `GET /.well-known/oauth-authorization-server` — server metadata (RFC 8414)
- `GET /oauth/authorize` — Authorization Code + PKCE (`S256` required)
- `POST /oauth/token` — token exchange (client secret required)
- `POST /oauth/register` — disabled; clients must be pre-provisioned

Dynamic client registration is disabled. Redirect URIs must be loopback `http(s)` only.
Auth key and OAuth can coexist — both are accepted as valid Bearer tokens.

### Config File (`~/.windows-mcp/config.toml`)

Instead of passing flags every time, store your configuration in `~/.windows-mcp/config.toml`. CLI flags always override config file values.

**Search order:**
1. `--config /path/to/config.toml`
2. `~/.windows-mcp/config.toml`

**stdio** — local only, no security needed:
```toml
[server]
transport = "stdio"
```

**SSE** — network access with auth and IP restriction:
```toml
[server]
transport = "sse"
host      = "0.0.0.0"
port      = 8000
auth_key  = "your-secret-key"

[security]
ip_allowlist = ["192.168.1.0/24"]
```

**Streamable HTTP** — with auth, TLS, and tool exclusions:
```toml
[server]
transport    = "streamable-http"
host         = "0.0.0.0"
port         = 8000
auth_key     = "your-secret-key"
ssl_certfile = "cert.pem"   # resolved relative to ~/.windows-mcp/
ssl_keyfile  = "key.pem"

[security]
ip_allowlist        = ["192.168.1.0/24"]
cors_origins        = ["https://my-client.example.com"]   # optional — browser CORS opt-in
oauth_client_id     = "my-client"      # optional — enables OAuth 2.0 + PKCE
oauth_client_secret = "my-secret"

[tools]
exclude = ["PowerShell", "Registry"]   # disable specific tools
```

Place cert and key files in the same directory:

```
~/.windows-mcp/
├── config.toml
├── cert.pem
└── key.pem
```

Generate a self-signed cert directly into that directory:

```shell
mkdir -p ~/.windows-mcp-pro
openssl req -x509 -newkey rsa:4096 \
  -keyout ~/.windows-mcp/key.pem \
  -out ~/.windows-mcp/cert.pem \
  -days 365 -nodes
```

### `auth` Helper

Generate an auth key and save a working config to `~/.windows-mcp/config.toml`:

```shell
windows-mcp-pro auth
```

Generate auth plus a self-signed TLS certificate:

```shell
windows-mcp-pro auth --transport streamable-http --host 0.0.0.0 --port 8000 --with-tls
```

This command writes the auth key into the config file, can generate `cert.pem` and `key.pem`, and prints an example MCP client configuration for the selected transport.

### SSRF Protection

`SearchPro` validates every fetch target before the worker process is spawned:
non-HTTP(S) schemes, URLs with embedded credentials, and hosts resolving to
private, loopback, link-local, multicast or reserved addresses are refused. Set
`WINDOWS_MCP_SEARCH_ALLOW_PRIVATE=1` when you deliberately want to scrape a
local dev server.

---

## ⚙️ Environment Variables

All variables are optional unless noted. Set them via the `env` key in `claude_desktop_config.json` (or your MCP client's equivalent config).

### Web search (SearchPro)

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_SEARCH_PYTHON` | _(auto-discovered)_ | Full path to the interpreter that holds the search stack (`ddgs`, `trafilatura`, `scrapling`, `crawl4ai`). Set it when discovery picks the wrong Python - `SearchPro mode=env` prints every candidate it tried and why it was rejected. |
| `WINDOWS_MCP_SEARCH_ALLOW_PRIVATE` | _(disabled)_ | Set to `1`, `true`, `yes`, or `on` to let `read`/`select`/`crawl` reach private, loopback and link-local addresses. Off by default so a prompt-injected agent cannot read `http://127.0.0.1` or a cloud metadata endpoint through the server. |
| `WINDOWS_MCP_CLIENT_TIMEOUT` | `55` | Shared ceiling in seconds for `PowerShell` and `SearchPro` timeouts, matching the ~60 s at which MCP clients abort a call. `0` disables the clamp. |
| `WINDOWS_MCP_SEARCH_CACHE_TTL` | `600` | Seconds a fetched page stays in the in-memory cache used by `read`, `select` and `crawl` (32 entries, keyed by mode, URL and options). Re-reading the same page inside one task is then free. `0` disables caching. |

### Security

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_AUTH_KEY` | _(none)_ | Bearer token required on all HTTP requests. Alternative to `--auth-key` CLI flag. |
| `WINDOWS_MCP_IP_ALLOWLIST` | _(none)_ | Comma-separated list of allowed client IPs or CIDR ranges (e.g., `203.0.113.0/24,198.51.100.5`). Alternative to `--ip-allowlist` CLI flag. |
| `WINDOWS_MCP_CORS_ORIGINS` | _(none)_ | Comma-separated list of origins permitted to make cross-origin browser requests (e.g., `https://my-client.example.com`). No CORS headers are emitted when unset. Alternative to `--cors-origins` CLI flag. |
| `WINDOWS_MCP_TOOLS` | _(all enabled)_ | Comma-separated explicit list of tools to enable (e.g., `PowerShell,Edit,Grep,Job`). Alternative to `--tools` CLI flag. |
| `WINDOWS_MCP_EXCLUDE_TOOLS` | _(none)_ | Comma-separated list of tools to disable (e.g., `PowerShell,Registry`). Alternative to `--exclude-tools` CLI flag. |
| `WINDOWS_MCP_SSL_CERTFILE` | _(none)_ | Path to TLS certificate file (.pem) for HTTPS. Must be provided with `WINDOWS_MCP_SSL_KEYFILE`. |
| `WINDOWS_MCP_SSL_KEYFILE` | _(none)_ | Path to TLS private key file (.pem) for HTTPS. Must be provided with `WINDOWS_MCP_SSL_CERTFILE`. |
| `WINDOWS_MCP_OAUTH_CLIENT_ID` | _(none)_ | OAuth client ID for HTTP transports. Must be provided with `WINDOWS_MCP_OAUTH_CLIENT_SECRET`. |
| `WINDOWS_MCP_OAUTH_CLIENT_SECRET` | _(none)_ | OAuth client secret for HTTP transports. Must be provided with `WINDOWS_MCP_OAUTH_CLIENT_ID`. |
| `WINDOWS_MCP_STATELESS_HTTP` | `false` | Set to `1`, `true`, `yes`, or `on` to run `streamable-http` without `Mcp-Session-Id` connection state. Useful for reconnects after restarts and for horizontally scaled deployments. |

### Telemetry

| Variable | Default | Description |
|---|---|---|
| `ANONYMIZED_TELEMETRY` | `true` | Set to `false` to disable anonymous usage telemetry. No personal data, tool arguments, or outputs are ever collected regardless of this setting. |
| `POSTHOG_API_KEY` | Project default | Override the PostHog project write key used for anonymous telemetry. Set to an empty string to skip PostHog client initialization. |
| `POSTHOG_HOST` | `https://us.i.posthog.com` | Override the PostHog host for anonymous telemetry, such as for a self-hosted PostHog deployment. |

### Debug

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_DEBUG` | `false` | Set to `1`, `true`, `yes`, or `on` to enable debug mode, which sets the log level to DEBUG for verbose output. Also available as the `--debug` CLI flag. |

### WatchDog

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_WATCHDOG` | `false` | Set to `on`, `1`, `true`, `yes`, or `enabled` (case-insensitive) to run the UIA focus watchdog. It is off by default because its only remaining effect is debug logging of focus changes, while it costs a background thread and a long-lived UIA event subscription that can crash the server on unstable UIA environments after long uptime (e.g. across a sleep/resume or session change). The accessibility tree is built on demand for every tool call either way, so tool behaviour is unaffected. |

**Example `claude_desktop_config.json`:**

Local (no security):
```json
{
  "mcpServers": {
    "windows-mcp-pro": {
      "command": "windows-mcp-pro",
      "args": ["serve"],
      "env": {
        "WINDOWS_MCP_SEARCH_PYTHON": "C:/Users/you/AppData/Local/Programs/Python/Python312/python.exe"
      }
    }
  }
}
```

Remote (with auth + IP allowlist + TLS):
```json
{
  "mcpServers": {
    "windows-mcp-pro": {
      "command": "windows-mcp-pro",
      "args": ["serve", "--transport", "sse", "--host", "0.0.0.0"],
      "env": {
        "WINDOWS_MCP_AUTH_KEY": "your_token",
        "WINDOWS_MCP_IP_ALLOWLIST": "203.0.113.0/24",
        "WINDOWS_MCP_SSL_CERTFILE": "/path/to/cert.pem",
        "WINDOWS_MCP_SSL_KEYFILE": "/path/to/key.pem"
      }
    }
  }
}
```

---

## 🔨MCP Tools

Windows-MCP Pro exposes **13 tools**. Every tool schema is re-sent to the model on
*every* request, so the tool list is a context budget, not a feature list - the
upstream GUI set was removed for exactly that reason (see
[Why 13 tools](#why-13-tools)).

### Coding

- `PowerShell`: Execute PowerShell with a graceful timeout (clamped to 55 s so the
  MCP client can never time out first), CLIXML-decoded stderr, real exit codes,
  optional persistent `session` and `cwd`.
- `Edit`: Surgical file editing - `replace`, `regex`, `lines`, `delete_lines`,
  `insert_after`, `insert_before`, `append`, `prepend`, `create`, `patch`; plus
  `dry_run`, automatic backups, `expected_sha256` optimistic locking, and a `view`
  mode with line numbers. Preserves each file's encoding and line endings.
- `Grep`: Code search in three shapes - `grep` (ripgrep-style with context),
  `map` (repository tree with sizes) and `outline` (symbols per file). Python
  files are outlined with the real Python parser: exact nesting (a method is
  shown indented under its class), signatures collapsed onto one line even when
  the source wraps them over five, line spans, decorators attached to what they
  decorate, and no `def` from a docstring reported as code. Other languages use
  pattern matching; a Python file that does not parse still gets an outline,
  with a note saying the result is pattern-matched. Results are capped so a wide
  pattern cannot flood the context window.
- `Job`: Run long commands in the background and poll them - `start`, `status`,
  `logs` (with `tail`/`head`/`pattern`), `stop`, `list`, `clean`. This is how test
  suites, builds and installs run without hitting the request timeout.
- `FileSystem`: Read, write, copy, move, delete, list, search and inspect files
  and directories.
- `Git`: Repository work without shelling out - `status` (changed files grouped
  by stage), `diff` (a `--stat` summary plus a patch trimmed to `max_lines`),
  `log`, `commit` (stages, then falls back to the previous commit's author when
  the machine has no git identity, so work is never lost to "Author identity
  unknown"), `branch` (list, switch, create) and `info` (git version, remotes,
  whether the GitHub CLI is logged in, and other repositories found nearby).
  `push`, `reset` and `rebase` are deliberately absent - run those through
  `PowerShell`, where the exact command is visible first.

### Web

- `SearchPro`: Web search and page extraction in one tool - `search`, `news`,
  `images`, `videos` (ddgs metasearch over brave/yandex/duckduckgo/bing/yahoo),
  `books` (Open Library, falling back to Google Books and then plain web
  search), `read` (URL to clean markdown, up to five
  URLs in one call), `select` (CSS selectors to records), `crawl` (headless
  Chromium with `wait_for`/`js`/`scroll`/`focus`) and `env` (diagnostics).
  Fetched pages are cached for ten minutes, so re-reading the same URL inside
  one task costs nothing. Runs out of process on the interpreter that owns the
  scraping stack. See [docs/search-pro.md](docs/search-pro.md).

### Windows

- `App`: Launch an application by Start Menu name or strictly by executable path
  with separated argv and optional cwd; resize, move, and switch between windows.
- `Process`: List running processes or terminate them by PID or name.
- `Registry`: Read, write, delete, or list Windows Registry values and keys.
- `Clipboard`: Read or set Windows clipboard content.
- `Notification`: Send a Windows toast notification with a title and message.
- `Wait`: Pause for a defined duration (clamped to 45 s) - useful when something
  outside the agent's control needs a moment to settle.

### Why 13 tools

These eleven upstream tools are **not** in this fork: `Snapshot`, `Screenshot`,
`Click`, `Type`, `Scroll`, `Move`, `Shortcut`, `MultiSelect`, `MultiEdit`,
`DisplayInventory`, `WaitFor`. Together their schemas cost roughly 14,000
characters (~4k tokens) of context on every request, and a coding agent never
calls them: it edits files and runs commands instead of clicking pixels. If you
want desktop/UI automation, use
[upstream Windows-MCP](https://github.com/CursorTouch/Windows-MCP) - this fork is
deliberately the other half of the problem.

`--tools` / `--exclude-tools` still work, so you can narrow the 13 further per
client.


## 🔗 Upstream project

This fork is not affiliated with CursorTouch - it just owes them most of the
code. Upstream's own channels:

- [CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP) - the original repository
- [X](https://x.com/CursorTouch) and [Discord](https://discord.com/invite/Aue9Yj2VzS) - upstream community
- [Upstream contributors](https://github.com/CursorTouch/Windows-MCP/graphs/contributors) - everyone whose work this fork inherits

Pulling upstream changes into a clone of this fork:

```shell
git remote add upstream https://github.com/CursorTouch/Windows-MCP
git fetch upstream
git merge upstream/main    # expect conflicts in tools/__init__.py and README.md
```

## 🔒 Security

**Important**: this server operates with full system access - `PowerShell`, `Edit`, `FileSystem` and `Registry` can perform irreversible operations, and `SearchPro` fetches remote content. Review the security guidelines before exposing it beyond localhost.

For detailed security information, including:
- Tool-specific risk assessments
- Deployment recommendations
- Vulnerability reporting procedures
- Compliance and auditing guidelines

Please read our [Security Policy](SECURITY.md).

## 📊 Telemetry

This fork inherits upstream's anonymous telemetry. No personal information, no tool arguments, no outputs are tracked.

To disable telemetry, set `ANONYMIZED_TELEMETRY` to `false` in your MCP client configuration:

```json
{
  "mcpServers": {
    "windows-mcp-pro": {
      "command": "windows-mcp-pro",
      "args": [
        "serve"
      ],
      "env": {
        "ANONYMIZED_TELEMETRY": "false"
      }
    }
  }
}
```

See the [Environment Variables](#-environment-variables) section for the full list of configurable options.

For detailed information on what data is collected and how it is handled, please refer to the [Telemetry and Data Privacy](SECURITY.md#telemetry-and-data-privacy) section in our Security Policy.

## 📝 Limitations

- **No GUI automation.** No clicking, typing, screenshots, or UI-tree
  inspection. If you need those, run
  [upstream Windows-MCP](https://github.com/CursorTouch/Windows-MCP) instead -
  or alongside this one, registered under a different client name.
- **`SearchPro` needs a second interpreter.** The search stack is not a server
  dependency; `SearchPro mode=env` reports which interpreter was chosen and
  what is missing.
- **Long work must go through `Job`.** MCP clients abort a call at ~60 s, so
  `PowerShell` and `SearchPro` clamp to 55 s. Builds, test suites and installs
  belong in `Job mode=start`.
- **Windows only**, and PowerShell 5.1 CLIXML stderr is unescaped on a
  best-effort basis rather than fully parsed.
- **Not on PyPI.** Install from git; `uvx windows-mcp-pro` fetches upstream.

## 🪪 License

MIT, unchanged from upstream - see [LICENSE](LICENSE). Upstream copyright stays
with CursorTouch; the changes in this fork ship under the same license.

## 🙏 Acknowledgements

This fork exists because of
[CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP) - the
server, transports, security layer and Windows plumbing are theirs.

It also stands on:

- [FastMCP](https://github.com/jlowin/fastmcp) - the MCP server framework
- [UIAutomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) - Windows accessibility access, still used by `App`
- [ddgs](https://github.com/deedy5/ddgs) - metasearch across Brave, Yandex, DuckDuckGo, Bing and Yahoo
- [trafilatura](https://github.com/adbar/trafilatura) - article extraction
- [Scrapling](https://github.com/D4Vinci/Scrapling) - CSS/XPath scraping
- [Crawl4AI](https://github.com/unclecode/crawl4ai) - headless-browser crawling

## 🤝Contributing

Issues and pull requests for the fork go to
[Windows-MCP-Pro/issues](https://github.com/Loki-Skylineop/Windows-MCP-Pro/issues).
Anything that also fixes upstream behaviour is better sent upstream first.

```shell
git clone https://github.com/Loki-Skylineop/Windows-MCP-Pro
cd Windows-MCP-Pro
uv sync
uv run pytest -q      # the full suite; no network, no live desktop
```

Two house rules: tests stay hermetic, and every new tool has to justify its
schema against the context budget.

Forked from [CursorTouch](https://github.com/CursorTouch)'s Windows-MCP.

## Citation

Most of this codebase is upstream's work - cite that:

```bibtex
@software{
  author       = {CursorTouch},
  title        = {Windows-MCP: Lightweight open-source project for integrating LLM agents with Windows},
  year         = {2024},
  publisher    = {GitHub},
  url={https://github.com/CursorTouch/Windows-MCP}
}
```

For this fork specifically, link <https://github.com/Loki-Skylineop/Windows-MCP-Pro>.
