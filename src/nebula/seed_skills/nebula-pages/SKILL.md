---
name: nebula-pages
description: How to write and update Nebula pages and records so RL's knowledge stays accurate, linked and easy to find. Use when saving or updating anything in Nebula.
---

# Writing Nebula pages and records

Nebula pages are summaries that sit on top of RL's sources. They must stay true to those sources and be easy for the next person (and the next Claude) to use.

## Before writing
1. `search` for the topic first, and also under its other names (Hindi or English, abbreviations).
2. If a page already exists, `open` it and update that page (pass its `id` and `expected_version`). Don't create a second page about the same thing.

## A good page
- One page per thing: a programme, a state, a partner, a team, a process, a metric, a glossary term.
- Start with two or three sentences saying what it is and why it matters.
- Then short sections with headings. Keep sentences plain; avoid jargon nobody outside the team knows.
- **Every claim cites its source** inline as `nebula:item:<id>` (add `#page=3`, `#slide=5` or `#sheet=Name` when useful). If you can't cite it, mark it "unconfirmed".
- Link related pages with `[[Page title]]`. Create missing pages when they matter, or leave the link so someone sees the gap.
- Put other names in `aliases` (e.g. `AP`, `Andhra Pradesh`, the Hindi name).
- Numbers come from `query` against a dataset, with the period and the metric definition named. Never copy a number from memory or from a snippet.
- Dates: write them in full (16 October 2026), and say when something stopped being true.

## Records
- Decision: what was decided, by whom, when, and why. Set `valid_from`.
- Fact: one checkable statement with its source.
- Metric: the definition in `data` (definition, sql, source_table, unit, owner).
- Correction: always set `supersedes` to the record it corrects; the old one stays in history.
- Lesson: what happened, what we learned, what to do next time.

## Status
Save as draft. Only mark something verified when the person is its owner or a lead and has checked it.
