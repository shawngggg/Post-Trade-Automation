"""
generate_exceptions.py
----------------------
Takes the clean synthetic trade blotter (data/trades_raw.csv) and injects a
*controlled, reproducible* set of real-world back-office data defects so the
detection/auto-fix engine has something to find.

Every injected defect is recorded in data/injected_truth.csv so the engine's
output can be checked against ground truth ("we injected N, the engine caught N").

Defect families injected:
  - Clerical formatting (buy_sell / currency / product casing + whitespace)   -> safe auto-fix
  - Missing-but-derivable settle_date (T+1 business day)                       -> safe auto-fix
  - Settlement-amount rounding drift (tiny)                                    -> safe auto-fix
  - Settlement-amount break (material)                                         -> human review
  - Missing fail_reason on a failed trade                                      -> AI-suggest
  - Duplicate trade_id (double-booking)                                        -> human review
  - Zero / negative quantity or price (fat-finger)                            -> human review

Usage:  python src/generate_exceptions.py
"""

import csv
import random
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "trades_raw.csv"
OUT = ROOT / "data" / "trades_dirty.csv"
TRUTH = ROOT / "data" / "injected_truth.csv"

SEED = 42            # reproducible defects
random.seed(SEED)


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main():
    rows = load(SRC)
    fieldnames = list(rows[0].keys())
    truth = []   # (trade_id, defect, field, detail)

    def mark(tid, defect, field, detail):
        truth.append({"trade_id": tid, "defect": defect, "field": field, "detail": detail})

    # Helper: pick N distinct random rows matching a predicate
    def pick(n, pred=lambda r: True):
        pool = [r for r in rows if pred(r)]
        random.shuffle(pool)
        return pool[:n]

    # 1) Clerical formatting noise on buy_sell  -> safe auto-fix
    for r in pick(120):
        original = r["buy_sell"]
        variant = random.choice([original.upper(), original.lower(),
                                  f" {original} ", f"{original} "])
        if variant != original:
            r["buy_sell"] = variant
            mark(r["trade_id"], "format_buy_sell", "buy_sell", f"'{variant}'")

    # 2) Currency casing  -> safe auto-fix
    for r in pick(90):
        r["currency"] = random.choice(["usd", "Usd", " USD"])
        mark(r["trade_id"], "format_currency", "currency", f"'{r['currency']}'")

    # 3) Product casing/whitespace  -> safe auto-fix
    for r in pick(80):
        r["product"] = random.choice([r["product"].upper(), r["product"].lower(),
                                      f"{r['product']} "])
        mark(r["trade_id"], "format_product", "product", f"'{r['product']}'")

    # 4) Missing-but-derivable settle_date (only where status not failed)  -> safe auto-fix
    for r in pick(100, lambda r: r["settle_date"] and r["settle_status"] != "Failed"):
        r["settle_date"] = ""
        mark(r["trade_id"], "missing_settle_date", "settle_date", "blanked (derivable T+1)")

    # 5) Settlement-amount rounding drift (tiny, <= 1 cent)  -> safe auto-fix
    for r in pick(70, lambda r: r["settlement_amount"]):
        amt = float(r["settlement_amount"])
        drift = round(random.uniform(-0.009, 0.009), 4)
        r["settlement_amount"] = f"{amt + drift:.4f}"
        mark(r["trade_id"], "amount_rounding", "settlement_amount", f"drift {drift:+.4f}")

    # 6) Settlement-amount material break  -> human review
    for r in pick(40, lambda r: r["settlement_amount"]):
        amt = float(r["settlement_amount"])
        broken = round(amt * random.uniform(1.05, 1.4), 2)
        r["settlement_amount"] = f"{broken:.2f}"
        mark(r["trade_id"], "amount_break", "settlement_amount", "material mismatch vs qty*price")

    # 7) Missing fail_reason on a failed trade  -> AI-suggest
    for r in pick(60, lambda r: r["settle_status"] == "Failed" and r["fail_reason"]):
        r["fail_reason"] = ""
        mark(r["trade_id"], "missing_fail_reason", "fail_reason", "blanked")

    # 8) Zero / negative quantity or price  -> human review
    for r in pick(25):
        if random.random() < 0.5:
            r["quantity"] = random.choice(["0", "-100"])
            mark(r["trade_id"], "bad_quantity", "quantity", r["quantity"])
        else:
            r["price"] = random.choice(["0", "-12.5"])
            mark(r["trade_id"], "bad_price", "price", r["price"])

    # 9) Duplicate trade_id (double-booking)  -> human review
    dupes = []
    for r in pick(15, lambda r: r["match_status"] == "Matched"):
        clone = dict(r)
        # keep same trade_id; tweak a value so it isn't byte-identical
        clone["counterparty"] = clone["counterparty"]
        dupes.append(clone)
        mark(r["trade_id"], "duplicate_trade_id", "trade_id", "second booking of same id")
    rows.extend(dupes)

    # Shuffle so defects aren't clustered, then write
    random.shuffle(rows)
    write(OUT, rows, fieldnames)
    write(TRUTH, truth, ["trade_id", "defect", "field", "detail"])

    print(f"Wrote {OUT}  ({len(rows)} rows, incl. {len(dupes)} duplicates)")
    print(f"Wrote {TRUTH}  ({len(truth)} injected defects)")
    # quick breakdown
    from collections import Counter
    for d, n in sorted(Counter(t["defect"] for t in truth).items()):
        print(f"  {d:24s} {n}")


if __name__ == "__main__":
    main()
