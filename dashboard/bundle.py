#!/usr/bin/env python3
"""Build ONE self-contained HTML file anyone can open by double-clicking.

Why this exists: index.html loads its data with fetch(), which browsers block on
file:// URLs. So emailing index.html on its own produces an error page. This
inlines the data instead, so the result needs no server, no install, no network
and no permissions — it opens from a Desktop, an email attachment or SharePoint.

The tradeoff, stated on the page itself: it is a dated snapshot and cannot
refresh itself.

Usage:
    python3 bundle.py
    python3 bundle.py --out ~/Desktop
    python3 bundle.py --selftest
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "index.html"
MARKER = "<script>"          # injection point: before the first inline script


def read_json(name):
    p = HERE / name
    return json.loads(p.read_text()) if p.exists() else None


def read_changes():
    p = HERE / "changes.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def build(outdir=None):
    html = SRC.read_text()
    competitors = read_json("competitors.json")
    if competitors is None:
        raise SystemExit("competitors.json not found — run from the dashboard folder")

    payload = {
        "competitors": competitors,
        "clients": read_json("clients.json"),
        # Without this the offline bundle loses every reviewed line and shows the
        # regex templates instead — worse than the served version, silently.
        "analysis": read_json("analysis.json"),
        "changes": read_changes(),
        "digest": (HERE / "digest.html").read_text()
                  if (HERE / "digest.html").exists() else "",
    }
    # </script> inside the digest string would close our own tag early
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    idx = html.find(MARKER)
    if idx < 0:
        raise SystemExit("could not find a <script> tag to inject before")
    inject = f"<script>window.__BUNDLED__ = {blob};</script>\n"
    out_html = html[:idx] + inject + html[idx:]

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = Path(outdir).expanduser() if outdir else HERE
    target.mkdir(parents=True, exist_ok=True)
    dest = target / f"WYA-market-monitor-{stamp}.html"
    dest.write_text(out_html)

    mb = dest.stat().st_size / 1_048_576
    print(f"wrote {dest}")
    print(f"  {mb:.1f} MB · {len(payload['changes'])} items · "
          f"{len(competitors['competitors'])} competitors · "
          f"{len((payload['clients'] or {}).get('clients', []))} clients")
    print("  Double-click to open. No server, no install, works offline.")
    if mb > 12:
        print(f"  NOTE: {mb:.0f} MB may be too large for some email systems. "
              f"Share via SharePoint or a link instead.")
    return dest


def selftest():
    html = SRC.read_text()
    assert MARKER in html
    assert '"analysis"' in Path(__file__).read_text(), \
        "bundle payload must include analysis.json or written analysis is lost offline"
    assert "window.__BUNDLED__" in html, \
        "index.html must check window.__BUNDLED__ or the bundle will still fetch"

    # the escaping that stops an embedded </script> breaking the page
    payload = {"digest": "<script>bad()</script><p>ok</p>"}
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    assert "</script>" not in blob, "embedded </script> would close the tag early"
    assert json.loads(blob.replace("<\\/", "</"))["digest"].endswith("<p>ok</p>")

    # injection must land before the first script, not after
    idx = html.find(MARKER)
    assert idx > html.find("<body"), "marker should be inside body"
    built = html[:idx] + "<script>window.__BUNDLED__={};</script>\n" + html[idx:]
    assert built.count("window.__BUNDLED__") == html.count("window.__BUNDLED__") + 1

    ch = read_changes()
    assert isinstance(ch, list)
    print("bundle selftest OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
    else:
        out = None
        if "--out" in args:
            i = args.index("--out")
            out = args[i + 1] if len(args) > i + 1 else None
        build(out)
