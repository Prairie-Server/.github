"""Unit tests for the audit's compile-break checks. Run: python3 -m unittest test_prairie_sync"""
import json
import os
import tempfile
import unittest

import prairie_sync as ps


class FakeCat:
    def __init__(self, files):
        self.files = files

    def get(self, oid):
        return self.files[oid].encode()


def tree(files):
    return {p: ("100644", p) for p in files}, FakeCat(files)


never = lambda p: False


class DuplicateDecls(unittest.TestCase):
    def test_go_constant_declared_in_two_files_of_a_package(self):
        t, cat = tree({"pkg/a.go": "package pkg\n\nconst (\n\tDefaultInterval = 10\n)\n",
                       "pkg/b.go": "package pkg\n\nconst DefaultInterval = 10.0\n"})
        self.assertIn("pkg (pkg): DefaultInterval", ps.duplicate_decls(cat, t, ["pkg/a.go"], never))

    def test_go_platform_variants_are_not_duplicates(self):
        t, cat = tree({"pkg/x_linux.go": "package pkg\n\nfunc statFS() {}\n",
                       "pkg/x_other.go": "//go:build !linux\n\npackage pkg\n\nfunc statFS() {}\n"})
        self.assertEqual({}, ps.duplicate_decls(cat, t, ["pkg/x_linux.go"], never))

    def test_swift_stored_property_added_by_both_sides(self):
        src = "struct R {\n    let a: String?\n    var userCode: String? = nil\n    var userCode: String? = nil\n}\n"
        t, cat = tree({"R.swift": src})
        self.assertIn("R.swift: userCode", ps.duplicate_decls(cat, t, ["R.swift"], never))

    def test_swift_conditional_compilation_and_sibling_params(self):
        swift = ("struct V {\n#if os(tvOS)\n    @State var sel = 0\n#endif\n#if os(iOS)\n"
                 "    @State var sel = 1\n#endif\n}\n")
        kt = "sealed class R {\n    data class A(\n        val id: String,\n    ) : R()\n    data class B(\n        val id: String,\n    ) : R()\n}\n"
        t, cat = tree({"V.swift": swift, "R.kt": kt})
        self.assertEqual({}, ps.duplicate_decls(cat, t, ["V.swift", "R.kt"], never))

    def test_kotlin_extension_properties_and_swift_static_twins(self):
        kt = ("class Reg {\n    private val Item.removable: Boolean\n        get() = true\n"
              "    private val Item.unqueued: Boolean\n        get() = removable\n    val id: String = \"\"\n}\n")
        swift = "struct S {\n    static let key = \"k\"\n    let key: String\n}\n"
        t, cat = tree({"Reg.kt": kt, "S.swift": swift})
        self.assertEqual({}, ps.duplicate_decls(cat, t, ["Reg.kt", "S.swift"], never))
        t, cat = tree({"D.kt": "class D {\n    val id = 1\n    val id = 2\n}\n"})
        self.assertIn("D.kt: id", ps.duplicate_decls(cat, t, ["D.kt"], never))


class DanglingSymbols(unittest.TestCase):
    def test_type_removed_upstream_but_still_used(self):
        cat = FakeCat({"old": "struct AuroraEyebrow {}\n", "new": "struct Other {}\n", "use": "let x = AuroraEyebrow()\n"})
        ours = {"Style.swift": ("100644", "old"), "V.swift": ("100644", "use")}
        res = {"Style.swift": ("100644", "new"), "V.swift": ("100644", "use")}
        self.assertIn("AuroraEyebrow", ps.dangling_symbols(cat, ours, {}, res, never))

    def test_locals_fields_and_comments_are_not_reported(self):
        ours, cat = tree({"a.go": "package a\n\nfunc helper() {}\n"})
        res = {"b.go": ("100644", "b.go")}
        cat.files["b.go"] = ("package a\n\ntype hooks struct {\n\thelper func()\n}\n\n"
                             "// helper used to exist\nfunc f(h hooks) { h.helper() }\n")
        self.assertEqual({}, ps.dangling_symbols(cat, ours, {}, res, never))

    def test_go_field_and_param_name_lists_declare_every_name(self):
        ours, cat = tree({"a.go": "package a\n\nfunc matchCode() {}\nfunc userCode() {}\nfunc limit() {}\n"})
        res = {"b.go": ("100644", "b.go")}
        cat.files["b.go"] = ("package a\n\ntype req struct {\n\tdeviceCode, userCode, matchCode string // codes\n}\n\n"
                             "func f(offset, limit int) int { return offset + limit }\n"
                             "func g(r req) string { return r.userCode + r.matchCode }\n")
        self.assertEqual({}, ps.dangling_symbols(cat, ours, {}, res, never))


class ProtectTokens(unittest.TestCase):
    def test_prefix_token_ending_in_punctuation_protects_keys(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"content": [["silo", "prairie"]], "protect_tokens": ["silo.admin.advanced.", "siloserver"]}, f)
        try:
            rb = ps.Rebrander(f.name)
        finally:
            os.remove(f.name)
        self.assertEqual(b'k = "silo.admin.advanced.playback"; siloserver; prairie.x; prairieservers',
                         rb.text(b'k = "silo.admin.advanced.playback"; siloserver; silo.x; siloservers'))


class ServerRebrand(unittest.TestCase):
    """Strings the server kept as silo (or renamed differently) must survive the rebrand."""

    rb = ps.Rebrander(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules", "server.json"))

    def check(self, upstream, want):
        self.assertEqual(want, self.rb.text(upstream.encode()).decode())

    def test_kept_identifiers(self):
        self.check('"urn:silo:request:" + id', '"urn:silo:request:" + id')
        self.check('"SUBSCRIBE silo:admin@db3 silo:transcode-move@db3"', '"SUBSCRIBE silo:admin@db3 prairie:transcode-move@db3"')
        self.check('"-NOPERM User silo has no permissions"', '"-NOPERM User silo has no permissions"')
        self.check('r.Header.Get("X-Amz-Meta-Silo-Sha256")', 'r.Header.Get("X-Amz-Meta-Silo-Sha256")')
        self.check('{"HTTPS", "Silo.Example.TEST:443"}', '{"HTTPS", "Silo.Example.TEST:443"}')

    def test_renamed_where_prairie_renamed(self):
        self.check('"apns_topic":"org.siloserver.silo"', '"apns_topic":"org.prairieserver.prairie"')
        self.check(r'{\"apns_topic\": \"org.siloserver.silo\"}', r'{\"apns_topic\": \"org.prairieserver.prairie\"}')
        self.check('"org.siloserver.silo:/auth/callback"', '"org.siloserver.prairie:/auth/callback"')
        self.check("`silo:admin` and silo:events:ws-ticket:", "`prairie:admin` and prairie:events:ws-ticket:")
        self.check("uri=https%3A%2F%2Fsilo.example.test", "uri=https%3A%2F%2Fprairie.example.test")


class VersionBump(unittest.TestCase):
    """Both sides bumped a plugin manifest's version: keep the higher one."""

    def resolve(self, o, b, t):
        return ps.resolve_hunk([o.encode()], [b.encode()], [t.encode()])

    def test_upstream_higher(self):
        self.assertEqual(([b'  "version": "0.3.2",\n'], "version-max"),
                         self.resolve('  "version": "0.3.1",\n', '  "version": "0.3.0",\n', '  "version": "0.3.2",\n'))

    def test_prairie_higher_compares_numerically(self):
        self.assertEqual(([b'  "version": "1.10.0",\n'], "version-max"),
                         self.resolve('  "version": "1.10.0",\n', '  "version": "1.8.0",\n', '  "version": "1.9.9",\n'))

    def test_v_prefix_kept(self):
        self.assertEqual(([b'VERSION = "v1.3.0"\n'], "version-max"),
                         self.resolve('VERSION = "v1.2.4"\n', 'VERSION = "v1.2.3"\n', 'VERSION = "v1.3.0"\n'))

    def test_other_text_changed_needs_a_human(self):
        self.assertEqual((None, None), self.resolve('  "version": "0.3.1", "a": 1\n', '  "version": "0.3.0", "a": 0\n',
                                                    '  "version": "0.3.2", "a": 2\n'))

    def test_dependency_pins_need_a_human(self):
        self.assertEqual((None, None), self.resolve("\tgoogle.golang.org/grpc v1.70.0\n", "\tgoogle.golang.org/grpc v1.68.0\n",
                                                    "\tgoogle.golang.org/grpc v1.71.1\n"))

    def test_line_with_two_version_tokens_needs_a_human(self):
        self.assertEqual((None, None), self.resolve('version = "1.2.4" # since 1.0.0\n', 'version = "1.2.3" # since 1.0.0\n',
                                                    'version = "1.3.0" # since 1.0.1\n'))

    def test_autoresolve_counts_it(self):
        data = (b'{\n<<<<<<< a\n  "version": "1.2.23",\n||||||| b\n  "version": "1.2.21",\n=======\n'
                b'  "version": "1.2.30",\n>>>>>>> c\n}\n')
        out, left, stats = ps.autoresolve(data)
        self.assertEqual((b'{\n  "version": "1.2.30",\n}\n', 0, {"version-max": 1}), (out, left, stats))


if __name__ == "__main__":
    unittest.main()
