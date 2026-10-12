# Upstream syncs (Silo → Prairie): agent guide

Every `prairie-*` fork tracks a `Silo-Server/silo-*` upstream. Use this guide for any
"sync with upstream" or "get X up to date with Silo" task. **Do not hand-merge.**

## Why hand-merging fails

Prairie renamed Silo → Prairie across code, paths, packages and wire names, so a plain
`git merge upstream/main` conflicts on nearly every file. Past agent attempts failed in
four ways:

1. **Merging a rebranded *copy* of upstream** (commit "Rebrand upstream silo tree…", then
   merge that). The real upstream commit never becomes a parent, so git doesn't know
   upstream was merged. The next sync starts from the old merge-base and re-conflicts
   everything. prairie-server #222 did this: 913 commits, 3,700 files.
2. **Resolving whole files to one side** ("upstream-preferred on overlaps", "union"). This
   silently deletes Prairie features while their tests survive. It has happened to Live TV
   wiring, stream-token auth, the quality ladder, the Tizen HLS fix and others.
3. **Then patching the fallout commit by commit** ("Restore X dropped by the re-merge",
   25–30 commits per PR). Each fix finds the next breakage. CI stays red and tokens burn.
4. **Squash-merging the sync PR.** Upstream ancestry is lost, with the same effect as (1).

## The procedure

Don't improvise the merge. Three scripts in this repo do the mechanical work; your job is only
the real conflicts they leave.

```sh
git clone --depth 1 https://github.com/Prairie-Server/.github prairie-dotgithub
T=prairie-dotgithub/upstream-sync
export SYNC_TRAILER="Co-Authored-By: <your model attribution>"   # optional

$T/sync.sh prairie-<repo>          # clone/fetch, rebrand-aware merge, worktree, brief, batches
# ... resolve the files listed in the batches (see below) ...
$T/finish.sh ~/prairie-sync/prairie-<repo>-sync   # commit, audit, invariants, push, PR, audit comment
```

**Before you start, check for automation output.** `upstream-sync-all.yml` runs every day for
every fork. A clean merge already arrives as a PR (`sync/upstream-auto`). Conflicts arrive as an
issue, **"Upstream sync needs conflict resolution"**, listing the files. Start from that issue,
and don't open a second sync PR while one is open.

### What `sync.sh` gives you

- `~/prairie-sync/prairie-<repo>-sync`: a worktree with an in-progress merge. `MERGE_HEAD` is the
  **real** upstream commit, so `finish.sh` (a plain `git commit`) records both parents.
- `~/prairie-sync/prairie-<repo>-merge.json`: stats, the conflict list,
  `upstream_deleted_prairie_modified`, and the generated files taken from upstream.
- `~/prairie-sync/prairie-<repo>-RESOLVE-BRIEF.md` and `prairie-<repo>-batches/NN.txt`: a
  self-contained brief and file batches of about 50. Hand one batch plus the brief to each
  sub-agent; they edit and `git add` only their files.

The tool rebrands the merge-base and upstream with the repo's rules, merges against Prairie
`main`, and auto-resolves every hunk where one side only changed brand words, whitespace,
formatting or imports. What's left is real overlap, usually 5–10% of the naive conflict count.

### Resolve the remaining hunks

Hunks are diff3 style: `<<<<<<< origin/main` (Prairie) / `||||||| refs/prairie-sync/base`
(rebranded merge-base) / `=======` / `>>>>>>> refs/prairie-sync/upstream` (rebranded upstream).

- See what each side meant: `git diff refs/prairie-sync/base origin/main -- <f>` (Prairie) and
  `git diff refs/prairie-sync/base refs/prairie-sync/upstream -- <f>` (upstream).
- **Keep both intents.** Take upstream's new structure and re-apply Prairie's change on top. If
  upstream renamed or removed an API Prairie code calls, port the Prairie code.
- If both sides added the same field, function or constant, keep **one**.
- Never resolve a whole file to one side unless the other side's hunk is cosmetic.
- Generated files (`regen` in the rules) were taken from upstream. The automation regenerates
  them on the PR branch (rules' `regen_cmds`). Don't hand-merge them.
- `upstream_deleted_prairie_modified`: upstream removed or moved these files and the delete was
  applied. Check Prairie's diff; if it was a feature, re-home it in upstream's replacement.
- Don't rebase, cherry-pick or squash the merge.

### Verify, then push: `finish.sh`

`finish.sh` refuses to continue while files are unmerged or markers remain. It then commits the
merge and runs `audit` and the repo's `scripts/check-prairie-invariants.sh`. The audit reports:

| check | meaning |
|---|---|
| conflict markers | must be zero |
| **used but declared nowhere** | a type/function that existed at Prairie main or upstream is gone, but the result still calls it. Almost always a compile error: port the caller to upstream's replacement. |
| **declared twice** | both sides added the same Go top-level name or Swift/Kotlin stored property. Keep one. |
| lost Prairie lines | Prairie customizations no longer present. Each is intended (upstream rewrote that code) or a lost feature; check them. |
| new Silo tokens | rebrand them, or add them to `protect_tokens` if deliberate |

The two compile-break checks stop the push unless `--force`. They're grep-level, with no
compiler. They catch what took several CI rounds in past syncs (duplicate fields from both sides,
removed design-system types, helpers renamed on one side only), but they won't catch everything.
Then `finish.sh` pushes, opens the PR and posts the audit as a sticky comment.

After that, CI is the build gate. **Rebrand fallout** goes in follow-up commits on the same
branch: new upstream tests asserting "Silo", `Silo/` user agents, `X-Silo-*` headers. Land with
**"Create a merge commit"**. A failing test after a sync usually means Prairie code was lost, not
that the test is stale. Don't edit `scripts/prairie-invariants.txt` to make it pass.

### Budget

- Don't build the big projects locally. prairie-server's Go compile has OOM-crashed a 7 GB host,
  and the Android and Apple builds need their own toolchains. Let CI build. Locally, run only
  targeted checks: gofmt, invariants, `finish.sh --no-push`, single-package tests.
- Fix all audit findings in one pass before pushing. Each CI round on prairie-apple costs about
  12 minutes.
- For CI failures, run `upstream-sync/ci-errors.sh prairie-<repo> <pr>`. It prints only the error
  lines of the failing jobs on the PR's current head. Don't read whole logs.

## Rules hygiene

When `audit` reports Silo tokens the rules should have caught, or noise conflicts the tool
should have resolved, fix `upstream-sync/rules/<repo>.json` in this repo. Don't fix each sync
by hand. To check a rules change:

```sh
python3 prairie_sync.py --rules rules/<repo>.json calibrate   # files where rebrand(base) != Prairie main
python3 prairie_sync.py --rules rules/<repo>.json learn       # per-token kept/renamed stats → protect_tokens
```

Rules files also carry `regen_cmds`, the shell commands the automation runs on a sync PR branch
to rebuild generated files. Keep them in step with the repo's Makefile.

`protect_tokens` holds Silo names Prairie deliberately kept: DB columns, persisted keys,
`x-silo-*` OpenAPI extensions, local identifiers the fork never renamed. A token in this list
stays `silo` in new upstream code too, so new code still references the same symbol.
