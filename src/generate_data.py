"""
Generate a realistic synthetic payments dataset for a payment-gateway business.

Tables produced (CSV, in data/raw/):
  merchants.csv     - merchant master (segment, category, city, onboarding date, pricing)
  transactions.csv  - payment attempts with status, method, amount, MDR fee and network cost
  refunds.csv       - refunds raised against captured payments

A small number of data-quality issues are injected on purpose (duplicate rows,
missing amounts, negative amounts, future-dated rows, unknown merchants) so the
pipeline's validation layer has something real to catch.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

SEGMENTS = {          # share of merchants, avg ticket (INR), monthly txns (lambda)
    "Enterprise": (0.05, 2400, 900),
    "Mid-Market": (0.20, 1300, 220),
    "SMB":        (0.75, 650, 45),
}
CATEGORIES = ["E-commerce", "Food Delivery", "Travel", "Education", "SaaS",
              "Gaming", "Healthcare", "BFSI", "Retail", "Utilities"]
CITIES = ["Bengaluru", "Mumbai", "Delhi NCR", "Hyderabad", "Chennai", "Pune",
          "Kolkata", "Ahmedabad", "Jaipur", "Chandigarh"]

# payment method -> (mix share, success rate, network cost as % of amount, flat cost INR)
METHODS = {
    "UPI":          (0.55, 0.93, 0.0000, 0.0),
    "Credit Card":  (0.18, 0.88, 0.0145, 0.0),
    "Debit Card":   (0.10, 0.86, 0.0040, 0.0),
    "Netbanking":   (0.09, 0.82, 0.0000, 6.0),
    "Wallet":       (0.05, 0.90, 0.0110, 0.0),
    "EMI":          (0.03, 0.80, 0.0170, 0.0),
}
# MDR charged to merchant by segment (UPI is zero-MDR for most merchants in India)
MDR = {"Enterprise": 0.0160, "Mid-Market": 0.0190, "SMB": 0.0200}


def make_merchants(rng: np.random.Generator, n: int, start: pd.Timestamp) -> pd.DataFrame:
    seg_names = list(SEGMENTS)
    seg_p = [SEGMENTS[s][0] for s in seg_names]
    segments = rng.choice(seg_names, size=n, p=seg_p)
    onboard_offsets = rng.integers(-540, 170, size=n)            # some onboard during the window
    df = pd.DataFrame({
        "merchant_id": [f"M{100000 + i}" for i in range(n)],
        "merchant_name": [f"Merchant {i:04d}" for i in range(n)],
        "segment": segments,
        "category": rng.choice(CATEGORIES, size=n),
        "city": rng.choice(CITIES, size=n, p=[.22, .18, .17, .1, .08, .08, .05, .05, .04, .03]),
        "onboarded_on": (start + pd.to_timedelta(onboard_offsets, unit="D")).date,
    })
    # negotiated discount for big merchants
    disc = np.where(df.segment == "Enterprise", rng.uniform(0.0, 0.004, n), 0.0)
    df["mdr_rate"] = (df.segment.map(MDR) - disc).round(4)
    return df


def make_transactions(rng, merchants: pd.DataFrame, start: pd.Timestamp, months: int) -> pd.DataFrame:
    frames = []
    method_names = list(METHODS)
    method_p = [METHODS[m][0] for m in method_names]
    for m_idx in range(months):
        month_start = start + pd.DateOffset(months=m_idx)
        days_in_month = (month_start + pd.DateOffset(months=1) - month_start).days
        growth = 1.0 + 0.045 * m_idx                       # ~4.5% MoM volume growth
        festive = 1.35 if month_start.month in (10, 11) else 1.0
        for seg, (_, ticket, lam) in SEGMENTS.items():
            seg_m = merchants[merchants.segment == seg]
            active = seg_m[pd.to_datetime(seg_m.onboarded_on) <= month_start + pd.DateOffset(days=days_in_month - 1)]
            # scale down so the dataset stays laptop-friendly
            counts = rng.poisson(lam * growth * festive / 6, size=len(active))
            churned = rng.random(len(active)) < 0.03 * m_idx / max(months - 1, 1)
            counts[churned] = 0
            mids = np.repeat(active.merchant_id.values, counts)
            n = len(mids)
            if n == 0:
                continue
            frames.append(pd.DataFrame({
                "merchant_id": mids,
                "segment": seg,
                "created_at": month_start
                + pd.to_timedelta(rng.integers(0, days_in_month * 86400, n), unit="s"),
                "method": rng.choice(method_names, size=n, p=method_p),
                "amount": np.round(rng.lognormal(np.log(ticket), 0.85, n), 2),
            }))
    tx = pd.concat(frames, ignore_index=True).sort_values("created_at", ignore_index=True)

    success_p = tx.method.map({k: v[1] for k, v in METHODS.items()}).values
    roll = rng.random(len(tx))
    tx["status"] = np.where(roll < success_p, "captured",
                            np.where(roll < success_p + 0.04, "authorized_expired", "failed"))

    mdr_rate = tx.merchant_id.map(merchants.set_index("merchant_id").mdr_rate)
    is_upi = tx.method.eq("UPI")
    captured = tx.status.eq("captured")
    # UPI zero MDR except a small platform fee for Enterprise value-added services
    tx["mdr_fee"] = np.where(captured & ~is_upi, tx.amount * mdr_rate, 0.0)
    tx["mdr_fee"] = np.where(captured & is_upi & tx.segment.eq("Enterprise"), tx.amount * 0.002, tx["mdr_fee"])
    pct = tx.method.map({k: v[2] for k, v in METHODS.items()})
    flat = tx.method.map({k: v[3] for k, v in METHODS.items()})
    tx["network_cost"] = np.where(captured, tx.amount * pct + flat, 0.0)
    tx["gst_on_fee"] = tx.mdr_fee * 0.18
    for c in ("mdr_fee", "network_cost", "gst_on_fee"):
        tx[c] = tx[c].round(2)

    tx.insert(0, "payment_id", [f"pay_{i:09d}" for i in range(len(tx))])
    tx = tx.drop(columns="segment")
    return tx


def make_refunds(rng, tx: pd.DataFrame) -> pd.DataFrame:
    cap = tx[tx.status == "captured"]
    pick = cap.sample(frac=0.035, random_state=SEED)
    partial = rng.random(len(pick)) < 0.3
    amt = np.where(partial, (pick.amount * rng.uniform(0.2, 0.8, len(pick))).round(2), pick.amount)
    return pd.DataFrame({
        "refund_id": [f"rfnd_{i:08d}" for i in range(len(pick))],
        "payment_id": pick.payment_id.values,
        "refund_amount": amt,
        "refunded_at": pick.created_at.values + pd.to_timedelta(rng.integers(1, 20, len(pick)), unit="D"),
    })


def inject_quality_issues(rng, tx: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Deliberately dirty ~0.3% of rows so the validation layer has work to do."""
    tx = tx.copy()
    n = len(tx)
    dup = tx.sample(n=max(25, n // 2000), random_state=1)                 # exact duplicates
    idx = rng.choice(n, size=max(15, n // 4000), replace=False)
    tx.loc[idx, "amount"] = np.nan                                          # missing amount
    idx = rng.choice(n, size=max(10, n // 6000), replace=False)
    tx.loc[idx, "amount"] = -tx.loc[idx, "amount"].abs()                    # negative amount
    idx = rng.choice(n, size=8, replace=False)
    tx.loc[idx, "created_at"] = as_of + pd.Timedelta(days=45)               # future-dated
    idx = rng.choice(n, size=12, replace=False)
    tx.loc[idx, "merchant_id"] = "M999999"                                  # orphan merchant
    return pd.concat([tx, dup], ignore_index=True)


def main(months: int = 6, n_merchants: int = 1200, end_month: str = "2026-09") -> None:
    rng = np.random.default_rng(SEED)
    end = pd.Timestamp(end_month + "-01")
    start = end - pd.DateOffset(months=months - 1)
    as_of = end + pd.DateOffset(months=1) - pd.Timedelta(seconds=1)

    merchants = make_merchants(rng, n_merchants, start)
    tx = make_transactions(rng, merchants, start, months)
    refunds = make_refunds(rng, tx)
    tx = inject_quality_issues(rng, tx, as_of)

    RAW.mkdir(parents=True, exist_ok=True)
    merchants.to_csv(RAW / "merchants.csv", index=False)
    tx.to_csv(RAW / "transactions.csv", index=False)
    refunds.to_csv(RAW / "refunds.csv", index=False)
    print(f"merchants={len(merchants):,}  transactions={len(tx):,}  refunds={len(refunds):,}  "
          f"window={start:%b %Y}–{end:%b %Y}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--merchants", type=int, default=1200)
    p.add_argument("--end-month", default="2026-09")
    a = p.parse_args()
    main(a.months, a.merchants, a.end_month)
