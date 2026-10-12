#!/usr/bin/env bash
# Finish an upstream sync started by sync.sh (people and agents).
#
#   upstream-sync/finish.sh [worktree] [--no-push] [--force]
#
# 1. Refuses while files are unmerged or conflict markers remain.
# 2. Commits the merge if it isn't yet (plain `git commit`, so both parents are recorded).
# 3. Runs the audit: conflict markers, likely compile breaks (names used but declared nowhere,
#    names declared twice), lost Prairie lines, new Silo tokens. Also runs
#    scripts/check-prairie-invariants.sh where the repo has it.
# 4. Unless --no-push: pushes the branch, opens the PR (body from the merge stats) and posts
#    the audit as a comment.
#
# Likely compile breaks stop the push unless --force. They're cheap to fix now and cost a CI
# round each later.
set -euo pipefail
source "$(dirname "$0")/common.sh"

wt=.; push=1; force=0
for arg in "$@"; do
  case $arg in
    --no-push) push=0 ;;
    --force) force=1 ;;
    -*) die "unknown flag $arg" ;;
    *) wt=$arg ;;
  esac
done
cd "$wt"
gd=$(git rev-parse --absolute-git-dir)
[ -f "$gd/prairie-sync.env" ] || die "not a sync.sh worktree (no $gd/prairie-sync.env)"
source "$gd/prairie-sync.env"

# 1. resolution complete?
unmerged=$(git diff --name-only --diff-filter=U)
[ -z "$unmerged" ] || die "still unmerged (resolve, then git add):"$'\n'"$unmerged"
markers=$(git grep -lE '^(<<<<<<<|>>>>>>>) ' -- . ':!*.md' || true)
[ -z "$markers" ] || die "conflict markers left in:"$'\n'"$markers"

# 2. commit the merge
if [ -f "$gd/MERGE_HEAD" ]; then
  git add -u
  msgfile=$(mktemp)
  cp "$gd/MERGE_MSG" "$msgfile"
  if [ -n "${SYNC_TRAILER:-}" ] && ! grep -qF "$SYNC_TRAILER" "$msgfile"; then printf '\n%s\n' "$SYNC_TRAILER" >> "$msgfile"; fi
  git commit --quiet -F "$msgfile"; rm -f "$msgfile"
  echo "committed merge $(git rev-parse --short HEAD)"
fi
merge=$(git log --merges -1 --format=%H)
up=$(git rev-parse "$merge^2")

# 3. audit
audit="$gd/prairie-sync-audit.json"
echo "auditing (merge $(git rev-parse --short "$merge"), upstream ${up:0:10}) ..." >&2
rc=0
python3 "$SYNC_PY" --rules "$RULES" --ours origin/main --upstream "$up" audit --result HEAD > "$audit" || rc=$?
python3 - "$audit" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"markers: {len(d['markers'])}   dangling symbols: {len(d.get('dangling_symbols', {}))}   "
      f"duplicate decls: {len(d.get('duplicate_decls', {}))}   files with lost Prairie lines: "
      f"{len(d['lost_prairie_lines'])}   new Silo tokens: {len(d['silo_tokens'])}")
for k, title in (("dangling_symbols", "used but declared nowhere"), ("duplicate_decls", "declared twice")):
    for n, locs in list(d.get(k, {}).items())[:40]:
        print(f"  {title}: {n}  {', '.join(locs[:2])}")
PY
echo "full report: $audit"
[ $rc = 0 ] || die "audit failed (conflict markers)"
inv=0
if [ -f scripts/check-prairie-invariants.sh ]; then bash scripts/check-prairie-invariants.sh || inv=$?; fi
breaks=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(len(d.get("dangling_symbols",{}))+len(d.get("duplicate_decls",{})))' "$audit")
if [ "$push" = 1 ] && [ "$force" = 0 ] && { [ "$breaks" != 0 ] || [ "$inv" != 0 ]; }; then
  die "fix the likely compile breaks / invariants above, commit, and rerun (or --force if they are false positives)"
fi
[ "$push" = 1 ] || { echo "not pushing (--no-push)"; exit 0; }

# 4. push + PR + audit comment
repo="$ORG/$FORK"
git push --quiet -u origin "HEAD:$BRANCH"
pr=$(gh pr list --repo "$repo" --head "$BRANCH" --state open --json number --jq '.[0].number // empty')
if [ -z "$pr" ]; then
  body=$(mktemp)
  python3 "$SYNC_PY" render pr "$MERGE_JSON" --repo "$repo" --upstream-repo "$UPSTREAM_REPO" > "$body"
  [ -z "${SYNC_PR_FOOTER:-}" ] || printf '\n%s\n' "$SYNC_PR_FOOTER" >> "$body"
  n=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["upstream_commits"])' "$MERGE_JSON")
  url=$(gh pr create --repo "$repo" --base main --head "$BRANCH" \
        --title "Sync upstream $UPSTREAM_REPO ($n commits)" --body-file "$body")
  pr=${url##*/}; rm -f "$body"
  echo "opened $url"
fi
comment=$(mktemp)
python3 "$SYNC_PY" render audit "$audit" > "$comment"
upsert_audit_comment "$repo" "$pr" "$comment"; rm -f "$comment"
echo "PR #$pr updated. Land it with \"Create a merge commit\", never squash."
