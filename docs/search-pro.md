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
| `books` | `query` | Open Library works: authors, first published, editions, languages |
| `read` | `url` | page as markdown, boilerplate stripped; 1-5 URLs per call |
| `select` | `url`, `selectors` | one row per record, columns you named |
| `crawl` | `url` | JS-rendered markdown, `http=` status, link counts |
| `env` | - | interpreter, package versions, live `ddgs` probe |

## Arguments

| Argument | Applies to | Default | Notes |
| --- | --- | --- | --- |
| `query` | search/news/images/videos/books | - | required for those modes |
| `url` | read/select/crawl | - | required for those modes; `read` takes up to 5, separated by commas, spaces or newlines |
| `max_results` | list modes, `select` | 8 | hard cap 50, clamp is reported |
| `region` | list modes | `wt-wt` | `ru-ru`, `us-en`, ... |
| `timelimit` | list modes | - | `d`/`w`/`m`/`y`, recent only |
| `backend` | list modes | auto chain | `auto,brave,yandex,duckduckgo,bing,yahoo`; ignored by `books` |
| `selectors` | `select` | - | `name=css` pairs separated by `;` or newlines (a comma is part of CSS), or a JSON object |
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

### Books come from Open Library, not ddgs

`ddgs` still exposes a `books()` method, but every backend behind it answers
`No results found` (measured 2026-09), so `mode=books` was a mode that could
only fail. It now queries [Open Library](https://openlibrary.org/dev/docs/api/search)
directly - no key, no quota - and returns the work title, up to three authors,
the first publication year, the edition count, languages and subjects, plus the
total number of matches when it exceeds the page. `backend` and `timelimit` do
not apply there and are reported as ignored. To find book *pages* on the open
web, `mode=search` is still the right tool.

### How select and read fetch

Both share one fetch ladder: a single scrapling attempt with half the timeout
budget, then plain `urllib` with the other half. scrapling defaults to three
attempts and gives each one the full timeout, so a host that rejects
curl_cffi's TLS fingerprint used to burn 3x20 s and die on the deadline instead
of failing over. The reply says which engine actually delivered the HTML
(`engine=scrapling` or `engine=urllib`).

### Caching

`read`, `select` and `crawl` replies are cached in-process for
`WINDOWS_MCP_SEARCH_CACHE_TTL` seconds (default 600, `0` disables it), 32
entries, keyed by mode + URL + every argument that changes the output. An agent
that reads a page, writes code, fails and reads the same page again used to pay
the full network cost each time - up to 55 s when a browser crawl was involved.

The cache is honest about itself: a served entry carries
`Note: served from cache, fetched 42s ago.`, so a stale answer can never be
mistaken for a fresh fetch. Failed and timed-out replies are never stored, and
`search`/`news`/`images`/`videos`/`books` are never cached - freshness is the
whole point of asking.

### Batch read

`read` accepts up to five URLs in one call:

```jsonc
{ "mode": "read", "url": "https://a.dev/docs, https://b.dev/api", "max_chars": 12000 }
```

They are fetched inside one worker run instead of one subprocess spawn plus one
model round trip per page, which is the difference between reading the top three
search hits in ~5 s and in ~20 s. Each page is returned under its own heading and
splits the `max_chars` budget evenly - the header states `budget=N chars per
page` - so five pages can never blow past the cap; if one URL dies it gets its own `failed: ...`
line and the others still arrive. The other modes reject a list rather than
silently reading the first URL.

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

// pull a table of records (pairs are separated by ';', never by a comma)
{"mode": "select", "url": "https://news.ycombinator.com/",
 "selectors": "title=span.titleline > a::text; url=span.titleline > a::attr(href)"}

// look up a book
{"mode": "books", "query": "dune frank herbert", "max_results": 5}

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
| `Invalid CSS selector 'h1, body=p'` | selector pairs joined with a comma | separate the pairs with `;` or a newline |
| `Open Library did not answer` | openlibrary.org unreachable or throttled | retry, or use `mode=search` for book pages |

The local playbook that this tool encodes - measurements, dead backends,
per-marketplace parsers - lives at `Утилиты\seach.md`.
