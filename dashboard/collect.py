#!/usr/bin/env python3
"""WYA market monitor. Python stdlib only - no pip install, ever.

Watches three things and appends anything new to changes.jsonl:

  competitor  who we compete with          (competitors.json)
  client      our own clients and prospects (clients.json)
  sector      what is happening in the space (clients.json, sector_topics)

Four source types per target:

  news     Google News RSS search   (free, no key, covers press + articles)
  post     the target's own RSS/Atom feed
  sitemap  sitemap.xml, reports URLs that were not there last run
  page     pricing / careers / solutions pages, text-hashed and diffed
  apps     App Store listings by developer id, version bumps only

Relevance. A company target is kept when its name appears in the headline. A
sector topic has no name to match, so it declares `must` (every term required)
and/or `any` (at least one required) in its watch block - without that, a query
like "festival crowd safety technology" returns unrelated news, verified.

Deliberately does NOT touch LinkedIn. Their User Agreement prohibits automated
collection, hiQ lost on breach of contract, and LinkedIn sued Proxycurl in 2026.
Public Ad Library and job boards are the safe surfaces if you want that later.

Usage:
    python3 collect.py                  # everything
    python3 collect.py crowd-connected  # one target by id
    python3 collect.py --only=client    # one entity type
    python3 collect.py --selftest       # offline checks, no network
    python3 collect.py --check-sources  # HEAD every watched URL, report 404s
"""

import email.utils
import gzip
import hashlib
import json
import re
from html import unescape
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
CONFIG = HERE / "competitors.json"
CHANGES = HERE / "changes.jsonl"
STATE = HERE / ".collector_state.json"

# (file, key in that file, entity_type). Add a row to watch a new category.
SOURCES = [
    ("competitors.json", "competitors",   "competitor"),
    ("clients.json",     "clients",       "client"),
    ("clients.json",     "sector_topics", "sector"),
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25
NEWS = "https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en"

# Google News search matches loosely, so a hit must also name the competitor.
# Without this the feed drags in things like a "CAOS ETF" article for
# "Crowd Connected". ponytail: substring check, upgrade to fuzzy only if it misses.
MIN_TITLE_HITS = 1

# Google News returns by relevance, not date, and will happily surface a 2016
# article; competitor blog feeds carry their whole archive. Anything published
# before this window is dropped so the feed reads as current market activity.
# Company-history milestones (funding, acquisitions, patents) live in each
# competitor's snapshot with a source, so nothing load-bearing is lost.
# Sitemap and page-change entries are never age-filtered - they are current by
# definition. Raise this if you want a longer positioning-history view.
MAX_AGE_DAYS = 540

# Client news pulls in a lot that is technically about the client and useless to
# sales: local crime reports from festivals, football match reports from stadiums,
# and what's-on listings. Applied to CLIENT targets only - the crowd-safety sector
# topic legitimately covers incidents, so it must not be filtered this way.
CLIENT_NOISE = [
    # court and crime reporting
    "arrest", "jailed", "court", "magistrat", "sentenc", "convict", "guilty",
    "police", "drug", "drunk", "spat at", "urinat", "stabbing",
    "shoplift", "offences", "charged with", "pleaded", "bulge",
    # sport match reporting from stadium venues
    " vs ", " v ", "fifa", "match report", "line-up announced", "train at",
    "kick-off", "full-time", "penalt",
    # listings and consumer guides
    "what's on", "things to do", "guide to", "tickets & more", "line-up, tickets",
    "review:", "in pictures", "best moments", "set times",
]

# A single low-quality aggregator (seen: "Mshale") republishes old YouTube video
# titles for any venue name it can match, each ending in what looks like a video ID
# in parentheses - e.g. "...Erling Haaland (araxmfu3My)". 34 of 109 Johan Cruijff
# ArenA items on 17 Aug 2026 were this, none of them real venue news. The trailing
# token is the tell: a short parenthesised alphanumeric code with no vowela pattern
# a real headline would use.
VIDEO_ID_SUFFIX_RE = re.compile(r"\([A-Za-z0-9_-]{8,15}\)\s*$")

# Google News titles end with " - Publisher". Pull that out so the digest can show
# a clean headline and name the outlet separately.
PUB_RE = re.compile(r"^(?P<t>.+?)\s+-\s+(?P<p>[^-]{2,42})$")


def split_publisher(title):
    m = PUB_RE.match(title.strip())
    if not m:
        return title.strip(), ""
    return m.group("t").strip(), m.group("p").strip()


def simkey(title):
    """Normalised key so 10 outlets reporting one story collapse to one item."""
    t = re.sub(r"[^a-z0-9 ]", " ", title.lower())
    words = [w for w in t.split() if len(w) > 2][:7]
    return " ".join(sorted(words))


# Every fetch is counted. A run where most fetches fail must not look like a run
# where the market was quiet — that is how a broken collector serves stale data
# for weeks without anyone noticing.
FETCHES = {"ok": 0, "fail": 0}


def fetch_health(ok, fail):
    """(failure_rate, healthy). Healthy means we actually reached the internet."""
    total = ok + fail
    if not total:
        return 0.0, True
    rate = fail / total
    return rate, rate <= 0.5


def fetch(url):
    """GET a URL, return text. Returns None on any failure - never raises."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    })
    try:
        # ponytail: default SSL context. Set check_hostname=False only if a
        # competitor ships a broken cert and you decide you still want them.
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            FETCHES["ok"] += 1
            return raw.decode("utf-8", errors="replace")
    # Deliberately broad. The docstring above promises this never raises, and the
    # narrower tuple that used to be here did not keep that promise: a truncated
    # gzip response raises EOFError, which is NOT an OSError subclass, so it
    # escaped and killed the whole run. Seen live on purple.ai, whose sitemap is
    # 29MB. Other escapees in the same family: zlib.error (deflate),
    # http.client.IncompleteRead (an HTTPException), and anything a future
    # urllib or ssl change decides to raise. A fetch failure is ordinary and is
    # already accounted for by FETCHES/fetch_health - it must never be fatal.
    except Exception as e:
        FETCHES["fail"] += 1
        print(f"    ! fetch failed {url}: {type(e).__name__} {e}", file=sys.stderr)
        return None


# A hash says a page moved; it cannot say the price went from £8 to £9. Storing the
# extracted text lets the next run diff it. Capped because the state file is committed
# on every scheduled run — 86 watched pages at this cap is roughly 1.7MB.
PAGE_TEXT_CAP = 8000
DIFF_LINES_KEPT = 6
DIFF_LINE_CAP = 240

# A newly discovered page gets fetched once for a real headline (see page_headline).
# Capped per target per run so a site rebuild — Polestar published 103 new pages
# across five days in August 2026 — cannot multiply one run's fetch time by 100.
# Pages beyond the cap fall back to the URL slug, same as before this existed.
MAX_HEADLINE_FETCHES = 25
# Text is kept for every watched page: "what changed on their home page" is a fair
# question. Capped, because the state file is committed on every scheduled run.


# Sites that publish in many languages emit the same article once per locale, which
# floods the feed with translated duplicates of a story already recorded in English.
# Purple alone produced 160 "new pages" this way, most of them German or Bengali
# versions of the same post. Keep the English one; count the rest as reach.
# The region subtag is not always two lowercase letters: BCP-47 also allows a
# script ("zh-Hans", "pa-Guru") and an uppercase region ("en-GB"). Mapsted
# publishes under zh-Hans, zh-Hant and pa-Guru, and every one of them slipped
# past a lowercase-only pattern and was recorded as a new English page.
LOCALE_RE = re.compile(r"^/([a-z]{2})-([a-z]{2}|[A-Z]{2}|[A-Z][a-z]{3})(/|$)")
# A country-less locale prefix ("/fr/", not "/fr-fr/") slips past LOCALE_RE entirely.
# Found 2026-08-20: Polestar's site mirrors its whole structure under /fr/, and 35
# French duplicates of pages already counted in English were being recorded as new
# competitor activity. Deliberately a short, unambiguous list of language codes —
# "no", "in" and "uk" are excluded because they collide with real English words or
# path segments (Norway/Norwegian, "in", United Kingdom) too often to risk it.
# The second row was added 2026-09-22 for Mapsted, which mirrors its whole site
# under 25 languages. "id" (Indonesian) is the one with real collision risk - it
# is also how a site might path an identifier - so it was checked first: across
# the entire recorded state and every item ever collected, the only URLs starting
# /id/ are Mapsted translations. Re-check that before adding a shorter code.
BARE_LOCALE_RE = re.compile(
    r"^/(fr|de|es|it|pt|nl|zh|ja|ko|ru|pl|sv|fi|tr|ar|el|cs|hu|ro|bg|hr|sk|sl|et|lv|lt|da"
    r"|hi|mr|ta|te|gu|kn|pa|fa|he|vi|th|id)(/|$)")
ENGLISH = {"en"}

# A change to a cookie policy, a registered office or a VAT number is a lawyer
# doing their job, not a competitor doing something. Kept narrow on purpose: these
# phrases are unambiguous, and a broader list would start eating real product copy.
BOILERPLATE_RE = re.compile(
    r"registered (?:office|address|in england|in scotland)|company (?:number|no\.)|"
    r"vat (?:no|number|registration)|cookie(?: policy| preferences| settings)|"
    r"privacy (?:policy|notice)|terms (?:and|&) conditions|all rights reserved|"
    r"copyright \u00a9|\u00a9 \d{4}|accessibility statement", re.I)


# Sitemaps list image and document assets alongside pages. A screenshot filename is
# not a competitor doing something, and it reads as broken on a dashboard.
ASSET_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|avif|ico|mp4|webm|zip|css|js"
                      r"|woff2?|ttf|eot)(?:$|\?)", re.I)


def is_asset(url):
    return bool(ASSET_RE.search(url or ""))


def canon_locale(url):
    """(canonical_url, locale) for a locale-prefixed URL, else (url, None)."""
    m = re.match(r"^(https?://[^/]+)(/.*)$", url or "")
    if not m:
        return url, None
    host, path = m.group(1), m.group(2)
    lm = LOCALE_RE.match(path)
    if lm:
        locale = f"{lm.group(1)}-{lm.group(2)}"
    else:
        lm = BARE_LOCALE_RE.match(path)
        if not lm:
            return url, None
        locale = lm.group(1)
    rest = path[len(lm.group(0)) - (1 if lm.group(lm.lastindex) == "/" else 0):] or "/"
    if not rest.startswith("/"):
        rest = "/" + rest
    return host + rest, locale


def is_foreign_locale(url):
    """True when the URL is a non-English localisation of another page."""
    _, loc = canon_locale(url)
    return bool(loc) and loc.split("-")[0] not in ENGLISH


def block_text(html):
    """Extracted text with one line per block element.

    strip_tags() collapses a page to a single line, which makes a line-level diff
    meaningless — the whole page reads as one changed line. This keeps block
    boundaries so a changed price or product name lands on its own line.
    """
    h = re.sub(r"(?is)<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?s)<!--.*?-->", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|li|tr|td|th|h[1-6]|section|article|header|footer"
               r"|nav|ul|ol|table|dt|dd|blockquote|figcaption|option|label)>", "\n", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = unescape(h)
    out = []
    for ln in h.splitlines():
        ln = " ".join(ln.split())
        if ln:
            out.append(ln)
    return "\n".join(out)


def page_diff(before, after):
    """Human-readable added/removed lines between two page texts.

    Line-level, not word-level: enough to see a changed price or a new product name
    without pulling in a diff library. Returns ([added], [removed]).
    """
    import difflib
    def lines(t):
        out = []
        for ln in (t or "").splitlines():
            ln = " ".join(ln.split())
            if len(ln) > 3:
                out.append(ln[:DIFF_LINE_CAP])
        return out
    a, b = lines(before), lines(after)
    # The stored text is cut at PAGE_TEXT_CAP; the new one is whole. Everything past
    # the cut was therefore "added" on every change of a long page, and the cut-off
    # last line "removed": on 10 Oct Purple's partners page "gained" Saudi Arabia,
    # Senegal... (a country dropdown) and its industries and case-study pages their
    # whole lower halves, and three findings described static pages as news. Only
    # compare what the stored copy covers: its last line may be partial, and nothing
    # after it was ever seen.
    edge = len(a) - 1 if before and len(before) >= PAGE_TEXT_CAP else None
    added, removed = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if edge is not None and i2 >= edge:
            if i1 >= edge:
                continue                 # at or past the cut: never seen before
            keep = edge - i1
            i2, j2 = edge, min(j2, j1 + keep)
        if tag in ("insert", "replace"):
            added.extend(b[j1:j2])
        if tag in ("delete", "replace"):
            removed.extend(a[i1:i2])
    # a wholesale template change floods this; keep the head and say how many were cut
    return added[:DIFF_LINES_KEPT], removed[:DIFF_LINES_KEPT], len(added), len(removed)


def strip_tags(html):
    """Crude HTML to text. Enough for hashing and diffing, not for parsing."""
    html = re.sub(r"(?is)<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;?", " ", html)
    html = re.sub(r"&amp;?", "&", html)
    return re.sub(r"\s+", " ", html).strip()


TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
H1_RE = re.compile(r"(?is)<h1[^>]*>(.*?)</h1>")
# Drop a trailing " | Site Name" / " - Site Name": the site is already known from
# which competitor this is, and the page-specific half is the useful part.
TITLE_SUFFIX_RE = re.compile(r"\s*[|–—-]\s*[^|–—-]{2,40}$")


def page_headline(html):
    """Best-effort real headline for a freshly discovered page: <title>, then <h1>.

    A "new page" was previously named from its URL slug alone ("wifi directional
    antennas"), which is sometimes the real heading and sometimes just a path
    segment with no bearing on what the page says — a reader had to click through
    to find out. Fetching the page once, on first sight, replaces the guess with
    what the page itself says. Returns "" if neither tag is present.
    """
    m = TITLE_RE.search(html) or H1_RE.search(html)
    if not m:
        return ""
    text = unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(1)))
    text = " ".join(text.split())
    if not text:
        return ""
    stripped = TITLE_SUFFIX_RE.sub("", text).strip()
    return stripped if len(stripped) >= 8 else text


def parse_feed(xml_text):
    """RSS or Atom to [(title, link, date)]. Returns [] if unparseable."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as e:
        print(f"    ! feed parse failed: {e}", file=sys.stderr)
        return []
    out = []
    for item in root.findall(".//item"):          # RSS
        out.append((
            (item.findtext("title") or "").strip(),
            (item.findtext("link") or "").strip(),
            (item.findtext("pubDate") or "").strip(),
        ))
    ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.findall(f".//{ns}entry"):   # Atom
        link = entry.find(f"{ns}link")
        out.append((
            (entry.findtext(f"{ns}title") or "").strip(),
            (link.get("href") if link is not None else "") or "",
            (entry.findtext(f"{ns}updated") or "").strip(),
        ))
    return out


# A runaway guard, not a noise filter. purple.ai's sitemap is 29.6MB / 13,310 URLs
# and parses fine, so it must stay under this - skipping it would cost real
# coverage, and the locale filter below is what removes its noise. This exists only
# so a site that starts emitting hundreds of MB cannot exhaust the runner. It is
# NOT the fix for the truncated-gzip crash; fetch() is.
MAX_SITEMAP_CHARS = 80_000_000


# Apple's iTunes Search API. Free, keyless, JSON, and part of the standard library's
# reach - no new dependency. It is a real competitive surface the monitor was blind
# to: Crowd Connected shipped "Crowd Connected Events" v2.0 on 7 September 2026, a
# major-version rewrite (bundle id com.crowdconnected.kmp.events), while the monitor
# reported them quiet since 12 August because nothing on their website had moved.
#
# ALWAYS keyed on artistId, never a search term. This is the same entity-collision
# trap that has bitten this project four times: searching "polestar" returns Polestar
# Performance AB, the electric car maker, not Polestar indoor location; "pointr"
# returns Delta Cygni Labs' remote-support app. A developer id is unambiguous.
#
# `only` narrows within one developer, for publishers whose id covers far more than
# the product we track - Cisco's artistId is all of Cisco, Jabber and Webex included.
ITUNES_LOOKUP = ("https://itunes.apple.com/lookup?id={dev}&entity=software"
                 "&country=gb&limit=200")


def apple_apps(dev_id, only=None):
    """[{track_id, name, version, released, url}] for one developer, or [] on failure.

    Never raises: an App Store outage must not be able to stop a collection run.
    """
    body = fetch(ITUNES_LOOKUP.format(dev=dev_id))
    if not body:
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"    ! itunes lookup not JSON for {dev_id}: {e}", file=sys.stderr)
        return []
    terms = [t.lower() for t in (only or [])]
    out = []
    for r in data.get("results", []):
        # The first result of a developer lookup is the artist record, not an app.
        if r.get("wrapperType") != "software" or not r.get("trackId"):
            continue
        name = r.get("trackName") or ""
        if terms and not any(t in name.lower() for t in terms):
            continue
        out.append({
            "track_id": str(r["trackId"]),
            "name": name,
            "version": str(r.get("version") or ""),
            "released": (r.get("currentVersionReleaseDate") or "")[:10],
            "url": r.get("trackViewUrl") or "",
            "notes": (r.get("releaseNotes") or "")[:400],
        })
    return sorted(out, key=lambda a: a["name"])


# Google News links -> the publisher's real article URL.
#
# A Google News link never resolves with a plain GET; a browser lands on a consent
# wall, which this project will not click through (see render.py). But the plain
# GET that the collector already does returns the interstitial page, and that page
# carries a signature (data-n-a-sg / data-n-a-ts) that its own script sends to
# Google's batchexecute endpoint to learn where the article lives. Making that same
# request resolves the link without accepting anything. Measured 2026-09-25 on the
# 30 most recent items: 29 resolved, every one to the correct article. The publisher
# route (match the headline in the outlet's own RSS/sitemap) got 4, two of them wrong.
#
# ponytail: undocumented Google endpoint, so it can change without notice. Every
# failure returns None and the item keeps its Google link - it degrades back to
# "headline only", it never breaks a run.
GNEWS_RE = re.compile(r"^https?://news\.google\.com/(?:rss/)?articles/([^?/#]+)")
GNEWS_RPC = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
# Google allows a burst of roughly 136 resolutions and then refuses for a while
# (measured 2026-09-25: 99 of the first 100, then nothing). A normal day brings
# 20-130 new news items, so pace them and stop well inside the burst. An item
# over the budget keeps its Google link, which is exactly the old behaviour.
GNEWS_BUDGET = {"left": 100, "misses": 0, "throttled": False}
# Stop on a real 429. The miss counter is only a backstop: it used to be the
# throttle signal, and five unresolvable links in a row (a Reuters picture page,
# say) stopped the 27 September catch-up with 34 items left and budget to spare.
GNEWS_MAX_MISSES = 25
GNEWS_PAUSE = 1.0


def resolve_gnews(url):
    """Publisher URL behind a Google News link, or None."""
    m = GNEWS_RE.match(url or "")
    if not m:
        return None
    aid = m.group(1)
    # Own GET rather than fetch(): a 429 must be told apart from "this one link
    # has no publisher behind it", and fetch() turns both into None.
    try:
        with urllib.request.urlopen(
                urllib.request.Request(f"https://news.google.com/articles/{aid}",
                                       headers={"User-Agent": UA}),
                timeout=TIMEOUT, context=ssl.create_default_context()) as r:
            page = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            GNEWS_BUDGET["throttled"] = True
        return None
    except Exception:
        return None
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts):
        return None
    inner = json.dumps(["garturlreq",
                        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                          None, None, None, None, None, 0, 1],
                         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                        aid, int(ts.group(1)), sg.group(1)])
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner]]]))
    req = urllib.request.Request(GNEWS_RPC, data=body.encode(), method="POST", headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            out = r.read().decode("utf-8", errors="replace")
    except Exception as e:          # same breadth as fetch(): never stop a run
        if getattr(e, "code", None) == 429:
            GNEWS_BUDGET["throttled"] = True
        print(f"    ! gnews resolve failed: {type(e).__name__} {e}", file=sys.stderr)
        return None
    hit = re.search(r'\[\\"garturlres\\",\\"(https?://[^\\"]+)\\"', out)
    if not hit or "news.google.com" in hit.group(1):
        return None
    return hit.group(1)


def gnews_budgeted(url):
    """resolve_gnews() within the run's budget; stops for the run once throttled."""
    b = GNEWS_BUDGET
    if b["left"] <= 0 or b["throttled"] or b["misses"] >= GNEWS_MAX_MISSES:
        return None
    b["left"] -= 1
    time.sleep(GNEWS_PAUSE)
    real = resolve_gnews(url)
    b["misses"] = 0 if real else b["misses"] + 1
    return real


def gnews_catch_up(days=7, path=None):
    """Spend what is left of the budget on recent items still on a Google link.

    New items are resolved as they are collected, but anything collected while
    Google was throttling, or before this existed, keeps its Google link and shows
    "headline only" on the dashboard for as long as it is on screen. Newest first,
    because the newest are the ones people read. The row key never changes, so this
    cannot make an item be collected twice. Returns how many rows it resolved.
    """
    target = Path(path) if path else CHANGES
    if not target.exists():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    lines = target.read_text().splitlines()
    rows = [json.loads(l) if l.strip() else None for l in lines]
    todo = sorted((i for i, r in enumerate(rows)
                   if r and r.get("kind") == "news" and not r.get("gnews_url")
                   and not r.get("gnews_unresolvable")
                   and GNEWS_RE.match(r.get("url") or "")
                   and (r.get("seen_at") or "") >= cutoff),
                  key=lambda i: rows[i].get("seen_at") or "", reverse=True)
    done = marked = 0
    for i in todo:
        real = gnews_budgeted(rows[i]["url"])
        if real is None and (GNEWS_BUDGET["left"] <= 0 or GNEWS_BUDGET["throttled"]
                             or GNEWS_BUDGET["misses"] >= GNEWS_MAX_MISSES):
            break
        if real:
            rows[i]["gnews_url"], rows[i]["url"] = rows[i]["url"], real
            done += 1
        else:
            # Asked while not throttled and Google has no publisher for it: do not
            # spend budget on it again tomorrow.
            rows[i]["gnews_unresolvable"] = True
            marked += 1
    if done or marked:
        target.write_text("\n".join(json.dumps(r, ensure_ascii=False) if r else ""
                                     for r in rows) + "\n")
    print(f"google news: {done} older link(s) resolved, "
          f"{len(todo) - done} still on a Google link")
    return done


def sitemap_urls(xml_text, depth=0):
    """All <loc> values. Follows one level of sitemap index."""
    if len(xml_text) > MAX_SITEMAP_CHARS:
        print(f"    ! sitemap is {len(xml_text)/1e6:.1f}MB, over the "
              f"{MAX_SITEMAP_CHARS/1e6:.0f}MB cap - skipped", file=sys.stderr)
        return []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as e:
        print(f"    ! sitemap parse failed: {e}", file=sys.stderr)
        return []
    locs = [e.text.strip() for e in root.iter() if
            e.tag.endswith("loc") and e.text]
    if depth == 0 and root.tag.endswith("sitemapindex"):
        nested = []
        for sm in locs[:10]:   # ponytail: cap at 10 child sitemaps
            body = fetch(sm)
            if body:
                nested += sitemap_urls(body, depth + 1)
        if len(locs) > 10:
            print(f"    (capped: {len(locs)} child sitemaps, read first 10)")
        return nested
    return locs


def load(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        print(f"    ! {path.name} unreadable, starting fresh", file=sys.stderr)
        return default


def parse_date(s):
    """Feed date string to ISO 8601, or None. Handles RFC 2822 and ISO."""
    if not s:
        return None
    try:                                    # RFC 2822: Fri, 19 Jun 2026 08:00:00 +0000
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:                                # Atom: 2026-06-19T08:00:00Z
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def matches(title, name, watch=None):
    """Is this headline actually about the target?

    Explicit `must` / `any` filters win when present - a sector topic has no
    company name to match on. Otherwise fall back to naming the target.
    """
    t = title.lower()
    watch = watch or {}
    must = [m.lower() for m in watch.get("must", [])]
    anyof = [a.lower() for a in watch.get("any", [])]
    none = [n.lower() for n in watch.get("none", [])]
    # exclusions apply to name-matched targets too: "Jockey Club" pulls in the
    # Hong Kong Jockey Club and a US race of the same name.
    if none and any(n in t for n in none):
        return False
    if must or anyof:
        if must and not all(m in t for m in must):
            return False
        if anyof and not any(a in t for a in anyof):
            return False
        return True
    core = re.sub(r"\s*\(.*?\)\s*", "", name).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", core) if len(w) > 2]
    if not words:
        return True
    return sum(1 for w in words if w in t) >= min(MIN_TITLE_HITS, len(words))


def targets(only=None, kind=None):
    """Yield (entity_type, entry) across every configured source file."""
    for fname, key, etype in SOURCES:
        path = HERE / fname
        if not path.exists():
            print(f"  (skipping {fname}: not present)")
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"  ! {fname} is not valid JSON: {e}", file=sys.stderr)
            continue
        for entry in data.get(key, []):
            if kind and etype != kind:
                continue
            if only and entry.get("id") != only:
                continue
            yield etype, entry


def prune(days=MAX_AGE_DAYS, path=None):
    """Drop items older than the window from changes.jsonl.

    Collection alone never removes anything - the file is append-only, so it
    grows for as long as the monitor runs. This makes "older data goes away"
    true at the storage layer too, not just in what the dashboard displays.

    Undated rows are always kept: an item we could not date is not evidence of
    an old item.
    """
    target = Path(path) if path else CHANGES
    if not target.exists():
        print("nothing to prune")
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .isoformat(timespec="seconds")
    kept, dropped = [], 0
    for line in target.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        when = r.get("published")
        if when and when < cutoff:
            dropped += 1
            continue
        kept.append(line)
    target.write_text("\n".join(kept) + ("\n" if kept else ""))
    print(f"pruned {dropped} items older than {days} days; {len(kept)} kept")
    return dropped


def collect(only=None, kind=None):
    state = load(STATE, {})
    seen, simseen = set(), set()
    if CHANGES.exists():
        for line in CHANGES.read_text().splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    seen.add(r["key"])
                    simseen.add(f"{r['competitor_id']}|{simkey(r['title'])}")
                except (json.JSONDecodeError, KeyError):
                    pass

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    cutoff = (now_dt - timedelta(days=MAX_AGE_DAYS)).isoformat(timespec="seconds")
    new = []
    flushed = 0         # how much of `new` is already on disk - see the loop's finally
    langs = {}          # competitor_id -> {locales seen}, from dropped translations

    for etype, comp in targets(only, kind):
        # One target's fault costs that target, not the run. changes.jsonl used to
        # be written once, after every target had been walked, so an exception on
        # target 20 of 33 discarded everything targets 1-19 had collected. The
        # fetch() fix removes the known crash, but 'the collector must not lose a
        # day's work to one bad site' is the property worth holding directly.
        try:
            watch = comp.get("watch") or {}
            if not any([watch.get("news_query"), watch.get("extra_queries"), watch.get("feeds"),
                        watch.get("sitemap"), watch.get("pages")]):
                continue

            print(f"\n[{etype}] {comp['name']}")
            cid = comp["id"]
            st = state.setdefault(cid, {})

            def record(itemkind, title, url, extra=None, _cid=cid, _comp=comp,
                       _etype=etype):
                clean, publisher = split_publisher(title)
                if not clean:          # some feeds emit a titleless item
                    return
                key = hashlib.sha256(
                    f"{_cid}|{itemkind}|{url}|{title}".encode()).hexdigest()[:20]
                if key in seen:
                    return
                if is_asset(url):
                    return
                if is_foreign_locale(url):
                    _, loc = canon_locale(url)
                    langs.setdefault(_cid, set()).add(loc)
                    return
                # collapse the same story reported by many outlets
                sk = f"{_cid}|{simkey(clean)}"
                if sk in simseen:
                    return
                seen.add(key)
                simseen.add(sk)
                row = {"key": key, "seen_at": now, "entity_type": _etype,
                       "competitor_id": _cid, "competitor": _comp["name"],
                       "priority": _comp.get("priority") or _comp.get("threat") or "",
                       "kind": itemkind, "title": clean, "publisher": publisher,
                       "url": url}
                row.update(extra or {})
                # Only reached for a genuinely new item, and the key above is already
                # hashed from the Google link, so swapping the stored url cannot make
                # tomorrow's run re-record the same story.
                if itemkind == "news" and GNEWS_RE.match(url):
                    real = gnews_budgeted(url)
                    if real:
                        row["url"], row["gnews_url"] = real, url
                new.append(row)
                print(f"    NEW [{itemkind}] {clean[:84]}")

            # 1. news - named in the headline, and recent. `extra_queries` runs the same
            # Google News pipeline with a different query string - typically a
            # site:-restricted one reaching review sites (G2, Capterra) and Reddit that
            # block direct RSS access but that Google still indexes. Same matches()
            # call, same watch dict, so must/any/none still gates these results exactly
            # as it gates the primary query - no separate collision-risk surface.
            # X/Twitter and job-board site: queries were tried and dropped: Google barely
            # indexes either, so a real query returned noise or nothing (measured
            # 2026-08-26), not a source worth carrying forward.
            for label, query in ([("news", watch["news_query"])] if watch.get("news_query") else []) + \
                                 [("news", q) for q in (watch.get("extra_queries") or [])]:
                body = fetch(NEWS.format(q=urllib.parse.quote(query)))
                if body:
                    items = parse_feed(body)
                    # Match the headline WITHOUT its " - Publisher" suffix. With it,
                    # every Isle of Wight County Press story (roadworks, book awards,
                    # sweetcorn) passed the Isle of Wight Festival filter.
                    named = [i for i in items if matches(split_publisher(i[0])[0],
                                                         comp["name"], watch)]
                    noise = CLIENT_NOISE if etype == "client" else []
                    kept, old, junk = [], 0, 0
                    for title, link, date in named:
                        if any(n in title.lower() for n in noise):
                            junk += 1
                            continue
                        # Test the CLEAN title: a Google News title ends " - Publisher",
                        # so the video-ID suffix is only final once that is stripped.
                        # Checking the raw title silently matched nothing.
                        if etype == "client" and VIDEO_ID_SUFFIX_RE.search(
                                split_publisher(title)[0]):
                            junk += 1
                            continue
                        iso = parse_date(date)
                        if iso and iso < cutoff:
                            old += 1
                            continue
                        kept.append((title, link, iso))
                    print(f"  {label} ({query[:40]}): {len(items)} results, {len(named)} named, "
                          f"{junk} noise, {old} too old, {len(kept)} kept")
                    for title, link, iso in kept:
                        record(label, title, link, {"published": iso})

            # 2. own feeds - archives run years back, so same recency window
            for feed in watch.get("feeds") or []:
                body = fetch(feed)
                if body:
                    items = parse_feed(body)
                    kept, old = [], 0
                    for title, link, date in items:
                        iso = parse_date(date)
                        if iso and iso < cutoff:
                            old += 1
                            continue
                        kept.append((title, link, iso))
                    print(f"  feed: {len(items)} items, {old} too old, "
                          f"{len(kept)} kept from {feed}")
                    for title, link, iso in kept:
                        record("post", title, link, {"published": iso})

            # 3. sitemap - new URLs only
            if watch.get("sitemap"):
                body = fetch(watch["sitemap"])
                if body:
                    # Filter locales HERE, before the diff, not at record() time.
                    # record() already drops translated duplicates, but only after
                    # they have been counted as "new" and, worse, after each one has
                    # been fetched for a headline. Purple listed ~78 new URLs a day
                    # this way, almost all /de-de/, /bn-in/, /es-es/ copies of a post
                    # already recorded in English. Filtering first also shrinks the
                    # stored URL list, which is committed daily: 1.16MB -> ~130KB.
                    # Collapse EVERY locale prefix to its canonical path, English
                    # ones included. Dropping only non-English locales was not
                    # enough: on 2026-09-03 Purple re-pathed its whole site under
                    # /en-gb/ and /en-us/, and because both are English, 165 URLs
                    # landed as "new pages" in one week - the same WiFi guides that
                    # already existed at the bare path, counted twice over. That one
                    # site migration produced 166 of the week's items and buried
                    # every other competitor. A page is the same page whichever
                    # locale serves it, so the canonical form is what gets compared
                    # and stored; the first locale seen wins as the URL we record.
                    raw_urls = sitemap_urls(body)
                    urls, canon_seen = set(), {}
                    for u in raw_urls:
                        if is_foreign_locale(u):
                            continue
                        c, _ = canon_locale(u)
                        if c in canon_seen:
                            continue
                        canon_seen[c] = u
                        urls.add(c)
                    if len(raw_urls) != len(urls):
                        print(f"    ({len(raw_urls) - len(urls)} locale/duplicate "
                              f"URLs collapsed, {len(urls)} canonical kept)")
                    known = set(st.get("sitemap_urls", []))
                    if not urls:
                        # A sitemap URL that returns HTML (a soft-404, or a site that
                        # serves index for every path) parses to zero URLs. Recording
                        # that as a baseline makes the watch a permanent no-op that
                        # looks healthy: known stays empty, so every future run also
                        # takes the "baseline" branch and nothing is ever compared.
                        # Count it as a fetch failure so it shows up in the health
                        # check instead of passing silently.
                        FETCHES["fail"] += 1
                        print(f"  ! sitemap parsed 0 URLs (not XML?): {watch['sitemap']}")
                    elif not known:
                        print(f"  sitemap: {len(urls)} URLs, baseline recorded")
                    else:
                        # `known` may still hold pre-canonical URLs from before
                        # this collapse existed. Canonicalise both sides or every
                        # stored /en-gb/x would read as new the first time round.
                        known = {canon_locale(k)[0] for k in known}
                        added = sorted(urls - known)
                        print(f"  sitemap: {len(urls)} URLs, {len(added)} new")
                        fetched = 0
                        for u in [canon_seen.get(a, a) for a in added]:
                            slug = u.rstrip("/").rsplit("/", 1)[-1] or u
                            headline = ""
                            if not is_asset(u):
                                if fetched < MAX_HEADLINE_FETCHES:
                                    body = fetch(u)
                                    fetched += 1
                                    if body:
                                        headline = page_headline(body)
                                elif fetched == MAX_HEADLINE_FETCHES:
                                    print(f"    ! headline fetch capped at "
                                          f"{MAX_HEADLINE_FETCHES} for this run; "
                                          f"remaining new pages use the URL slug")
                                    fetched += 1   # only print the notice once
                            record("new_page", headline or slug.replace("-", " "), u,
                                   {"headline_source": "fetched" if headline else "slug"})
                    st["sitemap_urls"] = sorted(urls)

            # 4. watched pages - text hash diff
            #
            # Two kinds of page change are not news, and both were reaching the feed.
            #
            # A shared header or footer edit diffs identically on every page that
            # carries it, so one nav tweak filed six rows for the same company on
            # the same day. Measured 2026-09-22: 23 such clusters in the corpus, up
            # to 6 rows each - the Activity tab repeating itself, and a reader
            # counting "movements" counting one edit six times.
            #
            # And a legal boilerplate change - a registered office address, a cookie
            # policy, a VAT number - is a competitor doing nothing. One of them led
            # the front page with Purple's company registration number.
            #
            # Both are dropped here, at the point of record, so nothing downstream
            # has to know about either.
            seen_diffs = set()
            for page in watch.get("pages") or []:
                body = fetch(page["url"])
                if not body:
                    continue
                text = strip_tags(body)
                h = hashlib.sha256(text.encode()).hexdigest()[:16]
                hashes = st.setdefault("page_hashes", {})
                texts = st.setdefault("page_texts", {})
                prev = hashes.get(page["url"])
                if prev is None:
                    print(f"  page: {page['label']} baseline {h} ({len(text)} chars)")
                elif prev != h:
                    nowtext = block_text(body)
                    added, removed, nadd, nrem = page_diff(texts.get(page["url"]), nowtext)
                    if not added and not removed and texts.get(page["url"]):
                        # The change lies past the stored cut: we cannot say what it
                        # is, so it is not reported as one.
                        print(f"  page: {page['label']} changed beyond the stored "
                              f"{PAGE_TEXT_CAP} chars - not recorded")
                        hashes[page["url"]] = h
                        texts[page["url"]] = nowtext[:PAGE_TEXT_CAP]
                        continue
                    now_lines = [ln for ln in nowtext.splitlines()
                                 if 25 < len(ln) < DIFF_LINE_CAP][:DIFF_LINES_KEPT]
                    summary = "; ".join(added[:2])[:200] if added else ""
                    title = (f"{page['label']} page changed — {summary}" if summary
                             else f"{page['label']} page changed")
                    print(f"  page: {page['label']} CHANGED {prev} -> {h} "
                          f"(+{nadd}/-{nrem} lines)")
                    fingerprint = tuple(added[:3])
                    if added and fingerprint in seen_diffs:
                        print(f"    same diff already filed for this company today "
                              f"- shared nav or footer, not {page['label']}")
                        hashes[page["url"]] = h
                        texts[page["url"]] = block_text(body)[:PAGE_TEXT_CAP]
                        continue
                    if added and all(BOILERPLATE_RE.search(a) for a in added):
                        print(f"    boilerplate only ({added[0][:60]}) - not recorded")
                        hashes[page["url"]] = h
                        texts[page["url"]] = block_text(body)[:PAGE_TEXT_CAP]
                        continue
                    seen_diffs.add(fingerprint)
                    record("page_change", title, page["url"],
                           {"prev_hash": prev, "hash": h, "chars": len(text),
                            "added": added, "removed": removed,
                            "added_total": nadd, "removed_total": nrem,
                            "now_lines": now_lines,
                            "had_baseline": bool(texts.get(page["url"]))})
                else:
                    print(f"  page: {page['label']} unchanged")
                hashes[page["url"]] = h
                texts[page["url"]] = block_text(body)[:PAGE_TEXT_CAP]

            # 5. published apps - version bumps and new releases
            #
            # A competitor's product ships in the App Store before, and sometimes
            # instead of, anything appearing on their website. Crowd Connected's
            # v2.0 rewrite landed on 7 September with no web announcement at all.
            # Only the version and release date are compared; nothing here fetches
            # a page, so it costs one request per developer per run.
            if watch.get("apps"):
                cfg = watch["apps"]
                apps = apple_apps(cfg.get("dev"), cfg.get("only"))
                if not apps:
                    print(f"  apps: no listings returned for dev "
                          f"{cfg.get('dev')} (filter: {cfg.get('only')})")
                else:
                    known_apps = st.setdefault("apps", {})
                    fresh = 0
                    for app in apps:
                        prev = known_apps.get(app["track_id"])
                        if prev is None:
                            # First sight of an app is a baseline, not news - the
                            # same rule every other watch here follows. Without it,
                            # turning this on would file every existing app as a
                            # release on day one.
                            pass
                        elif prev.get("version") != app["version"]:
                            fresh += 1
                            title = (f"{app['name']} {prev.get('version','?')} "
                                     f"\u2192 {app['version']}")
                            # An inferred surface must say so on the item itself. The
                            # caveat used to live only in the watch config, so the
                            # analyser saw a Clarifi Media release filed under
                            # Festyvent and reported "Festyvent shipped" as fact.
                            if cfg.get("note"):
                                title += (" (published by a developer only inferred "
                                          "to be linked to this company; unconfirmed)")
                            record("app_release", title, app["url"], {
                                "app": app["name"], "version": app["version"],
                                "prev_version": prev.get("version"),
                                "published": (app["released"] + "T12:00:00+00:00")
                                             if app["released"] else None,
                                "release_notes": app["notes"]})
                        known_apps[app["track_id"]] = {
                            "name": app["name"], "version": app["version"],
                            "released": app["released"]}
                    print(f"  apps: {len(apps)} tracked, {fresh} new release"
                          f"{'' if fresh == 1 else 's'}")

        except Exception as e:
            FETCHES["fail"] += 1
            print(f"  ! {comp['id']} failed, skipping it: {type(e).__name__} {e}",
                  file=sys.stderr)
        finally:
            # Flush what this target produced before moving to the next one.
            if len(new) > flushed:
                batch = new[flushed:]
                batch.sort(key=lambda r: r.get("published") or r["seen_at"],
                           reverse=True)
                new[flushed:] = batch
                with CHANGES.open("a") as f:
                    for row in batch:
                        f.write(json.dumps(row) + "\n")
                flushed = len(new)

    # Nothing to write here any more: each target flushed its own rows as it
    # finished (see the loop's `finally`). This assertion is the guard that keeps
    # that true if someone later appends to `new` outside the loop.
    assert flushed == len(new), "rows were added to `new` outside the per-target flush"
    STATE.write_text(json.dumps(state, indent=1))
    dated = [r for r in new if r.get("published")]
    print(f"\n{len(new)} new entries -> {CHANGES.name}")
    for et in ("competitor", "client", "sector"):
        n = sum(1 for r in new if r.get("entity_type") == et)
        if n:
            print(f"  {et:11} {n}")
    if dated:
        print(f"newest: {max(r['published'] for r in dated)[:10]}   "
              f"oldest: {min(r['published'] for r in dated)[:10]}")

    if langs:
        print("\ntranslated duplicates dropped (kept the English page):")
        for cid, ls in sorted(langs.items()):
            print(f"  {cid:22} {len(ls)} non-English locales: "
                  f"{', '.join(sorted(ls)[:6])}{' …' if len(ls) > 6 else ''}")

    rate, healthy = fetch_health(FETCHES["ok"], FETCHES["fail"])
    print(f"fetches: {FETCHES['ok']} ok, {FETCHES['fail']} failed "
          f"({rate:.0%} failure rate)")
    if not healthy:
        print(
            f"\nCOLLECTION FAILED: {rate:.0%} of fetches failed, so '{len(new)} new "
            f"entries' means the collector could not reach the internet, not that the "
            f"market was quiet. Do not publish this run. Common causes: no CA "
            f"certificate bundle for this Python (macOS: run 'Install "
            f"Certificates.command'), no network, or an outbound proxy.",
            file=sys.stderr)
        raise SystemExit(2)
    return new


def check_sources():
    """HEAD every configured URL and report what does not resolve.

    The offline self-test catches dead *state* - a watch whose stored data proves
    it can never fire. It cannot catch a watch pointing at a URL that 404s, because
    it makes no network call by design. That was the last silent-failure surface:
    a 404 page watch passes every check and reports "unchanged" forever.

    Networked, so it is opt-in and not part of the workflow's pre-flight.
    """
    seen, bad = set(), []
    for etype, comp in targets():
        watch = comp.get("watch") or {}
        urls = []
        if watch.get("sitemap"):
            urls.append(("sitemap", watch["sitemap"]))
        for f in watch.get("feeds") or []:
            urls.append(("feed", f))
        for pg in watch.get("pages") or []:
            urls.append((f"page:{pg.get('label', '?')}", pg["url"]))
        if not urls:
            continue
        print(f"\n[{etype}] {comp['name']}")
        for label, url in urls:
            if url in seen:
                print(f"  = {label:22} duplicate watch: {url}")
                bad.append((comp["id"], label, url, "duplicate"))
                continue
            seen.add(url)
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(
                        req, timeout=TIMEOUT,
                        context=ssl.create_default_context()) as r:
                    code, final = r.status, r.geturl()
            except urllib.error.HTTPError as e:
                # Some hosts reject HEAD but serve GET fine (purple.ai answers 503
                # to HEAD on a page the collector reads successfully every day).
                # Confirm with a real GET before calling a URL dead - a check that
                # cries wolf gets ignored, which is worse than not having it.
                code, final = e.code, url
                if code >= 400:
                    body = fetch(url)
                    if body:
                        code, final = 200, url
                        label += " (HEAD refused, GET ok)"
            except Exception as e:                  # never let one bad host stop the sweep
                print(f"  ! {label:22} {type(e).__name__}: {e}  {url}")
                bad.append((comp["id"], label, url, type(e).__name__))
                continue
            note = ""
            if final.rstrip("/") != url.rstrip("/"):
                note = f"  -> {final}"
            if code >= 400:
                print(f"  ! {label:22} HTTP {code}  {url}")
                bad.append((comp["id"], label, url, f"HTTP {code}"))
            else:
                print(f"  ok {label:22} {code}{note}")
    print(f"\n{len(seen)} URLs checked, {len(bad)} need attention")
    for cid, label, url, why in bad:
        print(f"  {cid:22} {label:22} {why:18} {url}")
    return bad


def selftest():
    """Offline checks. No network. Fails loudly if the parsers break."""
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Crowd Connected wins award</title>
        <link>https://x.test/a</link><pubDate>Mon, 01 Jan 2026</pubDate></item>
      <item><title>Unrelated ETF news</title><link>https://x.test/b</link></item>
    </channel></rss>"""
    items = parse_feed(rss)
    assert len(items) == 2, items
    assert items[0][0] == "Crowd Connected wins award"

    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Post one</title><link href="https://y.test/1"/>
      <updated>2026-01-01</updated></entry></feed>"""
    a = parse_feed(atom)
    assert a == [("Post one", "https://y.test/1", "2026-01-01")], a

    assert parse_feed("not xml at all") == []
    assert sitemap_urls("<broken") == []

    sm = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://a.test/1</loc></url><url><loc>https://a.test/2</loc></url>
    </urlset>"""
    assert sitemap_urls(sm) == ["https://a.test/1", "https://a.test/2"]

    assert fetch_health(0, 0) == (0.0, True)
    assert fetch_health(10, 0)[1] is True
    assert fetch_health(0, 173)[1] is False, "a total outage must not look healthy"
    assert fetch_health(6, 4)[1] is True and fetch_health(4, 6)[1] is False

    assert matches("Crowd Connected wins award", "Crowd Connected")
    assert not matches("CAOS ETF: An Imperfect Hedging Tool", "Crowd Connected")
    assert matches("Meshh expands", "Meshh (a Limelight Platform)")

    # sector topics match on must/any, not on a company name
    mw = {"must": ["crowd"], "any": ["crush", "safety"]}
    assert matches("Crowd safety review at arena", "topic", mw)
    assert not matches("Crowd Connected wins award", "topic", mw)   # no any-term
    assert not matches("Stadium safety upgrade", "topic", mw)       # no must-term
    assert matches("Martyn's Law deadline nears", "topic", {"any": ["martyn"]})
    # `none` excludes, and applies to name-matched targets too
    jc = {"must": ["jockey club"], "none": ["hong kong", "gold cup"]}
    assert matches("Jockey Club invests in Aintree", "The Jockey Club", jc)
    assert not matches("Hong Kong Jockey Club reports handle", "The Jockey Club", jc)
    # A target named only by the publisher is not named (2 Oct: IoW roadworks).
    _gn = "Island Roads explains why bridge works were postponed - Isle of Wight County Press"
    assert matches(_gn, "IoW", {"must": ["isle of wight"]}), "precondition: raw title matches"
    assert not matches(split_publisher(_gn)[0], "IoW", {"must": ["isle of wight"]})
    assert not matches("Forever Young to run in Jockey Club Gold Cup", "The Jockey Club", jc)
    assert not matches("Hong Kong Jockey Club news", "The Jockey Club", {"none": ["hong kong"]})

    assert split_publisher("Big news happened - The Guardian") == \
        ("Big news happened", "The Guardian")
    assert split_publisher("No publisher here") == ("No publisher here", "")
    # a hyphenated headline must not be mistaken for a publisher suffix
    t, p = split_publisher("Well-known firm wins deal - Racing Post")
    assert (t, p) == ("Well-known firm wins deal", "Racing Post"), (t, p)

    # the same story from two outlets collapses to one key
    assert simkey("Informa lifts FY guidance as H1 growth strengthens") == \
           simkey("Informa lifts FY guidance as H1 growth strengthens!")
    assert simkey("Jockey Club invests in Aintree") != \
           simkey("Jockey Club sells Kempton Park")

    # client noise terms must not filter the crowd-safety sector topic
    assert any("police" in n for n in CLIENT_NOISE)
    assert VIDEO_ID_SUFFIX_RE.search("DYEN @ Verknipt ArenA (araxmfu3My)")
    assert not VIDEO_ID_SUFFIX_RE.search("Jockey Club quits Racecourse Association")
    # the real-world shape: video id is only final after the publisher is split off
    _raw = "Social Capital - Johan Cruijff Arena Celtic Fc (DKgl6B9xHI) - Mshale"
    assert not VIDEO_ID_SUFFIX_RE.search(_raw), "raw title must not match"
    assert VIDEO_ID_SUFFIX_RE.search(split_publisher(_raw)[0]), \
        "clean title must match, or the filter never fires during collection"
    for et, e in targets(kind="sector"):
        if e["id"] == "crowd-safety-incidents":
            break
    else:
        raise AssertionError("crowd-safety-incidents topic missing")
    # must alone, and any alone, both work
    assert matches("festival cancelled", "topic", {"must": ["festival", "cancel"]})
    assert not matches("festival announced", "topic", {"must": ["festival", "cancel"]})

    assert parse_date("Fri, 19 Jun 2026 08:00:00 +0000") == "2026-06-19T08:00:00+00:00"
    assert parse_date("Tue, 21 Jul 2026 15:43:59 GMT") == "2026-07-21T15:43:59+00:00"
    assert parse_date("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00+00:00"
    assert parse_date("") is None and parse_date("garbage") is None
    # missing timezone must be assumed UTC, not crash
    assert parse_date("Mon, 01 Jan 2018 00:00:00") == "2018-01-01T00:00:00+00:00"
    # the age filter compares ISO strings directly, so ordering must hold as text
    assert parse_date("Mon, 01 Jan 2018 00:00:00") < parse_date("Fri, 19 Jun 2026 08:00:00 +0000")
    # an undated item returns None and is therefore KEPT, never silently dropped
    assert parse_date("no date here") is None

    assert is_asset("https://x.test/a/Screenshot%202026.jpeg") is True
    assert is_asset("https://x.test/logo.svg?v=2") is True
    assert is_asset("https://x.test/pricing") is False
    # a page whose path merely contains a format word is not an asset
    assert is_asset("https://x.test/blog/png-vs-jpeg-explained") is False

    assert canon_locale("https://x.test/de-de/blogs/a") == ("https://x.test/blogs/a", "de-de")
    assert canon_locale("https://x.test/en-gb/blogs/a") == ("https://x.test/blogs/a", "en-gb")
    assert canon_locale("https://x.test/blogs/a") == ("https://x.test/blogs/a", None)
    assert is_foreign_locale("https://x.test/de-de/blogs/a") is True
    assert is_foreign_locale("https://x.test/en-gb/blogs/a") is False
    assert is_foreign_locale("https://x.test/blogs/a") is False
    # a two-letter path segment that is not a locale must not be mistaken for one
    assert canon_locale("https://x.test/uk/pricing")[1] is None

    # a country-less locale prefix ("/fr/", found on Polestar's site 2026-08-20)
    assert canon_locale("https://x.test/fr/marches/lieux-publics/") == \
        ("https://x.test/marches/lieux-publics/", "fr")
    assert is_foreign_locale("https://x.test/fr/marches/lieux-publics/") is True
    assert is_foreign_locale("https://x.test/industries/public-venues/") is False

    # Script and non-Latin language subtags. Mapsted mirrors its whole site under 25
    # languages; a lowercase-only region pattern and a European-only bare list let
    # every one of them through as a "new page". Checked against the whole recorded
    # corpus before "id" was added - the only /id/ URLs anywhere are translations.
    assert canon_locale("https://x.test/zh-Hans/indoor-maps") == \
        ("https://x.test/indoor-maps", "zh-Hans")
    assert canon_locale("https://x.test/pa-Guru/indoor-maps")[1] == "pa-Guru"
    assert is_foreign_locale("https://x.test/zh-Hant/indoor-maps") is True
    for code in ("hi", "mr", "ta", "te", "gu", "kn", "pa", "fa", "he", "vi", "th", "id"):
        assert is_foreign_locale(f"https://x.test/{code}/indoor-maps") is True, code
    assert is_foreign_locale("https://x.test/en-GB/pricing") is False

    # Legal boilerplate is not competitor movement; product copy is.
    for junk in ("So Purple Group Ltd is a limited company registered in England and Wales",
                 "Company number 08187142", "Cookie Policy", "Terms and Conditions",
                 "VAT number GB123456789", "All rights reserved"):
        assert BOILERPLATE_RE.search(junk), junk
    for real in ("Guest WiFi software for venues and enterprise",
                 "Now with 1-5 metre accuracy", "New: Mapsted Flow for stadiums",
                 "Our terms of engagement with venue operators have changed"):
        assert not BOILERPLATE_RE.search(real), real

    # English locales are duplicates of each other and of the bare path. Purple
    # re-pathed its site under /en-gb/ and /en-us/ and produced 165 "new pages" in
    # one week, all of them guides that already existed. All three collapse to one.
    # Fifth instance of the recurring entity collision, found 2026-09-10: a target
    # whose name contains a common word matches on that word alone, because
    # MIN_TITLE_HITS is 1. "Apple Find My network" needs only "apple" in a headline,
    # so 124 items were collected and most were consumer accessory coverage. Any
    # target whose name contains a word this generic must pin itself with `must`.
    GENERIC = {"apple", "google", "purple", "cisco", "meta", "amazon", "spaces",
               "connected", "crowd", "network", "maps", "find", "life", "point"}
    for _c in json.loads(CONFIG.read_text())["competitors"]:
        _w = _c.get("watch") or {}
        if not _w.get("news_query") or _w.get("must") or _w.get("any"):
            continue
        _words = [x for x in re.split(r"[^a-z0-9]+", _c["name"].lower()) if len(x) > 2]
        if _words and all(x in GENERIC for x in _words):
            raise AssertionError(
                f"{_c['id']}: every word in {_c['name']!r} is generic and there is no "
                f"`must`/`any` filter, so a single common word matches any headline")
    # "WaitTime" is also a hashtag and a video-title suffix (12 Oct: a watch
    # listing and a Disneyland ride clip were collected on the first run).
    _wt = next((_c["watch"] for _c in json.loads(CONFIG.read_text())["competitors"]
                if _c["id"] == "waittime"), None)
    if _wt:
        assert not matches("Datejust 41 Mint Green Waittime", "WaitTime", _wt)
        assert matches("Alabama installs WaitTime at Bryant-Denny to improve crowd flow",
                       "WaitTime", _wt)

    # Google News resolution fails closed: anything it cannot resolve returns None
    # and the item keeps its Google link, so a broken endpoint never breaks a run.
    assert resolve_gnews("https://example.com/x") is None
    assert GNEWS_RE.match("https://news.google.com/rss/articles/CBMiabc?oc=5").group(1) == "CBMiabc"
    # Offline: urlopen is replaced, so this can never touch Google (a selftest that
    # does would fail the morning run whenever this runner's IP is throttled).
    class _Resp:
        def __init__(self, body): self.body = body.encode()
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    _uo = urllib.request.urlopen
    _saved_b = dict(GNEWS_BUDGET)
    try:
        urllib.request.urlopen = lambda *a, **k: _Resp("<html>no signature here</html>")
        assert resolve_gnews("https://news.google.com/rss/articles/CBMiabc") is None
        def _throttle(*a, **k):
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        urllib.request.urlopen = _throttle
        GNEWS_BUDGET["throttled"] = False
        assert resolve_gnews("https://news.google.com/rss/articles/CBMiabc") is None
        assert GNEWS_BUDGET["throttled"], "a 429 must be recognised as throttling"
        def _gone(*a, **k):
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        urllib.request.urlopen = _gone
        GNEWS_BUDGET["throttled"] = False
        assert resolve_gnews("https://news.google.com/rss/articles/CBMiabc") is None
        assert not GNEWS_BUDGET["throttled"], "a 404 is one bad link, not throttling"
    finally:
        urllib.request.urlopen = _uo
        GNEWS_BUDGET.clear(); GNEWS_BUDGET.update(_saved_b)

    # Catch-up: newest first, key untouched, stops when throttled, never raises.
    import tempfile
    _rs = globals()["resolve_gnews"]; _bud = dict(GNEWS_BUDGET); _pause = GNEWS_PAUSE
    try:
        globals()["GNEWS_PAUSE"] = 0
        GNEWS_BUDGET.update({"left": 10, "misses": 0})
        now_ = datetime.now(timezone.utc)
        _rows = [{"key": f"k{i}", "kind": "news", "title": f"t{i}",
                  "url": f"https://news.google.com/rss/articles/A{i}",
                  "seen_at": (now_ - timedelta(days=i)).isoformat()} for i in (0, 1, 30)]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as _t:
            _t.write("\n".join(json.dumps(r) for r in _rows) + "\n")
        globals()["resolve_gnews"] = lambda u: "https://pub.test/" + u.rsplit("/", 1)[1]
        assert gnews_catch_up(days=7, path=_t.name) == 2, "only the last 7 days"
        _after = {json.loads(l)["key"]: json.loads(l) for l in open(_t.name) if l.strip()}
        assert _after["k0"]["url"] == "https://pub.test/A0"
        assert _after["k0"]["gnews_url"].endswith("/A0"), "the Google link is kept"
        assert "gnews_url" not in _after["k30"], "older than the window is left alone"
        assert gnews_catch_up(days=7, path=_t.name) == 0, "idempotent"
        _many = [{"key": f"m{i}", "kind": "news", "title": "x",
                  "url": f"https://news.google.com/rss/articles/M{i}",
                  "seen_at": now_.isoformat()} for i in range(20)]
        # Links Google cannot resolve (not throttled): each is tried once and
        # marked, so tomorrow's budget is not spent on them again.
        GNEWS_BUDGET.update({"left": 100, "misses": 0, "throttled": False})
        globals()["resolve_gnews"] = lambda u: None
        Path(_t.name).write_text("\n".join(json.dumps(r) for r in _many) + "\n")
        gnews_catch_up(days=7, path=_t.name)
        assert GNEWS_BUDGET["left"] == 80, GNEWS_BUDGET
        assert all(json.loads(l).get("gnews_unresolvable") for l in open(_t.name) if l.strip())
        _left = GNEWS_BUDGET["left"]
        gnews_catch_up(days=7, path=_t.name)
        assert GNEWS_BUDGET["left"] == _left, "marked links must not be retried"
        # A real throttle stops the run at once and marks nothing.
        GNEWS_BUDGET.update({"left": 100, "misses": 0, "throttled": False})
        def _thr(u):
            GNEWS_BUDGET["throttled"] = True
            return None
        globals()["resolve_gnews"] = _thr
        Path(_t.name).write_text("\n".join(json.dumps(r) for r in _many) + "\n")
        gnews_catch_up(days=7, path=_t.name)
        assert GNEWS_BUDGET["left"] == 99, GNEWS_BUDGET
        assert not any(json.loads(l).get("gnews_unresolvable") for l in open(_t.name) if l.strip()), \
            "a throttled link is not unresolvable - it must be retried tomorrow"
    finally:
        globals()["resolve_gnews"] = _rs
        globals()["GNEWS_PAUSE"] = _pause
        GNEWS_BUDGET.clear(); GNEWS_BUDGET.update(_bud)

    # App watches must key on artistId, never a search term. Searching "polestar"
    # returns Polestar Performance AB, the car maker; "pointr" returns Delta Cygni
    # Labs. Every configured app watch carries a numeric dev id, and any developer
    # whose id spans more than the tracked product carries a name filter.
    _cfg = json.loads(CONFIG.read_text())
    _broad = {"298844389"}          # Cisco's id covers Jabber, Webex, everything
    for _c in _cfg["competitors"]:
        _a = (_c.get("watch") or {}).get("apps")
        if not _a:
            continue
        assert isinstance(_a.get("dev"), int), \
            f"{_c['id']}: app watch needs a numeric artistId, not a search term"
        if str(_a["dev"]) in _broad:
            assert _a.get("only"), \
                f"{_c['id']}: developer {_a['dev']} publishes far more than we track"
        assert "polestar" not in _c["id"] or _a["dev"] != 1403371934, \
            "1403371934 is Polestar Performance AB, the car maker"

    # A developer lookup's first row is the artist record, not an app; and the name
    # filter must actually narrow. Exercised without a network call.
    _fake = {"results": [
        {"wrapperType": "artist", "artistId": 1, "artistName": "Cisco"},
        {"wrapperType": "software", "trackId": 11, "trackName": "Cisco Spaces",
         "version": "1.9", "currentVersionReleaseDate": "2026-07-11T00:00:00Z",
         "trackViewUrl": "https://apps.apple.com/x"},
        {"wrapperType": "software", "trackId": 12, "trackName": "Cisco Jabber",
         "version": "15.3", "currentVersionReleaseDate": "2026-07-20T00:00:00Z"},
    ]}
    _saved = globals()["fetch"]
    try:
        globals()["fetch"] = lambda _u: json.dumps(_fake)
        _all = apple_apps(1)
        assert [a["name"] for a in _all] == ["Cisco Jabber", "Cisco Spaces"], _all
        _only = apple_apps(1, ["spaces"])
        assert [a["name"] for a in _only] == ["Cisco Spaces"], _only
        assert _only[0]["released"] == "2026-07-11", _only[0]
        globals()["fetch"] = lambda _u: "not json at all"
        assert apple_apps(1) == []          # malformed response must not raise
        globals()["fetch"] = lambda _u: None
        assert apple_apps(1) == []          # a dead API must not stop a run
    finally:
        globals()["fetch"] = _saved

    assert canon_locale("https://x.test/en-gb/guides/wpa3")[0] == \
           canon_locale("https://x.test/en-us/guides/wpa3")[0] == \
           "https://x.test/guides/wpa3"
    # "no", "in" and "uk" are deliberately excluded — too likely to be a real word
    # or region segment rather than a language switch
    assert canon_locale("https://x.test/no/thanks")[1] is None
    assert canon_locale("https://x.test/in/the-park")[1] is None

    bt = block_text("<h2>Pricing</h2><p>&pound;8 per delegate</p>"
                    "<li>Activation 3,000</li><script>x()</script>")
    assert bt.count("\n") >= 2, f"block_text must keep block boundaries: {bt!r}"
    assert "£8 per delegate" in bt, bt
    assert "x()" not in bt

    a, r, na, nr = page_diff("Price is 8 per head\nSame line\nDropped line",
                             "Price is 9 per head\nSame line")
    assert any("9 per head" in x for x in a), a
    assert any("8 per head" in x for x in r), r
    assert na >= 1 and nr >= 1
    assert page_diff(None, "First run\nhas no baseline")[2] >= 1, "no baseline must still diff"
    assert page_diff("same", "same")[2] == 0
    # A stored copy cut at PAGE_TEXT_CAP is compared only as far as it reaches
    # (Purple partners, 10 Oct: "Saudi" stored, "Saudi Arabia, Senegal..." live).
    _filler = "\n".join(f"Line number {i} of the page" for i in range(600))
    _cut = (_filler + "\nSaudi Arabia\nSenegal\nSerbia")[:PAGE_TEXT_CAP]
    assert len(_filler) > PAGE_TEXT_CAP
    _a, _r, _na, _nr = page_diff(_cut, _filler + "\nSaudi Arabia\nSenegal\nSerbia")
    assert (_a, _r) == ([], []), (_a, _r)
    _a, _r, _, _ = page_diff(_cut, "New headline here\n" + _filler)
    assert _a == ["New headline here"] and _r == [], (_a, _r)
    _a, _r, _, _ = page_diff(_cut, _cut.replace("Line number 5 of", "Line number five of"))
    assert _a == ["Line number five of the page"], _a
    assert _r == ["Line number 5 of the page"], _r

    t = strip_tags("<p>Hello <script>bad()</script> <b>world</b></p>&nbsp;now")
    assert "bad()" not in t and "Hello" in t and "world" in t, t

    assert page_headline("<html><head><title>Directional Antennas | Purple"
                         "</title></head><body></body></html>") == "Directional Antennas"
    assert page_headline("<html><body><h1>Meet the Team</h1></body></html>") == \
        "Meet the Team"
    # a title too short to survive the site-name trim keeps its original form
    assert page_headline("<title>FAQ | Acme Corp</title>") == "FAQ | Acme Corp"
    assert page_headline("<html><body>no title or h1 here</body></html>") == ""
    assert page_headline("<title>Case &amp; Study &lt;one&gt;</title>") == \
        "Case & Study <one>"

    cfg = json.loads(CONFIG.read_text())
    axes = {a["id"] for a in cfg["matrix_axes"]}
    for c in cfg["competitors"]:
        missing = axes - set(c.get("capabilities", {}))
        assert not missing, f"{c['id']} missing axes {missing}"

    # ids must be unique across ALL sources - they key the state file
    allids = [e["id"] for _, e in targets()]
    dupes = {i for i in allids if allids.count(i) > 1}
    assert not dupes, f"duplicate ids across sources: {dupes}"
    assert len(allids) > 20, f"only {len(allids)} targets loaded - a file missing?"

    # every sector topic needs a relevance filter or it collects noise
    for et, e in targets(kind="sector"):
        w = e.get("watch", {})
        assert w.get("must") or w.get("any"), \
            f"sector topic {e['id']} has no must/any filter"

    # No WYA commercial figure may reach these files. Public figures about other
    # companies are fine and useful - a competitor's sale price, a client's
    # announced capital programme - so a blanket currency ban is wrong. What must
    # never appear is a WYA rate: guard the terms those figures attach to.
    WYA_MONEY = ("per attendee", "per head", "spend per head", "spend-per-head",
                 "uplift %", "our rate", "our price", "volume tier")
    CURRENCY = re.compile(r"[£$€]\s?[\d,]+(?:\.\d+)?\s?(?:m|bn|k|million|billion)?",
                          re.I)
    found = []
    for fname, _, _ in SOURCES:
        p = HERE / fname
        if not p.exists():
            continue
        text = p.read_text()
        low = text.lower()
        for banned in ("self-healing", "self healing", "auto-rerouting",
                       "self-repairing"):
            assert banned not in low or "never" in low, f"{fname}: {banned}"
        # a currency figure in the same sentence as a WYA pricing term is a leak
        for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
            s = sentence.lower()
            if CURRENCY.search(sentence) and any(t in s for t in WYA_MONEY):
                raise AssertionError(
                    f"{fname}: currency figure beside a WYA pricing term - "
                    f"possible confidential leak: {sentence.strip()[:120]}")
        found += [(fname, m.group()) for m in CURRENCY.finditer(text)]
    if found:
        # not a failure: surfaced so a human can confirm each is public
        print("  currency figures present (each must be a public, sourced figure "
              "about another company):")
        for fname, fig in found:
            print(f"    {fname}: {fig}")

    # prune, on a throwaway file - never the real one
    import tempfile
    old = (datetime.now(timezone.utc) - timedelta(days=900)).isoformat(timespec="seconds")
    new = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for r in ({"key": "a", "published": old, "title": "ancient"},
                  {"key": "b", "published": new, "title": "recent"},
                  {"key": "c", "title": "undated"}):
            f.write(json.dumps(r) + "\n")
        tmp = f.name
    assert prune(540, tmp) == 1, "should drop exactly the 900-day-old row"
    rows = [json.loads(l) for l in Path(tmp).read_text().splitlines() if l.strip()]
    titles = {r["title"] for r in rows}
    assert titles == {"recent", "undated"}, titles
    assert "ancient" not in titles
    assert prune(540, tmp) == 0, "pruning twice must be a no-op"
    Path(tmp).unlink()

    # --- watch-health checks against the recorded state -----------------------
    # Everything above this line tests the parsers on synthetic input. That is
    # exactly the gap three real faults lived in: the parsers were fine and the
    # watches were dead. These read .collector_state.json, so they stay offline
    # but describe the collector as it actually ran.
    state_path = HERE / ".collector_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())

        # 1. Pages that all hash the same are an SPA catch-all: the site serves
        #    its homepage for every path, so /pricing and /careers are being
        #    "watched" and can never report a change. Found live on estimote,
        #    occuspace and steerpath — 19 pages, 3 hashes, zero possible signal.
        catchall = []
        for eid, ent in state.items():
            hashes = (ent or {}).get("page_hashes") or {}
            if len(hashes) >= 3 and len(set(hashes.values())) == 1:
                catchall.append((eid, len(hashes)))
        if catchall:
            detail = ", ".join(f"{e} ({n} pages)" for e, n in sorted(catchall))
            print(f"  ! page watches return identical content for: {detail}\n"
                  f"    Those sites serve one page for every path, so the "
                  f"pricing/careers/customers watches cannot ever fire. Point "
                  f"them at real URLs or drop them; leaving them looks like "
                  f"coverage and is not.")

        # 2. A sitemap watch whose stored URL list is empty never got a usable
        #    parse, so the "new URLs" comparison has nothing to compare against.
        dead_sitemaps = [eid for eid, ent in state.items()
                         if isinstance(ent, dict)
                         and "sitemap_urls" in ent
                         and not ent.get("sitemap_urls")]
        if dead_sitemaps:
            print(f"  ! sitemap watch recorded zero URLs for: "
                  f"{', '.join(sorted(dead_sitemaps))} — the URL is probably "
                  f"returning HTML, not XML.")

    # fetch() must swallow anything. This exists because it did not: EOFError from
    # a truncated gzip is not an OSError, so it escaped the old except tuple,
    # propagated out of a function whose docstring says "never raises", and failed
    # the whole scheduled run - losing every target collected before the one that
    # tripped it. Every exception here is one that has been seen from a real
    # competitor site or is in the same family.
    import http.client
    import zlib
    _real_urlopen = urllib.request.urlopen
    before = dict(FETCHES)
    for exc in (EOFError("Compressed file ended before the end-of-stream marker"),
                zlib.error("invalid distance too far back"),
                http.client.IncompleteRead(b"partial"),
                UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
                ssl.SSLError("certificate verify failed"),
                ValueError("unknown url type")):
        def _boom(*a, _e=exc, **k):
            raise _e
        urllib.request.urlopen = _boom
        try:
            got = fetch("https://x.test/anything")
        except Exception as e:                       # noqa: BLE001 - that is the bug
            urllib.request.urlopen = _real_urlopen
            raise AssertionError(
                f"fetch() let {type(e).__name__} escape - it must return None") from e
        assert got is None, f"fetch() must return None on {type(exc).__name__}"
    urllib.request.urlopen = _real_urlopen
    assert FETCHES["fail"] == before["fail"] + 6, "each failure must be counted"
    FETCHES.update(before)

    print("selftest OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
    elif "--check-sources" in args:
        check_sources()
    elif "--prune" in args:
        prune(next((int(a.split("=", 1)[1]) for a in args
                    if a.startswith("--prune-days=")), MAX_AGE_DAYS))
    else:
        kind = next((a.split("=", 1)[1] for a in args if a.startswith("--only=")), None)
        only = next((a for a in args if not a.startswith("-")), None)
        collect(only, kind)
        gnews_catch_up()
        if "--no-prune" not in args:
            prune()
