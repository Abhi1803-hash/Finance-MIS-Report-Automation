import pandas as pd

from src.ai_commentary import rule_based_commentary, verify_numbers
from src.quality_checks import validate

AS_OF = pd.Timestamp("2026-09-30 23:59:59")


def _frames():
    merchants = pd.DataFrame({"merchant_id": ["M1", "M2"], "segment": ["SMB", "SMB"]})
    tx = pd.DataFrame({
        "payment_id": ["p1", "p2", "p3", "p4", "p5", "p6"],
        "merchant_id": ["M1", "M1", "M2", "M2", "M9", "M1"],
        "created_at": ["2026-09-01", "2026-09-02", "2026-09-03", "2026-11-15", "2026-09-04", "2026-09-05"],
        "method": ["UPI"] * 6,
        "amount": [100.0, None, -50.0, 100.0, 100.0, 200.0],
        "status": ["captured"] * 6,
        "mdr_fee": [0.0] * 6,
        "network_cost": [0.0] * 6,
        "gst_on_fee": [0.0] * 6,
    })
    tx = pd.concat([tx, tx.iloc[[0]]], ignore_index=True)            # one exact duplicate
    refunds = pd.DataFrame({"refund_id": ["r1", "r2", "r3"], "payment_id": ["p1", "p6", "pX"],
                            "refund_amount": [50.0, 500.0, 10.0],
                            "refunded_at": ["2026-09-10"] * 3})
    return tx, merchants, refunds


def test_each_bad_row_is_quarantined_with_reason():
    tx, m, r = _frames()
    clean, clean_ref, quarantine, log = validate(tx, m, r, AS_OF)
    assert set(clean.payment_id) == {"p1", "p6"}
    reasons = dict(zip(quarantine.payment_id, quarantine.dq_reason))
    assert reasons["p2"] == "missing_amount"
    assert reasons["p3"] == "non_positive_amount"
    assert reasons["p4"] == "future_dated"
    assert reasons["p5"] == "orphan_merchant"
    assert "duplicate" in quarantine.dq_reason.values


def test_control_totals_tie_out():
    tx, m, r = _frames()
    clean, _, quarantine, log = validate(tx, m, r, AS_OF)
    assert len(clean) + len(quarantine) == len(tx)
    assert log.loc[log.check.str.startswith("Control total"), "status"].item() == "PASS"


def test_bad_refunds_excluded():
    tx, m, r = _frames()
    _, clean_ref, _, _ = validate(tx, m, r, AS_OF)
    assert list(clean_ref.refund_id) == ["r1"]          # r2 > captured amount, r3 orphan


def test_verifier_flags_invented_numbers():
    facts = {"report_month": "2026-09", "gmv_display": "₹5.70 Cr", "gmv_mom_pct": 7.7}
    assert verify_numbers("GMV was ₹5.70 Cr (+7.7% MoM).", facts) == []
    assert verify_numbers("GMV was ₹5.70 Cr (+9.1% MoM).", facts) == ["9.1"]


def test_fallback_commentary_is_fully_traceable():
    facts = {
        "report_month": "2026-09", "gmv_display": "₹5.70 Cr", "gmv_mom_pct": 7.7,
        "gross_revenue_display": "₹4.40 L", "revenue_mom_pct": 9.9, "contribution_display": "₹1.99 L",
        "contribution_margin_pct": 45.4, "take_rate_bps": 77.2, "prev_take_rate_bps": 75.7,
        "success_rate_pct": 90.05, "prev_success_rate_pct": 89.83, "active_merchants": 1164,
        "refunds_display": "₹15.20 L", "top_contribution_method": "Debit Card",
        "top_method_contribution_display": "₹67,338", "lowest_success_method": "EMI",
        "lowest_success_rate_pct": 79.59, "upi_gmv_share_pct": 56.8,
        "segments": [{"segment": "SMB", "gmv_display": "₹65.98 L", "gmv_mom_pct": 6.0, "take_rate_bps": 150.1}],
        "decline_threshold_pct": 40, "declining_merchants_count": 22,
        "declining_merchants_gmv_at_risk_display": "₹32.42 L",
        "dq_rows_quarantined": 152, "dq_checks_failed": 6, "dq_checks_total": 11,
    }
    assert verify_numbers(rule_based_commentary(facts), facts) == []
