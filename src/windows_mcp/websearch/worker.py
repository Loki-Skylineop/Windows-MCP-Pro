"""Out-of-process worker for the SearchPro tool.

This file is never imported by the server. It is executed by a *separate*
interpreter - the one that owns the scraping stack (ddgs, trafilatura, scrapling,
crawl4ai) - and speaks a one-line JSON protocol over stdout:

    <python.exe> worker.py <payload.json>
    __WMP_JSON__{"ok": true, "mode": "search", "engine": "ddgs:auto", ...}

Why out of process:

* The MCP server runs on Python 3.14 with pinned dependencies. The search stack
  lives in a different interpreter (Python 3.12 on this machine). Neither can
  break the other's dependency graph.
* A headless browser that hangs or segfaults kills a short-lived child process,
  not the server.
* stdout noise from chatty libraries (scrapling logs to stderr, crawl4ai prints
  progress) cannot corrupt the reply, because the reply is the line carrying the
  sentinel prefix.

Escalation ladder implemented here, cheapest first (see docs/search-pro.md):

    ddgs (pure HTTP metasearch)  ->  trafilatura  ->  scrapling  ->  crawl4ai
"""

from __future__ import annotations

import json
import re
import sys
import time

try:  # pragma: no cover - depends on the host interpreter
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - very old interpreters
    pass

SENTINEL = "__WMP_JSON__"

# Default backend order. 'auto' aggregates every live engine; the rest are the
# ones measured as working on this machine (google/mojeek/wikipedia are dead
# through ddgs and are deliberately not retried).
DEFAULT_BACKENDS = ("auto", "brave", "yandex", "duckduckgo", "bing", "yahoo")

# A short page containing one of these is a block page, not an answer.
BLOCK_MARKERS = (
    "captcha",
    "verifying you are human",
    "are you a robot",
    "enable javascript",
    "access denied",
    "unusual traffic",
    "cloudflare",
    "just a moment",
)
BLOCK_SIZE_LIMIT = 2000

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_STRIP_RE = re.compile(r"<[^>]+>")


def emit(obj: dict) -> None:
    sys.stdout.write(SENTINEL + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _clean_ws(value: object) -> str:
    return " ".join(str(value or "").split())


def _looks_blocked(text: str) -> bool:
    if not text:
        return False
    if len(text) > BLOCK_SIZE_LIMIT:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def _normalise_rows(rows: object) -> list[dict]:
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append({"title": _clean_ws(row), "url": "", "snippet": "", "extra": {}})
            continue
        title = row.get("title") or row.get("content") or ""
        url = (
            row.get("href")
            or row.get("url")
            or row.get("link")
            or row.get("image")
            or row.get("content")
            or ""
        )
        snippet = row.get("body") or row.get("description") or row.get("snippet") or ""
        extra = {}
        for key in ("date", "published", "source", "publisher", "duration", "width", "height"):
            value = row.get(key)
            if value:
                extra[key] = _clean_ws(value)
        out.append(
            {
                "title": _clean_ws(title),
                "url": str(url).strip(),
                "snippet": _clean_ws(snippet),
                "extra": extra,
            }
        )
    return out


def _dedupe(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        key = row.get("url") or row.get("title")
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _call_ddgs(method, query: str, **kwargs) -> list:
    """Call a ddgs method, dropping kwargs the installed version rejects."""
    for drop in (None, "timelimit", "region", "backend"):
        if drop is not None:
            kwargs.pop(drop, None)
        try:
            return list(method(query, **kwargs) or [])
        except TypeError:
            continue
    return list(method(query) or [])


def _search(payload: dict, kind: str) -> dict:
    from ddgs import DDGS

    query = (payload.get("query") or "").strip()
    if not query:
        raise ValueError(f"mode={payload.get('mode')} requires a query")

    limit = int(payload.get("max_results") or 8)
    region = payload.get("region") or "wt-wt"
    timelimit = payload.get("timelimit") or None
    backends = [payload["backend"]] if payload.get("backend") else list(DEFAULT_BACKENDS)

    client = DDGS()
    method = getattr(client, kind)
    tried: list[str] = []

    for backend in backends:
        kwargs = {"max_results": limit, "region": region, "backend": backend}
        if timelimit:
            kwargs["timelimit"] = timelimit
        try:
            rows = _call_ddgs(method, query, **kwargs)
        except Exception as exc:
            tried.append(f"{backend}: {type(exc).__name__}: {exc}")
            continue
        results = _dedupe(_normalise_rows(rows))[:limit]
        if results:
            return {
                "engine": f"ddgs:{backend}",
                "query": query,
                "region": region,
                "results": results,
                "count": len(results),
                "tried": tried,
            }
        tried.append(f"{backend}: 0 results")

    return {
        "ok": False,
        "engine": "ddgs",
        "query": query,
        "results": [],
        "count": 0,
        "tried": tried,
        "error": "every backend returned nothing - rephrase the query or pass backend=yandex",
    }


def do_search(payload: dict) -> dict:
    return _search(payload, "text")


def do_news(payload: dict) -> dict:
    return _search(payload, "news")


def do_images(payload: dict) -> dict:
    return _search(payload, "images")


def do_videos(payload: dict) -> dict:
    return _search(payload, "videos")


def do_books(payload: dict) -> dict:
    """ddgs also indexes books/publications; parity with ddgs' own MCP server."""
    return _search(payload, "books")


def _fetch_html(url: str, timeout: int) -> tuple[str, str]:
    """Return (engine, html). Tries scrapling first, then plain urllib."""
    try:
        from scrapling.fetchers import Fetcher

        page = Fetcher.get(url, timeout=timeout)
        html = getattr(page, "html_content", None) or getattr(page, "body", None) or str(page)
        if html:
            return "scrapling", str(html)
    except Exception:
        pass

    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return "urllib", raw.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    stripped = _TAG_RE.sub(" ", html)
    stripped = _STRIP_RE.sub(" ", stripped)
    return "\n".join(line.strip() for line in stripped.splitlines() if line.strip())


def _read_one(url: str, timeout: int) -> dict:
    """Extract a single URL to markdown: trafilatura first, then HTML fallbacks."""
    tried: list[str] = []

    try:
        import trafilatura

        html = trafilatura.fetch_url(url)
        if html:
            text = trafilatura.extract(
                html,
                output_format="markdown",
                include_links=True,
                include_tables=True,
            )
            if text and text.strip():
                return {
                    "engine": "trafilatura",
                    "url": url,
                    "text": text.strip(),
                    "chars": len(text.strip()),
                    "blocked": _looks_blocked(text),
                    "tried": tried,
                }
            tried.append("trafilatura: extracted nothing")
        else:
            tried.append("trafilatura: fetch returned nothing")
    except ImportError:
        tried.append("trafilatura: not installed")
    except Exception as exc:
        tried.append(f"trafilatura: {type(exc).__name__}: {exc}")

    engine, html = _fetch_html(url, timeout)
    text = ""
    try:
        import trafilatura

        text = (
            trafilatura.extract(html, output_format="markdown", include_links=True) or ""
        ).strip()
        if text:
            engine = f"{engine}+trafilatura"
    except Exception as exc:
        tried.append(f"trafilatura(html): {type(exc).__name__}: {exc}")

    if not text:
        text = _html_to_text(html)
        engine = f"{engine}+striptags"

    return {
        "engine": engine,
        "url": url,
        "text": text,
        "chars": len(text),
        "blocked": _looks_blocked(text),
        "tried": tried,
    }


def do_read(payload: dict) -> dict:
    """Read one URL, or several in a single worker run.

    A batch replies with ``documents`` so the service can tell the two shapes
    apart, and one unreachable page does not throw away the others.
    """
    urls = [str(item).strip() for item in (payload.get("urls") or []) if str(item).strip()]
    if not urls:
        single = (payload.get("url") or "").strip()
        urls = [single] if single else []
    if not urls:
        raise ValueError("mode=read requires a url")

    timeout = int(payload.get("fetch_timeout") or 30)

    if len(urls) == 1:
        return _read_one(urls[0], timeout)

    # Sequential on purpose: the deadline is shared with the service, and a
    # thread pool would make a partial failure much harder to read.
    documents: list[dict] = []
    for url in urls:
        try:
            documents.append(_read_one(url, timeout))
        except Exception as exc:
            documents.append(
                {
                    "url": url,
                    "engine": "-",
                    "text": "",
                    "chars": 0,
                    "blocked": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "tried": [],
                }
            )

    return {
        "engine": "batch",
        "urls": urls,
        "documents": documents,
        "count": len(documents),
    }
def do_select(payload: dict) -> dict:
    url = (payload.get("url") or "").strip()
    if not url:
        raise ValueError("mode=select requires a url")
    selectors = payload.get("selectors") or {}
    if not isinstance(selectors, dict) or not selectors:
        raise ValueError(
            "mode=select requires selectors, e.g. "
            "{'title': 'span.titleline > a::text', 'url': 'span.titleline > a::attr(href)'}"
        )
    timeout = int(payload.get("fetch_timeout") or 30)
    limit = int(payload.get("max_results") or 20)

    from scrapling.fetchers import Fetcher

    page = Fetcher.get(url, timeout=timeout)
    columns: dict[str, list[str]] = {}
    for name, selector in selectors.items():
        try:
            values = page.css(selector)
        except Exception as exc:
            raise ValueError(f"selector {name!r} ({selector!r}) failed: {exc}") from exc
        columns[str(name)] = [_clean_ws(value) for value in list(values)[:limit]]

    height = max((len(values) for values in columns.values()), default=0)
    rows = [
        {name: (values[index] if index < len(values) else "") for name, values in columns.items()}
        for index in range(height)
    ]
    return {
        "engine": "scrapling",
        "url": url,
        "rows": rows,
        "count": len(rows),
        "columns": list(columns),
    }


def do_crawl(payload: dict) -> dict:
    url = (payload.get("url") or "").strip()
    if not url:
        raise ValueError("mode=crawl requires a url")

    import asyncio

    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    run_kwargs: dict = {
        "cache_mode": CacheMode.BYPASS,
        "page_timeout": int(payload.get("page_timeout") or 45000),
    }
    if payload.get("wait_for"):
        run_kwargs["wait_for"] = payload["wait_for"]
    if payload.get("js"):
        run_kwargs["js_code"] = [payload["js"]]
    if payload.get("scroll"):
        run_kwargs["scan_full_page"] = True
    if payload.get("delay"):
        run_kwargs["delay_before_return_html"] = float(payload["delay"])

    focus = (payload.get("focus") or "").strip()
    if payload.get("clean", True):
        try:
            from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

            if focus:
                from crawl4ai.content_filter_strategy import BM25ContentFilter

                content_filter = BM25ContentFilter(user_query=focus)
            else:
                from crawl4ai.content_filter_strategy import PruningContentFilter

                content_filter = PruningContentFilter(threshold=0.45, threshold_type="dynamic")
            run_kwargs["markdown_generator"] = DefaultMarkdownGenerator(
                content_filter=content_filter
            )
        except Exception:
            pass

    async def _run() -> dict:
        browser = BrowserConfig(headless=True, verbose=False)
        async with AsyncWebCrawler(config=browser) as crawler:
            result = await crawler.arun(url=url, config=CrawlerRunConfig(**run_kwargs))
            if not getattr(result, "success", False):
                return {
                    "ok": False,
                    "engine": "crawl4ai",
                    "url": url,
                    "status_code": getattr(result, "status_code", None),
                    "error": getattr(result, "error_message", "crawl failed"),
                }
            markdown = getattr(result, "markdown", None)
            text = ""
            engine = "crawl4ai"
            if markdown is not None:
                fit = getattr(markdown, "fit_markdown", "") or ""
                raw = getattr(markdown, "raw_markdown", "") or ""
                if fit.strip():
                    text, engine = fit, "crawl4ai:filtered"
                else:
                    text, engine = raw, "crawl4ai:raw"
            links = getattr(result, "links", {}) or {}
            return {
                "engine": engine,
                "url": url,
                "status_code": getattr(result, "status_code", None),
                "text": text.strip(),
                "chars": len(text.strip()),
                "blocked": _looks_blocked(text),
                "links_internal": len(links.get("internal", []) or []),
                "links_external": len(links.get("external", []) or []),
            }

    return asyncio.run(_run())


def do_env(payload: dict) -> dict:
    from importlib.metadata import PackageNotFoundError, version

    packages = ("ddgs", "trafilatura", "scrapling", "crawl4ai", "playwright", "lxml", "curl-cffi")
    versions = {}
    for name in packages:
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = "MISSING"
        except Exception as exc:  # pragma: no cover - defensive
            versions[name] = f"error: {exc}"

    probe = {}
    if payload.get("probe", True):
        try:
            from ddgs import DDGS

            rows = _call_ddgs(DDGS().text, "windows mcp", max_results=3, backend="auto")
            probe["ddgs"] = f"ok, {len(rows)} result(s)"
        except Exception as exc:
            probe["ddgs"] = f"{type(exc).__name__}: {exc}"

    return {
        "engine": "env",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "versions": versions,
        "probe": probe,
    }


HANDLERS = {
    "search": do_search,
    "news": do_news,
    "images": do_images,
    "videos": do_videos,
    "books": do_books,
    "read": do_read,
    "select": do_select,
    "crawl": do_crawl,
    "env": do_env,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        emit({"ok": False, "error": "worker requires a payload file path"})
        return 2
    try:
        # utf-8-sig, not utf-8: anything written by PowerShell's
        # `Set-Content -Encoding UTF8` carries a BOM, and json.load chokes on it
        # with "Unexpected UTF-8 BOM". utf-8-sig reads both shapes.
        with open(argv[1], encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except Exception as exc:
        emit({"ok": False, "error": f"cannot read payload: {type(exc).__name__}: {exc}"})
        return 2

    mode = payload.get("mode") or "search"
    handler = HANDLERS.get(mode)
    if handler is None:
        emit(
            {
                "ok": False,
                "error": f"unknown mode {mode!r}; expected: {', '.join(sorted(HANDLERS))}",
            }
        )
        return 2

    started = time.perf_counter()
    try:
        result = handler(payload)
    except ImportError as exc:
        emit(
            {
                "ok": False,
                "mode": mode,
                "error": f"missing package for mode={mode}: {exc}",
                "elapsed": round(time.perf_counter() - started, 2),
            }
        )
        return 1
    except Exception as exc:
        emit(
            {
                "ok": False,
                "mode": mode,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed": round(time.perf_counter() - started, 2),
            }
        )
        return 1

    result.setdefault("ok", True)
    result.setdefault("mode", mode)
    result["elapsed"] = round(time.perf_counter() - started, 2)
    emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main(sys.argv))
