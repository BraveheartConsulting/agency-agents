#!/usr/bin/env python3
"""Exercise scripts/cabinet-validate.py against fixtures covering rules V1-V9."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "scripts" / "cabinet-validate.py"

CLEAN_PROJECT = """---
id: prj-cellation-forensics-lims
type: project
title: Forensics LIMS rollout
status: active
owner: marcel
start: 2026-10-01
end: 2026-12-15
progress: 0.4
depends_on:
  - prj-cellation-forensics-intake
tags: [cellation, forensics]
notion_id: 24f0a8b1c2d34e5f9a0b1c2d3e4f5a6b
---

## Scope

See [intake](intake.md) and [the handbook](https://example.com/handbook).
"""

CLEAN_DEP = """---
id: prj-cellation-forensics-intake
type: project
title: Sample intake
status: done
owner: marcel
start: 2026-09-01
end: 2026-09-30
---

## Notes

Nothing outstanding.
"""

CLEAN_DOC = """---
id: doc-cellation-forensics-intake
type: doc
title: Intake procedure
status: active
owner: marcel
---

## Steps

Start at step one.
"""


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules, so register first
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


validator = load_module("cabinet_validate", VALIDATOR_PATH)


def build(tmp: Path, files: dict[str, str]) -> Path:
    root = tmp / "cabinet"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    return root


def run(root: Path, base: str | None = None) -> list:
    config = validator.load_config(root, None)
    violations, _ = validator.validate(root, base, config)
    return violations


def rules(violations) -> list[str]:
    return sorted(v.rule for v in violations)


def expect(violations, expected: list[str], label: str) -> None:
    actual = rules(violations)
    assert actual == sorted(expected), (
        f"{label}: expected {sorted(expected)}, got {actual}\n"
        + "\n".join(v.render() for v in violations)
    )


def clean_corpus() -> dict[str, str]:
    return {
        "forensics/lims.md": CLEAN_PROJECT,
        "forensics/intake.md": CLEAN_DEP,
        "forensics/intake-doc.md": CLEAN_DOC,
    }


def case_clean(tmp: Path) -> None:
    root = build(tmp / "clean", clean_corpus())
    expect(run(root), [], "clean corpus")


def case_excluded_files_are_skipped(tmp: Path) -> None:
    files = clean_corpus()
    files["README.md"] = "# Cabinet\n\nNo front-matter here, and that is fine.\n"
    files["SCHEMA.md"] = "# Schema\n\nMentions notion.so on purpose.\n"
    files["forensics/_assets/note.md"] = "loose asset, no front-matter\n"
    root = build(tmp / "excluded", files)
    expect(run(root), [], "excluded files")


def case_v1(tmp: Path) -> None:
    root = build(
        tmp / "v1",
        {
            "none.md": "## No front-matter\n",
            "broken.md": "---\nid: [unclosed\n---\n\n## Body\n",
            "unknown.md": CLEAN_DOC.replace("owner: marcel", "owner: marcel\nassignee: someone"),
        },
    )
    violations = run(root)
    # a file with no parsable front-matter stops at V1 rather than cascading V2 errors
    expect(violations, ["V1", "V1", "V1"], "V1 shapes")
    assert any("unknown key 'assignee'" in v.message for v in violations), "unknown key not named"


def case_v2(tmp: Path) -> None:
    missing = """---
id: doc-a
type: doc
title: A
owner: marcel
---

## Body
"""
    bad_enum = """---
id: doc-b
type: memo
title: B
status: wip
owner: marcel
---

## Body
"""
    project_fields_on_doc = """---
id: doc-c
type: doc
title: C
status: active
owner: marcel
start: 2026-01-01
progress: 0.5
---

## Body
"""
    project_missing_dates = """---
id: prj-d
type: project
title: D
status: active
owner: marcel
---

## Body
"""
    bad_progress = """---
id: prj-e
type: project
title: E
status: active
owner: marcel
start: 2026-01-01
end: 2026-02-01
progress: 4
---

## Body
"""
    body_h1 = """---
id: doc-f
type: doc
title: F
status: active
owner: marcel
---

# F

Body.
"""
    root = build(
        tmp / "v2",
        {
            "missing.md": missing,
            "enum.md": bad_enum,
            "projectfields.md": project_fields_on_doc,
            "dates.md": project_missing_dates,
            "progress.md": bad_progress,
            "h1.md": body_h1,
        },
    )
    violations = run(root)
    by_file = {}
    for v in violations:
        by_file.setdefault(v.path, []).append(v.rule)
    assert by_file["missing.md"] == ["V2"], by_file["missing.md"]
    assert by_file["enum.md"] == ["V2", "V2"], by_file["enum.md"]
    assert by_file["projectfields.md"] == ["V2", "V2"], by_file["projectfields.md"]
    assert by_file["dates.md"] == ["V2", "V2"], by_file["dates.md"]
    assert by_file["progress.md"] == ["V2"], by_file["progress.md"]
    assert by_file["h1.md"] == ["V2"], by_file["h1.md"]
    assert any(
        v.path == "h1.md" and "H1" in v.message and v.line == 9 for v in violations
    ), "H1 line number wrong"


def case_v3(tmp: Path) -> None:
    bad_shape = CLEAN_DOC.replace("id: doc-cellation-forensics-intake", "id: Doc_Intake")
    dupe = CLEAN_DOC.replace("title: Intake procedure", "title: Duplicate")
    root = build(tmp / "v3", {"bad.md": bad_shape, "a.md": CLEAN_DOC, "b.md": dupe})
    violations = run(root)
    expect(violations, ["V3", "V3"], "V3 id shape and uniqueness")
    assert any("already used by a.md" in v.message for v in violations), "duplicate not attributed"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def case_v4(tmp: Path) -> None:
    root = build(tmp / "v4", clean_corpus())
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "test")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")

    expect(run(root, base="main"), [], "V4 unchanged")

    lims = root / "forensics" / "lims.md"
    original = lims.read_text(encoding="utf-8")

    renamed = root / "forensics" / "lims-rollout.md"
    lims.rename(renamed)
    expect(run(root, base="main"), [], "V4 rename keeps id")
    renamed.rename(lims)

    lims.write_text(
        original.replace("id: prj-cellation-forensics-lims", "id: prj-cellation-lims"),
        encoding="utf-8",
    )
    violations = run(root, base="main")
    assert "V4" in rules(violations), f"V4 missed an id change: {rules(violations)}"
    assert any("ids are immutable" in v.message for v in violations)
    lims.write_text(original, encoding="utf-8")

    lims.unlink()
    violations = run(root, base="main")
    assert "V4" in rules(violations), "V4 missed a vanished id"

    (root / "cabinet.yml").write_text(
        "schema_version: 1\nallow_removed_ids:\n  - prj-cellation-forensics-lims\n",
        encoding="utf-8",
    )
    violations = run(root, base="main")
    assert "V4" not in rules(violations), f"allow_removed_ids ignored: {rules(violations)}"


def case_v4_root_below_repo_top(tmp: Path) -> None:
    """V4 must anchor git paths when --root sits below the git top level."""
    repo = build(tmp / "v4sub", {f"docs/cab/{rel}": text for rel, text in clean_corpus().items()})
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")

    cabinet = repo / "docs" / "cab"
    expect(run(cabinet, base="main"), [], "V4 subdir unchanged")

    lims = cabinet / "forensics" / "lims.md"
    lims.write_text(
        lims.read_text(encoding="utf-8").replace(
            "id: prj-cellation-forensics-lims", "id: prj-renamed"
        ),
        encoding="utf-8",
    )
    violations = run(cabinet, base="main")
    assert "V4" in rules(violations), (
        f"V4 missed an id change when --root is below the repo top: {rules(violations)}"
    )


def case_v5_v6(tmp: Path) -> None:
    dangling = CLEAN_DEP.replace(
        "owner: marcel", "owner: marcel\ndepends_on: [prj-does-not-exist]"
    )
    root = build(tmp / "v5", {"a.md": dangling})
    expect(run(root), ["V5"], "V5 dangling dependency")

    selfdep = CLEAN_DEP.replace(
        "owner: marcel", "owner: marcel\ndepends_on: [prj-cellation-forensics-intake]"
    )
    root = build(tmp / "v5self", {"a.md": selfdep})
    expect(run(root), ["V5"], "V5 self dependency")

    a = """---
id: prj-a
type: project
title: A
status: active
owner: marcel
start: 2026-01-01
end: 2026-02-01
depends_on: [prj-b]
---

## Body
"""
    b = a.replace("id: prj-a", "id: prj-b").replace("[prj-b]", "[prj-c]").replace("title: A", "title: B")
    c = a.replace("id: prj-a", "id: prj-c").replace("[prj-b]", "[prj-a]").replace("title: A", "title: C")
    root = build(tmp / "v6", {"a.md": a, "b.md": b, "c.md": c})
    violations = run(root)
    expect(violations, ["V6"], "V6 cycle")
    assert "->" in violations[0].message, "cycle path not reported"


def case_v7(tmp: Path) -> None:
    backwards = CLEAN_DEP.replace("end: 2026-09-30", "end: 2026-08-01")
    not_a_date = CLEAN_DEP.replace("end: 2026-09-30", "end: 'Q4'")
    timestamp = CLEAN_DEP.replace("end: 2026-09-30", "end: 2026-09-30 12:00:00")
    milestone = CLEAN_DEP.replace("end: 2026-09-30", "end: 2026-09-01")
    root = build(
        tmp / "v7",
        {
            "back.md": backwards,
            "text.md": not_a_date.replace("id: prj-cellation-forensics-intake", "id: prj-t"),
            "stamp.md": timestamp.replace("id: prj-cellation-forensics-intake", "id: prj-s"),
            "milestone.md": milestone.replace("id: prj-cellation-forensics-intake", "id: prj-m"),
        },
    )
    violations = run(root)
    by_file = {}
    for v in violations:
        by_file.setdefault(v.path, []).append(v.rule)
    assert by_file.get("back.md") == ["V7"], by_file.get("back.md")
    assert by_file.get("text.md") == ["V7"], by_file.get("text.md")
    assert by_file.get("stamp.md") == ["V7"], by_file.get("stamp.md")
    assert "milestone.md" not in by_file, "start == end should be a valid milestone"


def case_v8(tmp: Path) -> None:
    broken = CLEAN_DOC.replace(
        "Start at step one.", "See [gone](../nowhere/missing.md)."
    )
    absolute = CLEAN_DOC.replace(
        "Start at step one.", "See [abs](/forensics/intake.md)."
    ).replace("id: doc-cellation-forensics-intake", "id: doc-abs")
    fenced = CLEAN_DOC.replace(
        "Start at step one.",
        "```\n[not a link](missing.md)\n```\n\nPlain text.",
    ).replace("id: doc-cellation-forensics-intake", "id: doc-fenced")
    encoded = CLEAN_DOC.replace(
        "Start at step one.", "See [enc](intake%20doc.md)."
    ).replace("id: doc-cellation-forensics-intake", "id: doc-enc")
    root = build(
        tmp / "v8",
        {
            "broken.md": broken,
            "absolute.md": absolute,
            "fenced.md": fenced,
            "encoded.md": encoded,
            "intake doc.md": CLEAN_DOC.replace(
                "id: doc-cellation-forensics-intake", "id: doc-spaced"
            ),
        },
    )
    violations = run(root)
    by_file = {}
    for v in violations:
        by_file.setdefault(v.path, []).append(v.rule)
    assert by_file.get("broken.md") == ["V8"], by_file.get("broken.md")
    assert by_file.get("absolute.md") == ["V8"], by_file.get("absolute.md")
    assert "fenced.md" not in by_file, "links inside a code fence must be ignored"
    assert "encoded.md" not in by_file, "percent-encoded link target must resolve"


def case_v9(tmp: Path) -> None:
    leftover = CLEAN_DOC.replace(
        "Start at step one.",
        "See [old](https://www.notion.so/Intake-24f0a8b1c2d34e5f9a0b1c2d3e4f5a6b).",
    )
    root = build(tmp / "v9", {"old.md": leftover})
    violations = run(root)
    assert "V9" in rules(violations), f"V9 missed a notion.so URL: {rules(violations)}"
    expected_line = leftover.splitlines().index(
        [line for line in leftover.splitlines() if "notion.so" in line][0]
    ) + 1
    assert any(
        v.rule == "V9" and v.line == expected_line for v in violations
    ), f"V9 line number wrong: {[v.render() for v in violations]}"


def case_cli(tmp: Path) -> None:
    root = build(tmp / "cli", clean_corpus())
    result = subprocess.run(
        ["python3", str(VALIDATOR_PATH), "--root", str(root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "V4 skipped" in result.stdout, result.stdout

    (root / "bad.md").write_text("## no front-matter\n", encoding="utf-8")
    result = subprocess.run(
        ["python3", str(VALIDATOR_PATH), "--root", str(root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "bad.md: V1" in result.stdout, result.stdout

    (root / "cabinet.yml").write_text("schema_version: 99\n", encoding="utf-8")
    result = subprocess.run(
        ["python3", str(VALIDATOR_PATH), "--root", str(root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, result.stdout + result.stderr


def main() -> int:
    cases = [
        case_clean,
        case_excluded_files_are_skipped,
        case_v1,
        case_v2,
        case_v3,
        case_v4,
        case_v4_root_below_repo_top,
        case_v5_v6,
        case_v7,
        case_v8,
        case_v9,
        case_cli,
    ]
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for case in cases:
            case(tmp)
    print(f"PASSED: cabinet validator satisfies {len(cases)} fixture cases across rules V1-V9.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
