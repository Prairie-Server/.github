#!/usr/bin/env bash
# Upstream sync automation for GitHub Actions. Run from inside a full (fetch-depth: 0) checkout
# of the fork, with GH_TOKEN set to a token that can push and open PRs/issues on it.
#
#   ci.sh sync <owner/fork> <owner/upstream>   daily: merge upstream, open a PR (clean) or a
#                                              conflicts issue; when a sync PR is open, merge
#                                              newer upstream into it and tend it instead
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

merge_json() {   # <file> <key>
  python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1" "$2"
}

# Forks with issues turned off can't take the conflicts issue; report in the job instead.
has_issues=$(gh api "repos/$repo" --jq .has_issues)
title="Upstream sync needs conflict resolution"
conflicts_issue() {
  [ "$has_issues" = true ] || return 0
  gh issue list --repo "$repo" --state open --search "\"$title\" in:title" --json number --jq '.[0].number // empty'
}
close_conflicts_issue() {   # <comment>
  local issue; issue=$(conflicts_issue)
  [ -z "$issue" ] || gh issue close "$issue" --repo "$repo" --comment "$1"
}

# advance_pr <pr>: an open sync PR must not freeze the fork at the upstream commit it was opened
# for. Merge newer upstream into its branch (the PR head is "ours", so earlier resolutions stay);
# if that conflicts, say so on the PR and leave the branch as it is.
advance_pr() {
  local pr=$1 head n
  head=$(gh pr view "$pr" --repo "$repo" --json headRefName --jq .headRefName)
  git fetch --quiet origin "$head"
  if git merge-base --is-ancestor "$up_head" "origin/$head"; then
    echo "sync PR #$pr already contains $upstream $(git rev-parse --short "$up_head")"; return 0
  fi
  python3 "$SYNC_PY" --rules "$rules" --ours "origin/$head" --upstream "$up_head" \
    merge --branch "$head" --upstream-name "$upstream" --ours-name "$head" > "$tmp/advance.json"
  case $(merge_json "$tmp/advance.json" status) in
    clean)
      # Fast-forward of the PR branch: the new merge commit has the old head as first parent.
      git push --quiet origin "refs/heads/$head:refs/heads/$head"
      n=$(git rev-list --count "origin/main..$up_head")
      gh pr edit "$pr" --repo "$repo" --title "Sync upstream $upstream ($n commits)" >/dev/null
      gh pr comment "$pr" --repo "$repo" --body "Merged $(merge_json "$tmp/advance.json" upstream_commits) newer upstream commits into this branch (\`$(git rev-parse --short "$up_head")\`)." >/dev/null
      echo "advanced sync PR #$pr to $upstream $(git rev-parse --short "$up_head")" | tee -a "$summary" ;;
    conflicts)
      python3 "$SYNC_PY" render issue "$tmp/advance.json" --repo "$repo" --upstream-repo "$upstream" > "$tmp/advance.md"
      { echo "<!-- prairie-sync-advance -->"
        echo "**Upstream has moved on and merging it into this branch conflicts.** Land this PR first (merge commit); the next daily run then opens the conflicts issue for the rest."
        echo; cat "$tmp/advance.md"; } > "$tmp/advance-comment.md"
      upsert_marked_comment "$pr" "<!-- prairie-sync-advance -->" "$tmp/advance-comment.md"
      echo "::warning::$repo: newer upstream conflicts with sync PR #$pr; left the branch as it was" ;;
  esac
}

upsert_marked_comment() {   # <pr> <marker> <body-file>
  local id
  id=$(gh api "repos/$repo/issues/$1/comments" --paginate \
        --jq "[.[]|select(.body|startswith(\"$2\"))][0].id // empty")
  if [ -n "$id" ]; then gh api -X PATCH "repos/$repo/issues/comments/$id" -F "body=@$3" >/dev/null
  else gh pr comment "$1" --repo "$repo" --body-file "$3" >/dev/null; fi
}

case $cmd in
  pr)
    tend_pr "${4:?pr number}" ;;
  sync)
    need_go_if_formatting "$rules"
    pr=$(open_sync_pr "$repo")
    if [ -n "$pr" ]; then
      echo "sync PR #$pr is open; advancing and tending it instead of starting a new sync"
      advance_pr "$pr"
      tend_pr "$pr"; exit
    fi
    python3 "$SYNC_PY" --rules "$rules" --ours origin/main --upstream "$up_head" \
      merge --branch sync/upstream-auto --upstream-name "$upstream" > "$tmp/merge.json"
    case $(merge_json "$tmp/merge.json" status) in
      up-to-date)
        echo "$repo is up to date with $upstream" | tee -a "$summary"
        close_conflicts_issue "Up to date with upstream now." ;;
      clean)
        git push --quiet --force origin sync/upstream-auto
        python3 "$SYNC_PY" render pr "$tmp/merge.json" --repo "$repo" --upstream-repo "$upstream" > "$tmp/pr.md"
        n=$(merge_json "$tmp/merge.json" upstream_commits)
        url=$(gh pr create --repo "$repo" --base main --head sync/upstream-auto \
              --title "Sync upstream $upstream ($n commits)" --body-file "$tmp/pr.md")
        echo "opened $url" | tee -a "$summary"
        close_conflicts_issue "Superseded by $url (merged cleanly)."
        tend_pr "${url##*/}" ;;
      conflicts)
        python3 "$SYNC_PY" render issue "$tmp/merge.json" --repo "$repo" --upstream-repo "$upstream" > "$tmp/issue.md"
        cat "$tmp/issue.md" >> "$summary"
        if [ "$has_issues" != true ]; then
          echo "::error::$repo has real conflicts with $upstream and issues are disabled; see the job summary"
          exit 1
        fi
        issue=$(conflicts_issue)
        if [ -n "$issue" ]; then gh issue edit "$issue" --repo "$repo" --body-file "$tmp/issue.md" >/dev/null
        else gh issue create --repo "$repo" --title "$title" --body-file "$tmp/issue.md" >/dev/null; fi
        echo "conflicts: see the \"$title\" issue on $repo" ;;
    esac ;;
  *) die "unknown command $cmd" ;;
esac
