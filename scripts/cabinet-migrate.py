#!/usr/bin/env python3
"""Convert an unzipped Notion "Markdown & CSV" export into a cabinet.

The export names every file "Title <32-hex page id>.md" and links pages by that
id. The id map is harvested BEFORE anything is renamed, then every internal link
is rewritten against it — skip that and the link graph is gone, which is the one
thing the export cannot rebuild.

What this does, in order:

  1. Walk the export and harvest {page id -> file} and {old path -> new path}.
  2. Slugify file and directory names, resolving collisions.
  3. Parse Notion's property block (the "Key: value" lines under the H1) into
     front-matter, and drop the H1 (SCHEMA.md keeps the title in front-matter).
  4. Rewrite relative links and notion.so URLs against the maps. An internal
     link with no mapping becomes plain text, because a surviving notion.so URL
     fails V9 forever; every one is named in the report.
  5. Write the cabinet and report what happened.

Attachments move to an `_assets/` folder beside the page that references them.
Database `.csv` files are not documents and are skipped — every row is already
its own `.md` in the folder beside them.

Usage: cabinet-migrate.py --export DIR --out DIR [--owner NAME] [--report FILE]
Exit codes: 0 migrated, 1 the output failed validation, 2 bad usage.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "scripts" / "cabinet-validate.py"

# Notion suffixes every exported name with the page id: 32 hex, sometimes
# preceded by a space, sometimes dash-grouped in a URL.
HEX32 = r"[0-9a-f]{32}"
NAME_ID_RE = re.compile(rf"^(?P<name>.*?)[ _-]+(?P<id>{HEX32})$")
URL_ID_RE = re.compile(rf"({HEX32})")
DASHED_UUID_RE = re.compile(
    r"([0-9a-f]{8})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{12})"
)

INLINE_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^\s*(```|~~~)")

ASSET_DIR = "_assets"

STATUS_MAP = {
    "done": "done",
    "complete": "done",
    "completed": "done",
    "shipped": "done",
    "closed": "done",
    "in progress": "active",
    "in-progress": "active",
    "doing": "active",
    "active": "active",
    "current": "active",
    "blocked": "blocked",
    "on hold": "blocked",
    "paused": "blocked",
    "waiting": "blocked",
    "archived": "archived",
    "archive": "archived",
    "not started": "draft",
    "backlog": "draft",
    "idea": "draft",
    "draft": "draft",
}

TYPE_PREFIX = {"project": "prj", "doc": "doc", "decision": "dec", "note": "note"}

# Notion property names, lowercased, that carry each piece of front-matter.
STATUS_KEYS = ("status", "state")
OWNER_KEYS = ("owner", "assignee", "person", "responsible", "lead")
TAG_KEYS = ("tags", "tag", "labels")
DATE_KEYS = ("dates", "date", "timeline", "period", "when", "due", "deadline")


def load_validator():
    spec = importlib.util.spec_from_file_location("cabinet_validate", VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cabinet_validate"] = module
    spec.loader.exec_module(module)
    return module


validator = load_validator()


@dataclass
class Page:
    old_rel: str
    new_rel: str
    page_id: str | None
    title: str
    body: str
    props: dict[str, str] = field(default_factory=dict)


@dataclass
class Report:
    pages: list[Page] = field(default_factory=list)
    assets: list[tuple[str, str]] = field(default_factory=list)
    skipped_csv: list[str] = field(default_factory=list)
    collisions: list[tuple[str, str]] = field(default_factory=list)
    links_rewritten: int = 0
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    dropped_props: dict[str, int] = field(default_factory=dict)


# Characters that NFKD does not decompose, so ascii-ignore would drop them
# silently rather than degrade them. Nordic and German names hit this.
TRANSLITERATE = str.maketrans(
    {"ß": "ss", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
     "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D"}
)


def slugify(name: str) -> str:
    """Lowercase ASCII slug. Empty input keeps a usable placeholder."""
    stripped = unicodedata.normalize("NFKD", name.translate(TRANSLITERATE))
    stripped = stripped.encode("ascii", "ignore").decode("ascii")
    stripped = re.sub(r"[^a-zA-Z0-9]+", "-", stripped).strip("-").lower()
    return stripped or "untitled"


def split_page_id(stem: str) -> tuple[str, str | None]:
    match = NAME_ID_RE.match(stem)
    if match:
        return match.group("name").strip(), match.group("id")
    return stem, None


def normalise_id(raw: str) -> str:
    return DASHED_UUID_RE.sub(r"\1\2\3\4\5", raw).lower()


def strip_code_fences(lines: list[str]) -> list[str]:
    out, in_fence = [], False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(None)  # type: ignore[arg-type]
            continue
        out.append(None if in_fence else line)  # type: ignore[arg-type]
    return out


def parse_export_page(text: str) -> tuple[str, dict[str, str], str]:
    """Return (title, properties, body) from one exported page."""
    lines = text.splitlines()
    index = 0
    title = ""
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and lines[index].startswith("# "):
        title = lines[index][2:].strip()
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1

    # Notion writes the property block as contiguous "Key: value" lines directly
    # under the title. The first line that is not one ends the block, so a
    # "Note: ..." sentence further down the body is never mistaken for a property.
    props: dict[str, str] = {}
    while index < len(lines):
        match = re.match(r"^([A-Za-z][A-Za-z0-9 _/-]{0,40}):[ \t]*(.*)$", lines[index])
        if not match:
            break
        props[match.group(1).strip()] = match.group(2).strip()
        index += 1

    body = "\n".join(lines[index:]).strip("\n")
    return title, props, body


def parse_notion_date(value: str) -> tuple[datetime.date | None, datetime.date | None]:
    """Notion writes 'October 1, 2026' or 'October 1, 2026 → December 15, 2026'."""
    if not value:
        return None, None
    parts = [p.strip() for p in re.split(r"→|->|–|—", value) if p.strip()]
    parsed: list[datetime.date | None] = []
    for part in parts[:2]:
        part = re.sub(r"\s+\d{1,2}:\d{2}\s*(AM|PM)?$", "", part, flags=re.I).strip()
        date = None
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
            try:
                date = datetime.datetime.strptime(part, fmt).date()
                break
            except ValueError:
                continue
        parsed.append(date)
    if not parsed:
        return None, None
    if len(parsed) == 1:
        return parsed[0], parsed[0]
    return parsed[0], parsed[1]


def pick(props: dict[str, str], keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    for key, value in props.items():
        if key.strip().lower() in keys:
            return key, value
    return None, None


def plan_paths(
    export: Path,
) -> tuple[list[Page], dict[str, str], dict[str, str], Report]:
    """Harvest ids and decide every new path before a single file is written."""
    report = Report()
    path_map: dict[str, str] = {}
    id_map: dict[str, str] = {}
    taken: set[str] = set()
    pages: list[Page] = []

    files = sorted(p for p in export.rglob("*") if p.is_file())

    for path in files:
        old_rel = path.relative_to(export).as_posix()
        parent_parts = []
        for part in path.parent.relative_to(export).parts:
            name, _ = split_page_id(part)
            parent_parts.append(slugify(name))

        if path.suffix.lower() == ".csv":
            report.skipped_csv.append(old_rel)
            # A database's rows live in the folder beside its CSV, so a link to
            # the CSV becomes a link to that folder rather than dead text.
            sibling = path.with_suffix("")
            if sibling.is_dir():
                stem, _ = split_page_id(sibling.name)
                folder = "/".join(parent_parts + [slugify(stem)])
                path_map[old_rel] = folder
            continue

        if path.suffix.lower() == ".md":
            stem, page_id = split_page_id(path.stem)
            new_dir = "/".join(parent_parts)
            candidate = f"{slugify(stem)}.md"
        else:
            # Attachments live beside the page that references them.
            page_id = None
            new_dir = "/".join(parent_parts + [ASSET_DIR])
            candidate = slugify(path.stem) + path.suffix.lower()

        new_rel = f"{new_dir}/{candidate}" if new_dir else candidate
        if new_rel in taken:
            base, ext = os.path.splitext(new_rel)
            counter = 2
            while f"{base}-{counter}{ext}" in taken:
                counter += 1
            report.collisions.append((old_rel, f"{base}-{counter}{ext}"))
            new_rel = f"{base}-{counter}{ext}"
        taken.add(new_rel)
        path_map[old_rel] = new_rel

        if page_id:
            id_map[page_id] = new_rel

        if path.suffix.lower() == ".md":
            title, props, body = parse_export_page(path.read_text(encoding="utf-8"))
            pages.append(
                Page(
                    old_rel=old_rel,
                    new_rel=new_rel,
                    page_id=page_id,
                    title=title or stem,
                    body=body,
                    props=props,
                )
            )
        else:
            report.assets.append((old_rel, new_rel))

    return pages, path_map, id_map, report


def rewrite_links(
    page: Page, path_map: dict[str, str], id_map: dict[str, str], report: Report
) -> str:
    old_dir = os.path.dirname(page.old_rel)
    new_dir = os.path.dirname(page.new_rel)

    lines = page.body.splitlines()
    scannable = strip_code_fences(lines)

    def resolve(target: str) -> str | None:
        split = urlsplit(target)
        if split.scheme or split.netloc:
            if "notion.so" not in split.netloc and "notion.site" not in split.netloc:
                return None  # a genuinely external link, left alone
            found = URL_ID_RE.search(normalise_id(target))
            if found and found.group(1) in id_map:
                return id_map[found.group(1)]
            return ""  # internal but unmapped
        path_part = unquote(split.path)
        if not path_part:
            return None
        candidate = os.path.normpath(os.path.join(old_dir, path_part))
        mapped = path_map.get(candidate.replace(os.sep, "/"))
        if mapped:
            return mapped
        return ""

    out: list[str] = []
    for lineno, line in enumerate(lines):
        if scannable[lineno] is None:
            out.append(line)
            continue

        def replace(match: re.Match) -> str:
            bang, text, target = match.group(1), match.group(2), match.group(3).strip()
            fragment = ""
            if "#" in target and not target.startswith("#"):
                target, _, fragment = target.partition("#")
                fragment = f"#{fragment}"
            mapped = resolve(target)
            if mapped is None:
                return match.group(0)
            if mapped == "":
                report.unresolved.append((page.new_rel, match.group(3).strip()))
                # A dead notion.so URL would fail V9 forever; keep the words.
                return text if not bang else ""
            relative = os.path.relpath(mapped, new_dir or ".").replace(os.sep, "/")
            report.links_rewritten += 1
            return f"{bang}[{text}]({relative}{fragment})"

        out.append(INLINE_LINK_RE.sub(replace, line))

    return "\n".join(out)


def front_matter(
    page: Page, owner_default: str, aliases: dict[str, str], report: Report
) -> dict:
    props = dict(page.props)

    status_key, status_raw = pick(props, STATUS_KEYS)
    status = STATUS_MAP.get((status_raw or "").strip().lower(), "draft")
    if status_key:
        props.pop(status_key)

    owner_key, owner_raw = pick(props, OWNER_KEYS)
    if owner_raw:
        owner = aliases.get(owner_raw.strip().lower(), slugify(owner_raw))
    else:
        owner = owner_default
    if owner_key:
        props.pop(owner_key)

    tag_key, tag_raw = pick(props, TAG_KEYS)
    tags = [slugify(t) for t in re.split(r"[,;]", tag_raw or "") if t.strip()] if tag_raw else []
    if tag_key:
        props.pop(tag_key)

    date_key, date_raw = pick(props, DATE_KEYS)
    start, end = parse_notion_date(date_raw or "")
    if date_key:
        props.pop(date_key)

    # Only a page with both dates can satisfy the schema's project rules; the
    # rest are documents until someone says otherwise.
    doc_type = "project" if start and end else "doc"
    slug = re.sub(r"\.md$", "", page.new_rel)
    doc_id = f"{TYPE_PREFIX[doc_type]}-{slugify(slug)}"

    meta = {
        "id": doc_id,
        "type": doc_type,
        "title": page.title,
        "status": status,
        "owner": owner or owner_default,
    }
    if doc_type == "project":
        meta["start"] = start.isoformat()
        meta["end"] = end.isoformat()
    if tags:
        meta["tags"] = tags
    if page.page_id:
        meta["notion_id"] = page.page_id

    for leftover in props:
        report.dropped_props[leftover] = report.dropped_props.get(leftover, 0) + 1
    return meta


def yaml_block(meta: dict) -> str:
    def quote(value: str) -> str:
        text = str(value)
        if text != text.strip() or re.search(r"[:#\[\]{}&*!|>'\"%@`,]", text) or not text:
            return "'" + text.replace("'", "''") + "'"
        return text

    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(quote(v) for v in value)}]")
        else:
            lines.append(f"{key}: {quote(value)}")
    lines.append("---")
    return "\n".join(lines)


def write_report(report: Report, out: Path) -> str:
    lines = [
        "# Migration report",
        "",
        f"- pages converted: {len(report.pages)}",
        f"- attachments moved: {len(report.assets)}",
        f"- links rewritten: {report.links_rewritten}",
        f"- links that could not be resolved: {len(report.unresolved)}",
        f"- name collisions resolved: {len(report.collisions)}",
        f"- database CSVs skipped: {len(report.skipped_csv)}",
        "",
    ]
    if report.unresolved:
        lines += [
            "## Unresolved links",
            "",
            "These pointed at pages outside the export. The link text was kept and the",
            "URL dropped, because a surviving notion.so URL fails V9 forever.",
            "",
        ]
        lines += [f"- `{path}` -> `{target}`" for path, target in report.unresolved]
        lines.append("")
    if report.dropped_props:
        lines += [
            "## Properties with nowhere to go",
            "",
            "Notion properties the schema has no field for. Add them to the schema or",
            "let them go.",
            "",
        ]
        lines += [
            f"- `{name}` on {count} page(s)"
            for name, count in sorted(report.dropped_props.items(), key=lambda kv: -kv[1])
        ]
        lines.append("")
    if report.collisions:
        lines += ["## Renamed to avoid collisions", ""]
        lines += [f"- `{old}` -> `{new}`" for old, new in report.collisions]
        lines.append("")
    if report.skipped_csv:
        lines += [
            "## Skipped database exports",
            "",
            "Every row is already its own page in the folder beside these.",
            "",
        ]
        lines += [f"- `{name}`" for name in report.skipped_csv]
        lines.append("")
    text = "\n".join(lines)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return text


def migrate(
    export: Path,
    out: Path,
    owner: str,
    aliases: dict[str, str],
    keep_unresolved: bool,
) -> Report:
    pages, path_map, id_map, report = plan_paths(export)
    report.pages = pages

    for old_rel, new_rel in report.assets:
        target = out / new_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(export / old_rel, target)

    for page in pages:
        if keep_unresolved:
            body = page.body
            report.links_rewritten += 0
        else:
            body = rewrite_links(page, path_map, id_map, report)
        meta = front_matter(page, owner, aliases, report)
        target = out / page.new_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"{yaml_block(meta)}\n\n{body}\n".replace("\n\n\n", "\n\n"), encoding="utf-8"
        )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", type=Path, required=True, help="unzipped Notion export")
    parser.add_argument("--out", type=Path, required=True, help="cabinet to write")
    parser.add_argument("--owner", default="unassigned", help="owner for pages with none")
    parser.add_argument(
        "--owner-alias",
        action="append",
        default=[],
        metavar="NOTION_NAME=HANDLE",
        help="map a Notion person to a handle, e.g. 'Marcel de Puit=marcel'; repeatable",
    )
    parser.add_argument("--report", type=Path, default=None, help="write the report here")
    parser.add_argument(
        "--keep-unresolved-links",
        action="store_true",
        help="leave notion.so URLs in place (the output will then fail V9)",
    )
    parser.add_argument("--force", action="store_true", help="write into a non-empty --out")
    args = parser.parse_args(argv)

    export = args.export.resolve()
    out = args.out.resolve()
    if not export.is_dir():
        print(f"FAILED: export {export} is not a directory", file=sys.stderr)
        return 2
    if out == export or export in out.parents:
        print("FAILED: --out must be outside the export directory", file=sys.stderr)
        return 2
    if out.exists() and any(out.iterdir()) and not args.force:
        print(f"FAILED: {out} is not empty (pass --force to write anyway)", file=sys.stderr)
        return 2

    aliases: dict[str, str] = {}
    for pair in args.owner_alias:
        if "=" not in pair:
            print(f"FAILED: --owner-alias {pair!r} is not NAME=HANDLE", file=sys.stderr)
            return 2
        name, _, handle = pair.partition("=")
        aliases[name.strip().lower()] = slugify(handle)

    out.mkdir(parents=True, exist_ok=True)
    report = migrate(export, out, args.owner, aliases, args.keep_unresolved_links)
    text = write_report(report, args.report) if args.report else write_report(report, None)

    print(text.split("## ")[0].strip())

    config = validator.Config(out, validator.DEFAULT_EXCLUDE, frozenset())
    violations, count = validator.validate(out, None, config)
    if violations:
        print()
        for violation in violations[:40]:
            print(violation.render())
        if len(violations) > 40:
            print(f"... and {len(violations) - 40} more")
        print(
            f"\nFAILED: the migrated cabinet has {len(violations)} schema violation(s).",
            file=sys.stderr,
        )
        return 1
    print(f"\nPASSED: {count} migrated document(s) satisfy the cabinet schema.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
