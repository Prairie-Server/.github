# context-mode — MANDATORY routing rules

Rules protect context window from flooding. One unrouted command dumps 56 KB into context.

**Only when `context-mode_ctx_*` tools are actually in your tool list** (Kilo loads them via `kilo.json`). Without them (e.g. Claude Code without the plugin), nothing below is blocked: apply the same intent with your own tools — filter in a script and print only the answer, pipe shell output through `grep`/`tail`/`head`, read only the file ranges you need. The **Model routing** and **Context budget** sections apply everywhere.

## Think in Code — MANDATORY

Analyze/count/filter/compare/search/parse/transform data: **write code** via `context-mode_ctx_execute(language, code)`, `console.log()` only the answer. Do NOT read raw data into context. PROGRAM the analysis, not COMPUTE it. Pure JavaScript — Node.js built-ins only (`fs`, `path`, `child_process`). `try/catch`, handle `null`/`undefined`. One script replaces ten tool calls.

## Model routing — MANDATORY (Claude Haiku 5.5, announced 2026-10-07)

Use the cheapest model that reliably does the task; reserve large models for work that needs them. Source: https://www.anthropic.com/claude-haiku-5-5

- **Haiku 5.5 (`claude-haiku-5-5`)** — default for high-volume, narrowly scoped, repetitive work: summaries, compaction, classification, database/query-style lookups, quick subagent tasks, speed-sensitive browser use. ~75% cheaper than Haiku 4.5; especially good value for prompts ≤100k tokens (~90% of requests). First Haiku-class model with adjustable effort (Low/Med/High/Xhigh/Max): use Low/Med for routine work, raise only when accuracy demands it.
- **Sonnet 5.5 / Opus 5.5** — keep for complex agentic coding (multi-step repo work, Terminal-Bench-class tasks). Sonnet 5.5 cache reads are now halved ($0.10/M vs $0.20), making most agentic runs ~20% cheaper — structure work to maximize cache hits: stable instruction prefix, append-only context, never rewrite large unchanged blocks.
- Never downgrade complex coding tasks to Haiku 5.5; never pay for Sonnet/Opus on trivial subtasks. Pair Haiku 5.5 as a subagent under Sonnet/Opus for coding work.

## BLOCKED — do NOT attempt

### curl / wget — BLOCKED
Shell `curl`/`wget` intercepted and blocked. Do NOT retry.
Use: `context-mode_ctx_fetch_and_index(url, source)` or `context-mode_ctx_execute(language: "javascript", code: "const r = await fetch(...)")`

### Inline HTTP — BLOCKED
`fetch('http`, `requests.get(`, `requests.post(`, `http.get(`, `http.request(` — intercepted. Do NOT retry.
Use: `context-mode_ctx_execute(language, code)` — only stdout enters context

### Direct web fetching — BLOCKED
Use: `context-mode_ctx_fetch_and_index(url, source)` then `context-mode_ctx_search(queries)`

## REDIRECTED — use sandbox

### Shell (>20 lines output)
Shell ONLY for: `git`, `mkdir`, `rm`, `mv`, `cd`, `ls`, `npm install`, `pip install`.
Otherwise: `context-mode_ctx_batch_execute(commands, queries)` or `context-mode_ctx_execute(language: "javascript", code: "...")`. Use `language: "shell"` only when code matches the host shell.

### File reading (for analysis)
Reading to **edit** → reading correct. Reading to **analyze/explore/summarize** → `context-mode_ctx_execute_file(path, language, code)`.

### grep / search (large results)
Use `context-mode_ctx_execute(language: "javascript", code: "...")` in sandbox for portable filtering/counting.

## Tool selection

0. **MEMORY**: `context-mode_ctx_search(sort: "timeline")` — after resume, check prior context before asking user.
1. **GATHER**: `context-mode_ctx_batch_execute(commands, queries)` — runs all commands, auto-indexes, returns search. ONE call replaces 30+. Each command: `{label: "header", command: "..."}`.
2. **FOLLOW-UP**: `context-mode_ctx_search(queries: ["q1", "q2", ...])` — all questions as array, ONE call (default relevance mode).
3. **PROCESSING**: `context-mode_ctx_execute(language, code)` | `context-mode_ctx_execute_file(path, language, code)` — sandbox, only stdout enters context.
4. **WEB**: `context-mode_ctx_fetch_and_index(url, source)` then `context-mode_ctx_search(queries)` — raw HTML never enters context.
5. **INDEX**: `context-mode_ctx_index(content, source)` — store in FTS5 for later search.

## Parallel I/O batches

For multi-URL fetches or multi-API calls, **always** include `concurrency: N` (1-8):

- `context-mode_ctx_batch_execute(commands: [3+ network commands], concurrency: 5)` — gh, curl, dig, docker inspect, multi-region cloud queries
- `context-mode_ctx_fetch_and_index(requests: [{url, source}, ...], concurrency: 5)` — multi-URL batch fetch

**Use concurrency 4-8** for I/O-bound work (network calls, API queries). **Keep concurrency 1** for CPU-bound (npm test, build, lint) or commands sharing state (ports, lock files, same-repo writes).

GitHub API rate-limit: cap at 4 for `gh` calls.

## Output

Write artifacts to FILES — never inline. Return: file path + 1-line description.
Descriptive source labels for `context-mode_ctx_search(source: "label")`.

## Session Continuity

Skills, roles, and decisions persist for the entire session. Do not abandon them as the conversation grows.

## Memory

Session history is persistent and searchable. On resume, search BEFORE asking the user:

| Need | Command |
|------|---------|
| What did we decide? | `context-mode_ctx_search(queries: ["decision"], source: "decision", sort: "timeline")` |
| What constraints exist? | `context-mode_ctx_search(queries: ["constraint"], source: "constraint")` |

DO NOT ask "what were we working on?" — SEARCH FIRST.
If search returns 0 results, proceed as a fresh session.

## ctx commands

| Command | Action |
|---------|--------|
| `ctx stats` | Call `stats` MCP tool, display full output verbatim |
| `ctx doctor` | Call `doctor` MCP tool, run returned shell command, display as checklist |
| `ctx upgrade` | Call `upgrade` MCP tool, run returned shell command, display as checklist |
| `ctx purge` | Call `purge` MCP tool with confirm: true. Warns before wiping knowledge base. |

After /clear or /compact: knowledge base and session stats preserved. Use `ctx purge` to start fresh.

## Context budget — MANDATORY

Cost is context size × turns, not output. In the 30 days to 2026-10-12, one session that bundled
four repo syncs used 75% of all workspace tokens: 266 calls, up to 418k of context re-sent per call, never compacted.

- **One repo per context.** A request that spans several repos (e.g. "sync apple, android and the SDK") fans
  out to one subagent per repo (`upstream-sync` agent for syncs). The main session only coordinates and
  keeps the one-paragraph results.
- **CI failures:** run `upstream-sync/ci-errors.sh <repo> <pr>` or delegate to the `ci-triage` agent. Never page whole logs into context.
- **Bound output:** `git diff --stat` before a full diff, `| tail -40` on builds and tests, `Grep`/ranged `Read`
  instead of whole large files.
- **Compact at phase boundaries.** Once a fix has landed, its logs and diffs are dead weight: `/compact` (or start a fresh session) before the next phase.

## Engineering discipline — distilled from ponytail + RALPH

### Code (ponytail)

Laziest solution that actually works. Stop at the first rung that holds: (1) does this need to exist? speculative = skip, say so in one line (YAGNI) → (2) stdlib does it → (3) native platform feature covers it → (4) already-installed dependency solves it (never add a new one for a few lines) → (5) one line? one line → (6) minimum code that works.

- No unrequested abstractions: no interface with one implementation, no factory for one product, no config for a constant. No boilerplate or scaffolding "for later".
- Deletion over addition. Boring over clever. Fewest files, shortest working diff.
- Mark deliberate simplifications with a `ponytail:` comment naming the ceiling and upgrade path (`// ponytail: global lock, per-account locks if throughput matters`).
- Never simplify away: input validation at trust boundaries, error handling that prevents data loss, security, accessibility basics, anything explicitly requested.
- Non-trivial logic (branch, loop, parser, security path) leaves ONE minimal runnable check. Trivial one-liners need no test — YAGNI applies to tests too.
- Lazy is opt-in via the `ponytail` skill trigger ("ponytail", "yagni", "be lazy"); these permanent rules are the floor, not a license to under-deliver explicit requests.

### Completion (RALPH)

- Never claim done without verification: run the repo's build/test/lint entry points (see `prairie-workspace` skill) and report the result.
- Fan out with bounded iterations and explicit completion criteria; one task per Agent Manager session/worktree. Never unbounded retry loops — always cap.
- Unrelated bugs found while working on a task: record them, do not fix them mid-task.
- When you learn new build/test/run commands for a repo, update `prairie-workspace`/AGENTS.md with the commands — commands only, no status prose.

## Upstream syncs (Silo → Prairie) — MANDATORY procedure

Any "sync with upstream", "update from Silo" or "get prairie-X up to date" task follows
`upstream-sync/AGENT-GUIDE.md` in this repo. Short version: never hand-merge, never merge a
rebranded copy of upstream, never resolve whole files to one side, never squash. Check first
for the daily automation's open sync PR or "Upstream sync needs conflict resolution" issue. Then
`upstream-sync/sync.sh prairie-<repo>`, resolve only the conflicts it leaves (brief + batches
provided), and `upstream-sync/finish.sh <worktree>` (commit, audit incl. compile-break checks,
push, PR). Land with a merge commit.
