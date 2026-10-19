#!/usr/bin/env bash
# Start an upstream sync locally (people and agents).
#
#   upstream-sync/sync.sh <fork> [--workdir DIR] [--batch N]
#     fork     e.g. prairie-server, prairie-android, prairie-plugin-metadata-tmdb
#     workdir  where clones and worktrees live (default $PRAIRIE_SYNC_DIR or ~/prairie-sync)
#     batch    conflicted files per agent batch file (default 50)
#
# Clones (or fetches) the fork with an `upstream` remote, runs the rebrand-aware merge, and:
#   up to date -> says so
#   clean      -> leaves the merge on branch sync/upstream-<date> in a worktree; run finish.sh there
#   conflicts  -> leaves an in-progress merge (MERGE_HEAD = real upstream) in the worktree, plus
#                 <workdir>/<fork>-RESOLVE-BRIEF.md and <workdir>/<fork>-batches/NN.txt for agents
#
# Env: SYNC_TRAILER  extra trailer for the merge commit (e.g. "Co-Authored-By: ...")
set -euo pipefail
source "$(dirname "$0")/common.sh"

fork=${1:-}; [ -n "$fork" ] || die "usage: sync.sh <fork> [--workdir DIR] [--batch N]"; shift
workdir=${PRAIRIE_SYNC_DIR:-$HOME/prairie-sync}; batch=50
while [ $# -gt 0 ]; do
  case $1 in
    --workdir) workdir=$2; shift 2 ;;
    --batch) batch=$2; shift 2 ;;
    *) die "unknown argument $1" ;;
  esac
done
mkdir -p "$workdir"; workdir=$(cd "$workdir" && pwd)
clone="$workdir/$fork"; wt="$workdir/$fork-sync"; branch="sync/upstream-$(date +%F)"
upstream=$(upstream_for "$fork"); rules=$(rules_for "$fork")
need_go_if_formatting "$rules"

[ -e "$wt" ] && die "$wt exists. Finish it (finish.sh) or remove it: git -C $clone worktree remove --force $wt"
if [ ! -d "$clone/.git" ]; then
  git clone --quiet -c core.autocrlf=false -c core.eol=lf "https://github.com/$ORG/$fork" "$clone"
fi
cd "$clone"
# LF checkouts on Windows too (worktrees share this config): CRLF breaks MERGE_HEAD, the
# invariants list and gofmt.
git config core.autocrlf false
git config core.eol lf
git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "https://github.com/$upstream"
# Prairie-only: gh resolves to origin (not the Silo parent) and pushes to upstream are impossible.
git config remote.origin.gh-resolved base
git remote set-url --push upstream DISABLED-prairie-only-no-push-to-silo
git fetch --quiet origin
git fetch --quiet upstream
up_head=$(upstream_head)

echo "merging $upstream ($up_head) into $ORG/$fork main with $(basename "$rules") ..." >&2
result="$workdir/$fork-merge.json"
trailer=(); [ -n "${SYNC_TRAILER:-}" ] && trailer=(--trailer "$SYNC_TRAILER")
python3 "$SYNC_PY" --rules "$rules" --ours origin/main --upstream "$up_head" \
  merge --branch "$branch" --worktree "$wt" --upstream-name "$upstream" "${trailer[@]}" > "$result"
status=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["status"])' "$result")

if [ "$status" = up-to-date ]; then
  echo "$ORG/$fork is up to date with $upstream."; exit 0
fi
[ "$status" = clean ] && git worktree add --quiet "$wt" "$branch"

gd=$(git -C "$wt" rev-parse --absolute-git-dir)
cat > "$gd/prairie-sync.env" <<ENV
FORK=$fork
UPSTREAM_REPO=$upstream
RULES=$rules
MERGE_JSON=$result
BRANCH=$branch
ENV

python3 - "$result" "$workdir" "$fork" "$batch" "$wt" "$upstream" "$TOOL_DIR" <<'PY'
import json, os, sys
res, workdir, fork, batch, wt, upstream, tool = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5], sys.argv[6], sys.argv[7]
d = json.load(open(res))
au = d.get("autoresolved", {})
print(f"\n{d['upstream_commits']} upstream commits; {au.get('files_fully_resolved', 0)} files auto-resolved; "
      f"{len(d['conflicts'])} files left with real conflicts.")
if au.get("upstream_deleted_prairie_modified"):
    print(f"{len(au['upstream_deleted_prairie_modified'])} files upstream deleted that Prairie had modified (see {res}).")
if not d["conflicts"]:
    print(f"\nClean. Review, then run: {tool}/finish.sh {wt}")
    sys.exit()
bd = os.path.join(workdir, f"{fork}-batches")
os.makedirs(bd, exist_ok=True)
for f in os.listdir(bd):
    os.remove(os.path.join(bd, f))
cs = d["conflicts"]
n = 0
for i in range(0, len(cs), batch):
    n += 1
    open(os.path.join(bd, f"{n:02d}.txt"), "w").write("\n".join(cs[i:i + batch]) + "\n")
brief = open(os.path.join(tool, "RESOLVE-BRIEF.md")).read()
for k, v in {"FORK": fork, "UPSTREAM_REPO": upstream, "WORKTREE": wt, "BASE": d["rebranded_base"],
             "OURS": d["ours"], "THEIRS": d["rebranded_upstream"], "RAW_BASE": d["base"],
             "RAW_UPSTREAM": d["upstream"], "N": str(d["upstream_commits"]), "BATCH_DIR": bd}.items():
    brief = brief.replace("{{" + k + "}}", v)
bp = os.path.join(workdir, f"{fork}-RESOLVE-BRIEF.md")
open(bp, "w").write(brief)
print(f"\nWorktree with the in-progress merge: {wt}")
print(f"Agent brief: {bp}")
print(f"Batches ({n}): {bd}/NN.txt  (one agent per batch; they only edit + git add their files)")
print(f"When every file is resolved: {tool}/finish.sh {wt}")
PY
