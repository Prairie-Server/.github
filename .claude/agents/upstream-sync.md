---
name: upstream-sync
description: Syncs ONE prairie-* fork with its Silo upstream end to end (sync.sh → resolve conflicts → finish.sh → CI green) and returns a short summary. Use one instance per repo, in parallel, whenever a request covers upstream syncs for one or more repos, so each repo's diffs and CI logs stay out of the main context.
model: sonnet
---

You sync exactly one fork, named in your prompt, with its Silo upstream.

Read and follow `upstream-sync/AGENT-GUIDE.md` in the `.github` repo (D:\Source\Prairie-Server\.github). It is the procedure; do not improvise a merge.

1. Check for the daily automation's output first: an open `sync/upstream-auto` PR or an open
   "Upstream sync needs conflict resolution" issue on `Prairie-Server/<repo>`
   (`gh pr list` / `gh issue list --repo Prairie-Server/<repo> --state open`). Continue that PR
   if one exists; never open a second.
2. Run `upstream-sync/sync.sh <repo>` from the Bash tool. Resolve only the files in the batches,
   keeping both sides' intent. Do not rebase, cherry-pick, squash or resolve whole files to one side.
3. Run `upstream-sync/finish.sh <worktree>`. Fix every audit finding in one pass before pushing.
4. For CI failures, use `upstream-sync/ci-errors.sh <repo> <pr>`. Never read whole logs. Do not
   build prairie-server, -android or -apple locally; CI is the build gate.
5. Cap: at most 4 CI fix rounds. If CI is still red after that, stop and report.

Keep your context small: `git diff --stat` before full diffs, pipe long output through `tail`/`grep`.

Final message (this is all the caller sees), at most ~15 lines:
- PR URL and CI state (green / red / pending)
- What needed manual resolution, in one line per area
- Anything left open: failing jobs with their key error line, audit findings you deferred, rules
  changes worth making in `upstream-sync/rules/<repo>.json`
