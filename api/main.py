"""
ProfileWatch — monitors small-business online reputation ("customer profiles")
the way an SRE monitors production services: a health score, SLA thresholds,
incident detection, and LLM-powered auto-remediation (open-weight models via Groq).

Run with (from the repo root — main.py itself lives in src/):
    pip install -r src/requirements.txt
    cp .env.example .env   # then fill in GROQ_API_KEY, or export it directly
    python src/main.py

Then open http://localhost:8000/
"""

from __future__ import annotations

import difflib
import hmac
import json
import os
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import groq
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from supabase import Client, create_client

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent

# Loads GROQ_API_KEY (and any PROFILEWATCH_* override) from a local .env
# file if present, without overriding whatever's already set in the shell
# environment. No .env file is required — export vars manually and this is a
# no-op. .env lives at the repo root (src/'s parent), not next to this file,
# so the path is explicit rather than relying on load_dotenv()'s own search.
load_dotenv(REPO_ROOT / ".env")

# The account fixture (real experience.com-sourced review/listing data) lives
# in Supabase, not in the repo, so it's never exposed by being world-readable
# on GitHub. The service_role key bypasses RLS, so it must only ever live in
# this server's environment (Render), never sent to the browser.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

# The fixture data is dated relative to a fixed "as of" date rather than
# wall-clock time, so the demo's health scores and incidents stay stable no
# matter when you run it. Override with PROFILEWATCH_ANCHOR_DATE if you
# extend the fixture with your own more-recent dates.
ANCHOR_DATE = date.fromisoformat(os.environ.get("PROFILEWATCH_ANCHOR_DATE", "2026-08-14"))

# Health score below this is an SRE-style "SLA breach" -> incident.
HEALTH_SLA_THRESHOLD = int(os.environ.get("PROFILEWATCH_HEALTH_THRESHOLD", "60"))

# Softer thresholds for "improvement opportunities" — real weaknesses (e.g. a
# 0% review-response rate) that don't drag the blended score below the SLA
# line but are still worth surfacing. This is what tends to separate a big
# firm with a team managing its reputation from a solo professional who
# doesn't have the time — the SLA-breach incidents above catch the acute
# cases, this catches the "quietly falling behind" cases.
RESPONSE_RATE_OPPORTUNITY_THRESHOLD = int(
    os.environ.get("PROFILEWATCH_RESPONSE_RATE_OPPORTUNITY_THRESHOLD", "50")
)
SLOW_RESPONSE_DAYS_THRESHOLD = int(os.environ.get("PROFILEWATCH_SLOW_RESPONSE_DAYS_THRESHOLD", "3"))

# Response-quality and rating-velocity thresholds follow the same
# hard-incident-vs-soft-opportunity pairing pattern as the two above.
RESPONSE_QUALITY_THRESHOLD = int(os.environ.get("PROFILEWATCH_RESPONSE_QUALITY_THRESHOLD", "50"))
VELOCITY_INCIDENT_THRESHOLD = int(os.environ.get("PROFILEWATCH_VELOCITY_INCIDENT_THRESHOLD", "30"))
VELOCITY_OPPORTUNITY_THRESHOLD = int(os.environ.get("PROFILEWATCH_VELOCITY_OPPORTUNITY_THRESHOLD", "40"))

# Open-weight model (served by Groq's free tier) used for all AI-generated
# content. gpt-oss is one of the Groq models that supports strict JSON-schema
# output, which every AI endpoint here depends on — pick another model only if
# it also supports strict mode.
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# Health-score methodology version. weekly_health_history points are stamped
# with the formula version that produced them. There's no snapshot of the
# reviews/listings behind each historical point, so an old point can never be
# retroactively recomputed under a new formula — this lets churn-risk (and
# the frontend's trend chart) tell "a real decline" apart from "we just
# changed how we measure," the same way a real monitoring dashboard
# annotates an SLI-definition change instead of pretending the line is
# continuous across it.
HEALTH_SCORE_FORMULA_VERSION = 2


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set — the account "
            "data is stored in Supabase, not in this repo."
        )
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def load_accounts() -> list[dict[str, Any]]:
    # A single embedded (graph) query: accounts joined to their reviews,
    # listings, and health-history rows in one round trip, via Postgrest's
    # foreign-key embedding. This runs with the service_role key, so it
    # bypasses RLS and sees every account — RLS is what limits a signed-in
    # subscriber's own direct Supabase queries to just their own account;
    # this server-side path never enforces that restriction (it needs every
    # account to compute scores, run the weekly digest, etc.), so callers of
    # this API are only allowed in by the access-key + rate-limit guards.
    rows = (
        get_supabase()
        .table("xmp_accounts")
        .select("id,business_name,category,account_type,source_url,"
                "reviews:xmp_reviews(*),listings:xmp_listings(*),weekly_health_history:xmp_weekly_health_history(*)")
        .order("id")
        .execute()
        .data
    )
    accounts = []
    for row in rows:
        reviews = sorted(
            ({k: v for k, v in r.items() if k != "account_id"} for r in row.pop("reviews", [])),
            key=lambda r: r["date"],
        )
        listings = [{k: v for k, v in l.items() if k not in ("id", "account_id")} for l in row.pop("listings", [])]
        history = sorted(
            ({k: v for k, v in h.items() if k != "account_id"} for h in row.pop("weekly_health_history", [])),
            key=lambda h: h["week_ending"],
        )
        accounts.append({**row, "reviews": reviews, "listings": listings, "weekly_health_history": history})
    return accounts


ACCOUNTS: list[dict[str, Any]] = load_accounts()


def find_account(account_id: str) -> dict[str, Any] | None:
    return next((a for a in ACCOUNTS if a["id"] == account_id), None)


# --------------------------------------------------------------------------
# Core SRE-style monitoring logic
# --------------------------------------------------------------------------

def _normalize(value: str) -> str:
    """Lowercase and strip everything but letters/digits, so cosmetic
    formatting differences (punctuation, casing, spacing) don't count as
    NAP drift — only genuinely different name/address/phone values do."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def compute_nap_inconsistencies(listings: list[dict[str, str]]) -> list[str]:
    """Return which of name/address/phone disagree across 2+ listings."""
    fields = ["name", "address", "phone"]
    inconsistent = []
    for field in fields:
        normalized_values = {_normalize(listing[field]) for listing in listings}
        if len(normalized_values) > 1:
            inconsistent.append(field)
    return inconsistent


# Directory-authority weights for NAP-severity scoring — a mismatch on a
# high-authority directory (the one most consumers and search engines
# actually see) matters more than the same mismatch on a minor one.
DIRECTORY_AUTHORITY = {
    "Google Business Profile": 1.0,
    "Yelp": 0.8,
    "Bing Places": 0.6,
    "Facebook": 0.6,
    "Experience.com": 0.5,
}
DEFAULT_DIRECTORY_AUTHORITY = 0.5
NAP_FIELD_BASE_PENALTY = {"name": 25, "address": 20, "phone": 25}
# A transposed-digit phone number still reaches a wrong or dead line, unlike
# a merely-cosmetic address variant — floor its similarity ratio so it's
# never scored as trivially as a missing suite number would be.
PHONE_MISMATCH_SEVERITY_FLOOR = 0.25


def compute_nap_severity(listings: list[dict[str, str]]) -> dict[str, Any]:
    """
    Severity-weighted alternative to the binary compute_nap_inconsistencies:
    penalizes each disagreeing field by how different it actually is
    (edit-distance-based) and by how authoritative the disagreeing directory
    is, instead of a flat -25/field regardless of a 1-character typo vs. a
    completely different value.
    """
    if len(listings) < 2:
        return {"nap_severity_score": 100.0, "reference_directory": None, "mismatches": []}

    fields = ["name", "address", "phone"]
    reference = max(
        listings,
        key=lambda l: DIRECTORY_AUTHORITY.get(l["directory"], DEFAULT_DIRECTORY_AUTHORITY),
    )

    mismatches: list[dict[str, Any]] = []
    total_penalty = 0.0
    for field in fields:
        ref_value = _normalize(reference[field])
        for listing in listings:
            if listing is reference:
                continue
            other_value = _normalize(listing[field])
            if other_value == ref_value:
                continue
            diff_ratio = 1 - difflib.SequenceMatcher(None, ref_value, other_value).ratio()
            if field == "phone":
                diff_ratio = max(diff_ratio, PHONE_MISMATCH_SEVERITY_FLOOR)
            authority = DIRECTORY_AUTHORITY.get(listing["directory"], DEFAULT_DIRECTORY_AUTHORITY)
            penalty = NAP_FIELD_BASE_PENALTY[field] * diff_ratio * authority
            total_penalty += penalty
            mismatches.append({
                "field": field,
                "directory": listing["directory"],
                "diff_ratio": round(diff_ratio, 3),
                "authority_weight": authority,
                "penalty": round(penalty, 2),
            })

    return {
        "nap_severity_score": round(max(0.0, 100.0 - total_penalty), 1),
        "reference_directory": reference["directory"],
        "mismatches": mismatches,
    }


# Complaint-theme keyword taxonomy — gated on rating <= 3, so a positive
# review that happens to mention "billing" in passing doesn't get flagged.
COMPLAINT_THEMES = {
    "billing": ["charg", "bill", "membership", "refund", "fee"],
    "quality_equipment": ["broken", "old", "outdated", "need work", "never installed"],
    "service_attitude": ["rude", "unhelpful", "unprofessional"],
    "wait_time": ["slow", "late", "took longer", "took a while", "hour late"],
}
THEME_ISOLATED_PENALTY = 5
THEME_SYSTEMIC_PENALTY = 15


def compute_sentiment_themes(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Beyond the numeric star average: names *which* specific complaint keeps
    coming up (billing, equipment, attitude, wait time) and whether it's an
    isolated one-off or a systemic pattern (2+ reviews) — a signal a flat
    average can't surface. A steady 4-star business with one recurring,
    specific complaint looks identical to one with scattered unrelated
    gripes under a plain average; this tells them apart.
    """
    theme_hits: dict[str, list[str]] = {theme: [] for theme in COMPLAINT_THEMES}
    for r in reviews:
        if r["rating"] > 3:
            continue
        text = r["text"].lower()
        for theme, keywords in COMPLAINT_THEMES.items():
            if any(kw in text for kw in keywords):
                theme_hits[theme].append(r["id"])

    themes: dict[str, Any] = {}
    penalty = 0
    for theme, review_ids in theme_hits.items():
        if not review_ids:
            continue
        severity = "systemic" if len(review_ids) >= 2 else "isolated"
        themes[theme] = {"review_ids": review_ids, "severity": severity}
        penalty += THEME_SYSTEMIC_PENALTY if severity == "systemic" else THEME_ISOLATED_PENALTY

    return {
        "sentiment_theme_score": round(max(0.0, 100.0 - penalty), 1),
        "themes": themes,
    }


# Words that signal a reply is actually engaging with the complaint (a
# concrete remedy) rather than just acknowledging it happened.
REMEDY_KEYWORDS = ["refund", "fix", "call us", "email us", "reach out", "replace", "credit", "corrected"]
GENERIC_REPLY_OPENERS = ["thanks for your review", "thank you for your feedback", "we appreciate your feedback"]
MIN_SUBSTANTIVE_REPLY_LENGTH = 40


def compute_response_quality(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Whether a reply *addresses* the complaint, not just whether one exists.
    Previously any responded=True review counted identically — a boilerplate
    "Thanks for your review!" scored the same as a specific, on-brand fix.
    Reviews with no response_text yet (not backfilled) fall back to the same
    neutral-default pattern used elsewhere in this module for missing data —
    an explicit, temporary state, not a bug.
    """
    details: list[dict[str, Any]] = []
    scored: list[float] = []

    for r in reviews:
        if not r.get("responded"):
            continue
        text = r.get("response_text")
        if not text:
            details.append({"review_id": r["id"], "score": 70.0, "flags": ["not_yet_backfilled"]})
            scored.append(70.0)
            continue

        lowered = text.lower()
        complaint_text = r["text"].lower()
        flags: list[str] = []
        score = 40.0

        if len(text) >= MIN_SUBSTANTIVE_REPLY_LENGTH:
            score += 20.0
            flags.append("substantive_length")

        matched_theme_kws = any(
            kw in complaint_text and kw in lowered
            for kws in COMPLAINT_THEMES.values()
            for kw in kws
        )
        if matched_theme_kws or any(kw in lowered for kw in REMEDY_KEYWORDS):
            score += 30.0
            flags.append("addresses_theme")

        is_pure_boilerplate = (
            any(opener in lowered for opener in GENERIC_REPLY_OPENERS)
            and len(text) < MIN_SUBSTANTIVE_REPLY_LENGTH + 20
        )
        if is_pure_boilerplate:
            score -= 20.0
            flags.append("generic")
        else:
            score += 10.0

        score = max(0.0, min(100.0, score))
        details.append({"review_id": r["id"], "score": round(score, 1), "flags": flags})
        scored.append(score)

    overall = round(sum(scored) / len(scored), 1) if scored else 70.0
    return {"response_quality_score": overall, "details": details}


def compute_rating_velocity(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Older-half vs. recent-half average rating, instead of one flat average —
    a business sliding 4.8->5.0 and one sliding 5.0->4.2 look identical under
    a flat 30-day average. This tells them apart, and can catch a decline
    before the lagging blended score crosses the SLA line.
    """
    dated = sorted(reviews, key=lambda r: r["date"])
    n = len(dated)
    if n < 2:
        return {"rating_velocity_score": 50.0, "older_avg_pct": None, "recent_avg_pct": None, "direction": "flat"}

    mid = n // 2
    older, recent = dated[:mid], dated[mid:]
    older_pct = (sum(r["rating"] for r in older) / len(older)) / 5 * 100
    recent_pct = (sum(r["rating"] for r in recent) / len(recent)) / 5 * 100
    velocity_score = max(0.0, min(100.0, 50 + (recent_pct - older_pct)))
    direction = "improving" if recent_pct > older_pct else "declining" if recent_pct < older_pct else "flat"

    return {
        "rating_velocity_score": round(velocity_score, 1),
        "older_avg_pct": round(older_pct, 1),
        "recent_avg_pct": round(recent_pct, 1),
        "direction": direction,
    }


def compute_health_score(account: dict[str, Any], as_of: date) -> dict[str, Any]:
    """
    Health score (0-100), blended from six signals — the reputation-
    management equivalents of SLO components:

      20%: % of reviews responded to within 24h (response speed)
      15%: whether replies actually address the complaint (response quality)
      25%: average star rating over the last 30 days (rating level)
      10%: older-half vs. recent-half rating trend (rating velocity)
      10%: recurring complaint themes surfaced from review text (sentiment)
      20%: NAP (name/address/phone) consistency, severity- and
           authority-weighted across directory listings
    """
    reviews = account["reviews"]
    listings = account["listings"]

    # --- Response-time component ---
    if reviews:
        within_24h = 0
        for r in reviews:
            if r["responded"] and r.get("response_date"):
                review_date = date.fromisoformat(r["date"])
                response_date = date.fromisoformat(r["response_date"])
                if (response_date - review_date).days <= 1:
                    within_24h += 1
        response_rate_24h = (within_24h / len(reviews)) * 100
    else:
        response_rate_24h = 100.0

    # --- Rating-level component (last 30 days, falling back to all-time) ---
    cutoff = as_of - timedelta(days=30)
    recent = [r for r in reviews if date.fromisoformat(r["date"]) >= cutoff]
    rated_pool = recent if recent else reviews
    if rated_pool:
        avg_rating_30d = sum(r["rating"] for r in rated_pool) / len(rated_pool)
        rating_level_score = (avg_rating_30d / 5) * 100
    else:
        avg_rating_30d = None
        rating_level_score = 70.0  # neutral default when there's no review data at all

    # --- The four richer signals ---
    theme_result = compute_sentiment_themes(reviews)
    quality_result = compute_response_quality(reviews)
    velocity_result = compute_rating_velocity(reviews)
    nap_result = compute_nap_severity(listings)

    # --- Binary NAP list, kept for display/back-compat alongside the
    #     severity-weighted score above ---
    inconsistent_fields = compute_nap_inconsistencies(listings)

    weights = {
        "response_rate_24h": 0.20,
        "response_quality": 0.15,
        "rating_level": 0.25,
        "rating_velocity": 0.10,
        "sentiment_theme": 0.10,
        "nap_severity": 0.20,
    }
    values = {
        "response_rate_24h": response_rate_24h,
        "response_quality": quality_result["response_quality_score"],
        "rating_level": rating_level_score,
        "rating_velocity": velocity_result["rating_velocity_score"],
        "sentiment_theme": theme_result["sentiment_theme_score"],
        "nap_severity": nap_result["nap_severity_score"],
    }
    labels = {
        "response_rate_24h": "Response time",
        "response_quality": "Response quality",
        "rating_level": "Rating",
        "rating_velocity": "Rating trend",
        "sentiment_theme": "Complaint themes",
        "nap_severity": "Listing consistency",
    }

    score_components = []
    health_score_raw = 0.0
    for key, weight in weights.items():
        value = values[key]
        contribution = value * weight
        health_score_raw += contribution
        score_components.append({
            "key": key,
            "label": labels[key],
            "weight": weight,
            "value": round(value, 1),
            "max": round(weight * 100, 1),
            "contribution": round(contribution, 2),
        })

    health_score = max(0, min(100, round(health_score_raw)))

    return {
        "health_score": health_score,
        "health_score_formula_version": HEALTH_SCORE_FORMULA_VERSION,
        "score_components": score_components,
        "response_rate_24h": round(response_rate_24h, 1),
        "avg_rating_30d": round(avg_rating_30d, 2) if avg_rating_30d is not None else None,
        "nap_inconsistent_fields": inconsistent_fields,
        "nap_inconsistency_count": len(inconsistent_fields),
        "nap_severity_detail": nap_result,
        "complaint_themes": theme_result["themes"],
        "response_quality_details": quality_result["details"],
        "rating_velocity": velocity_result,
    }


def detect_incidents(account: dict[str, Any], health: dict[str, Any], as_of: date) -> list[dict[str, Any]]:
    """
    Flag incidents the way an SRE dashboard flags service problems:
      1. health score below the SLA threshold
      2. a 1-2 star review left unanswered for >24h
      3. NAP data disagreeing across 2+ directory listings (severity-weighted)
      4. a negative review that WAS replied to, but the reply doesn't
         actually address it — acked the page, didn't fix anything
      5. rating trend declining sharply enough to catch it before the
         lagging blended score crosses the SLA line
    """
    incidents: list[dict[str, Any]] = []

    if health["health_score"] < HEALTH_SLA_THRESHOLD:
        incidents.append({
            "type": "low_health_score",
            "severity": "high" if health["health_score"] < 45 else "medium",
            "summary": (
                f"Health score is {health['health_score']}, below the "
                f"{HEALTH_SLA_THRESHOLD}-point SLA threshold."
            ),
        })

    quality_by_id = {d["review_id"]: d for d in health["response_quality_details"]}

    for r in account["reviews"]:
        if r["rating"] <= 2 and not r["responded"]:
            review_date = date.fromisoformat(r["date"])
            age_hours = (as_of - review_date).days * 24
            if age_hours > 24:
                incidents.append({
                    "type": "unanswered_negative_review",
                    "severity": "high",
                    "summary": (
                        f"{r['rating']}-star review from {r['date']} has gone "
                        f"unanswered for {age_hours}+ hours."
                    ),
                    "review": r,
                })
        elif r["rating"] <= 2 and r["responded"]:
            quality = quality_by_id.get(r["id"])
            if quality and quality["score"] < RESPONSE_QUALITY_THRESHOLD:
                incidents.append({
                    "type": "low_quality_response",
                    "severity": "medium",
                    "summary": (
                        f"{r['rating']}-star review from {r['date']} got a reply, but it doesn't "
                        "read as actually addressing the complaint."
                    ),
                    "review": r,
                })

    if health["nap_inconsistency_count"] >= 1 and len(account["listings"]) >= 2:
        fields = ", ".join(health["nap_inconsistent_fields"])
        max_penalty = max(
            (m["penalty"] for m in health["nap_severity_detail"]["mismatches"]), default=0
        )
        incidents.append({
            "type": "nap_drift",
            "severity": "high" if max_penalty >= 15 else "medium",
            "summary": (
                f"Listings disagree on {fields} across "
                f"{len(account['listings'])} directories."
            ),
        })

    velocity = health["rating_velocity"]
    if (
        velocity["direction"] == "declining"
        and velocity["rating_velocity_score"] < VELOCITY_INCIDENT_THRESHOLD
        and len(account["reviews"]) >= 3
    ):
        incidents.append({
            "type": "rating_velocity_decline",
            "severity": "high" if velocity["rating_velocity_score"] <= 15 else "medium",
            "summary": (
                f"Rating trend is declining — recent reviews average "
                f"{velocity['recent_avg_pct']}% vs. {velocity['older_avg_pct']}% earlier — "
                "worth catching before the blended score reflects it."
            ),
        })

    return incidents


def compute_improvement_opportunities(account: dict[str, Any], health: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Softer signals than detect_incidents(): real weaknesses that don't breach
    the SLA threshold but still represent room to improve. A perfect 5-star
    average with a 0% response rate nets out to exactly the SLA line under
    the blended score, so without this, an account that never responds to a
    single review shows as "no open incidents" — which misses the point for
    an account run by one person with no time to keep up with replies.
    """
    opportunities: list[dict[str, Any]] = []
    reviews = account["reviews"]

    if health["response_rate_24h"] < RESPONSE_RATE_OPPORTUNITY_THRESHOLD:
        opportunities.append({
            "type": "low_response_rate",
            "summary": (
                f"Only {health['response_rate_24h']}% of reviews get a reply within 24 hours. "
                "Responding quickly builds trust with people reading reviews later."
            ),
        })

    response_delays_days = []
    for r in reviews:
        if r["responded"] and r.get("response_date"):
            review_date = date.fromisoformat(r["date"])
            response_date = date.fromisoformat(r["response_date"])
            response_delays_days.append((response_date - review_date).days)

    if response_delays_days:
        avg_delay = sum(response_delays_days) / len(response_delays_days)
        if avg_delay > SLOW_RESPONSE_DAYS_THRESHOLD:
            opportunities.append({
                "type": "slow_response_time",
                "summary": (
                    f"Replies come in, but average {avg_delay:.1f} days after a review posts — "
                    "worth carving out regular time to answer reviews sooner."
                ),
            })

    for theme, detail in health["complaint_themes"].items():
        if detail["severity"] == "systemic":
            opportunities.append({
                "type": "recurring_complaint_theme",
                "summary": (
                    f"Multiple reviews mention the same issue ({theme.replace('_', ' ')}) — "
                    "worth addressing the root cause, not just replying to each review individually."
                ),
            })

    velocity = health["rating_velocity"]
    if (
        velocity["direction"] == "declining"
        and VELOCITY_INCIDENT_THRESHOLD <= velocity["rating_velocity_score"] < VELOCITY_OPPORTUNITY_THRESHOLD
    ):
        opportunities.append({
            "type": "mild_rating_decline",
            "summary": (
                f"Recent reviews are trending a bit lower than earlier ones "
                f"({velocity['recent_avg_pct']}% vs. {velocity['older_avg_pct']}%) — "
                "not urgent yet, but worth watching."
            ),
        })

    return opportunities


def compute_churn_risk(weekly_health_history: list[dict[str, Any]], rating_velocity_score: float) -> dict[str, Any]:
    """
    Churn risk = health score has declined for 2+ consecutive weeks, counting
    backward from the most recent data point — but only over the trailing
    run of points computed under the SAME formula version as the latest one,
    so a pure methodology change can't masquerade as a real decline (see
    HEALTH_SCORE_FORMULA_VERSION). Adds magnitude (how much it dropped) and a
    corroborating signal (independently-declining rating velocity) instead of
    a bare boolean off one blunt heuristic.
    """
    if not weekly_health_history:
        return {"churn_risk": False, "churn_risk_level": "none", "consecutive_weeks_declining": 0, "magnitude": 0}

    latest_version = weekly_health_history[-1].get("formula_version", HEALTH_SCORE_FORMULA_VERSION)
    trailing = []
    for point in reversed(weekly_health_history):
        if point.get("formula_version", HEALTH_SCORE_FORMULA_VERSION) != latest_version:
            break
        trailing.append(point)
    trailing.reverse()

    scores = [w["score"] for w in trailing]
    streak = 0
    for i in range(len(scores) - 1, 0, -1):
        if scores[i] < scores[i - 1]:
            streak += 1
        else:
            break
    magnitude = (scores[len(scores) - 1 - streak] - scores[-1]) if streak > 0 else 0

    if streak >= 3 or (streak >= 2 and magnitude >= 8):
        level = "high"
    elif streak == 1 and rating_velocity_score < VELOCITY_INCIDENT_THRESHOLD:
        level = "watch"
    else:
        level = "none"

    return {
        "churn_risk": level != "none",
        "churn_risk_level": level,
        "consecutive_weeks_declining": streak,
        "magnitude": magnitude,
    }


def build_account_view(account: dict[str, Any]) -> dict[str, Any]:
    health = compute_health_score(account, ANCHOR_DATE)
    incidents = detect_incidents(account, health, ANCHOR_DATE)
    opportunities = compute_improvement_opportunities(account, health)
    churn = compute_churn_risk(account["weekly_health_history"], health["rating_velocity"]["rating_velocity_score"])
    return {
        "id": account["id"],
        "business_name": account["business_name"],
        "category": account.get("category"),
        "account_type": account.get("account_type", "individual"),
        **health,
        "incidents": incidents,
        "improvement_opportunities": opportunities,
        "churn_risk": churn,
        "weekly_health_history": account["weekly_health_history"],
        "reviews": account["reviews"],
        "listings": account["listings"],
    }


# --------------------------------------------------------------------------
# LLM integration (Groq)
# --------------------------------------------------------------------------

INCIDENT_SYSTEM_PROMPT = (
    "You are ProfileWatch's incident analyst for local-business online reputation "
    "management. You'll be given one account's health metrics and open incidents, "
    "including complaint themes, rating trend, and listing-mismatch detail where "
    "relevant. Write for the business owner, not a technical audience: plain "
    "English, no jargon, no filler. Root cause: 2-4 sentences on what's actually "
    "driving the incident(s), referencing the SPECIFIC data given (which theme, "
    "which directory disagrees, which direction the trend is moving) rather than "
    "generic language. Suggested response: a warm, specific, on-brand reply to "
    "the single most urgent unanswered negative review, addressing the complaint "
    "directly and inviting the reviewer to follow up offline — never generic or "
    "defensive. If there is no unanswered negative review, say plainly that no "
    "reply is needed right now. Action plan: 2-4 ordered, concrete steps the "
    "owner can actually take, each with a short reason it will help and a "
    "realistic timeframe — not vague advice like 'improve communication.'"
)

DIGEST_SYSTEM_PROMPT = (
    "You write a weekly reputation-management digest for busy small-business "
    "owners who manage their online presence through ProfileWatch and don't have "
    "time to check a dashboard. For each account you're given, write one short "
    "paragraph (3-5 sentences) in plain English: how things are going overall, "
    "any open incidents and what they actually mean for the business, and what "
    "to do next if anything. If an account looks healthy, say so briefly and "
    "move on — don't manufacture urgency where none exists. Also give one "
    "single top priority per account: the one thing worth doing this week, in "
    "under 12 words."
)

SAVE_PLAY_SYSTEM_PROMPT = (
    "You advise Customer Success Managers at a company that sells online "
    "reputation monitoring to small businesses. You'll be given an account "
    "whose health score has declined for multiple consecutive weeks and is at "
    "risk of churning. Write one concrete, specific paragraph a CSM could act "
    "on this week to save the account — name the likely reason they're "
    "disengaging based on the data, and propose a specific outreach or fix, not "
    "generic advice like 'reach out to the customer.'"
)

SUCCESS_PLAN_SYSTEM_PROMPT = (
    "You advise small-business owners using ProfileWatch on how to actually "
    "improve their online reputation — not just fix what's broken, but get "
    "measurably better. You'll be given an account's full picture: health score "
    "breakdown, open incidents, softer improvement opportunities, churn risk, "
    "and rating trend. Write a brief overall assessment (2-3 sentences, plain "
    "English), then 1-5 ranked priorities — the highest-leverage things to do, "
    "ordered by impact, each with a concrete action, why it will help, and a "
    "realistic timeframe. If the account has no open incidents, focus "
    "priorities on the softer opportunities and on protecting what's already "
    "working. Close with a one-sentence, concrete definition of what success "
    "looks like a month from now — not a vague aspiration."
)

INCIDENT_EXPLANATION_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {
            "type": "string",
            "description": "Plain-English root-cause explanation, 2-4 sentences.",
        },
        "suggested_response": {
            "type": "string",
            "description": (
                "An on-brand, ready-to-post reply to the most urgent unanswered "
                "negative review, or a short note that no reply is needed."
            ),
        },
        "action_plan": {
            "type": "array",
            "description": "Exactly 2-4 ordered, concrete steps to actually fix this and improve.",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "integer"},
                    "action": {"type": "string"},
                    "why_it_works": {"type": "string"},
                    "timeframe": {"type": "string", "enum": ["today", "this week", "this month"]},
                },
                "required": ["step", "action", "why_it_works", "timeframe"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["root_cause", "suggested_response", "action_plan"],
    "additionalProperties": False,
}

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "digests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "business_name": {"type": "string"},
                    "paragraph": {
                        "type": "string",
                        "description": "One plain-English paragraph for a busy owner.",
                    },
                    "top_priority": {
                        "type": "string",
                        "description": "The single most important thing to do this week, under 12 words.",
                    },
                },
                "required": ["account_id", "business_name", "paragraph", "top_priority"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["digests"],
    "additionalProperties": False,
}

SAVE_PLAY_SCHEMA = {
    "type": "object",
    "properties": {
        "save_play": {
            "type": "string",
            "description": "One concrete paragraph a CSM can act on this week.",
        },
    },
    "required": ["save_play"],
    "additionalProperties": False,
}

SUCCESS_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_assessment": {"type": "string"},
        "priorities": {
            "type": "array",
            "description": "1-5 ranked priorities, ordered by impact.",
            "items": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer"},
                    "focus_area": {"type": "string"},
                    "action": {"type": "string"},
                    "expected_impact": {"type": "string"},
                    "timeframe": {"type": "string", "enum": ["this week", "this month", "ongoing"]},
                },
                "required": ["rank", "focus_area", "action", "expected_impact", "timeframe"],
                "additionalProperties": False,
            },
        },
        "definition_of_success": {"type": "string"},
    },
    "required": ["overall_assessment", "priorities", "definition_of_success"],
    "additionalProperties": False,
}


def get_llm_client() -> groq.Groq:
    if not os.environ.get("GROQ_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail=(
                "GROQ_API_KEY is not set. Get a free key at console.groq.com and set "
                "it in the server's environment before using any AI-powered feature. "
                "The dashboard's health scores and incident detection still work "
                "without it — only the explanations, response drafts, weekly "
                "digest, save plays, and success plans need a key."
            ),
        )
    # Reads GROQ_API_KEY from the environment automatically.
    return groq.Groq()


# gpt-oss is a reasoning model: its hidden reasoning tokens count against the
# completion budget, so each call gets this much headroom on top of the
# visible-answer budget it asks for.
REASONING_TOKEN_HEADROOM = 2048


def call_llm_structured(system: str, user_content: str, schema: dict, max_tokens: int = 1024) -> dict:
    client = get_llm_client()
    try:
        response = client.chat.completions.create(
            model=MODEL,
            max_completion_tokens=max_tokens + REASONING_TOKEN_HEADROOM,
            reasoning_effort="low",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": True},
            },
        )
    except groq.APIConnectionError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach the Groq API: {exc}") from exc
    except groq.APIStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Groq API error ({exc.status_code}): {exc.message}",
        ) from exc

    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise HTTPException(status_code=502, detail="The AI response was cut off before it finished.")
    if not choice.message.content:
        raise HTTPException(status_code=502, detail="The AI returned no text content.")

    try:
        return json.loads(choice.message.content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="The AI returned malformed JSON.") from exc


def generate_incident_explanation(account: dict[str, Any], view: dict[str, Any]) -> dict:
    negative_unanswered = [i["review"] for i in view["incidents"] if i["type"] == "unanswered_negative_review"]
    context = {
        "business_name": account["business_name"],
        "health_score": view["health_score"],
        "score_components": view["score_components"],
        "response_rate_24h_percent": view["response_rate_24h"],
        "avg_rating_30d": view["avg_rating_30d"],
        "rating_velocity": view["rating_velocity"],
        "complaint_themes": view["complaint_themes"],
        "nap_inconsistent_fields": view["nap_inconsistent_fields"],
        "nap_severity_detail": view["nap_severity_detail"] if view["nap_inconsistent_fields"] else None,
        "open_incidents": [{"type": i["type"], "summary": i["summary"]} for i in view["incidents"]],
        "most_urgent_unanswered_negative_review": negative_unanswered[0] if negative_unanswered else None,
    }
    user_content = (
        "Analyze this account's open incidents and respond using the JSON "
        f"schema you were given.\n\n{json.dumps(context, indent=2)}"
    )
    return call_llm_structured(INCIDENT_SYSTEM_PROMPT, user_content, INCIDENT_EXPLANATION_SCHEMA, max_tokens=1536)


def generate_weekly_digest(views: list[dict[str, Any]]) -> dict:
    accounts_context = [
        {
            "account_id": v["id"],
            "business_name": v["business_name"],
            "health_score": v["health_score"],
            "open_incidents": [i["summary"] for i in v["incidents"]],
            "churn_risk": v["churn_risk"]["churn_risk"],
            "churn_risk_level": v["churn_risk"]["churn_risk_level"],
            "consecutive_weeks_declining": v["churn_risk"]["consecutive_weeks_declining"],
        }
        for v in views
    ]
    user_content = (
        "Write the weekly digest for every account below, one paragraph plus one top "
        f"priority each, using the JSON schema you were given.\n\n{json.dumps(accounts_context, indent=2)}"
    )
    return call_llm_structured(DIGEST_SYSTEM_PROMPT, user_content, DIGEST_SCHEMA, max_tokens=4096)


def generate_save_play(account: dict[str, Any], view: dict[str, Any]) -> dict:
    context = {
        "business_name": account["business_name"],
        "health_score": view["health_score"],
        "consecutive_weeks_declining": view["churn_risk"]["consecutive_weeks_declining"],
        "weekly_health_history": account["weekly_health_history"],
        "open_incidents": [i["summary"] for i in view["incidents"]],
    }
    user_content = (
        "This account is flagged for churn risk. Draft the save play using the "
        f"JSON schema you were given.\n\n{json.dumps(context, indent=2)}"
    )
    return call_llm_structured(SAVE_PLAY_SYSTEM_PROMPT, user_content, SAVE_PLAY_SCHEMA, max_tokens=512)


def generate_success_plan(account: dict[str, Any], view: dict[str, Any]) -> dict:
    context = {
        "business_name": account["business_name"],
        "health_score": view["health_score"],
        "score_components": view["score_components"],
        "open_incidents": [i["summary"] for i in view["incidents"]],
        "improvement_opportunities": [o["summary"] for o in view["improvement_opportunities"]],
        "churn_risk": view["churn_risk"],
        "rating_velocity": view["rating_velocity"],
        "complaint_themes": view["complaint_themes"],
    }
    user_content = (
        "Write a prescriptive success plan for this account using the JSON "
        f"schema you were given.\n\n{json.dumps(context, indent=2)}"
    )
    return call_llm_structured(SUCCESS_PLAN_SYSTEM_PROMPT, user_content, SUCCESS_PLAN_SCHEMA, max_tokens=1536)


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------

app = FastAPI(title="ProfileWatch", description="SRE-style monitoring for customer reputation profiles.")

# When the UI is hosted separately (e.g. Netlify), set this to its origin so
# only that site can call the API from a browser. Defaults to "*" for local dev.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("PROFILEWATCH_ALLOWED_ORIGINS", "*").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Every AI call spends the Groq key's (rate-limited) quota, so on a public deployment
# those endpoints (and the admin reload) require this shared key via the
# X-Access-Key header. Unset = no check, so local dev is unchanged.
ACCESS_KEY = os.environ.get("PROFILEWATCH_ACCESS_KEY", "")

# Per-IP backstop in case the access key leaks. In-memory, so it resets on
# restart and is per-instance — fine for a single Render instance.
AI_RATE_LIMIT_PER_HOUR = int(os.environ.get("PROFILEWATCH_AI_RATE_LIMIT_PER_HOUR", "30"))
_ai_calls: dict[str, deque[float]] = defaultdict(deque)


def require_access_key(x_access_key: str | None = Header(default=None)) -> None:
    if ACCESS_KEY and not hmac.compare_digest(x_access_key or "", ACCESS_KEY):
        raise HTTPException(status_code=401, detail="A valid access key is required for this action.")


def ai_rate_limit(request: Request) -> None:
    forwarded = request.headers.get("x-forwarded-for")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    now = time.monotonic()
    calls = _ai_calls[ip]
    while calls and now - calls[0] > 3600:
        calls.popleft()
    if len(calls) >= AI_RATE_LIMIT_PER_HOUR:
        raise HTTPException(status_code=429, detail="Too many AI requests from this address — try again later.")
    calls.append(now)


AI_GUARDS = [Depends(require_access_key), Depends(ai_rate_limit)]


@app.get("/api/status")
def status():
    return {
        "ok": True,
        "ai_configured": bool(os.environ.get("GROQ_API_KEY")),
        "access_key_required": bool(ACCESS_KEY),
        "model": MODEL,
        "anchor_date": ANCHOR_DATE.isoformat(),
        "health_sla_threshold": HEALTH_SLA_THRESHOLD,
        "health_score_formula_version": HEALTH_SCORE_FORMULA_VERSION,
    }


@app.get("/api/accounts")
def list_accounts():
    return {"accounts": [build_account_view(a) for a in ACCOUNTS]}


@app.get("/api/accounts/{account_id}")
def get_account(account_id: str):
    account = find_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    return build_account_view(account)


@app.post("/api/accounts/{account_id}/explain", dependencies=AI_GUARDS)
def explain_incident(account_id: str):
    account = find_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    view = build_account_view(account)
    if not view["incidents"]:
        raise HTTPException(status_code=400, detail="This account has no open incidents to explain.")
    return generate_incident_explanation(account, view)


@app.post("/api/accounts/{account_id}/save-play", dependencies=AI_GUARDS)
def save_play(account_id: str):
    account = find_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    view = build_account_view(account)
    if not view["churn_risk"]["churn_risk"]:
        raise HTTPException(status_code=400, detail="This account is not currently flagged for churn risk.")
    return generate_save_play(account, view)


@app.post("/api/accounts/{account_id}/success-plan", dependencies=AI_GUARDS)
def success_plan(account_id: str):
    """
    Unlike /explain (incident-gated) and /save-play (churn-risk-gated), this
    is available for ANY account — a healthy account can still ask "how do I
    go from 85 to 95," and an account with only soft improvement
    opportunities (no hard incident) gets prescriptive help too.
    """
    account = find_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    view = build_account_view(account)
    return generate_success_plan(account, view)


@app.post("/api/digest", dependencies=AI_GUARDS)
def weekly_digest():
    views = [build_account_view(a) for a in ACCOUNTS]
    return generate_weekly_digest(views)


@app.post("/api/admin/reload-data", dependencies=[Depends(require_access_key)])
def reload_data():
    """
    Re-reads data/accounts.json from disk and swaps it in, without restarting
    the server. Meant to be called after scripts/refresh_accounts.py rewrites
    the file with freshly re-fetched experience.com data — otherwise the only
    way to pick up a changed accounts.json is to kill and relaunch uvicorn.
    """
    global ACCOUNTS
    ACCOUNTS = load_accounts()
    return {"ok": True, "accounts_loaded": len(ACCOUNTS)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
