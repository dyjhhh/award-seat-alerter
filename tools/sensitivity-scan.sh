#!/bin/bash
# Public pattern check. Private blocklists and Git history are not shipped.
cd "$(dirname "$0")/.." || exit 2
EXCL=(--exclude-dir=.git --exclude=.git --exclude=sensitivity-scan.sh
      --exclude-dir=__pycache__ --exclude-dir=node_modules --exclude-dir=.venv
      --exclude-dir=venv --exclude-dir=.pytest_cache --exclude-dir=dist
      --exclude-dir=build --exclude='*.pyc' --exclude='*.pyo')
err=$(mktemp) || exit 2
trap 'rm -f "$err"' EXIT
SECRETS='[0-9]{8,10}:[A-Za-z0-9_-]{35}|GOCSPX-[A-Za-z0-9_-]{10,}|ya29\.[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
CONTACT='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|org|net|io|me)\b|\b([0-9]{1,3}\.){3}[0-9]{1,3}\b|\b\+?1?[ .-]?\(?[0-9]{3}\)?[ .-][0-9]{3}[ .-][0-9]{4}\b'
HOMEPATH='/Users/[A-Za-z0-9_-]+'
run() {
  local hits rc
  hits=$(grep -rnIE "${EXCL[@]}" "$1" . 2>"$err"); rc=$?
  if [ "$rc" -gt 1 ] || [ -s "$err" ]; then
    echo "SENSITIVITY SCAN: detector failed, not clean" >&2
    cat "$err" >&2
    return 2
  fi
  printf '%s' "$hits"
}
# A placeholder elsewhere on a line must never suppress a credential match.
secrets=$(run "$SECRETS") || exit 2
if [ -n "$secrets" ]; then
  echo "SENSITIVITY SCAN: secret-shaped text found"
  printf '%s\n' "$secrets"
  exit 1
fi
raw=$(run "$CONTACT|$HOMEPATH") || exit 2
# Fictional placeholders and loopback addresses need human review too.
filtered=$(printf '%s\n' "$raw" | grep -vE '\$\{[A-Z_]+\}|<redacted-email>|127\.0\.0\.1|0\.0\.0\.0|example\.com|/Users/operator\b')
rc=$?
[ "$rc" -gt 1 ] && { echo "SENSITIVITY SCAN: filter failed" >&2; exit 2; }
if [ -n "$filtered" ]; then
  echo "SENSITIVITY SCAN: contact or home-path pattern found"
  printf '%s\n' "$filtered"
  exit 1
fi
echo "SENSITIVITY SCAN: no configured pattern matched (not a complete privacy audit)"
