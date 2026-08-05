"""
Populate the cyber-talent-map market data by pulling cyber-security
professionals from Crustdata for a grid of roles x countries, saving them
to the shared Supabase database via SourcingX's canonical save path, and
computing per-market analytics into market-map.json for the
cyber-talent-map website to read.

Usage (run from the SourcingX repo root so config.json loads):
    python scripts/populate_cyber_talent_map.py --only "security-researcher::Norway"
    python scripts/populate_cyber_talent_map.py --all

--only processes a single market and MERGES its result into the existing
market-map.json (other entries are preserved).
--all processes every role x country combination (168 markets). This is a
big, credit-consuming run — do not launch it without explicit sign-off.
"""

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

# Make sure we can import SourcingX modules when run as `python scripts/xxx.py`
# from the repo root (this already works since repo root is on sys.path when
# invoked that way, but be defensive in case of a different cwd).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from crustdata_search import search_people_db  # noqa: E402
from db import get_supabase_client, save_enriched_profiles_bulk  # noqa: E402


# ---------------------------------------------------------------------------
# Config: 14 roles x 12 countries
# ---------------------------------------------------------------------------

ROLES = [
    {"key": "security-engineer", "label": "Security Engineer", "titles": ["Security Engineer"]},
    {"key": "application-security-engineer", "label": "Application Security Engineer", "titles": ["Application Security"]},
    {"key": "cloud-security-engineer", "label": "Cloud Security Engineer", "titles": ["Cloud Security"]},
    {"key": "devsecops-engineer", "label": "DevSecOps Engineer", "titles": ["DevSecOps"]},
    {"key": "penetration-tester", "label": "Penetration Tester", "titles": ["Penetration Tester", "Pentester"]},
    {"key": "security-researcher", "label": "Security Researcher", "titles": ["Security Researcher"]},
    {"key": "soc-analyst", "label": "SOC Analyst", "titles": ["SOC Analyst"]},
    {"key": "incident-responder", "label": "Incident Responder", "titles": ["Incident Responder", "Incident Response"]},
    {"key": "security-architect", "label": "Security Architect", "titles": ["Security Architect"]},
    {"key": "ciso-head-of-security", "label": "CISO / Head of Security", "titles": [
        "CISO", "Chief Information Security Officer", "Head of Security", "Head of Information Security",
    ]},
    {"key": "red-team-engineer", "label": "Red Team Engineer", "titles": ["Red Team"]},
    {"key": "detection-engineer", "label": "Detection Engineer", "titles": ["Detection Engineer"]},
    {"key": "malware-analyst", "label": "Malware Analyst / Reverse Engineer", "titles": ["Malware Analyst", "Malware Researcher"]},
    {"key": "threat-intelligence-analyst", "label": "Threat Intelligence Analyst", "titles": [
        "Threat Intel", "Cyber Threat", "CTI Analyst", "Threat Researcher",
    ]},
]

COUNTRIES = [
    "United Kingdom", "Germany", "Netherlands", "Sweden", "France", "Ireland",
    "Switzerland", "Spain", "Denmark", "Finland", "Norway", "Israel",
]

ROLES_BY_KEY = {r["key"]: r for r in ROLES}

OUTPUT_PATH = r"C:\Users\gehta\projects\cyber-talent-map\src\data\market-map.json"

SENIOR_LEVELS = ["Senior", "Manager", "Director", "Vice President", "CXO"]
STARTUP_HEADCOUNT_RANGES = ["11-50", "51-200"]

MAX_PAGES = 200
PAGE_LIMIT = 1000


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Filter building
# ---------------------------------------------------------------------------

def build_filters(titles: list, country: str) -> dict:
    """Build the Crustdata filter dict for a role's title list x a country."""
    if len(titles) == 1:
        title_condition = {
            "column": "current_employers.title",
            "type": "[.]",
            "value": titles[0],
        }
    else:
        title_condition = {
            "op": "or",
            "conditions": [
                {"column": "current_employers.title", "type": "[.]", "value": t}
                for t in titles
            ],
        }

    return {
        "op": "and",
        "conditions": [
            title_condition,
            {"column": "location_country", "type": "=", "value": country},
        ],
    }


# ---------------------------------------------------------------------------
# Count-query filter builders (mirror the live website's src/lib/map.ts)
# ---------------------------------------------------------------------------

def _with_extra_condition(base_filters: dict, extra_condition: dict) -> dict:
    """AND one extra condition onto a base {op: and, conditions: [...]} filter.

    Flattens into the same conditions list rather than nesting, matching how
    the website's src/lib/map.ts builds its count-query filters.
    """
    return {
        "op": "and",
        "conditions": [*base_filters["conditions"], extra_condition],
    }


def _one_year_ago_str() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")


def build_senior_filters(base_filters: dict) -> dict:
    return _with_extra_condition(base_filters, {
        "column": "current_employers.seniority_level",
        "type": "in",
        "value": SENIOR_LEVELS,
    })


def build_startup_filters(base_filters: dict) -> dict:
    return _with_extra_condition(base_filters, {
        "column": "current_employers.company_headcount_range",
        "type": "in",
        "value": STARTUP_HEADCOUNT_RANGES,
    })


def build_mobility_filters(base_filters: dict) -> dict:
    return _with_extra_condition(base_filters, {
        "column": "current_employers.start_date",
        "type": ">",
        "value": _one_year_ago_str(),
    })


def run_count_query(filters: dict) -> tuple:
    """Run a limit:1 search just to read total_count. Returns (total_count, credits_used)."""
    result = search_people_db(filters, limit=1)
    return result.get("total_count", 0), result.get("credits_used", 0)


# ---------------------------------------------------------------------------
# Pull-all helper
# ---------------------------------------------------------------------------

def pull_all_profiles(filters: dict) -> tuple:
    """Page through search_people_db until exhausted. Returns (profiles, total_count, credits_used)."""
    profiles = []
    total_count = None
    cursor = None
    page_num = 0
    credits_used = 0

    while page_num < MAX_PAGES:
        page_num += 1
        result = search_people_db(filters, limit=PAGE_LIMIT, cursor=cursor)
        page_profiles = result.get("profiles", [])
        if total_count is None:
            total_count = result.get("total_count", len(page_profiles))
        credits_used += result.get("credits_used", 0) or 0

        log(f"    page {page_num}: got {len(page_profiles)} profiles "
            f"(credits used: {result.get('credits_used')})")

        profiles.extend(page_profiles)

        cursor = result.get("cursor")
        if not cursor or len(page_profiles) == 0:
            break

    if page_num >= MAX_PAGES:
        log(f"    WARNING: hit safety cap of {MAX_PAGES} pages, stopping early")

    return profiles, (total_count if total_count is not None else len(profiles)), credits_used


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
#
# seniorPlus / atStartups / mobility are NOT derived from the pulled profiles
# — they come from three separate Crustdata count queries (limit=1, read
# total_count), built exactly like the live website's src/lib/map.ts does.
# This keeps the dashboard numbers and the loader numbers on the same
# ground truth. total / topEmployers / employerSampleSize, plus the market-
# intelligence aggregates below (topSkills, topSchools, feederCompanies,
# seniorityDistribution, experienceDistribution, tenure stats), are all
# computed locally from the fully-pulled profile list — no extra Crustdata
# queries. Every field here is defensive about missing/None/empty data:
# many profiles have gaps (e.g. some have no skills tagged at all), and a
# gap just doesn't contribute rather than crashing the run.

EXPERIENCE_BUCKET_ORDER = ["0-2", "3-5", "6-10", "11-15", "16+", "Unknown"]


def _first_current_employer(profile: dict):
    """Return the profile's first current employer dict, or None.

    This is the single definition of "current employer" used consistently
    across topEmployers, seniorityDistribution, and the tenure stats, so
    they all agree on which employer counts as "current" for a given
    profile.
    """
    current_employers = profile.get("current_employers") or []
    if not isinstance(current_employers, list) or not current_employers:
        return None
    first = current_employers[0]
    return first if isinstance(first, dict) else None


def compute_top_employers(profiles: list) -> list:
    employer_counts: dict = {}
    for profile in profiles:
        emp = _first_current_employer(profile)
        if not emp:
            continue
        name = emp.get("name") or emp.get("company_name")
        if name:
            employer_counts[name] = employer_counts.get(name, 0) + 1

    top_employers = sorted(employer_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    return [{"name": name, "count": count} for name, count in top_employers]


def compute_top_skills(profiles: list, top_n: int = 20) -> list:
    """Each skill counted once per person, across all profiles in the market."""
    counts: dict = {}
    for profile in profiles:
        skills = profile.get("skills") or []
        if not isinstance(skills, list):
            continue
        seen = set()
        for s in skills:
            if not s:
                continue
            s = str(s).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            counts[s] = counts.get(s, 0) + 1

    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [{"skill": name, "count": count} for name, count in top]


def compute_top_schools(profiles: list, top_n: int = 10) -> list:
    """Each school counted once per person. Sources: education_background
    (list of dicts with institute_name) and all_schools (flat strings, or
    dicts as a defensive fallback), merged per-profile before counting so a
    school listed in both sources isn't double-counted for the same person.
    """
    counts: dict = {}
    for profile in profiles:
        names = set()

        edu = profile.get("education_background") or []
        if isinstance(edu, list):
            for item in edu:
                if isinstance(item, dict):
                    name = item.get("institute_name")
                    if name and str(name).strip():
                        names.add(str(name).strip())

        all_schools = profile.get("all_schools") or []
        if isinstance(all_schools, list):
            for item in all_schools:
                if isinstance(item, str):
                    if item.strip():
                        names.add(item.strip())
                elif isinstance(item, dict):
                    name = item.get("institute_name") or item.get("school") or item.get("name")
                    if name and str(name).strip():
                        names.add(str(name).strip())

        for name in names:
            counts[name] = counts.get(name, 0) + 1

    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [{"school": name, "count": count} for name, count in top]


def compute_feeder_companies(profiles: list, top_n: int = 10) -> list:
    """Prior employers (poaching feeders) — distinct from topEmployers, which
    is current employers. Each company counted once per person.
    """
    counts: dict = {}
    for profile in profiles:
        past = profile.get("past_employers") or []
        if not isinstance(past, list):
            continue
        names = set()
        for item in past:
            if isinstance(item, dict):
                name = item.get("name") or item.get("company_name")
                if name and str(name).strip():
                    names.add(str(name).strip())
        for name in names:
            counts[name] = counts.get(name, 0) + 1

    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [{"company": name, "count": count} for name, count in top]


def compute_seniority_distribution(profiles: list) -> dict:
    """Tally by the current employer's seniority_level (same "current
    employer" selection as topEmployers). Missing/blank -> "Unknown".
    """
    dist: dict = {}
    for profile in profiles:
        emp = _first_current_employer(profile)
        level = emp.get("seniority_level") if emp else None
        bucket = str(level).strip() if level and str(level).strip() else "Unknown"
        dist[bucket] = dist.get(bucket, 0) + 1
    return dist


def _experience_bucket(years) -> str:
    if years is None:
        return "Unknown"
    try:
        y = float(years)
    except (TypeError, ValueError):
        return "Unknown"
    if y < 0:
        return "Unknown"
    if y <= 2:
        return "0-2"
    if y <= 5:
        return "3-5"
    if y <= 10:
        return "6-10"
    if y <= 15:
        return "11-15"
    return "16+"


def compute_experience_distribution(profiles: list) -> dict:
    dist: dict = {}
    for profile in profiles:
        bucket = _experience_bucket(profile.get("years_of_experience_raw"))
        dist[bucket] = dist.get(bucket, 0) + 1
    return dist


def compute_tenure_stats(profiles: list) -> tuple:
    """Median + average years_at_company_raw at the current employer (same
    "current employer" selection as topEmployers). Returns
    (median_or_None, average_or_None), each rounded to 1 decimal.
    """
    values = []
    for profile in profiles:
        emp = _first_current_employer(profile)
        if not emp:
            continue
        raw = emp.get("years_at_company_raw")
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v < 0:
            continue
        values.append(v)

    if not values:
        return None, None

    median = round(statistics.median(values), 1)
    average = round(statistics.mean(values), 1)
    return median, average


def compute_analytics(role_key: str, country: str, profiles: list, total_count: int,
                       senior_plus: int, at_startups: int, mobility: int) -> dict:
    role = ROLES_BY_KEY[role_key]
    role_label = role["label"]

    median_tenure, average_tenure = compute_tenure_stats(profiles)

    return {
        "role": role_key,
        "roleLabel": role_label,
        "country": country,
        "total": total_count,
        "seniorPlus": senior_plus,
        "atStartups": at_startups,
        "mobility": mobility,
        "topEmployers": compute_top_employers(profiles),
        "employerSampleSize": len(profiles),
        "topSkills": compute_top_skills(profiles),
        "topSchools": compute_top_schools(profiles),
        "feederCompanies": compute_feeder_companies(profiles),
        "seniorityDistribution": compute_seniority_distribution(profiles),
        "experienceDistribution": compute_experience_distribution(profiles),
        "medianTenureYears": median_tenure,
        "averageTenureYears": average_tenure,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Output file handling
# ---------------------------------------------------------------------------

def load_existing_output() -> dict:
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log(f"WARNING: could not read existing {OUTPUT_PATH} ({e}); starting fresh")
    return {}


def write_output(data: dict) -> None:
    out_dir = os.path.dirname(OUTPUT_PATH)
    os.makedirs(out_dir, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log(f"Wrote {len(data)} market entries to {OUTPUT_PATH}")


_REQUIRED_ANALYTICS_KEYS = {
    "role", "roleLabel", "country", "total", "seniorPlus", "atStartups",
    "mobility", "topEmployers", "employerSampleSize", "generatedAt",
    # Market-intelligence fields added after the first Norway verification —
    # requiring them here means an --all run will treat any market saved
    # under the old (pre-topSkills) schema as incomplete and reprocess it,
    # rather than skipping it as "already done".
    "topSkills", "topSchools", "feederCompanies", "seniorityDistribution",
    "experienceDistribution", "medianTenureYears", "averageTenureYears",
}


def is_valid_analytics(entry) -> bool:
    """True if entry looks like a complete analytics object (used for resume/skip)."""
    if not isinstance(entry, dict):
        return False
    if not _REQUIRED_ANALYTICS_KEYS.issubset(entry.keys()):
        return False
    return entry.get("total") is not None


# ---------------------------------------------------------------------------
# Per-market processing
# ---------------------------------------------------------------------------

def process_market(role_key: str, country: str, client) -> dict:
    """Pull, save, and compute analytics for one role x country market.

    Returns a dict with keys: analytics, profiles_pulled, saved, errors,
    error_messages, total_count.
    """
    role = ROLES_BY_KEY.get(role_key)
    if role is None:
        raise ValueError(f"Unknown role key: {role_key!r}. Valid keys: {list(ROLES_BY_KEY)}")
    if country not in COUNTRIES:
        raise ValueError(f"Unknown country: {country!r}. Valid countries: {COUNTRIES}")

    log(f"[{role_key}::{country}] pulling from Crustdata...")
    filters = build_filters(role["titles"], country)
    profiles, total_count, pull_credits = pull_all_profiles(filters)
    log(f"[{role_key}::{country}] pulled {len(profiles)} profiles (total_count={total_count})")

    save_stats = {"saved": 0, "errors": 0, "error_messages": []}
    if profiles:
        log(f"[{role_key}::{country}] saving {len(profiles)} profiles to Supabase...")
        save_stats = save_enriched_profiles_bulk(client, profiles)
        log(f"[{role_key}::{country}] saved={save_stats['saved']} errors={save_stats['errors']}")
        if save_stats["error_messages"]:
            for msg in save_stats["error_messages"]:
                log(f"    ERROR: {msg}")
    else:
        log(f"[{role_key}::{country}] no profiles returned, nothing to save")

    log(f"[{role_key}::{country}] running seniorPlus/atStartups/mobility count queries...")
    senior_plus, senior_credits = run_count_query(build_senior_filters(filters))
    at_startups, startup_credits = run_count_query(build_startup_filters(filters))
    mobility, mobility_credits = run_count_query(build_mobility_filters(filters))
    count_credits_used = senior_credits + startup_credits + mobility_credits
    log(f"[{role_key}::{country}] seniorPlus={senior_plus} atStartups={at_startups} "
        f"mobility={mobility} (count-query credits used: {count_credits_used})")

    analytics = compute_analytics(role_key, country, profiles, total_count,
                                   senior_plus, at_startups, mobility)

    total_credits_used = pull_credits + count_credits_used

    return {
        "analytics": analytics,
        "profiles_pulled": len(profiles),
        "total_count": total_count,
        "saved": save_stats["saved"],
        "errors": save_stats["errors"],
        "error_messages": save_stats["error_messages"],
        "profiles": profiles,
        "pull_credits_used": pull_credits,
        "count_credits_used": count_credits_used,
        "credits_used": total_credits_used,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--only", metavar="ROLE_KEY::COUNTRY",
                        help='Process a single market, e.g. "security-researcher::Norway". '
                             'Merges into the existing market-map.json.')
    group.add_argument("--all", action="store_true",
                        help="Process all 168 role x country markets. Big credit-consuming run.")
    args = parser.parse_args()

    client = get_supabase_client()
    if client is None:
        log("ERROR: could not get Supabase client (check config.json)")
        sys.exit(1)

    existing = load_existing_output()

    if args.only:
        if "::" not in args.only:
            log(f"ERROR: --only value must be 'role_key::country', got {args.only!r}")
            sys.exit(1)
        role_key, country = args.only.split("::", 1)
        result = process_market(role_key, country, client)
        market_id = f"{role_key}::{country}"
        existing[market_id] = result["analytics"]
        write_output(existing)

        log(f"[{market_id}] DONE. total_count={result['total_count']} "
            f"pulled={result['profiles_pulled']} saved={result['saved']} "
            f"errors={result['errors']}")
        log(json.dumps(result["analytics"], indent=2))
        return

    if args.all:
        total_markets = len(ROLES) * len(COUNTRIES)
        log(f"Processing all {len(ROLES)} roles x {len(COUNTRIES)} countries "
            f"= {total_markets} markets...")

        skipped = []
        failed = []  # list of {"market_id", "error"}
        processed = []  # list of market_ids actually processed this run
        total_pulled = 0
        total_saved = 0
        total_save_errors = 0
        total_credits = 0

        idx = 0
        for role in ROLES:
            for country in COUNTRIES:
                idx += 1
                market_id = f"{role['key']}::{country}"

                # RESUME: skip markets already completed in a prior run.
                if market_id in existing and is_valid_analytics(existing[market_id]):
                    log(f"[{idx}/{total_markets}] {role['label']} × {country} -> SKIP (already done)")
                    skipped.append(market_id)
                    continue

                try:
                    result = process_market(role["key"], country, client)
                except Exception as e:
                    log(f"[{idx}/{total_markets}] {role['label']} × {country} -> FAILED: {e}")
                    failed.append({"market_id": market_id, "error": str(e)})
                    # RESILIENT: don't write anything for this market — next
                    # --all run will retry it since its key stays missing.
                    continue

                existing[market_id] = result["analytics"]
                # INCREMENTAL WRITE: merge+write immediately after each market
                # so a crash mid-run only loses the market in flight.
                write_output(existing)

                processed.append(market_id)
                total_pulled += result["profiles_pulled"]
                total_saved += result["saved"]
                total_save_errors += result["errors"]
                total_credits += result["credits_used"]

                log(f"[{idx}/{total_markets}] {role['label']} × {country} -> "
                    f"pulled {result['profiles_pulled']}, saved {result['saved']}, "
                    f"credits {result['credits_used']}")

                if result["error_messages"]:
                    for msg in result["error_messages"]:
                        log(f"    SAVE ERROR: {msg}")

        log("ALL MARKETS DONE.")

        summary = {
            "total_markets": total_markets,
            "processed_count": len(processed),
            "processed": processed,
            "skipped_count": len(skipped),
            "skipped": skipped,
            "failed_count": len(failed),
            "failed": failed,
            "total_profiles_pulled": total_pulled,
            "total_profiles_saved": total_saved,
            "total_save_errors": total_save_errors,
            "total_credits_used": total_credits,
            "market_map_entry_count": len(existing),
        }
        log(json.dumps(summary, indent=2))
        # Also print the summary to stdout (unlike per-market progress, which
        # goes to stderr) so a caller can capture just this final block.
        print(json.dumps(summary, indent=2))
        return


if __name__ == "__main__":
    main()
