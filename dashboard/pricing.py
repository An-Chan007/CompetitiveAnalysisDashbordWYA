#!/usr/bin/env python3
"""Track what competitors charge, from every source that publishes it.

WHY

The pricing a buyer brings into a conversation is the number they compare us
against. Until now every figure in competitors.json was hand-typed once, on
2026-08-21, from one source each, and never re-checked. Five of the nine
companies that compete had a pricing page watched at all; one of those 404s.
There was no history, so "Crowd Connected raised their published rate" was
something a person had to notice and remember rather than something the system
knew.

SOURCES, in descending order of how much a buyer would trust them. All free.

  own_page     Their own pricing page, rendered through render.py so tabbed and
               JavaScript-built tables are actually read. Estimote's "$199 dev
               kit" only exists in the rendered DOM.
  wayback      web.archive.org's CDX API over that same page. This is the
               retroactive unlock: it gives price history for changes that
               happened before this code existed, with a citable snapshot URL.
  procurement  UK Contracts Finder and Find a Tender. Awarded public-sector
               contract values are real, published numbers, and they are the
               only hard figures available for a vendor that says "quote only".
  review       G2 / Capterra / TrustRadius pricing panels.

WHAT THIS WILL NOT DO

Guess. A vendor that does not publish stays "not published". Procurement and
review evidence is recorded as evidence with its own confidence, and never
promoted into a headline rate as though the vendor had published it. Blank beats
plausible - that rule predates this file and is not relaxed by it.

Usage:
    python3 pricing.py --selftest
    python3 pricing.py --discover            # find pricing pages we do not watch
    python3 pricing.py --scan [id ...]       # render pricing pages, extract figures
    python3 pricing.py --wayback [id ...]    # build price history from archive.org
    python3 pricing.py --report              # what we know, and how stale it is
    python3 pricing.py --write               # merge findings into competitors.json
"""

import gzip
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import collect
import render

HERE = Path(__file__).parent
CONFIG = HERE / "competitors.json"
FINDINGS = HERE / "pricing_findings.json"

# The ten that actually compete. Pricing work is worth doing properly for these;
# the other 24 are tracked for market signal and a stale note is honest there.
COMPETES = {"cisco-spaces", "crowd-connected", "festyvent", "mapsted", "meshh", "pointr",
            "purple", "apple-find-my", "occuspace", "venuiq", "hoxton-ai", "waittime"}

# Paths worth trying when a competitor has no pricing page watched. Ordered by how
# often each one is the real thing, so discovery stops early on a hit.
CANDIDATE_PATHS = [
    "/pricing", "/pricing/", "/price", "/prices", "/plans", "/plans-pricing",
    "/pricing-plans", "/packages", "/cost", "/buy", "/store", "/order",
    "/product/pricing", "/products/pricing", "/en-gb/pricing", "/get-started",
    "/quote", "/request-a-quote", "/subscriptions",
]

CURRENCY = r"[£$€]|\b(?:GBP|USD|EUR)\b"

# A figure is only interesting with its context - "£8" alone is noise, "£8 per
# delegate" is a price. Captures a window either side so the extract can be read
# and judged by a person rather than trusted blind.
MONEY_RE = re.compile(
    rf"(?P<fig>(?:{CURRENCY})\s?\d[\d,]*(?:\.\d{{1,2}})?\s?(?:k|m|bn)?"
    # The trailing-currency form ("250 €") must not swallow a number that merely sits
    # in front of a leading-currency price. A square-footage slider rendered as
    # "0 100,000 $ 250 / mo" gave the headline "100,000 $ / mo" - the slider maximum
    # priced per month. The lookahead makes the figure lose to the real one.
    rf"|\b\d[\d,]*(?:\.\d{{1,2}})?\s?(?:{CURRENCY})(?!\s?\d))",
    re.I)

# Words that make a number a RATE rather than a random figure on the page. Without
# this, "80,000+ venues" and "ISO 27001" arrive as pricing.
# The abbreviations matter as much as the words. A SaaS pricing page writes a
# monthly rate as "/ mo" more often than as "per month", and without mo|mth|yr the
# figure was extracted and then discarded as "not a rate". Found 2026-09-22 on
# Mapsted's archived pricing page, which carried a published monthly rate for ten
# months and came back from the archive as zero rated figures - a real price
# reported as no price at all.
UNIT_RE = re.compile(
    # \b before the group was wrong: "/" is not a word character, so " / mo" never
    # matched. The word boundary belongs to "per" alone.
    # A quantity may sit between the slash and the unit: "/10K sqft/12 months",
    # "per 1,000 visitors". Cisco publishes its Spaces list price that way, and
    # without this the $4,725 and $10,395 per-10K-sqft rates were read as unitless
    # numbers and dropped - on the competitor whose pricing matters most.
    r"(?:\bper|/)\s?(?:\d[\d,.]*\s?[kKmM]?\s?)?(delegate|attendee|head|person|user|"
    r"visitor|device|tag|seat|"
    r"month|mo|mos|mth|mnth|year|yr|yrs|annum|day|event|site|venue|building|room|"
    r"sq\s?ft|sqm|m2|m²|1,?000\s?m|access\s?point|ap|licen[cs]e|pax|pp)s?\b"   # plural: "per 1,000 visitors", "/12 months"
    r"|\b(monthly|annually|per annum|billed (?:monthly|yearly|annually)|"
    r"one[- ]off|setup|set-up|activation|"
    r"installation|minimum|starting (?:at|from)|from)\b", re.I)

# Phrases that mean "we do not publish a price". Recorded as a finding in their own
# right: "quote only" is an answer, and a page that changes FROM quote-only TO a
# number is a real event.
QUOTE_ONLY_RE = re.compile(
    r"\b(request a (?:quote|demo|price)|contact (?:us|sales) for|get a quote|"
    r"talk to sales|custom pricing|pricing on request|bespoke pricing|"
    r"tailored (?:to|pricing))\b", re.I)

CONTEXT = 90
# The unit must be found close to the figure, not anywhere in the context window.
# With the wide window, "Activation from £4,500 one-off. Then £8 per delegate."
# gave £4,500 the unit "per delegate" - it bled in from the next figure - and the
# setup fee then outranked the real rate in the headline. The context stays wide
# because a person reads it; only the unit search is narrowed.
UNIT_AFTER = 34
UNIT_BEFORE = 22
MAX_FIGURES = 40
WAYBACK_CDX = ("https://web.archive.org/cdx/search/cdx?url={url}&output=json"
               "&fl=timestamp,original,statuscode,digest&collapse=digest"
               "&filter=statuscode:200&limit=60")
SNAPSHOT = "https://web.archive.org/web/{ts}/{url}"


def now_date():
    return datetime.now(timezone.utc).date().isoformat()


def save(key, value):
    """Merge one section into the findings file. Each subcommand writes its own
    key; --discover used to write the whole file and silently discard whatever
    --scan had just put there."""
    found = json.loads(FINDINGS.read_text()) if FINDINGS.exists() else {}
    found[key] = {**(found.get(key) or {}), **value}
    found["at"] = now_date()
    FINDINGS.write_text(json.dumps(found, indent=1, ensure_ascii=False) + "\n")
    return found


def load_config():
    return json.loads(CONFIG.read_text())


def by_id(cfg):
    return {c["id"]: c for c in cfg["competitors"]}


def pricing_page(comp):
    """The URL we should be reading for this competitor, if we know one."""
    p = comp.get("pricing") or {}
    if p.get("page"):
        return p["page"]
    for pg in (comp.get("watch") or {}).get("pages") or []:
        if pg.get("label") == "pricing":
            return pg["url"]
    return None


# ------------------------------------------------------------------ extraction

def http_status(url):
    """HEAD the URL, falling back to GET where a host refuses HEAD. None on error.

    A renderer will happily return a 404 page as successfully-fetched content, and
    VenuIQ's dead pricing page was being read as "quote only" on that basis - a
    false reading that looks exactly like a real finding. The status is what tells
    the difference between "they stopped publishing a price" and "the URL moved".
    """
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": collect.UA})
    try:
        with urllib.request.urlopen(req, timeout=collect.TIMEOUT) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code >= 400 and collect.fetch(url):
            return 200            # HEAD refused, GET works
        return e.code
    except Exception:
        return None


def extract(text, url=""):
    """Money figures with enough context to judge them, plus a quote-only read.

    Returns {figures: [...], quote_only: bool, rated: int}. `rated` counts the
    figures that sit next to a per-unit or billing word, which is what separates
    a price from a statistic. A caller should trust `rated` and treat the rest as
    material for a human to look at.
    """
    flat = " ".join((text or "").split())
    figures, seen = [], set()
    for m in MONEY_RE.finditer(flat):
        fig = " ".join(m.group("fig").split())
        lo, hi = max(0, m.start() - CONTEXT), min(len(flat), m.end() + CONTEXT)
        ctx = flat[lo:hi]
        # Look AFTER the figure first - "£8 per delegate" puts the unit there -
        # and only then just before it, for "from £199" / "activation £4,500".
        # Searching one window around the figure picked up whichever unit word
        # came first in the string, which for "Activation from £4,500 one-off.
        # Then £8 per delegate." handed the setup fee the word "activation" and
        # the real rate the word "one-off", so the headline named the wrong one.
        after = UNIT_RE.search(flat[m.end():m.end() + UNIT_AFTER])
        if after:
            unit = after
        else:
            head = flat[max(0, m.start() - UNIT_BEFORE):m.start()]
            hits = list(UNIT_RE.finditer(head))
            unit = hits[-1] if hits else None      # the closest one, not the first
        keyed = (fig.lower(), ctx[:40].lower())
        if keyed in seen:
            continue
        seen.add(keyed)
        figures.append({"figure": fig,
                        "unit": unit.group(0).strip().lower() if unit else "",
                        "context": ctx})
        if len(figures) >= MAX_FIGURES:
            break
    rated = sum(1 for f in figures if f["unit"])
    return {"figures": figures, "rated": rated,
            "quote_only": bool(QUOTE_ONLY_RE.search(flat)),
            "url": url, "chars": len(flat)}


def headline_from(figures):
    """The most quotable rate on the page: a figure that carries a per-unit word."""
    rated = [f for f in figures if f["unit"]]
    if not rated:
        return ""
    best = min(rated, key=lambda f: (0 if re.search(
        r"delegate|attendee|head|person|visitor", f["unit"]) else 1))
    return f"{best['figure']} {best['unit']}".strip()


# ------------------------------------------------------------------- discovery

def discover(ids=None, verbose=True):
    """Find pricing pages for competitors that have none watched.

    Four of the nine that compete had no pricing surface at all, so their row read
    "no public pricing page found" without anyone having looked recently.
    """
    cfg = load_config()
    found = {}
    for comp in cfg["competitors"]:
        cid = comp["id"]
        if ids and cid not in ids:
            continue
        if not ids and cid not in COMPETES:
            continue
        known = pricing_page(comp)
        site = (comp.get("website") or "").rstrip("/")
        if not site:
            continue
        if known:
            res = render.render_one(known)
            ok = res["ok"] and extract(res["text"])["figures"]
            if verbose:
                print(f"{cid:16} watched {known}  "
                      f"{'ok' if res['ok'] else 'DEAD'}"
                      f"{'' if ok else '  (no figures on it)'}")
            if res["ok"]:
                found[cid] = known
                continue
            if verbose:
                print(f"{cid:16}   watched page is dead, searching for another")
        for path in CANDIDATE_PATHS:
            url = site + path
            res = render.render_one(url)
            if not res["ok"]:
                continue
            info = extract(res["text"], url)
            if info["rated"] or info["quote_only"]:
                found[cid] = url
                if verbose:
                    print(f"{cid:16} FOUND {url}  "
                          f"{info['rated']} rated figures"
                          f"{', quote-only language' if info['quote_only'] else ''}")
                break
        else:
            if verbose and cid not in found:
                print(f"{cid:16} no pricing page found on {site}")
    return found


# ------------------------------------------------------------------------ scan

def scan(ids=None, verbose=True):
    """Render each pricing page and record what it says today."""
    cfg = load_config()
    out = {}
    for comp in cfg["competitors"]:
        cid = comp["id"]
        if ids and cid not in ids:
            continue
        if not ids and cid not in COMPETES:
            continue
        url = pricing_page(comp)
        if not url:
            # No known page used to mean "skip", so a company that publishes no
            # price was never looked at again: four of the ten showed a "checked"
            # date of 21 August a month later. Look for a page every run, and
            # record a dated "none found" when there still is none.
            url = discover([cid], verbose=False).get(cid)
            if not url:
                out[cid] = {"url": comp.get("website") or "", "seen": now_date(),
                            "ok": True, "no_page": True, "figures": [],
                            "headline": ""}
                if verbose:
                    print(f"{cid:16} still no pricing page on {comp.get('website')}")
                continue
        status = http_status(url)
        if status is not None and status >= 400:
            if verbose:
                print(f"{cid:16} ! HTTP {status} on {url} — recorded as unreachable, "
                      f"NOT as 'no price published'")
            out[cid] = {"url": url, "seen": now_date(), "ok": False,
                        "http": status, "error": f"HTTP {status}"}
            continue
        res = render.render_one(url, force=True)
        if not res["ok"]:
            if verbose:
                print(f"{cid:16} ! could not read {url}: {res['error'][:70]}")
            out[cid] = {"url": url, "seen": now_date(), "ok": False,
                        "error": res["error"][:200]}
            continue
        info = extract(res["text"], url)
        info.update({"seen": now_date(), "ok": True, "via": res["via"],
                     "http": status, "headline": headline_from(info["figures"])})
        out[cid] = info
        if verbose:
            print(f"{cid:16} {info['rated']:2} rated / {len(info['figures']):2} figures"
                  f"  {'quote-only  ' if info['quote_only'] else '            '}"
                  f"{info['headline'][:44]}")
            for f in [f for f in info["figures"] if f["unit"]][:4]:
                print(f"                   • {f['figure']:14} {f['unit'][:18]:20} "
                      f"…{f['context'][CONTEXT - 40:CONTEXT + 50]}…")
    return out


# --------------------------------------------------------------------- wayback

# archive.org's CDX endpoint is frequently slower than collect's 25s default and
# times out on a first call that then succeeds on a retry. Retrying is worth it:
# this is the only source of price history for a page that no longer exists, and
# VenuIQ's is exactly that.
CDX_ATTEMPTS = 3
CDX_TIMEOUT = 90


def cdx(url):
    """Archived snapshots of a URL, newest last. Deduped by content digest, so a
    row here means the page actually changed, not that it was crawled again."""
    api = WAYBACK_CDX.format(url=urllib.parse.quote(url, safe=""))
    body = None
    original = collect.TIMEOUT
    try:
        collect.TIMEOUT = CDX_TIMEOUT
        for attempt in range(CDX_ATTEMPTS):
            body = collect.fetch(api)
            if body:
                break
            if attempt < CDX_ATTEMPTS - 1:
                time.sleep(2 * (attempt + 1))
    finally:
        collect.TIMEOUT = original
    if not body:
        return []
    try:
        rows = json.loads(body)
    except json.JSONDecodeError:
        return []
    if not rows or len(rows) < 2:
        return []
    head, data = rows[0], rows[1:]
    return [dict(zip(head, r)) for r in data]


# ------------------------------------------------------------- common crawl

# A second archive, added 2026-09-22 because archive.org was offline for the whole
# day and the price history of a withdrawn page is the one thing that cannot be
# re-read from the live web. Common Crawl keeps its own WARCs and serves them by
# byte range over plain HTTPS, so this needs no key and no new dependency - it is
# a range request and a gunzip.
#
# Its weakness is the mirror of archive.org's: coverage is a snapshot every few
# weeks at best and a URL can be missed entirely, so it is a FALLBACK, not a
# replacement. On Mapsted it recovered six live captures of a pricing page that
# has 301'd to a contact form since early 2023, and showed the page was still up
# in September 2022 - nine months after Mapsted's own comparison page says the
# offer was discontinued.
CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_INDEX = "https://index.commoncrawl.org/{idx}-index?url={url}&output=json"
CC_DATA = "https://data.commoncrawl.org/{path}"
CC_MAX_INDEXES = 24       # ~128 exist; sampling the span beats crawling all of them
CC_ATTEMPTS = 3


def cc_indexes(limit=CC_MAX_INDEXES):
    """Index names spread evenly across Common Crawl's whole history, newest first."""
    body = collect.fetch(CC_COLLINFO)
    if not body:
        return []
    try:
        ids = [c["id"] for c in json.loads(body)]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    if len(ids) <= limit:
        return ids
    step = len(ids) / limit
    return [ids[int(i * step)] for i in range(limit)]


def cc_range(path, offset, length):
    """One WARC record, fetched by byte range and gunzipped. None on any failure."""
    req = urllib.request.Request(
        CC_DATA.format(path=path),
        headers={"User-Agent": collect.UA,
                 "Range": f"bytes={offset}-{offset + length - 1}"})
    try:
        with urllib.request.urlopen(req, timeout=CDX_TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            return gzip.decompress(r.read()).decode("utf-8", errors="replace")
    except Exception:
        return None


def commoncrawl(url, verbose=True):
    """Archived captures of a URL from Common Crawl, oldest first.

    Returns the same shape cdx() does - {timestamp, original, ...} plus the byte
    range needed to read the record back - so wayback() can treat both the same.
    """
    quoted = urllib.parse.quote(url.rstrip("/") + "*", safe="")
    rows, seen = [], set()
    for idx in cc_indexes():
        body = None
        for attempt in range(CC_ATTEMPTS):
            body = collect.fetch(CC_INDEX.format(idx=idx, url=quoted))
            if body:
                break
            time.sleep(2 * (attempt + 1))
        if not body:
            continue
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Only real page bodies. A 301 is a fact about the page (it tells us
            # WHEN it was withdrawn) but it carries no figures, so it is kept for
            # the date and skipped for extraction by wayback().
            if j.get("status") not in ("200", "301", "308"):
                continue
            key = (j.get("digest"), j.get("status"))
            if key in seen:
                continue
            seen.add(key)
            rows.append({"timestamp": j.get("timestamp", ""),
                         "original": j.get("url", url),
                         "statuscode": j.get("status"),
                         "digest": j.get("digest", ""),
                         "cc_path": j.get("filename"),
                         "cc_offset": int(j.get("offset", 0)),
                         "cc_length": int(j.get("length", 0))})
        time.sleep(0.6)                        # the index asks for gentleness
    rows.sort(key=lambda r: r["timestamp"])
    if verbose and rows:
        live = sum(1 for r in rows if r["statuscode"] == "200")
        print(f"  common crawl: {len(rows)} captures, {live} with a page body")
    return rows


# archive.org answers 429 to snapshot reads 0.5s apart: on 12 Oct two of Hoxton
# AI's three versions came back 429 and the history silently lost them. Pace each
# read, and on a failure wait and try again rather than skip - a skipped snapshot
# can be the only record of a price.
SNAPSHOT_PACE = 4
SNAPSHOT_RETRY_WAITS = (20, 60, 120)


def snapshot(url, sleep=time.sleep, fetch=None, verbose=True):
    """One archived page, paced, retried with backoff. None only if every try failed."""
    fetch = fetch or collect.fetch
    for wait in (0,) + SNAPSHOT_RETRY_WAITS:
        if wait and verbose:
            print(f"    archive.org refused; waiting {wait}s", file=sys.stderr)
        sleep(wait or SNAPSHOT_PACE)
        body = fetch(url)
        if body:
            return body
    return None


def wayback(ids=None, max_snapshots=60, verbose=True):
    """Build a price history from archived copies of each pricing page.

    Reads every distinct archived version, not a sample. The CDX query already
    collapses by content digest, so a row here means the page actually changed -
    there is nothing to thin out except real price movements.

    It used to read 8 evenly spread snapshots, and that was wrong in the exact
    case this function exists for. Mapsted has 54 distinct versions going back to
    2019; its self-serve price was live for about ten months of that, Nov 2021 to
    Sep 2022. An even spread stepped straight over the window and reported no
    price had ever been published. A short-lived price is the interesting one, and
    sampling is what hides it. This is a manual command, not part of the daily
    run, so reading all of them costs nothing that matters.
    """
    cfg = load_config()
    history = {}
    for comp in cfg["competitors"]:
        cid = comp["id"]
        if ids and cid not in ids:
            continue
        if not ids and cid not in COMPETES:
            continue
        url = pricing_page(comp)
        if not url:
            continue
        snaps = cdx(url)
        source = "archive.org"
        if not snaps:
            # Fallback, not a race: archive.org is the better index when it is up.
            if verbose:
                print(f"{cid:16} archive.org returned nothing for {url} — trying Common Crawl")
            snaps = commoncrawl(url, verbose=verbose)
            source = "commoncrawl"
        if not snaps:
            if verbose:
                print(f"{cid:16} no archived snapshots of {url}")
            continue
        picked = snaps[:max_snapshots]
        if snaps and snaps[-1] not in picked:
            picked.append(snaps[-1])          # always include the newest
        if verbose:
            print(f"\n{cid} — {len(snaps)} distinct archived versions, "
                  f"reading {len(picked)}")
        rows = []
        for s in picked:
            ts = s.get("timestamp", "")
            when = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
            if source == "commoncrawl":
                snap_url = s.get("original") or url
                if s.get("statuscode") != "200":
                    # A redirect carries no figures, but WHEN a pricing page
                    # started redirecting is the most useful date in the history.
                    rows.append({"date": when, "snapshot": snap_url,
                                 "headline": "", "figures": [], "quote_only": False,
                                 "withdrawn": True, "via": "commoncrawl",
                                 "http": s.get("statuscode")})
                    if verbose:
                        print(f"  {when}  HTTP {s.get('statuscode')} — page no longer served")
                    continue
                record = cc_range(s["cc_path"], s["cc_offset"], s["cc_length"])
                body = record.split("\r\n\r\n", 2)[-1] if record else None
            else:
                snap_url = SNAPSHOT.format(ts=ts, url=s.get("original") or url)
                body = snapshot(snap_url)
            if not body:
                continue
            info = extract(collect.block_text(body), snap_url)
            rated = [f for f in info["figures"] if f["unit"]]
            rows.append({"date": when, "snapshot": snap_url, "via": source,
                         "headline": headline_from(info["figures"]),
                         "figures": [f"{f['figure']} {f['unit']}".strip()
                                     for f in rated[:8]],
                         "quote_only": info["quote_only"]})
            if verbose:
                print(f"  {when}  {len(rated):2} rated  "
                      f"{(headline_from(info['figures']) or ('quote only' if info['quote_only'] else '—'))[:60]}")
        if rows:
            history[cid] = rows
    return history


# ---------------------------------------------------------------------- report

# ----------------------------------------------------------------------- write

# The discipline that makes this safe to run daily: machine extraction never
# overwrites a human judgement. `value`, `note`, `headline`, `source` and
# `confidence` are hand-written prose and stay hand-written. What gets written is
# additive and separately named - `observed`, `history`, `evidence`, `page` - so a
# disagreement between what a person wrote and what the page says today is
# VISIBLE rather than silently resolved in either direction. `disagrees` is the
# flag that says a human should look.
HUMAN_FIELDS = ("value", "note", "headline", "source", "confidence", "status",
                "model", "published")


def merge_findings(comp, scanned, history, discovered):
    """Return the new `pricing` dict for one competitor. Pure, so it is testable."""
    cid = comp["id"]
    p = dict(comp.get("pricing") or {})
    page = discovered.get(cid) or pricing_page(comp)
    if page:
        p["page"] = page

    sc = scanned.get(cid)
    if sc and sc.get("ok"):
        rated = [f for f in sc["figures"] if f["unit"]]
        p["observed"] = {
            "seen": sc["seen"],
            "via": sc.get("via", ""),
            "url": sc.get("url", page or ""),
            "headline": sc.get("headline", ""),
            "quote_only": sc.get("quote_only", False),
            "figures": [f"{f['figure']} {f['unit']}".strip() for f in rated[:10]],
            "quotes": [f["context"] for f in rated[:3]],
        }
        p["checked"] = sc["seen"]
        # Does what the page says today match what a person wrote? Compare only
        # the numerals - wording differs harmlessly, digits do not.
        written = re.findall(r"\d[\d,]*", p.get("headline") or "")
        seen_now = re.findall(r"\d[\d,]*", sc.get("headline") or "")
        # A written range ("$995-1,300 per entrance") agrees with either end: flag
        # only a figure the written headline does not contain at all (Hoxton AI).
        p["disagrees"] = bool(written and seen_now and seen_now[0] not in written)
    elif sc and not sc.get("ok"):
        # Unreachable is not the same as unpriced. Recorded so the dashboard can
        # say "we could not read this" rather than implying they publish nothing.
        p["observed"] = {"seen": sc["seen"], "url": sc.get("url", ""),
                         "error": sc.get("error", ""), "unreachable": True,
                         "http": sc.get("http"), "figures": []}

    h = history.get(cid)
    if h:
        # Collapse consecutive snapshots that say the same thing: the interesting
        # rows are the ones where the number moved.
        trimmed, last = [], None
        for row in h:
            key = row.get("headline") or ("quote only" if row.get("quote_only") else "")
            if not key:
                # A snapshot with no figure and no quote-only language says
                # nothing. Purple's page has nine of these; carrying them as
                # "history" would imply we know something we do not.
                continue
            if key == last:
                continue
            last = key
            trimmed.append(row)
        if trimmed:
            p["history"] = trimmed
    return p


def write(scanned=None, history=None, discovered=None, dry=False):
    """Merge findings into competitors.json. Additive only - see HUMAN_FIELDS."""
    found = json.loads(FINDINGS.read_text()) if FINDINGS.exists() else {}
    scanned = scanned if scanned is not None else (found.get("scan") or {})
    history = history if history is not None else (found.get("history") or {})
    discovered = discovered if discovered is not None else (found.get("discovered") or {})

    raw = CONFIG.read_text()
    cfg = json.loads(raw)
    changed = []
    for comp in cfg["competitors"]:
        cid = comp["id"]
        if cid not in scanned and cid not in history and cid not in discovered:
            continue
        before = json.dumps(comp.get("pricing") or {}, sort_keys=True)
        newp = merge_findings(comp, scanned, history, discovered)
        if json.dumps(newp, sort_keys=True) != before:
            comp["pricing"] = newp
            bits = []
            if newp.get("observed", {}).get("headline"):
                bits.append(f"observed {newp['observed']['headline']}")
            if newp.get("history"):
                bits.append(f"{len(newp['history'])} history points")
            if newp.get("disagrees"):
                bits.append("DISAGREES with the written headline")
            changed.append((cid, "; ".join(bits) or "page recorded"))

    for cid, what in changed:
        print(f"  {cid:18} {what}")
    if dry:
        print(f"\n{len(changed)} would change (dry run, nothing written)")
        return changed
    if changed:
        # Match the file's existing indent exactly. Writing indent=1 over a file
        # written with indent=2 reindents all 4,500 lines, so a two-field change
        # arrives as an unreviewable diff.
        indent = 2
        for line in raw.splitlines():
            if line.startswith(" ") and line.lstrip().startswith('"'):
                indent = len(line) - len(line.lstrip())
                break
        CONFIG.write_text(json.dumps(cfg, indent=indent, ensure_ascii=False) + "\n")
    print(f"\n{len(changed)} competitors updated in {CONFIG.name}")
    return changed


def report():
    cfg = load_config()
    today = datetime.now(timezone.utc).date()
    print(f"{'competitor':18}{'status':15}{'checked':12}{'age':>5}  headline")
    print("-" * 96)
    for comp in sorted(cfg["competitors"], key=lambda c: c["id"]):
        p = comp.get("pricing")
        if not p and comp["id"] not in COMPETES:
            continue
        if not p:
            print(f"{comp['id']:18}{'NOT TRACKED':15}{'—':12}{'':>5}  "
                  f"competes, and has no pricing record at all")
            continue
        checked = p.get("checked", "")
        try:
            age = (today - datetime.fromisoformat(checked).date()).days
            age_s = f"{age}d"
        except ValueError:
            age, age_s = 9999, "?"
        flag = "  <-- stale" if age > 14 else ""
        print(f"{comp['id']:18}{p.get('status', '?'):15}{checked or '—':12}"
              f"{age_s:>5}  {(p.get('headline') or '')[:44]}{flag}")
        n = len(p.get("history") or [])
        if n:
            h = p["history"]
            print(f"{'':35}history: {n} points, {h[0]['date']} → {h[-1]['date']}")


def selftest():
    """Offline. No network, no browser."""
    # Common Crawl sampling: cover the whole span, never more than the cap. The
    # bug this guards is quietly reading only the newest indexes, which would
    # return "no history" for exactly the withdrawn pages this exists to find.
    import unittest.mock as _mock
    fake = json.dumps([{"id": f"CC-MAIN-{y}-{w:02d}"}
                       for y in range(2015, 2027) for w in (5, 20, 35, 50)])
    with _mock.patch.object(collect, "fetch", lambda _u: fake):
        picked = cc_indexes(limit=6)
    assert len(picked) == 6, picked
    assert picked[0].startswith("CC-MAIN-2015"), picked
    assert picked[-1].startswith("CC-MAIN-202"), picked
    assert len(set(picked)) == 6, "sampling must not repeat an index"
    with _mock.patch.object(collect, "fetch", lambda _u: None):
        assert cc_indexes() == [], "a dead index list must not raise"

    # archive.org 429s a snapshot read: wait and retry, never skip it (Hoxton AI,
    # 12 Oct: two of three versions lost to 429 at a 0.5s pace).
    _calls, _waits = [], []
    def _flaky(_u):
        _calls.append(_u)
        return "page" if len(_calls) == 3 else None
    assert snapshot("u", sleep=_waits.append, fetch=_flaky, verbose=False) == "page"
    assert len(_calls) == 3 and _waits[0] >= 2 and _waits[1] >= 10, (_calls, _waits)
    assert snapshot("u", sleep=lambda _s: None, fetch=lambda _u: None, verbose=False) is None

    # A price is a figure plus a unit. A statistic is a figure without one.
    r = extract("Attendee Tracking is £8 per delegate plus a £4,500 activation fee. "
                "We serve 80,000 venues and hold ISO 27001.")
    figs = {f["figure"] for f in r["figures"]}
    assert "£8" in figs and "£4,500" in figs, figs
    assert r["rated"] >= 2, r
    assert "80,000" not in figs, "a venue count is not a price"
    assert headline_from(r["figures"]) == "£8 per delegate", headline_from(r["figures"])

    # Abbreviated rate units are still rates.
    for s in ("$250 / mo", "$250/mo", "$ 250 / mo", "€99 /yr", "£4.50 per pax"):
        got = extract(f"Indoor Maps and Wayfinding {s} Buy Online Now")
        assert got["rated"] == 1, (s, got["figures"])
    # ...and a bare number with no unit is still not a price.
    assert extract("We serve 4 billion sq ft across 30 countries.")["rated"] == 0

    # A figure in front of a price must not absorb its currency symbol.
    slider = extract("sq ft 0 100,000 $ 250 / mo Buy Online Now")
    assert headline_from(slider["figures"]) == "$ 250 / mo", slider["figures"]
    # ...while a genuine trailing-currency price still parses.
    assert extract("Prix 250 € / mo")["rated"] == 1

    # A quantity between the slash and the unit must not hide the rate.
    for s in ("$4725 /10K sqft/12 months", "$10,395 /10K sqft/12 months",
              "£12 per 1,000 visitors"):
        assert extract(f"List price {s}")["rated"] == 1, (s, extract(f"List price {s}"))

    # Quote-only is a finding, not an absence.
    q = extract("Enterprise plans are tailored. Contact us for pricing.")
    assert q["quote_only"] and not q["figures"], q
    assert headline_from(q["figures"]) == ""

    # Other currencies and formats seen on real competitor pages.
    for txt, want in (("from $199 per device", "$199"),
                      ("€55/month per 1,000m²", "€55"),
                      ("USD 2,000 per year", "USD 2,000"),
                      ("$0.96 per response", "$0.96")):
        got = {f["figure"] for f in extract(txt)["figures"]}
        assert any(w.lower() in g.lower() for g in got for w in [want]), (txt, got)

    # A per-delegate rate outranks a setup fee in the headline, because that is
    # the number a buyer compares.
    r2 = extract("Activation from £4,500 one-off. Then £8 per delegate.")
    assert "delegate" in headline_from(r2["figures"]), headline_from(r2["figures"])

    # cdx must survive a dead or nonsense response rather than raising.
    real = collect.fetch
    try:
        collect.fetch = lambda u: None
        assert cdx("https://x.test/pricing") == []
        collect.fetch = lambda u: "not json"
        assert cdx("https://x.test/pricing") == []
        collect.fetch = lambda u: "[]"
        assert cdx("https://x.test/pricing") == []
    finally:
        collect.fetch = real

    # The merge must be additive. A hand-written judgement is never overwritten by
    # what a scraper read today - that is the whole reason the observed figures
    # live under their own key and a mismatch raises `disagrees` instead.
    comp = {"id": "x", "pricing": {"headline": "£8 per delegate",
                                   "value": "hand written prose",
                                   "note": "a human wrote this",
                                   "source": "their site, checked by a person",
                                   "checked": "2026-08-21"}}
    scanned = {"x": {"ok": True, "seen": "2026-09-04", "via": "browser",
                     "url": "https://x.test/pricing", "headline": "£9 per delegate",
                     "quote_only": False,
                     "figures": [{"figure": "£9", "unit": "per delegate",
                                  "context": "now £9 per delegate"}]}}
    # A dead page must never be recorded as "they publish no price".
    dead = merge_findings(comp, {"x": {"ok": False, "seen": "2026-09-04",
                                       "http": 404, "url": "https://x.test/p",
                                       "error": "HTTP 404"}}, {}, {})
    assert dead["observed"]["unreachable"] is True and dead["observed"]["http"] == 404
    assert dead["headline"] == comp["pricing"]["headline"], "prose survives a 404"

    merged = merge_findings(comp, scanned, {}, {})
    for f in HUMAN_FIELDS:
        if f in comp["pricing"]:
            assert merged[f] == comp["pricing"][f], f"{f} must not be overwritten"
    assert merged["observed"]["headline"] == "£9 per delegate"
    assert merged["disagrees"] is True, "8 vs 9 must be flagged for a human"
    assert merged["checked"] == "2026-09-04"

    # Matching figures must NOT raise the flag.
    scanned["x"]["headline"] = "£8 per delegate"
    assert merge_findings(comp, scanned, {}, {})["disagrees"] is False
    _rng = {"id": "r", "pricing": {"headline": "$995\u20131,300 per entrance/yr"}}
    assert merge_findings(_rng, {"r": {"ok": True, "seen": "2026-10-12", "figures": [],
                                       "headline": "$1,300 / year"}}, {}, {})["disagrees"] is False

    # History collapses runs of the same price - the rows that matter are moves.
    hist = {"x": [{"date": "2024-01-01", "headline": "£6 per delegate"},
                  {"date": "2024-06-01", "headline": "£6 per delegate"},
                  {"date": "2025-01-01", "headline": "£7 per delegate"},
                  {"date": "2026-01-01", "headline": "", "quote_only": True}]}
    hist["x"].insert(0, {"date": "2023-01-01", "headline": "", "quote_only": False})
    h = merge_findings(comp, {}, hist, {})["history"]
    assert [r["date"] for r in h] == ["2024-01-01", "2025-01-01", "2026-01-01"], h
    # A competitor whose every snapshot is blank gets no history key at all,
    # rather than a list of rows that assert nothing.
    blank = merge_findings(comp, {}, {"x": [{"date": "2020-01-01", "headline": ""}]}, {})
    assert "history" not in blank, blank

    cfg = load_config()
    ids = {c["id"] for c in cfg["competitors"]}
    missing = COMPETES - ids
    assert not missing, f"COMPETES names ids that do not exist: {missing}"
    print("pricing selftest OK")


def main(argv):
    ids = [a for a in argv if not a.startswith("-")] or None
    if "--selftest" in argv:
        return selftest()
    if "--discover" in argv:
        found = discover(ids)
        save("discovered", found)
        print(f"\n{len(found)} pricing pages -> {FINDINGS.name}")
        return
    if "--wayback" in argv:
        hist = wayback(ids)
        save("history", hist)
        print(f"\nhistory for {len(hist)} competitors -> {FINDINGS.name}")
        return
    if "--write" in argv:
        return write(dry="--dry-run" in argv)
    if "--scan" in argv:
        got = scan(ids)
        save("scan", got)
        print(f"\nscanned {len(got)} -> {FINDINGS.name}")
        return
    report()


if __name__ == "__main__":
    main(sys.argv[1:])
