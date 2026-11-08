#!/usr/bin/env python3
"""Validate PACT's documentation.

Checks, all offline (no network):

1. Every JSON file under ``metadata/`` parses.
2. Every relative Markdown link in the tracked documentation resolves to a file
   that exists in the repository (anchors are stripped; absolute URLs are skipped).
3. The required root documentation files are present.
4. The required ``docs/`` files are present.

Run locally:  python scripts/check_docs.py
Exit code 0 on success, 1 on any failure.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Files that must exist at the repository root.
REQUIRED_ROOT = [
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "llms.txt",
    "llms-full.txt",
    "LICENSE",
]

# Files that must exist under docs/.
REQUIRED_DOCS = [
    "docs/README.md",
    "docs/knowledge-map.md",
    "docs/overview.md",
    "docs/architecture.md",
    "docs/payment-lifecycle.md",
    "docs/database.md",
    "docs/security.md",
    "docs/ai-system.md",
    "docs/api/README.md",
    "docs/deployment.md",
    "docs/beta-operations.md",
    "docs/beta-launch-checklist.md",
    "docs/beta-invitation.md",
    "docs/development.md",
    "docs/faq.md",
    "docs/glossary.md",
    "docs/implementation-status.md",
    "docs/devpost-submission-copy.md",
    "docs/evidence/README.md",
    "docs/architecture-decisions/README.md",
]

# Machine-readable metadata files.
REQUIRED_METADATA = [
    "metadata/project.json",
    "metadata/features.json",
    "metadata/api.json",
    "metadata/architecture.json",
    "metadata/integrations.json",
    "metadata/security-controls.json",
    "metadata/evidence.json",
]

# Markdown files that are scanned for links. Anything else (e.g. docs/demo/**)
# is left alone to keep the check fast and focused on the canonical set.
def markdown_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.md"):
        rel = path.relative_to(ROOT)
        if any(part in {".git", "node_modules", ".venv", "dist"} for part in rel.parts):
            continue
        files.append(path)
    return sorted(files)


LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
FENCE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`[^`]*`")


def strip_code(text: str) -> str:
    """Remove fenced and inline code so a link-looking example is not checked."""
    text = FENCE.sub("", text)
    return INLINE_CODE.sub("", text)


def check_json(errors: list[str]) -> None:
    for rel in REQUIRED_METADATA:
        path = ROOT / rel
        if not path.exists():
            errors.append(f"missing metadata file: {rel}")
            continue
        try:
            json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON in {rel}: {exc}")


def check_required(errors: list[str]) -> None:
    for rel in REQUIRED_ROOT + REQUIRED_DOCS:
        if not (ROOT / rel).exists():
            errors.append(f"missing required file: {rel}")


def check_links(errors: list[str]) -> None:
    for path in markdown_files():
        text = strip_code(path.read_text())
        for target in LINK.findall(text):
            target = target.strip()
            if not target or target.startswith(("#", "http://", "https://", "mailto:", "tel:")):
                continue
            # Drop an anchor and any surrounding angle brackets.
            target = target.split("#", 1)[0].strip("<>").strip()
            if not target:
                continue
            # A same-document anchor with no path is handled above.
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                rel = path.relative_to(ROOT)
                errors.append(f"broken link in {rel}: {target}")


def check_structure(errors: list[str]) -> None:
    """Lightweight Markdown structure checks (no external dependency).

    - each file has exactly one level-1 heading;
    - code fences are balanced (an even number of ``` lines);
    - the file ends with a newline.
    """
    for path in markdown_files():
        rel = path.relative_to(ROOT)
        raw = path.read_text()
        if not raw.endswith("\n"):
            errors.append(f"{rel}: file does not end with a newline")
        lines = raw.splitlines()
        fences = [ln for ln in lines if ln.lstrip().startswith("```")]
        if len(fences) % 2 != 0:
            errors.append(f"{rel}: unbalanced code fence ({len(fences)} fence lines)")
        # Count H1 headings outside fenced code (a bash comment starts with "# ").
        in_fence = False
        h1 = []
        for ln in lines:
            if ln.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence and ln.startswith("# "):
                h1.append(ln)
        if len(h1) != 1:
            errors.append(f"{rel}: expected exactly one level-1 heading, found {len(h1)}")


def main() -> int:
    errors: list[str] = []
    check_json(errors)
    check_required(errors)
    check_links(errors)
    check_structure(errors)

    if errors:
        print(f"Documentation check failed with {len(errors)} problem(s):\n")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(
        f"Documentation check passed: "
        f"{len(REQUIRED_METADATA)} metadata files, "
        f"{len(REQUIRED_ROOT) + len(REQUIRED_DOCS)} required files, "
        f"{len(markdown_files())} Markdown files scanned."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
