# Resolve conflicts in a Prairie upstream sync ({{FORK}})

You are resolving merge conflicts in `{{WORKTREE}}`. It holds an in-progress merge of
`{{UPSTREAM_REPO}}` ({{N}} new upstream commits) into `Prairie-Server/{{FORK}}`, a rebranded
(Silo → Prairie) fork. `upstream-sync/prairie_sync.py` already did the mechanical work:

- Upstream was rebranded before merging, so both sides already use Prairie names.
- Every hunk where one side only changed brand words, whitespace, formatting or imports was
  resolved automatically.

What's left are **real overlaps**, where Prairie and upstream changed the same code. Hunks use
diff3 style:

```
<<<<<<< origin/main                 OURS: Prairie main (the fork's customizations)
||||||| refs/prairie-sync/base      BASE: rebrand(merge-base)
=======
>>>>>>> refs/prairie-sync/upstream  THEIRS: rebrand(upstream)
```

Refs (run git inside the worktree):

| | commit |
|---|---|
| BASE (rebranded merge-base) | `{{BASE}}` |
| OURS (Prairie main) | `{{OURS}}` |
| THEIRS (rebranded upstream) | `{{THEIRS}}` |
| raw upstream range | `{{RAW_BASE}}..{{RAW_UPSTREAM}}` |

- What Prairie changed: `git diff {{BASE}} {{OURS}} -- <file>`
- What upstream changed: `git diff {{BASE}} {{THEIRS}} -- <file>`
- Why upstream changed it: `git log {{RAW_BASE}}..{{RAW_UPSTREAM}} -- <file with silo in place of prairie>`

## How to resolve each hunk

1. Work out what each side intended, using the two diffs above.
2. **Keep both intents.** Usually that means taking upstream's new structure and re-applying
   Prairie's change on top.
   - If upstream renamed or removed an API that Prairie code calls, port the Prairie code to the
     new API. Don't resurrect the old one.
   - If upstream deleted code Prairie had modified: when Prairie's change was cosmetic (lint,
     formatting, `t.Parallel`), drop it. When it was a feature, re-home it and report it.
   - If both sides added the same field, function or constant, keep **one**.
3. **Never resolve a whole file to one side** without confirming the other side's hunks are cosmetic.
4. Keep Prairie names. Silo names that are deliberately kept are in the repo's rules file under
   `protect`/`protect_tokens` (DB columns, persisted keys, `x-silo-*`, Silo Media LLC attribution,
   real dependency URLs such as `Silo-Server/AetherEngine`).
5. Generated files (lockfiles, go.sum, `*.pb.go`, OpenAPI/route inventories, `schema.ts`) were
   taken from upstream. Don't hand-merge them; they get regenerated.
6. JSON fixtures and manifests: keep entries from both sides. The result must be valid JSON with
   no duplicate keys.

## Rules

- Edit only the files in your batch (`{{BATCH_DIR}}/NN.txt`). Remove every marker. Run
  `git add <file>` when a file is done.
- Don't run heavy builds on a small host. For Go, `gofmt -l` is fine; leave compiling to CI.
- Don't commit, push, rebase or touch other branches. `finish.sh` commits the merge.
- Check your batch is clean: `grep -nE '^(<<<<<<<|=======|>>>>>>>|\|\|\|\|\|\|\|)( |$)' $(cat <batch>)`.
  A bare `=======` can be legitimate in Markdown, so check the context.

## Report (your final message)

- One line per file: `path: took theirs | took ours | combined - what you did`.
- A separate **Judgement calls** list: any Prairie feature dropped, moved or re-designed, and
  anything you were unsure about.
