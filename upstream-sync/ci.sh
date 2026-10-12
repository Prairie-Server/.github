#!/usr/bin/env bash
# Upstream sync automation for GitHub Actions. Run from inside a full (fetch-depth: 0) checkout
# of the fork, with GH_TOKEN set to a token that can push and open PRs/issues on it.
#
#   ci.sh sync <owner/fork> <owner/upstream>   daily: merge upstream, open a PR (clean) or a
#                                              conflicts issue; tend the open sync PR instead
#                                              when there is one
#   ci.sh pr   <owner/fork> <owner/upstream> <pr>
#                                              tend a sync PR: regenerate generated files
#                                              (rules' regen_cmds) and push them, then post or
#                                              refresh the audit comment
#
# Pushes made with GITHUB_TOKEN don't trigger CI on the fork, so use a PAT/app token (SYNC_TOKEN).
set -euo pipefail
source "$(dirname "$0")/common.sh"

cmd=${1:-}; repo=${2:-}; upstream=${3:-}
[ -n "$cmd" ] && [ -n "$repo" ] && [ -n "$upstream" ] || die "usage: ci.sh sync|pr <owner/fork> <owner/upstream> [pr]"
fork=${repo#*/}; rules=$(rules_for "$fork")
tmp=${RUNNER_TEMP:-$(mktemp -d)}
summary=${GITHUB_STEP_SUMMARY:-/dev/null}

git config user.name "prairie-sync[bot]"
git config user.email "prairie-sync@users.noreply.github.com"
git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "https://github.com/$upstream"
git fetch --quiet upstream
up_head=$(upstream_head)

tend_pr() {   # <pr>
  local pr=$1 head merged_up changed=0 failed=()
  head=$(gh pr view "$pr" --repo "$repo" --json headRefName --jq .headRefName)
  git fetch --quiet origin "$head"
  git checkout --quiet -B "$head" "origin/$head"

  # Regenerate generated files.
  mapfile -t cmds < <(rules_get "$rules" regen_cmds)
  if [ ${#cmds[@]} -gt 0 ]; then
    if printf '%s\n' "${cmds[@]}" | grep -q pnpm && ! command -v pnpm >/dev/null; then npm install -g --silent pnpm@10; fi
    for c in "${cmds[@]}"; do
      echo "::group::regen: $c"
      bash -c "$c" || failed+=("$c")
      echo "::endgroup::"
    done
    if [ -n "$(git status --porcelain)" ]; then
      git add -A
      git commit --quiet -m "Regenerate generated files after the upstream sync" \
        -m "Ran: $(printf '%s; ' "${cmds[@]}")"
      git push --quiet origin "HEAD:$head"
      changed=1
    fi
  fi

  # Audit and refresh the sticky comment.
  merged_up=$(git merge-base HEAD "$up_head")
  local rc=0
  python3 "$SYNC_PY" --rules "$rules" --ours origin/main --upstream "$merged_up" \
    audit --result HEAD > "$tmp/audit.json" || rc=$?
  python3 "$SYNC_PY" render audit "$tmp/audit.json" > "$tmp/audit.md"
  if [ ${#failed[@]} -gt 0 ]; then
    { echo; echo "**Regeneration commands that failed** (see the workflow log):"; printf -- '- `%s`\n' "${failed[@]}"; } >> "$tmp/audit.md"
  fi
  [ $changed = 1 ] && printf '\nRegenerated files were pushed to this branch (%s).\n' "$(git rev-parse --short HEAD)" >> "$tmp/audit.md"
  upsert_audit_comment "$repo" "$pr" "$tmp/audit.md"
  cat "$tmp/audit.md" >> "$summary"
  return $rc
}

case $cmd in
  pr)
    tend_pr "${4:?pr number}" ;;
  sync)
    pr=$(open_sync_pr "$repo")
    if [ -n "$pr" ]; then
      echo "sync PR #$pr is open; tending it instead of starting a new sync"
      tend_pr "$pr"; exit
    fi
    need_go_if_formatting "$rules"
    python3 "$SYNC_PY" --rules "$rules" --ours origin/main --upstream "$up_head" \
      merge --branch sync/upstream-auto --upstream-name "$upstream" > "$tmp/merge.json"
    status=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["status"])' "$tmp/merge.json")
    title="Upstream sync needs conflict resolution"
    issue=$(gh issue list --repo "$repo" --state open --search "\"$title\" in:title" --json number --jq '.[0].number // empty')
    case $status in
      up-to-date)
        echo "$repo is up to date with $upstream" | tee -a "$summary"
        [ -z "$issue" ] || gh issue close "$issue" --repo "$repo" --comment "Up to date with upstream now." ;;
      clean)
        git push --quiet --force origin sync/upstream-auto
        python3 "$SYNC_PY" render pr "$tmp/merge.json" --repo "$repo" --upstream-repo "$upstream" > "$tmp/pr.md"
        n=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["upstream_commits"])' "$tmp/merge.json")
        url=$(gh pr create --repo "$repo" --base main --head sync/upstream-auto \
              --title "Sync upstream $upstream ($n commits)" --body-file "$tmp/pr.md")
        echo "opened $url" | tee -a "$summary"
        [ -z "$issue" ] || gh issue close "$issue" --repo "$repo" --comment "Superseded by $url (merged cleanly)."
        tend_pr "${url##*/}" ;;
      conflicts)
        python3 "$SYNC_PY" render issue "$tmp/merge.json" --repo "$repo" --upstream-repo "$upstream" > "$tmp/issue.md"
        if [ -n "$issue" ]; then gh issue edit "$issue" --repo "$repo" --body-file "$tmp/issue.md" >/dev/null
        else gh issue create --repo "$repo" --title "$title" --body-file "$tmp/issue.md" >/dev/null; fi
        cat "$tmp/issue.md" >> "$summary"
        echo "conflicts: see the \"$title\" issue on $repo" ;;
    esac ;;
  *) die "unknown command $cmd" ;;
esac
