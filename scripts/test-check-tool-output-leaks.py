#!/usr/bin/env python3
"""Temp-directory regressions for check-tool-output-leaks.py.

Gutter fixtures are generated at runtime and harness phrases come from the
checker's own fragment-built constants, so this file never contains the text
the checker hunts for.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().with_name("check-tool-output-leaks.py")
_SPEC = importlib.util.spec_from_file_location("check_tool_output_leaks", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
leaks = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(leaks)

ARROW = "→"
BOX = "│"
SEPARATORS = {"arrow": ARROW, "tab": "\t", "box": BOX, "pipe": "|"}


def gutter(first: int, count: int, separator: str = ARROW, width: int = 6) -> str:
    """Read-tool style dump: right-aligned numbers glued to a separator."""
    return "".join(
        "{:>{}}{}line {}\n".format(number, width, separator, number)
        for number in range(first, first + count)
    )


class ToolOutputLeakTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ohstory-tool-leaks-")
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, relative: str, content: str | bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def run_checker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *args],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def scan(self, content: str, name: str = "doc.md") -> subprocess.CompletedProcess[str]:
        self.write(name, content)
        return self.run_checker(name)

    def finding_lines(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        return [
            line
            for line in result.stdout.splitlines()
            if not line.startswith(("OK:", "FAIL:"))
        ]

    # -- gutters -----------------------------------------------------------

    def test_gutter_run_of_five_is_flagged_at_run_start(self) -> None:
        result = self.scan("# Title\n\n" + gutter(12, 5) + "tail\n")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = self.finding_lines(result)
        self.assertEqual(len(findings), 1, findings)
        self.assertTrue(findings[0].startswith("doc.md:3: gutter: 5 consecutive"), findings)
        self.assertIn("(12..16)", findings[0])
        self.assertIn("FAIL: 1 tool-output leak finding(s) in 1 file(s)", result.stdout)

    def test_gutter_run_of_four_is_not_flagged(self) -> None:
        result = self.scan("intro\n" + gutter(1, 4) + "outro\n" + gutter(40, 4))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.finding_lines(result), [])

    def test_non_consecutive_numbers_are_not_flagged(self) -> None:
        skipping = "".join("{:>6}{}x\n".format(n, ARROW) for n in (1, 2, 3, 5, 6, 7, 9, 10))
        stepping = "".join("{:>6}\tx\n".format(n) for n in range(10, 100, 10))
        repeating = "".join("{}|x\n".format(n) for n in (4, 4, 4, 4, 4, 4))
        descending = "".join("{}{}x\n".format(n, BOX) for n in range(9, 0, -1))
        ratios = "15{0}30 ratio\n30{0}60 ratio\n60{0}120 ratio\n".format(ARROW)
        result = self.scan(skipping + stepping + repeating + descending + ratios)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_markdown_tables_and_numbered_lists_are_not_flagged(self) -> None:
        table = "| # | name |\n| --- | --- |\n" + "".join(
            "| {} | item {} |\n".format(n, n) for n in range(1, 11)
        )
        bare_table = "no | name\n--- | ---\n" + "".join(
            "{} | item {}\n".format(n, n) for n in range(1, 11)
        )
        numbered = "".join("{}. step {}\n".format(n, n) for n in range(1, 11))
        indented = "".join("   {}) step {}\n".format(n, n) for n in range(1, 11))
        result = self.scan(table + "\n" + bare_table + "\n" + numbered + indented)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_each_gutter_separator_form_is_flagged(self) -> None:
        for label, separator in SEPARATORS.items():
            for width in (0, 6):
                with self.subTest(separator=label, width=width):
                    name = "form-{}-{}.txt".format(label, width)
                    result = self.scan("head\n" + gutter(1, 5, separator, width), name)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    findings = self.finding_lines(result)
                    self.assertEqual(len(findings), 1, findings)
                    self.assertTrue(findings[0].startswith(name + ":2: gutter:"), findings)

    def test_blank_source_lines_inside_a_dump_keep_the_run_alive(self) -> None:
        dump = "     1{0}# Title\n     2{0}\n     3{0}para\n     4{0}\n     5{0}end\n".format(ARROW)
        result = self.scan(dump)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(len(self.finding_lines(result)), 1)

    def test_separate_runs_are_reported_separately(self) -> None:
        result = self.scan(gutter(1, 6) + "plain line\n" + gutter(100, 5, "\t"))
        self.assertEqual(result.returncode, 1, result.stdout)
        findings = self.finding_lines(result)
        self.assertEqual([f.split(": gutter")[0] for f in findings], ["doc.md:1", "doc.md:8"])

    # -- harness phrases ---------------------------------------------------

    def test_each_harness_phrase_is_flagged(self) -> None:
        for index, phrase in enumerate(leaks.HARNESS_PHRASES):
            with self.subTest(phrase=phrase):
                name = "leak-{}.md".format(index)
                result = self.scan("ok\nprefix {} suffix\nok\n".format(phrase), name)
                self.assertEqual(result.returncode, 1, result.stdout)
                findings = self.finding_lines(result)
                self.assertEqual(len(findings), 1, findings)
                self.assertTrue(findings[0].startswith(name + ":2: harness: "), findings)
                self.assertIn(repr(phrase), findings[0])

    def test_allowlisted_file_is_skipped_only_for_its_phrase(self) -> None:
        self.write("docs/hooks.md", "payload field {}\n".format(leaks.TOOL_USE_ID))
        self.write(
            "docs/other.md",
            "payload field {}\n{}\n".format(leaks.TOOL_USE_ID, leaks.ANTML_INVOKE),
        )
        allow = {"docs/hooks.md": frozenset({leaks.TOOL_USE_ID})}
        stdout = io.StringIO()
        original = leaks.ALLOWLIST
        leaks.ALLOWLIST = allow
        try:
            with contextlib.redirect_stdout(stdout):
                code = leaks.main(["--root", str(self.root), str(self.root / "docs")])
        finally:
            leaks.ALLOWLIST = original
        output = stdout.getvalue()
        self.assertEqual(code, 1, output)
        self.assertNotIn("docs/hooks.md", output)
        self.assertIn("docs/other.md:1: harness:", output)
        self.assertIn("docs/other.md:2: harness:", output)

        self.write("docs/other.md", "clean\n")
        leaks.ALLOWLIST = allow
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = leaks.main(["--root", str(self.root), str(self.root / "docs")])
        finally:
            leaks.ALLOWLIST = original
        self.assertEqual(code, 0)

    def test_allowlisted_gutter(self) -> None:
        self.write("fixture.txt", gutter(1, 8))
        original = leaks.ALLOWLIST
        leaks.ALLOWLIST = {"fixture.txt": frozenset({"gutter"})}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = leaks.main(["--root", str(self.root), str(self.root / "fixture.txt")])
        finally:
            leaks.ALLOWLIST = original
        self.assertEqual(code, 0)

    def test_checker_and_this_test_do_not_flag_themselves(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(SCRIPT), str(Path(__file__).resolve())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK: no tool-output leaks in 2 text files", result.stdout)

    # -- file selection ----------------------------------------------------

    def test_clean_file_passes(self) -> None:
        result = self.scan("# Clean\n\n1. one\n2. two\n\n| a | b |\n| - | - |\n| 1 | 2 |\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "OK: no tool-output leaks in 1 text files")
        self.assertEqual(result.stderr, "")

    def test_binaries_lockfiles_and_vendored_dirs_are_skipped(self) -> None:
        dump = gutter(1, 6)
        self.write("image.png", dump)
        self.write("font.woff2", dump)
        self.write("package-lock.json", dump)
        self.write("node_modules/pkg/index.js", dump)
        self.write("dist/bundle.js", dump)
        self.write("test-results/run.txt", dump)
        self.write("blob.bin", b"\x00\x01" + dump.encode("utf-8"))
        self.write("kept.md", "fine\n")
        result = self.run_checker(".")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("in 1 text files", result.stdout)

    def test_git_mode_scans_tracked_files_only(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],
                cwd=self.root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )

        git("init", "-q")
        self.write("tracked.md", "ok\n" + gutter(3, 5))
        self.write("node_modules/dep.js", gutter(1, 9))
        git("add", "tracked.md")
        git("add", "-f", "node_modules/dep.js")
        self.write("untracked.md", gutter(1, 9))
        result = self.run_checker()
        self.assertEqual(result.returncode, 1, result.stdout)
        findings = self.finding_lines(result)
        self.assertEqual(len(findings), 1, findings)
        self.assertTrue(findings[0].startswith("tracked.md:2: gutter:"), findings)

    def test_walk_fallback_outside_git(self) -> None:
        self.write("a/b.md", gutter(1, 5, "|"))
        self.write(".git/objects/x", gutter(1, 5))
        listed = [relative for relative, _ in leaks.walk_files(self.root, self.root)]
        self.assertEqual(listed, ["a/b.md"])

    # -- exit codes --------------------------------------------------------

    def test_exit_codes(self) -> None:
        self.write("clean.md", "clean\n")
        self.write("dirty.md", gutter(1, 5))
        self.assertEqual(self.run_checker("clean.md").returncode, 0)
        self.assertEqual(self.run_checker("dirty.md").returncode, 1)
        self.assertEqual(self.run_checker("clean.md", "dirty.md").returncode, 1)
        missing = self.run_checker("does-not-exist.md")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("no such path", missing.stderr)
        self.assertEqual(self.run_checker("--no-such-flag").returncode, 2)
        bad_root = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root / "nope")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(bad_root.returncode, 2)


if __name__ == "__main__":
    unittest.main()
