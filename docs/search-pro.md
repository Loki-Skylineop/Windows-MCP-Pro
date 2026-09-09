# SearchPro

One tool for everything web: metasearch, article extraction, CSS scraping and a
headless browser. It replaces the old `Scrape` tool, which could only fetch a
single URL and then asked the *client* to summarise it through MCP sampling - a
feature many clients (Notion among them) do not implement, so the default path
quietly returned raw HTML noise.

## Why it runs in a second interpreter

The scraping stack is **not** a dependency of the server:

| Process | Interpreter | Owns |
| --- | --- | --- |
| MCP server | Python 3.14 (`uv`) | fastmcp, pywin32, pillow ... |
| SearchPro worker | Python 3.12 | `ddgs`, `trafilatura`, `scrapling`, `crawl4ai`, Playwright/Chromium |

Consequences, all of them good:

* the two dependency graphs can never break each other (crawl4ai pins old
  `httpx`/`pydantic` ranges that would fight fastmcp);
* a headless Chromium that hangs or crashes kills a short-lived child process,
  not the server;
* chatty libraries cannot corrupt the reply - the worker answers on a single
  stdout line prefixed with `__WMP_JSON__`, everything else is ignored.

### Interpreter discovery

1. `WINDOWS_MCP_SEARCH_PYTHON` (explicit override, wins over everything);
2. `%LOCALAPPDATA%\Programs\Python\Python312|313|311\python.exe`, `%PROGRAMFILES%\Python312|313`;
3. `python3.12`, `python3.13`, `python3`, `python` from `PATH`;
4. the server's own interpreter, last.

Every candidate is **probed** for an importable `ddgs` before being accepted,
because on this machine the bare `python` on `PATH` resolves to a uv cache
interpreter that reports a plausible version and has no packages at all. The
result is cached for the life of the process; `mode=env` prints the whole
discovery log.

Install the stack into whatever interpreter you want to use:

```powershell
& 'C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe' -m pip install `
  ddgs trafilatura "scrapling[fetchers]" crawl4ai
```

## Escalation ladder

Cheapest first - this is the whole point of the tool:

```
don't know the URL          -> mode=search          ddgs metasearch,  ~1-4 s, no captcha
know the URL, want text     -> mode=read            trafilatura,      ~1 s
want specific fields        -> mode=select          scrapling + CSS,  ~1 s
read came back empty/JS     -> mode=crawl           Chromium,         ~2-6 s
something is broken         -> mode=env             versions + probe
```

`crawl` is last on purpose: it is 4-6x slower and gets challenged by anti-bot
pages that plain HTTP clients walk straight through.

## Modes

| Mode | Needs | Returns |
| --- | --- | --- |
| `search` | `query` | numbered title / URL / snippet list |
| `news` | `query` | same, with `date` and `source` |
| `images`, `videos` | `query` | media URLs with dimensions / duration |
| `read` | `url` | page as markdown, boilerplate stripped |
| `select` | `url`, `selectors` | one row per record, columns you named |
| `crawl` | `url` | JS-rendered markdown, `http=` status, link counts |
| `env` | - | interpreter, package versions, live `ddgs` probe |

## Arguments

| Argument | Applies to | Default | Notes |
| --- | --- | --- | --- |
| `query` | search/news/images/videos | - | required for those modes |
| `url` | read/select/crawl | - | required for those modes |
| `max_results` | list modes, `select` | 8 | hard cap 50, clamp is reported |
| `region` | list modes | `wt-wt` | `ru-ru`, `us-en`, ... |
| `timelimit` | list modes | - | `d`/`w`/`m`/`y`, recent only |
| `backend` | list modes | auto chain | `auto,brave,yandex,duckduckgo,bing,yahoo` |
| `selectors` | `select` | - | `name=css` pairs, or a JSON object |
| `max_chars` | read/crawl | 6000 | hard cap 120000, trim is reported |
| `timeout` | all | 40-55 s | clamped to 55 s, see below |
| `wait_for`, `js`, `scroll`, `delay` | `crawl` | - | CSS selector to await, JS to run, lazy-load scroll, extra settle time |
| `focus` | `crawl` | - | BM25 filter: keep only text relevant to this |
| `clean` | `crawl` | `true` | prune menus/footers (`fit_markdown`) |

### Backends

Measured on this machine (same query, `region=ru-ru`):
`yandex` 0.56 s, `yahoo` 0.71 s, `duckduckgo` 0.84 s, `mullvad_brave` 0.89 s,
`brave` 1.37 s (best quality), `bing` 1.45 s, `auto` 2.3-4.0 s (aggregates all).
`google`, `mojeek` and `wikipedia` are dead through ddgs and are never retried.
The default chain stops at the first backend that returns anything, and reports
what it skipped (`Backends tried before this: ...`).

## Guardrails

* **Timeouts are clamped to 55 s** (`WINDOWS_MCP_CLIENT_TIMEOUT`, `0` disables).
  The MCP client aborts a call at ~60 s, so a longer timeout would throw away the
  answer instead of extending it. Long crawls belong in `Job mode=start`.
* **Output is budgeted.** Snippets are cut at 320 characters, page text at
  `max_chars`, and the trim always says how much was hidden and how to see more.
* **Block pages are not passed off as content.** A short document containing
  `captcha`, `verifying you are human`, `enable javascript`, `just a moment`,
  `access denied`, `unusual traffic` or `cloudflare` is returned with an explicit
  `WARNING` and a suggestion to escalate.
* **Failures raise.** A dead backend or an unreadable page surfaces as
  `isError=true` with the list of what was tried, instead of a friendly-looking
  `"Error: ..."` string that an agent would happily quote as a result.
* **UTF-8 everywhere.** The child is started with `PYTHONUTF8=1` and
  `PYTHONIOENCODING=utf-8`, payloads are read as `utf-8-sig` (PowerShell writes a
  BOM), and `PYTHONPATH` is dropped so the server's `src/` never leaks into the
  worker.

## Examples

```jsonc
// what is new in a library
{"mode": "search", "query": "fastmcp 3.0 breaking changes", "max_results": 5, "timelimit": "m"}

// read the page you found
{"mode": "read", "url": "https://gofastmcp.com/changelog", "max_chars": 12000}

// pull a table of records
{"mode": "select", "url": "https://news.ycombinator.com/",
 "selectors": "title=span.titleline > a::text; url=span.titleline > a::attr(href)"}

// SPA that renders with JavaScript
{"mode": "crawl", "url": "https://example.com/app", "wait_for": "css:div.article-body",
 "scroll": true, "focus": "pricing"}

// diagnose
{"mode": "env"}
```

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `No Python interpreter with the search stack was found` | stack not installed, or only in an interpreter that is not on the discovery list | install it, or set `WINDOWS_MCP_SEARCH_PYTHON` |
| `every backend returned nothing` | query too narrow, or the chain is throttled | rephrase, or `backend="yandex"` |
| `WARNING: this looks like a block/captcha page` | anti-bot page | `mode=crawl`, or search instead of fetching |
| `mode=crawl` fails with a Chromium error | Playwright browser missing | `<python> -m playwright install chromium` |
| `did not finish within Ns` | slow site or huge page | lower `max_results`, narrow `focus`, or use `Job` |
| empty `select` output | site changed its CSS classes | check the markup with `mode=read`, fix the selectors |

The local playbook that this tool encodes - measurements, dead backends,
per-marketplace parsers - lives at `Утилиты\seach.md`.
