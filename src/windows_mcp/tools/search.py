r"""SearchPro tool - web search, page extraction and browser crawling.

Replaces the original ``Scrape`` tool, which could only fetch one URL and then
asked the client to summarise it through MCP sampling (a feature many clients,
including Notion, do not implement - so it silently returned raw HTML noise).

SearchPro follows the escalation ladder from the local playbook
(``Utilities/seach.md``): direct HTTP metasearch first, article extraction next,
headless browser only as a last resort.
"""

from mcp.types import ToolAnnotations
from windows_mcp.infrastructure import with_analytics
from windows_mcp.websearch import search_service
from fastmcp import Context

_DESCRIPTION = (
    "Search the web and read pages. Keywords: search, google, find, web, news, url, page, "
    "scrape, fetch, extract, crawl, browse, research, documentation, price, release notes. "
    "Modes: "
    "'search' - metasearch across brave/yandex/duckduckgo/bing/yahoo in one call (no captcha, "
    "~1-4s), returns title + url + snippet; "
    "'news' - same but recent news with dates; "
    "'images' / 'videos' - media results; "
    "'books' - book and publication search; "
    "'read' - URL to clean markdown without a browser (article text, menus stripped); "
    "pass up to 5 comma-separated URLs to read them all in one call; "
    "'select' - structured extraction with CSS selectors, e.g. "
    "selectors='title=span.titleline > a::text; url=span.titleline > a::attr(href)'; "
    "'crawl' - headless Chromium for JavaScript-rendered pages, supports wait_for (CSS), "
    "js, scroll and focus (keeps only text relevant to focus); "
    "'env' - report the interpreter and package versions behind this tool. "
    "Start with 'search' when you do not know the URL and 'read' when you do; escalate to "
    "'crawl' only if 'read' comes back empty or blocked. Useful arguments: max_results "
    "(default 8), region (e.g. ru-ru, us-en, wt-wt), timelimit (d/w/m/y for recent only), "
    "backend (force one engine), max_chars (output budget, default 6000), timeout. "
    "Output is trimmed with an explicit hint rather than flooding the context, and block/"
    "captcha pages are reported as such instead of being passed off as content. Repeat "
    "read/select/crawl calls are served from a 10-minute in-process cache, so re-reading "
    "a page you already fetched costs nothing."
)


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="SearchPro",
        description=_DESCRIPTION,
        annotations=ToolAnnotations(
            title="SearchPro",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "SearchPro-Tool")
    def search_pro_tool(
        mode: str = "search",
        query: str | None = None,
        url: str | None = None,
        max_results: int | None = None,
        region: str | None = None,
        timelimit: str | None = None,
        backend: str | None = None,
        selectors: str | None = None,
        max_chars: int | None = None,
        timeout: int | None = None,
        wait_for: str | None = None,
        js: str | None = None,
        scroll: bool | str = False,
        delay: float | None = None,
        focus: str | None = None,
        clean: bool | str = True,
        ctx: Context = None,
    ) -> str:
        return search_service.run(
            mode=mode,
            query=query,
            url=url,
            max_results=max_results,
            region=region,
            timelimit=timelimit,
            backend=backend,
            selectors=selectors,
            max_chars=max_chars,
            timeout=timeout,
            wait_for=wait_for,
            js=js,
            scroll=scroll,
            delay=delay,
            focus=focus,
            clean=clean,
        )

    return search_pro_tool
