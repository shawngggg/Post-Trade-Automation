"""
validate.py
-----------
Checks the engine's output against the injected ground truth
(data/injected_truth.csv) and reports recall per defect family — i.e. did the
engine catch what we deliberately broke?

Usage:  python src/validate.py
"""

import csv
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]

# injected defect family  ->  engine check name(s) that should catch it
MAPPING = {
    "format_buy_sell":     {"normalize_buy_sell"},
    "format_currency":     {"normalize_currency"},
    "format_product":      {"normalize_product"},
    "missing_settle_date": {"derive_settle_date"},
    "amount_rounding":     {"reconcile_rounding"},
    "amount_break":        {"amount_break"},
    "missing_fail_reason": {"missing_fail_reason"},
    "bad_quantity":        {"bad_quantity"},
    "bad_price":           {"bad_price"},
    "duplicate_trade_id":  {"duplicate_trade_id"},
}


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main():
    truth = load(ROOT / "data" / "injected_truth.csv")
    actions = load(ROOT / "output" / "exceptions.csv") + load_audit()

    caught = defaultdict(set)   # check -> {trade_ids}
    for a in actions:
        caught[a["check"]].add(a["trade_id"])

    print(f"{'DEFECT FAMILY':24s} {'INJECTED':>8} {'CAUGHT':>7} {'RECALL':>7}")
    print("-" * 50)
    total_inj = total_caught = 0
    by_family = defaultdict(set)
    for t in truth:
        by_family[t["defect"]].add(t["trade_id"])

    for fam, ids in sorted(by_family.items()):
        checks = MAPPING.get(fam, set())
        hit = set()
        for c in checks:
            hit |= caught.get(c, set())
        found = ids & hit
        recall = 100 * len(found) / len(ids) if ids else 0
        total_inj += len(ids)
        total_caught += len(found)
        print(f"{fam:24s} {len(ids):>8} {len(found):>7} {recall:>6.0f}%")

    overall = 100 * total_caught / total_inj if total_inj else 0
    print("-" * 50)
    print(f"{'OVERALL':24s} {total_inj:>8} {total_caught:>7} {overall:>6.0f}%")


def load_audit():
    """Auto-fix actions live in the audit log, not exceptions.csv."""
    import json
    rows = []
    p = ROOT / "output" / "audit_log.jsonl"
    if p.exists():
        for line in p.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


if __name__ == "__main__":
    main()
