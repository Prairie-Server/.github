# Upstream sync (Silo → Prairie)

Tooling that keeps every `Prairie-Server/prairie-*` fork current with its `Silo-Server/silo-*`
upstream, without hand-merging thousands of rebranded files.

| file | what it is |
|---|---|
| `prairie_sync.py` | the engine: rebrand-aware merge, audit, render (markdown), calibrate/learn (rules tuning) |
| `rules/<repo>.json` | per-repo rebrand rules, pinned paths, generated files and their `regen_cmds` |
| `sync.sh` | local start: clone/fetch, merge, worktree, agent brief and batches |
| `finish.sh` | local finish: commit, audit, invariants, push, PR and audit comment |
| `ci-errors.sh` | error lines of the failing CI jobs on a PR (instead of whole logs) |
| `ci.sh` | the same flow for GitHub Actions (`sync` and `pr` modes) |
| `RESOLVE-BRIEF.md` | template for the per-sync agent brief that `sync.sh` writes |
| `AGENT-GUIDE.md` | the procedure for people and agents, and why hand-merging fails |
| `../.github/workflows/upstream-sync-all.yml` | central automation for every fork |

## What runs automatically

`upstream-sync-all.yml` in this repo:

- **Daily (09:17 UTC), per fork, found via `gh repo list --fork`:**
  - Up to date: closes any stale conflicts issue.
  - Clean merge: pushes `sync/upstream-auto`, opens a PR, regenerates generated files on it and
    posts the audit comment.
  - Conflicts: opens or updates the issue **"Upstream sync needs conflict resolution"**, with the
    file list and the `sync.sh`/`finish.sh` commands.
  - A sync PR is already open: tends it (regen and audit) instead of starting another.
- **Every 3 hours:** tends open sync PRs only, so people's pushes get re-audited and regenerated.
- **Manual:** *Run workflow*, optionally with specific repos or PR-tending only.

A human or agent is only needed for real conflicts and red CI. Landing is always **"Create a
merge commit"**, never squash.

### One-time setup

1. **Org secret `SYNC_TOKEN`:** Settings → Secrets and variables → Actions → New organization
   secret.
   - Value: a fine-grained PAT, resource owner **Prairie-Server**, all repositories, with
     permissions Contents R/W, Pull requests R/W, Issues R/W, Workflows R/W and Metadata R.
   - Grant the secret to the `.github` repo at least.
   - Without it, the workflow fails fast with a clear error. `GITHUB_TOKEN` can't push to other
     repos, and its PRs don't trigger the forks' CI.
2. Optionally add a fork's own `.github/workflows/upstream-sync.yml` from `caller-example.yml` to
   get the audit as a status check on every push.
3. **New fork:** fork it into the org. Discovery is automatic. It uses `rules/default.json` unless
   `rules/<name minus prairie->.json` exists.

## Cost notes

- The merge, audit and render steps are pure git and Python: about 20 s on Apple and a few
  minutes on prairie-server, at about 250 MB RAM. Nothing is compiled.
- Most token spend in past syncs went on CI round-trips over compile errors. Use the audit's
  compile-break checks before pushing.
