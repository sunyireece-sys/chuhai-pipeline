"""
Parse a keyword markdown file produced by feeding step1.md to Claude web.

Expected format (case-insensitive section headers, leading "# " or "## "):

    # COUNTRIES
    - US
    - Germany

    # ANCHOR
    - ingredient distributor
    - bulk importer

    # MODIFIER-A
    - berry extract

    # MODIFIER-D
    - eye health supplement

Unknown sections are ignored with a warning printed to stderr.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Sections we know about. Everything else is ignored with a warning.
KNOWN_SECTIONS = {
    "COUNTRIES",
    "ANCHOR",
    "MODIFIER-A",
    "MODIFIER-B",
    "MODIFIER-C",
    "MODIFIER-D",
}

# Header patterns: "# COUNTRIES", "## MODIFIER-A", "### Anchor", etc.
_HEADER_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
# List item: "- foo", "* foo", "1. foo"
_ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+?)\s*$")


@dataclass
class KeywordPool:
    countries: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)
    modifier_a: list[str] = field(default_factory=list)
    modifier_b: list[str] = field(default_factory=list)
    modifier_c: list[str] = field(default_factory=list)
    modifier_d: list[str] = field(default_factory=list)

    def modifiers_by_dim(self) -> dict[str, list[str]]:
        return {
            "A": self.modifier_a,
            "B": self.modifier_b,
            "C": self.modifier_c,
            "D": self.modifier_d,
        }

    def total_modifiers(self) -> int:
        return sum(len(m) for m in self.modifiers_by_dim().values())


def parse_markdown(path: Path) -> KeywordPool:
    text = path.read_text(encoding="utf-8")
    sections: dict[str, list[str]] = {}
    current: str | None = None

    for line in text.splitlines():
        header_match = _HEADER_RE.match(line)
        if header_match:
            name = _normalize_section_name(header_match.group(1))
            if name in KNOWN_SECTIONS:
                current = name
                sections.setdefault(current, [])
            else:
                if name:
                    print(f"warn: ignoring unknown section '{header_match.group(1).strip()}'", file=sys.stderr)
                current = None
            continue

        if current is None:
            continue

        item_match = _ITEM_RE.match(line)
        if item_match:
            value = _strip_quotes(item_match.group(1))
            if value:
                sections[current].append(value)

    pool = KeywordPool(
        countries=sections.get("COUNTRIES", []),
        anchors=sections.get("ANCHOR", []),
        modifier_a=sections.get("MODIFIER-A", []),
        modifier_b=sections.get("MODIFIER-B", []),
        modifier_c=sections.get("MODIFIER-C", []),
        modifier_d=sections.get("MODIFIER-D", []),
    )

    _validate(pool, path)
    return pool


def _normalize_section_name(raw: str) -> str:
    # "MODIFIER A", "Modifier-A", "modifier_a" → "MODIFIER-A"
    cleaned = raw.strip().upper()
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    return cleaned


def _strip_quotes(value: str) -> str:
    value = value.strip()
    # Strip surrounding backticks, quotes, asterisks (markdown bold)
    value = re.sub(r"^[`'\"*]+|[`'\"*]+$", "", value).strip()
    return value


def _validate(pool: KeywordPool, path: Path) -> None:
    errors = []
    if not pool.countries:
        errors.append("missing COUNTRIES section (need at least one country)")
    if not pool.anchors:
        errors.append("missing ANCHOR section (need at least one anchor term)")
    if pool.total_modifiers() == 0:
        errors.append("no MODIFIER terms found (need at least one of MODIFIER-A/B/C/D)")
    if errors:
        details = "\n  - ".join(errors)
        raise ValueError(f"keyword file {path} is invalid:\n  - {details}")
