# Business Finance MIS — 2026-09

*Commentary source: Rule-based fallback (set ANTHROPIC_API_KEY to enable Claude)*  
*Unverified numbers: none — every figure traced to the KPI layer*

## Commentary

**Headline:** 2026-09 GMV was ₹5.70 Cr (+7.7% MoM) with gross revenue of ₹4.40 L (+9.9% MoM) and contribution of ₹1.99 L at a 45.4% margin.

**What moved**
- Take rate was 77.2 bps, up from 75.7 bps last month.
- UPI carried 56.8% of GMV; Debit Card was the largest contribution driver at ₹67,338.
- Segments: Enterprise ₹3.16 Cr (+8.8% MoM); Mid-Market ₹1.88 Cr (+6.4% MoM); SMB ₹65.98 L (+6.0% MoM).
- Payment success rate was 90.05% (prior month 89.83%) across 1164 active merchants.

**Watch-list**
- 22 merchants dropped more than 40% MoM, putting ₹32.42 L of GMV at risk.
- EMI has the weakest success rate at 79.59%.
- Refunds for the month were ₹15.20 L.

**Data quality:** 152 transaction rows were quarantined; 6 of 11 checks raised issues (see DQ log).

## Key metrics

| Metric | Value |
|---|---|
| GMV | ₹5.70 Cr (+7.7% MoM) |
| Gross revenue | ₹4.40 L (+9.9% MoM) |
| Contribution | ₹1.99 L (45.4% margin) |
| Take rate | 77.2 bps |
| Success rate | 90.05% |
| Active merchants | 1,164 |

![gmv_take_rate_2026-09](gmv_take_rate_2026-09.svg)
![method_contribution_2026-09](method_contribution_2026-09.svg)

## Data-quality log

| check                                        | table        | severity   |   rows_affected | action                                      | status   |
|:---------------------------------------------|:-------------|:-----------|----------------:|:--------------------------------------------|:---------|
| Duplicate payment rows                       | transactions | blocking   |              72 | Removed duplicate, kept first occurrence    | FAIL     |
| payment_id not unique (conflicting payloads) | transactions | blocking   |               0 | Quarantined for manual review               | PASS     |
| Missing amount                               | transactions | blocking   |              36 | Quarantined; raise with data engineering    | FAIL     |
| Zero / negative amount                       | transactions | blocking   |              24 | Quarantined; refunds live in refunds table  | FAIL     |
| Future-dated transaction                     | transactions | blocking   |               8 | Quarantined; likely timezone/ETL bug        | FAIL     |
| Merchant missing from master                 | transactions | blocking   |              12 | Quarantined; check merchant onboarding sync | FAIL     |
| MDR fee > 5% of amount                       | transactions | warning    |               0 | Kept; flagged to pricing team               | PASS     |
| Fee on non-captured payment                  | transactions | warning    |               0 | Kept; flagged to billing                    | PASS     |
| Refund for unknown / uncaptured payment      | refunds      | blocking   |               3 | Excluded from net revenue                   | FAIL     |
| Refund exceeds captured amount               | refunds      | blocking   |               0 | Excluded; escalate to finance ops           | PASS     |
| Control total: input = clean + quarantined   | transactions | blocking   |               0 | Row counts tie out                          | PASS     |