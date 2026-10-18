---
name: usage-audit
description: Use when the user asks about token use, Claude Code cost, ccusage, whether the token-optimisation changes worked, or the one-week usage check. Runs ccusage and compares it with the 18 Oct 2026 baseline.
---

# Usage audit

Compare current Claude Code spend with the baseline taken before the token-optimisation changes (claude-config PRs 31 to 34, merged 18 Oct 2026). Run the commands yourself; do not ask the user to paste output.

## Steps

1. Pull the data. Redirect to a file, because `npx` prints notices that break `jq`.

```bash
npx -y ccusage@latest session --json --since 20261018 2>/dev/null > "$TMPDIR/after.json"
npx -y ccusage@latest session --json --since 20261008 2>/dev/null > "$TMPDIR/before.json"
```

2. Summarise each file. Session id is `.period`; the date is `.metadata.lastActivity`.

```bash
jq '{sessions: (.session|length), cost: .totals.totalCost, cacheRead: .totals.cacheReadTokens, output: .totals.outputTokens,
     top3: ([.session[].totalCost]|sort|reverse|.[0:3]|add),
     top10: ([.session[].totalCost]|sort|reverse|.[0:10]|add),
     models: ([.session[].modelsUsed[]]|group_by(.)|map({(.[0]): length})|add)}' "$TMPDIR/after.json"
```

3. List the ten dearest sessions, with their date and models:

```bash
jq -r '.session|sort_by(-.totalCost)|.[0:10][]|"\(.totalCost*100|round/100)\t\(.period[0:8])\t\(.metadata.lastActivity[0:10])\t\(.modelsUsed|join(","))"' "$TMPDIR/after.json"
```

4. Report cost per day (cost divided by days covered), top 3 and top 10 share of total, cache-read tokens per session, and model mix. State clearly which numbers are verified and which are not.

## Baseline (Claude-Code VM, before the change)

- All time to 18 Oct: $701.74 across about 330 sessions.
- Since 8 Oct (run 18 Oct, mostly pre-change): 180 sessions, $321.27, cacheReadTokens 898224799, outputTokens 2954825. Opus 5.5 in 16 sessions, Sonnet 5.5 in 166. That is about $29 a day over 11 days.
- Concentration: top 10 sessions were $391.06 (about 56% of the all-time total), top 3 were $206.69 (about 29%). The dearest, cff7bbf5, was $94.07 and read about 291M cache tokens.
- Fresh-session context (`/context`, before typing): 43.1k of 1M. Skills 9.2k, system tools 14.1k, system prompt 8.1k, MCP tools 6.3k, memory files 3k.

## How to read the result

- Spend is driven by a few very long sessions. Compare top-3 share and cache-read tokens per session first; a lower cost per day with the same top-3 share means the habit has not changed.
- The changes under test: default Sonnet with an Opus advisor, compaction window 300000, github and context7 MCP denied, trimmed plugins, split memory, context warnings at 120k, 180k and 225k tokens.
- A week of data is weak evidence. Say so, and do not claim a saving the numbers do not show.
- ccusage figures are list-price estimates, not what the Pro plan charges.
