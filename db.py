"""
Supabase Database Module for LinkedIn Enricher
Uses REST API directly - no supabase package required.
"""

import os
import json
import requests
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
import pandas as pd

# Refresh threshold for stale profiles
ENRICHMENT_REFRESH_MONTHS = 6


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
            timeout=30
        )
        response.raise_for_status()
        if response.text:
            return response.json()
        return {}

    def select(self, table: str, columns: str = '*', filters: dict = None, limit: int = None) -> list:
        """Select rows from a table."""
        params = {'select': columns}
        if filters:
            for key, value in filters.items():
                params[key] = value
        if limit:
            params['limit'] = limit
        return self._request('GET', table, params=params)

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
            # Include error details in the exception
            error_msg = f"{response.status_code}: {response.text}"
            raise requests.HTTPError(error_msg)
        if response.text:
            return response.json()
        return []

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
    """Get Supabase client from config.json, Streamlit secrets, or environment."""
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

    if url and key:
        return SupabaseClient(url, key)
    return None


def normalize_linkedin_url(url: str) -> str:
    """Normalize LinkedIn URL for consistent matching."""
    if not url:
        return url
    url = str(url).strip().rstrip('/')
    # Add https:// if missing
    if url.startswith('www.'):
        url = 'https://' + url
    elif not url.startswith('http'):
        url = 'https://' + url
    if '?' in url:
        url = url.split('?')[0]
    return url.lower()


# ===== Profile Operations =====

def upsert_profile(client: SupabaseClient, profile_data: dict) -> dict:
    """Insert or update a profile. Returns the upserted record."""
    linkedin_url = normalize_linkedin_url(profile_data.get('linkedin_url') or profile_data.get('public_url'))
    if not linkedin_url:
        raise ValueError("linkedin_url is required")

    data = {
        'linkedin_url': linkedin_url,
        'updated_at': datetime.utcnow().isoformat(),
    }

    field_mapping = {
        'first_name': 'first_name',
        'last_name': 'last_name',
        'headline': 'headline',
        'location': 'location',
        'current_title': 'current_title',
        'current_company': 'current_company',
        'current_years_in_role': 'current_years_in_role',
        'skills': 'skills',
        'summary': 'summary',
        'email': 'email',
    }

    for source_key, db_key in field_mapping.items():
        if source_key in profile_data and profile_data[source_key]:
            data[db_key] = profile_data[source_key]

    result = client.upsert('profiles', data, on_conflict='linkedin_url')
    return result[0] if result else None


def is_nan_or_na(v):
    """Check if value is NaN, None, or pandas NA."""
    import math
    if v is None:
        return True
    # Check for pandas NA type first (not JSON serializable)
    if type(v).__name__ == 'NAType':
        return True
    # Check pandas isna (handles various NA types)
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    # Check float NaN/Inf
    if isinstance(v, float):
        try:
            if math.isnan(v) or math.isinf(v):
                return True
        except (TypeError, ValueError):
            pass
    return False


def clean_nan_values(obj, keep_keys=False):
    """Recursively clean NaN/None/NA values from dict for JSON serialization.

    Args:
        obj: The object to clean
        keep_keys: If True, keep dict keys but set NaN values to None (for batch upsert)
    """
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if is_nan_or_na(v):
                if keep_keys:
                    cleaned[k] = None
                # else: skip this key
            else:
                cleaned_v = clean_nan_values(v, keep_keys)
                if keep_keys or (cleaned_v is not None and not is_nan_or_na(cleaned_v)):
                    cleaned[k] = cleaned_v
        return cleaned
    elif isinstance(obj, list):
        return [clean_nan_values(item, keep_keys) for item in obj if not is_nan_or_na(item)]
    elif is_nan_or_na(obj):
        return None
    return obj


def upsert_profiles_from_phantombuster(client: SupabaseClient, profiles: list, search_id: str = None) -> dict:
    """Bulk upsert profiles from PhantomBuster scrape - optimized batch version."""
    stats = {'inserted': 0, 'updated': 0, 'skipped': 0, 'errors': 0, 'debug': []}

    if not profiles:
        stats['debug'].append('No profiles provided')
        return stats

    # Step 1: Extract valid LinkedIn URLs and prepare data
    profiles_to_upsert = []

    stats['debug'].append(f'Processing {len(profiles)} profiles')

    for i, profile in enumerate(profiles):
        profile = clean_nan_values(profile)

        # Get LinkedIn URL - check linkedin_url first since dashboard normalizes to this
        linkedin_url = profile.get('linkedin_url')

        # Debug first profile
        if i == 0:
            stats['debug'].append(f'First profile linkedin_url: {linkedin_url} (type: {type(linkedin_url).__name__})')
            stats['debug'].append(f'First profile keys: {list(profile.keys())[:5]}...')

        # Validate the URL
        if linkedin_url:
            if 'linkedin.com' in str(linkedin_url) and '/sales/' not in str(linkedin_url):
                linkedin_url = normalize_linkedin_url(linkedin_url)
            else:
                if i == 0:
                    stats['debug'].append(f'URL validation failed: {linkedin_url}')
                linkedin_url = None

        # If not found, try other fields
        if not linkedin_url:
            url_candidates = [
                profile.get('defaultProfileUrl'),
                profile.get('public_url'),
                profile.get('profileUrl'),
                profile.get('linkedInProfileUrl'),
                profile.get('profileLink'),
            ]
            for url in url_candidates:
                if url and 'linkedin.com' in str(url) and '/sales/' not in str(url):
                    linkedin_url = normalize_linkedin_url(url)
                    break

        if not linkedin_url:
            public_id = profile.get('publicIdentifier') or profile.get('public_identifier')
            if public_id and public_id != 'null':
                linkedin_url = f"https://www.linkedin.com/in/{public_id}"

        if not linkedin_url:
            stats['skipped'] += 1
            if i == 0:
                stats['debug'].append('First profile SKIPPED - no valid URL')
            continue

        # Parse duration text to numeric (e.g., "8 months in role" -> 0.67, "2 years" -> 2)
        def parse_duration(duration_str):
            if not duration_str or is_nan_or_na(duration_str):
                return None
            duration_str = str(duration_str).lower().strip()
            try:
                # Try direct number first
                return float(duration_str)
            except (ValueError, TypeError):
                pass
            # Parse text like "8 months", "2 years", "1 year 3 months"
            years = 0
            months = 0
            import re
            year_match = re.search(r'(\d+)\s*year', duration_str)
            month_match = re.search(r'(\d+)\s*month', duration_str)
            if year_match:
                years = int(year_match.group(1))
            if month_match:
                months = int(month_match.group(1))
            if years or months:
                return round(years + months / 12, 2)
            return None

        duration_in_role = profile.get('current_years_in_role') or profile.get('durationInRole')
        duration_at_company = profile.get('current_years_at_company') or profile.get('durationInCompany')

        # Prepare data for this profile - use fixed keys for batch upsert compatibility
        data = {
            'linkedin_url': linkedin_url,
            'first_name': profile.get('first_name') or profile.get('firstName') or None,
            'last_name': profile.get('last_name') or profile.get('lastName') or None,
            'headline': profile.get('headline') or None,
            'location': profile.get('location') or None,
            'current_title': profile.get('current_title') or profile.get('title') or None,
            'current_company': profile.get('current_company') or profile.get('company') or profile.get('companyName') or None,
            'current_years_in_role': parse_duration(duration_in_role),
            'current_years_at_company': parse_duration(duration_at_company),
            'summary': profile.get('summary') or None,
            'phantombuster_data': clean_nan_values(profile),
            'updated_at': datetime.utcnow().isoformat(),
            'status': 'scraped',
        }

        # Clean NaN values but keep all keys (replace NaN with None for batch upsert)
        data = clean_nan_values(data, keep_keys=True)
        profiles_to_upsert.append(data)

    if not profiles_to_upsert:
        stats['debug'].append('No profiles to upsert after processing')
        return stats

    stats['debug'].append(f'Prepared {len(profiles_to_upsert)} profiles for upsert')

    # Step 2: Get existing URLs in ONE query
    urls_to_check = [p['linkedin_url'] for p in profiles_to_upsert]
    existing_urls = set()
    try:
        # Query in batches of 100 to avoid URL length limits
        for i in range(0, len(urls_to_check), 100):
            batch_urls = urls_to_check[i:i+100]
            url_filter = ','.join([f'"{u}"' for u in batch_urls])
            existing = client.select('profiles', 'linkedin_url', {'linkedin_url': f'in.({url_filter})'})
            existing_urls.update(p['linkedin_url'] for p in existing)
    except Exception as e:
        print(f"[DB] Error checking existing: {e}")

    # Step 3: Batch upsert all profiles at once
    try:
        # Add created_at for ALL profiles (same keys required for batch upsert)
        new_count = 0
        now = datetime.utcnow().isoformat()
        for p in profiles_to_upsert:
            if p['linkedin_url'] not in existing_urls:
                p['created_at'] = now
                new_count += 1
            else:
                p['created_at'] = None  # Will be ignored on update but needed for consistent keys

        stats['debug'].append(f'{new_count} new, {len(profiles_to_upsert) - new_count} existing')

        # Serialize and clean for JSON
        json_str = json.dumps(profiles_to_upsert, allow_nan=True)
        json_str = json_str.replace(': NaN', ': null').replace(':NaN', ':null')
        json_str = json_str.replace(': Infinity', ': null').replace(':Infinity', ':null')
        json_str = json_str.replace(': -Infinity', ': null').replace(':-Infinity', ':null')
        clean_data = json.loads(json_str)

        stats['debug'].append(f'Calling upsert with {len(clean_data)} profiles')

        # Single batch upsert
        client.upsert('profiles', clean_data, on_conflict='linkedin_url')

        stats['debug'].append('Upsert completed successfully')

        # Count results
        for p in profiles_to_upsert:
            if p['linkedin_url'] in existing_urls:
                stats['updated'] += 1
            else:
                stats['inserted'] += 1

    except Exception as e:
        stats['debug'].append(f'Batch upsert ERROR: {type(e).__name__}: {e}')
        stats['errors'] = len(profiles_to_upsert)

    return stats


def update_profile_enrichment(client: SupabaseClient, linkedin_url: str, crustdata_response: dict) -> dict:
    """Update profile with Crustdata enrichment data."""
    linkedin_url = normalize_linkedin_url(linkedin_url)

    data = {
        'linkedin_url': linkedin_url,
        'crustdata_data': crustdata_response,
        'enriched_at': datetime.utcnow().isoformat(),
        'status': 'enriched',
    }

    if crustdata_response:
        data['first_name'] = crustdata_response.get('first_name')
        data['last_name'] = crustdata_response.get('last_name')
        data['headline'] = crustdata_response.get('headline')
        data['location'] = crustdata_response.get('location')
        data['summary'] = crustdata_response.get('summary')

        positions = crustdata_response.get('positions', [])
        if positions:
            current = positions[0]
            data['current_title'] = current.get('title')
            data['current_company'] = current.get('company_name')

    # Remove None values
    data = {k: v for k, v in data.items() if v is not None}

    result = client.upsert('profiles', data, on_conflict='linkedin_url')
    return result[0] if result else None


def update_profile_screening(client: SupabaseClient, linkedin_url: str, score: int, fit_level: str,
                              summary: str, reasoning: str) -> dict:
    """Update profile with AI screening results."""
    linkedin_url = normalize_linkedin_url(linkedin_url)

    data = {
        'linkedin_url': linkedin_url,
        'screening_score': score,
        'screening_fit_level': fit_level,
        'screening_summary': summary,
        'screening_reasoning': reasoning,
        'screened_at': datetime.utcnow().isoformat(),
        'status': 'screened',
    }

    result = client.upsert('profiles', data, on_conflict='linkedin_url')
    return result[0] if result else None


def update_profile_email(client: SupabaseClient, linkedin_url: str, email: str, source: str = 'salesql') -> dict:
    """Update profile with email from SalesQL or other source."""
    linkedin_url = normalize_linkedin_url(linkedin_url)
    client.update('profiles', {'email': email, 'email_source': source}, {'linkedin_url': linkedin_url})
    return {'linkedin_url': linkedin_url, 'email': email}


# ===== Query Operations =====

def get_profile(client: SupabaseClient, linkedin_url: str) -> Optional[dict]:
    """Get a single profile by LinkedIn URL."""
    linkedin_url = normalize_linkedin_url(linkedin_url)
    result = client.select('profiles', '*', {'linkedin_url': f'eq.{linkedin_url}'})
    return result[0] if result else None


def get_profiles_needing_enrichment(client: SupabaseClient, limit: int = 100) -> list:
    """Get profiles needing enrichment (never enriched OR older than 6 months)."""
    cutoff_date = (datetime.utcnow() - timedelta(days=ENRICHMENT_REFRESH_MONTHS * 30)).isoformat()

    # Get profiles where enriched_at is null
    never_enriched = client.select('profiles', '*', {'enriched_at': 'is.null'}, limit=limit)

    remaining = limit - len(never_enriched)
    stale = []
    if remaining > 0:
        stale = client.select('profiles', '*', {'enriched_at': f'lt.{cutoff_date}'}, limit=remaining)

    return never_enriched + stale


def get_profiles_needing_screening(client: SupabaseClient, limit: int = 100) -> list:
    """Get enriched profiles that haven't been screened yet."""
    return client.select('profiles', '*', {'status': 'eq.enriched', 'screening_score': 'is.null'}, limit=limit)


def get_profiles_by_status(client: SupabaseClient, status: str, limit: int = 1000) -> list:
    """Get profiles by pipeline status."""
    return client.select('profiles', '*', {'status': f'eq.{status}'}, limit=limit)


def get_profiles_by_fit_level(client: SupabaseClient, fit_level: str, limit: int = 1000) -> list:
    """Get profiles by screening fit level."""
    return client.select('profiles', '*', {'screening_fit_level': f'eq.{fit_level}'}, limit=limit)


def get_all_profiles(client: SupabaseClient, limit: int = 10000) -> list:
    """Get all profiles."""
    return client.select('profiles', '*', limit=limit)


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
    """Search profiles by name, company, or title."""
    # Simple search - Supabase REST API has limited OR support
    # Search in current_company first
    results = client.select('profiles', '*', {'current_company': f'ilike.%{query}%'}, limit=limit)
    if len(results) < limit:
        more = client.select('profiles', '*', {'first_name': f'ilike.%{query}%'}, limit=limit - len(results))
        results.extend(more)
    return results


# ===== Search/Batch Operations =====

def create_search(client: SupabaseClient, name: str, agent_id: str = None, search_url: str = None) -> dict:
    """Create a new search record."""
    result = client.insert('searches', {
        'name': name,
        'phantombuster_agent_id': agent_id,
        'search_url': search_url,
    })
    return result[0] if result else None


def update_search_count(client: SupabaseClient, search_id: str, count: int) -> None:
    """Update the profiles_found count for a search."""
    client.update('searches', {'profiles_found': count}, {'id': search_id})


# ===== DataFrame Conversion =====

def profiles_to_dataframe(profiles: list) -> pd.DataFrame:
    """Convert list of profile dicts to DataFrame."""
    if not profiles:
        return pd.DataFrame()

    df = pd.DataFrame(profiles)

    priority_cols = [
        'first_name', 'last_name', 'current_title', 'current_company',
        'screening_score', 'screening_fit_level', 'email', 'linkedin_url',
        'location', 'status', 'enriched_at', 'screened_at'
    ]

    existing_priority = [c for c in priority_cols if c in df.columns]
    other_cols = [c for c in df.columns if c not in priority_cols]
    df = df[existing_priority + other_cols]

    return df


def dataframe_to_profiles(df: pd.DataFrame) -> list:
    """Convert DataFrame back to list of profile dicts."""
    return df.to_dict('records')


# ===== Utility =====

def check_connection(client: SupabaseClient) -> bool:
    """Check if Supabase connection is working."""
    if not client:
        return False
    try:
        client.select('profiles', 'id', limit=1)
        return True
    except Exception:
        return False


# ===== PhantomBuster Deduplication =====

def get_all_linkedin_urls(client: SupabaseClient) -> list:
    """Get all LinkedIn URLs from database for PhantomBuster skip list."""
    result = client.select('profiles', 'linkedin_url', limit=50000)
    return [p['linkedin_url'] for p in result if p.get('linkedin_url')]


def get_recently_enriched_urls(client: SupabaseClient, months: int = 6) -> list:
    """Get LinkedIn URLs enriched within the last N months."""
    cutoff_date = (datetime.utcnow() - timedelta(days=months * 30)).isoformat()
    result = client.select('profiles', 'linkedin_url', {'enriched_at': f'gte.{cutoff_date}'}, limit=50000)
    return [p['linkedin_url'] for p in result if p.get('linkedin_url')]


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


# ===== API Usage Tracking =====

def log_api_usage(client: SupabaseClient, data: dict) -> Optional[dict]:
    """Insert a usage record into api_usage_logs table.

    Args:
        client: SupabaseClient instance
        data: Dict with keys: provider, operation, request_count, credits_used,
              tokens_input, tokens_output, cost_usd, status, error_message,
              response_time_ms, metadata

    Returns:
        The inserted record or None on error
    """
    try:
        result = client.insert('api_usage_logs', data)
        return result[0] if result else None
    except Exception as e:
        print(f"[DB] Failed to log API usage: {e}")
        return None


def get_usage_summary(client: SupabaseClient, days: int = None) -> dict:
    """Get aggregated usage stats by provider.

    Args:
        client: SupabaseClient instance
        days: Number of days to look back (None = all time)

    Returns:
        Dict with provider stats: {
            'crustdata': {'credits': X, 'requests': Y},
            'salesql': {'lookups': X, 'requests': Y},
            'openai': {'cost_usd': X, 'tokens_input': Y, 'tokens_output': Z, 'requests': W},
            'phantombuster': {'runs': X}
        }
    """
    filters = {}
    if days:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        filters['created_at'] = f'gte.{cutoff}'

    try:
        # Fetch all usage logs for the period
        logs = client.select('api_usage_logs', '*', filters, limit=10000)
    except Exception as e:
        print(f"[DB] Failed to fetch usage logs: {e}")
        return {}

    # Aggregate by provider
    summary = {
        'crustdata': {'credits': 0, 'requests': 0, 'errors': 0},
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
    """Get detailed usage logs with optional filtering.

    Args:
        client: SupabaseClient instance
        provider: Filter by provider name (optional)
        days: Number of days to look back (None = all time)
        limit: Max records to return
        offset: Number of records to skip (for pagination)

    Returns:
        List of usage log records
    """
    filters = {}

    if provider:
        filters['provider'] = f'eq.{provider.lower()}'

    if days:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        filters['created_at'] = f'gte.{cutoff}'

    try:
        # Build params for pagination and ordering
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
    """Get usage aggregated by date for charting.

    Args:
        client: SupabaseClient instance
        days: Number of days to look back

    Returns:
        List of dicts: [{'date': '2024-01-15', 'crustdata': X, 'salesql': Y, ...}, ...]
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    try:
        logs = client.select('api_usage_logs', '*', {'created_at': f'gte.{cutoff}'}, limit=10000)
    except Exception as e:
        print(f"[DB] Failed to fetch usage logs: {e}")
        return []

    # Group by date and provider
    by_date = {}
    for log in logs:
        created_at = log.get('created_at', '')
        if not created_at:
            continue
        # Extract date part
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

    # Sort by date
    result = sorted(by_date.values(), key=lambda x: x['date'])
    return result
