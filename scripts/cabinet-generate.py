#!/usr/bin/env python3
"""Render the docs cabinet as a self-contained index.html with a Gantt timeline.

Reads the same front-matter contract the validator enforces (docs/cabinet/SCHEMA.md),
refuses to render a corpus that violates it, and writes one HTML file with no
external assets: a project timeline as inline SVG plus a table view of every
document.

`updated` is derived from git history, never from front-matter — see SCHEMA.md.

Usage: cabinet-generate.py [--root DIR] [--out index.html] [--title NAME]
Exit codes: 0 written, 1 corpus invalid, 2 bad usage or unreadable config.
"""
from __future__ import annotations

import argparse
import datetime
import html
import importlib.util
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "scripts" / "cabinet-validate.py"

# Geometry. Bars stay under the 24px mark cap; rows leave the rest as air.
LABEL_W = 300
CHART_W = 760
ROW_H = 40
BAR_H = 20
AXIS_H = 44
PAD_BOTTOM = 20
MILESTONE_R = 7

# Two bar classes, both validated against each surface with the dataviz
# validator (all checks pass, light and dark): slot-1 blue for a project that is
# running, the critical status token for one that is blocked. Red means blocked
# and nothing else. Every row also carries a text status chip, so the colour is
# never the only channel.
BAR_BLOCKED = "blocked"
BAR_NORMAL = "normal"

STATUS_TONE = {
    "blocked": "critical",
    "active": "good",
    "draft": "muted",
    "done": "muted",
    "archived": "muted",
}


def load_validator():
    spec = importlib.util.spec_from_file_location("cabinet_validate", VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules, so register first
    sys.modules["cabinet_validate"] = module
    spec.loader.exec_module(module)
    return module


validator = load_validator()


@dataclass
class Row:
    doc_id: str
    title: str
    status: str
    owner: str
    doc_type: str
    rel: str
    start: datetime.date | None = None
    end: datetime.date | None = None
    progress: float | None = None
    depends_on: list[str] = field(default_factory=list)
    updated: str = ""

    @property
    def is_milestone(self) -> bool:
        return self.start is not None and self.start == self.end


def git_updated_map(root: Path) -> dict[str, str]:
    """Last commit date per path, in one pass. Empty when this is not a git tree."""
    result = subprocess.run(
        ["git", "log", "--date=short", "--pretty=format:%x01%cd", "--name-only"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    updated: dict[str, str] = {}
    current = ""
    for line in result.stdout.splitlines():
        if line.startswith("\x01"):
            current = line[1:].strip()
        elif line.strip() and current:
            updated.setdefault(line.strip(), current)
    return updated


def build_rows(docs, root: Path, config) -> list[Row]:
    prefix = config.content_root.relative_to(root).as_posix()
    prefix = "" if prefix == "." else f"{prefix}/"
    updated = git_updated_map(root)

    rows: list[Row] = []
    for doc in docs:
        # collect() only reads the text; front-matter is parsed by the check.
        # The corpus already validated, so the violations here are discarded.
        validator.check_front_matter(doc, [])
        meta = doc.meta or {}
        row = Row(
            doc_id=str(meta.get("id", "")),
            title=str(meta.get("title", "")),
            status=str(meta.get("status", "")),
            owner=str(meta.get("owner", "")),
            doc_type=str(meta.get("type", "")),
            rel=doc.rel,
            start=validator.as_date(meta.get("start")),
            end=validator.as_date(meta.get("end")),
            depends_on=[str(d) for d in (meta.get("depends_on") or [])],
            updated=updated.get(f"{prefix}{doc.rel}", ""),
        )
        if isinstance(meta.get("progress"), (int, float)) and not isinstance(
            meta.get("progress"), bool
        ):
            row.progress = float(meta["progress"])
        rows.append(row)
    return rows


def month_starts(first: datetime.date, last: datetime.date) -> list[datetime.date]:
    out = []
    cursor = datetime.date(first.year, first.month, 1)
    while cursor <= last:
        out.append(cursor)
        cursor = (
            datetime.date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else datetime.date(cursor.year, cursor.month + 1, 1)
        )
    return out


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def fmt_date(d: datetime.date | None) -> str:
    return d.strftime("%-d %b %Y") if d else "—"


def render_timeline(projects: list[Row], today: datetime.date) -> str:
    """Inline SVG Gantt. All geometry is computed here; the page ships no layout JS."""
    if not projects:
        return (
            '<p class="empty">No documents with <code>type: project</code> yet, '
            "so there is no timeline to draw.</p>"
        )

    domain_start = min(p.start for p in projects)
    domain_end = max(p.end for p in projects)
    months = month_starts(domain_start, domain_end)
    axis_start = months[0]
    axis_end = (
        datetime.date(domain_end.year + 1, 1, 1)
        if domain_end.month == 12
        else datetime.date(domain_end.year, domain_end.month + 1, 1)
    )
    span = max((axis_end - axis_start).days, 1)

    def x_of(d: datetime.date) -> float:
        return LABEL_W + (d - axis_start).days / span * CHART_W

    height = AXIS_H + len(projects) * ROW_H + PAD_BOTTOM
    width = LABEL_W + CHART_W
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="gantt" '
        f'viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" '
        f'aria-label="Project timeline, {len(projects)} projects from '
        f'{esc(fmt_date(domain_start))} to {esc(fmt_date(domain_end))}">',
        '<defs><marker id="dep-arrow" viewBox="0 0 6 6" refX="5" refY="3" '
        'markerWidth="5" markerHeight="5" orient="auto-start-reverse">'
        '<path class="dep-head" d="M 0 0 L 6 3 L 0 6 z" /></marker></defs>',
    ]

    # Month gridlines: solid hairlines, one step off the surface.
    for month in months:
        mx = round(x_of(month), 1)
        parts.append(
            f'<line class="grid" x1="{mx}" y1="{AXIS_H - 14}" x2="{mx}" '
            f'y2="{height - PAD_BOTTOM}" />'
        )
        parts.append(
            f'<text class="axis" x="{mx + 6}" y="{AXIS_H - 20}">'
            f'{esc(month.strftime("%b %Y"))}</text>'
        )
    parts.append(
        f'<line class="axis-rule" x1="{LABEL_W}" y1="{AXIS_H - 14}" '
        f'x2="{width}" y2="{AXIS_H - 14}" />'
    )

    row_y = {p.doc_id: AXIS_H + i * ROW_H for i, p in enumerate(projects)}

    # Dependency connectors, drawn under the bars in the recessive grid colour.
    by_id = {p.doc_id: p for p in projects}
    for project in projects:
        for dep_id in project.depends_on:
            dep = by_id.get(dep_id)
            if dep is None:
                continue
            x1 = round(x_of(dep.end), 1)
            y1 = round(row_y[dep.doc_id] + BAR_H / 2, 1)
            x2 = round(x_of(project.start), 1)
            y2 = round(row_y[project.doc_id] + BAR_H / 2, 1)
            # Run the horizontal leg through the gutter beside the target row,
            # never across the bars. A dependent that starts before its
            # predecessor ends routes backwards, which is the point: the
            # overlap is visible rather than hidden.
            below = row_y[project.doc_id] > row_y[dep.doc_id]
            gate = round(
                row_y[project.doc_id] - 9 if below else row_y[project.doc_id] + BAR_H + 9,
                1,
            )
            approach = round(x2 - 10, 1)
            parts.append(
                f'<path class="dep" d="M {x1} {y1} H {round(x1 + 8, 1)} V {gate} '
                f'H {approach} V {y2} H {x2}" marker-end="url(#dep-arrow)" />'
            )

    for project in projects:
        y = row_y[project.doc_id]
        tone = STATUS_TONE.get(project.status, "muted")
        bar_class = BAR_BLOCKED if project.status == "blocked" else BAR_NORMAL
        dates = f"{fmt_date(project.start)} – {fmt_date(project.end)}"
        pct = "not reported" if project.progress is None else f"{round(project.progress * 100)}%"
        deps = ", ".join(project.depends_on) if project.depends_on else "none"

        parts.append(
            f'<g class="row" tabindex="0" role="group" '
            f'data-title="{esc(project.title)}" data-status="{esc(project.status)}" '
            f'data-dates="{esc(dates)}" data-progress="{esc(pct)}" '
            f'data-owner="{esc(project.owner)}" data-deps="{esc(deps)}" '
            f'aria-label="{esc(project.title)}, {esc(project.status)}, {esc(dates)}, '
            f'progress {esc(pct)}">'
        )
        # Hit target spans the whole row, so the pointer never has to find a 20px bar.
        parts.append(
            f'<rect class="hit" x="0" y="{y - (ROW_H - BAR_H) / 2}" width="{width}" '
            f'height="{ROW_H}" />'
        )
        parts.append(
            f'<text class="row-title" x="0" y="{y + 4}">{esc(project.title)}</text>'
        )
        parts.append(
            f'<circle class="dot tone-{tone}" cx="4" cy="{y + 14}" r="4" />'
        )
        parts.append(
            f'<text class="row-meta" x="14" y="{y + 18}">'
            f'{esc(project.status)} · {esc(dates)}</text>'
        )

        if project.is_milestone:
            cx = round(x_of(project.start), 1)
            cy = round(y + BAR_H / 2, 1)
            parts.append(
                f'<rect class="bar-{bar_class} milestone" x="{cx - MILESTONE_R}" '
                f'y="{cy - MILESTONE_R}" width="{MILESTONE_R * 2}" '
                f'height="{MILESTONE_R * 2}" rx="2" '
                f'transform="rotate(45 {cx} {cy})" />'
            )
        else:
            bx = round(x_of(project.start), 1)
            bw = max(round(x_of(project.end) - bx, 1), 3)
            if project.progress is None:
                parts.append(
                    f'<rect class="bar-{bar_class}" x="{bx}" y="{y}" width="{bw}" '
                    f'height="{BAR_H}" rx="4" />'
                )
            else:
                # Meter: track is the same hue washed back, fill carries progress.
                parts.append(
                    f'<rect class="bar-{bar_class} track" x="{bx}" y="{y}" '
                    f'width="{bw}" height="{BAR_H}" rx="4" />'
                )
                fill_w = max(round(bw * min(max(project.progress, 0.0), 1.0), 1), 0)
                if fill_w > 0:
                    parts.append(
                        f'<rect class="bar-{bar_class}" x="{bx}" y="{y}" '
                        f'width="{fill_w}" height="{BAR_H}" rx="4" />'
                    )
        parts.append("</g>")

    if axis_start <= today <= axis_end:
        tx = round(x_of(today), 1)
        parts.append(
            f'<line class="today" x1="{tx}" y1="{AXIS_H - 14}" x2="{tx}" '
            f'y2="{height - PAD_BOTTOM}" />'
        )
        parts.append(f'<text class="today-label" x="{tx + 5}" y="{height - 6}">today</text>')

    parts.append("</svg>")
    return "".join(parts)


def render_table(rows: list[Row]) -> str:
    head = (
        "<thead><tr><th>Title</th><th>Type</th><th>Status</th><th>Owner</th>"
        "<th class=when>Start</th><th class=when>End</th><th class=num>Progress</th>"
        "<th>Depends on</th><th class=when>Updated</th><th>Path</th></tr></thead>"
    )
    body = []
    for row in sorted(rows, key=lambda r: (r.doc_type, r.title.lower())):
        tone = STATUS_TONE.get(row.status, "muted")
        progress = "—" if row.progress is None else f"{round(row.progress * 100)}%"
        body.append(
            "<tr>"
            f"<td>{esc(row.title)}</td>"
            f"<td>{esc(row.doc_type)}</td>"
            f'<td><span class="dot-inline tone-{tone}"></span>{esc(row.status)}</td>'
            f"<td>{esc(row.owner)}</td>"
            f'<td class="when">{esc(fmt_date(row.start))}</td>'
            f'<td class="when">{esc(fmt_date(row.end))}</td>'
            f'<td class="num">{esc(progress)}</td>'
            f"<td>{esc(', '.join(row.depends_on) if row.depends_on else '—')}</td>"
            f'<td class="when">{esc(row.updated or "—")}</td>'
            f"<td><a href=\"{esc(row.rel)}\"><code>{esc(row.rel)}</code></a></td>"
            "</tr>"
        )
    return f"<table>{head}<tbody>{''.join(body)}</tbody></table>"


STYLE = """
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface-1: #fcfcfb;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --border: rgba(11, 11, 11, 0.10);
  --series-1: #2a78d6;
  --status-good: #0ca30c;
  --status-critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface-1: #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255, 255, 255, 0.10);
    --series-1: #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d;
  --surface-1: #1a1a19;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --border: rgba(255, 255, 255, 0.10);
  --series-1: #3987e5;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 32px 16px 64px;
  background: var(--page);
  color: var(--text-primary);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 1100px; margin: 0 auto; }
header { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
h1 { font-size: 22px; font-weight: 600; margin: 0; }
h2 { font-size: 15px; font-weight: 600; margin: 40px 0 4px; }
.sub { color: var(--text-secondary); font-size: 13px; margin: 4px 0 0; }
#theme {
  margin-left: auto; font: inherit; font-size: 13px; cursor: pointer;
  background: var(--surface-1); color: var(--text-secondary);
  border: 1px solid var(--border); border-radius: 8px; padding: 5px 11px;
}

.stats { display: flex; flex-wrap: wrap; gap: 12px; margin: 24px 0 0; padding: 0; list-style: none; }
.stats li {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 18px; min-width: 128px;
}
.stats .label { display: block; font-size: 12px; color: var(--text-secondary); }
.stats .value { display: block; font-size: 26px; font-weight: 600; margin-top: 2px; }

.card {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px; margin-top: 12px; overflow-x: auto;
}
.legend { display: flex; gap: 18px; align-items: center; margin: 0 0 12px; padding: 0; list-style: none; font-size: 13px; color: var(--text-secondary); }
.legend li { display: flex; gap: 7px; align-items: center; }
.key { width: 14px; height: 10px; border-radius: 3px; display: inline-block; }
.key-normal { background: var(--series-1); }
.key-blocked { background: var(--status-critical); }

.gantt { display: block; max-width: 100%; height: auto; }
.gantt .grid { stroke: var(--grid); stroke-width: 1; }
.gantt .axis-rule { stroke: var(--axis); stroke-width: 1; }
.gantt .dep { stroke: var(--axis); stroke-width: 1; fill: none; }
.gantt .dep-head { fill: var(--axis); stroke: none; }
.gantt .today { stroke: var(--text-muted); stroke-width: 1; }
.gantt text { font: 12px system-ui, -apple-system, "Segoe UI", sans-serif; }
.gantt .axis, .gantt .today-label { fill: var(--text-muted); font-size: 11px; }
.gantt .row-title { fill: var(--text-primary); font-size: 13px; }
.gantt .row-meta { fill: var(--text-secondary); font-size: 11px; }
.gantt .bar-normal { fill: var(--series-1); }
.gantt .bar-blocked { fill: var(--status-critical); }
.gantt .track { opacity: 0.18; }
.gantt .hit { fill: transparent; }
.gantt .row { outline: none; }
.gantt .row:hover .hit, .gantt .row:focus-visible .hit { fill: var(--grid); opacity: 0.5; }

.dot.tone-good { fill: var(--status-good); }
.dot.tone-critical { fill: var(--status-critical); }
.dot.tone-muted { fill: var(--text-muted); }
.dot-inline {
  width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 7px;
  vertical-align: 1px; background: var(--text-muted);
}
.dot-inline.tone-good { background: var(--status-good); }
.dot-inline.tone-critical { background: var(--status-critical); }

table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 7px 12px 7px 0; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--text-secondary); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; padding-right: 18px; }
td.when, th.when { white-space: nowrap; }
code { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--text-secondary); }
a { color: inherit; }
.empty { color: var(--text-secondary); }

#tip {
  position: fixed; z-index: 10; pointer-events: none; opacity: 0;
  transition: opacity .1s; max-width: 280px;
  background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 10px;
  padding: 9px 12px; font-size: 12px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, .18);
}
#tip .tip-title { font-weight: 600; margin-bottom: 4px; }
#tip div { color: var(--text-secondary); }
#tip b { color: var(--text-primary); font-weight: 600; }
@media (prefers-reduced-motion: reduce) { #tip { transition: none; } }
"""

SCRIPT = """
(function () {
  var root = document.documentElement;
  var btn = document.getElementById('theme');
  try {
    var saved = localStorage.getItem('cabinet-theme');
    if (saved) root.setAttribute('data-theme', saved);
  } catch (e) {}
  btn.addEventListener('click', function () {
    var dark = root.getAttribute('data-theme') === 'dark'
      || (!root.getAttribute('data-theme')
          && window.matchMedia('(prefers-color-scheme: dark)').matches);
    var next = dark ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('cabinet-theme', next); } catch (e) {}
  });

  var tip = document.getElementById('tip');
  function line(label, value) {
    var d = document.createElement('div');
    d.appendChild(document.createTextNode(label + ' '));
    var b = document.createElement('b');
    b.textContent = value;
    d.appendChild(b);
    return d;
  }
  function show(row, x, y) {
    tip.textContent = '';
    var h = document.createElement('div');
    h.className = 'tip-title';
    h.textContent = row.dataset.title;
    tip.appendChild(h);
    tip.appendChild(line('status', row.dataset.status));
    tip.appendChild(line('dates', row.dataset.dates));
    tip.appendChild(line('progress', row.dataset.progress));
    tip.appendChild(line('owner', row.dataset.owner));
    tip.appendChild(line('depends on', row.dataset.deps));
    tip.style.opacity = '1';
    var box = tip.getBoundingClientRect();
    var left = Math.min(x + 14, window.innerWidth - box.width - 8);
    var top = Math.min(y + 14, window.innerHeight - box.height - 8);
    tip.style.left = Math.max(8, left) + 'px';
    tip.style.top = Math.max(8, top) + 'px';
  }
  function hide() { tip.style.opacity = '0'; }

  Array.prototype.forEach.call(document.querySelectorAll('.gantt .row'), function (row) {
    row.addEventListener('pointermove', function (e) { show(row, e.clientX, e.clientY); });
    row.addEventListener('pointerleave', hide);
    row.addEventListener('focus', function () {
      var b = row.getBoundingClientRect();
      show(row, b.left + 40, b.top);
    });
    row.addEventListener('blur', hide);
  });
})();
"""


def render_page(title: str, rows: list[Row], today: datetime.date) -> str:
    projects = sorted(
        (r for r in rows if r.doc_type == "project" and r.start and r.end),
        key=lambda r: (r.start, r.title.lower()),
    )
    active = sum(1 for r in projects if r.status == "active")
    blocked = sum(1 for r in projects if r.status == "blocked")

    stats = (
        '<ul class="stats">'
        f'<li><span class="label">Documents</span><span class="value">{len(rows)}</span></li>'
        f'<li><span class="label">Projects</span><span class="value">{len(projects)}</span></li>'
        f'<li><span class="label">Active</span><span class="value">{active}</span></li>'
        f'<li><span class="label">Blocked</span><span class="value">{blocked}</span></li>'
        "</ul>"
    )
    legend = (
        '<ul class="legend">'
        '<li><span class="key key-normal"></span>On track</li>'
        '<li><span class="key key-blocked"></span>Blocked</li>'
        "</ul>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>{esc(title)}</h1>
      <p class="sub">Generated {esc(today.isoformat())} · {len(rows)} documents</p>
    </div>
    <button id="theme" type="button">Toggle theme</button>
  </header>

  {stats}

  <h2>Project timeline</h2>
  <p class="sub">Bars span start to end. A diamond is a milestone (start equals end).
  Where progress is reported, the solid part of the bar is the share complete.</p>
  <div class="card">
    {legend}
    {render_timeline(projects, today)}
  </div>

  <h2>All documents</h2>
  <p class="sub">Every value in the timeline, readable without hovering.</p>
  <div class="card">
    {render_table(rows)}
  </div>
</main>
<div id="tip" role="status" aria-live="polite"></div>
<script>{SCRIPT}</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument("--config", type=Path, default=None, help="path to cabinet.yml")
    parser.add_argument("--out", type=Path, default=Path("index.html"), help="output file")
    parser.add_argument("--title", default="Cabinet", help="page title")
    parser.add_argument(
        "--today",
        default=None,
        help="ISO date to mark as today (default: the system date)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    try:
        config = validator.load_config(root, args.config)
    except Exception as exc:  # noqa: BLE001 — surfaced verbatim to the operator
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    if not config.content_root.is_dir():
        print(f"FAILED: content root {config.content_root} does not exist", file=sys.stderr)
        return 2

    try:
        today = (
            datetime.date.fromisoformat(args.today) if args.today else datetime.date.today()
        )
    except ValueError:
        print(f"FAILED: --today {args.today!r} is not an ISO date", file=sys.stderr)
        return 2

    violations, _ = validator.validate(root, None, config)
    if violations:
        for violation in violations:
            print(violation.render())
        print(
            f"\nFAILED: refusing to generate from {len(violations)} schema violation(s). "
            "Run cabinet-validate.py and fix them first.",
            file=sys.stderr,
        )
        return 1

    rows = build_rows(validator.collect(config), root, config)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_page(args.title, rows, today), encoding="utf-8")
    print(f"PASSED: wrote {args.out} from {len(rows)} document(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
