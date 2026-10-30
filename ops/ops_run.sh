#!/usr/bin/env bash
# One run of the Kelin catalogue mirror's operations (s20-2, 2026-10-30). Runs INSIDE a checkout of the mirror repository
# (cwd = its root), the same way from the weekly GitHub Actions job (.github/workflows/ops.yml, GH_TOKEN = GITHUB_TOKEN)
# and from a laptop (scripts/sync-dataset-mirror.sh, gh keyring). Steps, each a no-op when nothing changed:
#   1. download the LIVE dataset (https://kelintextile.design/data/, never undeployed data) and validate it
#   2. rebuild README / Pages (index + 10 category pages) / feed.xml / sitemap.xml / CITATION / datapackage (ops/build.py)
#      and commit + push only if a file differs — no filler commits
#   3. cut a release when the dataset version (content hash) is new, then post one "Dataset <version>" Announcement
#      with the differences vs the previous release's JSON (ops/backlink-ops.py announce)
#   4. post a "Series spotlight" Discussion if the newest one is ≥ 13 days old (rotates through the 28 series)
#   5. Wayback "Save Page Now" for the site's 16 key pages whose newest snapshot is ≥ 28 days old; log → ops/wayback.jsonl
# Usage: ops_run.sh <owner/repo> [--dry-run]      --dry-run: build only, show the diff, push/post nothing
# Exit 0 ok · 1 failed · 64 usage
set -euo pipefail
REPO=${1:-}; DRY=${2:-}
[[ $REPO == */* ]] || { echo "usage: ops_run.sh <owner/repo> [--dry-run]" >&2; exit 64; }
[[ -z $DRY || $DRY == --dry-run ]] || { echo "unknown argument: $DRY" >&2; exit 64; }
[[ -d .git && -f ops/build.py && -f ops/backlink-ops.py ]] || { echo "✗ run inside the mirror checkout (needs .git, ops/build.py, ops/backlink-ops.py)" >&2; exit 1; }
BASE=https://kelintextile.design/data/kelin-hotel-linen-catalog
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT

# git identity: the Actions bot in CI, the mirror identity (noreply address) on a laptop
if [[ -n ${GITHUB_ACTIONS:-} ]]; then
  GIT=(git -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com")
else
  OWNER=${REPO%%/*}; UID_=$(gh api user --jq .id)
  GIT=(git -c "credential.https://github.com.username=$OWNER" -c user.name="Kelin catalogue mirror" -c user.email="$UID_+$OWNER@users.noreply.github.com")
fi

# 1. live dataset
for i in 1 2 3; do curl -fsS --max-time 40 "$BASE.json" -o "$W/d.json" && curl -fsS --max-time 40 "$BASE.csv" -o "$W/d.csv" && break; sleep $((i*5)); done
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['categories'] and len(d['version'])==12, 'bad dataset'" "$W/d.json"
VER=$(python3 -c "import json;print(json.load(open('$W/d.json'))['version'])")
gh release list --repo "$REPO" --json tagName,publishedAt,name > "$W/releases.json"
PREV_TAG=$(python3 -c "
import json,sys; rs=[r for r in json.load(open('$W/releases.json')) if r['tagName']!='v-$VER']; rs.sort(key=lambda r:r['publishedAt'])
print(rs[-1]['tagName'] if rs else '')")

# 2. rebuild (everything except .git, ops/ and .github/ is generated)
find . -mindepth 1 -maxdepth 1 ! -name .git ! -name ops ! -name .github -exec rm -rf {} +
python3 ops/build.py "$W/d.json" "$W/d.csv" . "$REPO" "$W/releases.json"
"${GIT[@]}" add -A
if "${GIT[@]}" diff --cached --quiet; then echo "✓ mirror up to date (v$VER)"; else
  "${GIT[@]}" diff --cached --stat | tail -4
  if [[ $DRY == --dry-run ]]; then echo "(dry run: not pushed)"; else
    if gh release view "v-$VER" --repo "$REPO" >/dev/null 2>&1; then MSG="Rebuild pages (dataset $VER)"; else MSG="Dataset $VER (from $BASE.json)"; fi
    "${GIT[@]}" commit -qm "$MSG"; "${GIT[@]}" push -q origin HEAD:main; echo "✓ pushed: $MSG"
  fi
fi
[[ $DRY == --dry-run ]] && { echo "(dry run: no release / announcement / spotlight / wayback)"; exit 0; }

# 3. release + announcement
if ! gh release view "v-$VER" --repo "$REPO" >/dev/null 2>&1; then
  gh release create "v-$VER" --repo "$REPO" --target main --title "Dataset $VER" \
    --notes "Catalogue dataset version $VER, mirrored from $BASE.json. Series pages: https://kelintextile.design/products/?ref=gh-dataset — category pages on the mirror: https://${REPO%%/*}.github.io/${REPO##*/}/" \
    "./$(basename "$BASE").csv" "./$(basename "$BASE").json" >/dev/null
  echo "✓ release v-$VER"
fi
REL_URL="https://github.com/$REPO/releases/tag/v-$VER"
PREV=-
if [[ -n $PREV_TAG ]]; then
  mkdir -p "$W/prev" && gh release download "$PREV_TAG" --repo "$REPO" --pattern '*.json' -D "$W/prev" 2>/dev/null && PREV=$(ls "$W"/prev/*.json | head -1) || PREV=-
fi
python3 ops/backlink-ops.py announce "$REPO" "$W/d.json" "$PREV" "$REL_URL"

# 4. spotlight
python3 ops/backlink-ops.py spotlight "$REPO" "$W/d.json" --min-days 13

# 5. wayback (log committed only when a page was saved)
python3 ops/backlink-ops.py wayback "$W/d.json" ops/wayback.jsonl --min-days 28
"${GIT[@]}" add -A ops/wayback.jsonl
if ! "${GIT[@]}" diff --cached --quiet; then
  "${GIT[@]}" commit -qm "Wayback snapshots $(date -u +%F)"; "${GIT[@]}" push -q origin HEAD:main; echo "✓ wayback log pushed"
fi
echo "✓ ops run complete (v$VER)"
