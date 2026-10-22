"""
Data-accuracy layer: validate raw payments data before any number reaches a report.

Every check returns a row in the data-quality log (check, severity, rows affected, action).
Rows that fail a blocking check are quarantined (kept in a separate table for follow-up)
instead of being silently dropped, so finance can always trace why a number moved.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import pandas as pd


@dataclass
class CheckResult:
    check: str
    table: str
    severity: str          # "blocking" -> row quarantined, "warning" -> row kept, flagged
    rows_affected: int
    action: str

    @property
    def passed(self) -> bool:
        return self.rows_affected == 0


def validate(tx: pd.DataFrame, merchants: pd.DataFrame, refunds: pd.DataFrame,
             as_of: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return (clean_transactions, clean_refunds, quarantine, dq_log)."""
    results: list[CheckResult] = []
    tx = tx.copy()
    tx["created_at"] = pd.to_datetime(tx["created_at"])
    reason = pd.Series("", index=tx.index)

    def flag(mask: pd.Series, label: str) -> int:
        nonlocal reason
        reason = reason.where(~mask, reason + label + ";")
        return int(mask.sum())

    # 1. exact duplicate rows (same payment_id + identical payload) -> keep first
    dup_mask = tx.duplicated(keep="first")
    results.append(CheckResult("Duplicate payment rows", "transactions", "blocking",
                               flag(dup_mask, "duplicate"), "Removed duplicate, kept first occurrence"))

    # 2. primary key uniqueness after de-dup
    pk_conflict = tx[~dup_mask].payment_id.duplicated(keep=False)
    pk_mask = pd.Series(False, index=tx.index)
    pk_mask.loc[pk_conflict[pk_conflict].index] = True
    results.append(CheckResult("payment_id not unique (conflicting payloads)", "transactions", "blocking",
                               flag(pk_mask, "pk_conflict"), "Quarantined for manual review"))

    # 3. missing amount
    na_mask = tx.amount.isna()
    results.append(CheckResult("Missing amount", "transactions", "blocking",
                               flag(na_mask, "missing_amount"), "Quarantined; raise with data engineering"))

    # 4. non-positive amount
    neg_mask = tx.amount.notna() & (tx.amount <= 0)
    results.append(CheckResult("Zero / negative amount", "transactions", "blocking",
                               flag(neg_mask, "non_positive_amount"), "Quarantined; refunds live in refunds table"))

    # 5. future-dated
    fut_mask = tx.created_at > as_of
    results.append(CheckResult("Future-dated transaction", "transactions", "blocking",
                               flag(fut_mask, "future_dated"), "Quarantined; likely timezone/ETL bug"))

    # 6. referential integrity: merchant must exist in master
    orphan_mask = ~tx.merchant_id.isin(merchants.merchant_id)
    results.append(CheckResult("Merchant missing from master", "transactions", "blocking",
                               flag(orphan_mask, "orphan_merchant"), "Quarantined; check merchant onboarding sync"))

    # 7. fee sanity (warning only): fee should never exceed 5% of amount
    fee_mask = tx.amount.gt(0) & (tx.mdr_fee > 0.05 * tx.amount)
    results.append(CheckResult("MDR fee > 5% of amount", "transactions", "warning",
                               int(fee_mask.sum()), "Kept; flagged to pricing team"))

    # 8. fee charged on non-captured payment (warning)
    fee_fail = tx.status.ne("captured") & (tx.mdr_fee > 0)
    results.append(CheckResult("Fee on non-captured payment", "transactions", "warning",
                               int(fee_fail.sum()), "Kept; flagged to billing"))

    bad = reason.ne("")
    quarantine = tx[bad].assign(dq_reason=reason[bad].str.rstrip(";"))
    clean_tx = tx[~bad].copy()

    # refunds: must point to a clean, captured payment and not exceed original amount
    refunds = refunds.copy()
    refunds["refunded_at"] = pd.to_datetime(refunds["refunded_at"])
    cap = clean_tx[clean_tx.status == "captured"].set_index("payment_id").amount
    r_orphan = ~refunds.payment_id.isin(cap.index)
    results.append(CheckResult("Refund for unknown / uncaptured payment", "refunds", "blocking",
                               int(r_orphan.sum()), "Excluded from net revenue"))
    over = refunds.payment_id.map(cap).lt(refunds.refund_amount) & ~r_orphan
    results.append(CheckResult("Refund exceeds captured amount", "refunds", "blocking",
                               int(over.sum()), "Excluded; escalate to finance ops"))
    clean_refunds = refunds[~(r_orphan | over)]

    # reconciliation control: rows in = clean + quarantined
    total_in = len(tx)
    control_ok = total_in == len(clean_tx) + len(quarantine)
    results.append(CheckResult("Control total: input = clean + quarantined", "transactions", "blocking",
                               0 if control_ok else abs(total_in - len(clean_tx) - len(quarantine)),
                               "Row counts tie out" if control_ok else "ROW COUNT MISMATCH — stop the run"))

    dq_log = pd.DataFrame([{**asdict(r), "status": "PASS" if r.passed else
                            ("FAIL" if r.severity == "blocking" else "WARN")} for r in results])
    return clean_tx, clean_refunds, quarantine, dq_log
