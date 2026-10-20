#!/usr/bin/env python3
"""Read what we collected, then have Claude analyse it.

Two stages, deliberately separate because they have different requirements.

  --read      Fetch full article text for everything reachable. Stdlib only,
              no credentials. Writes content.jsonl.

  --analyse   Send the week's content to Claude and get real analysis back:
              what changed, what it means for WYA, what to do. Needs an
              Anthropic credential. Writes analysis.json.

WHY THIS EXISTS
The collector gathers headlines. A headline plus a regex-matched template is a
label, not analysis - the same sentence appears for every pricing item whatever
actually happened. Nothing in the system had read anything. This stage reads,
then reasons.

WHAT IS AND IS NOT REACHABLE (measured 2026-08-04, not assumed)
  post / new_page / page_change  fetchable - real publisher and competitor URLs
  news                           NOT fetchable - a Google News RSS link returns
                                 a ~590KB JavaScript interstitial whose only
                                 text is "Google News". The redirect to the
                                 publisher is client-side, so no stdlib HTTP
                                 call reaches the article. This needs a headless
                                 browser or a scraping service, and it is the
                                 one job in this project that genuinely justifies
                                 paying for Apify.

Usage:
    python3 analyse.py --read
    python3 analyse.py --read --days=14
    python3 analyse.py --analyse
    python3 analyse.py --selftest
"""

import gzip
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
CHANGES = HERE / "changes.jsonl"
CONTENT = HERE / "content.jsonl"
ANALYSIS = HERE / "analysis.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25
MAX_CHARS = 6000          # per article, plenty for analysis, bounds the prompt
# "news" is readable once collect.resolve_gnews() has swapped the Google link for
# the publisher's; an unresolved Google link is still skipped by GNEWS below.
FETCHABLE = {"post", "new_page", "page_change", "app_release", "news"}
MODEL = "claude-opus-5-5"     # switched from claude-opus-5 on 2026-10-12 (owner); 20% cheaper
PRICE_IN, PRICE_OUT = 4, 20   # $ per million tokens for MODEL
# A feed item published before this many days ago is not "new". The owner asked
# for new updates only, and a headline can be relevance-ranked into the packet
# long after it was news.
FEED_WINDOW_DAYS = 3

# A Google News link never resolves server-side. Recognise it and skip rather
# than storing 590KB of interstitial JavaScript as if it were an article.
GNEWS = re.compile(r"^https?://news\.google\.com/", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.geturl(), raw.decode("utf-8", errors="replace")
    # Broad on purpose - same reason as collect.fetch(). A truncated gzip raises
    # EOFError, which is not an OSError, and it escaped the old narrow tuple.
    except Exception as e:
        return None, f"__error__ {type(e).__name__}: {e}"


def article_text(html):
    """Strip chrome and markup down to readable prose."""
    h = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|aside|form)"
               r"[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?s)<!--.*?-->", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'),
                    ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&rsquo;", "'"), ("&ldquo;", '"'), ("&rdquo;", '"'),
                    ("&mdash;", "-"), ("&ndash;", "-")):
        h = h.replace(ent, ch)
    return re.sub(r"\s+", " ", h).strip()


def recent(days):
    if not CHANGES.exists():
        return []
    cut = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .isoformat(timespec="seconds")
    out = []
    for line in CHANGES.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (r.get("published") or r.get("seen_at") or "") >= cut:
            out.append(r)
    return out


def read(days=7):
    rows = recent(days)
    have = set()
    if CONTENT.exists():
        for line in CONTENT.read_text().splitlines():
            if line.strip():
                try:
                    have.add(json.loads(line)["key"])
                except (json.JSONDecodeError, KeyError):
                    pass

    fetchable = [r for r in rows
                 if r["kind"] in FETCHABLE and not GNEWS.match(r.get("url", ""))]
    blocked = [r for r in rows if GNEWS.match(r.get("url", ""))]
    todo = [r for r in fetchable if r["key"] not in have]

    print(f"{len(rows)} items in the last {days} days")
    print(f"  {len(fetchable)} reachable, {len(todo)} not yet read")
    print(f"  {len(blocked)} behind a Google News redirect - unreachable "
          f"without a headless browser or a scraping service")

    written = 0
    with CONTENT.open("a") as f:
        for r in todo:
            final, body = fetch(r["url"])
            if final is None:
                print(f"    ! {r['competitor'][:20]:22} {body[:60]}")
                continue
            text = article_text(body)[:MAX_CHARS]
            if len(text) < 200:
                print(f"    ~ {r['competitor'][:20]:22} too short ({len(text)}), skipped")
                continue
            f.write(json.dumps({
                "key": r["key"], "entity_type": r.get("entity_type"),
                "competitor_id": r.get("competitor_id"),
                "competitor": r.get("competitor"), "kind": r["kind"],
                "title": r["title"], "url": final,
                "published": r.get("published"), "text": text,
            }) + "\n")
            written += 1
            print(f"    + {r['competitor'][:20]:22} {len(text):5} chars  "
                  f"{r['title'][:44]}")
    print(f"\nread {written} new items -> {CONTENT.name}")
    return written


# --------------------------------------------------------------- analysis

SYSTEM = """You are a competitive intelligence analyst for Where You At Ltd (WYA), \
a UK company selling crowd intelligence to venues and events. WYA deploys its own \
Bluetooth mesh (FlexNet) that carries crowd data independently of cellular and WiFi, \
plus a Venue Operations Centre dashboard and an app SDK. Its proven segment is \
festivals; it is pushing into conferences, exhibitions, stadiums and mass participation.

You will be given what was collected in the last day or two: changes on competitors' \
own websites, their live pricing pages, and feed items. The output goes to a \
company-wide Slack channel every morning and to a dashboard the whole company reads - \
sales, product and leadership. Write only what is worth their attention.

WHAT TO WRITE ABOUT
- Concentrate on the companies that actually compete: {COMPETING}. Other companies \
are context. Write about one of them only for a hard \
commercial event - a funding round, an acquisition, a named client win in our segment.
- NEW MOVEMENT ONLY. The reader wants what changed, not what exists. Before writing an \
item, ask: did this happen recently, or has it just resurfaced? A page that has been \
migrated, re-pathed, republished or recycled - a 2021 panel write-up served as a new \
URL, a case study from a past event re-listed, an old guide under a new locale - is \
NOT movement. Leave it out entirely. Do not write an entry to say that it is recycled; \
that is still an entry about something old. Signs of old content: dates in the text \
well before the read date, references to past events as current, pandemic-era \
language, "republished", "originally published".
- Prefer hard commercial movement over published content: a price change, a named \
client, a senior hire, a funding round, a sector entered, an acquisition, a shipped \
release. A blog post or guide earns an entry only if it reveals who they are now \
selling to or what they now claim - and then the entry is about that, not the post.
- Reading more of an article already reported - its full text, a table in it, \
the same piece at another address - is not a new development. Leave it out.
- No running counts or comparisons across days ("its fifth case study in five \
days", "the second events show this week"). Your record of earlier days is partial, \
and two such counts were wrong on 2 October. State what this item is; leave the tally out.
- A page "not seen before" on a competitor's site is not proof it was just \
published. Never write "published", "launched" or "added" for a page unless the \
page itself carries a recent date; otherwise describe what the page says. Until \
2 October the daily site read reported old pages as new (Meshh case studies from \
2024 called "published" on fifteen days).
- Old work is not news, even on a page not seen before. A case study, event \
write-up or announcement describing work from more than a year before the read date \
is not movement. Leave it out.
- A follow-up on a story already reported - more detail, another outlet, analysis \
of it - is not new. Leave it out unless the story itself moved: a deal closed or \
was blocked, terms changed, a date was set. (7 October retold the 6 October \
Informa/Clarion deal as "Detail on the deal".)
- News ABOUT a competing company that it did not make itself - a customer, partner, \
developer or journalist using, testing or reporting on its product - counts as \
movement for that company when the item itself is new. Say plainly who did it; never \
present a third party's act as the company's own.
- Do not write an entry to record that nothing happened. If a company did nothing \
worth saying, omit it. An absence is reported elsewhere; here it is noise.
- Three to six items is a good day. One strong item beats five weak ones. Zero is \
a legitimate answer.

BIG CLIENT MOVES
- Set major_client_move to true only for an item about one of our own clients that \
changes the account itself: a change of ownership (acquired, merged, sold), \
insolvency, administration or closure, the client's own event cancelled, postponed or \
not returning, a new venue or site, a new chief executive, or a tender or contract for \
crowd, visitor or venue technology. These are sent to the whole company on their own, \
so routine client news - line-ups, fixtures, ticket sales, results, share-price or \
analyst commentary, a follow-up on a move already reported - is false. Competitor and \
market items are always false.

HOW TO WRITE IT
- what_changed: the concrete fact, with a real number, name or date from the text.
- why_it_matters: what it means for WYA, stated so it stands on its own - this line \
is shown separately under "What it means for us". It must not restate the fact, and \
it must not be a throwaway ("no action follows", "worth re-reading"). If there is no \
implication worth a sentence, the item does not belong in the list.
- No filler: never "it is worth noting", "importantly", "notably", "overall".

RULES YOU MUST FOLLOW
- Ground every claim in the supplied text. If the text does not support a claim, \
do not make it. Say "not stated" rather than inferring.
- If an item says its link to a company is inferred or unconfirmed, the entry must \
say so. Never state an inferred attribution as fact.
- Never invent a commercial figure. Blank beats plausible.
- Never describe FlexNet as self-healing, self-repairing or auto-rerouting.
- Do not use a crowd-safety incident as a sales opening. You may note that it \
changes what buyers are being asked internally.
- Be specific. "Competitor activity increased" is useless. "They published a \
buyer's guide for hospitals, which is the third healthcare asset this quarter" \
is useful.
- No sales-coaching voice. Never write "lead with", "say this", "do not open with", \
"ask early", "position this as", "remember to". State what is happening and what \
it implies; the reader decides what to do with it.
- Never name an individual person, or an internal document by name (e.g. an internal \
deck or slide). Write "our own records" instead.
- Never write "AI-drafted", "not yet reviewed", "automated" or any provenance label, \
and never describe how the source was read ("headline only", "the article was not \
read"). If a headline alone cannot carry the fact, leave the item out."""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string",
                     "description": "One sentence: the single most important thing this week."},
        "items": {
            "type": "array",
            "description": "Only NEW movement that changes a decision - never recycled, "
                            "migrated or republished content, and never an entry saying "
                            "nothing happened. Omit noise. `key` must be "
                            "copied exactly from one of the supplied items - it is how this "
                            "gets matched back to its company and source, not invented.",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string",
                            "description": "Exact key of the source item this is about."},
                    "what_changed": {"type": "string"},
                    "why_it_matters": {"type": "string",
                                        "description": "State the implication. Never an "
                                        "instruction to the reader (no 'lead with', 'say "
                                        "this', 'ask early') - the reader decides what to do."},
                    "confidence": {"type": "string",
                                   "enum": ["certain", "likely", "unverified"]},
                    "major_client_move": {"type": "boolean",
                                          "description": "True only for an item about one "
                                          "of our clients that changes the account itself "
                                          "(see BIG CLIENT MOVES). False for everything "
                                          "else, including every competitor and market item."},
                },
                "required": ["key", "what_changed", "why_it_matters", "confidence",
                             "major_client_move"],
                "additionalProperties": False,
            },
        },
        "discarded": {"type": "integer",
                      "description": "How many supplied items you judged not decision-relevant."},
        "overall": {"type": "string",
                    "description": "The week's read in 2-4 sentences."},
    },
    "required": ["headline", "items", "discarded", "overall"],
    "additionalProperties": False,
}


def system_prompt():
    """SYSTEM with the competing set filled in from competitors.json.

    The list used to be typed into the prompt, and went stale the day Mapsted was
    added as the tenth company: the analyser was told to concentrate on nine.
    """
    try:
        comp = json.loads((HERE / "competitors.json").read_text())["competitors"]
    except (OSError, ValueError, KeyError):
        comp = []
    def names(level):
        return ", ".join(sorted(c["name"] for c in comp if c.get("threat") == level
                                and c.get("status", "active") == "active"))
    listed = "; ".join(f"{names(l)} ({l} threat)" for l in ("high", "medium") if names(l))
    return SYSTEM.replace("{COMPETING}", listed or "the high and medium threat companies")


RECENT_CONTEXT_DAYS = 4
# Four days was too short: on 15 Oct Henley's chief executive leaving, reported on
# 1 Oct, went to the whole company again as a "big client move" because the
# 14 Oct article about the search for a successor looked new. For every company
# in today's packet the analyser now also sees that company's last 60 days.
COMPANY_CONTEXT_DAYS = 60
COMPANY_CONTEXT_MAX = 8


def slug_of(url):
    """Last path segment, when it is long enough to identify an article.

    Short or generic segments ("pricing", "case-studies", "blog") are shared by
    unrelated pages, so they never count as a match.
    """
    seg = (url or "").split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1].lower()
    return seg if seg.count("-") >= 3 and len(seg) >= 25 else ""


def recent_findings(days=RECENT_CONTEXT_DAYS, today=None, ids=()):
    """What was already reported in the last few days, one line per finding.

    Without this the analyser had no memory: on 28 September it wrote up the
    Oceans Calling cancellation for the third day running, from a new outlet, and
    got it worse ("lost its opening day") than the entry that had already
    established it was cancelled in full. Given the recent record, it can skip a
    story already told, or say precisely what is new about it.
    """
    today = today or datetime.now(timezone.utc).date()
    cut = (today - timedelta(days=days)).isoformat()
    try:
        rev = json.loads(ANALYSIS.read_text()).get("reviewed") or []
    except (OSError, ValueError):
        return []
    long_cut = (today - timedelta(days=COMPANY_CONTEXT_DAYS)).isoformat()
    out, per_co = [], {}
    for e in sorted(rev, key=lambda e: e.get("date") or "", reverse=True):
        d, cid = e.get("date") or "", e.get("competitor_id")
        if e.get("held") or not d < today.isoformat():
            continue
        if not (d >= cut or (cid in ids and d >= long_cut
                             and per_co.get(cid, 0) < COMPANY_CONTEXT_MAX)):
            continue
        per_co[cid] = per_co.get(cid, 0) + 1
        first = re.split(r"(?<=[.!?])\s", (e.get("text") or "").strip(), 1)[0]
        out.append(f"- {d} [{cid}] {first[:300]}")
    return sorted(out)


def build_prompt(items, recent=None):
    parts = []
    for i in items:
        parts.append(
            f"--- key: {i['key']}\n"
            f"type: {i.get('entity_type','?')} | company/topic: {i.get('competitor','?')}\n"
            f"kind: {i['kind']} | published: {(i.get('published') or '')[:10]}\n"
            f"title: {i['title']}\nurl: {i['url']}\n\n{i['text']}\n")
    head = ""
    if recent:
        head = ("ALREADY REPORTED in the last few days - do not report these again. "
                "Write about one of these stories only if an item below adds a "
                "genuinely new development, and then say what is new and keep "
                "every fact already established unless a newer source corrects it:\n"
                + "\n".join(recent) + "\n\n")
    return (head + "Here are today's collected items. Analyse them.\n\n"
            + "\n".join(parts))


INBOX = HERE / "inbox.json"


def _key(*parts):
    """Stable short key. build_generated_entries() maps the model's output back to
    its source by this, so it must be reproducible within a run and unique across
    items."""
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()[:20]


_MONTHS = ("january|february|march|april|may|june|july|august|september|october|"
           "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_TRIVIAL_RE = re.compile(rf"(\d+|{_MONTHS}|yesterday|today|days?|hours?|minutes?|weeks?|ago|"
                         rf"[#*_.,:;/\-+%()\s])+", re.I)


# An app build whose release notes say nothing is not movement. On 11 October Cisco
# Spaces 1.10 ("General Improvements") went to the channel as Cisco moving, and the
# finding's only content was the app's unchanged description.
_NOTE_WORD = r"(?:general|minor|small|various|performance|stability|security)"
_NOTE_THING = r"(?:improvements?|enhancements?|bug ?fix(?:es)?|fix(?:es)?|updates?|tweaks|changes)"
_GENERIC_NOTE_RE = re.compile(
    rf"(?:{_NOTE_WORD}\s+)*{_NOTE_THING}(?:\s*(?:and|&|,)\s*(?:{_NOTE_WORD}\s+)*{_NOTE_THING})*"
    r"|we update (?:the|our) app regularly.*|thanks for using.*", re.I)


def _generic_release(notes):
    """True when every line of the notes is boilerplate ("Bug fixes and
    performance improvements"). Empty or missing notes are not judged here."""
    lines = [re.sub(r"^[\s\-\u2022*\d.)]+|[\s.!]+$", "", ln)
             for ln in (notes or "").splitlines()]
    lines = [ln for ln in lines if ln]
    return bool(lines) and all(_GENERIC_NOTE_RE.fullmatch(ln) for ln in lines)


# Old work is not news, however new the page (owner, 2026-10-12: "new things only
# and latest up to date"). A page seen for the first time whose newest year is two
# or more years back - Meshh's 2017 Formula 1 and 2019 Westfield case studies - is
# dropped before the analyser sees it. Copyright footers do not count as a date.
# ponytail: newest year only; a 2025 write-up in 2026 still reaches the analyser,
# whose brief tells it to leave out work more than a year old.
_COPYRIGHT_RE = re.compile(r"(?i)(?:\u00a9|\(c\)|copyright)\s*(?:\d{4}\s*[-\u2013]\s*)?\d{4}"
                           r"|\d{4}\s*(?:[-\u2013]\s*\d{4}\s*)?(?:\u00a9\s*)?all rights reserved")


def _old_work(text, year=None):
    year = year or datetime.now(timezone.utc).year
    found = [int(y) for y in re.findall(r"\b(20[0-3]\d|19[89]\d)\b",
                                        _COPYRIGHT_RE.sub("", text or ""))]
    return bool(found) and max(found) <= year - 2


# A blog comment widget (WaitTime's Wix posts, 13-15 Oct) "changes" every read.
_WIDGET_RE = re.compile(r"(?:#+\s*)?(?:\d+\s+)?comments?|loading(?:\.\.\.|\u2026)?|"
                        r"(?:write a comment\.*\s*)+|sort by:?", re.I)


def _trivial_line(line):
    """Only numbers, dates and punctuation - a ticking counter or a date label -
    or a comment widget's own text."""
    return bool(_TRIVIAL_RE.fullmatch(line.strip()) or _WIDGET_RE.fullmatch(line.strip()))


def inbox_items(limit=25, days=None):
    """Prompt-ready items from inbox.json - the analysis packet sitescan.py builds.

    WHY THIS EXISTS. content.jsonl only ever holds what analyse.py --read could
    fetch, and the bulk of the feed is Google News links behind a consent wall that
    render.py deliberately will not cross. Measured 2026-09-10: content.jsonl held
    65 entries whose newest publication date was 25 August, while collection ran
    every day. So the CI analysis step was being handed almost nothing and produced
    almost nothing - 5 to 9 September collected data with zero judgement written,
    and the daily Slack correctly stayed silent, which read as a quiet market when
    it actually meant no analyst had run.

    inbox.json is the packet that already produces the good analysis by hand. It
    carries three kinds of material, and all three become items here:

      items[]        collected feed items (headline, publisher, url)
      site_changes   what actually changed on each competitor's own website,
                     line by line - the richest signal, and the one that produced
                     every worthwhile entry written on 10 September
      pricing        an observed rate read off their live pricing page, next to
                     the rate we last recorded

    Anything already carrying a written judgement is skipped, so a re-run does not
    re-analyse the same movement.
    """
    if not INBOX.exists():
        return []
    try:
        box = json.loads(INBOX.read_text())
    except (json.JSONDecodeError, ValueError) as e:
        print(f"inbox.json is not readable JSON: {e}", file=sys.stderr)
        return []

    # sitescan stores the full text of every page it read. A new page announced by
    # URL alone is unjudgeable - the model would be guessing from a slug - so the
    # text is pulled back in here. This is the same indirection the merge uses:
    # facts come from the corpus, never from anything the model restates.
    corpus = {}
    corpus_dir = HERE / "sitecorpus"
    if corpus_dir.is_dir():
        for f in corpus_dir.glob("*.json"):
            try:
                doc = json.loads(f.read_text())
            except (json.JSONDecodeError, ValueError):
                continue
            for pg in (doc.get("pages") or []):
                if pg.get("url") and pg.get("text"):
                    corpus[pg["url"].rstrip("/")] = pg["text"]

    def page_text(url, fallback=""):
        return corpus.get((url or "").rstrip("/"), fallback)

    done = set(box.get("already_analysed") or [])
    # The same article can live at more than one address: Mapsted's hospital cost
    # post was analysed at wordpress.mapsted.com/<slug> on 28 September and sent
    # again from mapsted.com/blog/<slug> on the 29th. Match on the article slug
    # too, so a mirror of something already judged is skipped.
    done_slugs = {slug_of(u) for u in done} - {""}

    def seen(u):
        return bool(u) and (u in done or slug_of(u) in done_slugs)
    # A page (not an article) can change again after it was judged. Skipping any
    # url ever judged meant Pointr's or Purple's homepage could never be looked at
    # again; skip a page change only if it was judged on or after this read.
    last_judged = {}
    try:
        for a in json.loads(ANALYSIS.read_text()).get("reviewed", []):
            if a.get("url"):
                last_judged[a["url"]] = max(last_judged.get(a["url"], ""), a.get("date", ""))
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    cut = None
    if days:
        cut = (datetime.now(timezone.utc) - timedelta(days=days)) \
            .isoformat(timespec="seconds")

    site, pricing, feed = [], [], []

    # 1. Website changes. One item per changed page, carrying the gained lines -
    #    this is the competitor describing its own move in its own words.
    for cid, sc in (box.get("site_changes") or {}).items():
        name = sc.get("name") or cid
        for pg in (sc.get("changed_pages") or [])[:4]:
            # A counter ticking (Pointr "### 93" -> "### 97") or a relative date
            # becoming a real one (Apple "3 days ago" -> "29 September 2026") is
            # not movement. Lines that are only numbers and dates are dropped.
            gained = [g for g in (pg.get("gained") or []) if len(g.strip()) > 2
                      and not _trivial_line(g)]
            if not gained or last_judged.get(pg.get("url"), "") >= (sc.get("read") or "9999"):
                continue
            site.append({
                "key": _key("site", cid, pg["url"], sc.get("read")),
                "entity_type": "competitor", "competitor_id": cid,
                "competitor": name, "kind": "page_change",
                "title": f"{name} website changed: {pg['url']}",
                "url": pg["url"],
                "published": (sc.get("read") or "") + "T12:00:00+00:00",
                "text": (f"Their page changed between {sc.get('previous_read')} and "
                         f"{sc.get('read')}. Lines now on the page that were not "
                         f"there before:\n- " + "\n- ".join(gained[:40])
                         + (f"\n\nLines removed:\n- "
                            + "\n- ".join((pg.get("lost") or [])[:12])
                            if pg.get("lost") else "")),
            })
        for np in (sc.get("new_pages") or [])[:4]:
            url = np.get("url") if isinstance(np, dict) else np
            if not url or seen(url):
                continue
            body = page_text(url, (np.get("text") or "") if isinstance(np, dict) else "")
            if not body.strip() or _old_work(body):
                # No text means no judgement is possible. A slug is not evidence,
                # and an item the model can only guess at is worse than one absent.
                continue
            site.append({
                "key": _key("newpage", cid, url),
                "entity_type": "competitor", "competitor_id": cid,
                "competitor": name, "kind": "new_page",
                "title": f"{name} page not seen before: {url}",
                "url": url,
                "published": (sc.get("read") or "") + "T12:00:00+00:00",
                "text": (f"A page on their site that was not in their sitemap or "
                         f"links on any earlier day. That does not date it: say "
                         f"when it was published only if the page itself gives a "
                         f"date. Its content:\n{body[:MAX_CHARS]}"),
            })

    # 2. Pricing observations, which are the reason the pricing scan exists.
    for cid, pr in (box.get("pricing") or {}).items():
        obs = pr.get("observed") or {}
        if not obs or obs.get("url") in done:
            continue
        # The same 22 September reading was re-sent every day and took two of the
        # 25 slots; a reading is news only within the window it was taken in.
        if cut and (obs.get("seen") or "") < cut[:10]:
            continue
        figs = obs.get("figures") or []
        pricing.append({
            "key": _key("price", cid, obs.get("seen"), obs.get("url")),
            "entity_type": "competitor", "competitor_id": cid,
            "competitor": pr.get("name") or cid, "kind": "page_change",
            "title": f"{pr.get('name') or cid} pricing page, read {obs.get('seen')}",
            "url": obs.get("url") or "",
            "published": (obs.get("seen") or "") + "T12:00:00+00:00",
            "text": (f"Read from their live pricing page via {obs.get('via')}.\n"
                     f"Observed headline: {obs.get('headline') or 'none stated'}\n"
                     f"Quote-only: {obs.get('quote_only')}\n"
                     f"Figures on the page: {'; '.join(figs[:12]) or 'none'}\n"
                     f"What we last recorded: {pr.get('written_headline') or 'nothing'}"),
        })

    # 3. Feed items. The article text when --read got it, otherwise the headline -
    #    and the headline case is said plainly, so the model never treats a
    #    headline as if it had read the article.
    read_text = {}
    if CONTENT.exists():
        for line in CONTENT.read_text().splitlines():
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if r.get("text"):
                read_text[r.get("key")] = r["text"]
    # collect.py diffs watched pages whole, so one edit to a site-wide menu shows
    # up as "pricing page changed", "products page changed"... with the same new
    # line. On 4 October that made the analyser describe Purple's unchanged
    # pricing page as new. Keep one item and say what it is.
    def _first_gained(t):   # "pricing page changed — line one; line two" -> "line one"
        return t.split(" \u2014 ", 1)[1].split("; ")[0] if " \u2014 " in t else None
    raw_items, sitewide = [], {}
    for it in (box.get("items") or []):
        t = it.get("title") or ""
        if it.get("kind") == "page_change" and " \u2014 " in t:
            sitewide.setdefault((it.get("competitor_id"), it.get("seen_at", "")[:10],
                                 _first_gained(t)), []).append(it)
    for it in (box.get("items") or []):
        t = it.get("title") or ""
        grp = sitewide.get((it.get("competitor_id"), it.get("seen_at", "")[:10],
                            _first_gained(t))) or []
        if len(grp) >= 3:
            if it is not grp[0]:
                continue
            pages = ", ".join(g["title"].split(" page changed", 1)[0] for g in grp)
            it = {**it, "title": f"{it.get('competitor')} site-wide menu or footer "
                                 f"changed — {_first_gained(t)}",
                  "sitewide_note": (f"The same new line appeared at once on {len(grp)} "
                                    f"of their watched pages ({pages}): a site-wide "
                                    f"menu or footer edit, not a change to any one "
                                    f"page's content.\n")}
        raw_items.append(it)
    for it in raw_items:
        if seen(it.get("url")) or seen(it.get("gnews_url")):
            continue
        if it.get("kind") == "app_release" and _generic_release(it.get("release_notes")):
            continue
        if cut and (it.get("published") or "") and it["published"] < cut:
            continue
        body = read_text.get(it.get("key"))
        if it.get("kind") == "new_page" and body and _old_work(body):
            continue        # collect.py records new_page only for a URL absent from the site's sitemap
        # the day before - real evidence of when it was added. Without saying so,
        # the checker (rightly) holds "published" on an undated page.
        dated = (f"New in their sitemap: this URL was not listed on the previous "
                 f"day's read and was first listed on {it['seen_at'][:10]}. That dates "
                 f"when the page went up, not when the work it describes happened.\n"
                 if it.get("kind") == "new_page" and it.get("seen_at") else "")
        dated = (it.pop("sitewide_note", "") if "sitewide_note" in it else "") + dated
        feed.append({**it, "key": it.get("key") or _key("feed", it.get("url")),
                     "text": dated + (f"Article text, read from {it.get('publisher') or 'the publisher'}:\n"
                              f"{body[:MAX_CHARS]}" if body else
                              f"Headline only, the article itself was not read.\n"
                              f"Headline: {it.get('title')}\n"
                              f"Publisher: {it.get('publisher') or 'unknown'}")})

    # Website changes and pricing first: they are what the competitor actually did,
    # not what someone wrote about them. Feed items fill whatever budget is left.
    #
    # Then round-robin by company. Straight concatenation let one busy site fill the
    # packet - Cisco published enough pages in a day to take most of the budget,
    # which is the same failure that put three Purple items in one Slack message.
    # A competitor that moved once still gets read.
    # Newest first within each company. Oldest-first let yesterday's 18 Purple
    # glossary pages fill its slots on 4 October while that day's 14 new pages,
    # among them "People counting WiFi vs camera", never reached the analyser.
    feed.sort(key=lambda i: i.get("seen_at") or i.get("published") or "", reverse=True)
    by_co = {}
    for it in pricing + site + feed:
        by_co.setdefault(it["competitor_id"], []).append(it)
    ordered, exhausted = [], False
    while not exhausted and len(ordered) < limit:
        exhausted = True
        for cid in list(by_co):
            if by_co[cid]:
                ordered.append(by_co[cid].pop(0))
                exhausted = False
                if len(ordered) >= limit:
                    break
    return ordered[:limit]


def analyse(days=7, limit=25):
    # inbox.json is the preferred input. content.jsonl is kept as the fallback so
    # a checkout without a sitescan run still analyses whatever was read.
    # Feed items older than the window never reach the model. With no window, a
    # Reddit thread published fifteen months earlier was in the 10 September
    # packet. The site read and the pricing scan are current by construction -
    # their date is the read date - so the window applies to feed items only.
    items = inbox_items(limit=limit, days=FEED_WINDOW_DAYS)
    if items:
        kinds = {}
        for i in items:
            k = ("website" if "website changed" in i["title"]
                 else "new page" if "new page" in i["title"]
                 else "pricing" if "pricing page" in i["title"] else "feed")
            kinds[k] = kinds.get(k, 0) + 1
        print(f"analysing {len(items)} items from inbox.json "
              f"({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))})")
    else:
        if not CONTENT.exists():
            raise SystemExit(
                "no inbox.json and no content.jsonl - run `python3 sitescan.py "
                "--daily` (preferred) or `python3 analyse.py --read` first")
        cut = (datetime.now(timezone.utc) - timedelta(days=days)) \
            .isoformat(timespec="seconds")
        for line in CONTENT.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (r.get("published") or "") >= cut or not r.get("published"):
                items.append(r)
        items = items[:limit]
        print(f"inbox.json empty or missing - fell back to content.jsonl "
              f"({len(items)} items)")
    if not items:
        raise SystemExit("nothing to analyse: inbox.json is empty and content.jsonl "
                         f"has nothing in the last {days} days")

    try:
        import anthropic
    except ImportError:
        raise SystemExit(
            "the anthropic SDK is not installed.\n"
            "  pip3 install --user anthropic\n"
            "then set a credential (ANTHROPIC_API_KEY, or `ant auth login`).")

    if not (os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("no ANTHROPIC_API_KEY in the environment - relying on an "
              "`ant auth login` profile if one exists", file=sys.stderr)

    client = anthropic.Anthropic()
    print(f"analysing {len(items)} items with {MODEL}...")

    # Stream: a long prompt with adaptive thinking can exceed the non-streaming
    # HTTP timeout. get_final_message() gives the assembled response.
    with client.messages.stream(
        model=MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": system_prompt(),
                 "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        output_config={"effort": "high",
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": build_prompt(items, recent_findings(
            ids={i.get("competitor_id") for i in items}))}],
    ) as stream:
        msg = stream.get_final_message()

    if msg.stop_reason == "refusal":
        raise SystemExit(f"request refused: "
                         f"{getattr(msg.stop_details, 'category', None)}")

    text = next((b.text for b in msg.content if b.type == "text"), "")
    data = json.loads(text)
    data["_meta"] = {
        "model": msg.model,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items_supplied": len(items),
        "window_days": days,
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
    }
    existing = {}
    if ANALYSIS.exists():
        try:
            existing = json.loads(ANALYSIS.read_text())
        except json.JSONDecodeError:
            existing = {}
    generated, skipped = build_generated_entries(data.get("items", []), items, msg.model)
    if skipped:
        print(f"  ! {skipped} model item(s) referenced a key not in the supplied set, "
              f"or had no text - dropped rather than written with a null competitor_id")
    check_claims(client, generated, recent_findings())
    merged, kept, replaced = merge_generated(existing, generated)
    merged.setdefault("meta", {})["updated"] = datetime.now(timezone.utc).date().isoformat()
    data["_meta"]["check_input_tokens"] = CHECK_USAGE["input_tokens"]
    data["_meta"]["check_output_tokens"] = CHECK_USAGE["output_tokens"]
    merged["_meta"] = data["_meta"]
    _cost = ((data["_meta"]["input_tokens"] + CHECK_USAGE["input_tokens"]) * PRICE_IN
             + (data["_meta"]["output_tokens"] + CHECK_USAGE["output_tokens"]) * PRICE_OUT) / 1e6
    print(f"API usage this run: analysis {data['_meta']['input_tokens']:,} in / "
          f"{data['_meta']['output_tokens']:,} out; claim check "
          f"{CHECK_USAGE['input_tokens']:,} in / {CHECK_USAGE['output_tokens']:,} out; "
          f"about ${_cost:.2f} at ${PRICE_IN}/${PRICE_OUT} per million")
    ANALYSIS.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
    print(f"\n{data['headline']}\n")
    print(f"kept {kept} existing entries, replaced {replaced} from today's earlier "
          f"run, added {len(generated)} generated")


def merge_generated(existing, generated, today=None):
    """(merged_doc, kept_count, replaced_count). Pure, so it is selftested.

    Every earlier generated entry is KEPT. The previous merge kept only the
    hand-written entries and dropped every generated one before appending the
    day's fresh batch, so generated analysis never accumulated: each morning's run
    erased the morning before. Measured 2026-09-14: four data commits since the
    CI analyser took over, and analysis.json held generated entries for exactly
    one date. The 10 September entries - the ones that produced the first proper
    Slack message - were gone.

    The one thing a re-run may replace is its own day: two runs on the same date
    would otherwise file the same movement twice. Hand-written entries are never
    touched here, whatever their date.
    """
    today = today or datetime.now(timezone.utc).date().isoformat()
    kept, replaced = [], 0
    for r in (existing.get("reviewed") or []):
        if r.get("generated") and r.get("date") == today:
            replaced += 1
            continue
        kept.append(r)
    merged = dict(existing)
    merged["reviewed"] = kept + generated
    return merged, len(kept), replaced


# A finding about the market must not narrate how we read it. On 6 October an
# Olympia finding went out carrying "Headline only; the article was not read" -
# a provenance note, which the owner had removed everywhere on 9 September.
# Deletion only: the sentence goes, nothing is rewritten.
_PROCESS_NOTE = re.compile(r"headline only|headline-only|(article|page|text)( itself)? "
                           r"(was|were|could) not (be )?read|not read in full", re.I)


# The analyser says so itself when it is retelling a story: on 7 October it wrote
# "Detail on the Informa/Clarion deal..." the day after reporting the deal. More
# detail on a story already told is not new movement; the item is dropped.
_FOLLOW_UP = re.compile(r"\s*(more |further |new )?(detail|details|update|follow-up|"
                        r"follow up)\s+(on|about|from|to)\b", re.I)


def strip_process_notes(text):
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(p for p in parts if not _PROCESS_NOTE.search(p))


def build_generated_entries(model_items, source_items, model_name):
    """(entries, skipped_count). Pure, so selftest can catch what a live run cannot
    be run to check in CI.

    The model's job is judgement, not bookkeeping: `competitor_id`, `url` and
    `sources` are looked up from the source item by `key`, never taken from anything
    the model wrote. A schema that asked the model to also emit
    competitor_id/text/sources previously went unused because the merge read
    different field names than the schema declared - every generated entry had
    competitor_id=None, which reviewedFor() in index.html silently drops
    (`if(!a.competitor_id) continue`), and an empty text field. That bug shipped
    unnoticed because it needed a live API key to trigger, which nothing had until
    2026-08-26 - hence this being a pure function selftest can exercise offline."""
    by_key = {it["key"]: it for it in source_items if it.get("key")}
    entries, skipped = [], 0
    for it in model_items:
        src = by_key.get(it.get("key"))
        if not src:
            skipped += 1
            continue
        what = (it.get("what_changed") or "").strip()
        why = (it.get("why_it_matters") or "").strip()
        text = f"{what} {why}".strip() if what and why else (what or why)
        text = strip_process_notes(text)
        if not text or _FOLLOW_UP.match(what or text):
            skipped += 1
            continue
        # The implication, kept separately so Slack never has to guess where the
        # fact ends: on 10-11 October its guess put fact sentences under "What it
        # means for us". apply_checks() keeps it in step with any deletion.
        means = strip_process_notes(why) if what and why else None
        if means is not None and not text.endswith(means):
            means = None
        url = src.get("url") or ""
        source_line = (f"{src.get('competitor', 'source')} — "
                       f"{src.get('title', '')}, {(src.get('published') or '')[:10]}"
                       + (f" — {url}" if url else ""))
        entries.append({
            "date": datetime.now(timezone.utc).date().isoformat(),
            "competitor_id": src.get("competitor_id"),
            "url": url or None,
            "text": text,
            "sources": [source_line],
            "confidence": it.get("confidence"),
            **({"means": means} if means is not None else {}),
            # A big move by one of our own clients sends Slack on its own (owner,
            # 12 Oct). Only ever true for a client item, whatever the model says.
            **({"major": True} if it.get("major_client_move")
               and src.get("entity_type") == "client" else {}),
            "generated": True,
            "model": model_name,
            # Transient: what the analyser read for this item AND every other item
            # it was given on the same company or topic - a finding may fairly
            # combine several articles on one story. Stripped before saving.
            "_source_text": "\n\n".join(
                f"{s2.get('title', '')}\n{s2.get('published') or ''}\n{s2.get('text') or ''}"
                for s2 in [src] + [o for o in source_items if o is not src
                                   and o.get("competitor_id") == src.get("competitor_id")]
            )[:20000],
        })
    return entries, skipped


# --------------------------------------------------------------- claim check
#
# Every finding is checked against its source before it can reach Slack or the
# dashboard. Two layers, because string matching alone would have caught neither
# real error found by hand: "55,000" WAS on the page (the error was "per day"),
# and the invented VenuIQ "release history" contained no number at all.
#
#   1. figures and quotes   every figure, year and quoted phrase must appear in
#                           the source. Deterministic; catches invented numbers.
#   2. a second model pass  reads each finding beside its source and lists what
#                           the source does not support; catches misreadings.
#
# A finding that fails is corrected to what the source says and re-checked; if it
# cannot be made clean it is HELD - kept in analysis.json with the reason, sent
# nowhere, shown nowhere.

FIGURE_RE = re.compile(
    r"[£$€]\s?\d[\d,.]*(?:\s?(?:k|m|bn|million|billion)\b)?"
    r"|\b\d[\d,.]*(?:\s?[-–]\s?\d[\d,.]*)?\s?(?:%|×|x\b)"
    r"|\b\d{1,3}(?:,\d{3})+\+?"
    r"|\b(?:19|20)\d{2}\b"
    r"|\b\d+(?:\.\d+)?(?:\s?[-–]\s?\d+(?:\.\d+)?)?\s?(?:hours?|minutes?|mins?|m²|sq ?ft|metres?|meters?|m\b|km)"
    r"|\b\d{3,}\b", re.I)
QUOTE_RE = re.compile(r'["“]([^"”]{6,160})["”]')


def _norm(s):
    s = (s or "").lower()
    for a, b in (("–", "-"), ("—", "-"), ("’", "'"), ("‘", "'"),
                 ("“", '"'), ("”", '"'), ("×", "x"), (" ", " ")):
        s = s.replace(a, b)
    s = re.sub(r"(?<=\d),(?=\d{3})", "", s)          # 55,000 -> 55000
    s = re.sub(r"(?<=\d),(?=\d{1,2}(?!\d))", ".", s)  # 15,5 % -> 15.5 % (decimal comma)
    s = re.sub(r"\s*-\s*", "-", s)                   # 3 - 5 -> 3-5
    return re.sub(r"\s+", " ", s)


def _range_in(core, src):
    """'1-5' is supported by "between 1 m and 5 m" / "1 to 5" in the source
    (Mapsted, 14 Oct: a true finding held on this)."""
    a, _, b = core.partition("-")
    if not (a and b):
        return False
    return bool(re.search(rf"(?<![\d.]){re.escape(a)}(?:\s?[^\s\d]+)?\s+(?:and|to)\s+"
                          rf"{re.escape(b)}(?![\d.])", src))


def unsupported_figures(text, source):
    """Figures and quoted phrases in `text` that appear nowhere in `source`."""
    src = _norm(source)
    src_digits = re.sub(r"\s+", "", src)
    out = []
    for m in FIGURE_RE.finditer(text):
        core = re.sub(r"[^0-9.\-]", "", _norm(m.group(0))).strip(".-")
        if core and core not in src_digits and not _range_in(core, src):
            out.append(m.group(0).strip())
    # Quotes are checked in the factual part only. A quote in the closing
    # "what it means" sentence is our own framing - on 2 October "we already have
    # WiFi", the objection WYA hears, held a true and important Purple finding.
    sents = _split_sentences(text)
    fact = " ".join(sents[:-1]) if len(sents) > 1 else text
    for m in QUOTE_RE.finditer(fact):
        if _norm(m.group(1)).strip(" .,") not in src:
            out.append(f'"{m.group(1)}"')
    return out


VERIFY_SYSTEM = """You are a fact-checker for a competitive intelligence feed. \
Each finding below was written from the source text shown with it. Check every \
factual assertion in the finding against that source.

Flag a claim when the source does not support it as written, including:
- a number applied differently (a total reported as "per day", "nearly 55,000" as "55,000 per day")
- an event described differently ("cancelled in full" vs "lost its opening day")
- features, history, dates, names or events that are not in the source
- over-generalisation (calling four events "sold-out" when the source says one was)
- "published", "launched", "added" or "new" said of a page the source does not \
date. A page "not seen before" is undated: a 2024 case study once read as new.

Do NOT flag:
- the implication sentence(s) about what it means for WYA - that is judgement - \
unless it asserts a fact that is not in the source. A prediction or condition \
stated as fact inside the implication IS a factual assertion: "cellular load at \
Greenwich will be worse than ever" must be flagged when the source says nothing \
about cellular load
- expanding a standard abbreviation (GSA -> General Services Administration)

Judge each finding against its OWN source text only. A reference to anything \
not in that source - an earlier event, another company's page, a previous day's \
finding, a running count - is unsupported and must be flagged, however plausible.

Flag only what you can check: a claim is unsupported when the source lacks it or \
says something different. Do not flag a fair summary of what the source does say, \
and do not assume the source contains things you cannot see in it.

For each finding return supported (true only if nothing is flagged) and the list \
of unsupported claims, each copied EXACTLY, word for word, from the finding - the \
copy is used to locate the sentence it came from."""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "supported": {"type": "boolean"},
            "unsupported": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["index", "supported", "unsupported"],
        "additionalProperties": False}}},
    "required": ["results"],
    "additionalProperties": False,
}


VERIFY_ADVERSARIAL = VERIFY_SYSTEM + """

Second reviewer. Assume each finding contains at least one claim that is wrong, \
and hunt for it: go through every number, date, name, event, product, quoted \
phrase and characterisation one at a time and find it in the source. Pay most \
attention to how a fact is CHARACTERISED (per day vs in total, cancelled vs \
shortened, an events show vs a workplace show, the only evidence vs some evidence). \
If, after checking every claim, nothing is wrong, say so - do not invent a flag."""


CHECK_USAGE = {"input_tokens": 0, "output_tokens": 0}   # summed per run, logged
CHECK_BATCH = 4          # findings per checking call
CHECK_MAX_TOKENS = 32000  # the 2 October suite run lost a whole pass at 8000:
                          # thinking used the budget and the reply came back empty


def _one_pass(client, system, entries, offset=0):
    """{global_index: result} for one batch; raises if no usable answer after a retry."""
    parts = [f"=== FINDING {i}\n{e['text']}\n\n--- SOURCE {i}\n"
             f"{(e.get('_source_text') or '')[:12000]}\n" for i, e in enumerate(entries)]
    last = None
    for _attempt in range(2):
        try:
            with client.messages.stream(
                model=MODEL, max_tokens=CHECK_MAX_TOKENS,
                system=[{"type": "text", "text": system}],
                thinking={"type": "adaptive"},
                output_config={"effort": "high",
                               "format": {"type": "json_schema", "schema": VERIFY_SCHEMA}},
                messages=[{"role": "user", "content": "\n".join(parts)}],
            ) as stream:
                msg = stream.get_final_message()
            CHECK_USAGE["input_tokens"] += msg.usage.input_tokens
            CHECK_USAGE["output_tokens"] += msg.usage.output_tokens
            text = next((b.text for b in msg.content if b.type == "text"), "")
            if not text.strip():
                raise ValueError(f"empty reply (stop_reason={msg.stop_reason})")
            got = {r["index"]: r for r in json.loads(text).get("results", [])}
            if set(got) != set(range(len(entries))):
                raise ValueError(f"answered {sorted(got)} of {len(entries)} findings")
            return {offset + i: r for i, r in got.items()}
        except Exception as e:
            last = e
    raise last


def merge_verdicts(a, b):
    """Union of two independent passes: a claim flagged by either is flagged."""
    out = {}
    for i in set(a) | set(b):
        x, y = a.get(i, {}), b.get(i, {})
        flags = list(dict.fromkeys((x.get("unsupported") or []) + (y.get("unsupported") or [])))
        out[i] = {"index": i, "supported": not flags and bool(x) and bool(y),
                  "unsupported": flags}
    return out


def model_check(client, entries, recent=None):
    """({index: result}, unchecked_indexes) from TWO independent passes, unioned.

    A finding counts as checked only when BOTH passes returned a verdict for it.
    Any finding a pass could not cover - API error, empty reply, a batch it
    answered only in part - is returned as unchecked, and the caller holds it:
    a transient error may shorten the morning message, it may never send a claim
    nothing has checked. (Earlier, a crashed pass fell back to one reviewer, and
    the suite showed one reviewer letting a known invented claim through.)"""
    verdicts, unchecked = {}, set()
    for off in range(0, len(entries), CHECK_BATCH):
        batch = entries[off:off + CHECK_BATCH]
        got = []
        for name, system in (("standard", VERIFY_SYSTEM), ("adversarial", VERIFY_ADVERSARIAL)):
            try:
                got.append(_one_pass(client, system, batch, off))
            except Exception as e:
                print(f"  ! claim check ({name} pass, findings {off}-{off + len(batch) - 1}) "
                      f"failed: {type(e).__name__} {e}", file=sys.stderr)
        if len(got) == 2:
            verdicts.update(merge_verdicts(*got))
        else:
            unchecked.update(range(off, off + len(batch)))
    return verdicts, unchecked


def _split_sentences(text):
    return [x for x in re.split(r'(?<=[.!?])\s+(?=[A-Z"\u201c(])', text.strip()) if x]


def apply_checks(entries, verdicts, model_ran=True):
    """Mutates entries: keeps, trims or holds each one. Pure given `verdicts`, so
    the decision logic is selftested without a model call.

    Deletion only. The checker may not write prose: on its first live run it
    "corrected" a true sentence into a false one ("alongside Retail..." - the page
    cites nothing alongside). So a flagged claim removes the sentence it sits in,
    and nothing is ever added. A flag in the first sentence - the fact itself -
    holds the finding; a flag that cannot be located in any sentence holds it too,
    because it cannot be removed precisely.
    """
    for i, e in enumerate(entries):
        src = e.get("_source_text") or ""
        v = verdicts.get(i)
        flagged = unsupported_figures(e["text"], src)
        if v and not v.get("supported"):
            flagged += [c for c in v.get("unsupported") or [] if c not in flagged]
        if not flagged:
            e["verified"] = "checked" if v else "figures only"
            continue
        # A flagged aside set off by a dash pair or brackets is cut out on its own -
        # still deletion only. On 2 October "its fifth new case study in five days"
        # (wrong: eight since 25 Sept) held a Meshh finding whose facts were all true.
        text, cut = e["text"], []
        means = e.get("means")
        for c in flagged:
            core = c.strip(' ."\u201c\u201d')
            for pat in (r"\s+[-\u2013\u2014]\s+" + re.escape(core) + r"\s+[-\u2013\u2014](?=\s)",
                        r"\s*\(\s*" + re.escape(core) + r"\s*\)"):
                new_text, n = re.subn(pat, "", text, count=1, flags=re.I)
                if n:
                    text = new_text
                    cut.append(c)
                    if means is not None:
                        means = re.sub(pat, "", means, count=1, flags=re.I)
                    break
        flagged_left = [c for c in flagged if c not in cut]
        if cut and not flagged_left:
            e["text"] = text
            e["verified"] = "trimmed"
            e["removed_claims"] = cut
            _set_means(e, means)
            continue
        sents = _split_sentences(text)
        # How many of the trailing sentences are the implication, if we know.
        n_means = len(_split_sentences(means)) if means else 0
        if means is not None and (not text.endswith(means) or n_means >= len(sents)
                                  or " ".join(sents[len(sents) - n_means:]) != means):
            means, n_means = None, 0
        hit, lost = set(), []
        flagged = flagged_left
        for c in flagged:
            key = _norm(c).strip(' ."\u201c\u201d')
            where = [j for j, x in enumerate(sents) if key and key in _norm(x)]
            if not where:
                lost.append(c)
            hit.update(where)
        keep = [x for j, x in enumerate(sents) if j not in hit]
        if lost or 0 in hit or not keep:
            e["held"] = True
            e["held_reason"] = "unsupported by source: " + "; ".join(cut + flagged)[:400]
        else:
            e["text"] = " ".join(keep)
            e["verified"] = "trimmed"
            e["removed_claims"] = cut + flagged
            _set_means(e, None if means is None else " ".join(
                x for j, x in enumerate(sents)
                if j not in hit and j >= len(sents) - n_means))
    return entries


def _set_means(e, means):
    """Record the implication after a deletion; "" when the check removed all of
    it (notify.py then keeps the finding out of Slack), absent when unknown."""
    if means is None or not e["text"].endswith(means):
        e.pop("means", None)
    else:
        e["means"] = means.strip()


def check_claims(client, entries, recent=None):
    if client:
        verdicts, unchecked = model_check(client, entries)
    else:
        verdicts, unchecked = {}, set(range(len(entries)))
    apply_checks(entries, verdicts)
    for i in unchecked:
        if not entries[i].get("held"):
            entries[i]["held"] = True
            entries[i]["held_reason"] = ("could not be fully checked: both checking "
                                         "passes must run and one did not")
    for e in entries:
        e.pop("_source_text", None)
    n_c = sum(1 for e in entries if e.get("verified") == "trimmed")
    n_h = sum(1 for e in entries if e.get("held"))
    print(f"claim check: {len(entries) - n_c - n_h} clean, {n_c} trimmed, {n_h} held")
    for e in entries:
        if e.get("held"):
            print(f"  HELD {e['competitor_id']}: {e['held_reason'][:160]}")
        elif e.get("removed_claims"):
            print(f"  trimmed {e['competitor_id']}: removed {e['removed_claims']}")
    return entries
    print(f"kept {len(data['items'])}, discarded {data['discarded']}")
    print(f"wrote {ANALYSIS.name}  "
          f"({msg.usage.input_tokens} in / {msg.usage.output_tokens} out)")
    return data


def selftest():
    t = article_text(
        "<html><head><style>a{}</style></head><body><nav>menu</nav>"
        "<script>bad()</script><p>Real &amp; useful prose here.</p>"
        "<footer>junk</footer></body></html>")
    assert "bad()" not in t and "menu" not in t and "junk" not in t, t
    assert "Real & useful prose here." in t, t

    # inbox.json is the analysis input. These guard the bridge between the packet
    # sitescan.py writes and the shape build_generated_entries() needs, because a
    # mismatch there shipped silently once before (see the schema/merge bug) and
    # only surfaced when a real key was set.
    import tempfile, os as _os
    _real = globals()["INBOX"]
    _realcorpus = HERE / "sitecorpus"
    try:
        with tempfile.TemporaryDirectory() as _d:
            _f = Path(_d) / "inbox.json"
            _f.write_text(json.dumps({
                "already_analysed": ["https://done.test/seen"],
                "items": [{"key": "k1", "competitor_id": "acme", "competitor": "Acme",
                           "kind": "news", "title": "Acme did a thing",
                           "url": "https://news.test/1", "published": "2026-09-10T00:00:00Z"},
                          {"key": "k2", "competitor_id": "acme", "competitor": "Acme",
                           "kind": "news", "title": "Already judged",
                           "url": "https://done.test/seen", "published": "2026-09-10T00:00:00Z"}],
                "site_changes": {"acme": {"name": "Acme", "read": "2026-09-10",
                                          "previous_read": "2026-09-09",
                                          "changed_pages": [{"url": "https://acme.test/pricing",
                                                             "gained": ["Now 9 pounds per head"],
                                                             "lost": ["Was 8 pounds"]}],
                                          "new_pages": ["https://acme.test/no-text"]}},
                "pricing": {"acme": {"name": "Acme", "written_headline": "8 pounds",
                                     "observed": {"seen": "2026-09-10", "via": "browser",
                                                  "url": "https://acme.test/pricing",
                                                  "headline": "9 pounds per head",
                                                  "quote_only": False,
                                                  "figures": ["9 pounds per head"]}}},
            }), encoding="utf-8")
            globals()["INBOX"] = _f
            got = inbox_items(limit=25)

            # An item already carrying a judgement is not re-analysed.
            assert not any(i["url"] == "https://done.test/seen" for i in got), got

            # A page change carries the lines that actually changed, both ways.
            chg = [i for i in got if "website changed" in i["title"]]
            assert chg and "Now 9 pounds per head" in chg[0]["text"], chg
            assert "Was 8 pounds" in chg[0]["text"], "removed lines must be shown too"

            # A pricing observation reaches the model next to what we last recorded,
            # which is the comparison that makes it worth judging at all.
            pr = [i for i in got if "pricing page" in i["title"]]
            assert pr and "9 pounds per head" in pr[0]["text"], pr
            assert "8 pounds" in pr[0]["text"], "must show the rate we had on record"

            # A new page with no text anywhere is dropped, not sent as a bare slug.
            assert not any("no-text" in (i["url"] or "") for i in got), \
                "an unjudgeable URL must not be sent as if it were evidence"

            # Every item must carry what the merge looks up by key.
            for i in got:
                for f in ("key", "competitor_id", "competitor", "title", "url", "text"):
                    assert i.get(f) is not None, (f, i)
            assert len({i["key"] for i in got}) == len(got), "keys must be unique"

            # The merge must accept these items unchanged.
            ents, skipped = build_generated_entries(
                [{"key": got[0]["key"], "what_changed": "They moved.",
                  "why_it_matters": "It costs us.", "confidence": "likely"}],
                got, "test-model")
            assert skipped == 0 and ents and ents[0]["competitor_id"] == "acme", (ents, skipped)
            assert ents[0]["text"] == "They moved. It costs us."

            # Feed items outside the window are excluded; site reads never are.
            _f.write_text(json.dumps({
                "items": [{"key": "old", "competitor_id": "acme", "competitor": "Acme",
                           "kind": "news", "title": "Ancient", "url": "https://n.test/old",
                           "published": "2024-01-01T00:00:00+00:00"},
                          {"key": "sm", "competitor_id": "acme", "competitor": "Acme",
                           "kind": "new_page", "title": "Compare", "url": "https://n.test/cmp",
                           "seen_at": "2026-10-01T07:16:50+00:00"},
                          {"key": "now", "competitor_id": "acme", "competitor": "Acme",
                           "kind": "news", "title": "Fresh", "url": "https://n.test/new",
                           "published": datetime.now(timezone.utc).isoformat()}],
                "site_changes": {"acme": {"name": "Acme", "read": "2020-01-01",
                                          "previous_read": "2019-12-31",
                                          "changed_pages": [{"url": "https://acme.test/p",
                                                             "gained": ["A line"]}]}},
            }))
            _w = inbox_items(limit=25, days=3)
            # A URL new to the sitemap carries the date it was first listed, so
            # "published" about it can be checked rather than held.
            _sm = [i for i in _w if i["key"] == "sm"]
            assert _sm and "first listed on 2026-10-01" in _sm[0]["text"], _sm
            _urls = {i["url"] for i in _w}
            assert "https://n.test/old" not in _urls, "an old feed item must not reach the model"
            assert "https://n.test/new" in _urls
            assert "https://acme.test/p" in _urls, "site reads are dated by the read, never windowed"

            # 4 October: six "page changed" items carrying the same new menu line
            # are one site-wide edit; a 12-day-old pricing reading is not news;
            # within a company the newest items come first.
            _now = datetime.now(timezone.utc)
            _iso = lambda d: (_now - timedelta(days=d)).isoformat(timespec="seconds")
            _menu = [{"key": f"m{i}", "competitor_id": "acme", "competitor": "Acme",
                      "kind": "page_change", "seen_at": _iso(0), "url": f"https://acme.test/{p}",
                      "title": f"{p} page changed \u2014 New menu tagline; {p} extra"}
                     for i, p in enumerate(["pricing", "products", "sectors"])]
            _f.write_text(json.dumps({
                "items": [{"key": "y", "competitor_id": "acme", "competitor": "Acme",
                           "kind": "new_page", "title": "Yesterday", "seen_at": _iso(1),
                           "url": "https://acme.test/y"},
                          {"key": "t", "competitor_id": "acme", "competitor": "Acme",
                           "kind": "new_page", "title": "Today", "seen_at": _iso(0),
                           "url": "https://acme.test/t"}] + _menu,
                "pricing": {"acme": {"name": "Acme", "observed": {
                    "seen": _iso(12)[:10], "url": "https://acme.test/price"}}}}))
            _g = inbox_items(limit=25, days=3)
            _keys = [i["key"] for i in _g]
            assert _keys.index("t") < _keys.index("y"), "newest first within a company"
            assert not any(i["url"] == "https://acme.test/price" for i in _g), \
                "a stale pricing reading must not be re-sent as news"
            _mw = [i for i in _g if i["key"].startswith("m")]
            assert len(_mw) == 1 and "site-wide" in _mw[0]["title"] \
                and "3 of their watched pages" in _mw[0]["text"], _mw

            # No packet at all is not a crash - the fallback path handles it.
            globals()["INBOX"] = Path(_d) / "absent.json"
            assert inbox_items() == []
    finally:
        globals()["INBOX"] = _real

    # Generated entries accumulate. The old merge erased every earlier generated
    # entry on each run, so history never built up.
    _doc = {"reviewed": [
        {"date": "2026-09-01", "competitor_id": "a", "text": "hand", "sources": []},
        {"date": "2026-09-10", "competitor_id": "b", "text": "old gen", "generated": True},
        {"date": "2026-09-14", "competitor_id": "c", "text": "today earlier", "generated": True},
        {"date": "2026-09-14", "competitor_id": "d", "text": "hand today", "sources": []},
    ]}
    _fresh = [{"date": "2026-09-14", "competitor_id": "e", "text": "today now", "generated": True}]
    _m, _kept, _rep = merge_generated(_doc, _fresh, today="2026-09-14")
    _texts = [r["text"] for r in _m["reviewed"]]
    assert "old gen" in _texts, "an earlier day's generated entry must survive"
    assert "hand" in _texts and "hand today" in _texts, "hand-written entries are never touched"
    assert "today earlier" not in _texts, "a same-day re-run replaces its own earlier output"
    assert "today now" in _texts and _rep == 1 and _kept == 3, (_kept, _rep)

    # The competing set comes from competitors.json, never typed into the prompt.
    _sp = system_prompt()
    assert "{COMPETING}" not in _sp
    for _c in json.loads((HERE / "competitors.json").read_text())["competitors"]:
        if _c.get("threat") in ("high", "medium") and _c.get("status", "active") == "active":
            assert _c["name"] in _sp, f"{_c['name']} missing from the analyser's brief"

    # The analyser sees what it already reported, so it does not repeat a story.
    _p = build_prompt([{"key": "k", "kind": "news", "title": "t", "url": "u", "text": "x"}],
                      ["- 2026-09-27 [festival-industry-health] Oceans Calling was cancelled in full."])
    assert _p.startswith("ALREADY REPORTED") and "cancelled in full" in _p
    assert "ALREADY REPORTED" not in build_prompt(
        [{"key": "k", "kind": "news", "title": "t", "url": "u", "text": "x"}], [])
    _r = recent_findings(today=datetime(2026, 9, 28).date())
    assert all(" 2026-09-28 " not in x for x in _r), "today's own entries are not 'already reported'"

    # An article already judged at one address is skipped at its mirror.
    _a = "https://wordpress.mapsted.com/what-does-a-digital-wayfinding-system-cost-for-a-large-hospital/"
    _b = "https://mapsted.com/blog/what-does-a-digital-wayfinding-system-cost-for-a-large-hospital"
    assert slug_of(_a) == slug_of(_b) != ""
    assert slug_of("https://purple.ai/pricing") == "" and slug_of("https://x.test/case-studies/") == ""

    # ---- claim check: layer 1, figures and quotes (real cases from the audits)
    _src = ("What CRE Teams Get Back 30-40% Typical real estate cost reduction "
            "3\u20135\u00d7 Lower TCO vs. alternative solutions 1M+ Sq ft. "
            "The announcement was disappointing for the nearly 55,000 people who planned "
            "to attend. Its consumer evidence: a 2015 YouGov study. "
            "\u201cBeacons are sometimes the right answer\u201d")
    assert unsupported_figures("They claim 3-5x lower TCO and 30-40% savings.", _src) == []
    assert unsupported_figures("It cites a 2015 YouGov study.", _src) == []
    assert unsupported_figures('They say "Beacons are sometimes the right answer".', _src) == []
    assert unsupported_figures("Accuracy is 72.7% across 180,000 sq ft.", _src) == ["72.7%", "180,000"], \
        unsupported_figures("Accuracy is 72.7% across 180,000 sq ft.", _src)
    assert unsupported_figures('Their words: "we never miss a delegate".', _src) != []
    # The limitation, stated so nobody relies on layer 1 alone: "55,000" IS in the
    # source, so "55,000 fans per day" passes here. Catching that misreading is
    # what the model pass exists for.
    assert unsupported_figures("Sold out at 55,000 fans per day.", _src) == []

    # ---- claim check: keep / trim / hold, deletion only, without a model call
    def _e(t): return {"text": t, "competitor_id": "x", "_source_text": _src}
    _es = [
        # clean
        _e("Clean finding with 30-40% savings. It matters for us in venues."),
        # misreading in the FACT sentence -> held (the 55,000-per-day case)
        _e("Sold out at 55,000 fans per day across three days. It matters a lot. It sets budgets."),
        # flag in a supporting sentence -> that sentence removed, nothing added
        _e("They claim 3-5x lower TCO. Their evidence is solely one survey. That weakens the claim."),
        # invented figure the model pass missed -> layer 1 still holds it
        _e("Accuracy is 72.7%. It matters. More."),
        # flag that cannot be located in the text -> held, cannot trim precisely
        _e("They claim 3-5x lower TCO. It matters. For us."),
    ]
    _v = {0: {"index": 0, "supported": True, "unsupported": []},
          1: {"index": 1, "supported": False, "unsupported": ["55,000 fans per day"]},
          2: {"index": 2, "supported": False, "unsupported": ["Their evidence is solely one survey"]},
          3: {"index": 3, "supported": True, "unsupported": []},
          4: {"index": 4, "supported": False, "unsupported": ["a claim the checker paraphrased"]}}
    apply_checks(_es, _v)
    assert _es[0].get("verified") == "checked" and not _es[0].get("held")
    assert _es[1].get("held"), "a flag in the fact sentence holds the finding"
    assert _es[2].get("verified") == "trimmed", _es[2]
    assert _es[2]["text"] == "They claim 3-5x lower TCO. That weakens the claim.", _es[2]["text"]
    assert set(_norm(_es[2]["text"]).split()) <= set(_norm(
        "They claim 3-5x lower TCO. Their evidence is solely one survey. That weakens the claim.").split()), \
        "trimming may only remove words, never add them"
    assert _es[3].get("held"), "the figure check overrides a model that missed an invented number"
    assert _es[4].get("held"), "a flag that cannot be located is held, not guessed at"

    # Two passes: a claim flagged by either is flagged; supported needs both.
    _mv = merge_verdicts({0: {"supported": True, "unsupported": []},
                          1: {"supported": True, "unsupported": []}},
                         {0: {"supported": False, "unsupported": ["WorkSpaces is an events show"]},
                          1: {"supported": True, "unsupported": []}})
    assert _mv[0]["supported"] is False and _mv[0]["unsupported"] == ["WorkSpaces is an events show"]
    assert _mv[1]["supported"] is True
    assert merge_verdicts({0: {"supported": True, "unsupported": []}}, {})[0]["supported"] is False, \
        "a finding only one pass saw is not fully checked"

    # If either checking pass fails, nothing it should have covered is sent.
    _real_one = globals()["_one_pass"]
    try:
        def _fake(client, system, batch, offset=0):
            if system is VERIFY_ADVERSARIAL:
                raise ValueError("empty reply")
            return {offset + i: {"index": i, "supported": True, "unsupported": []}
                    for i in range(len(batch))}
        globals()["_one_pass"] = _fake
        _cc = [{"text": "Clean fact with 30-40% savings. It matters.", "competitor_id": "x",
                "_source_text": _src} for _ in range(5)]
        check_claims(object(), _cc)
        assert all(c.get("held") for c in _cc), "one pass is not two: all must be held"
        assert "could not be fully checked" in _cc[0]["held_reason"]
        def _ok(client, system, batch, offset=0):
            return {offset + i: {"index": i, "supported": True, "unsupported": []}
                    for i in range(len(batch))}
        globals()["_one_pass"] = _ok
        _cc = [{"text": "Clean fact with 30-40% savings. It matters.", "competitor_id": "x",
                "_source_text": _src} for _ in range(5)]
        check_claims(object(), _cc)
        assert not any(c.get("held") for c in _cc) and all(c["verified"] == "checked" for c in _cc)
    finally:
        globals()["_one_pass"] = _real_one

    # 2 October, real cases. A quote in the closing implication is our own framing.
    _pu = {"text": "Purple published a comparison hub with 23 pages. Its frame is the "
                   "\"we already have WiFi\" objection WYA meets, now in 23 variants.",
           "competitor_id": "purple", "_source_text": "compare hub 23 pages Cisco Spaces"}
    apply_checks([_pu], {0: {"index": 0, "supported": True, "unsupported": []}})
    assert _pu.get("verified") == "checked", _pu
    # A wrong aside between dashes is cut on its own; the true facts survive.
    _me = {"text": "Meshh published a Brookfield Place NYC case study - its fifth new case "
                   "study in five days - covering 9 sensors. It reaches marketing budgets.",
           "competitor_id": "meshh", "_source_text": "9x sensors deployed at Brookfield Place"}
    apply_checks([_me], {0: {"index": 0, "supported": False,
                             "unsupported": ["its fifth new case study in five days"]}})
    assert _me.get("verified") == "trimmed", _me
    assert _me["text"] == ("Meshh published a Brookfield Place NYC case study covering 9 "
                           "sensors. It reaches marketing budgets."), _me["text"]
    # Model pass unavailable: figures still checked, nothing silently waved through.
    _m = [_e("Fine at 30-40%. It matters for venues and our pitch."), _e("Invented 88.8% rate. Bad.")]
    apply_checks(_m, {}, model_ran=False)
    assert _m[0]["verified"] == "figures only" and _m[1].get("held")
    # Held findings never feed the analyser's memory.
    _saved = ANALYSIS.read_text() if ANALYSIS.exists() else None
    try:
        ANALYSIS.write_text(json.dumps({"reviewed": [
            {"date": "2026-09-27", "competitor_id": "a", "text": "Kept. Yes."},
            {"date": "2026-09-27", "competitor_id": "b", "text": "Held one. No.", "held": True}]}))
        _rf = recent_findings(today=datetime(2026, 9, 28).date())
        assert any("Kept" in x for x in _rf) and not any("Held one" in x for x in _rf)
        # A company in today's packet is remembered for 60 days, others for 4
        # (Henley's chief executive leaving, reported 1 Oct, re-sent 15 Oct).
        ANALYSIS.write_text(json.dumps({"reviewed": [
            {"date": "2026-10-01", "competitor_id": "henley", "text": "CEO steps down. X."},
            {"date": "2026-10-01", "competitor_id": "other", "text": "Old other. X."}]}))
        _rf = recent_findings(today=datetime(2026, 10, 15).date(), ids={"henley"})
        assert any("CEO steps down" in x for x in _rf), _rf
        assert not any("Old other" in x for x in _rf), "only today's companies get 60 days"
    finally:
        if _saved is not None:
            ANALYSIS.write_text(_saved)

    assert GNEWS.match("https://news.google.com/rss/articles/CBMiabc")
    assert not GNEWS.match("https://www.meshh.com/blog/post")
    assert not GNEWS.match("https://newsroom.example.com/x")   # not Google

    # every fetchable kind must be a real kind the collector emits
    assert FETCHABLE <= {"news", "post", "new_page", "page_change", "app_release"}
    # News is fetchable now that the collector resolves Google links to the
    # publisher. What must still hold: an UNRESOLVED Google link is never fetched,
    # because that would store the ~590KB interstitial as if it were the article.
    _rows = [{"kind": "news", "url": "https://news.google.com/rss/articles/CBMiX", "key": "a"},
             {"kind": "news", "url": "https://publisher.test/story", "key": "b"}]
    _ok = [r for r in _rows if r["kind"] in FETCHABLE and not GNEWS.match(r.get("url", ""))]
    assert [r["key"] for r in _ok] == ["b"], _ok

    # the schema must be strict-output legal: additionalProperties false + required
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) <= set(SCHEMA["properties"])
    it = SCHEMA["properties"]["items"]["items"]
    assert it["additionalProperties"] is False
    assert set(it["required"]) == set(it["properties"])

    # the standing rules must actually be in the system prompt
    for rule in ("self-healing", "Blank beats plausible", "sales opening"):
        assert rule in SYSTEM, rule
    assert MODEL == "claude-opus-5-5"

    p = build_prompt([{"key": "k1", "competitor": "X", "kind": "post",
                       "title": "T", "url": "u", "text": "body",
                       "published": "2026-08-01T00:00:00+00:00"}])
    assert "key: k1" in p and "body" in p

    # The bug that shipped silently: a schema/merge field mismatch left every
    # generated entry with competitor_id=None and empty text. This exercises the
    # exact shape the model's structured output actually has and checks the
    # fields the dashboard, notify.py and the orphan-audit script all depend on.
    src_items = [{"key": "k1", "competitor_id": "purple", "competitor": "Purple",
                  "title": "Purple cuts price", "url": "https://purple.ai/pricing",
                  "published": "2026-08-26T00:00:00+00:00"}]
    model_items = [{"key": "k1", "what_changed": "Price dropped 20%.",
                    "why_it_matters": "Undercuts the segment.", "confidence": "certain"}]
    entries, skipped = build_generated_entries(model_items, src_items, "claude-opus-5")
    assert skipped == 0, skipped
    assert len(entries) == 1
    e = entries[0]
    assert e["competitor_id"] == "purple", "competitor_id must come from the source item"
    assert e["text"] == "Price dropped 20%. Undercuts the segment.", e["text"]
    assert e["sources"] and "purple.ai/pricing" in e["sources"][0]
    assert e["url"] == "https://purple.ai/pricing"
    assert e["generated"] is True and e["model"] == "claude-opus-5"

    # A model item whose key doesn't match any supplied item (hallucinated, or from
    # a stale prompt) must be dropped, never written with competitor_id=None - that
    # silently vanishes from the dashboard (reviewedFor() skips null competitor_id)
    # while still polluting Slack/email with a "Market" bucket entry.
    bad, skipped2 = build_generated_entries(
        [{"key": "no-such-key", "what_changed": "x", "why_it_matters": "y",
          "confidence": "certain"}], src_items, "claude-opus-5")
    assert bad == [] and skipped2 == 1

    # An item with empty judgement text must also be dropped, not written blank.
    empty, skipped3 = build_generated_entries(
        [{"key": "k1", "what_changed": "", "why_it_matters": "",
          "confidence": "certain"}], src_items, "claude-opus-5")
    assert empty == [] and skipped3 == 1

    # Counters and date labels are not movement; real words are.
    for _t in ("### 97", "29 September 2026", "3 days ago", "Yesterday", "+12%"):
        assert _trivial_line(_t), _t
    for _t in ("97 hospitals now live", "Purple Engage", "Pricing from 8 pounds"):
        assert not _trivial_line(_t), _t

    # 7 Oct: a finding that announces itself as more detail on a told story is dropped.
    _fu, _sk = build_generated_entries(
        [{"key": "k1", "what_changed": "Detail on the Informa/Clarion deal: 2,000 staff.",
          "why_it_matters": "", "confidence": "certain"}], src_items, "m")
    assert _fu == [] and _sk == 1, "a follow-up on a reported story must not be written"
    assert not _FOLLOW_UP.match("Detailed floor plans went live at Olympia.")

    # 6 Oct: a note on how the source was read is deleted; the facts stay.
    _o = ("Mixmag reports the Grand Hall cancelled its shows. Headline only; the article "
          "was not read. It matters for us.")
    assert strip_process_notes(_o) == "Mixmag reports the Grand Hall cancelled its shows. It matters for us."
    assert strip_process_notes("Purple added a page.") == "Purple added a page."

    # App builds whose notes say nothing never reach the analyser (Cisco Spaces
    # 1.10, 11 Oct: "General Improvements").
    for _n in ("General Improvements", "Bug fixes and performance improvements.",
               "- Minor bug fixes\n- Stability improvements", "Various fixes & enhancements"):
        assert _generic_release(_n), _n
    for _n in ("New Crowd Map view", "- Updated screenshots\n- Improved login process",
               "General Improvements\nAdded live occupancy", "", None):
        assert not _generic_release(_n), _n

    # The implication is stored exactly, and a client-only `major` flag.
    _src = [{"key": "c1", "competitor_id": "informa", "entity_type": "client",
             "competitor": "Informa", "title": "t", "url": "u"},
            {"key": "p1", "competitor_id": "purple", "entity_type": "competitor",
             "competitor": "Purple", "title": "t", "url": "u2"}]
    _ents, _ = build_generated_entries(
        [{"key": "c1", "what_changed": "Informa agreed to buy Clarion. The deal is 2bn.",
          "why_it_matters": "Our account changes owner.", "confidence": "certain",
          "major_client_move": True},
         {"key": "p1", "what_changed": "Purple cut its price.", "why_it_matters":
          "It undercuts us.", "confidence": "certain", "major_client_move": True}],
        _src, "m")
    assert _ents[0]["means"] == "Our account changes owner." and _ents[0].get("major")
    assert "major" not in _ents[1], "major is for client items only"
    # A deleted implication leaves means "" (kept out of Slack); a deleted middle
    # fact sentence leaves it whole; an aside cut inside it is cut there too.
    _e = [{"text": "Fact one. Fact two is wrong. It means a thing.",
           "means": "It means a thing.", "_source_text": ""},
          {"text": "Fact one. Fact two. It means a wrong thing.",
           "means": "It means a wrong thing.", "_source_text": ""},
          {"text": "Fact one. It means - a wrong aside - a thing.",
           "means": "It means - a wrong aside - a thing.", "_source_text": ""}]
    apply_checks(_e, {0: {"supported": False, "unsupported": ["Fact two is wrong"]},
                      1: {"supported": False, "unsupported": ["It means a wrong thing"]},
                      2: {"supported": False, "unsupported": ["a wrong aside"]}})
    assert _e[0]["means"] == "It means a thing." and _e[0]["text"] == "Fact one. It means a thing."
    assert _e[1]["means"] == "" and _e[1]["text"] == "Fact one. Fact two.", _e[1]
    assert _e[2]["means"] == "It means a thing.", _e[2]
    assert "third party" in SYSTEM and "major_client_move" in SYSTEM
    # Figure check: a decimal comma and a range written out in words are the same
    # figure (Wacken "15,5%", Mapsted "between roughly 1 m and 5 m", 14 Oct).
    assert unsupported_figures("from around 15.5% each", "from around 15,5% they held") == []
    assert unsupported_figures("roughly 1-5 m horizontal",
                               "Between roughly 1 m and 5 m horizontally") == []
    assert unsupported_figures("roughly 1-6 m", "Between roughly 1 m and 5 m") == ["1-6 m"]
    assert unsupported_figures("85,000 guests", "some 85,000 guests") == []
    for _t in ("## Comments", "Loading\u2026", "Write a comment...Write a comment...", "## 1 Comment", "Sort by:"):
        assert _trivial_line(_t), _t
    assert not _trivial_line("Comments from fans were overwhelmingly positive")
    # Old work never reaches the analyser (Meshh's 2019 case studies, owner 12 Oct).
    assert _old_work("Westfield Oculus, 2019 holiday campaign. \u00a9 2026 Meshh", 2026)
    assert not _old_work("Impact XM, 2024 to 2025 programme", 2026)
    assert not _old_work("No year anywhere. Copyright 2019", 2026)
    assert not _old_work("Launched at ISE 2026.", 2026)
    print("analyse selftest OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    days = next((int(a.split("=", 1)[1]) for a in args
                 if a.startswith("--days=")), 7)
    if "--selftest" in args:
        selftest()
    elif "--read" in args:
        read(days)
    elif "--analyse" in args or "--analyze" in args:
        analyse(days)
    elif "--recheck" in args:
        # Regression suite: real findings with their real sources. An "error" case
        # passes only if its known-bad phrase is gone (held, or removed); a "good"
        # case passes only if it is not held. Writes nothing, sends nothing; exits
        # non-zero on any failure so CI fails.
        import anthropic
        fx = json.loads(Path(args[args.index("--recheck") + 1]).read_text())
        check_claims(anthropic.Anthropic(), fx)
        # A known error reaching the channel is a hard failure. A good finding
        # being held is the accepted cost of strictness - reported, and only a
        # failure if the checker holds most good findings (it would be useless).
        missed, held_good, goods = 0, 0, 0
        for f in fx:
            got = "HELD" if f.get("held") else f.get("verified")
            if f["expect"] == "error":
                ok = f.get("held") or _norm(f["bad"]) not in _norm(f["text"])
                missed += not ok
                tag = "PASS" if ok else "FAIL"
            else:
                goods += 1
                held_good += bool(f.get("held"))
                tag = "PASS" if not f.get("held") else "WARN"
            print(f"\n{tag}  [{f['label']}] expected {f['expect']} -> {got}")
            if f.get("removed_claims"):
                print(f"   removed: {f['removed_claims']}")
            if f.get("held_reason"):
                print(f"   {f['held_reason'][:300]}")
        print(f"\nknown errors stopped: {sum(1 for f in fx if f['expect']=='error') - missed}"
              f"/{sum(1 for f in fx if f['expect']=='error')}   "
              f"good findings held: {held_good}/{goods}")
        sys.exit(1 if missed or held_good * 2 > goods else 0)
    else:
        raise SystemExit(__doc__)
