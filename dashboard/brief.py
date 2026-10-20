#!/usr/bin/env python3
"""Render one competitor to a standalone, printable brief. Python stdlib only.

Produces the same document shape as the existing internal competitor brief:
snapshot, products and mechanisms, strengths, weaknesses, head-to-head
comparison, what to know before pitching, assessment, sources.

Output is self-contained HTML that prints to PDF cleanly (Cmd-P, Save as PDF)
so it can be circulated like the original brief. No dependencies, no LaTeX.

Usage:
    python3 brief.py meshh            # writes briefs/meshh.html
    python3 brief.py --all            # every competitor with real content
    python3 brief.py --list           # ids and how complete each one is
    python3 brief.py --selftest
"""

import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
CONFIG = HERE / "competitors.json"
OUTDIR = HERE / "briefs"

# A brief is worth generating once a competitor has real assessed content.
# Below this it would be a page of "not yet assessed" headings.
MIN_SECTIONS = 3


def esc(s):
    return html.escape(str(s if s is not None else ""))


def completeness(c):
    return sum(1 for k in ("snapshot", "products", "strengths", "weaknesses",
                           "vs_wya", "know_before_you_pitch") if c.get(k))


def conf_badge(item):
    cf = item.get("confidence")
    if not cf:
        return ""
    return f'<span class="b b-{esc(cf)}">{esc(cf)}</span>'


def src_line(item):
    bits = []
    if item.get("source"):
        bits.append(esc(item["source"]))
    return (f'{conf_badge(item)}'
            f'{f"<span class=s>{bits[0]}</span>" if bits else ""}')


def bullets(items, empty="Not yet assessed."):
    if not items:
        return f'<p class="none">{empty}</p>'
    out = ['<ul>']
    for i in items:
        note = (f'<div class="n">{esc(i["note"])}</div>'
                if i.get("note") else "")
        out.append(f'<li>{esc(i.get("text",""))} {src_line(i)}{note}</li>')
    out.append('</ul>')
    return "".join(out)


CSS = """
@page{size:A4;margin:16mm}
*{box-sizing:border-box}
body{margin:0;font-family:Calibri,"Segoe UI",system-ui,sans-serif;
 font-size:10.6pt;line-height:1.5;color:#111;background:#fff}
.page{max-width:190mm;margin:0 auto;padding:0 6mm 18mm}
header{border-bottom:3px solid #00C9A7;padding:14mm 0 5mm;margin-bottom:7mm}
h1{margin:0;font-size:19pt;letter-spacing:-.01em}
.sub{color:#555;font-size:10pt;margin-top:2mm;max-width:150mm}
.badges{margin-top:4mm;display:flex;gap:3mm;flex-wrap:wrap;align-items:center}
.t{font-size:7.5pt;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
 padding:1mm 2.5mm;border-radius:1mm}
.t-high{background:#fde8e8;color:#991b1b}
.t-medium{background:#fdf1dc;color:#a1560a}
.t-low,.t-none{background:#eceef1;color:#4a5158}
h2{font-size:12.5pt;margin:8mm 0 2mm;padding-bottom:1.5mm;
 border-bottom:1.5pt solid #00C9A7;page-break-after:avoid}
h3{font-size:10.8pt;margin:4mm 0 1mm;page-break-after:avoid}
p{margin:0 0 2.5mm}
.warn{background:#fdf1dc;border-left:3pt solid #d97706;padding:3mm 4mm;
 margin:0 0 5mm;font-size:9.8pt}
.status{background:#f4f5f7;border-radius:2mm;padding:3mm 4mm;margin:0 0 5mm;
 font-size:9.8pt}
dl{display:grid;grid-template-columns:52mm 1fr;gap:2mm 5mm;margin:0}
dt{font-weight:700;color:#444;font-size:9.6pt}
dd{margin:0;color:#222}
ul{margin:0;padding-left:5mm}
li{margin-bottom:2.5mm;page-break-inside:avoid}
.b{display:inline-block;font-size:7pt;font-weight:700;letter-spacing:.05em;
 text-transform:uppercase;padding:.4mm 1.6mm;border-radius:1mm;
 vertical-align:1px;margin-right:1.5mm}
.b-certain{background:#d9f2e6;color:#046b4e}
.b-likely{background:#eceef1;color:#4a5158}
.b-unverified{background:#fdf1dc;color:#a1560a}
.b-stale{background:#fde8e8;color:#991b1b}
.b-unknown{background:#eceef1;color:#6b7280}
.s{color:#6b7280;font-size:8.6pt}
.n{color:#555;font-size:9pt;margin-top:1mm;padding-left:3mm;
 border-left:1.5pt solid #e2e5ea}
.none{color:#6b7280;font-style:italic}
table{border-collapse:collapse;width:100%;font-size:9.4pt;margin:0 0 4mm}
th,td{text-align:left;padding:2mm 2.5mm;border-bottom:.5pt solid #dfe2e6;
 vertical-align:top}
thead th{background:#f4f5f7;font-size:8pt;text-transform:uppercase;
 letter-spacing:.04em;color:#555}
tr{page-break-inside:avoid}
.eW{color:#046b4e;font-weight:700}
.eT{color:#991b1b;font-weight:700}
.eD{color:#a1560a;font-weight:700}
.prod{border:.5pt solid #dfe2e6;border-radius:2mm;padding:3mm 4mm;
 margin-bottom:3mm;page-break-inside:avoid}
.pill{font-size:7pt;font-weight:700;text-transform:uppercase;padding:.4mm 1.6mm;
 border-radius:1mm;margin-left:2mm}
.p-current{background:#d9f2e6;color:#046b4e}
.p-de-emphasised{background:#fdf1dc;color:#a1560a}
.p-discontinued,.p-gone{background:#fde8e8;color:#991b1b}
.assess{background:#f4f5f7;border-left:3pt solid #00C9A7;padding:4mm 5mm;
 border-radius:0 2mm 2mm 0}
footer{margin-top:9mm;padding-top:3mm;border-top:.5pt solid #dfe2e6;
 color:#6b7280;font-size:8.6pt}
@media print{.page{padding:0}header{padding-top:0}}
"""


def render(c, meta):
    edge_cls = {"WYA": "eW", "Draw": "eD"}
    name = c.get("name", c["id"])
    h = [f"""<!DOCTYPE html><html lang="en-GB"><head><meta charset="utf-8">
<title>{esc(name)} — Competitive Intelligence Brief</title>
<style>{CSS}</style></head><body><div class="page">
<header>
<h1>{esc(name)}: Competitive Intelligence Brief</h1>
<div class="sub">What they do, how they do it, and where they are genuinely strong
or exposed &mdash; neutral analysis for internal use.</div>
<div class="badges">
  <span class="t t-{esc(c.get('threat','low'))}">{esc(c.get('threat','low'))} threat</span>
  <span class="s">Assessed {esc(c.get('assessed') or meta.get('compiled',''))}</span>
  <span class="s">{esc(c.get('website',''))}</span>
</div>
</header>"""]

    if c.get("name_warning"):
        h.append(f'<div class="warn"><b>Naming.</b> {esc(c["name_warning"])}</div>')
    h.append(f'<p><b>{esc(c.get("one_liner",""))}</b></p>')
    if c.get("status_detail"):
        h.append(f'<div class="status"><b>Current status.</b> '
                 f'{esc(c["status_detail"])}</div>')

    if c.get("snapshot"):
        h.append("<h2>Company snapshot</h2><dl>")
        for s in c["snapshot"]:
            note = f'<div class="n">{esc(s["note"])}</div>' if s.get("note") else ""
            h.append(f'<dt>{esc(s.get("label",""))}</dt>'
                     f'<dd>{esc(s.get("value",""))} {src_line(s)}{note}</dd>')
        h.append("</dl>")

    if c.get("products"):
        h.append("<h2>Products and how they work</h2>")
        for p in c["products"]:
            st = p.get("status", "")
            note = (f'<div class="n">{esc(p["note"])}</div>' if p.get("note") else "")
            h.append(f'''<div class="prod"><b>{esc(p.get("name",""))}</b>
<span class="pill p-{esc(st)}">{esc(st)}</span>{conf_badge(p)}
<p style="margin:2mm 0 1mm"><b>Mechanism.</b> {esc(p.get("mechanism",""))}</p>
<p style="margin:0"><b>Use case.</b> {esc(p.get("use_case",""))}</p>{note}</div>''')

    h.append("<h2>Strengths</h2>" + bullets(c.get("strengths")))
    h.append("<h2>Weaknesses</h2>" + bullets(c.get("weaknesses")))

    if c.get("vs_wya"):
        h.append(f"<h2>WYA compared with {esc(name)}</h2>"
                 "<table><thead><tr><th style='width:32mm'>Dimension</th>"
                 f"<th>WYA</th><th>{esc(name)}</th>"
                 "<th style='width:22mm'>Advantage</th></tr></thead><tbody>")
        for d in c["vs_wya"]:
            note = (f'<div class="n">{esc(d["note"])}</div>' if d.get("note") else "")
            h.append(f'''<tr><td><b>{esc(d.get("dimension",""))}</b><br>{src_line(d)}</td>
<td>{esc(d.get("wya",""))}</td><td>{esc(d.get("them",""))}</td>
<td class="{edge_cls.get(d.get("edge"),"eT")}">{esc(d.get("edge",""))}{note}</td></tr>''')
        h.append("</tbody></table>")

    if c.get("know_before_you_pitch"):
        h.append("<h2>Know before you pitch</h2>" +
                 bullets(c["know_before_you_pitch"], ""))

    judged = recent_analyses(c["id"])
    if judged:
        h.append(f"<h2>What we have read into their recent movement</h2>"
                 f'<p class="note">{len(judged)} written judgement'
                 f'{"" if len(judged) == 1 else "s"} in the last {RECENT_DAYS} days, '
                 f"newest first.</p><ul>")
        for e in judged:
            src = " &middot; ".join(esc(s) for s in (e.get("sources") or [])[:2])
            h.append(f'<li><b>{esc(e["date"])}.</b> {esc(e["text"])}'
                     + (f'<div class="note">{src}</div>' if src else "") + "</li>")
        h.append("</ul>")

    if c.get("bottom_line"):
        h.append(f'<h2>Assessment</h2><div class="assess">'
                 f'{esc(c["bottom_line"])}</div>')

    if c.get("sources"):
        h.append("<h2>Sources</h2><ul>" +
                 "".join(f"<li>{esc(s)}</li>" for s in c["sources"]) + "</ul>")

    rules = meta.get("hard_rules", [])
    h.append(f"""<footer>
<b>Confidence levels.</b> certain = verifiable in a named public source or stated
in our own product material; likely = strong inference from a named source;
unverified = asserted but unconfirmed; stale = accurate when recorded, since
superseded.<br><br>
<b>Standards applied.</b> {"; ".join(esc(r) for r in rules)}<br><br>
{esc(meta.get('classification','Internal use only'))} Compiled for internal WYA
use &mdash; not for external distribution.
</footer></div></body></html>""")
    return "\n".join(h)


ANALYSIS = HERE / "analysis.json"
RECENT_DAYS = 30


def recent_analyses(cid, days=RECENT_DAYS):
    """The written judgements about this company, newest first.

    The brief is the one page that belongs to a single competitor, and until now it
    carried every standing fact about them and none of the reading of what they have
    actually been doing. The dashboard shows one card per company and points here for
    the rest, so the rest has to be here.
    """
    if not ANALYSIS.exists():
        return []
    try:
        entries = json.loads(ANALYSIS.read_text()).get("reviewed") or []
    except json.JSONDecodeError:
        return []
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    rows = [e for e in entries
            if e.get("competitor_id") == cid and e.get("text") and e.get("date", "") >= cutoff]
    return sorted(rows, key=lambda e: e["date"], reverse=True)


def build(ids=None, quiet=False):
    cfg = json.loads(CONFIG.read_text())
    meta = cfg.get("meta", {})
    by_id = {c["id"]: c for c in cfg["competitors"]}
    if ids:
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise SystemExit(f"unknown competitor id(s): {', '.join(missing)}\n"
                             f"available: {', '.join(sorted(by_id))}")
        chosen = [by_id[i] for i in ids]
    else:
        chosen = [c for c in cfg["competitors"]
                  if completeness(c) >= MIN_SECTIONS]
        skipped = [c["id"] for c in cfg["competitors"]
                   if completeness(c) < MIN_SECTIONS]
        if skipped and not quiet:
            print(f"skipping {len(skipped)} not yet assessed in enough depth: "
                  f"{', '.join(skipped)}")
    OUTDIR.mkdir(exist_ok=True)
    written = []
    for c in chosen:
        p = OUTDIR / f"{c['id']}.html"
        p.write_text(render(c, meta))
        written.append(p)
        if not quiet:
            print(f"wrote {p.relative_to(HERE)}  "
                  f"({completeness(c)}/6 sections, threat {c.get('threat','?')})")
    if not quiet:
        print(f"\n{len(written)} brief(s). Open in a browser and print to PDF "
              f"to circulate.")
    return written


def show_list():
    cfg = json.loads(CONFIG.read_text())
    rows = sorted(cfg["competitors"], key=lambda c: -completeness(c))
    print(f"{'id':22} {'threat':8} sections  name")
    for c in rows:
        n = completeness(c)
        flag = " " if n >= MIN_SECTIONS else "-"
        print(f"{flag}{c['id']:21} {c.get('threat','?'):8} {n}/6       {c['name']}")
    print(f"\n- = below the {MIN_SECTIONS}-section threshold, no brief generated yet")


def selftest():
    cfg = json.loads(CONFIG.read_text())
    by_id = {c["id"]: c for c in cfg["competitors"]}
    assert "meshh" in by_id and "crowd-connected" in by_id
    out = render(by_id["meshh"], cfg.get("meta", {}))
    assert out.startswith("<!DOCTYPE html>") and out.rstrip().endswith("</html>")
    # the naming warning must survive into the document
    assert "spatial analytics company" in out
    # a Draw must not be styled as a loss
    assert 'class="eD"' in out, "Draw edge not styled distinctly"
    # escaping must hold
    nasty = {"id": "x", "name": '<script>alert(1)</script>', "threat": "low",
             "one_liner": "a & b", "sources": ['"quoted"']}
    o = render(nasty, {})
    assert "<script>alert(1)</script>" not in o
    assert "&lt;script&gt;" in o and "a &amp; b" in o
    # a bare entry must not crash
    render({"id": "y", "name": "Y"}, {})
    assert completeness(by_id["meshh"]) == 6
    assert completeness({"id": "z"}) == 0
    print("brief selftest OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
    elif "--list" in args:
        show_list()
    elif "--all" in args:
        build()
    elif args:
        build([a for a in args if not a.startswith("-")])
    else:
        raise SystemExit(__doc__)
