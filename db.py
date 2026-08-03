"""
Supabase Database Module for LinkedIn Enricher (v2 - Crustdata Only)

Stores only Crustdata-enriched profiles. PhantomBuster data stays in UI session state.
Uses REST API directly - no supabase package required.
"""

import os
import json
import re
import hashlib
import requests
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
import pandas as pd

from normalizers import normalize_linkedin_url, pick_current_employer, _parse_start_date_sort_key

# Refresh threshold for re-enriching stale profiles
ENRICHMENT_REFRESH_MONTHS = 3

SCREENING_COLUMNS = (
    'screening_score',
    'screening_fit_level',
    'screening_summary',
    'screening_reasoning',
    'screening_notes',
    'screened_at',
)

DEFAULT_SCREENING_SOURCE_PROJECT = 'sourcingx'
DEFAULT_SCREENING_JD_HASH = 'default'


class SupabaseClient:
    """Simple Supabase REST API client."""

    def __init__(self, url: str, key: str):
        self.url = url.rstrip('/')
        self.key = key
        self.headers = {
            'apikey': key,
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'Prefer': 'return=representation'
        }

    def _request(self, method: str, endpoint: str, params: dict = None, json_data: dict = None) -> dict:
        """Make a request to Supabase REST API."""
        url = f"{self.url}/rest/v1/{endpoint}"
        response = requests.request(
            method,
            url,
            headers=self.headers,
            params=params,
            json=json_data,
            timeout=90
        )
        response.raise_for_status()
        if response.text:
            return response.json()
        return {}

    def select(self, table: str, columns: str = '*', filters: dict = None, limit: int = 5000,
               order_by: str = None, cursor_column: str = None, cursor_value: str = None) -> list:
        """Select rows from a table. Auto-paginates past Supabase 1000-row server limit.
        Default limit reduced to 5000 to prevent OOM on large datasets.

        For large datasets (2000+), use keyset pagination for better performance:
            order_by='enriched_at.desc', cursor_column='enriched_at'
        This avoids the O(n) scan-and-skip cost of offset pagination.
        """
        PAGE_SIZE = 1000
        params = {'select': columns}
        if filters:
            for key, value in filters.items():
                params[key] = value
        if order_by:
            params['order'] = order_by

        # Small requests don't need pagination
        if limit <= PAGE_SIZE:
            params['limit'] = limit
            if cursor_column and cursor_value:
                # Apply cursor filter for keyset pagination
                params[cursor_column] = f'lt.{cursor_value}' if order_by and '.desc' in order_by else f'gt.{cursor_value}'
            return self._request('GET', table, params=params)

        # Keyset pagination (faster for large datasets — no offset scan)
        if cursor_column and order_by:
            all_results = []
            current_cursor = cursor_value  # None on first page
            remaining = limit
            while remaining > 0:
                page_limit = min(PAGE_SIZE, remaining)
                page_params = {**params, 'limit': page_limit}
                if current_cursor:
                    is_desc = '.desc' in order_by
                    page_params[cursor_column] = f'lt.{current_cursor}' if is_desc else f'gt.{current_cursor}'
                page = self._request('GET', table, params=page_params)
                if not page:
                    break
                all_results.extend(page)
                remaining -= len(page)
                if len(page) < page_limit:
                    break
                # Move cursor to last row's value
                last_row = page[-1]
                current_cursor = last_row.get(cursor_column)
                if not current_cursor:
                    break
            return all_results

        # Offset pagination (fallback for queries without cursor)
        all_results = []
        offset = 0
        while offset < limit:
            page_limit = min(PAGE_SIZE, limit - offset)
            page_params = {**params, 'limit': page_limit, 'offset': offset}
            page = self._request('GET', table, params=page_params)
            if not page:
                break
            all_results.extend(page)
            if len(page) < page_limit:
                break
            offset += PAGE_SIZE
        return all_results

    def insert(self, table: str, data: dict) -> list:
        """Insert a row into a table."""
        return self._request('POST', table, json_data=data)

    def upsert(self, table: str, data: dict, on_conflict: str = None) -> list:
        """Upsert (insert or update) a row."""
        headers = self.headers.copy()
        if on_conflict:
            headers['Prefer'] = f'resolution=merge-duplicates,return=representation'
        url = f"{self.url}/rest/v1/{table}"
        params = {}
        if on_conflict:
            params['on_conflict'] = on_conflict
        # Pre-serialize JSON to handle NaN values
        json_str = json.dumps(data, allow_nan=True)
        json_str = json_str.replace(': NaN', ': null').replace(':NaN', ':null')
        json_str = json_str.replace(': Infinity', ': null').replace(':Infinity', ':null')
        json_str = json_str.replace(': -Infinity', ': null').replace(':-Infinity', ':null')
        response = requests.post(url, headers=headers, params=params, data=json_str, timeout=30)
        if response.status_code >= 400:
            error_msg = f"{response.status_code}: {response.text}"
            raise requests.HTTPError(error_msg)
        if response.text:
            return response.json()
        return []

    def upsert_batch(self, table: str, rows: list, on_conflict: str = None) -> list:
        """Upsert multiple rows in a single request (much faster than individual upserts)."""
        if not rows:
            return []
        headers = self.headers.copy()
        if on_conflict:
            headers['Prefer'] = f'resolution=merge-duplicates,return=representation'
        url = f"{self.url}/rest/v1/{table}"
        params = {}
        if on_conflict:
            params['on_conflict'] = on_conflict
        json_str = json.dumps(rows, allow_nan=True)
        json_str = json_str.replace(': NaN', ': null').replace(':NaN', ':null')
        json_str = json_str.replace(': Infinity', ': null').replace(':Infinity', ':null')
        json_str = json_str.replace(': -Infinity', ': null').replace(':-Infinity', ':null')
        response = requests.post(url, headers=headers, params=params, data=json_str, timeout=60)
        if response.status_code >= 400:
            error_msg = f"{response.status_code}: {response.text}"
            raise requests.HTTPError(error_msg)
        if response.text:
            return response.json()
        return []

    def rpc(self, function: str, params: dict = None, schema: str = None) -> dict:
        """Call a PostgREST RPC function: POST /rest/v1/rpc/{function}.

        `schema` selects a non-default exposed schema via PostgREST's
        `Content-Profile` header. Only `public` and `graphql_public` are
        exposed on this project (ciyyvbzblogtbwabhbmh) — `sourcing_core` and
        `sourcing_raw` are NOT reachable directly (406 PGRST106 "Invalid
        schema"). `public.sourcingx_upsert_profiles` (migrations/025) is the
        project-scoped public wrapper that reaches `sourcing_core.upsert_profile`
        indirectly — see save_enriched_profile[s_bulk] below.

        30s HTTP timeout — the RPC functions this project calls set their own
        `statement_timeout` comfortably under this, so a genuinely slow call
        fails with a clean Postgres statement-timeout error the caller can
        see, rather than this client giving up first while the write may
        still be committing server-side.
        """
        headers = self.headers.copy()
        if schema:
            headers['Content-Profile'] = schema
        url = f"{self.url}/rest/v1/rpc/{function}"
        # Sanitize NaN/Infinity before serializing (matches upsert/upsert_batch
        # below) — raw_data can legitimately contain NaN from Crustdata, and
        # posting it unsanitized lets Python's json encoder emit bare
        # NaN/Infinity, which PostgREST rejects, failing the whole batch.
        json_str = json.dumps(params or {}, allow_nan=True)
        json_str = json_str.replace(': NaN', ': null').replace(':NaN', ':null')
        json_str = json_str.replace(': Infinity', ': null').replace(':Infinity', ':null')
        json_str = json_str.replace(': -Infinity', ': null').replace(':-Infinity', ':null')
        response = requests.post(url, headers=headers, data=json_str, timeout=30)
        if response.status_code >= 400:
            raise requests.HTTPError(f"{response.status_code}: {response.text}")
        if response.text:
            return response.json()
        return None

    def update(self, table: str, data: dict, filters: dict) -> list:
        """Update rows matching filters."""
        params = {}
        for key, value in filters.items():
            params[key] = f'eq.{value}'
        return self._request('PATCH', table, params=params, json_data=data)

    def delete(self, table: str, filters: dict) -> list:
        """Delete rows matching filters."""
        params = {}
        for key, value in filters.items():
            params[key] = f'eq.{value}'
        return self._request('DELETE', table, params=params)

    def count(self, table: str, filters: dict = None) -> int:
        """Count rows in a table."""
        headers = self.headers.copy()
        headers['Prefer'] = 'count=exact'
        headers['Range-Unit'] = 'items'
        url = f"{self.url}/rest/v1/{table}"
        params = {'select': 'id'}
        if filters:
            for key, value in filters.items():
                params[key] = value
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        content_range = response.headers.get('Content-Range', '*/0')
        total = content_range.split('/')[-1]
        return int(total) if total != '*' else 0


def get_supabase_client() -> Optional[SupabaseClient]:
    """Get Supabase client from config.json, Streamlit secrets, or environment.

    Credentials are .strip()ed so a stray trailing newline (common when a
    secret is set via ``gh secret set --body``) doesn't poison the HTTP header
    layer with errors like "Invalid leading whitespace, reserved character(s),
    or return character(s) in header value".
    """
    url = None
    key = None

    # Try config.json first (local development)
    try:
        config_path = Path(__file__).parent / 'config.json'
        if config_path.exists():
            with open(config_path, 'r') as f:
                config = json.load(f)
                url = config.get('supabase_url')
                key = config.get('supabase_key')
    except Exception:
        pass

    # Try Streamlit secrets (cloud deployment)
    if not url or not key:
        try:
            import streamlit as st
            if hasattr(st, 'secrets'):
                url = url or st.secrets.get('supabase_url')
                key = key or st.secrets.get('supabase_key')
        except Exception:
            pass

    # Fall back to environment variables
    if not url:
        url = os.environ.get('SUPABASE_URL')
    if not key:
        key = os.environ.get('SUPABASE_KEY')

    # Defensive strip: remove stray whitespace/newlines from any source so the
    # values are safe to use as URL + HTTP header below.
    if url:
        url = url.strip()
    if key:
        key = key.strip()

    if url and key:
        return SupabaseClient(url, key)
    return None


# ============================================================================
# PROFILE OPERATIONS (Crustdata Enriched Profiles Only)
# ============================================================================

def _get_existing_original_urls(client: SupabaseClient, linkedin_url: str) -> list:
    """Get existing original_urls array for a profile (for append operation).

    Returns empty list if profile doesn't exist or has no original_urls.
    Backward compatible: Works before and after original_urls migration.
    """
    try:
        # Try with original_urls array first (post-migration)
        try:
            result = client.select(
                'profiles',
                'original_urls,original_url',
                filters={'linkedin_url': f'eq.{linkedin_url}'},
                limit=1
            )
        except Exception:
            # Fall back to original_url only (pre-migration)
            result = client.select(
                'profiles',
                'original_url',
                filters={'linkedin_url': f'eq.{linkedin_url}'},
                limit=1
            )

        if result and len(result) > 0:
            profile = result[0]
            # Get from array first, fall back to single field
            urls = profile.get('original_urls') or []
            if not urls and profile.get('original_url'):
                urls = [profile['original_url']]
            return urls if isinstance(urls, list) else []
    except Exception:
        pass
    return []


def _prepare_profile_row(linkedin_url: str, crustdata_response: dict, original_url: str = None,
                         existing_original_urls: list = None, email: str = None, email_source: str = None) -> dict:
    """Prepare a profile dict for DB upsert (pure function, no DB calls).

    Extracts indexed fields from the Crustdata response and builds the row dict.

    Args:
        linkedin_url: Normalized LinkedIn URL (primary key)
        crustdata_response: Raw response from Crustdata API
        original_url: The original input URL (for matching with loaded data)
        existing_original_urls: Existing original_urls from DB (for append)
        email: Optional email to include in the row
        email_source: Optional email source (e.g., 'csv')

    Returns:
        Dict ready for upsert
    """
    cd = crustdata_response or {}

    # Extract name for indexed column
    name = cd.get('name') or ''
    if not name:
        first_name = cd.get('first_name') or ''
        last_name = cd.get('last_name') or ''
        name = f"{first_name} {last_name}".strip()

    # Extract location. Fall back to `region` when Crustdata returns no explicit
    # `location` — otherwise profiles whose location only comes back as a region
    # (e.g. location=null, region="Tel Aviv District, Israel") get saved with a
    # blank location and silently vanish from every location-gated search. This
    # mirrors the existing fallback in the normalizer. SHARED save path — keep
    # identical across all sibling pipelines (this is the canonical copy).
    location = cd.get('location') or cd.get('region') or ''

    # Extract only title/company for indexed filtering
    current_title = None
    current_company = None

    # Try current_employers first (Crustdata format) — pick most recent
    emp = pick_current_employer(cd.get('current_employers'))
    current_start_date = None
    current_years_at_company = None
    if emp:
        current_title = emp.get('employee_title') or emp.get('title')
        current_company = emp.get('employer_name') or emp.get('company_name') or emp.get('name')
        # Tenure-at-current-company: persist alongside the indexed fields so
        # Filter+ can read it without raw_data. Parsed via the same helper the
        # normalizer + backfill use, so all three paths produce identical
        # values.
        raw_start = emp.get('start_date')
        if raw_start is not None and str(raw_start).strip():
            from datetime import datetime as _dt
            parseable, dt = _parse_start_date_sort_key(raw_start)
            if parseable:
                current_start_date = dt.isoformat()
                current_years_at_company = round((_dt.now() - dt).days / 365.25, 1)
            else:
                # Sentinel: row processed, value unknowable. Filter+ treats
                # this the same as NULL (does not drop the row).
                current_years_at_company = -1.0
    else:
        # No current employer at all — same sentinel so we don't keep
        # re-checking via the IS NULL backfill path.
        current_years_at_company = -1.0

    # Fallback: extract from headline (e.g., "CEO at Company")
    if not current_title or not current_company:
        headline = cd.get('headline', '')
        if headline and ' at ' in headline:
            parts = headline.split(' at ', 1)
            if not current_title:
                current_title = parts[0].strip()
            if not current_company and len(parts) > 1:
                current_company = parts[1].split('/')[0].strip()

    # Extract pre-flattened arrays from Crustdata.
    # Enrich endpoint: all_employers / all_titles / all_schools are flat string lists.
    # Search endpoint (compact=false): all_employers is a list of objects (each has 'name',
    # 'title', etc.); all_titles may be empty; education uses education_background objects.
    # Both paths must produce identical indexed string arrays in the DB row.

    def _extract_employer_arrays(raw_list):
        """Return (names, titles) from a list that is either strings or employer objects."""
        names, titles = [], []
        for item in (raw_list or []):
            if isinstance(item, dict):
                n = item.get('name') or item.get('employer_name') or ''
                t = item.get('title') or item.get('employee_title') or ''
                if n:
                    names.append(n)
                if t:
                    titles.append(t)
            elif isinstance(item, str) and item:
                names.append(item)
        return names, titles

    def _extract_school_array(raw_list):
        """Return school names from a list that is either strings or education objects."""
        schools = []
        for item in (raw_list or []):
            if isinstance(item, dict):
                s = item.get('institute_name') or item.get('school') or item.get('name') or ''
                if s:
                    schools.append(s)
            elif isinstance(item, str) and item:
                schools.append(item)
        return schools

    raw_all_employers = cd.get('all_employers') or []
    raw_all_titles = cd.get('all_titles') or []
    raw_all_schools = cd.get('all_schools') or []
    skills = cd.get('skills') or []

    # all_employers / all_titles: handle both flat strings and employer objects
    all_employers, employers_titles = _extract_employer_arrays(raw_all_employers)
    # all_titles may be a flat list (enrich) or empty (search — titles live in all_employers objects)
    all_titles = [str(x) for x in raw_all_titles if x] if raw_all_titles else employers_titles

    # all_schools: flat strings (enrich) or education objects (search via education_background)
    all_schools = _extract_school_array(raw_all_schools) or _extract_school_array(cd.get('education_background'))

    # past_employers fallback: if all_employers still empty, try the search 'past_employers' key
    if not all_employers:
        fallback_names, fallback_titles = _extract_employer_arrays(cd.get('past_employers'))
        all_employers = fallback_names
        if not all_titles:
            all_titles = fallback_titles

    skills = [str(x) for x in skills if x] if isinstance(skills, list) else []

    now = datetime.utcnow().isoformat()

    # Build original_urls array (merge existing + new)
    original_urls = list(existing_original_urls or [])
    if original_url and original_url not in original_urls:
        original_urls.append(original_url)

    # Data to save - indexed fields + raw_data for everything else
    data = {
        'linkedin_url': linkedin_url,
        'original_url': original_url,  # Latest URL (backward compatibility)
        'raw_data': crustdata_response,
        'name': name if name else None,
        'location': location if location else None,
        'current_title': current_title,
        'current_company': current_company,
        'all_employers': all_employers if all_employers else None,
        'all_titles': all_titles if all_titles else None,
        'all_schools': all_schools if all_schools else None,
        'skills': skills if skills else None,
        # NOTE: `status` column was dropped from profiles in the shared-DB migration.
        # Use enrichment_status instead — writing `status` causes a 400 and silently
        # fails the bulk upsert.
        'enriched_at': now,
        'enrichment_status': 'enriched',
        'enrichment_attempted_at': now,
        'current_start_date': current_start_date,
        'current_years_at_company': current_years_at_company,
    }

    # Include email if provided
    if email:
        data['email'] = email
        if email_source:
            data['email_source'] = email_source

    # Include original_urls array
    if original_urls:
        data['original_urls'] = original_urls

    # Remove None values
    data = {k: v for k, v in data.items() if v is not None}

    return data


# 2026-08-03 UPDATE: save_enriched_profile and save_enriched_profiles_bulk no
# longer write via client.upsert('profiles', on_conflict='linkedin_url'). That
# call fires trg_profiles_identity_before_write on the PROPOSED row BEFORE the
# ON CONFLICT is resolved, so it unconditionally rejects re-saving anyone
# already in `profiles` — see migrations/025_sourcingx_upsert_profiles.sql for
# the full diagnosis (same bug agent-kalamata fixed 2026-08-02). They now call
# the public.sourcingx_upsert_profiles RPC, which reaches the identity-aware
# sourcing_core.upsert_profile the trigger's own error message names. THIS IS A
# SHARED-DB BEHAVIOR CHANGE: the two autopilot siblings hit the exact same
# rejection and need the equivalent fix — not yet done anywhere but this
# project. _prepare_profile_row is no longer called by
# save_enriched_profile[s_bulk] directly (a new adapter,
# _prepare_profile_payload, wraps it instead) but keeps its own direct
# callers elsewhere in this repo. _bulk_fetch_existing_original_urls has no
# other caller left after this change — left in place rather than deleted.

def _prepare_profile_payload(linkedin_url: str, crustdata_response: dict, extra_alias: str = None) -> dict:
    """Adapt _prepare_profile_row's field extraction into the payload shape
    public.sourcingx_upsert_profiles / sourcing_core.upsert_profile actually
    reads (same contract as agent-kalamata's migrations/023 — see that
    project's db.py::_prepare_profile_payload for the full field-by-field
    verification against sourcing_core.upsert_profile's own SQL body).

    Reuses _prepare_profile_row for the extraction logic so there is ONE
    place that logic lives — _prepare_profile_row itself is unchanged.

    `alias_urls`: every OTHER LinkedIn URL known for this person — Crustdata's
    obfuscated `linkedin_profile_url` (the ACoAA search-result form) and/or
    the caller-supplied `extra_alias` (the original/obfuscated URL a caller
    resolved via its own original_url_map) — normalized, deduplicated, blanks
    dropped, anything equal to the canonical `linkedin_url` excluded. The RPC
    merges these into `profiles.original_urls` AFTER resolving the real
    identity, since `sourcing_core.upsert_profile` derives the identity
    handle from the ONE url it's given and can't register a second alias
    itself.

    NOT carried through — a real, deliberate gap versus the old raw-upsert
    path, not an oversight: current_start_date, current_years_at_company
    (sourcing_core.upsert_profile's INSERT/UPDATE never reference these
    `profiles` columns), and email/email_source (same reason —
    save_enriched_profiles_bulk's email_map parameter is kept for signature
    compatibility but is a no-op through this path). enriched_at is also not
    threaded through — the RPC always passes now() server-side.
    """
    row = _prepare_profile_row(linkedin_url, crustdata_response)
    cd = crustdata_response or {}
    canonical_url = row.get('linkedin_url')

    alias_candidates = [
        cd.get('linkedin_profile_url'),
        cd.get('linkedin_url'),
        extra_alias,
    ]
    alias_urls = []
    seen_aliases = set()
    for candidate in alias_candidates:
        norm = normalize_linkedin_url(candidate) if candidate else None
        if not norm or norm == canonical_url or norm in seen_aliases:
            continue
        seen_aliases.add(norm)
        alias_urls.append(norm)

    payload = {
        'linkedin_url': canonical_url,
        'name': row.get('name'),
        'current_title': row.get('current_title'),
        'current_company': row.get('current_company'),
        'location': row.get('location'),
        'enrichment_status': row.get('enrichment_status', 'enriched'),
        'raw_data': row.get('raw_data'),
        'skills': row.get('skills'),
        'all_titles': row.get('all_titles'),
        'all_employers': row.get('all_employers'),
        'all_schools': row.get('all_schools'),
        'github_url': cd.get('github_url'),
        'normalized_title': cd.get('normalized_title'),
        'seniority_level': cd.get('seniority_level'),
        'schema_version': cd.get('schema_version'),
        'alias_urls': alias_urls if alias_urls else None,
    }
    return {k: v for k, v in payload.items() if v is not None}


def save_enriched_profile(client: SupabaseClient, linkedin_url: str, crustdata_response: dict, original_url: str = None) -> dict:
    """Save a Crustdata-enriched profile via the identity-aware
    public.sourcingx_upsert_profiles RPC (migrations/025), NOT the raw
    client.upsert('profiles', on_conflict='linkedin_url') PostgREST call
    this function used before 2026-08-03.

    Raises RuntimeError if the RPC's own per-profile verdict for this
    linkedin_url is not a success (action not in inserted/updated/noop) —
    restores the same "raises on failure" contract the old raw-upsert call
    had (it raised requests.HTTPError on a PostgREST 4xx).

    Returns the RPC's per-profile result dict on success:
    {'sent_url', 'canonical_url', 'profile_id', 'action', 'error'}.
    """
    linkedin_url = normalize_linkedin_url(linkedin_url)
    if not linkedin_url:
        raise ValueError("Valid linkedin_url is required")

    payload = _prepare_profile_payload(linkedin_url, crustdata_response, extra_alias=original_url)
    result = client.rpc('sourcingx_upsert_profiles', {'p_profiles': [payload]})
    results = (result or {}).get('results') or []
    row = results[0] if results else None
    action = (row or {}).get('action')
    if action not in ('inserted', 'updated', 'noop'):
        err = (row or {}).get('error') or f"action={action or 'no-result'}"
        raise RuntimeError(f"sourcingx_upsert_profiles did not save {linkedin_url}: {err}")
    return row


def _bulk_fetch_existing_original_urls(client: SupabaseClient, linkedin_urls: list) -> dict:
    """Bulk-fetch existing original_urls for multiple profiles in one query.

    Returns:
        Dict mapping linkedin_url -> list of existing original_urls
    """
    if not linkedin_urls:
        return {}

    result_map = {}
    batch_size = 50  # Supabase URL length limits

    for i in range(0, len(linkedin_urls), batch_size):
        batch = linkedin_urls[i:i+batch_size]
        url_list = ','.join(batch)
        try:
            # Try with original_urls column (post-migration)
            try:
                results = client.select(
                    'profiles',
                    'linkedin_url,original_urls,original_url',
                    filters={'linkedin_url': f'in.({url_list})'},
                    limit=batch_size
                )
            except Exception:
                # Fall back to original_url only (pre-migration)
                results = client.select(
                    'profiles',
                    'linkedin_url,original_url',
                    filters={'linkedin_url': f'in.({url_list})'},
                    limit=batch_size
                )

            for profile in (results or []):
                url = profile.get('linkedin_url', '')
                urls = profile.get('original_urls') or []
                if not urls and profile.get('original_url'):
                    urls = [profile['original_url']]
                result_map[url] = urls if isinstance(urls, list) else []
        except Exception:
            pass

    return result_map


def save_enriched_profiles_bulk(client: SupabaseClient, profiles: list,
                                original_url_map: dict = None,
                                email_map: dict = None,
                                batch_size: int = 100) -> dict:
    """Save multiple enriched profiles via the identity-aware
    public.sourcingx_upsert_profiles RPC (migrations/025), NOT the raw batch
    client.upsert_batch('profiles', on_conflict='linkedin_url') call this
    function used before 2026-08-03.

    `original_url_map` is honoured: `original_url_map.get(norm_url)` is
    passed to `_prepare_profile_payload` as an alias candidate for each
    profile, so a caller-supplied source URL that differs from the chosen
    canonical URL is merged into `profiles.original_urls` server-side.
    `email_map` is a no-op through this path — see `_prepare_profile_payload`'s
    docstring for the full, deliberate list of behavior this RPC path does
    not reproduce (tenure columns, email columns).

    batch_size is capped at 100 regardless of what is passed — the RPC
    itself raises if handed more than 100 profiles in one call.

    Returns:
      {'saved': int, 'errors': int, 'error_messages': [str],
       'url_map': {sent_url: {'canonical_url': str, 'action': str}}}

    INVARIANT: `saved + errors == len(profiles)` always holds. `saved` counts
    RPC actions 'inserted'/'updated'/'noop' — all three mean the row is now
    correctly stored ('noop' means the already-stored copy was at least as
    rich as the incoming one, so nothing needed to change). `errors` counts:
    a profile with no usable URL field; a URL rejected by
    normalize_linkedin_url; a profile that failed to prepare; a batch-level
    RPC call failure; any sent URL whose RPC action was
    'rejected'/'error'/'unknown_action'; and any sent URL that never got a
    result row back at all.

    `url_map` is the critical addition callers MUST use instead of the URL
    they sent: sourcing_core.upsert_profile may resolve an incoming URL to a
    DIFFERENT existing canonical profile row — writing downstream state
    keyed on the sent URL after that happens silently loses the candidate.
    """
    if not profiles:
        return {'saved': 0, 'errors': 0, 'error_messages': [], 'url_map': {}}

    if batch_size > 100 or batch_size < 1:
        print(f"[DB] save_enriched_profiles_bulk: batch_size={batch_size} is outside "
              f"the RPC's 1-100 range; clamping to 100", file=__import__('sys').stderr)
        batch_size = 100

    original_url_map = original_url_map or {}
    stats = {'saved': 0, 'errors': 0, 'error_messages': [], 'url_map': {}}

    # Step 1: Collect all linkedin_urls and normalize. Every profile that
    # doesn't make it into url_profile_pairs MUST be counted in
    # stats['errors'] here — see this function's docstring INVARIANT.
    url_profile_pairs = []
    for idx, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            stats['errors'] += 1
            stats['error_messages'].append(f"profiles[{idx}]: not a dict (got {type(profile).__name__})")
            continue
        # Enrich response: linkedin_flagship_url. Search response: flagship_profile_url.
        linkedin_url = (
            profile.get('linkedin_flagship_url')
            or profile.get('flagship_profile_url')
            or profile.get('linkedin_profile_url')
            or profile.get('linkedin_url')
        )
        ident = profile.get('name')
        ident = f"{ident} (profiles[{idx}])" if ident else f"profiles[{idx}]"
        if not linkedin_url:
            stats['errors'] += 1
            stats['error_messages'].append(f"{ident}: no usable linkedin_url field")
            continue
        norm_url = normalize_linkedin_url(linkedin_url)
        if not norm_url:
            stats['errors'] += 1
            stats['error_messages'].append(f"{linkedin_url}: normalize_linkedin_url rejected this URL")
            continue
        url_profile_pairs.append((norm_url, profile))

    # Step 2: Prepare one RPC payload object per profile.
    payloads = []
    for norm_url, profile in url_profile_pairs:
        try:
            extra_alias = original_url_map.get(norm_url)
            payloads.append(_prepare_profile_payload(norm_url, profile, extra_alias=extra_alias))
        except Exception as e:
            stats['errors'] += 1
            stats['error_messages'].append(f"{norm_url}: prepare failed: {str(e)[:100]}")

    # Step 3: call the RPC in chunks of batch_size.
    for i in range(0, len(payloads), batch_size):
        batch = payloads[i:i + batch_size]
        try:
            result = client.rpc('sourcingx_upsert_profiles', {'p_profiles': batch})
        except Exception as e:
            error_str = str(e)[:200]
            print(f"[DB] sourcingx_upsert_profiles RPC failed for a batch of "
                  f"{len(batch)}: {error_str}", file=__import__('sys').stderr)
            for row in batch:
                url = row.get('linkedin_url', 'unknown')
                stats['errors'] += 1
                stats['error_messages'].append(f"{url}: RPC call failed: {error_str}")
            continue

        results_list = (result or {}).get('results') or []
        received = (result or {}).get('received')
        if received is not None and received != len(results_list):
            print(f"[DB] WARNING: sourcingx_upsert_profiles returned "
                  f"received={received} but {len(results_list)} result rows for a "
                  f"batch of {len(batch)} — investigate", file=__import__('sys').stderr)

        seen_sent = set()
        for row in results_list:
            sent = row.get('sent_url')
            action = row.get('action')
            canonical = row.get('canonical_url')
            if sent:
                seen_sent.add(sent)
                stats['url_map'][sent] = {'canonical_url': canonical, 'action': action}
            if action in ('inserted', 'updated', 'noop'):
                stats['saved'] += 1
            else:
                stats['errors'] += 1
                err = row.get('error') or f"action={action}"
                stats['error_messages'].append(f"{sent or 'unknown'}: {err[:150]}")

        # Defence-in-depth: any URL we sent in this batch but that came back
        # with no result row at all is an unaccounted failure — never
        # silently promote it.
        for sent_row in batch:
            u = sent_row.get('linkedin_url')
            if u and u not in seen_sent:
                stats['errors'] += 1
                stats['error_messages'].append(f"{u}: no result row returned by RPC")

    return stats


def save_enriched_profiles_batch(client: SupabaseClient, profiles: list[tuple[str, dict]]) -> dict:
    """Save multiple enriched profiles in a batch (legacy — uses individual saves).

    Args:
        client: SupabaseClient instance
        profiles: List of (linkedin_url, crustdata_response) tuples

    Returns:
        Stats dict with 'saved' and 'errors' counts
    """
    stats = {'saved': 0, 'errors': 0}

    for linkedin_url, crustdata_response in profiles:
        try:
            save_enriched_profile(client, linkedin_url, crustdata_response)
            stats['saved'] += 1
        except Exception as e:
            print(f"[DB] Error saving {linkedin_url}: {e}")
            stats['errors'] += 1

    return stats


# Backwards compatibility alias
def update_profile_enrichment(client: SupabaseClient, linkedin_url: str, crustdata_response: dict, original_url: str = None) -> dict:
    """Alias for save_enriched_profile (backwards compatibility)."""
    return save_enriched_profile(client, linkedin_url, crustdata_response, original_url=original_url)


def save_failed_enrichment(client: SupabaseClient, linkedin_url: str, error_message: str = None, original_url: str = None) -> dict:
    """Save a profile that failed enrichment (Crustdata has no data).

    This prevents the profile from showing as "to enrich" forever.
    Multi-source support: Appends to original_urls array.

    Args:
        client: SupabaseClient instance
        linkedin_url: The LinkedIn URL
        error_message: Optional error message from Crustdata
        original_url: The original input URL (for matching)

    Returns:
        The saved profile record or success indicator
    """
    linkedin_url = normalize_linkedin_url(linkedin_url)
    if not linkedin_url:
        return None

    original_url = normalize_linkedin_url(original_url) if original_url else None
    now = datetime.utcnow().isoformat()

    # Multi-source support: Get existing URLs and append (deduplicated)
    existing_urls = _get_existing_original_urls(client, linkedin_url)
    original_urls = list(existing_urls)
    if original_url and original_url not in original_urls:
        original_urls.append(original_url)

    try:
        # Use upsert to handle both new and existing profiles
        # profiles.status was dropped — do not write it (matches _prepare_profile_row)
        data = {
            'linkedin_url': linkedin_url,
            'enrichment_status': 'not_found',
            'enrichment_attempted_at': now,
        }
        if original_url:
            data['original_url'] = original_url

        # Try to save with original_urls array (post-migration)
        if original_urls:
            data_with_array = {**data, 'original_urls': original_urls}
            try:
                result = client.upsert('profiles', data_with_array, on_conflict='linkedin_url')
                if result:
                    return result[0] if isinstance(result, list) else result
            except Exception:
                # Column doesn't exist yet (pre-migration), fall through
                pass

        # Save without original_urls array (backward compatible)
        result = client.upsert('profiles', data, on_conflict='linkedin_url')
        if result:
            return result[0] if isinstance(result, list) else result
        else:
            return {'linkedin_url': linkedin_url, 'enrichment_status': 'not_found', '_saved': True}
    except Exception as e:
        return {'_error': f'{linkedin_url[:50]}: {str(e)[:100]}'}


def save_failed_enrichments_batch(client: SupabaseClient, failed_urls: list[dict]) -> dict:
    """Save multiple failed enrichments in a batch.

    Args:
        client: SupabaseClient instance
        failed_urls: List of dicts with 'url' and optional 'error' keys

    Returns:
        Stats dict with 'saved', 'errors' counts, and 'error_messages' list
    """
    stats = {'saved': 0, 'errors': 0, 'error_messages': []}

    for item in failed_urls:
        url = item.get('url') or item.get('linkedin_url')
        error = item.get('error')
        original = item.get('original_url')

        try:
            result = save_failed_enrichment(client, url, error_message=error, original_url=original)
            if result and '_error' not in result:
                stats['saved'] += 1
            elif result and '_error' in result:
                stats['errors'] += 1
                stats['error_messages'].append(result['_error'][:150])
            else:
                stats['errors'] += 1
                stats['error_messages'].append(f"No result for {url[:50]}")
        except Exception as e:
            stats['errors'] += 1
            stats['error_messages'].append(f"{url[:30]}: {str(e)[:100]}")

    return stats


def get_not_found_urls(client: SupabaseClient, months: int = 3) -> list[str]:
    """Get URLs that were marked as not_found in Crustdata within the last N months.

    Args:
        client: SupabaseClient instance
        months: Only return URLs attempted within this many months

    Returns:
        List of LinkedIn URLs that Crustdata doesn't have data for
    """
    cutoff = datetime.utcnow() - timedelta(days=months * 30)

    try:
        result = client.select('profiles',
            columns='linkedin_url',
            filters={
                'enrichment_status': 'eq.not_found',
                'enrichment_attempted_at': f'gte.{cutoff.isoformat()}'
            }
        )
        return [r['linkedin_url'] for r in result if r.get('linkedin_url')]
    except Exception as e:
        print(f"[DB] Error getting not_found URLs: {e}")
        return []


def update_profile_screening(client: SupabaseClient, linkedin_url: str, score: int, fit_level: str,
                              summary: str, reasoning: str) -> dict:
    """Write AI screening results to screening_results, not profiles."""
    result = insert_screening_result(
        client,
        linkedin_url=linkedin_url,
        source_project=DEFAULT_SCREENING_SOURCE_PROJECT,
        jd_hash=DEFAULT_SCREENING_JD_HASH,
        score=score,
        fit_level=fit_level,
        summary=summary,
        reasoning=reasoning,
    )
    return result[0] if result else None


def update_profile_screening_batch(client: SupabaseClient, results: list) -> dict:
    """Batch write AI screening results to screening_results.

    Args:
        client: SupabaseClient instance
        results: List of dicts with keys: linkedin_url, score, fit_level, summary, reasoning
    Returns:
        Stats dict with 'saved' and 'errors' counts
    """
    return insert_screening_results_batch(
        client,
        results,
        source_project=DEFAULT_SCREENING_SOURCE_PROJECT,
        jd_hash=DEFAULT_SCREENING_JD_HASH,
    )


def compute_jd_hash(jd_text: str) -> str:
    """Compute a stable hash from JD text for screening dedup."""
    return hashlib.sha256((jd_text or '')[:500].encode()).hexdigest()


def insert_screening_result(client: SupabaseClient, linkedin_url: str, source_project: str,
                             jd_hash: str, score: int = None, fit_level: str = None,
                             result: str = None, summary: str = None, reasoning: str = None,
                             notes: str = None, opener: str = None, jd_title: str = None,
                             position_id: str = None, ai_model: str = None) -> dict:
    """Insert screening result into the shared screening_results table.

    Uses upsert on (linkedin_url, jd_hash, source_project) so re-screening
    the same profile for the same JD overwrites the previous result.
    """
    linkedin_url = normalize_linkedin_url(linkedin_url)

    data = {
        'linkedin_url': linkedin_url,
        'source_project': source_project,
        'jd_hash': jd_hash,
        'jd_title': jd_title,
        'position_id': position_id,
        'screening_score': score,
        'screening_fit_level': fit_level,
        'screening_result': result,
        'screening_summary': summary,
        'screening_reasoning': reasoning,
        'screening_notes': notes,
        'email_opener': opener,
        'ai_model': ai_model,
        'screened_at': datetime.utcnow().isoformat(),
    }
    data = {k: v for k, v in data.items() if v is not None}

    try:
        return client.upsert('screening_results', data,
                              on_conflict='linkedin_url,jd_hash,source_project')
    except Exception as e:
        print(f"[DB] Warning: failed to write screening_results: {e}")
        return None


def insert_screening_results_batch(client: SupabaseClient, results: list,
                                     source_project: str, jd_hash: str,
                                     jd_title: str = None, ai_model: str = None) -> dict:
    """Batch insert screening results into shared screening_results table.

    Args:
        results: List of dicts with keys: linkedin_url, score, fit_level, summary, reasoning
        source_project: 'sourcingx' or 'autopilot'
        jd_hash: Hash of the JD used for screening
        jd_title: Human-readable JD title
        ai_model: AI model used for screening
    """
    if not results:
        return {'saved': 0, 'errors': 0}

    now = datetime.utcnow().isoformat()
    rows = []
    for r in results:
        url = normalize_linkedin_url(r.get('linkedin_url', ''))
        if not url:
            continue
        row = {
            'linkedin_url': url,
            'source_project': source_project,
            'jd_hash': jd_hash,
            'jd_title': jd_title,
            'screening_score': r.get('score'),
            'screening_fit_level': r.get('fit_level'),
            'screening_summary': r.get('summary'),
            'screening_reasoning': r.get('reasoning'),
            'ai_model': ai_model,
            'screened_at': now,
        }
        row = {k: v for k, v in row.items() if v is not None}
        rows.append(row)

    if not rows:
        return {'saved': 0, 'errors': 0}

    try:
        client.upsert_batch('screening_results', rows,
                             on_conflict='linkedin_url,jd_hash,source_project')
        return {'saved': len(rows), 'errors': 0}
    except Exception as e:
        print(f"[DB] Batch screening_results save failed: {e}")
        return {'saved': 0, 'errors': len(rows)}


def get_latest_screenings(client: SupabaseClient, fit_level: str = None, limit: int = 1000) -> list:
    """Get latest screening per profile from the shared screening_results table."""
    filters = {}
    if fit_level:
        filters['screening_fit_level'] = f'eq.{fit_level}'
    return client.select('latest_screening', '*', filters, limit=limit)


def _select_latest_screenings_for_urls(client: SupabaseClient, urls: list) -> dict:
    """Return latest_screening rows keyed by normalized LinkedIn URL."""
    normalized = [normalize_linkedin_url(u) for u in urls if u]
    normalized = [u for u in normalized if u]
    if not normalized:
        return {}

    rows = []
    batch_size = 50
    for i in range(0, len(normalized), batch_size):
        batch = normalized[i:i + batch_size]
        url_list = ','.join(batch)
        rows.extend(client.select(
            'latest_screening',
            '*',
            filters={'linkedin_url': f'in.({url_list})'},
            limit=len(batch),
        ))

    out = {}
    for row in rows:
        url = normalize_linkedin_url(row.get('linkedin_url', ''))
        if url:
            out[url] = row
    return out


def _merge_latest_screenings(profiles: list, screenings_by_url: dict) -> list:
    """Attach latest screening fields to profile rows for UI compatibility."""
    merged = []
    for profile in profiles or []:
        row = dict(profile)
        url = normalize_linkedin_url(row.get('linkedin_url', ''))
        screening = screenings_by_url.get(url, {})
        for key in SCREENING_COLUMNS:
            row[key] = screening.get(key)
        merged.append(row)
    return merged


def update_profile_email(client: SupabaseClient, linkedin_url: str, email: str, source: str = 'salesql') -> dict:
    """Update profile with email from SalesQL or other source."""
    linkedin_url = normalize_linkedin_url(linkedin_url)
    client.update('profiles', {'email': email, 'email_source': source}, {'linkedin_url': linkedin_url})
    return {'linkedin_url': linkedin_url, 'email': email}


def update_profile_emails_batch(client: SupabaseClient, emails: list, source: str = 'salesql') -> dict:
    """Batch update profiles with emails (single DB call instead of N).

    Args:
        client: SupabaseClient instance
        emails: List of dicts with keys: linkedin_url, email
        source: Email source (default 'salesql')
    Returns:
        Stats dict with 'saved' and 'errors' counts
    """
    if not emails:
        return {'saved': 0, 'errors': 0}

    rows = []
    for e in emails:
        url = normalize_linkedin_url(e.get('linkedin_url', ''))
        email = e.get('email', '')
        if not url or not email or not str(email).strip():
            continue
        rows.append({
            'linkedin_url': url,
            'email': email,
            'email_source': source,
        })

    if not rows:
        return {'saved': 0, 'errors': 0}

    try:
        client.upsert_batch('profiles', rows, on_conflict='linkedin_url')
        return {'saved': len(rows), 'errors': 0}
    except Exception as e:
        print(f"[DB] Batch email save failed: {e}")
        return {'saved': 0, 'errors': len(rows)}


# ============================================================================
# QUERY OPERATIONS
# ============================================================================

def get_profile(client: SupabaseClient, linkedin_url: str) -> Optional[dict]:
    """Get a single profile by LinkedIn URL."""
    linkedin_url = normalize_linkedin_url(linkedin_url)
    result = client.select('profiles', '*', {'linkedin_url': f'eq.{linkedin_url}'})
    return result[0] if result else None


def get_profiles_needing_screening(client: SupabaseClient, limit: int = 100) -> list:
    """Get enriched profiles that haven't been screened yet."""
    fetch_limit = min(max(limit * 5, 100), 5000)
    profiles = client.select(
        'profiles',
        '*',
        {'enrichment_status': 'eq.enriched'},
        limit=fetch_limit,
        order_by='enriched_at.desc',
        cursor_column='enriched_at',
    )
    screenings = _select_latest_screenings_for_urls(
        client,
        [p.get('linkedin_url') for p in profiles],
    )
    unscreened = [
        p for p in profiles
        if normalize_linkedin_url(p.get('linkedin_url', '')) not in screenings
    ]
    return unscreened[:limit]


def get_profiles_by_enrichment_status(client: SupabaseClient, enrichment_status: str, limit: int = 1000) -> list:
    """Get profiles by enrichment state (e.g. 'enriched', 'not_found', 'failed')."""
    return client.select('profiles', '*', {'enrichment_status': f'eq.{enrichment_status}'}, limit=limit)


def get_profiles_by_fit_level(client: SupabaseClient, fit_level: str, limit: int = 1000) -> list:
    """Get profiles by screening fit level."""
    screenings = get_latest_screenings(client, fit_level=fit_level, limit=limit)
    urls = [s.get('linkedin_url') for s in screenings if s.get('linkedin_url')]
    profiles = get_profiles_by_urls(client, urls, include_raw_data=False)
    return _merge_latest_screenings(
        profiles,
        {
            normalize_linkedin_url(s.get('linkedin_url', '')): s
            for s in screenings
            if s.get('linkedin_url')
        },
    )


def get_all_profiles(client: SupabaseClient, limit: int = 10000, include_raw_data: bool = False, lightweight: bool = False) -> list:
    """Get all profiles.

    Args:
        client: SupabaseClient instance
        limit: Maximum number of profiles to return
        include_raw_data: If True, includes raw_data JSONB (needed for screening)
        lightweight: If True, returns minimal columns for fast counting/listing
                     (excludes arrays, screening text — use for post-enrichment reload)

    Uses keyset pagination on enriched_at for faster loading of large datasets.
    """
    if include_raw_data:
        profiles = client.select('profiles', '*', limit=limit,
                                 order_by='enriched_at.desc', cursor_column='enriched_at')
    elif lightweight:
        # Minimal columns for fast post-enrichment reload (counting, basic display)
        columns = 'linkedin_url,original_url,name,location,current_title,current_company,enriched_at,email'
        profiles = client.select('profiles', columns, limit=limit,
                                 order_by='enriched_at.desc', cursor_column='enriched_at')
    else:
        # Standard columns for Database tab (includes arrays for filtering, screening summary)
        columns = 'linkedin_url,original_url,name,location,current_title,current_company,all_employers,all_titles,all_schools,skills,enriched_at,email'
        profiles = client.select('profiles', columns, limit=limit,
                                 order_by='enriched_at.desc', cursor_column='enriched_at')

    screenings = _select_latest_screenings_for_urls(
        client,
        [p.get('linkedin_url') for p in profiles],
    )
    return _merge_latest_screenings(profiles, screenings)


def get_profiles_by_urls(client: SupabaseClient, urls: list, include_raw_data: bool = True) -> list:
    """Fetch profiles by LinkedIn URLs.

    Args:
        client: SupabaseClient instance
        urls: List of LinkedIn URLs to fetch
        include_raw_data: If True, includes raw_data JSONB (needed for screening)

    Returns:
        List of profile dicts
    """
    if not urls:
        return []

    # Normalize URLs
    normalized = [normalize_linkedin_url(u) for u in urls if u]
    normalized = [u for u in normalized if u]

    if not normalized:
        return []

    results = []
    batch_size = 50  # Supabase URL length limits

    for i in range(0, len(normalized), batch_size):
        batch = normalized[i:i+batch_size]
        # Use 'in' filter for multiple URLs
        url_list = ','.join(batch)
        filters = {'linkedin_url': f'in.({url_list})'}

        if include_raw_data:
            columns = '*'
        else:
            columns = 'linkedin_url,name,location,current_title,current_company,all_employers,all_titles,all_schools,skills,email,enriched_at'

        batch_results = client.select('profiles', columns, filters, limit=len(batch))
        results.extend(batch_results)

    return results


def match_profiles_by_urls_rpc(
    client: SupabaseClient,
    urls: list,
    enriched_after_months: int = None,
    batch_size: int = 500
) -> list:
    """Match profiles via server-side RPC function.

    Uses PostgreSQL function to match input URLs against profiles table.
    Post-filters to only return profiles that exactly match input URLs.

    Args:
        client: SupabaseClient instance
        urls: List of LinkedIn URLs to match
        enriched_after_months: Only return profiles enriched within this many months (optional)
        batch_size: URLs per RPC call (default 500, max 2000)

    Returns:
        List of matching profile dicts (without raw_data to save bandwidth)
    """
    if not urls:
        return []

    # Build set of normalized input URLs for exact matching post-filter
    from normalizers import normalize_linkedin_url
    input_urls_normalized = set()
    input_usernames = set()
    input_url_to_username = {}  # Track mapping for "one profile per input URL"
    for u in urls:
        if u:
            norm = normalize_linkedin_url(u)
            if norm:
                input_urls_normalized.add(norm)
                # Also extract username for matching
                if '/in/' in norm:
                    username = norm.split('/in/')[-1].rstrip('/')
                    input_usernames.add(username)
                    input_url_to_username[norm] = username

    # Track which input URLs have been matched (to return only one profile per input URL)
    matched_input_urls = set()
    matched_input_usernames = set()

    # Calculate enriched_after timestamp if specified
    enriched_after = None
    if enriched_after_months:
        from datetime import datetime, timedelta
        enriched_after = (datetime.utcnow() - timedelta(days=enriched_after_months * 30)).isoformat()

    # Batch large URL lists to avoid request size limits
    batch_size = min(batch_size, 2000)  # Cap at 2000
    all_results = []
    seen_urls = set()  # Deduplicate across batches

    for i in range(0, len(urls), batch_size):
        batch = urls[i:i + batch_size]

        try:
            payload = {
                "input_urls": batch,
                "enriched_after": enriched_after
            }

            # Use exact matching (safer - no name reversal or hyphen removal)
            # Falls back to fuzzy matching if exact function doesn't exist
            response = requests.post(
                f"{client.url}/rest/v1/rpc/match_profiles_by_urls_exact",
                headers=client.headers,
                json=payload,
                timeout=60  # Longer timeout for large batches
            )
            # If exact function doesn't exist, fall back to original
            if response.status_code == 404:
                response = requests.post(
                    f"{client.url}/rest/v1/rpc/match_profiles_by_urls",
                    headers=client.headers,
                    json=payload,
                    timeout=60
                )
            response.raise_for_status()
            batch_results = response.json()

            # Deduplicate and filter to exact matches only
            for profile in batch_results:
                url = profile.get('linkedin_url')
                original = profile.get('original_url')
                if url and url not in seen_urls:
                    # Post-filter: only keep if linkedin_url or original_url exactly matches input
                    url_norm = normalize_linkedin_url(url)
                    url_username = url_norm.split('/in/')[-1].rstrip('/') if url_norm and '/in/' in url_norm else None

                    # Validate original_url - only use if base username matches linkedin_url
                    # (ignore corrupted original_url that points to different person)
                    original_norm = None
                    original_username = None
                    if original:
                        orig_norm_temp = normalize_linkedin_url(original)
                        if orig_norm_temp and '/in/' in orig_norm_temp:
                            orig_username_temp = orig_norm_temp.split('/in/')[-1].rstrip('/')
                            # Get base usernames (strip ID suffix)
                            def get_base(u):
                                if '-' in u:
                                    parts = u.rsplit('-', 1)
                                    if parts[-1].isdigit() or (len(parts[-1]) >= 5 and sum(c.isdigit() for c in parts[-1]) >= len(parts[-1]) * 0.5):
                                        return parts[0]
                                return u
                            url_base = get_base(url_username) if url_username else None
                            orig_base = get_base(orig_username_temp)
                            # Only use original_url if base names match
                            if url_base and orig_base and (url_base == orig_base or url_base.replace('-','') == orig_base.replace('-','')):
                                original_norm = orig_norm_temp
                                original_username = orig_username_temp

                    # Find which input URL(s) this profile matches
                    matching_input_url = None
                    matching_input_username = None

                    # Check exact URL match (only using validated original_url)
                    if url_norm in input_urls_normalized:
                        matching_input_url = url_norm
                    elif original_norm and original_norm in input_urls_normalized:
                        matching_input_url = original_norm

                    # Check username match (for cases where URL format differs)
                    if not matching_input_url:
                        if url_username in input_usernames:
                            matching_input_username = url_username
                        elif original_username and original_username in input_usernames:
                            matching_input_username = original_username
                        else:
                            # Fallback: prefix matching (same as "already enriched" check)
                            # e.g., DB has "nadavcovalio", input has "nadavcovalioprojectmaster"
                            db_no_hyphen = (url_username or '').replace('-', '').lower()
                            if len(db_no_hyphen) >= 5:
                                for input_user in input_usernames:
                                    input_no_hyphen = input_user.replace('-', '').lower()
                                    # Check if input starts with DB username or vice versa
                                    if input_no_hyphen.startswith(db_no_hyphen):
                                        matching_input_username = input_user
                                        break
                                    if len(input_no_hyphen) >= 5 and db_no_hyphen.startswith(input_no_hyphen):
                                        matching_input_username = input_user
                                        break

                    # Only add if we found a match AND that input hasn't been matched yet
                    if matching_input_url:
                        if matching_input_url not in matched_input_urls:
                            matched_input_urls.add(matching_input_url)
                            seen_urls.add(url)
                            all_results.append(profile)
                    elif matching_input_username:
                        if matching_input_username not in matched_input_usernames:
                            matched_input_usernames.add(matching_input_username)
                            seen_urls.add(url)
                            all_results.append(profile)

        except requests.exceptions.HTTPError as e:
            print(f"[DB] match_profiles_by_urls RPC error (batch {i//batch_size + 1}): {e}")
            # Continue with other batches
            continue
        except Exception as e:
            print(f"[DB] match_profiles_by_urls unexpected error: {e}")
            continue

    return all_results


def get_pipeline_stats(client: SupabaseClient) -> dict:
    """Get pipeline funnel statistics."""
    try:
        result = client.select('pipeline_stats', '*')
        if result:
            return result[0]
    except:
        pass
    return {}


def search_profiles(client: SupabaseClient, query: str, limit: int = 100) -> list:
    """Search profiles by name, company, or title (legacy - use search_profiles_fulltext for better results)."""
    results = client.select('profiles', '*', {'current_company': f'ilike.%{query}%'}, limit=limit)
    if len(results) < limit:
        more = client.select('profiles', '*', {'name': f'ilike.%{query}%'}, limit=limit - len(results))
        results.extend(more)
    return results


def _convert_to_tsquery(query: str) -> str:
    """Convert user-friendly boolean query to PostgreSQL tsquery syntax.

    Converts:
        OR, |, comma -> |
        AND, & -> &
        NOT, -, ! -> !
        "phrase" -> phrase (keeps as single term)

    Examples:
        "node OR node.js" -> "node | node.js"
        "python AND django" -> "python & django"
        "engineer NOT junior" -> "engineer & !junior"
    """
    import re

    # Handle quoted phrases - replace spaces with <-> for phrase search
    def handle_phrase(match):
        phrase = match.group(1)
        # Convert spaces to <-> for tsquery phrase matching
        words = phrase.split()
        if len(words) > 1:
            return ' <-> '.join(words)
        return phrase

    result = re.sub(r'"([^"]+)"', handle_phrase, query)

    # Convert boolean operators (case insensitive)
    result = re.sub(r'\bOR\b', '|', result, flags=re.IGNORECASE)
    result = re.sub(r'\bAND\b', '&', result, flags=re.IGNORECASE)
    result = re.sub(r'\bNOT\b', '& !', result, flags=re.IGNORECASE)

    # Convert symbols
    result = result.replace(',', ' | ')

    # Handle - and ! at word boundaries (NOT operator)
    result = re.sub(r'(?<![a-zA-Z0-9])-(?=[a-zA-Z])', '& !', result)
    result = re.sub(r'(?<![a-zA-Z0-9])!(?=[a-zA-Z])', '& !', result)

    # Clean up multiple spaces and operators
    result = re.sub(r'\s+', ' ', result).strip()
    result = re.sub(r'\|\s*\|', '|', result)  # || -> |
    result = re.sub(r'&\s*&', '&', result)    # && -> &
    result = re.sub(r'^\s*[&|]\s*', '', result)  # Remove leading operators
    result = re.sub(r'\s*[&|]\s*$', '', result)  # Remove trailing operators

    return result


def search_profiles_fulltext(client: SupabaseClient, query: str, limit: int = 5000) -> list:
    """Full-text search across all profile data with pagination.

    Searches: name, title, company, location, skills, all employers, all titles, all schools.
    Requires the search_profiles_text RPC function (see migrations/009_add_fulltext_search.sql).
    Uses pagination to bypass Supabase 1000 row limit.

    Supports boolean syntax:
        - OR: node OR node.js, node | node.js, node, node.js
        - AND: python AND django, python & django
        - NOT: engineer NOT junior, engineer -junior

    Args:
        client: SupabaseClient instance
        query: Search query (e.g., "node OR node.js", "python AND kubernetes")
        limit: Maximum results to return (default 5000, supports up to ~10k)

    Returns:
        List of matching profile dicts, ranked by relevance (without raw_data to save memory)
    """
    # Fields to keep (exclude raw_data and search_text to save memory)
    KEEP_FIELDS = {'linkedin_url', 'name', 'current_title', 'current_company', 'location',
                   'all_employers', 'all_titles', 'all_schools', 'skills', 'email',
                   'enriched_at', 'screening_score', 'screening_fit_level', 'enrichment_status'}

    try:
        all_results = []
        page_size = 1000  # Supabase max rows per request
        offset = 0

        while len(all_results) < limit:
            remaining = limit - len(all_results)
            batch_size = min(page_size, remaining)

            response = requests.post(
                f"{client.url}/rest/v1/rpc/search_profiles_text",
                headers=client.headers,
                json={"query": query, "p_limit": batch_size, "p_offset": offset},
                timeout=30
            )
            response.raise_for_status()
            batch = response.json()

            if not batch:
                break  # No more results

            # Strip large fields to save memory
            batch = [{k: v for k, v in p.items() if k in KEEP_FIELDS} for p in batch]
            all_results.extend(batch)

            if len(batch) < batch_size:
                break  # Last page

            offset += len(batch)

        return all_results
    except Exception as e:
        print(f"[DB] Full-text search error: {e}")
        # Fallback to basic search
        return search_profiles(client, query, limit)


# ============================================================================
# BOOLEAN QUERY PARSER
# ============================================================================

def parse_boolean_query(query: str, column: str) -> str:
    """Parse boolean query string into PostgREST filter syntax.

    Supports:
        - Quoted phrases: "fullstack developer"
        - OR: term1 OR term2, term1 | term2, term1, term2
        - AND: term1 AND term2, term1 & term2, +term
        - NOT: NOT term, -term, !term
        - Grouping: (term1 OR term2)

    Examples:
        '"fullstack developer" OR "fullstack engineer"'
        -> or(current_title.ilike.*fullstack developer*,current_title.ilike.*fullstack engineer*)

        '"full stack" AND (lead OR leader) NOT director'
        -> and(current_title.ilike.*full stack*,or(current_title.ilike.*lead*,current_title.ilike.*leader*),current_title.not.ilike.*director*)

    Args:
        query: Boolean query string
        column: Database column name to search

    Returns:
        PostgREST filter string (without the column= prefix)
    """
    # Tokenize the query
    tokens = _tokenize_boolean_query(query)

    # Parse tokens into AST
    ast = _parse_boolean_tokens(tokens)

    # Convert AST to PostgREST syntax
    return _ast_to_postgrest(ast, column)


def _tokenize_boolean_query(query: str) -> list:
    """Tokenize boolean query into list of tokens.

    Token types: 'PHRASE', 'WORD', 'AND', 'OR', 'NOT', 'LPAREN', 'RPAREN'
    """
    tokens = []
    i = 0
    query = query.strip()

    while i < len(query):
        # Skip whitespace
        if query[i].isspace():
            i += 1
            continue

        # Quoted phrase
        if query[i] == '"':
            end = query.find('"', i + 1)
            if end == -1:
                end = len(query)
            phrase = query[i+1:end]
            tokens.append(('PHRASE', phrase))
            i = end + 1
            continue

        # Parentheses
        if query[i] == '(':
            tokens.append(('LPAREN', '('))
            i += 1
            continue
        if query[i] == ')':
            tokens.append(('RPAREN', ')'))
            i += 1
            continue

        # Operators as symbols
        if query[i] == '|':
            tokens.append(('OR', 'OR'))
            i += 1
            continue
        if query[i] == '&':
            tokens.append(('AND', 'AND'))
            i += 1
            continue
        if query[i] == '!' or query[i] == '-':
            # Check if it's at start of word (NOT operator)
            if i == 0 or query[i-1].isspace() or query[i-1] in '(&|':
                tokens.append(('NOT', 'NOT'))
                i += 1
                continue
        if query[i] == '+':
            # Implicit AND (ignored, AND is default between terms)
            i += 1
            continue
        if query[i] == ',':
            # Comma as OR
            tokens.append(('OR', 'OR'))
            i += 1
            continue

        # Word (including operators as words)
        word_match = re.match(r'[\w\-]+', query[i:])
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

        # Unknown character, skip
        i += 1

    return tokens


def _parse_boolean_tokens(tokens: list) -> dict:
    """Parse tokens into an AST (Abstract Syntax Tree).

    Grammar (simplified):
        expr     -> or_expr
        or_expr  -> and_expr (OR and_expr)*
        and_expr -> not_expr (AND? not_expr)*
        not_expr -> NOT? primary
        primary  -> PHRASE | WORD | '(' expr ')'

    Returns AST as nested dicts:
        {'type': 'OR', 'children': [...]}
        {'type': 'AND', 'children': [...]}
        {'type': 'NOT', 'child': {...}}
        {'type': 'TERM', 'value': '...'}
    """
    pos = [0]  # Use list for mutable closure

    def peek():
        if pos[0] < len(tokens):
            return tokens[pos[0]]
        return None

    def consume():
        token = peek()
        pos[0] += 1
        return token

    def parse_or():
        left = parse_and()
        while peek() and peek()[0] == 'OR':
            consume()  # consume OR
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
            # Explicit AND
            if token[0] == 'AND':
                consume()
                right = parse_not()
            # Implicit AND (two terms next to each other)
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
            child = parse_primary()
            return {'type': 'NOT', 'child': child}
        return parse_primary()

    def parse_primary():
        token = peek()
        if not token:
            return {'type': 'TERM', 'value': ''}

        if token[0] == 'LPAREN':
            consume()  # consume (
            expr = parse_or()
            if peek() and peek()[0] == 'RPAREN':
                consume()  # consume )
            return expr

        if token[0] in ('WORD', 'PHRASE'):
            consume()
            return {'type': 'TERM', 'value': token[1]}

        # Unexpected token, skip
        consume()
        return parse_primary()

    if not tokens:
        return {'type': 'TERM', 'value': ''}

    return parse_or()


def _ast_to_postgrest(ast: dict, column: str) -> str:
    """Convert AST to PostgREST filter syntax.

    Args:
        ast: Parsed AST from _parse_boolean_tokens
        column: Database column name

    Returns:
        PostgREST filter string
    """
    node_type = ast.get('type')

    if node_type == 'TERM':
        value = ast.get('value', '').strip()
        if not value:
            return ''
        # Escape special characters in the value
        value = value.replace('%', r'\%').replace('_', r'\_')
        return f"{column}.ilike.*{value}*"

    if node_type == 'NOT':
        child_filter = _ast_to_postgrest(ast['child'], column)
        if not child_filter:
            return ''
        # PostgREST NOT syntax: column.not.operator.value
        # Convert "column.ilike.*val*" to "column.not.ilike.*val*"
        if '.ilike.' in child_filter:
            return child_filter.replace('.ilike.', '.not.ilike.')
        return f"not.{child_filter}"

    if node_type == 'OR':
        children = [_ast_to_postgrest(c, column) for c in ast['children']]
        children = [c for c in children if c]  # Remove empty
        if not children:
            return ''
        if len(children) == 1:
            return children[0]
        return f"or({','.join(children)})"

    if node_type == 'AND':
        children = [_ast_to_postgrest(c, column) for c in ast['children']]
        children = [c for c in children if c]  # Remove empty
        if not children:
            return ''
        if len(children) == 1:
            return children[0]
        return f"and({','.join(children)})"

    return ''


# Characters that have special meaning in PostgreSQL POSIX regex and must be
# escaped before being injected into a word-boundary pattern. We deliberately
# treat hyphen as a literal too — company names like "log-on" come through with
# a hyphen and POSIX considers it benign outside a char class, but escaping
# keeps the regex predictable across PostgREST quirks.
_POSIX_REGEX_SPECIALS = r'\^$.|?*+()[]{}'


def _regex_escape_for_word_boundary(value: str) -> str:
    """Escape a literal term for embedding inside a PostgREST `imatch` regex.

    Doubles backslashes so the resulting filter string survives the trip
    through the PostgREST URL encoding intact.
    """
    out = []
    for ch in value:
        if ch == '\\':
            out.append('\\\\')
        elif ch in _POSIX_REGEX_SPECIALS:
            out.append('\\' + ch)
        else:
            out.append(ch)
    return ''.join(out)


def _ast_to_postgrest_word_boundary(ast: dict, column: str) -> str:
    """Convert AST to PostgREST filter syntax using word-boundary regex.

    Identical contract to ``_ast_to_postgrest`` but each TERM becomes a POSIX
    regex anchored by ``\\y`` word boundaries via the ``imatch`` operator
    (case-insensitive regex match). This prevents the substring false-positive
    where the term ``elad`` matches inside ``Celadon Books``.

    Used for the Tab 7 Current Company filter — see
    ``test_db_search_company_substring.py`` for the regression contract.
    """
    node_type = ast.get('type')

    if node_type == 'TERM':
        value = ast.get('value', '').strip()
        if not value:
            return ''
        escaped = _regex_escape_for_word_boundary(value)
        # \y is PostgreSQL POSIX regex word-boundary; imatch is case-insensitive
        return f"{column}.imatch.\\y{escaped}\\y"

    if node_type == 'NOT':
        child_filter = _ast_to_postgrest_word_boundary(ast['child'], column)
        if not child_filter:
            return ''
        if '.imatch.' in child_filter:
            return child_filter.replace('.imatch.', '.not.imatch.')
        return f"not.{child_filter}"

    if node_type == 'OR':
        children = [_ast_to_postgrest_word_boundary(c, column) for c in ast['children']]
        children = [c for c in children if c]
        if not children:
            return ''
        if len(children) == 1:
            return children[0]
        return f"or({','.join(children)})"

    if node_type == 'AND':
        children = [_ast_to_postgrest_word_boundary(c, column) for c in ast['children']]
        children = [c for c in children if c]
        if not children:
            return ''
        if len(children) == 1:
            return children[0]
        return f"and({','.join(children)})"

    return ''


def parse_boolean_query_word_boundary(query: str, column: str) -> str:
    """Parse a boolean query into a PostgREST filter using word-boundary regex.

    Same grammar as :func:`parse_boolean_query`, but each term is matched as a
    whole word (via POSIX ``\\y``) rather than as a free-floating substring.
    This is required for the Current Company filter — the substring approach
    matched ``Celadon Books`` when the user typed ``elad``.
    """
    tokens = _tokenize_boolean_query(query)
    ast = _parse_boolean_tokens(tokens)
    return _ast_to_postgrest_word_boundary(ast, column)


def search_profiles_boolean(client: SupabaseClient, filters: dict, limit: int = 5000) -> list:
    """Search profiles with full boolean query support.

    Supports complex boolean expressions in each field:
        - Quoted phrases: "fullstack developer"
        - OR: term1 OR term2, term1 | term2, term1, term2
        - AND: term1 AND term2, term1 & term2
        - NOT: NOT term, -term
        - Grouping: (term1 OR term2)

    Args:
        client: SupabaseClient instance
        filters: Dict with optional keys:
            - name: Boolean query for name
            - current_title: Boolean query for current title
            - current_company: Boolean query for current company
            - location: Boolean query for location
            - has_email: Boolean, filter for profiles with email
            - date_after: ISO date string, enriched_at >= date
            - date_before: ISO date string, enriched_at <= date
        limit: Maximum results (default 5000)

    Examples:
        # Search for fullstack variations
        search_profiles_boolean(client, {
            'current_title': '"fullstack developer" OR "fullstack engineer" OR "full stack"'
        })

        # Complex boolean search
        search_profiles_boolean(client, {
            'current_title': '"full stack" AND (lead OR leader) NOT director',
            'location': 'israel OR "tel aviv"'
        })

    Returns:
        List of profile dicts
    """
    params = {}
    and_conditions = []

    # Process string columns with boolean parser.
    # current_company is intentionally NOT in this dict — it gets word-boundary
    # regex matching below to avoid the "elad matches Celadon Books" false
    # positive (see test_db_search_company_substring.py).
    string_columns = {
        'name': 'name',
        'current_title': 'current_title',
        'location': 'location'
    }

    for filter_key, column in string_columns.items():
        query = filters.get(filter_key)
        if query:
            postgrest_filter = parse_boolean_query(query, column)
            if postgrest_filter:
                and_conditions.append(postgrest_filter)

    # Current company gets word-boundary regex semantics so "elad" doesn't
    # match "Celadon Books". Implemented server-side via PostgREST imatch
    # with \y anchors so we don't fetch+discard thousands of false-positive
    # rows over the network.
    company_query = filters.get('current_company')
    if company_query:
        company_filter = parse_boolean_query_word_boundary(company_query, 'current_company')
        if company_filter:
            and_conditions.append(company_filter)

    # NOTE: Array columns (past_titles, past_companies, skills, schools) are NOT
    # filtered server-side because PostgREST doesn't support ::text casting inside
    # or()/and() groups, and the 'ov' operator only does exact element matching.
    # Array filters are applied client-side in dashboard.py after fetching results.

    # Boolean filters
    if filters.get('has_email'):
        params['email'] = 'not.is.null'

    # Date filters
    # When both bounds are set, emit as a single and() expression so they don't
    # collide on params['enriched_at'] in the simple-conditions loop below
    # (second write would silently overwrite the first, dropping the lower bound).
    if filters.get('date_after') and filters.get('date_before'):
        and_conditions.append(
            f"and(enriched_at.gte.{filters['date_after']},enriched_at.lte.{filters['date_before']})"
        )
    elif filters.get('date_after'):
        params['enriched_at'] = f"gte.{filters['date_after']}"
    elif filters.get('date_before'):
        params['enriched_at'] = f"lte.{filters['date_before']}"

    # Combine all AND conditions.
    # Simple col.op.val conditions (ilike, imatch, not.ilike, etc.) go as direct
    # URL params — PostgREST ANDs multiple params automatically, and this is more
    # reliable than wrapping them in and() which can silently fail in some
    # PostgREST versions when mixed with ilike/imatch operators.
    # Complex or()/and() expressions are collected and put in an and() group.
    complex_conditions = []
    for cond in and_conditions:
        if cond.startswith('or(') or cond.startswith('and('):
            complex_conditions.append(cond)
        else:
            parts = cond.split('.', 1)
            if len(parts) == 2:
                params[parts[0]] = parts[1]

    if complex_conditions:
        if len(complex_conditions) == 1:
            cond = complex_conditions[0]
            if cond.startswith('or('):
                params['or'] = f"({cond[3:-1]})"
            else:  # and(
                params['and'] = f"({cond[4:-1]})"
        else:
            params['and'] = f"({','.join(complex_conditions)})"

    # Select without raw_data for performance
    columns = 'linkedin_url,name,current_title,current_company,all_employers,all_titles,all_schools,skills,location,email,enriched_at'

    return client.select('profiles', columns, params, limit=limit)


def search_profiles_filtered(client: SupabaseClient, filters: dict, limit: int = 5000) -> list:
    """Server-side filtered search for profiles.

    Within each filter: OR logic (e.g., "wiz, monday" matches wiz OR monday)
    Between filters: AND logic (e.g., company AND location must both match)

    Args:
        client: SupabaseClient instance
        filters: Dict with optional keys:
            - name: Search in name field
            - current_title: Search in current_title only
            - past_titles: Search in all_titles array
            - current_company: Search in current_company only
            - past_companies: Search in all_employers
            - location: Search in location
            - skills: Search in skills array
            - schools: Search in all_schools
            - has_email: Boolean, filter for profiles with email
            - date_after: ISO date string, enriched_at >= date
            - date_before: ISO date string, enriched_at <= date
        limit: Maximum results

    Returns:
        List of profile dicts
    """
    # Build params - each filter passed directly (implicitly ANDed by PostgREST)
    params = {}
    or_groups = []  # Collect OR groups for complex AND/OR combinations

    def add_string_filter(column: str, terms_str: str):
        """Add filter for a string column. Single term -> column param, multiple -> or()."""
        terms = [t.strip() for t in terms_str.split(',') if t.strip()]
        if len(terms) == 1:
            # Single term: direct column filter
            params[column] = f"ilike.*{terms[0]}*"
        else:
            # Multiple terms: create OR group
            # PostgREST nested syntax: or(col.ilike.*term1*,col.ilike.*term2*)
            conditions = ','.join([f"{column}.ilike.*{t}*" for t in terms])
            or_groups.append(f"or({conditions})")

    # String columns - direct ilike filter or OR conditions
    if filters.get('name'):
        add_string_filter('name', filters['name'])

    if filters.get('current_title'):
        add_string_filter('current_title', filters['current_title'])

    if filters.get('current_company'):
        add_string_filter('current_company', filters['current_company'])

    if filters.get('location'):
        add_string_filter('location', filters['location'])

    # Array columns - exact element matching
    # 'cs' operator checks if array contains the exact element

    if filters.get('past_titles'):
        # Search ONLY all_titles array (past titles, excludes current)
        terms = [t.strip() for t in filters['past_titles'].split(',') if t.strip()]
        if terms:
            conditions = []
            for t in terms:
                # Exact array element match on all_titles
                conditions.append(f"all_titles.cs.{{\"{t}\"}}")
            or_groups.append(f"or({','.join(conditions)})")

    if filters.get('past_companies'):
        # Search ONLY all_employers array (past companies, excludes current)
        terms = [t.strip() for t in filters['past_companies'].split(',') if t.strip()]
        if terms:
            conditions = []
            for t in terms:
                # Exact array element match on all_employers
                conditions.append(f"all_employers.cs.{{\"{t}\"}}")
            or_groups.append(f"or({','.join(conditions)})")

    if filters.get('skills'):
        terms = [t.strip() for t in filters['skills'].split(',') if t.strip()]
        if terms:
            conditions = []
            for t in terms:
                # Exact array element match
                conditions.append(f"skills.cs.{{\"{t}\"}}")
            or_groups.append(f"or({','.join(conditions)})")

    if filters.get('schools'):
        terms = [t.strip() for t in filters['schools'].split(',') if t.strip()]
        if terms:
            conditions = []
            for t in terms:
                conditions.append(f"all_schools.cs.{{\"{t}\"}}")
            or_groups.append(f"or({','.join(conditions)})")

    # Boolean and date filters
    if filters.get('has_email'):
        params['email'] = 'not.is.null'

    # Handle date range - add to and_conditions if both dates provided
    and_conditions = []
    if filters.get('date_after') and filters.get('date_before'):
        # Both dates: add as AND condition
        and_conditions.append(f"enriched_at.gte.{filters['date_after']}")
        and_conditions.append(f"enriched_at.lte.{filters['date_before']}")
    elif filters.get('date_after'):
        params['enriched_at'] = f"gte.{filters['date_after']}"
    elif filters.get('date_before'):
        params['enriched_at'] = f"lte.{filters['date_before']}"

    # Combine OR groups with AND logic between them
    # PostgREST: Multiple OR groups need and=(or(...),or(...)) to AND them together
    if or_groups:
        if len(or_groups) == 1 and not and_conditions:
            # Single OR group, no other AND conditions: use 'or' parameter directly
            inner = or_groups[0][3:-1]  # Remove 'or(' prefix and ')' suffix
            params['or'] = f"({inner})"
        else:
            # Multiple OR groups or mixed with AND conditions: combine in 'and'
            and_conditions.extend(or_groups)

    # Set final 'and' parameter if we have conditions
    if and_conditions:
        params['and'] = f"({','.join(and_conditions)})"

    # Select without raw_data for performance (large field)
    columns = 'linkedin_url,name,current_title,current_company,all_employers,all_titles,all_schools,skills,location,email,enriched_at'

    return client.select('profiles', columns, params, limit=limit)


# ============================================================================
# DEDUPLICATION (for skipping already-enriched URLs)
# ============================================================================

def get_enriched_urls(client: SupabaseClient) -> set:
    """Get all LinkedIn URLs that have been enriched.

    Used to skip profiles that are already in the database when enriching.
    Orders by (enriched_at desc, linkedin_url desc) so pagination through
    30k+ rows stays stable. Without ORDER BY, Postgres can return rows in
    different orders across pages and silently drop some — see PR #57.
    A single-column keyset cursor on enriched_at is also unsafe because
    timestamp collisions across a page boundary skip rows (Codex round-4).
    """
    result = client.select(
        'profiles', 'linkedin_url',
        limit=50000,
        order_by='enriched_at.desc,linkedin_url.desc',
    )
    urls = set()
    for p in result:
        url = p.get('linkedin_url')
        if url:
            urls.add(normalize_linkedin_url(url))
    return urls


def get_all_linkedin_urls(client: SupabaseClient) -> list:
    """Get all LinkedIn URLs from database.

    See get_enriched_urls() for why the ORDER BY uses a unique tie-breaker.
    """
    result = client.select(
        'profiles', 'linkedin_url',
        limit=50000,
        order_by='enriched_at.desc,linkedin_url.desc',
    )
    return [p['linkedin_url'] for p in result if p.get('linkedin_url')]


def get_recently_enriched_urls(client: SupabaseClient, months: int = 6) -> list:
    """Get LinkedIn URLs enriched within the last N months.

    Returns linkedin_url, original_url, AND all URLs from original_urls array.
    This supports multi-source deduplication (same profile from PhantomBuster + Jam, etc.).
    Uses select() auto-pagination to handle large result sets.

    Backward compatible: Works before and after original_urls migration.
    """
    cutoff_date = (datetime.utcnow() - timedelta(days=months * 30)).isoformat()
    filters = {'enriched_at': f'gte.{cutoff_date}'}

    # Try with original_urls array first (post-migration), keyset pagination for speed
    try:
        all_results = client.select('profiles', 'linkedin_url,original_url,original_urls,enriched_at',
                                    filters, limit=50000,
                                    order_by='enriched_at.desc', cursor_column='enriched_at')
    except Exception:
        # Fall back to old columns only (pre-migration)
        all_results = client.select('profiles', 'linkedin_url,original_url,enriched_at',
                                    filters, limit=50000,
                                    order_by='enriched_at.desc', cursor_column='enriched_at')

    urls = set()  # Use set to avoid duplicates
    for p in all_results:
        # Add canonical URL
        if p.get('linkedin_url'):
            urls.add(p['linkedin_url'])

        # Add single original_url (backward compatibility)
        if p.get('original_url'):
            urls.add(p['original_url'])

        # Add all URLs from array (multi-source support, post-migration)
        original_urls_array = p.get('original_urls') or []
        if isinstance(original_urls_array, list):
            for url in original_urls_array:
                if url:
                    urls.add(url)

    return list(urls)


def get_dedup_stats(client: SupabaseClient) -> dict:
    """Get stats about profiles in database for dedup preview."""
    total = client.count('profiles')
    cutoff_date = (datetime.utcnow() - timedelta(days=ENRICHMENT_REFRESH_MONTHS * 30)).isoformat()
    recently_enriched = client.count('profiles', {'enriched_at': f'gte.{cutoff_date}'})

    return {
        'total_profiles': total,
        'recently_enriched': recently_enriched,
        'will_skip': recently_enriched,
    }


# ============================================================================
# DATAFRAME CONVERSION
# ============================================================================

def profiles_to_dataframe(profiles: list) -> pd.DataFrame:
    """Convert list of profile dicts to DataFrame.

    Uses helpers.profiles_to_display_df to extract fields from raw_data.
    """
    from helpers import profiles_to_display_df
    return profiles_to_display_df(profiles)


def dataframe_to_profiles(df: pd.DataFrame) -> list:
    """Convert DataFrame back to list of profile dicts."""
    return df.to_dict('records')


# ============================================================================
# UTILITY
# ============================================================================

def check_connection(client: SupabaseClient) -> bool:
    """Check if Supabase connection is working."""
    if not client:
        return False
    try:
        client.select('profiles', 'linkedin_url', limit=1)
        return True
    except Exception:
        return False


# ============================================================================
# API USAGE TRACKING
# ============================================================================

def log_api_usage(client: SupabaseClient, data: dict) -> Optional[dict]:
    """Insert a usage record into api_usage_logs table."""
    try:
        result = client.insert('api_usage_logs', data)
        return result[0] if result else None
    except Exception as e:
        print(f"[DB] Failed to log API usage: {e}")
        return None


def get_usage_summary(client: SupabaseClient, days: int = None) -> dict:
    """Get aggregated usage stats by provider."""
    filters = {}
    if days:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        filters['created_at'] = f'gte.{cutoff}'

    try:
        logs = client.select('api_usage_logs', '*', filters, limit=10000)
    except Exception as e:
        print(f"[DB] Failed to fetch usage logs: {e}")
        return {}

    summary = {
        'crustdata': {'credits': 0, 'cost_usd': 0.0, 'requests': 0, 'errors': 0},
        'salesql': {'lookups': 0, 'requests': 0, 'errors': 0},
        'openai': {'cost_usd': 0.0, 'tokens_input': 0, 'tokens_output': 0, 'requests': 0, 'errors': 0},
        'phantombuster': {'runs': 0, 'profiles_scraped': 0, 'errors': 0},
    }

    for log in logs:
        provider = log.get('provider', '').lower()
        if provider not in summary:
            continue

        summary[provider]['requests'] = summary[provider].get('requests', 0) + (log.get('request_count') or 1)

        if log.get('status') == 'error':
            summary[provider]['errors'] = summary[provider].get('errors', 0) + 1

        if provider == 'crustdata':
            summary[provider]['credits'] += log.get('credits_used') or 0
            summary[provider]['cost_usd'] += log.get('cost_usd') or 0
        elif provider == 'salesql':
            summary[provider]['lookups'] += log.get('credits_used') or 0
        elif provider == 'openai':
            summary[provider]['cost_usd'] += log.get('cost_usd') or 0
            summary[provider]['tokens_input'] += log.get('tokens_input') or 0
            summary[provider]['tokens_output'] += log.get('tokens_output') or 0
        elif provider == 'phantombuster':
            summary[provider]['runs'] += 1
            metadata = log.get('metadata') or {}
            summary[provider]['profiles_scraped'] += metadata.get('profiles_scraped') or 0

    return summary


def get_usage_logs(client: SupabaseClient, provider: str = None, days: int = None,
                   limit: int = 100, offset: int = 0) -> list:
    """Get detailed usage logs with optional filtering."""
    filters = {}

    if provider:
        filters['provider'] = f'eq.{provider.lower()}'

    if days:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        filters['created_at'] = f'gte.{cutoff}'

    try:
        params = {'select': '*', 'order': 'created_at.desc'}
        if filters:
            params.update(filters)
        if limit:
            params['limit'] = limit
        if offset:
            params['offset'] = offset

        url = f"{client.url}/rest/v1/api_usage_logs"
        response = requests.get(url, headers=client.headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json() if response.text else []
    except Exception as e:
        print(f"[DB] Failed to fetch usage logs: {e}")
        return []


def get_usage_by_date(client: SupabaseClient, days: int = 30) -> list:
    """Get usage aggregated by date for charting."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    try:
        logs = client.select('api_usage_logs', '*', {'created_at': f'gte.{cutoff}'}, limit=10000)
    except Exception as e:
        print(f"[DB] Failed to fetch usage logs: {e}")
        return []

    by_date = {}
    for log in logs:
        created_at = log.get('created_at', '')
        if not created_at:
            continue
        date_str = created_at[:10]
        provider = log.get('provider', '').lower()

        if date_str not in by_date:
            by_date[date_str] = {'date': date_str, 'crustdata': 0, 'salesql': 0, 'openai': 0, 'phantombuster': 0}

        if provider == 'crustdata':
            by_date[date_str]['crustdata'] += log.get('credits_used') or 0
        elif provider == 'salesql':
            by_date[date_str]['salesql'] += log.get('credits_used') or 0
        elif provider == 'openai':
            by_date[date_str]['openai'] += log.get('cost_usd') or 0
        elif provider == 'phantombuster':
            by_date[date_str]['phantombuster'] += 1

    result = sorted(by_date.values(), key=lambda x: x['date'])
    return result


# ============================================================================
# SETTINGS (key-value store for shared config like system prompts)
# ============================================================================

def get_setting(client: SupabaseClient, key: str) -> Optional[str]:
    """Get a setting value by key from the settings table."""
    try:
        result = client.select('settings', 'value', {'key': f'eq.{key}'}, limit=1)
        if result:
            return result[0].get('value')
    except Exception as e:
        print(f"[DB] Failed to get setting '{key}': {e}")
    return None


def save_setting(client: SupabaseClient, key: str, value: str) -> bool:
    """Save a setting value. Creates or updates."""
    try:
        client.upsert('settings', {
            'key': key,
            'value': value,
            'updated_at': datetime.utcnow().isoformat(),
        }, on_conflict='key')
        return True
    except Exception as e:
        print(f"[DB] Failed to save setting '{key}': {e}")
        return False


# ============================================================================
# SCREENING PROMPTS
# ============================================================================

def get_screening_prompts(client: SupabaseClient) -> list:
    """Get all screening prompts from the database."""
    try:
        result = client.select('screening_prompts', '*', limit=100)
        return result if result else []
    except Exception as e:
        print(f"[DB] Failed to get screening prompts: {e}")
        return []


def get_screening_prompt_by_role(client: SupabaseClient, role_type: str) -> Optional[dict]:
    """Get a screening prompt by role type."""
    try:
        result = client.select('screening_prompts', '*', {'role_type': f'eq.{role_type}'}, limit=1)
        if result:
            return result[0]
    except Exception as e:
        print(f"[DB] Failed to get prompt for role '{role_type}': {e}")
    return None


def get_default_screening_prompt(client: SupabaseClient) -> Optional[dict]:
    """Get the default screening prompt (is_default=true)."""
    try:
        result = client.select('screening_prompts', '*', {'is_default': 'eq.true'}, limit=1)
        if result:
            return result[0]
    except Exception as e:
        print(f"[DB] Failed to get default prompt: {e}")
    return None


def save_screening_prompt(client: SupabaseClient, role_type: str, prompt_text: str,
                          keywords: list = None, is_default: bool = False, name: str = None) -> bool:
    """Save or update a screening prompt."""
    try:
        data = {
            'role_type': role_type.lower().strip(),
            'prompt_text': prompt_text,
            'keywords': keywords or [],
            'is_default': is_default,
            'name': name or role_type.title(),
            'updated_at': datetime.utcnow().isoformat(),
        }
        client.upsert('screening_prompts', data, on_conflict='role_type')
        return True
    except Exception as e:
        print(f"[DB] Failed to save prompt '{role_type}': {e}")
        return False


def delete_screening_prompt(client: SupabaseClient, role_type: str) -> bool:
    """Delete a screening prompt by role type."""
    try:
        client.delete('screening_prompts', {'role_type': role_type})
        return True
    except Exception as e:
        print(f"[DB] Failed to delete prompt '{role_type}': {e}")
        return False


def delete_profile(client: SupabaseClient, linkedin_url: str) -> bool:
    """Permanently remove a profile from the database by LinkedIn URL."""
    try:
        normalized = normalize_linkedin_url(linkedin_url)
        if not normalized:
            print(f"[DB] Could not normalize URL: {linkedin_url}")
            return False
        deleted = client.delete('profiles', {'linkedin_url': normalized})
        if not deleted:
            print(f"[DB] No row matched for deletion: {normalized}")
            return False
        # Best-effort cleanup of screening data for this URL
        try:
            client.delete('screening_results', {'linkedin_url': normalized})
        except Exception as se:
            print(f"[DB] Warning: could not clean up screening_results for '{normalized}': {se}")
        return True
    except Exception as e:
        print(f"[DB] Failed to delete profile '{linkedin_url}': {e}")
        return False


def match_prompt_by_keywords(client: SupabaseClient, text: str) -> Optional[dict]:
    """Find the best matching prompt based on keywords in the text.

    Scans the job description text for keywords and returns the prompt
    with the most keyword matches. Leadership keywords get bonus points.
    """
    # Leadership keywords get extra weight to prioritize lead roles
    leadership_keywords = ['team lead', 'team leader', 'tech lead', 'tech leader',
                          'engineering lead', 'technical lead', 'lead engineer',
                          'engineering manager', 'manager', 'director', 'vp', 'head of']

    try:
        prompts = get_screening_prompts(client)
        if not prompts:
            return None

        text_lower = text.lower()
        best_match = None
        best_score = 0

        for prompt in prompts:
            keywords = prompt.get('keywords', [])
            if not keywords:
                continue

            # Score keyword matches with word-boundary matching
            score = 0
            for kw in keywords:
                kw_lower = kw.lower()
                is_leadership = any(lk in kw_lower for lk in leadership_keywords) or kw_lower in leadership_keywords

                if ' ' in kw_lower:
                    # Multi-word phrase: substring match
                    if kw_lower in text_lower:
                        score += 3 if is_leadership else 2
                else:
                    # Single word: word-boundary match to avoid false positives
                    if re.search(r'\b' + re.escape(kw_lower) + r'\b', text_lower):
                        score += 2 if is_leadership else 1

            if score > best_score:
                best_score = score
                best_match = prompt

        # Return match if score >= 2 (a phrase match, or 2+ single-word matches)
        if best_score >= 2:
            return best_match

        return None
    except Exception as e:
        print(f"[DB] Failed to match prompt: {e}")
        return None


# ============================================================================
# SEARCH HISTORY (PhantomBuster runs)
# ============================================================================

def get_search_history(client: SupabaseClient, agent_id: str = None) -> list:
    """Get search history from the search_history table.

    Args:
        client: SupabaseClient instance
        agent_id: If provided, filter history to this agent only

    Returns:
        List of search history entries, most recent first
    """
    try:
        params = {'select': '*', 'order': 'launched_at.desc'}
        if agent_id:
            params['agent_id'] = f'eq.{agent_id}'
        url = f"{client.url}/rest/v1/search_history"
        response = requests.get(url, headers=client.headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json() if response.text else []
    except Exception as e:
        print(f"[DB] Failed to get search history: {e}")
        return []


def save_search_history_entry(client: SupabaseClient, agent_id: str, csv_name: str,
                               search_url: str = None, profiles_requested: int = None,
                               search_name: str = None) -> bool:
    """Save a search entry to the search_history table.

    Args:
        client: SupabaseClient instance
        agent_id: PhantomBuster agent ID
        csv_name: Name of the output CSV file
        search_url: LinkedIn search URL
        profiles_requested: Number of profiles requested
        search_name: Optional human-readable name for the search

    Returns:
        True if saved successfully
    """
    try:
        data = {
            'agent_id': str(agent_id),
            'csv_name': csv_name,
            'launched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M'),
            'profiles_requested': profiles_requested,
            'search_name': search_name,
        }
        # Remove None values
        data = {k: v for k, v in data.items() if v is not None}
        client.insert('search_history', data)
        return True
    except Exception as e:
        print(f"[DB] Failed to save search history: {e}")
        return False


def delete_search_history_entry(client: SupabaseClient, agent_id: str, csv_name: str) -> bool:
    """Delete a search entry from the search_history table.

    Args:
        client: SupabaseClient instance
        agent_id: PhantomBuster agent ID
        csv_name: Name of the output CSV file to delete

    Returns:
        True if deleted successfully
    """
    try:
        client.delete('search_history', {'agent_id': str(agent_id), 'csv_name': csv_name})
        return True
    except Exception as e:
        print(f"[DB] Failed to delete search history entry: {e}")
        return False


# ============================================================================
# PEOPLE-DB SEARCH HISTORY (Search tab) — migration 023
# ----------------------------------------------------------------------------
# Per-user history of Crustdata People-DB searches. Lets the Search tab show
# "your recent searches" and warn before re-running an identical one. Distinct
# from `search_history` above (PhantomBuster launches). Privacy is per-user at
# the app layer — every query filters on `username`.
# ============================================================================

# Filter keys that change how many / how ordered, not WHO matches — so they are
# excluded from the dedup hash (same people criteria = same search).
_PEOPLE_SEARCH_HASH_IGNORE_KEYS = {'crust_search_sort', 'crust_search_limit', 'crust_semantic_limit'}


def _canonicalize_search_value(value):
    """Normalize one filter value for stable hashing/equality.

    Returns None for anything that means "unset" (empty string, 0, False, empty
    list) so it drops out of the hash. Text is lowercased; comma-lists and real
    lists are made order-insensitive so "react, node" == "node, react".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return True if value else None
    if isinstance(value, (int, float)):
        return value if value else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if ',' in s:
            parts = sorted({p.strip().lower() for p in s.split(',') if p.strip()})
            return ','.join(parts) if parts else None
        return s.lower()
    if isinstance(value, (list, tuple)):
        items = sorted({str(v).strip().lower() for v in value if str(v).strip()})
        return items or None
    return value


def hash_search_filters(filters: dict) -> str:
    """Stable sha256 of the *matching* filters (sort/limit and empty fields excluded).

    Two searches with the same people-matching criteria hash identically, even if
    typed in a different order/case or with a different results-limit or sort.
    """
    canon = {}
    for k, v in (filters or {}).items():
        if k in _PEOPLE_SEARCH_HASH_IGNORE_KEYS:
            continue
        cv = _canonicalize_search_value(v)
        if cv is None or cv == [] or cv == '':
            continue
        canon[k] = cv
    blob = json.dumps(canon, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def summarize_search_filters(filters: dict) -> str:
    """Short human label for a saved search, e.g. 'team lead · @Wiz · Tel Aviv · Senior'."""
    f = filters or {}

    # Search-by-description (semantic) searches have no filter fields, just a
    # free-text query — label them distinctly so they're recognizable in the
    # saved-searches list instead of falling through to "All profiles".
    _semantic_query = (f.get('crust_semantic_query') or '').strip()
    if _semantic_query:
        summary = f'🔍 "{_semantic_query}"'
        if len(summary) > 140:
            summary = summary[:139] + '…"'
        return summary

    def _txt(key):
        val = f.get(key)
        if isinstance(val, (list, tuple)):
            return ', '.join(str(x) for x in val if str(x).strip())
        if val in (None, '', 0, False):
            return ''
        return str(val).strip()

    parts = []
    title = _txt('crust_search_title')
    if title:
        parts.append(title)
    company = _txt('crust_search_company')
    if company:
        parts.append(f"@{company}")
    location = _txt('crust_search_location') or _txt('crust_search_country') or _txt('crust_search_geo_city')
    if location:
        parts.append(location)
    seniority = _txt('crust_search_seniority')
    if seniority:
        parts.append(seniority)
    skills = _txt('crust_search_skills')
    if skills:
        parts.append(skills)
    keywords = _txt('crust_search_keywords')
    if keywords:
        # Free-text keywords are the usual differentiator between two searches
        # with the same title + location, so quote them to stand out in the label.
        parts.append(f'"{keywords}"')

    summary = ' · '.join(parts)
    if len(summary) > 140:
        summary = summary[:139] + '…'
        if summary.count('"') % 2:
            # The cut landed inside the quoted keywords — close the quote so
            # the label never ends on a dangling '"'.
            summary = summary[:138] + '…"'
    return summary or 'All profiles (no filters)'


def record_people_search(client: SupabaseClient, username: str, filters: dict,
                         filters_hash: str, summary: str, result_count: int,
                         result_urls: list = None) -> bool:
    """Upsert a People-DB search into people_search_history (deduped per user by hash).

    Re-running the same search bumps run_count + last_run_at instead of creating a
    duplicate row. Never raises — a logging failure must not break the search.

    result_urls: normalized LinkedIn URLs the run loaded, stored so the search can
    later be reloaded from the profiles table for free. When None (older callers or
    a DB without the column), the link list is simply left untouched.
    """
    if not client or not username:
        return False
    try:
        now = datetime.utcnow().isoformat()
        existing = client.select(
            'people_search_history', 'id,run_count',
            filters={'username': f'eq.{username}', 'filters_hash': f'eq.{filters_hash}'},
            limit=1,
        )
        if existing:
            row = existing[0]
            updates = {
                'last_run_at': now,
                'result_count': result_count,
                'summary': summary,
                'filters': filters,
                'run_count': (row.get('run_count') or 1) + 1,
            }
            if result_urls is not None:
                updates['result_urls'] = result_urls
            try:
                client.update('people_search_history', updates, {'id': row['id']})
            except Exception:
                # Optional column may not be migrated yet (024). Retry without it
                # so the core history write still succeeds rather than regressing.
                if 'result_urls' in updates:
                    updates.pop('result_urls', None)
                    client.update('people_search_history', updates, {'id': row['id']})
                else:
                    raise
        else:
            new_row = {
                'username': username,
                'filters_hash': filters_hash,
                'filters': filters,
                'summary': summary,
                'result_count': result_count,
                'run_count': 1,
                'first_run_at': now,
                'last_run_at': now,
            }
            if result_urls is not None:
                new_row['result_urls'] = result_urls
            try:
                client.insert('people_search_history', new_row)
            except Exception:
                # Optional column may not be migrated yet (024). Retry without it.
                if 'result_urls' in new_row:
                    new_row.pop('result_urls', None)
                    client.insert('people_search_history', new_row)
                else:
                    raise
        return True
    except Exception as e:
        print(f"[DB] Failed to record people search: {e}")
        return False


def update_people_search_urls(client: SupabaseClient, username: str,
                              filters_hash: str, result_urls: list) -> bool:
    """Refresh only the stored result_urls for an existing saved search.

    Used after Load More so the free-reload list covers the extra pages it
    pulled, without bumping run_count (Load More is not a re-run). Never raises;
    if column 024 isn't migrated yet the write simply fails and is swallowed.
    """
    if not client or not username or result_urls is None:
        return False
    try:
        client.update(
            'people_search_history',
            {'result_urls': result_urls},
            {'username': username, 'filters_hash': filters_hash},
        )
        return True
    except Exception as e:
        print(f"[DB] Failed to update people search urls: {e}")
        return False


def find_people_search(client: SupabaseClient, username: str, filters_hash: str) -> Optional[dict]:
    """Return the user's prior search row matching this exact filters_hash, or None."""
    if not client or not username:
        return None
    try:
        rows = client.select(
            'people_search_history', '*',
            filters={'username': f'eq.{username}', 'filters_hash': f'eq.{filters_hash}'},
            limit=1,
        )
        return rows[0] if rows else None
    except Exception as e:
        print(f"[DB] Failed to look up people search: {e}")
        return None


def list_people_searches(client: SupabaseClient, username: str, limit: int = 25) -> list:
    """Return the user's most recent searches, newest first."""
    if not client or not username:
        return []
    try:
        return client.select(
            'people_search_history', '*',
            filters={'username': f'eq.{username}'},
            order_by='last_run_at.desc', limit=limit,
        )
    except Exception as e:
        print(f"[DB] Failed to list people searches: {e}")
        return []


def delete_people_search(client: SupabaseClient, search_id: str) -> bool:
    """Delete one saved search by id. Returns True on success."""
    if not client or not search_id:
        return False
    try:
        client.delete('people_search_history', {'id': search_id})
        return True
    except Exception as e:
        print(f"[DB] Failed to delete people search: {e}")
        return False


# ============================================================================
# SEMANTIC SEARCH (embeddings) — migration 019
# ============================================================================

def update_profile_embedding(
    client: SupabaseClient,
    linkedin_url: str,
    embedding: list,
    model: str,
    input_hash: str,
) -> bool:
    """Store an embedding vector for a single profile.

    Used by the enrichment pipeline (right after a Crustdata save) and by
    the standalone backfill script. Failures here are logged but never
    raised — embeddings are a *secondary* artefact, the enriched profile
    itself must not be rolled back if embedding generation fails.
    """
    try:
        client.update(
            'profiles',
            {
                'embedding': embedding,
                'embedded_at': datetime.utcnow().isoformat(),
                'embedding_model': model,
                'embedding_input_hash': input_hash,
            },
            {'linkedin_url': linkedin_url},
        )
        return True
    except Exception as e:
        print(f"[DB] Failed to update embedding for {linkedin_url}: {e}")
        return False


class SimilarityRPCError(Exception):
    """Raised when the match_profiles_by_embedding RPC fails.

    A bare ``return []`` would let the UI report "no similar profiles"
    even when the real cause is "migration 019 isn't applied" or "the
    embedding payload is the wrong shape". Callers can catch this and
    surface the real message to the user.
    """


def find_similar_profiles_rpc(
    client: SupabaseClient,
    query_embedding: list,
    match_count: int = 20,
    min_similarity: float = 0.0,
    country_terms: list | None = None,
    city_terms: list | None = None,
) -> list:
    """Return profiles ranked by cosine similarity to ``query_embedding``.

    Wraps the ``match_profiles_by_embedding`` Postgres function defined in
    migration 019 (location filter added in migration 021). Returns a list of
    dicts with the lightweight profile columns plus a ``similarity`` float in
    [0, 1] (1 = identical).

    ``country_terms`` and ``city_terms`` are optional lists of lowercase
    substrings. They combine as AND between the two groups, OR within each:
    a profile qualifies if its free-text ``location`` contains at least one
    country term AND at least one city term. An empty/None group adds no
    constraint, so passing neither reproduces the original (unfiltered)
    behaviour.

    Raises ``SimilarityRPCError`` on transport errors, HTTP failures, or
    unexpected response shapes. An empty list is reserved for the genuine
    "RPC succeeded, zero matches" case.
    """
    try:
        response = requests.post(
            f"{client.url}/rest/v1/rpc/match_profiles_by_embedding",
            headers=client.headers,
            json={
                "query_embedding": query_embedding,
                "match_count": match_count,
                "min_similarity": min_similarity,
                "country_terms": country_terms or [],
                "city_terms": city_terms or [],
            },
            timeout=30,
        )
    except requests.RequestException as e:
        raise SimilarityRPCError(f"Network error calling similarity RPC: {e}") from e

    if response.status_code >= 400:
        # Surface the Postgres error verbatim — it tells you whether the
        # column/index/function exists.
        body = response.text[:500] if response.text else "<empty>"
        raise SimilarityRPCError(
            f"Similarity RPC failed ({response.status_code}): {body}"
        )

    try:
        data = response.json()
    except ValueError as e:
        raise SimilarityRPCError(f"Similarity RPC returned non-JSON body: {e}") from e

    if data is None:
        return []
    if not isinstance(data, list):
        raise SimilarityRPCError(
            f"Similarity RPC returned unexpected shape: {type(data).__name__}"
        )
    return data


# ============================================================================
# BACKWARDS COMPATIBILITY - Deprecated functions
# ============================================================================

def upsert_profiles_from_phantombuster(client: SupabaseClient, profiles: list, search_id: str = None) -> dict:
    """DEPRECATED: PhantomBuster data is no longer stored in DB.

    This function is kept for backwards compatibility but does nothing.
    PhantomBuster data should only be used in UI session state.
    """
    print("[DB] WARNING: upsert_profiles_from_phantombuster is deprecated. PB data is no longer stored in DB.")
    return {'inserted': 0, 'updated': 0, 'skipped': len(profiles), 'errors': 0, 'debug': ['DEPRECATED - PB data not stored']}
