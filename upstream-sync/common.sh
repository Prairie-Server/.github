# Shared helpers for sync.sh / finish.sh / ci.sh. Source, don't run.
ORG=${PRAIRIE_ORG:-Prairie-Server}
TOOL_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SYNC_PY="$TOOL_DIR/prairie_sync.py"

die() { echo "error: $*" >&2; exit 1; }

# Windows Git Bash has no python3 (or only the Store stub); fall back to the py launcher.
if ! python3 -c '' 2>/dev/null && command -v py >/dev/null; then python3() { py -3 "$@"; }; fi

# rules_for <fork-name>: rules/<name minus prairie->.json, else default.json
rules_for() {
  local r="${1#prairie-}.json"
  [ -f "$TOOL_DIR/rules/$r" ] || r=default.json
  echo "$TOOL_DIR/rules/$r"
}

# upstream_for <fork-name>: the fork's GitHub parent, else Silo-Server/silo-<name>
upstream_for() {
  local p
  p=$(gh api "repos/$ORG/$1" --jq '.parent.full_name // empty' 2>/dev/null || true)
  echo "${p:-Silo-Server/silo-${1#prairie-}}"
}

# upstream_head: upstream's default branch as a local ref (after `git fetch upstream`)
upstream_head() {
  git remote set-head upstream --auto >/dev/null 2>&1 || true
  git symbolic-ref --short refs/remotes/upstream/HEAD 2>/dev/null || echo upstream/main
}

# rules_get <rules.json> <key>: one JSON list entry per line (empty if absent)
rules_get() {
  python3 -c 'import json,sys; [print(x) for x in json.load(open(sys.argv[1])).get(sys.argv[2], [])]' "$1" "$2"
}

# open_sync_pr <owner/repo>: number of the open PR whose head branch starts with sync/
open_sync_pr() {
  gh pr list --repo "$1" --state open --json number,headRefName \
    --jq '[.[]|select(.headRefName|startswith("sync/"))][0].number // empty'
}

# upsert_audit_comment <owner/repo> <pr> <body-file>: one sticky audit comment per PR
upsert_audit_comment() {
  local id
  id=$(gh api "repos/$1/issues/$2/comments" --paginate \
        --jq '[.[]|select(.body|startswith("<!-- prairie-sync-audit -->"))][0].id // empty')
  if [ -n "$id" ]; then
    gh api -X PATCH "repos/$1/issues/comments/$id" -F "body=@$3" >/dev/null
  else
    gh pr comment "$2" --repo "$1" --body-file "$3" >/dev/null
  fi
}

# need_go_if_formatting <rules.json>: gofmt must be on PATH when rules format *.go
need_go_if_formatting() {
  if grep -q '"gofmt"' "$1" && ! command -v gofmt >/dev/null; then
    die "these rules run gofmt on rebranded Go files; put Go on PATH"
  fi
}
