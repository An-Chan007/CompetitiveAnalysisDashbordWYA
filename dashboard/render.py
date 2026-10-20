#!/usr/bin/env python3
"""Fetch a page the way a browser would, so JavaScript-built content is readable.

WHY THIS EXISTS, AND WHAT IT IS ALLOWED TO BREAK

Everything else in this project is standard library only, deliberately: no pip
install, nothing to rot, nothing to pin. This module is the one exception, and it
is quarantined here so the exception cannot spread.

  collect.py       stdlib only. Never imports this module. If crawl4ai is broken,
                   missing, or its browser will not start, the daily collection
                   still runs exactly as before.
  render.py        may use crawl4ai. Falls back to collect.fetch() when it cannot.
  sitescan.py      reads through here.
  pricing.py       reads through here.

Three things stdlib provably cannot do, all of them measured, not assumed:

1. SPA catch-all sites. estimote.com, occuspace.io and steerpath.com return
   byte-identical HTML for every path - /pricing, /products, even /sitemap.xml.
   19 "watched" pages across the three share one hash per site and can never
   report anything. Occuspace is one of the nine companies that actually compete
   and its pricing page has therefore never been read.
2. Pricing tables built client-side, or hidden behind a tab that only exists once
   scripts run. Estimote's "$199 dev kit with 3 tags" is only in the rendered DOM.

A browser solves both. Measured with `--prove` on 2026-09-04:
   steerpath.com      454 -> 21,824 chars   (48x)
   occuspace.io     12,754 -> 24,647 chars
   estimote.com     11,166 -> 13,058 chars

WHAT A BROWSER DOES NOT SOLVE, TESTED AND ABANDONED

Google News links. HANDOVER lists these as the biggest constraint on analysis and
suggests a headless browser would fix them. It does not. Tested 2026-09-04 with
Playwright directly: the URL lands on consent.google.com and stays there. It is a
consent wall, not a redirect that more patience resolves. Getting past it means
programmatically accepting Google's consent terms on every fetch, at scale, which
this project will not do. GNEWS_RE is kept below so these URLs are recognised and
skipped rather than retried hopefully.

If article text behind those ~2,000 headlines is ever wanted, the honest route is
the publisher, not Google: the feed already carries the publisher name (see
split_publisher in collect.py), so the article can be looked for on the
publisher's own site. More work, lower yield, and it blocks nothing that has
actually been asked for - competitor sites and pricing pages are direct fetches.

Usage:
    python3 render.py --selftest          # offline, no browser needed
    python3 render.py --prove             # the four cases stdlib cannot do
    python3 render.py <url> [<url> ...]   # render and print the text
"""

import asyncio
import importlib.util
import re
import sys

import collect        # stdlib-only sibling: fetch(), block_text(), strip_tags()

TIMEOUT_MS = 45_000

# Sites known to serve one page for every path unless JavaScript runs. Recorded so
# a future reader knows these are not guesses - each was verified with curl,
# serving identical bytes for five different URLs.
SPA_HOSTS = {"estimote.com", "occuspace.io", "steerpath.com"}

GNEWS_RE = re.compile(r"^https?://news\.google\.com/", re.I)


def available():
    """True when crawl4ai can actually be imported. Cheap - no browser launched."""
    return importlib.util.find_spec("crawl4ai") is not None


def unreachable(url):
    """True when no amount of fetching will produce article text.

    Only Google News today: the link lands on a consent wall, verified with
    Playwright. Recognising it means we skip instead of spending a browser launch
    per item on ~2,000 of them and reporting the consent page as content.
    """
    return bool(GNEWS_RE.match(url or ""))


def _needs_browser(url):
    """A hint, not a rule. Callers may force rendering for anything."""
    if unreachable(url):
        return False
    host = re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0].lower()
    return host in SPA_HOSTS


async def _render_many(urls, wait_for_selector=None, clean=False):
    """Render each URL in one browser session. Returns {url: {...}}.

    `clean` prunes navigation, footers and image markup and returns prose only.
    Without it the first 1,400 characters of every Occuspace page are the same
    nav bar and a wall of ![](cdn...) image links, so an analyst reading a 6,000
    character extract spends a quarter of it on boilerplate and the pages of one
    site look nearly identical to each other. With it, the government page opens
    on "Federal and state agencies face strict privacy mandates... no-camera
    requirements". Links are kept in the default mode because sitescan needs them
    to discover pages.
    """
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    from crawl4ai.async_configs import CacheMode

    browser = BrowserConfig(headless=True, verbose=False)
    kw = {}
    if clean:
        try:
            from crawl4ai.content_filter_strategy import PruningContentFilter
            from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
            kw["markdown_generator"] = DefaultMarkdownGenerator(
                content_filter=PruningContentFilter(threshold=0.45,
                                                    threshold_type="dynamic"),
                options={"ignore_links": True, "ignore_images": True})
        except ImportError:
            pass                          # older crawl4ai: fall back to raw markdown
    run = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,      # a competitor's page must be today's
        page_timeout=TIMEOUT_MS,
        wait_for=wait_for_selector,
        scan_full_page=True,              # pull in lazy-loaded pricing rows
        remove_overlay_elements=True,     # cookie walls hide the content beneath
        **kw,
    )
    out = {}
    async with AsyncWebCrawler(config=browser) as crawler:
        for url in urls:
            try:
                res = await crawler.arun(url=url, config=run)
            except Exception as e:        # one bad page must not stop the batch
                out[url] = {"url": url, "ok": False, "via": "browser",
                            "error": f"{type(e).__name__}: {e}", "text": "",
                            "final_url": url}
                continue
            md = ""
            if getattr(res, "markdown", None):
                if clean:
                    md = getattr(res.markdown, "fit_markdown", "") or ""
                if not md:
                    md = getattr(res.markdown, "raw_markdown", None) or str(res.markdown)
            out[url] = {
                "url": url,
                "final_url": getattr(res, "url", url) or url,
                "ok": bool(getattr(res, "success", False)) and bool(md.strip()),
                "via": "browser",
                "text": md,
                "error": getattr(res, "error_message", "") or "",
            }
    return out


def _via_stdlib(url):
    """The fallback. Real content for ordinary server-rendered pages.

    Guarded even though collect.fetch() is itself guaranteed not to raise. That
    guarantee is exactly the one that was silently broken before (EOFError escaped
    a narrower except and killed the daily run for weeks), so the layer above does
    not lean on it.
    """
    try:
        body = collect.fetch(url)
    except Exception as e:
        return {"url": url, "final_url": url, "ok": False, "via": "stdlib",
                "text": "", "error": f"{type(e).__name__}: {e}"}
    if not body:
        return {"url": url, "final_url": url, "ok": False, "via": "stdlib",
                "text": "", "error": "fetch returned nothing"}
    return {"url": url, "final_url": url, "ok": True, "via": "stdlib",
            "text": collect.block_text(body), "error": ""}


def render(urls, force=False, wait_for_selector=None, clean=False):
    """Fetch each URL as well as this machine can. Never raises.

    Uses a browser when crawl4ai is installed and either `force` is set or the URL
    looks like it needs one; otherwise the stdlib path. A browser failure falls
    back rather than propagating - a degraded read beats no read, and `via` in the
    result says which path produced the text so a caller can tell the difference.
    """
    if isinstance(urls, str):
        urls = [urls]
    urls = list(urls)
    if not urls:
        return {}

    results_skipped = {u: {"url": u, "final_url": u, "ok": False, "via": "skipped",
                           "text": "", "error": "consent-walled, see unreachable()"}
                       for u in urls if unreachable(u)}
    urls = [u for u in urls if u not in results_skipped]
    if not urls:
        return results_skipped

    want = [u for u in urls if force or _needs_browser(u)] if not force else urls
    results = {}

    if want and available():
        try:
            results = asyncio.run(_render_many(want, wait_for_selector, clean))
        except Exception as e:            # browser missing, sandboxed, out of memory
            print(f"  ! browser unavailable, falling back to stdlib: "
                  f"{type(e).__name__} {e}", file=sys.stderr)
            results = {}
    elif want and not available():
        print("  ! crawl4ai not installed - stdlib fallback for every URL. "
              "JS-rendered pages will read as empty or as a boilerplate shell.",
              file=sys.stderr)

    for u in urls:
        if not results.get(u, {}).get("ok"):
            fallback = _via_stdlib(u)
            # Keep the browser's error message when it had one worth reporting.
            if u in results and results[u].get("error") and not fallback["ok"]:
                fallback["error"] = results[u]["error"]
            results[u] = fallback
    results.update(results_skipped)
    return results


def render_one(url, **kw):
    return render([url], **kw)[url]


# --------------------------------------------------------------------- prove

# The acceptance test for the dependency. If these do not come back with real
# text, crawl4ai has not earned its place and should be dropped rather than kept
# out of sunk cost. Each URL is one of the three documented stdlib failures.
PROVE = [
    ("https://steerpath.com/",
     "SPA catch-all: stdlib sees 454 chars of shell, a browser sees the whole "
     "site. The starkest of the three."),
    ("https://occuspace.io/pricing",
     "SPA catch-all: identical bytes for every path under stdlib. Occuspace is "
     "one of the nine that compete and this is its pricing page."),
    ("https://estimote.com/pricing", "SPA catch-all."),
]


def prove(urls=None):
    """Render the cases stdlib cannot do, and show the difference side by side."""
    cases = [(u, "") for u in urls] if urls else PROVE
    if not available():
        print("crawl4ai is not installed - nothing to prove.\n"
              "  pip3 install --user crawl4ai && crawl4ai-setup")
        return 1

    worse = 0
    for url, why in cases:
        print(f"\n{'=' * 72}\n{url}")
        if why:
            print(f"  why: {why}")
        plain = _via_stdlib(url)
        rich = render_one(url, force=True)
        pt, rt = plain["text"].strip(), rich["text"].strip()
        print(f"  stdlib : {len(pt):7,} chars  ok={plain['ok']}")
        print(f"  browser: {len(rt):7,} chars  ok={rich['ok']}  via={rich['via']}")
        if rich["error"]:
            print(f"  error  : {rich['error'][:160]}")
        if rt:
            head = " ".join(rt.split())[:220]
            print(f"  reads  : {head}")
        if len(rt) <= len(pt):
            worse += 1
            print("  ! the browser did not beat stdlib here")
    print(f"\n{len(cases) - worse}/{len(cases)} cases where rendering won.")
    return 0 if worse < len(cases) else 1


def selftest():
    """Offline. No browser, no network - this runs as a workflow pre-flight."""
    assert _needs_browser("https://occuspace.io/pricing")
    assert _needs_browser("https://www.steerpath.com/anything")
    assert not _needs_browser("https://crowdconnected.com/pricing")
    assert not _needs_browser("")
    # A Google News link is skipped, never rendered. Launching a browser for each
    # of ~2,000 of them to land on a consent page is the waste this prevents.
    gn = "https://news.google.com/rss/articles/CBMiabc"
    assert unreachable(gn) and not _needs_browser(gn)
    assert render([gn])[gn]["via"] == "skipped"

    # render() must never raise, whatever the fetch layer does, and must always
    # return one entry per URL. This is the same promise collect.fetch() makes and
    # broke - see the EOFError that used to kill the daily run.
    real = collect.fetch
    try:
        collect.fetch = lambda u: (_ for _ in ()).throw(RuntimeError("boom"))
        out = render(["https://a.test/x", "https://b.test/y"])
        assert set(out) == {"https://a.test/x", "https://b.test/y"}, out
        assert all(not r["ok"] for r in out.values())
        collect.fetch = lambda u: "<h2>Pricing</h2><p>&pound;8 per delegate</p>"
        got = render_one("https://a.test/x")
        assert got["ok"] and "8 per delegate" in got["text"], got
        assert got["via"] == "stdlib"
    finally:
        collect.fetch = real

    # available() must be a cheap import check, never a browser launch.
    assert isinstance(available(), bool)
    print("render selftest OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
    elif "--prove" in args:
        raise SystemExit(prove([a for a in args if not a.startswith("-")] or None))
    else:
        targets = [a for a in args if not a.startswith("-")]
        if not targets:
            raise SystemExit(__doc__)
        for url, res in render(targets, force=True).items():
            print(f"\n{'=' * 72}\n{url}  ({res['via']}, ok={res['ok']}, "
                  f"{len(res['text']):,} chars)")
            if res["error"]:
                print(f"error: {res['error']}")
            print(res["text"][:4000])
