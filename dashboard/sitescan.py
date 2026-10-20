#!/usr/bin/env python3
"""Read each competitor's website properly, and prepare it for analysis.

WHAT THIS IS FOR

Until now nothing read a competitor's site. collect.py hashed a handful of pages
and reported "the pricing page changed" - which is a notification, not an
understanding. It could not answer "what does this company actually sell now",
"who are they selling it to", or "what have they started saying that they were
not saying last month".

This does two separable jobs, and keeping them separate is the point:

  1. BUILD A CORPUS (this file, no credential needed)
     Render the pages that carry meaning, extract readable text, store it, and
     diff it against the last read. Costs nothing but time.

  2. HAND IT TO AN ANALYST (inbox.json)
     Write one packet containing the day's collected items, the site deltas and
     the pricing observations. Whoever analyses - a Claude session on a schedule,
     a session on demand, or an API key in CI - reads the same file and writes
     the same analysis.json. Swapping the driver needs no rebuild.

CADENCE

  --daily    every pricing page plus the ~12 highest-value pages per company
  --weekly   the full sweep, up to MAX_PAGES per company, chosen from the sitemap

Both are "daily fresh" for the pages that move; the weekly sweep is what notices
a company quietly publishing a new sector vertical.

SCOPE
The ten companies that actually compete. The other 24 are tracked for market
signal, and reading 40 pages a day of a company rated low threat would cost more
than it tells anyone.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import collect
import render
from pricing import http_status as pricing_status

HERE = Path(__file__).parent
CONFIG = HERE / "competitors.json"
CHANGES = HERE / "changes.jsonl"
CORPUS = HERE / "sitecorpus"
INBOX = HERE / "inbox.json"
ANALYSIS = HERE / "analysis.json"
FINDINGS = HERE / "pricing_findings.json"

COMPETES = ["cisco-spaces", "crowd-connected", "festyvent", "mapsted", "meshh", "pointr",
            "purple", "apple-find-my", "occuspace", "venuiq", "hoxton-ai", "waittime"]

# Path fragments worth reading, most telling first. A competitor's pricing,
# product and case-study pages say what they sell and to whom; their careers page
# says where they are investing before any of it is announced.
PRIORITY = [
    ("pricing", 100), ("price", 100), ("plans", 95),
    ("product", 80), ("platform", 78), ("solution", 76), ("feature", 70),
    ("case-stud", 68), ("case_stud", 68), ("customer", 66), ("client", 64),
    ("industr", 62), ("sector", 62), ("vertical", 60), ("use-case", 60),
    ("integrat", 50), ("partner", 48), ("hardware", 46), ("technolog", 44),
    ("career", 40), ("job", 38), ("about", 30), ("news", 26), ("blog", 24),
    ("resource", 20), ("guide", 18), ("whitepaper", 18),
]

DAILY_PAGES = 12
MAX_PAGES = 40
TEXT_CAP = 6000          # per page, matching analyse.MAX_CHARS
INBOX_ITEM_DAYS = 2
INBOX_MAX_ITEMS = 60


def today():
    return datetime.now(timezone.utc).date().isoformat()


def load_config():
    return json.loads(CONFIG.read_text())


def score(url):
    """How much this URL is likely to be worth reading. 0 means skip it."""
    if collect.is_asset(url) or collect.is_foreign_locale(url):
        return 0
    path = re.sub(r"^https?://[^/]+", "", url).lower()
    if path in ("", "/"):
        return 90                        # a homepage states the current pitch
    if re.search(r"/(privacy|terms|cookie|legal|gdpr|sitemap|tag/|author/)", path):
        return 0
    # A shallow page nobody thought to add a keyword for is still worth reading.
    # Occuspace names its sector pages /government-occupancy-analytics and its
    # products /macro, /micro, /octi - none of which contain "industry", "sector"
    # or "product", so a keywords-only score dropped every one of them while
    # keeping the blog. The explicit skip list above stays absolute; this is the
    # floor for everything else.
    best = 25 if path.strip("/").count("/") == 0 else 0
    for frag, weight in PRIORITY:
        if frag in path:
            best = max(best, weight)
    # A deep path is usually one article; a shallow one is usually a section page.
    depth = path.strip("/").count("/")
    return max(0, best - depth * 4)


def canon(u):
    """One spelling per page. www.festyvent.com and festyvent.com are one page;
    both were being read, costing a browser launch for identical text."""
    u = (u or "").split("#")[0].rstrip("/") or u
    return re.sub(r"^(https?://)www\.", r"\1", u)


def candidate_urls(comp, limit, pool_out=None):
    """The pages worth reading for one competitor, best first, deduped.
    pool_out, if given, receives every URL the site offered (sitemap or homepage
    links) - what we know exists, whether or not it is read today."""
    watch = comp.get("watch") or {}
    urls, seen = [], set()

    def add(u, forced=False):
        u = canon(u)
        if not u or u in seen:
            return
        if not forced and not score(u):
            return
        seen.add(u)
        urls.append(u)

    site = (comp.get("website") or "").rstrip("/")
    if site:
        add(site, forced=True)
    # A configured watch is a deliberate human choice - always read those.
    for pg in watch.get("pages") or []:
        add(pg.get("url"), forced=True)
    p = (comp.get("pricing") or {}).get("page")
    if p:
        add(p, forced=True)

    pool = set()
    if watch.get("sitemap"):
        body = collect.fetch(watch["sitemap"])
        if body:
            pool |= set(collect.sitemap_urls(body))
    if len(pool) < limit and site:
        # No sitemap, or a thin one. Occuspace, Festyvent and Apple Find My have
        # none, so the first run read exactly one page each and the corpus said
        # almost nothing about three of the nine. Their own homepage links are the
        # obvious next source and cost one extra fetch.
        pool |= set(homepage_links(site))
    if pool_out is not None:
        pool_out |= {canon(u) for u in pool if score(u)}   # only pages we could read
    if pool:
        ranked = sorted(((score(u), u) for u in pool), key=lambda t: -t[0])
        for sc, u in ranked:
            if sc <= 0 or len(urls) >= limit:
                break
            add(u)
    return urls[:limit]


# crawl4ai returns markdown, so links arrive as [text](url) rather than href=.
MD_LINK_RE = re.compile(r"\]\((https?://[^\s)]+)\)")


def _brand(host):
    """The registrable name, ignoring www and TLD: occuspace.io -> 'occuspace'."""
    h = re.sub(r"^www\.", "", (host or "").lower())
    return h.split(".")[0] if h else ""


def links_in(text, site):
    """Internal links found in rendered markdown. Pure, so it is testable offline.

    Matching is on the brand rather than the exact host, because a company that
    moves TLD keeps every link on the old site pointing at the new one. Occuspace
    has done exactly this - occuspace.io now links only to occuspace.com, so an
    exact-host filter returned zero links and the corpus held one stale page for
    a company rated medium threat.
    """
    host = re.sub(r"^https?://(www\.)?", "", site or "").split("/")[0].lower()
    if not host:
        return []
    brand = _brand(host)
    out = set()
    for m in MD_LINK_RE.finditer(text or ""):
        u = m.group(1).split("#")[0].rstrip("/")
        bare = re.sub(r"^(https?://)(www\.)", r"\1", u)
        if _brand(bare.split("//", 1)[-1].split("/")[0]) != brand:
            continue
        # A site links to itself both with and without www; keeping both reads
        # every such page twice and doubles the browser time for no new text.
        out.add(bare)
    return sorted(out)


def homepage_links(site):
    """Internal links from a site's own homepage. Rendered, because a JS-built nav
    is invisible to a plain fetch - which is the case for the three SPA sites."""
    res = render.render_one(site, force=True)
    return links_in(res.get("text"), site) if res.get("ok") else []


def read_site(comp, limit, verbose=True):
    """Render this competitor's pages and return a corpus dict."""
    cid = comp["id"]
    pool = set()
    urls = candidate_urls(comp, limit, pool)
    if verbose:
        print(f"\n{cid} — reading {len(urls)} pages")
    # clean=True prunes nav, footers and image markup. The analyst reads a capped
    # extract of each page, and without this a quarter of that cap is the same
    # navigation bar on every page of the site.
    results = render.render(urls, force=True, clean=True)
    pages, dead = [], []
    for u in urls:
        r = results.get(u) or {}
        text = (r.get("text") or "").strip()
        # A browser renders a 404 with the site's full chrome - VenuIQ's dead
        # pricing page comes back as 6,284 readable characters of nav and footer.
        # Storing that as a page puts boilerplate in front of the analyst as
        # though it were content, so the status is checked before it is kept.
        status = pricing_status(u)
        if status is not None and status >= 400:
            dead.append(u)
            if verbose:
                print(f"  ! {u[:70]}  HTTP {status}, not stored")
            continue
        if not r.get("ok") or len(text) < 200:
            if verbose:
                print(f"  ! {u[:78]}  ({r.get('error','too short')[:40]})")
            continue
        pages.append({"url": u, "via": r.get("via", ""),
                      "chars": len(text), "text": text[:TEXT_CAP]})
        if verbose:
            print(f"  ok {len(text):7,}  {u[:74]}")
    return {"id": cid, "name": comp["name"], "read": today(),
            "website": comp.get("website", ""), "pages": pages,
            "dead": dead, "pool": sorted(pool | set(urls))}


def known_urls(corpus):
    """Every page we have ever known this site to have: read, or offered by its
    sitemap or homepage links on any earlier day."""
    c = corpus or {}
    return set(c.get("known") or []) | {p["url"] for p in c.get("pages", [])}


def diff_corpus(old, new):
    """What changed between two reads. Page-level, because that is what a reader
    can act on: a page that appeared, one that vanished, one whose text moved.

    The daily read is a sample of ~12 pages, and which equal-scoring pages make
    the cut varies from day to day. Comparing one day's sample with the last one
    reported every page that rotated in as "new" and every one that rotated out
    as "gone": Meshh case studies from 2024 and February 2026 were reported as
    newly published on 15 separate days (25 Sep - 2 Oct alone: eight). So a page
    is new only if this site has never shown it to us before, and gone only if it
    was read and now answers 4xx/5xx. A first read is the baseline: nothing new."""
    oldp = {p["url"]: p["text"] for p in (old or {}).get("pages", [])}
    newp = {p["url"]: p["text"] for p in new.get("pages", [])}
    known = known_urls(old)
    added = sorted(set(newp) - known) if old else []
    gone = sorted(u for u in new.get("dead", []) if u in known)
    moved = []
    for u in sorted(set(oldp) & set(newp)):
        if oldp[u] == newp[u]:
            continue
        a, b = oldp[u].split("\n"), newp[u].split("\n")
        gained = [ln.strip() for ln in b if ln.strip() and ln not in a]
        lost = [ln.strip() for ln in a if ln.strip() and ln not in b]
        if gained or lost:
            moved.append({"url": u, "gained": gained[:8], "lost": lost[:4],
                          "n_gained": len(gained), "n_lost": len(lost)})
    return {"new_pages": added, "gone_pages": gone, "changed_pages": moved}


def scan(ids=None, limit=DAILY_PAGES, verbose=True):
    CORPUS.mkdir(exist_ok=True)
    cfg = load_config()
    wanted = ids or COMPETES
    out = {}
    for comp in cfg["competitors"]:
        if comp["id"] not in wanted:
            continue
        path = CORPUS / f"{comp['id']}.json"
        old = json.loads(path.read_text()) if path.exists() else None
        new = read_site(comp, limit, verbose)
        if not new["pages"]:
            if verbose:
                print(f"  ! {comp['id']}: nothing readable, keeping the last corpus")
            continue
        new["delta"] = diff_corpus(old, new)
        new["previous_read"] = (old or {}).get("read", "")
        new["known"] = sorted(known_urls(old) | set(new.pop("pool", [])) |
                              {p["url"] for p in new["pages"]})
        path.write_text(json.dumps(new, indent=1, ensure_ascii=False) + "\n")
        out[comp["id"]] = new
        if verbose:
            d = new["delta"]
            print(f"  -> {len(new['pages'])} pages, {len(d['new_pages'])} new, "
                  f"{len(d['changed_pages'])} changed, {len(d['gone_pages'])} gone")
    return out


# ------------------------------------------------------------------- the inbox

# The whole point of this file. One packet, everything an analyst needs, no
# credential required to produce it. The analysis driver is pluggable precisely
# because it reads this and nothing else.
def build_inbox(days=INBOX_ITEM_DAYS):
    cfg = load_config()
    comps = {c["id"]: c for c in cfg["competitors"]}
    cut = (datetime.now(timezone.utc).date() -
           __import__("datetime").timedelta(days=days)).isoformat()

    pub_cut = (datetime.now(timezone.utc).date() -
               __import__("datetime").timedelta(days=3)).isoformat()
    items = []
    if CHANGES.exists():
        for line in CHANGES.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (r.get("seen_at") or "")[:10] < cut:
                continue
            # The analyser drops anything published more than three days ago
            # (FEED_WINDOW_DAYS in analyse.py), so it must not take one of the
            # 60 slots here either - the packet was full on 9 October.
            if (r.get("published") or "9999")[:10] < pub_cut:
                continue
            items.append({k: r.get(k) for k in
                          ("key", "entity_type", "competitor_id", "competitor",
                           "kind", "title", "publisher", "url", "published",
                           "gnews_url", "seen_at", "release_notes")
                          if k != "release_notes" or r.get(k)})
    items = items[-INBOX_MAX_ITEMS:]

    sites = {}
    if CORPUS.exists():
        for f in sorted(CORPUS.glob("*.json")):
            c = json.loads(f.read_text())
            d = c.get("delta") or {}
            if not (d.get("new_pages") or d.get("changed_pages") or d.get("gone_pages")):
                continue
            sites[c["id"]] = {
                "name": c["name"], "read": c["read"],
                "previous_read": c.get("previous_read", ""),
                "new_pages": d["new_pages"][:15],
                "gone_pages": d["gone_pages"][:10],
                "changed_pages": d["changed_pages"][:10],
            }

    pricing = {}
    for cid, comp in comps.items():
        p = comp.get("pricing") or {}
        obs = p.get("observed")
        if not obs and not p.get("history"):
            continue
        pricing[cid] = {"name": comp["name"],
                        "written_headline": p.get("headline", ""),
                        "observed": obs, "history": p.get("history", []),
                        "disagrees": p.get("disagrees", False)}

    already = set()
    if ANALYSIS.exists():
        try:
            for a in json.loads(ANALYSIS.read_text()).get("reviewed", []):
                if a.get("url"):
                    already.add(a["url"])
        except json.JSONDecodeError:
            pass

    packet = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": days,
        "how_to_use": (
            "Analysis packet for the WYA market monitor. Judge what matters and "
            "write entries into analysis.json in the format its meta.style "
            "describes: two sentences, three at most - the concrete fact with a "
            "real number or name, then what it means for us, including saying "
            "'no signal' when that is true. Say when something does NOT matter. "
            "House rules, all enforced elsewhere and not to be broken here: never "
            "invent a commercial figure; never name an individual person or an "
            "internal document; no sales-coaching voice ('lead with', 'say this', "
            "'position this as'); never describe FlexNet as self-healing or "
            "auto-rerouting. Items whose url is in already_analysed have been "
            "covered - skip them."),
        "counts": {"items": len(items), "sites_with_changes": len(sites),
                   "pricing_records": len(pricing)},
        "already_analysed": sorted(already),
        "items": items,
        "site_changes": sites,
        "pricing": pricing,
    }
    INBOX.write_text(json.dumps(packet, indent=1, ensure_ascii=False) + "\n")
    return packet


def selftest():
    """Offline. No network, no browser."""
    # Scoring: a pricing page beats a blog post beats a cookie policy.
    assert score("https://x.test/pricing") > score("https://x.test/blog/a-post")
    assert score("https://x.test/privacy-policy") == 0
    # A page with no matching keyword is still read if it is shallow - that is how
    # /government-occupancy-analytics and /octi get picked up. A top-level page the
    # company chose to create outranks any single blog post.
    assert score("https://x.test/government-occupancy-analytics") > 0
    assert score("https://x.test/octi") > 0
    assert score("https://x.test/a/b/c/unknown-thing") == 0, "deep and unnamed"
    assert score("https://x.test/pricing") > score("https://x.test/octi")
    assert score("https://x.test/octi") > score("https://x.test/blog/a-post")
    assert score("https://x.test/logo.png") == 0
    assert score("https://x.test/") == 90, "a homepage states the current pitch"
    assert score("https://x.test/de-de/pricing") == 0, "locale copies are duplicates"
    assert score("https://x.test/products") > score("https://x.test/a/b/c/products")

    # A configured watch is a human decision and must be read even if scoring
    # would have skipped it. This is why `forced` exists.
    # Link extraction is pure, so it is tested directly rather than over the wire.
    md = ("See [pricing](https://x.test/pricing) and "
          "[a partner](https://other.test/x) and [docs](https://www.x.test/docs#top)")
    assert links_in(md, "https://x.test") == ["https://x.test/docs",
                                              "https://x.test/pricing"], links_in(md, "https://x.test")
    assert links_in(md, "") == []
    # A company that moves TLD must still be followed. Occuspace moved .io -> .com
    # and an exact-host filter found zero links on its own homepage.
    moved = "[new home](https://www.occuspace.com/technology) [not us](https://elsewhere.com/x)"
    assert links_in(moved, "https://occuspace.io") == ["https://occuspace.com/technology"]
    assert links_in(moved, "https://other.io") == []

    comp = {"id": "x", "name": "X", "website": "https://x.test",
            "watch": {"pages": [{"label": "privacy-ish", "url": "https://x.test/legal"}]},
            "pricing": {"page": "https://x.test/hidden-prices"}}
    # This selftest is a workflow pre-flight and must make no network call.
    global homepage_links
    _real = homepage_links
    homepage_links = lambda site: []
    try:
        urls = candidate_urls(comp, 10)
    finally:
        homepage_links = _real
    assert "https://x.test/legal" in urls, urls
    assert "https://x.test/hidden-prices" in urls, urls
    assert len(urls) == len(set(urls)), "no duplicates"

    # The diff is what the analyst actually reads, so it must be right.
    old = {"pages": [{"url": "a", "text": "one\ntwo"}, {"url": "b", "text": "keep"}],
           "known": ["a", "b", "r"]}
    new = {"pages": [{"url": "a", "text": "one\nthree"}, {"url": "c", "text": "fresh"},
                     {"url": "r", "text": "rotated in"}], "dead": []}
    d = diff_corpus(old, new)
    assert d["new_pages"] == ["c"], d
    assert d["gone_pages"] == [], "a page not sampled today is not gone"
    assert d["changed_pages"][0]["gained"] == ["three"], d
    assert d["changed_pages"][0]["lost"] == ["two"], d
    # The 2 Oct Meshh case: a page the site has always had, sampled today for
    # the first time in a while, is not new.
    assert "r" not in d["new_pages"], "a page we already knew is not new"
    assert diff_corpus(old, dict(new, dead=["b"]))["gone_pages"] == ["b"], \
        "a known page that now answers 4xx is gone"
    assert diff_corpus(None, new)["new_pages"] == [], "first read is the baseline"
    assert diff_corpus(new, new)["changed_pages"] == [], "identical read is no change"

    cfg = load_config()
    ids = {c["id"] for c in cfg["competitors"]}
    assert not set(COMPETES) - ids, set(COMPETES) - ids

    # The packet must always carry its own instructions and the house rules -
    # a driver reads this file with no other context.
    pkt = build_inbox(days=0)
    for phrase in ("never invent a commercial figure", "no sales-coaching voice",
                   "never name an individual", "no signal"):
        assert phrase in pkt["how_to_use"], phrase
    assert set(pkt) >= {"items", "site_changes", "pricing", "already_analysed"}
    print("sitescan selftest OK")


def main(argv):
    ids = [a for a in argv if not a.startswith("-")] or None
    if "--selftest" in argv:
        return selftest()
    if "--inbox" in argv:
        pkt = build_inbox()
        print(f"inbox.json: {pkt['counts']['items']} items, "
              f"{pkt['counts']['sites_with_changes']} sites with changes, "
              f"{pkt['counts']['pricing_records']} pricing records")
        return
    limit = MAX_PAGES if "--weekly" in argv else DAILY_PAGES
    scan(ids, limit)
    pkt = build_inbox()
    print(f"\ninbox.json: {pkt['counts']['items']} items, "
          f"{pkt['counts']['sites_with_changes']} sites with changes")


if __name__ == "__main__":
    main(sys.argv[1:])
