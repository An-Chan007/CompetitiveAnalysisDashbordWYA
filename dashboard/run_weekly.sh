#!/bin/sh
# Weekly WYA market monitor run. Collect, then regenerate the digest.
#
# Local:   ./run_weekly.sh
# Cron:    0 7 * * 1  /var/www/wya-ci/run_weekly.sh >> /var/log/wya-ci.log 2>&1
#
# Exits non-zero if either step fails, so cron mail / a monitor will notice.
set -eu
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "=== WYA market monitor: $(date -u '+%Y-%m-%d %H:%M UTC') ==="

# Fail fast on a broken config rather than collecting against it.
"$PY" collect.py --selftest
"$PY" digest.py --selftest

"$PY" collect.py
"$PY" digest.py --days=7

echo "=== done: $(date -u '+%Y-%m-%d %H:%M UTC') ==="
