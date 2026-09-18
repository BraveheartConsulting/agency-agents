#!/usr/bin/env python3
"""Validate a docs cabinet against the front-matter contract in docs/cabinet/SCHEMA.md.

Implements rules V1-V9. Each violation is reported as:

    <path>[:<line>]: <rule> <message>

Exit codes: 0 clean, 1 violations found, 2 bad usage or unreadable config.

V4 (id immutability) needs a git base to compare against; pass --base main.
Without it, V4 is skipped and the summary says so.
"""
from __future__ import annotations

import argparse
import datetime
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

SCHEMA_VERSION = 1

TYPES = ("project", "doc", "decision", "note")
STATUSES = ("draft", "active", "blocked", "done", "archived")

ALWAYS_REQUIRED = ("id", "type", "title", "status", "owner")
PROJECT_REQUIRED = ("start", "end")
PROJECT_ONLY = ("start", "end", "progress", "depends_on")
KNOWN_KEYS = frozenset(ALWAYS_REQUIRED + PROJECT_ONLY + ("tags", "notion_id"))

ID_RE = re.compile(r"^(prj|doc|dec|note)-[a-z0-9]+(-[a-z0-9]+)*$")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NOTION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
NOTION_URL_RE = re.compile(r"notion\.so", re.IGNORECASE)

FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REF_LINK_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(\S+)")

DEFAULT_EXCLUDE = ("SCHEMA.md", "README.md", "**/_assets/**", ".github/**")


@dataclass
class Violation:
    path: str
    rule: str
    message: str
    line: int | None = None

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.rule} {self.message}"


@dataclass
class Document:
    path: Path
    rel: str
    text: str
    meta: dict | None = None
    body_offset: int = 0
    links: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class Config:
    content_root: Path
    exclude: tuple[str, ...]
    allow_removed_ids: frozenset[str]


def load_config(root: Path, explicit: Path | None) -> Config:
    path = explicit or (root / "cabinet.yml")
    data: dict = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError(f"{path}: expected a mapping at the top level")
        data = loaded or {}

    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version {version!r} is not supported by this validator "
            f"(expects {SCHEMA_VERSION})"
        )

    content_root = root / str(data.get("content_root", "."))
    exclude = tuple(data.get("exclude", DEFAULT_EXCLUDE))
    allow_removed = frozenset(str(i) for i in data.get("allow_removed_ids", []))
    return Config(content_root.resolve(), exclude, allow_removed)


def is_excluded(rel: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def split_front_matter(text: str) -> tuple[str | None, int]:
    """Return (yaml_source, line number of first body line). None if absent."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, 1
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return "\n".join(lines[1:i]), i + 2
    return None, 1


def strip_code_fences(lines: list[str]) -> list[str]:
    out: list[str] = []
    in_fence = False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return out


def extract_links(doc: Document) -> list[tuple[int, str]]:
    body = doc.text.splitlines()[doc.body_offset - 1 :]
    found: list[tuple[int, str]] = []
    for offset, line in enumerate(strip_code_fences(body)):
        lineno = doc.body_offset + offset
        targets = INLINE_LINK_RE.findall(line)
        ref = REF_LINK_RE.match(line)
        if ref:
            targets = targets + [ref.group(1)]
        for target in targets:
            target = target.strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            target = target.split()[0] if target.split() else ""
            if target:
                found.append((lineno, target))
    return found


def as_date(value) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def check_front_matter(doc: Document, out: list[Violation]) -> None:
    """V1 and V2, plus the field-level typing V3/V7 build on."""
    raw, doc.body_offset = split_front_matter(doc.text)
    if raw is None:
        out.append(Violation(doc.rel, "V1", "no YAML front-matter block at the top of the file"))
        return
    try:
        meta = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        detail = str(exc).replace("\n", " ")
        out.append(Violation(doc.rel, "V1", f"front-matter is not valid YAML: {detail}"))
        return
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        out.append(Violation(doc.rel, "V1", "front-matter must be a mapping"))
        return
    doc.meta = meta

    for key in sorted(set(meta) - KNOWN_KEYS):
        out.append(Violation(doc.rel, "V1", f"unknown key {key!r}"))

    for key in ALWAYS_REQUIRED:
        if key not in meta:
            out.append(Violation(doc.rel, "V2", f"missing required key {key!r}"))

    doc_type = meta.get("type")
    if doc_type is not None and doc_type not in TYPES:
        out.append(
            Violation(doc.rel, "V2", f"type {doc_type!r} is not one of {', '.join(TYPES)}")
        )
    status = meta.get("status")
    if status is not None and status not in STATUSES:
        out.append(
            Violation(doc.rel, "V2", f"status {status!r} is not one of {', '.join(STATUSES)}")
        )
    for key in ("title", "owner"):
        if key in meta and not (isinstance(meta[key], str) and meta[key].strip()):
            out.append(Violation(doc.rel, "V2", f"{key!r} must be a non-empty string"))

    if doc_type == "project":
        for key in PROJECT_REQUIRED:
            if key not in meta:
                out.append(Violation(doc.rel, "V2", f"type: project requires {key!r}"))
    else:
        for key in PROJECT_ONLY:
            if key in meta:
                out.append(
                    Violation(doc.rel, "V2", f"{key!r} is only valid on type: project")
                )

    if "progress" in meta:
        progress = meta["progress"]
        if not isinstance(progress, (int, float)) or isinstance(progress, bool):
            out.append(Violation(doc.rel, "V2", "'progress' must be a number"))
        elif not 0.0 <= float(progress) <= 1.0:
            out.append(Violation(doc.rel, "V2", f"progress {progress!r} is outside 0.0-1.0"))

    for key in ("tags", "depends_on"):
        if key in meta and not isinstance(meta[key], list):
            out.append(Violation(doc.rel, "V2", f"{key!r} must be a list"))

    for tag in meta.get("tags", []) if isinstance(meta.get("tags"), list) else []:
        if not (isinstance(tag, str) and SLUG_RE.match(tag)):
            out.append(Violation(doc.rel, "V2", f"tag {tag!r} is not a slug"))

    if "notion_id" in meta:
        notion_id = meta["notion_id"]
        if not (isinstance(notion_id, str) and NOTION_ID_RE.match(notion_id)):
            out.append(
                Violation(doc.rel, "V2", f"notion_id {notion_id!r} is not 32 hex characters")
            )

    body = doc.text.splitlines()[doc.body_offset - 1 :]
    for offset, line in enumerate(strip_code_fences(body)):
        if line.startswith("# "):
            out.append(
                Violation(
                    doc.rel,
                    "V2",
                    "body must not open with an H1; the title comes from front-matter",
                    doc.body_offset + offset,
                )
            )
            break


def check_ids(docs: list[Document], out: list[Violation]) -> dict[str, Document]:
    """V3: id shape and repo-wide uniqueness."""
    by_id: dict[str, Document] = {}
    for doc in docs:
        if not doc.meta or "id" not in doc.meta:
            continue
        doc_id = doc.meta["id"]
        if not isinstance(doc_id, str) or not ID_RE.match(doc_id):
            out.append(
                Violation(doc.rel, "V3", f"id {doc_id!r} does not match {ID_RE.pattern}")
            )
            continue
        if doc_id in by_id:
            out.append(
                Violation(doc.rel, "V3", f"id {doc_id!r} is already used by {by_id[doc_id].rel}")
            )
            continue
        by_id[doc_id] = doc
    return by_id


def git_show(base: str, rel: str, cwd: Path) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{base}:{rel}"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else None


def git_ls_tree(base: str, cwd: Path) -> list[str] | None:
    result = subprocess.run(
        # --full-name: paths relative to the repo top, not the cwd
        ["git", "ls-tree", "-r", "--full-name", "--name-only", base],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def check_id_immutability(
    docs: list[Document],
    base: str,
    repo_root: Path,
    config: Config,
    out: list[Violation],
) -> None:
    """V4: no existing file's id changed, and no id silently vanished."""
    tracked = git_ls_tree(base, repo_root)
    if tracked is None:
        out.append(Violation("<repo>", "V4", f"cannot read git base {base!r}"))
        return

    # git reports paths relative to the repository top level, which is not
    # necessarily --root, so anchor the cabinet against both.
    show_prefix = subprocess.run(
        ["git", "rev-parse", "--show-prefix"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    prefix = show_prefix.stdout.strip() if show_prefix.returncode == 0 else ""
    content_rel = config.content_root.relative_to(repo_root).as_posix()
    if content_rel != ".":
        prefix = f"{prefix}{content_rel}/"

    base_ids: dict[str, str] = {}
    for rel in tracked:
        if not rel.endswith(".md") or not rel.startswith(prefix):
            continue
        cabinet_rel = rel[len(prefix) :]
        if is_excluded(cabinet_rel, config.exclude):
            continue
        text = git_show(base, rel, repo_root)
        if text is None:
            continue
        raw, _ = split_front_matter(text)
        if raw is None:
            continue
        try:
            meta = yaml.safe_load(raw)
        except yaml.YAMLError:
            continue
        if isinstance(meta, dict) and isinstance(meta.get("id"), str):
            base_ids[cabinet_rel] = meta["id"]

    head_by_rel = {doc.rel: doc for doc in docs}
    head_ids = {
        doc.meta["id"]
        for doc in docs
        if doc.meta and isinstance(doc.meta.get("id"), str)
    }

    for rel, old_id in sorted(base_ids.items()):
        doc = head_by_rel.get(rel)
        if doc is not None and doc.meta and isinstance(doc.meta.get("id"), str):
            if doc.meta["id"] != old_id:
                out.append(
                    Violation(
                        rel,
                        "V4",
                        f"id changed from {old_id!r} to {doc.meta['id']!r}; ids are immutable",
                    )
                )
            continue
        if old_id in head_ids or old_id in config.allow_removed_ids:
            continue
        out.append(
            Violation(
                rel,
                "V4",
                f"id {old_id!r} is gone from the cabinet; if the removal is intended, "
                "list it under allow_removed_ids in cabinet.yml",
            )
        )


def check_dependencies(
    docs: list[Document], by_id: dict[str, Document], out: list[Violation]
) -> None:
    """V5: depends_on resolves. V6: the graph is acyclic."""
    graph: dict[str, list[str]] = {}
    for doc in docs:
        if not doc.meta:
            continue
        doc_id = doc.meta.get("id")
        deps = doc.meta.get("depends_on") or []
        if not isinstance(deps, list):
            continue
        resolved: list[str] = []
        for dep in deps:
            if not isinstance(dep, str):
                out.append(Violation(doc.rel, "V5", f"depends_on entry {dep!r} is not an id"))
                continue
            if dep == doc_id:
                out.append(Violation(doc.rel, "V5", f"depends_on lists itself ({dep!r})"))
                continue
            if dep not in by_id:
                out.append(Violation(doc.rel, "V5", f"depends_on {dep!r} resolves to nothing"))
                continue
            resolved.append(dep)
        if isinstance(doc_id, str):
            graph[doc_id] = resolved

    WHITE, GREY, BLACK = 0, 1, 2
    colour = {node: WHITE for node in graph}
    reported: set[frozenset[str]] = set()

    for root in graph:
        if colour[root] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        colour[root] = GREY
        path.append(root)
        while stack:
            node, index = stack[-1]
            edges = graph.get(node, [])
            if index < len(edges):
                stack[-1] = (node, index + 1)
                nxt = edges[index]
                if colour.get(nxt, BLACK) == GREY:
                    cycle = path[path.index(nxt) :] + [nxt]
                    key = frozenset(cycle)
                    if key not in reported:
                        reported.add(key)
                        out.append(
                            Violation(
                                by_id[nxt].rel,
                                "V6",
                                "depends_on cycle: " + " -> ".join(cycle),
                            )
                        )
                elif colour.get(nxt, WHITE) == WHITE:
                    colour[nxt] = GREY
                    path.append(nxt)
                    stack.append((nxt, 0))
            else:
                colour[node] = BLACK
                stack.pop()
                if path and path[-1] == node:
                    path.pop()


def check_dates(docs: list[Document], out: list[Violation]) -> None:
    """V7: ISO dates, end >= start."""
    for doc in docs:
        if not doc.meta or doc.meta.get("type") != "project":
            continue
        parsed: dict[str, datetime.date] = {}
        for key in ("start", "end"):
            if key not in doc.meta:
                continue
            value = doc.meta[key]
            if isinstance(value, datetime.datetime):
                out.append(
                    Violation(doc.rel, "V7", f"{key} {value!r} must be a date, not a timestamp")
                )
                continue
            date = as_date(value)
            if date is None:
                out.append(Violation(doc.rel, "V7", f"{key} {value!r} is not an ISO date"))
            else:
                parsed[key] = date
        if "start" in parsed and "end" in parsed and parsed["end"] < parsed["start"]:
            out.append(
                Violation(
                    doc.rel,
                    "V7",
                    f"end {parsed['end'].isoformat()} is before start {parsed['start'].isoformat()}",
                )
            )


def check_links(docs: list[Document], config: Config, out: list[Violation]) -> None:
    """V8: relative .md links resolve."""
    for doc in docs:
        for lineno, target in doc.links:
            if target.startswith("#"):
                continue
            split = urlsplit(target)
            if split.scheme or split.netloc:
                continue
            path_part = unquote(split.path)
            if not path_part:
                continue
            if not path_part.endswith(".md"):
                continue
            if path_part.startswith("/"):
                out.append(
                    Violation(doc.rel, "V8", f"link {target!r} must be relative", lineno)
                )
                continue
            resolved = (doc.path.parent / path_part).resolve()
            if not resolved.is_file():
                out.append(
                    Violation(doc.rel, "V8", f"link {target!r} resolves to nothing", lineno)
                )
                continue
            try:
                resolved.relative_to(config.content_root)
            except ValueError:
                out.append(
                    Violation(
                        doc.rel, "V8", f"link {target!r} escapes the cabinet root", lineno
                    )
                )


def check_no_notion_urls(docs: list[Document], out: list[Violation]) -> None:
    """V9: no surviving notion.so URL."""
    for doc in docs:
        for offset, line in enumerate(doc.text.splitlines(), start=1):
            if NOTION_URL_RE.search(line):
                out.append(
                    Violation(doc.rel, "V9", "notion.so URL survived migration", offset)
                )


def collect(config: Config) -> list[Document]:
    docs: list[Document] = []
    for path in sorted(config.content_root.rglob("*.md")):
        rel = path.relative_to(config.content_root).as_posix()
        if is_excluded(rel, config.exclude):
            continue
        docs.append(Document(path=path, rel=rel, text=path.read_text(encoding="utf-8")))
    return docs


def validate(root: Path, base: str | None, config: Config) -> tuple[list[Violation], int]:
    violations: list[Violation] = []
    docs = collect(config)

    for doc in docs:
        check_front_matter(doc, violations)
        doc.links = extract_links(doc)

    by_id = check_ids(docs, violations)
    if base:
        check_id_immutability(docs, base, root, config, violations)
    check_dependencies(docs, by_id, violations)
    check_dates(docs, violations)
    check_links(docs, config, violations)
    check_no_notion_urls(docs, violations)

    violations.sort(key=lambda v: (v.path, v.line or 0, v.rule))
    return violations, len(docs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="repository root holding cabinet.yml (default: this repo)",
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="path to cabinet.yml (default: <root>/cabinet.yml)"
    )
    parser.add_argument(
        "--base",
        default=None,
        help="git ref to compare ids against for V4 (e.g. main, origin/main)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    try:
        config = load_config(root, args.config)
    except (ValueError, yaml.YAMLError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    if not config.content_root.is_dir():
        print(f"FAILED: content root {config.content_root} does not exist", file=sys.stderr)
        return 2

    violations, count = validate(root, args.base, config)

    for violation in violations:
        print(violation.render())

    skipped = "" if args.base else " (V4 skipped: no --base given)"
    if violations:
        print(
            f"\nFAILED: {len(violations)} violation(s) across {count} document(s){skipped}.",
            file=sys.stderr,
        )
        return 1
    print(f"PASSED: {count} document(s) satisfy cabinet schema v{SCHEMA_VERSION}{skipped}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
