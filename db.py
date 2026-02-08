"""
Supabase Database Module for LinkedIn Enricher (v2 - Crustdata Only)

Stores only Crustdata-enriched profiles. PhantomBuster data stays in UI session state.
Uses REST API directly - no supabase package required.
"""

import os
import json
import requests
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
import pandas as pd

from normalizers import normalize_linkedin_url

# Refresh threshold for re-enriching stale profiles
ENRICHMENT_REFRESH_MONTHS = 3


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


# ============================================================================
# PROFILE OPERATIONS (Crustdata Enriched Profiles Only)
# ============================================================================

def save_enriched_profile(client: SupabaseClient, linkedin_url: str, crustdata_response: dict) -> dict:
    """Save a Crustdata-enriched profile to the database.

    Simplified approach: Store raw_data as-is, extract only title/company for indexing.
    All other fields are extracted at display time from raw_data.

    Args:
        client: SupabaseClient instance
        linkedin_url: The LinkedIn URL (used as primary key)
        crustdata_response: Raw response from Crustdata API

    Returns:
        The saved profile record
    """
    linkedin_url = normalize_linkedin_url(linkedin_url)
    if not linkedin_url:
        raise ValueError("Valid linkedin_url is required")

    cd = crustdata_response or {}

    # Extract only title/company for indexed filtering
    current_title = None
    current_company = None

    # Try current_employers first (Crustdata format)
    current_employers = cd.get('current_employers') or []
    if current_employers and isinstance(current_employers, list):
        emp = current_employers[0] if current_employers else {}
        if isinstance(emp, dict):
            current_title = emp.get('employee_title') or emp.get('title')
            current_company = emp.get('employer_name') or emp.get('company_name')

    # Fallback: extract from headline (e.g., "CEO at Company")
    if not current_title or not current_company:
        headline = cd.get('headline', '')
        if headline and ' at ' in headline:
            parts = headline.split(' at ', 1)
            if not current_title:
                current_title = parts[0].strip()
            if not current_company and len(parts) > 1:
                current_company = parts[1].split('/')[0].strip()

    # Extract pre-flattened arrays from Crustdata (already provided by API)
    all_employers = cd.get('all_employers') or []
    all_titles = cd.get('all_titles') or []
    all_schools = cd.get('all_schools') or []
    skills = cd.get('skills') or []

    # Ensure they're lists of strings
    all_employers = [str(x) for x in all_employers if x] if isinstance(all_employers, list) else []
    all_titles = [str(x) for x in all_titles if x] if isinstance(all_titles, list) else []
    all_schools = [str(x) for x in all_schools if x] if isinstance(all_schools, list) else []
    skills = [str(x) for x in skills if x] if isinstance(skills, list) else []

    # Data to save - indexed fields + raw_data for everything else
    data = {
        'linkedin_url': linkedin_url,
        'raw_data': crustdata_response,
        'current_title': current_title,
        'current_company': current_company,
        'all_employers': all_employers if all_employers else None,
        'all_titles': all_titles if all_titles else None,
        'all_schools': all_schools if all_schools else None,
        'skills': skills if skills else None,
        'status': 'enriched',
        'enriched_at': datetime.utcnow().isoformat(),
    }

    # Remove None values
    data = {k: v for k, v in data.items() if v is not None}

    result = client.upsert('profiles', data, on_conflict='linkedin_url')
    return result[0] if result else None


def save_enriched_profiles_batch(client: SupabaseClient, profiles: list[tuple[str, dict]]) -> dict:
    """Save multiple enriched profiles in a batch.

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
def update_profile_enrichment(client: SupabaseClient, linkedin_url: str, crustdata_response: dict) -> dict:
    """Alias for save_enriched_profile (backwards compatibility)."""
    return save_enriched_profile(client, linkedin_url, crustdata_response)


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
    results = client.select('profiles', '*', {'current_company': f'ilike.%{query}%'}, limit=limit)
    if len(results) < limit:
        more = client.select('profiles', '*', {'first_name': f'ilike.%{query}%'}, limit=limit - len(results))
        results.extend(more)
    return results


# ============================================================================
# DEDUPLICATION (for skipping already-enriched URLs)
# ============================================================================

def get_enriched_urls(client: SupabaseClient) -> set:
    """Get all LinkedIn URLs that have been enriched.

    Used to skip profiles that are already in the database when enriching.
    """
    result = client.select('profiles', 'linkedin_url', limit=50000)
    urls = set()
    for p in result:
        url = p.get('linkedin_url')
        if url:
            urls.add(normalize_linkedin_url(url))
    return urls


def get_all_linkedin_urls(client: SupabaseClient) -> list:
    """Get all LinkedIn URLs from database."""
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
# SEARCH HISTORY (PhantomBuster runs)
# ============================================================================

def get_search_history(client: SupabaseClient, agent_id: str = None) -> list:
    """Get search history from database.

    Args:
        client: SupabaseClient instance
        agent_id: If provided, filter history to this agent only

    Returns:
        List of search history entries, most recent first
    """
    try:
        history_json = get_setting(client, 'search_history')
        if not history_json:
            return []

        history = json.loads(history_json)

        # Filter by agent_id if provided (convert to string for comparison)
        if agent_id:
            agent_id_str = str(agent_id)
            history = [h for h in history if str(h.get('agent_id', '')) == agent_id_str]

        # Sort by launched_at descending (most recent first)
        history.sort(key=lambda x: x.get('launched_at', ''), reverse=True)
        return history
    except Exception as e:
        print(f"[DB] Failed to get search history: {e}")
        return []


def save_search_history_entry(client: SupabaseClient, agent_id: str, csv_name: str,
                               search_url: str = None, profiles_requested: int = None,
                               search_name: str = None) -> bool:
    """Save a search entry to database history.

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
        # Load existing history
        history_json = get_setting(client, 'search_history')
        history = json.loads(history_json) if history_json else []

        # Add new entry (skip search_url - it's too long and not needed for UI)
        entry = {
            'agent_id': str(agent_id),
            'csv_name': csv_name,
            'launched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M'),
            'profiles_requested': profiles_requested,
            'search_name': search_name,
        }
        history.append(entry)

        # Keep only last 50 entries to avoid bloat
        if len(history) > 50:
            history = history[-50:]

        # Save back
        return save_setting(client, 'search_history', json.dumps(history))
    except Exception as e:
        print(f"[DB] Failed to save search history: {e}")
        return False


def delete_search_history_entry(client: SupabaseClient, agent_id: str, csv_name: str) -> bool:
    """Delete a search entry from database history.

    Args:
        client: SupabaseClient instance
        agent_id: PhantomBuster agent ID
        csv_name: Name of the output CSV file to delete

    Returns:
        True if deleted successfully
    """
    try:
        history_json = get_setting(client, 'search_history')
        if not history_json:
            return False

        history = json.loads(history_json)
        agent_id_str = str(agent_id)

        # Filter out the entry to delete
        original_len = len(history)
        history = [h for h in history if not (str(h.get('agent_id', '')) == agent_id_str and h.get('csv_name') == csv_name)]

        if len(history) == original_len:
            return False  # Entry not found

        # Save back
        return save_setting(client, 'search_history', json.dumps(history))
    except Exception as e:
        print(f"[DB] Failed to delete search history entry: {e}")
        return False


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
