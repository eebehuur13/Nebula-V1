---
name: nebula-upkeep
description: Weekly upkeep of a team's Nebula knowledge, for team leads and page owners. Use when asked to "do the Nebula upkeep", review stale pages, or as a weekly scheduled task.
---

# Weekly Nebula upkeep

Run this for one team (ask which, if the person leads several). It is meant to run weekly, for example as a Claude scheduled task.

1. **Review queue.** Call `admin` with action `review`. Note stale pages, open proposals, draft records and draft skills.
2. **What changed.** Call `admin` with action `recent` (default: the last 7 days) to list sources that were added or changed.
3. **Update pages.** For each changed source that matters, `open` it, find the pages it affects (`search`, then `open` the page and check its links), and update those pages following the `nebula-pages` skill. Cite the new sources. Never delete history; supersede outdated records with corrections.
4. **Stale pages.** For each page past its review date, check it against its sources. If it is still right, say so to the lead so they can verify it again. If it isn't, update it.
5. **Contradictions.** If two sources or records disagree, don't pick one silently. List them for the lead with both citations.
6. **Proposals and drafts.** Summarise open proposals and drafts and ask the lead which to accept or verify (`admin` decide_proposal / verify). Only act on their answer.
7. **Report.** Finish with a short summary: pages updated (with ids), pages that need the lead's decision, contradictions found, gaps (questions people asked that Nebula couldn't answer, if known).
