#!/usr/bin/env python3
"""Send the daily update to Slack or email, so it arrives instead of waiting to be opened.

The ask on record was for "an update" — something that turns up. A dashboard someone
has to remember to open is the thing that was not asked for. This posts the day's
analysed findings to a Slack channel and links back to the hosted page for the detail.
It can also send the same content as an email, which was asked for specifically —
Slack and email are two different delivery preferences, not the same request.

`--daily` (both channels) sends **only written analysis** from analysis.json, and sends
nothing at all on a day with none. That is deliberate: a pattern-matched headline
dressed as a judgement was rejected outright. The cost is real — measured over 10-26
August, 8 of 17 days had no analysis written and would have sent nothing. Setting
ANTHROPIC_API_KEY so analyse.py runs is what closes that gap; loosening the filter is
not.

Slack setup, once, by whoever owns the Slack workspace:

    Slack → Apps → Incoming Webhooks → add to the channel → copy the URL.

    export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
    python3 notify.py --dry-run      # prints exactly what would be posted
    python3 notify.py

Email setup, once, by whoever owns an SMTP-capable mailbox or relay (Google Workspace,
Microsoft 365, Postmark, Resend — anything that speaks SMTP works, since this uses only
the standard library):

    export SMTP_HOST='smtp.gmail.com'
    export SMTP_PORT='587'              # optional, defaults to 587
    export SMTP_USER='alerts@wya...'
    export SMTP_PASS='...'              # an app password, not the account password
    export EMAIL_FROM='alerts@wya...'   # optional, defaults to SMTP_USER
    export EMAIL_TO='sales@wya...,ceo@wya...'   # comma-separated
    python3 notify.py --email --dry-run   # prints the email body, sends nothing
    python3 notify.py --email

In GitHub Actions, add these as repository secrets. The daily workflow skips whichever
step's secrets are absent, so nothing breaks before they are set and nothing is sent by
accident.

Credentials are read from the environment and never written to a file in this repo.
"""

import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHANGES = HERE / "changes.jsonl"
COMPETITORS = HERE / "competitors.json"
ANALYSIS = HERE / "analysis.json"
# `or`, not a .get() default: the workflow always sets DASHBOARD_URL from an unset
# repo variable, i.e. to "", and .get() returns "" rather than the default. Every
# Slack message went out with a dead "<|Open the dashboard>" link because of it.
SITE = (os.environ.get("DASHBOARD_URL")
        or "https://your-dashboard.example.com")
DAYS = 7
MAX_LINES = 8
TIMEOUT = 30


def load_json(p):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def read_changes():
    if not CHANGES.exists():
        return []
    out = []
    for line in CHANGES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def when(row):
    return row.get("published") or row.get("seen_at") or ""


def competing_ids(comp):
    """Only companies chasing the same budget. Consumer apps do not page anyone."""
    return {c["id"] for c in (comp or {}).get("competitors", [])
            if c.get("threat") in ("high", "medium")}


# What is worth interrupting someone's day for. Anything outside this waits for the
# weekly digest — a daily that fires every day gets muted inside a week, and the one
# that mattered gets muted with it.
URGENT = (
    ("price", ("pricing", "price", "rate card", "per delegate", "tariff")),
    ("hire", ("appoints", "appointment", "hires", "joins as", "named as",
              "growth director", "head of", "chief ", "careers page")),
    ("money", ("funding", "raises", "acquisition", "acquires", "acquired", "merger",
               "investment", "series a", "series b", "series c", "ipo")),
    ("product", ("launch", "launches", "unveils", "partnership", "partners with",
                 "products page", "product page")),
)


def urgency(row):
    """Which daily-alert category this falls into, or None to hold for the weekly."""
    text = f"{row.get('title','')} {row.get('label','')}".lower()
    for name, words in URGENT:
        if any(w in text for w in words):
            return name
    return None


def build_daily(days=1):
    """Competitor and client movements worth sending today. ([lines], held_back)."""
    ids = competing_ids(load_json(COMPETITORS) or {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    lines, held = [], 0
    for r in sorted([x for x in read_changes() if when(x) >= cutoff],
                    key=when, reverse=True):
        et = r.get("entity_type")
        if et == "competitor" and r.get("competitor_id") not in ids:
            continue                      # consumer apps never page anyone
        if et == "sector":
            continue                      # market context is a weekly matter
        cat = urgency(r)
        if not cat:
            held += 1
            continue
        lines.append({"who": "CLIENT" if et == "client" else "COMPETITOR",
                      "cat": cat, "name": r.get("competitor"),
                      "title": (r.get("title") or "")[:160], "url": r.get("url") or ""})
    return lines, held


MAX_DAILY_ANALYSED = 10
# Five competitors is what fits on a phone screen without scrolling past it.
MAX_DAILY_COMPETITORS = 5
MAX_DAILY_OTHERS = 4
MAX_DAILY_MAJOR = 3        # big client moves, shown in full like a competitor's

# Phrases an entry uses when it is deliberately recording that nothing happened.
# The house style asks for exactly this, so it must be recognised rather than sent.
_NO_SIGNAL_RE = re.compile(
    r"\bno signal\b|does not change anything|nothing (?:changes|changed|new here)|"
    r"not a (?:new )?signal|no competitive signal|nothing competitive|"
    r"no action follows|worth re-reading rather than acting|"
    r"recycled (?:or migrated )?content|republished from earlier", re.I)


def _no_signal(text):
    return bool(_NO_SIGNAL_RE.search(text or ""))


def _names(comp, clients):
    names = {c["id"]: c["name"] for c in (comp.get("competitors") or [])}
    names.update({c["id"]: c["name"] for c in (clients.get("clients") or [])})
    names.update({c["id"]: c["name"] for c in (clients.get("sector_topics") or [])})
    return names


def competing_names():
    """Names of the companies we actually compete with - high and medium threat.

    These are the nine the monitor exists to watch. Every one of them is either a
    mover or explicitly reported as quiet, so silence about a company is always a
    stated finding rather than an absence the reader has to interpret.
    """
    comp = load_json(COMPETITORS) or {}
    return sorted(c["name"] for c in (comp.get("competitors") or [])
                  if c.get("threat") in ("high", "medium")
                  and c.get("status", "active") == "active")


def _kind_level(cid, comp, clients):
    """(kind, level) for a badge — kind in competitor/client/sector/market, level the
    threat or priority word as published in our own data, never invented here."""
    for c in (comp.get("competitors") or []):
        if c.get("id") == cid:
            return "competitor", c.get("threat", "unknown")
    for c in (clients.get("clients") or []):
        if c.get("id") == cid:
            return "client", c.get("priority", "unknown")
    for c in (clients.get("sector_topics") or []):
        if c.get("id") == cid:
            return "sector", c.get("priority", "unknown")
    return "market", "unknown"


def pick_competitors(found, cap=MAX_DAILY_COMPETITORS):
    """Every company's first finding before any company's second, up to `cap`,
    then grouped by company in the order companies first appear."""
    firsts, used = [], set()
    for e in found:
        if e["name"] not in used:
            used.add(e["name"])
            firsts.append(e)
    firsts = firsts[:cap]
    names = {f["name"] for f in firsts}
    extras = [e for e in found if e["name"] in names and not any(e is f for f in firsts)]
    picked = firsts + extras[:cap - len(firsts)]
    order = {f["name"]: i for i, f in enumerate(firsts)}
    return sorted(picked, key=lambda e: order[e["name"]])   # stable within a company


def build_daily_analysed(days=0):
    """Only the written, human-reviewed judgement — never a pattern-matched headline.
    A day with no fresh analysis sends nothing; volume was never the point, the
    judgement was. ([entries], dropped) — entries carry the display name, kind/level
    badge, the full analysis text and its sources, ranked by threat/priority so the
    most important reads first."""
    comp = load_json(COMPETITORS) or {}
    clients = load_json(HERE / "clients.json") or {}
    analysis = load_json(ANALYSIS) or {}
    names = _names(comp, clients)

    # days=0: today's findings only. The window used to be "today or yesterday",
    # so every message re-sent the previous day - and because the list sorted
    # OLDEST first within a company, one-per-company then kept yesterday's entry
    # and dropped today's. On 28 September that sent four findings word for word
    # from the 27th while two genuinely new ones (Purple, Mapsted) never went out.
    # Each run dates its entries with its own UTC date, so "today" is exactly the
    # run that just finished.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    # A held finding failed the claim check (analyse.py) and is sent nowhere.
    written = [a for a in (analysis.get("reviewed") or [])
               if a.get("date", "") >= cutoff and not a.get("held")]
    written.sort(key=lambda a: a.get("date", ""), reverse=True)        # newest first
    written.sort(key=lambda a: _rank(a.get("competitor_id"), comp, clients))  # stable

    entries = []
    for a in written:
        cid = a.get("competitor_id")
        kind, level = _kind_level(cid, comp, clients)
        entries.append({"name": names.get(cid, cid or "Market"),
                        "kind": kind, "level": level,
                        "text": a["text"].strip(),
                        # The implication as the analyser wrote it, kept exact
                        # through the claim check (analyse.py). None on older and
                        # hand-written entries: split_finding() then estimates.
                        "means": a.get("means"),
                        "major": kind == "client" and bool(a.get("major")),
                        "sources": a.get("sources") or [],
                        "url": a.get("url") or "",
                        # Same distinction the dashboard already makes (whyBox() in
                        # index.html): a machine-drafted entry is not a human
                        # judgement yet, and this update must not blur that — the
                        # whole reason it sends analysed-only content is trust.
                        "generated": bool(a.get("generated")),
                        "model": a.get("model") or ""})
    # An entry that says outright it is not a signal belongs on the dashboard, as a
    # record that the absence was checked - not in a message to the whole channel.
    # One send led with Estimote under a sentence reading "This does not change
    # anything for WYA."
    entries = [e for e in entries
               if not (e["kind"] == "competitor" and _no_signal(e["text"]))]

    # Only the companies we actually compete with reach the channel. A low-threat
    # vendor's blog post is a dashboard row, and letting it into a message about
    # competitor movement is what makes the message long enough to skim past.
    #
    # One finding per company, too. On 10 September the message carried three
    # Purple items and one Cisco, which reads as "Purple is everywhere and nobody
    # else exists" - when the truth was that Purple had re-pathed its website and
    # generated 165 spurious pages. So every company that moved gets its first
    # finding before any company gets a second. Room left under the cap then goes
    # to further findings: on 5 October Purple was the only mover, and two of its
    # three verified findings were hidden behind "...and 2 more today".
    #
    # Who did it does not matter (owner, 12 Oct): news ABOUT a competitor that it
    # did not make itself - a third-party developer's demo on Apple Find My, a
    # customer's announcement - counts as that competitor moving. The gate is the
    # company the finding is filed under, never the author of the source.
    #
    # A finding whose implication the claim check deleted has lost the reason it
    # was written (analyse.SYSTEM: no implication, no item). On 11 October two of
    # three findings sent were that - a country dropdown and a "General
    # Improvements" app build - and their leftover fact sentences were shown under
    # "What it means for us". They stay on the dashboard, not in the channel.
    comp = pick_competitors([e for e in entries if e["kind"] == "competitor"
                             and e["level"] in ("high", "medium")
                             and e["means"] != ""])

    # What triggers a send. Asked for on 2026-09-09: a company we compete with
    # moved. Added 2026-10-12 (owner): a big move by one of our own clients also
    # sends on its own - `major` is set by the analyser only for a client finding
    # that changes the account (ownership, insolvency or closure, its own event
    # cancelled or not returning, a new venue, a change of chief executive, a
    # tender for crowd or venue technology). Routine client news and market items
    # still only ride along.
    major = [e for e in entries if e["major"] and e["means"] != ""][:MAX_DAILY_MAJOR]
    if not comp and not major:
        return [], 0
    rest = [e for e in entries if e["kind"] != "competitor"
            and not any(e is m for m in major)]
    kept = comp + major + rest[:MAX_DAILY_OTHERS]
    return kept, max(0, len(entries) - len(kept))


# An analysis entry is written as "here is the fact, here is what it means for us".
# The message used to send only the FIRST sentence, which is always the fact - so
# the judgement, the entire reason the entry exists, never reached the channel. A
# reader got a summary of a competitor's web page and nothing about why they should
# care. These two split the text so both halves arrive, with the implication given
# its own line.
_FACT_MAX = 330
_MEANS_MAX = 460


def _sentences(text):
    """Split on sentence ends, ignoring terminators inside a quotation."""
    out, buf, quoted = [], [], False
    t = (text or "").strip()
    for i, c in enumerate(t):
        buf.append(c)
        if c in '"\u201c\u201d':
            quoted = not quoted
        elif c in ".!?" and not quoted:
            nxt = t[i + 1] if i + 1 < len(t) else " "
            if nxt.isspace():
                out.append("".join(buf).strip())
                buf = []
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return out


def split_finding(text, means=None):
    """(what happened, what it means for us). The second half may be empty.

    `means` is the implication exactly as the analyser wrote it (analyse.py keeps
    it through the claim check). With it the split is exact; "" means the check
    deleted the implication and there is none. Without it - older and hand-written
    entries - the implication is estimated as the tail of the entry. The estimate
    put a fact under "What it means for us" on 10 and 11 October.
    """
    if means is not None:
        t, m = (text or "").strip(), means.strip()
        if not m:
            return t, ""
        if t.endswith(m) and len(m) < len(t):
            return t[: len(t) - len(m)].strip(), m
    sent = _sentences(text)
    if len(sent) < 2:
        return " ".join(sent), ""
    # The implication is the last sentence, plus the one before it when the entry
    # is long enough to have spent two on it.
    cut = len(sent) - (2 if len(sent) >= 4 else 1)
    fact = " ".join(sent[:cut]).strip()
    means = " ".join(sent[cut:]).strip()
    return fact, means


def _clip(t, n):
    """Trim to the last COMPLETE sentence that fits, not to the nearest space.

    Cutting mid-clause and appending an ellipsis produced lines ending "...the
    same page states a Tyne and Wear Metro OpenRoaming\u2026", which reads as a
    truncation error rather than a summary. A whole sentence that stops early is
    better than a longer one that stops nowhere.
    """
    t = (t or "").strip()
    if len(t) <= n:
        return t
    sents = _sentences(t)
    kept = []
    total = 0
    for i, sent in enumerate(sents):
        if total + len(sent) + 1 > n and kept:
            break
        kept.append(i)
        total += len(sent) + 1
    # A caveat is never clipped away. On 10 October the Festyvent finding's "the
    # link to Festyvent is inferred and unconfirmed" was cut for length, and the
    # channel read an inferred attribution as fact.
    kept += [i for i, s in enumerate(sents) if i not in kept and _CAVEAT_RE.search(s)]
    if kept:
        return " ".join(sents[i] for i in sorted(kept))
    return t[:n].rsplit(" ", 1)[0].rstrip(",;:") + "\u2026"


_CAVEAT_RE = re.compile(r"\b(?:inferred|unconfirmed|not confirmed)\b", re.I)


def _unconfirmed(texts):
    """True when every finding for a company carries an attribution caveat: the
    company is then named as a mover only with "(unconfirmed)" after it."""
    return bool(texts) and all(_CAVEAT_RE.search(t or "") for t in texts)


def _one_line(text):
    """First sentence only, plus any caveat sentence - an attribution caveat
    travels with the fact everywhere (CLAUDE.md rule 8). Kept for the weekly
    digest, which is a headline list."""
    sents = _sentences(text)
    if not sents:
        return ""
    return " ".join([sents[0]] + [s for s in sents[1:] if _CAVEAT_RE.search(s)]).rstrip(".")


_KIND_EMOJI = {"competitor": "\U0001f3e2", "client": "\U0001f91d",
               "sector": "\U0001f310", "market": "\U0001f310"}


def held_today_names(days=0):
    """Competing companies whose finding today was held by the claim check. They
    moved; what they did could not be verified. Calling them "checked and quiet"
    is false - on 2 October Purple and Meshh were both listed as quiet."""
    comp = load_json(COMPETITORS) or {}
    names = _names(comp, load_json(HERE / "clients.json") or {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    # Likewise a finding kept on the dashboard but not sent because the check
    # deleted its implication: the company did something, so it is not quiet.
    return sorted({names.get(a.get("competitor_id"), "") for a in
                   ((load_json(ANALYSIS) or {}).get("reviewed") or [])
                   if (a.get("held") or a.get("means") == "")
                   and a.get("date", "") >= cutoff} - {""})


def daily_analysed_payload(entries, dropped=0, unverified=()):
    """One block per finding: what they did, then what it means for us.

    No provenance label. The owner asked on 2026-09-09 for "AI-drafted, not yet
    reviewed" removed everywhere - it was appended to every line and read as a
    disclaimer on work that is reviewed before it ships.
    """
    if not entries:
        return None
    today = datetime.now(timezone.utc).strftime("%-d %B %Y")
    # Each company once as a heading, its findings beneath. On 10 September three
    # Purple items filled the send while six other competitors went unmentioned;
    # build_daily_analysed now gives every mover its first slot before any company
    # gets a second, and the same cap applies here for any other caller.
    comp = pick_competitors([e for e in entries if e["kind"] == "competitor"])
    major = [e for e in entries if e.get("major")]
    rest = [e for e in entries if e["kind"] != "competitor" and not e.get("major")]

    blocks = [{"type": "header", "text": {"type": "plain_text",
               "text": f"WYA market monitor \u2014 {today}"}}]
    movers = sorted({e["name"] for e in comp})
    # A company whose finding was held is neither a mover nor quiet: say nothing.
    quiet = [n for n in competing_names() if n not in movers and n not in unverified]
    shown = [n + (" (unconfirmed)" if _unconfirmed([e["text"] for e in comp
                                                    if e["name"] == n]) else "")
             for n in movers]
    total = len(set(movers) | set(competing_names()))
    line = (f"*{len(movers)} of {total} competitors moved:* {', '.join(shown)}" if movers
            else f"*0 of {total} competitors moved.*")
    # Name the ones that did nothing. "Why is it always the same two companies?"
    # is the right question to ask of a monitor, and the message could not answer
    # it - a competitor missing from the list looked identical whether it had been
    # checked and was quiet, or was never checked because its watch was broken.
    if quiet:
        line += f"\n_Checked and quiet:_ {', '.join(quiet)}"
    # Clients never trigger a send, but silence about them read as "not checked".
    # Name the ones with a finding today, or say there was none.
    with_news = sorted({e["name"] for e in rest + major if e["kind"] == "client"})
    line += (f"\n_Our clients:_ {', '.join(with_news)} (below)" if with_news
             else "\n_Our clients:_ no new findings today")
    # The footer link was the only way through, and it sits below a long message.
    line += f"\n<{SITE}|Open the dashboard \u2192>"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": line}]})

    prev = None
    for e in comp + major:
        fact, means = split_finding(e["text"], e.get("means"))
        head = (f"{_KIND_EMOJI[e['kind']]} *{e['name']}*\n" if e["name"] != prev else "")
        prev = e["name"]
        body = f"{head}{_clip(fact, _FACT_MAX)}"
        if means:
            body += f"\n\n*What it means for us.* {_clip(means, _MEANS_MAX)}"
        # Link to what actually happened, so a reader can check it in one click
        # rather than hunting for it on the dashboard.
        if e.get("url", "").startswith("http"):
            body += f"\n<{e['url']}|Source \u2197>"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})

    if rest:
        blocks.append({"type": "divider"})
        others = "\n".join(
            f"{_KIND_EMOJI.get(e['kind'], '\u2022')} *{e['name']}* \u2014 "
            f"{_clip(_one_line(e['text']), 220)}." for e in rest[:4])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
            f"*Also, on our own accounts and the wider market*\n{others}"}})

    tail = f"...and {dropped} more today. " if dropped else ""
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"{tail}<{SITE}|Open the dashboard> for sources and full detail."}]})
    return {"blocks": blocks}


def daily_payload(lines):
    if not lines:
        return None                       # silence must mean nothing happened
    body = "\n".join(
        (f"• *{l['name']}* ({l['who'].lower()}) — <{l['url']}|{l['title']}>"
         if l["url"] else f"• *{l['name']}* ({l['who'].lower()}) — {l['title']}")
        for l in lines[:6])
    return {"blocks": [
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*Worth knowing today* — {len(lines)} movement"
                 f"{'' if len(lines) == 1 else 's'} across competitors and clients"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": f"Price, hiring, funding and product moves only. <{SITE}|Open the dashboard>"}]},
    ]}


def build(days=DAYS):
    """(headline, [lines], counts) for the window. Pure, so the self-test can check it."""
    comp = load_json(COMPETITORS) or {}
    analysis = load_json(ANALYSIS) or {}
    ids = competing_ids(comp)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    rows = [r for r in read_changes() if when(r) >= cutoff]
    keep = [r for r in rows
            if r.get("entity_type") != "competitor" or r.get("competitor_id") in ids]

    counts = {"competitor": 0, "client": 0, "sector": 0}
    for r in keep:
        if r.get("entity_type") in counts:
            counts[r["entity_type"]] += 1

    # A written analysis is the reason to read this; a headline is not, so analyses come first.
    written = [a for a in (analysis.get("reviewed") or []) if a.get("date", "") >= cutoff[:10]]
    lines = []
    for a in written[:4]:
        first = a["text"].split(". ")[0].rstrip(".")
        lines.append(f"*{a.get('competitor_id', 'market')}* — {first}.")

    seen = set()
    for r in sorted(keep, key=when, reverse=True):
        if len(lines) >= MAX_LINES:
            break
        cid = r.get("competitor_id")
        if cid in seen or r.get("entity_type") != "competitor":
            continue
        seen.add(cid)
        title = (r.get("title") or "")[:150]
        lines.append(f"*{r.get('competitor')}* — {title}")

    headline = (f"{counts['competitor']} competitor movements, {counts['client']} from our "
                f"own accounts, {counts['sector']} from the wider market")
    return headline, lines, counts


def payload(headline, lines):
    body = "\n".join(f"• {ln}" for ln in lines) or "_Nothing moved this week._"
    return {"blocks": [
        {"type": "header",
         "text": {"type": "plain_text", "text": "WYA market monitor — this week"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*{headline}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": (f"Low-threat and consumer companies are excluded from this summary. "
                      f"<{SITE}|Open the dashboard> for sources and detail.")}]},
    ]}


MAX_HIGHLIGHTS = 8   # same cap as build()'s MAX_LINES — "very, very long" is the thing to avoid


def _rank(cid, comp, clients):
    """0 (highest) to 3 (lowest/unknown), from competitor threat or client/sector
    priority. Used only to choose which few things lead the email — never to hide
    anything, since the dashboard itself still shows all of it."""
    order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    for c in (comp.get("competitors") or []):
        if c.get("id") == cid:
            return order.get(c.get("threat"), 3)
    for c in (clients.get("clients") or []) + (clients.get("sector_topics") or []):
        if c.get("id") == cid:
            return order.get(c.get("priority"), 3)
    return 3


def build_weekly_highlights(days=DAYS):
    """(headline, [lines]) filtered and ranked, capped at MAX_HIGHLIGHTS. This is
    "teach it what's important" for an email specifically — a plain, unclassified
    headline with no written judgement and no urgent category never appears here,
    and even the real signal is capped and ranked rather than dumped in full, which
    is the whole complaint this exists to answer. Nothing is hidden from the
    dashboard itself; this only decides what a short email leads with.
    """
    comp = load_json(COMPETITORS) or {}
    clients = load_json(HERE / "clients.json") or {}
    analysis = load_json(ANALYSIS) or {}
    ids = competing_ids(comp)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    rows = [r for r in read_changes() if when(r) >= cutoff]
    keep = [r for r in rows
            if r.get("entity_type") != "competitor" or r.get("competitor_id") in ids]

    counts = {"competitor": 0, "client": 0, "sector": 0}
    for r in keep:
        if r.get("entity_type") in counts:
            counts[r["entity_type"]] += 1

    written = [a for a in (analysis.get("reviewed") or []) if a.get("date", "") >= cutoff[:10]]
    candidates, covered = [], set()
    for a in written:
        first = a["text"].split(". ")[0].rstrip(".")
        cid = a.get("competitor_id", "market")
        candidates.append((_rank(cid, comp, clients), f"{cid}: {first}."))
        covered.add(cid)

    for r in sorted(keep, key=when, reverse=True):
        cid = r.get("competitor_id")
        if cid in covered or not urgency(r):
            continue                      # already covered by analysis, or not urgent
        covered.add(cid)
        title = (r.get("title") or "")[:150]
        candidates.append((_rank(cid, comp, clients), f"{r.get('competitor')}: {title}"))

    candidates.sort(key=lambda x: x[0])
    lines = [text for _, text in candidates[:MAX_HIGHLIGHTS]]
    dropped = len(candidates) - len(lines)
    if dropped > 0:
        lines.append(f"...and {dropped} more this week on the dashboard.")

    headline = (f"{counts['competitor']} competitor movements, {counts['client']} from our "
                f"own accounts, {counts['sector']} from the wider market")
    return headline, lines


def email_body(headline, lines):
    """(subject, text, html). Plain-stdlib templating, no dependency."""
    subject = "WYA market monitor — this week"
    if lines:
        text_items = "\n".join(f"- {ln}" for ln in lines)
        html_items = "\n".join(f"<li>{esc_html(ln)}</li>" for ln in lines)
    else:
        text_items = "Nothing worth flagging moved this week."
        html_items = "<li>Nothing worth flagging moved this week.</li>"
    text = (f"{subject}\n\n{headline}\n\n{text_items}\n\n"
            f"Open the dashboard for sources and everything else collected: {SITE}\n")
    html = (f"<h2 style='margin:0 0 4px'>{esc_html(subject)}</h2>"
            f"<p style='color:#555;margin:0 0 14px'>{esc_html(headline)}</p>"
            f"<ul style='padding-left:18px;line-height:1.6'>{html_items}</ul>"
            f"<p style='margin-top:16px'><a href='{SITE}'>Open the dashboard</a> "
            f"for sources and everything else collected.</p>")
    return subject, text, html


def esc_html(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_BADGE_COLOR = {
    ("competitor", "high"): "#dc2626", ("competitor", "medium"): "#d97706",
    ("competitor", "low"): "#9ca3af", ("competitor", "none"): "#9ca3af",
    ("client", "high"): "#2563eb", ("client", "medium"): "#3b82f6",
    ("client", "low"): "#93c5fd",
    ("sector", "high"): "#7c3aed", ("sector", "medium"): "#8b5cf6",
    ("sector", "low"): "#c4b5fd",
}
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif"


def _badge(kind, level):
    color = _BADGE_COLOR.get((kind, level), "#6b7280")
    label = f"{kind.upper()} · {level.upper()}" if level != "unknown" else kind.upper()
    return color, label


def daily_email_body(entries, dropped=0):
    """(subject, text, html) for the daily analysed update — a real designed email
    (banner, badges, cards, a CTA button), not a plain bullet dump. Table-based and
    inline-styled throughout since that's what actually survives Gmail/Outlook
    rendering, not flexbox/grid."""
    today = datetime.now(timezone.utc).strftime("%A %-d %B %Y")
    subject = f"WYA market monitor — {datetime.now(timezone.utc).strftime('%-d %B %Y')}"
    outer_open = (
        f"<body style='margin:0;padding:0;background:#f1f5f9'>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        f"style='background:#f1f5f9;padding:24px 0'><tr><td align='center'>"
        f"<table role='presentation' width='600' cellpadding='0' cellspacing='0' "
        f"style='background:#ffffff;border-radius:12px;overflow:hidden;"
        f"font-family:{_FONT};max-width:600px;width:100%'>"
        f"<tr><td style='background:#0f172a;padding:28px 32px'>"
        f"<div style='color:#ffffff;font-size:20px;font-weight:700'>WYA Market Monitor</div>"
        f"<div style='color:#94a3b8;font-size:13px;margin-top:4px'>{esc_html(today)}</div>"
        f"</td></tr>")
    outer_close = "</table></td></tr></table></body>"

    if not entries:
        text = f"WYA Market Monitor — {today}\n\nNothing analysed today.\n\n{SITE}\n"
        html = (outer_open +
                f"<tr><td style='padding:32px;color:#64748b;font-size:14px'>"
                f"Nothing analysed today.</td></tr>" + outer_close)
        return subject, text, html

    text_items = "\n\n".join(
        f"- [{e['kind'].upper()}/{e['level'].upper()}] "
        f"{e['name']}: {_one_line(e['text'])}."
        + (f"\n  Source: {'; '.join(e['sources'])}" if e["sources"] else "")
        for e in entries)
    if dropped:
        text_items += f"\n\n...and {dropped} more today on the dashboard."
    text = (f"WYA Market Monitor — {today}\n\n"
            f"{len(entries)} finding{'' if len(entries) == 1 else 's'} today\n\n"
            f"{text_items}\n\nOpen the dashboard for sources and full detail: {SITE}\n")

    stat_row = (
        f"<tr><td style='padding:20px 32px 4px'>"
        f"<span style='display:inline-block;background:#eff6ff;color:#1d4ed8;"
        f"font-size:12px;font-weight:700;letter-spacing:.03em;padding:5px 12px;"
        f"border-radius:999px'>{len(entries)} FINDING"
        f"{'' if len(entries) == 1 else 'S'} TODAY</span></td></tr>")

    cards = []
    for e in entries:
        color, badge = _badge(e["kind"], e["level"])
        source_html = (
            f"<div style='margin-top:8px;font-size:12px;color:#94a3b8'>"
            f"{esc_html('; '.join(e['sources']))}</div>" if e["sources"] else "")
        # No provenance badge: removed everywhere on 2026-09-09 at the owner's
        # request. Every entry renders the same way, as a finding.
        review_html = ""
        cards.append(
            f"<tr><td style='padding:14px 32px'>"
            f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>"
            f"<tr><td width='4' style='background:{color};border-radius:4px'></td>"
            f"<td style='padding:2px 0 2px 14px'>"
            f"<span style='display:inline-block;font-size:10px;font-weight:700;"
            f"letter-spacing:.04em;color:{color};margin-bottom:3px'>{esc_html(badge)}</span><br>"
            f"<span style='font-size:15px;font-weight:700;color:#0f172a'>{esc_html(e['name'])}</span>"
            f"<div style='margin-top:3px;font-size:14px;line-height:1.55;color:#334155'>"
            f"{esc_html(_one_line(e['text']))}.</div>"
            f"{review_html}"
            f"{source_html}"
            f"</td></tr></table></td></tr>"
            f"<tr><td style='padding:0 32px'><div style='border-top:1px solid #f1f5f9'></div></td></tr>")
    cards_html = "".join(cards)

    tail = (f"<tr><td style='padding:12px 32px 0;color:#94a3b8;font-size:13px'>"
            f"...and {dropped} more today on the dashboard.</td></tr>" if dropped else "")
    cta = (
        f"<tr><td style='padding:28px 32px 32px'>"
        f"<a href='{SITE}' style='display:inline-block;background:#0f172a;color:#ffffff;"
        f"text-decoration:none;font-size:14px;font-weight:600;padding:12px 22px;"
        f"border-radius:8px'>Open the dashboard →</a></td></tr>")

    html = outer_open + stat_row + cards_html + tail + cta + outer_close
    return subject, text, html


def send_email(subject, text, html):
    """Sends via SMTP using only the standard library. Returns None on success,
    or a string explaining why nothing was sent (never raises for a missing config —
    an unset secret is the normal state before setup, same convention as Slack)."""
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASS", "").strip()
    to = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    if not (host and user and pw and to):
        return ("SMTP_HOST/SMTP_USER/SMTP_PASS/EMAIL_TO not fully set — nothing sent. "
                "Use --email --dry-run to preview the message.")
    port = int(os.environ.get("SMTP_PORT", "587"))
    sender = os.environ.get("EMAIL_FROM", "").strip() or user
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(sender, to, msg.as_string())
    return None


def post(url, data):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f"could not reach Slack: {e}")


# ---------------------------------------------------------------- health

# You chose "Slack only when something moved", which is the honest default but
# leaves one hole: a silent channel looks identical whether the market was quiet
# or the pipeline is dead. It was dead-ish for a fortnight - scheduled runs
# failing, only manual clicks getting data through - and nothing said so. These
# two messages close that hole without adding noise on a genuinely quiet day:
# they fire ONLY when something is actually wrong.
STALE_HOURS = 48


def hours_since_collection():
    """Hours since the newest collected item, or None if there are none at all."""
    newest = ""
    for row in read_changes():
        seen = row.get("seen_at") or ""
        if seen > newest:
            newest = seen
    if not newest:
        return None
    try:
        then = datetime.fromisoformat(newest)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def health_payload(hours):
    """A message only when collection has gone stale. None means all is well."""
    if hours is None:
        text = ("*WYA market monitor — no data at all.* changes.jsonl is empty. "
                "The collector has never produced a row.")
    elif hours > STALE_HOURS:
        text = (f"*WYA market monitor — no new data for {hours:.0f} hours.* "
                f"The daily collection has not produced anything since then, so a "
                f"quiet channel right now means the pipeline is stuck, not that the "
                f"market is quiet. Check the most recent workflow run.")
    else:
        return None
    return {"blocks": [{"type": "section",
                        "text": {"type": "mrkdwn", "text": text}},
                       {"type": "context", "elements": [
                           {"type": "mrkdwn",
                            "text": f"<{SITE}|Open the dashboard>"}]}]}


def failure_payload(run_url=""):
    """Posted by the workflow when a step fails. The run URL is the whole point."""
    tail = f" <{run_url}|See the run.>" if run_url else ""
    return {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text":
            "*WYA market monitor — today's run failed.* No collection, no analysis "
            "and no update today. This message exists because a failed run used to "
            "look exactly like a quiet market." + tail}}]}


def analysis_failed_payload(run_url=""):
    """The run succeeded but the analysis step did not - no credit, a bad key, an
    API outage. Collection still happened, so the run itself is green and the
    daily message would simply not arrive, which reads as a quiet market. Found
    2026-10-02 when the Anthropic account ran out of credit mid-run."""
    tail = f" <{run_url}|See the run.>" if run_url else ""
    return {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text":
            "*WYA market monitor — today's analysis did not run.* Competitors were "
            "collected, but no findings were written or checked, so there is no "
            "update today. Most likely cause: the Anthropic API account is out of "
            "credit or the key is invalid." + tail}}]}


def selftest():
    """Offline. No network, no webhook needed."""
    assert "did not run" in json.dumps(analysis_failed_payload("https://x.test/r"))
    # Health: silent when fresh, loud when stale. A regression here brings back the
    # exact failure mode this was written for - a dead pipeline that looks quiet.
    assert health_payload(1.0) is None, "fresh data must not raise an alert"
    assert health_payload(STALE_HOURS - 1) is None
    stale = health_payload(STALE_HOURS + 1)
    assert stale and "no new data" in json.dumps(stale)
    assert health_payload(None) and "no data at all" in json.dumps(health_payload(None))
    assert "failed" in json.dumps(failure_payload())
    assert "run/9" in json.dumps(failure_payload("https://gh.test/run/9"))

    headline, lines, counts = build(days=3650)
    assert isinstance(lines, list)
    assert "competitor movements" in headline, headline
    p = payload(headline, lines)
    assert p["blocks"][0]["type"] == "header"
    blob = json.dumps(p)
    assert "hooks.slack.com" not in blob, "a webhook URL must never be embedded"
    # an empty week must still produce a valid, honest message
    empty = payload("0 competitor movements, 0 from our own accounts, 0 from the wider market", [])
    assert "Nothing moved" in json.dumps(empty)
    # a quiet day must produce no message at all
    assert daily_payload([]) is None, "an empty day must send nothing"
    assert urgency({"title": "pricing page changed"}) == "price"
    assert urgency({"title": "Purple appoints Growth Director"}) == "hire"
    assert urgency({"title": "Acme raises Series B"}) == "money"
    assert urgency({"title": "Ten tips for better events"}) is None, \
        "routine content must not page anyone"
    dl, _ = build_daily(days=3650)
    assert all(x["who"] in ("CLIENT", "COMPETITOR") for x in dl)

    entries, dropped = build_daily_analysed(days=3650)
    assert isinstance(entries, list) and dropped >= 0
    assert all({"name", "text", "sources", "generated", "model"} <= e.keys() for e in entries)
    assert daily_analysed_payload([]) is None, "no analysis today must send nothing"
    if entries:
        p3 = daily_analysed_payload(entries, dropped)
        assert p3["blocks"][0]["type"] == "header"
        assert json.dumps(p3).count("hooks.slack.com") == 0
        assert len(entries) <= MAX_DAILY_ANALYSED

    # The provenance label is gone from every channel, and stays gone. This
    # assertion is the inverse of the one it replaces: that one required
    # "AI-drafted, not yet reviewed" to appear on a generated entry, and it was
    # appended to every line of every message. Removed 2026-09-09 at the owner's
    # request - the analysis is reviewed before it ships.
    fake_gen = [{"name": "Test Co", "kind": "competitor", "level": "high",
                 "text": "Fabricated for selftest. It means nothing.",
                 "sources": [], "generated": True, "model": "claude-opus-5"}]
    for blob in (json.dumps(daily_analysed_payload(fake_gen, 0)),
                 *daily_email_body(fake_gen, 0)):
        low = blob.lower()
        for banned in ("not yet reviewed", "ai-drafted", "ai draft", "automated analysis"):
            assert banned not in low, f"{banned!r} must not appear in any message"

    # A "no signal" entry is a dashboard record, not a message. One send led with
    # a competitor under the sentence "This does not change anything for WYA."
    for t in ("No signal: recorded so the absence is visible.",
              "This does not change anything for WYA.",
              "Routine posting, not a new signal.",
              "Nothing changed; no action follows from this item.",
              "Worth re-reading rather than acting on.",
              "This reads as recycled or migrated content rather than new thinking."):
        assert _no_signal(t), t
    assert not _no_signal("They raised their published rate to eight pounds.")

    # Low-threat competitors do not reach the channel.
    mixed = [{"name": "Big", "kind": "competitor", "level": "high", "sources": [],
              "text": "They moved. It matters."},
             {"name": "Small", "kind": "competitor", "level": "low", "sources": [],
              "text": "They blogged. It is minor."}]
    names = json.dumps(daily_analysed_payload(
        [e for e in mixed if e["level"] in ("high", "medium")], 0))
    assert "Big" in names and "Small" not in names

    # One finding per company. Three Purple items shipped in a single message on
    # 10 September while six other competitors went unmentioned.
    three = [{"name": "Purple", "kind": "competitor", "level": "high", "sources": [],
              "text": f"Purple did thing {i}. It matters somewhat."} for i in range(3)]
    three.append({"name": "Pointr", "kind": "competitor", "level": "high",
                  "sources": [], "text": "Pointr moved. That is material."})
    body = json.dumps(daily_analysed_payload(three, 0))
    assert body.count("*Purple*") == 1, "a company is one heading, its findings beneath"
    assert "*Pointr*" in body
    assert all(f"Purple did thing {i}" in body for i in range(3)), \
        "with room under the cap, a mover's other findings are shown, not hidden"
    # A busy day: six movers, the first five each get a slot before any second one.
    busy = [{"name": f"Co{i}", "kind": "competitor", "level": "high", "sources": [],
             "text": f"Co{i} moved. It matters."} for i in range(6)]
    busy.insert(1, {"name": "Co0", "kind": "competitor", "level": "high", "sources": [],
                    "text": "Co0 again. It matters."})
    b2 = json.dumps(daily_analysed_payload(busy, 0))
    assert all(f"*Co{i}*" in b2 for i in range(5)) and "*Co5*" not in b2

    # Every competing company is either a mover or named as quiet, so silence about
    # one is always a stated finding rather than an absence to interpret.
    names_all = competing_names()
    assert names_all, "competing_names() must not be empty"
    ctx = json.dumps(daily_analysed_payload(
        [{"name": names_all[0], "kind": "competitor", "level": "high",
          "sources": [], "text": "They moved. It matters."}], 0))
    assert "Checked and quiet" in ctx and names_all[-1] in ctx
    # A company whose finding was held is not "checked and quiet".
    ctx2 = json.dumps(daily_analysed_payload(
        [{"name": names_all[0], "kind": "competitor", "level": "high",
          "sources": [], "text": "They moved. It matters."}], 0, [names_all[-1]]))
    assert names_all[-1] not in ctx2, "a held company must not be called quiet"
    assert "no new findings today" in ctx2, "a day without client news must say so"
    ctx3 = json.dumps(daily_analysed_payload(
        [{"name": names_all[0], "kind": "competitor", "level": "high", "sources": [],
          "text": "They moved. It matters."},
         {"name": "Some Client", "kind": "client", "level": "high", "sources": [],
          "text": "A client moved. It matters."}], 0))
    assert "Some Client (below)" in ctx3, "a client with news is named in the header"
    assert f"of {len(names_all)} competitors" in ctx2, "the total stays the total"

    # Nothing is sent unless a competitor moved. Client and market findings ride
    # along when one has, but must never trigger a message by themselves.
    only_clients = [{"date": "2999-01-01", "competitor_id": "informa",
                     "text": "A client did a thing. It matters somewhat.",
                     "sources": []}]
    saved = ANALYSIS.read_text() if ANALYSIS.exists() else None
    try:
        ANALYSIS.write_text(json.dumps({"reviewed": only_clients}))
        ents, _ = build_daily_analysed(days=400000)
        assert ents == [], "routine client findings must not trigger a send"
        assert daily_analysed_payload(ents, 0) is None
        # A big client move sends on its own (owner, 12 Oct), shown in full.
        ANALYSIS.write_text(json.dumps({"reviewed": only_clients + [
            {"date": "2999-01-01", "competitor_id": "informa", "major": True,
             "text": "Informa agreed to buy Clarion for 2.24bn. Our largest exhibition "
                     "account changes owner.", "means": "Our largest exhibition "
                     "account changes owner.", "sources": []}]}))
        ents, _ = build_daily_analysed(days=400000)
        _mj = json.dumps(daily_analysed_payload(ents, 0))
        assert "agreed to buy Clarion" in _mj, "a major client move must send alone"
        assert "*0 of " in _mj and "What it means for us.* Our largest" in _mj, _mj[:600]
        assert "Informa (below)" in _mj
        # `major` on a competitor or market entry is ignored.
        ANALYSIS.write_text(json.dumps({"reviewed": [
            {"date": "2999-01-01", "competitor_id": "festival-industry-health",
             "major": True, "text": "A festival was cancelled. It matters.",
             "sources": []}]}))
        assert build_daily_analysed(days=400000)[0] == [], "major is for clients only"
        # Third-party news about a competitor counts as that competitor moving
        # (owner, 12 Oct): the gate is the company it is filed under.
        _hi = next(c["id"] for c in (load_json(COMPETITORS) or {})["competitors"]
                   if c.get("threat") == "medium")
        ANALYSIS.write_text(json.dumps({"reviewed": [
            {"date": "2999-01-01", "competitor_id": _hi, "text": "A third-party "
             "developer demoed a tracker on the network. It widens the use.",
             "means": "It widens the use.", "sources": []}]}))
        ents, _ = build_daily_analysed(days=400000)
        assert len(ents) == 1, "news about a competitor, not by it, still counts"
        # The check deleted the implication: dashboard only, and not "quiet".
        ANALYSIS.write_text(json.dumps({"reviewed": [
            {"date": datetime.now(timezone.utc).date().isoformat(),
             "competitor_id": _hi, "verified": "trimmed",
             "text": "The app moved to 1.10 with General Improvements.", "means": "",
             "sources": []}]}))
        assert build_daily_analysed(days=400000)[0] == [], \
            "a finding stripped of its implication must not reach the channel"
        _nm = _names(load_json(COMPETITORS) or {}, {})
        assert _nm[_hi] in held_today_names(), "...nor be called quiet"
    finally:
        if saved is not None:
            ANALYSIS.write_text(saved)

    # The judgement must reach the channel, not just the fact. Sending only the
    # first sentence meant the reader got a summary of a web page and nothing
    # about why it mattered.
    fact, means = split_finding(
        "Purple published a case study with 786,817 logins. It also names G-Cloud 15. "
        "That gives them a public-sector buying route. The same buyers see it first.")
    assert "786,817" in fact and "public-sector" in means, (fact, means)
    assert split_finding("One sentence only.")[1] == ""
    # With the implication stored, the split is exact - 10 Oct put "Its
    # Find-a-Friend feature requires contacts access" under "What it means".
    _t = "A. B fact. C fact. D fact. The meaning."
    assert split_finding(_t, "The meaning.") == ("A. B fact. C fact. D fact.", "The meaning.")
    assert split_finding(_t, "") == (_t, ""), "no implication left: none is shown"
    _b = json.dumps(daily_analysed_payload(
        [{"name": "X", "kind": "competitor", "level": "high", "sources": [],
          "text": "They did a thing. It has a detail.", "means": ""}], 0))
    assert "What it means" not in _b, "a fact must never be labelled the implication"
    # A caveat survives clipping, and the mover is marked (Festyvent, 10 Oct).
    _fv = ("Version 1.4.0 of the University of Portsmouth Open Days app shipped on "
           "8 October with an improved login process and updated screenshots, "
           "following 1.3.0 in August. The app is published by Clarifi Media, whose "
           "portfolio includes Boardmasters, Victorious, Hideout, Snowboxx and Slam "
           "Dunk 2027, and routes support to a festyvent.com address; the link to "
           "Festyvent is inferred and unconfirmed. Its Find-a-Friend feature "
           "requires contacts access and background location. It matters.")
    _b = json.dumps(daily_analysed_payload(
        [{"name": "Festyvent", "kind": "competitor", "level": "high", "sources": [],
          "text": _fv, "means": "It matters."}], 0))
    assert "inferred and unconfirmed" in _b, "the caveat must reach the channel"
    assert "Festyvent (unconfirmed)" in _b, _b[:400]
    assert "inferred" in _one_line(_fv), "the caveat travels in one-line form too"
    # A terminator inside a quotation must not split the text.
    f2, _ = split_finding('They ask "Got Licenses? Find Them!" on the page. It works.')
    assert "Find Them" in f2, f2
    body = json.dumps(daily_analysed_payload(
        [{"name": "X", "kind": "competitor", "level": "high", "sources": [],
          "text": "They did a thing. That changes our position."}], 0))
    assert "What it means for us" in body, "the implication must be in the message"

    # Clipping lands on a sentence end, never mid-clause with a dangling ellipsis.
    long = ("One sentence here. A second sentence follows it. "
            "A third that will not fit inside a short budget.")
    c = _clip(long, 60)
    assert c.endswith("."), c
    assert "\u2026" not in c, c
    assert _clip("Short.", 100) == "Short."

    # Look for a webhook with real path segments, not the placeholder in the docstring —
    # and not this assertion's own text, which is why this is a shape check, not a
    # substring check.
    import re
    real = re.search(r"hooks\.slack\.com/services/T[A-Z0-9]{5,}", Path(__file__).read_text())
    assert not real, "a real webhook URL appears to be hard-coded in this file"

    hl, hlines = build_weekly_highlights(days=3650)
    assert isinstance(hlines, list)
    assert "competitor movements" in hl, hl
    # every raw-item line (the ones after the written-analysis lines) must be for a
    # movement urgency() actually classifies — a plain, unclassified headline with no
    # analysis and no urgent category is exactly the noise this filter must drop.
    analysed_ids = {a.get("competitor_id") for a in (load_json(ANALYSIS) or {}).get("reviewed", [])}
    for r in read_changes():
        if r.get("competitor_id") in analysed_ids or urgency(r):
            continue
        line = f"{r.get('competitor')}: {(r.get('title') or '')[:150]}"
        assert line not in hlines, f"unanalysed, non-urgent row leaked in: {line!r}"
    subj, text, html = email_body(hl, hlines)
    assert "WYA market monitor" in subj
    assert SITE in text and SITE in html
    # The existing asserts passed with SITE == "" ("" is in every string), which is
    # how the dead link shipped. Check the thing itself.
    assert SITE.startswith("https://"), f"dashboard link is {SITE!r}"

    # Today's findings only, and today's wins the one-per-company slot.
    _today = datetime.now(timezone.utc).date().isoformat()
    _yday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    _cid = next(c["id"] for c in (load_json(COMPETITORS) or {})["competitors"]
                if c.get("threat") == "high")
    _saved = ANALYSIS.read_text() if ANALYSIS.exists() else None
    try:
        ANALYSIS.write_text(json.dumps({"reviewed": [
            {"date": _yday, "competitor_id": _cid, "text": "OLD finding. It mattered.", "sources": []},
            {"date": _today, "competitor_id": _cid, "text": "NEW finding. It matters.", "sources": []}]}))
        _e, _ = build_daily_analysed()
        _body = json.dumps(daily_analysed_payload(_e, 0))
        assert "NEW finding" in _body and "OLD finding" not in _body, _body[:300]
        # A finding held by the claim check is never sent.
        ANALYSIS.write_text(json.dumps({"reviewed": [
            {"date": _today, "competitor_id": _cid, "text": "HELD finding. Unsupported.",
             "sources": [], "held": True}]}))
        _e, _ = build_daily_analysed()
        assert _e == [], "a held finding must not reach Slack or email"
    finally:
        if _saved is not None:
            ANALYSIS.write_text(_saved)
    _pl = json.dumps(daily_analysed_payload(
        [{"name": "X", "kind": "competitor", "level": "high", "sources": [],
          "url": "https://x.test/a", "text": "They moved. It matters."}], 0))
    assert "https://x.test/a|Source" in _pl, "each finding links to its source"
    assert _pl.count(SITE) >= 2, "dashboard link at the top and the foot"
    assert "<script" not in html.lower()
    empty_subj, empty_text, empty_html = email_body("0 movements", [])
    assert "Nothing worth flagging" in empty_text
    # a title containing HTML-significant characters must be escaped, not injected
    esc = esc_html("<b>x</b> & \"y\"")
    assert "<b>" not in esc and "&lt;b&gt;" in esc
    # Force the "not configured" path regardless of the real environment — this must
    # never attempt a real network connection during a selftest, so the relevant vars
    # are cleared and restored rather than trusted to already be absent.
    smtp_vars = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_TO")
    saved = {k: os.environ.pop(k, None) for k in smtp_vars}
    try:
        result = send_email("t", "body", "<p>body</p>")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    assert result is not None and "not fully set" in result, \
        "sending must degrade gracefully, never raise, when SMTP secrets are absent"

    d_entries, d_dropped = build_daily_analysed(days=3650)
    d_subj, d_text, d_html = daily_email_body(d_entries, d_dropped)
    assert "WYA market monitor" in d_subj
    assert SITE in d_text and SITE in d_html
    assert "<script" not in d_html.lower()
    for e in d_entries:
        assert esc_html(e["name"]) in d_html
    empty_subj, empty_text, empty_html = daily_email_body([], 0)
    assert "Nothing analysed today" in empty_text and "Nothing analysed today" in empty_html
    print("notify selftest OK")


def _send(data, dry, label):
    """Post `data`, or print it under --dry-run. Shared by --health and --failed."""
    if data is None:
        print(f"{label}: nothing to send")
        return
    if dry:
        print(json.dumps(data, indent=2))
        return
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("SLACK_WEBHOOK_URL not set — nothing sent.")
        return
    status, body = post(url, data)
    print(f"slack responded {status} {body}")
    if status >= 300:
        raise SystemExit(1)


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if "--health" in argv:
        hours = hours_since_collection()
        print(f"newest item is {hours:.1f}h old"
              if hours is not None else "no collected items at all")
        return _send(health_payload(hours), "--dry-run" in argv, "health")
    if "--analysis-failed" in argv:
        run = next((a.split("=", 1)[1] for a in argv if a.startswith("--run-url=")), "")
        return _send(analysis_failed_payload(run), "--dry-run" in argv, "analysis failure")
    if "--failed" in argv:
        run = next((a.split("=", 1)[1] for a in argv if a.startswith("--run-url=")), "")
        return _send(failure_payload(run), "--dry-run" in argv, "failure")
    if "--daily" in argv:
        entries, dropped = build_daily_analysed()
        data = daily_analysed_payload(entries, dropped, held_today_names())
        if data is None:
            print("no competitor moved today — no message sent")
            return
        if "--dry-run" in argv:
            for b in data["blocks"]:
                t = (b.get("text") or {}).get("text")
                if t is None and b.get("elements"):
                    t = " ".join(e.get("text", "") for e in b["elements"])
                print(("\n" + (t or "")) if b["type"] != "divider" else "\n---")
            return
        url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        if not url:
            print("SLACK_WEBHOOK_URL not set — nothing sent.")
            return
        status, body = post(url, data)
        print(f"slack responded {status} {body}")
        if status >= 300:
            raise SystemExit(1)
        return
    if "--email" in argv:
        entries, dropped = build_daily_analysed()
        subject, text, html = daily_email_body(entries, dropped)
        if "--dry-run" in argv:
            print(text)
            return
        result = send_email(subject, text, html)
        print(result if result else f"email sent to {os.environ.get('EMAIL_TO', '')}")
        return
    if "--email-weekly" in argv:
        headline, hlines = build_weekly_highlights()
        subject, text, html = email_body(headline, hlines)
        if "--dry-run" in argv:
            print(text)
            return
        result = send_email(subject, text, html)
        print(result if result else f"email sent to {os.environ.get('EMAIL_TO', '')}")
        return
    headline, lines, _ = build()
    data = payload(headline, lines)
    if "--dry-run" in argv:
        print(headline)
        for ln in lines:
            print(" •", ln)
        return
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        # Absent secret is the normal state before setup, not a failure.
        print("SLACK_WEBHOOK_URL not set — nothing sent. Use --dry-run to preview.")
        return
    status, body = post(url, data)
    print(f"slack responded {status} {body}")
    if status >= 300:
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
