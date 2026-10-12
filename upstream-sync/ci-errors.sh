#!/usr/bin/env bash
# Failing CI jobs on a PR's current head, with only their error lines. Saves reading whole logs.
#
#   upstream-sync/ci-errors.sh <fork> <pr> [max-lines-per-job]
#     fork  e.g. prairie-server (Prairie-Server/ is implied) or owner/repo
set -euo pipefail
source "$(dirname "$0")/common.sh"
repo=${1:?usage: ci-errors.sh <fork> <pr> [max-lines]}; pr=${2:?pr number}; max=${3:-25}
[[ $repo == */* ]] || repo="$ORG/$repo"
sha=$(gh pr view "$pr" --repo "$repo" --json headRefOid --jq .headRefOid)
echo "PR #$pr head ${sha:0:10}"
runs=$(gh api "repos/$repo/actions/runs?head_sha=$sha&per_page=50" --jq '.workflow_runs[].id')
pending=0
for run in $runs; do
  while IFS=$'\t' read -r id name status concl; do
    [ "$status" = completed ] || { pending=$((pending + 1)); continue; }
    [ "$concl" = failure ] || continue
    echo; echo "=== $name (job $id)"
    gh api "repos/$repo/actions/jobs/$id/logs" 2>/dev/null \
      | sed -E 's/^[0-9T:.-]+Z //' \
      | grep -E "error:|##\[error\]|FAILED|^e: |undefined: |panic:|^--- FAIL|^FAIL\b|ERR_PNPM|AssertionError|Expected|✗|×|not enough arguments|cannot use|Unresolved reference" \
      | grep -vE "^(SwiftCompile|SwiftDriver|\s+builtin-|\s+/Applications/)|^\S*\[36;1m| WARN |Problems API|lint-baseline|Process completed with exit code|if-no-files-found|google-cloud-errors" \
      | awk '!seen[$0]++' | head -n "$max" | cut -c1-300
  done < <(gh api "repos/$repo/actions/runs/$run/jobs?per_page=100" --jq '.jobs[] | [.id, .name, .status, (.conclusion // "")] | @tsv')
done
[ $pending = 0 ] || echo; [ $pending = 0 ] || echo "($pending job(s) still running on this head)"
