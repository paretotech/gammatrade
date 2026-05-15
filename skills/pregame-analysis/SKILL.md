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

The webapp's pregame view reads cached analysis from
`~/.gamma/automation/analyses/<YYYY-MM-DD>.json`. The file must use the
wrapper shape below — the inner `analysis` object is what gets rendered
in the UI; the outer fields tell the webapp whether the run succeeded
and when it ran.

```json
{
  "status": "ok",                                // "ok" | "no_api_key" | "error"
  "analysis": {
    "day_read": {
      "headline": "...",                         // one-sentence punchline
      "regime_assessment": "...",
      "conviction": "high|moderate|low|stay-out",
      "blackouts_or_warnings": ["..."]
    },
    "picks": [
      {
        "ticker": "MU",
        "verdict": "LIKE|WATCH|PASS",
        "reasoning": "...",                      // cite n + median ROI from reference or user history
        "key_risks": ["..."],
        "suggested_size": "full|half|quarter|skip"
      }
    ],
    "operational_notes": ["..."]
  },
  "error": null,
  "model": "claude-code-session (manual /gamma-pregame run)",
  "ts": "2026-05-11T09:15:00",                   // ISO-8601, seconds precision
  "cached": false
}
```

When `status` is not `"ok"`, set `analysis` to `null` and put the reason
in `error`. The webapp renders an inline warning instead of the analysis
panel in that case.

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

**Units gotcha:** `reference_trades.realized_roi` is already stored in
percent units — a value of `37.27` means a 37.27% return, not 3727%.
Do NOT multiply by 100 again when formatting. Compare to user trades,
where the equivalent value is computed from fills as a fraction
(`(avg_exit / avg_entry) - 1`) and DOES need ×100 for display.

For median, fetch all `realized_roi` values and compute in Python.

User's history on a ticker:
```sql
SELECT i.intent_id, i.ticker, i.status
FROM trade_intents i
WHERE i.ticker = ? AND i.status IN ('filled','closed');
```

Then compute realized P&L from `fills` for each intent_id (sum of SELL fill prices minus SUM of BUY).

## Persistence

After generating the analysis, write the wrapped JSON shape (see Output
section) to `~/.gamma/automation/analyses/<YYYY-MM-DD>.json`. Create
the directory if it doesn't exist. The webapp picks it up automatically
and renders at `http://localhost:8765/pregame/<YYYY-MM-DD>`.

Do NOT write to `data/pregames/<YYYY-MM-DD>.analysis.json` — earlier
versions of this skill specified that path, but the webapp's
`analysis.get_cached()` only reads from the `~/.gamma/automation/analyses/`
location. An analysis written to `data/pregames/` will not appear in the
UI.
