#!/usr/bin/env bash
# Delete the first line of the files in verification/ when that line is the
# captured command echo (``cd /home/... && .../gaia-env/bin/pytest ...``) that
# `make` writes into an output file. Such a line leaks the local absolute path.
# Files whose first line is real content (report text, CSV header, JSON) are
# left untouched, so the command never corrupts machine-readable outputs.
#
# usage: strip_verification_first_line.sh [--all] [--dry-run] [DIR]
#   --all       delete the first line of every file, content or not
#               (can corrupt CSV/JSON; only use when that is intended)
#   --dry-run   print what would be stripped, change nothing
#   DIR         directory to process (default: <repo>/verification)
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/verification"
ALL=0
DRY=0

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    --all) ALL=1 ;;
    --dry-run | -n) DRY=1 ;;
    -h | --help) usage; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) DIR="$arg" ;;
  esac
done

[[ -d "$DIR" ]] || { echo "not a directory: $DIR" >&2; exit 1; }

# A first line is a captured command when it is an absolute `cd` invocation or
# mentions the virtualenv path. Everything else is treated as real content.
is_captured_command() {
  local line="$1"
  [[ "$line" == "cd /"* ]] && return 0
  [[ "$line" == "/home/"* || "$line" == "/Users/"* ]] && return 0
  [[ "$line" == *"/gaia-env/"* ]] && return 0
  return 1
}

mapfile -d '' files < <(find "$DIR" -maxdepth 1 -type f -print0 | sort -z)
changed=0
skipped=0

for f in "${files[@]}"; do
  first="$(head -n 1 -- "$f")"
  if [[ $ALL -eq 0 ]] && ! is_captured_command "$first"; then
    skipped=$((skipped + 1))
    continue
  fi
  if [[ $DRY -eq 1 ]]; then
    printf 'would strip  %s  :: %s\n' "$f" "${first:0:120}"
    changed=$((changed + 1))
    continue
  fi
  tmp="$(mktemp "${f}.XXXXXX")" || exit 1
  if tail -n +2 -- "$f" >"$tmp"; then
    chmod --reference="$f" "$tmp" 2>/dev/null || true
    mv -f -- "$tmp" "$f"
    printf 'stripped  %s  :: %s\n' "$f" "${first:0:120}"
    changed=$((changed + 1))
  else
    rm -f -- "$tmp"
    echo "failed to rewrite: $f" >&2
    exit 1
  fi
done

if [[ $DRY -eq 1 ]]; then
  echo "dry run: $changed file(s) would lose their first line, $skipped left alone"
else
  echo "$changed file(s) stripped, $skipped left alone"
fi
