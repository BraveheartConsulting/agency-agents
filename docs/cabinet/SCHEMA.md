# Cabinet schema v1

The contract every `.md` file in the docs cabinet follows, so that three consumers
can read the same corpus without a database:

- **humans** editing files in any Markdown editor
- **agents** reading and proposing changes via PR
- **the generator** rendering the Gantt / index into `index.html`

Draft. Staged here pending creation of the separate docs repo.

## Scope

This covers the YAML front-matter block and the file conventions around it.
It does not cover the generator's rendering rules or the CI workflow — those
live alongside the validator.

## File conventions

**Layout mirrors the Notion export.** Directory structure carries no meaning to
the schema: it exists so the tree stays recognisable to a human who knows the
old workspace. Files may be moved or renamed freely — `id` is the stable
reference, never the path.

- One document per file, `.md` extension, UTF-8, LF endings.
- Filenames are slugs: lowercase, `a-z0-9-`, no Notion page-id suffix.
- Attachments live in an `_assets/` folder beside the file that references them.
- Links between documents are **relative paths** (`../forensics/intake.md`).
  Absolute `notion.so` URLs are a migration failure and are rejected.

## Front-matter

A single YAML block at the very top of the file, delimited by `---`.
Unknown keys are rejected — a typo'd key is a silently dropped field otherwise.

```yaml
---
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
```

### Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | slug | always | Unique repo-wide. **Immutable** — see below. |
| `type` | enum | always | `project` \| `doc` \| `decision` \| `note` |
| `title` | string | always | Single source of truth for the title. |
| `status` | enum | always | `draft` \| `active` \| `blocked` \| `done` \| `archived` |
| `owner` | string | always | Handle, not an email address. |
| `start` | ISO date | `type: project` | `YYYY-MM-DD`. |
| `end` | ISO date | `type: project` | `YYYY-MM-DD`, `>= start`. |
| `progress` | float | optional | `0.0`–`1.0`. `type: project` only. |
| `depends_on` | list of ids | optional | `type: project` only. |
| `tags` | list of slugs | optional | Flat, no hierarchy. |
| `notion_id` | 32-hex | optional | Provenance from migration. Never authored by hand. |

### Rules that carry weight

**`id` is immutable.** Once a file is merged, its `id` never changes — not on
rename, not on move, not on rewrite. Every `depends_on` edge and every agent
reference resolves through it, and changing one breaks them silently rather
than loudly. CI diffs front-matter against the base branch and rejects a
changed `id` on an existing file. Deleting a document and creating a new one
with a new `id` is the supported way to "rename" an identity.

Convention: prefix by type — `prj-`, `doc-`, `dec-`, `note-` — then the path
the document had at creation time, flattened. The prefix is a readability aid,
not a parsed field; `type` is what the schema reads.

**`title` lives only in front-matter.** The body must not open with an `# H1` —
the generator renders the title from front-matter, and a body H1 produces it
twice. Body headings start at `##`.

**`status` and `progress` are independent.** `status` is lifecycle state for
agents; `progress` is a fraction for the timeline bar. Neither derives from the
other. A project can sit at `progress: 0.9` and `status: blocked`, and that
combination is the most informative thing on the chart — do not let either
field imply the other.

**Milestones are zero-length projects.** `start == end` renders as a diamond
rather than a bar. No separate `type` for it.

**No `updated` field.** Hand-maintained timestamps rot within weeks. The
generator takes last-modified from `git log` for the file, which is correct by
construction and cannot drift.

## Body

Plain CommonMark below the front-matter. No required structure — the schema
constrains metadata, not prose. Tables, code blocks and images pass through
untouched.

## Validation

CI enforces these; the numbering is the validator's rule ids.

| id | Rule |
|---|---|
| V1 | Front-matter parses as YAML and contains no unknown keys. |
| V2 | All `type`-required fields are present and correctly typed. |
| V3 | `id` matches `^(prj\|doc\|dec\|note)-[a-z0-9]+(-[a-z0-9]+)*$` and is unique repo-wide. |
| V4 | No existing file's `id` changed relative to the base branch. |
| V5 | Every `depends_on` entry resolves to an `id` present in the repo. |
| V6 | The `depends_on` graph is acyclic. |
| V7 | `start`/`end` are ISO dates and `end >= start`. |
| V8 | Relative `.md` links resolve to existing files. |
| V9 | No `notion.so` URL appears anywhere in the corpus. |

V6 exists because a dependency cycle makes the generator loop rather than
error — it has to be caught upstream of rendering.

V9 doubles as the acceptance test for migration: the export's internal links
are rewritten against a `{notion page id → new path}` map, and a clean V9 run
is the proof that the link graph survived.

## Changing this schema

`schema_version` lives in `cabinet.yml` at the repo root. Adding an optional
field is a minor change and needs no version bump. Adding an enum value,
making a field required, or changing a rule's meaning bumps the version and
requires the validator to accept both versions for one migration cycle.
