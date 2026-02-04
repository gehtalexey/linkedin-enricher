"""
PhantomBuster Deduplication Module
Adds automatic skip list functionality without modifying existing PB integration.
"""

import requests
import json
from typing import Optional
import pandas as pd

try:
    from db import get_supabase_client, get_all_linkedin_urls, get_recently_enriched_urls, get_dedup_stats
    HAS_DB = True
except ImportError:
    HAS_DB = False


def update_phantombuster_with_skip_list(
    api_key: str,
    agent_id: str,
    search_url: str,
    num_profiles: int = 2500,
    csv_name: str = None,
    skip_urls: list[str] = None
) -> dict:
    """
    Update PhantomBuster agent with search URL AND skip list for deduplication.

    This wraps the existing update logic and adds skip list functionality.
    If skip_urls is None or empty, behaves exactly like existing function.

    Args:
        api_key: PhantomBuster API key
        agent_id: Agent ID
        search_url: Sales Navigator search URL
        num_profiles: Number of profiles to scrape
        csv_name: Custom output filename
        skip_urls: List of LinkedIn URLs to skip (dedup list from database)

    Returns dict with 'success': True, 'csvName': filename, 'skipped_count': N
    """
    from datetime import datetime

    try:
        # Fetch current agent config
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

        # Standard updates (same as existing function)
        arg_dict['salesNavigatorSearchUrl'] = search_url
        arg_dict['search'] = search_url
        arg_dict['numberOfProfiles'] = num_profiles
        arg_dict['numberOfResultsPerSearch'] = num_profiles
        arg_dict['csvName'] = csv_name

        # Add skip list for deduplication
        # PhantomBuster Sales Navigator Export uses various param names
        # We set multiple to ensure compatibility
        skipped_count = 0
        if skip_urls and len(skip_urls) > 0:
            skipped_count = len(skip_urls)
            # Common parameter names for skip lists in PB phantoms
            arg_dict['profileUrls'] = []  # Clear any input URLs
            arg_dict['removeDuplicates'] = True
            arg_dict['onlyNewProfiles'] = True
            # Some phantoms use these:
            arg_dict['alreadyScraped'] = skip_urls
            arg_dict['blacklist'] = skip_urls

        # Update the agent
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
            return {
                'success': True,
                'csvName': csv_name,
                'skipped_count': skipped_count
            }
        else:
            return {'error': f"Failed to update: {update_response.status_code} - {update_response.text}"}

    except Exception as e:
        return {'error': str(e)}


def get_skip_list_from_database() -> tuple[list[str], dict]:
    """
    Get skip list from Supabase database.

    Returns:
        (list of URLs to skip, stats dict)
    """
    if not HAS_DB:
        return [], {'error': 'Database module not available'}

    client = get_supabase_client()
    if not client:
        return [], {'error': 'Supabase not configured'}

    try:
        # Get recently enriched profiles (within 6 months) - these should be skipped
        skip_urls = get_recently_enriched_urls(client)
        stats = get_dedup_stats(client)
        return skip_urls, stats
    except Exception as e:
        return [], {'error': str(e)}


def filter_results_against_database(df: pd.DataFrame, url_column: str = None) -> tuple[pd.DataFrame, dict]:
    """
    Filter PhantomBuster results to remove profiles already in database.

    This is a POST-scrape filter - use when PB skip list doesn't work
    or as a safety net. Saves Crustdata enrichment credits.

    Args:
        df: DataFrame from PhantomBuster results
        url_column: Column containing LinkedIn URLs (auto-detected if None)

    Returns:
        (filtered DataFrame, stats dict)
    """
    if not HAS_DB:
        return df, {'error': 'Database module not available', 'filtered': 0}

    client = get_supabase_client()
    if not client:
        return df, {'error': 'Supabase not configured', 'filtered': 0}

    # Auto-detect URL column
    if url_column is None:
        for col in ['linkedin_url', 'public_url', 'defaultProfileUrl', 'LinkedIn URL', 'profileUrl']:
            if col in df.columns:
                url_column = col
                break

    if url_column is None or url_column not in df.columns:
        return df, {'error': 'No LinkedIn URL column found', 'filtered': 0}

    original_count = len(df)

    try:
        # Get all URLs from database
        db_urls = set(get_all_linkedin_urls(client))

        # Normalize URLs for comparison
        def normalize(url):
            if not url:
                return ''
            return str(url).strip().rstrip('/').lower().split('?')[0]

        db_urls_normalized = {normalize(u) for u in db_urls}

        # Filter out profiles already in database
        mask = ~df[url_column].apply(lambda x: normalize(x) in db_urls_normalized)
        filtered_df = df[mask].copy()

        filtered_count = original_count - len(filtered_df)

        return filtered_df, {
            'original_count': original_count,
            'filtered_count': filtered_count,
            'new_profiles': len(filtered_df),
            'db_profiles': len(db_urls)
        }

    except Exception as e:
        return df, {'error': str(e), 'filtered': 0}
