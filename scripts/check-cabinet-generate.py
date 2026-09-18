#!/usr/bin/env python3
"""Exercise scripts/cabinet-generate.py: refusal, geometry, escaping, and chrome."""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = REPO_ROOT / "scripts" / "cabinet-generate.py"

PROJECT = """---
id: {id}
type: project
title: {title}
status: {status}
owner: marcel
start: {start}
end: {end}
---

## Body

Text.
"""

DOC = """---
id: doc-handbook
type: doc
title: Handbook
status: active
owner: marcel
---

## Body

Text.
"""


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generator = load_module("cabinet_generate", GENERATOR_PATH)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def build(tmp: Path, name: str, files: dict[str, str], commit: bool = True) -> Path:
    root = tmp / name
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if commit:
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "test")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "sample")
    return root


def run(root: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(GENERATOR_PATH), "--root", str(root), "--out", str(out), *extra],
        capture_output=True,
        text=True,
    )


def svg_of(html: str) -> ET.Element:
    match = re.search(r'<svg xmlns=[^>]*class="gantt".*?</svg>', html, re.S)
    assert match, "no gantt svg in the page"
    return ET.fromstring(match.group(0))


def sample_files() -> dict[str, str]:
    return {
        "a.md": PROJECT.format(
            id="prj-a", title="Alpha", status="active", start="2026-01-01", end="2026-02-28"
        ),
        "b.md": PROJECT.format(
            id="prj-b", title="Beta", status="blocked", start="2026-03-01", end="2026-04-30"
        ),
        "c.md": PROJECT.format(
            id="prj-c", title="Gamma", status="done", start="2026-05-01", end="2026-05-01"
        ),
        "handbook.md": DOC,
    }


def case_refuses_invalid_corpus(tmp: Path) -> None:
    root = build(tmp, "invalid", {"bad.md": "## no front-matter\n"})
    out = root / "index.html"
    result = run(root, out)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "bad.md: V1" in result.stdout, result.stdout
    assert "refusing to generate" in result.stderr, result.stderr
    assert not out.exists(), "a refused run must not leave a page behind"


def case_renders_documents(tmp: Path) -> None:
    root = build(tmp, "valid", sample_files())
    out = root / "index.html"
    result = run(root, out, "--title", "Cabinet", "--today", "2026-03-15")
    assert result.returncode == 0, result.stdout + result.stderr
    html = out.read_text(encoding="utf-8")

    for title in ("Alpha", "Beta", "Gamma", "Handbook"):
        assert title in html, f"{title} missing from the page"

    svg = svg_of(html)
    rows = svg.findall('.//{http://www.w3.org/2000/svg}g[@class="row"]')
    assert len(rows) == 3, f"expected 3 project rows, got {len(rows)}"

    # The doc has no dates, so it appears in the table but never on the timeline.
    assert "Handbook" not in ET.tostring(svg, encoding="unicode")

    # Table carries every document, so no value is gated behind a tooltip.
    body = html.split("<tbody>")[1].split("</tbody>")[0]
    assert body.count("<tr>") == 4, body.count("<tr>")
    assert "100%" not in body, "progress must read as an em dash when not reported"

    assert 'id="tip"' in html and "pointermove" in html, "hover layer missing"
    assert "prefers-color-scheme: dark" in html and '[data-theme="dark"]' in html
    assert "http" not in html.split("<style>")[1].split("</style>")[0], (
        "stylesheet must not reference anything external"
    )


def case_geometry_follows_dates(tmp: Path) -> None:
    root = build(tmp, "geometry", sample_files())
    out = root / "index.html"
    assert run(root, out, "--today", "2026-03-15").returncode == 0
    svg = svg_of(out.read_text(encoding="utf-8"))
    ns = "{http://www.w3.org/2000/svg}"

    bars = []
    for row in svg.findall(f'.//{ns}g[@class="row"]'):
        rect = next(
            (
                r
                for r in row.findall(f"{ns}rect")
                if r.get("class", "").startswith("bar-")
                and "track" not in r.get("class", "")
            ),
            None,
        )
        assert rect is not None, "row without a bar"
        bars.append((row.get("data-title"), float(rect.get("x")), float(rect.get("width"))))

    by_title = {t: (x, w) for t, x, w in bars}
    assert by_title["Alpha"][0] < by_title["Beta"][0] < by_title["Gamma"][0], bars
    assert by_title["Alpha"][1] > 0 and by_title["Beta"][1] > 0

    # Every bar stays inside the plot area and under the 24px mark cap.
    for _, x, _ in bars:
        assert x >= generator.LABEL_W, f"bar starts left of the plot: {x}"
    for row in svg.findall(f'.//{ns}g[@class="row"]'):
        for rect in row.findall(f"{ns}rect"):
            if rect.get("class", "").startswith("bar-"):
                assert float(rect.get("height")) <= 24, rect.get("height")

    # Gamma's start equals its end, so it is a milestone, not a zero-width bar.
    gamma = next(r for r in svg.findall(f'.//{ns}g[@class="row"]') if r.get("data-title") == "Gamma")
    milestone = [r for r in gamma.findall(f"{ns}rect") if "milestone" in r.get("class", "")]
    assert len(milestone) == 1, "milestone should render as a rotated square"
    assert "rotate(45" in milestone[0].get("transform", "")

    # Blocked rows use the status class; nothing else does.
    blocked = [
        r.get("data-title")
        for r in svg.findall(f'.//{ns}g[@class="row"]')
        for rect in r.findall(f"{ns}rect")
        if rect.get("class", "").startswith("bar-blocked")
    ]
    assert set(blocked) == {"Beta"}, blocked


def case_today_marker(tmp: Path) -> None:
    root = build(tmp, "today", sample_files())
    out = root / "index.html"

    assert run(root, out, "--today", "2026-03-15").returncode == 0
    assert 'class="today"' in out.read_text(encoding="utf-8"), "today line missing inside the span"

    assert run(root, out, "--today", "2030-01-01").returncode == 0
    assert 'class="today"' not in out.read_text(encoding="utf-8"), (
        "today line drawn outside the chart's time span"
    )


def case_dependencies(tmp: Path) -> None:
    files = sample_files()
    files["b.md"] = files["b.md"].replace(
        "owner: marcel", "owner: marcel\ndepends_on: [prj-a]"
    )
    root = build(tmp, "deps", files)
    out = root / "index.html"
    assert run(root, out, "--today", "2026-03-15").returncode == 0
    html = out.read_text(encoding="utf-8")
    svg = svg_of(html)
    deps = svg.findall('.//{http://www.w3.org/2000/svg}path[@class="dep"]')
    assert len(deps) == 1, f"expected one connector, got {len(deps)}"
    assert deps[0].get("marker-end"), "connector needs an arrow head to read as a connector"
    assert 'data-deps="prj-a"' in html, "tooltip must carry the dependency"


def case_escaping(tmp: Path) -> None:
    files = {
        "x.md": PROJECT.format(
            id="prj-x",
            # single-quoted so the fixture is valid YAML but hostile HTML
            title='\'"><script>alert(1)</script>\'',
            status="active",
            start="2026-01-01",
            end="2026-02-01",
        )
    }
    root = build(tmp, "escaping", files)
    out = root / "index.html"
    assert run(root, out).returncode == 0
    html = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html, "title was interpolated unescaped"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&quot;&gt;" in html, "the attribute-breaking quote must be escaped too"
    svg_of(html)  # still well-formed


def case_no_projects(tmp: Path) -> None:
    root = build(tmp, "docsonly", {"handbook.md": DOC})
    out = root / "index.html"
    assert run(root, out).returncode == 0
    html = out.read_text(encoding="utf-8")
    assert 'class="gantt"' not in html, "an empty chart should not be drawn"
    assert "no timeline to draw" in html
    assert "Handbook" in html


def case_updated_from_git(tmp: Path) -> None:
    root = build(tmp, "gitdate", sample_files())
    out = root / "index.html"
    assert run(root, out).returncode == 0
    html = out.read_text(encoding="utf-8")
    assert re.search(r"<td class=\"when\">\d{4}-\d{2}-\d{2}</td>", html), (
        "updated column should carry a git commit date"
    )

    plain = build(tmp, "nogit", sample_files(), commit=False)
    out2 = plain / "index.html"
    assert run(plain, out2).returncode == 0, "a non-git cabinet must still render"


def case_bad_usage(tmp: Path) -> None:
    root = build(tmp, "usage", sample_files())
    out = root / "index.html"

    result = run(root, out, "--today", "not-a-date")
    assert result.returncode == 2, result.stdout + result.stderr

    (root / "cabinet.yml").write_text("schema_version: 99\n", encoding="utf-8")
    result = run(root, out)
    assert result.returncode == 2, result.stdout + result.stderr


def main() -> int:
    cases = [
        case_refuses_invalid_corpus,
        case_renders_documents,
        case_geometry_follows_dates,
        case_today_marker,
        case_dependencies,
        case_escaping,
        case_no_projects,
        case_updated_from_git,
        case_bad_usage,
    ]
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for case in cases:
            case(tmp)
    print(f"PASSED: cabinet generator satisfies {len(cases)} fixture cases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
