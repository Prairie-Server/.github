#!/usr/bin/env python3
"""Rebrand-aware upstream sync for the Prairie forks of Silo.

Why this exists: a plain `git merge upstream/main` conflicts on nearly every
file Prairie rebranded, because the Silo->Prairie rename is itself a diff.
Hand-resolving hundreds of those is where syncs lose Prairie code.

What it does instead:

  B  = merge-base(ours, upstream)       U = upstream tip
  B' = rebrand(B)                       U' = rebrand(U)   (synthetic commits)

  tree = 3-way merge(base=B', ours, theirs=U')

Rebrand noise cancels out (ours ~= B' wherever Prairie only renamed things),
so only real Prairie customizations can conflict with real upstream changes,
and new upstream code arrives already rebranded. The result is committed with
parents (ours, U) -- the REAL upstream commit -- so ancestry is preserved and
the next sync's merge-base moves forward.

Rules live in a JSON file per repo (Prairie-Server/.github: upstream-sync/rules/<repo>.json).

Subcommands:
  calibrate  how well the rules reproduce ours from the merge base
  merge      build B'/U', merge, and either commit (clean) or set up a
             worktree with a real in-progress merge for the conflicts
  render     PR body / conflicts issue / audit comment markdown from the JSON above
  audit      after resolution: conflict markers, Prairie lines that vanished,
             Silo tokens new upstream code brought in, and likely compile breaks
             (names still used but declared nowhere; names declared twice)
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

SILO_RE = re.compile(rb"silo", re.I)


def git(*args, input=None, check=True, cwd=None):
    p = subprocess.run(["git", *args], input=input, capture_output=True, cwd=cwd)
    if check and p.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{p.stderr.decode(errors='replace')}")
    return p


def rev(ref):
    return git("rev-parse", "--verify", ref + "^{commit}").stdout.decode().strip()


class Rebrander:
    def __init__(self, rules_path):
        with open(rules_path) as f:
            r = json.load(f)
        self.protect = [re.compile(p.encode()) for p in r.get("protect", [])]
        # "pre" runs before protection (e.g. module paths that a protect would shield).
        self.pre = [(re.compile(a.encode()), b.encode()) for a, b in r.get("pre", [])]
        self.content = [(re.compile(a.encode()), b.encode()) for a, b in r.get("content", [])]
        # Literal tokens the fork deliberately kept (from `learn`), matched as whole tokens.
        for t in r.get("protect_tokens", []):
            self.protect.append(re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(t.encode()) + rb"(?![A-Za-z0-9_])"))
        self.paths = [(re.compile(a), b) for a, b in r.get("paths", [])]
        # Paths are rebranded with the same content rules, except these globs
        # (e.g. migrations, whose file names are recorded in the database).
        self.path_keep = r.get("path_keep", [])
        # [[glob, [cmd, args...]]]: formatters run over rebranded files (the
        # fork ran gofmt after renaming, which re-sorts imports and realigns).
        self.format = r.get("format", [])
        self.skip = r.get("skip_content", [])
        # Paths where the fork's version always wins; upstream edits are ignored.
        self.ours = r.get("ours", [])
        # Path globs to drop from the rebranded upstream entirely (e.g. files
        # Prairie deleted on purpose and never wants back).
        self.drop = r.get("drop", [])
        # Generated files: on conflict take upstream's version and regenerate afterwards.
        self.regen = r.get("regen", [])

    def path(self, p):
        if not any(fnmatch.fnmatch(p, g) for g in self.path_keep):
            p = self.text(p.encode(errors="surrogateescape")).decode(errors="surrogateescape")
        for rx, rep in self.paths:
            p = rx.sub(rep, p)
        return p

    def skipped(self, p):
        return any(fnmatch.fnmatch(p, g) for g in self.skip)

    def text(self, data):
        if not SILO_RE.search(data):
            return data
        for rx, rep in self.pre:
            data = rx.sub(rep, data)
        saved = []

        def stash(m):
            saved.append(m.group(0))
            return b"\x00P%d\x00" % (len(saved) - 1)

        for rx in self.protect:
            data = rx.sub(stash, data)
        for rx, rep in self.content:
            data = rx.sub(rep, data)
        return re.sub(rb"\x00P(\d+)\x00", lambda m: saved[int(m.group(1))], data)

    def blob(self, path, data):
        if self.skipped(path) or b"\x00" in data[:8000]:
            return data
        return self.text(data)


class Cat:
    """One long-lived `git cat-file --batch`."""

    def __init__(self):
        self.p = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def get(self, oid):
        self.p.stdin.write(oid.encode() + b"\n")
        self.p.stdin.flush()
        hdr = self.p.stdout.readline().split()
        if len(hdr) < 3 or hdr[1] == b"missing":
            raise KeyError(oid)
        data = self.p.stdout.read(int(hdr[2]))
        self.p.stdout.read(1)
        return data


def ls_tree(commit):
    out = git("ls-tree", "-r", "-z", "--full-tree", commit).stdout
    for rec in out.split(b"\x00"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, typ, oid = meta.decode().split()
        yield mode, typ, oid, path.decode()


def run_formatters(rb, files):
    """files: {path: bytes}. Returns formatted copies (failures keep the input)."""
    import shutil, tempfile
    out = dict(files)
    for glob, cmd in rb.format:
        sel = [p for p in files if fnmatch.fnmatch(p, glob)]
        if not sel:
            continue
        if not shutil.which(cmd[0]):
            print(f"warning: formatter {cmd[0]} not on PATH; skipping {len(sel)} files", file=sys.stderr)
            continue
        with tempfile.TemporaryDirectory() as td:
            for i, p in enumerate(sel):
                fp = os.path.join(td, f"{i}{os.path.splitext(p)[1]}")
                with open(fp, "wb") as f:
                    f.write(out[p])
            names = [os.path.join(td, f"{i}{os.path.splitext(p)[1]}") for i, p in enumerate(sel)]
            for k in range(0, len(names), 200):
                # Per-file failures (syntax errors) leave that file untouched.
                subprocess.run(cmd + names[k:k + 200], capture_output=True)
            for p, fp in zip(sel, names):
                with open(fp, "rb") as f:
                    out[p] = f.read()
    return out


def rebranded_commit(rb, cat, src, ref, msg, parent=None, ours_tree=None):
    """Write rebrand(src) as a synthetic commit on `ref`, streamed into fast-import."""
    fi = subprocess.Popen(["git", "fast-import", "--quiet", "--force"], stdin=subprocess.PIPE)
    w = fi.stdin.write
    mark = [1]

    def blob(data):
        mark[0] += 1
        w(b"blob\nmark :%d\ndata %d\n" % (mark[0], len(data)))
        w(data)
        w(b"\n")
        return ":%d" % mark[0]

    entries, pending = [], {}
    seen = {}
    for mode, typ, oid, path in ls_tree(src):
        if any(fnmatch.fnmatch(path, g) for g in rb.drop):
            continue
        np = rb.path(path)
        if np in seen:
            print(f"warning: {seen[np]} and {path} both rebrand to {np}", file=sys.stderr)
        seen[np] = path
        if ours_tree is not None and any(fnmatch.fnmatch(np, g) for g in rb.ours):
            # Pin "ours" paths to the fork's content on both sides so they never change.
            if np in ours_tree:
                entries.append((ours_tree[np][0], ours_tree[np][1], np))
            continue
        if typ == "commit":
            entries.append((mode, oid, np))
            continue
        data = cat.get(oid)
        if mode == "120000":
            new = rb.path(data.decode(errors="surrogateescape")).encode(errors="surrogateescape")
        else:
            new = rb.blob(path, data)
        if new == data:
            entries.append((mode, oid, np))
        elif any(fnmatch.fnmatch(np, g) for g, _ in rb.format):
            pending[np] = new
            entries.append((mode, None, np))
        else:
            entries.append((mode, blob(new), np))
    formatted = run_formatters(rb, pending)
    w(b"commit %s\nmark :1\ncommitter prairie-sync <sync@prairie.invalid> 0 +0000\n" % ref.encode())
    w(b"data %d\n%s\n" % (len(msg), msg.encode()))
    if parent:
        w(b"from %s\n" % parent.encode())
    w(b"deleteall\n")
    # Inline data for formatted files (blob commands can't interleave inside a commit).
    for mode, ref_, np in entries:
        if ref_ is None:
            d = formatted[np]
            w(b"M %s inline %s\ndata %d\n" % (mode.encode(), np.encode(), len(d)))
            w(d)
            w(b"\n")
        else:
            w(b"M %s %s %s\n" % (mode.encode(), ref_.encode(), np.encode()))
    fi.stdin.close()
    if fi.wait() != 0:
        sys.exit("fast-import failed")
    return rev(ref)


def tree_map(commit):
    return {p: (m, o) for m, t, o, p in ls_tree(commit)}


def cmd_calibrate(a):
    rb = Rebrander(a.rules)
    ours, up = rev(a.ours), rev(a.upstream)
    base = git("merge-base", ours, up).stdout.decode().strip()
    cat = Cat()
    bp = rebranded_commit(rb, cat, base, "refs/prairie-sync/calib", "calib")
    diff = git("diff", "--numstat", "-z", "--no-renames", bp, ours).stdout.decode().split("\x00")
    files = []
    for rec in diff:
        if rec.count("\t") == 2:
            ad, de, p = rec.split("\t")
            files.append((p, ad, de))
    print(f"merge-base {base[:10]}: {len(files)} files differ between rebrand(base) and ours")
    if a.verbose:
        for p, ad, de in files:
            print(f"  +{ad} -{de} {p}")
    # Residual silo tokens on lines ours changed vs rebrand(base): candidates for rules/protect.
    tok = {}
    dp = subprocess.Popen(["git", "diff", "-U0", "--no-renames", bp, ours], stdout=subprocess.PIPE)
    for raw in dp.stdout:
        line = raw.decode(errors="replace")
        if line.startswith("-") and not line.startswith("---"):
            for t in re.findall(r"[\w.\-/:]*[Ss][Ii][Ll][Oo][\w.\-/:]*", line):
                tok[t] = tok.get(t, 0) + 1
    print("silo-ish tokens rebrand(base) still has where ours changed the line (top 60):")
    for t, n in sorted(tok.items(), key=lambda x: -x[1])[: a.top]:
        print(f"  {n:5d} {t}")


TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]*[Ss][Ii][Ll][Oo][A-Za-z0-9_.\-]*")


def tokens(line):
    return [t.strip(".-") for t in TOKEN_RE.findall(line) if re.search("silo", t.strip(".-"), re.I)]


def cmd_learn(a):
    """Per-token: did the fork keep or rename it? Suggest protect entries."""
    import difflib
    rb = Rebrander(a.rules)
    ours, up = rev(a.ours), rev(a.upstream)
    base = git("merge-base", ours, up).stdout.decode().strip()
    cat = Cat()
    bt, ot = tree_map(base), tree_map(ours)
    kept, conv = {}, {}
    for p, (_, oid) in bt.items():
        q = rb.path(p)
        if q not in ot or rb.skipped(p):
            continue
        a_ = cat.get(oid)
        if b"\x00" in a_[:8000] or not SILO_RE.search(a_):
            continue
        al = a_.decode(errors="replace").splitlines()
        bl = cat.get(ot[q][1]).decode(errors="replace").splitlines()
        sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            new = "\n".join(bl[j1:j2])
            for l in al[i1:i2]:
                for t in tokens(l):
                    if op == "equal" or t in new:
                        kept[t] = kept.get(t, 0) + 1
                    elif op == "replace" and re.search("prairie", new, re.I):
                        conv[t] = conv.get(t, 0) + 1
    rows = sorted(set(kept) | set(conv), key=lambda t: -(kept.get(t, 0) + conv.get(t, 0)))
    generic = {"silo", "Silo", "SILO"}
    suggest = [t for t in rows if t not in generic and kept.get(t, 0) >= 3 and kept.get(t, 0) >= 2 * conv.get(t, 0)]
    for t in rows[: a.top]:
        print(f"  kept={kept.get(t,0):5d} renamed={conv.get(t,0):5d} {'PROTECT? ' if t in suggest else '         '}{t}")
    with open(a.out, "w") as f:
        json.dump(suggest, f, indent=1)
    print(f"{len(suggest)} protect suggestions written to {a.out}")


BRAND_NORM = re.compile(rb"(?i)prairie|silo")


def norm(lines):
    """Compare modulo brand words, whitespace/line-wrapping and trailing commas."""
    d = BRAND_NORM.sub(b"X", b"".join(lines))
    d = re.sub(rb",(\s*[)\]}>])", rb"\1", d)
    return re.sub(rb"\s+", b"", d)


IMPORT_LINE = re.compile(rb'^\s*(import\b.*|"[^"]+"\s*|[A-Za-z_.]+\s+"[^"]+"\s*|\)\s*|from\s+\S+.*|\}\s*from\s+.*)$')


def resolve_hunk(o, b, t):
    """Return resolved lines, or None if this needs a human."""
    no, nb, nt = norm(o), norm(b), norm(t)
    if no == nb:          # Prairie only rebranded/reformatted here: take upstream
        return t, "theirs"
    if nt == nb:          # upstream only rebranded/reformatted here: keep Prairie
        return o, "ours"
    if no == nt:          # same change on both sides
        return o, "same"
    so, st, sb = set(map(bytes.strip, o)), set(map(bytes.strip, t)), set(map(bytes.strip, b))
    # Both sides only ADDED lines to base: if one side's additions contain the other's, take it.
    if sb <= so and sb <= st:
        if so - sb <= st - sb:
            return t, "superset"
        if st - sb <= so - sb:
            return o, "superset"
        # Disjoint pure import additions: union.
        if all(IMPORT_LINE.match(l.rstrip(b"\n")) or not l.strip() for l in o + t):
            seen, out = set(), []
            for l in o + t:
                if l.strip() not in seen or not l.strip():
                    seen.add(l.strip())
                    out.append(l)
            return out, "import-union"
    return None, None


def autoresolve(data):
    """Resolve diff3 hunks in data. Returns (new_data, remaining_hunks, stats)."""
    lines = data.splitlines(keepends=True)
    out, i, left, stats = [], 0, 0, {}
    while i < len(lines):
        if not lines[i].startswith(b"<<<<<<< "):
            out.append(lines[i]); i += 1; continue
        j = i + 1; o = []; b = []; t = []
        while j < len(lines) and not lines[j].startswith((b"||||||| ", b"=======")):
            o.append(lines[j]); j += 1
        if j < len(lines) and lines[j].startswith(b"||||||| "):
            j += 1
            while j < len(lines) and not lines[j].startswith(b"======="):
                b.append(lines[j]); j += 1
        j += 1
        while j < len(lines) and not lines[j].startswith(b">>>>>>> "):
            t.append(lines[j]); j += 1
        res, how = resolve_hunk(o, b, t)
        if res is None:
            out.extend(lines[i:j + 1]); left += 1
        else:
            out.extend(res); stats[how] = stats.get(how, 0) + 1
        i = j + 1
    return b"".join(out), left, stats


def autoresolve_tree(rb, cat, tree, conflicts, theirs, base=None, ours=None):
    """Auto-resolve hunks in conflicted files; 'regen' globs take upstream wholesale."""
    import tempfile
    tm = tree_map(tree)
    th = tree_map(theirs)
    bm = tree_map(base) if base else {}
    om = tree_map(ours) if ours else {}

    def noise_only(side, p):
        # side's version differs from base only by brand words/whitespace
        return p in bm and p in side and norm([cat.get(bm[p][1])]) == norm([cat.get(side[p][1])])
    idx = tempfile.mktemp(prefix="prairie-sync-idx-")
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    subprocess.run(["git", "read-tree", tree], env=env, check=True)
    left, stats = [], {"files_fully_resolved": 0, "regen_taken_from_upstream": []}
    for p in conflicts:
        # modify/delete: if the modifying side only rebranded, honour the delete.
        if p in bm and (p not in th) != (p not in om):
            mod = om if p in om else th
            noise = noise_only(mod, p)
            if noise or p not in th:
                # The delete wins: Prairie only rebranded it, or upstream removed/moved
                # the file (Prairie's edits are listed for review in the PR).
                if p in tm:
                    subprocess.run(["git", "update-index", "--force-remove", p], env=env, check=True)
                if noise:
                    stats["delete_wins_over_noise"] = stats.get("delete_wins_over_noise", 0) + 1
                else:
                    ns = git("diff", "--numstat", base, ours, "--", p).stdout.decode().split()
                    stats.setdefault("upstream_deleted_prairie_modified", []).append(f"+{ns[0]} -{ns[1]} {p}" if ns else p)
                continue
        if p not in tm:
            left.append(p); continue
        if any(fnmatch.fnmatch(p, g) for g in rb.regen) and p in th:
            m, o = th[p]
            subprocess.run(["git", "update-index", "--cacheinfo", f"{m},{o},{p}"], env=env, check=True)
            stats["regen_taken_from_upstream"].append(p); continue
        data = cat.get(tm[p][1])
        if b"<<<<<<< " not in data:
            left.append(p); continue   # modify/delete, add/add without markers, etc.
        new, n, st = autoresolve(data)
        for k, v in st.items():
            stats[k] = stats.get(k, 0) + v
        if new != data:
            oid = git("hash-object", "-w", "--stdin", input=new).stdout.decode().strip()
            subprocess.run(["git", "update-index", "--cacheinfo", f"{tm[p][0]},{oid},{p}"], env=env, check=True)
        if n:
            left.append(p)
        else:
            stats["files_fully_resolved"] += 1
    newtree = subprocess.run(["git", "write-tree"], env=env, capture_output=True, check=True).stdout.decode().strip()
    os.unlink(idx)
    return newtree, left, stats


def cmd_merge(a):
    rb = Rebrander(a.rules)
    ours, up = rev(a.ours), rev(a.upstream)
    if git("merge-base", "--is-ancestor", up, ours, check=False).returncode == 0:
        print(json.dumps({"status": "up-to-date"}))
        return
    base = git("merge-base", ours, up).stdout.decode().strip()
    cat = Cat()
    ot = tree_map(ours)
    bp = rebranded_commit(rb, cat, base, "refs/prairie-sync/base", f"rebrand({base})", ours_tree=ot)
    upp = rebranded_commit(rb, cat, up, "refs/prairie-sync/upstream", f"rebrand({up})", parent=bp, ours_tree=ot)
    p = git("-c", "merge.conflictStyle=diff3", "merge-tree", "--write-tree", "--name-only", "--no-messages",
            f"--merge-base={bp}", ours, upp, check=False)
    if p.returncode not in (0, 1):
        sys.exit(p.stderr.decode())
    lines = p.stdout.decode().splitlines()
    tree, conflicts = lines[0], sorted({l for l in lines[1:] if l})
    tree, conflicts, auto = autoresolve_tree(rb, cat, tree, conflicts, upp, bp, ours)
    n = git("rev-list", "--count", f"{base}..{up}").stdout.decode().strip()
    msg = (f"Merge upstream {a.upstream_name} {up[:10]} into {a.ours_name}\n\n"
           f"{n} upstream commits since {base[:10]}. Rebrand-aware merge by\n"
           f"Prairie-Server/.github upstream-sync/prairie_sync.py (base=rebrand(merge-base), theirs=rebrand(upstream)).\n"
           + "".join(f"\n{t}" for t in a.trailer))
    summary = {"autoresolved": auto, "base": base, "ours": ours, "upstream": up, "upstream_commits": int(n),
               "rebranded_base": bp, "rebranded_upstream": upp, "tree": tree, "conflicts": conflicts}
    if not conflicts:
        c = git("commit-tree", tree, "-p", ours, "-p", up, input=msg.encode()).stdout.decode().strip()
        git("update-ref", f"refs/heads/{a.branch}", c)
        summary.update(status="clean", commit=c, branch=a.branch)
    else:
        summary["status"] = "conflicts"
        if a.worktree:
            wt = a.worktree
            git("worktree", "add", "-f", "-B", a.branch, wt, ours)
            # Real in-progress merge: index/worktree = merged tree (with markers),
            # MERGE_HEAD = real upstream, so a plain `git commit` records both parents.
            git("read-tree", "--reset", "-u", tree, cwd=wt)
            gd = git("rev-parse", "--git-dir", cwd=wt).stdout.decode().strip()
            gd = gd if os.path.isabs(gd) else os.path.join(wt, gd)
            with open(os.path.join(gd, "MERGE_HEAD"), "w") as f:
                f.write(up + "\n")
            with open(os.path.join(gd, "MERGE_MSG"), "w") as f:
                f.write(msg + "\nConflicts resolved by hand:\n" + "".join(f"\t{c}\n" for c in conflicts))
            summary["worktree"] = wt
    print(json.dumps(summary, indent=2))


# --- symbol checks (catch compile breaks without a compiler) -------------------------------
CODE_EXT = (".go", ".swift", ".kt", ".kts", ".ts", ".tsx")
DECL_RE = {
    ".go": [re.compile(r"^func (?:\([^)]*\) )?([A-Za-z_]\w*)", re.M),
            re.compile(r"^type ([A-Za-z_]\w*)", re.M),
            re.compile(r"^(?:const|var) ([A-Za-z_]\w*)", re.M)],
    ".swift": [re.compile(r"\b(?:class|struct|enum|protocol|actor|typealias)\s+([A-Za-z_]\w*)"),
               re.compile(r"\bfunc\s+([A-Za-z_]\w*)"),
               re.compile(r"^\s*case\s+([a-z_]\w*)\b(?!\s*(?:=|let|var))", re.M),
               re.compile(r"^ {0,8}(?:(?:public|private|internal|fileprivate|static|nonisolated|lazy|weak|override|final|@\w+(?:\([^)]*\))?)\s+)*(?:let|var)\s+([A-Za-z_]\w*)", re.M)],
    ".kt": [re.compile(r"\b(?:class|interface|object|typealias)\s+([A-Za-z_]\w*)"),
            re.compile(r"\bfun\s+(?:<[^>]*>\s*)?(?:[\w.<>?, ]+\.)?([A-Za-z_]\w*)\s*\("),
            re.compile(r"^ {0,8}(?:(?:public|private|internal|protected|override|const|lateinit|open|@\w+)\s+)*(?:val|var)\s+([A-Za-z_]\w*)", re.M)],
    ".ts": [re.compile(r"\bexport\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:function\*?|class|interface|type|const|let|enum)\s+([A-Za-z_$][\w$]*)")],
}
DECL_RE[".kts"] = DECL_RE[".kt"]
DECL_RE[".tsx"] = DECL_RE[".ts"]
# Patterns for names that are visible beyond a function body: types and functions (and Go
# top-level names). Stored properties and enum cases are left out: their regexes also match
# locals, which would drown the report.
SYMBOL_RE = {".go": DECL_RE[".go"], ".swift": DECL_RE[".swift"][:2], ".kt": DECL_RE[".kt"][:2],
             ".kts": DECL_RE[".kt"][:2], ".ts": DECL_RE[".ts"], ".tsx": DECL_RE[".ts"]}
# "Is this name declared anywhere?" -- deliberately loose: inline constructor params, locals, labels.
LOOSE_RE = {e: rs + [re.compile(r"\b(?:let|var|val|const)\s+([A-Za-z_]\w*)"),
                     re.compile(r"\bcase\s+([A-Za-z_]\w*)"),
                     re.compile(r"\b(?:function|fun|func)\s+([A-Za-z_]\w*)"),
                     # params, named args, struct/interface/object fields: `name: Type`
                     re.compile(r"\b([A-Za-z_]\w*)\??\s*:\s*[A-Za-z_(\[{@'\"]")]
            for e, rs in DECL_RE.items()}
for _e in (".ts", ".tsx"):   # TS method definitions `name(args) {` / `name(args): T` (not calls)
    LOOSE_RE[_e] = LOOSE_RE[_e] + [re.compile(r"^\s+(?:async\s+)?([A-Za-z_]\w*)\??\s*\([^)]*\)\s*[:{]", re.M)]
LOOSE_RE[".go"] = LOOSE_RE[".go"] + [
    re.compile(r"(?:^|[\s(,])([A-Za-z_]\w*)(?:\s*,\s*[A-Za-z_]\w*)*\s*:=", re.M),        # x := / x, y :=
    re.compile(r",\s*([A-Za-z_]\w*)(?:\s*,\s*[A-Za-z_]\w*)*\s*:="),
    re.compile(r"[(,]\s*([A-Za-z_]\w*)\s+(?:\.\.\.)?[*\[\]\w.]+\s*[,)]"),             # params
    re.compile(r"^\s+([A-Za-z_]\w*)\s+[*\[\]\w.{}]+(?:\s+`[^`]*`)?\s*(?://.*)?$", re.M),  # struct fields
    re.compile(r"\bfor\s+([A-Za-z_]\w*)(?:\s*,\s*[A-Za-z_]\w*)?\s*:?=\s*range"),
    re.compile(r"^\s+([A-Za-z_]\w*):\s*$", re.M),                                       # labels
    re.compile(r"^\s+([A-Za-z_]\w*)\s+(?:func|chan|map|interface|struct)\b", re.M),        # func-typed fields
    re.compile(r"\b(?:const|var)\s+[\w\s,]*?,\s*([A-Za-z_]\w*)\s*(?:=|[A-Za-z*\[])"),     # const a, b =
    re.compile(r"^\s+([A-Za-z_]\w*)\s*(?:[\w.*\[\]]+\s*)?=[^=]", re.M)]                  # var ( x = ... ) in funcs
WORD_RE = re.compile(r"[A-Za-z_]\w{3,}")
STRIP_RE = re.compile(r"//.*|\"(?:\\.|[^\"\\])*\"|`[^`]*`")
KEYWORDS = {"mutating", "nonmutating", "override", "async", "await", "throws", "rethrows", "inout",
            "some", "self", "Self", "super", "init", "deinit", "where", "suspend", "inline", "operator",
            "infix", "data", "sealed", "open", "internal", "public", "private", "protected", "readonly",
            "static", "final", "lazy", "weak", "unowned", "convenience", "required", "dynamic", "func", "this"}
COMMENT_RE = re.compile(r"^\s*(?://|\*|/\*|#)")


def code_files(tm, skip):
    return {p: o for p, (m, o) in tm.items() if p.endswith(CODE_EXT) and m != "160000" and not skip(p)}


def declared(cat, files, table=None):
    table = table or SYMBOL_RE
    names = set()
    for p, o in files.items():
        txt = cat.get(o).decode(errors="replace")
        for rx in table[os.path.splitext(p)[1]]:
            names.update(rx.findall(txt))
        if p.endswith(".go"):
            for blk in GO_BLOCK_RE.findall(txt):
                names.update(re.findall(r"^\t([A-Za-z_]\w*)\b", blk, re.M))
    return names


def dangling_symbols(cat, ours_t, up_t, res_t, skip, limit=60):
    """Names declared at ours or upstream, declared nowhere in the result, still referenced
    in the result's code. Each is a likely compile error: upstream renamed/removed something
    Prairie code still calls, or a resolution dropped a definition."""
    rf = code_files(res_t, skip)
    # "Declared anywhere in the result" uses the loose table (members, cases, locals too), so a
    # name only counts as gone when nothing at all declares it any more.
    gone = (declared(cat, code_files(ours_t, skip)) | declared(cat, code_files(up_t, skip))) - declared(cat, rf, LOOSE_RE)
    gone = {n for n in gone if len(n) >= 4 and n not in KEYWORDS}
    hits = {}
    for p, o in rf.items():
        for i, line in enumerate(cat.get(o).decode(errors="replace").splitlines(), 1):
            if COMMENT_RE.match(line):
                continue
            for w in set(WORD_RE.findall(STRIP_RE.sub("", line))) & gone:
                hits.setdefault(w, []).append(f"{p}:{i}")
    return {n: v[:5] for n, v in sorted(hits.items())[:limit]}


GO_PKG_RE = re.compile(r"^package (\w+)", re.M)
GO_TOP_RE = [re.compile(r"^func ([A-Za-z_]\w*)", re.M), re.compile(r"^type ([A-Za-z_]\w*)", re.M),
             re.compile(r"^(?:const|var) ([A-Za-z_]\w*)", re.M)]
GO_BLOCK_RE = re.compile(r"^(?:const|var) \((.*?)^\)", re.M | re.S)
GO_CONSTRAINED_RE = re.compile(r"_(?:linux|darwin|windows|freebsd|openbsd|netbsd|unix|other|fallback|"
                               r"amd64|arm64|386|arm|js|wasm|android|ios|plan9|solaris)(?:_test)?\.go$")
MEMBER_RE = {".swift": DECL_RE[".swift"][3], ".kt": DECL_RE[".kt"][2], ".kts": DECL_RE[".kt"][2]}


def duplicate_decls(cat, res_t, changed, skip):
    """Same name declared twice where the language forbids it: Go top-level names within a
    package, and Swift/Kotlin stored properties within one type body. Both sides of a merge
    adding the same thing is the usual cause. Only packages/files the sync touched."""
    out = {}
    go_dirs = {os.path.dirname(p) for p in changed if p.endswith(".go")}
    pkgs = {}
    for p, o in code_files(res_t, skip).items():
        if p.endswith(".go") and os.path.dirname(p) in go_dirs and not p.endswith("_gen.go"):
            txt = cat.get(o).decode(errors="replace")
            m = GO_PKG_RE.search(txt)
            if not m:
                continue
            if GO_CONSTRAINED_RE.search(p) or re.search(r"^//go:build ", txt, re.M):
                continue   # per-platform variants legitimately repeat names
            names = [n for rx in GO_TOP_RE for n in rx.findall(txt)]
            for blk in GO_BLOCK_RE.findall(txt):
                names += re.findall(r"^\t([A-Za-z_]\w*)\b", blk, re.M)
            for n in names:
                if n not in ("init", "_", "main"):
                    pkgs.setdefault((os.path.dirname(p), m.group(1), n), []).append(p)
    for (d, pkg, n), ps in pkgs.items():
        if len(ps) > 1:
            out[f"{d} ({pkg}): {n}"] = sorted(ps)
    for p in changed:
        ext = os.path.splitext(p)[1]
        if ext not in MEMBER_RE or p not in res_t or skip(p):
            continue
        depth, parens, seen = 0, 0, {}
        for i, line in enumerate(cat.get(res_t[p][1]).decode(errors="replace").splitlines(), 1):
            code = STRIP_RE.sub("", line)
            in_params = parens > 0   # constructor/function parameters, not members
            if re.match(r"\s*#(?:if|else|elseif)\b", line):
                seen = {}   # Swift conditional compilation: each branch may declare the same name
            m = MEMBER_RE[ext].match(line)
            if m and depth == 1 and not in_params and not COMMENT_RE.match(line) and not re.search(r"\b(?:class|object|interface|struct)\b.*\(", line):
                n = m.group(1)
                if n in seen:
                    out[f"{p}: {n}"] = [f"{p}:{seen[n]}", f"{p}:{i}"]
                seen[n] = i
            depth += code.count("{") - code.count("}")
            parens = max(0, parens + code.count("(") - code.count(")"))
            if depth <= 0:
                depth, seen = 0, {}
    return out


def added_lines(a, b, path):
    """Non-trivial lines added going a->b in path."""
    out = git("diff", "-U0", "--no-renames", a, b, "--", path, check=False).stdout.decode(errors="replace")
    res = []
    for l in out.splitlines():
        if l.startswith("+") and not l.startswith("+++"):
            s = l[1:].strip()
            if len(s) >= 6 and not re.fullmatch(r"[\W_]+", s):
                res.append(s)
    return res


def cmd_audit(a):
    rb = Rebrander(a.rules)
    ours, up, res = rev(a.ours), rev(a.upstream), rev(a.result)
    base = git("merge-base", ours, up).stdout.decode().strip()
    cat = Cat()
    ot = tree_map(ours)
    bp = rebranded_commit(rb, cat, base, "refs/prairie-sync/base", f"rebrand({base})", ours_tree=ot)
    upp = rebranded_commit(rb, cat, up, "refs/prairie-sync/upstream", f"rebrand({up})", parent=bp, ours_tree=ot)
    rt = tree_map(res)
    report = {"lost_prairie_lines": {}, "markers": [], "silo_tokens": {}}
    # 1. Prairie customizations (ours vs rebrand(base)) that are gone from the result
    #    and that upstream did not itself remove.
    custom = [x for x in git("diff", "-z", "--name-only", "--no-renames", "--diff-filter=AM", bp, ours).stdout.decode().split("\0") if x]
    for p in custom:
        if any(fnmatch.fnmatch(p, g) for g in rb.regen):
            continue   # generated; regenerated rather than merged
        if p not in rt:
            report["lost_prairie_lines"][p] = ["<file deleted>"]
            continue
        result_txt = cat.get(rt[p][1]).decode(errors="replace")
        key = lambda l: norm([l.encode()]).lower()
        result_lines = {key(l) for l in result_txt.splitlines()}
        lost = [l for l in added_lines(bp, ours, p) if key(l) not in result_lines]
        if lost:
            report["lost_prairie_lines"][p] = lost[: a.max_lines]
    # 2. Conflict markers left behind.
    g = git("grep", "-l", "-E", "^(<<<<<<<|>>>>>>>) ", res, check=False).stdout.decode().split()
    report["markers"] = [x.split(":", 1)[1] for x in g]
    # 3. Silo tokens on lines the sync added (rules gaps).
    for p in [x for x in git("diff", "-z", "--name-only", "--no-renames", "--diff-filter=AM", ours, res).stdout.decode().split("\0") if x]:
        if rb.skipped(p):
            continue
        for l in added_lines(ours, res, p):
            for rx in rb.protect:
                l = rx.sub(b"", l.encode()).decode(errors="replace")
            for t in re.findall(r"[\w.\-/:]*[Ss]ilo[\w.\-/:]*|[\w.\-/:]*SILO[\w.\-/:]*", l):
                report["silo_tokens"].setdefault(t, []).append(p)
    report["silo_tokens"] = {t: sorted(set(ps))[:5] for t, ps in report["silo_tokens"].items()}
    # 4./5. Compile breaks a grep can see: names that no longer exist but are still used,
    #       and names now declared twice.
    skip = lambda p: rb.skipped(p) or any(fnmatch.fnmatch(p, g) for g in rb.regen)
    if not a.no_symbols:
        report["dangling_symbols"] = dangling_symbols(cat, ot, tree_map(upp), rt, skip)
        changed = [x for x in git("diff", "-z", "--name-only", "--no-renames", "--diff-filter=AM", ours, res).stdout.decode().split("\0") if x]
        report["duplicate_decls"] = duplicate_decls(cat, rt, changed, skip)
    print(json.dumps(report, indent=2))
    bad = bool(report["markers"]) or (a.strict and any(report.get(k) for k in
          ("lost_prairie_lines", "silo_tokens", "dangling_symbols", "duplicate_decls")))
    sys.exit(1 if bad else 0)


AUDIT_MARK = "<!-- prairie-sync-audit -->"


def cmd_render(a):
    """Markdown for a PR body / issue body / audit comment, from merge or audit JSON."""
    d = json.load(open(a.json))
    up, repo = a.upstream_repo, a.repo
    out = []
    w = out.append
    lst = lambda xs: "".join(f"\n- `{x}`" for x in xs)
    if a.kind in ("pr", "issue"):
        au = d.get("autoresolved", {})
        stats = (f"{au.get('files_fully_resolved', 0)} files auto-resolved (hunks: theirs {au.get('theirs', 0)}, "
                 f"ours {au.get('ours', 0)}, same {au.get('same', 0)}, superset {au.get('superset', 0)}, "
                 f"import-union {au.get('import-union', 0)}; delete-over-noise {au.get('delete_wins_over_noise', 0)})")
    if a.kind == "pr":
        w(f"Rebrand-aware merge of `{up}` at `{d['upstream'][:10]}` ({d['upstream_commits']} upstream commits).\n")
        w("**Land with \"Create a merge commit\". Never squash.** The second parent is the real upstream commit; "
          "that is what keeps the next sync small.\n")
        w(stats + ".")
        if au.get("regen_taken_from_upstream"):
            w("\n**Generated files taken from upstream** (the regen job rebuilds them on this branch):" + lst(au["regen_taken_from_upstream"]))
        if au.get("upstream_deleted_prairie_modified"):
            w("\n**Upstream deleted files Prairie had modified.** The delete was applied. Check that Prairie's change "
              "isn't a feature that needs re-homing:" + lst(au["upstream_deleted_prairie_modified"]))
        w("\nThe audit comment below lists likely compile breaks and lost Prairie lines. CI must be green, including "
          "`Prairie invariants`; a failing test after a sync usually means Prairie code was lost, not that the test is stale.")
    elif a.kind == "issue":
        name = repo.split("/")[1]
        w(f"Upstream `{up}` is {d['upstream_commits']} commits ahead (`{d['upstream'][:10]}`). {stats}, "
          f"but **{len(d['conflicts'])} files still have real conflicts**.\n")
        w("## Finish it\n")
        w("```sh")
        w("git clone --depth 1 https://github.com/Prairie-Server/.github prairie-dotgithub")
        w(f"prairie-dotgithub/upstream-sync/sync.sh {name}      # clones/fetches, merges, leaves a worktree")
        w(f"# resolve the files below in the worktree, then:")
        w(f"prairie-dotgithub/upstream-sync/finish.sh           # commit, audit, push, open the PR")
        w("```\n")
        w("Follow `upstream-sync/AGENT-GUIDE.md` in Prairie-Server/.github. `sync.sh` also writes a resolution brief "
          "and file batches for agents.\n")
        w(f"## Conflicted files ({len(d['conflicts'])})\n" + lst(d["conflicts"]))
        if au.get("upstream_deleted_prairie_modified"):
            w("\n## Upstream deleted files Prairie had modified\n\nThe delete was applied; check for features to re-home." + lst(au["upstream_deleted_prairie_modified"]))
    elif a.kind == "audit":
        w(AUDIT_MARK)
        w("## Upstream sync audit\n")
        mk, dg, dp = d.get("markers", []), d.get("dangling_symbols", {}), d.get("duplicate_decls", {})
        lost, toks = d.get("lost_prairie_lines", {}), d.get("silo_tokens", {})
        w("| check | count |\n|---|---|")
        w(f"| conflict markers | {'❌ ' if mk else ''}{len(mk)} |")
        w(f"| likely compile breaks: names used but declared nowhere | {'⚠️ ' if dg else ''}{len(dg)} |")
        w(f"| likely compile breaks: names declared twice | {'⚠️ ' if dp else ''}{len(dp)} |")
        w(f"| files with Prairie lines no longer present | {len(lost)} |")
        w(f"| Silo tokens added by the sync | {len(toks)} |\n")
        if mk:
            w("**Conflict markers**" + lst(mk))
        if dg:
            w("\n**Names still used but declared nowhere.** Upstream renamed/removed them, or a resolution dropped the "
              "definition. Port the caller to upstream's replacement:\n")
            for n, locs in dg.items():
                w(f"- `{n}`: " + ", ".join(f"`{x}`" for x in locs[:3]))
        if dp:
            w("\n**Declared twice.** Usually both sides added the same thing; keep one:\n")
            for n, locs in dp.items():
                w(f"- `{n}`: " + ", ".join(f"`{x}`" for x in locs[:3]))
        if lost:
            w("\n**Prairie lines no longer present.** Each is either intended (upstream rewrote that code) or a lost "
              "Prairie change:\n")
            for p_, ls in list(lost.items())[:80]:
                w(f"<details><summary><code>{p_}</code> ({len(ls)})</summary>\n\n```\n" + "\n".join(ls) + "\n```\n</details>")
        if toks:
            w("\n**Silo tokens added by this sync.** Rebrand them, or add them to the rules' `protect_tokens` if intentional:\n")
            for t, ps in list(toks.items())[:100]:
                w(f"- `{t}`: " + ", ".join(f"`{x}`" for x in ps))
    body = "\n".join(out)
    if len(body) > 60000:   # GitHub caps bodies at 65536 chars
        body = body[:60000] + "\n\n…truncated; run `audit` locally for the full report."
    print(body)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules", default=".github/prairie-sync/rules.json")
    ap.add_argument("--ours", default="origin/main")
    ap.add_argument("--upstream", default="upstream/main")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("-v", "--verbose", action="store_true")
    c.add_argument("--top", type=int, default=60)
    le = sub.add_parser("learn")
    le.add_argument("--top", type=int, default=150)
    le.add_argument("--out", default="protect-suggestions.json")
    m = sub.add_parser("merge")
    m.add_argument("--branch", required=True)
    m.add_argument("--worktree")
    m.add_argument("--upstream-name", default="Silo")
    m.add_argument("--ours-name", default="main")
    m.add_argument("--trailer", action="append", default=[], help="trailer line(s) for the merge commit")
    au = sub.add_parser("audit")
    au.add_argument("--result", default="HEAD")
    au.add_argument("--max-lines", type=int, default=20)
    au.add_argument("--strict", action="store_true", help="also fail on lost lines, Silo tokens, dangling or duplicate symbols")
    au.add_argument("--no-symbols", action="store_true", help="skip the dangling/duplicate symbol checks")
    r = sub.add_parser("render", help="markdown for a PR body, conflicts issue or audit comment")
    r.add_argument("kind", choices=["pr", "issue", "audit"])
    r.add_argument("json", help="merge or audit JSON output")
    r.add_argument("--repo", default="Prairie-Server/<repo>")
    r.add_argument("--upstream-repo", default="Silo-Server/<repo>")
    a = ap.parse_args()
    {"calibrate": cmd_calibrate, "learn": cmd_learn, "merge": cmd_merge, "audit": cmd_audit,
     "render": cmd_render}[a.cmd](a)


if __name__ == "__main__":
    main()
