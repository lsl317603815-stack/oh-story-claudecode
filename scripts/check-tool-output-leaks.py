#!/usr/bin/env python3
"""Catch agent tool output that was written back into tracked files.

A batch edit that pastes a Read/cat result back into its source leaves two
recognisable scars, and this gate looks for both in every tracked text file:

* gutter   -- a run of lines that each start with a line-number gutter such as
              the Read tool's right-aligned number plus arrow, ``cat -n``'s
              number plus TAB, or a box-drawing / pipe column. Only runs whose
              numbers climb by exactly one per line, for at least
              GUTTER_MIN_RUN lines, count, so Markdown tables, numbered lists
              and stray numbered lines never trip it.
* harness  -- transcript scaffolding (reminder tags, tool-call markup, tool
              result headers, interrupt notices) that has no business in a
              shipped file.

Default scope is ``git ls-files`` under the repository root (falling back to a
filtered tree walk when git is unavailable). Positional paths restrict the scan
to those files or directories. Exit 1 on findings, 0 when clean, 2 on usage
errors.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

GUTTER_MIN_RUN = 5
# Optional leading spaces, an ASCII integer, then the separator glued directly
# to it: Read-tool arrow, cat -n TAB, box-drawing bar, or ASCII pipe. The rest
# of the line is the original content and may be empty (blank source lines
# still carry a gutter), so it is not constrained here.
GUTTER = re.compile("^ *([0-9]{1,9})(?:\u2192|\t|\u2502|\\|)")

# Phrases are assembled from fragments so this checker and its test never
# contain the literal text they hunt for.
_LT = "<"
SYSTEM_REMINDER_OPEN = _LT + "system" + "-reminder>"
SYSTEM_REMINDER_CLOSE = _LT + "/system" + "-reminder>"
FUNCTION_CALLS_OPEN = _LT + "function" + "_calls>"
FUNCTION_RESULTS_OPEN = _LT + "function" + "_results>"
ANTML_INVOKE = "antml" + ":invoke"
ANTML_PARAMETER = "antml" + ":parameter"
CALLED_READ_TOOL = "Called the " + "Read tool"
RESULT_OF_CALLING = "Result of " + "calling the"
REQUEST_INTERRUPTED = "[Request " + "interrupted by user"
TOOL_USE_ERROR_OPEN = _LT + "tool_use" + "_error>"
TOOL_USE_ID = "tool_use" + "_id"

HARNESS_PHRASES = (
    SYSTEM_REMINDER_OPEN,
    SYSTEM_REMINDER_CLOSE,
    FUNCTION_CALLS_OPEN,
    FUNCTION_RESULTS_OPEN,
    ANTML_INVOKE,
    ANTML_PARAMETER,
    CALLED_READ_TOOL,
    RESULT_OF_CALLING,
    REQUEST_INTERRUPTED,
    TOOL_USE_ERROR_OPEN,
    TOOL_USE_ID,
)

# Per-file exemptions, keyed by POSIX path relative to --root. Each value lists
# the phrase constants (or "gutter") that file may legitimately contain. Every
# entry needs a comment saying why; refer to phrases by constant name so the
# entry itself stays clean. Example for a doc that quotes Claude Code hook JSON:
#   "docs/hooks.md": frozenset({TOOL_USE_ID}),  # documents the hook payload
# Empty on purpose: as of 0.11.1 no tracked file needs an exemption.
ALLOWLIST: dict[str, frozenset[str]] = {}

SKIP_DIRS = frozenset({
    ".git",
    "node_modules",
    "dist",
    "test-results",
    "playwright-report",
    "__pycache__",
    ".venv",
})
LOCKFILES = frozenset({
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "composer.lock",
    "Gemfile.lock",
})
BINARY_SUFFIXES = frozenset({
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".icns",
    ".tif", ".tiff", ".avif", ".heic", ".svg",
    # fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # media, archives, compiled artefacts, databases
    ".pdf", ".mp3", ".mp4", ".m4a", ".mov", ".wav", ".ogg", ".webm",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".tar", ".jar",
    ".pyc", ".pyo", ".class", ".so", ".dylib", ".dll", ".exe", ".wasm",
    ".db", ".sqlite", ".sqlite3", ".lock",
})
SNIPPET_LIMIT = 100


def should_skip(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    if name in LOCKFILES:
        return True
    return os.path.splitext(name)[1].lower() in BINARY_SUFFIXES


def tracked_files(root: Path) -> list[str] | None:
    """Root-relative POSIX paths from git, or None when git cannot answer."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    names = completed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return [name for name in names if name]


def walk_files(root: Path, start: Path) -> list[tuple[str, Path]]:
    """Filtered tree walk below start, as (display path, file path) pairs."""
    found: list[tuple[str, Path]] = []
    for current, dirs, files in os.walk(start):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(current) / name
            if not should_skip(path.relative_to(start).as_posix()):
                found.append((display_path(root, path), path))
    return found


def display_path(root: Path, path: Path) -> str:
    absolute = path if path.is_absolute() else Path.cwd() / path
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError:
        return absolute.as_posix()


def read_text(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def snippet(line: str) -> str:
    shown = line.replace("\t", "\\t").replace("\r", "\\r")
    if len(shown) > SNIPPET_LIMIT:
        shown = shown[: SNIPPET_LIMIT - 3] + "..."
    return shown


def scan_text(relative: str, text: str) -> list[str]:
    allowed = ALLOWLIST.get(relative, frozenset())
    lines = text.split("\n")
    findings: list[tuple[int, str]] = []

    if "gutter" not in allowed:
        run_start = 0
        run_length = 0
        previous = -1
        for index, line in enumerate(lines):
            match = GUTTER.match(line)
            if match is None:
                if run_length >= GUTTER_MIN_RUN:
                    findings.append(gutter_finding(lines, run_start, run_length))
                run_length = 0
                previous = -1
                continue
            number = int(match.group(1))
            if run_length and number == previous + 1:
                run_length += 1
            else:
                if run_length >= GUTTER_MIN_RUN:
                    findings.append(gutter_finding(lines, run_start, run_length))
                run_start = index
                run_length = 1
            previous = number
        if run_length >= GUTTER_MIN_RUN:
            findings.append(gutter_finding(lines, run_start, run_length))

    for phrase in HARNESS_PHRASES:
        if phrase in allowed or phrase not in text:
            continue
        for index, line in enumerate(lines):
            if phrase in line:
                findings.append(
                    (index + 1, "harness: {!r} in {}".format(phrase, snippet(line.strip())))
                )

    findings.sort(key=lambda item: item[0])
    return ["{}:{}: {}".format(relative, number, message) for number, message in findings]


def gutter_finding(lines: list[str], start: int, length: int) -> tuple[int, str]:
    first = GUTTER.match(lines[start])
    last = GUTTER.match(lines[start + length - 1])
    assert first is not None and last is not None
    return (
        start + 1,
        "gutter: {} consecutive numbered lines ({}..{}) starting {}".format(
            length, first.group(1), last.group(1), snippet(lines[start])
        ),
    )


def collect(root: Path, targets: list[Path]) -> list[tuple[str, Path]]:
    """(display path, file path) pairs to scan, already filtered by skip rules.

    Skip rules are applied relative to the listing origin (repo root or the
    walked directory) so directory names above it never matter. A file named
    explicitly is scanned unless its own name marks it as binary or a lockfile.
    """
    if not targets:
        listed = tracked_files(root)
        if listed is None:
            return walk_files(root, root)
        return [(name, root / name) for name in listed if not should_skip(name)]
    collected: list[tuple[str, Path]] = []
    for target in targets:
        if target.is_dir():
            collected.extend(walk_files(root, target))
        elif not should_skip(target.name):
            collected.append((display_path(root, target), target))
    return collected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when tool output (line-number gutters, harness text) leaked into files."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root to list and report paths against (default: this repo)",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="restrict the scan to these files or directories",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        parser.error("--root is not a directory: {}".format(args.root))
    missing = [str(path) for path in args.paths if not path.exists()]
    if missing:
        parser.error("no such path: {}".format(", ".join(missing)))

    # Snippets carry arrows, box bars and CJK text; a Windows console codec
    # must not turn a finding into a UnicodeEncodeError.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="backslashreplace")

    scanned = 0
    findings: list[str] = []
    flagged: set[str] = set()
    for relative, path in collect(root, [path.resolve() for path in args.paths]):
        text = read_text(path)
        if text is None:
            continue
        scanned += 1
        file_findings = scan_text(relative, text)
        if file_findings:
            flagged.add(relative)
            findings.extend(file_findings)

    for finding in findings:
        print(finding)
    if findings:
        print(
            "FAIL: {} tool-output leak finding(s) in {} file(s); {} text files scanned".format(
                len(findings), len(flagged), scanned
            )
        )
        return 1
    print("OK: no tool-output leaks in {} text files".format(scanned))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
