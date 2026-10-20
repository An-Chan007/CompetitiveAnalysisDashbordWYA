#!/usr/bin/env python3
"""Weekly market digest. Python stdlib only.

Turns changes.jsonl into a short update someone will actually read, grouped the
way it was asked for: what the competition is doing, what our clients are doing,
and what is happening in the space.

Writes digest.md (readable anywhere, pasteable into email or Slack) and
digest.html (styled, matches the dashboard). Both are regenerated each run.

Also archives each run as a dated edition under digest/ with an index, so there
is a history to look back through. The live digest.html stays rolling - old items
drop off - because that is the behaviour that was reviewed and approved. The
archive is additive and deliberately out of the way of the live view.

Usage:
    python3 digest.py            # last 7 days, live files + archived edition
    python3 digest.py --days=14  # any window
    python3 digest.py --no-archive
    python3 digest.py --selftest
"""

import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
CHANGES = HERE / "changes.jsonl"
OUT_MD = HERE / "digest.md"
OUT_HTML = HERE / "digest.html"
ARCHIVE = HERE / "digest"

SECTIONS = [
    ("client",     "What our clients are doing",
     "Public news about WYA's own clients and prospects. Read this before a client call."),
    ("competitor", "What the competition is doing",
     "Competitor announcements, published output and changes to their own pages."),
    ("sector",     "What is happening in the space",
     "Regulation, market moves and sector conditions that affect the pitch."),
]

# Words that make an item worth leading with. Ordered loosely by how much a
# headline containing them tends to change a conversation. This ranks within a
# section; it never hides anything - everything still appears below the lead.
SIGNALS = [
    (6, r"\b(acquir|acquisition|merger|takeover|divest|sold|sells|buyout)\b"),
    (6, r"\b(chief execu|\bceo\b|chair|managing director|\bmd\b|appoint|steps down|resign|hires?|joins)\b"),
    (5, r"\b(invest|funding|raises?|£\d|\$\d|\bm\b partnership|million|billion)\b"),
    (5, r"\b(law|regulation|legislation|compliance|deadline|duty|mandat)\b"),
    (4, r"\b(restructur|redundanc|cull|insolven|administration|profit|revenue|loss)\b"),
    (4, r"\b(launch|unveil|introduc|new (product|platform|sensor|tool))\b"),
    (3, r"\b(pricing|price|tier|free trial)\b"),
    (3, r"\b(partnership|contract|deal|wins?|secures?|renew)\b"),
    (2, r"\b(cancel|boycott|withdraw|backlash|controvers)\b"),
]
# An app release is a shipped product change with a version and a date behind
# it, so it ranks with a pricing-page edit rather than with a new blog page.
KIND_WEIGHT = {"page_change": 5, "app_release": 5, "new_page": 2,
               "news": 2, "post": 0}
PRIORITY_WEIGHT = {"high": 3, "medium": 1, "low": 0, "none": -3, "": 0}


# A Google News RSS link is ~600 characters of base64 that resolves to a consent
# wall, not an article (tested with a real browser, 2026-09-04). Printing it makes
# the digest unreadable and the link does not work. The publisher name and the
# headline are the parts that carry information, so those are what we show.
GNEWS_RE = re.compile(r"^https?://news\.google\.com/", re.I)


def linkable(url):
    return bool(url) and not GNEWS_RE.match(url)


def md_link(title, url, publisher=""):
    """A markdown link when the URL is worth clicking, plain text when it is not."""
    if linkable(url):
        return f"[{title}]({url})"
    return f"{title}" + (f" — {publisher}" if publisher else "")


# Marketing copy that changed is not the week's top story. These are the diffs
# that fire on nav, cookie banners and accordion state - Purple filed two
# "pricing page changed" items in two days and both were an FAQ toggling open.
COSMETIC_RE = re.compile(
    r"^(features?|products?|solutions?|pricing|menu|home|resources?|company|"
    r"about|contact|login|log in|sign up|book a demo|get started|guest wifi|"
    r"learn more|read more|see more|view all|accept|cookies?|privacy|"
    r"do you offer|free|overview)\b[^\w]*$", re.I)


def cosmetic_change(row):
    """True when a page_change reports only chrome moving, not content.

    An item with no `added` KEY is not cosmetic, it is unknown: page text has only
    been captured since the second read of each page, so early rows carry no diff
    at all. Demoting those would bury a real price change purely because we did
    not record what moved. An empty `added` list is different - the diff ran and
    found nothing readable.
    """
    if row.get("kind") != "page_change" or "added" not in row:
        return False
    added = [a.strip() for a in (row.get("added") or []) if a.strip()]
    if not added:
        return True                       # a hash moved and nothing readable did
    return all(COSMETIC_RE.match(a) or len(a) < 12 for a in added)


# Two outlets covering one event rarely produce the same words in the same order,
# so an exact normalised key does not collapse them: "BREAKING: ... sells Global
# Wellness Summit to Informa" and "... sells Global Wellness Summit to Informa,
# CEO to Depart" differ by one word out of six. Overlap is the test that works.
STORY_OVERLAP = 0.6
# Comparison is always scoped to one company, so an absolute bar is safe here and
# catches what the ratio misses: a wire story and a trade write-up of one
# acquisition shared only 4 of 9 words ("global wellness summit informa") because
# each outlet added its own detail, and both led the digest.
STORY_SHARED = 4
STOP = {"the", "and", "for", "with", "from", "that", "this", "after", "into",
        "breaking", "exclusive", "says", "will", "new", "has", "its"}


def story_words(title):
    t = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower())
    return {w for w in t.split() if len(w) > 3 and w not in STOP}


def same_story(a, b):
    """True when two headlines are almost certainly about one event."""
    wa, wb = story_words(a), story_words(b)
    if len(wa) < 3 or len(wb) < 3:
        return False
    shared = len(wa & wb)
    return shared >= STORY_SHARED or shared / min(len(wa), len(wb)) >= STORY_OVERLAP


def score(row):
    """How much should this lead? Higher is more likely to matter."""
    t = (row.get("title") or "").lower()
    s = KIND_WEIGHT.get(row.get("kind"), 0)
    s += PRIORITY_WEIGHT.get(row.get("priority", ""), 0)
    # A page whose nav moved must not outrank an acquisition. Two Purple "pricing
    # page changed" items led the digest in consecutive weeks; both were an
    # accordion opening.
    if cosmetic_change(row):
        s -= 8
    for weight, pat in SIGNALS:
        if re.search(pat, t):
            s += weight
    return s


def load_rows(days):
    if not CHANGES.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .isoformat(timespec="seconds")
    rows = []
    for line in CHANGES.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        when = r.get("published") or r.get("seen_at") or ""
        if when >= cutoff:
            r["_when"] = when
            rows.append(r)
    return rows


def group(rows):
    """{entity_type: {target_name: [rows sorted by score then date]}}"""
    out = {}
    for r in rows:
        out.setdefault(r.get("entity_type", "competitor"), {}) \
           .setdefault(r.get("competitor", "unknown"), []).append(r)
    for ents in out.values():
        for lst in ents.values():
            lst.sort(key=lambda r: (-score(r), r["_when"]), reverse=False)
    return out


def fmt_day(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d %b")
    except (ValueError, TypeError):
        return ""


def write_archive(today, days, n_items, md_text, html_text):
    """Keep a dated edition of this run, plus an index of every edition.

    Same day twice overwrites rather than accumulating - a Monday cron run and a
    manual refresh on the same Monday are one edition, not two.
    """
    ARCHIVE.mkdir(exist_ok=True)
    stamp = today.strftime("%Y-%m-%d")
    (ARCHIVE / f"{stamp}.md").write_text(md_text)
    (ARCHIVE / f"{stamp}.html").write_text(html_text)

    editions = sorted((p.stem for p in ARCHIVE.glob("*.html")
                       if p.stem != "index"), reverse=True)
    counts = {}
    for e in editions:
        first = (ARCHIVE / f"{e}.md").read_text().split("\n")[:4]
        counts[e] = next((ln for ln in first if "items" in ln), "")

    rows = "\n".join(
        f'<li><a href="{e}.html">{e}</a>'
        f'<span class="n">{esc_count(counts.get(e,""))}</span></li>'
        for e in editions)
    (ARCHIVE / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WYA market update — past editions</title><style>
:root{{color-scheme:light;--teal:#00C9A7;--ink:#007f6b;--bg:#F7F7F7;--panel:#fff;
--text:#111;--muted:#6b7280;--line:#e2e5ea}}
@media (prefers-color-scheme:dark){{:root{{color-scheme:dark;--ink:#3ddcbd;
--bg:#0d0f11;--panel:#16191c;--text:#e9ecef;--muted:#8e97a3;--line:#272b30}}}}
body{{margin:0;background:var(--bg);color:var(--text);font-size:15.5px;
font-family:Calibri,"Segoe UI",system-ui,sans-serif;line-height:1.6}}
header{{background:#000;color:#fff;padding:24px 0;border-bottom:4px solid var(--teal)}}
.page{{max-width:760px;margin:0 auto;padding:0 22px 44px}}
h1{{margin:0;font-size:22px}}h1 b{{color:var(--teal)}}
.sub{{color:#9aa3ad;font-size:13px;margin-top:5px}}
p.lede{{color:var(--muted);font-size:13.5px;margin:22px 0 18px}}
ul{{list-style:none;padding:0;margin:0;background:var(--panel);
border:1px solid var(--line);border-radius:10px;overflow:hidden}}
li{{display:flex;gap:14px;align-items:baseline;padding:13px 18px;
border-bottom:1px solid var(--line)}}
li:last-child{{border-bottom:0}}
a{{color:var(--ink);text-decoration:none;font-weight:600}}
a:hover{{text-decoration:underline}}
.n{{color:var(--muted);font-size:12.5px;margin-left:auto}}
</style></head><body>
<header><div class="page"><h1>WYA <b>market update</b> — past editions</h1>
<div class="sub">{len(editions)} archived</div></div></header>
<div class="page">
<p class="lede">Each edition is the update as it stood on that date. The live
update rolls forward and older items drop off it; these do not change.</p>
<ul>{rows}</ul></div></body></html>""")
    return stamp, len(editions)


def esc_count(line):
    """Pull '42 items' out of a digest header line for the index."""
    import re as _re
    m = _re.search(r"(\d+)\s+items", line)
    return f"{m.group(1)} items" if m else ""


def html_link(title, url, esc):
    """Anchor when the URL resolves to something, plain text when it does not."""
    if linkable(url):
        return (f"<a href='{esc(url)}' target='_blank' rel='noopener'>"
                f"{esc(title)}</a>")
    return f"<span class='nolink'>{esc(title)}</span>"


def build(days=7, archive=True):
    rows = load_rows(days)
    grouped = group(rows)
    today = datetime.now(timezone.utc)
    period = (f"{(today - timedelta(days=days)).strftime('%d %B')} to "
              f"{today.strftime('%d %B %Y')}")

    md = [f"# WYA market update", "",
          f"**{period}** · {len(rows)} items · generated {today.strftime('%d %B %Y')}", ""]
    if not rows:
        md += ["Nothing new in this window.", "",
               "That is a real result, not a failure — several tracked companies",
               "produce very little press in any given week. Widen the window with",
               "`python3 digest.py --days=30` to confirm.", ""]

    # The lead: highest-scoring items across everything, one story each. Three of
    # the five leads were the same Informa acquisition reported by three outlets -
    # collect.py dedupes by simkey within a target, but the lead list is drawn
    # across all of them and had no such guard, so one event could fill the digest.
    leads = []
    for r in sorted(rows, key=score, reverse=True):
        title = r.get("title") or ""
        if any(l.get("competitor_id") == r.get("competitor_id")
               and same_story(l.get("title") or "", title) for l in leads):
            continue
        leads.append(r)
        if len(leads) >= 5:
            break
    if leads:
        md += ["## Lead items", "",
               "_Ranked by how much the headline tends to change a conversation._", ""]
        for r in leads:
            tag = {"client": "CLIENT", "competitor": "COMPETITOR",
                   "sector": "SPACE"}.get(r.get("entity_type"), "")
            pub = f" · {r['publisher']}" if r.get("publisher") else ""
            md.append(f"- **[{tag}] {r['competitor']}** — "
                      f"{md_link(r['title'], r['url'], r.get('publisher',''))}"
                      f"  <sub>{fmt_day(r['_when'])}{pub}</sub>")
        md.append("")

    for etype, heading, blurb in SECTIONS:
        ents = grouped.get(etype, {})
        md += [f"## {heading}", "", f"_{blurb}_", ""]
        if not ents:
            md += ["Nothing this week.", ""]
            continue
        # busiest target first
        for name in sorted(ents, key=lambda n: -len(ents[n])):
            items = ents[name]
            md.append(f"**{name}** ({len(items)})")
            md.append("")
            for r in items[:6]:
                pub = f" · {r['publisher']}" if r.get("publisher") else ""
                md.append(f"- {md_link(r['title'], r['url'])} "
                          f"<sub>{fmt_day(r['_when'])}{pub}</sub>")
            if len(items) > 6:
                md.append(f"- _…and {len(items) - 6} more_")
            md.append("")

    md += ["---", "",
           "Collected from public sources only: Google News, the targets' own RSS "
           "feeds, sitemap diffs and page-content hashes. No social platform is "
           "accessed in breach of its terms.", "",
           "Internal — not for external distribution."]
    md_text = "\n".join(md)
    OUT_MD.write_text(md_text)

    # html version, same content, dashboard styling
    def esc(s):
        return html.escape(str(s))
    h = [f"""<!DOCTYPE html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WYA market update — {esc(period)}</title><style>
:root{{color-scheme:light;--teal:#00C9A7;--ink:#007f6b;--bg:#F7F7F7;--panel:#fff;
--text:#111;--t2:#4a5158;--muted:#6b7280;--line:#e2e5ea;--s2:#eceef1}}
@media (prefers-color-scheme:dark){{:root{{color-scheme:dark;--ink:#3ddcbd;
--bg:#0d0f11;--panel:#16191c;--text:#e9ecef;--t2:#b6bec7;--muted:#8e97a3;
--line:#272b30;--s2:#22262a}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);
font-family:Calibri,"Segoe UI",system-ui,sans-serif;font-size:15.5px;line-height:1.6}}
.page{{max-width:820px;margin:0 auto;padding:0 22px 48px}}
header{{background:#000;color:#fff;padding:24px 0;margin-bottom:26px;
border-bottom:4px solid var(--teal)}}
header .page{{padding-bottom:0}}h1{{margin:0;font-size:23px}}h1 b{{color:var(--teal)}}
header .sub{{color:#9aa3ad;font-size:13px;margin-top:6px}}
h2{{font-size:18px;margin:34px 0 4px;padding-bottom:7px;border-bottom:2px solid var(--teal)}}
.blurb{{color:var(--muted);font-size:13.5px;margin:0 0 18px}}
.lead{{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--teal);
border-radius:0 10px 10px 0;padding:16px 20px;margin-bottom:8px}}
.lead a{{font-weight:600}}
.nolink{{font-weight:600}}
.tag{{display:inline-block;font-size:9.5px;font-weight:700;letter-spacing:.07em;
padding:2px 6px;border-radius:4px;background:var(--s2);color:var(--muted);
margin-right:7px;vertical-align:2px}}
.who{{font-weight:700;margin-top:22px;font-size:15px}}
.n{{color:var(--muted);font-weight:400;font-size:13px}}
ul{{margin:7px 0 0;padding-left:20px}}li{{margin-bottom:7px;color:var(--t2)}}
a{{color:var(--ink);text-decoration:none;border-bottom:1px solid transparent}}
a:hover{{border-bottom-color:currentColor}}
sub{{color:var(--muted);font-size:11.5px;margin-left:5px}}
.none{{color:var(--muted);font-style:italic}}
footer{{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);
color:var(--muted);font-size:12.5px}}
</style></head><body>
<header><div class="page"><h1>WYA <b>market update</b></h1>
<div class="sub">{esc(period)} &middot; {len(rows)} items</div></div></header>
<div class="page">"""]
    if leads:
        h.append("<h2>Lead items</h2><p class='blurb'>Ranked by how much the "
                 "headline tends to change a conversation.</p>")
        for r in leads:
            tag = {"client": "CLIENT", "competitor": "COMPETITOR",
                   "sector": "SPACE"}.get(r.get("entity_type"), "")
            h.append(f"<div class='lead'><span class='tag'>{esc(tag)}</span>"
                     f"<b>{esc(r['competitor'])}</b> — "
                     f"{html_link(r['title'], r['url'], esc)}"
                     f"<sub>{esc(fmt_day(r['_when']))}</sub></div>")
    for etype, heading, blurb in SECTIONS:
        ents = grouped.get(etype, {})
        h.append(f"<h2>{esc(heading)}</h2><p class='blurb'>{esc(blurb)}</p>")
        if not ents:
            h.append("<p class='none'>Nothing this week.</p>")
            continue
        for name in sorted(ents, key=lambda n: -len(ents[n])):
            items = ents[name]
            h.append(f"<div class='who'>{esc(name)} "
                     f"<span class='n'>({len(items)})</span></div><ul>")
            for r in items[:6]:
                pub = f" &middot; {esc(r['publisher'])}" if r.get("publisher") else ""
                h.append(f"<li>{html_link(r['title'], r['url'], esc)}"
                         f"<sub>{esc(fmt_day(r['_when']))}{pub}</sub></li>")
            if len(items) > 6:
                h.append(f"<li class='none'>…and {len(items)-6} more</li>")
            h.append("</ul>")
    h.append("""<footer>Collected from public sources only: Google News, the
targets' own RSS feeds, sitemap diffs and page-content hashes. No social platform
is accessed in breach of its terms.<br><br>
Internal — not for external distribution.</footer></div></body></html>""")
    html_text = "\n".join(h)
    OUT_HTML.write_text(html_text)

    print(f"{len(rows)} items in the last {days} days")
    for etype, heading, _ in SECTIONS:
        n = sum(len(v) for v in grouped.get(etype, {}).values())
        print(f"  {etype:11} {n}")
    print(f"wrote {OUT_MD.name} and {OUT_HTML.name}")
    if archive:
        stamp, total = write_archive(today, days, len(rows), md_text, html_text)
        print(f"archived edition {stamp} ({total} editions in {ARCHIVE.name}/)")
    return rows


def _selftest_links():
    """A Google News URL never resolves to an article, so it must never be a link."""
    gn = "https://news.google.com/rss/articles/CBMi" + "x" * 400
    assert not linkable(gn)
    assert linkable("https://crowdconnected.com/pricing")
    assert "](" not in md_link("Some headline", gn, "The Times")
    assert "The Times" in md_link("Some headline", gn, "The Times")
    assert md_link("T", "https://x.test/a") == "[T](https://x.test/a)"
    e = lambda x: x
    assert "<a " not in html_link("T", gn, e) and "T" in html_link("T", gn, e)
    assert "<a " in html_link("T", "https://x.test/a", e)

    # Chrome moving is not news. Both of these are real Purple items that led the
    # digest; both were an accordion or a nav label.
    for added in (["Free"], ["Features"], ["Products", "Guest WiFi \u203a"], []):
        assert cosmetic_change({"kind": "page_change", "added": added}), added
    # No diff recorded at all is unknown, not cosmetic - it must not be demoted.
    assert not cosmetic_change({"kind": "page_change", "title": "pricing changed"})
    assert not cosmetic_change(
        {"kind": "page_change",
         "added": ["\u00a38 per delegate plus a \u00a34,500 activation charge"]})
    assert not cosmetic_change({"kind": "news", "title": "x"})
    # and it must actually demote
    real = {"kind": "page_change", "priority": "high",
            "title": "pricing page changed", "added": ["\u00a39 per delegate now"]}
    fake = {"kind": "page_change", "priority": "high",
            "title": "pricing page changed", "added": ["Features"]}
    assert score(real) > score(fake), (score(real), score(fake))


def selftest():
    _selftest_links()
    assert score({"title": "Acme acquires Foo", "kind": "news", "priority": "high"}) > \
           score({"title": "Acme posts a blog", "kind": "post", "priority": "low"})
    # a page_change on a high-priority competitor should outrank a low-signal post
    assert score({"title": "pricing page changed", "kind": "page_change",
                  "priority": "high"}) > \
           score({"title": "our week in review", "kind": "post", "priority": "high"})
    # an exited competitor is pushed down
    assert score({"title": "Acme launch", "kind": "news", "priority": "none"}) < \
           score({"title": "Acme launch", "kind": "news", "priority": "high"})
    # One event reported by three outlets is one lead, not three. This filled the
    # lead list with a single Informa acquisition.
    a = "BREAKING: Susie Ellis sells Global Wellness Summit to Informa"
    b = "Susie Ellis sells Global Wellness Summit to Informa, CEO to depart"
    c = "Pointr publishes an RFP guide for hospitals"
    assert same_story(a, b), (story_words(a), story_words(b))
    assert not same_story(a, c)
    assert not same_story("Pointr launches X", "Purple launches Y")
    d = "Global Wellness Summit Gets Acquired by Informa, CEO to Depart"
    assert same_story(a, d), (story_words(a) & story_words(d))
    # Two genuinely different stories about one company must NOT collapse.
    assert not same_story("Informa reports half-year revenue growth",
                          "Informa launches a new maritime event in Monaco")
    assert not same_story("short one", "another short")   # too few words to judge
    assert fmt_day("2026-07-29T10:00:00+00:00") == "29 Jul"
    assert fmt_day("nonsense") == ""
    assert fmt_day(None) == ""
    g = group([{"entity_type": "client", "competitor": "X", "title": "t",
                "kind": "news", "_when": "2026-07-01T00:00:00+00:00"}])
    assert list(g) == ["client"] and list(g["client"]) == ["X"]
    assert esc_count("**23 July to 30 July 2026** · 34 items · generated") == "34 items"
    assert esc_count("no number here") == ""
    print("digest selftest OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
    else:
        d = next((int(a.split("=", 1)[1]) for a in args if a.startswith("--days=")), 7)
        build(d, archive="--no-archive" not in args)
