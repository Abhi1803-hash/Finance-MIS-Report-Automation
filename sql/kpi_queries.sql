-- =====================================================================
-- Business Finance KPI queries (SQLite dialect; ports cleanly to
-- PostgreSQL / Snowflake / BigQuery).  Each block is named with
-- "-- name: <key>" so the Python runner can execute them one by one.
--
-- Definitions used throughout
--   GMV             = sum of captured payment amounts
--   Gross revenue   = MDR fee earned (ex-GST)
--   Network cost    = interchange / bank / network charges we pay
--   Contribution    = Gross revenue - Network cost
--   Take rate (bps) = Gross revenue / GMV * 10,000
--   Success rate    = captured / all attempts
-- =====================================================================

-- name: monthly_pnl
WITH monthly AS (
    SELECT
        strftime('%Y-%m', t.created_at)                          AS month,
        COUNT(*)                                                 AS attempts,
        SUM(CASE WHEN t.status = 'captured' THEN 1 ELSE 0 END)   AS captured_txns,
        SUM(CASE WHEN t.status = 'captured' THEN t.amount END)   AS gmv,
        SUM(t.mdr_fee)                                           AS gross_revenue,
        SUM(t.network_cost)                                      AS network_cost,
        COUNT(DISTINCT CASE WHEN t.status = 'captured' THEN t.merchant_id END) AS active_merchants
    FROM transactions t
    GROUP BY 1
),
refunds_m AS (
    SELECT strftime('%Y-%m', refunded_at) AS month, SUM(refund_amount) AS refunds
    FROM refunds GROUP BY 1
)
SELECT
    m.month,
    m.attempts,
    m.captured_txns,
    ROUND(m.gmv, 2)                                              AS gmv,
    ROUND(COALESCE(r.refunds, 0), 2)                             AS refunds,
    ROUND(m.gmv - COALESCE(r.refunds, 0), 2)                     AS net_gmv,
    ROUND(m.gross_revenue, 2)                                    AS gross_revenue,
    ROUND(m.network_cost, 2)                                     AS network_cost,
    ROUND(m.gross_revenue - m.network_cost, 2)                   AS contribution,
    ROUND(100.0 * (m.gross_revenue - m.network_cost) / NULLIF(m.gross_revenue, 0), 1) AS contribution_margin_pct,
    ROUND(10000.0 * m.gross_revenue / NULLIF(m.gmv, 0), 1)       AS take_rate_bps,
    ROUND(100.0 * m.captured_txns / m.attempts, 2)               AS success_rate_pct,
    m.active_merchants,
    ROUND(m.gmv / m.captured_txns, 0)                            AS avg_ticket,
    ROUND(100.0 * (m.gmv - LAG(m.gmv) OVER (ORDER BY m.month))
          / LAG(m.gmv) OVER (ORDER BY m.month), 1)               AS gmv_mom_pct,
    ROUND(100.0 * (m.gross_revenue - LAG(m.gross_revenue) OVER (ORDER BY m.month))
          / LAG(m.gross_revenue) OVER (ORDER BY m.month), 1)     AS revenue_mom_pct
FROM monthly m
LEFT JOIN refunds_m r USING (month)
WHERE m.month <= :report_month
ORDER BY m.month;

-- name: method_mix
-- Payment-method economics for the report month
SELECT
    t.method,
    COUNT(*)                                                     AS attempts,
    ROUND(100.0 * SUM(t.status = 'captured') / COUNT(*), 2)      AS success_rate_pct,
    ROUND(SUM(CASE WHEN t.status = 'captured' THEN t.amount END), 2) AS gmv,
    ROUND(100.0 * SUM(CASE WHEN t.status = 'captured' THEN t.amount END)
          / SUM(SUM(CASE WHEN t.status = 'captured' THEN t.amount END)) OVER (), 1) AS gmv_share_pct,
    ROUND(SUM(t.mdr_fee), 2)                                     AS gross_revenue,
    ROUND(SUM(t.mdr_fee) - SUM(t.network_cost), 2)               AS contribution,
    ROUND(10000.0 * SUM(t.mdr_fee) / NULLIF(SUM(CASE WHEN t.status = 'captured' THEN t.amount END), 0), 1) AS take_rate_bps
FROM transactions t
WHERE strftime('%Y-%m', t.created_at) = :report_month
GROUP BY t.method
ORDER BY gmv DESC;

-- name: segment_summary
-- Segment view for the report month vs previous month
WITH seg AS (
    SELECT
        m.segment,
        strftime('%Y-%m', t.created_at) AS month,
        SUM(CASE WHEN t.status = 'captured' THEN t.amount END) AS gmv,
        SUM(t.mdr_fee)                                        AS gross_revenue,
        SUM(t.mdr_fee) - SUM(t.network_cost)                  AS contribution,
        COUNT(DISTINCT t.merchant_id)                         AS merchants
    FROM transactions t
    JOIN merchants m USING (merchant_id)
    GROUP BY 1, 2
)
SELECT
    cur.segment,
    cur.merchants,
    ROUND(cur.gmv, 2)                                            AS gmv,
    ROUND(cur.gross_revenue, 2)                                  AS gross_revenue,
    ROUND(cur.contribution, 2)                                   AS contribution,
    ROUND(10000.0 * cur.gross_revenue / cur.gmv, 1)              AS take_rate_bps,
    ROUND(100.0 * (cur.gmv - prev.gmv) / prev.gmv, 1)            AS gmv_mom_pct
FROM seg cur
LEFT JOIN seg prev
       ON prev.segment = cur.segment
      AND prev.month   = strftime('%Y-%m', date(:report_month || '-01', '-1 month'))
WHERE cur.month = :report_month
ORDER BY gmv DESC;

-- name: top_merchants
-- Top 15 merchants by contribution in the report month, with rank movement
WITH merchant_month AS (
    SELECT
        t.merchant_id,
        strftime('%Y-%m', t.created_at) AS month,
        SUM(CASE WHEN t.status = 'captured' THEN t.amount END) AS gmv,
        SUM(t.mdr_fee) - SUM(t.network_cost)                  AS contribution
    FROM transactions t
    GROUP BY 1, 2
),
ranked AS (
    SELECT *, RANK() OVER (PARTITION BY month ORDER BY contribution DESC) AS rnk
    FROM merchant_month
)
SELECT
    cur.rnk                                                      AS rank,
    cur.merchant_id,
    m.merchant_name,
    m.segment,
    m.category,
    ROUND(cur.gmv, 2)                                            AS gmv,
    ROUND(cur.contribution, 2)                                   AS contribution,
    prev.rnk                                                     AS prev_rank,
    CASE WHEN prev.rnk IS NULL THEN 'NEW'
         ELSE printf('%+d', prev.rnk - cur.rnk) END              AS rank_change
FROM ranked cur
JOIN merchants m USING (merchant_id)
LEFT JOIN ranked prev
       ON prev.merchant_id = cur.merchant_id
      AND prev.month = strftime('%Y-%m', date(:report_month || '-01', '-1 month'))
WHERE cur.month = :report_month
ORDER BY cur.rnk
LIMIT 15;

-- name: category_trend
-- Monthly GMV by merchant category (pivot-ready long format)
SELECT
    strftime('%Y-%m', t.created_at) AS month,
    m.category,
    ROUND(SUM(CASE WHEN t.status = 'captured' THEN t.amount END), 2) AS gmv
FROM transactions t
JOIN merchants m USING (merchant_id)
WHERE strftime('%Y-%m', t.created_at) <= :report_month
GROUP BY 1, 2
ORDER BY 1, 3 DESC;

-- name: merchant_retention
-- Cohort retention: share of merchants transacting in month 0 that are still transacting N months later
WITH active AS (
    SELECT DISTINCT merchant_id, strftime('%Y-%m', created_at) AS month
    FROM transactions WHERE status = 'captured'
),
first_month AS (
    SELECT merchant_id, MIN(month) AS cohort FROM active GROUP BY 1
),
joined AS (
    SELECT f.cohort, a.month,
           (CAST(substr(a.month, 1, 4) AS INT) - CAST(substr(f.cohort, 1, 4) AS INT)) * 12
         + (CAST(substr(a.month, 6, 2) AS INT) - CAST(substr(f.cohort, 6, 2) AS INT)) AS month_n,
           a.merchant_id
    FROM active a JOIN first_month f USING (merchant_id)
)
SELECT
    cohort,
    month_n,
    COUNT(DISTINCT merchant_id) AS merchants,
    ROUND(100.0 * COUNT(DISTINCT merchant_id)
          / FIRST_VALUE(COUNT(DISTINCT merchant_id)) OVER (PARTITION BY cohort ORDER BY month_n), 1) AS retention_pct
FROM joined
WHERE month <= :report_month
GROUP BY 1, 2
ORDER BY 1, 2;

-- name: declining_merchants
-- Early-warning list: merchants whose GMV fell >40% MoM (report month vs previous), min ₹50k prior GMV
WITH mm AS (
    SELECT merchant_id, strftime('%Y-%m', created_at) AS month,
           SUM(CASE WHEN status = 'captured' THEN amount END) AS gmv
    FROM transactions GROUP BY 1, 2
)
SELECT
    p.merchant_id,
    m.merchant_name,
    m.segment,
    ROUND(p.gmv, 2)                          AS prev_month_gmv,
    ROUND(COALESCE(c.gmv, 0), 2)             AS report_month_gmv,
    ROUND(100.0 * (COALESCE(c.gmv, 0) - p.gmv) / p.gmv, 1) AS change_pct
FROM mm p
JOIN merchants m USING (merchant_id)
LEFT JOIN mm c
       ON c.merchant_id = p.merchant_id AND c.month = :report_month
WHERE p.month = strftime('%Y-%m', date(:report_month || '-01', '-1 month'))
  AND p.gmv >= 50000
  AND COALESCE(c.gmv, 0) < 0.6 * p.gmv
ORDER BY p.gmv - COALESCE(c.gmv, 0) DESC
LIMIT 25;
