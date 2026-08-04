"""
SourcingX Dashboard
Run with: streamlit run dashboard.py
"""

import streamlit as st
import pandas as pd
import json
import time
import re
import requests
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from openai import OpenAI
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import sys
from api_helpers import get_rate_limiter

# Platform-specific imports (for sound/notifications on Windows)
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

try:
    from plyer import notification
    HAS_PLYER = True
except ImportError:
    HAS_PLYER = False

# Unified normalizers (single source of truth for field mappings)
from normalizers import (
    normalize_linkedin_url,
    normalize_phantombuster_profile,
    normalize_crustdata_profile as normalize_crustdata_record,
    normalize_phantombuster_batch,
    normalize_crustdata_batch,
    profile_to_display_dict,
    profiles_to_display_df,
    parse_duration,
    clean_dict,
    clean_value,
    pick_current_employer,
)
from helpers import format_past_positions, format_education
from company_matching import (
    normalize_company_name as _normalize_company_name,
    company_matches_filter_list as _company_matches_filter_list,
)
from screening_policy import (
    get_system_prompt as _policy_system_prompt,
    build_user_prompt as _policy_user_prompt,
    get_structured_system_prompt as _structured_system_prompt,
    build_structured_user_prompt as _structured_user_prompt,
    build_nice_to_have_prompt as _nice_to_have_prompt,
    NICE_TO_HAVE_SYSTEM_PROMPT as _NICE_TO_HAVE_SYSTEM,
)


# Email generator module
try:
    from email_generator import (
        generate_emails_batch,
        estimate_cost as estimate_email_cost,
        get_angle_distribution,
        SENDER_PERSONAS,
        TONE_DESCRIPTIONS,
        LENGTH_DESCRIPTIONS,
    )
    HAS_EMAIL_GENERATOR = True
except ImportError:
    HAS_EMAIL_GENERATOR = False

# Database module (Supabase integration)
# Note: PhantomBuster data is NOT stored in DB - only Crustdata enriched profiles
try:
    from db import (
        get_supabase_client, check_connection, save_enriched_profile,
        update_profile_enrichment, update_profile_screening, update_profile_screening_batch, get_all_profiles, save_enriched_profiles_bulk,
        get_pipeline_stats, get_profiles_by_fit_level, get_all_linkedin_urls,
        get_dedup_stats, profiles_to_dataframe, get_usage_summary, get_usage_logs,
        get_usage_by_date, get_enriched_urls, get_recently_enriched_urls,
        get_setting, save_setting,
        get_search_history, save_search_history_entry, delete_search_history_entry,
        hash_search_filters, summarize_search_filters, record_people_search,
        update_people_search_urls,
        find_people_search, list_people_searches, delete_people_search,
        match_profiles_by_urls_rpc,
        save_failed_enrichments_batch, get_not_found_urls,
        update_profile_email, update_profile_emails_batch, get_profile,
        get_profiles_by_urls,
        ENRICHMENT_REFRESH_MONTHS,
    )
    from pb_dedup import filter_results_against_database, update_phantombuster_with_skip_list, get_skip_list_from_database
    HAS_DATABASE = True
except ImportError:
    HAS_DATABASE = False

# Structured screening module
try:
    from screening_prompt_builder import (
        STRUCTURED_SYSTEM_PROMPT,
        build_user_prompt,
        screen_with_structured_prompt,
        screen_batch_structured,
    )
    HAS_STRUCTURED_SCREENING = True
except ImportError:
    HAS_STRUCTURED_SCREENING = False

# Usage tracking module
try:
    from usage_tracker import UsageTracker, calculate_openai_cost
    HAS_USAGE_TRACKER = True
except ImportError:
    HAS_USAGE_TRACKER = False

# Crustdata people search module
try:
    from crustdata_search import (
        search_people_db,
        search_people_db_v2,
        build_filters as build_search_filters,
        normalize_search_results_to_df,
        check_credits as check_crustdata_credits,
        expand_variations,
        search_people_semantic,
        semantic_profile_to_legacy_shape,
        batch_enrich_profiles,
        sync_enrich_profile,
        SENIORITY_LEVELS,
        HEADCOUNT_RANGES,
        FUNCTION_CATEGORIES,
        COMPANY_INDUSTRIES,
    )
    HAS_CRUSTDATA_SEARCH = True

    # Crustdata's legacy /screener/persondb/search endpoint (compact=false)
    # returns full profiles — skills/summary included — in one call, and is
    # cheaper/richer than the new v2025-11-01 /person/search endpoint, which
    # never returns skills/summary regardless of what's requested. Crustdata
    # has confirmed the legacy endpoint stays live until end of September
    # 2026, so this branch's search-endpoint migration ships DISABLED by
    # default — search_people_db() (legacy) stays the default search
    # function everywhere in this file. The auto-fill-before-screening step
    # (enrich_thin_profiles_for_batch, already live since 2026-07-20) already
    # covers thin profiles regardless of which search sourced them, so
    # flipping this one flag is the only change needed when the legacy
    # endpoint is retired — nothing else in this file changes. Flip via
    # CRUSTDATA_USE_SEARCH_V2=true in the environment (not config.json —
    # this is a deploy-time switch, not a per-user setting).
    CRUSTDATA_USE_SEARCH_V2 = os.environ.get('CRUSTDATA_USE_SEARCH_V2', 'false').strip().lower() == 'true'
    _active_search_people_db = search_people_db_v2 if CRUSTDATA_USE_SEARCH_V2 else search_people_db
except ImportError:
    HAS_CRUSTDATA_SEARCH = False
    CRUSTDATA_USE_SEARCH_V2 = False

# Plotly for charts
try:
    import plotly.express as px
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

# Authentication (optional - only enabled when auth secrets are configured)
try:
    import streamlit_authenticator as stauth
    HAS_AUTHENTICATOR = True
except ImportError:
    HAS_AUTHENTICATOR = False

# Cookie manager for session persistence
try:
    import extra_streamlit_components as stx
    HAS_COOKIE_MANAGER = True
except ImportError:
    HAS_COOKIE_MANAGER = False

import hashlib  # For performance caching

# =============================================================================
# AI field-expansion helpers (shared across Tab 0 Search and Tab 7 Database)
# =============================================================================
# The Crustdata People DB tab (Tab 0) introduced an inline "AI refine" UX:
# user types a value into a text input, clicks the "Refine with AI" button,
# the LLM (via crustdata_search.expand_variations) returns 5-10 LinkedIn-style
# variations, those land in a multiselect, and on Search the manual input +
# the multiselect selections are merged into the value passed to the backend.
#
# These helpers are reusable across tabs as long as each tab uses its own
# session-state key prefix (Tab 0 uses ``crust_search_<field>`` /
# ``expanded_<field>``; Tab 7 uses ``db_search_<field>`` /
# ``db_expanded_<field>``). The helpers don't hard-code any keys.
#
# Streamlit constraints:
# - Button ``on_click`` callbacks can't fire inside ``st.form(...)``. Tabs that
#   want AI refine must keep the inputs OUTSIDE the form (state still persists
#   via ``key=`` on the text input).
# - The multiselect's ``key`` is always ``f"sel_{expanded_key}"`` so
#   ``_ai_effective_value`` can read the user's selection.


def _read_geo_context(geo_key):
    """Read the user's Location filter from session state.

    Used to scope company / school AI expansions to a region. Returns the
    Location field's current text (and any AI-multiselect picks merged in via
    _ai_effective_value if those keys exist) or None if no geo context is set
    or no key was supplied.
    """
    if not geo_key:
        return None
    # If the geo_key has a matching expanded_<field>, use the effective value
    # (merge of typed text + multiselect selections) so an AI-expanded
    # location like "Tel Aviv, Ramat Gan, Herzliya" all participates.
    # Tab 0 uses crust_search_location + expanded_locations.
    # Tab 7 uses db_f_location + db_expanded_location.
    expanded_candidates = []
    if geo_key.startswith('crust_search_'):
        expanded_candidates.append(geo_key.replace('crust_search_', 'expanded_') + 's')
        expanded_candidates.append(geo_key.replace('crust_search_', 'expanded_'))
    elif geo_key.startswith('db_f_'):
        expanded_candidates.append(geo_key.replace('db_f_', 'db_expanded_'))
    for cand in expanded_candidates:
        if cand in st.session_state:
            eff = _ai_effective_value(geo_key, cand)
            if eff:
                return eff
    val = st.session_state.get(geo_key, '')
    val = (val or '').strip()
    return val or None


def _ai_expand_callback(input_key, expanded_key, field_type, openai_api_key, geo_key=None):
    """on_click: read input_key, AI-expand each manual term, merge with existing.

    Stores the merged list at ``st.session_state[expanded_key]`` and sets
    ``st.session_state[f'sel_{expanded_key}']`` to the same list so the
    multiselect defaults to "everything selected".

    If ``geo_key`` is supplied, the corresponding Location field's value is
    read and passed to ``expand_variations`` so company/school suggestions
    are scoped to the recruiter's geographic search (e.g. typing
    "monday.com" with Location "Israel" returns Israeli SaaS peers, not
    global ones).
    """
    if not openai_api_key or not HAS_CRUSTDATA_SEARCH:
        return
    val = st.session_state.get(input_key, '').strip()
    if not val:
        return
    geo_context = _read_geo_context(geo_key)
    # Parse comma-separated manual values
    manual_terms = [t.strip() for t in val.split(',') if t.strip()]
    # Start with existing suggestions + manual terms
    all_items = list(st.session_state.get(expanded_key, []))
    all_items.extend(manual_terms)
    # AI-expand each manual term
    for term in manual_terms:
        sug = expand_variations(term, field_type=field_type,
                                openai_api_key=openai_api_key,
                                geo_context=geo_context)
        all_items.extend(sug)
    # Dedupe preserving order (case-insensitive)
    seen = set()
    merged = []
    for item in all_items:
        k = item.lower()
        if k not in seen:
            seen.add(k)
            merged.append(item)
    st.session_state[expanded_key] = merged
    st.session_state[f"sel_{expanded_key}"] = merged


def _ai_generate_more_callback(input_key, expanded_key, field_type, openai_api_key, geo_key=None):
    """on_click: generate additional suggestions, excluding the ones already shown."""
    if not openai_api_key or not HAS_CRUSTDATA_SEARCH:
        return
    val = st.session_state.get(input_key, '').strip()
    if not val:
        return
    geo_context = _read_geo_context(geo_key)
    existing = st.session_state.get(expanded_key, [])
    manual_terms = [t.strip() for t in val.split(',') if t.strip()]
    new_items = list(existing)
    for term in manual_terms:
        sug = expand_variations(term, field_type=field_type,
                                openai_api_key=openai_api_key, exclude=existing,
                                geo_context=geo_context)
        new_items.extend(sug)
    seen = set()
    merged = []
    for item in new_items:
        k = item.lower()
        if k not in seen:
            seen.add(k)
            merged.append(item)
    st.session_state[expanded_key] = merged
    st.session_state[f"sel_{expanded_key}"] = merged


def _ai_clear_expanded_callback(expanded_key):
    """on_click: drop the AI suggestions for a field."""
    st.session_state.pop(expanded_key, None)


def _ai_add_custom_value_callback(custom_key, expanded_key):
    """on_change: add a free-text variant to the multiselect options + auto-select it."""
    val = st.session_state.get(custom_key, '').strip()
    if val and expanded_key in st.session_state:
        new_items = [v.strip() for v in val.split(',') if v.strip()]
        existing = st.session_state[expanded_key]
        existing_lower = {e.lower() for e in existing}
        sel_key = f"sel_{expanded_key}"
        current_sel = list(st.session_state.get(sel_key, existing))
        for item in new_items:
            if item.lower() not in existing_lower:
                existing.append(item)
                existing_lower.add(item.lower())
                current_sel.append(item)
        st.session_state[expanded_key] = existing
        st.session_state[sel_key] = current_sel
        st.session_state[custom_key] = ''


def _ai_render_field(container, label, input_key, expanded_key, field_type,
                     placeholder, openai_api_key, has_ai, geo_key=None):
    """Render text input + "AI Refine" button + (if expanded) a multiselect.

    Uses the provided container so callers can place the field inside their
    own column layout without running into Streamlit's nested-columns rule.

    ``geo_key`` (optional) is the session-state key holding the user's
    Location filter. When supplied AND ``field_type`` is one where geography
    matters (company / school), the expansion call passes that location as
    context so suggestions are scoped to the recruiter's geographic search.
    Callers SHOULD NOT pass ``geo_key`` when rendering the Location field
    itself — it would be circular.
    """
    with container:
        st.text_input(label, key=input_key, placeholder=placeholder)
        if has_ai:
            st.button(
                "✨",
                key=f"btn_ai_{input_key}",
                on_click=_ai_expand_callback,
                args=(input_key, expanded_key, field_type, openai_api_key, geo_key),
                help="AI suggest variations",
                use_container_width=True,
            )

        if st.session_state.get(expanded_key):
            st.multiselect(
                f"Select {label.split('(')[0].strip()}:",
                options=st.session_state[expanded_key],
                default=st.session_state[expanded_key],
                key=f"sel_{expanded_key}",
                label_visibility="collapsed",
            )
            custom_key = f"custom_{expanded_key}"
            st.text_input(
                "Add custom:",
                key=custom_key,
                placeholder="Type and press Enter",
                on_change=_ai_add_custom_value_callback,
                args=(custom_key, expanded_key),
                label_visibility="collapsed",
            )
            more_col, clear_col = st.columns(2)
            with more_col:
                st.button(
                    "✨ More",
                    key=f"btn_more_{expanded_key}",
                    on_click=_ai_generate_more_callback,
                    args=(input_key, expanded_key, field_type, openai_api_key, geo_key),
                    help="Generate more suggestions",
                    use_container_width=True,
                )
            with clear_col:
                st.button(
                    "✕ Clear",
                    key=f"btn_clear_{expanded_key}",
                    on_click=_ai_clear_expanded_callback,
                    args=(expanded_key,),
                    help="Clear suggestions",
                    use_container_width=True,
                )


def _ai_effective_value(input_key, expanded_key):
    """Merge manual text input + AI multiselect selections.

    Returns a comma-separated string of unique values (case-insensitive
    dedupe, original case preserved). Works with any key prefix — both Tab 0
    (``crust_search_*`` / ``expanded_*``) and Tab 7 (``db_search_*`` /
    ``db_expanded_*``) call this with their own keys.
    """
    parts = []
    raw = st.session_state.get(input_key, '').strip()
    if raw:
        parts.extend([v.strip() for v in raw.split(',') if v.strip()])
    sel = st.session_state.get(f"sel_{expanded_key}", [])
    if sel:
        parts.extend(sel)
    seen = set()
    unique = []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            unique.append(p)
    return ', '.join(unique)


# ===== Performance: on_click callbacks to avoid st.rerun() =====
# These callbacks update session state directly; Streamlit auto-reruns after callbacks

def _cb_logout():
    """Logout callback - clears auth state."""
    st.session_state['authentication_status'] = None
    st.session_state['username'] = None
    st.session_state['name'] = None

def _cb_select_all(key: str, count: int):
    """Select all items callback."""
    st.session_state[key] = list(range(count))

def _cb_deselect_all(key: str):
    """Deselect all items callback."""
    st.session_state[key] = []

def _cb_clear_keys(keys: list):
    """Clear multiple session state keys."""
    for key in keys:
        st.session_state.pop(key, None)

def _cb_set_value(key: str, value):
    """Set a session state value."""
    st.session_state[key] = value

def _cb_clear_screening_results():
    """Clear screening results."""
    st.session_state.pop('screening_results', None)

def _cb_reset_phantom():
    """Reset PhantomBuster launch state."""
    st.session_state['pb_launch_status'] = 'idle'
    st.session_state.pop('pb_launch_error', None)
    st.session_state.pop('pb_launch_csv_name', None)
    st.session_state['pb_progress_info'] = {}
    st.session_state['pb_launch_skip_count'] = 0

def _cb_toggle_exclude_preset(cat_keywords: list):
    """Toggle exclude title preset category."""
    current = st.session_state.get('exclude_title_presets', [])
    if all(kw in current for kw in cat_keywords):
        st.session_state['exclude_title_presets'] = [k for k in current if k not in cat_keywords]
    else:
        st.session_state['exclude_title_presets'] = list(set(current + cat_keywords))


# ===== Performance: Module-level cached functions (avoid re-creating in tabs) =====
# These were previously nested inside tab logic and re-created on every render

@st.cache_data(ttl=300, max_entries=3)
def _get_crustdata_credits_cached(_api_key: str):
    """Cached Crustdata credits check - module level for efficiency."""
    try:
        from crustdata_search import check_credits as check_crustdata_credits
        return check_crustdata_credits(api_key=_api_key)
    except Exception as e:
        return {"remaining": "?", "error": str(e)}

@st.cache_data(ttl=120, max_entries=3)
def _get_db_restore_counts_cached():
    """Cached DB enriched profile count - module level for efficiency."""
    try:
        c = _get_db_client()
        if c:
            return c.count('profiles', {'enrichment_status': 'eq.enriched'})
        return 0
    except Exception:
        return 0

@st.cache_data(ttl=300, max_entries=5)
def _load_search_history_module(agent_id: str):
    """Cached search history loader - module level for efficiency."""
    try:
        from db import get_search_history as load_search_history
        return load_search_history(agent_id=agent_id)
    except Exception:
        return []

@st.cache_data(ttl=120, max_entries=3, show_spinner=False)
def _cached_not_found_urls(months: int) -> list:
    """Cached not-found URLs from DB - avoids re-fetching on every enrich tab render."""
    try:
        c = _get_db_client()
        if c:
            return get_not_found_urls(c, months=months)
    except Exception:
        pass
    return []

@st.cache_data(ttl=60, max_entries=3, show_spinner="Loading profiles...")
def _cached_all_profiles(limit: int = 50000) -> list:
    """Cached all-profiles fetch for database tab (no-filter case)."""
    try:
        c = _get_db_client()
        if c:
            return get_all_profiles(c, limit=limit)
    except Exception:
        pass
    return []

@st.cache_data(ttl=120, max_entries=3, show_spinner=False)
def _cached_recently_enriched_urls(months: int) -> list:
    """Cached recently enriched URL list from DB."""
    try:
        c = _get_db_client()
        if c:
            return get_recently_enriched_urls(c, months=months)
    except Exception:
        pass
    return []


def _build_candidate_query_specs(urls: list, cutoff: str) -> list:
    """Build the PostgREST query specs that fetch DB profiles matching `urls`.

    Pure (no I/O) so it can be unit-tested. Returns a list of dicts shaped
    {'filters': <primary>, 'fallback': <optional simpler filter>}, each meant to
    run as one HTTP GET. Two kinds of spec are produced:

    1. Exact pass — the normalized URL (and its hyphen-free form) matched against
       linkedin_url / original_url / original_urls.
    2. Suffix pass — a clean multi-part name slug (john-doe) may be stored in the
       DB only under an ID-suffixed form (john-doe-abc123, or the hyphen-free
       johndoe-abc123). An exact lookup would miss those, so a left-anchored LIKE
       prefix (`/in/<stem>-*`) is matched against linkedin_url / original_url for
       both the original slug and its hyphen-free form. The variant matcher in
       the caller re-filters, so an over-broad prefix hit costs a wasted row
       fetch, never a wrong match.

    Accepted residual: the suffix pass does NOT prefix-match inside the
    original_urls array — PostgREST can't express "any array element LIKE
    prefix" without a custom DB function, and this is a shared database. So a
    multi-source row whose ONLY suffixed form lives in original_urls (its
    linkedin_url / original_url being some other source) is not fetched for a
    clean-slug input. Measured at 47 / 31.5k rows (0.15%) and self-healing — the
    profile gets re-enriched once (~$0.03), after which its canonical URL is
    recorded and every later lookup matches. Pinned by
    test_suffixed_url_only_in_original_urls_array_is_accepted_residual.
    """
    exact_urls = set()        # for the exact in.() / ov.{} pass
    prefix_stems = set()      # for the suffix-variant prefix pass

    for url in urls:
        normalized = normalize_linkedin_url(url)
        if not normalized or '/in/' not in normalized:
            continue
        # Use the normalized URL exactly as-is. The DB stores linkedin_url via
        # the same normalize_linkedin_url(), which lowercases ASCII but PRESERVES
        # percent-encoding case (e.g. Hebrew slugs: %D7%90). Lowercasing here
        # would turn %D7 into %d7 and the exact in.() match would silently miss.
        exact_urls.add(normalized)
        slug = normalized.split('/in/')[-1].rstrip('/')
        no_hyphen = slug.replace('-', '')
        if no_hyphen and no_hyphen != slug:
            exact_urls.add(f'https://www.linkedin.com/in/{no_hyphen}')
        # Suffix pass — for a hyphenated slug whose last segment is NOT a numeric
        # LinkedIn ID. Such a slug (john-doe, einav-friedman) may be stored in
        # the DB only under an ID-suffixed form; the exact pass would miss it. If
        # the last segment already contains a digit the slug IS the full
        # ID-suffixed form, so there's nothing extra to look for. '%' slugs are
        # skipped — '%' is a LIKE wildcard — and are covered exactly. Both the
        # slug and its hyphen-free form are added as stems, because the DB may
        # store the suffixed form either way (john-doe-abc123 OR johndoe-abc123).
        if '-' in slug and '%' not in slug:
            last_segment = slug.rsplit('-', 1)[-1]
            if not any(ch.isdigit() for ch in last_segment):
                prefix_stems.add(slug)
                if no_hyphen and no_hyphen != slug:
                    prefix_stems.add(no_hyphen)

    query_specs = []

    # Exact pass. Batch kept small: the quoted URL list is repeated 3x in the OR
    # filter, and PostgREST rejects GET query strings beyond ~20k chars (400).
    exact_list = sorted(exact_urls)
    for i in range(0, len(exact_list), 60):
        batch = exact_list[i:i + 60]
        quoted = ','.join(f'"{u}"' for u in batch)
        query_specs.append({
            'filters': {
                'or': (
                    f'(linkedin_url.in.({quoted}),'
                    f'original_url.in.({quoted}),'
                    f'original_urls.ov.{{{quoted}}})'
                ),
                'enriched_at': f'gte.{cutoff}',
            },
            # Fallback if the combined OR is rejected (e.g. original_urls column
            # missing on an un-migrated DB): plain linkedin_url lookup.
            'fallback': {'linkedin_url': f'in.({quoted})', 'enriched_at': f'gte.{cutoff}'},
        })

    # Suffix pass. A left-anchored LIKE prefix per stem — matches any stored URL
    # whose path is `/in/<stem>-...`. LIKE is ~2.5x faster than an equivalent
    # regex here and returns the same rows. The value is double-quoted so the
    # ':' '/' '*' chars survive or=() parsing. Stems containing '%' (which LIKE
    # would treat as a wildcard) were already excluded above.
    prefix_list = sorted(prefix_stems)
    for i in range(0, len(prefix_list), 100):
        batch = prefix_list[i:i + 100]
        clauses = []
        for stem in batch:
            pattern = f'"https://www.linkedin.com/in/{stem}-*"'
            clauses.append(f'linkedin_url.like.{pattern}')
            clauses.append(f'original_url.like.{pattern}')
        query_specs.append({
            'filters': {
                'or': '(' + ','.join(clauses) + ')',
                'enriched_at': f'gte.{cutoff}',
            },
        })

    return query_specs


def _fetch_candidate_profiles_for_urls(db_client, urls: list, columns: str, cutoff: str) -> list:
    """Fetch only the DB profiles that could match the given input URLs.

    Replaces the old "download every profile enriched in the last N months and
    build a 30k+-entry lookup" approach, which got slower on every enrichment as
    the table grew. The caller still runs the full username-variant matching on
    whatever comes back — this only narrows the candidate set from the whole
    table to roughly the input size. See _build_candidate_query_specs for how
    the (exact + suffix-variant) matching contract is preserved.
    """
    query_specs = _build_candidate_query_specs(urls, cutoff)
    if not query_specs:
        return []

    def _fetch_one(spec):
        try:
            return db_client.select('profiles', columns, filters=spec['filters'], limit=1000)
        except Exception:
            fallback = spec.get('fallback')
            if fallback:
                try:
                    return db_client.select('profiles', columns, filters=fallback, limit=1000)
                except Exception:
                    return []
            return []

    # Run the batch queries concurrently — they're independent HTTP GETs, and
    # the round-trip latency to Supabase (eu-west-1) dominates. Serial fetching
    # of these batches was the remaining slow part after killing the full scan.
    seen_pk = set()
    rows = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        for page in executor.map(_fetch_one, query_specs):
            for p in (page or []):
                pk = p.get('linkedin_url')
                if pk and pk in seen_pk:
                    continue
                if pk:
                    seen_pk.add(pk)
                rows.append(p)
    return rows


def _start_keep_alive():
    """Ping the app's own URL every 10 minutes to prevent Streamlit Cloud from sleeping."""
    app_url = None
    try:
        app_url = st.secrets.get("keep_alive_url")
    except Exception:
        pass
    if not app_url:
        return  # No URL configured — skip keep-alive
    print(f"[Keep-Alive] Started — pinging {app_url} every 10 min")
    def ping():
        while True:
            try:
                r = requests.get(app_url, timeout=30)
                print(f"[Keep-Alive] Ping OK — status {r.status_code}")
            except Exception as e:
                print(f"[Keep-Alive] Ping failed — {e}")
            time.sleep(600)  # 10 minutes
    t = threading.Thread(target=ping, daemon=True)
    t.start()

if 'keep_alive_started' not in st.session_state:
    _start_keep_alive()
    st.session_state['keep_alive_started'] = True


def get_auth_config():
    """Load authentication config from Streamlit secrets."""
    try:
        if hasattr(st, 'secrets') and 'auth' in st.secrets:
            # Deep copy credentials to avoid immutability issues
            creds = st.secrets['auth']['credentials']
            credentials = {
                'usernames': {}
            }
            for username in creds['usernames']:
                user_data = creds['usernames'][username]
                credentials['usernames'][username] = {
                    'email': user_data.get('email', ''),
                    'first_name': user_data.get('first_name', ''),
                    'last_name': user_data.get('last_name', ''),
                    'password': user_data.get('password', '')
                }
            return {
                'credentials': credentials,
                'cookie': {
                    'name': st.secrets['auth']['cookie_name'],
                    'key': st.secrets['auth']['cookie_key'],
                    'expiry_days': st.secrets['auth']['cookie_expiry_days']
                }
            }
    except Exception as e:
        pass
    return None


# Page config
st.set_page_config(
    page_title="SourcingX",
    page_icon="🔍",
    layout="wide"
)

# Authentication check (only when auth is configured)
auth_config = get_auth_config()
authenticator = None
cookie_manager = None

if auth_config and HAS_AUTHENTICATOR:
    # Initialize cookie manager for session persistence
    if HAS_COOKIE_MANAGER:
        cookie_manager = stx.CookieManager(key="auth_cookie_manager")

    # Check for existing auth cookie BEFORE showing login form
    auth_token = None
    if cookie_manager:
        auth_token = cookie_manager.get(auth_config['cookie']['name'])

    # If we have a valid auth token, restore session
    if auth_token and auth_token in auth_config['credentials']['usernames']:
        st.session_state['authentication_status'] = True
        st.session_state['username'] = auth_token
        user_data = auth_config['credentials']['usernames'][auth_token]
        st.session_state['name'] = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip() or auth_token

    # Create authenticator (still needed for login form and password verification)
    authenticator = stauth.Authenticate(
        auth_config['credentials'],
        auth_config['cookie']['name'],
        auth_config['cookie']['key'],
        auth_config['cookie']['expiry_days']
    )

    # Only show login form if not already authenticated
    if st.session_state.get('authentication_status') is not True:
        authenticator.login(location='main')

    if st.session_state.get('authentication_status') is False:
        st.error('Username/password is incorrect')
        st.stop()
    elif st.session_state.get('authentication_status') is None:
        st.warning('Please enter your username and password')
        st.stop()
    else:
        # User is logged in - save cookie for persistence
        if cookie_manager and st.session_state.get('username'):
            current_cookie = cookie_manager.get(auth_config['cookie']['name'])
            if current_cookie != st.session_state.get('username'):
                cookie_manager.set(
                    auth_config['cookie']['name'],
                    st.session_state.get('username'),
                    expires_at=datetime.now() + timedelta(days=auth_config['cookie']['expiry_days'])
                )

        # Add logout button to sidebar
        with st.sidebar:
            st.write(f"Welcome, **{st.session_state.get('name', 'User')}**")
            # Define logout callback with closure for cookie_manager
            def _do_logout():
                if cookie_manager:
                    cookie_manager.delete(auth_config['cookie']['name'])
                _cb_logout()
            st.button("Logout", key="logout_btn", on_click=_do_logout)

# Dark theme UI styling (StockPeers-inspired)
st.markdown("""
<style>
    /* Tab styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background-color: #0f172b;
        padding: 0.5rem;
        border-radius: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        background-color: #1d293d;
        border-radius: 6px;
        padding: 0.5rem 1rem;
        font-weight: 400;
        border: 1px solid #314158;
    }
    .stTabs [aria-selected="true"] {
        background-color: #615fff !important;
        color: #e2e8f0 !important;
        border-color: #615fff !important;
    }

    /* Title styling */
    h1 { color: #615fff; border-bottom: 2px solid #314158; padding-bottom: 0.5rem; }

    /* Metric cards */
    [data-testid="stMetricValue"] { font-size: 2rem; color: #615fff; }

    /* Buttons */
    .stButton > button[kind="primary"] {
        background-color: #615fff;
        border-radius: 24px;
        font-weight: 600;
    }

    /* Sidebar */
    [data-testid="stSidebar"] { background-color: #0f172b; }
</style>
""", unsafe_allow_html=True)

# Load API keys
@st.cache_data(ttl=3600, max_entries=3)  # Config rarely changes - cache for 1 hour
def is_admin_user() -> bool:
    """Return True if the current logged-in username is in config.admin_usernames.
    Used to gate the legacy role-prompt dropdown (non-admins see only the
    unified-policy textbox)."""
    try:
        cfg = load_config()
        admins = cfg.get('admin_usernames') or []
        username = st.session_state.get('username') or ''
        return bool(username) and username in admins
    except Exception:
        return False


def load_config():
    """Load config from config.json or Streamlit secrets (for cloud deployment)."""
    config = {}

    # Try loading from config.json (local development)
    config_path = Path(__file__).parent / 'config.json'
    if config_path.exists():
        with open(config_path, 'r') as f:
            config = json.load(f)

    # Local: if google_credentials_file is specified, load the JSON file into google_credentials
    if config.get('google_credentials_file') and not config.get('google_credentials'):
        gc_path = Path(__file__).parent / config['google_credentials_file']
        if gc_path.exists():
            with open(gc_path, 'r') as f:
                config['google_credentials'] = json.load(f)

    # Local: map google_sheets URLs to filter_sheets config if filter_sheets not set
    if config.get('google_sheets') and not config.get('filter_sheets'):
        gs = config['google_sheets']
        # All sheets share the same spreadsheet URL - extract from any entry
        first_url = next(iter(gs.values()), '')
        config['filter_sheets'] = {
            'url': first_url,
            'target_companies': 'Target Companies',
            'not_relevant': 'NotRelevant Companies',
            'blacklist': 'Blacklist',
            'past_candidates': 'Past Candidates',
            'layoff_companies': 'Layoff Companies',
            'universities': 'Universities',
            'client_wanted_companies': 'Client specific wanted companies',
        }

    # Override with Streamlit secrets if available (cloud deployment)
    try:
        if hasattr(st, 'secrets') and len(st.secrets) > 0:
            # Map Streamlit secrets to config keys
            if 'api_key' in st.secrets:
                config['api_key'] = st.secrets['api_key']
            if 'openai_api_key' in st.secrets:
                config['openai_api_key'] = st.secrets['openai_api_key']
            elif 'OPENAI_API_KEY' in st.secrets:
                config['openai_api_key'] = st.secrets['OPENAI_API_KEY']
            if 'phantombuster_api_key' in st.secrets:
                config['phantombuster_api_key'] = st.secrets['phantombuster_api_key']
            if 'phantombuster_agent_id' in st.secrets:
                config['phantombuster_agent_id'] = st.secrets['phantombuster_agent_id']
            if 'google_credentials' in st.secrets:
                config['google_credentials'] = dict(st.secrets['google_credentials'])
            if 'filter_sheets' in st.secrets:
                config['filter_sheets'] = dict(st.secrets['filter_sheets'])
            if 'salesql_api_key' in st.secrets:
                config['salesql_api_key'] = st.secrets['salesql_api_key']
            if 'anthropic_api_key' in st.secrets:
                config['anthropic_api_key'] = st.secrets['anthropic_api_key']
            elif 'ANTHROPIC_API_KEY' in st.secrets:
                config['anthropic_api_key'] = st.secrets['ANTHROPIC_API_KEY']
            if 'admin_usernames' in st.secrets:
                # Streamlit secrets returns AttrDict for lists — coerce to list
                config['admin_usernames'] = list(st.secrets['admin_usernames'])
    except Exception:
        pass

    return config

def _strip_secret(val):
    """Return ``val`` with surrounding whitespace removed, or ``val`` unchanged
    when falsy (so empty/None semantics are preserved for callers that test
    truthiness).

    Defensive against secrets stored with a stray leading/trailing newline —
    most often a trailing ``\\n`` from a sloppy ``gh secret set --body``
    invocation — which otherwise blow up the HTTP layer with cryptic errors
    like "Invalid leading whitespace, reserved character(s), or return
    character(s) in header value". Only strips outer whitespace; an embedded
    space in the middle of a key is left alone.
    """
    if val and isinstance(val, str):
        return val.strip()
    return val


def load_api_key():
    config = load_config()
    return _strip_secret(config.get('api_key'))

def load_openai_key():
    config = load_config()
    return _strip_secret(config.get('openai_api_key'))

def load_anthropic_key():
    config = load_config()
    key = config.get('anthropic_api_key')
    if not key:
        try:
            if 'anthropic_api_key' in st.secrets:
                key = st.secrets['anthropic_api_key']
            elif 'ANTHROPIC_API_KEY' in st.secrets:
                key = st.secrets['ANTHROPIC_API_KEY']
        except Exception:
            pass
    return _strip_secret(key)


def load_phantombuster_key():
    config = load_config()
    return _strip_secret(config.get('phantombuster_api_key'))


def load_phantombuster_agent_id():
    config = load_config()
    return _strip_secret(config.get('phantombuster_agent_id'))


def load_salesql_key():
    config = load_config()
    return _strip_secret(config.get('salesql_api_key'))


# ===== Export Helpers =====
def blank_tenure_sentinel(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with the -1 tenure sentinel blanked out.

    -1 is an internal sentinel for "tenure unknown" on current_years_at_company
    (legacy backfill marker). Don't leak it into anything user-facing — not the
    explicit "Download CSV" buttons AND not the on-screen st.dataframe tables
    (Streamlit's built-in download icon exports those raw). Match both numeric
    and string forms in case the column was ever coerced to object.
    """
    if df is None or df.empty or 'current_years_at_company' not in df.columns:
        return df
    out = df.copy()
    col = out['current_years_at_company']
    mask = (col == -1) | (col == '-1') | (col == -1.0) | (col == '-1.0')
    out.loc[mask, 'current_years_at_company'] = None
    return out


def prepare_df_for_export(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare a DataFrame for CSV export with consistent column order.

    Ensures key columns (first_name, last_name, location, university) come first,
    followed by other profile data, then screening/email fields.
    """
    if df is None or df.empty:
        return df

    # Preferred column order - personal info first, then role, then extras
    preferred_order = [
        'first_name', 'last_name', 'name', 'email', 'salesql_email', 'linkedin_url', 'public_url',
        'location', 'university', 'education', 'all_schools',
        'current_title', 'current_company', 'headline', 'title', 'company',
        'score', 'fit', 'summary', 'reasoning',
        'subject_line', 'email_opener', 'subject_angle', 'opener_angle'
    ]

    # Build final column order
    final_cols = []
    for col in preferred_order:
        if col in df.columns:
            final_cols.append(col)

    # Add any remaining columns not in preferred order (except internal ones)
    for col in df.columns:
        if col not in final_cols and not col.startswith('_') and col != 'index':
            final_cols.append(col)

    out = df[final_cols]

    return blank_tenure_sentinel(out)


def build_full_details_export_df(raw_profiles: list) -> pd.DataFrame:
    """Build the export-ready DataFrame for the Database tab's full-details CSV.

    Takes profiles that were fetched WITH raw_data (so ``summary`` and the full
    ``past_positions`` job-description text are populated) and runs them through
    the same ``profiles_to_dataframe`` -> ``prepare_df_for_export`` composition
    every other CSV export uses, so the column ordering stays consistent.

    Also attaches a ``crustdata_raw_json`` column with the complete raw
    Crustdata response verbatim (``json.dumps(raw_data)``). The flattened
    columns above only surface a curated subset (e.g. they drop certifications,
    languages, the current role's own description, education dates/degrees) —
    the raw JSON column guarantees this export has everything Crustdata
    actually returned for each person, not just what got hand-picked into a
    column.

    Pure/transient by design: it returns a fresh DataFrame and never touches
    session state or the shared listing — the caller stashes only the resulting
    CSV bytes, not this DataFrame or the raw profile dicts.
    """
    from db import profiles_to_dataframe
    df = profiles_to_dataframe(raw_profiles)

    raw_json_by_url = {
        p.get('linkedin_url', ''): json.dumps(p.get('raw_data') or {}, ensure_ascii=False)
        for p in (raw_profiles or [])
        if p.get('linkedin_url')
    }
    if not df.empty and 'linkedin_url' in df.columns:
        df['crustdata_raw_json'] = df['linkedin_url'].map(raw_json_by_url).fillna('')

    return prepare_df_for_export(df)


def build_search_full_details_export_df(raw_profiles: list) -> pd.DataFrame:
    """Build the export-ready DataFrame for the Search tab's full-details CSV.

    Runs profiles through the existing normalize_search_results_to_df() ->
    prepare_df_for_export() composition (the same readable columns the
    regular Search tab CSV already has), then attaches a crustdata_raw_json
    column with the complete raw Crustdata response for each person verbatim.

    No extra API calls needed. A regular filter-search profile IS the full
    raw Crustdata response already (compact=false) — nothing to unwrap. A
    description (semantic) search result is a lossy shim, so its original
    nested response is kept separately under _raw_semantic_result (see
    semantic_profile_to_legacy_shape in crustdata_search.py). Either way the
    data is already sitting in memory from the search that was just run —
    this just surfaces it.
    """
    df = normalize_search_results_to_df(raw_profiles)
    if df.empty:
        return df

    raw_json_by_url = {}
    for p in (raw_profiles or []):
        raw = p.get('_raw_search_result') or p.get('_raw_semantic_result') or p
        url = (
            p.get('flagship_profile_url') or p.get('linkedin_flagship_url')
            or p.get('linkedin_profile_url') or p.get('linkedin_url')
        )
        url = normalize_linkedin_url(url) if url else None
        if url:
            raw_json_by_url[url] = json.dumps(raw, ensure_ascii=False, default=str)

    if 'linkedin_url' in df.columns:
        df['crustdata_raw_json'] = df['linkedin_url'].map(raw_json_by_url).fillna('')

    return prepare_df_for_export(df)


# ===== Session Persistence =====
# On Streamlit Cloud, use /tmp which is writable (but ephemeral across reboots)
# Locally, use .sessions in app directory
_IS_STREAMLIT_CLOUD = os.environ.get('STREAMLIT_SHARING_MODE') or '/mount/src' in str(Path(__file__))
if _IS_STREAMLIT_CLOUD:
    SESSION_DIR = Path('/tmp/.streamlit_sessions')
else:
    SESSION_DIR = Path(__file__).parent / '.sessions'
SESSION_DIR.mkdir(exist_ok=True)
# Legacy shared file (for migration)
_LEGACY_SESSION_FILE = Path(__file__).parent / '.last_session.json'

# ===== Startup Memory Optimization =====
# On Streamlit Cloud, proactively clean up memory on each rerun
if _IS_STREAMLIT_CLOUD:
    import gc
    gc.collect()
    # Log memory on every rerun for crash diagnosis
    try:
        _startup_total = 0
        _startup_keys = []
        for _k in st.session_state:
            _v = st.session_state[_k]
            if isinstance(_v, pd.DataFrame):
                _s = int(_v.memory_usage(deep=True).sum())
            elif isinstance(_v, (dict, list)) and _v:
                _s = len(json.dumps(_v, default=str).encode('utf-8'))
            else:
                _s = sys.getsizeof(_v)
            _startup_total += _s
            if _s > 10240:  # > 10KB
                _startup_keys.append((_k, _s))
        _startup_keys.sort(key=lambda x: x[1], reverse=True)
        print(f"[Memory] RERUN | Total session: {_startup_total / 1024 / 1024:.1f}MB | Keys>10KB: {len(_startup_keys)}")
        for _k, _s in _startup_keys[:5]:
            print(f"[Memory]   {_k}: {_s / 1024 / 1024:.2f}MB")
        # Also log process memory
        try:
            import psutil
            _rss = psutil.Process().memory_info().rss
            print(f"[Memory] Process RSS: {_rss / 1024 / 1024:.0f}MB")
        except ImportError:
            pass
    except Exception as _e:
        print(f"[Memory] Startup log failed: {_e}")
    # Clear any stale batch state that might have survived a reboot
    if 'screening_batch_state' in st.session_state and not st.session_state.get('screening_batch_mode'):
        del st.session_state['screening_batch_state']
        gc.collect()


def cleanup_memory():
    """Aggressive memory cleanup - call after major operations."""
    import gc

    # Remove heavy columns from DataFrames
    heavy_cols = ['raw_crustdata', 'raw_data', 'raw_phantombuster', 'education_details', 'certifications', 'past_positions_raw']
    for df_key in ['results_df', 'enriched_df', 'passed_candidates_df']:
        if df_key in st.session_state and isinstance(st.session_state[df_key], pd.DataFrame):
            df = st.session_state[df_key]
            cols_to_drop = [c for c in heavy_cols if c in df.columns]
            if cols_to_drop:
                st.session_state[df_key] = df.drop(columns=cols_to_drop)

    # MEMORY OPTIMIZATION: Clear list versions - they're duplicates of DataFrames
    # UI checks now use results_df directly
    for list_key in ['results', 'enriched_results']:
        if list_key in st.session_state:
            del st.session_state[list_key]

    # NOTE: Keep passed_candidates_df - needed for filtered candidates preview
    # It's the same as results_df but we need it for the UI flow

    # Clear filtered_out if it exists (legacy)
    for key in ['filtered_out']:
        if key in st.session_state:
            del st.session_state[key]

    # Clear debug data
    for key in ['_enrich_debug', '_enrich_match_debug', '_debug_url_cols', '_debug_all_cols', '_debug_valid_urls']:
        if key in st.session_state:
            del st.session_state[key]

    # Clear screening batch state if not actively screening
    if 'screening_batch_state' in st.session_state and not st.session_state.get('screening_batch_mode'):
        del st.session_state['screening_batch_state']

    # Clear orphaned raw data caches (enriched_profiles_raw can accumulate)
    st.session_state.pop('enriched_profiles_raw', None)

    gc.collect()


# Session-state keys derived from the loaded results_df. When the user replaces
# or appends the loaded set, these must be cleared so downstream tabs (Filter+,
# Enrich, AI Screen) don't feed stale data forward — e.g. the Enrich tab prefers
# passed_candidates_df when present, so leaving it would silently ignore a fresh
# upload.
_RESULTS_DERIVED_KEYS = (
    'passed_candidates_df',
    'enriched_df',
    'enriched_profiles_raw',
    'filter_stats',
    'filtered_out',
    'filtered_out_counts',
    'filtered_out_light',
    'screening_results',
    'enriched_results',
    'results',
)


def clear_results_derived_state(state) -> None:
    """Drop every session-state value derived from the previously loaded results_df.

    Used when a CSV/JSON upload replaces or appends to the loaded set. Operates
    on any mapping with .pop(key, None) so it's unit-testable without Streamlit.
    """
    for key in _RESULTS_DERIVED_KEYS:
        state.pop(key, None)


def get_profiles_df() -> pd.DataFrame:
    """Get the current profiles DataFrame. Single source of truth."""
    # Priority: enriched_df > results_df > empty
    if 'enriched_df' in st.session_state and st.session_state['enriched_df'] is not None:
        df = st.session_state['enriched_df']
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df
    if 'results_df' in st.session_state and st.session_state['results_df'] is not None:
        df = st.session_state['results_df']
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df
    return pd.DataFrame()


def get_profiles_list() -> list:
    """Get profiles as list. Derived from DataFrame - NOT stored separately."""
    df = get_profiles_df()
    if df.empty:
        return []
    return df.to_dict('records')


def get_profile_count() -> int:
    """Get current LOADED profile count (filtered if applied, otherwise original)."""
    # Use passed_candidates_df (filtered) if available, otherwise results_df (original)
    passed_df = st.session_state.get('passed_candidates_df')
    if passed_df is not None and isinstance(passed_df, pd.DataFrame) and not passed_df.empty:
        return len(passed_df)
    if 'results_df' in st.session_state and st.session_state['results_df'] is not None:
        df = st.session_state['results_df']
        if isinstance(df, pd.DataFrame) and not df.empty:
            return len(df)
    return 0


def count_passed_email_split(df) -> tuple:
    """Return (have_email, need_email) counts for the passed-only enrichment UI.

    'Has email' = either the `email` column or the `salesql_email` column is
    populated (non-null, non-empty). Mirrors the skip logic in
    `enrich_profiles_with_salesql` (dashboard.py:1763-1768) so the count shown
    next to the SalesQL passed-only button never drifts from the count of
    profiles that function actually processes.

    Returns (0, 0) on empty/None input.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return (0, 0)
    mask = pd.Series([False] * len(df), index=df.index)
    if 'salesql_email' in df.columns:
        mask |= (df['salesql_email'].notna() & (df['salesql_email'] != ''))
    if 'email' in df.columns:
        mask |= (df['email'].notna() & (df['email'] != ''))
    have = int(mask.sum())
    return (have, len(df) - have)


def _get_session_file():
    """Get per-user session file path."""
    username = st.session_state.get('username', 'default')
    safe_name = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in username)
    return SESSION_DIR / f'.session_{safe_name}.json'


def _save_session_to_db(session_data: dict, username: str) -> bool:
    """Save session to Supabase for persistence across reboots."""
    if not HAS_DATABASE:
        return False
    try:
        from db import get_supabase_client
        client = get_supabase_client()
        if not client:
            return False

        # Compress session data to reduce storage size
        import gzip
        import base64
        json_str = json.dumps(session_data)
        compressed = gzip.compress(json_str.encode('utf-8'))
        encoded = base64.b64encode(compressed).decode('ascii')

        # Upsert to sessions table
        from datetime import datetime
        client.upsert('sessions', {
            'username': username,
            'session_data': encoded,
            'updated_at': datetime.utcnow().isoformat()
        }, on_conflict='username')
        return True
    except Exception as e:
        print(f"[Session] DB save failed: {e}")
        return False


def _load_session_from_db(username: str):
    """Load session from Supabase."""
    if not HAS_DATABASE:
        return None
    try:
        from db import get_supabase_client
        client = get_supabase_client()
        if not client:
            return None

        result = client.select('sessions', 'session_data', {'username': f'eq.{username}'}, limit=1)
        if result and result[0].get('session_data'):
            import gzip
            import base64
            encoded = result[0]['session_data']
            compressed = base64.b64decode(encoded)
            json_str = gzip.decompress(compressed).decode('utf-8')
            return json.loads(json_str)
    except Exception as e:
        print(f"[Session] DB load failed: {e}")
    return None

def _clean_for_json(obj):
    """Recursively clean data for JSON serialization (handle NaN, numpy types, etc.)."""
    import math
    import numpy as np
    if isinstance(obj, dict):
        return {str(k): _clean_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_clean_for_json(v) for v in obj]
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return _clean_for_json(obj.tolist())
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif pd.isna(obj):
        return None
    return obj

def _get_session_key():
    """Get the session storage key based on current user."""
    username = st.session_state.get('username', 'default')
    return f"session_{username}"


def _build_session_data():
    """Build session data dict from current session state."""
    session_data = {}
    # MEMORY OPTIMIZATION: Only save essential keys, avoid redundant copies
    # - Don't save 'results' (list) when we have 'results_df' (DataFrame) - they're the same data
    # - Don't save 'enriched_results' when we have 'enriched_df'
    keys_to_save = [
        'results_df', 'enriched_df', 'original_results_df', 'passed_candidates_df',  # Save filtered data too
        'screening_results',  # Essential - screening is expensive
        'filter_stats', 'last_load_count', 'last_load_file',
        'user_sheet_url',
        'active_screening_prompt', 'active_screening_role',
        'jd_screening'
    ]
    serialized_dfs = []  # [(key, DataFrame)] already serialized this pass
    for key in keys_to_save:
        if key in st.session_state and st.session_state[key] is not None:
            value = st.session_state[key]
            # Convert DataFrames to dict for JSON serialization
            if isinstance(value, pd.DataFrame):
                # Dedup: right after a load, results_df / original_results_df /
                # enriched_df often hold identical data. Store a reference
                # instead of serializing (and writing) the same rows twice.
                ref_key = None
                for prev_key, prev_df in serialized_dfs:
                    if value is prev_df or prev_df.equals(value):
                        ref_key = prev_key
                        break
                if ref_key is not None:
                    session_data[key] = {'_type': 'ref', 'ref': ref_key}
                else:
                    # Fast C-level serialization — handles NaN/inf/numpy types
                    # natively, avoiding the slow pure-Python per-cell walk.
                    data = json.loads(value.to_json(orient='records', date_format='iso'))
                    session_data[key] = {'_type': 'dataframe', 'data': data}
                serialized_dfs.append((key, value))
            elif isinstance(value, list):
                session_data[key] = {'_type': 'list', 'data': _clean_for_json(value)}
            elif isinstance(value, dict):
                session_data[key] = {'_type': 'dict', 'data': _clean_for_json(value)}
            else:
                session_data[key] = {'_type': 'value', 'data': _clean_for_json(value)}
    return session_data


def _restore_session_data(session_data: dict):
    """Restore session state from session data dict."""
    refs = {}
    for key, item in session_data.items():
        item_type = item['_type']
        if item_type == 'dataframe':
            st.session_state[key] = pd.DataFrame(item['data'])
        elif item_type == 'ref':
            # Resolved in a second pass once the target DataFrame is restored
            refs[key] = item['ref']
        elif item_type == 'list':
            st.session_state[key] = item['data']
        elif item_type == 'dict':
            st.session_state[key] = item['data']
        else:
            st.session_state[key] = item['data']

    # Second pass: resolve DataFrame references (dedup'd in _build_session_data)
    for key, ref_key in refs.items():
        src = st.session_state.get(ref_key)
        if isinstance(src, pd.DataFrame):
            st.session_state[key] = src.copy()

    # MEMORY OPTIMIZATION: Don't reconstruct list versions - they duplicate DataFrames
    # Code should use results_df / enriched_df directly


def _log_session_memory(context: str = ""):
    """Log session state memory breakdown to console (for crash diagnosis)."""
    try:
        sizes = {}
        total = 0
        for key in st.session_state:
            value = st.session_state[key]
            if isinstance(value, pd.DataFrame):
                size = int(value.memory_usage(deep=True).sum())
                heavy = [c for c in ['raw_crustdata', 'raw_data', 'raw_phantombuster'] if c in value.columns]
                label = f"DF({len(value)}r×{len(value.columns)}c)"
                if heavy:
                    label += f" HEAVY:{','.join(heavy)}"
            elif isinstance(value, (dict, list)):
                size = len(json.dumps(value, default=str).encode('utf-8')) if value else 0
                label = f"{type(value).__name__}({len(value)})"
            else:
                size = sys.getsizeof(value)
                label = type(value).__name__
            sizes[key] = (size, label)
            total += size
        # Print top 10 by size
        top = sorted(sizes.items(), key=lambda x: x[1][0], reverse=True)[:10]
        print(f"[Memory] {context} | Total session: {total / 1024 / 1024:.1f}MB")
        for key, (size, label) in top:
            if size > 1024:
                print(f"[Memory]   {key}: {size / 1024 / 1024:.2f}MB ({label})")
    except Exception as e:
        print(f"[Memory] Log failed: {e}")


def save_session_state(to_db: bool = False):
    """Save current session state to local file and optionally to Supabase.

    Args:
        to_db: If True, also save to Supabase for persistence across reboots.
               Default False to avoid excessive IO on every auto-save.
    """
    try:
        # Log memory state before save (for crash diagnosis). Throttled to at
        # most once per 30s — the memory audit re-serializes the whole session,
        # so running it on every save makes uploads noticeably slower on cloud.
        # The timestamp lives in session_state so the throttle survives
        # Streamlit reruns (a module global would reset on each re-execution).
        if _IS_STREAMLIT_CLOUD:
            now = time.time()
            if now - st.session_state.get('_last_memory_log_ts', 0.0) > 30:
                st.session_state['_last_memory_log_ts'] = now
                import traceback
                caller = traceback.extract_stack(limit=3)[0]
                _log_session_memory(f"save from {caller.filename.split('/')[-1]}:{caller.lineno}")

        session_data = _build_session_data()
        if not session_data:
            return False

        username = st.session_state.get('username', 'default')

        # Always save to local file
        session_file = _get_session_file()
        with open(session_file, 'w') as f:
            json.dump(session_data, f)

        # Optionally save to Supabase for persistence across reboots
        if to_db or _IS_STREAMLIT_CLOUD:
            _save_session_to_db(session_data, username)

        return True
    except Exception as e:
        print(f"[Session] Save failed: {e}")
    return False


def load_session_state():
    """Load session state from local file or Supabase."""
    username = st.session_state.get('username', 'default')

    try:
        # Try local file first (faster)
        session_file = _get_session_file()
        if session_file.exists():
            with open(session_file, 'r') as f:
                session_data = json.load(f)
            _restore_session_data(session_data)
            return True, None

        # Try legacy shared file
        if _LEGACY_SESSION_FILE.exists():
            with open(_LEGACY_SESSION_FILE, 'r') as f:
                session_data = json.load(f)
            _restore_session_data(session_data)
            _LEGACY_SESSION_FILE.unlink(missing_ok=True)
            return True, None

        # Try Supabase (for cloud persistence across reboots)
        session_data = _load_session_from_db(username)
        if session_data:
            _restore_session_data(session_data)
            # Also save locally for faster access next time
            with open(session_file, 'w') as f:
                json.dump(session_data, f)
            return True, "Restored from database"

        # No session found anywhere
        return False, f"No session found for user '{username}'"
    except json.JSONDecodeError as e:
        return False, f"Corrupt session file: {e}"
    except Exception as e:
        return False, f"Load error: {e}"


def clear_session_file():
    """Delete session data from local file."""
    cleared = False

    # Clear per-user local file
    try:
        session_file = _get_session_file()
        if session_file.exists():
            session_file.unlink()
            cleared = True
        # Also clean up legacy shared file if it exists
        if _LEGACY_SESSION_FILE.exists():
            _LEGACY_SESSION_FILE.unlink()
            cleared = True
    except Exception:
        pass

    return cleared


# Sidebar - Session Controls (placed after save/clear functions are defined)
with st.sidebar:
    st.markdown("### Session")
    col_save, col_clear = st.columns(2)
    with col_save:
        if st.button("Save", key="sidebar_save_session", help="Save session to DB for persistence"):
            if save_session_state(to_db=True):
                st.success("Saved to DB!")
            else:
                st.error("Failed to save")
    with col_clear:
        if st.button("Clear", key="sidebar_clear_session", help="Clear saved session"):
            clear_session_file()
            # Clear all data-heavy session state keys
            keys_to_clear = [
                'results', 'results_df', 'enriched_results', 'enriched_df', 'screening_results',
                'passed_candidates_df', 'filter_stats', 'original_results_df',
                'active_screening_prompt', 'active_screening_role', 'jd_screening',
                'filtered_out', 'screening_batch_state', 'screening_batch_progress',
                '_enrich_debug', '_enrich_match_debug', '_debug_url_cols', '_debug_all_cols'
            ]
            for key in keys_to_clear:
                if key in st.session_state:
                    del st.session_state[key]
            import gc
            gc.collect()
            st.success("Cleared!")
            st.rerun()
    # ===== Memory Diagnostics (collapsible) =====
    with st.expander("Memory Diagnostics", expanded=False):
        if st.button("Measure Memory", key="btn_measure_memory"):
            import gc
            total_bytes = 0
            rows = []
            for key in sorted(st.session_state.keys()):
                value = st.session_state[key]
                if isinstance(value, pd.DataFrame):
                    size = int(value.memory_usage(deep=True).sum())
                    row_count = len(value)
                    cols = len(value.columns)
                    heavy = [c for c in ['raw_crustdata', 'raw_data', 'raw_phantombuster'] if c in value.columns]
                    detail = f"DataFrame {row_count}r×{cols}c"
                    if heavy:
                        detail += f" [HEAVY: {', '.join(heavy)}]"
                elif isinstance(value, dict):
                    try:
                        size = len(json.dumps(value, default=str).encode('utf-8'))
                    except Exception:
                        size = sys.getsizeof(value)
                    detail = f"dict ({len(value)} keys)"
                elif isinstance(value, list):
                    try:
                        size = len(json.dumps(value, default=str).encode('utf-8'))
                    except Exception:
                        size = sum(sys.getsizeof(item) for item in value[:100])
                    detail = f"list ({len(value)} items)"
                elif isinstance(value, str):
                    size = len(value.encode('utf-8'))
                    detail = f"str ({size} bytes)"
                else:
                    size = sys.getsizeof(value)
                    detail = type(value).__name__
                total_bytes += size
                if size > 1024:  # Only show keys > 1KB
                    rows.append({
                        'Key': key,
                        'Size': f"{size / 1024 / 1024:.2f} MB" if size > 1024*1024 else f"{size / 1024:.1f} KB",
                        'Size_bytes': size,
                        'Detail': detail
                    })
            # Sort by size descending
            rows.sort(key=lambda r: r['Size_bytes'], reverse=True)
            st.metric("Total Session State", f"{total_bytes / 1024 / 1024:.1f} MB")
            if total_bytes > 100 * 1024 * 1024:
                st.error("Session state > 100MB — crash risk!")
            elif total_bytes > 50 * 1024 * 1024:
                st.warning("Session state > 50MB — getting heavy")
            if rows:
                st.dataframe(
                    pd.DataFrame(rows)[['Key', 'Size', 'Detail']],
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.info("Session state is lightweight (all keys < 1KB)")
            # Process memory (if psutil available)
            try:
                import psutil
                process = psutil.Process()
                mem = process.memory_info()
                st.metric("Process RSS", f"{mem.rss / 1024 / 1024:.0f} MB")
                if mem.rss > 800 * 1024 * 1024:
                    st.error("Process using > 800MB — near 1GB crash limit!")
            except ImportError:
                st.caption("Install psutil for process memory: pip install psutil")
    st.divider()


@st.cache_resource(ttl=300)
def _get_db_client():
    """Cached Supabase client (reused across reruns)."""
    if not HAS_DATABASE:
        return None
    try:
        client = get_supabase_client()
        if client and check_connection(client):
            return client
    except Exception:
        pass
    return None


def get_usage_tracker():
    """Get a UsageTracker instance with database connection."""
    if not HAS_USAGE_TRACKER:
        return None
    client = _get_db_client()
    if client:
        return UsageTracker(client)
    return None


@st.cache_data(ttl=120, max_entries=3, show_spinner="Loading profiles...")
def _get_cached_all_profiles(_db_client_id: str, limit: int = 500) -> list:
    """Cached wrapper for get_all_profiles to reduce database queries.

    Cached for 2 minutes to balance freshness with performance.
    The _db_client_id is used as a cache key differentiator (underscore prefix makes it unhashable-safe).
    """
    db_client = _get_db_client()
    if not db_client:
        return []
    return get_all_profiles(db_client, limit=limit)


# ===== SalesQL Email Enrichment =====
# Rate limiting: 180 requests/minute, 5000/day
SALESQL_REQUESTS_PER_MINUTE = 180
SALESQL_DAILY_LIMIT = 5000
SALESQL_DELAY_BETWEEN_REQUESTS = 0.35  # ~170 requests/minute to stay safe


@st.cache_resource
def _get_screening_counter():
    """Shared counter of active screening sessions across all users.
    Used to dynamically scale OpenAI concurrent workers."""
    return {'lock': threading.Lock(), 'active': 0}


def _screening_session_start():
    """Increment active screening sessions counter. Returns recommended max_workers."""
    counter = _get_screening_counter()
    with counter['lock']:
        counter['active'] += 1
        # Scale workers: 15 for 1 user, 7 for 2, 5 for 3, etc. Min 3.
        # Capped at 15 to prevent thread explosion and memory fragmentation.
        workers = max(3, 15 // counter['active'])
        return workers


def _screening_session_end():
    """Decrement active screening sessions counter."""
    counter = _get_screening_counter()
    with counter['lock']:
        counter['active'] = max(0, counter['active'] - 1)


@st.cache_resource
def _get_global_rate_limiter():
    """Shared rate limiter across all user sessions.
    Returns a dict with a lock and timestamp list for token-bucket limiting."""
    return {
        'lock': threading.Lock(),
        'timestamps': [],  # Recent request timestamps
        'max_per_minute': 140,  # Global cap (below 180 hard limit)
    }


def _global_rate_limit_wait():
    """Wait if needed to stay under the global SalesQL rate limit.
    Call this BEFORE each SalesQL API request."""
    limiter = _get_global_rate_limiter()
    with limiter['lock']:
        now = time.time()
        cutoff = now - 60.0
        # Prune old timestamps
        limiter['timestamps'] = [t for t in limiter['timestamps'] if t > cutoff]
        if len(limiter['timestamps']) >= limiter['max_per_minute']:
            # Wait until the oldest request in the window expires
            wait_time = limiter['timestamps'][0] - cutoff + 0.1
            if wait_time > 0:
                time.sleep(wait_time)
            # Re-prune after waiting
            now = time.time()
            cutoff = now - 60.0
            limiter['timestamps'] = [t for t in limiter['timestamps'] if t > cutoff]
        limiter['timestamps'].append(time.time())

def enrich_with_salesql(linkedin_url: str, api_key: str, personal_only: bool = True, tracker: 'UsageTracker' = None) -> dict:
    """Enrich a single profile with SalesQL to get email.

    Args:
        linkedin_url: LinkedIn profile URL
        api_key: SalesQL API key
        personal_only: If True, only return results with personal/direct emails
        tracker: Optional UsageTracker for logging API usage

    Returns dict with 'emails' list and 'error' if any.
    """
    start_time = time.time()
    try:
        params = {'linkedin_url': linkedin_url}
        if personal_only:
            params['match_if_direct_email'] = 'true'

        response = requests.get(
            'https://api-public.salesql.com/v1/persons/enrich/',
            params=params,
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=30
        )
        elapsed_ms = int((time.time() - start_time) * 1000)

        if response.status_code == 200:
            data = response.json()
            # Filter to only Direct (personal) emails
            emails = data.get('emails', [])
            if personal_only:
                emails = [e for e in emails if e.get('type') == 'Direct']

            # Log successful usage
            if tracker:
                tracker.log_salesql(
                    lookups=1,
                    emails_found=len(emails),
                    status='success',
                    response_time_ms=elapsed_ms
                )

            return {
                'emails': emails,
                'first_name': data.get('first_name'),
                'last_name': data.get('last_name'),
                'title': data.get('title'),
                'organization': data.get('organization', {}).get('name'),
            }
        elif response.status_code == 404:
            if tracker:
                tracker.log_salesql(lookups=1, status='success', response_time_ms=elapsed_ms)
            return {'emails': [], 'error': 'Profile not found'}
        elif response.status_code == 429:
            if tracker:
                tracker.log_salesql(lookups=0, status='error', error_message='Rate limit exceeded', response_time_ms=elapsed_ms)
            return {'emails': [], 'error': 'Rate limit exceeded'}
        else:
            if tracker:
                tracker.log_salesql(lookups=1, status='error', error_message=f'API error {response.status_code}', response_time_ms=elapsed_ms)
            return {'emails': [], 'error': f'API error {response.status_code}'}
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        if tracker:
            tracker.log_salesql(lookups=0, status='error', error_message=str(e)[:200], response_time_ms=elapsed_ms)
        return {'emails': [], 'error': str(e)}


def enrich_profiles_with_salesql(profiles_df: pd.DataFrame, api_key: str, progress_callback=None, personal_only: bool = True, limit: int = None, max_workers: int = 10) -> pd.DataFrame:
    """Enrich multiple profiles with SalesQL emails in parallel.

    Args:
        profiles_df: DataFrame with linkedin_url column
        api_key: SalesQL API key
        progress_callback: Optional callback(current, total) for progress updates
        personal_only: If True, only get personal/direct emails (default True)
        limit: Maximum number of profiles to enrich (None = all)
        max_workers: Number of parallel requests (default 10)

    Returns DataFrame with added email columns.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    df = profiles_df.copy()

    # Get usage tracker for logging
    tracker = get_usage_tracker()

    # Find LinkedIn URL column
    url_col = None
    for col in ['linkedin_url', 'public_url', 'defaultProfileUrl', 'LinkedIn URL']:
        if col in df.columns:
            url_col = col
            break

    if not url_col:
        return df

    # Add email columns if not exist
    if 'salesql_email' not in df.columns:
        df['salesql_email'] = ''
    if 'salesql_email_type' not in df.columns:
        df['salesql_email_type'] = ''

    # Count profiles that need enrichment (skip those with any email)
    needs_enrichment = []
    for idx, row in df.iterrows():
        linkedin_url = row.get(url_col)
        if not linkedin_url or pd.isna(linkedin_url):
            continue
        # Skip if has SalesQL email
        if row.get('salesql_email') and not pd.isna(row.get('salesql_email')) and row.get('salesql_email') != '':
            continue
        # Skip if has DB email
        if row.get('email') and not pd.isna(row.get('email')) and row.get('email') != '':
            continue
        needs_enrichment.append((idx, linkedin_url))

    # Apply limit
    if limit and limit < len(needs_enrichment):
        needs_enrichment = needs_enrichment[:limit]

    total = len(needs_enrichment)
    if total == 0:
        return df

    # Thread-safe counter and results storage
    processed_count = [0]
    results_lock = threading.Lock()
    results_map = {}  # idx -> (email, email_type)

    def enrich_single(idx_url_tuple):
        """Enrich a single profile with rate limiting."""
        idx, linkedin_url = idx_url_tuple

        # Global rate limit — shared across all users to stay under 180 req/min
        _global_rate_limit_wait()

        result = enrich_with_salesql(linkedin_url, api_key, personal_only=personal_only, tracker=tracker)

        best_email = None
        best_type = None
        if result.get('emails'):
            for email_obj in result['emails']:
                email = email_obj.get('email', '')
                email_type = email_obj.get('type', '')
                if email_type == 'Direct':
                    best_email = email
                    best_type = 'Direct'
                    break

        with results_lock:
            processed_count[0] += 1
            if best_email:
                results_map[idx] = (best_email, best_type)

        return idx, best_email

    # Run parallel requests
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(enrich_single, item): item for item in needs_enrichment}

        for future in as_completed(futures):
            try:
                future.result()  # Get result to catch any exceptions
            except Exception:
                pass  # Errors already handled in enrich_single

            # Update progress (approximate - may not be perfectly synchronized)
            if progress_callback:
                with results_lock:
                    progress_callback(processed_count[0], total)

    # Apply results to DataFrame
    for idx, (email, email_type) in results_map.items():
        df.at[idx, 'salesql_email'] = email
        df.at[idx, 'salesql_email_type'] = email_type

    return df


# ===== Search History Functions =====
def get_search_history_path() -> Path:
    """Get path to search history file."""
    return Path(__file__).parent / 'search_history.json'


def load_search_history(agent_id: str = None) -> list[dict]:
    """Load search history from database (if available) or local file.

    Args:
        agent_id: If provided, filter history to this agent only

    Returns list of dicts with keys: agent_id, csv_name, search_url, launched_at, profiles_requested
    """
    # Try database first (for cloud deployment)
    if HAS_DATABASE:
        try:
            db_client = _get_db_client()
            if db_client:
                db_history = get_search_history(db_client, agent_id)
                if db_history:
                    return db_history
        except Exception:
            pass

    # Fall back to local file
    history_path = get_search_history_path()
    if not history_path.exists():
        return []

    try:
        with open(history_path, 'r') as f:
            history = json.load(f)

        # Filter by agent_id if provided (convert to string for comparison)
        if agent_id:
            agent_id_str = str(agent_id)
            history = [h for h in history if str(h.get('agent_id', '')) == agent_id_str]

        # Sort by launched_at descending (most recent first)
        history.sort(key=lambda x: x.get('launched_at', ''), reverse=True)
        return history
    except Exception:
        return []


def save_search_to_history(agent_id: str, csv_name: str, search_url: str = None, profiles_requested: int = None, search_name: str = None) -> bool:
    """Save a search to history (database if available, also local file).

    Returns True if saved successfully.
    """
    saved = False

    # Save to database if available (for cloud persistence)
    if HAS_DATABASE:
        try:
            db_client = _get_db_client()
            if db_client:
                saved = save_search_history_entry(db_client, agent_id, csv_name, search_url, profiles_requested, search_name)
        except Exception:
            pass

    # Also save to local file (for local development)
    history_path = get_search_history_path()
    try:
        # Load existing history
        if history_path.exists():
            with open(history_path, 'r') as f:
                history = json.load(f)
        else:
            history = []

        # Add new entry (ensure agent_id is stored as string)
        entry = {
            'agent_id': str(agent_id),
            'csv_name': csv_name,
            'launched_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'search_url': search_url,
            'profiles_requested': profiles_requested,
            'search_name': search_name,
        }
        history.append(entry)

        # Save
        with open(history_path, 'w') as f:
            json.dump(history, f, indent=2)

        saved = True
    except Exception:
        pass

    return saved


def delete_search_from_history(agent_id: str, csv_name: str, api_key: str = None, delete_file: bool = False) -> bool:
    """Delete a search from history (database and local) and optionally delete the file from PhantomBuster.

    Returns True if deleted successfully.
    """
    deleted = False
    agent_id_str = str(agent_id)

    # Delete from database if available
    if HAS_DATABASE:
        try:
            db_client = _get_db_client()
            if db_client:
                deleted = delete_search_history_entry(db_client, agent_id, csv_name)
        except Exception:
            pass

    # Also delete from local file
    history_path = get_search_history_path()
    try:
        if history_path.exists():
            with open(history_path, 'r') as f:
                history = json.load(f)

            # Find and remove entry (convert to string for comparison)
            original_len = len(history)
            history = [h for h in history if not (str(h.get('agent_id', '')) == agent_id_str and h.get('csv_name') == csv_name)]

            if len(history) < original_len:
                with open(history_path, 'w') as f:
                    json.dump(history, f, indent=2)
                deleted = True
    except Exception:
        pass

    # Delete file from PhantomBuster if requested
    if delete_file and api_key:
        delete_phantombuster_file(api_key, agent_id, f'{csv_name}.csv')
        delete_phantombuster_file(api_key, agent_id, f'{csv_name}.json')

    return deleted


def get_file_size_from_phantombuster(api_key: str, agent_id: str, csv_name: str) -> int:
    """Get file size from PhantomBuster cache. Returns size in bytes or 0 if not found."""
    try:
        # Get agent info for S3 folders
        agent_response = requests.get(
            'https://api.phantombuster.com/api/v2/agents/fetch',
            params={'id': agent_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=10
        )

        if agent_response.status_code != 200:
            return 0

        agent_data = agent_response.json()
        s3_folder = agent_data.get('s3Folder')
        org_s3_folder = agent_data.get('orgS3Folder')

        if not s3_folder or not org_s3_folder:
            return 0

        # Check cache URL
        cache_url = f'https://cache1.phantombooster.com/{org_s3_folder}/{s3_folder}/{csv_name}.csv'
        resp = requests.head(cache_url, timeout=5)

        if resp.status_code == 200:
            return int(resp.headers.get('content-length', 0))
        return 0
    except Exception:
        return 0


@st.cache_data(ttl=300, max_entries=3)  # Cache for 5 minutes, limit size
def fetch_phantombuster_agents(api_key: str) -> dict:
    """Fetch list of all PhantomBuster agents.

    Returns dict with 'agents' list and optional 'error' message.
    """
    try:
        response = requests.get(
            'https://api.phantombuster.com/api/v2/agents/fetch-all',
            headers={'X-Phantombuster-Key': api_key},
            timeout=30
        )
        if response.status_code == 200:
            agents = response.json()
            # Return list of {id, name} for dropdown
            return {'agents': [{'id': a['id'], 'name': a['name']} for a in agents]}
        return {'agents': [], 'error': f"API returned status {response.status_code}"}
    except Exception as e:
        return {'agents': [], 'error': str(e)}


def fetch_phantombuster_results(api_key: str, agent_id: str) -> list[dict]:
    """Fetch results from PhantomBuster agent."""
    try:
        # Get agent output
        response = requests.get(
            f'https://api.phantombuster.com/api/v2/agents/fetch-output',
            params={'id': agent_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=60
        )

        if response.status_code == 200:
            data = response.json()
            # PhantomBuster returns results in 'output' or as direct JSON
            if 'output' in data:
                # Parse the output if it's a JSON string
                output = data['output']
                if isinstance(output, str):
                    try:
                        return json.loads(output)
                    except:
                        return []
                return output if isinstance(output, list) else []
            return []
        else:
            return []
    except Exception as e:
        st.error(f"PhantomBuster API error: {e}")
        return []


def fetch_phantombuster_result_csv(api_key: str, agent_id: str, debug: bool = False, filename: str = None) -> pd.DataFrame:
    """Fetch results from PhantomBuster agent. Tries multiple methods:
    1. Authenticated API to get output files
    2. Result object from container

    Args:
        filename: Optional specific filename to fetch (without .csv extension)
    """
    from io import StringIO

    try:
        # First, try to get the agent's info
        agent_response = requests.get(
            'https://api.phantombuster.com/api/v2/agents/fetch',
            params={'id': agent_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=30
        )

        agent_name = 'Unknown'
        s3_folder = None
        org_s3_folder = None

        if agent_response.status_code == 200:
            agent_data = agent_response.json()
            s3_folder = agent_data.get('s3Folder')
            org_s3_folder = agent_data.get('orgS3Folder')
            agent_name = agent_data.get('name', 'Unknown')

            if debug:
                st.info(f"Agent: {agent_name}")
                st.info(f"S3 Folder: {s3_folder}")
                if filename:
                    st.info(f"Looking for file: {filename}.csv")
                with st.expander("Agent API Response"):
                    st.json(agent_data)

        # Method 0: Try direct PhantomBuster cache URL (most reliable for files)
        if s3_folder and org_s3_folder:
            # If specific filename provided, try it and common PB variants (e.g. database- prefix)
            files_to_try = []
            if filename:
                files_to_try = [
                    f'{filename}.csv', f'{filename}.json',
                    f'database-{filename}.csv', f'database-{filename}.json',
                    'result.csv', 'result.json',
                ]
            else:
                files_to_try = ['result.csv', 'result.json']

            for fname in files_to_try:
                try:
                    cache_url = f'https://cache1.phantombooster.com/{org_s3_folder}/{s3_folder}/{fname}'
                    if debug:
                        st.info(f"Trying cache URL: {cache_url}")
                    cache_response = requests.get(cache_url, timeout=60)
                    if cache_response.status_code == 200:
                        if fname.endswith('.csv'):
                            df = pd.read_csv(StringIO(cache_response.text))
                            if not df.empty:
                                if debug:
                                    st.success(f"Loaded from cache URL: {fname}")
                                return df
                        elif fname.endswith('.json'):
                            profiles = cache_response.json()
                            if isinstance(profiles, list) and profiles:
                                if debug:
                                    st.success(f"Loaded from cache URL: {fname}")
                                return pd.DataFrame(profiles)
                except Exception as e:
                    if debug:
                        st.warning(f"Cache URL error: {e}")

        # Method 1: Try fetch-output endpoint (gets last run output/logs)
        try:
            output_response = requests.get(
                'https://api.phantombuster.com/api/v2/agents/fetch-output',
                params={'id': agent_id},
                headers={'X-Phantombuster-Key': api_key},
                timeout=60
            )

            if debug:
                st.info(f"Fetch-output API: {output_response.status_code}")

            if output_response.status_code == 200:
                output_data = output_response.json()
                if debug:
                    with st.expander("Fetch-output Response"):
                        # Truncate output for display
                        display_data = output_data.copy()
                        if 'output' in display_data and len(str(display_data['output'])) > 2000:
                            display_data['output'] = str(display_data['output'])[:2000] + '... (truncated)'
                        st.json(display_data)

                # Check for resultObject in output
                if output_data.get('resultObject'):
                    result_obj = output_data['resultObject']
                    if isinstance(result_obj, str):
                        profiles = json.loads(result_obj)
                    else:
                        profiles = result_obj
                    if isinstance(profiles, list) and profiles:
                        st.success(f"Loaded {len(profiles)} profiles from fetch-output")
                        return pd.DataFrame(profiles)
        except Exception as e:
            if debug:
                st.warning(f"Fetch-output error: {e}")

        # Method 2: Try the store/fetch API endpoint (authenticated file access)
        # First, list all files in the agent's storage
        try:
            files_response = requests.get(
                'https://api.phantombuster.com/api/v2/agents/fetch-output',
                params={'id': agent_id},
                headers={'X-Phantombuster-Key': api_key},
                timeout=30
            )
            if files_response.status_code == 200:
                files_data = files_response.json()
                if debug:
                    with st.expander("Agent Output Data"):
                        st.json({k: v for k, v in files_data.items() if k != 'output'})
        except:
            pass

        # Try common file patterns - Sales Navigator Export uses these
        possible_files = []

        # If specific filename provided, try it first with common PB variants, then fallbacks
        if filename:
            possible_files = [
                f'{filename}.csv', f'{filename}.json',
                f'database-{filename}.csv', f'database-{filename}.json',
                'result.csv', 'result.json',
            ]
        else:
            # Add agent name-based files (Sales Navigator Export pattern)
            if agent_name and agent_name != 'Unknown':
                possible_files.append(f'{agent_name}.csv')
                possible_files.append(f'{agent_name}.json')

            # Common PhantomBuster output files - Sales Navigator Export uses database files
            # Try many variations of database filename patterns
            possible_files.extend([
                'result.csv',
                'result.json',
                'database-result.csv',
                'database-linkedin-sales-navigator-search-export.csv',
                'database-Sales Navigator Search Export.csv',
                'Sales Navigator Search Export.csv',
                'database-sales-navigator-search-export.csv',
                'LinkedIn Sales Navigator Search Export result.csv',
                'Sales Navigator Search Export result.csv',
                # Lowercase variations
                'database-sales navigator search export.csv',
                'sales navigator search export.csv',
                # With underscores
                'database_linkedin_sales_navigator_search_export.csv',
                'database_result.csv',
                # Output variations
                'output.csv',
                'output.json',
                'profiles.csv',
                'leads.csv',
            ])

            # Also try with s3Folder prefix patterns
            if s3_folder:
                possible_files.insert(0, f'database-linkedin-sales-navigator-search-export.csv')
                possible_files.insert(0, f'result.csv')

        # Try to list all files in agent storage using store API
        try:
            # Try listing files with common patterns
            for pattern in ['database-', 'result', agent_name.replace(' ', '-').lower() if agent_name else '']:
                if not pattern:
                    continue
                list_response = requests.get(
                    'https://api.phantombuster.com/api/v2/store/fetch',
                    params={'id': agent_id, 'name': f'{pattern}'},
                    headers={'X-Phantombuster-Key': api_key},
                    timeout=10
                )
                # 200 means file exists, add it
                if list_response.status_code == 200:
                    if debug:
                        st.success(f"Found file with pattern: {pattern}")
        except Exception as e:
            if debug:
                st.warning(f"List files error: {e}")

        if debug:
            st.info(f"Trying files: {possible_files[:5]}...")

        for fname in possible_files:
            try:
                # Try with agent ID
                store_response = requests.get(
                    'https://api.phantombuster.com/api/v2/store/fetch',
                    params={'id': agent_id, 'name': fname},
                    headers={'X-Phantombuster-Key': api_key},
                    timeout=60
                )

                if debug:
                    st.info(f"Store API {fname}: {store_response.status_code}")

                # If 404, try with s3Folder path
                if store_response.status_code == 404 and s3_folder:
                    store_response = requests.get(
                        'https://api.phantombuster.com/api/v2/store/fetch',
                        params={'id': agent_id, 'name': f'{s3_folder}/{fname}'},
                        headers={'X-Phantombuster-Key': api_key},
                        timeout=60
                    )
                    if debug:
                        st.info(f"Store API (with folder) {s3_folder}/{fname}: {store_response.status_code}")

                # Also try direct org folder access
                if store_response.status_code == 404 and org_s3_folder:
                    store_response = requests.get(
                        'https://api.phantombuster.com/api/v2/store/fetch',
                        params={'id': agent_id, 'name': f'{org_s3_folder}/{s3_folder}/{fname}'},
                        headers={'X-Phantombuster-Key': api_key},
                        timeout=60
                    )
                    if debug:
                        st.info(f"Store API (full path): {store_response.status_code}")

                if store_response.status_code == 200:
                    content_type = store_response.headers.get('content-type', '')

                    if 'csv' in fname or 'csv' in content_type:
                        df = pd.read_csv(StringIO(store_response.text))
                        if not df.empty:
                            st.success(f"Loaded {len(df)} profiles from {fname}")
                            return df
                    elif 'json' in fname or 'json' in content_type:
                        profiles = store_response.json()
                        if isinstance(profiles, list) and profiles:
                            st.success(f"Loaded {len(profiles)} profiles from {fname}")
                            return pd.DataFrame(profiles)
            except Exception as e:
                if debug:
                    st.warning(f"Store API {fname}: {e}")


        # Try the container result object method
        response = requests.get(
            'https://api.phantombuster.com/api/v2/containers/fetch-all',
            params={'agentId': agent_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=60
        )

        if response.status_code != 200:
            st.error(f"Failed to fetch containers: {response.status_code}")
            return pd.DataFrame()

        data = response.json()
        containers = data.get('containers', [])

        if debug:
            st.info(f"Found {len(containers)} containers")

        if not containers:
            st.error("No runs found for this agent. Run the agent first.")
            return pd.DataFrame()

        # Get the most recent finished container
        finished_containers = [c for c in containers if c.get('status') == 'finished']
        if not finished_containers:
            st.error("No completed runs found. Wait for the agent to finish.")
            return pd.DataFrame()

        container = finished_containers[0]
        container_id = container['id']

        if debug:
            with st.expander("Latest Container"):
                st.json(container)

        # Check if container has output file URL
        output_url = container.get('resultObject') or container.get('output')
        if output_url and isinstance(output_url, str) and output_url.startswith('http'):
            try:
                output_response = requests.get(output_url, timeout=60)
                if output_response.status_code == 200:
                    if '.csv' in output_url or 'text/csv' in output_response.headers.get('content-type', ''):
                        df = pd.read_csv(StringIO(output_response.text))
                        if not df.empty:
                            st.success(f"Loaded {len(df)} profiles from container output")
                            return df
                    else:
                        profiles = output_response.json()
                        if isinstance(profiles, list) and profiles:
                            return pd.DataFrame(profiles)
            except Exception as e:
                if debug:
                    st.warning(f"Container output URL failed: {e}")

        # Fetch the result object using the container ID
        result_response = requests.get(
            'https://api.phantombuster.com/api/v2/containers/fetch-result-object',
            params={'id': container_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=60
        )

        if debug:
            with st.expander("Result Object Response"):
                st.json(result_response.json() if result_response.status_code == 200 else {"status": result_response.status_code})

        if result_response.status_code != 200:
            st.error(f"Failed to fetch results: {result_response.status_code}")
            return pd.DataFrame()

        result_data = result_response.json()
        result_object = result_data.get('resultObject')

        if not result_object:
            # Check if the output says "already been processed"
            output_text = ""
            try:
                out_resp = requests.get(
                    'https://api.phantombuster.com/api/v2/agents/fetch-output',
                    params={'id': agent_id},
                    headers={'X-Phantombuster-Key': api_key},
                    timeout=30
                )
                if out_resp.status_code == 200:
                    output_text = out_resp.json().get('output', '')
            except:
                pass

            if 'already been processed' in output_text:
                st.warning("""
**No new profiles found.**

The phantom says "This search has already been processed" - meaning it already scraped these profiles before.

**To get fresh results:**
1. Go to **Launch Search** section below
2. Enter your name and a NEW Sales Navigator search URL
3. Click **Launch** - this will clear old results and start fresh
                """)
            else:
                st.error("No result object found.")
                st.warning(f"""
**Troubleshooting:**
- Agent: {agent_name}
- Containers found: {len(containers)}
- Finished runs: {len(finished_containers)}
- Latest run status: {container.get('status')}

The phantom may not have produced any results. Try:
1. Running the phantom with a new search URL
2. Or download CSV directly from PhantomBuster dashboard
                """)
            return pd.DataFrame()

        # Parse the result object (it's a JSON string)
        if isinstance(result_object, str):
            profiles = json.loads(result_object)
        else:
            profiles = result_object

        if isinstance(profiles, list):
            return pd.DataFrame(profiles)
        else:
            return pd.DataFrame([profiles])

    except json.JSONDecodeError as e:
        st.error(f"Error parsing PhantomBuster results: {e}")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"PhantomBuster fetch error: {e}")
        return pd.DataFrame()


def find_user_phantom(api_key: str, user_email: str, agents: list) -> dict:
    """Find a phantom that belongs to a specific user based on email in the name.

    Returns the agent dict if found, None otherwise.
    """
    user_identifier = user_email.split('@')[0].lower()  # Use part before @ for matching

    for agent in agents:
        agent_name = agent.get('name', '').lower()
        # Check if agent name contains the user identifier
        if user_identifier in agent_name or user_email.lower() in agent_name:
            return agent

    return None


def duplicate_phantom_for_user(api_key: str, template_agent_id: str, user_email: str) -> dict:
    """Duplicate a template phantom for a new user.

    Returns dict with 'id' and 'name' on success, or 'error' on failure.
    """
    try:
        # First fetch the template agent's full config
        fetch_response = requests.get(
            'https://api.phantombuster.com/api/v2/agents/fetch',
            params={'id': template_agent_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=30
        )

        if fetch_response.status_code != 200:
            return {'error': f"Failed to fetch template: {fetch_response.status_code}"}

        template = fetch_response.json()

        # Create new agent name with user email
        user_name = user_email.split('@')[0].title()
        new_name = f"Sales Nav Export - {user_name}"

        # Prepare the new agent config (copy from template)
        new_agent = {
            'name': new_name,
            'script': template.get('script'),
            'branch': template.get('branch', 'master'),
            'environment': template.get('environment', 'release'),
            'argument': template.get('argument', '{}'),
        }

        # Create the new agent
        create_response = requests.post(
            'https://api.phantombuster.com/api/v2/agents/save',
            headers={
                'X-Phantombuster-Key': api_key,
                'Content-Type': 'application/json'
            },
            json=new_agent,
            timeout=30
        )

        if create_response.status_code == 200:
            result = create_response.json()
            return {'id': result.get('id'), 'name': new_name}
        else:
            return {'error': f"Failed to create: {create_response.status_code} - {create_response.text}"}

    except Exception as e:
        return {'error': str(e)}


def update_phantombuster_search_url(api_key: str, agent_id: str, search_url: str, num_profiles: int = 2500, csv_name: str = None) -> dict:
    """Update the phantom's saved search URL configuration.

    This updates the phantom's saved argument so it uses the new search URL
    when launched with saved settings.

    Args:
        api_key: PhantomBuster API key
        agent_id: Agent ID
        search_url: Sales Navigator search URL
        num_profiles: Number of profiles to scrape
        csv_name: Custom output filename (without .csv extension). If None, uses timestamp.

    Returns dict with 'success': True, 'csvName': filename or 'error': message
    """
    from datetime import datetime

    try:
        # First fetch current agent config to preserve other settings
        fetch_response = requests.get(
            'https://api.phantombuster.com/api/v2/agents/fetch',
            params={'id': agent_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=30
        )

        if fetch_response.status_code != 200:
            return {'error': f"Failed to fetch agent: {fetch_response.status_code}"}

        agent_data = fetch_response.json()
        current_argument = agent_data.get('argument', '{}')

        # Parse current argument
        try:
            if isinstance(current_argument, str):
                arg_dict = json.loads(current_argument)
            else:
                arg_dict = current_argument
        except:
            arg_dict = {}

        # Generate timestamped filename if not provided
        if csv_name is None:
            timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M')
            csv_name = f'search_{timestamp}'

        # Update search URL, profile count, and output filename
        arg_dict['salesNavigatorSearchUrl'] = search_url
        arg_dict['search'] = search_url
        arg_dict['numberOfProfiles'] = num_profiles
        arg_dict['numberOfResultsPerSearch'] = num_profiles
        arg_dict['csvName'] = csv_name  # PhantomBuster uses this for output file naming

        # Update the agent with new argument
        update_response = requests.post(
            'https://api.phantombuster.com/api/v2/agents/save',
            headers={
                'X-Phantombuster-Key': api_key,
                'Content-Type': 'application/json'
            },
            json={
                'id': agent_id,
                'argument': json.dumps(arg_dict)
            },
            timeout=30
        )

        if update_response.status_code == 200:
            return {'success': True, 'csvName': csv_name}
        else:
            return {'error': f"Failed to update: {update_response.status_code} - {update_response.text}"}

    except Exception as e:
        return {'error': str(e)}


def list_phantombuster_files(api_key: str, agent_id: str) -> list[dict]:
    """List all files in a PhantomBuster agent's storage.

    Returns list of dicts with 'name', 'size', 'lastModified' for each file.
    """
    try:
        # First get agent info to get S3 folder
        agent_response = requests.get(
            'https://api.phantombuster.com/api/v2/agents/fetch',
            params={'id': agent_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=30
        )

        if agent_response.status_code != 200:
            return []

        agent_data = agent_response.json()
        s3_folder = agent_data.get('s3Folder')
        org_s3_folder = agent_data.get('orgS3Folder')
        agent_name = agent_data.get('name', '')

        files = []
        seen_names = set()

        def add_file(name, size=0, last_modified=''):
            if name not in seen_names:
                seen_names.add(name)
                files.append({
                    'name': name,
                    'size': size,
                    'lastModified': last_modified,
                })

        # Try to list files from agent's fileMgmt if available
        file_mgmt = agent_data.get('fileMgmt', {})
        if file_mgmt:
            for filename, info in file_mgmt.items():
                if isinstance(info, dict):
                    add_file(filename, info.get('size', 0), info.get('lastModified', ''))
                else:
                    add_file(filename)

        # Check lastSaveFolder for output files
        last_save = agent_data.get('lastSaveFolder')
        if last_save:
            add_file(f'{last_save}.csv')
            add_file(f'{last_save}.json')

        # Build list of files to check via cache URL (fastest method)
        files_to_check = ['result.csv', 'result.json']

        # Add agent name patterns
        if agent_name:
            files_to_check.extend([
                f'{agent_name}.csv',
                f'{agent_name}.json',
                f'database-{agent_name}.csv',
            ])

        # Add common Sales Navigator patterns
        files_to_check.extend([
            'database-linkedin-sales-navigator-search-export.csv',
            'database-sales-navigator-search-export.csv',
            'database-Sales Navigator Search Export.csv',
        ])

        # Check via cache URL (Store API doesn't work for these files)
        if s3_folder and org_s3_folder:
            cache_base = f'https://cache1.phantombooster.com/{org_s3_folder}/{s3_folder}/'
            for fname in files_to_check:
                if fname in seen_names:
                    continue
                try:
                    cache_response = requests.head(cache_base + fname, timeout=5)
                    if cache_response.status_code == 200:
                        add_file(
                            fname,
                            int(cache_response.headers.get('content-length', 0)),
                            cache_response.headers.get('last-modified', '')
                        )
                except:
                    pass

        # Also add files from local search history for this agent
        try:
            local_history = load_search_history(agent_id=agent_id)
            for h in local_history:
                csv_name = h.get('csv_name')
                if csv_name:
                    fname = f'{csv_name}.csv'
                    if fname not in seen_names and s3_folder and org_s3_folder:
                        # Verify file exists via cache URL (use GET, not HEAD - HEAD returns 403)
                        try:
                            cache_url = f'https://cache1.phantombooster.com/{org_s3_folder}/{s3_folder}/{fname}'
                            check = requests.get(cache_url, timeout=5, stream=True)
                            if check.status_code == 200:
                                add_file(
                                    fname,
                                    int(check.headers.get('content-length', 0)),
                                    h.get('launched_at', '')
                                )
                            check.close()
                        except:
                            pass
        except:
            pass


        # Get recent container outputs (shows files from recent runs)
        try:
            containers_response = requests.get(
                'https://api.phantombuster.com/api/v2/containers/fetch-all',
                params={'agentId': agent_id},
                headers={'X-Phantombuster-Key': api_key},
                timeout=15
            )
            if containers_response.status_code == 200:
                containers = containers_response.json()
                for container in containers[:10]:  # Check last 10 runs
                    # Check for output files in container
                    output_files = container.get('outputFiles', [])
                    for of in output_files:
                        if isinstance(of, str) and of.endswith('.csv'):
                            add_file(of)
                    # Also check resultObject filename
                    result_obj = container.get('resultObject')
                    if result_obj and isinstance(result_obj, str):
                        try:
                            # Sometimes resultObject contains the filename
                            if result_obj.endswith('.csv') or result_obj.endswith('.json'):
                                add_file(result_obj)
                        except:
                            pass
        except:
            pass

        return files
    except Exception as e:
        return []


def delete_phantombuster_file(api_key: str, agent_id: str, filename: str) -> bool:
    """Delete a file from PhantomBuster agent's storage.

    Returns True if deleted successfully or file didn't exist.
    """
    try:
        response = requests.delete(
            'https://api.phantombuster.com/api/v2/store/delete',
            params={'id': agent_id, 'name': filename},
            headers={'X-Phantombuster-Key': api_key},
            timeout=30
        )
        # 200 = deleted, 404 = didn't exist (both are fine)
        return response.status_code in [200, 204, 404]
    except Exception:
        return False


def clear_all_phantombuster_files(api_key: str, agent_id: str) -> int:
    """Delete ALL files from PhantomBuster agent's storage for a completely fresh start.

    Returns the number of files deleted.
    """
    deleted_count = 0
    try:
        # First, get the agent info to find storage folders
        response = requests.get(
            'https://api.phantombuster.com/api/v2/agents/fetch',
            params={'id': agent_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=30
        )

        if response.status_code == 200:
            agent_data = response.json()
            s3_folder = agent_data.get('s3Folder', '')
            org_s3_folder = agent_data.get('orgS3Folder', '')

            if s3_folder and org_s3_folder:
                # Try to list files from PhantomBuster storage
                # Use the store/fetch-all endpoint if available, otherwise delete known patterns
                files_response = requests.get(
                    f'https://cache1.phantombooster.com/{org_s3_folder}/{s3_folder}/',
                    timeout=30
                )

                # Try common file patterns
                common_files = [
                    'result.csv', 'result.json',
                    'database-result.csv', 'database.csv',
                    'database-linkedin-sales-navigator-search-export.csv',
                    'database-Sales Navigator Search Export.csv',
                    'database-sales-navigator-search-export.csv',
                ]

                for filename in common_files:
                    if delete_phantombuster_file(api_key, agent_id, filename):
                        deleted_count += 1

    except Exception:
        pass

    return deleted_count


@st.cache_resource
def _get_pb_agent_locks():
    """Shared tracker for PhantomBuster agent launches across all user sessions.
    Prevents two users from launching the same agent simultaneously."""
    return {'lock': threading.Lock(), 'running': {}}  # {agent_id: {'user': username, 'started': timestamp, 'container_id': str}}


def pb_agent_is_busy(agent_id: str) -> dict:
    """Check if a PhantomBuster agent is currently locked by another user.
    Returns the lock info dict if busy, or None if free.
    Auto-expires locks older than 15 minutes (agents should finish by then)."""
    locks = _get_pb_agent_locks()
    with locks['lock']:
        info = locks['running'].get(str(agent_id))
        if info and (time.time() - info['started']) > 900:  # 15 min expiry
            del locks['running'][str(agent_id)]
            return None
        return info


def pb_agent_lock(agent_id: str, username: str, container_id: str = None):
    """Mark an agent as running by a user."""
    locks = _get_pb_agent_locks()
    with locks['lock']:
        locks['running'][str(agent_id)] = {
            'user': username,
            'started': time.time(),
            'container_id': container_id,
        }


def pb_agent_unlock(agent_id: str):
    """Release the lock on an agent."""
    locks = _get_pb_agent_locks()
    with locks['lock']:
        locks['running'].pop(str(agent_id), None)


def launch_phantombuster_agent(api_key: str, agent_id: str, argument: dict = None, clear_results: bool = False, tracker: 'UsageTracker' = None) -> dict:
    """Launch a PhantomBuster agent with the given argument.

    Returns dict with 'containerId' on success, or 'error' on failure.
    Note: Passing any argument overrides the phantom's saved config including cookie!

    Args:
        clear_results: If True, delete existing result AND database files before launching for fresh results
        tracker: Optional UsageTracker for logging API usage
    """
    # Check if another user is already running this agent
    busy = pb_agent_is_busy(agent_id)
    if busy:
        return {'error': f"Agent is already running (launched by {busy['user']} {int((time.time() - busy['started']) / 60)}m ago). Please wait or use a different agent."}

    start_time = time.time()
    try:
        # Delete existing results AND database for a fresh start
        if clear_results:
            # First try to list actual files and delete them
            actual_files = list_phantombuster_files(api_key, agent_id)
            for f in actual_files:
                filename = f['name'] if isinstance(f, dict) else f
                delete_phantombuster_file(api_key, agent_id, filename)

            # Also delete common file patterns (in case listing missed some)
            common_files = [
                'result.csv', 'result.json',
                'database-result.csv', 'database.csv',
                'database-linkedin-sales-navigator-search-export.csv',
                'database-Sales Navigator Search Export.csv',
                'database-sales-navigator-search-export.csv',
            ]
            for f in common_files:
                delete_phantombuster_file(api_key, agent_id, f)

        payload = {'id': agent_id}
        if argument:
            # Pass argument as JSON string to merge with saved config rather than replace
            payload['argument'] = json.dumps(argument)

        response = requests.post(
            'https://api.phantombuster.com/api/v2/agents/launch',
            headers={
                'X-Phantombuster-Key': api_key,
                'Content-Type': 'application/json'
            },
            json=payload,
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            container_id = data.get('containerId')

            # Lock the agent so other users see it's running
            username = st.session_state.get('username', 'unknown')
            pb_agent_lock(agent_id, username, container_id)

            # Log successful launch
            if tracker:
                tracker.log_phantombuster(
                    operation='launch',
                    status='success',
                    agent_id=agent_id,
                    container_id=container_id
                )

            return {'containerId': container_id}
        else:
            if tracker:
                tracker.log_phantombuster(
                    operation='launch',
                    status='error',
                    error_message=f"API error {response.status_code}",
                    agent_id=agent_id
                )
            return {'error': f"API error {response.status_code}: {response.text}"}
    except Exception as e:
        if tracker:
            tracker.log_phantombuster(
                operation='launch',
                status='error',
                error_message=str(e)[:200],
                agent_id=agent_id
            )
        return {'error': str(e)}


def fetch_container_status(api_key: str, container_id: str) -> dict:
    """Fetch the status of a PhantomBuster container.

    Returns dict with 'status' (running, finished, error) and other details.
    """
    try:
        response = requests.get(
            'https://api.phantombuster.com/api/v2/containers/fetch',
            params={'id': container_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            result = {
                'status': data.get('status', 'unknown'),
                'exitCode': data.get('exitCode'),
                'exitMessage': data.get('exitMessage'),
                'progress': data.get('progress'),
                'executionTime': data.get('executionTime'),
                'output': data.get('output', ''),
            }

            # Try to extract profile count from output (PhantomBuster logs progress)
            output = data.get('output', '')
            if output:
                # Look for patterns like "Scraped 50 profiles" or "50 profiles saved"
                import re
                matches = re.findall(r'(\d+)\s*(?:profiles?|leads?|results?)', output.lower())
                if matches:
                    result['profiles_count'] = int(matches[-1])  # Get last match

                # Look for progress percentage
                pct_matches = re.findall(r'(\d+)%', output)
                if pct_matches:
                    result['progress_pct'] = int(pct_matches[-1])

            return result
        else:
            return {'status': 'error', 'error': f"API error {response.status_code}"}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def normalize_phantombuster_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize PhantomBuster column names using shared normalizers.

    Uses normalizers.py for consistent field mapping across dashboard and db.
    Does NOT add Crustdata-specific fields - keeps PhantomBuster data clean.
    """
    # Debug: log all columns to help identify URL fields
    url_cols = [c for c in df.columns if 'url' in c.lower() or 'link' in c.lower() or 'profile' in c.lower() or 'identifier' in c.lower()]
    st.session_state['_debug_url_cols'] = url_cols
    st.session_state['_debug_all_cols'] = list(df.columns)

    # Convert DataFrame to list of dicts, normalize each, convert back
    records = df.to_dict('records')
    normalized_records = []

    for raw in records:
        normalized = normalize_phantombuster_profile(raw)
        if normalized:
            # Build display record with PhantomBuster fields only (no Crustdata fields)
            first = normalized.get('first_name') or ''
            last = normalized.get('last_name') or ''
            name = f"{first} {last}".strip() or 'Unknown'

            # Get raw PhantomBuster data for additional fields
            raw_pb = normalized.get('raw_phantombuster') or {}

            display_record = {
                'name': name,
                'first_name': first,
                'last_name': last,
                'current_company': normalized.get('current_company') or '',
                'current_title': normalized.get('current_title') or '',
                'headline': normalized.get('headline') or '',
                'location': normalized.get('location') or '',
                'linkedin_url': normalized.get('linkedin_url') or '',
                'summary': normalized.get('summary') or raw_pb.get('summary') or '',
                'title_description': raw_pb.get('titleDescription') or '',
                'industry': raw_pb.get('industry') or '',
                'company_location': raw_pb.get('companyLocation') or '',
                'current_years_in_role': normalized.get('current_years_in_role'),
                'current_years_at_company': normalized.get('current_years_at_company'),
                # MEMORY: Don't store raw_phantombuster in display DataFrame
                # It's 5-20KB per profile and not needed for filtering/display
            }
            normalized_records.append(display_record)
        else:
            # Keep original record but mark as invalid (no URL)
            raw['linkedin_url'] = None
            normalized_records.append(raw)

    result_df = pd.DataFrame(normalized_records)

    # Debug: count valid URLs
    if 'linkedin_url' in result_df.columns:
        valid_count = result_df['linkedin_url'].notna().sum()
        st.session_state['_debug_valid_urls'] = f"{valid_count}/{len(result_df)}"
    else:
        st.session_state['_debug_valid_urls'] = "0/0"

    return result_df


def normalize_uploaded_csv(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize uploaded CSV columns to match expected dashboard format.

    Handles GEM exports and other common CSV formats.
    """
    # GEM column mapping
    gem_column_map = {
        'First Name': 'first_name',
        'Last Name': 'last_name',
        'Company': 'current_company',
        'Title': 'current_title',
        'Location': 'location',
        'Primary Email': 'email',
        'Phone Number': 'phone',
        'LinkedIn': 'linkedin_url',
        'School': 'school',
        'Sourcer': 'sourcer',
        'Time Sourced': 'sourced_at',
        'All Emails': 'all_emails',
        'All Phone Numbers': 'all_phones',
    }

    # Generic column mapping (other CSV formats)
    # Note: Order matters - defaultProfileUrl should come before linkedInProfileUrl
    # because linkedInProfileUrl often contains encoded ACwAAA... identifiers that get rejected
    generic_column_map = {
        'first_name': 'first_name',
        'last_name': 'last_name',
        'firstName': 'first_name',
        'lastName': 'last_name',
        'fullName': 'name',
        'company': 'current_company',
        'companyName': 'current_company',
        'title': 'current_title',
        'job_title': 'current_title',
        'email': 'email',
        'phone': 'phone',
        'linkedin_url': 'linkedin_url',
        'LinkedIn URL': 'linkedin_url',
        'linkedinUrl': 'linkedin_url',
        'profile_url': 'linkedin_url',
        # PhantomBuster Search Export columns - defaultProfileUrl is the clean URL
        'defaultProfileUrl': 'linkedin_url',
    }

    # Combine mappings (GEM takes priority)
    column_map = {**generic_column_map, **gem_column_map}

    # Rename columns
    rename_dict = {}
    used_targets = set()

    for old_name, new_name in column_map.items():
        if old_name in df.columns and new_name not in used_targets:
            rename_dict[old_name] = new_name
            used_targets.add(new_name)

    df = df.rename(columns=rename_dict)

    # Remove duplicate columns (keep first occurrence)
    # This can happen when CSV has both 'fullName' and 'name' columns
    df = df.loc[:, ~df.columns.duplicated(keep='first')]

    # Fallback: auto-detect LinkedIn URL column if not found
    if 'linkedin_url' not in df.columns:
        def is_regular_linkedin_url(url):
            """Check if URL is a regular LinkedIn profile URL (not Sales Nav, company, etc.)"""
            if not url or pd.isna(url):
                return False
            url = str(url).lower()
            # Must have linkedin.com and /in/ (profile URL)
            # Exclude: /sales/, /company/, /jobs/, /school/, /groups/
            if 'linkedin.com' in url and '/in/' in url:
                if '/sales/' not in url:
                    return True
            return False

        # Find columns with "linkedin" in the name
        linkedin_cols = [c for c in df.columns if 'linkedin' in c.lower()]

        for col in linkedin_cols:
            # Check first 5 non-empty values to verify it contains regular LinkedIn URLs
            sample_values = df[col].dropna().head(5).tolist()
            if sample_values:
                valid_count = sum(1 for v in sample_values if is_regular_linkedin_url(v))
                # If at least 60% of samples are valid LinkedIn profile URLs, use this column
                if valid_count >= len(sample_values) * 0.6:
                    df = df.rename(columns={col: 'linkedin_url'})
                    break

    # Create combined 'name' column from first_name + last_name if missing
    if 'name' not in df.columns and 'first_name' in df.columns and 'last_name' in df.columns:
        df['name'] = (df['first_name'].fillna('').astype(str).str.strip() + ' ' +
                      df['last_name'].fillna('').astype(str).str.strip()).str.strip()

    # Use headline as current_title if current_title doesn't exist
    if 'headline' in df.columns and 'current_title' not in df.columns:
        df['current_title'] = df['headline']

    # Clean and normalize LinkedIn URLs
    if 'linkedin_url' in df.columns:
        def clean_linkedin_url(url):
            if pd.isna(url) or not url:
                return None
            url = str(url).strip()
            # Add https:// if missing
            if url.startswith('www.'):
                url = 'https://' + url
            elif not url.startswith('http'):
                url = 'https://' + url
            # Must be linkedin.com and have /in/
            if 'linkedin.com' in url and '/in/' in url:
                # Remove query params
                url = url.split('?')[0].rstrip('/')
                # Filter out encoded URLs (ACwAAA... format - internal LinkedIn IDs)
                username_part = url.split('/in/')[-1]
                if username_part.startswith('ACw') or username_part.startswith('Acw'):
                    return None  # Encoded URL, not usable
                return url
            return None
        df['linkedin_url'] = df['linkedin_url'].apply(clean_linkedin_url)

    # Create public_url as alias for linkedin_url (for display compatibility)
    if 'linkedin_url' in df.columns and 'public_url' not in df.columns:
        df['public_url'] = df['linkedin_url']

    # Filter out profiles without valid LinkedIn URLs
    original_count = len(df)
    skipped_no_url = 0
    skipped_encoded = 0
    if 'linkedin_url' in df.columns:
        # Count encoded URLs before filtering (they were set to None by clean_linkedin_url)
        # We need to re-check the original data to count encoded vs no URL
        df = df[df['linkedin_url'].notna()].reset_index(drop=True)
    skipped_count = original_count - len(df)

    # Store skipped count for display
    df.attrs['_skipped_no_url'] = skipped_count

    return df


def extract_urls_from_phantombuster(df: pd.DataFrame) -> list[str]:
    """Extract LinkedIn URLs from PhantomBuster results (regular linkedin.com URLs only)."""
    urls = []
    # Use only regular LinkedIn URLs, not Sales Navigator URLs
    url_columns = ['linkedin_url', 'public_url', 'defaultProfileUrl']

    for col in url_columns:
        if col in df.columns:
            urls.extend(df[col].dropna().tolist())
            break

    # Normalize URLs
    normalized = []
    for u in urls:
        if u and 'linkedin.com' in str(u):
            u = str(u).strip()
            if not u.startswith('http'):
                u = 'https://' + u
            normalized.append(u)
    return normalized


@st.cache_resource(ttl=300)
def get_gspread_client():
    """Get authenticated gspread client using service account."""
    config = load_config()

    scopes = [
        'https://www.googleapis.com/auth/spreadsheets.readonly',
        'https://www.googleapis.com/auth/drive.readonly'
    ]

    # Try using credentials dict (from Streamlit secrets, config.json, or loaded from credentials file)
    if config.get('google_credentials'):
        from google.oauth2.service_account import Credentials as SACredentials
        creds = SACredentials.from_service_account_info(config['google_credentials'], scopes=scopes)
        return gspread.authorize(creds)

    # Fall back to credentials file (local development)
    creds_file = config.get('google_credentials_file')
    if not creds_file:
        return None

    creds_path = Path(__file__).parent / creds_file
    if not creds_path.exists():
        return None

    creds = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
    return gspread.authorize(creds)


_GSPREAD_SERVICE_ACCOUNT = "linkedin-enricher@linkedin-enricher-485616.iam.gserviceaccount.com"


def render_filter_sheet_status(filter_sheets: dict, gspread_client) -> bool:
    """Render a visible Google Sheets connection status badge in the current tab.

    Returns True iff the sheet is actually reachable (the helper has successfully
    opened it via gspread). Used by both the Filter and Filter+ tabs so the
    user can always tell whether their sheet-based filters will run.

    States:
      - No URL configured        → st.info  ("paste a URL")
      - URL but no gspread client → st.error ("credentials missing/invalid")
      - URL + client but open fails → st.error (actual exception class + msg)
      - All good                  → st.success ("Filter sheet connected: <name>")
    """
    url = filter_sheets.get('url') if filter_sheets else None
    if not url:
        st.info(
            "No filter sheet URL configured — paste a Google Sheets URL above "
            "to enable sheet-based filters (blacklist, not-relevant, target companies, etc.)."
        )
        return False
    if gspread_client is None:
        st.error(
            "**Couldn't connect — Google credentials missing or invalid.** "
            "Sheet-based filters (blacklist, not-relevant, target companies, etc.) "
            "will be skipped. Check `config.json`'s `google_credentials_file` setting, "
            "or set `google_credentials` in your Streamlit secrets."
        )
        return False

    # Try to open the sheet (cached in session_state to avoid repeated API calls
    # within one Streamlit run). Store both the OK title and any error reason so
    # repeated renders don't retry on every interaction.
    cache_key = f"sheet_status::{url}"
    if cache_key not in st.session_state:
        try:
            title = gspread_client.open_by_url(url).title
            st.session_state[cache_key] = ('ok', title)
        except Exception as e:
            st.session_state[cache_key] = ('error', f"{type(e).__name__}: {e}")

    status, payload = st.session_state[cache_key]
    if status == 'ok':
        st.success(f"Filter sheet connected: **{payload}**")
        return True

    st.error(
        f"**Couldn't open the filter sheet.** {payload}\n\n"
        "Common causes:\n"
        f"- The sheet isn't shared with the service account `{_GSPREAD_SERVICE_ACCOUNT}`.\n"
        "- The URL doesn't point at a Google Sheet (it should look like "
        "`https://docs.google.com/spreadsheets/d/…`).\n"
        "- The sheet was deleted or moved."
    )
    return False


def render_filter_funnel_breakdown(counts: dict) -> None:
    """Render the per-filter "X candidates dropped by Y filter" funnel summary.

    Used by both the Filter and Filter+ tabs so users see the same shape of
    drop-off breakdown after applying filters. Reads a {filter_name: count}
    dict and renders one bullet per non-zero filter, plus a hint if anything
    was filtered out (or an info message if nothing was).

    Args:
        counts: Mapping of human-readable filter name -> number removed.
                Values <= 0 are skipped.
    """
    st.divider()
    st.markdown("### Filtered Candidates Summary")
    st.caption("Counts of candidates removed by each filter (data not stored to save memory)")

    filter_names = [k for k, v in (counts or {}).items() if v and int(v) > 0]
    if filter_names:
        for filter_name in filter_names:
            st.markdown(f"- **{filter_name}**: {int(counts[filter_name])} removed")
        st.info("💡 To restore candidates, reload original data and re-apply filters with different settings")
    else:
        st.info("No candidates were filtered out")


@st.cache_data(ttl=600, max_entries=10, show_spinner="Loading sheet...")
def load_sheet_as_df(sheet_url: str, worksheet_name: str = None) -> pd.DataFrame:
    """Load a Google Sheet as a pandas DataFrame.

    Cached for 10 minutes to reduce Google Sheets API calls during filtering.
    """
    client = get_gspread_client()
    if not client:
        return None

    try:
        spreadsheet = client.open_by_url(sheet_url)
        if worksheet_name:
            worksheet = spreadsheet.worksheet(worksheet_name)
        else:
            worksheet = spreadsheet.sheet1

        rows = worksheet.get_all_values()
        if not rows:
            return pd.DataFrame()
        headers = rows[0]
        # Find last non-empty header to trim blank trailing columns
        last_col = 0
        for i, h in enumerate(headers):
            if h.strip():
                last_col = i + 1
        headers = headers[:last_col]
        data = [row[:last_col] for row in rows[1:]]
        return pd.DataFrame(data, columns=headers)
    except Exception as e:
        st.error(f"Error loading sheet: {e}")
        return None


def get_filter_sheets_config():
    """Get filter sheets URLs from config."""
    config = load_config()
    return config.get('filter_sheets', {})


def send_notification(title, message):
    """Send desktop notification with sound, plus in-browser toast."""
    try:
        st.toast(f"**{title}** — {message}", icon="✅")
    except Exception:
        pass
    try:
        if HAS_WINSOUND:
            winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS)
        if HAS_PLYER:
            notification.notify(
                title=title,
                message=message,
                app_name="SourcingX",
                timeout=10
            )
    except Exception:
        try:
            if HAS_WINSOUND:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except:
            pass


def extract_urls(uploaded_file) -> list[str]:
    """Extract LinkedIn URLs from uploaded file."""
    urls = []

    if uploaded_file.name.endswith('.json'):
        data = json.load(uploaded_file)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    url = (item.get('url') or item.get('linkedin_url') or
                           item.get('profile_url') or item.get('linkedinUrl') or
                           item.get('public_url'))
                    if url:
                        urls.append(url)
                elif isinstance(item, str):
                    urls.append(item)

    elif uploaded_file.name.endswith('.csv'):
        df = pd.read_csv(uploaded_file)
        for col in ['url', 'linkedin_url', 'profile_url', 'URL', 'LinkedIn URL', 'linkedinUrl', 'LinkedIn', 'linkedin', 'public_url']:
            if col in df.columns:
                urls = df[col].dropna().tolist()
                break
        if not urls and len(df.columns) > 0:
            urls = df.iloc[:, 0].dropna().tolist()

    # Filter to only LinkedIn URLs and normalize them
    normalized = []
    for u in urls:
        if u and 'linkedin.com' in str(u):
            u = str(u).strip()
            if u.startswith('www.'):
                u = 'https://' + u
            elif not u.startswith('http'):
                u = 'https://' + u
            normalized.append(u)
    return normalized


def enrich_batch(urls: list[str], api_key: str, tracker: 'UsageTracker' = None) -> list[dict]:
    """Enrich a batch of URLs via Crustdata's cheap v2025-11-01 batch-enrich
    pipeline (POST /batch/person/enrich, 1 credit/profile base) — NOT the
    legacy GET /screener/person/enrich (3 credits/profile) this function used
    before 2026-08-04. Reuses the same submit/poll/download/translate
    machinery AI Screen's thin-profile auto-enrich step already relies on
    (crustdata_search.batch_enrich_profiles()), so the response-mapping is
    already proven in production rather than new integration.

    Returns one dict per input URL, same order, same shape callers already
    expect (see the "Enrich N profiles" button and its downstream
    flatten_for_csv()/save_enriched_profiles_bulk() calls a few hundred
    lines below):
      - Match: the flat legacy-enrich dict (enrich_profile_to_legacy_shape())
        plus '_original_url' set to the input URL — used to build
        original_url_map for save_enriched_profiles_bulk, same as before.
      - No match / rejected / unknown: {'error': ..., 'linkedin_url': url} —
        same shape the old error path returned, read via
        record.get('linkedin_url') downstream. The error message
        distinguishes a confirmed no-match from a batch outcome we
        couldn't confirm (Codex review, PR #127, round 2, 2026-08-04 —
        see batch_enrich_profiles()'s docstring for what 'unknown' means).
    """
    start_time = time.time()

    try:
        result = batch_enrich_profiles(urls, api_key=api_key)
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        if tracker:
            tracker.log_crustdata_batch_enrich(
                requested=len(urls),
                fulfilled=0,
                status='error',
                error_message=str(e)[:200],
                response_time_ms=elapsed_ms,
            )
        return [{'error': str(e), 'linkedin_url': u} for u in urls]

    elapsed_ms = int((time.time() - start_time) * 1000)
    by_url = result.get('by_url') or {}
    unknown_norms = {normalize_linkedin_url(u) or u for u in (result.get('unknown') or [])}
    rejected_norms = {normalize_linkedin_url(u) or u for u in (result.get('rejected') or [])}

    output = []
    for url in urls:
        flat = by_url.get(url) or by_url.get(normalize_linkedin_url(url))
        if flat:
            item = dict(flat)
            item['_original_url'] = url
            output.append(item)
        else:
            norm_url = normalize_linkedin_url(url) or url
            if norm_url in unknown_norms:
                output.append({
                    'error': 'Crustdata batch status unknown — could not confirm enrichment result (may have consumed credits)',
                    'linkedin_url': url,
                })
            elif norm_url in rejected_norms:
                output.append({
                    'error': 'Crustdata returned data that failed identity/content validation',
                    'linkedin_url': url,
                })
            else:
                output.append({'error': 'Not found via Crustdata batch enrich', 'linkedin_url': url})

    if tracker:
        unknown_count = len(result.get('unknown') or [])
        if unknown_count:
            # A job whose outcome we couldn't confirm may have already
            # billed — must not be logged as a clean, free success.
            tracker.log_crustdata_batch_enrich(
                requested=result.get('requested', len(urls)),
                fulfilled=result.get('fulfilled', 0),
                status='error',
                error_message=(
                    f"{unknown_count} of {result.get('requested', len(urls))} profiles have "
                    f"unknown Crustdata billing status (batch poll/download failure) — "
                    f"batch_ids: {result.get('batch_ids')}"
                ),
                response_time_ms=elapsed_ms,
            )
        else:
            tracker.log_crustdata_batch_enrich(
                requested=result.get('requested', len(urls)),
                fulfilled=result.get('fulfilled', 0),
                status='success',
                response_time_ms=elapsed_ms,
            )

    return output


def log_similar_profiles_crustdata_usage(tracker: 'UsageTracker' = None, source: str = None, error: Exception = None) -> None:
    """Log Crustdata usage for the "Find Similar Profiles" tab's
    on-the-fly enrich call — extracted so it's unit-testable, since the
    tab itself is inline Streamlit UI code with no other extracted
    function (Codex review, PR #127, 2026-08-04: this call site had no
    usage logging at all before, defeating the point of a cost-control
    PR). similar_profiles.search_similar() intentionally isn't threaded
    with a UsageTracker parameter — SimilarProfileError instead carries
    crustdata_attempted/crustdata_fulfilled/crustdata_error attributes so
    this boundary function can log correctly even on failure paths (see
    that class's docstring for the full contract).

    Call with `source=result.get('source')` after a successful
    search_similar() call, XOR `error=<the caught SimilarProfileError>`.
    No-op if `tracker` is falsy, or if no Crustdata call was actually
    attempted (source not "enriched" and error has no crustdata_attempted).
    """
    if not tracker:
        return
    if source == "enriched":
        tracker.log_crustdata_sync_enrich(requested=1, fulfilled=1, status='success')
        return
    if error is not None and getattr(error, "crustdata_attempted", False):
        error_message = getattr(error, "crustdata_error", None)
        if error_message:
            tracker.log_crustdata_sync_enrich(
                requested=1, fulfilled=0, status='error',
                error_message=error_message,
            )
        else:
            tracker.log_crustdata_sync_enrich(
                requested=1,
                fulfilled=1 if getattr(error, "crustdata_fulfilled", False) else 0,
                status='success',
            )


def normalize_crustdata_profile(record: dict) -> dict:
    """Normalize a single Crustdata profile using shared normalizers.

    This is a wrapper around normalizers.normalize_crustdata_record for backwards compatibility.
    """
    original_url = record.get('_original_linkedin_url')
    normalized = normalize_crustdata_record(record, original_url)

    if not normalized:
        # Return minimal dict for failed records
        return {
            'linkedin_url': original_url or record.get('linkedin_profile_url') or record.get('linkedin_url') or '',
            'name': '',
            'first_name': '',
            'last_name': '',
            'current_company': '',
            'current_title': '',
            'headline': '',
            'location': '',
            'summary': '',
            'skills': '',
            'education': '',
            'all_employers': '',
            'all_titles': '',
            'all_schools': '',
        }

    # Convert to display format (includes all_employers, all_titles, all_schools, skills)
    display = profile_to_display_dict(normalized)

    return display


def flatten_for_csv(data: list[dict]) -> pd.DataFrame:
    """Flatten and normalize Crustdata profiles for display and CSV export.

    Uses shared normalizers for consistent field mapping.
    """
    normalized_records = []

    for record in data:
        normalized = normalize_crustdata_profile(record)
        normalized_records.append(normalized)

    return pd.DataFrame(normalized_records)


# ========== PRE-FILTERING FUNCTIONS ==========

def filter_csv_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Filter CSV to keep only screening-relevant columns."""

    def calc_years_to_today(date_str):
        if pd.isna(date_str) or not str(date_str).strip():
            return None
        try:
            dt = datetime.strptime(str(date_str).strip(), '%d %b %Y')
            years = (datetime.now() - dt).days / 365.25
            return round(years, 1)
        except:
            return None

    def calc_years_between(start_str, end_str):
        if pd.isna(start_str) or not str(start_str).strip():
            return None
        try:
            start = datetime.strptime(str(start_str).strip(), '%d %b %Y')
            if pd.isna(end_str) or not str(end_str).strip():
                end = datetime.now()
            else:
                end = datetime.strptime(str(end_str).strip(), '%d %b %Y')
            years = (end - start).days / 365.25
            return round(years, 1)
        except:
            return None

    result = pd.DataFrame()

    # Basic info
    result['first_name'] = df.get('first_name', pd.Series([''] * len(df)))
    result['last_name'] = df.get('last_name', pd.Series([''] * len(df)))
    result['headline'] = df.get('headline', pd.Series([''] * len(df)))
    result['location'] = df.get('location', pd.Series([''] * len(df)))
    result['summary'] = df.get('summary', pd.Series([''] * len(df)))

    # Current position
    result['current_title'] = df.get('job_1_job_title', pd.Series([''] * len(df)))
    result['current_company'] = df.get('job_1_job_company_name', pd.Series([''] * len(df)))
    result['current_start_date'] = df.get('job_1_job_start_date', pd.Series([''] * len(df)))
    result['current_years_in_role'] = df.get('job_1_job_start_date', pd.Series()).apply(calc_years_to_today)
    result['current_description'] = df.get('job_1_job_description', pd.Series([''] * len(df)))

    # Past positions
    def combine_past_positions(row):
        positions = []
        for i in range(2, 20):
            title_col = f'job_{i}_job_title'
            company_col = f'job_{i}_job_company_name'
            start_col = f'job_{i}_job_start_date'
            end_col = f'job_{i}_job_end_date'
            desc_col = f'job_{i}_job_description'

            if title_col in df.columns and pd.notna(row.get(title_col)):
                title = row.get(title_col, '')
                company = row.get(company_col, '')
                start = row.get(start_col, '')
                end = row.get(end_col, '')
                desc = row.get(desc_col, '')
                years = calc_years_between(start, end)
                years_str = f" [{years} yrs]" if years else ""
                desc_str = f": {desc}" if pd.notna(desc) and desc else ""
                positions.append(f"{title} at {company} ({start} - {end}){years_str}{desc_str}")
        return ' || '.join(positions)

    result['past_positions'] = df.apply(combine_past_positions, axis=1)

    # Education
    def combine_education(row):
        educations = []
        for i in range(1, 10):
            school_col = f'edu_{i}_school_name'
            degree_col = f'edu_{i}_degree'
            field_col = f'edu_{i}_field_of_study'

            if school_col in df.columns and pd.notna(row.get(school_col)):
                school = row.get(school_col, '')
                degree = row.get(degree_col, '')
                field = row.get(field_col, '')
                parts = [school]
                if pd.notna(degree) and degree:
                    parts.append(degree)
                if pd.notna(field) and field:
                    parts.append(f"in {field}")
                educations.append(', '.join(parts))
        return ' | '.join(educations)

    result['education'] = df.apply(combine_education, axis=1)

    # Skills
    skill_cols = [col for col in df.columns if re.match(r'^skill_\d+_name$', col)]
    def combine_skills(row):
        skills = []
        for col in skill_cols:
            if pd.notna(row.get(col)):
                skills.append(str(row[col]))
        return ', '.join(skills)

    result['skills'] = df.apply(combine_skills, axis=1)

    # LinkedIn URL
    result['public_url'] = df.get('public_url', df.get('linkedin_url', pd.Series([''] * len(df))))

    return result


def apply_pre_filters(df: pd.DataFrame, filters: dict) -> tuple[pd.DataFrame, dict, dict]:
    """Apply pre-filters to candidates. Returns filtered df, stats, and filtered_out dict (lightweight)."""
    stats = {}
    filtered_out = {}  # Store LIGHTWEIGHT removed candidates by filter type (display cols only)
    original_count = len(df)

    # MEMORY: Only keep display columns in filtered_out (not full row copies)
    _LIGHT_COLS = ['name', 'first_name', 'last_name', 'current_title', 'current_company', 'linkedin_url']

    def _store_filtered(label, mask, temp_cols=None):
        """Store lightweight version of filtered-out rows (max 100)."""
        count = mask.sum()
        if count > 0:
            available = [c for c in _LIGHT_COLS if c in df.columns]
            if available:
                filtered_out[label] = df.loc[mask, available].head(100).copy()
            else:
                filtered_out[label] = df.loc[mask, list(df.columns)[:5]].head(100).copy()
        return count

    df = df.copy()  # Avoid SettingWithCopyWarning

    # Filter helpers — module-level versions are used so they can be unit-tested
    # and so the same matching rules apply across every filter callsite.
    matches_list = _company_matches_filter_list

    def matches_list_in_text(text, company_list):
        """Check if any company from list appears in text - uses word boundary matching."""
        if pd.isna(text) or not str(text).strip():
            return False
        text_lower = str(text).lower()
        for c in company_list:
            c_norm = _normalize_company_name(c)
            if not c_norm or len(c_norm) < 3:
                continue
            pattern = r'\b' + re.escape(c_norm) + r'\b'
            if re.search(pattern, text_lower):
                return True
        return False

    # ========== EXCLUSION FILTERS ==========
    # Track skipped filters for diagnostics
    stats['_skipped_filters'] = []

    # 1. Past candidates filter (match by LinkedIn URL first, then by name)
    if filters.get('past_candidates_df') is not None:
        past_df = filters['past_candidates_df']
        df['_is_past'] = False

        # Helper to normalize LinkedIn URLs for comparison (slug-level)
        def normalize_linkedin_url(url):
            if pd.isna(url) or not url:
                return ''
            url = str(url).lower().strip().rstrip('/')
            if '/in/' in url:
                return url.split('/in/')[-1].split('/')[0].split('?')[0]
            return url

        # Case-insensitive column finder
        def _find_col_ci(col_list, candidates):
            col_lower = {c.lower(): c for c in col_list}
            for c in candidates:
                if c in col_list:
                    return c
                if c.lower() in col_lower:
                    return col_lower[c.lower()]
            return None

        # Find LinkedIn URL column in past_df — case-insensitive, covers common
        # export formats (HeyReach, LinkedIn, Instantly, SourcingX CSV)
        _url_col_candidates = [
            'LinkedIn', 'linkedin_url', 'LinkedIn URL', 'LinkedIn Profile URL',
            'linkedinUrl', 'profileUrl', 'profile_url', 'URL',
        ]
        linkedin_col = _find_col_ci(past_df.columns.tolist(), _url_col_candidates)

        # Try URL matching; fall back to public_url when linkedin_url is absent
        _df_url_col = 'linkedin_url' if 'linkedin_url' in df.columns else (
            'public_url' if 'public_url' in df.columns else None
        )
        if linkedin_col and _df_url_col:
            past_urls = {normalize_linkedin_url(u) for u in past_df[linkedin_col].dropna()} - {''}
            if past_urls:
                df['_norm_url'] = df[_df_url_col].apply(normalize_linkedin_url)
                df['_is_past'] = df['_norm_url'].isin(past_urls)
                df = df.drop(columns=['_norm_url'])

        # Helper to normalize names for comparison
        def normalize_name(name):
            if pd.isna(name) or not name:
                return ''
            name = str(name).lower().strip()
            name = ' '.join(name.split())
            import re
            name = re.sub(r'[^\w\s]', '', name)
            return name.strip()

        # Build past_names from the sheet.  Handles:
        #   • Combined Name column  (e.g. SourcingX export, Loxo)
        #   • Separate First Name / Last Name columns  (HeyReach, Filterly, Instantly)
        past_names = set()
        _name_col_candidates = ['Name', 'name', 'Full Name', 'fullName', 'Candidate Name']
        name_col = _find_col_ci(past_df.columns.tolist(), _name_col_candidates)
        _first_col = _find_col_ci(past_df.columns.tolist(), ['First Name', 'first_name', 'firstName'])
        _last_col  = _find_col_ci(past_df.columns.tolist(), ['Last Name',  'last_name',  'lastName'])

        if name_col:
            past_names.update(
                normalize_name(n) for n in past_df[name_col].dropna() if str(n).strip()
            )
        if _first_col and _last_col:
            for _, row in past_df[[_first_col, _last_col]].iterrows():
                combined = normalize_name(f"{row[_first_col]} {row[_last_col]}")
                if combined:
                    past_names.add(combined)
        elif _first_col and not name_col:
            # Only first name available — still worth trying
            past_names.update(
                normalize_name(n) for n in past_df[_first_col].dropna() if str(n).strip()
            )
        past_names.discard('')

        if past_names:
            if 'name' in df.columns:
                df['_norm_name'] = df['name'].apply(normalize_name)
                df['_is_past'] = df['_is_past'] | df['_norm_name'].isin(past_names)
                df = df.drop(columns=['_norm_name'])
            if 'first_name' in df.columns and 'last_name' in df.columns:
                df['_full_name'] = df.apply(
                    lambda r: normalize_name(f"{r.get('first_name', '')} {r.get('last_name', '')}"),
                    axis=1
                )
                df['_is_past'] = df['_is_past'] | df['_full_name'].isin(past_names)
                df = df.drop(columns=['_full_name'])

        stats['past_candidates'] = df['_is_past'].sum()
        _store_filtered('Past Candidates', df['_is_past'])
        df = df[~df['_is_past']].drop(columns=['_is_past'])

        _had_url_col  = linkedin_col is not None
        _had_name_col = name_col is not None or _first_col is not None
        if stats['past_candidates'] == 0 and not _had_url_col and not _had_name_col:
            stats['_skipped_filters'].append(
                f"past_candidates (no LinkedIn/Name column found — "
                f"sheet columns: {list(past_df.columns[:8])})"
            )
        elif stats['past_candidates'] == 0:
            stats['_skipped_filters'].append(
                f"past_candidates (0 matches — "
                f"URL col: {linkedin_col or 'none'}, "
                f"name col: {name_col or ((_first_col and _last_col) and 'First+Last') or 'none'})"
            )

    # 2. Blacklist filter
    if filters.get('blacklist'):
        blacklist = [c.lower().strip() for c in filters['blacklist']]
        if 'current_company' in df.columns:
            df['_blacklisted'] = df['current_company'].apply(lambda x: matches_list(x, blacklist))
            stats['blacklist'] = df['_blacklisted'].sum()
            _store_filtered('Blacklist Companies', df['_blacklisted'])
            df = df[~df['_blacklisted']].drop(columns=['_blacklisted'])
        else:
            stats['_skipped_filters'].append('blacklist (missing current_company column)')

    # 3. Not relevant companies (current)
    if filters.get('not_relevant'):
        not_relevant = [c.lower().strip() for c in filters['not_relevant']]
        if 'current_company' in df.columns:
            df['_not_relevant'] = df['current_company'].apply(lambda x: matches_list(x, not_relevant))
            stats['not_relevant_current'] = df['_not_relevant'].sum()
            _store_filtered('Not Relevant (Current)', df['_not_relevant'])
            df = df[~df['_not_relevant']].drop(columns=['_not_relevant'])
        else:
            stats['_skipped_filters'].append('not_relevant (missing current_company column)')

    # 4. Exclude title keywords filter (word boundary matching)
    if filters.get('exclude_titles') and 'current_title' in df.columns:
        import re
        exclude_keywords = filters['exclude_titles']
        # Multi-word keywords use substring match, single/short words use word boundary
        exclude_patterns = []
        for kw in exclude_keywords:
            escaped = re.escape(kw)
            if ' ' in kw:
                # Multi-word phrases: substring match (e.g. "head of", "self employed")
                exclude_patterns.append(escaped)
            else:
                # Single words: word boundary match (e.g. "it" won't match "digital")
                exclude_patterns.append(r'\b' + escaped + r'\b')
        exclude_regex = re.compile('|'.join(exclude_patterns), re.IGNORECASE)

        def has_excluded_title(title):
            if pd.isna(title) or not str(title).strip():
                return False
            return bool(exclude_regex.search(str(title)))

        df['_excluded_title'] = df['current_title'].apply(has_excluded_title)
        stats['excluded_titles'] = df['_excluded_title'].sum()
        _store_filtered('Excluded Titles', df['_excluded_title'])
        df = df[~df['_excluded_title']].drop(columns=['_excluded_title'])

    # 5. Include title keywords filter (only keep matching, word boundary)
    if filters.get('include_titles') and 'current_title' in df.columns:
        import re
        include_keywords = filters['include_titles']
        include_patterns = []
        for kw in include_keywords:
            escaped = re.escape(kw)
            if ' ' in kw:
                include_patterns.append(escaped)
            else:
                include_patterns.append(r'\b' + escaped + r'\b')
        include_regex = re.compile('|'.join(include_patterns), re.IGNORECASE)

        def has_included_title(title):
            if pd.isna(title) or not str(title).strip():
                return False
            return bool(include_regex.search(str(title)))

        df['_included_title'] = df['current_title'].apply(has_included_title)
        not_included = ~df['_included_title']
        stats['not_matching_titles'] = not_included.sum()
        _store_filtered('Not Matching Titles', not_included)
        df = df[df['_included_title']].drop(columns=['_included_title'])

    # 6. Duration filters (from Phantom data)
    # Helper to parse duration to months - handles both numeric (years) and text formats
    def parse_duration_to_months(duration_val):
        if pd.isna(duration_val):
            return None

        # If it's already numeric (years as float from normalize_phantombuster_profile)
        if isinstance(duration_val, (int, float)):
            months = int(duration_val * 12)
            return months if months > 0 else None

        # Try to parse as numeric string first (e.g., "2.5")
        text = str(duration_val).strip()
        if not text:
            return None
        try:
            years = float(text)
            months = int(years * 12)
            return months if months > 0 else None
        except ValueError:
            pass

        # Parse text format like "2 years 3 months"
        text = text.lower()
        years = 0
        months = 0
        import re
        year_match = re.search(r'(\d+)\s*(?:year|yr)', text)
        if year_match:
            years = int(year_match.group(1))
        month_match = re.search(r'(\d+)\s*(?:month|mo)', text)
        if month_match:
            months = int(month_match.group(1))
        total = years * 12 + months
        return total if total > 0 else None

    # Check for duration columns (Phantom format) - flexible case-insensitive matching
    def find_duration_col(df, patterns):
        """Find first matching duration column from patterns (case-insensitive)."""
        cols_lower = {c.lower(): c for c in df.columns}
        for pattern in patterns:
            if pattern.lower() in cols_lower:
                return cols_lower[pattern.lower()]
        return None

    role_col = find_duration_col(df, ['durationInRole', 'duration_in_role', 'current_years_in_role', 'yearsInRole', 'years_in_role', 'Duration in role', 'duration in role', 'Time in role', 'time in role'])
    company_col = find_duration_col(df, ['durationInCompany', 'duration_in_company', 'current_years_at_company', 'yearsInCompany', 'years_at_company', 'Duration in company', 'duration in company', 'Time in company', 'time in company'])

    # Track if duration columns are missing
    if filters.get('min_role_months') and not role_col:
        stats['_skipped_filters'].append('min_role_months (no duration column found)')
    if filters.get('max_role_months') and not role_col:
        stats['_skipped_filters'].append('max_role_months (no duration column found)')
    if filters.get('min_company_months') and not company_col:
        stats['_skipped_filters'].append('min_company_months (no duration column found)')
    if filters.get('max_company_months') and not company_col:
        stats['_skipped_filters'].append('max_company_months (no duration column found)')

    # Min role duration
    if filters.get('min_role_months') and role_col:
        min_months = filters['min_role_months']
        df['_role_months'] = df[role_col].apply(parse_duration_to_months)
        df['_role_too_short'] = df['_role_months'].apply(lambda x: x < min_months if pd.notna(x) else False)
        stats['role_too_short'] = df['_role_too_short'].sum()
        _store_filtered('Role Too Short', df['_role_too_short'])
        df = df[~df['_role_too_short']].drop(columns=['_role_months', '_role_too_short'], errors='ignore')

    # Max role duration
    if filters.get('max_role_months') and role_col:
        max_months = filters['max_role_months']
        df['_role_months'] = df[role_col].apply(parse_duration_to_months)
        df['_role_too_long'] = df['_role_months'].apply(lambda x: x > max_months if pd.notna(x) else False)
        stats['role_too_long'] = df['_role_too_long'].sum()
        _store_filtered('Role Too Long', df['_role_too_long'])
        df = df[~df['_role_too_long']].drop(columns=['_role_months', '_role_too_long'], errors='ignore')

    # Min company duration
    if filters.get('min_company_months') and company_col:
        min_months = filters['min_company_months']
        df['_company_months'] = df[company_col].apply(parse_duration_to_months)
        df['_company_too_short'] = df['_company_months'].apply(lambda x: x < min_months if pd.notna(x) else False)
        stats['company_too_short'] = df['_company_too_short'].sum()
        _store_filtered('Company Too Short', df['_company_too_short'])
        df = df[~df['_company_too_short']].drop(columns=['_company_months', '_company_too_short'], errors='ignore')

    # Max company duration
    if filters.get('max_company_months') and company_col:
        max_months = filters['max_company_months']
        df['_company_months'] = df[company_col].apply(parse_duration_to_months)
        df['_company_too_long'] = df['_company_months'].apply(lambda x: x > max_months if pd.notna(x) else False)
        stats['company_too_long'] = df['_company_too_long'].sum()
        _store_filtered('Company Too Long', df['_company_too_long'])
        df = df[~df['_company_too_long']].drop(columns=['_company_months', '_company_too_long'], errors='ignore')

    # 7. Universities filter (keep only top university graduates - only for enriched data)
    if filters.get('universities') and 'education' in df.columns:
        universities = [u.lower().strip() for u in filters['universities']]

        def has_top_university(education):
            """Check if education matches a target university using stricter matching."""
            if pd.isna(education) or not str(education).strip():
                return False
            edu_lower = str(education).lower()
            # Split education into parts (may contain multiple schools)
            edu_parts = [p.strip() for p in edu_lower.replace('|', ',').split(',')]

            for edu_part in edu_parts:
                for uni in universities:
                    if not uni:
                        continue
                    # Exact match
                    if edu_part == uni:
                        return True
                    # Education part starts with university name
                    if edu_part.startswith(uni):
                        return True
                    # University name starts with education part (if part is substantial)
                    if uni.startswith(edu_part) and len(edu_part) > 10:
                        return True
                    # Word-based matching for longer names
                    if len(uni) > 8:
                        uni_words = set(uni.split())
                        edu_words = set(edu_part.split())
                        common = uni_words & edu_words
                        # Require 70%+ of university words to match
                        if len(common) >= len(uni_words) * 0.7:
                            return True
            return False

        df['_top_uni'] = df['education'].apply(has_top_university)
        not_top_uni = ~df['_top_uni']
        stats['not_top_university'] = not_top_uni.sum()
        _store_filtered('Not Top University', not_top_uni)
        df = df[df['_top_uni']].drop(columns=['_top_uni'])

    stats['original'] = original_count
    stats['final'] = len(df)
    stats['total_removed'] = original_count - len(df)

    # filtered_out already contains only lightweight display columns (via _store_filtered)
    return df, stats, filtered_out


# =============================================================================
# AI SCREENING (Simple Custom GPT Style)
# =============================================================================


# --- Pre-computation helpers (Python does date math, AI does decisions) ---

def _first_sentence(text):
    """Extract first sentence from text, max 200 chars."""
    if not text:
        return ''
    s = text.split('.')[0][:200]
    return s.strip() + '.' if s else ''


def _parse_date(d):
    """Parse ISO date string to datetime. Returns None on failure."""
    if not d:
        return None
    try:
        dt = datetime.fromisoformat(str(d))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ===== Performance: Cache for compute_role_durations =====
_duration_cache = {}
_DURATION_CACHE_MAX = 1000

def _hash_profile_for_cache(raw: dict) -> str:
    """Create a stable hash key for caching duration calculations."""
    key_parts = [
        raw.get('linkedin_url', '') or raw.get('linkedin_flagship_url', ''),
        str(len(raw.get('past_employers') or [])),
        str(len(raw.get('current_employers') or [])),
    ]
    return hashlib.md5('|'.join(key_parts).encode()).hexdigest()

def compute_role_durations_cached(raw: dict) -> str:
    """Cached wrapper for compute_role_durations - avoids recomputing for same profile."""
    if not isinstance(raw, dict):
        return ""

    cache_key = _hash_profile_for_cache(raw)
    if cache_key in _duration_cache:
        return _duration_cache[cache_key]

    result = compute_role_durations(raw)

    # LRU-style eviction when cache is full
    if len(_duration_cache) >= _DURATION_CACHE_MAX:
        keys_to_remove = list(_duration_cache.keys())[:100]
        for k in keys_to_remove:
            del _duration_cache[k]

    _duration_cache[cache_key] = result
    return result


def compute_role_durations(raw):
    """Pre-calculate role durations from raw Crustdata JSON.

    Python does ONLY date math. No role classification, no verdicts.
    Returns formatted text block with duration per role.
    """
    if not isinstance(raw, dict):
        return ""

    today = datetime.now(timezone.utc)
    lines = ['ROLE DURATIONS (pre-calculated, use these numbers):']
    has_roles = False

    def _fmt_duration(months):
        """Format months as 'Xy Ym' (or just 'Xm' if under a year)."""
        y, m = divmod(months, 12)
        if y == 0:
            return f"{m}m"
        return f"{y}y {m}m"

    def _role_end(emp, today):
        """Interpret a role's end_date, treating blank/"present"/"current"
        as CURRENT (runs to today) and a non-empty-but-unparseable end_date
        on a non-current role as UNKNOWN (returns end=None — do not inflate
        to today).
        """
        raw_end = emp.get('end_date')
        norm = str(raw_end).strip().lower() if raw_end is not None else ''
        is_current = (raw_end is None) or norm in ('', 'present', 'current', 'none', 'null')
        if is_current:
            return today, True
        parsed = _parse_date(raw_end)
        return (parsed, False) if parsed else (None, False)

    # Merged-interval company tenure (rehire gaps excluded, internal
    # promotions merged) — same logic used by tenure_constraint_validator's
    # max-tenure hard constraint (Fix 3), imported here so both stay in sync.
    from tenure_constraint_validator import merge_company_intervals_months

    # Track company-level data for stability summary
    companies = {}  # company_name -> {intervals: [(start,end)], max_end, is_current, titles}
    career_start = None  # Track earliest role start date

    for emp_key in ['past_employers', 'current_employers']:
        for emp in (raw.get(emp_key) or []):
            title = (emp.get('employee_title') or '').strip()
            if not title:
                continue
            has_roles = True
            company = emp.get('employer_name', '?')
            start = _parse_date(emp.get('start_date'))
            end, is_current = _role_end(emp, today)
            months = max(0, (end.year - start.year) * 12 + (end.month - start.month)) if start and end else 0
            start_str = start.strftime('%b %Y') if start else '?'
            if is_current:
                end_str = 'today'
            elif end:
                end_str = end.strftime('%b %Y')
            else:
                end_str = '?'
            dur = _fmt_duration(months)

            # Track career start date
            if start and (career_start is None or start < career_start):
                career_start = start

            if is_current:
                lines.append(f"  >>> {title} at {company}: {start_str} - {end_str} = {dur} <<< CURRENT ROLE")
            else:
                lines.append(f"  {title} at {company}: {start_str} - {end_str} = {dur}")

            # Group by company (also track titles for internship detection)
            if company not in companies:
                companies[company] = {'intervals': [], 'max_end': end, 'is_current': False, 'titles': []}
            companies[company]['intervals'].append((start, end))
            if end and (companies[company]['max_end'] is None or end > companies[company]['max_end']):
                companies[company]['max_end'] = end
            companies[company]['titles'].append(title.lower())
            if is_current:
                companies[company]['is_current'] = True

    if not has_roles:
        return ""

    # Compute stability summary
    short_companies = []
    excluded_early_career = []
    current_company_name = None
    current_company_months = None

    # Early career threshold: first 3 years of career OR more than 10 years ago
    early_career_cutoff = None
    ten_years_ago = today - timedelta(days=365 * 10)
    if career_start:
        early_career_cutoff = career_start + timedelta(days=365 * 3)  # 3 years into career

    # Internship keywords
    _intern_keywords = {'intern', 'internship', 'trainee', 'apprentice', 'student', 'associate'}

    for comp_name, comp_data in companies.items():
        # Sum of merged (start, end) intervals — rehire gaps are excluded
        # (two stints at the same company with a real gap between them do
        # NOT get bridged into one long tenure); adjacent/overlapping
        # intervals (internal promotions) still merge into one stint.
        total_months = merge_company_intervals_months(comp_data['intervals'])
        if comp_data['is_current']:
            # If multiple current companies, pick the longest tenure (primary role)
            if current_company_months is None or total_months > current_company_months:
                current_company_name = comp_name
                current_company_months = total_months
        elif total_months < 12:
            # Check if this is an internship (any title contains intern keywords)
            is_internship = any(any(kw in t for kw in _intern_keywords) for t in comp_data.get('titles', []))

            # Check if this is early career (ended before early_career_cutoff OR more than 10 years ago)
            is_early_career = False
            if comp_data['max_end']:
                if early_career_cutoff and comp_data['max_end'] < early_career_cutoff:
                    is_early_career = True
                elif comp_data['max_end'] < ten_years_ago:
                    is_early_career = True

            if is_internship or is_early_career:
                reason = "intern" if is_internship else "early career"
                excluded_early_career.append(f"{comp_name} ({_fmt_duration(total_months)}, {reason})")
            else:
                short_companies.append(f"{comp_name} ({_fmt_duration(total_months)})")

    lines.append('')
    lines.append('STABILITY SUMMARY (pre-calculated by company, use these numbers):')
    lines.append(f'  Short-stint companies (<12 months, excluding current/intern/early-career): {len(short_companies)}' +
                 (f' — {", ".join(short_companies)}' if short_companies else ''))
    if excluded_early_career:
        lines.append(f'  Excluded from short-stint count (intern/early career): {", ".join(excluded_early_career)}')
    if current_company_name:
        lines.append(f'  Current company: {current_company_name} = {_fmt_duration(current_company_months)}')
    else:
        lines.append('  Current company: none found')

    # Pre-computed STABILITY VERDICT — hard cap the AI must follow.
    # Short CURRENT-company tenure is intentionally NOT a cap here: per the
    # unified policy it is informational unless the recruiter stated a tenure
    # minimum, which is enforced separately and deterministically by
    # tenure_constraint_validator.enforce_tenure_constraint after the model.
    if len(short_companies) >= 3:
        lines.append(f'>>> STABILITY VERDICT: FAIL — {len(short_companies)} short-stint companies >= 3 → MAX SCORE 4 <<<')
    else:
        lines.append('>>> STABILITY VERDICT: PASS <<<')

    # Pre-computed EXPERIENCE SUMMARY — total career span and military detection.
    # Israeli military service (IDF / 8200 / Mamram / Talpiot / C4I / Hebrew
    # variants) is mandatory at age 18-21 and MUST NOT count toward any
    # user-stated "max N years experience" / "reject >N years" rule.
    # Detection is centralized in normalizers.is_military_position so the
    # keyword list (English + Hebrew + bare unit numbers) is the single
    # source of truth used by the regression test in
    # test_ai_screen_excludes_army.py.
    from normalizers import is_military_position
    all_starts = []
    all_starts_non_mil = []
    military_months = 0
    military_positions = []
    for emp_key in ['past_employers', 'current_employers']:
        for emp in (raw.get(emp_key) or []):
            title = (emp.get('employee_title') or '').strip()
            if not title:
                continue
            start = _parse_date(emp.get('start_date'))
            end, _is_current_mil = _role_end(emp, today)
            if not start:
                continue
            is_military = is_military_position(title, emp.get('employer_name'))
            all_starts.append(start)
            if is_military:
                mil_months = max(0, (end.year - start.year) * 12 + (end.month - start.month)) if end else 0
                military_months += mil_months
                military_positions.append(f"{title} at {emp.get('employer_name', '?')} ({_fmt_duration(mil_months)})")
            else:
                all_starts_non_mil.append(start)

    if all_starts:
        lines.append('')
        lines.append('EXPERIENCE SUMMARY (pre-calculated — DO NOT recalculate):')
        earliest = min(all_starts)
        total_months = max(0, (today.year - earliest.year) * 12 + (today.month - earliest.month))
        lines.append(f'  TOTAL CAREER SPAN: {_fmt_duration(total_months)} (from {earliest.strftime("%b %Y")} to today — includes military)')
        if military_months > 0:
            half_mil = military_months // 2
            lines.append(f'  MILITARY SERVICE: {_fmt_duration(military_months)} — {"; ".join(military_positions)}')
            lines.append(f'    → counts as HALF = {_fmt_duration(half_mil)} for role-specific experience')
            # Calculate industry experience as total minus military
            industry_months = max(0, total_months - military_months)
            lines.append(f'  INDUSTRY EXPERIENCE (excl military): {_fmt_duration(industry_months)}')
            lines.append('>>> EXPERIENCE LIMIT CHECK: MAXIMUM / ceiling rules ("max N years", "reject >N years") — compare against INDUSTRY EXPERIENCE above (civilian only), NEVER TOTAL CAREER SPAN. MINIMUM rules ("min N years", "N+ years") for a specific role/skill — count civilian role-relevant experience PLUS the HALF-credit military figure above, ONLY if that military service is relevant to the required skill; NEVER count military at full credit toward a minimum, and NEVER use TOTAL CAREER SPAN for either. <<<')
        else:
            lines.append(f'  INDUSTRY EXPERIENCE: {_fmt_duration(total_months)} (no military service detected)')
            lines.append('>>> EXPERIENCE LIMIT CHECK: MAXIMUM rules ("max N years", "reject >N years") — compare against INDUSTRY EXPERIENCE above, never TOTAL CAREER SPAN. MINIMUM rules ("min N years", "N+ years") for a specific role/skill — count the ROLE-RELEVANT experience from the ROLE DURATIONS above (no military service to credit here). <<<')
        lines.append('  NOTE: AI must determine which roles are role-specific from the durations above. The baseline above is a starting point.')

    # Detect SWE roles with DevOps skill overlap — flag for AI to consider half-credit
    _devops_skills = {'kubernetes', 'k8s', 'terraform', 'docker', 'ci/cd', 'jenkins', 'argocd',
                      'aws', 'amazon web services', 'gcp', 'azure', 'ansible', 'helm',
                      'infrastructure as code', 'iac', 'prometheus', 'grafana', 'datadog'}
    _swe_keywords = {'software engineer', 'software developer', 'full stack', 'fullstack', 'backend engineer', 'frontend engineer'}
    candidate_skills = {s.lower() for s in (raw.get('skills') or [])}
    devops_overlap = candidate_skills & _devops_skills
    if devops_overlap:
        swe_roles = []
        for emp_key in ['past_employers', 'current_employers']:
            for emp in (raw.get(emp_key) or []):
                title = (emp.get('employee_title') or '').strip()
                if not title:
                    continue
                title_lower = title.lower()
                if is_military_position(title, emp.get('employer_name')):
                    continue
                if any(kw in title_lower for kw in _swe_keywords):
                    start = _parse_date(emp.get('start_date'))
                    end, _is_current_swe = _role_end(emp, today)
                    months = max(0, (end.year - start.year) * 12 + (end.month - start.month)) if start and end else 0
                    swe_roles.append(f'{title} at {emp.get("employer_name", "?")} ({_fmt_duration(months)})')
        if swe_roles:
            lines.append('')
            lines.append(f'SWE HALF-CREDIT CHECK: candidate has Software Engineer roles AND DevOps skills ({", ".join(list(devops_overlap)[:5])}):')
            for role in swe_roles:
                lines.append(f'  → {role} — count as HALF if role involved DevOps/infrastructure work')

    return '\n'.join(lines)


def trim_raw_profile(raw):
    """Create trimmed copy of raw Crustdata JSON for AI screening.

    Removes logos, IDs, long company descriptions (keeps first sentence).
    Skips employer entries with empty titles.
    Does NOT modify the original dict.
    """
    if not isinstance(raw, dict):
        return raw

    trimmed = {}
    for key in ['name', 'title', 'headline', 'location', 'summary', 'skills',
                'languages', 'all_titles', 'all_employers', 'all_schools',
                'all_degrees', 'linkedin_flagship_url', 'num_of_connections']:
        if key in raw:
            trimmed[key] = raw[key]

    for emp_key in ['past_employers', 'current_employers']:
        if emp_key in raw:
            trimmed_emps = []
            for emp in (raw[emp_key] or []):
                title = (emp.get('employee_title') or '').strip()
                if not title:
                    continue
                trimmed_emps.append({
                    'employee_title': title,
                    'employer_name': emp.get('employer_name'),
                    'start_date': emp.get('start_date'),
                    'end_date': emp.get('end_date'),
                    'employee_description': emp.get('employee_description') or None,
                    'employer_description': _first_sentence(emp.get('employer_linkedin_description'))
                })
            trimmed[emp_key] = trimmed_emps

    if 'education_background' in raw:
        trimmed['education'] = [
            {'school': e.get('institute_name'), 'degree': e.get('degree_name'), 'field': e.get('field_of_study')}
            for e in raw['education_background']
        ]

    return trimmed


def _extract_json_from_text(text):
    """Extract JSON from text, stripping markdown code fences if present."""
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1] if '\n' in text else text[3:]
    if text.endswith('```'):
        text = text[:-3].strip()
    start = text.find('{')
    end = text.rfind('}') + 1
    if start >= 0 and end > start:
        return text[start:end]
    return text


GO_CONFIDENCE_THRESHOLD = 7  # GO-decision at/above this score = confident GO; below = Maybe


def _decision_to_fit_label(decision: str, score: int) -> str:
    """Map a screening {decision, score} to one recruiter-facing bucket.

    The decision is structural — a NO GO means a must-have failed or an
    exclusion matched. The score then splits the GO-decisions into confident
    GOs vs. fence-sitters worth a human glance:
      - NO GO decision           → "Not a Fit"  (skip)
      - GO decision, score >= 7  → "Good Fit"   (act — confident GO)
      - GO decision, score <= 6  → "Maybe"      (glance — cleared the bar, low confidence)

    A failed must-have can never be a "Maybe": if the decision is NO GO the
    bucket is "Not a Fit" regardless of score. The internal label strings
    ("Good Fit"/"Maybe"/"Not a Fit") are unchanged so existing filters keep
    working; the recruiter-facing display maps them to GO/MAYBE/NO GO."""
    is_go = str(decision).upper().strip() == "GO"
    if not is_go:
        return "Not a Fit"
    try:
        s = int(score or 0)
    except (TypeError, ValueError):
        s = 0
    return "Good Fit" if s >= GO_CONFIDENCE_THRESHOLD else "Maybe"


def _tri(v):
    """Tri-state truthiness for a verdict field ('met'/'matched'). Returns
    True/False ONLY for explicit, unambiguous signals; anything else (missing
    key, None, empty string, an unrecognized string, or any other type) comes
    back as None ("unknown") so callers never mistake a malformed/incomplete
    verdict for an explicit one."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v == 1:
            return True
        if v == 0:
            return False
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ('true', 'yes', 'y', '1'):
            return True
        if s in ('false', 'no', 'n', '0'):
            return False
        return None
    return None


def _verdicts_force_no_go(must_have_verdicts, exclusion_verdicts):
    """Return (force_no_go: bool, reason: str). True when the model's own
    per-criterion verdicts EXPLICITLY contradict a GO: any must-have with an
    explicit met=false, or any exclusion with an explicit matched=true. A
    missing or ambiguous field (_tri(...) is None) is never treated as a
    contradiction -- only an explicit signal from the model may force an
    override, so a malformed/incomplete verdict can never wrongly flip a
    legitimate GO into a NO GO. Conservative: if there are NO verdicts at all
    (empty/None), return (False, '') so we never override a decision the
    model made without emitting verdicts."""
    failed = [str(m.get('text', '')).strip() for m in (must_have_verdicts or [])
              if isinstance(m, dict) and _tri(m.get('met')) is False]
    matched = [str(e.get('text', '')).strip() for e in (exclusion_verdicts or [])
               if isinstance(e, dict) and _tri(e.get('matched')) is True]
    if not (must_have_verdicts or exclusion_verdicts):
        return (False, '')
    if failed or matched:
        bits = []
        if failed:
            bits.append('must-have(s) not met: ' + '; '.join(x for x in failed if x))
        if matched:
            bits.append('exclusion(s) matched: ' + '; '.join(x for x in matched if x))
        return (True, ' | '.join(bits))
    return (False, '')


_DEFAULT_USER_REQUEST = "Senior software engineering candidate. Apply the standard policy."


def _screening_api_call(client, ai_provider, ai_model, system_prompt, user_prompt,
                        max_tokens, tracker, start_time, profiles_screened=1):
    """One screening AI call. Handles OpenAI and Anthropic (Anthropic gets
    prompt caching on the system prompt + 429 retry with exponential backoff).
    Returns the parsed JSON dict; raises on failure.

    Pass profiles_screened=0 for a secondary call on the same profile (e.g. the
    nice-to-have bonus pass) so the profile isn't double-counted in usage stats
    while its tokens are still logged for cost tracking.
    """
    if ai_provider == "anthropic":
        # System prompt is identical for every profile in a run, so caching it
        # cuts cost dramatically for all calls after the first.
        _anthropic_delay = 2
        for _attempt in range(3):
            try:
                response = client.messages.create(
                    model=ai_model,
                    max_tokens=max(max_tokens, 400),
                    system=[{
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_prompt}],
                    temperature=0,
                )
                break  # success
            except Exception as _e:
                if '429' in str(_e) and _attempt < 2:
                    time.sleep(_anthropic_delay)
                    _anthropic_delay *= 2  # 2s → 4s → give up
                else:
                    raise
        if tracker and hasattr(response, 'usage') and response.usage:
            usage = response.usage
            tracker.log_openai(
                tokens_input=usage.input_tokens,  # Anthropic bills net of cache
                tokens_output=usage.output_tokens,
                model=ai_model,
                profiles_screened=profiles_screened,
                status='success',
                response_time_ms=int((time.time() - start_time) * 1000),
            )
        return json.loads(_extract_json_from_text(response.content[0].text))

    # OpenAI
    response = client.chat.completions.create(
        model=ai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    if tracker and hasattr(response, 'usage') and response.usage:
        tracker.log_openai(
            tokens_input=response.usage.prompt_tokens,
            tokens_output=response.usage.completion_tokens,
            model=ai_model,
            profiles_screened=profiles_screened,
            status='success',
            response_time_ms=int((time.time() - start_time) * 1000),
        )
    return json.loads(_extract_json_from_text(response.choices[0].message.content))


def screen_profile(profile: dict, job_description: str, client,
                   tracker: 'UsageTracker' = None, mode: str = "detailed",
                   ai_model: str = "gpt-4.1-mini",
                   ai_provider: str = "openai", user_request: str = None,
                   screening_brief: dict = None) -> dict:
    """Screen a profile against a job's requirements using OpenAI or Anthropic.

    Args:
        profile: Profile dict (must have raw_crustdata or raw_data for best results)
        job_description: Legacy freeform job requirements text
        client: OpenAI or Anthropic client instance
        tracker: Optional usage tracker
        mode: "quick" for cheaper/faster or "detailed" for full analysis
        ai_model: Model to use (e.g. gpt-4.1-mini, claude-haiku-4-5-20251001)
        ai_provider: "openai" or "anthropic"
        user_request: Legacy freeform recruiter request text. Used only when
            ``screening_brief`` is not supplied.
        screening_brief: Structured brief dict with keys:
            ``role_context`` (str), ``must_haves`` (list[str]),
            ``nice_to_haves`` (list[str]), ``exclusions`` (list[str]).
            When provided, screening uses the structured per-criterion path:
            a screening call that returns an explicit verdict on each must-have
            and exclusion, plus a SEPARATE nice-to-have bonus pass (so a
            nice-to-have can never cause a NO GO). When None, falls back to the
            legacy freeform path for non-UI callers.
    """
    start_time = time.time()

    # Get raw data
    raw = profile.get('raw_crustdata') or profile.get('raw_data') or profile
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = profile

    # Validate we have something to screen
    name = raw.get('name') or profile.get('name') or 'Unknown'
    if not raw.get('current_employers') and not raw.get('past_employers') and not raw.get('skills'):
        # Try to construct minimal profile from flat fields
        if profile.get('current_title') or profile.get('current_company'):
            raw = {
                'name': name,
                'current_title': profile.get('current_title'),
                'current_company': profile.get('current_company'),
                'skills': profile.get('skills', []),
                'linkedin_url': profile.get('linkedin_url')
            }
        else:
            return {
                "score": 0,
                "fit": "Skipped",
                "summary": "Insufficient profile data"
            }

    # Pre-compute durations and trim raw JSON (creates new objects, doesn't modify originals)
    durations_text = compute_role_durations_cached(raw)
    trimmed_raw = trim_raw_profile(raw)

    try:
        if screening_brief:
            # ---- Structured per-criterion path ----
            role_context = screening_brief.get("role_context", "") or ""
            must_haves = screening_brief.get("must_haves", []) or []
            nice_to_haves = screening_brief.get("nice_to_haves", []) or []
            exclusions = screening_brief.get("exclusions", []) or []

            # Screening call: per-criterion verdicts + decision + score.
            scr = _screening_api_call(
                client, ai_provider, ai_model,
                _structured_system_prompt(),
                _structured_user_prompt(role_context, must_haves, exclusions,
                                        durations_text, trimmed_raw),
                max_tokens=800, tracker=tracker, start_time=start_time,
                profiles_screened=1,
            )

            # Separate nice-to-have bonus pass — isolated so any failure here
            # cannot affect the GO/NO GO result already stored in `scr`.
            bonus_tags = []
            if any(str(n).strip() for n in nice_to_haves):
                try:
                    nice = _screening_api_call(
                        client, ai_provider, ai_model,
                        _NICE_TO_HAVE_SYSTEM,
                        _nice_to_have_prompt(nice_to_haves, trimmed_raw),
                        max_tokens=400, tracker=tracker, start_time=start_time,
                        profiles_screened=0,  # same profile — don't double-count
                    )
                    bonus_tags = [
                        str(n.get("text", "")).strip()
                        for n in nice.get("nice_to_haves", [])
                        if n.get("met") is True and str(n.get("text", "")).strip()
                    ]
                except Exception:
                    bonus_tags = []  # bonus pass failed — preserve main result

            decision = str(scr.get("decision", "NO GO")).upper().strip()
            score = int(scr.get("score", 0) or 0)
            reasoning = str(scr.get("reasoning", "") or "")
            result = {
                "score": score,
                "fit": _decision_to_fit_label(decision, score),
                "summary": reasoning,
                "decision": decision,
                "reasoning": reasoning,
                "must_have_verdicts": scr.get("must_haves", []),
                "exclusion_verdicts": scr.get("exclusions", []),
                "bonus_tags": bonus_tags,
            }

            # Deterministic safety catch: the model returns a per-criterion
            # verdict on every must-have/exclusion AND a top-level decision,
            # but nothing previously checked the two against each other — a
            # model could mark a must-have "met": false yet still say
            # "decision": "GO". Enforce the rule the prompt already states:
            # GO only when every must-have is met and no exclusion matched.
            # Never flips a NO GO into a GO — only tightens.
            force_no_go, guard_reason = _verdicts_force_no_go(
                result["must_have_verdicts"], result["exclusion_verdicts"]
            )
            if force_no_go and decision == "GO":
                decision = "NO GO"
                result["decision"] = "NO GO"
                result["fit"] = _decision_to_fit_label("NO GO", score)
                override_note = (
                    f"Auto-NO GO (model verdicts contradicted its GO): "
                    f"{guard_reason}. {reasoning}"
                )
                result["reasoning"] = override_note
                result["summary"] = override_note
                result["decision_guard_override"] = True

            constraint_text = "\n".join(
                [str(m) for m in must_haves] + [str(e) for e in exclusions]
            )
        else:
            # ---- Legacy freeform path (non-UI callers) ----
            effective_user_request = user_request or job_description or _DEFAULT_USER_REQUEST
            result = _screening_api_call(
                client, ai_provider, ai_model,
                _policy_system_prompt(),
                _policy_user_prompt(effective_user_request, durations_text, trimmed_raw),
                max_tokens=400, tracker=tracker, start_time=start_time,
            )
            decision = str(result.get("decision", "NO GO")).upper().strip()
            score = int(result.get("score", 0) or 0)
            reasoning = str(result.get("reasoning", "") or "")
            result = {
                "score": score,
                "fit": _decision_to_fit_label(decision, score),
                "summary": reasoning,
                "decision": decision,
                "reasoning": reasoning,
            }
            constraint_text = "\n".join(
                t for t in (user_request, job_description) if t
            )

        # Deterministic post-screening: tenure hard-constraint override.
        # Strengthening the prompt is not enough — the model can still trade
        # the rule off. This forces NO GO / Not a Fit when the recruiter
        # asked for a minimum tenure and the candidate is below it.
        # See tenure_constraint_validator.py (Issue 7, Chen).
        try:
            from tenure_constraint_validator import (
                enforce_tenure_constraint,
                enforce_max_tenure_constraint,
            )
            result = enforce_tenure_constraint(result, constraint_text, raw)
            # Mirror check: recruiter-stated MAXIMUM tenure at one company
            # (e.g. "no more than 5 years at one company"). Same
            # deterministic, post-model override pattern as the minimum
            # check above. See tenure_constraint_validator.py.
            result = enforce_max_tenure_constraint(result, constraint_text, raw)
        except Exception:
            # Validator must NEVER crash screening. On any error, fall
            # through with the original model result.
            pass

        return result

    except json.JSONDecodeError as e:
        return {
            "score": 0,
            "fit": "Error",
            "summary": f"JSON parse error: {str(e)[:80]}"
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        if tracker:
            tracker.log_openai(
                tokens_input=0, tokens_output=0, model=ai_model,
                profiles_screened=0, status='error',
                error_message=str(e)[:200], response_time_ms=elapsed_ms
            )
        return {
            "score": 0,
            "fit": "Error",
            "summary": f"API error: {str(e)[:80]}"
        }

def _ensure_raw_dict(raw):
    """Parse raw_data if it's a JSON string, return dict or empty dict."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def build_raw_data_index(enriched_results: list) -> dict:
    """Build a URL->raw_data index from enriched_results (call once at screening start).

    Returns:
        Dict mapping linkedin_url variants to raw_data dicts
    """
    raw_by_url = {}
    if not enriched_results:
        return raw_by_url

    for ep in enriched_results:
        raw = ep.get('raw_data') or ep.get('raw_crustdata')
        if not raw:
            continue
        parsed_raw = _ensure_raw_dict(raw) if not isinstance(raw, dict) else raw
        if not parsed_raw:
            continue
        # Index under all possible URL variants
        for url_key in ['linkedin_flagship_url', 'linkedin_url', 'linkedin_profile_url']:
            for source in [ep, parsed_raw]:
                url = source.get(url_key, '') if isinstance(source, dict) else ''
                if url:
                    raw_by_url[url] = parsed_raw
    return raw_by_url


def fetch_raw_data_for_batch(profiles: list, raw_index: dict = None, db_client = None) -> None:
    """Fetch raw_data for a batch of profiles (memory-efficient, on-demand).

    Modifies profiles in-place to add raw_data/raw_crustdata.
    Only fetches for profiles that don't already have raw_data.

    Args:
        profiles: List of profile dicts to fetch raw_data for
        raw_index: Pre-built URL->raw_data index (from build_raw_data_index)
        db_client: Optional database client for fetching from DB
    """
    # Find profiles missing raw_data
    missing = [p for p in profiles if not p.get('raw_crustdata') and not p.get('raw_data')]
    if not missing:
        return

    # FIRST: Use pre-built index (fast lookup)
    if raw_index:
        for p in missing:
            url = p.get('linkedin_url', '')
            if url and url in raw_index:
                p['raw_crustdata'] = raw_index[url]

    # SECOND: Fetch from DB for still-missing profiles
    still_missing = [p for p in profiles if not p.get('raw_crustdata') and not p.get('raw_data')]
    if still_missing and db_client:
        missing_urls = [p.get('linkedin_url', '') for p in still_missing if p.get('linkedin_url')]
        if missing_urls:
            db_raw_by_url = {}
            # Try batch fetch first (faster), fall back to individual fetches
            try:
                # Quote URLs to handle special chars in Supabase 'in' filter
                quoted_urls = [f'"{u}"' for u in missing_urls]
                url_filter = ','.join(quoted_urls)
                db_profiles = db_client.select('profiles', 'linkedin_url,raw_data',
                                               {'linkedin_url': f'in.({url_filter})'},
                                               limit=len(missing_urls))
                for dp in db_profiles:
                    if dp.get('linkedin_url'):
                        db_raw_by_url[dp['linkedin_url']] = _ensure_raw_dict(dp.get('raw_data'))
            except Exception as e:
                print(f"[Screening] Batch fetch failed ({e}), trying individual fetches...")

            # Fall back: fetch individually for any still missing
            fetched_count = 0
            for p in still_missing:
                url = p.get('linkedin_url', '')
                if url and url not in db_raw_by_url:
                    try:
                        from db import get_profile
                        db_profile = get_profile(db_client, url)
                        if db_profile and db_profile.get('raw_data'):
                            db_raw_by_url[url] = _ensure_raw_dict(db_profile['raw_data'])
                            fetched_count += 1
                    except Exception:
                        pass

            # Apply fetched raw data to profiles
            applied = 0
            for p in still_missing:
                url = p.get('linkedin_url', '')
                if url and url in db_raw_by_url and db_raw_by_url[url]:
                    p['raw_crustdata'] = db_raw_by_url[url]
                    applied += 1
            if applied < len(still_missing):
                print(f"[Screening] Warning: {len(still_missing) - applied} profiles still missing raw data after DB fetch")


# How long to wait before re-attempting Crustdata enrichment for a profile
# that was already validated-and-saved but came back with neither skills
# nor a summary (Codex review, PR #127, round 3, HIGH: without this, the
# thin-profile auto-enrich step re-buys the exact same empty result on
# every single screening run, forever — observed live, a person with 50
# skills stored got zero skills back from one enrich call, and nothing
# stopped the next run from paying to ask again). Long enough that a
# normal day of screening runs against the same JD doesn't hammer
# Crustdata for data it just told us it doesn't have; short enough that
# if Crustdata's own data for that person improves later, we notice
# within a day or two rather than never.
THIN_PROFILE_REENRICH_COOLDOWN_HOURS = 24

# Marker key stamped into a profile's flat enriched dict (which flows
# straight into profiles.raw_data via save_enriched_profiles_bulk — no
# new `profiles` column needed, matching this repo's existing
# underscore-prefixed in-raw_data marker convention: _needs_enrichment,
# _semantic_incomplete). NOT the same thing as the `profiles.
# enrichment_attempted_at` COLUMN — that column exists but is not
# populated by the current RPC-based save path (_prepare_profile_payload
# in db.py doesn't include it, verified 2026-08-04), and db.py is off
# limits for this PR, so a raw_data marker is the only available
# persistence point for this signal.
_THIN_LAST_ENRICH_ATTEMPT_KEY = '_last_enrichment_attempt'


def _thin_profile_on_cooldown(raw: dict) -> bool:
    """True if `raw` (a profile's raw_crustdata/raw_data dict) carries a
    recent _last_enrichment_attempt marker — i.e. we already validated
    and saved an enrichment attempt for this exact profile within
    THIN_PROFILE_REENRICH_COOLDOWN_HOURS and it came back without
    skills/summary. Returns False (never skip) when the marker is
    missing or unparseable — a profile that's never been marked, or
    whose marker we can't make sense of, is treated as never-attempted
    and stays eligible for enrichment. This must never suppress a
    genuinely never-enriched profile.
    """
    if not isinstance(raw, dict):
        return False
    marker = raw.get(_THIN_LAST_ENRICH_ATTEMPT_KEY)
    if not marker:
        return False
    try:
        attempted_at = datetime.fromisoformat(str(marker))
    except (TypeError, ValueError):
        return False
    return datetime.utcnow() - attempted_at < timedelta(hours=THIN_PROFILE_REENRICH_COOLDOWN_HOURS)


def enrich_thin_profiles_for_batch(profiles: list, api_key: str, db_client=None,
                                    tracker: 'UsageTracker' = None) -> dict:
    """Top up any profile in this batch that's missing BOTH skills and a
    summary (About text) via Crustdata's new batch-enrich endpoint, right
    before AI Screen sees it.

    Why this exists: the description-search beta (search_people_semantic,
    via semantic_profile_to_legacy_shape) doesn't return skills/summary —
    only the regular filter search (search_people_db, compact=false) does.
    This is the auto-fill step that keeps AI Screen quality unchanged
    regardless of which search sourced a profile — it's endpoint-agnostic,
    so it also covers any future search path (e.g. a filter-search
    migration) that returns thin profiles, without needing changes here.

    Detection is AND (only enrich when BOTH skills and summary are missing,
    not just one) and unmatched profiles are screened as-is rather than
    blocking the batch — both explicit calls Alexey made, 2026-07-20, when
    this feature was planned. No concurrency guard against two sessions
    enriching the same profile at once (same conversation: accepted as a
    small, rare risk rather than adding a re-check-before-submit step).

    Call this unconditionally for every screening batch — do NOT gate it
    behind "only if the raw-data fetch step reported something missing".
    Profiles can already be thin in memory (e.g. straight from the
    description-search beta) with nothing upstream flagging that; this
    function's own thin-detection below is what has to catch them, and it
    only runs if it's actually called every time.

    Mutates `profiles` in place: on a match, replaces raw_crustdata (and
    clears any stale raw_data) with the enriched flat profile, so
    screen_profile() reads the rich version. Also saves every newly-enriched
    profile back to Supabase immediately (via the same save_enriched_profiles_bulk
    path search-save already uses), so the same person is never re-enriched
    for a future JD.

    Returns a stats dict for the UI: {thin_found, enriched, unmatched, unknown,
    on_cooldown, on_cooldown_urls, credits_used}.
    `unknown` (Codex review, PR #127, round 2, 2026-08-04) counts profiles whose
    Crustdata batch outcome couldn't be confirmed (poll/download failure after
    the job was accepted) — distinct from `unmatched` (a confirmed no-match).
    Both are screened as-is; `unknown` also flips the usage-log status to
    'error' below, since a job we lost track of may have already billed.
    `on_cooldown` / `on_cooldown_urls` (Codex review, PR #127, round 3,
    HIGH) count/list profiles skipped because they were already validated
    and saved recently and STILL came back without skills/summary — see
    THIN_PROFILE_REENRICH_COOLDOWN_HOURS. Surfaced rather than hidden so
    someone can find and explicitly retry them later if needed.
    """
    stats = {
        'thin_found': 0, 'enriched': 0, 'unmatched': 0, 'unknown': 0,
        'on_cooldown': 0, 'on_cooldown_urls': [], 'credits_used': 0,
    }

    thin_profiles = []
    for p in profiles:
        if not isinstance(p, dict):
            continue
        url = p.get('linkedin_url')
        if not url:
            continue
        # Fall back to the profile's own top-level flat skills/summary (e.g. a
        # CSV-imported row with no nested raw blob at all) before deciding a
        # profile is thin — mirrors screen_profile()'s own fallback ("Try to
        # construct minimal profile from flat fields") a few lines below.
        # Without this, a profile with no raw blob but perfectly usable flat
        # fields gets misclassified as thin and enriched needlessly (Codex
        # review, 2026-07-20).
        # clean_value() coerces pandas/CSV NaN to None — a DataFrame-derived
        # profile's missing skills/summary column is often literal
        # float('nan'), and bool(float('nan')) is True in Python, so a raw
        # truthiness check would treat a genuinely-missing value as present
        # and skip enrichment for a profile that actually needs it (Codex
        # review, 2026-07-20).
        raw = _ensure_raw_dict(p.get('raw_crustdata') or p.get('raw_data'))
        skills = clean_value(raw.get('skills')) or clean_value(p.get('skills'))
        summary = clean_value(raw.get('summary')) or clean_value(p.get('summary'))
        if not skills and not summary:
            if _thin_profile_on_cooldown(raw):
                # Already tried recently, already saved, still came back
                # empty — don't re-buy the same answer every run.
                stats['on_cooldown'] += 1
                stats['on_cooldown_urls'].append(url)
                continue
            thin_profiles.append(p)

    stats['thin_found'] = len(thin_profiles)
    if not thin_profiles:
        return stats

    urls = [p['linkedin_url'] for p in thin_profiles]

    try:
        result = batch_enrich_profiles(urls, api_key=api_key)
    except Exception as e:
        # Never let an enrichment failure block screening — every thin
        # profile just stays thin and screens as-is.
        if tracker:
            tracker.log_crustdata_batch_enrich(
                requested=len(urls), fulfilled=0, status='error',
                error_message=str(e)[:200],
            )
        stats['unmatched'] = len(thin_profiles)
        return stats

    by_url = result.get('by_url') or {}
    # Representative urls whose Crustdata batch outcome is indeterminate
    # (poll/download failure after the job was accepted) — see
    # batch_enrich_profiles()'s docstring. Distinct from a confirmed
    # no-match: the job may have already billed.
    unknown_norms = {normalize_linkedin_url(u) or u for u in (result.get('unknown') or [])}
    newly_enriched = []  # flat Crustdata-shaped dicts — passed directly to
                          # save_enriched_profiles_bulk(), NOT wrapped, since
                          # that function (via _prepare_profile_row) reads
                          # name/skills/employer fields straight off each dict.
    original_url_map = {}

    for p in thin_profiles:
        url = p['linkedin_url']
        flat = by_url.get(url) or by_url.get(normalize_linkedin_url(url))
        if not flat:
            if (normalize_linkedin_url(url) or url) in unknown_norms:
                stats['unknown'] += 1
            else:
                stats['unmatched'] += 1
            continue
        # Codex review, PR #127, round 3 (HIGH): the identity/content gate
        # already confirmed this IS the right person with SOME real data —
        # but Crustdata may still have nothing for skills/summary
        # specifically. Stamp the cooldown marker so the NEXT screening
        # run doesn't pay to ask again for data we just confirmed isn't
        # there. Only stamped when still genuinely thin — a profile that
        # came back WITH skills or a summary is not marked at all, since
        # it's no longer thin and thin-detection won't reconsider it
        # anyway.
        if not (clean_value(flat.get('skills')) or clean_value(flat.get('summary'))):
            flat[_THIN_LAST_ENRICH_ATTEMPT_KEY] = datetime.utcnow().isoformat()
        p['raw_crustdata'] = flat
        p.pop('raw_data', None)
        stats['enriched'] += 1
        newly_enriched.append(flat)
        norm_url = normalize_linkedin_url(flat.get('linkedin_flagship_url') or url)
        if norm_url:
            original_url_map[norm_url] = url

    stats['credits_used'] = result.get('credits_used', 0)

    if tracker:
        unknown_count = len(result.get('unknown') or [])
        if unknown_count:
            # A job whose outcome we couldn't confirm may have already
            # billed — must not be logged as a clean, free success (Codex
            # review, PR #127, round 2, 2026-08-04).
            tracker.log_crustdata_batch_enrich(
                requested=result.get('requested', len(urls)),
                fulfilled=result.get('fulfilled', 0),
                status='error',
                error_message=(
                    f"{unknown_count} of {result.get('requested', len(urls))} profiles have "
                    f"unknown Crustdata billing status (batch poll/download failure) — "
                    f"batch_ids: {result.get('batch_ids')}"
                ),
            )
        else:
            tracker.log_crustdata_batch_enrich(
                requested=result.get('requested', len(urls)),
                fulfilled=result.get('fulfilled', 0),
                status='success',
            )

    if newly_enriched and db_client:
        try:
            save_enriched_profiles_bulk(
                db_client, newly_enriched,
                original_url_map=original_url_map,
                batch_size=100,
            )
        except Exception as e:
            print(f"[Screening] Warning: failed to save batch-enriched profiles ({e})")

    return stats


def screen_profiles_batch(profiles: list, job_description: str, openai_api_key: str,
                          max_workers: int = 15,
                          progress_callback=None, cancel_flag=None, mode: str = "detailed",
                          ai_model: str = "gpt-4.1-mini",
                          ai_provider: str = "openai", api_key: str = None,
                          user_request: str = None, screening_brief: dict = None) -> list:
    """Screen multiple profiles in parallel using ThreadPoolExecutor.

    Args:
        profiles: List of profile dicts to screen
        job_description: The job description to screen against
        openai_api_key: OpenAI API key (backward compat, use api_key for new code)
        max_workers: Number of concurrent threads
        progress_callback: Function(completed, total, result) called after each profile
        cancel_flag: Dict with 'cancelled' key to check for cancellation
        mode: "quick" for cheaper/faster or "detailed" for full analysis
        ai_model: Model to use
        ai_provider: "openai" or "anthropic"
        api_key: API key for the selected provider (overrides openai_api_key)
        user_request: Legacy freeform recruiter request text routed to ``screen_profile``.
        screening_brief: Structured brief dict (role_context / must_haves /
            nice_to_haves / exclusions). When provided, every profile is screened
            via the structured per-criterion path. See ``screen_profile``.

    Returns:
        List of screening results with profile info included
    """
    results = []
    completed_count = [0]  # Use list to allow modification in nested function
    lock = threading.Lock()
    total = len(profiles)

    # Get usage tracker (will be shared across threads)
    tracker = get_usage_tracker()

    def screen_single(profile, index):
        # Check cancellation before starting
        if cancel_flag and cancel_flag.get('cancelled'):
            return None

        # Create client per thread to avoid thread-safety issues
        _key = api_key or openai_api_key
        if ai_provider == "anthropic":
            client = anthropic.Anthropic(api_key=_key)
        else:
            client = OpenAI(api_key=_key)

        try:
            # Retry logic with exponential backoff for rate limits
            max_retries = 5
            base_delay = 2  # seconds
            result = None
            last_error = None

            for attempt in range(max_retries):
                try:
                    # Check cancellation before each attempt
                    if cancel_flag and cancel_flag.get('cancelled'):
                        return None

                    result = screen_profile(profile, job_description, client, tracker=tracker, mode=mode, ai_model=ai_model, ai_provider=ai_provider, user_request=user_request, screening_brief=screening_brief)
                    break  # Success, exit retry loop

                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()
                    error_code = getattr(e, 'status_code', None) or ('429' if '429' in str(e) else ('403' if '403' in str(e) else None))

                    # Check if it's a rate limit error (429 or 403 for Anthropic)
                    is_rate_limit = (
                        error_code in [429, 403, '429', '403'] or
                        'rate' in error_str or
                        'limit' in error_str or
                        'too many' in error_str or
                        'overloaded' in error_str
                    )

                    if is_rate_limit and attempt < max_retries - 1:
                        # Exponential backoff: 2, 4, 8, 16, 32 seconds
                        delay = base_delay * (2 ** attempt)
                        print(f"[Screening] Rate limit on profile {index}, attempt {attempt+1}/{max_retries}, waiting {delay}s...")
                        time.sleep(delay)
                        continue
                    else:
                        # Not a rate limit error or max retries reached, re-raise
                        print(f"[Screening] FAILED profile {index} after {attempt+1} attempts: {str(e)[:100]}")
                        raise

            if result is None:
                raise last_error or Exception("Failed after max retries")

            # Add profile info to result
            name = profile.get('name', '') or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
            if not name:
                name = profile.get('full_name', '') or profile.get('fullName', '') or f"Profile {index}"
            result['name'] = name
            result['first_name'] = profile.get('first_name', '')
            result['last_name'] = profile.get('last_name', '')
            # If no first/last name, try to split from full name
            if not result['first_name'] and name and ' ' in name:
                parts = name.split(' ', 1)
                result['first_name'] = parts[0]
                result['last_name'] = parts[1] if len(parts) > 1 else ''
            result['current_title'] = profile.get('current_title', '') or profile.get('headline', '') or profile.get('title', '') or ''
            result['current_company'] = profile.get('current_company', '') or profile.get('companyName', '') or profile.get('company', '') or ''
            result['linkedin_url'] = profile.get('linkedin_url', '') or profile.get('public_url', '') or profile.get('defaultProfileUrl', '') or ''
            result['location'] = profile.get('location', '') or ''
            # Get university from raw_data education_background (full name, not normalized)
            university = ''
            raw_data = profile.get('raw_data') or profile.get('raw_crustdata') or {}
            if isinstance(raw_data, str):
                try:
                    raw_data = json.loads(raw_data)
                except:
                    raw_data = {}
            edu_bg = raw_data.get('education_background', []) or []
            if edu_bg:
                # Get first school's full name
                university = edu_bg[0].get('institute_name') or edu_bg[0].get('school_name') or ''
            elif profile.get('all_schools'):
                university = profile['all_schools'][0] if profile['all_schools'] else ''
            elif profile.get('education'):
                university = profile['education']
            result['university'] = university
            # Include email for export (prefer DB email, fallback to SalesQL)
            result['email'] = profile.get('email', '') or profile.get('salesql_email', '') or ''
            result['index'] = index
        except Exception as e:
            import traceback
            fallback_name = profile.get('name', '') or profile.get('first_name', '') or profile.get('fullName', '') or f"Profile {index}"
            first_name = profile.get('first_name', '')
            last_name = profile.get('last_name', '')
            if not first_name and fallback_name and ' ' in fallback_name:
                parts = fallback_name.split(' ', 1)
                first_name = parts[0]
                last_name = parts[1] if len(parts) > 1 else ''
            # Get university from raw_data education_background (full name)
            err_university = ''
            err_raw_data = profile.get('raw_data') or profile.get('raw_crustdata') or {}
            if isinstance(err_raw_data, str):
                try:
                    err_raw_data = json.loads(err_raw_data)
                except:
                    err_raw_data = {}
            err_edu_bg = err_raw_data.get('education_background', []) or []
            if err_edu_bg:
                err_university = err_edu_bg[0].get('institute_name') or err_edu_bg[0].get('school_name') or ''
            elif profile.get('all_schools'):
                err_university = profile['all_schools'][0] if profile['all_schools'] else ''
            result = {
                "score": 0,
                "fit": "Error",
                "summary": f"Screen error: {str(e)[:80]}",
                "name": fallback_name,
                "first_name": first_name,
                "last_name": last_name,
                "current_title": "",
                "current_company": "",
                "linkedin_url": "",
                "location": profile.get('location', ''),
                "university": err_university,
                "email": profile.get('email', '') or profile.get('salesql_email', '') or '',
                "index": index
            }

        with lock:
            completed_count[0] += 1

        return result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_index = {
            executor.submit(screen_single, profile, i): i
            for i, profile in enumerate(profiles)
        }

        # Collect results as they complete
        for future in as_completed(future_to_index):
            # Check cancellation
            if cancel_flag and cancel_flag.get('cancelled'):
                executor.shutdown(wait=False, cancel_futures=True)
                break

            try:
                result = future.result()
                if result is not None:  # Skip cancelled results
                    results.append(result)
                    # Call progress callback
                    if progress_callback:
                        progress_callback(len(results), total, result)
            except Exception as e:
                import traceback
                idx = future_to_index[future]
                error_msg = str(e) if str(e) else "Unknown error"
                error_result = {
                    "score": 0,
                    "fit": "Error",
                    "summary": f"Thread error: {error_msg[:80]}",
                    "name": f"Profile {idx}",
                    "current_title": "",
                    "current_company": "",
                    "linkedin_url": "",
                    "email": "",
                    "index": idx
                }
                results.append(error_result)
                if progress_callback:
                    progress_callback(len(results), total, error_result)

    # Sort by original index to maintain order
    results.sort(key=lambda x: x.get('index', 0))
    return results


# ===== Sidebar: API Connection Status =====
with st.sidebar:
    config = load_config()
    _db_ok = bool(HAS_DATABASE and _get_db_client())
    _api_status = {
        'Crustdata': bool(config.get('api_key')),
        'OpenAI': bool(config.get('openai_api_key')),
        'Anthropic': bool(load_anthropic_key()),
        'PhantomBuster': bool(config.get('phantombuster_api_key')),
        'SalesQL': bool(config.get('salesql_api_key')),
        'Google Sheets': bool(config.get('google_credentials') or config.get('google_credentials_file')),
        'Supabase': _db_ok,
    }
    _missing = [name for name, ok in _api_status.items() if not ok]
    if _missing:
        st.warning(f"Missing keys: {', '.join(_missing)}")
    else:
        st.success("All APIs connected")
    with st.expander("API Status", expanded=bool(_missing)):
        for name, ok in _api_status.items():
            st.markdown(f"{'🟢' if ok else '🔴'} **{name}**")
    st.divider()

    # Memory management
    with st.expander("Memory", expanded=False):
        # Check for raw_crustdata in dataframes
        _has_raw = False
        for _df_key in ['results_df', 'enriched_df']:
            if _df_key in st.session_state and isinstance(st.session_state[_df_key], pd.DataFrame):
                if 'raw_crustdata' in st.session_state[_df_key].columns:
                    _has_raw = True
                    break

        if _has_raw:
            if st.button("⚡ Strip Raw Data (saves memory)", width="stretch", type="primary"):
                cleanup_memory()  # Use centralized cleanup
                st.success("Memory optimized!")
                st.rerun()

        if is_admin_user():
            if st.button("🗑️ Clear Debug Data", width="stretch"):
                _debug_keys = ['_enrich_debug', '_enrich_match_debug', '_debug_url_cols', '_debug_all_cols', '_debug_valid_urls']
                for k in _debug_keys:
                    if k in st.session_state:
                        del st.session_state[k]
                st.rerun()

        if st.button("🗑️ Clear Filtered Data", width="stretch"):
            if 'filtered_out' in st.session_state:
                del st.session_state['filtered_out']
            st.rerun()

        if st.button("🔄 Clear All & Reboot", type="secondary", width="stretch"):
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            # Also clear caches
            st.cache_data.clear()
            import gc
            gc.collect()
            st.rerun()
    st.divider()

# Main UI
st.title("SourcingX")

# Check API keys
api_key = load_api_key()
has_crust_key = api_key and api_key != "YOUR_CRUSTDATA_API_KEY_HERE"

# Show data status in header (always render to keep widget tree stable for tabs).
# Surface both the original CSV upload count AND the working/enriched count so
# the user always knows the size of the source set, not just what's currently
# enriched/loaded.
_profile_count = get_profile_count()
_orig_csv_df = st.session_state.get('original_results_df')
_orig_count = (
    len(_orig_csv_df)
    if _orig_csv_df is not None and isinstance(_orig_csv_df, pd.DataFrame) and not _orig_csv_df.empty
    else 0
)
if _profile_count and _orig_count and _orig_count != _profile_count:
    st.info(
        f"📊 **{_orig_count}** profiles uploaded from CSV · "
        f"**{_profile_count}** ready in working set"
    )
elif _orig_count:
    st.info(f"📊 **{_orig_count}** profiles uploaded from CSV")
elif _profile_count:
    st.info(f"📊 **{_profile_count}** profiles loaded")
else:
    st.info("No profiles loaded — start from the Load tab")

# Create tabs
tab_search, tab_upload, tab_filter, tab_screening, tab_emails, tab_database, tab_similar, tab_usage = st.tabs([
    "0. Search", "1. Load", "2. Filter", "3. AI Screen", "4. Emails", "5. Database", "6. Find Similar", "7. Usage"
])

# ========== TAB 0: Search (Crustdata People DB) ==========
with tab_search:
    st.markdown("### Crustdata People Database")

    # Check if module is available
    if not HAS_CRUSTDATA_SEARCH:
        st.warning("Crustdata search module not available. Check crustdata_search.py import.")
    elif not has_crust_key:
        st.warning("Crustdata API key not configured. Add your API key to config.json.")
    else:
        # Show credits in header
        credits_col1, credits_col2 = st.columns([3, 1])
        with credits_col1:
            st.caption("Search 100M+ professional profiles directly from Crustdata's database")
        with credits_col2:
            # Use module-level cached function for efficiency
            credits_info = _get_crustdata_credits_cached(api_key)
            if "error" not in credits_info:
                st.metric("Credits", f"{credits_info.get('remaining', '?'):,}")
            else:
                st.caption(f"Credits: unavailable")

        st.divider()

        # ===== Pre-search sheet connection =====
        # Loads not-relevant companies, blacklist, and past candidates from the
        # user's Google Sheet before the first search so all exclusions fire
        # without needing to visit the Filters tab first.
        _any_loaded = (
            st.session_state.get('nr_for_search') or
            st.session_state.get('blacklist_for_search') or
            st.session_state.get('past_candidates_urls_for_search') or
            st.session_state.get('past_candidates_names_for_search')
        )
        with st.expander("Search exclusions (not-relevant, blacklist, past candidates)", expanded=not _any_loaded):
            _st_sheet_url = st.text_input(
                "Google Sheet URL",
                value=st.session_state.get('user_sheet_url', ''),
                placeholder="https://docs.google.com/spreadsheets/d/...",
                key="search_tab_nr_sheet_url",
                help="Paste your sheet URL. Tab names are read from your configured sheet settings.",
            )
            if _st_sheet_url:
                st.session_state['user_sheet_url'] = _st_sheet_url
                # If the user pasted a different URL, clear stale exclusions so they reload
                if st.session_state.get('_search_exclusions_loaded_from') != _st_sheet_url:
                    for _stale_key in ('nr_for_search', 'blacklist_for_search',
                                       'past_candidates_urls_for_search', 'past_candidates_names_for_search'):
                        st.session_state.pop(_stale_key, None)
                    st.session_state['_search_exclusions_loaded_from'] = _st_sheet_url
                _st_gspread = get_gspread_client()
                if _st_gspread:
                    # Use the same tab names as the Filter tab so renamed tabs still work
                    _fs_cfg = get_filter_sheets_config()
                    _nr_tab = _fs_cfg.get('not_relevant', 'NotRelevant Companies')
                    _bl_tab = _fs_cfg.get('blacklist', 'Blacklist')
                    _pc_tab = _fs_cfg.get('past_candidates', 'Past Candidates')
                    # Not-relevant companies
                    if not st.session_state.get('nr_for_search'):
                        _nr_df = load_sheet_as_df(_st_sheet_url, _nr_tab)
                        if _nr_df is not None and len(_nr_df.columns) > 0:
                            _nr_list = []
                            for _col in _nr_df.columns:
                                _nr_list.extend(_nr_df[_col].dropna().tolist())
                            st.session_state['nr_for_search'] = list(set(_nr_list))
                    # Blacklist
                    if not st.session_state.get('blacklist_for_search'):
                        _bl_df = load_sheet_as_df(_st_sheet_url, _bl_tab)
                        if _bl_df is not None and len(_bl_df.columns) > 0:
                            _bl_list = []
                            for _col in _bl_df.columns:
                                _bl_list.extend(_bl_df[_col].dropna().tolist())
                            st.session_state['blacklist_for_search'] = list(set(_bl_list))
                    # Past candidates — extract LinkedIn URLs (API exclusion) + names (post-filter fallback)
                    # Guard each half independently: a sheet may have URLs but no name column
                    # (or vice versa), and the AND guard would block the missing half from ever loading.
                    _pc_needs_load = (
                        not st.session_state.get('past_candidates_urls_for_search') or
                        not st.session_state.get('past_candidates_names_for_search')
                    )
                    if _pc_needs_load:
                        _pc_df = load_sheet_as_df(_st_sheet_url, _pc_tab)
                        if _pc_df is not None:
                            _pc_col_lower = {c.lower(): c for c in _pc_df.columns}

                            def _find_pc_col(candidates):
                                for c in candidates:
                                    if c in _pc_df.columns:
                                        return c
                                    if c.lower() in _pc_col_lower:
                                        return _pc_col_lower[c.lower()]
                                return None

                            # LinkedIn URLs → API-level exclusion
                            _pc_linkedin_col = _find_pc_col([
                                'LinkedIn', 'linkedin_url', 'LinkedIn URL', 'LinkedIn Profile URL',
                                'linkedinUrl', 'profileUrl', 'profile_url', 'URL',
                            ])
                            if _pc_linkedin_col:
                                _pc_urls = [u.strip() for u in _pc_df[_pc_linkedin_col].dropna() if u and str(u).strip()]
                                if _pc_urls:
                                    st.session_state['past_candidates_urls_for_search'] = _pc_urls

                            # Names → post-search filter fallback
                            # Normalize: lowercase, collapse whitespace, strip punctuation
                            def _norm_pc_name(n):
                                n = str(n).lower().strip()
                                n = ' '.join(n.split())
                                n = re.sub(r'[^\w\s]', '', n)
                                return n.strip()

                            _pc_name_col = _find_pc_col(['Name', 'Full Name', 'full_name', 'FullName', 'Candidate Name'])
                            if _pc_name_col:
                                _pc_names = [_norm_pc_name(n) for n in _pc_df[_pc_name_col].dropna() if n and str(n).strip()]
                                if _pc_names:
                                    st.session_state['past_candidates_names_for_search'] = _pc_names
                            else:
                                # Try First Name + Last Name columns
                                _pc_first_col = _find_pc_col(['First Name', 'first_name', 'FirstName', 'first'])
                                _pc_last_col = _find_pc_col(['Last Name', 'last_name', 'LastName', 'last'])
                                if _pc_first_col and _pc_last_col:
                                    _pc_names = [
                                        _norm_pc_name(f"{str(r[_pc_first_col]).strip()} {str(r[_pc_last_col]).strip()}")
                                        for _, r in _pc_df[[_pc_first_col, _pc_last_col]].dropna().iterrows()
                                        if str(r[_pc_first_col]).strip() or str(r[_pc_last_col]).strip()
                                    ]
                                    if _pc_names:
                                        st.session_state['past_candidates_names_for_search'] = _pc_names

            else:
                # URL cleared — remove stale exclusions and the shared sheet URL so
                # the Filter tab doesn't silently reconnect the old spreadsheet.
                for _k in ('nr_for_search', 'blacklist_for_search',
                           'past_candidates_urls_for_search', 'past_candidates_names_for_search',
                           '_search_exclusions_loaded_from', 'user_sheet_url'):
                    st.session_state.pop(_k, None)

            # Status row
            _st_cols = st.columns(3)
            with _st_cols[0]:
                if st.session_state.get('nr_for_search'):
                    st.success(f"{len(st.session_state['nr_for_search'])} not-relevant companies")
                elif _st_sheet_url:
                    st.caption("Not-relevant: not loaded")
            with _st_cols[1]:
                if st.session_state.get('blacklist_for_search'):
                    st.success(f"{len(st.session_state['blacklist_for_search'])} blacklisted companies")
                elif _st_sheet_url:
                    st.caption("Blacklist: not loaded")
            with _st_cols[2]:
                _pc_url_count = len(st.session_state.get('past_candidates_urls_for_search') or [])
                _pc_name_count = len(st.session_state.get('past_candidates_names_for_search') or [])
                if _pc_url_count or _pc_name_count:
                    _pc_detail = f"{_pc_url_count} by URL, {_pc_name_count} by name"
                    st.success(f"Past candidates: {_pc_detail}")
                elif _st_sheet_url:
                    st.caption("Past candidates: not loaded")

        st.divider()

        # ===== Search Filters (with inline AI expansion) =====
        openai_key = load_openai_key()
        _has_ai = bool(openai_key and HAS_CRUSTDATA_SEARCH)

        # --- AI expansion helper ---
        def _do_ai_expand(input_key, expanded_key, field_type, geo_key=None):
            """on_click callback: parses manual values, calls AI for each, merges all.

            When ``geo_key`` is supplied (the session-state key holding the
            user's Location filter), company/school expansions are scoped to
            that region so suggestions stay relevant to the recruiter's
            geographic search.
            """
            val = st.session_state.get(input_key, '').strip()
            if not val or not _has_ai:
                return
            geo_context = _read_geo_context(geo_key)
            # Parse comma-separated manual values
            manual_terms = [t.strip() for t in val.split(',') if t.strip()]
            # Start with existing suggestions + manual terms
            all_items = list(st.session_state.get(expanded_key, []))
            all_items.extend(manual_terms)
            # AI-expand each manual term
            for term in manual_terms:
                sug = expand_variations(term, field_type=field_type,
                                        openai_api_key=openai_key,
                                        geo_context=geo_context)
                all_items.extend(sug)
            # Dedupe preserving order
            seen = set()
            merged = []
            for item in all_items:
                k = item.lower()
                if k not in seen:
                    seen.add(k)
                    merged.append(item)
            st.session_state[expanded_key] = merged
            # Also update the multiselect selection to include all new items
            st.session_state[f"sel_{expanded_key}"] = merged

        def _generate_more(input_key, expanded_key, field_type, geo_key=None):
            """on_click callback: generate more suggestions excluding existing ones."""
            val = st.session_state.get(input_key, '').strip()
            if not val or not _has_ai:
                return
            geo_context = _read_geo_context(geo_key)
            existing = st.session_state.get(expanded_key, [])
            manual_terms = [t.strip() for t in val.split(',') if t.strip()]
            new_items = list(existing)
            for term in manual_terms:
                sug = expand_variations(term, field_type=field_type,
                                        openai_api_key=openai_key, exclude=existing,
                                        geo_context=geo_context)
                new_items.extend(sug)
            # Dedupe
            seen = set()
            merged = []
            for item in new_items:
                k = item.lower()
                if k not in seen:
                    seen.add(k)
                    merged.append(item)
            st.session_state[expanded_key] = merged
            st.session_state[f"sel_{expanded_key}"] = merged

        def _clear_expanded(expanded_key):
            """on_click callback: clear AI suggestions."""
            st.session_state.pop(expanded_key, None)

        def _add_custom_value(custom_key, expanded_key):
            """on_change callback: add custom value to expanded list + auto-select it."""
            val = st.session_state.get(custom_key, '').strip()
            if val and expanded_key in st.session_state:
                new_items = [v.strip() for v in val.split(',') if v.strip()]
                existing = st.session_state[expanded_key]
                existing_lower = {e.lower() for e in existing}
                sel_key = f"sel_{expanded_key}"
                current_sel = list(st.session_state.get(sel_key, existing))
                for item in new_items:
                    if item.lower() not in existing_lower:
                        existing.append(item)
                        existing_lower.add(item.lower())
                        current_sel.append(item)
                st.session_state[expanded_key] = existing
                st.session_state[sel_key] = current_sel
                st.session_state[custom_key] = ''

        def _render_ai_field(container, label, input_key, expanded_key, field_type,
                             placeholder, geo_key=None):
            """Render a text input with optional AI suggest button and multiselect.
            Uses the provided container to avoid nested columns issues.

            ``geo_key`` is the session-state key holding the user's Location
            filter. When set AND field_type is company/school, AI suggestions
            are scoped to that region. Do NOT pass geo_key for the Location
            field itself (circular).
            """
            with container:
                st.text_input(label, key=input_key, placeholder=placeholder)
                if _has_ai:
                    st.button("✨", key=f"btn_ai_{input_key}",
                              on_click=_do_ai_expand,
                              args=(input_key, expanded_key, field_type, geo_key),
                              help="AI suggest variations", use_container_width=True)

                # Show multiselect if AI suggestions exist
                if st.session_state.get(expanded_key):
                    st.multiselect(
                        f"Select {label.split('(')[0].strip()}:",
                        options=st.session_state[expanded_key],
                        default=st.session_state[expanded_key],
                        key=f"sel_{expanded_key}",
                        label_visibility="collapsed",
                    )
                    custom_key = f"custom_{expanded_key}"
                    st.text_input("Add custom:", key=custom_key,
                                  placeholder="Type and press Enter",
                                  on_change=_add_custom_value, args=(custom_key, expanded_key),
                                  label_visibility="collapsed")
                    more_col, clear_col = st.columns(2)
                    with more_col:
                        st.button("✨ More", key=f"btn_more_{expanded_key}",
                                  on_click=_generate_more,
                                  args=(input_key, expanded_key, field_type, geo_key),
                                  help="Generate more suggestions", use_container_width=True)
                    with clear_col:
                        st.button("✕ Clear", key=f"btn_clear_{expanded_key}",
                                  on_click=_clear_expanded, args=(expanded_key,),
                                  help="Clear suggestions", use_container_width=True)

        # ===== Your recent searches (auto-saved history) =====
        # Rendered ABOVE the filter widgets so the "Load" handler can write the
        # widgets' session_state keys before those widgets are instantiated this
        # run (Streamlit forbids setting a widget key after it's built).
        _ps_username = st.session_state.get('username') or 'default'
        _ps_db = _get_db_client() if HAS_DATABASE else None

        # Multiselect option lists — used to drop any stored value no longer valid.
        _PS_MS_OPTIONS = {
            'crust_search_seniority': SENIORITY_LEVELS,
            'crust_search_headcount': HEADCOUNT_RANGES,
            'crust_search_function': FUNCTION_CATEGORIES,
            'crust_search_industry': COMPANY_INDUSTRIES,
        }
        # Text field → its AI-expansion state keys (cleared on load so the stored,
        # already-merged text isn't doubled by stale AI selections).
        _PS_AI_EXPANDED = {
            'crust_search_title': 'expanded_titles',
            'crust_search_company': 'expanded_companies',
            'crust_search_location': 'expanded_locations',
            'crust_search_keywords': 'expanded_keywords',
            'crust_search_skills': 'expanded_skills',
            'crust_search_past_titles': 'expanded_past_titles',
            'crust_search_past_companies': 'expanded_past_companies',
            'crust_search_school': 'expanded_schools',
        }

        def _load_people_search_into_form(stored_filters):
            """Refill the Search-tab widgets from a saved search's stored filters."""
            for _ek in _PS_AI_EXPANDED.values():
                for _k in (_ek, f'sel_{_ek}', f'custom_{_ek}'):
                    st.session_state.pop(_k, None)
            for _key, _val in (stored_filters or {}).items():
                if _key in _PS_MS_OPTIONS:
                    _opts = _PS_MS_OPTIONS[_key]
                    _val = [v for v in (_val or []) if v in _opts]
                st.session_state[_key] = _val

        if st.session_state.get('_ps_loaded_msg'):
            st.success(st.session_state.pop('_ps_loaded_msg'))

        if _ps_db:
            _recent_searches = list_people_searches(_ps_db, _ps_username, limit=25)
            if _recent_searches:
                with st.expander(f"⭐ Your recent searches ({len(_recent_searches)})", expanded=False):
                    st.caption(
                        "Auto-saved each time you run a search. **📥 Load saved (free)** "
                        "pulls the same people back from your database with no Crustdata "
                        "credits. **↻** refills the filters so you can Search again for new "
                        "people. The trash icon deletes the saved search."
                    )
                    for _rs in _recent_searches:
                        _rs_id = _rs.get('id')
                        _summary = _rs.get('summary') or 'All profiles (no filters)'
                        _when = (_rs.get('last_run_at') or '')[:10]
                        _rc = _rs.get('result_count')
                        _runs = _rs.get('run_count') or 1
                        _meta = []
                        if _when:
                            _meta.append(_when)
                        if _rc is not None:
                            _meta.append(f"{_rc:,} results")
                        if _runs > 1:
                            _meta.append(f"run {_runs}×")
                        _rs_urls = _rs.get('result_urls') or []
                        _row_load, _row_get, _row_del = st.columns([5, 2, 1])
                        with _row_load:
                            if st.button(
                                f"↻ {_summary}",
                                key=f"ps_load_{_rs_id}",
                                use_container_width=True,
                                help=" · ".join(_meta) if _meta else None,
                            ):
                                _rs_filters = _rs.get('filters') or {}
                                _load_people_search_into_form(_rs_filters)
                                if _rs_filters.get('crust_semantic_query'):
                                    st.session_state['_sem_expand_after_load'] = True
                                st.session_state['_ps_loaded_msg'] = f"Loaded filters from: {_summary}"
                                st.rerun()
                        with _row_get:
                            # Free reload: pull the exact people this search found from the
                            # profiles table — no Crustdata call. Only offered for searches
                            # recorded with stored links (older rows have none).
                            if _rs_urls and st.button(
                                f"📥 Load {len(_rs_urls):,} saved (free)",
                                key=f"ps_get_{_rs_id}",
                                use_container_width=True,
                                help="Load the exact people this search found, straight from "
                                     "your database — no Crustdata credits used.",
                            ):
                                try:
                                    # Generous window so older saved searches still return
                                    # their people (cutoff filters on save date).
                                    _cut = (datetime.utcnow() - timedelta(days=3650)).isoformat()
                                    with st.spinner(
                                        f"Loading {len(_rs_urls):,} saved profiles from your database..."
                                    ):
                                        _saved_profiles = _fetch_candidate_profiles_for_urls(
                                            _ps_db, _rs_urls, '*', _cut
                                        )
                                    # Strict-filter to the saved URL set (drop suffix-pass
                                    # extras that point at different people with similar
                                    # slugs) — same overlap check the CSV "already in DB"
                                    # path uses, so we never load unrelated profiles.
                                    _saved_norm_set = {
                                        normalize_linkedin_url(_u) for _u in _rs_urls if _u
                                    }
                                    _saved_norm_set.discard(None)
                                    _saved_norm_set.discard('')
                                    _saved_profiles = [
                                        _p for _p in (_saved_profiles or [])
                                        if any(
                                            normalize_linkedin_url(_u) in _saved_norm_set
                                            for _u in (
                                                _p.get('linkedin_url'),
                                                _p.get('original_url'),
                                                *(_p.get('original_urls') or []),
                                            )
                                            if _u
                                        )
                                    ]
                                    if _saved_profiles:
                                        _saved_df = profiles_to_dataframe(_saved_profiles)
                                        # Mirror the proven "Load existing from DB" writes so
                                        # the Filter/Screen tabs behave identically.
                                        clear_results_derived_state(st.session_state)
                                        st.session_state['enriched_df'] = _saved_df
                                        st.session_state['results_df'] = _saved_df
                                        # original_results_df marks a CSV upload. Setting it made
                                        # the header banner mislabel these DB-loaded people as
                                        # "uploaded from CSV". Clear it instead (don't just skip
                                        # setting it): a stale value from an earlier CSV this
                                        # session would keep the wrong banner count AND make Reset
                                        # Filters restore the old CSV. With it cleared, the first
                                        # Apply Filters re-baselines Reset to this saved set.
                                        st.session_state.pop('original_results_df', None)
                                        st.session_state['enriched_profiles_raw'] = {
                                            p.get('linkedin_url', ''): p
                                            for p in _saved_profiles if p.get('raw_data')
                                        }
                                        save_session_state()
                                        st.session_state['_ps_loaded_msg'] = (
                                            f"Loaded {len(_saved_df):,} saved profiles from: {_summary} "
                                            f"— no Crustdata credits used. See them in the **1. Load** "
                                            f"tab; filter them in **2. Filter**."
                                        )
                                    else:
                                        st.session_state['_ps_loaded_msg'] = (
                                            f"No saved profiles are still in your database for: "
                                            f"{_summary}. Run the search to fetch them again."
                                        )
                                    st.rerun()
                                except Exception as _e:
                                    st.warning(f"Could not load saved results: {str(_e)[:120]}")
                        with _row_del:
                            if st.button("\U0001f5d1", key=f"ps_del_{_rs_id}",
                                         use_container_width=True, help="Delete this saved search"):
                                delete_people_search(_ps_db, _rs_id)
                                st.rerun()
                        if _meta:
                            st.caption(" · ".join(_meta))

        # ===== Search by description (beta) =====
        # Crustdata's newer natural-language search: type a plain description
        # instead of filling in filters, and get people ranked by how well
        # their whole profile matches. Feeds the exact same results table /
        # selection / CSV export / "add to pipeline" code as the regular
        # filter search below, via semantic_profile_to_legacy_shape().
        with st.expander("🔍 Search by description (beta)", expanded=st.session_state.pop('_sem_expand_after_load', False)):
            st.caption(
                "Type who you're looking for in plain language — e.g. \"founding "
                "engineers at developer-tools startups in Israel\" — instead of "
                "filling in the filters below. Crustdata ranks people by how well "
                "they match. This is a new, beta feature of Crustdata, so quality "
                "can vary — each result costs the same credits as a regular search "
                "(0.03 credits per result)."
            )
            semantic_query = st.text_area(
                "Describe who you're looking for",
                key="crust_semantic_query",
                placeholder="e.g. senior backend engineers with Kubernetes experience in Tel Aviv",
                height=80,
            )
            sem_col1, sem_col2 = st.columns([1, 3])
            with sem_col1:
                semantic_limit = st.number_input(
                    "Results", min_value=5, max_value=100, value=20, step=5,
                    key="crust_semantic_limit",
                    help="Max 100 per search (Crustdata beta limit).",
                )
            with sem_col2:
                st.write("")  # vertical spacer to align button with the number input
                semantic_submitted = st.button(
                    "Search by description", key="crust_semantic_search_btn",
                )

            if semantic_submitted:
                if not semantic_query or not semantic_query.strip():
                    st.warning("Type a description first.")
                else:
                    try:
                        sem_progress = st.empty()
                        sem_progress.info("Searching Crustdata by description...")
                        sem_results = search_people_semantic(
                            semantic_query.strip(),
                            limit=int(semantic_limit),
                            api_key=api_key,
                        )
                        sem_shimmed = [
                            semantic_profile_to_legacy_shape(p)
                            for p in (sem_results.get("profiles") or [])
                        ]

                        # Apply the same past-candidates / blacklist / not-relevant
                        # exclusions the filter search applies, so description
                        # search doesn't resurface people already ruled out.
                        _sem_pc_names = {
                            str(n).lower().strip()
                            for n in (st.session_state.get('past_candidates_names_for_search') or [])
                        }
                        # The filter search excludes these URLs server-side (exclude_profiles);
                        # search_people_semantic has no equivalent param, so past candidates
                        # known only by URL (no matching name) must be filtered here too.
                        _sem_pc_urls = {
                            normalize_linkedin_url(u)
                            for u in (st.session_state.get('past_candidates_urls_for_search') or [])
                            if u
                        }
                        _sem_pc_urls.discard(None)
                        _sem_bl = [
                            c for c in (st.session_state.get('blacklist_for_search') or [])
                            if c and str(c).strip() and len(str(c).strip()) >= 3
                        ]
                        _sem_nr = [
                            c for c in (st.session_state.get('nr_for_search') or [])
                            if c and str(c).strip() and len(str(c).strip()) >= 3
                        ]

                        def _sem_name_of(p):
                            n = str(p.get('name') or '').lower().strip()
                            return ' '.join(re.sub(r'[^\w\s]', '', n).split())

                        def _sem_url_of(p):
                            raw = p.get('flagship_profile_url')
                            return normalize_linkedin_url(raw) if raw else None

                        def _sem_company_of(p):
                            emp = pick_current_employer(p.get('current_employers'))
                            return (emp.get('name') or '') if emp else ''

                        _sem_removed = 0
                        _sem_clean = []
                        for p in sem_shimmed:
                            if not p:
                                continue
                            if _sem_pc_names and _sem_name_of(p) in _sem_pc_names:
                                _sem_removed += 1
                                continue
                            if _sem_pc_urls and _sem_url_of(p) in _sem_pc_urls:
                                _sem_removed += 1
                                continue
                            _sem_co = _sem_company_of(p)
                            if _sem_co and _sem_bl and _company_matches_filter_list(_sem_co, _sem_bl):
                                _sem_removed += 1
                                continue
                            if _sem_co and _sem_nr and _company_matches_filter_list(_sem_co, _sem_nr):
                                _sem_removed += 1
                                continue
                            _sem_clean.append(p)

                        st.session_state['crustdata_search_results'] = _sem_clean
                        st.session_state['crustdata_search_cursor'] = sem_results.get('cursor')
                        st.session_state['crustdata_search_total'] = sem_results.get('total_count', len(_sem_clean))
                        st.session_state['crustdata_search_credits_used'] = sem_results.get('credits_used', 0)
                        st.session_state['crustdata_search_selected'] = list(range(len(_sem_clean)))
                        # Tells Load More which endpoint/cursor pairing to use — the cursor
                        # above comes from the semantic endpoint, not the filter-search one,
                        # so Load More must keep calling search_people_semantic with it.
                        st.session_state['crustdata_search_mode'] = 'semantic'
                        st.session_state['_last_semantic_params'] = {
                            'query': semantic_query.strip(),
                            'limit': int(semantic_limit),
                        }
                        # Deliberately NOT setting _pending_initial_save here (unlike the
                        # filter search below). That flag background-saves results via
                        # save_enriched_profiles_bulk(), which unconditionally stamps
                        # enrichment_status='enriched' + enriched_at — but these profiles
                        # are known-incomplete (_semantic_incomplete, see
                        # semantic_profile_to_legacy_shape docstring). Marking them
                        # "enriched" would make get_enriched_urls()/get_recently_enriched_urls()
                        # (the skip-lists used before re-scraping/re-enriching someone)
                        # treat these people as already fully done, silently blocking the
                        # real enrichment they still need — across this app AND the other
                        # pipelines sharing this database. So "Load saved (free)" only
                        # works for a description-search history entry once those people
                        # have actually been enriched through the normal Load-tab flow.

                        # Auto-save this description search to the same "recent
                        # searches" history the filter search uses, so it shows up
                        # there too (deduped per user by hash). Never fatal.
                        try:
                            _sem_filters_state = {
                                'crust_semantic_query': semantic_query.strip(),
                                'crust_semantic_limit': int(semantic_limit),
                            }
                            _sem_hash = hash_search_filters(_sem_filters_state)
                            _sem_summary = summarize_search_filters(_sem_filters_state)
                            _sem_result_urls = []
                            _sem_seen_u = set()
                            for _p in _sem_clean:
                                _raw_u = _p.get('flagship_profile_url')
                                _nu = normalize_linkedin_url(_raw_u) if _raw_u else None
                                if _nu and _nu not in _sem_seen_u:
                                    _sem_seen_u.add(_nu)
                                    _sem_result_urls.append(_nu)
                            record_people_search(
                                _ps_db, _ps_username, _sem_filters_state, _sem_hash, _sem_summary,
                                int(st.session_state.get('crustdata_search_total') or 0),
                                result_urls=_sem_result_urls or None,
                            )
                            st.session_state['_ps_history_ref'] = {
                                'username': _ps_username,
                                'filters_hash': _sem_hash,
                            }
                        except Exception:
                            pass

                        _sem_note = f" ({_sem_removed} already-seen removed)" if _sem_removed else ""
                        st.session_state['_search_loaded_msg'] = (
                            f"Found **{len(_sem_clean):,}** profiles matching your description{_sem_note}. "
                            f"Read the **Fit** column (strong/possible/weak) — description search always "
                            f"returns results, so a low count doesn't mean poor quality and a high count "
                            f"doesn't guarantee good matches."
                        )
                        st.rerun()
                    except Exception as _sem_e:
                        st.error(f"Description search failed: {str(_sem_e)[:300]}")

        st.markdown("##### Search Filters")

        # Row 1: Title, Company, Location (each with AI button below)
        col1, col2, col3 = st.columns(3)
        _render_ai_field(col1, "Title/Role", "crust_search_title", "expanded_titles", "title",
                         "e.g., team leader")
        _render_ai_field(col2, "Company", "crust_search_company", "expanded_companies", "company",
                         "e.g., SaaS startups in Tel Aviv",
                         geo_key="crust_search_location")
        with col2:
            search_exact_company = st.checkbox(
                "Exact match only",
                key="crust_search_exact_company",
                help="Match the exact company name instead of substring. Type the name exactly as it appears on LinkedIn."
            )
        _render_ai_field(col3, "Location", "crust_search_location", "expanded_locations", "location",
                         "e.g., west coast usa")

        # Row 1b: Country, Continent, Geo radius (all location filters together)
        _COUNTRIES = [
            "", "Argentina", "Australia", "Bangladesh", "Belgium", "Brazil",
            "Canada", "Chile", "China", "Colombia", "Denmark", "Ecuador",
            "Egypt", "France", "Germany", "India", "Indonesia", "Iran",
            "Ireland", "Israel", "Italy", "Japan", "Kenya", "Malaysia",
            "Mexico", "Morocco", "Netherlands", "New Zealand", "Nigeria",
            "Norway", "Pakistan", "Peru", "Philippines", "Poland", "Portugal",
            "Romania", "Russia", "Saudi Arabia", "Singapore", "South Africa",
            "South Korea", "Spain", "Sweden", "Switzerland", "Thailand",
            "Turkey", "Ukraine", "United Arab Emirates", "United Kingdom",
            "United States", "Venezuela",
        ]
        _CONTINENTS = ["", "Africa", "Asia", "Europe", "North America", "Oceania", "South America"]
        col1b1, col1b2, col1b3, col1b4 = st.columns(4)
        with col1b1:
            search_country = st.selectbox(
                "Country",
                options=_COUNTRIES,
                index=0,
                key="crust_search_country",
                help="Structured country filter — more reliable than the Location text box above."
            )
        with col1b2:
            search_continent = st.selectbox(
                "Continent",
                options=_CONTINENTS,
                index=0,
                key="crust_search_continent"
            )
        with col1b3:
            search_geo_city = st.text_input(
                "City (radius center)",
                key="crust_search_geo_city",
                placeholder="e.g., Tel Aviv, London"
            )
        with col1b4:
            search_geo_radius = st.number_input(
                "Radius (km)",
                min_value=0,
                max_value=500,
                value=0,
                key="crust_search_geo_radius",
                help="0 = disabled. Use with City to find people within N km."
            )

        # Row 2: Seniority, Company Size
        col4, col5 = st.columns(2)
        with col4:
            search_seniority = st.multiselect(
                "Seniority",
                options=SENIORITY_LEVELS,
                default=[],
                key="crust_search_seniority"
            )
        with col5:
            search_headcount = st.multiselect(
                "Company Size",
                options=HEADCOUNT_RANGES,
                default=[],
                key="crust_search_headcount"
            )

        # Row 2b: Function, Industry
        col2b1, col2b2 = st.columns(2)
        with col2b1:
            search_function = st.multiselect(
                "Function",
                options=FUNCTION_CATEGORIES,
                default=[],
                key="crust_search_function",
                help="Filter by job function (e.g. Engineering, Sales). Matches people currently in these functions."
            )
        with col2b2:
            search_industry = st.multiselect(
                "Industry",
                options=COMPANY_INDUSTRIES,
                default=[],
                key="crust_search_industry",
                help="Filter by the industry their current employer is in."
            )

        # Row 3: Experience range, Keywords
        col6, col7, col8 = st.columns([1, 1, 2])
        with col6:
            search_exp_min = st.number_input(
                "Min Experience (years)",
                min_value=0,
                max_value=50,
                value=0,
                key="crust_search_exp_min"
            )
        with col7:
            search_exp_max = st.number_input(
                "Max Experience (years)",
                min_value=0,
                max_value=50,
                value=0,
                key="crust_search_exp_max",
                help="0 = no limit"
            )
        _render_ai_field(col8, "Keywords", "crust_search_keywords", "expanded_keywords", "keywords",
                         "(node OR node.js) AND (react OR react.js)")

        # Advanced Filters
        with st.expander("Advanced Filters"):
            # Skills - full width, no nesting issue
            sk_col1, sk_col2 = st.columns([4, 1])
            with sk_col1:
                st.text_input("Skills (comma = OR)", key="crust_search_skills",
                              placeholder="e.g., aws, docker, kubernetes")
            with sk_col2:
                if _has_ai:
                    st.write("")  # label spacer
                    st.button("✨", key="btn_ai_crust_search_skills",
                              on_click=_do_ai_expand, args=("crust_search_skills", "expanded_skills", "skill"),
                              help="AI suggest variations", use_container_width=True)
            if st.session_state.get('expanded_skills'):
                st.multiselect("Select Skills:", options=st.session_state['expanded_skills'],
                               default=st.session_state['expanded_skills'],
                               key="sel_expanded_skills", label_visibility="collapsed")
                st.text_input("Add custom:", key="custom_expanded_skills",
                              placeholder="Type and press Enter",
                              on_change=_add_custom_value, args=("custom_expanded_skills", "expanded_skills"),
                              label_visibility="collapsed")
                sk_more_col, sk_clear_col = st.columns(2)
                with sk_more_col:
                    st.button("✨ More", key="btn_more_expanded_skills",
                              on_click=_generate_more, args=("crust_search_skills", "expanded_skills", "skill"),
                              help="Generate more suggestions", use_container_width=True)
                with sk_clear_col:
                    st.button("✕ Clear", key="btn_clear_expanded_skills",
                              on_click=_clear_expanded, args=("expanded_skills",), use_container_width=True)

            st.divider()

            adv_col1, adv_col2 = st.columns(2)
            _render_ai_field(adv_col1, "Past Titles (comma = OR)", "crust_search_past_titles", "expanded_past_titles", "title",
                             "e.g., devops, backend engineer")
            _render_ai_field(adv_col2, "Past Companies (comma = OR)", "crust_search_past_companies", "expanded_past_companies", "company",
                             "e.g., big tech, Microsoft",
                             geo_key="crust_search_location")

            adv_col3, adv_col4 = st.columns(2)
            _render_ai_field(adv_col3, "School/University", "crust_search_school", "expanded_schools", "school",
                             "e.g., top tech universities in London",
                             geo_key="crust_search_location")
            with adv_col4:
                pass  # Spacer

            adv_checkbox_col1, adv_checkbox_col2 = st.columns(2)
            with adv_checkbox_col1:
                search_recently_changed = st.checkbox(
                    "Recently changed jobs",
                    key="crust_search_recently_changed",
                    help="Started new job in last 90 days"
                )
            with adv_checkbox_col2:
                search_has_email = st.checkbox(
                    "Has verified email",
                    key="crust_search_has_email"
                )

            st.divider()

            # Min connections (geo radius moved to main section)
            search_min_connections = st.number_input(
                "Min connections",
                min_value=0,
                value=0,
                key="crust_search_min_connections",
                help="0 = no minimum. Filters for people with at least this many LinkedIn connections."
            )

        # Controls row
        ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([1, 1, 2])
        with ctrl_col1:
            search_limit = st.selectbox(
                "Results limit",
                options=[25, 50, 100, 250, 500, 1000, 2000, 5000],
                index=2,  # Default 100
                key="crust_search_limit"
            )
        with ctrl_col2:
            search_sort = st.selectbox(
                "Sort by",
                options=["Connections (most first)", "Experience (most first)", "No sort"],
                index=0,
                key="crust_search_sort"
            )
        with ctrl_col3:
            btn_col1, btn_col2 = st.columns(2)
            with btn_col1:
                search_submitted = st.button("Search", type="primary", use_container_width=True)
            with btn_col2:
                clear_submitted = st.button("Clear Filters", use_container_width=True)

        # Repeat-search guard prompt — set when the user tries to re-run filters
        # they've already searched before. Asks for confirmation instead of
        # silently re-spending effort/credits on the same search.
        if st.session_state.get('_people_search_repeat'):
            _psr = st.session_state['_people_search_repeat']
            _psr_when = (_psr.get('last_run_at') or '')[:10]
            _psr_msg = "⚠️ You already ran this exact search"
            if _psr_when:
                _psr_msg += f" on {_psr_when}"
            if _psr.get('result_count') is not None:
                _psr_msg += f" — {_psr['result_count']:,} results"
            _psr_msg += ". Run it again anyway?"
            st.warning(_psr_msg)
            _psr_c1, _psr_c2, _psr_c3 = st.columns([1, 1, 3])
            with _psr_c1:
                if st.button("Run anyway", type="primary", key="ps_run_anyway", use_container_width=True):
                    st.session_state['_people_search_force_run'] = _psr['hash']
                    st.session_state.pop('_people_search_repeat', None)
                    st.rerun()
            with _psr_c2:
                if st.button("Cancel", key="ps_cancel_repeat", use_container_width=True):
                    st.session_state.pop('_people_search_repeat', None)
                    st.rerun()

        # --- Helper to get effective value for a field ---
        def _effective_val(input_key, expanded_key):
            """Merge text input + AI multiselect selections. Both contribute."""
            parts = []
            # Manual text input (comma-separated values)
            raw = st.session_state.get(input_key, '').strip()
            if raw:
                parts.extend([v.strip() for v in raw.split(',') if v.strip()])
            # AI-selected values
            sel = st.session_state.get(f"sel_{expanded_key}", [])
            if sel:
                parts.extend(sel)
            # Dedupe preserving order
            seen = set()
            unique = []
            for p in parts:
                if p.lower() not in seen:
                    seen.add(p.lower())
                    unique.append(p)
            return ', '.join(unique)

        # Handle clear filters
        if clear_submitted:
            keys_to_clear = [
                'crustdata_search_results', 'crustdata_search_cursor',
                'crustdata_search_total', 'crustdata_search_selected',
                'crustdata_search_credits_used', 'crustdata_search_mode',
                '_last_semantic_params',
                'crust_search_exact_company', 'crust_search_function', 'crust_search_industry',
                'crust_search_sort', 'crust_search_country', 'crust_search_continent',
                'crust_search_geo_city', 'crust_search_geo_radius', 'crust_search_min_connections',
                'expanded_titles', 'expanded_companies', 'expanded_locations',
                'expanded_keywords', 'expanded_skills', 'expanded_past_titles',
                'expanded_past_companies', 'expanded_schools',
                'sel_expanded_titles', 'sel_expanded_companies', 'sel_expanded_locations',
                'sel_expanded_keywords', 'sel_expanded_skills', 'sel_expanded_past_titles',
                'sel_expanded_past_companies', 'sel_expanded_schools',
                'custom_expanded_titles', 'custom_expanded_companies', 'custom_expanded_locations',
                'custom_expanded_keywords', 'custom_expanded_skills', 'custom_expanded_past_titles',
                'custom_expanded_past_companies', 'custom_expanded_schools',
            ]
            for key in keys_to_clear:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

        # Handle search. A confirmed re-run arrives via the force flag (set by the
        # "Run anyway" button) rather than the transient Search button press.
        _ps_force_hash = st.session_state.pop('_people_search_force_run', None)
        if search_submitted or _ps_force_hash:
            effective_title = _effective_val('crust_search_title', 'expanded_titles')
            effective_company = _effective_val('crust_search_company', 'expanded_companies')
            effective_location = _effective_val('crust_search_location', 'expanded_locations')
            effective_keywords = _effective_val('crust_search_keywords', 'expanded_keywords')
            effective_skills = _effective_val('crust_search_skills', 'expanded_skills')
            effective_past_titles = _effective_val('crust_search_past_titles', 'expanded_past_titles')
            effective_past_companies = _effective_val('crust_search_past_companies', 'expanded_past_companies')
            effective_school = _effective_val('crust_search_school', 'expanded_schools')

            # Build skill groups from effective skills value
            skill_groups = [effective_skills] if effective_skills else []

            has_filters = any([
                effective_title, effective_company, effective_location,
                search_seniority, search_headcount,
                search_exp_min > 0, search_exp_max > 0,
                effective_keywords, skill_groups, effective_past_titles,
                effective_past_companies, effective_school,
                search_recently_changed, search_has_email,
                search_function, search_industry, search_country, search_continent,
                search_geo_city and search_geo_radius > 0, search_min_connections > 0,
            ])

            _SORT_MAP = {
                "Connections (most first)": [{"column": "num_of_connections", "order": "desc"}],
                "Experience (most first)": [{"column": "years_of_experience_raw", "order": "desc"}],
                "No sort": None,
            }
            search_sorts = _SORT_MAP.get(search_sort, [{"column": "num_of_connections", "order": "desc"}])

            if not has_filters:
                st.warning("Please provide at least one search filter.")
            else:
                # Geo only affects results when BOTH city and radius are set —
                # build_search_filters drops it otherwise. Mirror that here so a
                # leftover city with radius 0 doesn't make an otherwise-identical
                # search hash differently (and miss the repeat guard).
                _ps_geo_active = bool(search_geo_city) and int(search_geo_radius or 0) > 0

                # Snapshot the filters for history + the repeat guard. Keyed by the
                # Search-tab widget keys so a saved search reloads one-to-one.
                _ps_filters_state = {
                    'crust_search_title': effective_title,
                    'crust_search_company': effective_company,
                    'crust_search_location': effective_location,
                    'crust_search_keywords': effective_keywords,
                    'crust_search_skills': effective_skills,
                    'crust_search_past_titles': effective_past_titles,
                    'crust_search_past_companies': effective_past_companies,
                    'crust_search_school': effective_school,
                    'crust_search_geo_city': search_geo_city if _ps_geo_active else '',
                    'crust_search_seniority': search_seniority,
                    'crust_search_headcount': search_headcount,
                    'crust_search_function': search_function,
                    'crust_search_industry': search_industry,
                    'crust_search_country': search_country,
                    'crust_search_continent': search_continent,
                    # exact_company only matters when a company is set — build_search_filters
                    # ignores it otherwise, so don't let a stray checkbox split the hash.
                    'crust_search_exact_company': bool(search_exact_company) and bool(effective_company),
                    'crust_search_recently_changed': search_recently_changed,
                    'crust_search_has_email': search_has_email,
                    'crust_search_geo_radius': search_geo_radius if _ps_geo_active else 0,
                    'crust_search_exp_min': search_exp_min,
                    'crust_search_exp_max': search_exp_max,
                    'crust_search_min_connections': search_min_connections,
                    'crust_search_sort': search_sort,
                    'crust_search_limit': search_limit,
                }
                _ps_hash = hash_search_filters(_ps_filters_state)
                _ps_summary = summarize_search_filters(_ps_filters_state)

                # Repeat guard: if these exact filters were run before AND this
                # isn't a confirmed re-run, show the warning and stop here.
                if _ps_force_hash != _ps_hash:
                    _ps_prior = find_people_search(_ps_db, _ps_username, _ps_hash) if _ps_db else None
                    if _ps_prior:
                        st.session_state['_people_search_repeat'] = {
                            'hash': _ps_hash,
                            'summary': _ps_summary,
                            'last_run_at': _ps_prior.get('last_run_at'),
                            'result_count': _ps_prior.get('result_count'),
                            'run_count': _ps_prior.get('run_count'),
                        }
                        st.rerun()
                else:
                    st.session_state.pop('_people_search_repeat', None)

                filters = build_search_filters(
                    title=effective_title if effective_title else None,
                    company=effective_company if effective_company else None,
                    location=effective_location if effective_location else None,
                    seniority=search_seniority if search_seniority else None,
                    headcount=search_headcount if search_headcount else None,
                    experience_min=search_exp_min if search_exp_min > 0 else None,
                    experience_max=search_exp_max if search_exp_max > 0 else None,
                    skill_groups=skill_groups if skill_groups else None,
                    keywords=effective_keywords if effective_keywords else None,
                    past_companies=effective_past_companies if effective_past_companies else None,
                    past_titles=effective_past_titles if effective_past_titles else None,
                    school=effective_school if effective_school else None,
                    recently_changed_jobs=search_recently_changed if search_recently_changed else None,
                    has_verified_email=search_has_email if search_has_email else None,
                    function_categories=search_function if search_function else None,
                    industries=search_industry if search_industry else None,
                    country=search_country if search_country else None,
                    continent=search_continent if search_continent else None,
                    geo_city=search_geo_city if search_geo_city else None,
                    geo_radius_km=int(search_geo_radius) if search_geo_radius > 0 else None,
                    min_connections=int(search_min_connections) if search_min_connections > 0 else None,
                    exact_company=search_exact_company,
                    # not_relevant and blacklist are applied client-side in _clean_page()
                    # rather than as API filters — sending them to Crustdata changes its
                    # scoring universe and degrades result quality.
                )

                try:
                    # First request (API caps at 1000 per request)
                    progress_placeholder = st.empty()
                    progress_placeholder.info("Searching Crustdata database...")

                    _exclude_urls = st.session_state.get('past_candidates_urls_for_search') or None
                    results = _active_search_people_db(filters, limit=min(search_limit, 1000), sorts=search_sorts, api_key=api_key, exclude_profiles=_exclude_urls)

                    if results.get("profiles"):
                        # Name filter applied inline during pagination so we keep
                        # fetching until we have the requested count (not just raw API count).
                        def _norm_name(n):
                            n = str(n).lower().strip()
                            n = ' '.join(n.split())
                            n = re.sub(r'[^\w\s]', '', n)
                            return n.strip()

                        _pc_names_set = set(st.session_state.get('past_candidates_names_for_search') or [])
                        _bl_raw = [c for c in (st.session_state.get('blacklist_for_search') or []) if c and str(c).strip() and len(str(c).strip()) >= 3]
                        _nr_raw = [c for c in (st.session_state.get('nr_for_search') or []) if c and str(c).strip() and len(str(c).strip()) >= 3]

                        def _name_of(p):
                            return _norm_name(p.get('name') or f"{p.get('first_name', '')} {p.get('last_name', '')}")

                        def _get_co(p):
                            emp = pick_current_employer(p.get('current_employers'))
                            return (emp.get('employer_name') or emp.get('name', '')) if emp else ''

                        def _is_bl(p):
                            if not _bl_raw:
                                return False
                            co = _get_co(p)
                            return bool(co) and _company_matches_filter_list(co, _bl_raw)

                        def _is_nr(p):
                            if not _nr_raw:
                                return False
                            co = _get_co(p)
                            return bool(co) and _company_matches_filter_list(co, _nr_raw)

                        # Track why profiles get removed so the user sees the breakdown
                        # instead of an unexplained drop (e.g. 706 matching -> 570 loaded).
                        _removed_counts = {'past_candidates': 0, 'blacklist': 0, 'not_relevant': 0}

                        def _clean_page(profiles):
                            out = profiles
                            if _pc_names_set:
                                _before = len(out)
                                out = [p for p in out if _name_of(p) not in _pc_names_set]
                                _removed_counts['past_candidates'] += _before - len(out)
                            if _bl_raw:
                                _before = len(out)
                                out = [p for p in out if not _is_bl(p)]
                                _removed_counts['blacklist'] += _before - len(out)
                            if _nr_raw:
                                _before = len(out)
                                out = [p for p in out if not _is_nr(p)]
                                _removed_counts['not_relevant'] += _before - len(out)
                            return out

                        all_profiles = _clean_page(results['profiles'])
                        total_count = results.get('total_count', len(results['profiles']))
                        total_credits = results.get('credits_used', 0)
                        cursor = results.get('cursor')

                        # Auto-paginate if limit > 1000 and more results available
                        target = min(search_limit, total_count)
                        while cursor and len(all_profiles) < target:
                            progress_placeholder.info(f"Loading profiles... {len(all_profiles):,} / {target:,}")
                            remaining = target - len(all_profiles)
                            page_results = _active_search_people_db(
                                filters,
                                limit=min(remaining, 1000),
                                cursor=cursor,
                                sorts=search_sorts,
                                api_key=api_key,
                                exclude_profiles=_exclude_urls,
                            )
                            if page_results.get("profiles"):
                                all_profiles.extend(_clean_page(page_results['profiles']))
                                total_credits += page_results.get('credits_used', 0)
                                cursor = page_results.get('cursor')
                            else:
                                break

                        loaded_count = len(all_profiles)
                        st.session_state['crustdata_search_results'] = all_profiles
                        st.session_state['crustdata_search_cursor'] = cursor
                        st.session_state['crustdata_search_total'] = total_count
                        st.session_state['crustdata_search_credits_used'] = total_credits
                        st.session_state['crustdata_search_selected'] = list(range(loaded_count))
                        # Tells Load More which endpoint/cursor pairing to use — this is a
                        # filter search, not a description (semantic) search.
                        st.session_state['crustdata_search_mode'] = 'filter'
                        # Persist effective filter values so Load More uses the same payload
                        st.session_state['_last_search_params'] = {
                            'title': effective_title,
                            'company': effective_company,
                            'location': effective_location,
                            'seniority': search_seniority,
                            'headcount': search_headcount,
                            'experience_min': search_exp_min if search_exp_min > 0 else None,
                            'experience_max': search_exp_max if search_exp_max > 0 else None,
                            'skill_groups': skill_groups if skill_groups else None,
                            'keywords': effective_keywords,
                            'past_companies': effective_past_companies,
                            'past_titles': effective_past_titles,
                            'school': effective_school,
                            'recently_changed_jobs': search_recently_changed if search_recently_changed else None,
                            'has_verified_email': search_has_email if search_has_email else None,
                            'function_categories': search_function if search_function else None,
                            'industries': search_industry if search_industry else None,
                            'country': search_country if search_country else None,
                            'continent': search_continent if search_continent else None,
                            'geo_city': search_geo_city if search_geo_city else None,
                            'geo_radius_km': int(search_geo_radius) if search_geo_radius > 0 else None,
                            'min_connections': int(search_min_connections) if search_min_connections > 0 else None,
                            'exact_company': search_exact_company,
                            'limit': search_limit,
                            'sorts': search_sorts,
                            # Snapshot exclusions so Load More uses the same lists even if
                            # the user changes the sheet URL between search and Load More.
                            'nr_for_search': st.session_state.get('nr_for_search'),
                            'blacklist_for_search': st.session_state.get('blacklist_for_search'),
                            'past_candidates_urls_for_search': st.session_state.get('past_candidates_urls_for_search'),
                            'past_candidates_names_for_search': st.session_state.get('past_candidates_names_for_search'),
                        }
                        # Defer DB save to after rerun so results render immediately
                        st.session_state['_pending_initial_save'] = True
                        _removed_total = sum(_removed_counts.values())
                        if _removed_total > 0:
                            _parts = []
                            if _removed_counts['past_candidates']:
                                _parts.append(f"{_removed_counts['past_candidates']:,} past candidates")
                            if _removed_counts['blacklist']:
                                _parts.append(f"{_removed_counts['blacklist']:,} blacklisted companies")
                            if _removed_counts['not_relevant']:
                                _parts.append(f"{_removed_counts['not_relevant']:,} not-relevant companies")
                            _removed_note = f" — {_removed_total:,} removed ({', '.join(_parts)})"
                        else:
                            _removed_note = ""
                        st.session_state['_search_loaded_msg'] = f"Loaded **{loaded_count:,}** profiles (of {total_count:,} total matching){_removed_note}"
                    else:
                        progress_placeholder.empty()
                        st.session_state['crustdata_search_results'] = []
                        st.session_state['crustdata_search_total'] = 0
                        st.info("No profiles found matching your criteria. Try adjusting your filters.")

                    # Collect the links of the profiles this run actually loaded, so the
                    # search can later be reloaded straight from the profiles table for
                    # free (no Crustdata call). Same URL precedence as save_enriched_*.
                    _ps_result_urls = []
                    try:
                        _ps_seen_u = set()
                        for _p in (st.session_state.get('crustdata_search_results') or []):
                            _raw_u = (
                                _p.get('linkedin_flagship_url')
                                or _p.get('flagship_profile_url')
                                or _p.get('linkedin_profile_url')
                                or _p.get('linkedin_url')
                            )
                            _nu = normalize_linkedin_url(_raw_u) if _raw_u else None
                            if _nu and _nu not in _ps_seen_u:
                                _ps_seen_u.add(_nu)
                                _ps_result_urls.append(_nu)
                    except Exception:
                        _ps_result_urls = []

                    # Auto-save this search to history (deduped per user by hash;
                    # a repeat just bumps run_count + last_run_at). Never fatal.
                    try:
                        record_people_search(
                            _ps_db, _ps_username, _ps_filters_state, _ps_hash, _ps_summary,
                            int(st.session_state.get('crustdata_search_total') or 0),
                            result_urls=_ps_result_urls or None,
                        )
                        # Remember this search's history identity so Load More can
                        # refresh its saved result_urls with the extra pages it pulls.
                        st.session_state['_ps_history_ref'] = {
                            'username': _ps_username,
                            'filters_hash': _ps_hash,
                        }
                    except Exception:
                        pass

                    _get_crustdata_credits_cached.clear()
                    progress_placeholder.empty()
                    st.rerun()

                except Exception as e:
                    st.error(f"Search failed: {str(e)}")

        # ===== Results Section =====
        if 'crustdata_search_results' in st.session_state and st.session_state['crustdata_search_results']:
            st.divider()
            results = st.session_state['crustdata_search_results']
            total_count = st.session_state.get('crustdata_search_total', len(results))
            credits_used = st.session_state.get('crustdata_search_credits_used', 0)

            # Show search success message (deferred from search rerun)
            if '_search_loaded_msg' in st.session_state:
                st.success(st.session_state.pop('_search_loaded_msg'))

            # Show deferred Load More save result (stored before st.rerun())
            if '_load_more_save_msg' in st.session_state:
                st.caption(st.session_state.pop('_load_more_save_msg'))

            # Background DB save — fires in a daemon thread so results render immediately
            if st.session_state.get('_pending_initial_save'):
                del st.session_state['_pending_initial_save']
                db_client = _get_db_client()
                if db_client:
                    def _bg_save(client, profiles):
                        try:
                            save_enriched_profiles_bulk(client, profiles)
                        except Exception:
                            pass
                    threading.Thread(target=_bg_save, args=(db_client, list(results)), daemon=True).start()

            # Results header
            res_col1, res_col2 = st.columns([2, 1])
            with res_col1:
                st.markdown(f"##### Results")
                st.caption(f"Showing {len(results)} of {total_count:,} profiles | Credits used: {credits_used}")
                show_all_cols = st.checkbox("Show all columns", key="crust_show_all_cols")
            with res_col2:
                # Select/Deselect all
                sel_col1, sel_col2 = st.columns(2)
                with sel_col1:
                    st.button("Select All", key="crust_select_all", use_container_width=True,
                              on_click=_cb_select_all, args=('crustdata_search_selected', len(results)))
                with sel_col2:
                    st.button("Deselect All", key="crust_deselect_all", use_container_width=True,
                              on_click=_cb_deselect_all, args=('crustdata_search_selected',))

            # Build dataframe for display
            display_data = []
            for i, profile in enumerate(results):
                # Extract current employer info — most recent when multiple
                current_company = ""
                current_title = ""
                seniority = ""
                company_size = ""
                emp = pick_current_employer(profile.get("current_employers"))
                if emp:
                    current_company = emp.get("employer_name") or emp.get("name", "")
                    current_title = emp.get("employee_title") or emp.get("title", "")
                    seniority = emp.get("seniority_level", "")
                    company_size = emp.get("company_headcount_range", "")

                # Fallback to top-level fields
                if not current_title:
                    current_title = profile.get("title", "")
                if not current_company:
                    current_company = profile.get("company", "")

                # Use flagship_profile_url (clean URL) not linkedin_profile_url (URN format)
                linkedin_url = profile.get("flagship_profile_url") or profile.get("linkedin_flagship_url") or profile.get("linkedin_profile_url", "")

                # Extract top 3 skills
                skills_list = profile.get("skills", [])
                top_skills = ", ".join(skills_list[:3]) if skills_list else ""

                # Headline
                headline = profile.get("headline", "")

                # Extra fields for "show all columns" mode
                all_skills = ", ".join(skills_list) if skills_list else ""
                connections = profile.get("num_of_connections", "") or ""
                country = profile.get("location_country", "") or ""
                function_cat = (emp.get("function_category", "") if emp else "") or ""
                industries_raw = (emp.get("company_industries") if emp else None) or []
                if isinstance(industries_raw, str):
                    industries_raw = [industries_raw]
                industry = ", ".join(str(x) for x in industries_raw)
                summary = profile.get("summary", "") or ""

                # Full work history: current + past employers with dates and role descriptions
                _hist_parts = []
                for _e in (profile.get("current_employers") or []):
                    _t = _e.get("employee_title") or _e.get("title", "")
                    _c = _e.get("employer_name") or _e.get("name", "")
                    _s = (_e.get("start_date") or "")[:4]
                    _line = f"{_t} @ {_c}" if _t or _c else ""
                    if _s:
                        _line += f" ({_s}–present)"
                    _desc = _e.get("employee_description") or _e.get("description") or ""
                    if _desc:
                        _line += f"\n{_desc}"
                    if _line.strip():
                        _hist_parts.append(_line)
                for _e in (profile.get("past_employers") or []):
                    _t = _e.get("employee_title") or _e.get("title", "")
                    _c = _e.get("employer_name") or _e.get("name", "")
                    _s = (_e.get("start_date") or "")[:4]
                    _en = (_e.get("end_date") or "")[:4]
                    _dur = _e.get("duration", "")
                    _line = f"{_t} @ {_c}" if _t or _c else ""
                    if _s:
                        _date = f"{_s}–{_en}" if _en else _s
                        if _dur:
                            _date += f" · {_dur}"
                        _line += f" ({_date})"
                    _desc = _e.get("employee_description") or _e.get("description") or ""
                    if _desc:
                        _line += f"\n{_desc}"
                    if _line.strip():
                        _hist_parts.append(_line)
                work_history = "\n\n".join(_hist_parts)

                _all_emp_raw = profile.get('all_employers') or profile.get('past_employers') or []
                _all_emp_names = []
                for _ae in _all_emp_raw:
                    _n = (_ae.get('name') or _ae.get('employer_name') or '') if isinstance(_ae, dict) else str(_ae)
                    if _n: _all_emp_names.append(_n)
                all_employers_display = ', '.join(_all_emp_names)

                display_data.append({
                    "idx": i,
                    "Select": i in st.session_state.get('crustdata_search_selected', []),
                    "Fit": profile.get("_fit", ""),
                    "Name": profile.get("name", ""),
                    "Headline": headline[:60] + "..." if len(headline) > 60 else headline,
                    "Title": current_title,
                    "Company": current_company,
                    "Seniority": seniority,
                    "Size": company_size,
                    "Location": profile.get("region", ""),
                    "Country": country,
                    "Function": function_cat,
                    "Industry": industry,
                    "Exp": f"{profile.get('years_of_experience_raw', '')}y" if profile.get('years_of_experience_raw') else "",
                    "Connections": connections,
                    "Skills": top_skills,
                    "All Skills": all_skills,
                    "All Employers": all_employers_display,
                    "Summary": summary,
                    "Work History": work_history,
                    "LinkedIn": linkedin_url,
                })

            display_df = pd.DataFrame(display_data)

            # Display with data_editor for selection
            _compact_cols = ["Select", "Name", "Title", "Headline", "Company", "Location", "Size", "Exp", "Skills", "LinkedIn"]
            _all_cols = ["Select", "Name", "Title", "Headline", "Company", "All Employers", "Location", "Country", "Seniority", "Function", "Industry", "Size", "Exp", "Connections", "All Skills", "Summary", "Work History", "LinkedIn"]
            # "Fit" (Crustdata's relevance tier: strong/possible/weak) is only
            # populated for description-search results — only show the column
            # when at least one row actually has a value, so filter search
            # results don't display an all-blank column.
            if display_df.get("Fit", pd.Series(dtype=str)).astype(str).str.strip().any():
                _compact_cols.insert(1, "Fit")
                _all_cols.insert(1, "Fit")
            _show_cols = _all_cols if show_all_cols else _compact_cols

            edited_df = st.data_editor(
                display_df[_show_cols],
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Select": st.column_config.CheckboxColumn("Select", default=True),
                    "Fit": st.column_config.TextColumn("Fit", width="small", help="Crustdata's relevance tier for description search: strong/possible/weak"),
                    "Name": st.column_config.TextColumn("Name", width="small"),
                    "Title": st.column_config.TextColumn("Title", width="small"),
                    "Headline": st.column_config.TextColumn("Headline", width="medium"),
                    "Company": st.column_config.TextColumn("Company", width="small"),
                    "Location": st.column_config.TextColumn("Location", width="small"),
                    "Size": st.column_config.TextColumn("Size", width="small"),
                    "Exp": st.column_config.TextColumn("Exp", width="small"),
                    "Skills": st.column_config.TextColumn("Skills", width="medium"),
                    "LinkedIn": st.column_config.LinkColumn("LinkedIn", width="small", display_text="View"),
                    "Country": st.column_config.TextColumn("Country", width="small"),
                    "Seniority": st.column_config.TextColumn("Seniority", width="small"),
                    "Function": st.column_config.TextColumn("Function", width="small"),
                    "Industry": st.column_config.TextColumn("Industry", width="medium"),
                    "Connections": st.column_config.TextColumn("Connections", width="small"),
                    "All Skills": st.column_config.TextColumn("All Skills", width="large"),
                    "All Employers": st.column_config.TextColumn("All Employers", width="large"),
                    "Summary": st.column_config.TextColumn("Summary", width="large"),
                    "Work History": st.column_config.TextColumn("Work History", width="large"),
                },
                disabled=["Fit", "Name", "Title", "Headline", "Company", "Location", "Size", "Exp", "Skills", "LinkedIn",
                          "Country", "Seniority", "Function", "Industry", "Connections", "All Skills", "All Employers", "Summary", "Work History"],
                key="crust_results_editor"
            )

            # Update selected indices based on editor state
            selected_indices = edited_df[edited_df["Select"] == True].index.tolist()
            st.session_state['crustdata_search_selected'] = selected_indices

            # Selection count
            st.caption(f"**{len(selected_indices)}** profiles selected")

            # Action buttons
            st.divider()
            action_col1, action_col2, action_col3, action_col4 = st.columns([2, 1, 1, 1.3])

            with action_col1:
                # Send to Filter Tab button
                if st.button("Send to Filter Tab", type="primary", use_container_width=True, key="crust_send_to_filter"):
                    if not selected_indices:
                        st.warning("No profiles selected. Select at least one profile.")
                    else:
                        # Get selected profiles
                        selected_profiles = [results[i] for i in selected_indices]

                        # Normalize to DataFrame
                        normalized_df = normalize_search_results_to_df(selected_profiles)

                        if not normalized_df.empty:
                            clear_results_derived_state(st.session_state)
                            st.session_state['results_df'] = normalized_df
                            st.session_state['original_results_df'] = normalized_df.copy()
                            st.session_state['enriched_df'] = normalized_df.copy()
                            st.session_state['_data_source'] = 'crustdata_search'
                            # Search results are complete (compact=false) — treat as enriched
                            # so AI Screen can use them without a separate enrichment step.
                            st.session_state['enriched_df'] = normalized_df.copy()

                            st.success(f"Sent **{len(normalized_df)}** profiles to Filter tab!")
                            st.info("Navigate to **1. Load** or **2. Filter** tab to continue processing.")
                        else:
                            st.error("Failed to normalize profiles. Check the data.")

            with action_col2:
                # Load More button (pagination)
                cursor = st.session_state.get('crustdata_search_cursor')
                if cursor:
                    if st.button("Load More", use_container_width=True, key="crust_load_more"):
                        with st.spinner("Loading more results..."):
                            try:
                                # Which search produced the current results decides which
                                # endpoint/cursor pairing Load More must keep using — the
                                # semantic endpoint's cursor is not valid on the filter
                                # endpoint (and vice versa).
                                _lm_mode = st.session_state.get('crustdata_search_mode', 'filter')
                                _lm_p = st.session_state.get('_last_search_params', {})

                                if _lm_mode == 'semantic':
                                    _lm_sem_p = st.session_state.get('_last_semantic_params', {})

                                    def _lm_fetch(cursor_val):
                                        raw = search_people_semantic(
                                            _lm_sem_p.get('query', ''),
                                            limit=_lm_sem_p.get('limit', 20),
                                            cursor=cursor_val,
                                            api_key=api_key,
                                        )
                                        raw['profiles'] = [
                                            semantic_profile_to_legacy_shape(p)
                                            for p in (raw.get('profiles') or [])
                                        ]
                                        return raw

                                    # Semantic search has no server-side exclude_profiles
                                    # param wired up (see search_people_semantic) — exclusion
                                    # is entirely client-side here, same as the initial
                                    # description search above.
                                    _lm_names_set = {
                                        str(n).lower().strip()
                                        for n in (st.session_state.get('past_candidates_names_for_search') or [])
                                    }
                                    _lm_pc_urls = {
                                        normalize_linkedin_url(u)
                                        for u in (st.session_state.get('past_candidates_urls_for_search') or [])
                                        if u
                                    }
                                    _lm_pc_urls.discard(None)
                                    _lm_bl_raw = [c for c in (st.session_state.get('blacklist_for_search') or []) if c and str(c).strip() and len(str(c).strip()) >= 3]
                                    _lm_nr_raw = [c for c in (st.session_state.get('nr_for_search') or []) if c and str(c).strip() and len(str(c).strip()) >= 3]
                                else:
                                    # Use persisted effective params from initial search so pagination
                                    # uses the same filters including AI-expanded values, not raw widgets.
                                    filters = build_search_filters(
                                        title=_lm_p.get('title'),
                                        company=_lm_p.get('company'),
                                        location=_lm_p.get('location'),
                                        seniority=_lm_p.get('seniority'),
                                        headcount=_lm_p.get('headcount'),
                                        experience_min=_lm_p.get('experience_min'),
                                        experience_max=_lm_p.get('experience_max'),
                                        skill_groups=_lm_p.get('skill_groups'),
                                        keywords=_lm_p.get('keywords'),
                                        past_companies=_lm_p.get('past_companies'),
                                        past_titles=_lm_p.get('past_titles'),
                                        school=_lm_p.get('school'),
                                        recently_changed_jobs=_lm_p.get('recently_changed_jobs'),
                                        has_verified_email=_lm_p.get('has_verified_email'),
                                        function_categories=_lm_p.get('function_categories'),
                                        industries=_lm_p.get('industries'),
                                        country=_lm_p.get('country'),
                                        continent=_lm_p.get('continent'),
                                        geo_city=_lm_p.get('geo_city'),
                                        geo_radius_km=_lm_p.get('geo_radius_km'),
                                        min_connections=_lm_p.get('min_connections'),
                                        exact_company=_lm_p.get('exact_company', False),
                                        # not_relevant and blacklist applied client-side in _lm_clean()
                                    )

                                    def _lm_fetch(cursor_val):
                                        return _active_search_people_db(
                                            filters,
                                            limit=_lm_p.get('limit', 100),
                                            cursor=cursor_val,
                                            sorts=_lm_p.get('sorts'),
                                            api_key=api_key,
                                            exclude_profiles=_lm_p.get('past_candidates_urls_for_search') or None,
                                        )

                                    _lm_names_set = set(_lm_p.get('past_candidates_names_for_search') or [])
                                    # Already excluded server-side via exclude_profiles above.
                                    _lm_pc_urls = set()
                                    _lm_bl_raw = [c for c in (_lm_p.get('blacklist_for_search') or []) if c and str(c).strip() and len(str(c).strip()) >= 3]
                                    _lm_nr_raw = [c for c in (_lm_p.get('nr_for_search') or []) if c and str(c).strip() and len(str(c).strip()) >= 3]

                                more_results = _lm_fetch(cursor)

                                # Post-filter helpers (same exclusions as original search)
                                def _lm_norm_name(n):
                                    n = str(n).lower().strip()
                                    n = ' '.join(n.split())
                                    n = re.sub(r'[^\w\s]', '', n)
                                    return n.strip()

                                def _lm_get_co(p):
                                    emp = pick_current_employer(p.get('current_employers'))
                                    return (emp.get('employer_name') or emp.get('name', '')) if emp else ''

                                def _lm_url_of(p):
                                    raw = (
                                        p.get('flagship_profile_url')
                                        or p.get('linkedin_flagship_url')
                                        or p.get('linkedin_profile_url')
                                    )
                                    return normalize_linkedin_url(raw) if raw else None

                                def _lm_is_excluded(p):
                                    co = _lm_get_co(p)
                                    if _lm_bl_raw and co and _company_matches_filter_list(co, _lm_bl_raw):
                                        return True
                                    if _lm_nr_raw and co and _company_matches_filter_list(co, _lm_nr_raw):
                                        return True
                                    return False

                                def _lm_clean(profiles):
                                    out = profiles
                                    if _lm_names_set:
                                        out = [p for p in out if _lm_norm_name(p.get('name') or f"{p.get('first_name','')} {p.get('last_name','')}") not in _lm_names_set]
                                    if _lm_pc_urls:
                                        out = [p for p in out if _lm_url_of(p) not in _lm_pc_urls]
                                    if _lm_bl_raw or _lm_nr_raw:
                                        out = [p for p in out if not _lm_is_excluded(p)]
                                    return out

                                # Keep fetching until we have at least one valid profile or run out of pages
                                current_results = st.session_state.get('crustdata_search_results', [])
                                new_profiles = _lm_clean(more_results.get('profiles') or [])
                                next_cursor = more_results.get('cursor')
                                total_lm_credits = more_results.get('credits_used', 0)
                                while not new_profiles and next_cursor:
                                    extra = _lm_fetch(next_cursor)
                                    new_profiles = _lm_clean(extra.get('profiles') or [])
                                    next_cursor = extra.get('cursor')
                                    total_lm_credits += extra.get('credits_used', 0)

                                if new_profiles or more_results.get("profiles"):
                                    st.session_state['crustdata_search_results'] = current_results + new_profiles
                                    st.session_state['crustdata_search_cursor'] = next_cursor
                                    st.session_state['crustdata_search_credits_used'] = (
                                        st.session_state.get('crustdata_search_credits_used', 0) +
                                        total_lm_credits
                                    )
                                    # Select new profiles by default
                                    current_selected = st.session_state.get('crustdata_search_selected', [])
                                    new_indices = list(range(len(current_results), len(current_results) + len(new_profiles)))
                                    st.session_state['crustdata_search_selected'] = current_selected + new_indices

                                    # Auto-save new page to Supabase (store result for display after rerun)
                                    try:
                                        db_client = _get_db_client()
                                        if not db_client:
                                            st.session_state['_load_more_save_msg'] = "DB save skipped: no database connection"
                                        else:
                                            bulk_result = save_enriched_profiles_bulk(db_client, new_profiles)
                                            st.session_state['_load_more_save_msg'] = f"Saved {bulk_result['saved']}/{len(new_profiles)} to database"
                                    except Exception as db_err:
                                        st.session_state['_load_more_save_msg'] = f"DB save skipped: {db_err}"

                                    # Refresh the saved-search row's result_urls so
                                    # "Load saved (free)" restores the full set including
                                    # these extra pages, not just the first page.
                                    try:
                                        _ref = st.session_state.get('_ps_history_ref') or {}
                                        if _ref.get('filters_hash'):
                                            _lm_urls = []
                                            _lm_seen = set()
                                            for _p in (st.session_state.get('crustdata_search_results') or []):
                                                _raw_u = (
                                                    _p.get('linkedin_flagship_url')
                                                    or _p.get('flagship_profile_url')
                                                    or _p.get('linkedin_profile_url')
                                                    or _p.get('linkedin_url')
                                                )
                                                _nu = normalize_linkedin_url(_raw_u) if _raw_u else None
                                                if _nu and _nu not in _lm_seen:
                                                    _lm_seen.add(_nu)
                                                    _lm_urls.append(_nu)
                                            if _lm_urls:
                                                update_people_search_urls(
                                                    _get_db_client(), _ref['username'],
                                                    _ref['filters_hash'], _lm_urls,
                                                )
                                    except Exception:
                                        pass
                                    _get_crustdata_credits_cached.clear()
                                    st.rerun()
                                else:
                                    st.info("No more results available.")
                                    st.session_state['crustdata_search_cursor'] = None

                            except Exception as e:
                                st.error(f"Failed to load more: {str(e)}")
                else:
                    st.button("Load More", disabled=True, use_container_width=True, key="crust_load_more_disabled")

            with action_col3:
                # Direct download button for selected profiles
                if selected_indices:
                    selected_profiles = [results[i] for i in selected_indices]
                    export_df = normalize_search_results_to_df(selected_profiles)
                    export_df = prepare_df_for_export(export_df)
                    export_data = export_df.to_csv(index=False).encode('utf-8-sig')

                    st.download_button(
                        f"Download CSV ({len(selected_indices)})",
                        export_data,
                        f"crustdata_search_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                        "text/csv",
                        use_container_width=True,
                        key="crust_download_csv"
                    )
                else:
                    st.button("Download CSV (0)", disabled=True, use_container_width=True, key="crust_download_disabled")

            with action_col4:
                # Full-details CSV: same selected profiles, but with a
                # crustdata_raw_json column carrying the complete raw
                # Crustdata response per person — everything Crustdata
                # returned, not just the flattened columns. No extra API
                # calls: the raw response for each result is already sitting
                # in memory (see build_search_full_details_export_df).
                if selected_indices:
                    selected_profiles_fd = [results[i] for i in selected_indices]
                    full_export_df = build_search_full_details_export_df(selected_profiles_fd)
                    full_export_data = full_export_df.to_csv(index=False).encode('utf-8-sig')

                    st.download_button(
                        f"📄 Full Details ({len(selected_indices)})",
                        full_export_data,
                        f"crustdata_search_full_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                        "text/csv",
                        use_container_width=True,
                        key="crust_download_full_details_csv",
                        help="Includes a crustdata_raw_json column with everything Crustdata "
                             "returned for each person. Description-search results still won't "
                             "have skills/summary/job descriptions in it — Crustdata itself "
                             "doesn't send those back for that search type."
                    )
                else:
                    st.button("📄 Full Details (0)", disabled=True, use_container_width=True, key="crust_download_full_details_disabled")

# ========== TAB 1: Upload ==========
with tab_upload:
    # ===== Resume Last Session =====
    has_local_session = _get_session_file().exists() or _LEGACY_SESSION_FILE.exists()

    # Show restore options if there's data to restore
    if has_local_session or HAS_DATABASE:
        with st.expander("Resume Last Session", expanded=False):
            # Session restore (from Supabase on cloud, local file for dev)
            if has_local_session or HAS_DATABASE:
                st.markdown("**Saved Session** (includes filtered data)")
                col_local1, col_local2 = st.columns([3, 1])
                with col_local1:
                    if st.button("Restore Last Session", key="restore_local_session", type="primary"):
                        success, error_msg = load_session_state()
                        if success:
                            st.success("Session restored!")
                            st.rerun()
                        else:
                            st.error(f"Failed to restore: {error_msg}")
                with col_local2:
                    if st.button("Clear", key="clear_local_session"):
                        clear_session_file()
                        st.success("Session cleared")
                        st.rerun()
                st.divider()

            # Database restore
            if HAS_DATABASE:
                try:
                    db_client = _get_db_client()
                    if db_client:
                        from db import get_profiles_by_enrichment_status

                        # Use module-level cached function for efficiency
                        enriched_count = _get_db_restore_counts_cached()

                        if enriched_count > 0:
                            st.markdown("**From Database**")
                            st.caption("Load enriched profiles from Supabase (screening is always fresh per JD)")

                            if st.button(f"Load Enriched ({enriched_count})", key="resume_enriched"):
                                profiles = get_profiles_by_enrichment_status(db_client, "enriched", limit=500)  # Reduced for memory
                                if profiles:
                                    df = profiles_to_dataframe(profiles)
                                    st.session_state['results_df'] = df
                                    st.session_state['original_results_df'] = df.copy()
                                    st.session_state['enriched_df'] = df
                                    # Cache raw profile dicts so screening doesn't re-fetch from DB
                                    # Only for small sets (<= 500) to save memory
                                    if len(profiles) <= 500:
                                        st.session_state['enriched_profiles_raw'] = {
                                            p.get('linkedin_url', ''): p for p in profiles if p.get('raw_data')
                                        }
                                    cleanup_memory()
                                    st.success(f"Loaded {len(profiles)} enriched profiles!")
                                    st.rerun()
                except Exception as e:
                    st.caption(f"Database restore unavailable: {e}")

    # ===== File Upload Section =====
    st.markdown("### Upload File")
    _csv_uploader_nonce = st.session_state.get('pre_enriched_upload_nonce', 0)
    pre_enriched_file = st.file_uploader(
        "Upload pre-enriched CSV or JSON",
        type=['csv', 'json'],
        key=f"pre_enriched_upload_{_csv_uploader_nonce}",
    )

    if pre_enriched_file:
        try:
            # Parse the uploaded file into a DataFrame (no commit to session yet)
            if pre_enriched_file.name.endswith('.json'):
                pre_enriched_data = json.load(pre_enriched_file)
                if isinstance(pre_enriched_data, list):
                    df_uploaded = flatten_for_csv(pre_enriched_data)
                    skipped_no_url = 0
                else:
                    df_uploaded = pd.DataFrame()
                    skipped_no_url = 0
            else:
                pre_enriched_file.seek(0)
                df_uploaded = pd.read_csv(pre_enriched_file, encoding='utf-8')
                df_uploaded = normalize_uploaded_csv(df_uploaded)
                skipped_no_url = df_uploaded.attrs.get('_skipped_no_url', 0)

            if df_uploaded.empty:
                st.error("No profiles parsed from the uploaded file.")
            else:
                # If there are existing profiles, let the user pick append vs replace.
                # Otherwise just load directly (matches the prior single-step UX).
                existing_df = st.session_state.get('results_df')
                has_existing = existing_df is not None and not existing_df.empty

                def _commit_results(new_df):
                    st.session_state['results_df'] = new_df
                    st.session_state['original_results_df'] = new_df.copy()
                    clear_results_derived_state(st.session_state)
                    save_session_state()

                def _apply_replace_with_existing():
                    _commit_results(df_uploaded)
                    st.session_state['last_load_count'] = len(df_uploaded)
                    st.session_state['last_load_file'] = pre_enriched_file.name
                    st.session_state['last_load_mode'] = 'loaded'
                    st.session_state['_data_source'] = 'csv'

                def _apply_append():
                    combined_df = pd.concat([existing_df, df_uploaded], ignore_index=True)
                    if 'linkedin_url' in combined_df.columns:
                        combined_df = combined_df.drop_duplicates(subset=['linkedin_url'], keep='first')
                    elif 'public_url' in combined_df.columns:
                        combined_df = combined_df.drop_duplicates(subset=['public_url'], keep='first')
                    elif 'name' in combined_df.columns:
                        combined_df = combined_df.drop_duplicates(subset=['name'], keep='first')
                    new_count = len(combined_df) - len(existing_df)
                    _commit_results(combined_df)
                    st.session_state['last_load_count'] = new_count
                    st.session_state['last_load_file'] = pre_enriched_file.name
                    st.session_state['last_load_mode'] = 'added'
                    st.session_state['last_load_total'] = len(combined_df)
                    st.session_state['_data_source'] = 'csv'

                def _reset_uploader():
                    st.session_state['pre_enriched_upload_nonce'] = _csv_uploader_nonce + 1

                if not has_existing:
                    # Fresh load — commit, then reset the uploader and rerun so
                    # the file isn't re-parsed on every subsequent interaction.
                    _commit_results(df_uploaded)
                    st.session_state['last_load_count'] = len(df_uploaded)
                    st.session_state['last_load_file'] = pre_enriched_file.name
                    st.session_state['last_load_mode'] = 'loaded'
                    st.session_state['_data_source'] = 'csv'
                    if skipped_no_url > 0:
                        st.session_state['last_load_skipped'] = skipped_no_url
                    _reset_uploader()
                    st.rerun()
                else:
                    st.info(
                        f"You already have **{len(existing_df)}** profiles loaded. "
                        f"This file has **{len(df_uploaded)}** profiles — "
                        "add them to the existing set, or replace it?"
                    )
                    col_replace, col_append = st.columns(2)
                    with col_replace:
                        if st.button(
                            "Replace existing",
                            key="csv_replace_btn",
                            width="stretch",
                            help="Discard the current profiles and load only the uploaded file",
                        ):
                            _apply_replace_with_existing()
                            _reset_uploader()
                            st.rerun()
                    with col_append:
                        if st.button(
                            "+ Add to existing",
                            type="primary",
                            key="csv_append_btn",
                            width="stretch",
                            help="Keep current profiles and merge in the uploaded file (deduped by LinkedIn URL)",
                        ):
                            _apply_append()
                            _reset_uploader()
                            st.rerun()

        except Exception as e:
            st.error(f"Error: {e}")

    # ===== Preview =====
    # Shows loaded results from PhantomBuster or CSV upload
    results_df = get_profiles_df()
    if not results_df.empty:
            st.divider()
            st.markdown("### Preview")

            # Show last load message (from PhantomBuster or CSV)
            if 'last_load_count' in st.session_state:
                load_count = st.session_state['last_load_count']
                load_file = st.session_state.get('last_load_file', '')
                load_mode = st.session_state.get('last_load_mode', 'loaded')
                load_total = st.session_state.get('last_load_total')

                if load_mode == 'added':
                    st.success(f"Added **{load_count}** new profiles (total: **{load_total}**) from **{load_file}**")
                else:
                    skipped = st.session_state.pop('last_load_skipped', 0)
                    skip_note = f" ({skipped} skipped - no LinkedIn URL)" if skipped else ""
                    st.success(f"Loaded **{load_count}** profiles from **{load_file}**{skip_note} - ready for enrichment")

                # Clear after showing once
                del st.session_state['last_load_count']
                if 'last_load_file' in st.session_state:
                    del st.session_state['last_load_file']
                if 'last_load_mode' in st.session_state:
                    del st.session_state['last_load_mode']
                if 'last_load_total' in st.session_state:
                    del st.session_state['last_load_total']

            # Toggle to show all columns
            show_all_cols_csv = st.checkbox("Show all columns", value=False, key="upload_show_all_cols")

            # Pagination settings
            page_size = 10
            total_profiles = len(results_df)
            total_pages = max(1, (total_profiles + page_size - 1) // page_size)

            if 'csv_preview_page' not in st.session_state:
                st.session_state['csv_preview_page'] = 0

            current_page = st.session_state['csv_preview_page']
            start_idx = current_page * page_size
            end_idx = min(start_idx + page_size, total_profiles)

            page_df = results_df.iloc[start_idx:end_idx]

            if show_all_cols_csv:
                # Show all columns from the source
                st.dataframe(
                    page_df,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                        "defaultProfileUrl": st.column_config.LinkColumn("Profile URL"),
                    }
                )
                st.caption(f"{len(results_df.columns)} columns")
            else:
                # Show basic columns only
                key_cols = ['name', 'current_title', 'current_company', 'location', 'linkedin_url']
                available_cols = [c for c in key_cols if c in results_df.columns]

                if available_cols:
                    st.dataframe(
                        page_df[available_cols],
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                        }
                    )
                else:
                    st.dataframe(page_df.head(10), width="stretch", hide_index=True)

            # Pagination controls
            col_prev, col_info, col_next = st.columns([1, 2, 1])
            with col_prev:
                if st.button("< Prev", key="csv_prev", disabled=current_page == 0):
                    st.session_state['csv_preview_page'] = current_page - 1
                    st.rerun()
            with col_info:
                st.caption(f"Page {current_page + 1} of {total_pages} ({start_idx + 1}-{end_idx} of {total_profiles} profiles)")
            with col_next:
                if st.button("Next >", key="csv_next", disabled=current_page >= total_pages - 1):
                    st.session_state['csv_preview_page'] = current_page + 1
                    st.rerun()

            st.divider()

            # Show enrichment section for CSV/PB data; search results are already complete
            _load_data_source = st.session_state.get('_data_source', '')
            if _load_data_source == 'crustdata_search':
                st.info("**Next step:** Go to **2. Filter** or **3. AI Screen** — profiles from Crustdata search are already complete.")
            else:
                # ===== Enrich with Crustdata =====
                st.markdown("### Enrich with Crustdata")
                st.caption("Pull full LinkedIn profile data: work history, education, skills")

                # Show "loaded existing from DB" message if just clicked
                # (st.success doesn't survive st.rerun(); pop from session_state on next render)
                if '_load_existing_message' in st.session_state:
                    _msg = st.session_state.pop('_load_existing_message')
                    st.success(_msg)
                    st.info(
                        "**Profiles are now in the Filter tab.** "
                        "Click **2. Filter** above to apply blacklists, target companies, and other filters."
                    )

                # Show enrichment result message if stored
                if 'enrichment_message' in st.session_state:
                    msg = st.session_state.pop('enrichment_message')
                    if msg.startswith('warning:'):
                        st.warning(msg[8:])
                    elif msg.startswith('success:'):
                        st.success(msg[8:])
                    else:
                        st.info(msg)
                    # After enrichment, the enriched DataFrame is auto-routed into
                    # results_df, so the Filter tab is immediately ready to use.
                    _enriched_after = st.session_state.get('enriched_df')
                    if _enriched_after is not None and not _enriched_after.empty:
                        st.info(
                            f"**{len(_enriched_after)} enriched profiles ready in the Filter tab.** "
                            "Click **2. Filter** above to apply blacklists, target companies, and other filters."
                        )

                if not HAS_DATABASE:
                    st.error("Supabase is not connected. Enrichment is disabled because results won't be saved.")
                elif not has_crust_key:
                    st.warning("Crustdata API key not configured. Add 'api_key' to config.json")
                else:
                    # Source URLs from original_results_df (immutable CSV snapshot) so
                    # the still-to-enrich rows survive even after Load Existing narrows
                    # results_df to enriched-only. Fall back to results_df if no original.
                    _enrich_src_df = st.session_state.get('original_results_df')
                    if _enrich_src_df is None or _enrich_src_df.empty:
                        _enrich_src_df = results_df
                    # Only enrich rows that aren't already complete (excludes Crustdata search rows)
                    if '_needs_enrichment' in _enrich_src_df.columns:
                        _enrich_source_df = _enrich_src_df[_enrich_src_df['_needs_enrichment'] != False]
                    else:
                        _enrich_source_df = _enrich_src_df
                    _enrich_urls = extract_urls_from_phantombuster(_enrich_source_df)
                    if _enrich_urls:
                        # ----- DB dedup: split CSV URLs into already-in-DB vs new -----
                        # Recruiters were paying 3 credits per URL even for profiles
                        # the DB already had fresh data on. Targeted check via the
                        # indexed url-variant query — cached in session_state keyed by
                        # the CSV's URL set, so a re-render of this tab is instant.
                        _csv_url_key = hashlib.md5(
                            '\n'.join(sorted(filter(None, _enrich_urls))).encode('utf-8')
                        ).hexdigest()
                        _existing_norm_set = st.session_state.get(f'_db_check_existing_{_csv_url_key}')
                        _not_found_norm_set = st.session_state.get(f'_db_check_notfound_{_csv_url_key}')
                        if _existing_norm_set is None or _not_found_norm_set is None:
                            _existing_norm_set = set()
                            _not_found_norm_set = set()
                            if HAS_DATABASE:
                                try:
                                    _db_c = _get_db_client()
                                    if _db_c:
                                        # Use the battle-tested helper from PR #63 — it handles URL
                                        # quoting, suffix variants (john-doe vs john-doe-abc123),
                                        # and parallel batch fetches. Light columns only.
                                        _cutoff = (datetime.utcnow() - timedelta(days=180)).isoformat()
                                        _light_cols = 'linkedin_url,original_url,original_urls,enriched_at'
                                        with st.spinner(
                                            f"Checking which of {len(_enrich_urls)} CSV profiles are already in your database..."
                                        ):
                                            _hits = _fetch_candidate_profiles_for_urls(
                                                _db_c, _enrich_urls, _light_cols, _cutoff
                                            )
                                        # Build the set of "known" URLs from every URL
                                        # variant the matched profiles know — so we can
                                        # tag each CSV URL as existing even when its
                                        # canonical column differs.
                                        for _p in _hits:
                                            for _u in (
                                                _p.get('linkedin_url'),
                                                _p.get('original_url'),
                                                *(_p.get('original_urls') or []),
                                            ):
                                                _nu = normalize_linkedin_url(_u) if _u else None
                                                if _nu:
                                                    _existing_norm_set.add(_nu)
                                        # Also pull the URLs Crustdata previously couldn't find.
                                        # We don't want to keep proposing them for re-enrichment.
                                        try:
                                            _nf_urls = _cached_not_found_urls(months=6)
                                            for _u in _nf_urls:
                                                _nu = normalize_linkedin_url(_u) if _u else None
                                                if _nu:
                                                    _not_found_norm_set.add(_nu)
                                        except Exception:
                                            pass
                                except Exception as _ce:
                                    st.caption(f"Note: DB dedup check failed — treating all as new. ({_ce})")
                            st.session_state[f'_db_check_existing_{_csv_url_key}'] = _existing_norm_set
                            st.session_state[f'_db_check_notfound_{_csv_url_key}'] = _not_found_norm_set

                        _new_urls = []
                        _existing_urls = []
                        _not_found_urls = []
                        for _u in _enrich_urls:
                            _nu = normalize_linkedin_url(_u) if _u else None
                            if _nu and _nu in _existing_norm_set:
                                _existing_urls.append(_u)
                            elif _nu and _nu in _not_found_norm_set:
                                _not_found_urls.append(_u)
                            else:
                                _new_urls.append(_u)

                        # Always show the breakdown so the user can see the dedup check
                        # ran, even when nothing matched.
                        _nf_line = (
                            f" **{len(_not_found_urls)}** previously attempted — "
                            "Crustdata has no data for them (skipped)."
                            if _not_found_urls else ""
                        )
                        if _existing_urls or _not_found_urls:
                            st.info(
                                f"**{len(_existing_urls)} of {len(_enrich_urls)}** profiles already enriched in "
                                f"your database (no credits needed)."
                                + _nf_line +
                                f" **{len(_new_urls)}** need fresh enrichment "
                                f"(~{len(_new_urls) * 3} credits)."
                            )
                            _force_reenrich = st.checkbox(
                                "Force re-enrich profiles already in DB (overwrites stored data)",
                                value=False,
                                key="load_force_reenrich",
                                help="Off by default. Enable only if you want to refresh stale profile data."
                            )
                        else:
                            st.info(
                                f"**0 of {len(_enrich_urls)}** profiles found in your database — "
                                f"all **{len(_enrich_urls)}** need enrichment "
                                f"(~{len(_enrich_urls) * 3} credits)."
                            )
                            _force_reenrich = False

                        _enrich_pool = _enrich_urls if _force_reenrich else _new_urls
                        _enrich_pool_size = len(_enrich_pool)

                        col_enrich1, col_enrich2 = st.columns(2)
                        with col_enrich1:
                            if _enrich_pool_size > 0:
                                _max_enrich = st.number_input(
                                    "Profiles to enrich",
                                    min_value=1,
                                    max_value=_enrich_pool_size,
                                    value=min(10, _enrich_pool_size),
                                    key="load_enrich_max",
                                    help="Start with a few to test, then increase"
                                )
                            else:
                                _max_enrich = 0
                                st.caption("Nothing new to enrich — all profiles already in DB.")
                        with col_enrich2:
                            if _enrich_pool_size > 0:
                                st.caption(f"**{_enrich_pool_size}** pending · 3 Crustdata credits each")
                            else:
                                st.caption("Tip: click *Load existing* to pull them into the Filter tab.")

                        # Always allow loading existing DB profiles into the workspace,
                        # so the user can filter / screen them without spending credits.
                        if _existing_urls:
                            if st.button(
                                f"Load {len(_existing_urls)} existing from DB into Filter tab",
                                key="load_existing_from_db_btn",
                            ):
                                try:
                                    _db_c = _get_db_client()
                                    if _db_c:
                                        # Same proven helper as the dedup check — handles URL
                                        # quoting + suffix variants. '*' brings back raw_data
                                        # which downstream screening needs.
                                        _cutoff = (datetime.utcnow() - timedelta(days=180)).isoformat()
                                        with st.spinner(f"Loading {len(_existing_urls)} profiles from DB..."):
                                            _existing_profiles = _fetch_candidate_profiles_for_urls(
                                                _db_c, _existing_urls, '*', _cutoff
                                            )

                                        # Strict-filter: the helper's suffix pass can over-fetch
                                        # rows whose slug just starts the same way as a CSV slug
                                        # (different people). Drop any row whose URL fields don't
                                        # overlap the CSV input — so loaded count matches the
                                        # dedup breakdown exactly.
                                        _csv_norm_set = {
                                            normalize_linkedin_url(_u) for _u in _existing_urls if _u
                                        }
                                        _csv_norm_set.discard(None)
                                        _csv_norm_set.discard('')

                                        def _profile_overlaps_csv(_p):
                                            for _u in (
                                                _p.get('linkedin_url'),
                                                _p.get('original_url'),
                                                *(_p.get('original_urls') or []),
                                            ):
                                                _nu = normalize_linkedin_url(_u) if _u else None
                                                if _nu and _nu in _csv_norm_set:
                                                    return True
                                            return False

                                        _existing_profiles = [
                                            _p for _p in _existing_profiles if _profile_overlaps_csv(_p)
                                        ]

                                        if _existing_profiles:
                                            _existing_df = profiles_to_dataframe(_existing_profiles)
                                            # Build the "loaded" URL set from EVERY URL variant the
                                            # matched profiles know — canonical linkedin_url,
                                            # original_url, and original_urls[]. CSV URLs that
                                            # matched via any variant must not be counted as still-
                                            # to-enrich, even if their text doesn't equal the
                                            # canonical column.
                                            _loaded_norm = set()
                                            for _profile in _existing_profiles:
                                                for _u in (
                                                    _profile.get('linkedin_url'),
                                                    _profile.get('original_url'),
                                                    *(_profile.get('original_urls') or []),
                                                ):
                                                    _nu = normalize_linkedin_url(_u) if _u else None
                                                    if _nu:
                                                        _loaded_norm.add(_nu)
                                            _orig_csv_df = st.session_state.get('original_results_df')
                                            _unmatched_csv_rows = pd.DataFrame()
                                            if (
                                                _orig_csv_df is not None
                                                and not _orig_csv_df.empty
                                                and 'linkedin_url' in _orig_csv_df.columns
                                            ):
                                                _orig_norm = _orig_csv_df['linkedin_url'].apply(
                                                    lambda u: normalize_linkedin_url(u) if u else None
                                                )
                                                _unmatched_csv_rows = _orig_csv_df[
                                                    ~_orig_norm.isin(_loaded_norm)
                                                ].copy()
                                            # (Earlier revisions merged `_unmatched_csv_rows` into
                                            # results_df; current contract is rich-only in the
                                            # working set — the unmatched rows are tracked via
                                            # original_results_df instead. `_unmatched_csv_rows`
                                            # is kept only to populate _new_count below.)
                                            clear_results_derived_state(st.session_state)
                                            # Both refs hold just the rich profiles so the Filter
                                            # tab shows N (not N + sparse). The Load tab's enrich
                                            # block sources URLs from original_results_df (the
                                            # immutable CSV snapshot) so the still-to-enrich
                                            # rows aren't lost.
                                            st.session_state['enriched_df'] = _existing_df
                                            st.session_state['results_df'] = _existing_df
                                            st.session_state['enriched_profiles_raw'] = {
                                                p.get('linkedin_url', ''): p for p in _existing_profiles if p.get('raw_data')
                                            }
                                            save_session_state()
                                            # Build a plain-English breakdown so the working-set
                                            # number is never a surprise: uploaded links → matched
                                            # in DB → unique people (some links were the same
                                            # person) → not-in-DB → still-need-enrichment.
                                            _people = len(_existing_df)
                                            _matched = len(_existing_urls)
                                            _merged = _matched - _people
                                            _not_in_db = len(_not_found_urls)
                                            _uploaded = len(_enrich_urls)
                                            # Base "still need enrichment" on the genuinely-new
                                            # URLs the Enrich button actually acts on (_new_urls),
                                            # NOT _unmatched_csv_rows — the latter also includes the
                                            # not-in-DB rows, which can't be enriched and are already
                                            # reported on their own line. Using it would tell the
                                            # user to enrich profiles we just listed as skipped.
                                            _new_count = len(_new_urls)

                                            _match_line = f"- **{_matched:,}** already in your database"
                                            if _merged > 0:
                                                _match_line += (
                                                    f" → merged to **{_people:,}** unique people "
                                                    f"({_merged:,} were the same person under different links)"
                                                )
                                            else:
                                                _match_line += f" → **{_people:,}** unique people"

                                            _lines = [
                                                f"**{_people:,} unique people loaded into the Filter tab.**",
                                                "",
                                                f"From **{_uploaded:,}** links in your upload:",
                                                _match_line,
                                            ]
                                            if _not_in_db > 0:
                                                _lines.append(
                                                    f"- **{_not_in_db:,}** not in the database "
                                                    "(no Crustdata data — skipped)"
                                                )
                                            if _new_count > 0:
                                                _lines.append(
                                                    f"- **{_new_count:,}** still need enrichment — "
                                                    "click *Enrich* below"
                                                )
                                            # Store message in session_state — st.success
                                            # doesn't survive st.rerun(), so without this the
                                            # user sees no confirmation that the load worked.
                                            st.session_state['_load_existing_message'] = "\n".join(_lines)
                                            st.rerun()
                                        else:
                                            st.warning("Could not load any profiles from DB.")
                                    else:
                                        st.error("No DB connection.")
                                except Exception as _le:
                                    st.error(f"Failed to load existing: {_le}")

                        if _enrich_pool_size > 0 and st.button(
                            f"Enrich {_max_enrich} profiles", type="primary", key="load_enrich_btn"
                        ):
                            _urls_to_enrich = _enrich_pool[:_max_enrich]
                            _enrich_results = []
                            _enrich_progress = st.progress(0)
                            _enrich_status = st.empty()
                            _enrich_batch_size = 25
                            _enrich_total_batches = (_max_enrich + _enrich_batch_size - 1) // _enrich_batch_size
                            _enrich_tracker = get_usage_tracker()
                            _enrich_start = time.time()

                            for _ei in range(0, len(_urls_to_enrich), _enrich_batch_size):
                                _batch = _urls_to_enrich[_ei:_ei + _enrich_batch_size]
                                _enrich_status.text(f"Processing batch {_ei // _enrich_batch_size + 1}/{_enrich_total_batches}...")
                                _enrich_results.extend(enrich_batch(_batch, api_key, tracker=_enrich_tracker))
                                _enrich_progress.progress(min((_ei + _enrich_batch_size) / len(_urls_to_enrich), 1.0))
                                if _ei + _enrich_batch_size < len(_urls_to_enrich):
                                    time.sleep(2)

                            _enrich_elapsed = time.time() - _enrich_start
                            _time_str = f"{int(_enrich_elapsed//60)}m {int(_enrich_elapsed%60)}s" if _enrich_elapsed >= 60 else f"{int(_enrich_elapsed)}s"
                            _enrich_status.text(f"Done in {_time_str}")
                            send_notification("Enrichment Complete", f"Processed {len(_enrich_results)} profiles in {_time_str}")

                            _enrich_errors = [r for r in _enrich_results if 'error' in r]
                            _enrich_successful = [r for r in _enrich_results if 'error' not in r]

                            if _enrich_successful:
                                _new_enriched_df = flatten_for_csv(_enrich_successful)

                                # Preserve emails and names from original CSV
                                _orig_df = st.session_state.get('original_results_df')
                                if _orig_df is not None and not _orig_df.empty and 'linkedin_url' in _new_enriched_df.columns:
                                    _preserve = {}
                                    _orig_url_col = 'linkedin_url' if 'linkedin_url' in _orig_df.columns else None
                                    if _orig_url_col:
                                        for _, _row in _orig_df.iterrows():
                                            _u = _row.get(_orig_url_col)
                                            if _u:
                                                _preserve[normalize_linkedin_url(_u)] = {
                                                    'email': _row.get('email') if pd.notna(_row.get('email')) else None,
                                                    'name': _row.get('name') if pd.notna(_row.get('name')) else None,
                                                }
                                    if _preserve:
                                        if 'email' not in _new_enriched_df.columns:
                                            _new_enriched_df['email'] = ''
                                        for _idx, _row in _new_enriched_df.iterrows():
                                            _nu = normalize_linkedin_url(_row.get('linkedin_url')) if _row.get('linkedin_url') else None
                                            if _nu and _nu in _preserve:
                                                _p = _preserve[_nu]
                                                if _p.get('email') and not _row.get('email'):
                                                    _new_enriched_df.at[_idx, 'email'] = _p['email']
                                                if _p.get('name') and not _row.get('name'):
                                                    _new_enriched_df.at[_idx, 'name'] = _p['name']

                                # Also pull profiles the CSV already had in DB so the
                                # Filter tab sees the full uploaded set (newly enriched +
                                # already-known), not just what we just paid to enrich.
                                _db_existing_df = pd.DataFrame()
                                if _existing_urls and HAS_DATABASE:
                                    try:
                                        _db_c = _get_db_client()
                                        if _db_c:
                                            _cutoff = (datetime.utcnow() - timedelta(days=180)).isoformat()
                                            _db_rows = _fetch_candidate_profiles_for_urls(
                                                _db_c, _existing_urls, '*', _cutoff
                                            )
                                            # Strict-filter to CSV overlap (drop suffix-pass extras
                                            # that point at different people with similar slugs).
                                            _csv_norm_set = {
                                                normalize_linkedin_url(_u) for _u in _existing_urls if _u
                                            }
                                            _csv_norm_set.discard(None)
                                            _csv_norm_set.discard('')
                                            _db_rows = [
                                                _p for _p in _db_rows
                                                if any(
                                                    normalize_linkedin_url(_u) in _csv_norm_set
                                                    for _u in (
                                                        _p.get('linkedin_url'),
                                                        _p.get('original_url'),
                                                        *(_p.get('original_urls') or []),
                                                    )
                                                    if _u
                                                )
                                            ]
                                            if _db_rows:
                                                _db_existing_df = profiles_to_dataframe(_db_rows)
                                                # Cache raw_data for downstream screening
                                                _raw_map = st.session_state.get('enriched_profiles_raw', {}) or {}
                                                for _p in _db_rows:
                                                    _k = _p.get('linkedin_url', '')
                                                    if _k and _p.get('raw_data'):
                                                        _raw_map[_k] = _p
                                                st.session_state['enriched_profiles_raw'] = _raw_map
                                    except Exception as _ddbe:
                                        st.caption(f"Note: couldn't load already-in-DB profiles ({_ddbe})")

                                # Merge: existing-in-DB + new-just-enriched + any prior enriched_df
                                _frames = [_df for _df in (
                                    st.session_state.get('enriched_df'),
                                    _db_existing_df,
                                    _new_enriched_df,
                                ) if _df is not None and not _df.empty]
                                if _frames:
                                    _combined = pd.concat(_frames, ignore_index=True)
                                    _url_col = 'linkedin_url' if 'linkedin_url' in _combined.columns else None
                                    if _url_col:
                                        # keep='last' lets newly enriched overwrite older copies
                                        _combined = _combined.drop_duplicates(subset=[_url_col], keep='last')
                                    _out_enriched = _combined
                                else:
                                    _out_enriched = _new_enriched_df

                                st.session_state['enriched_df'] = _out_enriched
                                # Wire enriched data into the Filter tab: the basic
                                # Filter section reads results_df. original_results_df
                                # still holds the raw uploaded CSV for reference.
                                st.session_state['results_df'] = _out_enriched
                                if 'enriched_results' in st.session_state:
                                    del st.session_state['enriched_results']
                                save_session_state()

                                # Save to Supabase
                                _db_saved = 0
                                if HAS_DATABASE:
                                    try:
                                        _db_client = _get_db_client()
                                        if _db_client:
                                            _orig_emails = {}
                                            if _orig_df is not None and not _orig_df.empty:
                                                _oc = 'linkedin_url' if 'linkedin_url' in _orig_df.columns else None
                                                if _oc and 'email' in _orig_df.columns:
                                                    for _, _r in _orig_df.iterrows():
                                                        _eu = _r.get(_oc)
                                                        _em = _r.get('email')
                                                        if _eu and _em and str(_em).strip() and str(_em).lower() != 'nan':
                                                            _orig_emails[normalize_linkedin_url(_eu)] = str(_em).strip()
                                            _orig_url_map = {}
                                            for _p in _enrich_successful:
                                                _pu = _p.get('linkedin_flagship_url') or _p.get('linkedin_url')
                                                _po = _p.get('_original_url')
                                                if _pu:
                                                    _pn = normalize_linkedin_url(_pu)
                                                    if _pn and _po:
                                                        _orig_url_map[_pn] = normalize_linkedin_url(_po)
                                            _bulk = save_enriched_profiles_bulk(
                                                _db_client, _enrich_successful,
                                                original_url_map=_orig_url_map,
                                                email_map=_orig_emails,
                                                batch_size=100,
                                            )
                                            _db_saved = _bulk['saved']

                                            # Persist failures as not_found so the same dead
                                            # profiles aren't proposed for re-enrichment.
                                            # Crustdata error rows use linkedin_profile_url; the
                                            # http-error path uses linkedin_url. Read both.
                                            # DATA-INTEGRITY GUARD (PR #87 review): when
                                            # force-reenrich is on, the pool includes URLs that
                                            # already have enriched data in DB. Saving those as
                                            # not_found would DOWNGRADE good profiles. Skip them.
                                            _refresh_skip_norm = set()
                                            if _force_reenrich:
                                                for _u in _existing_urls:
                                                    _nu = normalize_linkedin_url(_u) if _u else None
                                                    if _nu:
                                                        _refresh_skip_norm.add(_nu)
                                            if _enrich_errors:
                                                _failed_records = []
                                                for _e in _enrich_errors:
                                                    _u_raw = _e.get('linkedin_url') or _e.get('linkedin_profile_url')
                                                    if not _u_raw:
                                                        continue
                                                    if normalize_linkedin_url(_u_raw) in _refresh_skip_norm:
                                                        # Already enriched, force-refresh failed —
                                                        # leave existing 'enriched' status intact.
                                                        continue
                                                    _failed_records.append({
                                                        'url': _u_raw,
                                                        'error': _e.get('error'),
                                                    })
                                                if _failed_records:
                                                    try:
                                                        save_failed_enrichments_batch(_db_client, _failed_records)
                                                    except Exception as _fbe:
                                                        print(f"[Load] failed_enrichments save failed: {_fbe}")
                                    except Exception as _dbe:
                                        st.warning(f"Database save failed: {_dbe}")

                                # Clear ONLY the caches actually invalidated by enrichment.
                                # The previous wholesale st.cache_data.clear() wiped every
                                # cached function in the app (credits, search history, sheet
                                # data, etc.), causing a 5-10s freeze after enrichment.
                                for _cache in (
                                    _cached_recently_enriched_urls,
                                    _cached_not_found_urls,
                                    _get_db_restore_counts_cached,
                                    _cached_all_profiles,
                                ):
                                    try:
                                        _cache.clear()
                                    except Exception:
                                        pass

                                # Drop the CSV-keyed dedup result so the breakdown
                                # reflects the new DB state on next render (otherwise
                                # the post-enrichment numbers stay stale).
                                for _ckey in [
                                    k for k in list(st.session_state.keys())
                                    if k.startswith('_db_check_existing_')
                                    or k.startswith('_db_check_notfound_')
                                ]:
                                    st.session_state.pop(_ckey, None)

                                _db_msg = f" | DB: {_db_saved}/{len(_enrich_successful)} saved" if HAS_DATABASE else ""
                                if _enrich_errors:
                                    # Surface the first distinct error so the user sees WHY,
                                    # not just a count. Truncated to 200 chars to keep the
                                    # banner readable.
                                    _err_samples = []
                                    _seen_errs = set()
                                    for _e in _enrich_errors:
                                        _emsg = str(_e.get('error', 'Unknown'))[:200]
                                        if _emsg not in _seen_errs:
                                            _seen_errs.add(_emsg)
                                            _err_samples.append(_emsg)
                                        if len(_err_samples) >= 2:
                                            break
                                    _err_blurb = " | First errors: " + " · ".join(_err_samples) if _err_samples else ""
                                    st.session_state['enrichment_message'] = (
                                        f"warning:Enriched {len(_enrich_successful)} profiles in {_time_str}{_db_msg}. "
                                        f"{len(_enrich_errors)} failed.{_err_blurb}"
                                    )
                                else:
                                    st.session_state['enrichment_message'] = f"success:Enriched {len(_enrich_successful)} profiles in {_time_str}{_db_msg}"
                                st.rerun()
                            elif _enrich_errors:
                                # All profiles failed — still persist as not_found so they
                                # don't keep showing up as "to enrich" on every render.
                                # Crustdata error rows use linkedin_profile_url; the http-error
                                # path uses linkedin_url. Read both.
                                if HAS_DATABASE:
                                    try:
                                        _db_client_f = _get_db_client()
                                        if _db_client_f:
                                            # DATA-INTEGRITY GUARD (PR #87 review): skip
                                            # failures whose URL is already enriched in DB
                                            # (force-refresh path). Saving as not_found would
                                            # downgrade them.
                                            _refresh_skip_norm = set()
                                            if _force_reenrich:
                                                for _u in _existing_urls:
                                                    _nu = normalize_linkedin_url(_u) if _u else None
                                                    if _nu:
                                                        _refresh_skip_norm.add(_nu)
                                            _failed_records = []
                                            for _e in _enrich_errors:
                                                _u_raw = _e.get('linkedin_url') or _e.get('linkedin_profile_url')
                                                if not _u_raw:
                                                    continue
                                                if normalize_linkedin_url(_u_raw) in _refresh_skip_norm:
                                                    continue
                                                _failed_records.append({
                                                    'url': _u_raw,
                                                    'error': _e.get('error'),
                                                })
                                            if _failed_records:
                                                save_failed_enrichments_batch(_db_client_f, _failed_records)
                                    except Exception as _fbe:
                                        print(f"[Load] failed_enrichments save (all-failed) failed: {_fbe}")

                                # Invalidate dedup caches so the next render sees these URLs
                                # as not_found, not as "need enrichment".
                                for _cache in (_cached_not_found_urls, _cached_recently_enriched_urls):
                                    try:
                                        _cache.clear()
                                    except Exception:
                                        pass
                                for _ckey in [
                                    k for k in list(st.session_state.keys())
                                    if k.startswith('_db_check_existing_')
                                    or k.startswith('_db_check_notfound_')
                                ]:
                                    st.session_state.pop(_ckey, None)

                                # Show a clear message: not a system failure, just no data.
                                _err_samples = []
                                _seen_errs = set()
                                for _e in _enrich_errors:
                                    _emsg = str(_e.get('error', 'Unknown'))[:160]
                                    if _emsg not in _seen_errs:
                                        _seen_errs.add(_emsg)
                                        _err_samples.append(_emsg)
                                    if len(_err_samples) >= 2:
                                        break
                                _err_blurb = " | ".join(_err_samples) if _err_samples else "Unknown"
                                st.session_state['enrichment_message'] = (
                                    f"warning:Crustdata had no data for {len(_enrich_errors)} profile(s). "
                                    f"They've been marked not_found and won't be re-proposed. "
                                    f"First error: {_err_blurb}"
                                )
                                st.rerun()
                            else:
                                st.error("No results returned from API. Check your API key and credits.")
                    else:
                        st.warning("No LinkedIn URLs found in loaded profiles.")

    st.divider()

    pb_key = load_phantombuster_key()
    has_pb_key = pb_key and pb_key != "YOUR_PHANTOMBUSTER_API_KEY_HERE"

    if has_pb_key:
        # Fetch all agents once
        pb_result = fetch_phantombuster_agents(pb_key)
        agents = pb_result.get('agents', [])
        pb_error = pb_result.get('error')

        # ===== SECTION 1: Load from PhantomBuster =====
        st.markdown("### Load from PhantomBuster")

        if pb_error:
            st.error(f"PhantomBuster API error: {pb_error}")

        if agents:
            agent_names = [a['name'] for a in agents]

            selected_agent_name = st.selectbox(
                "Select Phantom",
                options=agent_names,
                key="pb_agent_select"
            )

            selected_agent = next((a for a in agents if a['name'] == selected_agent_name), None)

            if selected_agent:
                # Use module-level cached function for efficiency
                search_history = _load_search_history_module(agent_id=selected_agent['id'])

                if search_history:
                    # Build dropdown options from history (most recent first)
                    history_options = []
                    for h in search_history:
                        csv_name = h.get('csv_name', 'unknown')
                        launched_at = h.get('launched_at', '')
                        profiles = h.get('profiles_requested', '')
                        search_name = h.get('search_name', '')
                        profile_str = f", {profiles} profiles" if profiles else ""
                        # Format: "Search Name - Feb 2, 09:10 (2500 profiles)" or "Feb 2, 09:10 - csv_name (2500 profiles)"
                        if search_name:
                            display_name = f"{search_name} - {launched_at}{profile_str}"
                        else:
                            display_name = f"{launched_at} - {csv_name}{profile_str}"
                        history_options.append({
                            'display': display_name,
                            'csv_name': csv_name,
                            'launched_at': launched_at,
                        })

                    # Dropdown to select search (most recent is default - index 0)
                    col_select, col_delete = st.columns([5, 1])

                    with col_select:
                        selected_idx = st.selectbox(
                            "Select search results",
                            options=range(len(history_options)),
                            format_func=lambda i: history_options[i]['display'],
                            key="pb_history_select",
                            help="Select which search results to load (most recent first)"
                        )

                    selected_search = history_options[selected_idx] if history_options else None

                    # Delete button with confirmation
                    with col_delete:
                        st.write("")  # Spacing
                        if st.button("🗑️", key="pb_delete_search", help="Delete this search from history and PhantomBuster"):
                            st.session_state['pb_confirm_delete'] = selected_search['csv_name'] if selected_search else None

                    # Show confirmation dialog if delete was clicked
                    if st.session_state.get('pb_confirm_delete'):
                        csv_to_delete = st.session_state['pb_confirm_delete']
                        st.warning(f"Delete **{csv_to_delete}** from history and PhantomBuster?")
                        col_yes, col_no = st.columns(2)
                        with col_yes:
                            if st.button("Yes, delete", key="pb_confirm_yes", type="primary"):
                                with st.spinner("Deleting..."):
                                    success = delete_search_from_history(
                                        agent_id=selected_agent['id'],
                                        csv_name=csv_to_delete,
                                        api_key=pb_key,
                                        delete_file=True
                                    )
                                    st.session_state['pb_confirm_delete'] = None
                                    if success:
                                        _load_search_history_module.clear()
                                        st.success(f"Deleted {csv_to_delete}")
                                        st.rerun()
                                    else:
                                        st.error("Failed to delete")
                        with col_no:
                            if st.button("Cancel", key="pb_confirm_no"):
                                st.session_state['pb_confirm_delete'] = None
                                st.rerun()

                    # Show current loaded count if any
                    existing_count = get_profile_count()
                    if existing_count > 0:
                        st.caption(f"Currently loaded: **{existing_count}** profiles")

                    # Option to accumulate results
                    col_load, col_add = st.columns([1, 1])

                    with col_load:
                        if st.button("Load Results", type="primary", key="pb_load_btn", width="stretch", help="Replace current results"):
                            if selected_search:
                                with st.spinner("Loading results..."):
                                    filename = selected_search['csv_name']
                                    pb_df = fetch_phantombuster_result_csv(pb_key, selected_agent['id'], debug=False, filename=filename)
                                    if not pb_df.empty:
                                        pb_df = normalize_phantombuster_columns(pb_df)

                                        # Clear derived state (enriched_df etc.) so stale search
                                        # results don't shadow the new PB data in get_profiles_df()
                                        clear_results_derived_state(st.session_state)
                                        st.session_state['results_df'] = pb_df
                                        st.session_state['original_results_df'] = pb_df.copy()
                                        st.session_state['preview_page'] = 0  # Reset pagination
                                        st.session_state['last_load_count'] = len(pb_df)
                                        st.session_state['last_load_file'] = filename
                                        st.session_state['_data_source'] = 'phantombuster'
                                        save_session_state()  # Save for restore
                                        st.rerun()
                                    else:
                                        st.error("No results found. File may have been deleted from PhantomBuster.")

                    with col_add:
                        if st.button("+ Add to Results", key="pb_add_btn", width="stretch", help="Add to current results"):
                            if selected_search:
                                with st.spinner("Adding results..."):
                                    filename = selected_search['csv_name']
                                    pb_df = fetch_phantombuster_result_csv(pb_key, selected_agent['id'], debug=False, filename=filename)
                                    if not pb_df.empty:
                                        pb_df = normalize_phantombuster_columns(pb_df)

                                        # PhantomBuster data stays in session state only (not saved to DB)
                                        # Merge with existing results
                                        if 'results_df' in st.session_state and not st.session_state['results_df'].empty:
                                            existing_df = st.session_state['results_df']
                                            # Combine and remove duplicates based on linkedin_url or name
                                            combined_df = pd.concat([existing_df, pb_df], ignore_index=True)
                                            # Remove duplicates - prefer keeping first occurrence
                                            if 'linkedin_url' in combined_df.columns:
                                                combined_df = combined_df.drop_duplicates(subset=['linkedin_url'], keep='first')
                                            elif 'public_url' in combined_df.columns:
                                                combined_df = combined_df.drop_duplicates(subset=['public_url'], keep='first')
                                            elif 'name' in combined_df.columns:
                                                combined_df = combined_df.drop_duplicates(subset=['name'], keep='first')
                                            new_count = len(combined_df) - len(existing_df)
                                            st.session_state['results_df'] = combined_df
                                            st.session_state['original_results_df'] = combined_df.copy()
                                            st.session_state['last_load_count'] = new_count
                                            st.session_state['last_load_file'] = filename
                                            st.session_state['last_load_mode'] = 'added'
                                            st.session_state['last_load_total'] = len(combined_df)
                                        else:
                                            st.session_state['results_df'] = pb_df
                                            st.session_state['original_results_df'] = pb_df.copy()
                                            st.session_state['last_load_count'] = len(pb_df)
                                            st.session_state['last_load_file'] = filename
                                            st.session_state['last_load_mode'] = 'loaded'

                                        st.session_state['preview_page'] = 0
                                        st.session_state['_data_source'] = 'phantombuster'
                                        # Clear derived state so stale enriched_df doesn't shadow combined results
                                        clear_results_derived_state(st.session_state)
                                        save_session_state()  # Save for restore
                                        st.rerun()
                                    else:
                                        st.error("No results found. File may have been deleted from PhantomBuster.")
                else:
                    # No search history — allow loading default results directly
                    st.caption("No search history found for this phantom. You can load the latest results directly.")
                    if st.button("Load Latest Results", type="primary", key="pb_load_latest_btn"):
                        with st.spinner("Loading results..."):
                            pb_df = fetch_phantombuster_result_csv(pb_key, selected_agent['id'], debug=False)
                            if not pb_df.empty:
                                pb_df = normalize_phantombuster_columns(pb_df)
                                clear_results_derived_state(st.session_state)
                                st.session_state['results_df'] = pb_df
                                st.session_state['original_results_df'] = pb_df.copy()
                                st.session_state['preview_page'] = 0
                                st.session_state['last_load_count'] = len(pb_df)
                                st.session_state['last_load_file'] = selected_agent['name']
                                st.session_state['_data_source'] = 'phantombuster'
                                save_session_state()
                                st.rerun()
                            else:
                                st.error("No results found for this phantom.")
        else:
            st.warning("No phantoms found in your PhantomBuster account")

        st.divider()

        # ===== SECTION 2: Launch Search =====
        st.markdown("### Launch Search")
        st.caption("Launch your personal phantom with a Sales Navigator search URL")

        # Initialize session state
        if 'pb_launch_status' not in st.session_state:
            st.session_state['pb_launch_status'] = 'idle'
        if 'pb_launch_agent_id' not in st.session_state:
            st.session_state['pb_launch_agent_id'] = None
        if 'pb_launch_container_id' not in st.session_state:
            st.session_state['pb_launch_container_id'] = None
        if 'pb_launch_error' not in st.session_state:
            st.session_state['pb_launch_error'] = None
        if 'pb_launch_start_time' not in st.session_state:
            st.session_state['pb_launch_start_time'] = None

        # User name input
        user_name = st.text_input(
            "Your name",
            value=st.session_state.get('pb_user_name', ''),
            placeholder="e.g., John",
            key="pb_user_name_input"
        )

        if user_name:
            st.session_state['pb_user_name'] = user_name

        # Sales Navigator URL input
        search_url = st.text_input(
            "Sales Navigator Search URL",
            placeholder="https://www.linkedin.com/sales/search/people?...",
            key="pb_search_url"
        )

        # Optional search name
        search_name = st.text_input(
            "Search name (optional)",
            placeholder="e.g., Senior Engineers SF",
            key="pb_search_name",
            help="Give this search a name to easily identify it later"
        )

        # Find user's phantom
        user_phantom = None
        if user_name and agents:
            user_name_lower = user_name.lower()
            for agent in agents:
                if user_name_lower in agent.get('name', '').lower():
                    user_phantom = agent
                    break

            if user_phantom:
                st.success(f"Found your phantom: **{user_phantom['name']}**")
            else:
                st.warning(f"No phantom found with '{user_name}' in the name")

        # Launch status and buttons
        current_status = st.session_state['pb_launch_status']

        if current_status == 'running':
            # Auto-fetch latest status
            container_id = st.session_state['pb_launch_container_id']
            if container_id:
                status_result = fetch_container_status(pb_key, container_id)
                container_status = status_result.get('status', 'unknown')

                # Store progress info
                st.session_state['pb_progress_info'] = {
                    'profiles_count': status_result.get('profiles_count', 0),
                    'progress_pct': status_result.get('progress_pct', 0),
                    'progress': status_result.get('progress'),
                }

                # Check if finished or error
                if container_status == 'finished':
                    st.session_state['pb_launch_status'] = 'finished'
                    # Release agent lock so other users can launch
                    if st.session_state.get('pb_launch_agent_id'):
                        pb_agent_unlock(st.session_state['pb_launch_agent_id'])
                    # Desktop notification
                    try:
                        profiles = status_result.get('profiles_count', 0)
                        msg = f"Extracted {profiles} profiles" if profiles else "Ready to load results"
                        st.session_state['_pb_pending_toast'] = ("✅", f"**PhantomBuster Finished** — {msg}")
                        if HAS_PLYER:
                            notification.notify(
                                title="PhantomBuster Finished",
                                message=msg,
                                app_name="SourcingX",
                                timeout=10
                            )
                        # Windows sound
                        if HAS_WINSOUND:
                            try:
                                winsound.MessageBeep(winsound.MB_OK)
                            except:
                                pass
                    except:
                        pass
                    st.rerun()
                elif container_status == 'error':
                    st.session_state['pb_launch_status'] = 'error'
                    st.session_state['pb_launch_error'] = status_result.get('exitMessage', 'Phantom failed')
                    # Release agent lock on error too
                    if st.session_state.get('pb_launch_agent_id'):
                        pb_agent_unlock(st.session_state['pb_launch_agent_id'])
                    # Desktop notification for error
                    try:
                        err_msg = status_result.get('exitMessage', 'Phantom failed')
                        st.session_state['_pb_pending_toast'] = ("❌", f"**PhantomBuster Error** — {err_msg}")
                        if HAS_PLYER:
                            notification.notify(
                                title="PhantomBuster Error",
                                message=err_msg,
                                app_name="SourcingX",
                                timeout=10
                            )
                        if HAS_WINSOUND:
                            try:
                                winsound.MessageBeep(winsound.MB_ICONHAND)
                            except:
                                pass
                    except:
                        pass
                    st.rerun()

            # Show running status with progress
            elapsed = ""
            if st.session_state['pb_launch_start_time']:
                elapsed_seconds = int(time.time() - st.session_state['pb_launch_start_time'])
                elapsed_min = elapsed_seconds // 60
                elapsed_sec = elapsed_seconds % 60
                elapsed = f"{elapsed_min}m {elapsed_sec}s"

            # Display progress info
            progress_info = st.session_state.get('pb_progress_info', {})
            profiles_count = progress_info.get('profiles_count', 0)
            progress_pct = progress_info.get('progress_pct', 0)

            # Progress display
            st.info(f"**Running** - {elapsed} (auto-refreshing every 10s)")
            if profiles_count > 0 or progress_pct > 0:
                col_prog1, col_prog2 = st.columns(2)
                with col_prog1:
                    if profiles_count > 0:
                        st.metric("Profiles extracted", profiles_count)
                with col_prog2:
                    if progress_pct > 0:
                        st.metric("Progress", f"{progress_pct}%")

            # Show progress bar if we have percentage (clamp between 0 and 1)
            if progress_pct > 0:
                st.progress(min(1.0, max(0.0, progress_pct / 100)))

            # Cancel button — actually aborts the phantom on PhantomBuster
            if st.button("Cancel", key="pb_cancel_btn", type="secondary"):
                agent_id = st.session_state.get('pb_launch_agent_id')
                if agent_id and pb_key:
                    try:
                        resp = requests.post(
                            f'https://api.phantombuster.com/api/v1/agent/{agent_id}/abort',
                            headers={'X-Phantombuster-Key': pb_key},
                            timeout=10
                        )
                        if resp.status_code == 200:
                            st.success("Phantom aborted successfully")
                        else:
                            st.warning(f"Abort request returned status {resp.status_code} — phantom may still be running")
                    except Exception as e:
                        st.warning(f"Could not abort phantom: {e}")
                st.session_state['pb_launch_status'] = 'idle'
                st.session_state['pb_launch_container_id'] = None
                st.session_state['pb_launch_start_time'] = None
                st.session_state['pb_progress_info'] = {}
                st.session_state['pb_launch_skip_count'] = 0
                st.rerun()

            # Auto-refresh every 10 seconds
            time.sleep(10)
            st.rerun()

        elif current_status == 'finished':
            if '_pb_pending_toast' in st.session_state:
                _icon, _tmsg = st.session_state.pop('_pb_pending_toast')
                st.toast(_tmsg, icon=_icon)
            progress_info = st.session_state.get('pb_progress_info', {})
            profiles_count = progress_info.get('profiles_count', 0)
            csv_name = st.session_state.get('pb_launch_csv_name')
            skip_count = st.session_state.get('pb_launch_skip_count', 0)

            if profiles_count > 0:
                if skip_count > 0:
                    st.success(f"Phantom finished! Extracted **{profiles_count}** profiles (skipped {skip_count} already in database)")
                else:
                    st.success(f"Phantom finished! Extracted **{profiles_count}** profiles")
            else:
                st.success("Phantom finished!")

            if csv_name:
                st.info(f"Results saved to: **{csv_name}.csv**")

            if st.button("Load Results", type="primary", key="pb_load_results_btn"):
                agent_id = st.session_state['pb_launch_agent_id']
                with st.spinner("Loading results..."):
                    # Load the specific file created during this launch
                    pb_df = fetch_phantombuster_result_csv(pb_key, agent_id, filename=csv_name)
                    if not pb_df.empty:
                        pb_df = normalize_phantombuster_columns(pb_df)

                        # Log PhantomBuster completion with profiles scraped
                        pb_tracker = get_usage_tracker()
                        if pb_tracker:
                            pb_tracker.log_phantombuster(
                                operation='scrape',
                                profiles_scraped=len(pb_df),
                                status='success',
                                agent_id=agent_id,
                                container_id=st.session_state.get('pb_launch_container_id')
                            )

                        # PhantomBuster data stays in session state only (not saved to DB)
                        # DB save happens after Crustdata enrichment
                        st.session_state['results_df'] = pb_df
                        st.session_state['original_results_df'] = pb_df.copy()
                        st.session_state['preview_page'] = 0
                        st.session_state['pb_launch_status'] = 'idle'
                        st.session_state['pb_launch_container_id'] = None
                        st.session_state['pb_launch_start_time'] = None
                        st.session_state['pb_launch_csv_name'] = None
                        st.session_state['pb_progress_info'] = {}
                        st.session_state['pb_launch_skip_count'] = 0
                        st.session_state['last_load_count'] = len(pb_df)
                        st.session_state['last_load_file'] = f"{csv_name}.csv" if csv_name else "results"
                        save_session_state()  # Save for restore
                        st.rerun()
                    else:
                        st.error("Could not load results.")

        elif current_status == 'error':
            if '_pb_pending_toast' in st.session_state:
                _icon, _tmsg = st.session_state.pop('_pb_pending_toast')
                st.toast(_tmsg, icon=_icon)
            error_msg = st.session_state['pb_launch_error'] or 'Unknown error'
            st.error(f"Error: {error_msg}")

            # Show detailed warning for session cookie issues
            if 'session cookie' in error_msg.lower() or 're-authenticate' in error_msg.lower():
                st.warning("""
**LinkedIn Session Expired or Invalid**

This can happen when:
1. Your LinkedIn session cookie expired
2. A previous launch attempt corrupted the session
3. LinkedIn invalidated your session for security reasons

**⚠️ Important:** When LinkedIn invalidates a session, it affects **ALL phantoms** using that account - not just this one.

**How to fix:**
1. Log into [Sales Navigator](https://www.linkedin.com/sales/) in your browser
2. Open PhantomBuster and go to your phantom's settings
3. Click "Connect to LinkedIn" or "LinkedIn Session"
4. Re-authenticate with a fresh session cookie
5. Save the phantom and try again

**Note:** You may need to update ALL your phantoms if they share the same LinkedIn account.
                """)

            st.button("Reset", key="pb_reset_btn", on_click=_cb_reset_phantom)

        else:  # idle
            # Database deduplication info
            if HAS_DATABASE:
                try:
                    db_client = _get_db_client()
                    if db_client:
                        dedup_stats = get_dedup_stats(db_client)
                        if dedup_stats.get('total_profiles', 0) > 0:
                            st.info(f"Database: **{dedup_stats.get('total_profiles', 0)}** profiles stored, **{dedup_stats.get('recently_enriched', 0)}** recently enriched")
                except:
                    pass

            # Button is enabled when phantom is found, validates URL on click
            if st.button("Launch", type="primary", key="pb_launch_btn", disabled=not user_phantom):
                # Validate URL on click
                if not search_url or 'linkedin.com' not in search_url:
                    st.error("Please enter a valid Sales Navigator URL first")
                    st.stop()

                st.session_state['pb_launch_status'] = 'launching'
                st.session_state['pb_launch_agent_id'] = user_phantom['id']
                st.session_state['pb_launch_error'] = None

                # Get skip list from database to avoid re-scraping existing profiles
                skip_urls = []
                skip_stats = {}
                if HAS_DATABASE:
                    skip_urls, skip_stats = get_skip_list_from_database()
                    if skip_urls:
                        st.session_state['pb_launch_skip_count'] = len(skip_urls)

                # Update phantom with new search URL, skip list, and timestamped output filename
                update_result = update_phantombuster_with_skip_list(
                    pb_key, user_phantom['id'], search_url, 2500,
                    csv_name=None, skip_urls=skip_urls
                )
                if update_result.get('success'):
                    # Store the generated filename for later retrieval
                    st.session_state['pb_launch_csv_name'] = update_result.get('csvName')

                    # Get usage tracker for logging
                    pb_tracker = get_usage_tracker()

                    # Launch without clearing results - new file will be created with unique name
                    result = launch_phantombuster_agent(pb_key, user_phantom['id'], None, clear_results=False, tracker=pb_tracker)

                    if 'containerId' in result:
                        st.session_state['pb_launch_container_id'] = result['containerId']
                        st.session_state['pb_launch_status'] = 'running'
                        st.session_state['pb_launch_start_time'] = time.time()

                        # Save search to history
                        csv_name = update_result.get('csvName')
                        if csv_name:
                            save_search_to_history(
                                agent_id=user_phantom['id'],
                                csv_name=csv_name,
                                search_url=search_url,
                                profiles_requested=2500,
                                search_name=search_name if search_name else None
                            )
                            # Clear cached search history so new entry shows up
                            _load_search_history_module.clear()
                    else:
                        st.session_state['pb_launch_status'] = 'error'
                        st.session_state['pb_launch_error'] = result.get('error', 'Unknown error')
                else:
                    st.session_state['pb_launch_status'] = 'error'
                    st.session_state['pb_launch_error'] = update_result.get('error', 'Failed to update search URL')

                st.rerun()

            if not user_name:
                st.info("Enter your name to find your phantom")
            elif not user_phantom:
                st.info("No phantom found - ask admin to create one for you")
            elif not search_url:
                st.info("Paste a Sales Navigator search URL to launch")

# ========== TAB 2: Filter ==========
with tab_filter:
    if 'results_df' not in st.session_state or st.session_state.get('results_df') is None or (isinstance(st.session_state.get('results_df'), pd.DataFrame) and st.session_state['results_df'].empty):
        st.info("Upload data in the Upload tab first.")
    else:
        df = st.session_state['results_df']

        # Header with profile count and export button
        header_col1, header_col2 = st.columns([3, 1])
        with header_col1:
            st.markdown(f"**{len(df)} profiles loaded** — configure filters below")
        with header_col2:
            export_df = prepare_df_for_export(df)
            csv_data = export_df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                "Export All (CSV)",
                csv_data,
                f"loaded_profiles_{len(df)}.csv",
                "text/csv",
                key="filter_tab_export_all"
            )

        # Warn if the loaded data isn't enriched yet — most filters need
        # enriched fields (current_title, current_company, skills, all_employers).
        # A sparse CSV upload that hasn't been Enriched / Loaded from DB will pass
        # most filters silently (no data to match), giving misleading results.
        _enriched_check = st.session_state.get('enriched_df')
        _is_enriched_ready = (
            _enriched_check is not None
            and isinstance(_enriched_check, pd.DataFrame)
            and not _enriched_check.empty
        )
        if not _is_enriched_ready:
            st.warning(
                "**These profiles aren't enriched yet.** Most filters (blacklist by company, "
                "target companies, title keywords, tenure, universities) need enriched data — "
                "go back to **1. Load** and click *Load existing from DB* or *Enrich N profiles* first. "
                "Only the **Past Candidates** name filter works on the raw CSV as-is."
            )

        needs_filtering = 'job_1_job_title' in df.columns and 'current_title' not in df.columns

        if needs_filtering:
            if st.button("Convert to Screening Format"):
                filtered_df = filter_csv_columns(df)
                st.session_state['results_df'] = filtered_df
                save_session_state()  # Save for restore
                st.rerun()

        # Check for Google Sheets config
        filter_sheets = get_filter_sheets_config().copy()
        gspread_client = get_gspread_client()

        # Allow user to specify their own filter sheet
        st.markdown("**Your Filter Sheet:**")
        st.caption(f"Share your sheet with: `linkedin-enricher@linkedin-enricher-485616.iam.gserviceaccount.com`")
        user_sheet_url = st.text_input(
            "Google Sheet URL (required — paste your sheet)",
            value=st.session_state.get('user_sheet_url', ''),
            placeholder="https://docs.google.com/spreadsheets/d/...",
            key="user_sheet_input"
        )

        # Save to session state and set default tab names.
        # No fallback to a baked-in default sheet: if the user hasn't pasted a
        # URL, clear it so render_filter_sheet_status shows the "paste a URL"
        # hint and downstream filters skip cleanly. Multi-user setups must not
        # share a single default sheet from config.json / secrets.toml.
        if user_sheet_url:
            st.session_state['user_sheet_url'] = user_sheet_url
            filter_sheets['url'] = user_sheet_url
            # Set default tab names if not already configured. These are tab
            # names within the user's own spreadsheet, not URLs, so the
            # defaults are safe to keep.
            if not filter_sheets.get('past_candidates'):
                filter_sheets['past_candidates'] = 'Past Candidates'
            if not filter_sheets.get('blacklist'):
                filter_sheets['blacklist'] = 'Blacklist'
            if not filter_sheets.get('not_relevant'):
                filter_sheets['not_relevant'] = 'NotRelevant Companies'
            if not filter_sheets.get('target_companies'):
                filter_sheets['target_companies'] = 'Target Companies'
            if not filter_sheets.get('tech_alerts'):
                filter_sheets['tech_alerts'] = 'Tech Alerts'
            if not filter_sheets.get('universities'):
                filter_sheets['universities'] = 'Universities'
            if not filter_sheets.get('client_wanted_companies'):
                filter_sheets['client_wanted_companies'] = 'Client specific wanted companies'
        else:
            # User left the input empty — treat the sheet as not connected.
            filter_sheets['url'] = ''

        sheet_connected = render_filter_sheet_status(filter_sheets, gspread_client)
        has_sheets = sheet_connected

        # Detailed verification — only useful when the sheet is reachable.
        if sheet_connected and st.button("Verify Sheet Connection", key="verify_sheet"):
            with st.spinner("Checking sheet access..."):
                try:
                    spreadsheet = gspread_client.open_by_url(filter_sheets['url'])
                    tabs = [ws.title for ws in spreadsheet.worksheets()]
                    st.success(f"Connected to **{spreadsheet.title}**! Found {len(tabs)} tabs")
                    expected_tabs = ['Past Candidates', 'Blacklist', 'NotRelevant Companies', 'Target Companies', 'Universities', 'Tech Alerts']
                    missing = [t for t in expected_tabs if t not in tabs]
                    if missing:
                        st.warning(f"Missing expected tabs: {', '.join(missing)}")
                    else:
                        st.info("All expected tabs found")
                except Exception as e:
                    st.error(f"Cannot access sheet: {e}")
                    st.info("Make sure you shared the sheet with the service account email above")

        st.divider()
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Filter Data Sources:**")

            # Past candidates
            use_sheets_past = False
            past_candidates_file = None
            if has_sheets and filter_sheets.get('past_candidates'):
                use_sheets_past = st.checkbox("Load Past Candidates from Google Sheet", value=True, key="use_sheets_past")
                if not use_sheets_past:
                    past_candidates_file = st.file_uploader("Past Candidates CSV", type=['csv'], key="past_candidates")
            else:
                past_candidates_file = st.file_uploader("Past Candidates CSV", type=['csv'], key="past_candidates")

            # Blacklist
            use_sheets_blacklist = False
            blacklist_file = None
            if has_sheets and filter_sheets.get('blacklist'):
                use_sheets_blacklist = st.checkbox("Load Blacklist from Google Sheet", value=True, key="use_sheets_blacklist")
                if not use_sheets_blacklist:
                    blacklist_file = st.file_uploader("Blacklist Companies CSV", type=['csv'], key="blacklist")
            else:
                blacklist_file = st.file_uploader("Blacklist Companies CSV", type=['csv'], key="blacklist")

            # Not relevant
            use_sheets_not_relevant = False
            not_relevant_file = None
            if has_sheets and filter_sheets.get('not_relevant'):
                use_sheets_not_relevant = st.checkbox("Load Not Relevant from Google Sheet", value=True, key="use_sheets_not_relevant")
                if not use_sheets_not_relevant:
                    not_relevant_file = st.file_uploader("Not Relevant Companies CSV", type=['csv'], key="not_relevant")
            else:
                not_relevant_file = st.file_uploader("Not Relevant Companies CSV", type=['csv'], key="not_relevant")

            nr_scope = st.radio(
                "Not Relevant scope:",
                ["Current company only", "All past employers"],
                key="nr_scope", horizontal=True,
                help="'All past employers' also checks every previous job — catches people who left a not-relevant company"
            )

        with col2:
            st.markdown("**Title Keywords Filter:**")
            st.caption("Exclude profiles with these keywords in title")

            # Predefined exclusion keywords by category
            EXCLUDE_CATEGORIES = {
                "Leadership": ["vp", "director", "manager", "head of", "product manager"],
                "C-Level/Founders": ["cto", "ceo", "coo", "cfo", "owner", "founder", "co-founder"],
                "Non-Employee": ["freelancer", "self employed", "consultant"],
                "Junior": ["student", "intern", "junior"],
                "Technical": ["qa", "automation", "embedded", "low level", "real time", "hardware", "firmware", "c++", "gui", "dsp", "unity", "integration", "test", "system", "systems"],
                "Mobile": ["mobile", "ios", "android"],
                "Design": ["ui", "ux", "design", "designer"],
                "Data/ML": ["big data", "machine learning", "data", "bi", "science", "ml", "algorithm", "algorithms", "computer vision", "algo"],
                "DevOps/Security": ["devops", "devsecops", "security", "it", "infra", "infrastructure"],
                "Product": ["product", "analyst", "research", "business"],
                "Support/Quality": ["client", "clients", "escalation", "quality", "support", "sales"]
            }
            ALL_EXCLUDE_TITLES = [kw for keywords in EXCLUDE_CATEGORIES.values() for kw in keywords]

            # Quick select buttons - organized in rows
            st.caption("Quick select (click to toggle):")

            # Scoped CSS: keep button labels on one line within a word and let
            # multi-word labels wrap at spaces rather than character-by-character.
            # Without this, Streamlit buttons inside a narrow nested column
            # collapse min-width and break every character onto its own line
            # (e.g. "C-Level/Founders" -> "C-/Lev/el/F/oun/der/s").
            #
            # Selectors are scoped to `.exclude-quick-select` so the styles only
            # apply to the exclude quick-select buttons (wrapped below) and do
            # NOT affect any other Streamlit buttons rendered on the page.
            st.markdown(
                """
                <style>
                .exclude-quick-select div[data-testid="stButton"] > button {
                    white-space: normal;
                    word-break: keep-all;
                    overflow-wrap: normal;
                    min-width: 0;
                    padding-left: 0.25rem;
                    padding-right: 0.25rem;
                }
                .exclude-quick-select div[data-testid="stButton"] > button > div,
                .exclude-quick-select div[data-testid="stButton"] > button p {
                    white-space: normal;
                    word-break: keep-all;
                    overflow-wrap: normal;
                }
                </style>
                """,
                unsafe_allow_html=True,
            )

            # Open a scoping wrapper so the CSS above only targets buttons
            # rendered inside this block.
            st.markdown('<div class="exclude-quick-select">', unsafe_allow_html=True)

            category_items = list(EXCLUDE_CATEGORIES.items())
            # Lay out as rows of 3 so each button has enough horizontal room
            # for its label (longest is "C-Level/Founders"). Buttons wrap to a
            # new row naturally as we re-enter the loop.
            buttons_per_row = 3
            quick_buttons = (
                [("All", ALL_EXCLUDE_TITLES, "exc_all", _cb_set_value,
                  ('exclude_title_presets', ALL_EXCLUDE_TITLES))]
                + [(name, kws, f"exc_{name}", _cb_toggle_exclude_preset, (kws,))
                   for name, kws in category_items]
                + [("Clear", [], "exc_clear", _cb_set_value,
                    ('exclude_title_presets', []))]
            )

            for row_start in range(0, len(quick_buttons), buttons_per_row):
                row = quick_buttons[row_start:row_start + buttons_per_row]
                row_cols = st.columns(buttons_per_row)
                for col_idx, (label, _kws, key, cb, cb_args) in enumerate(row):
                    with row_cols[col_idx]:
                        st.button(label, key=key, width="stretch",
                                  on_click=cb, args=cb_args)

            # Close the scoping wrapper.
            st.markdown('</div>', unsafe_allow_html=True)

            selected_exclude = st.multiselect(
                "Selected exclusions",
                options=ALL_EXCLUDE_TITLES,
                default=st.session_state.get('exclude_title_presets', []),
                key="exclude_title_presets"
            )
            exclude_title_keywords = st.text_input(
                "Additional keywords (comma-separated)",
                placeholder="e.g., lead, principal",
                key="exclude_title_keywords"
            )

            st.markdown("**Include Title Keywords:**")
            st.caption("Only keep profiles with these keywords (leave empty for all)")
            include_title_keywords = st.text_input(
                "Include title keywords (comma-separated)",
                placeholder="e.g., engineer, developer, architect",
                key="include_title_keywords"
            )

            # Duration filters (only show if data has duration columns - typically PhantomBuster)
            # Check for various column name formats (case-insensitive)
            def find_duration_col(df, patterns):
                """Find first matching duration column from patterns (case-insensitive)."""
                cols_lower = {c.lower(): c for c in df.columns}
                for pattern in patterns:
                    if pattern.lower() in cols_lower:
                        return cols_lower[pattern.lower()]
                return None

            role_duration_col = find_duration_col(df, ['durationInRole', 'duration_in_role', 'current_years_in_role', 'yearsInRole', 'years_in_role', 'Duration in role', 'duration in role', 'Time in role', 'time in role'])
            company_duration_col = find_duration_col(df, ['durationInCompany', 'duration_in_company', 'current_years_at_company', 'yearsInCompany', 'years_at_company', 'Duration in company', 'duration in company', 'Time in company', 'time in company'])
            has_role_duration = role_duration_col is not None
            has_company_duration = company_duration_col is not None

            min_role_months = 0
            max_role_months = 0
            min_company_months = 0
            max_company_months = 0

            st.markdown("**Tenure / Duration Filters:**")
            if not has_role_duration and not has_company_duration:
                st.caption("Tenure data not in current results — computed from employer start dates when profiles are saved to DB.")
            dur_col1, dur_col2 = st.columns(2)
            with dur_col1:
                min_role_months = st.number_input("Min months in role", min_value=0, max_value=240, value=0, key="min_role_months", disabled=not has_role_duration)
                min_company_months = st.number_input("Min months at company", min_value=0, max_value=240, value=0, key="min_company_months", disabled=not has_company_duration)
            with dur_col2:
                max_role_months = st.number_input("Max months in role", min_value=0, max_value=480, value=0, help="0 = no limit", key="max_role_months", disabled=not has_role_duration)
                max_company_months = st.number_input("Max months at company", min_value=0, max_value=480, value=0, help="0 = no limit", key="max_company_months", disabled=not has_company_duration)

        st.markdown("**Keyword Search (searches full profile — skills, headline, summary, work history):**")
        bool_kw_col1, bool_kw_col2 = st.columns(2)
        with bool_kw_col1:
            keywords_full_profile = st.text_input(
                "Full profile boolean search",
                placeholder='e.g., python AND (aws OR gcp) NOT "data scientist"',
                key="keywords_full_profile",
                help="Supports AND / OR / NOT and quoted phrases. Searches skills, headline, summary, and all work history."
            )
        with bool_kw_col2:
            keywords_skills_only = st.text_input(
                "Skills only boolean search",
                placeholder="e.g., react OR vue OR angular",
                key="keywords_skills_only",
                help="Same boolean syntax but searches only the Skills field."
            )

        st.divider()

        btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 2])
        with btn_col1:
            apply_clicked = st.button("Apply Filters", type="primary", key="apply_filters_main")
        with btn_col2:
            if st.button("Reset Filters", key="reset_filters_main"):
                # Reset to original unfiltered data
                original_df = st.session_state.get('original_results_df')
                if original_df is not None and not (isinstance(original_df, pd.DataFrame) and original_df.empty):
                    st.session_state['results_df'] = original_df
                    st.session_state['passed_candidates_df'] = original_df
                    for key in ['filter_stats', 'filtered_out', 'filtered_out_counts', 'filtered_out_light']:
                        if key in st.session_state:
                            del st.session_state[key]
                    st.success(f"Reset to {len(original_df)} profiles")
                    # Tell the user any SalesQL emails enriched during this session
                    # revert in-memory to the pre-enrichment baseline. They're safe in
                    # Supabase — a reload picks them back up — but the visible state
                    # right after Reset will look "less enriched" than before. Heads-up
                    # so they don't think emails were lost.
                    st.caption(
                        "Note: in-memory emails added during this session revert to the "
                        "pre-enrichment baseline. They're preserved in Supabase — re-running "
                        "*Load existing from DB* or re-uploading the CSV brings them back."
                    )
                    save_session_state()
                    st.rerun()
                else:
                    st.warning("No original data to restore. Try reloading your CSV/data.")

        if apply_clicked:
            # Store original data before first filter (only if not already stored)
            if 'original_results_df' not in st.session_state or st.session_state.get('original_results_df') is None:
                st.session_state['original_results_df'] = st.session_state.get('results_df').copy()
            filters = {}

            # Load filter data from Google Sheets or files
            with st.spinner("Loading filter data..."):
                sheet_url = filter_sheets.get('url', '')

                # Past candidates
                if use_sheets_past and filter_sheets.get('past_candidates'):
                    past_df = load_sheet_as_df(sheet_url, filter_sheets['past_candidates'])
                    if past_df is not None:
                        filters['past_candidates_df'] = past_df
                        st.info(f"Loaded {len(past_df)} past candidates from Google Sheet (columns: {list(past_df.columns)[:5]})")
                elif past_candidates_file:
                    filters['past_candidates_df'] = pd.read_csv(past_candidates_file)

                # Blacklist (read ALL columns)
                if use_sheets_blacklist and filter_sheets.get('blacklist'):
                    bl_df = load_sheet_as_df(sheet_url, filter_sheets['blacklist'])
                    if bl_df is not None and len(bl_df.columns) > 0:
                        blacklist_items = []
                        for col in bl_df.columns:
                            blacklist_items.extend(bl_df[col].dropna().tolist())
                        filters['blacklist'] = list(set(blacklist_items))  # Dedupe
                        st.info(f"Loaded {len(filters['blacklist'])} blacklist companies from Google Sheet ({len(bl_df.columns)} columns)")
                elif blacklist_file:
                    bl_df = pd.read_csv(blacklist_file)
                    blacklist_items = []
                    for col in bl_df.columns:
                        blacklist_items.extend(bl_df[col].dropna().tolist())
                    filters['blacklist'] = list(set(blacklist_items))

                # Not relevant (read ALL columns)
                if use_sheets_not_relevant and filter_sheets.get('not_relevant'):
                    nr_df = load_sheet_as_df(sheet_url, filter_sheets['not_relevant'])
                    if nr_df is not None and len(nr_df.columns) > 0:
                        not_relevant_list = []
                        for col in nr_df.columns:
                            not_relevant_list.extend(nr_df[col].dropna().tolist())
                        filters['not_relevant'] = list(set(not_relevant_list))  # Dedupe
                        st.session_state['nr_for_search'] = filters['not_relevant']
                        st.info(f"Loaded {len(filters['not_relevant'])} not-relevant companies from Google Sheet ({len(nr_df.columns)} columns)")
                elif not_relevant_file:
                    nr_df = pd.read_csv(not_relevant_file)
                    not_relevant_list = []
                    for col in nr_df.columns:
                        not_relevant_list.extend(nr_df[col].dropna().tolist())
                    filters['not_relevant'] = list(set(not_relevant_list))

                # Title keywords - combine presets and custom
                all_exclude_titles = list(selected_exclude)  # From multiselect
                if exclude_title_keywords:
                    all_exclude_titles.extend([kw.strip().lower() for kw in exclude_title_keywords.split(',') if kw.strip()])
                if all_exclude_titles:
                    filters['exclude_titles'] = all_exclude_titles
                if include_title_keywords:
                    filters['include_titles'] = [kw.strip().lower() for kw in include_title_keywords.split(',') if kw.strip()]

                # Duration filters
                if min_role_months > 0:
                    filters['min_role_months'] = min_role_months
                if max_role_months > 0:
                    filters['max_role_months'] = max_role_months
                if min_company_months > 0:
                    filters['min_company_months'] = min_company_months
                if max_company_months > 0:
                    filters['max_company_months'] = max_company_months

            # Apply filters
            with st.spinner("Applying filters..."):
                df = st.session_state['results_df']
                filtered_df, stats, filtered_out = apply_pre_filters(df, filters)

                # Track which filters were enabled
                stats['_filters_enabled'] = list(filters.keys())

                # Not Relevant — all past employers scope
                if nr_scope == "All past employers" and filters.get('not_relevant') and 'all_employers' in filtered_df.columns:
                    _nr_list = [c.lower().strip() for c in filters['not_relevant']]
                    def _has_nr_employer(employers_data):
                        if employers_data is None: return False
                        try:
                            if pd.isna(employers_data): return False
                        except (ValueError, TypeError): pass
                        if not employers_data: return False
                        emps = employers_data if isinstance(employers_data, (list, tuple)) else [e.strip() for e in str(employers_data).split(',')]
                        return any(_company_matches_filter_list(e, _nr_list) for e in emps if e)
                    _nr_all_mask = filtered_df['all_employers'].apply(_has_nr_employer)
                    _nr_all_count = int(_nr_all_mask.sum())
                    stats['not_relevant_all_employers'] = _nr_all_count
                    # Mirror _store_filtered's contract: capture a lightweight
                    # snapshot (display columns, max 100 rows) into filtered_out
                    # so the "Review filtered out candidates" dropdown can show
                    # this category alongside the current-company variant.
                    if _nr_all_count > 0:
                        _nr_all_label = 'Not Relevant (All Employers)'
                        _nr_light_cols = ['name', 'first_name', 'last_name', 'current_title', 'current_company', 'linkedin_url']
                        _nr_avail = [c for c in _nr_light_cols if c in filtered_df.columns]
                        if _nr_avail:
                            filtered_out[_nr_all_label] = filtered_df.loc[_nr_all_mask, _nr_avail].head(100).copy()
                        else:
                            filtered_out[_nr_all_label] = filtered_df.loc[_nr_all_mask, list(filtered_df.columns)[:5]].head(100).copy()
                    filtered_df = filtered_df[~_nr_all_mask]

                # Boolean keyword search — full profile and/or skills only
                try:
                    from boolean_query import match_boolean_query as _mbq
                    import json as _json_mod
                    def _row_text(row, cols):
                        parts = []
                        for c in cols:
                            if c not in row or row.get(c) is None: continue
                            val = row[c]
                            parts.append(_json_mod.dumps(val) if isinstance(val, dict) else str(val))
                        return ' '.join(parts)

                    if keywords_full_profile and keywords_full_profile.strip():
                        _fp_cols = [c for c in ['skills', 'all_titles', 'all_employers', 'past_positions', 'summary', 'headline'] if c in filtered_df.columns]
                        if _fp_cols:
                            _fp_mask = filtered_df.apply(lambda r: _mbq(keywords_full_profile, _row_text(r, _fp_cols)), axis=1)
                            stats['missing_keywords_full_profile'] = int((~_fp_mask).sum())
                            filtered_df = filtered_df[_fp_mask]

                    if keywords_skills_only and keywords_skills_only.strip() and 'skills' in filtered_df.columns:
                        _sk_mask = filtered_df['skills'].fillna('').astype(str).apply(lambda t: _mbq(keywords_skills_only, t))
                        stats['missing_keywords_skills'] = int((~_sk_mask).sum())
                        filtered_df = filtered_df[_sk_mask]
                except ImportError:
                    if keywords_full_profile or keywords_skills_only:
                        st.warning("Boolean keyword search unavailable — boolean_query module not found.")

                # Sync `final` and `total_removed` with the actual post-trim state.
                # apply_pre_filters set both before the post-apply trims (Not Relevant
                # All Employers + Boolean keyword search) ran, so the "X remaining"
                # banner, the Remaining metric, and the Removed metric all reported
                # stale numbers that didn't add up to Original.
                stats['final'] = len(filtered_df)
                stats['total_removed'] = stats.get('original', len(filtered_df)) - len(filtered_df)
                st.session_state['passed_candidates_df'] = filtered_df  # Store filtered results separately
                st.session_state['results_df'] = filtered_df
                st.session_state['filter_stats'] = stats

                # filtered_out is already lightweight (display cols only, max 100 per category)
                # Get real counts from stats (filtered_out DataFrames are capped at 100)
                real_counts = {
                    'Past Candidates': stats.get('past_candidates', 0),
                    'Blacklist Companies': stats.get('blacklist', 0),
                    'Not Relevant (Current)': stats.get('not_relevant_current', 0),
                    'Not Relevant (All Employers)': stats.get('not_relevant_all_employers', 0),
                    'Excluded Titles': stats.get('excluded_titles', 0),
                    'Not Matching Titles': stats.get('not_matching_titles', 0),
                    'Role Too Short': stats.get('role_too_short', 0),
                    'Role Too Long': stats.get('role_too_long', 0),
                    'Company Too Short': stats.get('company_too_short', 0),
                    'Company Too Long': stats.get('company_too_long', 0),
                    'Not Top University': stats.get('not_top_university', 0),
                }
                st.session_state['filtered_out_counts'] = {k: real_counts.get(k, len(v)) for k, v in filtered_out.items()}
                st.session_state['filtered_out_light'] = filtered_out
                cleanup_memory()  # Aggressive memory cleanup
                save_session_state()  # Save for restore

            st.success(f"Filtering complete! {stats['final']} candidates remaining")
            st.rerun()


    # Show filter stats if available
    if 'filter_stats' in st.session_state:
        stats = st.session_state['filter_stats']
        st.markdown("**Filter Results:**")
        stats_cols = st.columns(4)
        with stats_cols[0]:
            st.metric("Original", stats.get('original', 0))
        with stats_cols[1]:
            st.metric("Removed", stats.get('total_removed', 0))
        with stats_cols[2]:
            st.metric("Remaining", stats.get('final', 0))
        with stats_cols[3]:
            pct = round((stats.get('final', 0) / stats.get('original', 1)) * 100) if stats.get('original', 0) > 0 else 0
            st.metric("Keep Rate", f"{pct}%")

        with st.expander("Detailed Breakdown"):
            st.markdown("**Filtered Out:**")
            exclude_keys = ['original', 'final', 'total_removed', 'removed_by', 'priority_count', '_filters_enabled', '_skipped_filters']
            shown_any = False
            for key, value in stats.items():
                if key not in exclude_keys and not key.startswith('_'):
                    try:
                        num_value = int(value)
                        if num_value > 0:
                            st.text(f"  ✗ {key.replace('_', ' ').title()}: {num_value} removed")
                            shown_any = True
                    except (TypeError, ValueError):
                        pass

            # Show skipped filters (missing columns)
            skipped = stats.get('_skipped_filters', [])
            if skipped:
                st.markdown("**⚠️ Skipped Filters (missing data):**")
                for skip_reason in skipped:
                    st.caption(f"  - {skip_reason}")

            if not shown_any and stats.get('total_removed', 0) > 0:
                filters_enabled = stats.get('_filters_enabled', [])
                if filters_enabled:
                    st.caption(f"Filters enabled: {', '.join(filters_enabled)}")
                st.caption("No individual matches found. Check column names in your data.")

        # View filtered-out candidates by category
        filtered_out_light = st.session_state.get('filtered_out_light', {})
        skipped_filters = stats.get('_skipped_filters', [])

        with st.expander("View Filtered-Out Candidates", expanded=False):
            # Show skipped filters warning
            if skipped_filters:
                st.warning(f"**Skipped filters** (missing data columns):")
                for skip in skipped_filters:
                    st.caption(f"  ⚠️ {skip}")
                st.divider()

            if filtered_out_light:
                filter_tabs = list(filtered_out_light.keys())
                if filter_tabs:
                    selected_filter = st.selectbox(
                        "Select filter category",
                        filter_tabs,
                        format_func=lambda x: f"{x.replace('_', ' ').title()} ({len(filtered_out_light[x])} shown)"
                    )

                    if selected_filter and selected_filter in filtered_out_light:
                        view_df = filtered_out_light[selected_filter]
                        total_count = st.session_state.get('filtered_out_counts', {}).get(selected_filter, len(view_df))

                        st.markdown(f"**{selected_filter.replace('_', ' ').title()}**: {total_count} removed")
                        if total_count > 100:
                            st.caption(f"Showing first 100 of {total_count}")

                        # Determine best name column
                        if 'name' in view_df.columns and view_df['name'].notna().any():
                            name_col = 'name'
                        elif 'first_name' in view_df.columns:
                            view_df = view_df.copy()
                            view_df['name'] = (view_df.get('first_name', '').fillna('') + ' ' + view_df.get('last_name', '').fillna('')).str.strip()
                            name_col = 'name'
                        else:
                            name_col = None

                        # Display columns
                        display_cols = ['name', 'current_title', 'current_company', 'linkedin_url']
                        if name_col is None:
                            display_cols = list(view_df.columns)[:4]
                        available = [c for c in display_cols if c in view_df.columns]

                        st.dataframe(
                            view_df[available] if available else view_df,
                            width="stretch",
                            hide_index=True,
                            column_config={
                                "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                                "public_url": st.column_config.LinkColumn("LinkedIn"),
                            }
                        )
            elif not skipped_filters:
                st.info("No candidates were filtered out.")

    # Passed Candidates Preview (after filtering)
    if 'filter_stats' in st.session_state:
        passed_preview_df = st.session_state.get('passed_candidates_df')
        if passed_preview_df is not None and not passed_preview_df.empty:
            with st.expander(f"View Passed Candidates ({len(passed_preview_df)})", expanded=True):
                # Determine display columns - include priority columns if they exist
                preview_cols = ['name', 'current_title', 'current_company', 'location']
                # Add priority columns if present
                for prio_col in ['is_target_company', 'is_layoff_company', 'is_client_wanted']:
                    if prio_col in passed_preview_df.columns:
                        preview_cols.append(prio_col)
                preview_cols.append('linkedin_url')
                # Handle PhantomBuster column names
                if 'name' not in passed_preview_df.columns and 'firstName' in passed_preview_df.columns:
                    display_df = passed_preview_df.copy()
                    display_df['name'] = (display_df.get('firstName', '').fillna('') + ' ' + display_df.get('lastName', '').fillna('')).str.strip()
                else:
                    display_df = passed_preview_df

                # Map alternative column names
                col_mapping = {
                    'current_title': ['title', 'jobTitle', 'job_title', 'headline'],
                    'current_company': ['company', 'companyName', 'company_name'],
                    'linkedin_url': ['linkedinUrl', 'publicUrl', 'public_url', 'profileUrl', 'profile_url']
                }
                for target, alternatives in col_mapping.items():
                    if target not in display_df.columns:
                        for alt in alternatives:
                            if alt in display_df.columns:
                                display_df[target] = display_df[alt]
                                break

                available_cols = [c for c in preview_cols if c in display_df.columns]
                if not available_cols:
                    available_cols = list(display_df.columns)[:4]

                st.dataframe(
                    display_df[available_cols].head(100),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                        "is_target_company": st.column_config.CheckboxColumn("Target", default=False),
                        "is_layoff_company": st.column_config.CheckboxColumn("Layoff", default=False),
                        "is_client_wanted": st.column_config.CheckboxColumn("Client", default=False),
                    }
                )
                if len(passed_preview_df) > 100:
                    st.caption(f"Showing first 100 of {len(passed_preview_df)} passed candidates")

            # Export button always visible after filtering (outside expander)
            _exp_col1, _exp_col2 = st.columns([1, 2])
            with _exp_col1:
                _export_filtered = prepare_df_for_export(passed_preview_df)
                _csv_filtered = _export_filtered.to_csv(index=False).encode('utf-8-sig')
                st.download_button(
                    f"Export Filtered ({len(passed_preview_df)}) CSV",
                    _csv_filtered,
                    f"filtered_candidates_{len(passed_preview_df)}.csv",
                    "text/csv",
                    key="filter_tab_export_filtered",
                    type="primary"
                )

            # ===== SalesQL personal-email enrichment for passed-only =====
            # Explicit scope: only the profiles that survived filtering. Avoids
            # accidentally spending credits on the full upload when the recruiter
            # only wants emails for the short-list. Personal-only by default —
            # work emails come later via the AI Screen / outreach flow.
            with _exp_col2:
                _passed_salesql_key = load_salesql_key()
                if _passed_salesql_key:
                    _passed_df_full = st.session_state.get('passed_candidates_df')
                    if _passed_df_full is not None and not _passed_df_full.empty:
                        _have_email, _need_email = count_passed_email_split(_passed_df_full)
                        # Show the split so the user always knows the full picture:
                        # how many already have an email vs. how many would be enriched.
                        st.caption(
                            f"**{len(_passed_df_full)}** passed · "
                            f"**{_have_email}** already have an email · "
                            f"**{_need_email}** need personal email"
                        )
                        if _need_email > 0:
                            if st.button(
                                f"Enrich {_need_email} passed with personal email",
                                key="filter_passed_salesql_personal",
                                help="SalesQL personal-email enrichment on profiles that survived filtering. Skips profiles that already have an email.",
                            ):
                                _progress = st.progress(0)
                                _status = st.empty()
                                def _passed_progress(current, total):
                                    _progress.progress(current / total if total else 0)
                                    _status.text(f"Enriching {current}/{total}...")
                                _enriched_passed = enrich_profiles_with_salesql(
                                    _passed_df_full,
                                    _passed_salesql_key,
                                    progress_callback=_passed_progress,
                                    personal_only=True,
                                    limit=_need_email,
                                )
                                # Persist new emails to DB for cross-session reuse
                                if HAS_DATABASE:
                                    _dbc = _get_db_client()
                                    if _dbc:
                                        _emails_to_save = []
                                        for _, _r in _enriched_passed.iterrows():
                                            _em = _r.get('salesql_email')
                                            _url = _r.get('linkedin_url')
                                            if _em and _url and str(_em).strip():
                                                _emails_to_save.append({'linkedin_url': _url, 'email': _em})
                                        if _emails_to_save:
                                            _res = update_profile_emails_batch(_dbc, _emails_to_save, source='salesql')
                                            if _res.get('saved', 0) > 0:
                                                st.caption(f"💾 Saved {_res['saved']} emails to database")
                                # Update both refs so the new emails are visible everywhere
                                st.session_state['passed_candidates_df'] = _enriched_passed
                                st.session_state['results_df'] = _enriched_passed
                                save_session_state()
                                _new_emails = (
                                    int(_enriched_passed['salesql_email'].notna().sum())
                                    if 'salesql_email' in _enriched_passed.columns else 0
                                )
                                st.success(f"Done. {_new_emails} passed profiles now have a personal email.")
                                st.rerun()
                        else:
                            st.caption("All passed profiles already have an email.")
                else:
                    st.caption("SalesQL not configured.")

    # Priority Lists Section (Step 2 - after filtering)
    st.divider()
    st.markdown("##### 📊 Priority Lists")
    st.caption("Categorize filtered candidates by company lists from Google Sheet")

    # Get passed candidates
    if 'passed_candidates_df' in st.session_state and st.session_state.get('passed_candidates_df') is not None and not st.session_state['passed_candidates_df'].empty:
        passed_df = st.session_state['passed_candidates_df']
    elif 'results_df' in st.session_state and st.session_state.get('results_df') is not None and not st.session_state['results_df'].empty:
        passed_df = st.session_state.get('results_df', pd.DataFrame())
    else:
        passed_df = pd.DataFrame()

    if passed_df is not None and len(passed_df) > 0:
        prio_col1, prio_col2, prio_col3, prio_col4 = st.columns([1, 1, 1, 1])
        with prio_col1:
            load_target_companies = st.checkbox("Target Companies", value=False, key="load_target_companies")
        with prio_col2:
            load_tech_alerts = st.checkbox("Tech Alerts / Layoffs", value=False, key="load_tech_alerts")
        with prio_col3:
            load_client_wanted = st.checkbox("Client Wanted", value=False, key="load_client_wanted")
            client_wanted_scope = st.radio(
                "Client Wanted scope:",
                ["Current company only", "All employers"],
                key="client_wanted_scope", horizontal=True
            )
        with prio_col4:
            load_target_universities = st.checkbox("Target Universities", value=False, key="load_target_universities")
            apply_priority = st.button("Apply Priority Lists", type="secondary", key="apply_priority_lists")

        if apply_priority and (load_target_companies or load_tech_alerts or load_client_wanted or load_target_universities):
            with st.spinner("Loading priority lists..."):
                priority_df = passed_df.copy()
                sheet_url = filter_sheets.get('url', '')
                priority_loaded = []

                # Use the module-level company-filter helper so all callsites
                # share the same matching rules (incl. token-subset).
                matches_list = _company_matches_filter_list

                # Target companies
                if load_target_companies and filter_sheets.get('target_companies'):
                    tc_df = load_sheet_as_df(sheet_url, filter_sheets['target_companies'])
                    if tc_df is not None and len(tc_df.columns) > 0:
                        target_companies = []
                        for col in tc_df.columns:
                            if 'company' in col.lower() or 'name' in col.lower():
                                target_companies.extend(tc_df[col].dropna().tolist())
                        target_list = [str(c).lower().strip() for c in target_companies if c]
                        if target_list:
                            if 'current_company' in priority_df.columns:
                                priority_df['is_target_company'] = priority_df['current_company'].apply(lambda x: matches_list(x, target_list))
                                priority_loaded.append(f"Target: {len(target_list)} companies, {priority_df['is_target_company'].sum()} matches")
                            else:
                                st.warning("Cannot match target companies: 'current_company' column not found in data")

                # Tech alerts / Layoffs
                if load_tech_alerts and filter_sheets.get('tech_alerts'):
                    ta_df = load_sheet_as_df(sheet_url, filter_sheets['tech_alerts'])
                    if ta_df is not None and len(ta_df.columns) > 0:
                        tech_alerts = []
                        for col in ta_df.columns:
                            if 'company' in col.lower() or 'name' in col.lower():
                                tech_alerts.extend(ta_df[col].dropna().tolist())
                        alerts_list = [str(c).lower().strip() for c in tech_alerts if c]
                        if alerts_list:
                            if 'current_company' in priority_df.columns:
                                priority_df['is_layoff_company'] = priority_df['current_company'].apply(lambda x: matches_list(x, alerts_list))
                                priority_loaded.append(f"Layoffs: {len(alerts_list)} companies, {priority_df['is_layoff_company'].sum()} matches")
                            else:
                                st.warning("Cannot match layoff companies: 'current_company' column not found in data")

                # Client Wanted Companies
                if load_client_wanted and filter_sheets.get('client_wanted_companies'):
                    cw_df = load_sheet_as_df(sheet_url, filter_sheets['client_wanted_companies'])
                    if cw_df is not None and len(cw_df.columns) > 0:
                        client_wanted = []
                        for col in cw_df.columns:
                            if 'company' in col.lower() or 'name' in col.lower():
                                client_wanted.extend(cw_df[col].dropna().tolist())
                        # Fallback: if no matching columns found, try all columns
                        if not client_wanted:
                            for col in cw_df.columns:
                                client_wanted.extend(cw_df[col].dropna().tolist())
                        client_list = [str(c).lower().strip() for c in client_wanted if c]
                        client_list = list(set(client_list))
                        if client_list:
                            def _has_client_wanted(row):
                                if matches_list(row.get('current_company'), client_list):
                                    return True
                                if client_wanted_scope == "All employers" and 'all_employers' in row.index:
                                    emps_raw = row.get('all_employers')
                                    if emps_raw is None: return False
                                    try:
                                        if pd.isna(emps_raw): return False
                                    except (ValueError, TypeError): pass
                                    emps = emps_raw if isinstance(emps_raw, (list, tuple)) else [e.strip() for e in str(emps_raw).split(',')]
                                    return any(matches_list(e, client_list) for e in emps if e)
                                return False
                            priority_df['is_client_wanted'] = priority_df.apply(_has_client_wanted, axis=1)
                            scope_label = "all employers" if client_wanted_scope == "All employers" else "current"
                            priority_loaded.append(f"Client Wanted ({scope_label}): {len(client_list)} companies, {priority_df['is_client_wanted'].sum()} matches")

                # Target Universities
                if load_target_universities and filter_sheets.get('universities'):
                    uni_df = load_sheet_as_df(sheet_url, filter_sheets['universities'])
                    if uni_df is not None and not uni_df.empty:
                        target_unis = set()
                        for col in uni_df.columns:
                            target_unis.update(uni_df[col].dropna().str.lower().str.strip().tolist())
                        if target_unis and 'all_schools' in priority_df.columns:
                            def _has_target_uni(schools_data):
                                if schools_data is None: return False
                                try:
                                    if pd.isna(schools_data): return False
                                except (ValueError, TypeError): pass
                                if not schools_data: return False
                                schools = schools_data if isinstance(schools_data, (list, tuple)) else [s.strip().lower() for s in str(schools_data).split(',')]
                                for school in [str(s).strip().lower() for s in schools]:
                                    for target in target_unis:
                                        if not target: continue
                                        if school == target or school.startswith(target) or target.startswith(school): return True
                                        if len(target) > 8 and len(school) > 8:
                                            tw = set(target.split()); sw = set(school.split())
                                            if len(tw & sw) >= len(tw) * 0.7: return True
                                return False
                            priority_df['is_target_university'] = priority_df['all_schools'].apply(_has_target_uni)
                            priority_loaded.append(f"Target Universities: {priority_df['is_target_university'].sum()} matches")

                # Save with priority columns
                st.session_state['passed_candidates_df'] = priority_df
                st.session_state['results_df'] = priority_df

                if priority_loaded:
                    st.success(f"Priority lists applied: {' | '.join(priority_loaded)}")
                st.rerun()

    # Show priority category filters if data has priority columns
    has_any_priority = passed_df is not None and len(passed_df) > 0 and (
        'is_target_company' in passed_df.columns or
        'is_layoff_company' in passed_df.columns or
        'is_client_wanted' in passed_df.columns or
        'is_target_university' in passed_df.columns
    )

    if has_any_priority:

        # Filter checkboxes at the top
        st.markdown("**Filter by category:**")

        has_target = 'is_target_company' in passed_df.columns
        has_layoff = 'is_layoff_company' in passed_df.columns
        has_client_wanted = 'is_client_wanted' in passed_df.columns
        has_target_uni = 'is_target_university' in passed_df.columns

        filter_cols = st.columns(5)
        with filter_cols[0]:
            show_all = st.checkbox("All", value=True, key="filter_all")
        with filter_cols[1]:
            if has_target:
                count = int(passed_df['is_target_company'].fillna(False).sum())
                show_target = st.checkbox(f"Target ({count})", value=False, key="filter_target")
            else:
                show_target = False
        with filter_cols[2]:
            if has_layoff:
                count = int(passed_df['is_layoff_company'].fillna(False).sum())
                show_layoff = st.checkbox(f"Layoff ({count})", value=False, key="filter_layoff")
            else:
                show_layoff = False
        with filter_cols[3]:
            if has_client_wanted:
                count = int(passed_df['is_client_wanted'].fillna(False).sum())
                show_client_wanted = st.checkbox(f"Client Wanted ({count})", value=False, key="filter_client_wanted")
            else:
                show_client_wanted = False
        with filter_cols[4]:
            if has_target_uni:
                count = int(passed_df['is_target_university'].fillna(False).sum())
                show_target_uni = st.checkbox(f"University ({count})", value=False, key="filter_target_uni")
            else:
                show_target_uni = False

        # Filter based on checkboxes
        if show_all or (not show_target and not show_layoff and not show_client_wanted and not show_target_uni):
            view_df = passed_df.copy()
        else:
            # Combine selected filters with OR
            mask = pd.Series([False] * len(passed_df), index=passed_df.index)
            if show_target and has_target:
                mask = mask | passed_df['is_target_company'].fillna(False)
            if show_layoff and has_layoff:
                mask = mask | passed_df['is_layoff_company'].fillna(False)
            if show_client_wanted and has_client_wanted:
                mask = mask | passed_df['is_client_wanted'].fillna(False)
            if show_target_uni and has_target_uni:
                mask = mask | passed_df['is_target_university'].fillna(False)
            view_df = passed_df[mask].copy()

        st.success(f"**{len(view_df)}** candidates")

        # Toggle to show all columns
        show_all_cols_filter = st.checkbox("Show all columns", value=False, key="filter_show_all_cols")

        if show_all_cols_filter:
            # Show all columns
            st.dataframe(
                view_df,
                width="stretch",
                hide_index=True,
                column_config={
                    "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                    "public_url": st.column_config.LinkColumn("LinkedIn"),
                }
            )
            st.caption(f"{len(view_df.columns)} columns")
        else:
            # Display columns (without category columns)
            # Prefer 'name' over 'first_name'/'last_name' for DB-loaded profiles
            display_cols = ['name', 'current_title', 'current_company', 'location', 'education', 'linkedin_url']
            # Fallback for PhantomBuster data with separate name columns
            if 'name' not in view_df.columns or view_df['name'].isna().all():
                display_cols = ['first_name', 'last_name', 'current_title', 'current_company', 'location', 'education', 'public_url']
            available_cols = [c for c in display_cols if c in view_df.columns]

            if available_cols:
                st.dataframe(
                    view_df[available_cols],
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "public_url": st.column_config.LinkColumn("LinkedIn"),
                    }
                )

        # Download and Send to AI Screen buttons
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            export_view_df = prepare_df_for_export(view_df)
            csv_data = export_view_df.to_csv(index=False)
            st.download_button(
                label="Download View (CSV)",
                data=csv_data,
                file_name="passed_candidates.csv",
                mime="text/csv"
            )
        with col2:
            if st.button(f"📤 Send {len(view_df)} to AI Screen", type="primary", key="send_to_enrich"):
                # MEMORY: Single copy shared by both (they're always used together)
                enrich_df = view_df.copy()
                st.session_state['results_df'] = enrich_df
                st.session_state['passed_candidates_df'] = enrich_df
                st.success(f"✓ {len(view_df)} profiles ready. Go to **3. AI Screen** tab.")
                save_session_state()

    # Review filtered candidates section - MEMORY OPTIMIZED: only show counts
    if 'filtered_out_counts' in st.session_state and st.session_state['filtered_out_counts']:
        render_filter_funnel_breakdown(st.session_state['filtered_out_counts'])

    # ===== SalesQL Email Enrichment =====
    st.divider()
    st.markdown("### Email Enrichment (SalesQL)")
    salesql_key = load_salesql_key()
    if salesql_key:
        current_df = get_profiles_df()
        if not current_df.empty:
            current_count = len(current_df)
            # Check both email fields (DB email and SalesQL email)
            email_mask = pd.Series([False] * len(current_df), index=current_df.index)
            if 'salesql_email' in current_df.columns:
                email_mask |= (current_df['salesql_email'].notna() & (current_df['salesql_email'] != ''))
            if 'email' in current_df.columns:
                email_mask |= (current_df['email'].notna() & (current_df['email'] != ''))
            already_enriched = email_mask.sum()
            not_enriched = current_count - already_enriched
            st.caption(f"{current_count} profiles | {already_enriched} already have emails | {not_enriched} remaining")

            # Ask how many to enrich
            if not_enriched > 0:
                col_option, col_number = st.columns([1, 1])
                with col_option:
                    enrich_option = st.radio("How many to enrich?", ["All", "Specific number"], key="enrich_option_tab2", horizontal=True)
                with col_number:
                    if enrich_option == "Specific number":
                        enrich_count = st.number_input("Number of profiles", min_value=1, max_value=not_enriched, value=min(50, not_enriched), key="enrich_count_tab2")
                    else:
                        enrich_count = not_enriched

                if st.button(f"Enrich {enrich_count} profiles with Emails", key="salesql_tab2", type="primary"):
                    progress_bar = st.progress(0)
                    status_text = st.empty()

                    def update_progress(current, total):
                        progress_bar.progress(current / total)
                        status_text.text(f"Enriching {current}/{total}...")

                    enriched_df = enrich_profiles_with_salesql(
                        st.session_state['results_df'],
                        salesql_key,
                        progress_callback=update_progress,
                        limit=enrich_count
                    )
                    # Save emails to database for persistence (batch update - single DB call)
                    if HAS_DATABASE:
                        db_client = _get_db_client()
                        if db_client:
                            # Build batch of emails to save
                            emails_to_save = []
                            for _, row in enriched_df.iterrows():
                                email = row.get('salesql_email')
                                url = row.get('linkedin_url')
                                if email and url and str(email).strip():
                                    emails_to_save.append({'linkedin_url': url, 'email': email})
                            if emails_to_save:
                                result = update_profile_emails_batch(db_client, emails_to_save, source='salesql')
                                if result['saved'] > 0:
                                    st.caption(f"💾 Saved {result['saved']} emails to database")
                    st.session_state['results_df'] = enriched_df
                    save_session_state()  # Persist emails to session file
                    new_emails = enriched_df['salesql_email'].notna().sum() if 'salesql_email' in enriched_df.columns else 0
                    st.success(f"Done! {new_emails} profiles now have emails.")
                    st.rerun()
            else:
                st.success("All profiles already have emails!")
        else:
            st.info("Load profiles first to enrich with emails")
    else:
        st.caption("SalesQL not configured. Add 'salesql_api_key' to secrets.")

    # Next button
    st.divider()
    st.info("**Next step:** Proceed to **3. AI Screen** to screen candidates with AI.")

# ========== TAB 5: AI Screening ==========
with tab_screening:
    openai_key = load_openai_key()
    anthropic_key = load_anthropic_key()

    # Resolve profiles: filtered > enriched > raw search results
    enriched_df = st.session_state.get('enriched_df')
    results_df = st.session_state.get('results_df')
    profiles_df = None

    if not openai_key and not anthropic_key:
        st.warning("No AI API key configured. Add 'openai_api_key' or 'anthropic_api_key' to config.json")
    else:
        # Use passed_candidates_df if available (filtered), otherwise enriched, otherwise raw results
        if 'passed_candidates_df' in st.session_state and not st.session_state['passed_candidates_df'].empty:
            profiles_df = st.session_state['passed_candidates_df']
            st.success(f"**{len(profiles_df)}** filtered candidates ready for screening")
        elif enriched_df is not None and not enriched_df.empty:
            profiles_df = enriched_df
            st.info(f"**{len(profiles_df)}** profiles ready for screening")
        elif results_df is not None and not results_df.empty:
            profiles_df = results_df
            st.info(f"**{len(profiles_df)}** profiles ready for screening")
        else:
            st.warning("No profiles loaded. Run a search or upload candidates first.")

    if profiles_df is not None:

        num_profiles = len(profiles_df)

        # ===== STEP 1: Structured screening criteria =====
        # Four labelled fields instead of one freeform box. The recruiter fills
        # the boxes directly — no prompt-engineering skill needed, and the AI
        # never has to guess what's a must-have vs a nice-to-have vs an
        # exclusion. The screening_brief dict flows through screen_profile /
        # screen_profiles_batch to the structured per-criterion path.
        st.markdown("### 1. Screening criteria")
        st.caption("Fill the boxes the way you'd brief a colleague. Must-haves are dealbreakers (all required); nice-to-haves appear as informational bonus tags (they cannot cause a rejection); exclusions are instant disqualifiers. One item per line.")

        role_context = st.text_input(
            "Role & context",
            key="screen_role_context",
            placeholder="e.g. Senior Backend Engineer for an early-stage fintech startup in Tel Aviv",
        )
        _col_mh, _col_nh = st.columns(2)
        with _col_mh:
            must_haves_text = st.text_area(
                "Must-haves — all required (one per line)",
                height=160,
                key="screen_must_haves",
                placeholder="5+ years of backend engineering experience\nStrong Python or Node.js\nWorked at a startup or product company",
            )
        with _col_nh:
            nice_to_haves_text = st.text_area(
                "Nice-to-haves — bonus only (one per line)",
                height=160,
                key="screen_nice_to_haves",
                placeholder="Microservices / REST API experience\nDocker and Kubernetes\nFintech or payments background",
            )
        exclusions_text = st.text_area(
            "Exclusions / deal-breakers (one per line)",
            height=90,
            key="screen_exclusions",
            placeholder="Pure team leads / managers, not hands-on recently\nCurrently at a large enterprise with no startup background",
        )

        def _brief_lines(txt):
            return [ln.strip() for ln in (txt or "").splitlines() if ln.strip()]

        screening_brief = {
            "role_context": (role_context or "").strip(),
            "must_haves": _brief_lines(must_haves_text),
            "nice_to_haves": _brief_lines(nice_to_haves_text),
            "exclusions": _brief_lines(exclusions_text),
        }
        # Freeform rendering kept for display, jd hashing, and legacy callers.
        job_description = "\n".join(p for p in [
            f"Role: {screening_brief['role_context']}" if screening_brief['role_context'] else "",
            ("Must-haves:\n" + "\n".join(f"- {m}" for m in screening_brief['must_haves'])) if screening_brief['must_haves'] else "",
            ("Nice-to-haves:\n" + "\n".join(f"- {n}" for n in screening_brief['nice_to_haves'])) if screening_brief['nice_to_haves'] else "",
            ("Exclusions:\n" + "\n".join(f"- {e}" for e in screening_brief['exclusions'])) if screening_brief['exclusions'] else "",
        ] if p)
        # Ready to screen once there's a role or at least one must-have.
        has_screening_input = bool(screening_brief['role_context'] or screening_brief['must_haves'])

        # Screening Configuration
        screen_count = st.number_input(
            "Number of profiles to screen",
            min_value=1,
            max_value=num_profiles,
            value=min(100, num_profiles),
            step=10,
            key="screen_count"
        )

        # Fixed settings - gpt-4.1-mini detailed mode (stronger instruction-following)
        screening_mode = "Detailed"
        ai_model = "gpt-4.1-mini"
        ai_provider = "openai"
        model_input_cost = 0.40   # $0.40/1M input tokens
        model_output_cost = 1.60  # $1.60/1M output tokens
        output_tokens = 150

        est_cost = (screen_count * 2500 * model_input_cost / 1_000_000) + (screen_count * output_tokens * model_output_cost / 1_000_000)
        st.info(f"Rubric: **Unified policy** | Model: **gpt-4.1-mini** | Est. cost: **${est_cost:.3f}**")

        # Debug: Show available fields and test single profile (admin-only)
        if is_admin_user():
            with st.expander("Debug: Profile Fields & Test"):
                if not profiles_df.empty:
                    st.write("Available fields:", list(profiles_df.columns))
                    sample = {k: str(v)[:100] for k, v in profiles_df.iloc[0].to_dict().items()}
                    st.write("Sample profile:", sample)

                    if has_screening_input and st.button("Test Single Profile", key="test_single"):
                        try:
                            if ai_provider == "anthropic":
                                client = anthropic.Anthropic(api_key=anthropic_key)
                            else:
                                client = OpenAI(api_key=openai_key)
                            test_mode = 'detailed'
                            st.write(f"Testing with first profile ({test_mode} mode, structured per-criterion path, model: {ai_model})...")
                            test_profile = profiles_df.iloc[0].to_dict()
                            # Clean NaN
                            import math
                            for k, v in test_profile.items():
                                if isinstance(v, float) and math.isnan(v):
                                    test_profile[k] = ''
                            result = screen_profile(test_profile, job_description, client, mode=test_mode, ai_model=ai_model, ai_provider=ai_provider, screening_brief=screening_brief)
                            st.write("Result:", result)
                        except Exception as e:
                            import traceback
                            st.error(f"Error: {e}")
                            st.code(traceback.format_exc())

        # Screen Button
        if has_screening_input:
            # Initialize cancel flag in session state
            if 'screening_cancel_flag' not in st.session_state:
                st.session_state['screening_cancel_flag'] = {'cancelled': False}

            # Check if screening is in progress (batch mode)
            screening_in_progress = st.session_state.get('screening_batch_mode', False)

            if screening_in_progress:
                # Show cancel button and progress
                batch_progress = st.session_state.get('screening_batch_progress', {})
                completed = batch_progress.get('completed', 0)
                total = batch_progress.get('total', 0)
                pct = (completed / total * 100) if total > 0 else 0

                # Progress bar (clamp value between 0 and 1)
                progress_val = min(1.0, max(0.0, completed / total)) if total > 0 else 0.0
                st.progress(progress_val, text=f"Screening: {completed}/{total} ({pct:.0f}%)")

                # Live stats from completed results
                all_results = st.session_state.get('screening_batch_state', {}).get('results', [])
                if all_results:
                    strong = sum(1 for r in all_results if r.get('fit') == 'Strong Fit')
                    good = sum(1 for r in all_results if r.get('fit') == 'Good Fit')
                    partial = sum(1 for r in all_results if r.get('fit') == 'Partial Fit')
                    not_fit = sum(1 for r in all_results if r.get('fit') == 'Not a Fit')
                    st.markdown(f"🟢 Strong: **{strong}** | 🔵 Good: **{good}** | 🟡 Partial: **{partial}** | ⚪ Not Fit: **{not_fit}**")

                    # Show top candidates so far
                    with st.expander("Top candidates so far", expanded=True):
                        top5 = sorted(all_results, key=lambda x: x.get('score', 0), reverse=True)[:5]
                        for r in top5:
                            score = r.get('score', 0)
                            fit = r.get('fit', '')
                            name = r.get('name', 'Unknown')
                            title = r.get('current_title', '')[:40]
                            emoji = '🟢' if fit == 'Strong Fit' else '🔵' if fit == 'Good Fit' else '🟡' if fit == 'Partial Fit' else '⚪'
                            st.markdown(f"{emoji} **{score}/10** - {name} | {title}")

                col1, col2 = st.columns([3, 1])
                with col1:
                    current_batch = st.session_state.get('screening_batch_state', {}).get('current_batch', 0) + 1
                    st.caption(f"Processing batch {current_batch}... Click Cancel to stop and keep completed results.")
                with col2:
                    if st.button("⏹ Cancel", type="secondary", key="cancel_screening"):
                        # Keep partial results in session (not saved to DB — screening is always fresh per JD)
                        partial_results = st.session_state.get('screening_batch_state', {}).get('results', [])
                        if partial_results:
                            st.session_state['screening_results'] = partial_results
                        st.session_state['screening_cancelled'] = True
                        st.session_state['screening_batch_mode'] = False
                        if st.session_state.get('_screening_active'):
                            _screening_session_end()
                            st.session_state['_screening_active'] = False

                        # MEMORY CLEANUP: Clear heavy intermediate state on cancellation
                        # Same cleanup as completion to prevent memory bloat
                        if 'screening_batch_state' in st.session_state:
                            del st.session_state['screening_batch_state']
                        if 'enriched_profiles_raw' in st.session_state:
                            del st.session_state['enriched_profiles_raw']
                        import gc
                        gc.collect()

                        save_session_state()  # Save partial results for restore
                        st.warning(f"Screening cancelled! {len(partial_results)} profiles were completed and saved.")
                        st.rerun()

            # Show existing results and options
            existing_results = st.session_state.get('screening_results', [])
            start_button = False
            continue_button = False
            rescreen_selected_button = False

            if existing_results and not screening_in_progress:
                # Find profiles not yet screened (lightweight — just compare URLs)
                screened_urls = {r.get('linkedin_url') for r in existing_results if r.get('linkedin_url')}
                all_urls = set(profiles_df['linkedin_url'].dropna()) if 'linkedin_url' in profiles_df.columns else set()
                unscreened_count = len(all_urls - screened_urls)

                st.info(f"📊 **{len(existing_results)}** screened | **{unscreened_count}** remaining | **{num_profiles}** total")

                col1, col2, col3 = st.columns([2, 2, 1])
                with col1:
                    start_button = st.button("🔄 Screen All Fresh", type="secondary", key="start_screening")
                with col2:
                    if unscreened_count > 0:
                        continue_count = min(unscreened_count, screen_count)
                        continue_button = st.button(f"▶️ Continue ({continue_count} of {unscreened_count} left)", type="primary", key="continue_screening")
                    else:
                        st.success("✅ All profiles screened!")
                with col3:
                    if st.button("🗑️ Clear", key="clear_screening_results"):
                        st.session_state['screening_results'] = []
                        st.session_state['screening_batch_state'] = {}
                        st.session_state['screening_batch_progress'] = {}
                        save_session_state()  # Persist cleared state to file
                        st.success("Results cleared!")
                        st.rerun()

                # Re-screen options
                with st.expander("🔄 Re-screen options"):
                    st.markdown("**Re-screen by count:**")
                    rescreen_col1, rescreen_col2 = st.columns([1, 2])
                    with rescreen_col1:
                        rescreen_count = st.number_input(
                            "Number to re-screen",
                            min_value=1,
                            max_value=len(existing_results),
                            value=min(10, len(existing_results)),
                            key="rescreen_count"
                        )
                    with rescreen_col2:
                        rescreen_order = st.radio(
                            "Order",
                            options=["First N", "Lowest scores", "Highest scores"],
                            horizontal=True,
                            key="rescreen_order"
                        )

                    # Determine which profiles to re-screen based on order
                    if rescreen_order == "Lowest scores":
                        sorted_results = sorted(existing_results, key=lambda x: x.get('score', 0))
                    elif rescreen_order == "Highest scores":
                        sorted_results = sorted(existing_results, key=lambda x: x.get('score', 0), reverse=True)
                    else:
                        sorted_results = existing_results

                    rescreen_n_urls = [r.get('linkedin_url', '') for r in sorted_results[:rescreen_count] if r.get('linkedin_url')]

                    if st.button(f"🔄 Re-screen {rescreen_count} profiles ({rescreen_order.lower()})", key="rescreen_n_btn", type="primary"):
                        st.session_state['rescreen_selected_urls'] = rescreen_n_urls
                        rescreen_selected_button = True

                    st.divider()
                    st.markdown("**Re-screen by selection:**")
                    profile_options = {
                        f"{r.get('name', 'Unknown')} — {r.get('current_title', '')[:30]} ({r.get('fit', '?')}, {r.get('score', 0)}/10)": r.get('linkedin_url', '')
                        for r in existing_results if r.get('linkedin_url')
                    }
                    selected_labels = st.multiselect(
                        "Select specific profiles",
                        options=list(profile_options.keys()),
                        key="rescreen_profile_select"
                    )
                    selected_urls = [profile_options[label] for label in selected_labels]
                    if selected_urls:
                        if st.button(f"🔄 Re-screen {len(selected_urls)} selected", key="rescreen_selected_btn"):
                            st.session_state['rescreen_selected_urls'] = selected_urls
                            rescreen_selected_button = True
            else:
                # Always screen fresh — no loading previous results from DB
                start_disabled = screening_in_progress
                start_button = st.button("Start Screening", type="primary", key="start_screening", disabled=start_disabled)

            # Continue batch processing if in progress
            if screening_in_progress and not st.session_state.get('screening_cancelled', False):
                # Get batch state
                batch_state = st.session_state.get('screening_batch_state', {})
                profiles_to_screen = batch_state.get('profiles', [])
                job_desc = batch_state.get('job_description', '')
                screen_mode = batch_state.get('mode', 'detailed')
                batch_ai_model = batch_state.get('ai_model', 'gpt-4.1-mini')
                batch_ai_provider = batch_state.get('ai_provider', 'openai')
                batch_api_key = batch_state.get('api_key', openai_key)
                max_workers = batch_state.get('max_workers', 10)  # Reduced from 15 to avoid rate limits
                # Ensure Anthropic uses reduced concurrency even when resuming
                if batch_ai_provider == "anthropic":
                    max_workers = min(max_workers, 5)
                elif batch_ai_provider == "openai":
                    max_workers = min(max_workers, 10)  # Cap OpenAI at 10 concurrent
                batch_user_request = batch_state.get('user_request')
                batch_screening_brief = batch_state.get('screening_brief')
                batch_size = 50  # Tier 3: 50 parallel requests, 50 × 15KB = ~750KB memory
                current_batch = batch_state.get('current_batch', 0)
                all_results = batch_state.get('results', [])

                # Build raw_data index once (first batch only), reuse for all batches
                # Performance: Cache raw_index across screening sessions if enriched_df unchanged
                raw_index = batch_state.get('raw_index')
                if raw_index is None:
                    # Try enriched_df first (may have raw_data if fresh from API enrichment)
                    enriched_df_src = st.session_state.get('enriched_df')
                    if enriched_df_src is not None and not enriched_df_src.empty and 'raw_data' in enriched_df_src.columns:
                        # Check if we can reuse cached raw_index (hash validation)
                        enriched_urls = tuple(enriched_df_src.get('linkedin_url', []))
                        enriched_hash = hash(enriched_urls)
                        cached_hash = st.session_state.get('_raw_index_hash')
                        if cached_hash == enriched_hash and '_raw_index_cache' in st.session_state:
                            raw_index = st.session_state['_raw_index_cache']
                        else:
                            raw_index = build_raw_data_index(enriched_df_src.to_dict('records'))
                            st.session_state['_raw_index_cache'] = raw_index
                            st.session_state['_raw_index_hash'] = enriched_hash
                    else:
                        raw_index = {}  # DB fetch in fetch_raw_data_for_batch() will handle it
                    st.session_state['screening_batch_state']['raw_index'] = raw_index

                start_idx = current_batch * batch_size
                end_idx = min(start_idx + batch_size, len(profiles_to_screen))
                batch_profiles = profiles_to_screen[start_idx:end_idx]

                if batch_profiles:
                    with st.status(f"Processing batch {current_batch + 1} ({screen_mode} mode)...", expanded=True) as status:
                        db_client = _get_db_client() if HAS_DATABASE else None

                        # Only fetch raw_data from DB if profiles don't already have it
                        missing_before = sum(1 for p in batch_profiles if not p.get('raw_crustdata') and not p.get('raw_data'))
                        if missing_before > 0:
                            fetch_raw_data_for_batch(
                                batch_profiles,
                                raw_index=raw_index,
                                db_client=db_client
                            )
                            missing_after = sum(1 for p in batch_profiles if not p.get('raw_crustdata') and not p.get('raw_data'))
                            fetched = missing_before - missing_after
                            st.write(f"📦 Fetched raw data for {fetched}/{missing_before} profiles from DB" + (f" ({missing_after} still missing)" if missing_after > 0 else ""))

                        # Top up any profile still missing skills/summary (e.g. sourced from
                        # the description-search beta, which doesn't return them) via the new
                        # batch-enrich endpoint — unconditional, not gated behind
                        # missing_before, since a profile can already be thin in memory with
                        # nothing upstream flagging it. See enrich_thin_profiles_for_batch()
                        # docstring for why this must not be nested inside the block above.
                        if has_crust_key:
                            _enrich_stats = enrich_thin_profiles_for_batch(
                                batch_profiles,
                                api_key=api_key,
                                db_client=db_client,
                                tracker=get_usage_tracker(),
                            )
                            if _enrich_stats['thin_found'] > 0:
                                st.write(
                                    f"🔎 Enriching {_enrich_stats['thin_found']} thin profiles… "
                                    f"{_enrich_stats['enriched']} filled, "
                                    f"{_enrich_stats['unmatched']} not found (screened as-is)"
                                )
                            if _enrich_stats.get('on_cooldown'):
                                # Surfaced, not hidden (Codex review, PR #127, round 3):
                                # these were validated-and-saved recently and still
                                # came back without skills/summary, so they're
                                # skipped for now rather than re-bought every run.
                                # The actual URLs are in on_cooldown_urls for anyone
                                # who wants to force a retry.
                                st.caption(
                                    f"⏳ {_enrich_stats['on_cooldown']} profile(s) skipped — "
                                    f"already enriched within the last "
                                    f"{THIN_PROFILE_REENRICH_COOLDOWN_HOURS}h and still came back "
                                    f"without skills/summary. Screened with existing data."
                                )

                        # Thread-safe progress tracking for this batch
                        batch_progress = {'completed': 0, 'strong': 0, 'good': 0, 'partial': 0, 'error': 0}
                        batch_lock = threading.Lock()

                        def _on_profile_screened(completed, total, result):
                            """Update batch progress (thread-safe)."""
                            with batch_lock:
                                batch_progress['completed'] += 1
                                fit = result.get('fit', '') if isinstance(result, dict) else ''
                                if 'Strong' in fit:
                                    batch_progress['strong'] += 1
                                elif 'Good' in fit:
                                    batch_progress['good'] += 1
                                elif fit in ('Error', 'Skipped', ''):
                                    batch_progress['error'] += 1
                                else:
                                    batch_progress['partial'] += 1

                        # Screen this batch - use dynamic workers (scales with concurrent users).
                        # When a structured brief is present every profile goes
                        # through the structured per-criterion path; otherwise the
                        # legacy freeform ``user_request`` path is used.
                        batch_results = screen_profiles_batch(
                            batch_profiles,
                            job_desc,
                            openai_key,
                            max_workers=min(max_workers, len(batch_profiles)),
                            mode=screen_mode,
                            ai_model=batch_ai_model,
                            progress_callback=_on_profile_screened,
                            ai_provider=batch_ai_provider,
                            api_key=batch_api_key,
                            user_request=batch_user_request,
                            screening_brief=batch_screening_brief
                        )
                        all_results.extend(batch_results)

                        # Show batch summary with progress stats
                        status.update(
                            label=f"Batch {current_batch + 1}: {batch_progress['completed']} screened | Strong: {batch_progress['strong']} | Good: {batch_progress['good']} | Partial: {batch_progress['partial']}",
                            state="complete"
                        )

                        # Memory cleanup: clear raw_crustdata from screened profiles
                        for p in batch_profiles:
                            if isinstance(p, dict):
                                p.pop('raw_crustdata', None)
                                p.pop('raw_data', None)
                        # Force garbage collection after each batch
                        import gc
                        gc.collect()

                        # Update progress
                        st.session_state['screening_batch_state']['results'] = all_results
                        st.session_state['screening_batch_state']['current_batch'] = current_batch + 1
                        st.session_state['screening_batch_progress'] = {
                            'completed': len(all_results),
                            'total': len(profiles_to_screen)
                        }

                        # Persist to session state after each batch (in-memory only, save at end)
                        st.session_state['screening_results'] = all_results

                        # Show batch stats
                        strong = sum(1 for r in batch_results if r.get('fit') == 'Strong Fit')
                        good = sum(1 for r in batch_results if r.get('fit') == 'Good Fit')
                        errors_in_batch = sum(1 for r in batch_results if r.get('fit') in ('Error', 'Skipped'))
                        status.update(label=f"Batch {current_batch + 1}: +{strong} strong, +{good} good", state="complete")

                    # Abort early if entire batch failed (API key issue, quota, etc.)
                    if errors_in_batch == len(batch_results) and current_batch == 0:
                        st.session_state['screening_batch_mode'] = False
                        st.session_state['screening_results'] = all_results
                        if st.session_state.get('_screening_active'):
                            _screening_session_end()
                            st.session_state['_screening_active'] = False
                        error_sample = batch_results[0].get('summary', '') if batch_results else 'Unknown'
                        st.error(f"All profiles in first batch failed. Stopping to save credits.\n\nError: {error_sample}")
                        st.rerun()

                    # Check if more batches
                    if end_idx < len(profiles_to_screen):
                        time.sleep(0.5)  # Brief pause between batches
                        st.rerun()  # Continue with next batch
                    else:
                        # All done - finalize
                        st.session_state['screening_batch_mode'] = False
                        st.session_state['screening_results'] = all_results

                        # Screening results are kept in session only — not saved to DB
                        # Each JD requires fresh screening, so cached DB scores are not useful

                        if st.session_state.get('_screening_active'):
                            _screening_session_end()
                            st.session_state['_screening_active'] = False

                        # Auto-cleanup: strip raw_crustdata and filtered_out to free memory
                        for _df_key in ['results_df', 'enriched_df']:
                            if _df_key in st.session_state and isinstance(st.session_state[_df_key], pd.DataFrame):
                                if 'raw_crustdata' in st.session_state[_df_key].columns:
                                    st.session_state[_df_key] = st.session_state[_df_key].drop(columns=['raw_crustdata'])
                        # MEMORY: list duplicates removed, no need to clean them
                        # Clear filtered_out (stores full DataFrame copies)
                        if 'filtered_out' in st.session_state:
                            del st.session_state['filtered_out']
                        # Clear screening_batch_state (stores profiles copy during screening)
                        if 'screening_batch_state' in st.session_state:
                            del st.session_state['screening_batch_state']
                        # Clear orphaned raw data caches
                        st.session_state.pop('enriched_profiles_raw', None)
                        # Keep original_results_df — needed for Reset Filters
                        # Clear debug data
                        for _debug_key in ['_enrich_debug', '_enrich_match_debug', '_debug_url_cols', '_debug_all_cols']:
                            if _debug_key in st.session_state:
                                del st.session_state[_debug_key]

                        st.success(f"✅ Screening complete! {len(all_results)} profiles screened fresh")
                        send_notification("Screening Complete", f"Screened {len(all_results)} profiles")
                        save_session_state()  # Save for restore
                        st.rerun()

            if start_button or rescreen_selected_button or continue_button:
                # === Prepare profiles (only when screening starts, not on every rerun) ===
                profiles = profiles_df.to_dict('records')
                import math
                for p in profiles:
                    for k, v in p.items():
                        if isinstance(v, float) and math.isnan(v):
                            p[k] = ''
                # Restore raw_data from cache
                raw_cache = st.session_state.get('enriched_profiles_raw', {})
                if raw_cache:
                    for p in profiles:
                        url = p.get('linkedin_url', '')
                        if url and not p.get('raw_data') and not p.get('raw_crustdata') and url in raw_cache:
                            cached = raw_cache[url]
                            if cached.get('raw_data'):
                                p['raw_crustdata'] = cached['raw_data']
                # Strip raw data for large batches to save memory
                has_raw = sum(1 for p in profiles if p.get('raw_data') or p.get('raw_crustdata'))
                if len(profiles) > 500 and has_raw > 0:
                    for p in profiles:
                        p.pop('raw_data', None)
                        p.pop('raw_crustdata', None)
                # Dynamic concurrent workers
                max_workers = _screening_session_start()
                st.session_state['_screening_active'] = True

                # Reduce concurrency for Anthropic (stricter rate limits than OpenAI)
                if ai_provider == "anthropic":
                    max_workers = min(max_workers, 5)  # Cap at 5 for Anthropic

                # Validate API key before starting
                if ai_provider == "anthropic":
                    if not anthropic_key:
                        st.error("Anthropic API key not configured. Add 'anthropic_api_key' to config.json")
                        st.stop()
                    try:
                        test_client = anthropic.Anthropic(api_key=anthropic_key)
                        test_client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=10, messages=[{"role": "user", "content": "hi"}])
                    except Exception as e:
                        error_msg = str(e)
                        if '401' in error_msg or 'authentication' in error_msg.lower():
                            st.error(f"Invalid Anthropic API key. Please check your key in config.json.\n\nError: {error_msg[:200]}")
                        elif '429' in error_msg:
                            st.error(f"Anthropic rate limit exceeded. Check your billing at console.anthropic.com.\n\nError: {error_msg[:200]}")
                        else:
                            st.error(f"Anthropic connection failed: {error_msg[:200]}")
                        st.stop()
                else:
                    try:
                        test_client = OpenAI(api_key=openai_key)
                        test_client.models.list()
                    except Exception as e:
                        error_msg = str(e)
                        if '401' in error_msg or 'Incorrect API key' in error_msg:
                            st.error(f"Invalid OpenAI API key. Please check your key in config.json or Streamlit secrets.\n\nError: {error_msg[:200]}")
                        elif '429' in error_msg:
                            st.error(f"OpenAI rate limit or quota exceeded. Check your billing at platform.openai.com.\n\nError: {error_msg[:200]}")
                        else:
                            st.error(f"OpenAI connection failed: {error_msg[:200]}")
                        st.stop()

                # Determine which profiles to screen
                if rescreen_selected_button:
                    # Re-screen specific profiles: screen only selected, keep the rest
                    rescreen_urls = set(st.session_state.get('rescreen_selected_urls', []))
                    existing_results = st.session_state.get('screening_results', [])
                    initial_results = [r for r in existing_results if r.get('linkedin_url', '') not in rescreen_urls]
                    profiles_to_screen = [p for p in profiles if p.get('linkedin_url', '') in rescreen_urls]

                    # Reload raw_crustdata from DB if missing (after memory cleanup)
                    if HAS_DATABASE and profiles_to_screen:
                        db_client = _get_db_client()
                        if db_client:
                            for p in profiles_to_screen:
                                if not p.get('raw_crustdata') and not p.get('raw_data'):
                                    url = normalize_linkedin_url(p.get('linkedin_url', ''))
                                    if url:
                                        try:
                                            db_profile = get_profile(db_client, url)
                                            if db_profile and db_profile.get('raw_data'):
                                                p['raw_crustdata'] = db_profile['raw_data']
                                        except:
                                            pass
                elif continue_button:
                    # Continue screening: keep existing results, screen only unscreened profiles (up to screen_count)
                    existing_results = st.session_state.get('screening_results', [])
                    screened_urls = {r.get('linkedin_url') for r in existing_results if r.get('linkedin_url')}
                    initial_results = existing_results  # Keep all existing results
                    unscreened = [p for p in profiles if p.get('linkedin_url') not in screened_urls]
                    profiles_to_screen = unscreened[:screen_count]  # Respect the screen_count limit
                else:
                    # Always screen fresh — each JD gets a fresh evaluation
                    profiles_to_screen = profiles[:screen_count]
                    initial_results = []

                # Pre-screening memory cleanup
                import gc
                for _cleanup_key in ['filtered_out', '_enrich_debug', '_enrich_match_debug']:
                    if _cleanup_key in st.session_state:
                        del st.session_state[_cleanup_key]
                gc.collect()

                st.session_state['screening_batch_mode'] = True
                st.session_state['screening_cancelled'] = False
                st.session_state['screening_batch_state'] = {
                    'profiles': profiles_to_screen,
                    'job_description': job_description,
                    'mode': 'detailed',
                    'ai_model': ai_model,
                    'ai_provider': ai_provider,
                    'api_key': anthropic_key if ai_provider == "anthropic" else openai_key,
                    'user_request': job_description,  # Legacy freeform fallback
                    'screening_brief': screening_brief,  # Structured per-criterion path
                    'current_batch': 0,
                    'results': initial_results,  # Start with existing results if re-screening selected
                    'is_continue': False,  # Always fresh screening
                    'max_workers': max_workers,
                }
                st.session_state['screening_batch_progress'] = {
                    'completed': len(initial_results),
                    'total': len(initial_results) + len(profiles_to_screen)
                }

                action = "Re-screening selected" if rescreen_selected_button else ("Continuing screening" if continue_button else "Starting fresh screening")
                st.info(f"{action}: {len(profiles_to_screen)} profiles in batches...")
                st.rerun()
        else:
            st.warning("Please paste AI screening requirements to start screening")

        # Show screening results
        if 'screening_results' in st.session_state and st.session_state['screening_results']:
            st.divider()
            st.markdown("### Screening Results")

            screening_results = st.session_state['screening_results']

            # Summary stats (unified policy: Good Fit = GO, Maybe = borderline NO GO, Not a Fit = NO GO)
            stats_col1, stats_col2, stats_col3 = st.columns(3)
            good_fit = sum(1 for r in screening_results if r.get('fit') == 'Good Fit')
            maybe_fit = sum(1 for r in screening_results if r.get('fit') == 'Maybe')
            not_fit = sum(1 for r in screening_results if r.get('fit') == 'Not a Fit')

            stats_col1.metric("GO", good_fit, delta=None)
            stats_col2.metric("MAYBE", maybe_fit, delta=None)
            stats_col3.metric("NO GO", not_fit, delta=None)

            # Filter by fit level
            fit_filter = st.multiselect(
                "Filter by fit level",
                options=["Good Fit", "Maybe", "Not a Fit", "Error"],
                default=["Good Fit", "Maybe"],
                key="fit_filter"
            )

            # Filter and sort results — GO first, then by score desc.
            # Unified-policy results have a 'decision' field ("GO"/"NO GO");
            # legacy role-prompt results don't, so fall back to score.
            filtered_results = [r for r in screening_results if r.get('fit') in fit_filter]
            def _sort_key(r):
                # Bucket order: GO → Maybe → NO GO; then score desc within bucket
                fit = r.get('fit', '')
                if fit == 'Good Fit':
                    bucket = 0
                elif fit == 'Maybe':
                    bucket = 1
                else:
                    bucket = 2
                return (bucket, -int(r.get('score', 0) or 0))
            sorted_results = sorted(filtered_results, key=_sort_key)

            # Check for profiles with missing data
            missing_data_profiles = [r for r in screening_results if r.get('fit') == 'Missing Data' or r.get('missing_data')]
            if missing_data_profiles:
                with st.expander(f"⚠️ {len(missing_data_profiles)} profiles skipped - Missing Crustdata", expanded=False):
                    st.warning("These profiles have no work history from Crustdata. Re-enrich them to get complete data.")
                    for p in missing_data_profiles[:20]:  # Show max 20
                        name = p.get('name', 'Unknown')
                        url = p.get('linkedin_url', '')
                        st.markdown(f"- **{name}** - [Re-enrich]({url})")
                    if len(missing_data_profiles) > 20:
                        st.caption(f"... and {len(missing_data_profiles) - 20} more")

            col_info, col_toggle = st.columns([3, 1])
            with col_info:
                st.info(f"Showing **{len(sorted_results)}** of {len(screening_results)} screened profiles")
            with col_toggle:
                show_all_cols = st.checkbox("Show all columns", value=False, key="screening_show_all_cols")

            # Create display dataframe
            if show_all_cols:
                # Show all screening + enriched data merged
                df_display = pd.DataFrame(sorted_results)

                # Merge with enriched profile data if available
                # EXCLUDE old screening columns from merge to avoid confusion with fresh screening
                enriched_df_merge = st.session_state.get('enriched_df')
                if enriched_df_merge is not None and not enriched_df_merge.empty and 'linkedin_url' in df_display.columns:
                    # Columns that should NOT be merged from old data (screening is always fresh per JD)
                    exclude_from_merge = {
                        # Session screening columns
                        'score', 'fit', 'summary',
                        # Database screening columns
                        'screening_score', 'screening_fit_level', 'screening_summary',
                        'screening_reasoning', 'screened_at',
                        # Any other screening-related
                        'screening_fit', 'screening_status', 'fit_level'
                    }
                    merge_cols = [c for c in enriched_df_merge.columns
                                  if c not in df_display.columns and c not in exclude_from_merge]
                    if merge_cols and 'linkedin_url' in enriched_df_merge.columns:
                        df_display = df_display.merge(
                            enriched_df_merge[['linkedin_url'] + merge_cols],
                            on='linkedin_url', how='left'
                        )

                # Reorder columns to put important ones first (linkedin_url next to name for easy click)
                priority_cols = ['score', 'fit', 'name', 'linkedin_url', 'current_title', 'current_company', 'summary', 'skills', 'all_employers', 'all_titles', 'all_schools', 'location']
                ordered_cols = [c for c in priority_cols if c in df_display.columns]
                other_cols = [c for c in df_display.columns if c not in priority_cols and c != 'index']
                df_display = df_display[ordered_cols + other_cols]

                st.dataframe(
                    blank_tenure_sentinel(df_display),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "score": st.column_config.NumberColumn("Score", format="%d/10"),
                        "decision": st.column_config.TextColumn("Decision", width="small", help="GO / NO GO (unified policy only)"),
                        "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                    }
                )
                st.caption(f"Showing {len(df_display.columns)} columns")
            else:
                # Show summary view
                def _format_criteria_summary(r):
                    """Compact per-criterion breakdown for the results table.
                    Structured-path results carry must_have_verdicts /
                    exclusion_verdicts; legacy results don't (return '')."""
                    mh = r.get('must_have_verdicts') or []
                    ex = r.get('exclusion_verdicts') or []
                    if not mh and not ex:
                        return ''
                    parts = []
                    if mh:
                        met = sum(1 for m in mh if m.get('met') is True)
                        parts.append(f"Must-haves {met}/{len(mh)}")
                        missing = [str(m.get('text', ''))[:32]
                                   for m in mh if m.get('met') is not True]
                        if missing:
                            parts.append("missing: " + "; ".join(missing))
                    matched = [str(e.get('text', ''))[:32]
                               for e in ex if e.get('matched') is True]
                    if matched:
                        parts.append("EXCLUDED: " + "; ".join(matched))
                    elif ex:
                        parts.append("exclusions clear")
                    return " · ".join(parts)

                # One recruiter-facing verdict column (GO / MAYBE / NO GO),
                # not the old redundant Decision + Fit pair.
                _verdict_label = {"Good Fit": "GO", "Maybe": "MAYBE", "Not a Fit": "NO GO"}
                display_data = []
                for r in sorted_results:
                    fit_val = r.get('fit', '') or ''
                    display_data.append({
                        'Verdict': _verdict_label.get(fit_val, fit_val),  # Error/Skipped pass through
                        'Score': r.get('score', 0),
                        'Name': r.get('name', '') or '',
                        'LinkedIn': r.get('linkedin_url', ''),
                        'Title': str(r.get('current_title', '') or '')[:40],
                        'Company': str(r.get('current_company', '') or '')[:30],
                        'Why': _format_criteria_summary(r),
                        'Bonus': ", ".join(r.get('bonus_tags') or []),
                        'Summary': str(r.get('summary', '') or '')[:100]
                    })

                df_display = pd.DataFrame(display_data)

                st.dataframe(
                    df_display,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Verdict": st.column_config.TextColumn("Verdict", width="small", help="GO = reach out · MAYBE = cleared every must-have but the AI scored it 5-6, worth a glance · NO GO = failed a must-have or hit an exclusion"),
                        "Score": st.column_config.NumberColumn("Score", format="%d/10", width="small"),
                        "Name": st.column_config.TextColumn("Name", width="medium"),
                        "LinkedIn": st.column_config.LinkColumn("🔗", width="small", display_text="Open"),
                        "Title": st.column_config.TextColumn("Title", width="medium"),
                        "Company": st.column_config.TextColumn("Company", width="small"),
                        "Why": st.column_config.TextColumn("Why", width="large", help="Per-criterion verdict: how many must-haves were met, which were missing, and whether any exclusion matched"),
                        "Bonus": st.column_config.TextColumn("Bonus", width="medium", help="Nice-to-haves the candidate has (informational only — never affects the verdict)"),
                        "Summary": st.column_config.TextColumn("Summary", width="large")
                    }
                )

            # ===== SalesQL Email Enrichment =====
            st.markdown("### Email Enrichment (SalesQL)")
            salesql_key = load_salesql_key()
            if salesql_key:
                screening_df = pd.DataFrame(sorted_results)
                # Note: Email data is already merged from enriched_df into screening_results above

                # Choose which candidates to enrich (unified policy buckets)
                candidate_source = st.radio(
                    "Which candidates to enrich?",
                    ["GO only", "GO + Maybe", "All candidates"],
                    key="candidate_source_tab5",
                    horizontal=True
                )

                if candidate_source == "GO only":
                    enrich_df = screening_df[screening_df['fit'] == 'Good Fit'].copy() if 'fit' in screening_df.columns else screening_df.copy()
                    st.caption(f"GO candidates: {len(enrich_df)}")
                elif candidate_source == "GO + Maybe":
                    enrich_df = screening_df[screening_df['fit'].isin(['Good Fit', 'Maybe'])].copy() if 'fit' in screening_df.columns else screening_df.copy()
                    st.caption(f"GO + Maybe candidates: {len(enrich_df)}")
                else:
                    enrich_df = screening_df.copy()
                    st.caption(f"All candidates: {len(enrich_df)}")

                current_count = len(enrich_df)
                # Check both email fields (DB email and SalesQL email)
                has_salesql = (enrich_df['salesql_email'].notna() & (enrich_df['salesql_email'] != '')).sum() if 'salesql_email' in enrich_df.columns else 0
                has_db_email = (enrich_df['email'].notna() & (enrich_df['email'] != '')).sum() if 'email' in enrich_df.columns else 0
                # Count profiles with ANY email (union, not sum)
                email_mask = pd.Series([False] * len(enrich_df), index=enrich_df.index)
                if 'salesql_email' in enrich_df.columns:
                    email_mask |= (enrich_df['salesql_email'].notna() & (enrich_df['salesql_email'] != ''))
                if 'email' in enrich_df.columns:
                    email_mask |= (enrich_df['email'].notna() & (enrich_df['email'] != ''))
                already_enriched = email_mask.sum()
                not_enriched = current_count - already_enriched
                st.caption(f"{current_count} profiles | {already_enriched} already have emails | {not_enriched} remaining")

                # Ask how many to enrich
                if not_enriched > 0:
                    col_option, col_number = st.columns([1, 1])
                    with col_option:
                        enrich_option = st.radio("How many to enrich?", ["All", "Specific number"], key="enrich_option_tab5", horizontal=True)
                    with col_number:
                        if enrich_option == "Specific number":
                            enrich_count = st.number_input("Number of profiles", min_value=1, max_value=not_enriched, value=min(50, not_enriched), key="enrich_count_tab5")
                        else:
                            enrich_count = not_enriched

                    if st.button(f"Enrich {enrich_count} profiles with Emails", key="salesql_tab5", type="primary"):
                        progress_bar = st.progress(0)
                        status_text = st.empty()

                        def update_progress(current, total):
                            progress_bar.progress(current / total)
                            status_text.text(f"Enriching {current}/{total}...")

                        enriched_df = enrich_profiles_with_salesql(enrich_df, salesql_key, progress_callback=update_progress, limit=enrich_count)

                        # Save emails to database for persistence (batch update - single DB call)
                        if HAS_DATABASE:
                            db_client = _get_db_client()
                            if db_client:
                                # Build batch of emails to save
                                emails_to_save = []
                                for _, row in enriched_df.iterrows():
                                    email = row.get('salesql_email')
                                    url = row.get('linkedin_url')
                                    if email and url and str(email).strip():
                                        emails_to_save.append({'linkedin_url': url, 'email': email})
                                if emails_to_save:
                                    result = update_profile_emails_batch(db_client, emails_to_save, source='salesql')
                                    if result['saved'] > 0:
                                        st.caption(f"💾 Saved {result['saved']} emails to database")

                        # Merge enriched emails back into full screening results
                        screening_df_updated = screening_df.copy()
                        if 'salesql_email' not in screening_df_updated.columns:
                            screening_df_updated['salesql_email'] = ''
                            screening_df_updated['salesql_email_type'] = ''
                        for idx, row in enriched_df.iterrows():
                            if row.get('salesql_email') and row['salesql_email'] != '':
                                # Find matching row in original df by linkedin_url or name
                                url_col = 'linkedin_url' if 'linkedin_url' in screening_df_updated.columns else 'public_url'
                                if url_col in screening_df_updated.columns:
                                    mask = screening_df_updated[url_col] == row.get(url_col)
                                    screening_df_updated.loc[mask, 'salesql_email'] = row['salesql_email']
                                    screening_df_updated.loc[mask, 'salesql_email_type'] = row.get('salesql_email_type', '')

                        st.session_state['screening_results'] = screening_df_updated.to_dict('records')
                        save_session_state()  # Persist emails to session file
                        new_emails = (enriched_df['salesql_email'].notna() & (enriched_df['salesql_email'] != '')).sum() if 'salesql_email' in enriched_df.columns else 0
                        st.success(f"Done! Found {new_emails} emails.")
                        st.rerun()
                else:
                    st.success("All profiles already have emails!")

                # Preview enriched profiles with emails
                if 'salesql_email' in screening_df.columns:
                    enriched_preview = screening_df[screening_df['salesql_email'].notna() & (screening_df['salesql_email'] != '')].copy()
                    if len(enriched_preview) > 0:
                        st.markdown("**Enriched Profiles Preview:**")
                        preview_cols = ['name', 'fit', 'salesql_email']
                        if 'current_title' in enriched_preview.columns:
                            preview_cols.insert(1, 'current_title')
                        if 'current_company' in enriched_preview.columns:
                            preview_cols.insert(2, 'current_company')
                        available_cols = [c for c in preview_cols if c in enriched_preview.columns]
                        st.dataframe(enriched_preview[available_cols], width="stretch", hide_index=True)
            else:
                st.caption("SalesQL not configured. Add 'salesql_api_key' to secrets.")

            # Export options
            st.markdown("### Export Results")

            # Helper to prepare screening results for export — always all columns
            def prepare_screening_export(results_list):
                """Prepare screening results for CSV export with all enriched profile columns."""
                if not results_list:
                    return ""
                df = pd.DataFrame(results_list)

                # Split name into first_name/last_name if missing
                if 'first_name' not in df.columns:
                    df['first_name'] = ''
                if 'last_name' not in df.columns:
                    df['last_name'] = ''
                if 'name' in df.columns:
                    for idx, row in df.iterrows():
                        if not row.get('first_name') and row.get('name') and ' ' in str(row['name']):
                            parts = str(row['name']).split(' ', 1)
                            df.at[idx, 'first_name'] = parts[0]
                            df.at[idx, 'last_name'] = parts[1] if len(parts) > 1 else ''

                # Merge all enriched profile columns (skills, work history, location, etc.)
                exclude_from_merge = {
                    'score', 'fit', 'summary', 'screening_score', 'screening_fit_level',
                    'screening_summary', 'screening_reasoning', 'screened_at',
                    'screening_fit', 'screening_status', 'fit_level'
                }
                enriched_df_merge = st.session_state.get('enriched_df')
                if enriched_df_merge is not None and not enriched_df_merge.empty and 'linkedin_url' in df.columns and 'linkedin_url' in enriched_df_merge.columns:
                    merge_cols = [c for c in enriched_df_merge.columns
                                  if c not in df.columns and c not in exclude_from_merge
                                  and not c.startswith('_')]
                    if merge_cols:
                        df = df.merge(
                            enriched_df_merge[['linkedin_url'] + merge_cols],
                            on='linkedin_url', how='left'
                        )

                # Preferred column order: identity first, then screening verdict, then profile detail
                priority_cols = [
                    'first_name', 'last_name', 'name', 'email', 'salesql_email', 'linkedin_url',
                    'location', 'university', 'current_title', 'current_company',
                    'score', 'fit', 'summary', 'reasoning',
                    'skills', 'all_employers', 'all_titles', 'all_schools', 'headline'
                ]
                ordered = [c for c in priority_cols if c in df.columns]
                remaining = [c for c in df.columns if c not in priority_cols and c != 'index']
                return blank_tenure_sentinel(df[ordered + remaining]).to_csv(index=False)

            # Unified-policy buckets
            go_list = [r for r in screening_results if r.get('fit') == 'Good Fit']
            maybe_list = [r for r in screening_results if r.get('fit') == 'Maybe']
            no_go_list = [r for r in screening_results if r.get('fit') == 'Not a Fit']

            exp_col1, exp_col2, exp_col3, exp_col4, exp_col5 = st.columns(5)

            with exp_col1:
                st.download_button(
                    f"GO ({len(go_list)})",
                    prepare_screening_export(go_list),
                    "screening_go.csv",
                    "text/csv",
                    disabled=len(go_list) == 0,
                    type="primary",
                    key="export_go"
                )

            with exp_col2:
                st.download_button(
                    f"Maybe ({len(maybe_list)})",
                    prepare_screening_export(maybe_list),
                    "screening_maybe.csv",
                    "text/csv",
                    disabled=len(maybe_list) == 0,
                    key="export_maybe"
                )

            with exp_col3:
                st.download_button(
                    f"NO GO ({len(no_go_list)})",
                    prepare_screening_export(no_go_list),
                    "screening_no_go.csv",
                    "text/csv",
                    disabled=len(no_go_list) == 0,
                    key="export_no_go"
                )

            with exp_col4:
                st.download_button(
                    f"All Results ({len(sorted_results)})",
                    prepare_screening_export(sorted_results),
                    "screening_results_all.csv",
                    "text/csv"
                )

            with exp_col5:
                st.button("Clear Results", key="clear_screening", on_click=_cb_clear_screening_results)

# ========== TAB 6: Emails ==========
with tab_emails:
    st.markdown("### Generate Email Openers")
    
    # Get screening results from session state
    screening_results = st.session_state.get('screening_results', [])
    
    if not HAS_EMAIL_GENERATOR:
        st.warning("Email generator module not available.")
    elif not screening_results:
        st.info("Run AI Screening first to generate emails for screened profiles.")
    else:
        st.markdown("### Generate Email Openers")
        st.caption("Create personalized subject lines and email openers for your top candidates")

        # Check if we have profiles with raw data
        profiles_with_raw = [r for r in screening_results if r.get('linkedin_url')]
        if not profiles_with_raw:
            st.warning("No profiles with LinkedIn URLs found in results.")
        else:
            # Settings
            email_col1, email_col2, email_col3, email_col4, email_col5 = st.columns(5)

            with email_col1:
                email_generate_type = st.selectbox(
                    "Generate",
                    options=["both", "subject_only", "opener_only"],
                    format_func=lambda x: {"both": "Both", "subject_only": "Subject Only", "opener_only": "Opener Only"}[x],
                    index=0,
                    key="email_generate_type"
                )

            with email_col2:
                st.caption("Model: **gpt-4o-mini**")
                # Fixed model - gpt-4o-mini only
                email_ai_model = "gpt-4o-mini"
                email_ai_provider = "openai"

            with email_col3:
                email_sender = st.selectbox(
                    "Sender Persona",
                    options=list(SENDER_PERSONAS.keys()),
                    format_func=lambda x: x.replace('_', ' ').title(),
                    index=0,
                    key="email_sender"
                )

            with email_col4:
                email_tone = st.selectbox(
                    "Tone",
                    options=list(TONE_DESCRIPTIONS.keys()),
                    format_func=lambda x: x.title(),
                    index=1,  # Default to 'professional'
                    key="email_tone"
                )

            with email_col5:
                email_length = st.selectbox(
                    "Opener Length",
                    options=list(LENGTH_DESCRIPTIONS.keys()),
                    format_func=lambda x: x.title(),
                    index=1,  # Default to 'medium'
                    key="email_length"
                )

            # Second row: position, company context, fit filter
            email_row2_col0, email_row2_col1, email_row2_col2 = st.columns([1, 2, 1])

            with email_row2_col0:
                email_position = st.text_input(
                    "Position",
                    placeholder="E.g., DevOps Engineer, Sales Manager, Product Lead",
                    key="email_position"
                )

            with email_row2_col1:
                email_company_context = st.text_input(
                    "Company & Product",
                    placeholder="E.g., Orca Security — agentless cloud security platform, competitor to Wiz",
                    key="email_company_context"
                )

            with email_row2_col2:
                email_fit_filter = st.multiselect(
                    "Include Fit Levels",
                    options=["Good Fit", "Maybe"],
                    default=["Good Fit"],
                    key="email_fit_filter",
                    help="Good Fit = GO, Maybe = borderline NO GO (score ≥ threshold)"
                )

            # Third row: custom instruction (optional)
            email_custom_instruction = st.text_input(
                "Custom Instruction (optional)",
                placeholder="E.g., 'Mention we just raised $50M' or 'Focus on cloud experience, skip education angle'",
                key="email_custom_instruction"
            )

            # Combine company context + custom instruction for the AI
            email_full_instruction = ""
            if email_company_context:
                email_full_instruction += f"COMPANY CONTEXT: {email_company_context}"
            if email_custom_instruction:
                if email_full_instruction:
                    email_full_instruction += f"\n{email_custom_instruction}"
                else:
                    email_full_instruction = email_custom_instruction

            # Filter profiles by selected buckets (Good Fit = GO, Maybe = borderline)
            filtered_profiles = [r for r in profiles_with_raw if r.get('fit') in email_fit_filter]

            # Test batch size and cost estimate
            email_test_col1, email_test_col2, email_test_col3 = st.columns([1, 1, 2])

            with email_test_col1:
                test_batch_size = st.number_input(
                    "Test Batch Size",
                    min_value=1,
                    max_value=min(20, len(filtered_profiles)) if filtered_profiles else 1,
                    value=min(5, len(filtered_profiles)) if filtered_profiles else 1,
                    key="email_test_batch_size"
                )

            with email_test_col2:
                if filtered_profiles:
                    test_cost = estimate_email_cost(test_batch_size, email_ai_model)
                    full_cost = estimate_email_cost(len(filtered_profiles), email_ai_model)
                    st.metric("Est. Cost", f"Test: ${test_cost:.4f} | All: ${full_cost:.3f}")
                else:
                    st.metric("Profiles Selected", "0")

            with email_test_col3:
                st.info(f"**{len(filtered_profiles)}** profiles selected ({', '.join(email_fit_filter) or 'none'})")

            # Initialize session state for email results
            if 'email_generation_results' not in st.session_state:
                st.session_state['email_generation_results'] = []
            if 'email_test_approved' not in st.session_state:
                st.session_state['email_test_approved'] = False

            # Action buttons
            email_btn_col1, email_btn_col2, email_btn_col3, email_btn_col4 = st.columns([1, 1, 1, 1])

            with email_btn_col1:
                generate_test_btn = st.button(
                    f"Generate Test ({test_batch_size})",
                    disabled=len(filtered_profiles) == 0,
                    key="email_generate_test"
                )

            with email_btn_col2:
                email_custom_count = st.number_input(
                    "Custom count",
                    min_value=1,
                    max_value=len(filtered_profiles) if filtered_profiles else 1,
                    value=min(20, len(filtered_profiles)) if filtered_profiles else 1,
                    key="email_custom_count"
                )
                generate_custom_btn = st.button(
                    f"Generate {email_custom_count}",
                    disabled=len(filtered_profiles) == 0 or not st.session_state.get('email_test_approved'),
                    type="primary" if st.session_state.get('email_test_approved') else "secondary",
                    key="email_generate_custom"
                )

            with email_btn_col3:
                generate_all_btn = st.button(
                    f"Generate All ({len(filtered_profiles)})",
                    disabled=len(filtered_profiles) == 0 or not st.session_state.get('email_test_approved'),
                    type="primary" if st.session_state.get('email_test_approved') else "secondary",
                    key="email_generate_all"
                )

            with email_btn_col4:
                if not st.session_state.get('email_test_approved'):
                    st.caption("Run a test batch first to unlock custom & all")

            # Get OpenAI API key (use load_openai_key which handles st.secrets for cloud)
            openai_key = st.session_state.get('openai_api_key') or os.environ.get('OPENAI_API_KEY') or load_openai_key()

            # Generate test batch
            if generate_test_btn:
                if not openai_key:
                    st.error("OpenAI API key is required. Set it in Streamlit secrets (openai_api_key), config.json, or environment variable OPENAI_API_KEY.")
                elif not filtered_profiles:
                    st.warning("No profiles selected. Check that profiles have matching fit levels.")

            if generate_test_btn and openai_key and filtered_profiles:
                test_profiles = filtered_profiles[:test_batch_size]

                # Need to load raw_data for profiles
                profiles_for_email = []
                raw_data_loaded = 0
                raw_data_failed = []
                for p in test_profiles:
                    profile_data = {'name': p.get('name'), 'linkedin_url': p.get('linkedin_url'),
                                   'current_title': p.get('current_title'), 'current_company': p.get('current_company'),
                                   'email': p.get('email')}
                    # Try to get raw_data from database
                    if HAS_DATABASE:
                        try:
                            db_client = _get_db_client()
                            if db_client and p.get('linkedin_url'):
                                db_profile = get_profile(db_client, p['linkedin_url'])
                                if db_profile and db_profile.get('raw_data'):
                                    profile_data['raw_data'] = db_profile['raw_data']
                                    raw_data_loaded += 1
                                else:
                                    raw_data_failed.append(p.get('name', p.get('linkedin_url', 'Unknown')))
                        except Exception as e:
                            raw_data_failed.append(f"{p.get('name', 'Unknown')}: {str(e)[:50]}")
                    profiles_for_email.append(profile_data)

                if raw_data_failed:
                    st.warning(f"Could not load raw_data for {len(raw_data_failed)}/{len(test_profiles)} profiles. These will return empty emails.")

                with st.status(f"Generating test batch ({len(profiles_for_email)} profiles)...", expanded=True) as status:
                    progress_bar = st.progress(0)
                    results_placeholder = st.empty()

                    def update_progress(completed, total, result):
                        progress_bar.progress(completed / total)
                        results_placeholder.text(f"Completed {completed}/{total}")

                    try:
                        email_api_key = anthropic_key if email_ai_provider == "anthropic" else openai_key
                        if not email_api_key:
                            raise ValueError(f"{'Anthropic' if email_ai_provider == 'anthropic' else 'OpenAI'} API key not configured")
                        results = generate_emails_batch(
                            profiles_for_email,
                            email_api_key,
                            sender=email_sender,
                            tone=email_tone,
                            length=email_length,
                            custom_instruction=email_full_instruction if email_full_instruction else None,
                            ai_model=email_ai_model,
                            max_workers=5,
                            progress_callback=update_progress,
                            generate_type=email_generate_type,
                            position=email_position if email_position else None,
                            ai_provider=email_ai_provider
                        )

                        st.session_state['email_generation_results'] = results
                        st.session_state['email_test_approved'] = True

                        # Show angle distribution
                        dist = get_angle_distribution(results)
                        status.update(label=f"Test complete! {len(results)} emails generated", state="complete")

                    except Exception as e:
                        st.error(f"Error generating emails: {str(e)}")
                        status.update(label="Error", state="error")

                st.rerun()

            # Generate all
            if generate_all_btn:
                email_api_key_check = anthropic_key if email_ai_provider == "anthropic" else openai_key
                if not email_api_key_check:
                    st.error(f"{'Anthropic' if email_ai_provider == 'anthropic' else 'OpenAI'} API key is required. Add it to config.json or Streamlit secrets.")
                elif not filtered_profiles:
                    st.warning("No profiles selected. Check that profiles have matching fit levels.")
                elif not st.session_state.get('email_test_approved'):
                    st.warning("Run a test batch first to unlock 'Generate All'.")

            # Generate custom count
            email_api_key_all = anthropic_key if email_ai_provider == "anthropic" else openai_key
            if generate_custom_btn and email_api_key_all and filtered_profiles and st.session_state.get('email_test_approved'):
                custom_profiles = filtered_profiles[:email_custom_count]
                with st.spinner(f"Loading profile data for {len(custom_profiles)} profiles..."):
                    profiles_for_email = []
                    raw_data_loaded = 0
                    raw_data_missing = 0

                    db_profiles_map = {}
                    if HAS_DATABASE:
                        try:
                            db_client = _get_db_client()
                            if db_client:
                                urls_to_fetch = [p.get('linkedin_url') for p in custom_profiles if p.get('linkedin_url')]
                                if urls_to_fetch:
                                    from db import get_profiles_by_urls
                                    db_profiles = get_profiles_by_urls(db_client, urls_to_fetch, include_raw_data=True)
                                    for dp in db_profiles:
                                        url = dp.get('linkedin_url', '')
                                        if url:
                                            db_profiles_map[url.lower().rstrip('/')] = dp
                                        orig_url = dp.get('original_url', '')
                                        if orig_url:
                                            db_profiles_map[orig_url.lower().rstrip('/')] = dp
                        except Exception as e:
                            st.warning(f"Could not batch load from DB: {str(e)[:100]}")

                    for p in custom_profiles:
                        profile_data = {'name': p.get('name'), 'linkedin_url': p.get('linkedin_url'),
                                       'current_title': p.get('current_title'), 'current_company': p.get('current_company'),
                                       'email': p.get('email')}
                        url = (p.get('linkedin_url') or '').lower().rstrip('/')
                        db_profile = db_profiles_map.get(url)
                        if db_profile and db_profile.get('raw_data'):
                            profile_data['raw_data'] = db_profile['raw_data']
                            raw_data_loaded += 1
                        else:
                            raw_data_missing += 1
                        profiles_for_email.append(profile_data)

                if raw_data_missing > 0:
                    st.warning(f"Could not load raw_data for {raw_data_missing}/{len(custom_profiles)} profiles.")

                with st.status(f"Generating emails for {len(profiles_for_email)} profiles...", expanded=True) as status:
                    progress_bar = st.progress(0)
                    progress_text = st.empty()

                    def update_custom_progress(completed, total, result):
                        progress_bar.progress(completed / total)
                        progress_text.text(f"Completed {completed}/{total}")

                    try:
                        results = generate_emails_batch(
                            profiles_for_email,
                            email_api_key_all,
                            sender=email_sender,
                            tone=email_tone,
                            length=email_length,
                            custom_instruction=email_full_instruction if email_full_instruction else None,
                            ai_model=email_ai_model,
                            max_workers=10,
                            progress_callback=update_custom_progress,
                            generate_type=email_generate_type,
                            position=email_position if email_position else None,
                            ai_provider=email_ai_provider
                        )

                        st.session_state['email_generation_results'] = results
                        status.update(label=f"Complete! {len(results)} emails generated", state="complete")

                    except Exception as e:
                        st.error(f"Error generating emails: {str(e)}")
                        status.update(label="Error", state="error")

                st.rerun()

            # Generate all
            if generate_all_btn and email_api_key_all and filtered_profiles and st.session_state.get('email_test_approved'):
                # Batch load raw_data for all profiles (much faster than individual calls)
                with st.spinner(f"Loading profile data for {len(filtered_profiles)} profiles..."):
                    profiles_for_email = []
                    raw_data_loaded = 0
                    raw_data_missing = 0

                    # Batch fetch from DB
                    db_profiles_map = {}
                    if HAS_DATABASE:
                        try:
                            db_client = _get_db_client()
                            if db_client:
                                # Get all URLs to fetch
                                urls_to_fetch = [p.get('linkedin_url') for p in filtered_profiles if p.get('linkedin_url')]
                                if urls_to_fetch:
                                    # Batch fetch using get_profiles_by_urls
                                    from db import get_profiles_by_urls
                                    db_profiles = get_profiles_by_urls(db_client, urls_to_fetch, include_raw_data=True)
                                    # Build lookup map by URL
                                    for dp in db_profiles:
                                        url = dp.get('linkedin_url', '')
                                        if url:
                                            db_profiles_map[url.lower().rstrip('/')] = dp
                                        # Also index by original_url
                                        orig_url = dp.get('original_url', '')
                                        if orig_url:
                                            db_profiles_map[orig_url.lower().rstrip('/')] = dp
                        except Exception as e:
                            st.warning(f"Could not batch load from DB: {str(e)[:100]}")

                    # Build profiles_for_email with raw_data
                    for p in filtered_profiles:
                        profile_data = {'name': p.get('name'), 'linkedin_url': p.get('linkedin_url'),
                                       'current_title': p.get('current_title'), 'current_company': p.get('current_company'),
                                       'email': p.get('email')}
                        # Look up raw_data from batch-fetched map
                        url = (p.get('linkedin_url') or '').lower().rstrip('/')
                        db_profile = db_profiles_map.get(url)
                        if db_profile and db_profile.get('raw_data'):
                            profile_data['raw_data'] = db_profile['raw_data']
                            raw_data_loaded += 1
                        else:
                            raw_data_missing += 1
                        profiles_for_email.append(profile_data)

                if raw_data_missing > 0:
                    st.warning(f"Could not load raw_data for {raw_data_missing}/{len(filtered_profiles)} profiles (URL mismatch or not in DB). These will return empty emails.")

                with st.status(f"Generating emails for {len(profiles_for_email)} profiles...", expanded=True) as status:
                    progress_bar = st.progress(0)
                    progress_text = st.empty()

                    def update_all_progress(completed, total, result):
                        progress_bar.progress(completed / total)
                        progress_text.text(f"Completed {completed}/{total}")

                    try:
                        results = generate_emails_batch(
                            profiles_for_email,
                            email_api_key_all,
                            sender=email_sender,
                            tone=email_tone,
                            length=email_length,
                            custom_instruction=email_full_instruction if email_full_instruction else None,
                            ai_model=email_ai_model,
                            max_workers=10,
                            progress_callback=update_all_progress,
                            generate_type=email_generate_type,
                            position=email_position if email_position else None,
                            ai_provider=email_ai_provider
                        )

                        st.session_state['email_generation_results'] = results
                        status.update(label=f"Complete! {len(results)} emails generated", state="complete")

                    except Exception as e:
                        st.error(f"Error generating emails: {str(e)}")
                        status.update(label="Error", state="error")

                st.rerun()

            # Display results
            email_results = st.session_state.get('email_generation_results', [])
            if email_results:
                st.markdown("#### Generated Emails")

                # Angle distribution summary
                dist = get_angle_distribution(email_results)
                angle_summary = ", ".join([f"{k}: {v}" for k, v in dist['subject_angles'].items() if k != 'unknown'])
                st.caption(f"Subject angles: {angle_summary}")

                # Results table
                display_results = []
                errors_count = 0
                for r in email_results:
                    if r.get('error'):
                        errors_count += 1
                    display_results.append({
                        'Name': r.get('name', ''),
                        'LinkedIn': r.get('linkedin_url', ''),
                        'Subject': r.get('subject_line', ''),
                        'Opener': r.get('email_opener', ''),
                        'Subject Angle': r.get('subject_angle', ''),
                        'Opener Angle': r.get('opener_angle', ''),
                        'Email': r.get('email', ''),
                        'Error': r.get('error', ''),
                    })

                if errors_count > 0:
                    st.warning(f"{errors_count} profiles failed to generate emails. Check 'Error' column for details.")

                email_df = pd.DataFrame(display_results)
                st.dataframe(
                    email_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "LinkedIn": st.column_config.LinkColumn("LinkedIn", display_text="View"),
                    }
                )

                # Export buttons
                email_export_col1, email_export_col2, email_export_col3, email_export_col4 = st.columns(4)

                with email_export_col1:
                    # Full export with all fields
                    export_df = pd.DataFrame(email_results)
                    priority_email_cols = ['first_name', 'last_name', 'name', 'email', 'linkedin_url',
                                          'location', 'university', 'current_title', 'current_company',
                                          'subject_line', 'email_opener', 'subject_angle', 'opener_angle']
                    ordered_cols = [c for c in priority_email_cols if c in export_df.columns]
                    remaining_cols = [c for c in export_df.columns if c not in priority_email_cols and c != 'index']
                    st.download_button(
                        f"Export All ({len(email_results)})",
                        export_df[ordered_cols + remaining_cols].to_csv(index=False),
                        "email_openers.csv",
                        "text/csv",
                        type="primary"
                    )

                with email_export_col2:
                    # Export only with emails
                    with_email = [r for r in email_results if r.get('email')]
                    if with_email:
                        email_export_df = pd.DataFrame(with_email)
                        ordered_cols_email = [c for c in priority_email_cols if c in email_export_df.columns]
                        remaining_cols_email = [c for c in email_export_df.columns if c not in priority_email_cols and c != 'index']
                        st.download_button(
                            f"With Email ({len(with_email)})",
                            email_export_df[ordered_cols_email + remaining_cols_email].to_csv(index=False),
                            "email_openers_with_email.csv",
                            "text/csv"
                        )
                    else:
                        st.button("With Email (0)", disabled=True)

                with email_export_col3:
                    # Refresh emails from SalesQL/DB
                    if st.button("Refresh Emails", key="refresh_emails_from_salesql", help="Update with latest SalesQL emails"):
                        updated_count = 0
                        skipped_mismatch = 0
                        from normalizers import normalize_linkedin_url as norm_url

                        # Helper to normalize URL for matching
                        def norm(url):
                            if not url:
                                return ''
                            try:
                                return norm_url(url) or url.lower().rstrip('/')
                            except:
                                return url.lower().rstrip('/')

                        # Helper to get first name for verification
                        def get_first_name(name):
                            if not name:
                                return ''
                            return str(name).split()[0].lower().strip() if name else ''

                        # Helper to check if names match (first name comparison)
                        def names_match(name1, name2):
                            fn1 = get_first_name(name1)
                            fn2 = get_first_name(name2)
                            if not fn1 or not fn2:
                                return True  # Can't verify, allow match
                            return fn1 == fn2

                        # Collect all email sources with normalized URLs
                        # Store (email, name) tuples for verification
                        all_emails = {}

                        # 1. From screening results
                        screening_results = st.session_state.get('screening_results', [])
                        for r in screening_results:
                            url = norm(r.get('linkedin_url'))
                            email = r.get('salesql_email') or r.get('email')
                            name = r.get('name', '')
                            if url and email:
                                all_emails[url] = (email, name)

                        # 2. From passed_candidates_df (SalesQL enrichment results)
                        passed_df = st.session_state.get('passed_candidates_df')
                        if passed_df is not None and not passed_df.empty:
                            url_col = 'linkedin_url' if 'linkedin_url' in passed_df.columns else 'public_url'
                            email_col = 'salesql_email' if 'salesql_email' in passed_df.columns else 'email'
                            name_col = 'name' if 'name' in passed_df.columns else 'first_name'
                            if url_col in passed_df.columns and email_col in passed_df.columns:
                                for _, row in passed_df.iterrows():
                                    url = norm(row.get(url_col))
                                    email = row.get(email_col)
                                    name = row.get(name_col, '')
                                    if url and email and pd.notna(email):
                                        all_emails[url] = (email, name)

                        # 3. From database (batch lookup for efficiency)
                        if HAS_DATABASE:
                            try:
                                db_client = _get_db_client()
                                if db_client:
                                    # Get URLs we don't have emails for yet
                                    missing_urls = []
                                    for r in email_results:
                                        url = r.get('linkedin_url')
                                        if url and norm(url) not in all_emails:
                                            missing_urls.append(url)

                                    if missing_urls:
                                        from db import get_profiles_by_urls
                                        db_profiles = get_profiles_by_urls(db_client, missing_urls[:100])
                                        for p in db_profiles:
                                            url = norm(p.get('linkedin_url'))
                                            email = p.get('email')
                                            name = p.get('name', '')
                                            if url and email:
                                                all_emails[url] = (email, name)
                            except Exception as e:
                                st.warning(f"DB lookup failed: {str(e)[:50]}")

                        # Update email_generation_results with name verification
                        for r in st.session_state['email_generation_results']:
                            url = norm(r.get('linkedin_url'))
                            match = all_emails.get(url)
                            if match:
                                new_email, source_name = match
                                target_name = r.get('name', '')
                                if new_email and new_email != r.get('email'):
                                    # Verify names match before assigning
                                    if names_match(target_name, source_name):
                                        r['email'] = new_email
                                        updated_count += 1
                                    else:
                                        skipped_mismatch += 1

                        if updated_count > 0:
                            msg = f"Updated {updated_count} emails!"
                            if skipped_mismatch > 0:
                                msg += f" (Skipped {skipped_mismatch} - name mismatch)"
                            st.success(msg)
                        else:
                            msg = f"No new emails found. Checked {len(all_emails)} sources."
                            if skipped_mismatch > 0:
                                msg += f" Skipped {skipped_mismatch} due to name mismatch."
                            st.info(msg)
                        st.rerun()

                with email_export_col4:
                    if st.button("Clear Results", key="clear_email_results"):
                        st.session_state['email_generation_results'] = []
                        st.session_state['email_test_approved'] = False
                        st.rerun()

# ========== TAB 7: Database ==========

with tab_database:
    st.markdown("### Profile Database")
    st.caption("View and manage all profiles stored in Supabase")

    if not HAS_DATABASE:
        st.warning("Database module not available. Check db.py import.")
    else:
        try:
            db_client = _get_db_client()
            if not db_client:
                st.warning("Supabase not configured or cannot connect. Check your credentials.")
            else:
                # Connection successful (_get_db_client already verified connection)
                st.success("Connected to Supabase")

                # Pipeline stats - only load on demand
                with st.expander("Database Overview", expanded=False):
                    if st.button("Load Stats", key="db_load_stats"):
                        try:
                            import datetime as dt
                            total = db_client.count('profiles')
                            # Fresh = enriched within last 6 months
                            six_months_ago = (dt.datetime.utcnow() - dt.timedelta(days=180)).isoformat()
                            fresh = db_client.count('profiles', {'enriched_at': f'gte.{six_months_ago}'})
                            stale = total - fresh
                            has_email = db_client.count('profiles', {'email': 'not.is.null'})
                            st.session_state['db_pipeline_stats'] = {'total': total, 'fresh': fresh, 'stale': stale, 'has_email': has_email}
                        except Exception as e:
                            st.warning(f"Could not load stats: {e}")
                    stats = st.session_state.get('db_pipeline_stats', {})
                    if stats:
                        stat_cols = st.columns(4)
                        stat_cols[0].metric("Total Profiles", stats.get('total', 0))
                        stat_cols[1].metric("Fresh (<6mo)", stats.get('fresh', 0))
                        stat_cols[2].metric("Stale (>6mo)", stats.get('stale', 0))
                        stat_cols[3].metric("Has Email", stats.get('has_email', 0))

                st.divider()

                # Browse & filter profiles
                st.markdown("#### Browse Profiles")

                # --- Search Filters (server-side) ---
                st.markdown("##### Search Filters")

                # AI refine config (mirrors Tab 0 Search). Buttons can't fire inside
                # ``st.form`` callbacks, so this section is a regular block — the
                # text inputs persist via ``key=`` and the Search button below
                # triggers the search on click. Tab-7 specific key namespace:
                # ``db_f_<field>`` for inputs, ``db_expanded_<field>`` for AI
                # suggestions. The module-level helpers
                # (_ai_render_field / _ai_effective_value) do the same work the
                # Tab 0 closures do, just parameterised by key prefix.
                _db_openai_key = load_openai_key()
                _db_has_ai = bool(_db_openai_key and HAS_CRUSTDATA_SEARCH)

                # Full-text search (no AI refine — it's already a boolean expression)
                fulltext_query = st.text_input(
                    "Full-text search",
                    key="db_fulltext_search",
                    placeholder="e.g., (react OR angular) AND node.js NOT director",
                    help=(
                        "**Boolean syntax** (LinkedIn-style):\n"
                        "- `AND` (or just space) — both terms required\n"
                        "- `OR` (or `,`) — either term matches\n"
                        "- `NOT` (or `-` prefix) — exclude term\n"
                        "- `( ... )` — group terms\n\n"
                        "**Searches across:** name, titles, companies, skills, schools, "
                        "summary, headline, job descriptions, education, languages."
                    )
                )

                # Row 1: Name, Location (Location has AI refine)
                fcol1, fcol2 = st.columns(2)
                with fcol1:
                    st.text_input("Name", key="db_f_name", placeholder="john doe")
                _ai_render_field(
                    fcol2, "Location", "db_f_location", "db_expanded_location",
                    "location", "israel, nyc",
                    _db_openai_key, _db_has_ai,
                )

                # Row 2: Current Title (AI), Past Titles
                fcol3, fcol4 = st.columns(2)
                _ai_render_field(
                    fcol3, "Current Title", "db_f_current_title", "db_expanded_current_title",
                    "title", "backend, devops",
                    _db_openai_key, _db_has_ai,
                )
                with fcol4:
                    st.text_input("Past Titles", key="db_f_past_titles", placeholder="backend, team lead",
                                  help="Partial match on past titles (uses full-text search)")

                # Row 3: Current Company (AI, geo-aware), Past Companies (AI, geo-aware)
                fcol5, fcol6 = st.columns(2)
                _ai_render_field(
                    fcol5, "Current Company", "db_f_current_company", "db_expanded_current_company",
                    "company", "wiz, monday",
                    _db_openai_key, _db_has_ai,
                    geo_key="db_f_location",
                )
                _ai_render_field(
                    fcol6, "Past Companies", "db_f_past_companies", "db_expanded_past_companies",
                    "company", "google, meta",
                    _db_openai_key, _db_has_ai,
                    geo_key="db_f_location",
                )

                # Row 4: Skills (AI), Schools (AI, geo-aware)
                fcol7, fcol8 = st.columns(2)
                _ai_render_field(
                    fcol7, "Skills (any of)", "db_f_skills", "db_expanded_skills",
                    "skill", "python, kubernetes, react",
                    _db_openai_key, _db_has_ai,
                )
                _ai_render_field(
                    fcol8, "Schools", "db_f_schools", "db_expanded_schools",
                    "school", "technion, tel aviv",
                    _db_openai_key, _db_has_ai,
                    geo_key="db_f_location",
                )

                # Row 5: Freshness, Email, date_after, Search button
                fcol9, fcol10, fcol11, fcol12 = st.columns([1, 1, 1, 1])
                with fcol9:
                    f_freshness = st.selectbox(
                        "Freshness",
                        ["All", "Fresh (<6mo)", "Stale (>6mo)"],
                        key="db_f_freshness"
                    )
                with fcol10:
                    f_has_email = st.checkbox("Has Email", key="db_f_has_email")
                with fcol11:
                    f_date_after = st.date_input("Enriched After", value=None, key="db_f_date_after")
                with fcol12:
                    st.write("")  # vertical spacer to align button with selectbox
                    search_clicked = st.button("Search", key="db_search_btn", type="primary", width="stretch")

                # Read non-AI inputs from session state
                f_name = st.session_state.get("db_f_name", "")
                f_past_titles = st.session_state.get("db_f_past_titles", "")

                # Merge AI-refined fields (manual input + multiselect selections)
                f_location = _ai_effective_value("db_f_location", "db_expanded_location")
                f_current_title = _ai_effective_value("db_f_current_title", "db_expanded_current_title")
                f_current_company = _ai_effective_value("db_f_current_company", "db_expanded_current_company")
                f_past_companies = _ai_effective_value("db_f_past_companies", "db_expanded_past_companies")
                f_skills = _ai_effective_value("db_f_skills", "db_expanded_skills")
                f_schools = _ai_effective_value("db_f_schools", "db_expanded_schools")

                # Build filters dict (now uses the merged effective values)
                current_filters = {
                    'name': f_name, 'current_title': f_current_title, 'past_titles': f_past_titles,
                    'current_company': f_current_company, 'past_companies': f_past_companies,
                    'location': f_location, 'skills': f_skills, 'schools': f_schools,
                    'date_after': str(f_date_after) if f_date_after else None,
                    'freshness': f_freshness,
                    'has_email': f_has_email
                }
                has_column_filters = any([f_name, f_current_title, f_past_titles, f_current_company, f_past_companies,
                                          f_location, f_skills, f_schools, f_date_after, f_freshness != "All", f_has_email])

                # Store search state — clear cached results so the next render re-queries
                if search_clicked:
                    st.session_state['db_search_executed'] = True
                    st.session_state['db_applied_filters'] = current_filters
                    st.session_state['db_fulltext'] = fulltext_query
                    st.session_state.pop('db_search_results', None)

                # Fetch profiles based on search (SERVER-SIDE filtering)
                all_profiles = []
                search_executed = st.session_state.get('db_search_executed', False)

                if search_executed:
                    af = st.session_state.get('db_applied_filters', {})
                    ft = st.session_state.get('db_fulltext', '')

                    has_column_filters = any(af.get(k) for k in ['name', 'current_title', 'past_titles', 'current_company', 'past_companies',
                                                   'location', 'skills', 'schools', 'date_after', 'has_email']) or af.get('freshness', 'All') != 'All'

                    if 'db_search_results' in st.session_state:
                        # Use cached results — no DB query on this render
                        all_profiles = st.session_state['db_search_results']
                        if ft and ft.strip():
                            st.caption(f"Full-text search: **{ft}**" + (" + column filters" if has_column_filters else ""))
                        elif has_column_filters:
                            st.caption("Server-side filtered search")
                    else:
                        # Fresh query — only runs immediately after Search is clicked

                        # For past_titles and past_companies, we need full-text search for partial matching
                        # Build a combined fulltext query if these filters are used
                        array_field_query_parts = []
                        if af.get('past_titles'):
                            # Convert "backend, frontend" to "backend OR frontend"
                            terms = [t.strip() for t in af['past_titles'].split(',') if t.strip()]
                            if terms:
                                array_field_query_parts.append(' OR '.join(terms))
                        if af.get('past_companies'):
                            terms = [t.strip() for t in af['past_companies'].split(',') if t.strip()]
                            if terms:
                                array_field_query_parts.append(' OR '.join(terms))

                        # Combine with existing fulltext query
                        if array_field_query_parts:
                            array_query = ' AND '.join(f"({p})" for p in array_field_query_parts)
                            if ft and ft.strip():
                                ft = f"({ft}) AND ({array_query})"
                            else:
                                ft = array_query
                            # Remove these from column filters since we're using fulltext
                            af = {k: v for k, v in af.items() if k not in ['past_titles', 'past_companies']}

                        if ft and ft.strip():
                            # Full-text search (supports partial matching on all fields).
                            # Cap at 5000 rows: broad queries like "senior" match 22K+ rows,
                            # and OFFSET-based pagination past a few thousand rows is slow
                            # enough to freeze the UI. Real-world flow is to narrow with
                            # column filters (title, location, skills) inside this set.
                            from db import search_profiles_fulltext
                            with st.spinner("Searching profiles..."):
                                all_profiles = search_profiles_fulltext(db_client, ft.strip(), limit=5000)
                            st.caption(f"Full-text search: **{ft}**" + (" + column filters" if has_column_filters else ""))
                        elif has_column_filters:
                            # Column filters only (server-side)
                            from db import search_profiles_boolean
                            with st.spinner("Searching profiles..."):
                                all_profiles = search_profiles_boolean(db_client, af, limit=5000)
                            st.caption("Server-side filtered search")
                        else:
                            # No filters - load all (cached, 1-min TTL)
                            with st.spinner("Loading profiles..."):
                                all_profiles = _cached_all_profiles(limit=5000)

                        st.session_state['db_search_results'] = all_profiles
                else:
                    st.info("Enter filters and click **Search** to find profiles")

                if all_profiles:
                    df = profiles_to_dataframe(all_profiles)

                    # Capture pre-filter count so we can show users where the funnel
                    # collapsed (a confused user reported "0 results" without knowing
                    # whether the column filters or the fulltext was responsible).
                    _pre_column_filter_count = len(df)

                    # Create combined name column only if name is empty
                    if 'name' not in df.columns:
                        df['name'] = ''
                    if 'first_name' in df.columns and 'last_name' in df.columns:
                        combined = (df['first_name'].fillna('') + ' ' + df['last_name'].fillna('')).str.strip()
                        df['name'] = df['name'].fillna('').replace('', pd.NA).fillna(combined)

                    # Apply column filters client-side when combined with full-text search
                    # (full-text search RPC doesn't support column filters natively)
                    if ft and ft.strip() and has_column_filters:
                        def _parse_boolean_query_client(query: str) -> dict:
                            """Parse boolean query for client-side filtering.
                            Returns AST: {'type': 'OR'|'AND'|'NOT'|'TERM', 'children'|'child'|'value': ...}
                            """
                            import re
                            # Tokenize
                            tokens = []
                            i = 0
                            query = query.strip()
                            while i < len(query):
                                if query[i].isspace():
                                    i += 1
                                    continue
                                if query[i] == '"':
                                    end = query.find('"', i + 1)
                                    if end == -1:
                                        end = len(query)
                                    tokens.append(('PHRASE', query[i+1:end]))
                                    i = end + 1
                                    continue
                                if query[i] == '(':
                                    tokens.append(('LPAREN', '('))
                                    i += 1
                                    continue
                                if query[i] == ')':
                                    tokens.append(('RPAREN', ')'))
                                    i += 1
                                    continue
                                if query[i] == '|':
                                    tokens.append(('OR', 'OR'))
                                    i += 1
                                    continue
                                if query[i] == '&':
                                    tokens.append(('AND', 'AND'))
                                    i += 1
                                    continue
                                if query[i] in '!-' and (i == 0 or query[i-1].isspace() or query[i-1] in '(&|'):
                                    tokens.append(('NOT', 'NOT'))
                                    i += 1
                                    continue
                                if query[i] == ',':
                                    tokens.append(('OR', 'OR'))
                                    i += 1
                                    continue
                                if query[i] == '+':
                                    i += 1
                                    continue
                                word_match = re.match(r'[\w\.\-]+', query[i:])
                                if word_match:
                                    word = word_match.group()
                                    upper_word = word.upper()
                                    if upper_word == 'AND':
                                        tokens.append(('AND', 'AND'))
                                    elif upper_word == 'OR':
                                        tokens.append(('OR', 'OR'))
                                    elif upper_word == 'NOT':
                                        tokens.append(('NOT', 'NOT'))
                                    else:
                                        tokens.append(('WORD', word))
                                    i += len(word)
                                    continue
                                i += 1

                            # Parse tokens to AST
                            pos = [0]
                            def peek():
                                return tokens[pos[0]] if pos[0] < len(tokens) else None
                            def consume():
                                t = peek()
                                pos[0] += 1
                                return t
                            def parse_or():
                                left = parse_and()
                                while peek() and peek()[0] == 'OR':
                                    consume()
                                    right = parse_and()
                                    if left.get('type') == 'OR':
                                        left['children'].append(right)
                                    else:
                                        left = {'type': 'OR', 'children': [left, right]}
                                return left
                            def parse_and():
                                left = parse_not()
                                while peek():
                                    token = peek()
                                    if token[0] == 'AND':
                                        consume()
                                        right = parse_not()
                                    elif token[0] in ('WORD', 'PHRASE', 'NOT', 'LPAREN'):
                                        right = parse_not()
                                    else:
                                        break
                                    if left.get('type') == 'AND':
                                        left['children'].append(right)
                                    else:
                                        left = {'type': 'AND', 'children': [left, right]}
                                return left
                            def parse_not():
                                if peek() and peek()[0] == 'NOT':
                                    consume()
                                    return {'type': 'NOT', 'child': parse_primary()}
                                return parse_primary()
                            def parse_primary():
                                token = peek()
                                if not token:
                                    return {'type': 'TERM', 'value': ''}
                                if token[0] == 'LPAREN':
                                    consume()
                                    expr = parse_or()
                                    if peek() and peek()[0] == 'RPAREN':
                                        consume()
                                    return expr
                                if token[0] in ('WORD', 'PHRASE'):
                                    consume()
                                    return {'type': 'TERM', 'value': token[1]}
                                consume()
                                return parse_primary()

                            if not tokens:
                                return {'type': 'TERM', 'value': ''}
                            return parse_or()

                        def _eval_boolean_ast(ast: dict, text: str) -> bool:
                            """Evaluate boolean AST against text."""
                            text = text.lower()
                            node_type = ast.get('type')
                            if node_type == 'TERM':
                                value = ast.get('value', '').lower()
                                return value in text if value else True
                            if node_type == 'NOT':
                                return not _eval_boolean_ast(ast['child'], text)
                            if node_type == 'OR':
                                return any(_eval_boolean_ast(c, text) for c in ast['children'])
                            if node_type == 'AND':
                                return all(_eval_boolean_ast(c, text) for c in ast['children'])
                            return True

                        def _boolean_filter(series, query_str):
                            """Boolean filter supporting OR, AND, NOT, phrases."""
                            ast = _parse_boolean_query_client(query_str)
                            return series.fillna('').apply(lambda val: _eval_boolean_ast(ast, str(val)))

                        def _boolean_array_filter(series, query_str):
                            """Boolean filter for array columns."""
                            ast = _parse_boolean_query_client(query_str)
                            def matches(val):
                                if isinstance(val, list):
                                    joined = ' '.join(str(v) for v in val)
                                elif isinstance(val, str):
                                    joined = val
                                else:
                                    return False
                                return _eval_boolean_ast(ast, joined)
                            return series.apply(matches)

                        mask = pd.Series(True, index=df.index)
                        str_filters = {'name': 'name', 'current_title': 'current_title',
                                       'current_company': 'current_company', 'location': 'location'}
                        arr_filters = {'past_titles': 'all_titles', 'past_companies': 'all_employers',
                                       'skills': 'skills', 'schools': 'all_schools'}

                        for fkey, col in str_filters.items():
                            if af.get(fkey) and col in df.columns:
                                mask &= _boolean_filter(df[col], af[fkey])
                        for fkey, col in arr_filters.items():
                            if af.get(fkey) and col in df.columns:
                                mask &= _boolean_array_filter(df[col], af[fkey])
                        if af.get('has_email') and 'email' in df.columns:
                            mask &= df['email'].fillna('').astype(bool)

                        df = df[mask]

                    # Apply array column filters client-side (always, since server-side
                    # PostgREST can't do substring matching on array columns)
                    af = st.session_state.get('db_applied_filters', {})
                    _arr_filters_map = {'past_titles': 'all_titles', 'past_companies': 'all_employers',
                                        'skills': 'skills', 'schools': 'all_schools'}
                    _has_arr_filters = any(af.get(k) for k in _arr_filters_map)
                    if _has_arr_filters:
                        # Re-use or define the boolean query helpers
                        if '_parse_boolean_query_client' not in dir():
                            from db import _tokenize_boolean_query, _parse_boolean_tokens
                            def _parse_boolean_query_client(query: str) -> dict:
                                tokens = _tokenize_boolean_query(query)
                                return _parse_boolean_tokens(tokens)
                            def _eval_boolean_ast(ast: dict, text: str) -> bool:
                                text = text.lower()
                                node_type = ast.get('type')
                                if node_type == 'TERM':
                                    value = ast.get('value', '').lower()
                                    return value in text if value else True
                                if node_type == 'NOT':
                                    return not _eval_boolean_ast(ast['child'], text)
                                if node_type == 'OR':
                                    return any(_eval_boolean_ast(c, text) for c in ast['children'])
                                if node_type == 'AND':
                                    return all(_eval_boolean_ast(c, text) for c in ast['children'])
                                return True
                        _arr_mask = pd.Series(True, index=df.index)
                        for _fkey, _col in _arr_filters_map.items():
                            if af.get(_fkey) and _col in df.columns:
                                _ast = _parse_boolean_query_client(af[_fkey])
                                def _arr_match(val, ast=_ast):
                                    if isinstance(val, list):
                                        joined = ' '.join(str(v) for v in val)
                                    elif isinstance(val, str):
                                        joined = val
                                    else:
                                        return False
                                    return _eval_boolean_ast(ast, joined)
                                _arr_mask &= df[_col].apply(_arr_match)
                        df = df[_arr_mask]

                    # Apply freshness filter (always, not just with full-text search)
                    freshness = af.get('freshness', 'All')
                    if freshness != 'All' and 'enriched_at' in df.columns:
                        import datetime as dt
                        six_months_ago = dt.datetime.utcnow() - dt.timedelta(days=180)
                        df['enriched_at_dt'] = pd.to_datetime(df['enriched_at'], errors='coerce', utc=True)
                        if freshness == 'Fresh (<6mo)':
                            df = df[df['enriched_at_dt'] >= six_months_ago.replace(tzinfo=dt.timezone.utc)]
                        elif freshness == 'Stale (>6mo)':
                            df = df[df['enriched_at_dt'] < six_months_ago.replace(tzinfo=dt.timezone.utc)]
                        df = df.drop(columns=['enriched_at_dt'], errors='ignore')

                    # Apply has_email filter (when not already applied via full-text combo)
                    if af.get('has_email') and 'email' in df.columns:
                        df = df[df['email'].fillna('').astype(bool)]

                    filtered_df = df

                    # Display limit to prevent browser freeze
                    DISPLAY_LIMIT = 500

                    # Results — show the search funnel so users can see where the
                    # narrowing happened (e.g. "FT found 500, column filters left 0").
                    _final_count = len(filtered_df)
                    _ft_active = bool(ft and ft.strip())
                    _columns_narrowed = (
                        _ft_active and has_column_filters and _final_count < _pre_column_filter_count
                    )

                    if _final_count > DISPLAY_LIMIT:
                        st.info(f"Found **{_final_count}** profiles (showing first {DISPLAY_LIMIT})")
                    else:
                        st.info(f"Found **{_final_count}** profiles")

                    if _columns_narrowed:
                        _dropped = _pre_column_filter_count - _final_count
                        st.caption(
                            f"Full-text search matched **{_pre_column_filter_count}** profiles; "
                            f"column filters dropped **{_dropped}**. "
                            "If that's not what you expected, try removing one column filter "
                            "(e.g. *Current Company* or *Current Title*) to see broader matches."
                        )
                    elif _final_count == 0 and not _ft_active and has_column_filters:
                        # Column-filter-only search collapsed to 0 after client-side
                        # filtering — surface the server-side phase so the user knows
                        # which filter to relax first.
                        st.caption(
                            "Server-side filters returned 0 matches. Try relaxing one "
                            "filter (e.g. broaden Current Company or Current Title)."
                        )
                    elif _final_count == 0 and (_ft_active or has_column_filters):
                        st.caption(
                            "Tip: 0 results often means the DB doesn't have profiles enriched "
                            "for these criteria yet, not that the filter is broken. Try relaxing "
                            "one filter at a time."
                        )

                    # Toggle to show all columns
                    show_all_db_cols = st.checkbox("Show all columns", value=False, key="db_show_all_cols")

                    # Limit display to prevent browser freeze (full data still available for actions)
                    display_df = filtered_df.head(DISPLAY_LIMIT).copy()

                    # Add Freshness column for display
                    if 'enriched_at' in display_df.columns:
                        import datetime as dt
                        six_months_ago = dt.datetime.utcnow() - dt.timedelta(days=180)
                        display_df['enriched_at_dt'] = pd.to_datetime(display_df['enriched_at'], errors='coerce', utc=True)
                        display_df['freshness'] = display_df['enriched_at_dt'].apply(
                            lambda x: 'Fresh' if pd.notna(x) and x >= six_months_ago.replace(tzinfo=dt.timezone.utc) else 'Stale'
                        )
                        display_df = display_df.drop(columns=['enriched_at_dt'], errors='ignore')

                    if show_all_db_cols:
                        _all_edit_df = blank_tenure_sentinel(display_df).copy()
                        _all_edit_df.insert(0, 'Remove', False)
                        import hashlib as _hl
                        _all_key = _hl.md5(",".join(_all_edit_df.get('linkedin_url', pd.Series()).dropna().astype(str).tolist()).encode()).hexdigest()[:8]
                        _edited_db_df = st.data_editor(
                            _all_edit_df,
                            use_container_width=True,
                            hide_index=True,
                            disabled=[c for c in _all_edit_df.columns if c != 'Remove'],
                            key=f"db_editor_all_{_all_key}",
                            column_config={
                                "Remove": st.column_config.CheckboxColumn("Remove"),
                                "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                                "public_url": st.column_config.LinkColumn("LinkedIn"),
                                "enriched_at": st.column_config.DatetimeColumn("Enriched", format="YYYY-MM-DD"),
                                "freshness": st.column_config.TextColumn("Freshness"),
                            }
                        )
                        st.caption(f"{len(display_df.columns)} columns")
                    else:
                        preview_cols = ['name', 'current_title', 'current_company', 'email', 'freshness', 'location', 'linkedin_url']
                        available_cols = [c for c in preview_cols if c in display_df.columns]
                        _preview_edit_df = (display_df[available_cols] if available_cols else display_df).copy()
                        _preview_edit_df.insert(0, 'Remove', False)
                        import hashlib as _hl
                        _preview_key = _hl.md5(",".join(_preview_edit_df.get('linkedin_url', pd.Series()).dropna().astype(str).tolist()).encode()).hexdigest()[:8]
                        _edited_db_df = st.data_editor(
                            _preview_edit_df,
                            use_container_width=True,
                            hide_index=True,
                            disabled=[c for c in _preview_edit_df.columns if c != 'Remove'],
                            key=f"db_editor_preview_{_preview_key}",
                            column_config={
                                "Remove": st.column_config.CheckboxColumn("Remove"),
                                "name": st.column_config.TextColumn("Name"),
                                "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                                "current_company": st.column_config.TextColumn("Company"),
                                "current_title": st.column_config.TextColumn("Title"),
                                "location": st.column_config.TextColumn("Location"),
                                "email": st.column_config.TextColumn("Email"),
                                "freshness": st.column_config.TextColumn("Freshness"),
                            }
                        )

                    # Show "Remove selected" button only when at least one row is checked
                    _to_remove_db = _edited_db_df[_edited_db_df['Remove'] == True]
                    if len(_to_remove_db) > 0:
                        _n_db = len(_to_remove_db)
                        if st.button(
                            f"Remove {_n_db} selected from database",
                            key="db_remove_selected_btn",
                            type="secondary",
                        ):
                            from db import delete_profile as _del_prof_db
                            _removed_db = 0
                            _failed_db = []
                            _deleted_db_urls = set()
                            for _url in _to_remove_db['linkedin_url'].dropna():
                                if _del_prof_db(db_client, str(_url)):
                                    _removed_db += 1
                                    _deleted_db_urls.add(str(_url))
                                else:
                                    _failed_db.append(_url)
                            if _deleted_db_urls:
                                if 'db_search_results' in st.session_state:
                                    st.session_state['db_search_results'] = [
                                        p for p in st.session_state['db_search_results']
                                        if p.get('linkedin_url') not in _deleted_db_urls
                                    ]
                                _cached_all_profiles.clear()
                            if _removed_db:
                                st.success(f"Removed {_removed_db} profile{'s' if _removed_db > 1 else ''} from the database.")
                            if _failed_db:
                                st.warning(f"Could not remove: {', '.join(str(u) for u in _failed_db)}")
                            if _removed_db:
                                st.rerun()

                    # Action buttons
                    btn_col1, btn_col2, btn_col3 = st.columns(3)
                    with btn_col1:
                        export_filtered_df = prepare_df_for_export(filtered_df)
                        st.download_button(
                            f"Download CSV ({len(filtered_df)})",
                            export_filtered_df.to_csv(index=False),
                            "database_filtered.csv",
                            "text/csv",
                            key="db_download_btn"
                        )
                    with btn_col3:
                        # Opt-in full-details export. The on-screen listing never
                        # loads raw_data (kept lightweight for large lists), so the
                        # normal CSV above is missing summary + job descriptions.
                        # Only on click do we fetch raw_data for the FILTERED rows,
                        # build the CSV, and stash ONLY the CSV bytes (never the raw
                        # profile dicts) — so the persistent listing stays lean.
                        if len(filtered_df) > 0:
                            if st.button(
                                f"📄 Full Details CSV ({len(filtered_df)})",
                                key="db_full_details_btn"
                            ):
                                urls = filtered_df['linkedin_url'].tolist()
                                with st.spinner(f"Fetching full details for {len(urls)} profiles..."):
                                    profiles_with_raw = get_profiles_by_urls(db_client, urls, include_raw_data=True)
                                    if profiles_with_raw:
                                        # get_profiles_by_urls reads the profiles table only —
                                        # unlike get_all_profiles (used for the normal listing),
                                        # it doesn't merge in screening results. Without this,
                                        # the full-details CSV would blank out screening_score/
                                        # fit/summary for already-screened people even though the
                                        # regular CSV for the same rows has them.
                                        from db import _select_latest_screenings_for_urls, _merge_latest_screenings
                                        screenings_by_url = _select_latest_screenings_for_urls(db_client, urls)
                                        profiles_with_raw = _merge_latest_screenings(profiles_with_raw, screenings_by_url)
                                if profiles_with_raw:
                                    export_df = build_full_details_export_df(profiles_with_raw)
                                    st.session_state['db_full_details_csv'] = export_df.to_csv(index=False).encode('utf-8-sig')
                                    # Fingerprint the exact filtered set this CSV covers,
                                    # so the download button below hides itself instead
                                    # of silently offering a stale file once the user
                                    # changes filters (a different row count/set).
                                    st.session_state['db_full_details_csv_urls'] = frozenset(urls)
                                    st.rerun()
                                else:
                                    st.warning("Could not load full details for these profiles.")
                            st.caption(
                                "Includes summary + full job descriptions. Pulls raw data "
                                "live, so a large filtered set can take a bit longer."
                            )
                            # After the rerun, offer the prepared CSV for download — but
                            # only while it still matches the current filtered set. If the
                            # user changed filters since generating it, hide it rather than
                            # offer a CSV for the wrong people.
                            _fd_current_urls = frozenset(filtered_df['linkedin_url'].tolist())
                            if (
                                st.session_state.get('db_full_details_csv') is not None
                                and st.session_state.get('db_full_details_csv_urls') == _fd_current_urls
                            ):
                                st.download_button(
                                    "⬇️ Download full details",
                                    st.session_state['db_full_details_csv'],
                                    "database_full_details.csv",
                                    "text/csv",
                                    key="db_full_details_download_btn"
                                )
                    with btn_col2:
                        if len(filtered_df) > 0:
                            if st.button(f"Send {len(filtered_df)} to Filter", key="db_to_filter_btn", type="primary"):
                                # Get URLs from filtered results
                                urls = filtered_df['linkedin_url'].tolist()

                                # Fetch profiles WITH raw_data (needed for screening)
                                from db import get_profiles_by_urls
                                with st.spinner(f"Loading {len(urls)} profiles with raw data..."):
                                    profiles_with_raw = get_profiles_by_urls(db_client, urls, include_raw_data=True)

                                if profiles_with_raw:
                                    # Convert to DataFrame for the Filter tab's Advanced Filtering section
                                    result_df = profiles_to_dataframe(profiles_with_raw)
                                    # Drop stale derived state from any previous load
                                    # (filter_stats, passed_candidates_df, screening_results, etc.)
                                    clear_results_derived_state(st.session_state)
                                    # Populate both results_df (basic Filter section) and
                                    # enriched_df (Advanced Filtering section) so the Filter
                                    # tab doesn't show "Upload data first".
                                    st.session_state['results_df'] = result_df
                                    st.session_state['original_results_df'] = result_df.copy()
                                    st.session_state['enriched_df'] = result_df.copy()
                                    st.session_state['_data_source'] = 'database_search'

                                    # Cache raw_data for screening
                                    st.session_state['enriched_profiles_raw'] = {
                                        p.get('linkedin_url', ''): p for p in profiles_with_raw if p.get('raw_data')
                                    }

                                    st.session_state['db_send_success'] = len(result_df)
                                    st.rerun()
                                else:
                                    st.warning("Could not load profiles with raw data")
                            # Show success message after rerun
                            if st.session_state.pop('db_send_success', None):
                                st.success(f"Loaded {st.session_state.get('enriched_df', pd.DataFrame()).shape[0]} profiles. Go to **Filter** tab → Advanced Filtering section to continue.")
                elif search_executed:
                    # Search ran but returned zero rows before any client-side
                    # filtering. Tell the user explicitly and point at the most
                    # likely cause so they can relax the right filter.
                    _ft_active = bool(ft and ft.strip())
                    st.info("Found **0** profiles")
                    if _ft_active:
                        st.caption(
                            "Full-text search returned 0 matches. Try a broader query "
                            "(fewer terms, OR instead of AND), or remove the full-text "
                            "search and rely on column filters."
                        )
                    elif has_column_filters:
                        st.caption(
                            "Server-side filters returned 0 matches. Try relaxing one "
                            "filter (e.g. broaden Current Company or Current Title)."
                        )
                    else:
                        st.caption(
                            "No profiles in the database yet. Run a search or load profiles first."
                        )
                else:
                    st.info("No profiles in database yet")

                # Re-enrich profile section
                st.divider()
                st.markdown("#### Re-Enrich Profile")
                st.caption("Refresh profile data from Crustdata API (useful for stale/incomplete profiles)")

                reenrich_url = st.text_input(
                    "LinkedIn URL",
                    key="reenrich_url",
                    placeholder="https://www.linkedin.com/in/username"
                )

                if st.button("Re-Enrich Profile", key="reenrich_btn", type="primary"):
                    if not reenrich_url:
                        st.warning("Please enter a LinkedIn URL")
                    elif not api_key or api_key == "YOUR_CRUSTDATA_API_KEY_HERE":
                        st.error("Crustdata API key not configured")
                    else:
                        with st.spinner("Fetching fresh data from Crustdata..."):
                            # Cheap v2025-11-01 sync enrich (1 credit base) — NOT the
                            # legacy GET /screener/person/enrich (3 credits) this
                            # button used before 2026-08-04. Also the first time this
                            # button logs usage at all (it never did before).
                            _reenrich_tracker = get_usage_tracker()
                            _reenrich_start = time.time()
                            try:
                                result = sync_enrich_profile(reenrich_url, api_key=api_key)
                                _reenrich_elapsed_ms = int((time.time() - _reenrich_start) * 1000)

                                if not result:
                                    if _reenrich_tracker:
                                        _reenrich_tracker.log_crustdata_sync_enrich(
                                            requested=1, fulfilled=0, status='success',
                                            response_time_ms=_reenrich_elapsed_ms,
                                        )
                                    st.error("Profile not found on Crustdata")
                                else:
                                    if _reenrich_tracker:
                                        _reenrich_tracker.log_crustdata_sync_enrich(
                                            requested=1, fulfilled=1, status='success',
                                            response_time_ms=_reenrich_elapsed_ms,
                                        )

                                    # Extract LinkedIn URL from response
                                    linkedin_url = result.get('linkedin_flagship_url') or result.get('linkedin_url')
                                    if linkedin_url:
                                        # Save to database
                                        saved = save_enriched_profile(db_client, linkedin_url, result, reenrich_url)
                                        if saved:
                                            st.success(f"Profile re-enriched and saved!")

                                            # Show what was updated
                                            name = result.get('name', 'Unknown')
                                            all_titles = result.get('all_titles', [])
                                            all_employers = result.get('all_employers', [])
                                            skills = result.get('skills', [])

                                            st.markdown(f"**{name}**")
                                            st.markdown(f"**Titles:** {', '.join(all_titles[:5]) if all_titles else 'N/A'}")
                                            st.markdown(f"**Employers:** {', '.join(all_employers[:5]) if all_employers else 'N/A'}")
                                            st.markdown(f"**Skills:** {', '.join(skills[:10]) if skills else 'N/A'}...")

                                            # Show work history
                                            current_employers = result.get('current_employers', [])
                                            past_employers = result.get('past_employers', [])
                                            all_positions = current_employers + past_employers
                                            if all_positions:
                                                with st.expander("Full Work History", expanded=True):
                                                    for emp in all_positions[:10]:
                                                        title = emp.get('employee_title', '')
                                                        company = emp.get('employer_name', '')
                                                        start = emp.get('start_date', '')[:7] if emp.get('start_date') else '?'
                                                        end = emp.get('end_date', '')[:7] if emp.get('end_date') else 'Present'
                                                        st.markdown(f"- **{title}** at {company} ({start} - {end})")
                                        else:
                                            st.error("Failed to save to database")
                                    else:
                                        st.error("Could not extract LinkedIn URL from response")
                            except Exception as e:
                                _reenrich_elapsed_ms = int((time.time() - _reenrich_start) * 1000)
                                if _reenrich_tracker:
                                    _reenrich_tracker.log_crustdata_sync_enrich(
                                        requested=1, fulfilled=0, status='error',
                                        error_message=str(e)[:200],
                                        response_time_ms=_reenrich_elapsed_ms,
                                    )
                                st.error(f"Error: {e}")

        except Exception as e:
            st.error(f"Database error: {e}")

# ========== TAB 7: Find Similar Profiles (semantic search via embeddings) ==========
with tab_similar:
    st.markdown("### Find Similar Profiles")
    st.caption(
        "Paste a LinkedIn URL. We'll find profiles in our database whose "
        "career, skills and background are semantically close — even if "
        "the exact keywords differ."
    )

    if not HAS_DATABASE:
        st.warning("Supabase not configured — semantic search needs the profiles database.")
    else:
        sim_url = st.text_input(
            "LinkedIn URL",
            key="similar_input_url",
            placeholder="https://www.linkedin.com/in/jane-doe/",
        )
        c1, c2 = st.columns([1, 1])
        with c1:
            sim_top_n = st.number_input(
                "How many matches", min_value=5, max_value=100, value=20, step=5,
                key="similar_top_n",
            )
        with c2:
            sim_min_score = st.slider(
                "Minimum similarity", min_value=0.0, max_value=1.0,
                value=0.30, step=0.05,
                key="similar_min_score",
                help="0 = show everything ranked; 0.5 = clearly related; 0.8 = near-duplicates",
            )

        # Location filter — pick a country and/or type a city. You type one
        # plain word ("Israel" / "Tel Aviv"); the app expands it to every
        # related term (cities, spellings, metro areas) behind the scenes.
        _SIM_COUNTRIES = [
            "", "Argentina", "Australia", "Bangladesh", "Belgium", "Brazil",
            "Canada", "Chile", "China", "Colombia", "Denmark", "Ecuador",
            "Egypt", "France", "Germany", "India", "Indonesia", "Iran",
            "Ireland", "Israel", "Italy", "Japan", "Kenya", "Malaysia",
            "Mexico", "Morocco", "Netherlands", "New Zealand", "Nigeria",
            "Norway", "Pakistan", "Peru", "Philippines", "Poland", "Portugal",
            "Romania", "Russia", "Saudi Arabia", "Singapore", "South Africa",
            "South Korea", "Spain", "Sweden", "Switzerland", "Thailand",
            "Turkey", "Ukraine", "United Arab Emirates", "United Kingdom",
            "United States", "Venezuela",
        ]
        cl1, cl2 = st.columns([1, 1])
        with cl1:
            sim_country = st.selectbox(
                "Country (optional)",
                options=_SIM_COUNTRIES,
                index=0,
                key="similar_country",
                help="Limit matches to this country. For Israel the app also "
                     "searches all its cities automatically; other countries "
                     "match on the country name (use the City box for a "
                     "specific city anywhere).",
            )
        with cl2:
            sim_city = st.text_input(
                "City / area (optional)",
                key="similar_city",
                placeholder="e.g., Tel Aviv",
                help="Type one city. For major cities the app also matches "
                     "other spellings and nearby areas; smaller places match "
                     "the name you type.",
            )

        sim_button = st.button("Find similar", type="primary", key="similar_run_btn")

        if sim_button:
            if not sim_url.strip():
                st.warning("Paste a LinkedIn URL first.")
            else:
                try:
                    from similar_profiles import search_similar, SimilarProfileError
                    from openai import OpenAI as _OpenAIClient

                    _openai_key = load_openai_key()
                    _crustdata_key = api_key  # loaded at module top via load_api_key()
                    if not _openai_key:
                        st.error("OpenAI API key not configured. Add 'openai_api_key' to config.json.")
                    else:
                        # Spinner copy depends on whether we *might* need to enrich.
                        # The auto-enrich path only fires when the URL isn't in the DB.
                        with st.spinner("Looking up profile, enriching if needed, then searching…"):
                            result = search_similar(
                                db_client=get_supabase_client(),
                                openai_client=_OpenAIClient(api_key=_openai_key),
                                linkedin_url=sim_url.strip(),
                                match_count=int(sim_top_n),
                                min_similarity=float(sim_min_score),
                                exclude_self=True,
                                crustdata_key=_crustdata_key or None,
                                country=(sim_country or None),
                                city=(sim_city.strip() or None),
                            )

                        st.session_state["similar_last_result"] = result
                        source = result.get("source")
                        if source == "enriched":
                            log_similar_profiles_crustdata_usage(get_usage_tracker(), source=source)
                            st.info(
                                "This profile wasn't in our database. We enriched it via "
                                "Crustdata (~1 credit), saved it, and embedded it. Future "
                                "searches for the same URL are free."
                            )
                        elif source == "embedded":
                            st.info(
                                "This profile was in our database but had no embedding yet — "
                                "we generated one. Future searches for it are instant."
                            )

                except SimilarProfileError as e:
                    log_similar_profiles_crustdata_usage(get_usage_tracker(), error=e)
                    st.warning(str(e))
                except Exception as e:
                    st.error(f"Similar search failed: {e}")

        # Render the most recent result (survives reruns)
        result = st.session_state.get("similar_last_result")
        if result:
            qp = result.get("query_profile") or {}
            matches = result.get("matches") or []
            st.markdown("---")
            st.markdown(
                f"**Searching from:** {qp.get('name') or qp.get('linkedin_url')} "
                f"— {qp.get('current_title') or '?'} @ {qp.get('current_company') or '?'}"
            )

            if not matches:
                _loc_active = bool(st.session_state.get("similar_country")) or bool((st.session_state.get("similar_city") or "").strip())
                if _loc_active:
                    st.info(
                        "No matches in that location above the minimum-similarity "
                        "threshold. Try lowering the slider, widening the location "
                        "(clear the city, or pick the country instead), or removing "
                        "the location filter."
                    )
                else:
                    st.info("No similar profiles found above the minimum-similarity threshold. Try lowering the slider.")
            else:
                import pandas as _pd
                df = _pd.DataFrame([
                    {
                        "Similarity": round(m.get("similarity", 0.0), 3),
                        "Name": m.get("name"),
                        "Current Title": m.get("current_title"),
                        "Current Company": m.get("current_company"),
                        "Location": m.get("location"),
                        "Email": m.get("email"),
                        "LinkedIn": m.get("linkedin_url"),
                    }
                    for m in matches
                ])
                df.insert(0, 'Remove', False)
                import hashlib as _hl
                _sim_key = _hl.md5(",".join(df['LinkedIn'].dropna().astype(str).tolist()).encode()).hexdigest()[:8]
                _edited_sim_df = st.data_editor(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    disabled=[c for c in df.columns if c != 'Remove'],
                    key=f"sim_editor_{_sim_key}",
                    column_config={
                        "Remove": st.column_config.CheckboxColumn("Remove"),
                        "LinkedIn": st.column_config.LinkColumn("LinkedIn"),
                        "Similarity": st.column_config.ProgressColumn(
                            "Similarity", min_value=0.0, max_value=1.0, format="%.3f"
                        ),
                    },
                )
                _to_remove_sim = _edited_sim_df[_edited_sim_df['Remove'] == True]
                if len(_to_remove_sim) > 0:
                    _n_sim = len(_to_remove_sim)
                    if st.button(
                        f"Remove {_n_sim} selected from database",
                        key="sim_remove_selected_btn",
                        type="secondary",
                    ):
                        from db import delete_profile as _del_prof_sim
                        _sim_db = get_supabase_client()
                        _removed_sim = 0
                        _failed_sim = []
                        _deleted_sim_urls = set()
                        for _url in _to_remove_sim['LinkedIn'].dropna():
                            if _sim_db and _del_prof_sim(_sim_db, str(_url)):
                                _removed_sim += 1
                                _deleted_sim_urls.add(str(_url))
                            else:
                                _failed_sim.append(_url)
                        if _deleted_sim_urls:
                            if 'similar_last_result' in st.session_state:
                                _sr = st.session_state['similar_last_result']
                                _sr['matches'] = [
                                    m for m in (_sr.get('matches') or [])
                                    if m.get('linkedin_url') not in _deleted_sim_urls
                                ]
                            if 'db_search_results' in st.session_state:
                                st.session_state['db_search_results'] = [
                                    p for p in st.session_state['db_search_results']
                                    if p.get('linkedin_url') not in _deleted_sim_urls
                                ]
                            _cached_all_profiles.clear()
                        if _removed_sim:
                            st.success(f"Removed {_removed_sim} profile{'s' if _removed_sim > 1 else ''} from the database.")
                        if _failed_sim:
                            st.warning(f"Could not remove: {', '.join(str(u) for u in _failed_sim)}")
                        if _removed_sim:
                            st.rerun()
                st.download_button(
                    "Download as CSV",
                    df.drop(columns=['Remove'], errors='ignore').to_csv(index=False).encode("utf-8"),
                    file_name="similar_profiles.csv",
                    mime="text/csv",
                )

# ========== TAB 8: Usage ==========
with tab_usage:
    st.markdown("### API Usage Dashboard")
    st.caption("Track API consumption across all providers")

    if not HAS_DATABASE:
        st.warning("Database module not available. Usage tracking requires Supabase.")
    else:
        try:
            db_client = _get_db_client()
            if not db_client:
                st.warning("Supabase not configured or cannot connect. Check your credentials.")
            else:
                # Date range selector
                ucol1, ucol2 = st.columns([2, 1])
                with ucol1:
                    date_range = st.selectbox(
                        "Date Range",
                        ["Today", "7 Days", "30 Days", "All Time"],
                        index=1,
                        key="usage_date_range"
                    )
                with ucol2:
                    load_usage = st.button("Load Usage Data", type="primary", key="load_usage_btn")

                # Map selection to days
                days_map = {"Today": 1, "7 Days": 7, "30 Days": 30, "All Time": None}
                selected_days = days_map[date_range]

                # Fetch usage summary only when requested
                if load_usage:
                    st.session_state['usage_summary'] = get_usage_summary(db_client, days=selected_days)
                    st.session_state['usage_selected_days'] = selected_days
                summary = st.session_state.get('usage_summary', {})

                if not summary:
                    st.info("Click **Load Usage Data** to view API consumption metrics.")
                else:
                    # Display metrics in columns
                    st.markdown("#### Provider Summary")
                    metric_cols = st.columns(4)

                    with metric_cols[0]:
                        crustdata = summary.get('crustdata', {})
                        crust_cost = crustdata.get('cost_usd', 0)
                        st.metric(
                            "Crustdata",
                            f"${crust_cost:.2f}",
                            help="$1,500 for 150K credits (3 credits/profile, $0.03/profile)"
                        )
                        st.caption(f"{int(crustdata.get('credits', 0)):,} credits | {crustdata.get('requests', 0)} requests")

                    with metric_cols[1]:
                        salesql = summary.get('salesql', {})
                        st.metric(
                            "SalesQL",
                            f"{int(salesql.get('lookups', 0)):,} lookups",
                            help="5,000/day limit"
                        )
                        st.caption(f"{salesql.get('requests', 0)} requests")

                    with metric_cols[2]:
                        openai = summary.get('openai', {})
                        cost = openai.get('cost_usd', 0)
                        st.metric(
                            "OpenAI",
                            f"${cost:.4f}",
                            help="gpt-4.1-mini: $0.40/1M input, $1.60/1M output"
                        )
                        tokens_in = openai.get('tokens_input', 0)
                        tokens_out = openai.get('tokens_output', 0)
                        st.caption(f"{tokens_in:,} in / {tokens_out:,} out tokens")

                    with metric_cols[3]:
                        phantombuster = summary.get('phantombuster', {})
                        st.metric(
                            "PhantomBuster",
                            f"{phantombuster.get('runs', 0)} runs",
                            help="Scraping operations"
                        )
                        st.caption(f"{phantombuster.get('profiles_scraped', 0):,} profiles scraped")

                    st.divider()

                    # Charts section
                    usage_days = st.session_state.get('usage_selected_days')
                    if HAS_PLOTLY and usage_days:
                        st.markdown("#### Usage Over Time")

                        # Fetch daily usage data (only when Load was clicked — stored in session)
                        if load_usage:
                            st.session_state['usage_daily_data'] = get_usage_by_date(db_client, days=usage_days or 365)
                        daily_data = st.session_state.get('usage_daily_data')

                        if daily_data:
                            df_daily = pd.DataFrame(daily_data)

                            # Line chart for usage over time
                            fig_line = go.Figure()

                            fig_line.add_trace(go.Scatter(
                                x=df_daily['date'],
                                y=df_daily['crustdata'],
                                mode='lines+markers',
                                name='Crustdata (credits)',
                                line=dict(color='#615fff')
                            ))

                            fig_line.add_trace(go.Scatter(
                                x=df_daily['date'],
                                y=df_daily['salesql'],
                                mode='lines+markers',
                                name='SalesQL (lookups)',
                                line=dict(color='#38bdf8')
                            ))

                            fig_line.add_trace(go.Scatter(
                                x=df_daily['date'],
                                y=df_daily['phantombuster'],
                                mode='lines+markers',
                                name='PhantomBuster (runs)',
                                line=dict(color='#a78bfa')
                            ))

                            fig_line.update_layout(
                                title='API Usage by Day',
                                xaxis_title='Date',
                                yaxis_title='Count',
                                hovermode='x unified',
                                legend=dict(orientation='h', yanchor='bottom', y=1.02),
                                height=350,
                                paper_bgcolor='rgba(0,0,0,0)',
                                plot_bgcolor='rgba(0,0,0,0)',
                                font=dict(color='#e2e8f0'),
                            )

                            st.plotly_chart(fig_line, width="stretch")

                            # Cost chart (OpenAI)
                            if df_daily['openai'].sum() > 0:
                                fig_cost = px.area(
                                    df_daily,
                                    x='date',
                                    y='openai',
                                    title='OpenAI Cost by Day ($)',
                                    labels={'openai': 'Cost (USD)', 'date': 'Date'}
                                )
                                fig_cost.update_traces(fill='tozeroy', line_color='#34d399')
                                fig_cost.update_layout(height=250, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#e2e8f0'))
                                st.plotly_chart(fig_cost, width="stretch")

                        else:
                            st.info("No usage data available for the selected period")

                        # Pie chart for cost breakdown
                        st.markdown("#### Cost Breakdown")
                        cost_data = {
                            'Provider': ['Crustdata', 'OpenAI'],
                            'Cost': [
                                summary.get('crustdata', {}).get('cost_usd', 0),
                                summary.get('openai', {}).get('cost_usd', 0),
                            ]
                        }

                        if sum(cost_data['Cost']) > 0:
                            fig_pie = px.pie(
                                pd.DataFrame(cost_data),
                                values='Cost',
                                names='Provider',
                                title='Cost Distribution (USD)',
                                color_discrete_sequence=['#34d399', '#615fff', '#38bdf8', '#a78bfa']
                            )
                            fig_pie.update_layout(height=300, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#e2e8f0'))
                            st.plotly_chart(fig_pie, width="stretch")
                        else:
                            st.info("No cost data to display")

                    elif not HAS_PLOTLY:
                        st.info("Install plotly for charts: `pip install plotly>=5.18.0`")

                    st.divider()

                    # Detailed logs table
                    st.markdown("#### Detailed Logs")

                    # Filter options
                    log_cols = st.columns([2, 2, 1])
                    with log_cols[0]:
                        provider_filter = st.selectbox(
                            "Provider",
                            ["All", "crustdata", "salesql", "openai", "phantombuster"],
                            key="usage_provider_filter"
                        )
                    with log_cols[1]:
                        log_limit = st.selectbox(
                            "Show",
                            [25, 50, 100, 200],
                            index=1,
                            key="usage_log_limit"
                        )
                    with log_cols[2]:
                        load_logs = st.button("Load Logs", key="usage_load_logs")

                    if load_logs:
                        st.session_state['usage_logs'] = get_usage_logs(
                            db_client,
                            provider=provider_filter if provider_filter != "All" else None,
                            days=selected_days,
                            limit=log_limit
                        )

                    logs = st.session_state.get('usage_logs')

                    if logs:
                        # Convert to DataFrame for display
                        logs_df = pd.DataFrame(logs)

                        # Select and rename columns for display
                        display_cols = ['created_at', 'provider', 'operation', 'request_count',
                                        'credits_used', 'tokens_input', 'tokens_output', 'cost_usd',
                                        'status', 'response_time_ms']
                        available_cols = [c for c in display_cols if c in logs_df.columns]

                        st.dataframe(
                            logs_df[available_cols],
                            width="stretch",
                            hide_index=True,
                            column_config={
                                "created_at": st.column_config.DatetimeColumn("Time", format="YYYY-MM-DD HH:mm"),
                                "provider": st.column_config.TextColumn("Provider"),
                                "operation": st.column_config.TextColumn("Operation"),
                                "request_count": st.column_config.NumberColumn("Requests", format="%d"),
                                "credits_used": st.column_config.NumberColumn("Credits", format="%.1f"),
                                "tokens_input": st.column_config.NumberColumn("Tokens In", format="%d"),
                                "tokens_output": st.column_config.NumberColumn("Tokens Out", format="%d"),
                                "cost_usd": st.column_config.NumberColumn("Cost ($)", format="%.6f"),
                                "status": st.column_config.TextColumn("Status"),
                                "response_time_ms": st.column_config.NumberColumn("Time (ms)", format="%d"),
                            }
                        )

                        # Export option
                        st.download_button(
                            "Download Logs (CSV)",
                            logs_df.to_csv(index=False),
                            f"api_usage_logs_{date_range.lower().replace(' ', '_')}.csv",
                            "text/csv"
                        )
                    elif load_logs:
                        st.info("No usage logs found for the selected filters")

        except Exception as e:
            st.error(f"Usage dashboard error: {e}")
