#!/usr/bin/env bash
# A clean copy of this project, safe to hand over, zipped beside it.
#
#   make export                              # ../gaia-clean-YYYYMMDD/ and .zip
#   make export ARGS="--out /tmp/gaia --zip /tmp/gaia.zip"
#   make export ARGS="--also 'Acme Ltd' --force"
#
# What it copies: every file git tracks (the working tree's version), plus
# verification/share/ and its contents — nothing else. .env, gaia-env/,
# evaluations/, other verification outputs, caches and build output never
# enter the copy.
# What it does: removes anything that looks like a compiler or cache leftover,
# replaces personal data in the documents (consistently — the same value is the
# same token; the key stays outside the copy) and the explicit --also names
# everywhere, then runs the PII scan over the whole copy with --include-src
# --fail-on-findings. A finding stops the run before anything is zipped.
#
# Options:
#   --out DIR        the copy (default: <parent>/gaia-clean-YYYYMMDD)
#   --zip FILE       the archive (default: <out>.zip; --no-zip to skip)
#   --also TEXT      one more string to replace, repeatable (a name, a company)
#   --also-file F    more strings, one per line (default: scripts/export_also.txt)
#   --key FILE       where the replacement key is written (default: <out>-key.json)
#   --force          replace an existing copy
#   --no-zip         build the copy only
#   -h, --help       this text
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="$(dirname "$ROOT")"
STAMP="$(date +%Y%m%d)"
OUT="$PARENT/gaia-clean-$STAMP"
ZIP=""
ALSO=()
ALSO_FILE="$ROOT/scripts/export_also.txt"
KEY=""
FORCE=0
DO_ZIP=1

usage() { sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while (($#)); do
  case "$1" in
    --out) OUT="${2:?--out needs a directory}"; shift 2 ;;
    --zip) ZIP="${2:?--zip needs a file}"; shift 2 ;;
    --also) ALSO+=("${2:?--also needs text}"); shift 2 ;;
    --also-file) ALSO_FILE="${2:?--also-file needs a file}"; shift 2 ;;
    --key) KEY="${2:?--key needs a file}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --no-zip) DO_ZIP=0; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "export: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

OUT="${OUT%/}"
ZIP="${ZIP:-$OUT.zip}"
KEY="${KEY:-$PARENT/$(basename "$OUT")-key.json}"
REPORT="${ZIP%.zip}-export.txt"

PY="$ROOT/gaia-env/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "export: no python3 found" >&2; exit 2; }
export PYTHONPATH="$ROOT/backend/src${PYTHONPATH:+:$PYTHONPATH}"
if ! "$PY" -c 'import gaia.tools.redact' 2>/dev/null; then
  echo "export: $PY cannot import gaia.tools.redact — run 'make venv' first" >&2
  exit 2
fi

if [[ -e "$OUT" ]]; then
  if ((FORCE)); then
    echo "export: replacing $OUT"
    rm -rf "$OUT"
  else
    echo "export: $OUT exists — pass --force to replace it, or --out another name" >&2
    exit 2
  fi
fi
untracked="$(git -C "$ROOT" ls-files --others --exclude-standard | head -20)"
if [[ -n "$untracked" ]]; then
  echo "export: these are NOT copied — commit them to include them:" >&2
  echo "$untracked" | sed 's/^/  /' >&2
fi
mkdir -p "$OUT"

# 1. The tracked tree, at its working-tree state (nothing untracked, no .git).
echo "export: copying the tracked tree"
git -C "$ROOT" ls-files -z | tar -C "$ROOT" --null -T - -cf - | tar -C "$OUT" -xf -

# 2. The one untracked directory that ships: verification/share/ (already redacted).
if [[ -d "$ROOT/verification/share" ]]; then
  echo "export: adding verification/share/"
  mkdir -p "$OUT/verification/share"
  (cd "$ROOT/verification/share" && tar -cf - .) | tar -C "$OUT/verification/share" -xf -
fi


# 3. Defensive: no compiler or cache leftover, no secrets, no environment.
find "$OUT" -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \
    -o -name .mypy_cache -o -name .vite -o -name node_modules -o -name '*.egg-info' \) \
    -prune -exec rm -rf {} +
find "$OUT" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
rm -rf "$OUT/gaia-env" "$OUT/.env" "$OUT/.env.local" "$OUT/frontend/dist" \
       "$OUT/frontend/node_modules" "$OUT/verification/share-key.json"
if [[ -d "$OUT/verification" ]]; then
  find "$OUT/verification" -mindepth 1 -maxdepth 1 ! -name share -exec rm -rf {} +
fi
leftover="$(find "$OUT" \( -name .env -o -name gaia-env -o -name node_modules -o -name __pycache__ \
    -o -name '*.pyc' -o -name '*.egg-info' \) -print -quit)"
if [[ -n "$leftover" ]]; then
  echo "export: $leftover must not be in the copy" >&2
  exit 1
fi

# 4. Personal data out of every text file that is not code (code would break).
echo "export: replacing personal data"
redact_args=(--tree "$OUT" --key "$KEY")
[[ -f "$ALSO_FILE" ]] && redact_args+=(--also-file "$ALSO_FILE")
for word in "${ALSO[@]}"; do redact_args+=(--also "$word"); done
"$PY" -m gaia.tools.redact "${redact_args[@]}"

# 5. Prove it: the scan reads code too, and a finding stops the run.
echo "export: scanning the copy"
scan_args=(--include-src --fail-on-findings -v)
if [[ -f "$ALSO_FILE" ]]; then
  while IFS= read -r word; do
    word="${word#"${word%%[![:space:]]*}"}"
    word="${word%"${word##*[![:space:]]}"}"
    [[ -z "$word" || "$word" == \#* ]] && continue
    scan_args+=(--also "$word")
  done < "$ALSO_FILE"
fi
for word in "${ALSO[@]}"; do scan_args+=(--also "$word"); done
if ! "$PY" "$ROOT/scripts/pii_scan.py" "${scan_args[@]}" "$OUT"; then
  echo "export: personal data or a credential remains — nothing zipped ($OUT kept)" >&2
  exit 1
fi

# 6. The archive, and the same check read from inside it.
if ((DO_ZIP)); then
  rm -f "$ZIP"
  (cd "$(dirname "$OUT")" && zip -rqX "$ZIP" "$(basename "$OUT")")
  if unzip -Z1 "$ZIP" | grep -qE '(^|/)(\.env|__pycache__|node_modules|gaia-env|\.pytest_cache|\.ruff_cache|\.mypy_cache|\.git)(/|$)|\.pyc$'; then
    echo "export: $ZIP carries a forbidden path" >&2
    exit 1
  fi
fi

files="$(find "$OUT" -type f | wc -l)"
{
  echo "Gaia clean copy — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "source: $ROOT (git $(git -C "$ROOT" log -1 --format='%h %cs' 2>/dev/null || echo '?'))"
  echo "copy:   $OUT — $files file(s), $(du -sh "$OUT" | cut -f1)"
  ((DO_ZIP)) && echo "zip:    $ZIP — $(du -h "$ZIP" | cut -f1)"
  echo "key:    $KEY (keep it; it never enters the copy)"
  echo "scan:   no personal data and no credential (pii_scan --include-src --fail-on-findings)"
  echo
  echo "Copied: every git-tracked file, plus verification/share/."
  echo "Not copied: .env, gaia-env/, .git/, evaluations/, other verification outputs,"
  echo "            __pycache__, node_modules, dist, .pytest_cache, .ruff_cache."
} > "$REPORT"

echo
echo "export: clean copy   $OUT ($files file(s))"
((DO_ZIP)) && echo "export: zip          $ZIP ($(du -h "$ZIP" | cut -f1))"
echo "export: key          $KEY (never part of the copy)"
echo "export: report       $REPORT"
