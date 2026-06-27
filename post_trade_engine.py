"""
post_trade_engine.py
====================
Post-Trade Back-Office Automation Engine

Reduces manual touchpoints in a post-trade operations workflow by triaging every
trade through a THREE-TIER control model:

    TIER 1  AUTO-FIX      Safe, deterministic, reversible, clerical corrections.
                          Applied automatically. Every change is logged with a
                          before/after value so it is fully reversible & auditable.

    TIER 2  AI-SUGGEST    Judgment calls. The engine proposes a resolution but does
                          NOT act — the item is routed to a human with a suggested
                          action. (The suggestion function is model-pluggable: a
                          rules-based heuristic ships by default; an LLM can be
                          dropped in behind the same interface — see suggest_resolution.)

    TIER 3  HUMAN-ONLY    Risk-bearing decisions (e.g., authorizing settlement /
                          money movement). The engine never acts; it only assembles
                          the review queue.

Design principle: automate the safe work, keep people on the decisions that carry
risk, and log everything.

Outputs (written to output/):
    cleaned_trades.csv    trades with Tier-1 auto-fixes applied + processing_status
    exceptions.csv        every item needing human attention (tier, severity, suggestion)
    audit_log.jsonl       one JSON record per action taken (auto-fix / suggest / route)
    run_summary.txt       human-readable summary printed at the end of a run

Usage:
    python src/post_trade_engine.py --input data/trades_dirty.csv --asof 2026-06-19
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ----- canonical reference data -------------------------------------------------
CANON_SIDE = {"BUY": "Buy", "SELL": "Sell"}
CANON_PRODUCT = {"EQUITY": "Equity", "FIXED INCOME": "Fixed Income",
                 "MUTUAL FUND": "Mutual Fund", "OPTION": "Option"}
SETTLE_OFFSET_DAYS = {            # business-day T+N by product (US conventions)
    "Equity": 1, "Option": 1, "Fixed Income": 1, "Mutual Fund": 1,
}
ROUNDING_TOLERANCE = 0.01         # <= 1 cent amount drift is auto-fixable rounding

TIER_AUTOFIX = "AUTO_FIX"
TIER_SUGGEST = "AI_SUGGEST"
TIER_HUMAN = "HUMAN_ONLY"


# ----- audit record -------------------------------------------------------------
@dataclass
class Action:
    trade_id: str
    tier: str
    check: str
    severity: str
    field_name: str = ""
    old_value: str = ""
    new_value: str = ""
    detail: str = ""
    suggestion: str = ""
    timestamp: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())


# ----- helpers ------------------------------------------------------------------
def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def add_business_days(start: dt.date, n: int) -> dt.date:
    d, added = start, 0
    while added < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:        # Mon-Fri
            added += 1
    return d


def to_float(s):
    try:
        return float(str(s).strip())
    except (ValueError, AttributeError):
        return None


# ----- the engine ---------------------------------------------------------------
class PostTradeEngine:
    def __init__(self, asof: dt.date):
        self.asof = asof
        self.actions: list[Action] = []

    def log(self, action: Action):
        self.actions.append(action)

    # ---- TIER 2 suggestion layer (model-pluggable) -----------------------------
    def suggest_resolution(self, row: dict, check: str) -> str:
        """Return a proposed resolution for a judgment-call exception.

        Ships as a transparent rules-based heuristic so the tool runs anywhere with
        no API key and is fully reproducible. To use an LLM instead, implement the
        same signature (row, check) -> str and call it here; the human-in-the-loop
        contract is unchanged because the output is still only a *suggestion*.
        """
        if check == "missing_fail_reason":
            ms, ssi = row.get("match_status", ""), row.get("ssi_present", "")
            if ms in ("Unmatched", "DK"):
                return "Likely 'DK / Unmatched' \u2014 confirm with counterparty, then repair match."
            if ssi == "N":
                return "Likely 'SSI Mismatch' \u2014 obtain/verify standing settlement instructions."
            return "Likely 'Counterparty Fail' \u2014 chase counterparty for status."
        if check == "amount_break":
            q, p = to_float(row.get("quantity")), to_float(row.get("price"))
            if q is not None and p is not None:
                return f"Recompute settlement_amount to {q * p:,.2f} (qty \u00d7 price); verify before applying."
            return "Recompute settlement_amount from qty \u00d7 price; verify before applying."
        if check == "unmatched_or_dk":
            return "Route to matching/repair queue; chase counterparty affirmation."
        if check == "missing_ssi":
            return "Obtain SSI from client/counterparty before settlement is authorized."
        if check == "aging_pending":
            return "Escalate as aged fail risk; confirm funding/securities and chase settlement."
        if check == "price_outlier":
            return "Review for possible fat-finger / price error before settlement."
        if check == "duplicate_trade_id":
            return "Investigate possible double-booking; confirm one is a genuine duplicate."
        if check in ("bad_quantity", "bad_price"):
            return "Reject and return to trade capture for correction."
        return "Route to operations for manual review."

    # ---- per-trade processing --------------------------------------------------
    def process_row(self, row: dict) -> dict:
        """Apply Tier-1 auto-fixes in place; emit Tier-2/3 actions. Returns the
        (possibly corrected) row with a processing_status field."""
        statuses = []

        # ---------- TIER 1: AUTO-FIX (safe, deterministic, reversible) ----------
        # 1a. normalize side
        raw = row.get("buy_sell", "")
        norm = CANON_SIDE.get(raw.strip().upper())
        if norm and norm != raw:
            self.log(Action(row["trade_id"], TIER_AUTOFIX, "normalize_buy_sell", "low",
                            "buy_sell", raw, norm, "standardized casing/whitespace"))
            row["buy_sell"] = norm
            statuses.append("auto_fixed")

        # 1b. normalize currency
        raw = row.get("currency", "")
        if raw.strip().upper() == "USD" and raw != "USD":
            self.log(Action(row["trade_id"], TIER_AUTOFIX, "normalize_currency", "low",
                            "currency", raw, "USD", "standardized currency code"))
            row["currency"] = "USD"
            statuses.append("auto_fixed")

        # 1c. normalize product
        raw = row.get("product", "")
        norm = CANON_PRODUCT.get(raw.strip().upper())
        if norm and norm != raw:
            self.log(Action(row["trade_id"], TIER_AUTOFIX, "normalize_product", "low",
                            "product", raw, norm, "standardized product label"))
            row["product"] = norm
            statuses.append("auto_fixed")

        # 1d. derive missing settle_date (deterministic T+N business days)
        if not (row.get("settle_date") or "").strip():
            td = parse_date(row.get("trade_date"))
            offset = SETTLE_OFFSET_DAYS.get(row.get("product"))
            if td and offset and row.get("settle_status") != "Failed":
                derived = add_business_days(td, offset).isoformat()
                self.log(Action(row["trade_id"], TIER_AUTOFIX, "derive_settle_date", "low",
                                "settle_date", "", derived,
                                f"derived T+{offset} business days from trade_date"))
                row["settle_date"] = derived
                statuses.append("auto_fixed")

        # 1e. reconcile tiny rounding drift on settlement_amount
        q, p, amt = to_float(row.get("quantity")), to_float(row.get("price")), to_float(row.get("settlement_amount"))
        if q is not None and p is not None and amt is not None and q > 0 and p > 0:
            expected = round(q * p, 2)
            diff = abs(expected - amt)
            if 0 < diff <= ROUNDING_TOLERANCE:
                self.log(Action(row["trade_id"], TIER_AUTOFIX, "reconcile_rounding", "low",
                                "settlement_amount", f"{amt}", f"{expected:.2f}",
                                "corrected sub-cent rounding drift"))
                row["settlement_amount"] = f"{expected:.2f}"
                statuses.append("auto_fixed")

        # ---------- TIER 2: AI-SUGGEST (propose, route to human) ----------------
        # 2a. material settlement-amount break (beyond rounding)
        if q is not None and p is not None and amt is not None and q > 0 and p > 0:
            expected = round(q * p, 2)
            if abs(expected - amt) > ROUNDING_TOLERANCE:
                self._suggest(row, "amount_break", "high",
                              f"settlement_amount {amt} vs qty\u00d7price {expected:.2f}")
                statuses.append("review")

        # 2b. missing fail_reason on a failed trade
        if row.get("settle_status") == "Failed" and not (row.get("fail_reason") or "").strip():
            self._suggest(row, "missing_fail_reason", "medium", "failed trade missing fail_reason")
            statuses.append("review")

        # 2c. unmatched / DK
        if row.get("match_status") in ("Unmatched", "DK"):
            self._suggest(row, "unmatched_or_dk", "medium",
                          f"match_status={row.get('match_status')}")
            statuses.append("review")

        # 2d. missing SSI and not yet settled
        if row.get("ssi_present") == "N" and row.get("settle_status") != "On-time":
            self._suggest(row, "missing_ssi", "medium", "SSI not present pre-settlement")
            statuses.append("review")

        # 2e. aging pending past settle date
        sd = parse_date(row.get("settle_date"))
        if row.get("settle_status") == "Pending" and sd and sd < self.asof:
            self._suggest(row, "aging_pending", "high",
                          f"pending; settle_date {sd} < as-of {self.asof}")
            statuses.append("review")

        # 2f. bad quantity / price (fat-finger)
        if q is not None and q <= 0:
            self._suggest(row, "bad_quantity", "high", f"quantity={row.get('quantity')}")
            statuses.append("review")
        if p is not None and p <= 0:
            self._suggest(row, "bad_price", "high", f"price={row.get('price')}")
            statuses.append("review")

        # ---------- TIER 3: HUMAN-ONLY (never auto-action) ----------------------
        # ready-to-settle: matched, SSI present, on/after settle date, not yet settled
        if (row.get("match_status") == "Matched" and row.get("ssi_present") == "Y"
                and row.get("settle_status") == "Pending" and sd and sd <= self.asof):
            self.log(Action(row["trade_id"], TIER_HUMAN, "settlement_release", "high",
                            detail="ready to settle; settlement authorization is human-only",
                            suggestion="Authorize settlement / money movement (human sign-off required)."))
            statuses.append("human_release")

        row["processing_status"] = (
            "auto_fixed+review" if "auto_fixed" in statuses and "review" in statuses
            else "auto_fixed" if "auto_fixed" in statuses
            else "review" if "review" in statuses
            else "human_release" if "human_release" in statuses
            else "clean"
        )
        return row

    def _suggest(self, row, check, severity, detail):
        self.log(Action(row["trade_id"], TIER_SUGGEST, check, severity,
                        detail=detail, suggestion=self.suggest_resolution(row, check)))

    # ---- price-outlier pass (needs full population for z-scores) ----------------
    def flag_price_outliers(self, rows, z_threshold=4.0):
        by_product = defaultdict(list)
        for r in rows:
            p = to_float(r.get("price"))
            if p is not None and p > 0:
                by_product[r.get("product")].append((r, p))
        for product, items in by_product.items():
            prices = [p for _, p in items]
            if len(prices) < 30:
                continue
            mu = statistics.mean(prices)
            sd = statistics.pstdev(prices)
            if sd == 0:
                continue
            for r, p in items:
                z = (p - mu) / sd
                if abs(z) >= z_threshold:
                    self._suggest(r, "price_outlier", "medium",
                                  f"price {p} is {z:+.1f}\u03c3 vs {product} mean {mu:,.2f}")
                    if "review" not in r.get("processing_status", ""):
                        r["processing_status"] = (r.get("processing_status", "clean")
                                                  .replace("clean", "review"))

    # ---- duplicate detection (needs full population) ---------------------------
    def flag_duplicates(self, rows):
        seen = Counter(r["trade_id"] for r in rows)
        for r in rows:
            if seen[r["trade_id"]] > 1:
                self.log(Action(r["trade_id"], TIER_SUGGEST, "duplicate_trade_id", "high",
                                detail=f"trade_id appears {seen[r['trade_id']]}x",
                                suggestion=self.suggest_resolution(r, "duplicate_trade_id")))


# ----- run / IO -----------------------------------------------------------------
def run(input_path: Path, asof: dt.date, outdir: Path):
    with open(input_path, newline="") as f:
        rows = list(csv.DictReader(f))

    engine = PostTradeEngine(asof=asof)
    processed = [engine.process_row(dict(r)) for r in rows]
    engine.flag_price_outliers(processed)
    engine.flag_duplicates(processed)

    outdir.mkdir(parents=True, exist_ok=True)

    # cleaned trades
    fieldnames = list(processed[0].keys())
    with open(outdir / "cleaned_trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader(); w.writerows(processed)

    # audit log (one JSON object per line)
    with open(outdir / "audit_log.jsonl", "w") as f:
        for a in engine.actions:
            f.write(json.dumps(asdict(a)) + "\n")

    # exceptions (everything needing human attention = Tier 2 + Tier 3)
    exceptions = [a for a in engine.actions if a.tier in (TIER_SUGGEST, TIER_HUMAN)]
    with open(outdir / "exceptions.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["trade_id", "tier", "check", "severity",
                                          "detail", "suggestion", "timestamp"])
        w.writeheader()
        for a in exceptions:
            w.writerow({k: getattr(a, k) for k in
                        ["trade_id", "tier", "check", "severity", "detail", "suggestion", "timestamp"]})

    summary = build_summary(processed, engine, asof, input_path)
    (outdir / "run_summary.txt").write_text(summary)
    print(summary)


def build_summary(processed, engine, asof, input_path):
    n = len(processed)
    tier_counts = Counter(a.tier for a in engine.actions)
    autofix_by_check = Counter(a.check for a in engine.actions if a.tier == TIER_AUTOFIX)
    suggest_by_check = Counter(a.check for a in engine.actions if a.tier == TIER_SUGGEST)
    status_counts = Counter(r["processing_status"] for r in processed)

    trades_touched = len({a.trade_id for a in engine.actions})
    trades_autofixed = len({a.trade_id for a in engine.actions if a.tier == TIER_AUTOFIX})
    trades_review = len({a.trade_id for a in engine.actions
                         if a.tier in (TIER_SUGGEST, TIER_HUMAN)})
    auto_only = len({a.trade_id for a in engine.actions if a.tier == TIER_AUTOFIX}
                    - {a.trade_id for a in engine.actions if a.tier in (TIER_SUGGEST, TIER_HUMAN)})

    L = []
    A = L.append
    A("=" * 70)
    A("POST-TRADE BACK-OFFICE AUTOMATION ENGINE \u2014 RUN SUMMARY")
    A("=" * 70)
    A(f"Input            : {input_path}")
    A(f"As-of date       : {asof}")
    A(f"Trades processed : {n:,}")
    A("")
    A("THREE-TIER OUTCOME")
    A("-" * 70)
    A(f"  Tier 1  AUTO-FIX    actions: {tier_counts[TIER_AUTOFIX]:>5}   "
      f"(applied automatically, fully logged & reversible)")
    A(f"  Tier 2  AI-SUGGEST  actions: {tier_counts[TIER_SUGGEST]:>5}   "
      f"(proposed; routed to a human to approve)")
    A(f"  Tier 3  HUMAN-ONLY  actions: {tier_counts[TIER_HUMAN]:>5}   "
      f"(settlement / money movement \u2014 never auto-actioned)")
    A("")
    A("TOUCHPOINT REDUCTION")
    A("-" * 70)
    A(f"  Trades fully cleared by automation (no human needed) : {auto_only:,}")
    A(f"  Trades requiring a human review                      : {trades_review:,}")
    if trades_touched:
        pct = 100 * auto_only / max(trades_touched, 1)
        A(f"  Of all trades the engine touched, {pct:.0f}% needed zero human effort.")
    A("")
    A("TIER 1 AUTO-FIXES BY TYPE")
    A("-" * 70)
    for c, k in autofix_by_check.most_common():
        A(f"  {c:24s} {k:>5}")
    A("")
    A("TIER 2 SUGGESTIONS BY TYPE (human approves)")
    A("-" * 70)
    for c, k in suggest_by_check.most_common():
        A(f"  {c:24s} {k:>5}")
    A("")
    A("PER-TRADE PROCESSING STATUS")
    A("-" * 70)
    for s, k in status_counts.most_common():
        A(f"  {s:24s} {k:>5}")
    A("=" * 70)
    A("Outputs: cleaned_trades.csv | exceptions.csv | audit_log.jsonl")
    A("=" * 70)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Post-trade back-office automation engine")
    ap.add_argument("--input", default=str(ROOT / "data" / "trades_dirty.csv"))
    ap.add_argument("--asof", default="2026-06-19", help="YYYY-MM-DD as-of date")
    ap.add_argument("--outdir", default=str(ROOT / "output"))
    args = ap.parse_args()
    run(Path(args.input), dt.date.fromisoformat(args.asof), Path(args.outdir))


if __name__ == "__main__":
    main()
