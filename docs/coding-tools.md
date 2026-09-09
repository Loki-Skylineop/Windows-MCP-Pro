# Coding tools

Windows-MCP was built to drive a Windows *desktop*. These four tools make the same
server usable as a **coding** backend: reading, searching, editing and building a
repository without a human at the keyboard.

| Tool | Replaces | Why it exists |
| --- | --- | --- |
| `PowerShell` | the old fire-and-forget shell | real exit codes, stderr, a working directory, reusable sessions, partial output on timeout |
| `Edit` | `FileSystem write` | surgical edits with validation, backups and atomic writes - never rewrite a file to change three lines |
| `Grep` | `FileSystem search` (names only) | search file *contents*, map a repository, outline one file |
| `Job` | nothing | anything slower than a request timeout: installs, builds, test suites |

---

## PowerShell

```jsonc
{ "command": "pytest -q", "cwd": "C:/src/app", "session": "build", "timeout": 600 }
```

* **Real exit code.** The wrapper captures `$LASTEXITCODE` / `$?` and reports it; a
  failing command is a *result*, not an exception, so `isError` stays reserved for
  the transport actually breaking.
* **Both streams.** stdout and stderr are returned together, in order.
* **`cwd`** runs the command elsewhere without `cd &&` gymnastics. A missing
  directory is reported and the command still runs from the default location.
* **`session`** keeps the working directory and environment variables alive between
  calls, so `cd build` / `$env:FLAG='1'` survive to the next command.
* **`timeout`** default 30 s and effectively capped at 55 s: MCP clients abort the
  call at ~60 s while the command keeps running server-side, so a longer timeout
  only guarantees the output is thrown away. A larger value is clamped with a note
  pointing at `Job` (`WINDOWS_MCP_CLIENT_TIMEOUT` changes the ceiling, `0` disables
  it). On timeout the child tree is killed, exit code `124` is reported, **and the
  output produced so far is kept** - a hung build still tells you where it hung.
* **Readable stderr.** Windows PowerShell 5.1 serialises a redirected error stream
  as CLIXML. Text output is requested up front, any remaining CLIXML payload is
  decoded, and the wrapper's own script echo is stripped from error records.
* Console encoding is forced to UTF-8, so Cyrillic, emoji and box drawing survive.

## Edit

`mode='view'` first (it prints size, line count, EOL, encoding and sha256), then
`mode='apply'` with a batch of edits.

```jsonc
{ "edits": [
  { "file": "src/app.ts", "mode": "replace", "old": "const port = 3000", "new": "const port = 8080" },
  { "file": "src/app.ts", "mode": "insert_after", "old": "import http", "new": "import https" },
  { "file": "README.md", "mode": "append", "new": "\n## Changelog\n" }
]}
```

Modes: `replace` (default), `regex`, `lines`, `delete_lines`, `insert_after`,
`insert_before`, `append`, `prepend`, `create`, `patch`.

Guarantees:

* **All-or-nothing.** Every edit in the batch is applied to an in-memory copy and
  validated first. One bad anchor and *nothing* is written, across all files.
* **Atomic writes.** Content goes to a temp file, is fsynced, then `os.replace`d -
  no half-written source files if the process dies mid-write.
* **Backups.** Each modified file is copied to
  `%LOCALAPPDATA%\windows-mcp\edit-backups\<timestamp>-<name>-<digest>.bak`.
* **Encoding fidelity.** CRLF vs LF and a UTF-8 BOM are detected and restored.
* **`expected_sha256`** is a compare-and-swap guard: if the file changed on disk
  since it was read, the edit is refused instead of clobbering someone's work.
* **`dry_run: true`** returns a unified diff and writes nothing.
* Failed matches report the nearest lines and whether the difference is only
  whitespace, so the retry can be exact.

### mode='patch' - unified diffs

```jsonc
{ "edits": [{ "file": "src/app.ts", "mode": "patch", "patch": "@@ -12,3 +12,4 @@\n context\n-old line\n+new line\n+added line\n" }] }
```

One edit can carry many hunks - the cheapest way to express a scattered change.

* Hunks are located by their **context**, searching outwards from the header
  position (up to `PATCH_FUZZ = 400` lines). Line numbers in `@@` headers are stale
  the moment anything above them moves, so they are treated as a hint, not a fact.
* Drift introduced by earlier hunks is carried into the search for later ones.
* `diff --git`, `index`, `---`, `+++` preamble and `\ No newline at end of file`
  are ignored, so output from `git diff` can be pasted in unchanged.
* A hunk that does not match aborts the whole batch and reports which lines it was
  probably aimed at. Re-applying an already-applied diff therefore fails loudly
  instead of corrupting the file.

## Grep

* `mode='grep'` (default) - regex or `literal=true` search under `root`, with
  `glob='*.py,*.ts'`, `context=N`, `ignore_case`, `multiline`, `max_results`.
  Output is `path:line: text`, ready to paste straight into an `Edit` anchor.
* `mode='map'` - size and line totals for a tree, per-extension breakdown and the
  largest files: the fastest way into an unfamiliar repository.
* `mode='outline'` - declarations of one file (functions, classes, types, Markdown
  headings) with line numbers, so a 5,000-line file can be navigated without being
  read in full. Capped at `max_results` declarations (default 100, hard cap 2000),
  with the number of hidden declarations reported, so a generated file cannot
  flood the caller's context window.

### Python outlines are parsed, not pattern-matched

`.py` and `.pyi` files go through the real Python parser (`ast`), so the outline
is exact:

```text
    43| class GitError(RuntimeError)  [43-45]
    58| def commit(message, *, paths=..., add_all=...) -> str  [58-121]
    92|   def _fallback_identity() -> tuple  [92-99]
```

* Nesting is real: a method is indented under its class, a closure under its
  function. The regex scanner could not tell a method from a module-level
  function.
* Signatures are collapsed onto one line even when the source wraps them over
  five, and the return annotation is kept.
* `[start-end]` is the line span, so `Edit mode=lines` or `mode=view` can jump
  straight to the body.
* Decorators are attached to what they decorate (`... @property`) instead of
  being listed as separate symbols.
* A `def` inside a docstring, a comment or a string is no longer reported as
  code, and module- or class-level constants are listed while function locals
  are not.
* A file that does not parse - mid-edit, or Python 2 - still gets an outline
  from the pattern scanner, prefixed with a note that says so and points at the
  offending line. Silence would be worse than an approximate answer.

Other languages keep the pattern scanner. tree-sitter would buy the same
precision for TypeScript, Go and Rust, but it is a compiled dependency with a
separate grammar wheel per language, and this server keeps its import graph
small enough that no third-party package can stop it from starting - the same
reason the search stack runs out of process.

It runs in-process (no PowerShell start-up cost, no `ripgrep` install needed) and
skips `.git`, `node_modules`, `__pycache__`, `.venv`, `dist`, `build`, `target`.

## Job

```jsonc
{ "mode": "start", "command": "npm run build", "cwd": "C:/src/app", "name": "build" }
{ "mode": "status" }                      // most recent job
{ "mode": "logs", "tail": 40 }            // or head=N, or pattern=REGEX
{ "mode": "stop", "job_id": "last" }      // kills the whole process tree
```

* Output is streamed line by line to a UTF-8 log, so it can be grepped **while the
  job is still running**.
* State lives on disk (`%LOCALAPPDATA%\windows-mcp\jobs\<id>\` holds `command.ps1`,
  `output.log`, `exit.code`, `meta.json`), so jobs survive a server restart.
* `job_id` accepts a full id, a unique prefix, or `last`.
* Jobs are started in a new process group with no console window - they are
  *backgrounded*, not detached, so killing a job kills its children too.
* A command that dies instantly without output is reported as an error rather than
  as a silently "finished" job.

## Git

```jsonc
{ "mode": "status", "path": "C:/src/app" }
{ "mode": "diff", "path": "C:/src/app", "max_lines": 120 }   // --stat + trimmed patch
{ "mode": "log", "path": "C:/src/app", "limit": 10, "pattern": "fix" }
{ "mode": "commit", "path": "C:/src/app", "message": "fix: null guard" }
{ "mode": "branch", "path": "C:/src/app", "name": "spike", "create": true }
{ "mode": "info", "path": "C:/src/app" }
```

`PowerShell` can already run git, so this tool only exists for the three things
that go wrong when it does:

* **Output budget.** A real `git diff` is thousands of lines and all of them land
  in the model's context. `mode='diff'` always leads with `--stat` and trims the
  patch to `max_lines` (default 200, hard cap 4000), saying how much it hid.
  `status` caps the file list, `log` caps at 200 commits.
* **The identity trap.** A fresh Windows box has no `user.email`, so the first
  commit an agent attempts dies with *Author identity unknown* - the work is
  done and nothing is saved. `mode='commit'` retries with the previous commit's
  author, falls back to `windows-mcp <windows-mcp@localhost>`, and says which
  identity it used.
* **One question, one call.** `mode='info'` answers "which repositories do I
  have and is GitHub connected?" in a single result: git version, configured
  identity, current repo with branch/remotes/dirty count, `gh auth status`, and
  other repositories found within two directory levels (repositories are never
  descended into, `node_modules` and friends are skipped).

`push`, `reset`, `rebase`, `cherry-pick` and anything else that rewrites history
or touches a remote are **deliberately not exposed**. They stay in `PowerShell`,
where the exact command is visible before it runs.
---

## Running the tests

```powershell
$env:PYTHONPATH = "<repo>\src;<deps>\Lib\site-packages"
uv run --no-project --with pytest --with pytest-asyncio --python 3.12 `
  python -m pytest tests -q -p no:cacheprovider
```

The coding tools have their own suites: `tests/test_coding_edit.py`,
`tests/test_coding_patch.py`, `tests/test_coding_grep.py`,
`tests/test_coding_jobs.py`, `tests/test_git_tool.py`.
`tests/test_stdio_handshake.py` guards the exact set
of registered tools - add a tool, update that set.
