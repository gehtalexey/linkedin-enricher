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
        response = requests.post(url, headers=headers, params=params, json=data, timeout=30)
        response.raise_for_status()
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


def upsert_profiles_from_phantombuster(client: SupabaseClient, profiles: list, search_id: str = None) -> dict:
    """Bulk upsert profiles from PhantomBuster scrape."""
    stats = {'inserted': 0, 'updated': 0, 'skipped': 0, 'errors': 0}

    for profile in profiles:
        try:
            linkedin_url = normalize_linkedin_url(
                profile.get('linkedin_url') or profile.get('public_url') or profile.get('defaultProfileUrl')
            )
            if not linkedin_url:
                stats['skipped'] += 1
                continue

            # Check if exists
            existing = client.select('profiles', 'id,enriched_at,status', {'linkedin_url': f'eq.{linkedin_url}'})

            data = {
                'linkedin_url': linkedin_url,
                'first_name': profile.get('first_name') or profile.get('firstName'),
                'last_name': profile.get('last_name') or profile.get('lastName'),
                'headline': profile.get('headline'),
                'location': profile.get('location'),
                'current_title': profile.get('current_title') or profile.get('title'),
                'current_company': profile.get('current_company') or profile.get('company'),
                'current_years_in_role': profile.get('current_years_in_role'),
                'skills': profile.get('skills'),
                'summary': profile.get('summary'),
                'phantombuster_data': profile,
                'updated_at': datetime.utcnow().isoformat(),
            }

            # Remove None values
            data = {k: v for k, v in data.items() if v is not None}

            if existing:
                stats['updated'] += 1
                data['status'] = existing[0].get('status', 'scraped')
            else:
                stats['inserted'] += 1
                data['status'] = 'scraped'
                data['created_at'] = datetime.utcnow().isoformat()

            client.upsert('profiles', data, on_conflict='linkedin_url')

        except Exception as e:
            stats['errors'] += 1
            print(f"Error upserting profile: {e}")

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
