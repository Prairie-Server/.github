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

The tool lives in this repo (`upstream-sync/prairie_sync.py`, rules in `upstream-sync/rules/`).
It rebrands the merge-base and upstream with the repo's rules, does the 3-way merge against
Prairie `main`, and auto-resolves every hunk where one side only changed brand words,
whitespace, formatting or imports. What's left is real overlap, usually 5–10% of the
naive conflict count.

A scheduled GitHub Action (`upstream-sync.yml`) already runs this every day. If the merge is
clean it opens a PR. If not, it opens an issue titled **"Upstream sync needs conflict
resolution"** listing the files. Start from that issue. Locally:

```sh
git clone https://github.com/Prairie-Server/<repo> && cd <repo>
git remote add upstream https://github.com/Silo-Server/<silo-repo> && git fetch upstream
git clone --depth 1 https://github.com/Prairie-Server/.github ../prairie-dotgithub
T=../prairie-dotgithub/upstream-sync
# prints JSON: conflicts, autoresolved stats, upstream_deleted_prairie_modified
python3 $T/prairie_sync.py --rules $T/rules/<rules>.json \
  merge --branch sync/upstream-$(date +%F) --worktree ../<repo>-sync \
  --trailer "Co-Authored-By: <your model attribution>"
cd ../<repo>-sync      # merge in progress; MERGE_HEAD = the real upstream commit
```

Rules file: `server.json`, `android.json`, `apple.json`, `plugin-sdk.json` and `plugins.json`
are tuned. Use `default.json` for the plugin, theme and push-relay repos. If the tool needs
gofmt, put Go on PATH.

### Resolve the remaining hunks

Hunks are in diff3 style: `<<<<<<< origin/main` (Prairie) / `||||||| refs/prairie-sync/base`
(rebranded merge-base) / `=======` / `>>>>>>> refs/prairie-sync/upstream` (rebranded upstream).

- See what each side meant with `git diff refs/prairie-sync/base origin/main -- <f>` (Prairie)
  and `git diff refs/prairie-sync/base refs/prairie-sync/upstream -- <f>` (upstream).
- **Keep both intents.** Take upstream's new structure and re-apply Prairie's change on top.
  If upstream renamed an API that Prairie code calls, port the Prairie code.
- Never resolve a whole file to one side unless you've confirmed the other side's hunk is
  cosmetic.
- Generated files listed under `regen_taken_from_upstream` (route-inventory, schema.ts,
  lockfiles, `.pb.go`, go.sum) were taken from upstream. **Regenerate them**; don't hand-merge.
- `upstream_deleted_prairie_modified`: upstream removed or moved these files, and the delete
  was applied. Check Prairie's diff. If it was a feature, re-home it in upstream's
  replacement.
- Remove every marker, then `git add`. Commit with plain `git commit`, which records both
  parents. Do **not** rebase, cherry-pick or squash the merge.

### Verify, then push

```sh
python3 $T/prairie_sync.py --rules $T/rules/<rules>.json --ours origin/main \
  --upstream "$(git rev-parse MERGE_HEAD 2>/dev/null || git rev-parse HEAD^2)" audit
bash scripts/check-prairie-invariants.sh   # where the repo has it
```

`audit` fails if any conflict markers remain. It also lists **Prairie lines that vanished**
(each one is either intended or a lost feature, so check them) and **Silo tokens the sync
introduced** (rebrand them).

Push the branch, open a PR, and wait for CI. **Rebrand fallout** (new upstream tests asserting
"Silo", `Silo/` user agents, `X-Silo-*` headers) goes in follow-up commits on the same branch.
Land it with **"Create a merge commit"**. A failing test after a sync usually means Prairie
code was lost, not that the test is stale. Don't edit `scripts/prairie-invariants.txt` to make
it pass.

### Budget

Don't build the big projects locally. prairie-server's Go compile has OOM-crashed a 7 GB
host, and Android and Apple builds need their own toolchains. Let CI build. Locally, run
targeted checks only: gofmt, the invariants grep, single-package tests.

## Rules hygiene

When `audit` reports Silo tokens the rules should have caught, or noise conflicts the tool
should have resolved, fix `upstream-sync/rules/<repo>.json` in this repo. Don't fix each sync
by hand. To check a rules change:

```sh
python3 prairie_sync.py --rules rules/<repo>.json calibrate   # files where rebrand(base) != Prairie main
python3 prairie_sync.py --rules rules/<repo>.json learn       # per-token kept/renamed stats → protect_tokens
```

`protect_tokens` holds Silo names Prairie deliberately kept: DB columns, persisted keys,
`x-silo-*` OpenAPI extensions, local identifiers the fork never renamed. A token in this list
stays `silo` in new upstream code too, so new code still references the same symbol.
