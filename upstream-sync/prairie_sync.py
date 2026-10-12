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
  audit      after resolution: Prairie lines that vanished, and Silo tokens
             that new upstream code brought in
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
    custom = git("diff", "--name-only", "--no-renames", "--diff-filter=AM", bp, ours).stdout.decode().split()
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
    for p in git("diff", "--name-only", "--no-renames", "--diff-filter=AM", ours, res).stdout.decode().split():
        if rb.skipped(p):
            continue
        for l in added_lines(ours, res, p):
            for rx in rb.protect:
                l = rx.sub(b"", l.encode()).decode(errors="replace")
            for t in re.findall(r"[\w.\-/:]*[Ss]ilo[\w.\-/:]*|[\w.\-/:]*SILO[\w.\-/:]*", l):
                report["silo_tokens"].setdefault(t, []).append(p)
    report["silo_tokens"] = {t: sorted(set(ps))[:5] for t, ps in report["silo_tokens"].items()}
    print(json.dumps(report, indent=2))
    bad = bool(report["markers"]) or (a.strict and (report["lost_prairie_lines"] or report["silo_tokens"]))
    sys.exit(1 if bad else 0)


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
    au.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    {"calibrate": cmd_calibrate, "learn": cmd_learn, "merge": cmd_merge, "audit": cmd_audit}[a.cmd](a)


if __name__ == "__main__":
    main()
