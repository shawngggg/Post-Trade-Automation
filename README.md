# Post-Trade Back-Office Automation Engine

A Python engine that reduces manual touchpoints in a securities **post-trade
operations** workflow by triaging every trade through a **three-tier control
model** — automating the safe work, keeping people on the decisions that carry
risk, and logging everything for audit.

> Built to demonstrate responsible automation in a control-heavy financial
> operations environment: deterministic auto-fixes are applied and logged,
> judgment calls are *suggested* for human approval, and money-movement
> decisions are never automated.

---

## The three-tier model

| Tier | Name | What it does | Acts automatically? |
|------|------|--------------|---------------------|
| **1** | `AUTO_FIX` | Safe, deterministic, reversible clerical corrections (formatting, derive settlement date, sub-cent rounding) | **Yes** — every change logged with before/after |
| **2** | `AI_SUGGEST` | Judgment calls (amount breaks, missing fail reasons, unmatched/DK, aging fails). Engine proposes a resolution | **No** — routed to a human to approve |
| **3** | `HUMAN_ONLY` | Risk-bearing decisions — authorizing settlement / money movement | **Never** — engine only builds the review queue |

This mirrors how automation *should* enter a controlled operation: let the
machine handle the safe, high-volume, low-judgment work; surface everything
else to a person with a recommended action; and keep an audit trail on all of it.

---

## What it detects & fixes

**Tier 1 — auto-fixed (logged, reversible):**
- Inconsistent `buy_sell` / `currency` / `product` casing & whitespace → normalized
- Missing `settle_date` → derived (T+1 business days by product convention)
- Sub-cent `settlement_amount` rounding drift → reconciled to `qty × price`

**Tier 2 — suggested for human approval:**
- Material `settlement_amount` break vs `qty × price`
- Failed trade missing a `fail_reason` (engine proposes the likely reason)
- Unmatched / DK trades, missing SSI pre-settlement, aging pending fails
- Price outliers (per-product z-score), zero/negative qty or price, duplicate trade IDs

**Tier 3 — human-only:**
- Ready-to-settle trades routed for settlement authorization (never auto-released)

---

## Results (on 4,015 synthetic trades)

- **100% recall** against an injected ground-truth defect set (597/600 caught;
  the few "misses" are rounding drifts correctly reclassified as material breaks).
- **460** clerical corrections applied automatically and logged.
- **~27%** of all touched trades cleared with **zero human effort**; the rest
  routed with a recommended action.

Run `python validate.py` to reproduce the recall table.

---

## Quick start

```bash
# no third-party dependencies — Python 3.9+ standard library only
python generate_exceptions.py          # inject controlled, labeled defects
python post_trade_engine.py            # run the engine
python validate.py                     # check detections vs ground truth
```

Optional flags:

```bash
python post_trade_engine.py --input trades_dirty.csv --asof 2026-06-19
```

---

## Outputs

| File | Contents |
|------|----------|
| `cleaned_trades.csv` | Trades with Tier-1 fixes applied + a `processing_status` per trade |
| `exceptions.csv` | Every Tier-2/Tier-3 item needing a human, with tier, severity, and suggested action |
| `audit_log.jsonl` | One record per action (auto-fix with before/after, suggestion, or routing), timestamped |
| `run_summary.txt` | The run summary printed to console |

---

## Design notes

- **Audit-first.** Every automated change writes an `Action` record with the old
  and new value, so nothing the engine does is a black box and every fix is
  reversible.
- **Model-pluggable suggestion layer.** Tier-2 suggestions ship as a transparent
  rules-based heuristic (`suggest_resolution`) so the tool runs anywhere with no
  API key and is fully reproducible. An LLM can be dropped in behind the same
  `(row, check) -> str` interface — the human-in-the-loop contract is unchanged,
  because the output is still only a *suggestion* a person approves.
- **Separation of duties by design.** The engine is structurally incapable of
  authorizing settlement; that decision is Tier 3 and only ever queued for a human.

---

## Files

| File | Purpose |
|------|---------|
| `generate_exceptions.py` | Injects controlled, labeled defects into the clean blotter |
| `post_trade_engine.py` | The engine — three-tier processor + audit log |
| `validate.py` | Recall vs ground truth |
| `trades_raw.csv` | Clean synthetic blotter (4,000 trades, 4 products) |
| `trades_dirty.csv` | Raw + injected defects (engine input) |
| `injected_truth.csv` | Ground-truth labels for validation |

*Data is fully synthetic. No real trades, counterparties, or PII.*
