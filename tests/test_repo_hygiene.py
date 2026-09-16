"""Public-repo guard: no engagement-identifying term may appear in any file in the repo (D-006)."""

from __future__ import annotations

from pathlib import Path

from banned_terms import BANNED_RE

REPO_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", ".git", "data", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
TEXT_SUFFIXES = {".md", ".py", ".toml", ".yml", ".yaml", ".json", ".sql", ".txt", ".example", ".cfg", ".ini"}
TEXT_NAMES = {"Makefile", "Dockerfile", ".gitignore", ".dockerignore"}
SKIP_FILES = {Path(__file__).resolve(), (REPO_ROOT / "tests" / "banned_terms.py").resolve()}


def _repo_text_files() -> list[Path]:
    files = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        if path.resolve() in SKIP_FILES:
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            files.append(path)
    return files


def test_scan_covers_docs() -> None:
    names = {p.name for p in _repo_text_files()}
    assert {"CLAUDE.md", "plan.md", "addendum1.md", "decisions.md"} <= names


def test_no_engagement_terms_in_repo() -> None:
    hits = []
    for path in _repo_text_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if match := BANNED_RE.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {match.group(0)!r}")
    assert not hits, "banned terms found:\n" + "\n".join(hits)
