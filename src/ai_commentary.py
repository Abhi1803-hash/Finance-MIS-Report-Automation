"""
AI-written management commentary for the monthly finance report, using Claude.

Design choices that keep the AI output trustworthy for finance:
  1. Claude only ever sees *aggregated* KPIs (no transaction-level or merchant PII).
  2. The prompt tells Claude to use only the numbers it is given.
  3. A verification step extracts every number Claude wrote and checks it against the
     facts sheet. Any number that cannot be traced back is flagged in the report, so a
     human reviewer knows exactly what to double-check.
  4. If no API key is set (or the call fails), a deterministic rule-based commentary
     is produced instead, so the scheduled report never breaks.
"""
from __future__ import annotations

import json
import os
import re

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

SYSTEM_PROMPT = """You are a business-finance analyst at an Indian payment gateway writing the
monthly MIS commentary for the CFO. Write in crisp, plain business English.

Rules:
- Use ONLY numbers present in the FACTS JSON. Do not compute new figures, do not round
  differently, do not invent causes that are not supported by the facts.
- Amounts are in INR. Write crores/lakhs exactly as given in the *_display fields.
- Structure: (1) Headline (one sentence), (2) What moved (3-4 bullets),
  (3) Watch-list / risks (2-3 bullets), (4) Data quality note (one bullet).
- Maximum 220 words. No preamble, no sign-off."""


def _inr(x: float) -> str:
    """Format INR in Indian units for humans (and for the LLM to copy verbatim)."""
    if x is None:
        return "n/a"
    if abs(x) >= 1e7:
        return f"₹{x / 1e7:.2f} Cr"
    if abs(x) >= 1e5:
        return f"₹{x / 1e5:.2f} L"
    return f"₹{x:,.0f}"


def build_facts(pnl, methods, segments, declining, dq_log) -> dict:
    cur, prev = pnl.iloc[-1], pnl.iloc[-2] if len(pnl) > 1 else None
    best_method = methods.sort_values("contribution", ascending=False).iloc[0]
    worst_sr = methods.sort_values("success_rate_pct").iloc[0]
    facts = {
        "report_month": cur.month,
        "gmv_display": _inr(cur.gmv),
        "gmv_mom_pct": cur.gmv_mom_pct,
        "gross_revenue_display": _inr(cur.gross_revenue),
        "revenue_mom_pct": cur.revenue_mom_pct,
        "contribution_display": _inr(cur.contribution),
        "contribution_margin_pct": cur.contribution_margin_pct,
        "take_rate_bps": cur.take_rate_bps,
        "prev_take_rate_bps": None if prev is None else prev.take_rate_bps,
        "success_rate_pct": cur.success_rate_pct,
        "prev_success_rate_pct": None if prev is None else prev.success_rate_pct,
        "active_merchants": int(cur.active_merchants),
        "refunds_display": _inr(cur.refunds),
        "top_contribution_method": best_method.method,
        "top_method_contribution_display": _inr(best_method.contribution),
        "lowest_success_method": worst_sr.method,
        "lowest_success_rate_pct": worst_sr.success_rate_pct,
        "upi_gmv_share_pct": float(methods.loc[methods.method == "UPI", "gmv_share_pct"].sum()),
        "segments": [
            {"segment": r.segment, "gmv_display": _inr(r.gmv), "gmv_mom_pct": r.gmv_mom_pct,
             "take_rate_bps": r.take_rate_bps}
            for r in segments.itertuples()
        ],
        "decline_threshold_pct": 40,
        "declining_merchants_count": int(len(declining)),
        "declining_merchants_gmv_at_risk_display": _inr(
            float((declining.prev_month_gmv - declining.report_month_gmv).sum())) if len(declining) else "₹0",
        "dq_rows_quarantined": int(dq_log.loc[dq_log.severity == "blocking"]
                                   .query("table == 'transactions' and check != 'Control total: input = clean + quarantined'")
                                   .rows_affected.sum()),
        "dq_checks_failed": int((dq_log.status == "FAIL").sum()),
        "dq_checks_total": int(len(dq_log)),
    }
    return facts


def _numbers(text: str) -> set[str]:
    """Pull numeric tokens (e.g. 12.4, 1,203, 38.6) out of text, normalised."""
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*\.?\d*", text)}


def verify_numbers(commentary: str, facts: dict) -> list[str]:
    """Return numbers in the commentary that do not appear anywhere in the facts."""
    allowed = _numbers(json.dumps(facts, ensure_ascii=False))
    allowed |= {facts["report_month"][:4], facts["report_month"][5:]}
    found = _numbers(commentary)
    # ignore list numbering and trivially small integers used as counts in prose (1-4)
    return sorted(n for n in found - allowed if n not in {"1", "2", "3", "4"})


def rule_based_commentary(f: dict) -> str:
    seg_lines = "; ".join(f"{s['segment']} {s['gmv_display']} ({s['gmv_mom_pct']:+}% MoM)"
                          for s in f["segments"])
    tr_dir = "up" if (f["prev_take_rate_bps"] or 0) < f["take_rate_bps"] else "down"
    return (
        f"**Headline:** {f['report_month']} GMV was {f['gmv_display']} ({f['gmv_mom_pct']:+}% MoM) with gross "
        f"revenue of {f['gross_revenue_display']} ({f['revenue_mom_pct']:+}% MoM) and contribution of "
        f"{f['contribution_display']} at a {f['contribution_margin_pct']}% margin.\n\n"
        f"**What moved**\n"
        f"- Take rate was {f['take_rate_bps']} bps, {tr_dir} from {f['prev_take_rate_bps']} bps last month.\n"
        f"- UPI carried {f['upi_gmv_share_pct']}% of GMV; {f['top_contribution_method']} was the largest "
        f"contribution driver at {f['top_method_contribution_display']}.\n"
        f"- Segments: {seg_lines}.\n"
        f"- Payment success rate was {f['success_rate_pct']}% (prior month {f['prev_success_rate_pct']}%) across "
        f"{f['active_merchants']} active merchants.\n\n"
        f"**Watch-list**\n"
        f"- {f['declining_merchants_count']} merchants dropped more than {f['decline_threshold_pct']}% MoM, putting "
        f"{f['declining_merchants_gmv_at_risk_display']} of GMV at risk.\n"
        f"- {f['lowest_success_method']} has the weakest success rate at {f['lowest_success_rate_pct']}%.\n"
        f"- Refunds for the month were {f['refunds_display']}.\n\n"
        f"**Data quality:** {f['dq_rows_quarantined']} transaction rows were quarantined; "
        f"{f['dq_checks_failed']} of {f['dq_checks_total']} checks raised issues (see DQ log)."
    )


def generate_commentary(facts: dict) -> tuple[str, str, list[str]]:
    """Return (commentary_markdown, source, unverified_numbers)."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=MODEL,
                max_tokens=700,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user",
                           "content": "FACTS:\n" + json.dumps(facts, indent=2, ensure_ascii=False)}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
            return text, f"Claude ({MODEL})", verify_numbers(text, facts)
        except Exception as exc:  # network / auth / quota: never break the scheduled run
            print(f"[ai_commentary] Claude call failed ({exc.__class__.__name__}); using rule-based fallback")
    text = rule_based_commentary(facts)
    return text, "Rule-based fallback (set ANTHROPIC_API_KEY to enable Claude)", verify_numbers(text, facts)
