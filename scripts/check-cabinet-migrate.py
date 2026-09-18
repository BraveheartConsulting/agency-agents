#!/usr/bin/env python3
"""Exercise scripts/cabinet-migrate.py against synthetic Notion exports."""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATE_PATH = REPO_ROOT / "scripts" / "cabinet-migrate.py"

OPS_ID = "7cb06f4af45782ca98a301e75358ce67"
PROJECTS_ID = "8aa16f4af45782ca98a301e75358ce68"
LIMS_ID = "24f0a8b1c2d34e5f9a0b1c2d3e4f5a6b"
INTAKE_ID = "35e1b9c2d3e45f609b1c2d3e4f5a6b7c"
WIKI_ID = "46f2cad3e4f56071ac2d3e4f5a6b7c8d"
GHOST_ID = "99999999999999999999999999999999"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


migrate = load_module("cabinet_migrate", MIGRATE_PATH)


def export_files() -> dict[str, str]:
    ops = f"Operations Headquarters {OPS_ID}"
    projects = f"{ops}/Projects {PROJECTS_ID}"
    wiki = f"{ops}/Wiki Cellation {WIKI_ID}"
    return {
        f"{ops}.md": f"""# Operations Headquarters

Owner: Marcel de Puit
Tags: operations, cellation

The venture organisation lives here.

See [Projects]({ops.replace(' ', '%20')}/Projects%20{PROJECTS_ID}.csv) and the
[Cellation wiki](https://www.notion.so/Wiki-Cellation-46f2cad3-e4f5-6071-ac2d-3e4f5a6b7c8d).

An [external page](https://example.com/handbook) stays as it is.

A [ghost page](https://www.notion.so/Gone-{GHOST_ID}) no longer exists.

```
[not a link](Projects%20{PROJECTS_ID}.md)
```
""",
        f"{projects}.csv": 'Name,Status\nForensics LIMS rollout,In progress\n',
        f"{projects}/Forensics LIMS rollout {LIMS_ID}.md": f"""# Forensics LIMS rollout

Status: In progress
Owner: Marcel de Puit
Dates: October 1, 2026 → December 15, 2026
Tags: forensics
Priority: High

## Scope

Depends on [Sample intake redesign](Sample%20intake%20redesign%20{INTAKE_ID}.md).

Note: this line is body text, not a property.
""",
        f"{projects}/Sample intake redesign {INTAKE_ID}.md": """# Sample intake redesign

Status: Done
Owner: Marcel de Puit
Dates: August 15, 2026 → September 30, 2026

## Notes

Signed off.
""",
        f"{wiki}.md": f"""# Wiki Cellation

Status: Active
Owner: Marcel de Puit

## Overview

![Architecture](Wiki%20Cellation%20{WIKI_ID}/diagram.png)
""",
        f"{wiki}/diagram.png": "fake png bytes",
    }


def build_export(tmp: Path, name: str, files: dict[str, str]) -> Path:
    root = tmp / name / "export"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    return root


def run(export: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "python3",
            str(MIGRATE_PATH),
            "--export",
            str(export),
            "--out",
            str(out),
            "--owner",
            "marcel",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def read(out: Path, rel: str) -> str:
    return (out / rel).read_text(encoding="utf-8")


def case_happy_path(tmp: Path) -> None:
    export = build_export(tmp, "happy", export_files())
    out = tmp / "happy" / "cabinet"
    result = run(export, out, "--owner-alias", "Marcel de Puit=marcel")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "satisfy the cabinet schema" in result.stdout, result.stdout

    written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert written == [
        "operations-headquarters.md",
        "operations-headquarters/projects/forensics-lims-rollout.md",
        "operations-headquarters/projects/sample-intake-redesign.md",
        "operations-headquarters/wiki-cellation.md",
        "operations-headquarters/wiki-cellation/_assets/diagram.png",
    ], written

    lims = read(out, "operations-headquarters/projects/forensics-lims-rollout.md")
    assert "type: project" in lims, lims
    assert "start: 2026-10-01" in lims and "end: 2026-12-15" in lims, lims
    assert "status: active" in lims, lims
    assert "owner: marcel" in lims, "owner alias was not applied"
    assert f"notion_id: {LIMS_ID}" in lims, lims
    assert "tags: [forensics]" in lims, lims
    assert "# Forensics LIMS rollout" not in lims, "the body H1 must be dropped"
    assert "Note: this line is body text" in lims, "a body line was eaten as a property"
    assert "Priority" not in lims, "an unmapped property leaked into front-matter"


def case_links(tmp: Path) -> None:
    export = build_export(tmp, "links", export_files())
    out = tmp / "links" / "cabinet"
    assert run(export, out).returncode == 0

    ops = read(out, "operations-headquarters.md")
    # A dashed notion.so UUID resolves through the id map harvested before renaming.
    assert "[Cellation wiki](operations-headquarters/wiki-cellation.md)" in ops, ops
    # A database CSV link points at the folder its rows landed in.
    assert "[Projects](operations-headquarters/projects)" in ops, ops
    # An external link is left exactly as it was.
    assert "[external page](https://example.com/handbook)" in ops, ops
    # A link inside a fence is not a link.
    assert f"[not a link](Projects%20{PROJECTS_ID}.md)" in ops, ops

    lims = read(out, "operations-headquarters/projects/forensics-lims-rollout.md")
    assert "[Sample intake redesign](sample-intake-redesign.md)" in lims, lims

    wiki = read(out, "operations-headquarters/wiki-cellation.md")
    assert "![Architecture](wiki-cellation/_assets/diagram.png)" in wiki, wiki

    for rel in ("operations-headquarters.md",):
        assert "notion.so" not in read(out, rel), "a notion.so URL survived (V9)"
    assert "ghost page no longer exists" in ops, "unresolved link should keep its text"


def case_keep_unresolved_fails_validation(tmp: Path) -> None:
    export = build_export(tmp, "keep", export_files())
    out = tmp / "keep" / "cabinet"
    result = run(export, out, "--keep-unresolved-links")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "V9" in result.stdout, result.stdout
    assert "notion.so" in read(out, "operations-headquarters.md")


def case_collisions(tmp: Path) -> None:
    files = {
        f"Plan {LIMS_ID}.md": "# Plan\n\nOne.\n",
        f"plan! {INTAKE_ID}.md": "# Plan!\n\nTwo.\n",
    }
    export = build_export(tmp, "collide", files)
    out = tmp / "collide" / "cabinet"
    result = run(export, out)
    assert result.returncode == 0, result.stdout + result.stderr
    written = sorted(p.name for p in out.rglob("*.md"))
    assert written == ["plan-2.md", "plan.md"], written
    # Both ids stay unique, which V3 would otherwise reject.
    ids = sorted(
        re.search(r"^id: (.+)$", read(out, name), re.M).group(1) for name in written
    )
    assert len(set(ids)) == 2, ids


def case_type_inference(tmp: Path) -> None:
    files = {
        f"Dated {LIMS_ID}.md": (
            "# Dated\n\nStatus: Done\nDates: January 5, 2027 → March 31, 2027\n\n## B\n\nx\n"
        ),
        f"Undated {INTAKE_ID}.md": "# Undated\n\nStatus: Done\n\n## B\n\nx\n",
        f"OneDay {WIKI_ID}.md": "# OneDay\n\nDates: November 10, 2026\n\n## B\n\nx\n",
    }
    export = build_export(tmp, "types", files)
    out = tmp / "types" / "cabinet"
    assert run(export, out).returncode == 0

    dated = read(out, "dated.md")
    assert "type: project" in dated and "id: prj-dated" in dated, dated
    undated = read(out, "undated.md")
    assert "type: doc" in undated and "id: doc-undated" in undated, undated
    # A single Notion date is a milestone: start == end, which the schema allows.
    oneday = read(out, "oneday.md")
    assert "start: 2026-11-10" in oneday and "end: 2026-11-10" in oneday, oneday


def case_report(tmp: Path) -> None:
    export = build_export(tmp, "report", export_files())
    out = tmp / "report" / "cabinet"
    report_path = tmp / "report" / "migration-report.md"
    assert run(export, out, "--report", str(report_path)).returncode == 0

    report = report_path.read_text(encoding="utf-8")
    assert "- pages converted: 4" in report, report
    assert "- attachments moved: 1" in report, report
    assert "- links that could not be resolved: 1" in report, report
    assert "- database CSVs skipped: 1" in report, report
    assert "`Priority` on 1 page(s)" in report, report
    assert GHOST_ID in report, "the unresolved link must be named, not just counted"


def case_safety(tmp: Path) -> None:
    export = build_export(tmp, "safety", export_files())
    out = tmp / "safety" / "cabinet"

    inside = run(export, export / "sub")
    assert inside.returncode == 2, inside.stdout + inside.stderr
    assert "outside the export" in inside.stderr, inside.stderr

    missing = run(tmp / "safety" / "nope", out)
    assert missing.returncode == 2, missing.stdout + missing.stderr

    assert run(export, out).returncode == 0
    again = run(export, out)
    assert again.returncode == 2, "a non-empty --out must not be overwritten silently"
    assert "--force" in again.stderr, again.stderr
    assert run(export, out, "--force").returncode == 0


def case_unicode_titles(tmp: Path) -> None:
    files = {
        f"Überprüfung & Größe {LIMS_ID}.md": "# Überprüfung & Größe\n\n## B\n\nx\n",
        f"日本語 {INTAKE_ID}.md": "# 日本語\n\n## B\n\nx\n",
    }
    export = build_export(tmp, "unicode", files)
    out = tmp / "unicode" / "cabinet"
    result = run(export, out)
    assert result.returncode == 0, result.stdout + result.stderr
    names = sorted(p.name for p in out.rglob("*.md"))
    # ss for ß rather than the silent drop ascii-ignore would give.
    assert names == ["uberprufung-grosse.md", "untitled.md"], names
    # The title survives in full even when the filename cannot carry it.
    assert "title: 日本語" in read(out, "untitled.md")


def main() -> int:
    cases = [
        case_happy_path,
        case_links,
        case_keep_unresolved_fails_validation,
        case_collisions,
        case_type_inference,
        case_report,
        case_safety,
        case_unicode_titles,
    ]
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for case in cases:
            case(tmp)
    print(f"PASSED: cabinet migration satisfies {len(cases)} fixture cases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
