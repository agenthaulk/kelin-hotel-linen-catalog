#!/usr/bin/env bash
# One run of the Kelin catalogue mirror's operations (s20-2, 2026-10-30; s51, 2026-11-23; review 30- fixes 2026-11-23).
# Runs INSIDE a checkout of the mirror repository (cwd = its root), the same way from the weekly GitHub Actions job
# (.github/workflows/ops.yml, GH_TOKEN = GITHUB_TOKEN, passes --execute) and from a laptop (scripts/sync-dataset-mirror.sh,
# gh keyring). Steps, each a no-op when nothing changed:
#   0. target check: the repository argument, $GITHUB_REPOSITORY (Actions), ops/mirror-config.json and the checkout's
#      origin remote must all be the pinned agenthaulk/kelin-hotel-linen-catalog, and HEAD must be its branch main
#   1. download the LIVE dataset (https://kelintextile.design/data/, never undeployed data); any download failure stops the
#      run; the JSON / CSV pair is validated (JSON version = content hash, CSV header + every row = the JSON) — this one
#      verified file set is used for the mirror, the release assets and (via --save-input) the local gist
#   2. build README / Pages / feed / sitemap / CITATION / datapackage / LICENSE / .zenodo.json into an EMPTY temp dir
#      (ops/build.py; --strict on --execute: licence from ops/mirror-config.json only, must equal the dataset's own licence),
#      gate it, then replace only the managed files in the checkout (ops/managed-files.json; .git, ops/, .github/ and any
#      file the build never wrote are left alone), gate the whole checkout again, commit + push only if a file differs
#   3. cut a release when the dataset version is new — its assets are the verified files (sha256 checked before upload) —
#      then post one "Dataset <version>" Announcement with the differences vs the previous release's JSON
#   4. post a "Series spotlight" Discussion if the newest one is ≥ 13 days old (rotates through all series)
#   5. Wayback (backlink-ops.py wayback) with the ONE shared checkpoint ops/wayback.jsonl (the local runs append to the
#      same file); skipped while ops/mirror-config.json wayback.local_first_pass is true (the laptop does the first pass)
#   6. Hugging Face copy (s58, 2026-11-26): only when $KELIN_HF_TOKEN is set (weekly job: repository secret of the same
#      name; surrounding whitespace / line breaks are trimmed) AND huggingface.repo_id is configured AND ops/hf-sync.py and
#      ops/hf-requirements.txt are present — otherwise one log line, skipped. Execute: the exact pinned huggingface_hub set
#      (ops/hf-requirements.txt, --require-hashes, wheels only) into a throwaway venv under the run's temp dir, then
#      hf-sync.py --execute on the same verified pair (it uploads only the files that differ; nothing differs = clean
#      no-op; it never creates the repository). Both run with an allow-listed environment (no GitHub token, no PIP_* /
#      PYTHON* / HF_* settings; HOME in the temp dir) and under a hard time limit. A failure or a timeout is a ::warning::,
#      the run stays green (as Wayback). Dry run: hf-sync.py dry run only (card + gate, no library, never uploads).
#      The token is moved out of the environment at the start (only step 6's hf-sync.py process gets it back, via its
#      environment — never on a command line, never printed).
# Usage: ops_run.sh <owner/repo> [--dry-run | --execute] [--from-dir DIR] [--save-input DIR]
#   default = --dry-run: build + gate + diff only; nothing pushed, released, posted or archived.
#   --from-dir DIR (dry run only): build from DIR/kelin-hotel-linen-catalog.{json,csv} instead of the live files.
#   --save-input DIR: copy the verified dataset pair there (sync-dataset-mirror.sh uses it for the gist).
# Exit 0 ok · 1 failed (incl. the gate, a download or a target check) · 64 usage
set -euo pipefail
# s58: keep the Hugging Face token away from every other command of this run (gh, git, curl, the python steps)
unset HF_TOKEN_                                                   # a same-named exported variable of the caller is not reused
HF_TOKEN_=${KELIN_HF_TOKEN:-}; unset KELIN_HF_TOKEN
HF_TOKEN_=${HF_TOKEN_#"${HF_TOKEN_%%[![:space:]]*}"}; HF_TOKEN_=${HF_TOKEN_%"${HF_TOKEN_##*[![:space:]]}"}   # trim (\r, \n, spaces)
REPO=${1:-}; shift || true
MODE=--dry-run; FROM=; SAVE=
SEEN=
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run|--execute) [[ -n $SEEN && $SEEN != "$1" ]] && { echo "✗ --dry-run and --execute together" >&2; exit 64; }; SEEN=$1; MODE=$1 ;;
    --from-dir) [[ -n ${2:-} && $2 != --* ]] || { echo "✗ --from-dir needs a directory" >&2; exit 64; }; FROM=$2; shift ;;
    --save-input) [[ -n ${2:-} && $2 != --* ]] || { echo "✗ --save-input needs a directory" >&2; exit 64; }; SAVE=$2; shift ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac; shift
done
[[ $REPO == */* ]] || { echo "usage: ops_run.sh <owner/repo> [--dry-run | --execute] [--from-dir DIR] [--save-input DIR]" >&2; exit 64; }
[[ -n $FROM && $MODE == --execute ]] && { echo "✗ --from-dir is for dry runs only (never publish undeployed data)" >&2; exit 64; }
[[ -d .git && -f ops/build.py && -f ops/backlink-ops.py && -f ops/mirror_check.py && -f ops/mirror-config.json ]] \
  || { echo "✗ run inside the mirror checkout (needs .git, ops/build.py, ops/backlink-ops.py, ops/mirror_check.py, ops/mirror-config.json)" >&2; exit 1; }
MC=(python3 -I ops/mirror_check.py)
CFG=ops/mirror-config.json

# 0. target check (review 30- #1): one repository, one branch, the checkout really is that repository
PINNED=$("${MC[@]}" config "$CFG" repo)          # exits non-zero when the config names anything but the pinned values
BRANCH=$("${MC[@]}" config "$CFG" branch)
[[ $REPO == "$PINNED" ]] || { echo "✗ repository $REPO is not the pinned $PINNED" >&2; exit 1; }
[[ -z ${GITHUB_REPOSITORY:-} || $GITHUB_REPOSITORY == "$PINNED" ]] || { echo "✗ running in $GITHUB_REPOSITORY, not $PINNED" >&2; exit 1; }
ORIGIN=$(git config --get remote.origin.url || true)            # as configured (no url rewriting applied)
"${MC[@]}" check-origin "$ORIGIN" || { echo "✗ origin remote ($ORIGIN) is not github.com/$PINNED" >&2; exit 1; }
if [[ $MODE == --execute ]]; then                                  # where a push would really go (pushurl / insteadOf applied)
  PUSHTO=$(git remote get-url --push origin 2>/dev/null || true)
  "${MC[@]}" check-origin "$PUSHTO" || { echo "✗ pushes would go to $PUSHTO, not github.com/$PINNED" >&2; exit 1; }
fi
HEAD_BRANCH=$(git rev-parse --abbrev-ref HEAD)
[[ $HEAD_BRANCH == "$BRANCH" ]] || { echo "✗ checkout is on $HEAD_BRANCH, not $BRANCH" >&2; exit 1; }

BASE=https://kelintextile.design/data/kelin-hotel-linen-catalog
NAME=$(basename "$BASE")
WAYBACK_MAX=${WAYBACK_MAX:-10}
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT

# git identity: the Actions bot in CI, the mirror identity (noreply address) on a laptop
if [[ -n ${GITHUB_ACTIONS:-} ]]; then
  GIT=(git -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com")
else
  OWNER=${REPO%%/*}; UID_=$(gh api user --jq .id)
  GIT=(git -c "credential.https://github.com.username=$OWNER" -c user.name="Kelin catalogue mirror" -c user.email="$UID_+$OWNER@users.noreply.github.com")
fi

# 1. dataset (live, or a local directory for a dry-run preview) — review 30- #2: a failed download stops the run
mkdir -p "$W/in"
if [[ -n $FROM ]]; then
  cp "$FROM/$NAME.json" "$W/in/$NAME.json"; cp "$FROM/$NAME.csv" "$W/in/$NAME.csv"; echo "(dry run: dataset from $FROM, not the live site)"
else
  ok=
  for i in 1 2 3; do
    rm -f "$W/in/$NAME.json" "$W/in/$NAME.csv"
    if curl -fsS --max-time 40 "$BASE.json" -o "$W/in/$NAME.json" && curl -fsS --max-time 60 "$BASE.csv" -o "$W/in/$NAME.csv" \
       && "${MC[@]}" validate-pair "$W/in/$NAME.json" "$W/in/$NAME.csv" >/dev/null; then ok=1; break; fi
    sleep $((i*5))
  done
  [[ -n $ok ]] || { echo "✗ could not download a consistent dataset pair from $BASE.{json,csv} (3 attempts) — nothing changed" >&2; exit 1; }
fi
"${MC[@]}" validate-pair "$W/in/$NAME.json" "$W/in/$NAME.csv"      # exits 1 on an inconsistent pair (set -e)
SHA_JSON=$(python3 -I -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$W/in/$NAME.json")
SHA_CSV=$(python3 -I -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$W/in/$NAME.csv")
if [[ -n $SAVE ]]; then mkdir -p "$SAVE"; cp "$W/in/$NAME.json" "$W/in/$NAME.csv" "$SAVE/"; fi
VER=$(python3 -I -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$W/in/$NAME.json")
echo "dataset $VER: json sha256 ${SHA_JSON:0:16}…, csv sha256 ${SHA_CSV:0:16}…"
gh release list --repo "$REPO" --json tagName,publishedAt,name > "$W/releases.json"
# does release v-$VER exist? "no" only for a real HTTP 404 — a network / auth error stops the run (set -e on the assignment)
rel_exists() { "${MC[@]}" gh-exists "repos/$REPO/releases/tags/v-$VER"; }
REL=$(rel_exists)
PREV_TAG=$(python3 -I -c "
import json,sys; rs=[r for r in json.load(open(sys.argv[1])) if r['tagName']!='v-'+sys.argv[2]]; rs.sort(key=lambda r:r['publishedAt'])
print(rs[-1]['tagName'] if rs else '')" "$W/releases.json" "$VER")

# 2. isolated build + gate, then managed install + gate on the whole checkout (review 30- #1)
STRICT=(); [[ $MODE == --execute ]] && STRICT=(--strict)
python3 -I ops/build.py "$W/in/$NAME.json" "$W/in/$NAME.csv" "$W/out" "$REPO" "$W/releases.json" --config "$CFG" ${STRICT[@]+"${STRICT[@]}"}
"${MC[@]}" install "$W/out" .
"${MC[@]}" .                                                      # gate: generated files + ops/ + .github/ + anything left
"${GIT[@]}" add -A
if "${GIT[@]}" diff --cached --quiet; then echo "✓ mirror up to date (v$VER)"; else
  "${GIT[@]}" diff --cached --stat | tail -30
  if [[ $MODE == --dry-run ]]; then echo "(dry run: not committed, not pushed)"; else
    if [[ $REL == no ]]; then MSG="Dataset $VER (from $BASE.json)"
    elif "${GIT[@]}" diff --cached --name-only | grep -qvE '^(ops/|\.github/)'; then MSG="Rebuild pages (dataset $VER)"
    else MSG="Update ops code"; fi
    "${GIT[@]}" commit -qm "$MSG"; "${GIT[@]}" push -q origin "HEAD:$BRANCH"; echo "✓ pushed: $MSG"
  fi
fi
WB_SKIP=$("${MC[@]}" config "$CFG" wayback.local_first_pass)
HF_REPO=$("${MC[@]}" config "$CFG" huggingface.repo_id)          # validated: empty or the pinned HF repository
hf_skip_reason() {                                                 # '' = the Hugging Face step runs
  if [[ -z $HF_TOKEN_ ]]; then echo "KELIN_HF_TOKEN not set"
  elif [[ -z $HF_REPO ]]; then echo "huggingface.repo_id not configured"
  elif [[ ! -f ops/hf-sync.py ]]; then echo "ops/hf-sync.py not installed in this checkout"
  elif [[ ! -f ops/hf-requirements.txt ]]; then echo "ops/hf-requirements.txt not installed in this checkout"
  elif [[ ! $HF_TOKEN_ =~ ^hf_[A-Za-z0-9]{20,}$ ]]; then echo "KELIN_HF_TOKEN is not in the form hf_ + at least 20 letters / digits (value not shown)"; fi
}
HF_SKIP=$(hf_skip_reason)
# KELIN_SKIP_WAYBACK=1: skip the Wayback step for this run only (a sync on a day the laptop already archived; low-frequency rule 2026-11-23)
[[ ${KELIN_SKIP_WAYBACK:-} == 1 ]] && WB_SKIP=true
if [[ $MODE == --dry-run ]]; then
  if [[ $REL == yes ]]; then echo "(dry run) release v-$VER exists — no release / announcement"
  else echo "(dry run) would cut release v-$VER (assets: the verified pair) and post the announcement"; fi
  [[ $WB_SKIP == true ]] && echo "(dry run) Wayback step: skipped while wayback.local_first_pass is true" \
    || echo "(dry run) Wayback step: backlink-ops.py wayback with the shared checkpoint ops/wayback.jsonl"
  # round-1 should-fix: the preview runs the same post decisions as an execute (read-only queries, "(dry run) would post …")
  python3 -I ops/backlink-ops.py announce "$REPO" "$W/in/$NAME.json" - "https://github.com/$REPO/releases/tag/v-$VER" || true
  python3 -I ops/backlink-ops.py spotlight "$REPO" "$W/in/$NAME.json" --min-days 13 || true
  if [[ -n $HF_SKIP ]]; then echo "= Hugging Face step skipped: $HF_SKIP"
  else
    echo "(dry run) Hugging Face step: hf-sync.py dry run for $HF_REPO (no upload)"
    # the token goes along only so the preview checks for it like an execute (a dry run of hf-sync.py never sends a request)
    KELIN_HF_TOKEN=$HF_TOKEN_ python3 -I ops/hf-sync.py "$W/in/$NAME.json" "$W/in/$NAME.csv" --out "$W/hf" --config "$CFG" \
      || echo "::warning::Hugging Face dry run failed — see the log above"
  fi
  echo "(dry run: nothing released, posted, archived or uploaded)"
  exit 0
fi

# 3. release + announcement — the assets are the verified pair (the copies build.py made from it, checked byte for byte)
REL=$(rel_exists)                                                  # again, right before the decision
if [[ $REL == no ]]; then
  for f in json csv; do
    want=$([[ $f == json ]] && echo "$SHA_JSON" || echo "$SHA_CSV")
    got=$(python3 -I -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "./$NAME.$f")
    [[ $got == "$want" ]] || { echo "✗ ./$NAME.$f differs from the verified download — no release" >&2; exit 1; }
  done
  HEAD_SHA=$(git rev-parse HEAD)                                   # the commit that passed the gate and is on origin
  [[ $(git ls-remote origin "refs/heads/$BRANCH" | cut -f1) == "$HEAD_SHA" ]] || { echo "✗ origin/$BRANCH is not the gated commit $HEAD_SHA — no release" >&2; exit 1; }
  gh release create "v-$VER" --repo "$REPO" --target "$HEAD_SHA" --title "Dataset $VER" \
    --notes "Catalogue dataset version $VER, mirrored from $BASE.json. Series pages: https://kelintextile.design/products/?ref=gh-dataset — category pages on the mirror: https://${REPO%%/*}.github.io/${REPO##*/}/" \
    "./$NAME.csv" "./$NAME.json" >/dev/null
  TAG_SHA=$(gh api "repos/$REPO/commits/v-$VER" --jq .sha)
  [[ $TAG_SHA == "$HEAD_SHA" ]] || { echo "✗ tag v-$VER points at $TAG_SHA, not the gated commit $HEAD_SHA — check the release (Zenodo archives the tag)" >&2; exit 1; }
  echo "✓ release v-$VER on $HEAD_SHA (tag checked)"
fi
REL_URL="https://github.com/$REPO/releases/tag/v-$VER"
PREV=-
if [[ -n $PREV_TAG ]]; then
  # the previous release's JSON decides the announcement text: a failed download stops (never "first release")
  mkdir -p "$W/prev"
  gh release download "$PREV_TAG" --repo "$REPO" --pattern '*.json' -D "$W/prev" || { echo "✗ could not download $PREV_TAG's JSON — no announcement posted" >&2; exit 1; }
  PREV=$(ls "$W"/prev/*.json | head -1); [[ -s $PREV ]] || { echo "✗ $PREV_TAG has no JSON asset — no announcement posted" >&2; exit 1; }
fi
python3 -I ops/backlink-ops.py announce "$REPO" "$W/in/$NAME.json" "$PREV" "$REL_URL" --execute

# 4. spotlight
python3 -I ops/backlink-ops.py spotlight "$REPO" "$W/in/$NAME.json" --min-days 13 --execute

# 5. wayback — shared checkpoint; skipped during the local first pass; exit 3 = rate-limited, 4 = incomplete (rest next run)
CKPT=$("${MC[@]}" config "$CFG" wayback.checkpoint); CKPT=${CKPT:-ops/wayback.jsonl}
if [[ $WB_SKIP == true ]]; then
  echo "= Wayback skipped: wayback.local_first_pass is true (the first whole-site pass runs from the laptop on the same checkpoint)"
else
  rc=0
  python3 -I ops/backlink-ops.py wayback "$W/in/$NAME.json" "$CKPT" --max "$WAYBACK_MAX" --per-minute 10 --budget-min 15 --execute || rc=$?
  case $rc in
    0) ;;
    3) echo "::warning::Wayback rate-limited (HTTP 429) — stopped; the remaining pages are retried next week" ;;
    4) echo "::notice::Wayback incomplete — the remaining pages are taken in the next runs (see the plan above)" ;;
    *) echo "::warning::Wayback step failed (exit $rc) — see the log above; the remaining pages are retried next week" ;;
  esac
  # one shared checkpoint: commit + push (union-merge on a race, never force); "verified on origin" only after a re-fetch
  # shows every line there — otherwise the job fails and prints the lines so they are kept in the job log (33- c)
  n=${GIT_CONFIG_COUNT:-0}
  if [[ -z ${GITHUB_ACTIONS:-} ]]; then export "GIT_CONFIG_KEY_$n=credential.https://github.com.username" "GIT_CONFIG_VALUE_$n=${REPO%%/*}"; n=$((n+1)); fi
  export "GIT_CONFIG_KEY_$n=user.email" "GIT_CONFIG_VALUE_$n=$("${GIT[@]}" config user.email)"; n=$((n+1))
  export "GIT_CONFIG_KEY_$n=user.name" "GIT_CONFIG_VALUE_$n=$("${GIT[@]}" config user.name)"; n=$((n+1)); export GIT_CONFIG_COUNT=$n
  if ! python3 -I ops/backlink-ops.py checkpoint-push . "$CKPT" "$BRANCH" --keep "$W/keep" --message "Wayback snapshots $(date -u +%F)"; then
    echo "::error::Wayback checkpoint not on origin/$BRANCH — the lines of this run follow (copy them back into $CKPT):"
    cat "$W"/keep/*.jsonl 2>/dev/null || true
    exit 1
  fi
fi

# 6. Hugging Face copy — optional, never fails the run; time-limited; minimal environment
with_limit() {   # with_limit SECONDS cmd… — hard time limit that works on ubuntu and macOS (no `timeout` there): TERM, KILL 5 s later; 124 = timed out
  local secs=$1 pid wd rc=0 flag="$W/limit.$RANDOM"; shift
  "$@" & pid=$!
  ( i=0; while (( i < secs )); do sleep 1; kill -0 "$pid" 2>/dev/null || exit 0; i=$((i+1)); done
    : > "$flag"; kill -TERM "$pid" 2>/dev/null || true
    for i in 1 2 3 4 5; do sleep 1; kill -0 "$pid" 2>/dev/null || exit 0; done; kill -KILL "$pid" 2>/dev/null || true; exit 0 ) &
  wd=$!
  wait "$pid" || rc=$?
  wait "$wd" 2>/dev/null || true
  if [[ -e $flag ]]; then rm -f "$flag"; return 124; fi
  return "$rc"
}
hf_env() {       # hf_env [--token] cmd… — ONLY as the command of with_limit (it runs in that background subshell): drops every
                 # environment variable except PATH, TMPDIR and the proxy settings, then execs cmd (the pid with_limit watches)
  local give=; [[ $1 == --token ]] && { give=1; shift; }
  local v home="$W/hfhome" tok="$HF_TOKEN_"                        # taken before the environment is cleared
  for v in $(compgen -e); do
    case $v in PATH|TMPDIR|HTTPS_PROXY|https_proxy|HTTP_PROXY|http_proxy|NO_PROXY|no_proxy|ALL_PROXY|all_proxy) ;; *) unset "$v" 2>/dev/null || true ;; esac
  done
  [[ -z ${GH_TOKEN:-}${GITHUB_TOKEN:-}${KELIN_HF_TOKEN:-} ]] || { echo "✗ environment not cleared" >&2; exit 97; }
  export HOME="$home" HF_HOME="$home/hf" PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 HF_HUB_DISABLE_TELEMETRY=1
  [[ -n $give ]] && export KELIN_HF_TOKEN="$tok"
  exec "$@"
}
HF_PIP_S=150; HF_SYNC_S=300; HF_SYNC_INNER_S=240
if [[ -n $HF_SKIP ]]; then
  case $HF_SKIP in *"not in the form"*) echo "::warning::Hugging Face step skipped: $HF_SKIP" ;; *) echo "= Hugging Face step skipped: $HF_SKIP" ;; esac
else
  # all pushes of this run are done: drop the checkout's GitHub credential (actions/checkout extraheader) so step 6 cannot read it
  if [[ -n ${GITHUB_ACTIONS:-} ]]; then git config --local --unset-all 'http.https://github.com/.extraheader' 2>/dev/null || true; fi
  mkdir -p "$W/hfhome"
  rc=0
  with_limit "$HF_PIP_S" hf_env python3 -I -m venv "$W/hfvenv" || rc=$?
  if [[ $rc == 0 ]]; then
    with_limit "$HF_PIP_S" hf_env "$W/hfvenv/bin/python" -I -m pip --isolated install -q --no-cache-dir --require-hashes --only-binary=:all: \
      -r "$PWD/ops/hf-requirements.txt" || rc=$?
  fi
  if [[ $rc == 124 ]]; then
    echo "::warning::Hugging Face step: installing huggingface_hub timed out (> ${HF_PIP_S} s) — retried next week"
  elif [[ $rc != 0 ]]; then
    echo "::warning::Hugging Face step: could not install huggingface_hub (exit $rc) — retried next week"
  else
    with_limit "$HF_SYNC_S" hf_env --token "$W/hfvenv/bin/python" -X utf8 -I "$PWD/ops/hf-sync.py" "$W/in/$NAME.json" "$W/in/$NAME.csv" \
      --out "$W/hf" --config "$PWD/$CFG" --timeout "$HF_SYNC_INNER_S" --execute || rc=$?
    case $rc in
      0) ;;
      3|124) echo "::warning::Hugging Face step timed out (exit $rc) — stopped; retried next week" ;;
      *) echo "::warning::Hugging Face step failed (exit $rc) — see the log above; retried next week" ;;
    esac
  fi
fi
echo "✓ ops run complete (v$VER)"
