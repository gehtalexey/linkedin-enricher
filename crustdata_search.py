"""
Crustdata People Search Database API Client

This module provides functions to search Crustdata's 100M+ professional database.
Used by the Search tab in dashboard.py to find candidates before enrichment.

API Endpoint: POST https://api.crustdata.com/screener/persondb/search
Cost: 3 credits per 100 results

Usage:
    from crustdata_search import search_people_db, build_filters, normalize_search_results_to_df

    # Build filters from UI inputs
    filters = build_filters(
        title="Backend Engineer",
        company="Google",
        location="Israel",
        experience_min=3,
        experience_max=10
    )

    # Search
    results = search_people_db(filters, limit=100)

    # Convert to DataFrame for pipeline
    df = normalize_search_results_to_df(results['profiles'])
"""

import gzip
import json
import time
import requests
import pandas as pd
from pathlib import Path
from typing import Optional, List, Dict, Any

from api_helpers import get_rate_limiter, RateLimitExceeded
from error_handling import (
    retry_with_backoff,
    ExternalServiceError,
    RateLimitError,
    AuthenticationError,
    ServiceUnavailableError,
    classify_http_error,
)
from normalizers import normalize_linkedin_url, clean_value, is_nan_or_none, pick_current_employer


# =============================================================================
# CONSTANTS
# =============================================================================

CRUSTDATA_SEARCH_ENDPOINT = "https://api.crustdata.com/screener/persondb/search"
CRUSTDATA_CREDITS_ENDPOINT = "https://api.crustdata.com/account/credits"

# Natural-language ("semantic") people search — Crustdata's newer v2025-11-01
# API. Same /person/search dataset as the filter search above, but ranks
# people by how well their whole profile matches a plain-language query
# instead of exact filter conditions. Verified against the live Crustdata
# docs (docs.crustdata.com/person-docs/search/introduction) on 2026-07-20 —
# request body is {"search": {"query", "mode"}, "mode": "exact"|"managed",
# "limit"}, auth is "Authorization: Bearer <key>" + "x-api-version" header
# (both different from the legacy endpoint above, which still uses "Token").
CRUSTDATA_SEMANTIC_SEARCH_ENDPOINT = "https://api.crustdata.com/person/search"
CRUSTDATA_API_VERSION = "2025-11-01"
CREDITS_PER_RESULT_SEMANTIC = 0.03

# Filter-based v2025-11-01 search (replaces CRUSTDATA_SEARCH_ENDPOINT above once
# the migration cuts over — see search_people_db_v2()). Same dataset/endpoint
# as the semantic search above, different request shape (filters, not a
# free-text query).
CRUSTDATA_SEARCH_V2_ENDPOINT = "https://api.crustdata.com/person/search"
CREDITS_PER_RESULT_V2 = 0.03  # unchanged from legacy persondb/search (3 per 100)

# Async batch enrichment (v2025-11-01) — fills in skills/summary/employment
# history for profiles the description-search endpoint above can't return
# them for. Verified live against docs.crustdata.com 2026-07-20: up to
# 10,000 LinkedIn URLs per job, base profile = 1 credit (additive pricing,
# same as the sync /person/enrich — see CREDITS_PER_ENRICH_PROFILE_BASE
# below). Note: the filter-based search (search_people_db, compact=false)
# already returns full profiles and stays the default search until
# Crustdata retires the legacy endpoint — this enrichment step only ever
# fires for profiles that come back thin, i.e. today, description-search
# results.
CRUSTDATA_BATCH_ENRICH_ENDPOINT = "https://api.crustdata.com/batch/person/enrich"
CRUSTDATA_BATCH_STATUS_ENDPOINT = "https://api.crustdata.com/batch"  # + f"/{batch_id}"

# Synchronous single-profile enrichment (v2025-11-01) — same additive
# pricing family as the batch endpoint above (base profile = 1 credit), but
# answers inline instead of submit/poll/download. Use this for call sites
# that need exactly one profile right now (e.g. a "re-enrich this URL"
# button); use the batch endpoint above for anything that can tolerate the
# poll/download round trip. Crustdata docs cap this endpoint at 25
# identifiers per call (professional_network_profile_urls OR
# business_emails, not both — verified against docs.crustdata.com
# 2026-07-20); sync_enrich_profile() below only ever sends one.
CRUSTDATA_SYNC_ENRICH_ENDPOINT = "https://api.crustdata.com/person/enrich"
BATCH_ENRICH_FIELDS = [
    "basic_profile", "experience", "education",
    "skills", "professional_network", "social_handles",
]
CREDITS_PER_ENRICH_PROFILE_BASE = 1  # base profile only — no contact/phone/dev-platform requested

# Seniority levels supported by Crustdata.
# Canonical values verified via crustdata_autocomplete_person on 2026-05-17.
# Previously this list used "Manager", "Senior", "Entry", "Training" which
# Crustdata silently does not recognize, so those filters returned zero matches.
SENIORITY_LEVELS = [
    "Entry Level",
    "Entry Level Manager",
    "Senior",
    "Experienced Manager",
    "Director",
    "Vice President",
    "CXO",
    "Owner / Partner",
    "In Training",
    "Strategic",
]

# Company headcount ranges supported by Crustdata.
# Note the comma in "10,001+" — Crustdata writes the top bucket that way and
# the value sent in filters must match exactly. Previously this list used
# "10001+" (no comma), so the largest-company filter silently missed matches.
HEADCOUNT_RANGES = [
    "1-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1000",
    "1001-5000",
    "5001-10000",
    "10,001+",
]

# Job function categories (current_employers.function_category).
# Values verified via crustdata_autocomplete_person on 2026-05-17.
FUNCTION_CATEGORIES = [
    "Engineering",
    "Product Management",
    "Sales",
    "Marketing",
    "Operations",
    "Finance",
    "Consulting",
    "Human Resources",
    "Research",
    "Legal",
    "Customer Success and Support",
    "Arts and Design",
]

# Industry values for current_employers.company_industries (curated top values).
# Verified via crustdata_autocomplete_person on 2026-05-17.
COMPANY_INDUSTRIES = [
    "Software Development",
    "Technology, Information and Internet",
    "Technology, Information and Media",
    "IT Services and IT Consulting",
    "Financial Services",
    "Capital Markets",
    "Business Consulting and Services",
    "Professional Services",
    "Manufacturing",
    "Hospitals and Health Care",
    "Retail",
    "Education",
    "Real Estate",
    "Advertising Services",
    "Marketing Services",
    "Media and Telecommunications",
    "Government Administration",
    "Non-profit Organizations",
    "Construction",
    "Transportation, Logistics, Supply Chain and Storage",
    "Legal Services",
    "Accounting",
    "Architecture and Planning",
    "Design Services",
    "Consumer Services",
]

# Credits per 100 results
CREDITS_PER_100_RESULTS = 3


# =============================================================================
# CONFIG LOADING
# =============================================================================

def _load_api_key() -> str:
    """Load Crustdata API key from config.json or environment.

    The returned key is .strip()ed so a stray trailing newline (common when a
    secret is set via ``gh secret set --body``) doesn't poison the HTTP header
    layer with errors like "Invalid leading whitespace, reserved character(s),
    or return character(s) in header value".
    """
    config_path = Path(__file__).parent / 'config.json'

    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                api_key = config.get('api_key')
                if api_key and api_key != "YOUR_CRUSTDATA_API_KEY_HERE":
                    return api_key.strip()
        except (json.JSONDecodeError, IOError):
            pass

    # Fallback to environment variable
    import os
    api_key = os.environ.get('CRUSTDATA_API_KEY')
    if api_key:
        return api_key.strip()

    raise AuthenticationError(
        "Crustdata",
        message="No Crustdata API key found. Set in config.json or CRUSTDATA_API_KEY env var."
    )


# =============================================================================
# FILTER BUILDERS
# =============================================================================

def _parse_keyword_boolean(expr: str) -> List[List[str]]:
    """
    Parse boolean keyword expression into AND groups of OR terms.

    Syntax:
        (node OR node.js) AND (react OR react.js)
        node OR node.js  (single OR group)
        node, node.js    (comma = OR for simple cases)
        kubernetes       (single term)

    Returns:
        List of groups, where each group is a list of OR terms.
        Groups are combined with AND logic.
        E.g., [["node", "node.js"], ["react", "react.js"]]
    """
    import re

    if not expr or not expr.strip():
        return []

    expr = expr.strip()

    # Check if it contains AND keyword (case insensitive)
    if re.search(r'\bAND\b', expr, re.IGNORECASE):
        # Split by AND (case insensitive, word boundary)
        and_parts = re.split(r'\s+AND\s+', expr, flags=re.IGNORECASE)
        groups = []
        for part in and_parts:
            part = part.strip()
            # Remove surrounding parentheses if present
            if part.startswith('(') and part.endswith(')'):
                part = part[1:-1].strip()
            # Split by OR (case insensitive) or comma
            if re.search(r'\bOR\b', part, re.IGNORECASE):
                or_terms = re.split(r'\s+OR\s+', part, flags=re.IGNORECASE)
            else:
                or_terms = part.split(',')
            # Clean up terms
            or_terms = [t.strip() for t in or_terms if t.strip()]
            if or_terms:
                groups.append(or_terms)
        return groups

    # No AND - check for OR
    if re.search(r'\bOR\b', expr, re.IGNORECASE):
        # Remove surrounding parentheses if present
        if expr.startswith('(') and expr.endswith(')'):
            expr = expr[1:-1].strip()
        or_terms = re.split(r'\s+OR\s+', expr, flags=re.IGNORECASE)
        or_terms = [t.strip() for t in or_terms if t.strip()]
        return [or_terms] if or_terms else []

    # No AND/OR - treat commas as OR (backwards compatibility)
    if ',' in expr:
        terms = [t.strip() for t in expr.split(',') if t.strip()]
        return [terms] if terms else []

    # Single term
    return [[expr.strip()]]


def build_filters(
    title: str = None,
    company: str = None,
    location: str = None,
    seniority: List[str] = None,
    headcount: List[str] = None,
    experience_min: int = None,
    experience_max: int = None,
    skills: List[str] = None,
    skills_and: bool = False,
    skill_groups: List[str] = None,
    keywords: str = None,
    past_companies: str = None,
    past_titles: str = None,
    school: str = None,
    recently_changed_jobs: bool = None,
    has_verified_email: bool = None,
    function_categories: List[str] = None,
    industries: List[str] = None,
    country: str = None,
    continent: str = None,
    geo_city: str = None,
    geo_radius_km: int = None,
    min_connections: int = None,
    exact_company: bool = False,
    not_relevant_companies: List[str] = None,
    blacklist_companies: List[str] = None,
) -> Dict[str, Any]:
    """
    Build Crustdata filter object from UI inputs.

    All conditions are combined with AND logic.
    Keywords search across headline + summary + skills with OR logic.

    Filter operators:
        [.] = substring match (contains)
        (.) = fuzzy match
        in / not_in = set membership (value must be array)
        >, <, >=, <= = numeric comparison

    Args:
        title: Job title (substring match). Comma-separated for OR logic.
        company: Company name (substring match). Comma-separated for OR logic.
        location: Location/region (substring match)
        seniority: List of seniority levels (in operator)
        headcount: List of headcount ranges (in operator)
        experience_min: Minimum years of experience (>= operator)
        experience_max: Maximum years of experience (<= operator)
        skills: List of skills (legacy, use skill_groups instead)
        skills_and: If True, require ALL skills (AND). If False, require ANY skill (OR)
        skill_groups: List of comma-separated skill strings. Each group is OR, groups combined with AND.
                      E.g., ["aws, gcp", "docker, kubernetes"] means (AWS OR GCP) AND (Docker OR Kubernetes)
        keywords: Boolean keyword expression. E.g., "(node OR node.js) AND (react OR react.js)"
                      Supports AND, OR keywords (case insensitive) and parentheses. Comma = OR for simple cases.
        past_companies: Comma-separated past company names (substring match)
        past_titles: Comma-separated past job titles (substring match)
        school: School/university name (substring match)
        recently_changed_jobs: If True, filter for job changes in last 90 days
        has_verified_email: If True, filter for verified business email

    Returns:
        Filter dict ready for Crustdata API: {"op": "and", "conditions": [...]}
    """
    conditions = []

    # Title filter (substring match on current job title OR headline)
    # Supports comma-separated values for OR logic.
    #
    # Why OR across two columns:
    # Crustdata persondb evaluates AND across nested-array columns
    # (current_employers.*) per-array-element. That makes
    # title=X AND company=Y collapse to "must be in the SAME current
    # employment entry" — see PR #39 / docs/research/crustdata-filter-semantics.md
    # for the per-element AND collapse and live probe transcript.
    #
    # Mirroring each title onto `headline` (a top-level profile field
    # evaluated per-profile, not per-array-element) keeps everything the
    # previous shape already matched and adds all profiles whose headline
    # mentions the title — measured lift on the PR #28 repro:
    # title="software developer" + 4 companies + region=Israel: 5 -> 12-149.
    if title and title.strip():
        title_values = [t.strip() for t in title.split(",") if t.strip()]
        if title_values:
            title_conditions = []
            for t in title_values:
                title_conditions.append(
                    {"column": "current_employers.title", "type": "[.]", "value": t}
                )
                title_conditions.append(
                    {"column": "headline", "type": "[.]", "value": t}
                )
            conditions.append({
                "op": "or",
                "conditions": title_conditions,
            })

    # Current company filter (comma-separated for OR logic)
    if company and company.strip():
        company_values = [c.strip() for c in company.split(",") if c.strip()]
        match_type = "=" if exact_company else "[.]"
        if len(company_values) == 1:
            conditions.append({
                "column": "current_employers.name",
                "type": match_type,
                "value": company_values[0]
            })
        elif len(company_values) > 1:
            company_conditions = [
                {"column": "current_employers.name", "type": match_type, "value": c}
                for c in company_values
            ]
            conditions.append({
                "op": "or",
                "conditions": company_conditions
            })

    # Location filter (comma-separated for OR logic)
    if location and location.strip():
        location_values = [l.strip() for l in location.split(",") if l.strip()]
        if len(location_values) == 1:
            conditions.append({
                "column": "region",
                "type": "[.]",
                "value": location_values[0]
            })
        elif len(location_values) > 1:
            location_conditions = [
                {"column": "region", "type": "[.]", "value": l}
                for l in location_values
            ]
            conditions.append({
                "op": "or",
                "conditions": location_conditions
            })

    # Seniority filter (set membership)
    if seniority and len(seniority) > 0:
        # Validate seniority levels
        valid_seniority = [s for s in seniority if s in SENIORITY_LEVELS]
        if valid_seniority:
            conditions.append({
                "column": "current_employers.seniority_level",
                "type": "in",
                "value": valid_seniority
            })

    # Headcount filter (set membership)
    if headcount and len(headcount) > 0:
        # Validate headcount ranges
        valid_headcount = [h for h in headcount if h in HEADCOUNT_RANGES]
        if valid_headcount:
            conditions.append({
                "column": "current_employers.company_headcount_range",
                "type": "in",
                "value": valid_headcount
            })

    # Experience range filters
    # Note: API doesn't support >= or <=, so we use > and < with adjusted values
    if experience_min is not None and experience_min > 0:
        conditions.append({
            "column": "years_of_experience_raw",
            "type": ">",
            "value": experience_min - 1  # ">= 3" becomes "> 2"
        })

    if experience_max is not None and experience_max > 0:
        conditions.append({
            "column": "years_of_experience_raw",
            "type": "<",
            "value": experience_max + 1  # "<= 10" becomes "< 11"
        })

    # Skill groups filter (each group is OR, groups combined with AND)
    # E.g., ["aws, gcp", "docker, kubernetes"] means (AWS OR GCP) AND (Docker OR Kubernetes)
    if skill_groups and len(skill_groups) > 0:
        for group in skill_groups:
            if not group or not group.strip():
                continue
            group_skills = [s.strip() for s in group.split(',') if s.strip()]
            if len(group_skills) == 1:
                # Single skill in group - simple condition
                conditions.append({
                    "column": "skills",
                    "type": "[.]",
                    "value": group_skills[0]
                })
            elif len(group_skills) > 1:
                # Multiple skills in group - OR condition
                group_conditions = [
                    {"column": "skills", "type": "[.]", "value": s}
                    for s in group_skills
                ]
                conditions.append({
                    "op": "or",
                    "conditions": group_conditions
                })

    # Legacy skills filter (AND or OR based on skills_and flag) - for backwards compatibility
    elif skills and len(skills) > 0:
        skill_values = [s.strip() if isinstance(s, str) else str(s) for s in skills]
        skill_values = [s for s in skill_values if s]
        if len(skill_values) == 1:
            conditions.append({
                "column": "skills",
                "type": "[.]",
                "value": skill_values[0]
            })
        elif len(skill_values) > 1:
            skill_conditions = [
                {"column": "skills", "type": "[.]", "value": s}
                for s in skill_values
            ]
            if skills_and:
                # AND mode: add each skill as separate condition (all must match)
                conditions.extend(skill_conditions)
            else:
                # OR mode: wrap in OR condition (any must match)
                conditions.append({
                    "op": "or",
                    "conditions": skill_conditions
                })

    # Keywords filter (searches headline, summary, skills)
    # Boolean syntax: (node OR node.js) AND (react OR react.js)
    # Also supports: node, node.js (comma = OR for simple cases)
    if keywords and keywords.strip():
        # Parse boolean expression
        keyword_groups = _parse_keyword_boolean(keywords)

        for group in keyword_groups:
            if group:
                # Build OR condition for keywords in this group
                keyword_conditions = []
                for kw in group:
                    # Search in headline
                    keyword_conditions.append({
                        "column": "headline",
                        "type": "[.]",
                        "value": kw
                    })
                    # Search in summary
                    keyword_conditions.append({
                        "column": "summary",
                        "type": "[.]",
                        "value": kw
                    })
                    # Search in skills
                    keyword_conditions.append({
                        "column": "skills",
                        "type": "[.]",
                        "value": kw
                    })

                # Wrap group in OR condition, add to main conditions (AND)
                conditions.append({
                    "op": "or",
                    "conditions": keyword_conditions
                })

    # Past companies filter
    if past_companies and past_companies.strip():
        company_list = [c.strip() for c in past_companies.split(',') if c.strip()]
        if company_list:
            # OR condition for past companies
            past_company_conditions = []
            for pc in company_list:
                past_company_conditions.append({
                    "column": "past_employers.name",
                    "type": "[.]",
                    "value": pc
                })
            conditions.append({
                "op": "or",
                "conditions": past_company_conditions
            })

    # Past titles filter
    if past_titles and past_titles.strip():
        title_list = [t.strip() for t in past_titles.split(',') if t.strip()]
        if title_list:
            # OR condition for past titles
            past_title_conditions = []
            for pt in title_list:
                past_title_conditions.append({
                    "column": "past_employers.title",
                    "type": "[.]",
                    "value": pt
                })
            conditions.append({
                "op": "or",
                "conditions": past_title_conditions
            })

    # School filter (comma-separated for OR logic)
    if school and school.strip():
        school_values = [s.strip() for s in school.split(",") if s.strip()]
        if len(school_values) == 1:
            conditions.append({
                "column": "education_background.institute_name",
                "type": "[.]",
                "value": school_values[0]
            })
        elif len(school_values) > 1:
            school_conditions = [
                {"column": "education_background.institute_name", "type": "[.]", "value": s}
                for s in school_values
            ]
            conditions.append({
                "op": "or",
                "conditions": school_conditions
            })

    # Recently changed jobs filter
    if recently_changed_jobs:
        conditions.append({
            "column": "recently_changed_jobs",
            "type": "=",
            "value": True
        })

    # Has verified email filter
    if has_verified_email:
        conditions.append({
            "column": "current_employers.business_email_verified",
            "type": "=",
            "value": True
        })

    # Function category filter (set membership on current job function)
    if function_categories:
        valid_functions = [f for f in function_categories if f in FUNCTION_CATEGORIES]
        if valid_functions:
            conditions.append({
                "column": "current_employers.function_category",
                "type": "in",
                "value": valid_functions
            })

    # Industry filter (set membership on company industries)
    if industries:
        conditions.append({
            "column": "current_employers.company_industries",
            "type": "in",
            "value": list(industries)
        })

    # Country filter (exact match — values are case-sensitive)
    if country and country.strip():
        conditions.append({
            "column": "location_country",
            "type": "=",
            "value": country.strip()
        })

    # Continent filter (exact match)
    if continent and continent.strip():
        conditions.append({
            "column": "location_continent",
            "type": "=",
            "value": continent.strip()
        })

    # Geo radius filter ("within N km of CITY")
    if geo_city and geo_city.strip() and geo_radius_km and geo_radius_km > 0:
        conditions.append({
            "column": "region",
            "type": "geo_distance",
            "value": {"location": geo_city.strip(), "distance": geo_radius_km, "unit": "km"}
        })

    # Min connections filter (=> is Crustdata's "greater than or equal to" operator)
    if min_connections and min_connections > 0:
        conditions.append({
            "column": "num_of_connections",
            "type": "=>",
            "value": min_connections
        })

    # Exclude not-relevant and blacklisted companies (current employer only).
    # Both lists are merged into a single not_in to avoid ambiguity if the API
    # treats two conditions on the same column with OR rather than AND semantics.
    # not_in is exact/case-sensitive; the post-search fuzzy filter handles variants.
    _excl_set = set()
    if not_relevant_companies:
        _excl_set.update(n.strip().strip('"').strip() for n in not_relevant_companies if n and n.strip())
    if blacklist_companies:
        _excl_set.update(n.strip().strip('"').strip() for n in blacklist_companies if n and n.strip())
    if _excl_set:
        conditions.append({
            "column": "current_employers.name",
            "type": "not_in",
            "value": sorted(_excl_set)
        })

    # Return combined filter
    if not conditions:
        # Return empty filter that matches everything
        return {}

    if len(conditions) == 1:
        # Single condition doesn't need wrapper
        return {"filters": conditions[0]}

    # Multiple conditions with AND
    return {
        "filters": {
            "op": "and",
            "conditions": conditions
        }
    }


def _v2_headers(api_key: str) -> Dict[str, str]:
    """Shared header set for every v2025-11-01 endpoint (Bearer auth + version
    header — legacy endpoints use `Token` auth and no version header)."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "x-api-version": CRUSTDATA_API_VERSION,
    }


# =============================================================================
# FILTER GRAMMAR — legacy (`column`) -> v2025-11-01 (`field`)
# =============================================================================

# Maps every filter column build_filters() can emit to its v2025-11-01
# field path. Enumerated by reading build_filters() directly (Codex review,
# 2026-07-20) rather than guessing a partial list — a filter added to
# build_filters() later without a matching entry here falls through to the
# "pass through unchanged" branch in _remap_filters(), which fails loudly
# (wrong/zero results) rather than silently dropping the condition.
_LEGACY_TO_V2_FIELD = {
    "current_employers.title": "experience.employment_details.current.title",
    "current_employers.name": "experience.employment_details.current.name",
    "current_employers.seniority_level": "experience.employment_details.current.seniority_level",
    "current_employers.company_headcount_range": "experience.employment_details.current.company_headcount_range",
    "current_employers.company_industries": "experience.employment_details.current.company_industries",
    "current_employers.business_email_verified": "experience.employment_details.current.business_email_verified",
    "current_employers.function_category": "experience.employment_details.current.function_category",
    "past_employers.name": "experience.employment_details.past.name",
    "past_employers.title": "experience.employment_details.past.title",
    "region": "professional_network.location.raw",
    "headline": "basic_profile.headline",
    "summary": "basic_profile.summary",
    "skills": "skills.professional_network_skills",
    "education_background.institute_name": "education.schools.school",
    "years_of_experience_raw": "years_of_experience_raw",
    "recently_changed_jobs": "recently_changed_jobs",
    "num_of_connections": "professional_network.connections",
    "location_country": "basic_profile.location.country",
    "location_continent": "basic_profile.location.continent",
}


def _remap_filters(node):
    """Recursively rewrite a legacy filter tree (`column`/`type`/`value` leaf
    conditions, `op`+`conditions[]` groups) into the v2025-11-01 shape
    (`field` instead of `column`, same operators — `>=`/`<=` rewritten to
    `=>`/`=<` defensively, though build_filters() doesn't emit those today).
    """
    if not isinstance(node, dict):
        return node
    if "conditions" in node:
        return {
            **{k: v for k, v in node.items() if k != "conditions"},
            "conditions": [_remap_filters(c) for c in node["conditions"]],
        }
    if "column" in node:
        new_node = {k: v for k, v in node.items() if k != "column"}
        legacy_field = node["column"]
        new_node["field"] = _LEGACY_TO_V2_FIELD.get(legacy_field, legacy_field)
        if new_node.get("type") == ">=":
            new_node["type"] = "=>"
        elif new_node.get("type") == "<=":
            new_node["type"] = "=<"
        return new_node
    return node


# =============================================================================
# API FUNCTIONS
# =============================================================================

@retry_with_backoff(
    max_retries=3,
    base_delay=2.0,
    retryable_exceptions=(RateLimitError, ServiceUnavailableError, ConnectionError, TimeoutError),
)
def search_people_db(
    filters: Dict[str, Any],
    limit: int = 100,
    cursor: str = None,
    sorts: List[Dict[str, str]] = None,
    api_key: str = None,
    exclude_profiles: List[str] = None,
) -> Dict[str, Any]:
    """
    Search Crustdata's people database.

    Args:
        filters: Filter dict from build_filters() or raw filter object
        limit: Results per page (max 1000, default 100)
        cursor: Pagination cursor from previous response
        sorts: Optional sorting list, e.g., [{"column": "years_of_experience_raw", "order": "desc"}]
        api_key: Optional API key (if not provided, loads from config.json or env var)

    Returns:
        {
            "profiles": [...],       # List of profile dicts
            "cursor": "...",         # Next page cursor (None if no more results)
            "total_count": N,        # Total matching profiles
            "credits_used": N        # Credits consumed
        }

    Raises:
        AuthenticationError: Invalid API key
        RateLimitError: Rate limit exceeded
        ExternalServiceError: API error
    """
    if not api_key:
        api_key = _load_api_key()
    limiter = get_rate_limiter('crustdata')

    # Build request body.
    # compact=false is the load-bearing parameter that makes search return the
    # FULL profile (past_employers with descriptions, certifications, summary,
    # flagship_profile_url, etc.). Crustdata's default is compact=true which
    # silently strips nested data — that's what created the original (incorrect)
    # assumption that enrichment was needed after every search.
    # Verified via Crustdata founder call + live test against Ami Blonder
    # on 2026-05-15. See .planning equivalent docs / GitHub issue #68.
    body = {
        "limit": min(limit, 1000),  # Cap at 1000
        "compact": False,
    }

    # Add filters if provided
    if filters:
        if "filters" in filters:
            body["filters"] = filters["filters"]
        elif "op" in filters or "column" in filters:
            body["filters"] = filters

    # Add pagination cursor
    if cursor:
        body["cursor"] = cursor

    # Exclude specific LinkedIn profiles (past candidates).
    # Crustdata nests this under post_processing, not at the top level.
    # Normalize URLs to canonical form so scheme/www/trailing-slash variants match.
    if exclude_profiles:
        clean_urls = [normalize_linkedin_url(u) for u in exclude_profiles if u and str(u).strip()]
        clean_urls = [u for u in clean_urls if u]
        if clean_urls:
            body["post_processing"] = {"exclude_profiles": clean_urls}

    # Add sorting
    if sorts:
        body["sorts"] = sorts

    # Rate limiting
    try:
        limiter.wait_if_needed()
    except RateLimitExceeded as e:
        raise RateLimitError("Crustdata", message=str(e))

    start_time = time.time()

    try:
        response = requests.post(
            CRUSTDATA_SEARCH_ENDPOINT,
            json=body,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )

        limiter.record_request()

        # Handle HTTP errors
        if response.status_code == 401:
            raise AuthenticationError("Crustdata")
        elif response.status_code == 429:
            retry_after = response.headers.get('Retry-After')
            raise RateLimitError(
                "Crustdata",
                retry_after=float(retry_after) if retry_after else None
            )
        elif response.status_code >= 500:
            raise ServiceUnavailableError(
                "Crustdata",
                status_code=response.status_code,
                response_body=response.text[:500]
            )
        elif response.status_code >= 400:
            raise ExternalServiceError(
                "Crustdata",
                message=f"Search failed: {response.text[:500]}",
                status_code=response.status_code,
                response_body=response.text
            )

        data = response.json()

        # Extract results - API returns "profiles" not "data", "next_cursor" not "cursor"
        profiles = data.get("profiles", [])
        next_cursor = data.get("next_cursor")
        total_count = data.get("total_count", len(profiles))

        # Calculate credits used (3 credits per 100 results)
        credits_used = (len(profiles) // 100 + (1 if len(profiles) % 100 > 0 else 0)) * CREDITS_PER_100_RESULTS

        return {
            "profiles": profiles,
            "cursor": next_cursor,
            "total_count": total_count,
            "credits_used": credits_used,
            "response_time_ms": int((time.time() - start_time) * 1000),
        }

    except requests.exceptions.Timeout:
        raise ExternalServiceError(
            "Crustdata",
            message="Search request timed out",
            status_code=504
        )
    except requests.exceptions.ConnectionError as e:
        raise ServiceUnavailableError(
            "Crustdata",
            message=f"Connection error: {str(e)[:200]}"
        )


@retry_with_backoff(
    max_retries=2,
    base_delay=1.0,
    retryable_exceptions=(RateLimitError, ServiceUnavailableError, ConnectionError),
)
def check_credits(api_key: str = None) -> Dict[str, Any]:
    """
    Check remaining Crustdata credits.

    Args:
        api_key: Optional API key (if not provided, loads from config.json or env var)

    Returns:
        {
            "remaining": N,      # Credits remaining
            "used": N,           # Credits used (if available)
            "total": N           # Total credits (if available)
        }

    Raises:
        AuthenticationError: Invalid API key
        ExternalServiceError: API error
    """
    if not api_key:
        api_key = _load_api_key()
    limiter = get_rate_limiter('crustdata')

    try:
        limiter.wait_if_needed()
    except RateLimitExceeded as e:
        raise RateLimitError("Crustdata", message=str(e))

    try:
        response = requests.get(
            CRUSTDATA_CREDITS_ENDPOINT,
            headers={"Authorization": f"Token {api_key}"},
            timeout=30,
        )

        limiter.record_request()

        if response.status_code == 401:
            raise AuthenticationError("Crustdata")
        elif response.status_code >= 400:
            raise ExternalServiceError(
                "Crustdata",
                message=f"Credits check failed: {response.text[:200]}",
                status_code=response.status_code
            )

        data = response.json()

        return {
            "remaining": data.get("credits_remaining", data.get("remaining", 0)),
            "used": data.get("credits_used", data.get("used", 0)),
            "total": data.get("credits_total", data.get("total", 0)),
        }

    except requests.exceptions.Timeout:
        raise ExternalServiceError(
            "Crustdata",
            message="Credits check timed out",
            status_code=504
        )
    except requests.exceptions.ConnectionError as e:
        raise ServiceUnavailableError(
            "Crustdata",
            message=f"Connection error: {str(e)[:200]}"
        )


@retry_with_backoff(
    max_retries=3,
    base_delay=2.0,
    retryable_exceptions=(RateLimitError, ServiceUnavailableError, ConnectionError, TimeoutError),
)
def search_people_semantic(
    query: str,
    limit: int = 20,
    cursor: str = None,
    search_mode: str = "hybrid",
    recall_mode: str = "managed",
    api_key: str = None,
) -> Dict[str, Any]:
    """
    Natural-language ("search by description") people search — beta.

    Instead of building filter conditions, pass a plain-language description
    of who you're looking for (a role, a persona, or a pasted JD) and get
    back people ranked by how well their whole profile matches it. Each
    result carries a "fit" tier (strong/possible/weak) in the raw profile —
    read it to judge quality; total_count is the size of the ranked pool,
    not a count of good matches.

    Args:
        query: Plain-language description, e.g. "founding engineers at
            developer-tools startups in Israel".
        limit: Results to return (1-100, default 20).
        cursor: Pagination cursor from a previous response.
        search_mode: "hybrid" (default, keyword+vector), "lexical" (exact
            terms only), or "semantic" (vector/meaning only).
        recall_mode: "managed" (default — query is the main signal) or
            "exact" (only used if filters are added later; kept here so
            callers can opt in without a signature change).
        api_key: Optional API key (loads from config.json / env var if omitted).

    Returns:
        {
            "profiles": [...],   # raw nested v2 profile dicts (see
                                  # semantic_profile_to_legacy_shape() to adapt
                                  # them for the rest of the pipeline)
            "cursor": "...",
            "total_count": N,
            "credits_used": N,   # 0.03 credits per result returned
            "response_time_ms": N,
        }
    """
    if not query or not query.strip():
        raise ValueError("query is required for semantic search")

    if not api_key:
        api_key = _load_api_key()
    limiter = get_rate_limiter('crustdata')

    body = {
        "search": {"query": query.strip(), "mode": search_mode},
        "limit": max(1, min(limit, 100)),
    }
    if recall_mode == "exact":
        body["mode"] = "exact"
    if cursor:
        body["cursor"] = cursor

    try:
        limiter.wait_if_needed()
    except RateLimitExceeded as e:
        raise RateLimitError("Crustdata", message=str(e))

    start_time = time.time()

    try:
        response = requests.post(
            CRUSTDATA_SEMANTIC_SEARCH_ENDPOINT,
            json=body,
            headers=_v2_headers(api_key),
            timeout=60,
        )

        limiter.record_request()

        if response.status_code == 401:
            raise AuthenticationError("Crustdata")
        elif response.status_code == 429:
            retry_after = response.headers.get('Retry-After')
            raise RateLimitError(
                "Crustdata",
                retry_after=float(retry_after) if retry_after else None
            )
        elif response.status_code >= 500:
            raise ServiceUnavailableError(
                "Crustdata",
                status_code=response.status_code,
                response_body=response.text[:500]
            )
        elif response.status_code >= 400:
            raise ExternalServiceError(
                "Crustdata",
                message=f"Semantic search failed: {response.text[:500]}",
                status_code=response.status_code,
                response_body=response.text
            )

        data = response.json()
        profiles = data.get("profiles", [])
        next_cursor = data.get("next_cursor")
        total_count = data.get("total_count", len(profiles))
        credits_used = round(len(profiles) * CREDITS_PER_RESULT_SEMANTIC, 2)

        return {
            "profiles": profiles,
            "cursor": next_cursor,
            "total_count": total_count,
            "credits_used": credits_used,
            "response_time_ms": int((time.time() - start_time) * 1000),
        }

    except requests.exceptions.Timeout:
        raise ExternalServiceError(
            "Crustdata",
            message="Semantic search request timed out",
            status_code=504
        )
    except requests.exceptions.ConnectionError as e:
        raise ServiceUnavailableError(
            "Crustdata",
            message=f"Connection error: {str(e)[:200]}"
        )


@retry_with_backoff(
    max_retries=3,
    base_delay=2.0,
    retryable_exceptions=(RateLimitError, ServiceUnavailableError, ConnectionError, TimeoutError),
)
def search_people_db_v2(
    filters: Dict[str, Any],
    limit: int = 100,
    cursor: str = None,
    sorts: List[Dict[str, str]] = None,
    api_key: str = None,
    exclude_profiles: List[str] = None,
) -> Dict[str, Any]:
    """
    Filter-based people search via the new v2025-11-01 POST /person/search
    endpoint — the eventual replacement for search_people_db(). Takes the
    SAME filters dict shape as search_people_db() (build_filters() output);
    remaps the legacy column/AND-OR grammar to the new field-based grammar
    internally via _remap_filters(), so callers don't change.

    IMPORTANT: unlike the legacy endpoint (compact=false), /person/search
    does NOT return `skills` or `summary` in results — they're filterable
    but stripped from the response (verified live 2026-07-20). Every
    returned profile is passed through semantic_profile_to_legacy_shape()
    (the same v2-nested -> flat-legacy translator search_people_semantic()
    results already go through — both endpoints return the same nested
    shape), which marks each result `_semantic_incomplete: True` so
    normalize_search_result() sets `_needs_enrichment: True`. That flags
    thin profiles for the existing "1. Load" tab enrichment queue; SourcingX
    also auto-fills them right before AI Screen regardless (see
    enrich_thin_profiles_for_batch() in dashboard.py, live since 2026-07-20),
    so this flag is a visibility signal, not the only path to getting them
    filled in.

    Returns the same shape as search_people_db(): {"profiles", "cursor",
    "total_count", "credits_used", "response_time_ms"}.
    """
    if not api_key:
        api_key = _load_api_key()
    limiter = get_rate_limiter('crustdata')

    body = {"limit": min(limit, 1000)}

    if filters:
        raw_filters = filters.get("filters") if "filters" in filters else filters
        if raw_filters:
            body["filters"] = _remap_filters(raw_filters)

    if cursor:
        body["cursor"] = cursor

    # Crustdata nests exclusions under post_processing on both API versions.
    if exclude_profiles:
        clean_urls = [normalize_linkedin_url(u) for u in exclude_profiles if u and str(u).strip()]
        clean_urls = [u for u in clean_urls if u]
        if clean_urls:
            body["post_processing"] = {"exclude_profiles": clean_urls}

    if sorts:
        # The sort field needs the SAME column -> field remap filters get —
        # not just a key rename. Sending an unmapped legacy name like
        # `num_of_connections` (the dashboard's default sort) as a v2 `field`
        # value would sort wrong or get rejected outright (Codex review,
        # 2026-07-20).
        body["sorts"] = [
            {
                "field": _LEGACY_TO_V2_FIELD.get(s.get("field") or s.get("column"), s.get("field") or s.get("column")),
                "order": s.get("order"),
            }
            for s in sorts
        ]

    # Keep the response small — skills/summary are never returned by this
    # endpoint regardless of what's requested, so only ask for the sections
    # the results table + semantic_profile_to_legacy_shape() actually read.
    body["fields"] = [
        "basic_profile", "experience.employment_details", "education.schools",
        "social_handles.professional_network_identifier", "professional_network",
        "years_of_experience_raw", "recently_changed_jobs", "crustdata_person_id",
    ]

    try:
        limiter.wait_if_needed()
    except RateLimitExceeded as e:
        raise RateLimitError("Crustdata", message=str(e))

    start_time = time.time()

    try:
        response = requests.post(
            CRUSTDATA_SEARCH_V2_ENDPOINT,
            json=body,
            headers=_v2_headers(api_key),
            timeout=60,
        )

        limiter.record_request()

        if response.status_code == 401:
            raise AuthenticationError("Crustdata")
        elif response.status_code == 429:
            retry_after = response.headers.get('Retry-After')
            raise RateLimitError(
                "Crustdata",
                retry_after=float(retry_after) if retry_after else None
            )
        elif response.status_code >= 500:
            raise ServiceUnavailableError(
                "Crustdata",
                status_code=response.status_code,
                response_body=response.text[:500]
            )
        elif response.status_code >= 400:
            raise ExternalServiceError(
                "Crustdata",
                message=f"Search v2 failed: {response.text[:500]}",
                status_code=response.status_code,
                response_body=response.text
            )

        data = response.json()
        raw_profiles = data.get("profiles", [])
        next_cursor = data.get("next_cursor")
        total_count = data.get("total_count", len(raw_profiles))

        profiles = [semantic_profile_to_legacy_shape(p) for p in raw_profiles]
        credits_used = round(len(raw_profiles) * CREDITS_PER_RESULT_V2, 2)

        return {
            "profiles": profiles,
            "cursor": next_cursor,
            "total_count": total_count,
            "credits_used": credits_used,
            "response_time_ms": int((time.time() - start_time) * 1000),
        }

    except requests.exceptions.Timeout:
        raise ExternalServiceError(
            "Crustdata",
            message="Search v2 request timed out",
            status_code=504
        )
    except requests.exceptions.ConnectionError as e:
        raise ServiceUnavailableError(
            "Crustdata",
            message=f"Connection error: {str(e)[:200]}"
        )


# =============================================================================
# BATCH ENRICHMENT (NEW v2025-11-01 API)
# =============================================================================
# Fills in skills/summary/employment history for profiles the new search
# endpoints can't return them for. Async job: submit up to 10,000 LinkedIn
# URLs, poll for completion, download one JSON record per profile. Base
# profile = 1 credit (additive pricing, same as sync /person/enrich).
# Distinct from the legacy enrich_batch() in dashboard.py (25 URLs/call,
# 3 credits/profile, no rate limiter/retry) — do not confuse the two.

@retry_with_backoff(
    max_retries=3,
    base_delay=2.0,
    retryable_exceptions=(RateLimitError, ServiceUnavailableError, ConnectionError, TimeoutError),
)
def submit_batch_enrich(
    linkedin_urls: List[str],
    api_key: str = None,
    chunk_size: int = 100,
    fields: List[str] = None,
) -> str:
    """
    Submit up to 10,000 LinkedIn profile URLs to POST /batch/person/enrich.
    Returns the batch_id to poll via get_batch_status().

    Raises ValueError if more than 10,000 URLs are passed — batch_enrich_profiles()
    is the caller that splits large lists into multiple jobs; call this
    directly only when you already know you're under the cap.
    """
    if not linkedin_urls:
        raise ValueError("submit_batch_enrich requires at least one LinkedIn URL")
    if len(linkedin_urls) > 10000:
        raise ValueError(f"submit_batch_enrich accepts at most 10,000 URLs, got {len(linkedin_urls)}")

    if not api_key:
        api_key = _load_api_key()
    limiter = get_rate_limiter('crustdata')

    body = {
        "professional_network_profile_urls": linkedin_urls,
        "fields": fields or BATCH_ENRICH_FIELDS,
        "chunk_size": max(10, min(chunk_size, 1000)),
    }

    try:
        limiter.wait_if_needed()
    except RateLimitExceeded as e:
        raise RateLimitError("Crustdata", message=str(e))

    try:
        response = requests.post(
            CRUSTDATA_BATCH_ENRICH_ENDPOINT,
            json=body,
            headers=_v2_headers(api_key),
            timeout=60,
        )
        limiter.record_request()

        if response.status_code == 401:
            raise AuthenticationError("Crustdata")
        elif response.status_code == 429:
            retry_after = response.headers.get('Retry-After')
            raise RateLimitError(
                "Crustdata",
                retry_after=float(retry_after) if retry_after else None
            )
        elif response.status_code >= 500:
            raise ServiceUnavailableError(
                "Crustdata",
                status_code=response.status_code,
                response_body=response.text[:500]
            )
        elif response.status_code >= 400:
            raise ExternalServiceError(
                "Crustdata",
                message=f"Batch enrich submit failed: {response.text[:500]}",
                status_code=response.status_code,
                response_body=response.text
            )

        data = response.json()
        batch_id = data.get("batch_id") or data.get("id")
        if not batch_id:
            raise ExternalServiceError(
                "Crustdata",
                message=f"Batch enrich submit returned no batch id: {response.text[:300]}"
            )
        return batch_id

    except requests.exceptions.Timeout:
        raise ExternalServiceError(
            "Crustdata",
            message="Batch enrich submit timed out",
            status_code=504
        )
    except requests.exceptions.ConnectionError as e:
        raise ServiceUnavailableError(
            "Crustdata",
            message=f"Connection error: {str(e)[:200]}"
        )


def _extract_sync_enrich_record(payload: Any, requested_url: str) -> Optional[Dict[str, Any]]:
    """Pull the single per-query record out of a sync POST /person/enrich
    response — the ``{match_type, matched_on, matches}`` wrapper, NOT the
    profile itself (see _select_person_data_from_matches() for that step).

    VERIFIED LIVE 2026-08-04 (two real 1-credit calls against
    POST /person/enrich): the top-level response is a plain LIST, one entry
    per queried identifier:

        [
          {
            "match_type": <str>,
            "matched_on": ...,
            "matches": [
              {"confidence_score": <float>, "person_data": {...}},
              ...
            ]
          }
        ]

    ``person_data`` is what carries basic_profile/experience/education/
    skills/professional_network/social_handles — the shape
    enrich_profile_to_legacy_shape() expects (that inner shape was already
    verified separately, 2026-07-20, against the batch endpoint's download
    file — same nested structure).

    A dict-wrapped top level (keyed "results"/"profiles"/"data") is also
    tolerated here even though it hasn't been observed live, since other
    v2025-11-01 endpoints in this file use that shape and Crustdata's API
    isn't perfectly consistent call to call — worst case a wrong guess here
    is a harmless None (caller reports "not found"), never a mis-billed
    request, since Crustdata's global no-charge-on-no-result policy means
    we only pay for records we actually parse out.
    """
    if payload is None:
        return None
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = None
        for key in ("results", "profiles", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                records = val
                break
        if records is None:
            # No known wrapping key held a list — treat the dict itself as
            # the (single, unwrapped) record.
            records = [payload]
    else:
        return None

    records = [r for r in records if isinstance(r, dict)]
    if not records:
        return None
    if len(records) == 1:
        return records[0]

    # More than one top-level record for a 1-URL request shouldn't happen,
    # but match by `matched_on` (the verified shape's echo of the query)
    # defensively rather than assume ordering.
    norm_target = normalize_linkedin_url(requested_url)
    for rec in records:
        matched_on = rec.get("matched_on")
        if matched_on == requested_url or (matched_on and normalize_linkedin_url(str(matched_on)) == norm_target):
            return rec
    return records[0]


def _match_confidence(match: Dict[str, Any]) -> float:
    """Coerce a `matches[]` entry's confidence_score to a comparable float —
    defensively, since its exact numeric type/range isn't pinned in docs."""
    try:
        return float(match.get("confidence_score"))
    except (TypeError, ValueError):
        return 0.0


def _person_data_url(person_data: Dict[str, Any]) -> Optional[str]:
    social_id = (person_data.get("social_handles") or {}).get("professional_network_identifier") or {}
    return social_id.get("profile_url") or None


def _person_data_has_real_content(person_data: Dict[str, Any]) -> bool:
    """Content gate: a match is only "found" if it carries a real name AND
    at least one of experience/education/skills is actually populated.

    Required because Crustdata can wrap an empty shell (e.g.
    {"basic_profile": {}}) around what is really a no-match — that shell
    is a non-empty, truthy dict, so without this check it would sail past
    a bare `if person_data:` guard and get written into the shared
    profiles table as a blank/corrupted row (Codex review, PR #127,
    2026-08-04). Deliberately strict: when in doubt, this returns False
    and the caller returns None — a missed enrichment costs nothing
    (Crustdata doesn't bill no-match), a wrong/blank one corrupts a row
    four projects read.
    """
    basic = person_data.get("basic_profile") or {}
    if not (basic.get("name") or "").strip():
        return False
    employment = (person_data.get("experience") or {}).get("employment_details") or {}
    has_experience = bool(employment.get("current")) or bool(employment.get("past"))
    has_education = bool((person_data.get("education") or {}).get("schools"))
    has_skills = bool((person_data.get("skills") or {}).get("professional_network_skills"))
    return has_experience or has_education or has_skills


def _person_data_matches_url(person_data: Any, expected_url: str) -> bool:
    """Identity check: does `person_data`'s own LinkedIn URL (via
    social_handles.professional_network_identifier.profile_url) normalize
    to the same value as `expected_url`?

    This is the identity half of _is_valid_person_data() below, split out
    so it stays testable on its own; use _is_valid_person_data() for the
    actual identity+content gate.
    """
    if not isinstance(person_data, dict) or not person_data:
        return False
    norm_expected = normalize_linkedin_url(expected_url)
    if not norm_expected:
        return False
    candidate_url = _person_data_url(person_data)
    if not candidate_url:
        return False
    return normalize_linkedin_url(candidate_url) == norm_expected


def _is_valid_person_data(person_data: Any, expected_url: str) -> bool:
    """THE single identity+content gate a `person_data`/`data` payload must
    clear before it's trusted enough to translate and write into the
    shared Supabase profiles table (four projects read that table —
    writing another person's data onto a row, or a blank shell, is data
    corruption, not a harmless miss).

    Used by BOTH enrich paths — the sync endpoint's
    _select_person_data_from_matches() and the batch endpoint's
    batch_enrich_profiles() — as ONE shared standard rather than two
    separate checks that could drift (Codex review, PR #127, round 2,
    2026-08-04: round 1 hardened only the sync path; the batch path
    still trusted any non-empty `data` payload that echoed the right
    `original_identifier`, without checking the payload's OWN claimed
    LinkedIn URL against what was actually requested — recreating the
    exact wrong-person risk round 1 fixed for sync).

    A candidate passes only if BOTH:
      1. Its own LinkedIn URL normalizes to the same value as
         `expected_url` (_person_data_matches_url()) — a confidence
         score or an echoed identifier is Crustdata's claim, not
         verification; comparing the payload's own data against what we
         actually asked about is.
      2. It has real content (_person_data_has_real_content()) — not an
         empty shell.

    When in doubt, this returns False: a missed enrichment costs nothing
    (Crustdata's global no-charge-on-no-result policy), a wrongly-trusted
    one corrupts a shared row.
    """
    if not _person_data_matches_url(person_data, expected_url):
        return False
    return _person_data_has_real_content(person_data)


def _select_person_data_from_matches(matches: Any, requested_url: str) -> Optional[Dict[str, Any]]:
    """Pick the right `person_data` dict out of a sync-enrich record's
    `matches` list (verified shape — see _extract_sync_enrich_record()).

    Every candidate is run through _is_valid_person_data() — identity
    verified BEFORE ranking, not only as a confidence tie-break (Codex
    review, PR #127, 2026-08-04 — the earlier version let a
    high-confidence WRONG-person match win over a lower-confidence right
    one). Among candidates that pass, the highest confidence_score wins
    (a tie doesn't matter for correctness at that point, since every
    eligible candidate already points at the same requested URL).

    Returns None — never a half-built or wrong-person profile — when
    `matches` is missing/empty or no candidate clears
    _is_valid_person_data().
    """
    if not isinstance(matches, list) or not matches:
        return None

    eligible = [
        m for m in matches
        if isinstance(m, dict) and _is_valid_person_data(m.get("person_data"), requested_url)
    ]
    if not eligible:
        return None

    best = max(eligible, key=_match_confidence)
    return best["person_data"]


@retry_with_backoff(
    max_retries=3,
    base_delay=2.0,
    retryable_exceptions=(RateLimitError, ServiceUnavailableError, ConnectionError, TimeoutError),
)
def sync_enrich_profile(
    linkedin_url: str,
    api_key: str = None,
    fields: List[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Enrich ONE LinkedIn profile via the synchronous v2025-11-01
    POST /person/enrich endpoint. Base profile = 1 credit (additive
    pricing, same family as submit_batch_enrich() above) — for a single
    profile this skips the async submit/poll/download round trip the batch
    pipeline needs for volume.

    Returns the same flat legacy-enrich shape as enrich_profile_to_legacy_shape()
    (via that function), or None if Crustdata had no match for this URL —
    never raises for a plain no-match, only for transport/HTTP failures
    (mirrors submit_batch_enrich()'s error handling).

    Response unwrap (VERIFIED live 2026-08-04 — see
    _extract_sync_enrich_record()'s docstring for the full shape): top-level
    list -> per-query {match_type, matched_on, matches} record -> matches[]
    -> best-scoring match's person_data -> enrich_profile_to_legacy_shape().
    """
    if not linkedin_url or not str(linkedin_url).strip():
        return None
    if not api_key:
        api_key = _load_api_key()
    limiter = get_rate_limiter('crustdata')

    body = {
        "professional_network_profile_urls": [linkedin_url],
        "fields": fields or BATCH_ENRICH_FIELDS,
    }

    try:
        limiter.wait_if_needed()
    except RateLimitExceeded as e:
        raise RateLimitError("Crustdata", message=str(e))

    try:
        response = requests.post(
            CRUSTDATA_SYNC_ENRICH_ENDPOINT,
            json=body,
            headers=_v2_headers(api_key),
            timeout=60,
        )
        limiter.record_request()

        if response.status_code == 401:
            raise AuthenticationError("Crustdata")
        elif response.status_code == 429:
            retry_after = response.headers.get('Retry-After')
            raise RateLimitError(
                "Crustdata",
                retry_after=float(retry_after) if retry_after else None
            )
        elif response.status_code >= 500:
            raise ServiceUnavailableError(
                "Crustdata",
                status_code=response.status_code,
                response_body=response.text[:500]
            )
        elif response.status_code >= 400:
            raise ExternalServiceError(
                "Crustdata",
                message=f"Sync enrich failed: {response.text[:500]}",
                status_code=response.status_code,
                response_body=response.text
            )

        record = _extract_sync_enrich_record(response.json(), linkedin_url)
        if not record:
            return None
        person_data = _select_person_data_from_matches(record.get("matches"), linkedin_url)
        if not person_data:
            return None
        return enrich_profile_to_legacy_shape(person_data)

    except requests.exceptions.Timeout:
        raise ExternalServiceError(
            "Crustdata",
            message="Sync enrich request timed out",
            status_code=504
        )
    except requests.exceptions.ConnectionError as e:
        raise ServiceUnavailableError(
            "Crustdata",
            message=f"Connection error: {str(e)[:200]}"
        )


@retry_with_backoff(
    max_retries=3,
    base_delay=2.0,
    retryable_exceptions=(RateLimitError, ServiceUnavailableError, ConnectionError, TimeoutError),
)
def get_batch_status(batch_id: str, api_key: str = None) -> Dict[str, Any]:
    """GET /batch/{batch_id}. Free — no credits consumed. Returns the raw
    status payload (exact key vocabulary not pinned in Crustdata's docs as
    of 2026-07-20 — _is_batch_terminal()/_download_batch_results() below
    handle the documented shape defensively)."""
    if not api_key:
        api_key = _load_api_key()
    limiter = get_rate_limiter('crustdata')

    try:
        limiter.wait_if_needed()
    except RateLimitExceeded as e:
        raise RateLimitError("Crustdata", message=str(e))

    try:
        response = requests.get(
            f"{CRUSTDATA_BATCH_STATUS_ENDPOINT}/{batch_id}",
            headers=_v2_headers(api_key),
            timeout=30,
        )
        limiter.record_request()

        if response.status_code == 401:
            raise AuthenticationError("Crustdata")
        elif response.status_code == 429:
            retry_after = response.headers.get('Retry-After')
            raise RateLimitError(
                "Crustdata",
                retry_after=float(retry_after) if retry_after else None
            )
        elif response.status_code >= 500:
            raise ServiceUnavailableError(
                "Crustdata",
                status_code=response.status_code,
                response_body=response.text[:500]
            )
        elif response.status_code >= 400:
            raise ExternalServiceError(
                "Crustdata",
                message=f"Batch status check failed: {response.text[:500]}",
                status_code=response.status_code,
                response_body=response.text
            )

        return response.json()

    except requests.exceptions.Timeout:
        raise ExternalServiceError(
            "Crustdata",
            message="Batch status check timed out",
            status_code=504
        )
    except requests.exceptions.ConnectionError as e:
        raise ServiceUnavailableError(
            "Crustdata",
            message=f"Connection error: {str(e)[:200]}"
        )


_BATCH_TERMINAL_SUCCESS = {"completed", "succeeded", "success", "done"}
_BATCH_TERMINAL_FAILURE = {"failed", "error", "cancelled", "canceled"}


def _is_batch_terminal(status_payload: Dict[str, Any]) -> bool:
    status = str(status_payload.get("status", "")).lower()
    return status in _BATCH_TERMINAL_SUCCESS or status in _BATCH_TERMINAL_FAILURE


def _download_batch_results(status_payload: Dict[str, Any], api_key: str) -> List[Dict[str, Any]]:
    """Return the list of {original_identifier, internal_id, data} records
    for a completed batch job. Results may be inline on the status payload
    (`results`/`data`), or behind one or more download URLs.

    Verified live against a real batch job 2026-07-20 (this shape was
    guessed/unverified before then — see the corrections below, both
    confirmed against real API responses, not docs):
      - The status payload carries BOTH `download_url` (one file) and
        `download_urls` (a list — larger jobs split into multiple
        `part-NNN.jsonl.gz` files). Must fetch every URL in `download_urls`
        when present, not just the singular one, or a large job's later
        parts get silently dropped.
      - Each file is gzip-compressed (`Content-Type: application/gzip`,
        magic bytes `\\x1f\\x8b`) but the server does NOT set the HTTP
        `Content-Encoding: gzip` header, so `requests` does not
        auto-decompress it — reading `.text` on the raw response silently
        returns garbled bytes, every JSON-parse fails, and the whole batch
        looks like zero matches even when Crustdata successfully enriched
        everyone. Must gunzip explicitly.
      - These are presigned S3 URLs (auth is in the query string) — do NOT
        send our own Bearer header to them; only crustdata.com URLs need it
        (kept for `results_file_url`/single-file inline compatibility in
        case a future API version serves an uncompressed file directly
        from Crustdata itself).
    """
    inline = status_payload.get("results") or status_payload.get("data")
    if isinstance(inline, list):
        return inline

    download_urls = status_payload.get("download_urls")
    if not download_urls:
        single = status_payload.get("download_url") or status_payload.get("results_file_url")
        download_urls = [single] if single else []
    if not download_urls:
        return []

    records = []
    for download_url in download_urls:
        if not download_url:
            continue
        response = requests.get(
            download_url,
            headers=_v2_headers(api_key) if download_url.startswith("https://api.crustdata.com") else None,
            timeout=120,
        )
        response.raise_for_status()

        content = response.content
        if content[:2] == b"\x1f\x8b":  # gzip magic number
            content = gzip.decompress(content)
        text = content.decode("utf-8")

        # One JSON object per line (JSONL).
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


# Possible key names for a fulfilled-count on a batch status payload — not
# pinned in Crustdata's docs (same caveat as _is_batch_terminal()/
# _download_batch_results() above), so checked defensively in order.
_STATUS_FULFILLED_HINT_KEYS = ("entities_fulfilled", "fulfilled_count", "fulfilled")


def _status_fulfilled_hint(status_payload: Dict[str, Any]) -> Optional[int]:
    """Best-effort extraction of a fulfilled-count from a batch status
    payload, for jobs that land in the `unknown` bucket (see
    batch_enrich_profiles()'s docstring) — surfaced for reconciliation
    rather than discarded, per Codex review PR #127 round 2. Returns None
    if no known key holds a usable number."""
    if not isinstance(status_payload, dict):
        return None
    for key in _STATUS_FULFILLED_HINT_KEYS:
        val = status_payload.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            return int(val)
    return None


def _submit_definitely_never_started(exc: Exception) -> bool:
    """True ONLY for a submit_batch_enrich() failure where we can be
    CONFIDENT the batch job never started on Crustdata's side — safe to
    report as genuine no-spend. Everything else (a timeout or connection
    loss during the POST, a 5xx response, a 200 with no batch_id, or any
    exception type not specifically recognized here) is AMBIGUOUS and
    must be routed to the `unknown` bucket instead (Codex review, PR #127,
    round 3, HIGH): a client-side timeout/connection loss during the
    submit POST can happen AFTER Crustdata already accepted and started
    the job — reporting that as a clean, free no-match recreates the
    exact spend-observability failure round 2 already fixed for the
    post-acceptance (poll/download) window.

    Deliberately a narrow ALLOWLIST, not a broad denylist:
    - ValueError: submit_batch_enrich()'s own client-side input
      validation (empty list / over 10,000 URLs) — raised before any
      network call is even attempted.
    - AuthenticationError (401) / RateLimitError (429): the request was
      rejected by Crustdata's gateway before any job processing could
      begin.
    - A generic 4xx (ExternalServiceError with a real 400-499
      status_code straight off an HTTP response, excluding the 5xx-only
      ServiceUnavailableError subclass): the same kind of explicit
      rejection Codex's own examples named ("DNS failure, 4xx rejection,
      auth error").

    Nothing else qualifies — in particular NOT a bare requests.Timeout/
    ConnectionError (translated by submit_batch_enrich() into
    ExternalServiceError(status_code=504) / ServiceUnavailableError
    respectively), NOT a genuine 5xx response, and NOT the "200 but no
    batch_id in the response body" case — all of those mean we simply
    don't know whether Crustdata received and started processing the
    request.

    No client-side idempotency/request key is used here because
    Crustdata's v2025-11-01 batch-enrich API doesn't document support
    for one (checked docs.crustdata.com and this repo's Crustdata API
    reference skill, 2026-08-04) — a retried submit after an ambiguous
    failure can't be deduplicated server-side, so the caller is left
    with the `unknown` bucket's diagnostics for manual reconciliation
    rather than an automatic dedupe.
    """
    if isinstance(exc, ValueError):
        return True
    if isinstance(exc, (AuthenticationError, RateLimitError)):
        return True
    if isinstance(exc, ExternalServiceError) and not isinstance(exc, ServiceUnavailableError):
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int) and 400 <= status_code < 500:
            return True
    return False


def batch_enrich_profiles(
    linkedin_urls: List[str],
    api_key: str = None,
    poll_interval_s: float = 5.0,
    max_wait_s: float = 300.0,
    chunk_size: int = 100,
    fields: List[str] = None,
) -> Dict[str, Any]:
    """
    Submit, poll-and-wait, download, and translate a batch enrichment job.
    This is the function screening calls (via dashboard.py's
    enrich_thin_profiles_for_batch()) — it never raises on partial no-matches
    or a poll timeout, since SourcingX's policy is to screen thin profiles
    anyway rather than block a whole batch over a few unmatched people.

    Splits into multiple <=10,000-URL jobs if linkedin_urls is larger than
    that (submitted sequentially — in practice a single AI-Screen batch is
    at most ~50 profiles, so this only matters for very large runs).

    Args:
        linkedin_urls: Normalized LinkedIn URLs to enrich.
        poll_interval_s: Seconds between status checks (default 5s — well
            under the 60 RPM Crustdata rate limit).
        max_wait_s: Give up waiting on a single job after this many seconds;
            whatever isn't done yet lands in `unmatched`, not an exception.

    Returns:
        {
            "by_url": {<url>: <flat legacy-enrich-shape dict>},
            "requested": int,
            "fulfilled": int,
            "unmatched": [urls...],       # no record came back, job outcome
                                           # was otherwise fully readable
            "rejected": [urls...],        # a record came back but failed
                                           # identity/content validation —
                                           # see _is_valid_person_data()
            "unknown": [urls...],         # job was submitted/accepted but
                                           # its outcome could NOT be read
                                           # (poll error, poll timeout, or a
                                           # completed job whose results we
                                           # couldn't download) — NOT the
                                           # same as a genuine no-match; see
                                           # "Unknown billing" below
            "unknown_diagnostics": [...], # {batch_id, reason, fulfilled_hint,
                                           # last_status_payload} per job
                                           # that landed in `unknown`
            "credits_used": fulfilled * 1,
            "batch_ids": [...],
        }

    Unknown billing (Codex review, PR #127, round 2, 2026-08-04): a job
    that was successfully SUBMITTED and ACCEPTED by Crustdata may already
    be running (and billing) even if we then lose the ability to read its
    outcome — a transient error while polling status, a poll timeout with
    no terminal status ever observed, or a download/parse failure AFTER
    Crustdata reported the job complete. The previous version collapsed
    all three into `records = []`, which flows into `unmatched` —
    indistinguishable from Crustdata genuinely finding nothing, and
    reported to usage logging as a clean, free, zero-credit run. That is
    backwards for a cost-control PR: it hides spend in exactly the
    direction that flatters us. These three cases now land in `unknown`
    instead, are never silently treated as free, and dashboard.py logs
    them as an error/unknown-spend event carrying the batch_id(s) so the
    account balance can be reconciled manually. When Crustdata's own
    status payload happens to report a fulfilled count for a job we
    otherwise couldn't fully read (see `_status_fulfilled_hint()`), it's
    surfaced via
    `unknown_diagnostics[i]["fulfilled_hint"]` rather than discarded —
    but it is NOT folded into `credits_used`, which stays a conservative,
    only-what-we-actually-validated number.

    Deduplicates by normalized URL BEFORE submitting (Codex review, PR
    #127, 2026-08-04): a caller-supplied list containing the same URL
    twice (verbatim, or as two strings that normalize to the same profile)
    previously got submitted to Crustdata unchanged — paying for and
    fetching the same profile twice — while `requested`/`fulfilled`/
    `credits_used` were derived from a de-duplicated set, silently
    UNDER-counting the true spend. Only one representative raw string per
    normalized identity is ever sent to Crustdata; `by_url` is still
    populated under every one of the caller's original duplicate strings
    (not just the representative), so callers indexing results against
    their own input list — including the duplicate — still get a hit at
    every position.

    Every downloaded record is run through the SAME identity+content gate
    as the sync path, _is_valid_person_data() (Codex review, PR #127,
    round 2, 2026-08-04): round 1 hardened only sync_enrich_profile()'s
    match selection; this endpoint's records were still trusted on the
    strength of `original_identifier` alone, with no check that the
    payload's OWN claimed LinkedIn URL actually matched what was
    submitted for it. A record that echoes the right identifier but
    carries a wrong person (or an empty shell) is now counted in
    `rejected`, NOT mapped into `by_url`, and NOT counted fulfilled —
    same standard, same reasoning: a missed enrichment costs nothing, a
    wrongly-trusted one corrupts a row four other projects read.
    """
    linkedin_urls = [u for u in (linkedin_urls or []) if u and str(u).strip()]
    if not linkedin_urls:
        return {
            "by_url": {}, "requested": 0, "fulfilled": 0, "unmatched": [],
            "rejected": [], "unknown": [], "unknown_diagnostics": [],
            "credits_used": 0, "batch_ids": [],
        }

    if not api_key:
        api_key = _load_api_key()

    # canonical_to_inputs preserves first-seen order (dict insertion order)
    # and maps each distinct normalized identity to every raw input string
    # that resolves to it — including the duplicates we're about to skip
    # submitting.
    canonical_to_inputs: Dict[str, List[str]] = {}
    for u in linkedin_urls:
        norm = normalize_linkedin_url(u) or u
        canonical_to_inputs.setdefault(norm, []).append(u)

    # One representative raw string per canonical identity — this is what
    # actually gets billed.
    submit_urls = [inputs[0] for inputs in canonical_to_inputs.values()]

    jobs = [submit_urls[i:i + 10000] for i in range(0, len(submit_urls), 10000)]
    by_url: Dict[str, Dict[str, Any]] = {}
    batch_ids: List[str] = []
    rejected_canonical: set = set()
    unknown_canonical: set = set()
    unknown_diagnostics: List[Dict[str, Any]] = []

    for job_urls in jobs:
        job_norms = {normalize_linkedin_url(u) or u for u in job_urls}

        try:
            batch_id = submit_batch_enrich(job_urls, api_key=api_key, chunk_size=chunk_size, fields=fields)
        except Exception as e:
            if _submit_definitely_never_started(e):
                # Confidently rejected before any job processing could
                # begin — genuinely nothing was billed. Its URLs stay
                # unmatched below.
                continue
            # Ambiguous (Codex review, PR #127, round 3): a timeout or
            # connection loss during the submit POST itself means we
            # don't know whether Crustdata received and started the job
            # before we lost the response. Must not collapse to a free
            # no-match — same "unknown" treatment as a post-acceptance
            # poll/download failure, just with no batch_id to show for it.
            unknown_canonical.update(job_norms)
            unknown_diagnostics.append({
                "batch_id": None,
                "reason": "submit_error",
                "fulfilled_hint": None,
                "last_status_payload": None,
                "error": str(e)[:300],
            })
            continue
        batch_ids.append(batch_id)

        # From here on the job WAS accepted — Crustdata may already be
        # running (and billing) it, so any failure to read its outcome
        # from this point on is "unknown", never silently "no match".

        elapsed = 0.0
        status_payload = {}
        poll_failed = False
        timed_out = False
        while elapsed < max_wait_s:
            try:
                status_payload = get_batch_status(batch_id, api_key=api_key)
            except Exception:
                poll_failed = True
                break
            if _is_batch_terminal(status_payload):
                break
            time.sleep(poll_interval_s)
            elapsed += poll_interval_s
        else:
            # Loop exhausted max_wait_s without ever hitting a `break` —
            # i.e. we never observed a terminal status at all.
            timed_out = True

        if poll_failed or timed_out:
            unknown_canonical.update(job_norms)
            unknown_diagnostics.append({
                "batch_id": batch_id,
                "reason": "poll_error" if poll_failed else "poll_timeout",
                "fulfilled_hint": _status_fulfilled_hint(status_payload),
                "last_status_payload": status_payload,
            })
            continue

        if str(status_payload.get("status", "")).lower() in _BATCH_TERMINAL_FAILURE:
            # Crustdata explicitly told us the job failed — a real
            # negative signal, not an unreadable one. Stays unmatched.
            continue

        try:
            records = _download_batch_results(status_payload, api_key)
        except Exception:
            # The job reached a terminal status (Crustdata's side is done,
            # likely billed) but we couldn't retrieve/parse the results —
            # unknown, not "no match".
            unknown_canonical.update(job_norms)
            unknown_diagnostics.append({
                "batch_id": batch_id,
                "reason": "download_error",
                "fulfilled_hint": _status_fulfilled_hint(status_payload),
                "last_status_payload": status_payload,
            })
            continue

        for record in records:
            data = record.get("data") or {}
            if not data:
                continue  # genuinely empty payload — stays unmatched, not "rejected"

            identifier = record.get("original_identifier")
            if not identifier:
                # No independent identity to verify this payload against —
                # cannot safely trust it (see _is_valid_person_data()'s
                # docstring). Uncounted rather than guessed at.
                continue
            norm_key = normalize_linkedin_url(identifier) or identifier
            if norm_key not in canonical_to_inputs:
                # Echoes an identifier we never actually submitted — ignore.
                continue

            if not _is_valid_person_data(data, identifier):
                rejected_canonical.add(norm_key)
                continue

            flat = enrich_profile_to_legacy_shape(data)
            by_url[identifier] = flat
            if norm_key not in by_url:
                by_url[norm_key] = flat
            # Fan out to every original caller-supplied string that maps to
            # this same normalized identity, so a duplicate URL in the
            # input list resolves at every position it appeared.
            for original_input in canonical_to_inputs.get(norm_key, []):
                by_url.setdefault(original_input, flat)

    # unknown_canonical is only ever populated at the whole-job level, for
    # jobs whose per-record loop above never ran (every `continue` in the
    # unknown-marking branches happens before the records loop) — so it
    # can't overlap with by_url or rejected_canonical, which are only
    # populated inside that loop.
    unmatched_canonical = [
        norm for norm in canonical_to_inputs
        if norm not in by_url and norm not in rejected_canonical and norm not in unknown_canonical
    ]
    unmatched = sorted(canonical_to_inputs[norm][0] for norm in unmatched_canonical)
    rejected = sorted(canonical_to_inputs[norm][0] for norm in rejected_canonical)
    unknown = sorted(canonical_to_inputs[norm][0] for norm in unknown_canonical if norm in canonical_to_inputs)
    requested = len(canonical_to_inputs)
    fulfilled = requested - len(unmatched_canonical) - len(rejected_canonical) - len(unknown_canonical)

    return {
        "by_url": by_url,
        "requested": requested,
        "fulfilled": fulfilled,
        "unmatched": unmatched,
        "rejected": rejected,
        "unknown": unknown,
        "unknown_diagnostics": unknown_diagnostics,
        "credits_used": fulfilled * CREDITS_PER_ENRICH_PROFILE_BASE,
        "batch_ids": batch_ids,
    }


# =============================================================================
# NORMALIZATION
# =============================================================================

def normalize_search_result(profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a Crustdata search result to pipeline DataFrame format.

    With ``compact=false`` (now the default in ``search_people_db``), Crustdata
    search returns the FULL profile: flagship_profile_url, past_employers with
    full descriptions and dates, education, certifications, summary, skills,
    languages, etc. The only field NOT returned by search is ``emails`` (SourcingX
    uses SalesQL for emails separately). A follow-up enrichment call is no
    longer needed in the default pipeline — see GitHub issue #68 / the
    Crustdata-founder call notes.

    Args:
        profile: Raw profile dict from search results

    Returns:
        Normalized dict ready for pipeline DataFrame with fields:
            - linkedin_url
            - name, first_name, last_name
            - headline, location, summary
            - current_company, current_title, seniority, company_size
            - skills (comma-separated string)
            - years_experience
            - _source = 'crustdata_search'
            - _needs_enrichment = False for a normal filter search (compact=false
              returns the full profile). True when the input carries the
              ``_semantic_incomplete`` marker — set by
              semantic_profile_to_legacy_shape() because Crustdata's
              description-search endpoint doesn't return skills, summary, or
              years of experience, so those rows still need a real enrichment
              pass before screening.
    """
    if not profile:
        return None

    # Extract LinkedIn URL - use flagship (clean URL) not linkedin_profile_url (URN format)
    linkedin_url = (
        profile.get('flagship_profile_url') or
        profile.get('linkedin_flagship_url') or
        profile.get('linkedin_profile_url') or
        profile.get('linkedin_url')
    )
    linkedin_url = normalize_linkedin_url(linkedin_url)

    if not linkedin_url:
        return None

    # Name fields
    full_name = profile.get('name', '')
    first_name = profile.get('first_name', '')
    last_name = profile.get('last_name', '')

    # Parse full name if first/last not available
    if not first_name and not last_name and full_name:
        parts = full_name.split(' ', 1)
        first_name = parts[0] if parts else ''
        last_name = parts[1] if len(parts) > 1 else ''

    # Build full name if not available
    if not full_name and (first_name or last_name):
        full_name = f"{first_name} {last_name}".strip()

    # Current employer info — pick the most recent when multiple are present
    current_company = ''
    current_title = ''
    seniority = ''
    company_size = ''
    current_years_in_role = None
    current_years_at_company = None
    all_current = profile.get('current_employers') or []
    emp = pick_current_employer(all_current)
    if emp:
        current_company = emp.get('employer_name') or emp.get('name') or ''
        current_title = emp.get('employee_title') or emp.get('title') or ''
        seniority = emp.get('seniority_level') or emp.get('seniority') or ''
        company_size = emp.get('company_headcount_range') or emp.get('headcount') or ''

        # Tenure in current ROLE (most recent entry's start_date)
        raw_start = emp.get('start_date')
        if raw_start:
            from normalizers import _parse_start_date_sort_key
            parseable, dt = _parse_start_date_sort_key(raw_start)
            if parseable:
                from datetime import datetime
                current_years_in_role = round((datetime.now() - dt).days / 365.25, 1)

        # Tenure at COMPANY — find earliest start_date for the same company
        current_years_at_company = current_years_in_role
        if current_company and isinstance(all_current, list) and len(all_current) > 1:
            from normalizers import _parse_start_date_sort_key as _psd
            from datetime import datetime as _dt
            company_lower = current_company.lower().strip()
            earliest = None
            for entry in all_current:
                if not isinstance(entry, dict):
                    continue
                name = (entry.get('employer_name') or entry.get('name') or '').lower().strip()
                if name != company_lower:
                    continue
                ok, d = _psd(entry.get('start_date'))
                if ok and (earliest is None or d < earliest):
                    earliest = d
            if earliest:
                current_years_at_company = round((_dt.now() - earliest).days / 365.25, 1)

    # Fallback to top-level fields
    if not current_title:
        current_title = profile.get('title', '')
    if not current_company:
        current_company = profile.get('company', '')

    # Skills
    skills = profile.get('skills', [])
    if isinstance(skills, list):
        skills_str = ', '.join(str(s) for s in skills[:50] if s)
    elif skills:
        skills_str = str(skills)
    else:
        skills_str = ''

    # All employers / titles / schools — handle both flat strings (enrich endpoint)
    # and objects (compact=false search endpoint, mirrors db._prepare_profile_row logic)
    def _extract_names_titles(raw):
        names, titles = [], []
        for item in (raw or []):
            if isinstance(item, dict):
                n = item.get('name') or item.get('employer_name') or ''
                t = item.get('title') or item.get('employee_title') or ''
                if n: names.append(n)
                if t: titles.append(t)
            elif isinstance(item, str) and item:
                names.append(item)
        return names, titles

    def _extract_schools(raw):
        schools = []
        for item in (raw or []):
            if isinstance(item, dict):
                s = item.get('institute_name') or item.get('school') or item.get('name') or ''
                if s: schools.append(s)
            elif isinstance(item, str) and item:
                schools.append(item)
        return schools

    _emp_names, _emp_titles = _extract_names_titles(profile.get('all_employers'))
    if not _emp_names:
        _fb_names, _fb_titles = _extract_names_titles(profile.get('past_employers'))
        _emp_names = _fb_names
        _emp_titles = _emp_titles or _fb_titles

    _raw_titles = profile.get('all_titles') or []
    _titles = [str(x) for x in _raw_titles if x] if _raw_titles else _emp_titles
    _schools = _extract_schools(profile.get('all_schools')) or _extract_schools(profile.get('education_background'))

    all_employers_str = ', '.join(_emp_names)
    all_titles_str = ', '.join(_titles)
    all_schools_str = ', '.join(_schools)

    # Connections
    connections_count = profile.get('num_of_connections') or profile.get('connections_count')

    # Experience
    years_exp = profile.get('years_of_experience_raw')
    if years_exp is None:
        years_exp = profile.get('years_of_experience')

    return {
        # Core identifiers (snake_case to match pipeline)
        'linkedin_url': linkedin_url,
        'name': clean_value(full_name) or '',
        'first_name': clean_value(first_name) or '',
        'last_name': clean_value(last_name) or '',
        # Profile content
        'headline': clean_value(profile.get('headline', '')) or '',
        'location': clean_value(profile.get('region') or profile.get('location', '')) or '',
        'summary': clean_value(profile.get('summary', '')) or '',
        # Current employment
        'current_company': clean_value(current_company) or '',
        'current_title': clean_value(current_title) or '',
        'seniority': clean_value(seniority) or '',
        'company_size': clean_value(company_size) or '',
        # Tenure
        'current_years_in_role': current_years_in_role,
        'current_years_at_company': current_years_at_company,
        # Skills and experience
        'skills': skills_str,
        'years_experience': years_exp,
        # Filter tab fields
        'all_employers': all_employers_str,
        'all_titles': all_titles_str,
        'all_schools': all_schools_str,
        'connections_count': connections_count,
        # Metadata
        '_source': 'crustdata_search',
        # Only description-search rows (marked by the shim below) still need
        # enrichment — a normal filter search already returns the full profile.
        '_needs_enrichment': bool(profile.get('_semantic_incomplete', False)),
        '_raw_search_result': profile,  # Keep raw for debugging
    }


def semantic_profile_to_legacy_shape(profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adapt one nested v2025-11-01 person-search profile (as returned by
    search_people_semantic) to the flat legacy profile shape that
    normalize_search_result() and the Search tab's results table already
    know how to read (current_employers/past_employers as lists of
    {name, title, ...}, top-level headline/region/summary, etc.).

    This lets semantic search results flow through the exact same
    display, selection, CSV export, and pipeline code as the regular
    filter search — no parallel code path needed.

    Crustdata's description-search endpoint doesn't return skills, summary,
    or years of experience (verified live 2026-07-20 — confirmed empty/absent
    in a real response), and the AI screening prompt treats missing skills as
    a hard FAIL rather than "unknown". The shim marks its output with
    ``_semantic_incomplete: True`` so normalize_search_result() sets
    ``_needs_enrichment: True`` on these rows — that's what makes them show
    up in the normal "1. Load" tab enrichment queue instead of silently
    heading into AI Screen with blank fields.

    Verified response field paths against the live Crustdata docs on
    2026-07-20 (basic_profile.location.{raw,country}, experience
    .employment_details.{current,past}, social_handles
    .professional_network_identifier.profile_url, education.schools,
    skills.professional_network_skills, professional_network.connections).
    """
    if not profile:
        return {}

    basic = profile.get('basic_profile') or {}
    location = basic.get('location') or {}
    employment = (profile.get('experience') or {}).get('employment_details') or {}
    schools = (profile.get('education') or {}).get('schools') or []
    skills = (profile.get('skills') or {}).get('professional_network_skills') or []
    connections = (profile.get('professional_network') or {}).get('connections')
    social_id = (profile.get('social_handles') or {}).get('professional_network_identifier') or {}

    def _employer_entries(raw_entries):
        # v2 uses "company_headcount_latest"; the legacy code this profile
        # feeds into reads "company_headcount_range" — alias it across so
        # company size still shows up without touching that shared code.
        out = []
        for entry in (raw_entries or []):
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            if 'company_headcount_range' not in entry and 'company_headcount_latest' in entry:
                entry['company_headcount_range'] = entry['company_headcount_latest']
            out.append(entry)
        return out

    return {
        'name': basic.get('name', ''),
        'headline': basic.get('headline', ''),
        'region': location.get('raw', ''),
        'location_country': location.get('country', ''),
        'summary': basic.get('summary', ''),
        'flagship_profile_url': social_id.get('profile_url'),
        'current_employers': _employer_entries(employment.get('current')),
        'past_employers': _employer_entries(employment.get('past')),
        'skills': skills,
        'num_of_connections': connections,
        'years_of_experience_raw': profile.get('years_of_experience_raw'),
        'all_schools': [
            s.get('school') for s in schools if isinstance(s, dict) and s.get('school')
        ],
        'crustdata_person_id': profile.get('crustdata_person_id'),
        # Crustdata's relevance tier for this result: strong/possible/weak.
        # Not part of the legacy shape — carried through as an extra field
        # so the results table can show it.
        '_fit': profile.get('fit', ''),
        # The shim above only maps the fields the legacy shape/pipeline knows
        # about — it drops some of the nested v2 response (contact flags,
        # normalized_title, professional_network handle, etc.). Keep the
        # original nested profile so a "full details" export can still show
        # everything Crustdata actually returned for this person, the same
        # way normalize_search_result() keeps _raw_search_result for the
        # regular filter search.
        '_raw_semantic_result': profile,
        # Tells normalize_search_result() this row is missing skills/summary/
        # experience and needs real enrichment before AI Screen sees it.
        '_semantic_incomplete': True,
    }


def _v2_coerce_skill(skill: Any) -> str:
    """Skills come back as either bare strings or {"name": ...} objects
    (element type not confirmed live as of 2026-07-20 — two sample profiles
    both had empty skills lists) — handle both defensively."""
    if isinstance(skill, dict):
        return skill.get("name") or skill.get("skill") or ""
    return str(skill) if skill else ""


def _v2_employer_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map one experience.employment_details.{current,past}[] entry (v2
    field names: title/name/start_date/end_date/description) to the flat
    legacy employer shape (employee_title/employer_name/.../
    employee_description) that trim_raw_profile() and compute_role_durations()
    read. Verified live against a real /person/enrich response 2026-07-20.
    """
    if not isinstance(entry, dict):
        return None
    return {
        "employee_title": (entry.get("title") or "").strip(),
        "employer_name": entry.get("name") or "",
        "start_date": entry.get("start_date"),
        "end_date": entry.get("end_date"),
        "employee_description": entry.get("description") or None,
        # The new API has no per-employer company-description field (confirmed
        # live — absent even where a `description` exists on the position
        # itself). trim_raw_profile() only keeps the first sentence anyway,
        # so a missing value here just degrades gracefully to nothing shown.
        "employer_linkedin_description": None,
    }


def _v2_school_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map one education.schools[] entry (v2 field names: school/degree/
    field_of_study — NOT institute_name/degree_name) to the flat legacy
    education_background shape. Verified live 2026-07-20."""
    if not isinstance(entry, dict):
        return None
    return {
        "institute_name": entry.get("school") or "",
        "degree_name": entry.get("degree") or "",
        "field_of_study": entry.get("field_of_study") or "",
    }


def enrich_profile_to_legacy_shape(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map one nested /batch/person/enrich (or /person/enrich) record's `data`
    payload to the FLAT LEGACY ENRICH shape that trim_raw_profile(),
    compute_role_durations(), and db._prepare_profile_row() all read.

    Distinct from semantic_profile_to_legacy_shape() above, which targets a
    different output contract (the search-results table) and deliberately
    keeps a partial field set — feeding its output to the screening path
    would silently drop every job title and duration. This translator is
    built for the screening/DB-save path specifically.

    Verified against a real /person/enrich response (2026-07-20, same nested
    shape as the batch endpoint) rather than guessed — see
    _v2_employer_entry() / _v2_school_entry() docstrings for the confirmed
    field-name differences from the legacy shape.
    """
    if not data:
        return {}

    basic = data.get("basic_profile") or {}
    location = basic.get("location") or {}
    employment = (data.get("experience") or {}).get("employment_details") or {}
    schools = (data.get("education") or {}).get("schools") or []
    raw_skills = (data.get("skills") or {}).get("professional_network_skills") or []
    professional_network = data.get("professional_network") or {}
    pn_location = professional_network.get("location") or {}
    social_id = (data.get("social_handles") or {}).get("professional_network_identifier") or {}

    skills = [s for s in (_v2_coerce_skill(s) for s in raw_skills) if s]
    current_employers = [e for e in (_v2_employer_entry(e) for e in (employment.get("current") or [])) if e]
    past_employers = [e for e in (_v2_employer_entry(e) for e in (employment.get("past") or [])) if e]
    education_background = [e for e in (_v2_school_entry(e) for e in schools) if e]

    all_employers = [e["employer_name"] for e in (current_employers + past_employers) if e.get("employer_name")]
    all_titles = [e["employee_title"] for e in (current_employers + past_employers) if e.get("employee_title")]
    all_schools = [e["institute_name"] for e in education_background if e.get("institute_name")]
    all_degrees = [e["degree_name"] for e in education_background if e.get("degree_name")]

    flagship_url = social_id.get("profile_url") or ""

    # basic_profile.current_title is present directly on the new API
    # (confirmed live) — prefer it over deriving from the employer list,
    # falling back only if it's ever absent.
    title = basic.get("current_title") or (current_employers[0]["employee_title"] if current_employers else "")

    return {
        "name": basic.get("name") or "",
        "title": title,
        "headline": basic.get("headline") or "",
        "summary": basic.get("summary") or "",
        "location": location.get("raw") or pn_location.get("raw") or "",
        "region": pn_location.get("raw") or location.get("raw") or "",
        "num_of_connections": professional_network.get("connections"),
        "skills": skills,
        "languages": basic.get("languages") or [],
        "linkedin_flagship_url": flagship_url,
        "linkedin_url": flagship_url,
        "current_employers": current_employers,
        "past_employers": past_employers,
        "education_background": education_background,
        "all_employers": all_employers,
        "all_titles": all_titles,
        "all_schools": all_schools,
        "all_degrees": all_degrees,
        "crustdata_person_id": data.get("crustdata_person_id"),
    }


def normalize_search_results_to_df(profiles: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    Batch normalize search results and return DataFrame ready for pipeline.

    Args:
        profiles: List of raw profile dicts from search_people_db()

    Returns:
        pandas DataFrame with normalized columns matching pipeline format
    """
    if not profiles:
        return pd.DataFrame()

    normalized = []
    for profile in profiles:
        result = normalize_search_result(profile)
        if result:
            normalized.append(result)

    if not normalized:
        return pd.DataFrame()

    df = pd.DataFrame(normalized)

    # Ensure consistent column order (snake_case to match pipeline)
    column_order = [
        'linkedin_url',
        'name',
        'first_name',
        'last_name',
        'headline',
        'current_title',
        'current_company',
        'location',
        'seniority',
        'company_size',
        'years_experience',
        'skills',
        'summary',
        '_source',
        '_needs_enrichment',
    ]

    # Reorder columns (keep any extra columns at the end)
    existing_cols = [c for c in column_order if c in df.columns]
    extra_cols = [c for c in df.columns if c not in column_order]
    df = df[existing_cols + extra_cols]

    return df


# =============================================================================
# USAGE TRACKING HELPER
# =============================================================================

def log_search_usage(
    tracker,
    profiles_found: int,
    credits_used: int,
    status: str = 'success',
    error_message: str = None,
    response_time_ms: int = None,
) -> Optional[Dict]:
    """
    Log Crustdata search usage to the usage tracker.

    Args:
        tracker: UsageTracker instance
        profiles_found: Number of profiles returned
        credits_used: Credits consumed
        status: 'success' or 'error'
        error_message: Error details if status is 'error'
        response_time_ms: API response time

    Returns:
        Logged record or None
    """
    if not tracker:
        return None

    return tracker.log_usage(
        provider='crustdata',
        operation='search',
        request_count=1,
        credits_used=credits_used,
        cost_usd=credits_used * 0.01,  # $0.01 per credit
        status=status,
        error_message=error_message,
        response_time_ms=response_time_ms,
        metadata={'profiles_found': profiles_found}
    )


# =============================================================================
# AI-ASSISTED EXPANSION
# =============================================================================

_EXPANSION_PROMPTS = {
    'title': (
        'You are a senior tech recruiter searching LinkedIn. Given the job title "{term}", '
        'list 5-10 alternative titles that REAL PEOPLE actually use on their LinkedIn profiles '
        'for the same role.\n'
        'Rules:\n'
        '- Every title you suggest MUST be something you would realistically find on LinkedIn\n'
        '- Do NOT invent creative combinations by mixing the input word with random nouns '
        '(e.g., "team guide", "team overseer", "team architect", "team chief" are NOT real titles)\n'
        '- Focus on: how different companies name the same job, abbreviations, seniority prefixes\n'
        '- Do NOT include titles at a higher seniority (e.g., director, VP) unless the input is at that level\n'
        'Return ONLY a valid JSON array of lowercase strings.\n'
        'Example for "team leader": ["team lead", "tech lead", "engineering lead", "r&d team lead"]\n'
        'Example for "devops engineer": ["devops developer", "cloud engineer", '
        '"infrastructure engineer", "site reliability engineer", "sre", "platform engineer"]'
    ),
    'skill': (
        'Given the technical skill "{term}", list 5-10 alternative names, abbreviations, '
        'or very closely related tools that recruiters treat as interchangeable. '
        'Only include skills that someone searching for "{term}" would also want to match. '
        'Do NOT include loosely related or adjacent technologies. '
        'Return ONLY a valid JSON array of lowercase strings. '
        'Example for "kubernetes": ["k8s", "docker", "helm", "container orchestration", "openshift"]'
    ),
    'company': (
        'Given the description "{term}"{geo_clause}, list 5-10 specific company names that match. '
        'The input may be a category (e.g., "SaaS startups in Tel Aviv"), a single company '
        '(e.g., "Wiz" → suggest similar companies), or a concept (e.g., "big tech"). '
        'Return ONLY a valid JSON array of strings with proper company name capitalization. '
        'Example: ["Wiz", "Monday.com", "Gong", "Snyk", "Fireblocks"]'
    ),
    'location': (
        'Given the location "{term}", list 5-10 specific locations useful for a LinkedIn '
        'people search. Rules:\n'
        '- If input is a country (e.g., "Israel"), return the country name plus its major cities\n'
        '- If input is a region (e.g., "west coast usa"), return specific cities in that region\n'
        '- If input is a city, return nearby cities and the metro area name\n'
        '- Use proper city names as they appear on LinkedIn profiles (not airport codes)\n'
        '- Include the original input if it is already a valid location\n'
        'Return ONLY a valid JSON array of strings.\n'
        'Example for "west coast usa": ["San Francisco", "Los Angeles", "Seattle", "San Diego", "Portland"]'
    ),
    'school': (
        'Given the description "{term}"{geo_clause}, list 5-10 specific university or school names '
        'that match. The input may be a category (e.g., "top tech universities in London"), '
        'a country, or a single school (suggest similar ones). '
        'Return ONLY a valid JSON array of strings with proper capitalization. '
        'Example: ["Imperial College London", "UCL", "King\'s College London"]'
    ),
    'keywords': (
        'Given the keyword or concept "{term}", list 5-10 closely related keywords, '
        'technologies, or terms that recruiters would search for together. '
        'Return ONLY a valid JSON array of lowercase strings. '
        'Example: ["microservices", "kubernetes", "docker", "service mesh", "api gateway"]'
    ),
}


def expand_variations(
    term: str,
    field_type: str = 'title',
    openai_api_key: str = None,
    exclude: List[str] = None,
    geo_context: str = None,
) -> List[str]:
    """
    Use OpenAI gpt-4o-mini to expand a term into common variations.

    Args:
        term: The user's input (e.g., "team leader", "SaaS startups in Tel Aviv")
        field_type: 'title', 'skill', 'company', 'location', 'school', or 'keywords'
        openai_api_key: OpenAI API key
        exclude: List of already-suggested values to exclude from results
        geo_context: Optional geographic scope (e.g. "Israel", "Tel Aviv") read
            from the user's Location filter. Only injected into prompts for
            field types where region matters in practice — currently 'company'
            and 'school'. Has no effect for 'location' (would be circular) or
            for skill/keyword expansions (region-neutral).

    Returns:
        List of 5-10 variations (always includes the original term for title/skill types).
        On failure, returns [term] (graceful fallback).
    """
    if not term or not term.strip():
        return []

    term = term.strip()

    if not openai_api_key:
        return [term]

    # Only field types whose prompt template references {geo_clause} get the
    # geographic scope. Other prompts ignore the parameter.
    if geo_context and field_type in ('company', 'school'):
        geo_clause = (
            f' (only suggest options with significant presence in {geo_context.strip()} — '
            f'companies/schools with offices, engineering teams, or hiring in that region)'
        )
    else:
        geo_clause = ''

    prompt_template = _EXPANSION_PROMPTS.get(field_type, _EXPANSION_PROMPTS['title'])
    # Templates that don't reference {geo_clause} ignore it via .format kwargs.
    try:
        prompt = prompt_template.format(term=term, geo_clause=geo_clause)
    except KeyError:
        # Defensive fallback for any template that doesn't accept geo_clause.
        prompt = prompt_template.format(term=term)

    if exclude:
        prompt += f'\nDo NOT include any of these (already suggested): {json.dumps(exclude)}'

    try:
        from openai import OpenAI
        client = OpenAI(api_key=openai_api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.3,
        )

        text = response.choices[0].message.content.strip()
        # Handle markdown code fences
        if text.startswith('```'):
            text = text.split('\n', 1)[1] if '\n' in text else text[3:]
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()
            if text.startswith('json'):
                text = text[4:].strip()

        variations = json.loads(text)
        if not isinstance(variations, list):
            return [term]

        # For title/skill/keywords: lowercase and include original term
        # For company/location/school: preserve capitalization
        preserve_case = field_type in ('company', 'location', 'school')
        if preserve_case:
            variations = [v.strip() for v in variations if isinstance(v, str) and v.strip()]
        else:
            variations = [v.strip().lower() for v in variations if isinstance(v, str) and v.strip()]
            if term.lower() not in variations:
                variations.insert(0, term.lower())

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for v in variations:
            if v not in seen:
                seen.add(v)
                unique.append(v)

        return unique

    except Exception:
        return [term]


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Constants
    'SENIORITY_LEVELS',
    'HEADCOUNT_RANGES',
    'CREDITS_PER_100_RESULTS',
    'CREDITS_PER_RESULT_SEMANTIC',
    'CREDITS_PER_RESULT_V2',
    'CREDITS_PER_ENRICH_PROFILE_BASE',
    'BATCH_ENRICH_FIELDS',
    # Main functions
    'search_people_db',
    'search_people_db_v2',
    'build_filters',
    'check_credits',
    'search_people_semantic',
    # Batch enrichment (new v2025-11-01 API)
    'submit_batch_enrich',
    'get_batch_status',
    'batch_enrich_profiles',
    'sync_enrich_profile',
    # Normalization
    'normalize_search_result',
    'normalize_search_results_to_df',
    'semantic_profile_to_legacy_shape',
    'enrich_profile_to_legacy_shape',
    # Usage tracking
    'log_search_usage',
    # AI expansion
    'expand_variations',
]
