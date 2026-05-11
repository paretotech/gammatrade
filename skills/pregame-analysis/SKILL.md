---
name: pregame-analysis
description: Analyze a daily pregame trading plan against the local reference cohort + user trade history. Reads from the local SQLite at ~/.gamma/automation/state.db. Returns a structured JSON output that the webapp consumes.
---

# Pregame Analysis Skill

When invoked, read the user's pregame plan and produce a structured analytical read.

## Inputs

- A pregame source: `.docx`, `.txt`, or pasted text
- Local SQLite at `~/.gamma/automation/state.db` with two tables:
  - `reference_trades` — anonymized reference cohort, ~742 closed trades
  - `trade_intents` + `fills` — the user's own trades

## Output

A single JSON object saved to `data/pregames/<YYYY-MM-DD>.analysis.json`:

```json
{
  "day_read": {
    "headline": "...",            // one-sentence punchline
    "regime_assessment": "...",
    "conviction": "high|moderate|low|stay-out",
    "blackouts_or_warnings": ["..."]
  },
  "picks": [
    {
      "ticker": "MU",
      "verdict": "LIKE|WATCH|PASS",
      "reasoning": "...",         // cite n + median ROI from reference or user history
      "key_risks": ["..."],
      "suggested_size": "full|half|quarter|skip"
    }
  ],
  "operational_notes": ["..."]
}
```

## Rules

1. **Lead with the punchline.** No hedging.
2. **Use medians, not means** — option returns are heavily skewed.
3. **Cite n on every cohort.** `n=12` is honest; `win rate 67%` without n is not.
4. **Never invent rules.** Findings → hypotheses, not rules. Don't suggest a "rule" the data doesn't back.
5. **Default risk posture:**
   - Pre-FOMC (Tue → Wed 14:30 ET of FOMC week) and CPI release days = HARD SKIPS. Surface prominently.
   - Familiar tickers only. Unfamiliar → WATCH-ONLY.
   - Index strikes (QQQ/SPX/SPY): ATM or 1-OTM only.
   - Sized-for-zero is the default risk frame.
6. **Verdict semantics:**
   - `LIKE` = soft lean to take if confluence holds
   - `WATCH` = setup OK but waiting for confirmation
   - `PASS` = skip; specific reason required

## SQLite queries to use

Reference cohort stats for a ticker:
```sql
SELECT COUNT(*) AS n,
       AVG(realized_roi) AS mean_roi,
       SUM(CASE WHEN realized_roi > 0 THEN 1 ELSE 0 END) AS wins
FROM reference_trades
WHERE ticker = ? AND status = 'fully_closed';
```

For median, fetch all `realized_roi` values and compute in Python.

User's history on a ticker:
```sql
SELECT i.intent_id, i.ticker, i.status
FROM trade_intents i
WHERE i.ticker = ? AND i.status IN ('filled','closed');
```

Then compute realized P&L from `fills` for each intent_id (sum of SELL fill prices minus SUM of BUY).

## Persistence

After generating the analysis, write to `data/pregames/<YYYY-MM-DD>.analysis.json`. The webapp picks it up automatically and renders at `http://localhost:8765/pregame/<YYYY-MM-DD>`.
