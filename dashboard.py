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
from datetime import datetime
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

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
)

# Database module (Supabase integration)
# Note: PhantomBuster data is NOT stored in DB - only Crustdata enriched profiles
try:
    from db import (
        get_supabase_client, check_connection, save_enriched_profile,
        update_profile_enrichment, update_profile_screening, get_all_profiles,
        get_pipeline_stats, get_profiles_by_fit_level, get_all_linkedin_urls,
        get_dedup_stats, profiles_to_dataframe, get_usage_summary, get_usage_logs,
        get_usage_by_date, get_enriched_urls, get_recently_enriched_urls,
        get_setting, save_setting,
        get_search_history, save_search_history_entry, delete_search_history_entry,
        get_screening_prompts, get_screening_prompt_by_role, get_default_screening_prompt,
        save_screening_prompt, delete_screening_prompt, match_prompt_by_keywords,
        ENRICHMENT_REFRESH_MONTHS,
    )
    from pb_dedup import filter_results_against_database, update_phantombuster_with_skip_list, get_skip_list_from_database
    HAS_DATABASE = True
except ImportError:
    HAS_DATABASE = False

# Usage tracking module
try:
    from usage_tracker import UsageTracker, calculate_openai_cost
    HAS_USAGE_TRACKER = True
except ImportError:
    HAS_USAGE_TRACKER = False

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
if auth_config and HAS_AUTHENTICATOR:
    authenticator = stauth.Authenticate(
        auth_config['credentials'],
        auth_config['cookie']['name'],
        auth_config['cookie']['key'],
        auth_config['cookie']['expiry_days']
    )
    authenticator.login(location='main')

    if st.session_state.get('authentication_status') is False:
        st.error('Username/password is incorrect')
        st.stop()
    elif st.session_state.get('authentication_status') is None:
        st.warning('Please enter your username and password')
        st.stop()
    else:
        # User is logged in - add logout button to sidebar
        with st.sidebar:
            st.write(f"Welcome, **{st.session_state.get('name', 'User')}**")
            authenticator.logout("Logout", "sidebar")

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

# Sidebar - Session Controls
with st.sidebar:
    st.markdown("### Session")
    col_save, col_clear = st.columns(2)
    with col_save:
        if st.button("Save", key="sidebar_save_session", help="Save session to restore after refresh"):
            from pathlib import Path
            SESSION_FILE = Path(__file__).parent / '.last_session.json'
            # Quick inline save - use the full save function
            if save_session_state():
                st.success("Saved!")
            else:
                st.error("Failed to save")
    with col_clear:
        if st.button("Clear", key="sidebar_clear_session", help="Clear saved session"):
            from pathlib import Path
            SESSION_FILE = Path(__file__).parent / '.last_session.json'
            if SESSION_FILE.exists():
                SESSION_FILE.unlink()
            for key in ['results', 'results_df', 'enriched_results', 'enriched_df', 'screening_results',
                        'passed_candidates_df', 'filter_stats', 'f2_filter_stats', 'original_results_df',
                        'active_screening_prompt', 'active_screening_role', 'jd_screening', 'extra_requirements']:
                if key in st.session_state:
                    del st.session_state[key]
            st.success("Cleared!")
            st.rerun()
    st.divider()

# Load API keys
@st.cache_data(ttl=60)
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
        }

    # Override with Streamlit secrets if available (cloud deployment)
    try:
        if hasattr(st, 'secrets') and len(st.secrets) > 0:
            # Map Streamlit secrets to config keys
            if 'api_key' in st.secrets:
                config['api_key'] = st.secrets['api_key']
            if 'openai_api_key' in st.secrets:
                config['openai_api_key'] = st.secrets['openai_api_key']
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
    except Exception:
        pass

    return config

def load_api_key():
    config = load_config()
    return config.get('api_key')

def load_openai_key():
    config = load_config()
    return config.get('openai_api_key')


def load_phantombuster_key():
    config = load_config()
    return config.get('phantombuster_api_key')


def load_phantombuster_agent_id():
    config = load_config()
    return config.get('phantombuster_agent_id')


def load_salesql_key():
    config = load_config()
    return config.get('salesql_api_key')


# ===== Session Persistence =====
SESSION_FILE = Path(__file__).parent / '.last_session.json'

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

def save_session_state():
    """Save current session state to a local file for persistence across refreshes."""
    try:
        session_data = {}
        keys_to_save = [
            'results', 'results_df', 'enriched_results', 'enriched_df',
            'screening_results', 'filtered_results', 'passed_candidates_df',
            'filter_stats', 'f2_filter_stats', 'last_load_count', 'last_load_file',
            'user_sheet_url', 'original_results_df',
            'active_screening_prompt', 'active_screening_role',
            'jd_screening', 'extra_requirements'
        ]
        for key in keys_to_save:
            if key in st.session_state and st.session_state[key] is not None:
                value = st.session_state[key]
                # Convert DataFrames to dict for JSON serialization
                if isinstance(value, pd.DataFrame):
                    # Replace NaN with None for JSON compatibility
                    clean_df = value.where(pd.notnull(value), None)
                    data = _clean_for_json(clean_df.to_dict('records'))
                    session_data[key] = {'_type': 'dataframe', 'data': data}
                elif isinstance(value, list):
                    session_data[key] = {'_type': 'list', 'data': _clean_for_json(value)}
                elif isinstance(value, dict):
                    session_data[key] = {'_type': 'dict', 'data': _clean_for_json(value)}
                else:
                    session_data[key] = {'_type': 'value', 'data': _clean_for_json(value)}

        if session_data:
            with open(SESSION_FILE, 'w') as f:
                json.dump(session_data, f)
            return True
    except Exception as e:
        print(f"[Session] Save failed: {e}")
    return False


def load_session_state():
    """Load session state from local file."""
    try:
        if SESSION_FILE.exists():
            with open(SESSION_FILE, 'r') as f:
                session_data = json.load(f)

            for key, item in session_data.items():
                if item['_type'] == 'dataframe':
                    st.session_state[key] = pd.DataFrame(item['data'])
                elif item['_type'] == 'list':
                    st.session_state[key] = item['data']
                elif item['_type'] == 'dict':
                    st.session_state[key] = item['data']
                else:
                    st.session_state[key] = item['data']
            return True
    except Exception as e:
        print(f"[Session] Load failed: {e}")
    return False


def clear_session_file():
    """Delete the session file."""
    try:
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
            return True
    except Exception:
        pass
    return False


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


# ===== SalesQL Email Enrichment =====
# Rate limiting: 180 requests/minute, 5000/day
SALESQL_REQUESTS_PER_MINUTE = 180
SALESQL_DAILY_LIMIT = 5000
SALESQL_DELAY_BETWEEN_REQUESTS = 0.35  # ~170 requests/minute to stay safe

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


def enrich_profiles_with_salesql(profiles_df: pd.DataFrame, api_key: str, progress_callback=None, personal_only: bool = True, limit: int = None) -> pd.DataFrame:
    """Enrich multiple profiles with SalesQL emails (personal emails only).

    Args:
        profiles_df: DataFrame with linkedin_url column
        api_key: SalesQL API key
        progress_callback: Optional callback(current, total) for progress updates
        personal_only: If True, only get personal/direct emails (default True)
        limit: Maximum number of profiles to enrich (None = all)

    Returns DataFrame with added email columns.
    """
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

    # Count profiles that need enrichment
    needs_enrichment = []
    for idx, row in df.iterrows():
        linkedin_url = row.get(url_col)
        if not linkedin_url or pd.isna(linkedin_url):
            continue
        if row.get('salesql_email') and not pd.isna(row.get('salesql_email')) and row.get('salesql_email') != '':
            continue
        needs_enrichment.append(idx)

    # Apply limit
    if limit and limit < len(needs_enrichment):
        needs_enrichment = needs_enrichment[:limit]

    total = len(needs_enrichment)
    processed_count = 0
    for idx in needs_enrichment:
        row = df.loc[idx]
        linkedin_url = row.get(url_col)

        result = enrich_with_salesql(linkedin_url, api_key, personal_only=personal_only, tracker=tracker)
        processed_count += 1

        if result.get('emails'):
            # Only use Direct (personal) emails
            best_email = None
            best_type = None
            for email_obj in result['emails']:
                email = email_obj.get('email', '')
                email_type = email_obj.get('type', '')
                if email_type == 'Direct':
                    best_email = email
                    best_type = 'Direct'
                    break

            if best_email:
                df.at[idx, 'salesql_email'] = best_email
                df.at[idx, 'salesql_email_type'] = best_type

        if progress_callback:
            progress_callback(processed_count, total)

        # Rate limiting - 1 request per second to stay within limits
        time.sleep(SALESQL_DELAY_BETWEEN_REQUESTS)

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


@st.cache_data(ttl=300)  # Cache for 5 minutes
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


def launch_phantombuster_agent(api_key: str, agent_id: str, argument: dict = None, clear_results: bool = False, tracker: 'UsageTracker' = None) -> dict:
    """Launch a PhantomBuster agent with the given argument.

    Returns dict with 'containerId' on success, or 'error' on failure.
    Note: Passing any argument overrides the phantom's saved config including cookie!

    Args:
        clear_results: If True, delete existing result AND database files before launching for fresh results
        tracker: Optional UsageTracker for logging API usage
    """
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
                'raw_phantombuster': raw_pb,
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
    generic_column_map = {
        'first_name': 'first_name',
        'last_name': 'last_name',
        'firstName': 'first_name',
        'lastName': 'last_name',
        'company': 'current_company',
        'title': 'current_title',
        'job_title': 'current_title',
        'email': 'email',
        'phone': 'phone',
        'linkedin_url': 'linkedin_url',
        'LinkedIn URL': 'linkedin_url',
        'linkedinUrl': 'linkedin_url',
        'profile_url': 'linkedin_url',
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


def load_sheet_as_df(sheet_url: str, worksheet_name: str = None) -> pd.DataFrame:
    """Load a Google Sheet as a pandas DataFrame."""
    client = get_gspread_client()
    if not client:
        return None

    try:
        spreadsheet = client.open_by_url(sheet_url)
        if worksheet_name:
            worksheet = spreadsheet.worksheet(worksheet_name)
        else:
            worksheet = spreadsheet.sheet1

        data = worksheet.get_all_records()
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"Error loading sheet: {e}")
        return None


def get_filter_sheets_config():
    """Get filter sheets URLs from config."""
    config = load_config()
    return config.get('filter_sheets', {})


def send_notification(title, message):
    """Send desktop notification with sound."""
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
    """Enrich a batch of URLs via Crust Data API."""
    batch_str = ','.join(urls)
    start_time = time.time()

    # Build mapping from username to original URL for matching results back
    def extract_username(url):
        """Extract username from LinkedIn URL (part after /in/)"""
        if '/in/' in str(url).lower():
            username = str(url).lower().split('/in/')[-1].rstrip('/').split('?')[0]
            return username
        return None

    def get_base_username(username):
        """Remove numeric ID suffix from username (e.g., john-doe-12345 -> john-doe)"""
        if '-' in username:
            parts = username.rsplit('-', 1)
            suffix = parts[-1]
            # Only strip if it's clearly an ID:
            # - All digits (12345)
            # - Mostly digits with a few letters (a1234, 1234a, but not regular names)
            if suffix.isdigit():
                return parts[0]
            # Check if it's alphanumeric with majority digits (likely an ID like "a12345")
            if len(suffix) >= 5 and suffix.isalnum():
                digit_count = sum(1 for c in suffix if c.isdigit())
                if digit_count >= len(suffix) * 0.5:  # At least 50% digits
                    return parts[0]
        return username

    def get_reversed_name(username):
        """Reverse name order (first-last -> last-first) for matching different formats."""
        base = get_base_username(username)
        if '-' in base:
            parts = base.split('-')
            if len(parts) == 2:
                return f"{parts[1]}-{parts[0]}"
        return None

    def get_normalized_name(username):
        """Remove all hyphens to create a normalized version for fuzzy matching."""
        base = get_base_username(username)
        return base.replace('-', '') if base else None

    original_url_map = {}
    normalized_url_map = {}  # Hyphen-free versions for fuzzy matching
    failed_extracts = []
    for url in urls:
        username = extract_username(url)
        if username:
            # Map full username
            original_url_map[username] = url
            # Also map base username (without ID suffix)
            base = get_base_username(username)
            if base != username:
                original_url_map[base] = url
            # Also map reversed name order (first-last -> last-first)
            reversed_name = get_reversed_name(username)
            if reversed_name and reversed_name not in original_url_map:
                original_url_map[reversed_name] = url
            # Also map hyphen-free version for fuzzy matching (adaya-o-neill -> adayaoneill)
            normalized = get_normalized_name(username)
            if normalized:
                normalized_url_map[normalized] = url
        else:
            failed_extracts.append(url[:80] if len(str(url)) > 80 else url)

    # Debug: store mapping info in session state for UI display
    import streamlit as st
    st.session_state['_enrich_debug'] = {
        'input_urls': len(urls),
        'map_keys': len(original_url_map),
        'failed_extract': len(failed_extracts),
        'sample_inputs': [str(u)[:60] for u in urls],  # Show all inputs
        'all_map_keys': list(original_url_map.keys()),  # Show all keys
        'failed_samples': failed_extracts[:3] if failed_extracts else []
    }

    try:
        response = requests.get(
            'https://api.crustdata.com/screener/person/enrich',
            params={'linkedin_profile_url': batch_str},
            headers={'Authorization': f'Token {api_key}'},
            timeout=120
        )
        elapsed_ms = int((time.time() - start_time) * 1000)

        if response.status_code == 200:
            data = response.json()
            result = data if isinstance(data, list) else [data]

            # Inject original_url into each result by matching username
            # Use linkedin_flagship_url (canonical) for matching, not linkedin_url (encoded)
            unmatched = []
            for item in result:
                if isinstance(item, dict) and 'error' not in item:
                    result_url = item.get('linkedin_flagship_url') or item.get('linkedin_url', '')
                    result_username = extract_username(result_url)
                    matched = False
                    if result_username:
                        # Try exact match first
                        if result_username in original_url_map:
                            item['_original_url'] = original_url_map[result_username]
                            matched = True
                        else:
                            # Try base username (without suffix) - handles input URLs with ID suffixes
                            base = get_base_username(result_username)
                            if base in original_url_map:
                                item['_original_url'] = original_url_map[base]
                                matched = True
                            else:
                                # Try hyphen-free matching (handles o-neill vs oneill)
                                normalized = get_normalized_name(result_username)
                                if normalized and normalized in normalized_url_map:
                                    item['_original_url'] = normalized_url_map[normalized]
                                    matched = True
                                else:
                                    # Also try matching result username against base versions in the map
                                    for map_key, map_url in original_url_map.items():
                                        if get_base_username(map_key) == result_username:
                                            item['_original_url'] = map_url
                                            matched = True
                                            break

                    if not matched:
                        unmatched.append(result_username or 'NO_USERNAME')

            # Debug: show matching stats
            matched_count = sum(1 for item in result if isinstance(item, dict) and item.get('_original_url'))

            # Store matching debug in session state
            match_debug = {
                'results': len(result),
                'matched': matched_count,
                'unmatched_count': len(unmatched),
                'unmatched_samples': unmatched[:5],
                'map_keys_sample': list(original_url_map.keys())[:10],
                'result_samples': []
            }
            for i, item in enumerate(result[:5]):
                if isinstance(item, dict):
                    match_debug['result_samples'].append({
                        'flagship': (item.get('linkedin_flagship_url') or 'N/A')[:50],
                        'matched': '_original_url' in item,
                        'original_url': (item.get('_original_url') or 'N/A')[:50] if item.get('_original_url') else None
                    })
            st.session_state['_enrich_match_debug'] = match_debug

            # Log successful usage
            if tracker:
                tracker.log_crustdata(
                    profiles_enriched=len(urls),
                    status='success',
                    response_time_ms=elapsed_ms
                )

            return result
        else:
            # Log error
            if tracker:
                tracker.log_crustdata(
                    profiles_enriched=0,
                    status='error',
                    error_message=f'API error {response.status_code}: {response.text[:200]}',
                    response_time_ms=elapsed_ms
                )
            return [{'error': response.text, 'linkedin_url': u} for u in urls]

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        if tracker:
            tracker.log_crustdata(
                profiles_enriched=0,
                status='error',
                error_message=str(e)[:200],
                response_time_ms=elapsed_ms
            )
        return [{'error': str(e), 'linkedin_url': u} for u in urls]


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
    """Apply pre-filters to candidates. Returns filtered df, stats, and filtered_out dict."""
    stats = {}
    filtered_out = {}  # Store removed candidates by filter type
    original_count = len(df)

    df = df.copy()  # Avoid SettingWithCopyWarning

    # Helper functions
    def normalize_company(name):
        """Normalize company name for comparison."""
        if pd.isna(name) or not str(name).strip():
            return ''
        # Lowercase and strip
        name = str(name).lower().strip()
        # Remove common suffixes
        for suffix in [' ltd', ' inc', ' corp', ' llc', ' limited', ' israel', ' il', ' technologies', ' tech', ' software', ' solutions', ' group']:
            if name.endswith(suffix):
                name = name[:-len(suffix)].strip()
        return name

    def matches_list(company, company_list):
        """Check if company matches any in the list - uses normalized exact match."""
        if pd.isna(company) or not str(company).strip():
            return False
        company_norm = normalize_company(company)
        if not company_norm:
            return False
        for c in company_list:
            c_norm = normalize_company(c)
            if not c_norm:
                continue
            # Exact match after normalization
            if company_norm == c_norm:
                return True
            # One contains the other fully (for cases like "Bank Leumi" vs "Bank Leumi Le-Israel")
            if len(c_norm) >= 4 and len(company_norm) >= 4:
                if company_norm.startswith(c_norm) or c_norm.startswith(company_norm):
                    return True
        return False

    def matches_list_in_text(text, company_list):
        """Check if any company from list appears in text - uses word boundary matching."""
        if pd.isna(text) or not str(text).strip():
            return False
        text_lower = str(text).lower()
        for c in company_list:
            c_norm = normalize_company(c)
            if not c_norm or len(c_norm) < 3:
                continue
            # Use word boundary pattern for longer names
            pattern = r'\b' + re.escape(c_norm) + r'\b'
            if re.search(pattern, text_lower):
                return True
        return False

    # ========== EXCLUSION FILTERS ==========
    # Track skipped filters for diagnostics
    stats['_skipped_filters'] = []

    # 1. Past candidates filter
    if filters.get('past_candidates_df') is not None:
        past_df = filters['past_candidates_df']
        if 'Name' in past_df.columns:
            past_names = set(str(name).lower().strip() for name in past_df['Name'].dropna())
            if 'first_name' in df.columns and 'last_name' in df.columns:
                df['_full_name'] = (df['first_name'].fillna('').str.lower().str.strip() + ' ' +
                                   df['last_name'].fillna('').str.lower().str.strip())
                df['_is_past'] = df['_full_name'].isin(past_names)
                stats['past_candidates'] = df['_is_past'].sum()
                filtered_out['Past Candidates'] = df[df['_is_past']].drop(columns=['_is_past', '_full_name']).copy()
                df = df[~df['_is_past']].drop(columns=['_is_past', '_full_name'])
            else:
                stats['_skipped_filters'].append('past_candidates (missing first_name/last_name columns)')
        else:
            stats['_skipped_filters'].append(f"past_candidates (sheet needs 'Name' column, has: {list(past_df.columns)})")

    # 2. Blacklist filter
    if filters.get('blacklist'):
        blacklist = [c.lower().strip() for c in filters['blacklist']]
        if 'current_company' in df.columns:
            df['_blacklisted'] = df['current_company'].apply(lambda x: matches_list(x, blacklist))
            stats['blacklist'] = df['_blacklisted'].sum()
            filtered_out['Blacklist Companies'] = df[df['_blacklisted']].drop(columns=['_blacklisted']).copy()
            df = df[~df['_blacklisted']].drop(columns=['_blacklisted'])
        else:
            stats['_skipped_filters'].append('blacklist (missing current_company column)')

    # 3. Not relevant companies (current)
    if filters.get('not_relevant'):
        not_relevant = [c.lower().strip() for c in filters['not_relevant']]
        if 'current_company' in df.columns:
            df['_not_relevant'] = df['current_company'].apply(lambda x: matches_list(x, not_relevant))
            stats['not_relevant_current'] = df['_not_relevant'].sum()
            filtered_out['Not Relevant (Current)'] = df[df['_not_relevant']].drop(columns=['_not_relevant']).copy()
            df = df[~df['_not_relevant']].drop(columns=['_not_relevant'])
        else:
            stats['_skipped_filters'].append('not_relevant (missing current_company column)')

    # 4. Exclude title keywords filter
    if filters.get('exclude_titles') and 'current_title' in df.columns:
        exclude_keywords = filters['exclude_titles']

        def has_excluded_title(title):
            if pd.isna(title) or not str(title).strip():
                return False
            title_lower = str(title).lower()
            return any(kw in title_lower for kw in exclude_keywords)

        df['_excluded_title'] = df['current_title'].apply(has_excluded_title)
        stats['excluded_titles'] = df['_excluded_title'].sum()
        filtered_out['Excluded Titles'] = df[df['_excluded_title']].drop(columns=['_excluded_title']).copy()
        df = df[~df['_excluded_title']].drop(columns=['_excluded_title'])

    # 5. Include title keywords filter (only keep matching)
    if filters.get('include_titles') and 'current_title' in df.columns:
        include_keywords = filters['include_titles']

        def has_included_title(title):
            if pd.isna(title) or not str(title).strip():
                return False
            title_lower = str(title).lower()
            return any(kw in title_lower for kw in include_keywords)

        df['_included_title'] = df['current_title'].apply(has_included_title)
        not_included = ~df['_included_title']
        stats['not_matching_titles'] = not_included.sum()
        filtered_out['Not Matching Titles'] = df[not_included].drop(columns=['_included_title']).copy()
        df = df[df['_included_title']].drop(columns=['_included_title'])

    # 6. Duration filters (from Phantom data)
    # Helper to parse duration strings like "2 years 3 months" to months
    def parse_duration_to_months(duration_str):
        if pd.isna(duration_str) or not str(duration_str).strip():
            return None
        text = str(duration_str).lower()
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

    # Check for duration columns (Phantom format)
    role_col = 'durationInRole' if 'durationInRole' in df.columns else 'current_years_in_role' if 'current_years_in_role' in df.columns else None
    company_col = 'durationInCompany' if 'durationInCompany' in df.columns else 'current_years_at_company' if 'current_years_at_company' in df.columns else None

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
        filtered_out['Role Too Short'] = df[df['_role_too_short']].drop(columns=['_role_months', '_role_too_short'], errors='ignore').copy()
        df = df[~df['_role_too_short']].drop(columns=['_role_months', '_role_too_short'], errors='ignore')

    # Max role duration
    if filters.get('max_role_months') and role_col:
        max_months = filters['max_role_months']
        df['_role_months'] = df[role_col].apply(parse_duration_to_months)
        df['_role_too_long'] = df['_role_months'].apply(lambda x: x > max_months if pd.notna(x) else False)
        stats['role_too_long'] = df['_role_too_long'].sum()
        filtered_out['Role Too Long'] = df[df['_role_too_long']].drop(columns=['_role_months', '_role_too_long'], errors='ignore').copy()
        df = df[~df['_role_too_long']].drop(columns=['_role_months', '_role_too_long'], errors='ignore')

    # Min company duration
    if filters.get('min_company_months') and company_col:
        min_months = filters['min_company_months']
        df['_company_months'] = df[company_col].apply(parse_duration_to_months)
        df['_company_too_short'] = df['_company_months'].apply(lambda x: x < min_months if pd.notna(x) else False)
        stats['company_too_short'] = df['_company_too_short'].sum()
        filtered_out['Company Too Short'] = df[df['_company_too_short']].drop(columns=['_company_months', '_company_too_short'], errors='ignore').copy()
        df = df[~df['_company_too_short']].drop(columns=['_company_months', '_company_too_short'], errors='ignore')

    # Max company duration
    if filters.get('max_company_months') and company_col:
        max_months = filters['max_company_months']
        df['_company_months'] = df[company_col].apply(parse_duration_to_months)
        df['_company_too_long'] = df['_company_months'].apply(lambda x: x > max_months if pd.notna(x) else False)
        stats['company_too_long'] = df['_company_too_long'].sum()
        filtered_out['Company Too Long'] = df[df['_company_too_long']].drop(columns=['_company_months', '_company_too_long'], errors='ignore').copy()
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
        filtered_out['Not Top University'] = df[not_top_uni].drop(columns=['_top_uni']).copy()
        df = df[df['_top_uni']].drop(columns=['_top_uni'])

    stats['original'] = original_count
    stats['final'] = len(df)
    stats['total_removed'] = original_count - len(df)

    return df, stats, filtered_out


# Default screening prompts for different roles
# Structure: Israel Engineering, Global GTM, Global Engineering (generic)
DEFAULT_PROMPTS = {
    # ==================== ISRAEL ENGINEERING ====================
    'backend_israel': {
        'name': 'Backend - Israel',
        'keywords': ['backend', 'node', 'python', 'java', 'golang', 'go', 'api', 'microservices', 'distributed', 'server-side', 'software engineer', 'israel', 'tel aviv', 'herzliya', 'raanana', 'petah tikva'],
        'prompt': """You are an expert technical recruiter for top Israeli startups. You screen backend engineers.

## Scoring Rubric
- **9-10**: Top-tier company + elite army/top university + modern stack + 4-10 years. Rare.
- **7-8**: Strong company background + good signals. Top 20% candidate.
- **5-6**: Decent background but missing 1-2 signals.
- **3-4**: Weak company background or red flags.
- **1-2**: Not a fit. Multiple disqualifiers.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: Wiz, Monday, Snyk, Wix, AppsFlyer, Fiverr, BigID, Cyera, Finout, AI21, Armis, Run:AI
2. **Elite Army Unit**: 8200, Mamram, Talpiot (only if engineering role - count as ~half years of experience)
3. **Top Universities**: Technion, Tel Aviv University, Hebrew University, Ben-Gurion, Weizmann

## Score Boosters (+1 point each)
1. **Good Companies**: Microsoft Israel, Palo Alto, Tenable, D-ID, DoiT, Earnix, Env0, Fireblocks, LakeFS, Logz.io, Check Point, CyberArk, SentinelOne, Aqua Security
2. **Modern Stack**: Node.js, Go, TypeScript, Python, Java (modern), Kubernetes, Terraform, AWS/GCP/Azure
3. **Systems/Security Stack**: C, C++, Rust, Linux kernel, low-level networking (valuable for security companies)
4. **Stable Tenure**: 2-4 years per company average

## Auto-Disqualifiers (Score 3 or below)
- **Agencies/IT/Consulting**: Tikal, Matrix, Ness, Sela, Malam Team, Bynet, SQLink, etc.
- **Non-tech industries**: Telecom, banks, government, insurance, retail, food, manufacturing
- **Old Stack Only**: Legacy .NET/C#, PHP, legacy Java without modern experience (Note: C/C++ at security companies is NOT old stack)
- **Job Hopper**: Multiple companies with <1.5 years each (internal moves OK)
- **Too Long at One Company**: 7+ years at single company (may be stagnant)
- **Too Junior**: <3 years experience
- **Too Senior**: 50+ years old or 20+ years experience (overqualified, expensive)
- **Freelancer/Contractor**: No stable employment history
- **QA/Automation Only**: Limited backend development depth
- **Pure Hardware/Firmware Only**: Only disqualify if no software/backend work (C/C++ at software companies is valid backend experience)

## Experience Guidelines
- Ideal: 4-10 years total experience
- Army experience counts as ~half (2 years army = 1 year work experience)
- Company brand matters more than skill list for Israeli startups

## Sparse Profile Handling
IMPORTANT: This is a LinkedIn profile, NOT a CV. Many strong engineers have minimal profiles.
- If profile shows strong signals (top company, elite army, top university) but lacks details → DO NOT disqualify
- A Wiz/Monday/8200 engineer with sparse skills list is still a strong candidate
- Score based on available signals, not on profile completeness
- Only disqualify if you see NEGATIVE signals, not missing information

## Output
Be direct and calibrated. Reference specific companies, years, and signals from the profile."""
    },
    'frontend_israel': {
        'name': 'Frontend - Israel',
        'keywords': ['frontend', 'front-end', 'react', 'vue', 'angular', 'javascript', 'typescript', 'ui', 'ux', 'web developer', 'israel', 'tel aviv'],
        'prompt': """You are an expert technical recruiter for top Israeli startups. You screen frontend engineers.

## Scoring Rubric
- **9-10**: Top-tier company + elite army/top university + modern stack + 4-10 years. Rare.
- **7-8**: Strong company background + good signals. Top 20% candidate.
- **5-6**: Decent background but missing 1-2 signals.
- **3-4**: Weak company background or red flags.
- **1-2**: Not a fit. Multiple disqualifiers.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: Wiz, Monday, Snyk, Wix, AppsFlyer, Fiverr, BigID, Cyera, AI21, Armis
2. **Elite Army Unit**: 8200, Mamram (only if engineering role)
3. **Top Universities**: Technion, Tel Aviv University, Hebrew University

## Score Boosters (+1 point each)
1. **Good Companies**: Microsoft Israel, Palo Alto, Env0, Fireblocks, Check Point
2. **Modern Stack**: React, TypeScript, Vue 3, Next.js, design systems, testing
3. **Full Product Ownership**: Not just component work

## Auto-Disqualifiers (Score 3 or below)
- **Agencies/IT/Consulting**: Tikal, Matrix, Ness, Sela, etc.
- **Non-tech industries**: Telecom, banks, government, insurance
- **Old Stack Only**: jQuery only, no modern frameworks, no TypeScript
- **Job Hopper**: Multiple companies with <1.5 years each
- **Too Long at One Company**: 7+ years (may be stagnant)
- **WordPress/Wix Sites Only**: Not real frontend engineering
- **Designer Only**: No coding experience

## Experience Guidelines
- Ideal: 4-10 years total experience
- Look for React/TypeScript depth, not just usage
- Company brand matters significantly

## Sparse Profile Handling
IMPORTANT: This is a LinkedIn profile, NOT a CV. Many strong engineers have minimal profiles.
- If profile shows strong signals (top company, elite army, top university) but lacks details → DO NOT disqualify
- A Wiz/Monday/8200 engineer with sparse skills list is still a strong candidate
- Score based on available signals, not on profile completeness
- Only disqualify if you see NEGATIVE signals, not missing information"""
    },
    'fullstack_israel': {
        'name': 'Fullstack - Israel',
        'keywords': ['fullstack', 'full-stack', 'full stack', 'israel', 'tel aviv'],
        'prompt': """You are an expert technical recruiter for top Israeli startups. You screen fullstack engineers.

## Scoring Rubric
- **9-10**: Top-tier company + elite army/top university + modern stack both FE & BE + 4-10 years.
- **7-8**: Strong company background + solid skills both sides.
- **5-6**: Decent background but stronger on one side.
- **3-4**: Weak company or clearly FE-only or BE-only.
- **1-2**: Not a fit.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: Wiz, Monday, Snyk, Wix, AppsFlyer, Fiverr, BigID, Cyera, AI21, Armis
2. **Elite Army Unit**: 8200, Mamram (only if engineering role)
3. **Top Universities**: Technion, Tel Aviv University, Hebrew University

## Score Boosters (+1 point each)
1. **Good Companies**: Microsoft Israel, Palo Alto, Env0, Fireblocks, Check Point
2. **Modern Stack Both Sides**: React/TS + Node/Python/Go, cloud experience
3. **True Fullstack**: Evidence of owning features end-to-end

## Auto-Disqualifiers (Score 3 or below)
- **Agencies/IT/Consulting**: Tikal, Matrix, Ness, Sela, etc.
- **Non-tech industries**: Telecom, banks, government
- **One-sided Only**: Pure FE or pure BE claiming fullstack
- **Job Hopper**: Multiple companies <1.5 years each
- **Old Stack**: jQuery + PHP, no modern stack

## Experience Guidelines
- Ideal: 4-10 years total experience
- Must show competency on BOTH frontend and backend
- Startups value true fullstack ownership

## Sparse Profile Handling
IMPORTANT: This is a LinkedIn profile, NOT a CV. Many strong engineers have minimal profiles.
- If profile shows strong signals (top company, elite army, top university) but lacks details → DO NOT disqualify
- A Wiz/Monday/8200 engineer with sparse skills list is still a strong candidate
- Score based on available signals, not on profile completeness
- Only disqualify if you see NEGATIVE signals, not missing information"""
    },
    'devops_israel': {
        'name': 'DevOps - Israel',
        'keywords': ['devops', 'sre', 'platform', 'infrastructure', 'kubernetes', 'k8s', 'terraform', 'aws', 'gcp', 'azure', 'israel', 'tel aviv'],
        'prompt': """You are an expert technical recruiter for top Israeli startups. You screen DevOps/Platform engineers.

## Scoring Rubric
- **9-10**: Top-tier company + strong cloud/K8s + 4-10 years + automation mindset.
- **7-8**: Good company background + solid DevOps skills.
- **5-6**: Decent experience but gaps in cloud or scale.
- **3-4**: Limited cloud or mainly IT/operations.
- **1-2**: Not a fit.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: Wiz, Monday, AppsFlyer, Fiverr, Armis, or similar scale
2. **Elite Army Unit**: 8200, Mamram (if infrastructure/ops focused)
3. **Scale Experience**: Production K8s, multi-region, 100+ services

## Score Boosters (+1 point each)
1. **Good Companies**: Microsoft Israel, Check Point, Palo Alto, big startups
2. **Modern Stack**: Kubernetes, Terraform, ArgoCD, cloud-native tools
3. **Certifications**: CKA, AWS/GCP Professional

## Auto-Disqualifiers (Score 3 or below)
- **Agencies/IT/Consulting**: Tikal, Matrix, Ness, Sela
- **Only On-Prem**: No cloud experience
- **IT Support/Helpdesk**: Not engineering
- **Windows/IIS Only**: No Linux/containers
- **Job Hopper**: Multiple companies <1.5 years each

## Experience Guidelines
- Ideal: 4-10 years total experience
- Cloud-native is critical
- Automation mindset > manual operations

## Sparse Profile Handling
IMPORTANT: This is a LinkedIn profile, NOT a CV. Many strong engineers have minimal profiles.
- If profile shows strong signals (top company, elite army, top university) but lacks details → DO NOT disqualify
- A Wiz/Monday/8200 engineer with sparse skills list is still a strong candidate
- Score based on available signals, not on profile completeness
- Only disqualify if you see NEGATIVE signals, not missing information"""
    },
    'teamlead_israel': {
        'name': 'Team Lead - Israel',
        'keywords': ['team lead', 'tech lead', 'engineering lead', 'technical lead', 'lead engineer', 'staff engineer', 'israel', 'tel aviv'],
        'prompt': """You are an expert technical recruiter for top Israeli startups. You screen Team/Tech Leads.

## Scoring Rubric
- **9-10**: Led team at top company + strong technical + mentorship evidence + 6-12 years.
- **7-8**: Good company + led team of 4+ + still hands-on.
- **5-6**: Senior engineer ready to lead, or lead with gaps.
- **3-4**: Limited leadership or technical depth.
- **1-2**: Not a fit.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: Led team at Wiz, Monday, Snyk, Wix, AppsFlyer, Fiverr, BigID
2. **Elite Army Unit**: 8200, Mamram (shows leadership early)
3. **Team Growth**: Hired/grew team, promoted engineers

## Score Boosters (+1 point each)
1. **Good Companies**: Microsoft Israel, Palo Alto, Check Point, good startups
2. **Technical Depth**: System design, architecture decisions
3. **Still Coding**: Hands-on at least 30-50% of time

## Auto-Disqualifiers (Score 3 or below)
- **Agencies/IT/Consulting**: Tikal, Matrix, Ness, Sela
- **Manager Only**: No technical depth, pure people management
- **Only 1-2 Reports**: Not real team lead scope
- **No Coding 3+ Years**: Too far from technical
- **Project Lead Only**: Coordination without ownership
- **Job Hopper**: Multiple companies <1.5 years each

## Experience Guidelines
- Ideal: 6-12 years total, 2+ years leading
- Must balance technical + leadership
- Hands-on coding still expected

## Sparse Profile Handling
IMPORTANT: This is a LinkedIn profile, NOT a CV. Many strong engineers have minimal profiles.
- If profile shows strong signals (top company, elite army, top university) but lacks details → DO NOT disqualify
- A Wiz/Monday/8200 engineer with sparse skills list is still a strong candidate
- Score based on available signals, not on profile completeness
- Only disqualify if you see NEGATIVE signals, not missing information"""
    },
    'product_israel': {
        'name': 'Product Manager - Israel',
        'keywords': ['product manager', 'product owner', 'pm', 'product lead', 'israel', 'tel aviv'],
        'prompt': """You are an expert recruiter for top Israeli startups. You screen Product Managers.

## Scoring Rubric
- **9-10**: PM at top company + technical background + shipped products + 5-10 years.
- **7-8**: Good company + clear ownership + data-driven.
- **5-6**: PM experience but gaps in ownership or impact.
- **3-4**: Limited product ownership or mostly process.
- **1-2**: Not a fit.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: PM at Wiz, Monday, Snyk, Wix, AppsFlyer, Fiverr
2. **Technical Background**: CS degree, was engineer before PM
3. **0-to-1 Products**: Built products from scratch

## Score Boosters (+1 point each)
1. **Good Companies**: Microsoft Israel, Palo Alto, good startups
2. **Top Universities**: Technion, TAU, Hebrew U
3. **Measurable Impact**: Revenue, adoption, retention metrics

## Auto-Disqualifiers (Score 3 or below)
- **Agencies/Consulting**: No in-house product experience
- **Scrum Master Only**: Process role, not ownership
- **Project Manager**: Execution, not strategy
- **Business Analyst**: Analysis without ownership
- **Non-tech Products**: Only non-tech industries
- **Job Hopper**: Multiple companies <1.5 years each

## Experience Guidelines
- Ideal: 5-10 years, including 3+ as PM
- Technical depth is valued in Israeli startups
- Look for ownership and impact evidence

## Sparse Profile Handling
IMPORTANT: This is a LinkedIn profile, NOT a CV. Many strong candidates have minimal profiles.
- If profile shows strong signals (top company, elite army, top university) but lacks details → DO NOT disqualify
- A Wiz/Monday PM with sparse description is still a strong candidate
- Score based on available signals, not on profile completeness
- Only disqualify if you see NEGATIVE signals, not missing information"""
    },
    # ==================== GLOBAL GTM ====================
    'sales_global': {
        'name': 'Sales / AE - Global',
        'keywords': ['sales', 'account executive', 'ae', 'enterprise sales', 'saas sales', 'b2b sales', 'quota', 'remote', 'us', 'usa', 'emea', 'apac'],
        'prompt': """You are an expert sales recruiter for high-growth B2B SaaS companies hiring globally.

## Scoring Rubric
- **9-10**: Top performer at top company + consistent quota achievement + enterprise experience + 4-10 years.
- **7-8**: Strong track record + met quotas + relevant industry.
- **5-6**: B2B SaaS experience but gaps in seniority or results.
- **3-4**: Limited SaaS or mostly SMB experience.
- **1-2**: Not a fit.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: Gong, Salesforce, Datadog, HubSpot, ZoomInfo, Outreach, Monday, Snowflake
2. **Quota Achievement**: 120%+ consistently, President's Club
3. **Enterprise Deals**: $100K+ ACV, complex sales cycles

## Score Boosters (+1 point each)
1. **Good Companies**: Established SaaS companies, well-funded startups
2. **Career Progression**: SDR → AE → Senior AE → Enterprise
3. **Vertical Expertise**: Security, data, cloud, DevOps

## Auto-Disqualifiers (Score 3 or below)
- **B2C/Retail Only**: No B2B SaaS experience
- **No Tech Industry**: Only selling non-tech products
- **Job Hopper**: Multiple companies <1 year each
- **No Quota Evidence**: Can't demonstrate achievement
- **SMB Only for Enterprise Role**: Never sold to enterprise
- **Inside Sales Only**: For field/enterprise roles

## Experience Guidelines
- Ideal: 4-10 years in B2B SaaS sales
- Look for quota achievement numbers
- Progression from SDR to AE is positive signal
- Industry knowledge (security, data, cloud) is valuable"""
    },
    'sdr_global': {
        'name': 'SDR/BDR - Global',
        'keywords': ['sdr', 'bdr', 'sales development', 'business development representative', 'outbound', 'prospecting', 'remote', 'us', 'emea'],
        'prompt': """You are an expert recruiter for B2B SaaS companies hiring SDR/BDRs globally.

## Scoring Rubric
- **9-10**: SDR at top company + proven metrics + promotion potential + 1-3 years.
- **7-8**: Good SDR experience + meets quotas + hungry.
- **5-6**: Some SDR experience or strong adjacent background.
- **3-4**: Limited outbound or B2B experience.
- **1-2**: Not a fit.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: SDR at Gong, Salesforce, HubSpot, Outreach, top startups
2. **Strong Metrics**: Meeting/exceeding pipeline targets
3. **Promoted to AE**: Shows trajectory

## Score Boosters (+1 point each)
1. **Good Companies**: Established SaaS, well-funded startups
2. **Relevant Background**: Sales, customer-facing roles
3. **Tech Savvy**: Uses sales tools effectively

## Auto-Disqualifiers (Score 3 or below)
- **No B2B Experience**: Only B2C or retail
- **No Outbound Experience**: Only inbound/support
- **Too Senior**: 5+ years as SDR without progression
- **Job Hopper**: <6 months at multiple companies

## Experience Guidelines
- Ideal: 1-3 years SDR/sales experience
- Entry-level OK if strong potential signals
- Look for hunger and coachability"""
    },
    'marketing_global': {
        'name': 'Marketing - Global',
        'keywords': ['marketing', 'growth', 'demand gen', 'product marketing', 'content marketing', 'digital marketing', 'plg', 'abm', 'remote', 'us', 'emea'],
        'prompt': """You are an expert marketing recruiter for high-growth B2B SaaS companies hiring globally.

## Scoring Rubric
- **9-10**: Top company + measurable pipeline impact + strategic + 4-10 years.
- **7-8**: Strong B2B SaaS track record + data-driven.
- **5-6**: Marketing experience but gaps in B2B or metrics.
- **3-4**: Limited B2B tech marketing.
- **1-2**: Not a fit.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: HubSpot, Gong, Datadog, Salesforce, Monday, Fiverr
2. **Measurable Impact**: Pipeline generation, conversion improvements, CAC/LTV
3. **PLG Experience**: Product-led growth, self-serve funnels

## Score Boosters (+1 point each)
1. **Good Companies**: Established SaaS, well-funded startups
2. **Technical Background**: Can work with product/engineering
3. **Full Funnel**: Demand gen + content + product marketing

## Auto-Disqualifiers (Score 3 or below)
- **Agency Only**: No in-house B2B experience
- **B2C Only**: Consumer marketing doesn't transfer
- **No Metrics Focus**: "Creative" without data
- **Traditional Only**: Print, events, no digital
- **Job Hopper**: Multiple companies <1.5 years each

## Experience Guidelines
- Ideal: 4-10 years, mostly B2B SaaS
- Look for pipeline/revenue impact numbers
- Growth/demand gen valued over brand for startups"""
    },
    'customer_success_global': {
        'name': 'Customer Success - Global',
        'keywords': ['customer success', 'csm', 'cs manager', 'account manager', 'client success', 'renewals', 'expansion', 'remote', 'us', 'emea'],
        'prompt': """You are an expert recruiter for B2B SaaS companies hiring Customer Success globally.

## Scoring Rubric
- **9-10**: Top company + strong retention/expansion metrics + strategic + 4-10 years.
- **7-8**: Good CS experience + enterprise accounts + metrics-driven.
- **5-6**: CS experience but gaps in enterprise or expansion.
- **3-4**: Limited SaaS or mostly support.
- **1-2**: Not a fit.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: Salesforce, Gainsight, HubSpot, Gong, top SaaS
2. **Strong Metrics**: NRR, retention, expansion revenue
3. **Enterprise Experience**: Managed $100K+ ARR accounts

## Score Boosters (+1 point each)
1. **Good Companies**: Established SaaS, well-funded startups
2. **Technical Aptitude**: Can understand product deeply
3. **Upsell/Expansion**: Proven revenue expansion

## Auto-Disqualifiers (Score 3 or below)
- **Support Only**: No strategic account management
- **No SaaS Experience**: Only non-tech CS
- **No Metrics**: Can't demonstrate retention/expansion impact
- **SMB Only for Enterprise Role**: No large account experience
- **Job Hopper**: Multiple companies <1.5 years each

## Experience Guidelines
- Ideal: 4-10 years in CS/account management
- Look for retention and expansion numbers
- Enterprise experience for enterprise roles"""
    },
    'solutions_engineer_global': {
        'name': 'Solutions Engineer - Global',
        'keywords': ['solutions engineer', 'se', 'sales engineer', 'presales', 'technical sales', 'demo', 'poc', 'remote', 'us', 'emea'],
        'prompt': """You are an expert recruiter for B2B SaaS companies hiring Solutions Engineers globally.

## Scoring Rubric
- **9-10**: Top company + technical depth + sales acumen + 4-10 years.
- **7-8**: Good SE experience + strong demos + complex POCs.
- **5-6**: SE experience but gaps in technical or sales.
- **3-4**: Limited SE or mostly support.
- **1-2**: Not a fit.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: Datadog, Snowflake, HashiCorp, Salesforce, top SaaS
2. **Technical Background**: Engineering before SE
3. **Complex POCs**: Led technical evaluations for enterprise

## Score Boosters (+1 point each)
1. **Good Companies**: Established SaaS, well-funded startups
2. **Quota Involvement**: Tied to sales quota
3. **Industry Expertise**: Security, data, cloud, DevOps

## Auto-Disqualifiers (Score 3 or below)
- **Support Only**: No sales involvement
- **No Technical Depth**: Can't go deep on product
- **No SaaS Experience**: Only hardware/on-prem
- **Job Hopper**: Multiple companies <1.5 years each

## Experience Guidelines
- Ideal: 4-10 years, mix of technical + customer-facing
- Engineering background is strong signal
- Look for enterprise deal involvement"""
    },
    # ==================== GLOBAL ENGINEERING (Generic) ====================
    'engineering_global': {
        'name': 'Engineering - Global',
        'keywords': ['software engineer', 'developer', 'engineering', 'remote', 'us', 'usa', 'europe', 'global', 'san francisco', 'new york', 'london'],
        'prompt': """You are an expert technical recruiter for high-growth tech companies hiring globally.

## Scoring Rubric
- **9-10**: FAANG/top startup + modern stack + 4-10 years + strong impact.
- **7-8**: Good company background + solid skills + stable tenure.
- **5-6**: Decent experience but gaps in company or stack.
- **3-4**: Weak company background or old stack.
- **1-2**: Not a fit.

## Score Boosters (+2 points each)
1. **Top Tier Companies**: Google, Meta, Amazon, Apple, Netflix, Stripe, Datadog, Snowflake
2. **Top Universities**: MIT, Stanford, CMU, Berkeley, top CS programs
3. **Strong Impact**: Led major projects, scaled systems

## Score Boosters (+1 point each)
1. **Good Companies**: Microsoft, well-funded startups, public tech companies
2. **Modern Stack**: Cloud-native, modern languages, distributed systems
3. **Open Source**: Contributions to major projects

## Auto-Disqualifiers (Score 3 or below)
- **Agencies/Consulting**: Accenture, Deloitte, Infosys, Wipro, TCS for engineering
- **Old Stack Only**: Legacy systems without modern experience
- **Job Hopper**: Multiple companies <1.5 years each
- **Too Long at One Company**: 7+ years (may be stagnant)
- **Non-tech Industries**: Banks, government, insurance, telecom (unless tech role)

## Experience Guidelines
- Ideal: 4-10 years total experience
- Company brand matters
- Modern stack and cloud experience important"""
    },
    # ==================== OTHER ROLES (Generic) ====================
    'manager': {
        'name': 'Engineering Manager',
        'keywords': ['engineering manager', 'em', 'dev manager', 'development manager', 'r&d manager', 'software manager'],
        'prompt': """You are an expert recruiter specializing in engineering management roles.

## Scoring Rubric
- **9-10**: Proven EM. Multiple team building cycles. Technical credibility. Strong delivery.
- **7-8**: Good EM experience. Built teams. Delivered projects.
- **5-6**: New manager or manager with gaps in team size/impact.
- **3-4**: Limited management experience.
- **1-2**: No management background.

## Score Boosters (+1 to +2 points)
1. **Strong Companies**: EM at Wiz, Monday, Google, Meta, scaled startups
2. **Team Size**: Managed 8+ engineers
3. **Hiring Track Record**: Built team from scratch
4. **Technical Background**: Was a strong engineer first

## Auto-Disqualifiers (Score 3 or below)
- **Only project management**: No people management
- **No technical background**: Can't evaluate engineers
- **Only offshore/outsource management**: Different skill set
- **HR/admin focus**: Not engineering management

## Guidelines
1. People skills + technical credibility both matter
2. Look for hiring and team growth
3. Delivery track record is key"""
    },
    'vp': {
        'name': 'VP / Director Engineering',
        'keywords': ['vp engineering', 'vp r&d', 'director engineering', 'head of engineering', 'cto', 'chief technology', 'vp product'],
        'prompt': """You are an expert executive recruiter specializing in VP/Director level engineering roles.

## Scoring Rubric
- **9-10**: Proven VP/Director. Built orgs of 30+. Strategic impact. Board/exec presence.
- **7-8**: Strong director. Multiple teams. Good strategic thinking.
- **5-6**: Senior manager ready to step up, or director with limited scope.
- **3-4**: Limited org building experience.
- **1-2**: No executive experience.

## Score Boosters (+1 to +2 points)
1. **Strong Companies**: VP at unicorn, FAANG director, successful startup CTO
2. **Org Scale**: Built 50+ person engineering org
3. **Business Impact**: Revenue/product outcomes, not just delivery
4. **Board Exposure**: M&A, fundraising, exec team experience

## Auto-Disqualifiers (Score 3 or below)
- **Only managed managers**: No org building
- **Only 1 team**: Not real VP scope
- **No strategic work**: Pure execution role
- **Outdated tech context**: >5 years since hands-on

## Guidelines
1. Strategy and org building are key
2. Business acumen matters at this level
3. Look for scale and complexity"""
    },
    'datascience': {
        'name': 'Data Science / ML',
        'keywords': ['data science', 'data scientist', 'machine learning', 'ml engineer', 'ai', 'deep learning', 'nlp', 'computer vision', 'mlops'],
        'prompt': """You are an expert technical recruiter specializing in Data Science and Machine Learning roles.

## Scoring Rubric
- **9-10**: Strong ML/DS background. Production models. Research publications or real impact.
- **7-8**: Solid data science skills. End-to-end model development.
- **5-6**: Some ML experience but gaps in production or depth.
- **3-4**: Limited ML experience, mostly analytics.
- **1-2**: No data science background.

## Score Boosters (+1 to +2 points)
1. **Strong Companies**: Google AI, Meta AI, OpenAI, DeepMind, top ML teams
2. **Education**: PhD in ML/Stats, top programs (Stanford, CMU, MIT, Technion)
3. **Publications**: NeurIPS, ICML, top conferences
4. **Production ML**: Models serving millions of users

## Auto-Disqualifiers (Score 3 or below)
- **Only BI/Analytics**: No ML modeling experience
- **Only Kaggle**: No production experience
- **Data Engineering only**: Not model development
- **Outdated ML**: Only classical ML, no deep learning

## Guidelines
1. Production experience > research alone
2. Look for end-to-end ownership
3. Strong CS fundamentals matter"""
    },
    'automation': {
        'name': 'QA Automation / SDET',
        'keywords': ['qa automation', 'sdet', 'test automation', 'quality engineer', 'automation engineer', 'selenium', 'cypress', 'playwright', 'test engineer'],
        'prompt': """You are an expert technical recruiter specializing in QA Automation and SDET roles.

## Scoring Rubric
- **9-10**: Strong automation architect. Framework design. CI/CD integration. Performance testing.
- **7-8**: Solid automation engineer. Good coverage. Multiple frameworks.
- **5-6**: Some automation but gaps in framework design or coverage.
- **3-4**: Mostly manual QA with some automation.
- **1-2**: Manual QA only.

## Score Boosters (+1 to +2 points)
1. **Strong Companies**: SDET at Google, Microsoft, Wix, Monday, top startups
2. **Framework Design**: Built automation frameworks from scratch
3. **Full Stack Testing**: UI + API + Performance
4. **CI/CD**: Deep integration with pipelines

## Auto-Disqualifiers (Score 3 or below)
- **Manual QA only**: No automation experience
- **Only record/playback**: No real coding
- **Only mobile/only web**: Too narrow for full stack
- **No programming**: Can't write maintainable tests

## Guidelines
1. Coding skills matter - treat as engineer
2. Framework design > just writing tests
3. Look for CI/CD and DevOps mindset"""
    },
    'general': {
        'name': 'General',
        'keywords': [],
        'prompt': """You are an expert recruiter evaluating candidates for a role.

## Scoring Rubric
- **9-10**: Exceptional match. Meets all requirements with bonus qualifications.
- **7-8**: Strong match. Meets all core requirements.
- **5-6**: Partial match. Missing 1-2 requirements but has potential.
- **3-4**: Weak match. Missing multiple requirements.
- **1-2**: Not a fit.

## Guidelines
1. Match skills and experience to job requirements
2. Consider company background and career progression
3. Look for evidence of impact and achievements
4. Be calibrated - 10/10 is rare"""
    }
}

# Legacy default (for backwards compatibility)
DEFAULT_SCREENING_PROMPT = DEFAULT_PROMPTS['backend_israel']['prompt']


def get_screening_prompt_for_role(role_type: str = None, job_description: str = None) -> tuple:
    """Get screening prompt, either by role or auto-detected from JD.

    Returns: (prompt_text, role_type, role_name)
    """
    db_client = _get_db_client() if HAS_DATABASE else None

    # If role specified, get that prompt
    if role_type and role_type != 'auto':
        if db_client:
            prompt_data = get_screening_prompt_by_role(db_client, role_type)
            if prompt_data:
                return prompt_data['prompt_text'], role_type, prompt_data.get('name', role_type.title())
        # Fall back to defaults
        if role_type in DEFAULT_PROMPTS:
            return DEFAULT_PROMPTS[role_type]['prompt'], role_type, DEFAULT_PROMPTS[role_type]['name']

    # Auto-detect from job description
    if job_description and db_client:
        matched = match_prompt_by_keywords(db_client, job_description)
        if matched:
            return matched['prompt_text'], matched['role_type'], matched.get('name', matched['role_type'].title())

    # Auto-detect using default prompts (no DB)
    if job_description:
        jd_lower = job_description.lower()
        best_match = None
        best_score = 0
        for role_key, role_data in DEFAULT_PROMPTS.items():
            if role_key == 'general':
                continue
            score = sum(1 for kw in role_data['keywords'] if kw in jd_lower)
            if score > best_score:
                best_score = score
                best_match = role_key
        if best_score >= 2 and best_match:
            return DEFAULT_PROMPTS[best_match]['prompt'], best_match, DEFAULT_PROMPTS[best_match]['name']

    # Fall back to general/default
    if db_client:
        default_prompt = get_default_screening_prompt(db_client)
        if default_prompt:
            return default_prompt['prompt_text'], default_prompt['role_type'], default_prompt.get('name', 'Default')

    return DEFAULT_SCREENING_PROMPT, 'backend', 'Backend Engineering'


def get_screening_prompt() -> str:
    """Legacy function for backwards compatibility."""
    prompt, _, _ = get_screening_prompt_for_role()
    return prompt


def screen_profile(profile: dict, job_description: str, client: OpenAI, extra_requirements: str = "", tracker: 'UsageTracker' = None, mode: str = "detailed", system_prompt: str = None) -> dict:
    """Screen a profile against a job description using OpenAI.

    Args:
        mode: "quick" for cheaper/faster (score + fit + summary) or "detailed" for full analysis
        system_prompt: Custom system prompt (if None, uses default)
    """
    # Validate profile has minimum useful data before calling OpenAI
    name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    title = profile.get('current_title', '') or profile.get('headline', '')
    company = profile.get('current_company', '')
    has_useful_data = bool(name and (title or company or profile.get('skills') or profile.get('past_positions') or profile.get('summary')))

    if not has_useful_data:
        return {
            "score": 0,
            "fit": "Skipped",
            "summary": "Insufficient profile data - skipped to save API credits",
            "strengths": [],
            "concerns": []
        }

    start_time = time.time()

    # Build concise profile summary for the prompt (with safe string conversion)
    def safe_str(value, max_len=500):
        if value is None:
            return 'N/A'
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        return str(value)[:max_len] if value else 'N/A'

    profile_summary = f"""Name: {safe_str(profile.get('first_name', ''))} {safe_str(profile.get('last_name', ''))}
Title: {safe_str(profile.get('current_title', profile.get('headline', 'N/A')))}
Company: {safe_str(profile.get('current_company', 'N/A'))}
Location: {safe_str(profile.get('location', 'N/A'))}
Education: {safe_str(profile.get('education', profile.get('all_schools', 'N/A')))}
Skills: {safe_str(profile.get('skills', 'N/A'))}
Summary: {safe_str(profile.get('summary', 'N/A'))}
Past Positions: {safe_str(profile.get('past_positions', 'N/A'))}"""

    # Different prompts based on mode
    if mode == "quick":
        response_format = '{"score": <1-10>, "fit": "<Strong Fit|Good Fit|Partial Fit|Not a Fit>", "summary": "<one sentence>"}'
        max_tokens = 100
    else:
        response_format = '{"score": <1-10>, "fit": "<Strong Fit|Good Fit|Partial Fit|Not a Fit>", "summary": "<2-3 sentences about the candidate>", "strengths": ["<strength1>", "<strength2>"], "concerns": ["<concern1>", "<concern2>"]}'
        max_tokens = 400

    user_prompt = f"""Evaluate this candidate against the job description.

## Job Description:
{job_description}

{f"## Extra Requirements:{chr(10)}{extra_requirements}" if extra_requirements else ""}

## Candidate Profile:
{profile_summary}

Respond with ONLY valid JSON in this exact format:
{response_format}"""

    try:
        # Use provided prompt or fall back to default
        prompt_to_use = system_prompt if system_prompt else get_screening_prompt()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt_to_use},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=max_tokens
        )
        elapsed_ms = int((time.time() - start_time) * 1000)

        # Log usage with token counts
        if tracker and hasattr(response, 'usage') and response.usage:
            tracker.log_openai(
                tokens_input=response.usage.prompt_tokens,
                tokens_output=response.usage.completion_tokens,
                model='gpt-4o-mini',
                profiles_screened=1,
                status='success',
                response_time_ms=elapsed_ms
            )

        content = response.choices[0].message.content.strip()
        # Handle potential markdown code blocks
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()

        result = json.loads(content)
        return result
    except json.JSONDecodeError as e:
        return {
            "score": 0,
            "fit": "Error",
            "summary": f"JSON parse error: {str(e)[:80]}",
            "strengths": [],
            "concerns": []
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        if tracker:
            tracker.log_openai(
                tokens_input=0,
                tokens_output=0,
                model='gpt-4o-mini',
                profiles_screened=0,
                status='error',
                error_message=str(e)[:200],
                response_time_ms=elapsed_ms
            )
        return {
            "score": 0,
            "fit": "Error",
            "summary": f"API error: {str(e)[:80]}",
            "strengths": [],
            "concerns": []
        }


def screen_profiles_batch(profiles: list, job_description: str, openai_api_key: str,
                          extra_requirements: str = "", max_workers: int = 50,
                          progress_callback=None, cancel_flag=None, mode: str = "detailed",
                          system_prompt: str = None) -> list:
    """Screen multiple profiles in parallel using ThreadPoolExecutor.

    Args:
        profiles: List of profile dicts to screen
        job_description: The job description to screen against
        openai_api_key: OpenAI API key (we create client per thread for safety)
        extra_requirements: Additional screening criteria
        max_workers: Number of concurrent threads (default 50 for Tier 3)
        progress_callback: Function(completed, total, result) called after each profile
        cancel_flag: Dict with 'cancelled' key to check for cancellation
        system_prompt: Custom system prompt for screening

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
        client = OpenAI(api_key=openai_api_key)
        try:
            result = screen_profile(profile, job_description, client, extra_requirements, tracker=tracker, mode=mode, system_prompt=system_prompt)
            # Add profile info to result
            name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
            if not name:
                name = profile.get('full_name', '') or profile.get('fullName', '') or f"Profile {index}"
            result['name'] = name
            result['current_title'] = profile.get('current_title', '') or profile.get('headline', '') or profile.get('title', '') or ''
            result['current_company'] = profile.get('current_company', '') or profile.get('companyName', '') or profile.get('company', '') or ''
            result['linkedin_url'] = profile.get('linkedin_url', '') or profile.get('public_url', '') or profile.get('defaultProfileUrl', '') or ''
            result['index'] = index
        except Exception as e:
            import traceback
            result = {
                "score": 0,
                "fit": "Error",
                "summary": f"Screen error: {str(e)[:80]}",
                "strengths": [],
                "concerns": [],
                "name": profile.get('first_name', '') or profile.get('fullName', '') or f"Profile {index}",
                "current_title": "",
                "current_company": "",
                "linkedin_url": "",
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
                    "strengths": [],
                    "concerns": [],
                    "name": f"Profile {idx}",
                    "current_title": "",
                    "current_company": "",
                    "linkedin_url": "",
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
    _api_status = {
        'Crustdata': bool(config.get('api_key')),
        'OpenAI': bool(config.get('openai_api_key')),
        'PhantomBuster': bool(config.get('phantombuster_api_key')),
        'SalesQL': bool(config.get('salesql_api_key')),
        'Google Sheets': bool(config.get('google_credentials')),
        'Supabase': bool(HAS_DATABASE and (config.get('supabase_url') or (hasattr(st, 'secrets') and 'supabase_url' in st.secrets))),
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

# Main UI
st.title("SourcingX")

# Check API keys
api_key = load_api_key()
has_crust_key = api_key and api_key != "YOUR_CRUSTDATA_API_KEY_HERE"

# Show data status in header
if 'results' in st.session_state and st.session_state['results']:
    st.info(f"📊 **{len(st.session_state['results'])}** profiles loaded")

# Create tabs
tab_upload, tab_filter, tab_enrich, tab_filter2, tab_screening, tab_database, tab_usage = st.tabs([
    "1. Load", "2. Filter", "3. Enrich", "4. Filter+", "5. AI Screen", "6. Database", "7. Usage"
])

# ========== TAB 1: Upload ==========
with tab_upload:
    # ===== Resume Last Session =====
    has_local_session = SESSION_FILE.exists()

    # Show restore options if there's data to restore
    if has_local_session or HAS_DATABASE:
        with st.expander("Resume Last Session", expanded=False):
            # Local session restore (includes filtered data)
            if has_local_session:
                st.markdown("**Local Session** (includes filtered data)")
                col_local1, col_local2 = st.columns([3, 1])
                with col_local1:
                    if st.button("Restore Last Session", key="restore_local_session", type="primary"):
                        if load_session_state():
                            st.success("Session restored!")
                            st.rerun()
                        else:
                            st.error("Failed to restore session")
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
                        from db import get_profiles_by_status

                        @st.cache_data(ttl=120)
                        def _get_db_restore_counts():
                            c = _get_db_client()
                            return (
                                c.count('profiles', {'status': 'eq.enriched'}),
                                c.count('profiles', {'status': 'eq.screened'}),
                            )

                        enriched_count, screened_count = _get_db_restore_counts()

                        if enriched_count > 0 or screened_count > 0:
                            st.markdown("**From Database**")
                            st.caption("Load enriched or screened profiles from Supabase")

                            col1, col2, col3 = st.columns(3)
                            with col1:
                                if enriched_count > 0 and st.button(f"Load Enriched ({enriched_count})", key="resume_enriched"):
                                    profiles = get_profiles_by_status(db_client, "enriched", limit=1000)
                                    if profiles:
                                        df = profiles_to_dataframe(profiles)
                                        st.session_state['results'] = profiles
                                        st.session_state['results_df'] = df
                                        st.session_state['enriched_results'] = profiles
                                        st.session_state['enriched_df'] = df
                                        st.success(f"Loaded {len(profiles)} enriched profiles!")
                                        st.rerun()

                            with col2:
                                if screened_count > 0 and st.button(f"Load Screened ({screened_count})", key="resume_screened"):
                                    profiles = get_profiles_by_status(db_client, "screened", limit=1000)
                                    if profiles:
                                        df = profiles_to_dataframe(profiles)
                                        st.session_state['results'] = profiles
                                        st.session_state['results_df'] = df
                                        st.session_state['enriched_results'] = profiles
                                        st.session_state['enriched_df'] = df
                                        screening_results = []
                                        for p in profiles:
                                            screening_results.append({
                                                'name': f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                                                'score': p.get('screening_score', 0),
                                                'fit': p.get('screening_fit_level', ''),
                                                'summary': p.get('screening_summary', ''),
                                                'why': p.get('screening_reasoning', ''),
                                                'current_title': p.get('current_title', ''),
                                                'current_company': p.get('current_company', ''),
                                                'linkedin_url': p.get('linkedin_url', ''),
                                                'strengths': [],
                                                'concerns': []
                                            })
                                        st.session_state['screening_results'] = screening_results
                                        st.success(f"Loaded {len(profiles)} screened profiles!")
                                        st.rerun()

                            with col3:
                                all_count = enriched_count + screened_count
                                if st.button(f"Load All ({all_count})", key="resume_all"):
                                    profiles = get_all_profiles(db_client, limit=2000)
                                    if profiles:
                                        df = profiles_to_dataframe(profiles)
                                        st.session_state['results'] = profiles
                                        st.session_state['results_df'] = df
                                        st.success(f"Loaded {len(profiles)} profiles!")
                                        st.rerun()
                except Exception as e:
                    pass  # Silently fail if database not available

    # ===== File Upload Section =====
    st.markdown("### Upload File")
    pre_enriched_file = st.file_uploader(
        "Upload pre-enriched CSV or JSON",
        type=['csv', 'json'],
        key="pre_enriched_upload"
    )

    if pre_enriched_file:
        try:
            if pre_enriched_file.name.endswith('.json'):
                pre_enriched_data = json.load(pre_enriched_file)
                if isinstance(pre_enriched_data, list):
                    st.session_state['results'] = pre_enriched_data
                    st.session_state['results_df'] = flatten_for_csv(pre_enriched_data)
                    st.success(f"Loaded **{len(pre_enriched_data)}** profiles!")
            else:
                pre_enriched_file.seek(0)
                df_uploaded = pd.read_csv(pre_enriched_file, encoding='utf-8')

                # Normalize columns (handles GEM and other CSV formats)
                df_uploaded = normalize_uploaded_csv(df_uploaded)

                # Get count of skipped profiles (no LinkedIn URL)
                skipped_no_url = df_uploaded.attrs.get('_skipped_no_url', 0)

                # PhantomBuster/CSV data stays in session state only (not saved to DB)
                # DB save happens after Crustdata enrichment
                st.session_state['results'] = df_uploaded.to_dict('records')
                st.session_state['results_df'] = df_uploaded
                save_session_state()  # Save for restore

                # Show success message with details
                msg = f"Loaded **{len(df_uploaded)}** profiles"
                if skipped_no_url > 0:
                    msg += f" ({skipped_no_url} skipped - no LinkedIn URL)"
                st.success(msg)

        except Exception as e:
            st.error(f"Error: {e}")

    # ===== Preview =====
    # Shows loaded results from PhantomBuster or CSV upload
    if 'results' in st.session_state and st.session_state['results']:
        results_df = st.session_state.get('results_df')
        if results_df is not None and not results_df.empty:
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
                    st.success(f"Loaded **{load_count}** profiles from **{load_file}** - ready for enrichment")

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
                    use_container_width=True,
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
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                        }
                    )
                else:
                    st.dataframe(page_df.head(10), use_container_width=True, hide_index=True)

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
            st.info("**Next step:** Click on **2. Filter** tab to filter profiles (optional) or **3. Enrich** to enrich directly")

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
                # Load search history for this agent (cached to avoid DB call on every rerun)
                @st.cache_data(ttl=60)
                def _load_search_history_cached(agent_id):
                    return load_search_history(agent_id=agent_id)

                search_history = _load_search_history_cached(agent_id=selected_agent['id'])

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
                                        _load_search_history_cached.clear()
                                        st.success(f"Deleted {csv_to_delete}")
                                        st.rerun()
                                    else:
                                        st.error("Failed to delete")
                        with col_no:
                            if st.button("Cancel", key="pb_confirm_no"):
                                st.session_state['pb_confirm_delete'] = None
                                st.rerun()

                    # Show current loaded count if any
                    existing_count = len(st.session_state.get('results', []))
                    if existing_count > 0:
                        st.caption(f"Currently loaded: **{existing_count}** profiles")

                    # Option to accumulate results
                    col_load, col_add = st.columns([1, 1])

                    with col_load:
                        if st.button("Load Results", type="primary", key="pb_load_btn", use_container_width=True, help="Replace current results"):
                            if selected_search:
                                with st.spinner("Loading results..."):
                                    filename = selected_search['csv_name']
                                    pb_df = fetch_phantombuster_result_csv(pb_key, selected_agent['id'], debug=False, filename=filename)
                                    if not pb_df.empty:
                                        pb_df = normalize_phantombuster_columns(pb_df)

                                        # PhantomBuster data stays in session state only (not saved to DB)
                                        # DB save happens after Crustdata enrichment
                                        st.session_state['results'] = pb_df.to_dict('records')
                                        st.session_state['results_df'] = pb_df
                                        st.session_state['preview_page'] = 0  # Reset pagination
                                        st.session_state['last_load_count'] = len(pb_df)
                                        st.session_state['last_load_file'] = filename
                                        save_session_state()  # Save for restore
                                        st.rerun()
                                    else:
                                        st.error("No results found. File may have been deleted from PhantomBuster.")

                    with col_add:
                        if st.button("+ Add to Results", key="pb_add_btn", use_container_width=True, help="Add to current results"):
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
                                            st.session_state['results'] = combined_df.to_dict('records')
                                            st.session_state['results_df'] = combined_df
                                            st.session_state['last_load_count'] = new_count
                                            st.session_state['last_load_file'] = filename
                                            st.session_state['last_load_mode'] = 'added'
                                            st.session_state['last_load_total'] = len(combined_df)
                                        else:
                                            st.session_state['results'] = pb_df.to_dict('records')
                                            st.session_state['results_df'] = pb_df
                                            st.session_state['last_load_count'] = len(pb_df)
                                            st.session_state['last_load_file'] = filename
                                            st.session_state['last_load_mode'] = 'loaded'

                                        st.session_state['preview_page'] = 0
                                        save_session_state()  # Save for restore
                                        st.rerun()
                                    else:
                                        st.error("No results found. File may have been deleted from PhantomBuster.")
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
                    # Desktop notification
                    try:
                        profiles = status_result.get('profiles_count', 0)
                        msg = f"Extracted {profiles} profiles" if profiles else "Ready to load results"
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
                    # Desktop notification for error
                    try:
                        if HAS_PLYER:
                            notification.notify(
                                title="PhantomBuster Error",
                                message=status_result.get('exitMessage', 'Phantom failed'),
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

            # Show progress bar if we have percentage
            if progress_pct > 0:
                st.progress(progress_pct / 100)

            # Cancel button
            if st.button("Cancel", key="pb_cancel_btn"):
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
                        st.session_state['results'] = pb_df.to_dict('records')
                        st.session_state['results_df'] = pb_df
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
            st.error(f"Error: {st.session_state['pb_launch_error']}")
            if st.button("Reset", key="pb_reset_btn"):
                st.session_state['pb_launch_status'] = 'idle'
                st.session_state['pb_launch_error'] = None
                st.session_state['pb_launch_csv_name'] = None
                st.session_state['pb_progress_info'] = {}
                st.session_state['pb_launch_skip_count'] = 0
                st.rerun()

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
                            _load_search_history_cached.clear()
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
    if 'results' not in st.session_state or not st.session_state['results']:
        st.info("Upload data in the Upload tab first.")
    else:
        df = st.session_state['results_df']
        # Store original data if not already stored (for reset functionality)
        if 'original_results_df' not in st.session_state or st.session_state.get('original_results_df') is None:
            st.session_state['original_results_df'] = df.copy()
        needs_filtering = 'job_1_job_title' in df.columns and 'current_title' not in df.columns

        if needs_filtering:
            if st.button("Convert to Screening Format"):
                filtered_df = filter_csv_columns(df)
                st.session_state['results_df'] = filtered_df
                st.session_state['results'] = filtered_df.to_dict('records')
                save_session_state()  # Save for restore
                st.rerun()

        # Check for Google Sheets config
        filter_sheets = get_filter_sheets_config().copy()
        gspread_client = get_gspread_client()

        # Allow user to specify their own filter sheet
        st.markdown("**Your Filter Sheet:**")
        st.caption(f"Share your sheet with: `linkedin-enricher@linkedin-enricher-485616.iam.gserviceaccount.com`")
        user_sheet_url = st.text_input(
            "Google Sheet URL (paste your own, or leave empty for default)",
            value=st.session_state.get('user_sheet_url', ''),
            placeholder="https://docs.google.com/spreadsheets/d/...",
            key="user_sheet_input"
        )

        # Save to session state and set default tab names
        if user_sheet_url:
            st.session_state['user_sheet_url'] = user_sheet_url
            filter_sheets['url'] = user_sheet_url
            # Set default tab names if not already configured
            if not filter_sheets.get('past_candidates'):
                filter_sheets['past_candidates'] = 'Past Candidates'
            if not filter_sheets.get('blacklist'):
                filter_sheets['blacklist'] = 'Blacklist'
            if not filter_sheets.get('not_relevant'):
                filter_sheets['not_relevant'] = 'NotRelevant Companies'

        has_sheets = bool(filter_sheets.get('url')) and gspread_client is not None

        if filter_sheets.get('url') and gspread_client is None:
            st.error("Google credentials not configured. Cannot connect to Google Sheets.")

        if has_sheets:
            # Try to fetch and display sheet name
            try:
                spreadsheet = gspread_client.open_by_url(filter_sheets['url'])
                sheet_name = spreadsheet.title
                if user_sheet_url:
                    st.success(f"📊 **{sheet_name}** (your personal filter sheet)")
                else:
                    st.success(f"📊 **{sheet_name}** (default filter sheet)")
            except Exception:
                if user_sheet_url:
                    st.success("Using your personal filter sheet")
                else:
                    st.success("Using default filter sheet")

            # Validate sheet connection
            if st.button("Verify Sheet Connection", key="verify_sheet"):
                with st.spinner("Checking sheet access..."):
                    try:
                        spreadsheet = gspread_client.open_by_url(filter_sheets['url'])
                        tabs = [ws.title for ws in spreadsheet.worksheets()]
                        st.success(f"Connected! Found {len(tabs)} tabs")
                        expected_tabs = ['Past Candidates', 'Blacklist', 'NotRelevant Companies', 'Target Companies', 'Universities', 'Tech Alerts']
                        missing = [t for t in expected_tabs if t not in tabs]
                        if missing:
                            st.warning(f"Missing expected tabs: {', '.join(missing)}")
                        else:
                            st.info(f"All expected tabs found")
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

        with col2:
            st.markdown("**Title Keywords Filter:**")
            st.caption("Exclude profiles with these keywords in title")

            # Predefined exclusion keywords by category
            EXCLUDE_CATEGORIES = {
                "Leadership": ["vp", "director", "manager", "head of"],
                "C-Level/Founders": ["cto", "ceo", "coo", "cfo", "owner", "founder", "co-founder"],
                "Non-Employee": ["freelancer", "self employed", "consultant"],
                "Junior": ["student", "intern", "junior"],
                "Technical": ["qa", "automation", "embedded", "low level", "real time", "hardware", "design"]
            }
            ALL_EXCLUDE_TITLES = [kw for keywords in EXCLUDE_CATEGORIES.values() for kw in keywords]

            # Quick select buttons
            st.caption("Quick select:")
            btn_cols = st.columns(len(EXCLUDE_CATEGORIES) + 1)
            with btn_cols[0]:
                if st.button("All", key="exc_all", use_container_width=True):
                    st.session_state['exclude_title_presets'] = ALL_EXCLUDE_TITLES
                    st.rerun()
            for i, (cat_name, cat_keywords) in enumerate(EXCLUDE_CATEGORIES.items()):
                with btn_cols[i + 1]:
                    if st.button(cat_name, key=f"exc_{cat_name}", use_container_width=True):
                        current = st.session_state.get('exclude_title_presets', [])
                        # Toggle: add if not all present, remove if all present
                        if all(kw in current for kw in cat_keywords):
                            st.session_state['exclude_title_presets'] = [k for k in current if k not in cat_keywords]
                        else:
                            st.session_state['exclude_title_presets'] = list(set(current + cat_keywords))
                        st.rerun()

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

            # Duration filters (from Phantom data)
            st.markdown("**Duration Filters:**")
            dur_col1, dur_col2 = st.columns(2)
            with dur_col1:
                min_role_months = st.number_input("Min months in role", min_value=0, max_value=120, value=0, key="min_role_months")
                min_company_months = st.number_input("Min months at company", min_value=0, max_value=120, value=0, key="min_company_months")
            with dur_col2:
                max_role_months = st.number_input("Max months in role", min_value=0, max_value=240, value=0, help="0 = no limit", key="max_role_months")
                max_company_months = st.number_input("Max months at company", min_value=0, max_value=240, value=0, help="0 = no limit", key="max_company_months")

        btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 2])
        with btn_col1:
            apply_clicked = st.button("Apply Filters", type="primary", key="apply_filters_main")
        with btn_col2:
            if st.button("Reset Filters", key="reset_filters_main"):
                # Reset to original unfiltered data
                original_df = st.session_state.get('original_results_df')
                if original_df is not None and not original_df.empty:
                    st.session_state['results_df'] = original_df.copy()
                    st.session_state['results'] = original_df.to_dict('records')
                    st.session_state['passed_candidates_df'] = original_df.copy()
                    if 'filter_stats' in st.session_state:
                        del st.session_state['filter_stats']
                    if 'filtered_out' in st.session_state:
                        del st.session_state['filtered_out']
                    st.success(f"Reset to {len(original_df)} profiles")
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
                        st.info(f"Loaded {len(past_df)} past candidates from Google Sheet")
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

                st.session_state['passed_candidates_df'] = filtered_df  # Store filtered results separately
                st.session_state['results_df'] = filtered_df
                st.session_state['results'] = filtered_df.to_dict('records')
                st.session_state['filter_stats'] = stats
                st.session_state['filtered_out'] = filtered_out
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

    # View passed candidates section (only show after filtering)
    if 'filter_stats' in st.session_state and 'passed_candidates_df' in st.session_state:
        st.divider()
        st.markdown("### View Passed Candidates")
        st.caption("Browse candidates that passed all filters, with priority categorization")

        passed_df = st.session_state['passed_candidates_df']

        # Priority categorization section
        filter_sheets = get_filter_sheets_config()
        gspread_client = get_gspread_client()
        has_sheets = bool(filter_sheets.get('url')) and gspread_client is not None

        if has_sheets and st.button("Load Priority Categories", key="apply_categories"):
            sheet_url = filter_sheets.get('url', '')

            # Helper function for matching
            def normalize_company(name):
                if pd.isna(name) or not str(name).strip():
                    return ''
                name = str(name).lower().strip()
                for suffix in [' ltd', ' inc', ' corp', ' llc', ' limited', ' israel', ' il', ' technologies', ' tech', ' software', ' solutions', ' group']:
                    if name.endswith(suffix):
                        name = name[:-len(suffix)].strip()
                return name

            def matches_list(company, company_list):
                if pd.isna(company) or not str(company).strip():
                    return False
                company_norm = normalize_company(company)
                if not company_norm:
                    return False
                for c in company_list:
                    c_norm = normalize_company(c)
                    if not c_norm:
                        continue
                    if company_norm == c_norm:
                        return True
                    if len(c_norm) >= 4 and len(company_norm) >= 4:
                        if company_norm.startswith(c_norm) or c_norm.startswith(company_norm):
                            return True
                return False

            def matches_list_in_text(text, items_list):
                """Stricter matching for universities - avoids partial matches like
                'Tel Aviv University' matching 'Afeka Tel Aviv College'."""
                if pd.isna(text) or not str(text).strip():
                    return False
                text_lower = str(text).lower()
                text_parts = [p.strip() for p in text_lower.replace('|', ',').split(',')]

                for item in items_list:
                    item_norm = str(item).lower().strip()
                    if not item_norm or len(item_norm) < 3:
                        continue

                    for text_part in text_parts:
                        # Exact match
                        if text_part == item_norm:
                            return True
                        # Text part starts with item
                        if text_part.startswith(item_norm):
                            return True
                        # Item starts with text part (if substantial)
                        if item_norm.startswith(text_part) and len(text_part) > 10:
                            return True
                        # Word-based matching for longer items
                        if len(item_norm) > 8:
                            item_words = set(item_norm.split())
                            text_words = set(text_part.split())
                            common = item_words & text_words
                            if len(common) >= len(item_words) * 0.7:
                                return True
                return False

            with st.spinner("Loading priority lists..."):
                # Target companies
                if filter_sheets.get('target_companies'):
                    tc_df = load_sheet_as_df(sheet_url, filter_sheets['target_companies'])
                    if tc_df is not None and len(tc_df.columns) > 0:
                        target_companies = []
                        for col in tc_df.columns:
                            if 'company' in col.lower() or 'name' in col.lower():
                                target_companies.extend(tc_df[col].dropna().tolist())
                        target_list = [str(c).lower().strip() for c in target_companies if c]
                        passed_df['is_target_company'] = passed_df['current_company'].apply(lambda x: matches_list(x, target_list))
                        st.info(f"Target Companies: {len(target_list)} loaded, {passed_df['is_target_company'].sum()} matches")

                # Layoff alerts
                if filter_sheets.get('tech_alerts'):
                    ta_df = load_sheet_as_df(sheet_url, filter_sheets['tech_alerts'])
                    if ta_df is not None and len(ta_df.columns) > 0:
                        tech_alerts = []
                        for col in ta_df.columns:
                            if 'company' in col.lower() or 'name' in col.lower():
                                tech_alerts.extend(ta_df[col].dropna().tolist())
                        alerts_list = [str(c).lower().strip() for c in tech_alerts if c]
                        passed_df['is_layoff_company'] = passed_df['current_company'].apply(lambda x: matches_list(x, alerts_list))
                        st.info(f"Layoff Alerts: {len(alerts_list)} loaded, {passed_df['is_layoff_company'].sum()} matches")

                # Universities (read ALL columns)
                if filter_sheets.get('universities') and 'education' in passed_df.columns:
                    uni_df = load_sheet_as_df(sheet_url, filter_sheets['universities'])
                    if uni_df is not None and len(uni_df.columns) > 0:
                        uni_list = []
                        for col in uni_df.columns:
                            uni_list.extend(uni_df[col].dropna().tolist())
                        uni_list = list(set(uni_list))  # Dedupe
                        passed_df['is_top_university'] = passed_df['education'].apply(lambda x: matches_list_in_text(x, uni_list))
                        st.info(f"Top Universities: {len(uni_list)} loaded ({len(uni_df.columns)} columns), {passed_df['is_top_university'].sum()} matches")

            # Save categorized data
            st.session_state['passed_candidates_df'] = passed_df
            st.session_state['categories_applied'] = True
            st.rerun()

        # Re-read from session state to get latest data with categories
        passed_df = st.session_state['passed_candidates_df']

        # Filter checkboxes at the top
        st.markdown("**Filter by category:**")
        filter_cols = st.columns(4)

        with filter_cols[0]:
            show_all = st.checkbox("All", value=True, key="filter_all")

        has_target = 'is_target_company' in passed_df.columns
        has_layoff = 'is_layoff_company' in passed_df.columns
        has_uni = 'is_top_university' in passed_df.columns

        with filter_cols[1]:
            if has_target:
                count = int(passed_df['is_target_company'].fillna(False).sum())
                show_target = st.checkbox(f"Target Companies ({count})", value=False, key="filter_target")
            else:
                show_target = False

        with filter_cols[2]:
            if has_layoff:
                count = int(passed_df['is_layoff_company'].fillna(False).sum())
                show_layoff = st.checkbox(f"Layoff Alerts ({count})", value=False, key="filter_layoff")
            else:
                show_layoff = False

        with filter_cols[3]:
            if has_uni:
                count = int(passed_df['is_top_university'].fillna(False).sum())
                show_uni = st.checkbox(f"Top Universities ({count})", value=False, key="filter_uni")
            else:
                show_uni = False

        # Filter based on checkboxes
        if show_all or (not show_target and not show_layoff and not show_uni):
            view_df = passed_df.copy()
        else:
            # Combine selected filters with OR
            mask = pd.Series([False] * len(passed_df), index=passed_df.index)
            if show_target and has_target:
                mask = mask | passed_df['is_target_company'].fillna(False)
            if show_layoff and has_layoff:
                mask = mask | passed_df['is_layoff_company'].fillna(False)
            if show_uni and has_uni:
                mask = mask | passed_df['is_top_university'].fillna(False)
            view_df = passed_df[mask].copy()

        st.success(f"**{len(view_df)}** candidates")

        # Display columns (without category columns)
        display_cols = ['first_name', 'last_name', 'current_title', 'current_company', 'education', 'location', 'public_url']
        available_cols = [c for c in display_cols if c in view_df.columns]

        if available_cols:
            st.dataframe(
                view_df[available_cols],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "public_url": st.column_config.LinkColumn("LinkedIn"),
                }
            )

        # Download passed candidates
        col1, col2 = st.columns(2)
        with col1:
            csv_data = view_df.to_csv(index=False)
            st.download_button(
                label="Download View (CSV)",
                data=csv_data,
                file_name="passed_candidates.csv",
                mime="text/csv"
            )

    # Review filtered candidates section
    if 'filtered_out' in st.session_state and st.session_state['filtered_out']:
        st.divider()
        st.markdown("### Review Filtered Candidates")
        st.caption("View candidates removed by each filter and restore selected ones")

        filtered_out = st.session_state['filtered_out']
        filter_names = [k for k, v in filtered_out.items() if len(v) > 0]

        if filter_names:
            selected_filter = st.selectbox("Select filter to review:", filter_names)

            if selected_filter and selected_filter in filtered_out:
                filter_df = filtered_out[selected_filter]
                st.info(f"**{len(filter_df)}** candidates removed by: {selected_filter}")

                # Show key columns for review
                display_cols = ['first_name', 'last_name', 'current_title', 'current_company', 'education', 'public_url']
                available_cols = [c for c in display_cols if c in filter_df.columns]

                if available_cols:
                    st.dataframe(
                        filter_df[available_cols],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "public_url": st.column_config.LinkColumn("LinkedIn")
                        }
                    )

                # Restore functionality
                st.markdown("**Restore candidates:**")
                restore_indices = st.multiselect(
                    f"Select rows to restore (by index):",
                    options=list(range(len(filter_df))),
                    format_func=lambda i: f"{filter_df.iloc[i].get('first_name', '')} {filter_df.iloc[i].get('last_name', '')} - {filter_df.iloc[i].get('current_company', '')}"
                )

                if st.button("Restore Selected", key="restore_btn"):
                    if restore_indices:
                        # Get candidates to restore
                        to_restore = filter_df.iloc[restore_indices]

                        # Add back to main results
                        current_df = st.session_state['results_df']
                        restored_df = pd.concat([current_df, to_restore], ignore_index=True)
                        st.session_state['results_df'] = restored_df
                        st.session_state['results'] = restored_df.to_dict('records')

                        # Remove from filtered_out
                        remaining = filter_df.drop(filter_df.index[restore_indices])
                        st.session_state['filtered_out'][selected_filter] = remaining

                        st.success(f"Restored {len(restore_indices)} candidates!")
                        st.rerun()
                    else:
                        st.warning("Select candidates to restore first")
        else:
            st.info("No candidates were filtered out")

    # ===== SalesQL Email Enrichment =====
    st.divider()
    st.markdown("### Email Enrichment (SalesQL)")
    salesql_key = load_salesql_key()
    if salesql_key:
        if 'results' in st.session_state and st.session_state['results']:
            current_count = len(st.session_state['results_df'])
            already_enriched = (st.session_state['results_df']['salesql_email'].notna() & (st.session_state['results_df']['salesql_email'] != '')).sum() if 'salesql_email' in st.session_state['results_df'].columns else 0
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
                    st.session_state['results_df'] = enriched_df
                    st.session_state['results'] = enriched_df.to_dict('records')
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
    st.info("**Next step:** Click on **3. Enrich** tab to enrich profiles with full LinkedIn data")

# ========== TAB 3: Enrich ==========
with tab_enrich:
    st.markdown("### Enrich with Crust Data API")
    st.caption("Add full LinkedIn profile data: work history, education, skills, and more")

    # Show enrichment result message if stored
    if 'enrichment_message' in st.session_state:
        msg = st.session_state.pop('enrichment_message')
        if msg.startswith('warning:'):
            st.warning(msg[8:])
        elif msg.startswith('success:'):
            st.success(msg[8:])
        else:
            st.info(msg)

    if not HAS_DATABASE:
        st.error("Supabase is not connected. Enrichment is disabled because results won't be saved. Check your supabase_url and supabase_key in secrets.")
    elif not has_crust_key:
        st.warning("Crust Data API key not configured. Add 'api_key' to config.json")
    elif 'results' not in st.session_state or not st.session_state['results']:
        st.info("Load profiles first (tab 1). Filtering (tab 2) is optional.")
    else:
        # Use filtered data if available (from Filter+ tab), otherwise use loaded data
        passed_df = st.session_state.get('passed_candidates_df')
        using_filtered = passed_df is not None and not passed_df.empty
        results_df = passed_df if using_filtered else st.session_state.get('results_df')
        enriched_df = st.session_state.get('enriched_df')

        # Show data source indicator
        if using_filtered:
            st.caption(f"Using filtered data ({len(results_df) if results_df is not None else 0} profiles from Filter+ tab)")
        else:
            st.caption(f"Using all loaded data ({len(results_df) if results_df is not None else 0} profiles)")

        # Check if already enriched (enriched_df exists)
        is_enriched = enriched_df is not None and not enriched_df.empty

        if is_enriched:
            st.success(f"**{len(enriched_df)}** profiles enriched! Proceed to Filter+ or AI Screen tab.")

            # Show URL matching debug info (persisted from last enrichment)
            if '_enrich_debug' in st.session_state or '_enrich_match_debug' in st.session_state:
                with st.expander("Debug: Last Enrichment URL Matching", expanded=True):
                    enrich_debug = st.session_state.get('_enrich_debug', {})
                    match_debug = st.session_state.get('_enrich_match_debug', {})

                    col1, col2 = st.columns(2)
                    with col1:
                        st.write("**Input URL Mapping:**")
                        st.write(f"- Input URLs: {enrich_debug.get('input_urls', 'N/A')}")
                        st.write(f"- Map keys created: {enrich_debug.get('map_keys', 'N/A')}")
                        st.write(f"- Failed to extract: {enrich_debug.get('failed_extract', 'N/A')}")
                        st.write(f"- Sample inputs: {enrich_debug.get('sample_inputs', [])}")
                        st.write(f"- ALL map keys: {enrich_debug.get('all_map_keys', enrich_debug.get('sample_map_keys', []))}")

                    with col2:
                        st.write("**Result Matching:**")
                        st.write(f"- Results: {match_debug.get('results', 'N/A')}")
                        st.write(f"- Matched: {match_debug.get('matched', 'N/A')}")
                        st.write(f"- Unmatched: {match_debug.get('unmatched_count', 'N/A')}")
                        st.write(f"- Unmatched samples: {match_debug.get('unmatched_samples', [])}")

                    if match_debug.get('result_samples'):
                        st.write("**Result samples:**")
                        for sample in match_debug.get('result_samples', []):
                            st.write(f"  - flagship: {sample.get('flagship')}, matched: {sample.get('matched')}")

            # Show enriched data preview
            st.markdown("### Enriched Profiles Preview")

            # Toggle to show all columns
            show_all_cols = st.checkbox("Show all columns", value=False, key="enrich_show_all_cols")

            # Data is already normalized by flatten_for_csv with consistent column names:
            # name, first_name, last_name, current_company, current_title, linkedin_url, etc.
            display_df = enriched_df.copy()

            # Debug: show available columns
            with st.expander("Debug: Available columns", expanded=False):
                st.write(f"Columns in enriched data: {list(display_df.columns)}")
                # Show sample values for key columns
                if len(display_df) > 0:
                    st.write("Sample row:")
                    sample = display_df.iloc[0]
                    for col in ['name', 'current_company', 'current_title', 'linkedin_url']:
                        if col in display_df.columns:
                            st.write(f"  {col}: {sample.get(col, 'N/A')}")

            if show_all_cols:
                # Show all Crustdata columns
                all_cols = ['name', 'current_title', 'current_company', 'all_employers', 'all_titles', 'all_schools', 'skills', 'past_positions', 'headline', 'location', 'summary', 'connections_count', 'linkedin_url']
                available_cols = [c for c in all_cols if c in display_df.columns]
                st.dataframe(
                    display_df[available_cols].head(20) if available_cols else display_df.head(20),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                    }
                )
                st.caption(f"Showing {min(20, len(display_df))} of {len(display_df)} profiles | {len(available_cols)} columns")
            else:
                # Simple preview: name, title, company, linkedin
                preview_cols = ['name', 'current_title', 'current_company', 'linkedin_url']
                available_cols = [c for c in preview_cols if c in display_df.columns]

                if available_cols:
                    st.dataframe(
                        display_df[available_cols].head(20),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                            "current_company": st.column_config.TextColumn("Company"),
                            "current_title": st.column_config.TextColumn("Title"),
                        }
                    )
                else:
                    st.warning("No data columns available. Check Debug expander for column names.")

            # Download full enriched data
            st.download_button(
                "Download Enriched Data (CSV)",
                enriched_df.to_csv(index=False),
                "enriched_profiles.csv",
                "text/csv"
            )

        if results_df is not None and not results_df.empty:
            urls = extract_urls_from_phantombuster(results_df)

            if urls:
                # Helper function for username extraction
                def get_base_username_from_url(url):
                    """Extract base username from URL, removing ID suffix"""
                    if not url or '/in/' not in str(url).lower():
                        return None
                    username = str(url).lower().split('/in/')[-1].rstrip('/').split('?')[0]
                    # Remove numeric ID suffix (e.g., john-doe-12345 -> john-doe)
                    if '-' in username:
                        parts = username.rsplit('-', 1)
                        suffix = parts[-1]
                        # Only strip if it's clearly an ID (all digits or mostly digits)
                        if suffix.isdigit():
                            return parts[0]
                        if len(suffix) >= 5 and suffix.isalnum():
                            digit_count = sum(1 for c in suffix if c.isdigit())
                            if digit_count >= len(suffix) * 0.5:  # At least 50% digits
                                return parts[0]
                    return username

                def get_reversed_username(base):
                    """Reverse name order (first-last -> last-first) for matching."""
                    if base and '-' in base:
                        parts = base.split('-')
                        if len(parts) == 2:
                            return f"{parts[1]}-{parts[0]}"
                    return None

                # Check for recently enriched profiles in database (within ENRICHMENT_REFRESH_MONTHS)
                recently_enriched = set()
                recently_enriched_usernames = set()
                recently_enriched_list = []
                db_check_error = None
                refresh_months = 3  # Default fallback
                if HAS_DATABASE:
                    try:
                        refresh_months = ENRICHMENT_REFRESH_MONTHS
                        db_client = _get_db_client()
                        if db_client:
                            # Only skip profiles enriched within the refresh period
                            recently_enriched_list = get_recently_enriched_urls(db_client, months=refresh_months)
                            recently_enriched = set(normalize_linkedin_url(u) for u in recently_enriched_list if u)

                            # Also build set of base usernames (without ID suffix) for fuzzy matching
                            for u in recently_enriched_list:
                                base = get_base_username_from_url(u)
                                if base:
                                    recently_enriched_usernames.add(base)
                                    # Also add reversed name order
                                    reversed_name = get_reversed_username(base)
                                    if reversed_name:
                                        recently_enriched_usernames.add(reversed_name)
                    except Exception as e:
                        db_check_error = str(e)

                # Filter out recently enriched URLs (older ones can be re-enriched)
                new_urls = []
                skipped_urls = []
                for url in urls:
                    normalized = normalize_linkedin_url(url) if HAS_DATABASE else url
                    # Try exact URL match first
                    if normalized in recently_enriched:
                        skipped_urls.append(url)
                    else:
                        # Try base username match (handles ID suffix differences)
                        base_username = get_base_username_from_url(url) if HAS_DATABASE else None
                        if base_username and base_username in recently_enriched_usernames:
                            skipped_urls.append(url)
                        else:
                            # Try reversed name order (first-last vs last-first)
                            reversed_username = get_reversed_username(base_username) if base_username else None
                            if reversed_username and reversed_username in recently_enriched_usernames:
                                skipped_urls.append(url)
                            else:
                                new_urls.append(url)

                # Debug info
                with st.expander("Debug: Enrichment check", expanded=False):
                    st.write(f"URLs in loaded data: {len(urls)}")
                    st.write(f"Recently enriched list (raw): {len(recently_enriched_list)}")
                    st.write(f"Recently enriched set (normalized): {len(recently_enriched)}")
                    st.write(f"Recently enriched usernames (base): {len(recently_enriched_usernames)}")

                    # Debug: show sample base usernames from DB and loaded
                    db_usernames_sample = list(recently_enriched_usernames)[:5]
                    loaded_usernames_sample = [get_base_username_from_url(u) for u in urls[:5]]
                    st.write(f"Sample DB base usernames: {db_usernames_sample}")
                    st.write(f"Sample loaded base usernames: {loaded_usernames_sample}")
                    st.write(f"New or stale (need enrichment): {len(new_urls)}")
                    st.write(f"Skipped (fresh in DB): {len(skipped_urls)}")
                    if db_check_error:
                        st.error(f"DB check error: {db_check_error}")

                    # Show normalized comparison
                    loaded_normalized = [normalize_linkedin_url(u) for u in urls[:5]]
                    st.write("Sample loaded URLs (normalized):", loaded_normalized)

                    # Show raw DB URLs (before normalization) to see original_urls
                    if recently_enriched_list:
                        # Find URLs with ID suffix (likely original_urls)
                        urls_with_suffix = [u for u in recently_enriched_list[:50] if u and '-' in u.split('/in/')[-1] and any(c.isdigit() for c in u.split('-')[-1])]
                        st.write(f"Sample DB URLs with ID suffix (original_urls): {urls_with_suffix[:5]}")
                        st.write("Sample DB URLs (normalized):", list(recently_enriched)[:5])

                        # Check for near-matches (same username, different format)
                        loaded_usernames = set()
                        for u in urls[:100]:
                            norm = normalize_linkedin_url(u)
                            if norm and '/in/' in norm:
                                username = norm.split('/in/')[-1].rstrip('/')
                                loaded_usernames.add(username)

                        db_usernames = set()
                        for u in list(recently_enriched)[:1000]:
                            if u and '/in/' in u:
                                username = u.split('/in/')[-1].rstrip('/')
                                db_usernames.add(username)

                        overlap = loaded_usernames & db_usernames
                        st.write(f"Username overlap (first 100 loaded vs DB): {len(overlap)} matches")
                        if overlap:
                            st.write("Sample matching usernames:", list(overlap)[:5])

                # Show stats - skip recently enriched by default
                if skipped_urls:
                    st.info(f"**{len(new_urls)}** profiles to enrich | **{len(skipped_urls)}** skipped (enriched < {refresh_months} months ago)")
                    urls_for_enrichment = new_urls

                    # Option to load enriched profiles from DB for this list
                    if HAS_DATABASE and len(skipped_urls) > 0:
                        if st.button(f"Load {len(skipped_urls)} enriched profiles for screening", type="primary", key="load_enriched_for_list"):
                            with st.spinner("Loading profiles from database..."):
                                try:
                                    db_client = _get_db_client()
                                    if not db_client:
                                        st.error("Database connection failed")
                                    else:
                                        # Get all recently enriched profiles with pagination (Supabase 1000 row limit)
                                        import datetime as dt
                                        cutoff_date = (dt.datetime.utcnow() - dt.timedelta(days=refresh_months * 30)).isoformat()

                                        # Paginate to get all profiles
                                        all_db_profiles = []
                                        offset = 0
                                        page_size = 1000
                                        while True:
                                            filters = {'enriched_at': f'gte.{cutoff_date}', 'offset': str(offset)}
                                            batch = db_client.select('profiles', '*', filters, limit=page_size)
                                            if not batch:
                                                break
                                            all_db_profiles.extend(batch)
                                            if len(batch) < page_size:
                                                break
                                            offset += page_size

                                        st.write(f"Debug: Got {len(all_db_profiles)} profiles from DB (with pagination)")
                                        all_profiles = all_db_profiles  # Already filtered by date in query

                                        # Build comprehensive set of variations from skipped URLs for matching
                                        skipped_variations = set()
                                        for u in skipped_urls:
                                            # Add normalized URL
                                            normalized = normalize_linkedin_url(u)
                                            if normalized:
                                                skipped_variations.add(normalized)

                                            # Add base username and variations
                                            base = get_base_username_from_url(u)
                                            if base:
                                                skipped_variations.add(base)
                                                # Reversed name
                                                reversed_name = get_reversed_username(base)
                                                if reversed_name:
                                                    skipped_variations.add(reversed_name)
                                                # Hyphen-free version
                                                skipped_variations.add(base.replace('-', ''))

                                        # Filter to matching profiles
                                        matched_profiles = []
                                        seen_urls = set()  # Avoid duplicates
                                        for p in all_profiles:
                                            p_url = p.get('linkedin_url') or ''
                                            if p_url in seen_urls:
                                                continue

                                            matched = False
                                            # Check normalized URL
                                            p_normalized = normalize_linkedin_url(p_url)
                                            if p_normalized and p_normalized in skipped_variations:
                                                matched = True

                                            # Check base username and variations
                                            if not matched:
                                                p_base = get_base_username_from_url(p_url)
                                                if p_base:
                                                    if p_base in skipped_variations:
                                                        matched = True
                                                    elif p_base.replace('-', '') in skipped_variations:
                                                        matched = True
                                                    else:
                                                        p_reversed = get_reversed_username(p_base)
                                                        if p_reversed and p_reversed in skipped_variations:
                                                            matched = True

                                            if matched:
                                                matched_profiles.append(p)
                                                seen_urls.add(p_url)

                                        if matched_profiles:
                                            enriched_df = profiles_to_dataframe(matched_profiles)
                                            st.session_state['enriched_results'] = matched_profiles
                                            st.session_state['enriched_df'] = enriched_df
                                            save_session_state()
                                            st.success(f"Loaded **{len(matched_profiles)}** enriched profiles for screening! (from {len(all_profiles)} in DB, {len(skipped_variations)} variations)")
                                            st.balloons()
                                        else:
                                            st.warning(f"No matching profiles found. DB has {len(all_profiles)} profiles, tried {len(skipped_variations)} variations.")
                                except Exception as e:
                                    st.error(f"Error loading profiles: {e}")

                    # Only show re-enrich option in expander if user really wants it
                    with st.expander("Re-enrich options", expanded=False):
                        if st.checkbox("Re-enrich recently-enriched profiles", value=False, key="reenrich_cb"):
                            urls_for_enrichment = urls
                            st.warning(f"Will re-enrich all {len(urls)} profiles including {len(skipped_urls)} recently enriched")
                else:
                    st.info(f"**{len(urls)}** profiles ready for enrichment (none found in DB < {refresh_months} months)")
                    urls_for_enrichment = urls

                if not urls_for_enrichment:
                    st.warning(f"All profiles were enriched within the last {refresh_months} months.")
                else:
                    col1, col2 = st.columns(2)
                    with col1:
                        max_profiles = st.number_input(
                            "Number of profiles to enrich",
                            min_value=1,
                            max_value=len(urls_for_enrichment),
                            value=min(10, len(urls_for_enrichment)),
                            help="Start with a few to test, then increase"
                        )
                    with col2:
                        batch_size = st.slider("Batch size", min_value=1, max_value=25, value=10, key="enrich_batch")

                    st.caption("Each profile costs 3 Crustdata credits")

                    if st.button("Start Enrichment", type="primary", key="start_enrich_tab"):
                        urls_to_process = urls_for_enrichment[:max_profiles]
                        results = []
                        original_urls = []  # Track original URLs in order
                        progress_bar = st.progress(0)
                        status_text = st.empty()
                        total_batches = (len(urls_to_process) + batch_size - 1) // batch_size

                        # Get usage tracker for logging
                        tracker = get_usage_tracker()

                        # Start timer
                        start_time = time.time()

                        for i in range(0, len(urls_to_process), batch_size):
                            batch = urls_to_process[i:i + batch_size]
                            batch_num = i // batch_size + 1
                            status_text.text(f"Processing batch {batch_num}/{total_batches}...")
                            batch_results = enrich_batch(batch, api_key, tracker=tracker)
                            results.extend(batch_results)
                            original_urls.extend(batch)  # Keep track of original URLs
                            progress_bar.progress(min((i + batch_size) / len(urls_to_process), 1.0))
                            if i + batch_size < len(urls_to_process):
                                time.sleep(2)

                        progress_bar.progress(1.0)

                        # Calculate elapsed time
                        elapsed_time = time.time() - start_time
                        minutes = int(elapsed_time // 60)
                        seconds = int(elapsed_time % 60)
                        time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

                        status_text.text(f"Enrichment complete! Time: {time_str}")
                        send_notification("Enrichment Complete", f"Processed {len(results)} profiles in {time_str}")

                        # Use Crustdata's response directly - no URL matching needed
                        # Crustdata's linkedin_profile_url is the source of truth

                        # Check for errors in results
                        errors = [r for r in results if 'error' in r]
                        successful = [r for r in results if 'error' not in r]

                        # Debug info
                        st.write(f"**Debug:** Total results: {len(results)}, Successful: {len(successful)}, Errors: {len(errors)}")
                        if errors:
                            st.warning(f"Errors: {[e.get('error', 'unknown')[:100] for e in errors[:3]]}")

                        # Show URL mapping debug info
                        with st.expander("Debug: URL Matching Details", expanded=True):
                            enrich_debug = st.session_state.get('_enrich_debug', {})
                            match_debug = st.session_state.get('_enrich_match_debug', {})

                            st.write("**Input URL Mapping:**")
                            st.write(f"- Input URLs: {enrich_debug.get('input_urls', 'N/A')}")
                            st.write(f"- Map keys created: {enrich_debug.get('map_keys', 'N/A')}")
                            st.write(f"- Failed to extract: {enrich_debug.get('failed_extract', 'N/A')}")
                            st.write(f"- Sample inputs: {enrich_debug.get('sample_inputs', [])}")
                            st.write(f"- Sample map keys: {enrich_debug.get('sample_map_keys', [])}")
                            if enrich_debug.get('failed_samples'):
                                st.write(f"- Failed samples: {enrich_debug.get('failed_samples', [])}")

                            st.write("**Result Matching:**")
                            st.write(f"- Results: {match_debug.get('results', 'N/A')}")
                            st.write(f"- Matched: {match_debug.get('matched', 'N/A')}")
                            st.write(f"- Unmatched: {match_debug.get('unmatched_count', 'N/A')}")
                            st.write(f"- Unmatched samples: {match_debug.get('unmatched_samples', [])}")
                            st.write(f"- Result samples: {match_debug.get('result_samples', [])}")

                        if successful:
                            # Save enriched data separately - don't overwrite original loaded data
                            st.session_state['enriched_results'] = successful
                            enriched_df = flatten_for_csv(successful)
                            st.session_state['enriched_df'] = enriched_df
                            save_session_state()  # Save for restore

                            # Debug: show what was created
                            st.write(f"**Debug:** Created enriched_df with {len(enriched_df)} rows, columns: {list(enriched_df.columns)[:10]}")

                            # Auto-save enrichment to Supabase database
                            db_saved = 0
                            if HAS_DATABASE:
                                try:
                                    db_client = _get_db_client()
                                    if db_client:
                                        # Debug: show URL fields and matching stats
                                        if successful:
                                            sample = successful[0]
                                            st.write(f"**Debug URLs:** flagship={sample.get('linkedin_flagship_url')}, _original_url={sample.get('_original_url')}")
                                            # Count matched vs unmatched
                                            matched = sum(1 for p in successful if p.get('_original_url'))
                                            st.write(f"**Matching:** {matched}/{len(successful)} profiles matched to original URLs")

                                        for profile in successful:
                                            # Use linkedin_flagship_url (canonical) as primary, not encoded linkedin_url
                                            linkedin_url = profile.get('linkedin_flagship_url') or profile.get('linkedin_url')
                                            # Use tracked original URL for matching with loaded data
                                            original_url = profile.get('_original_url')
                                            if linkedin_url:
                                                update_profile_enrichment(db_client, linkedin_url, profile, original_url=original_url)
                                                db_saved += 1
                                except Exception as e:
                                    st.warning(f"Database save failed: {e}")

                            # Store message to show after rerun
                            db_msg = f" (DB: {db_saved} saved)" if db_saved > 0 else ""
                            if len(enriched_df) == 0:
                                st.error("Enrichment returned empty DataFrame. Check if profiles have valid data.")
                            elif errors:
                                st.session_state['enrichment_message'] = f"warning:Enriched {len(successful)} profiles in {time_str}{db_msg}. {len(errors)} failed: {errors[0].get('error', 'Unknown')[:150]}"
                                st.rerun()
                            else:
                                st.session_state['enrichment_message'] = f"success:Enriched {len(successful)} profiles in {time_str}{db_msg}"
                                st.rerun()
                        elif errors:
                            # All failed - show error, original data stays intact
                            st.error(f"Enrichment failed for all profiles. Error: {errors[0].get('error', 'Unknown')[:200]}")
                        else:
                            st.error("No results returned from API. Check your API key and credits.")
            else:
                st.warning("No LinkedIn URLs found in loaded profiles.")

        # Next button (show only if enriched)
        if is_enriched:
            st.divider()
            st.info("**Next step:** Click on **4. Filter+** tab to filter on enriched data, or **5. AI Screen** to screen candidates")

# ========== TAB 4: Filter+ (Post-Enrichment) ==========
with tab_filter2:
    st.markdown("### Advanced Filtering (Enriched Data)")
    st.caption("Filter on full profile data: work history, education, skills")

    enriched_df = st.session_state.get('enriched_df')
    is_enriched = enriched_df is not None and not enriched_df.empty

    if not is_enriched:
        st.info("Enrich profiles first (tab 3) to use advanced filtering.")
    else:
        st.success(f"**{len(enriched_df)}** enriched profiles ready for filtering")

        # Show available enriched columns and sample data for debugging
        with st.expander("Debug: Searchable columns & sample data"):
            search_cols = ['skills', 'all_titles', 'all_employers', 'past_positions', 'summary', 'headline']
            available = [c for c in search_cols if c in enriched_df.columns]
            st.write(f"**Searchable columns:** {available}")

            # Show sample data from first non-empty row
            if len(enriched_df) > 0:
                sample_row = enriched_df.iloc[0]
                for col in available:
                    val = sample_row.get(col, '')
                    try:
                        is_valid = val is not None and str(val).strip() and str(val) != 'nan'
                    except (ValueError, TypeError):
                        is_valid = False
                    if is_valid:
                        st.write(f"**{col}:** {str(val)[:200]}{'...' if len(str(val)) > 200 else ''}")
                    else:
                        st.write(f"**{col}:** *(empty)*")

            # Count non-empty values
            st.write("**Non-empty counts:**")
            for col in available:
                non_empty = (enriched_df[col].notna() & (enriched_df[col] != '')).sum()
                st.write(f"  - {col}: {non_empty}/{len(enriched_df)} profiles")

        # Google Sheets filtering (same as Filter tab)
        filter_sheets = get_filter_sheets_config().copy()
        gspread_client = get_gspread_client()

        user_sheet_url = st.text_input(
            "Google Sheet URL for filtering",
            value=st.session_state.get('user_sheet_url', ''),
            placeholder="https://docs.google.com/spreadsheets/d/...",
            key="filter2_sheet_input"
        )

        if user_sheet_url:
            st.session_state['user_sheet_url'] = user_sheet_url
            filter_sheets['url'] = user_sheet_url
            # Set default tab names if not already set
            if 'past_candidates' not in filter_sheets:
                filter_sheets['past_candidates'] = 'Past Candidates'
            if 'blacklist' not in filter_sheets:
                filter_sheets['blacklist'] = 'Blacklist'
            if 'not_relevant' not in filter_sheets:
                filter_sheets['not_relevant'] = 'NotRelevant Companies'
            if 'universities' not in filter_sheets:
                filter_sheets['universities'] = 'Universities'

        has_sheets = bool(filter_sheets.get('url')) and gspread_client is not None

        if has_sheets:
            # Try to show sheet name and available tabs
            try:
                spreadsheet = gspread_client.open_by_url(filter_sheets['url'])
                tabs = [ws.title for ws in spreadsheet.worksheets()]
                st.success(f"📊 **{spreadsheet.title}** connected")
                with st.expander("Sheet tabs", expanded=False):
                    st.write(f"Available tabs: {', '.join(tabs)}")
                    st.caption("Expected tabs: Past Candidates, Blacklist, NotRelevant Companies, Universities")
            except Exception:
                st.success("Filter sheet connected")

        st.divider()
        st.markdown("**Filter Options:**")

        col1, col2 = st.columns(2)

        with col1:
            # Sheet-based filters
            st.markdown("**From Google Sheet:**")
            use_not_relevant = st.checkbox("Exclude Not Relevant Companies (all employers)", value=True, key="f2_not_relevant",
                                          help="Check against ALL past employers, not just current")
            uni_filter_mode = st.radio("Target Universities:",
                                       ["Off", "Prioritize (move to top)", "Require (filter others out)"],
                                       key="f2_uni_mode", horizontal=True)

        with col2:
            # Keyword filters (job titles only)
            st.markdown("**Job Title Keywords:**")
            include_keywords = st.text_input("Include keywords (comma-separated)", key="f2_include_kw",
                                            placeholder="e.g., backend, senior, lead",
                                            help="Profile must have at least one of these in job titles")
            exclude_keywords = st.text_input("Exclude keywords (comma-separated)", key="f2_exclude_kw",
                                            placeholder="e.g., intern, student, freelance",
                                            help="Exclude profiles with these in job titles")

        # Required skills/keywords
        skill_col1, skill_col2, skill_col3 = st.columns([3, 1, 2])
        with skill_col1:
            required_skills = st.text_input("Required keywords (comma-separated)", key="f2_required_skills",
                                           placeholder="e.g., Python, AWS, Kubernetes")
        with skill_col2:
            skills_logic = st.radio("Logic:", ["AND", "OR"], key="f2_skills_logic", horizontal=True,
                                   help="AND = must have ALL keywords, OR = must have at least ONE")
        with skill_col3:
            search_scope = st.radio("Search in:", ["Skills only", "Full profile"], key="f2_search_scope", horizontal=True,
                                   help="Skills = skills column only | Full profile = everything including job descriptions")

        btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 2])
        with btn_col1:
            apply_clicked = st.button("Apply Filters", type="primary", key="apply_filters_enriched")
        with btn_col2:
            if st.button("Reset Filters", key="reset_filters_enriched"):
                # Reset to original enriched_df
                st.session_state['passed_candidates_df'] = enriched_df.copy()
                if 'f2_filter_stats' in st.session_state:
                    del st.session_state['f2_filter_stats']
                if 'f2_filtered_out' in st.session_state:
                    del st.session_state['f2_filtered_out']
                st.success(f"Reset to {len(enriched_df)} profiles")
                save_session_state()
                st.rerun()

        if apply_clicked:
            with st.spinner("Applying filters..."):
                df = enriched_df.copy()
                original_count = len(df)
                removed = {}
                filtered_out = {}  # Track filtered profiles by reason
                priority_matches = []

                sheet_url = filter_sheets.get('url', '') if has_sheets else ''

                # Not relevant companies filter (checks ALL employers)
                if use_not_relevant and has_sheets and filter_sheets.get('not_relevant'):
                    nr_df = load_sheet_as_df(sheet_url, filter_sheets['not_relevant'])
                    if nr_df is not None and not nr_df.empty:
                        not_relevant_companies = set()
                        for col in nr_df.columns:
                            not_relevant_companies.update(nr_df[col].dropna().str.lower().str.strip().tolist())
                        if 'all_employers' in df.columns:
                            def has_not_relevant_employer(employers_data):
                                # Handle None, NaN, empty
                                if employers_data is None:
                                    return False
                                try:
                                    if pd.isna(employers_data):
                                        return False
                                except (ValueError, TypeError):
                                    pass  # Handle arrays that can't be checked with isna
                                if not employers_data:
                                    return False
                                # Handle list/array or comma-separated string
                                if isinstance(employers_data, (list, tuple)):
                                    employers = [str(e).strip().lower() for e in employers_data if e]
                                else:
                                    employers = [e.strip().lower() for e in str(employers_data).split(',')]
                                return any(emp in not_relevant_companies for emp in employers)
                            mask = df['all_employers'].apply(has_not_relevant_employer)
                            filtered_out['Not Relevant Companies'] = df[mask].to_dict('records')
                            removed['Not Relevant Companies'] = mask.sum()
                            df = df[~mask]

                # Target universities filter
                if uni_filter_mode != "Off" and has_sheets and filter_sheets.get('universities'):
                    uni_df = load_sheet_as_df(sheet_url, filter_sheets['universities'])
                    if uni_df is not None and not uni_df.empty:
                        target_unis = set()
                        for col in uni_df.columns:
                            target_unis.update(uni_df[col].dropna().str.lower().str.strip().tolist())
                        if 'all_schools' in df.columns:
                            def has_target_university(schools_data):
                                """Check if profile attended a target university.
                                Uses stricter matching: target must match start of school name
                                or be a significant portion (>60%) to avoid false positives like
                                'Tel Aviv University' matching 'Afeka Tel Aviv College'.
                                """
                                # Handle None, NaN, empty
                                if schools_data is None:
                                    return False
                                try:
                                    if pd.isna(schools_data):
                                        return False
                                except (ValueError, TypeError):
                                    pass  # Handle arrays that can't be checked with isna
                                if not schools_data:
                                    return False
                                # Handle list/array or comma-separated string
                                if isinstance(schools_data, (list, tuple)):
                                    schools = [str(s).strip().lower() for s in schools_data if s]
                                else:
                                    schools = [s.strip().lower() for s in str(schools_data).split(',')]
                                for school in schools:
                                    for target in target_unis:
                                        if not target:
                                            continue
                                        # Exact match
                                        if school == target:
                                            return True
                                        # School starts with target (e.g., "technion" matches "technion - israel institute of technology")
                                        if school.startswith(target):
                                            return True
                                        # Target starts with school (e.g., "tel aviv university" in target, "tel aviv uni" in school)
                                        if target.startswith(school) and len(school) > 10:
                                            return True
                                        # Target is >70% of school name length and appears at start
                                        # This catches "hebrew university" matching "hebrew university of jerusalem"
                                        if len(target) > 8 and school.startswith(target.split()[0]) and len(target) / len(school) > 0.5:
                                            # Check if key words match
                                            target_words = set(target.split())
                                            school_words = set(school.split())
                                            common = target_words & school_words
                                            if len(common) >= len(target_words) * 0.7:
                                                return True
                                return False
                            df['_target_uni'] = df['all_schools'].apply(has_target_university)
                            priority_matches = df[df['_target_uni']].index.tolist()

                            if uni_filter_mode == "Require (filter others out)":
                                mask = ~df['_target_uni']
                                filtered_out['Non-Target Universities'] = df[mask].drop(columns=['_target_uni']).to_dict('records')
                                removed['Non-Target Universities'] = mask.sum()
                                df = df[df['_target_uni']]
                                df = df.drop(columns=['_target_uni'])
                            else:
                                df = pd.concat([df[df['_target_uni']], df[~df['_target_uni']]])
                                df = df.drop(columns=['_target_uni'])

                # Include keywords filter (job titles only)
                if include_keywords and include_keywords.strip():
                    keywords = [k.strip().lower() for k in include_keywords.split(',') if k.strip()]
                    if keywords:
                        search_cols = ['past_positions', 'current_title']
                        available_search_cols = [c for c in search_cols if c in df.columns]
                        def has_include_keyword(row):
                            text = ' '.join(str(row[c]) for c in available_search_cols if row.get(c) is not None).lower()
                            return any(kw in text for kw in keywords)
                        mask = df.apply(has_include_keyword, axis=1)
                        filtered_out['Missing Title Keywords'] = df[~mask].to_dict('records')
                        removed['Missing Title Keywords'] = (~mask).sum()
                        df = df[mask]

                # Exclude keywords filter (job titles only)
                if exclude_keywords and exclude_keywords.strip():
                    keywords = [k.strip().lower() for k in exclude_keywords.split(',') if k.strip()]
                    if keywords:
                        search_cols = ['past_positions', 'current_title']
                        available_search_cols = [c for c in search_cols if c in df.columns]
                        def has_exclude_keyword(row):
                            text = ' '.join(str(row[c]) for c in available_search_cols if row.get(c) is not None).lower()
                            return any(kw in text for kw in keywords)
                        mask = df.apply(has_exclude_keyword, axis=1)
                        filtered_out['Excluded Title Keywords'] = df[mask].to_dict('records')
                        removed['Excluded Title Keywords'] = mask.sum()
                        df = df[~mask]

                # Required keywords filter (skills or full experience)
                if required_skills and required_skills.strip():
                    skills_list = [s.strip().lower() for s in required_skills.split(',') if s.strip()]
                    if skills_list:
                        # Determine which columns to search based on scope
                        if search_scope == "Full profile":
                            search_cols = ['skills', 'all_titles', 'all_employers', 'past_positions', 'summary', 'headline', 'raw_crustdata']
                            scope_label = "profile"
                        else:
                            search_cols = ['skills']
                            scope_label = "skills"

                        available_search_cols = [c for c in search_cols if c in df.columns]

                        if available_search_cols:
                            import json as json_module
                            def has_required_keywords(row):
                                # Combine text from all search columns
                                text_parts = []
                                for c in available_search_cols:
                                    if c not in row or row.get(c) is None:
                                        continue
                                    val = row[c]
                                    # Handle raw_crustdata dict/JSON
                                    if c == 'raw_crustdata' and isinstance(val, dict):
                                        text_parts.append(json_module.dumps(val, ensure_ascii=False))
                                    else:
                                        text_parts.append(str(val))
                                combined_text = ' '.join(text_parts).lower()

                                if not combined_text.strip():
                                    return False

                                if skills_logic == "AND":
                                    return all(kw in combined_text for kw in skills_list)
                                else:  # OR
                                    return any(kw in combined_text for kw in skills_list)

                            mask = df.apply(has_required_keywords, axis=1)
                            logic_label = "all" if skills_logic == "AND" else "any"
                            filter_name = f'Missing Keywords in {scope_label} ({logic_label})'
                            filtered_out[filter_name] = df[~mask].to_dict('records')
                            removed[filter_name] = (~mask).sum()
                            df = df[mask]
                        else:
                            st.warning(f"No searchable columns found. Available: {list(df.columns)}")

                # Store results
                st.session_state['passed_candidates_df'] = df.reset_index(drop=True)
                st.session_state['f2_filtered_out'] = filtered_out
                st.session_state['f2_filter_stats'] = {
                    'original': original_count,
                    'total_removed': original_count - len(df),
                    'final': len(df),
                    'removed_by': removed,
                    'priority_count': len(priority_matches)
                }

                st.success(f"Filtered: {original_count} → {len(df)} profiles")
                for reason, count in removed.items():
                    if count > 0:
                        st.caption(f"  - {reason}: {count} removed")
                if priority_matches and uni_filter_mode == "Prioritize (move to top)":
                    st.info(f"🎓 {len(priority_matches)} candidates from target universities (shown first)")
                save_session_state()  # Save for restore
                st.rerun()

        # Show filter stats if available
        if 'f2_filter_stats' in st.session_state:
            stats = st.session_state['f2_filter_stats']
            st.divider()
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

            removed_by = stats.get('removed_by', {})
            with st.expander("Detailed Breakdown"):
                shown_any = False
                for reason, count in removed_by.items():
                    if count > 0:
                        st.text(f"  ✗ {reason}: {count} removed")
                        shown_any = True
                if not shown_any:
                    if stats.get('total_removed', 0) > 0:
                        st.caption("No individual filter breakdown available.")
                    else:
                        st.caption("No profiles were filtered out.")

        # Show passed candidates
        st.divider()
        st.markdown("### Passed Candidates")
        display_df = st.session_state.get('passed_candidates_df', enriched_df)

        col1, col2 = st.columns([3, 1])
        with col1:
            st.success(f"**{len(display_df)}** profiles passed filters")
        with col2:
            show_all_cols = st.checkbox("Show all columns", value=False, key="filter2_show_all_cols")

        if show_all_cols:
            st.dataframe(display_df.head(100), use_container_width=True, hide_index=True,
                        column_config={
                            "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                            "public_url": st.column_config.LinkColumn("LinkedIn")
                        })
        else:
            display_cols = ['first_name', 'last_name', 'current_title', 'current_company', 'location', 'linkedin_url', 'public_url']
            available_cols = [c for c in display_cols if c in display_df.columns]
            st.dataframe(display_df[available_cols].head(100), use_container_width=True, hide_index=True,
                        column_config={
                            "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                            "public_url": st.column_config.LinkColumn("LinkedIn")
                        })

        if len(display_df) > 0:
            csv_data = display_df.to_csv(index=False)
            st.download_button("Download Passed (CSV)", csv_data, "passed_profiles.csv", "text/csv", key="download_passed")

        # Show filtered out candidates
        if 'f2_filtered_out' in st.session_state and st.session_state['f2_filtered_out']:
            st.divider()
            st.markdown("### Filtered Out Candidates")
            st.caption("Review candidates removed by each filter")

            filtered_out = st.session_state['f2_filtered_out']
            filter_names = [k for k, v in filtered_out.items() if len(v) > 0]

            if filter_names:
                selected_filter = st.selectbox("Select filter to review:", filter_names, key="f2_review_filter")
                if selected_filter:
                    filtered_profiles = filtered_out[selected_filter]

                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.warning(f"**{len(filtered_profiles)}** profiles removed by: {selected_filter}")
                    with col2:
                        show_all_cols = st.checkbox("Show all columns", value=False, key="f2_filtered_show_all")

                    review_df = pd.DataFrame(filtered_profiles)
                    if show_all_cols:
                        st.dataframe(review_df.head(50), use_container_width=True, hide_index=True,
                                    column_config={
                                        "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                                        "public_url": st.column_config.LinkColumn("LinkedIn")
                                    })
                        st.caption(f"Showing {min(50, len(review_df))} of {len(review_df)} profiles | {len(review_df.columns)} columns")
                    else:
                        display_cols = ['first_name', 'last_name', 'current_title', 'current_company', 'all_employers', 'all_schools', 'skills', 'linkedin_url']
                        available_cols = [c for c in display_cols if c in review_df.columns]
                        st.dataframe(review_df[available_cols].head(50), use_container_width=True, hide_index=True,
                                    column_config={
                                        "linkedin_url": st.column_config.LinkColumn("LinkedIn")
                                    })

        # ===== SalesQL Email Enrichment =====
        st.divider()
        st.markdown("### Email Enrichment (SalesQL)")
        salesql_key = load_salesql_key()
        if salesql_key:
            # Use passed_candidates_df if available
            email_df = st.session_state.get('passed_candidates_df', display_df)
            if email_df is not None and not email_df.empty:
                current_count = len(email_df)
                already_enriched = (email_df['salesql_email'].notna() & (email_df['salesql_email'] != '')).sum() if 'salesql_email' in email_df.columns else 0
                not_enriched = current_count - already_enriched
                st.caption(f"{current_count} profiles | {already_enriched} already have emails | {not_enriched} remaining")

                # Ask how many to enrich
                if not_enriched > 0:
                    col_option, col_number = st.columns([1, 1])
                    with col_option:
                        enrich_option = st.radio("How many to enrich?", ["All", "Specific number"], key="enrich_option_tab4", horizontal=True)
                    with col_number:
                        if enrich_option == "Specific number":
                            enrich_count = st.number_input("Number of profiles", min_value=1, max_value=not_enriched, value=min(50, not_enriched), key="enrich_count_tab4")
                        else:
                            enrich_count = not_enriched

                    if st.button(f"Enrich {enrich_count} profiles with Emails", key="salesql_tab4", type="primary"):
                        progress_bar = st.progress(0)
                        status_text = st.empty()

                        def update_progress(current, total):
                            progress_bar.progress(current / total)
                            status_text.text(f"Enriching {current}/{total}...")

                        enriched_df = enrich_profiles_with_salesql(email_df, salesql_key, progress_callback=update_progress, limit=enrich_count)
                        st.session_state['passed_candidates_df'] = enriched_df
                        new_emails = enriched_df['salesql_email'].notna().sum() if 'salesql_email' in enriched_df.columns else 0
                        st.success(f"Done! {new_emails} profiles now have emails.")
                        st.rerun()
                else:
                    st.success("All profiles already have emails!")
        else:
            st.caption("SalesQL not configured. Add 'salesql_api_key' to secrets.")

        # Next button
        st.divider()
        st.info("**Next step:** Click on **5. AI Screen** tab to screen candidates with AI")

# ========== TAB 5: AI Screening ==========
with tab_screening:
    openai_key = load_openai_key()

    # Check if data is enriched
    enriched_df = st.session_state.get('enriched_df')
    is_enriched = enriched_df is not None and not enriched_df.empty

    if not openai_key:
        st.warning("OpenAI API key not configured. Add 'openai_api_key' to config.json")
    elif not is_enriched:
        st.warning("Profiles must be enriched before AI screening.")
        st.info("Go to **tab 3 (Enrich)** to enrich profiles with full LinkedIn data, then come back here.")
    else:
        # Use passed_candidates_df if available (filtered), otherwise use enriched_df
        if 'passed_candidates_df' in st.session_state and not st.session_state['passed_candidates_df'].empty:
            profiles_df = st.session_state['passed_candidates_df']
            st.success(f"**{len(profiles_df)}** filtered candidates ready for screening")
        else:
            profiles_df = enriched_df
            st.info(f"**{len(profiles_df)}** enriched profiles ready for screening")

        profiles = profiles_df.to_dict('records')

        # Job Description Input
        st.markdown("### Job Description")
        job_description = st.text_area(
            "Paste the job description",
            height=150,
            key="jd_screening",
            placeholder="Paste the full job description here..."
        )

        # Extra Requirements (optional)
        extra_requirements = ""  # Default value
        with st.expander("Extra Requirements (optional)"):
            extra_requirements = st.text_area(
                "Additional must-have criteria",
                height=100,
                key="extra_requirements",
                placeholder="e.g., Must have AWS experience, Hebrew speaker preferred, etc."
            )

        # Role Detection and Prompt Selection
        st.markdown("### Screening Prompt")

        # Auto-detect role from JD
        detected_prompt, detected_role, detected_name = get_screening_prompt_for_role(job_description=job_description)

        # Build role options
        role_options = ['Auto-detect']
        role_options.extend([f"{v['name']}" for k, v in DEFAULT_PROMPTS.items()])

        # Load custom prompts from DB
        db_prompts = []
        if HAS_DATABASE:
            db_client = _get_db_client()
            if db_client:
                db_prompts = get_screening_prompts(db_client)
                for p in db_prompts:
                    name = p.get('name', p['role_type'].title())
                    if name not in role_options:
                        role_options.append(name)

        col_role, col_detected = st.columns([2, 3])
        with col_role:
            selected_role = st.selectbox(
                "Prompt type",
                options=role_options,
                index=0,
                key="screening_role_select",
                help="Auto-detect picks the best prompt based on job description keywords"
            )

        with col_detected:
            if selected_role == 'Auto-detect':
                if job_description:
                    st.success(f"Detected: **{detected_name}**")
                else:
                    st.info("Paste job description to auto-detect role")

        # Get the actual prompt to use
        if selected_role == 'Auto-detect':
            active_prompt = detected_prompt
            active_role = detected_role
            active_name = detected_name
        else:
            # Find the selected prompt
            active_prompt = None
            active_role = None
            active_name = selected_role

            # Check defaults first
            for role_key, role_data in DEFAULT_PROMPTS.items():
                if role_data['name'] == selected_role:
                    active_prompt = role_data['prompt']
                    active_role = role_key
                    break

            # Check DB prompts
            if not active_prompt:
                for p in db_prompts:
                    if p.get('name', p['role_type'].title()) == selected_role:
                        active_prompt = p['prompt_text']
                        active_role = p['role_type']
                        break

            if not active_prompt:
                active_prompt = DEFAULT_SCREENING_PROMPT
                active_role = 'backend'

        # Store active prompt in session state for screening
        st.session_state['active_screening_prompt'] = active_prompt
        st.session_state['active_screening_role'] = active_role

        # Admin section for managing prompts
        ADMIN_NAMES = {'alexey', 'dana', 'admin'}
        current_user = st.session_state.get('username', '').lower()
        is_admin = (
            not authenticator  # no auth = local dev, always show
            or current_user in ADMIN_NAMES
            or any(name in current_user for name in ADMIN_NAMES)
        )

        if is_admin:
            with st.expander("Manage Prompts (admin)"):
                prompt_tabs = st.tabs(["View/Edit Current", "Add New", "Manage All"])

                with prompt_tabs[0]:
                    st.markdown(f"**Currently selected:** {active_name}")
                    edited_prompt = st.text_area(
                        "Edit prompt",
                        value=active_prompt,
                        height=250,
                        key="edit_current_prompt"
                    )
                    if st.button("Save Changes", key="save_current_prompt"):
                        if HAS_DATABASE and active_role:
                            db_client = _get_db_client()
                            # Get keywords for this role
                            keywords = DEFAULT_PROMPTS.get(active_role, {}).get('keywords', [])
                            if save_screening_prompt(db_client, active_role, edited_prompt, keywords):
                                st.success(f"Saved prompt for {active_name}")
                                st.rerun()
                            else:
                                st.error("Failed to save")
                        else:
                            st.error("Database not connected")

                with prompt_tabs[1]:
                    st.markdown("**Create new prompt:**")
                    new_role_type = st.text_input("Role type (e.g., 'devops', 'qa')", key="new_role_type")
                    new_role_name = st.text_input("Display name (e.g., 'DevOps Engineer')", key="new_role_name")
                    new_keywords = st.text_input("Keywords (comma-separated)", key="new_keywords",
                                                 placeholder="e.g., devops, kubernetes, ci/cd, docker")
                    new_prompt = st.text_area("Prompt text", height=200, key="new_prompt_text",
                                              placeholder="Paste or write your screening prompt here...")
                    new_is_default = st.checkbox("Set as default prompt", key="new_is_default")

                    if st.button("Add Prompt", key="add_new_prompt"):
                        if new_role_type and new_prompt:
                            if HAS_DATABASE:
                                db_client = _get_db_client()
                                keywords_list = [k.strip().lower() for k in new_keywords.split(',') if k.strip()]
                                # Save with name in prompt_text metadata (we'll store name separately)
                                data = {
                                    'role_type': new_role_type.lower().strip(),
                                    'prompt_text': new_prompt,
                                    'keywords': keywords_list,
                                    'is_default': new_is_default,
                                    'name': new_role_name or new_role_type.title(),
                                    'updated_at': datetime.utcnow().isoformat(),
                                }
                                try:
                                    db_client.upsert('screening_prompts', data, on_conflict='role_type')
                                    st.success(f"Added prompt: {new_role_name or new_role_type}")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Failed to add: {e}")
                            else:
                                st.error("Database not connected")
                        else:
                            st.warning("Role type and prompt text are required")

                with prompt_tabs[2]:
                    st.markdown("**All saved prompts:**")
                    if db_prompts:
                        for p in db_prompts:
                            col1, col2, col3 = st.columns([3, 1, 1])
                            with col1:
                                name = p.get('name', p['role_type'].title())
                                default_badge = " (default)" if p.get('is_default') else ""
                                st.write(f"**{name}**{default_badge}")
                                st.caption(f"Keywords: {', '.join(p.get('keywords', []))}")
                            with col2:
                                if st.button("Set Default", key=f"default_{p['role_type']}"):
                                    if HAS_DATABASE:
                                        db_client = _get_db_client()
                                        # Clear other defaults first
                                        for other in db_prompts:
                                            if other.get('is_default'):
                                                save_screening_prompt(db_client, other['role_type'],
                                                                     other['prompt_text'], other.get('keywords', []), False)
                                        save_screening_prompt(db_client, p['role_type'],
                                                             p['prompt_text'], p.get('keywords', []), True)
                                        st.rerun()
                            with col3:
                                if st.button("Delete", key=f"del_{p['role_type']}"):
                                    if HAS_DATABASE:
                                        db_client = _get_db_client()
                                        if delete_screening_prompt(db_client, p['role_type']):
                                            st.rerun()
                    else:
                        st.info("No custom prompts saved yet. Default prompts are built-in.")

        # Screening Configuration
        st.markdown("### Screening Configuration")

        col_count, col_mode = st.columns([1, 1])
        with col_count:
            screen_count = st.number_input(
                "Number of profiles to screen",
                min_value=1,
                max_value=len(profiles),
                value=min(100, len(profiles)),
                step=10,
                key="screen_count"
            )
        with col_mode:
            screening_mode = st.radio(
                "Screening mode",
                options=["Quick (cheaper)", "Detailed"],
                index=0,
                key="screening_mode",
                help="Quick: score + fit + short summary | Detailed: adds reasoning, strengths, concerns"
            )

        # Fixed concurrent workers (optimal for Tier 3)
        max_workers = 50

        # Cost estimate based on mode
        if screening_mode == "Quick (cheaper)":
            output_tokens = 50  # ~50 tokens for quick response
            st.caption("Quick mode: Returns score, fit level, and brief summary only")
        else:
            output_tokens = 200  # ~200 tokens for detailed response
            st.caption("Detailed mode: Returns full analysis with reasoning, strengths, and concerns")

        est_cost = (screen_count * 2500 * 0.15 / 1_000_000) + (screen_count * output_tokens * 0.60 / 1_000_000)
        est_time = (screen_count / 10) * 2  # ~2 seconds per batch of 10
        st.info(f"💰 Estimated cost: **${est_cost:.3f}** | ⏱️ Time: ~{est_time:.0f}s")

        # Debug: Show available fields and test single profile
        with st.expander("Debug: Profile Fields & Test"):
            if profiles:
                st.write("Available fields in profiles:", list(profiles[0].keys()))
                st.write("Sample profile:", {k: str(v)[:100] for k, v in profiles[0].items()})

                if job_description and st.button("Test Single Profile", key="test_single"):
                    try:
                        client = OpenAI(api_key=openai_key)
                        test_mode = 'quick' if screening_mode == "Quick (cheaper)" else 'detailed'
                        test_prompt = st.session_state.get('active_screening_prompt', active_prompt)
                        st.write(f"Testing with first profile ({test_mode} mode, prompt: {active_name})...")
                        result = screen_profile(profiles[0], job_description, client, extra_requirements, mode=test_mode, system_prompt=test_prompt)
                        st.write("Result:", result)
                    except Exception as e:
                        import traceback
                        st.error(f"Error: {e}")
                        st.code(traceback.format_exc())

        # Screen Button
        if job_description:
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

                # Progress bar
                st.progress(completed / total if total > 0 else 0, text=f"Screening: {completed}/{total} ({pct:.0f}%)")

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
                        # Save partial results before cancelling
                        partial_results = st.session_state.get('screening_batch_state', {}).get('results', [])
                        if partial_results:
                            st.session_state['screening_results'] = partial_results
                            # Save partial results to DB
                            if HAS_DATABASE:
                                try:
                                    db_client = _get_db_client()
                                    if db_client:
                                        for result in partial_results:
                                            linkedin_url = result.get('linkedin_url') or result.get('public_url')
                                            if linkedin_url and result.get('fit') != 'Error':
                                                update_profile_screening(
                                                    db_client, linkedin_url,
                                                    score=result.get('score', 0),
                                                    fit_level=result.get('fit', ''),
                                                    summary=result.get('summary', ''),
                                                    reasoning=result.get('reasoning', '')
                                                )
                                except:
                                    pass
                        st.session_state['screening_cancelled'] = True
                        st.session_state['screening_batch_mode'] = False
                        save_session_state()  # Save partial results for restore
                        st.warning(f"Screening cancelled! {len(partial_results)} profiles were completed and saved.")
                        st.rerun()

            # Show existing results and options
            existing_results = st.session_state.get('screening_results', [])
            start_button = False
            continue_button = False

            if existing_results and not screening_in_progress:
                # Find profiles not yet screened
                screened_urls = set(r.get('linkedin_url', '') for r in existing_results if r.get('linkedin_url'))
                unscreened_profiles = [p for p in profiles if p.get('linkedin_url', '') not in screened_urls]

                st.info(f"📊 **{len(existing_results)}** profiles already screened | **{len(unscreened_profiles)}** remaining")

                col1, col2, col3 = st.columns([2, 2, 1])
                with col1:
                    if unscreened_profiles:
                        continue_button = st.button(f"▶️ Continue ({len(unscreened_profiles)} left)", type="primary", key="continue_screening")
                    else:
                        st.success("All profiles screened!")
                with col2:
                    start_button = st.button("🔄 Re-screen All", key="start_screening")
                with col3:
                    if st.button("🗑️ Clear", key="clear_screening_results"):
                        st.session_state['screening_results'] = []
                        st.session_state['screening_batch_state'] = {}
                        st.success("Results cleared!")
                        st.rerun()
            else:
                start_disabled = screening_in_progress
                start_button = st.button("Start Screening", type="primary", key="start_screening", disabled=start_disabled)

            # Continue batch processing if in progress
            if screening_in_progress and not st.session_state.get('screening_cancelled', False):
                # Get batch state
                batch_state = st.session_state.get('screening_batch_state', {})
                profiles_to_screen = batch_state.get('profiles', [])
                job_desc = batch_state.get('job_description', '')
                extra_req = batch_state.get('extra_requirements', '')
                screen_mode = batch_state.get('mode', 'detailed')
                system_prompt = batch_state.get('system_prompt')
                batch_size = 10  # Process 10 at a time for responsive cancellation
                current_batch = batch_state.get('current_batch', 0)
                all_results = batch_state.get('results', [])

                start_idx = current_batch * batch_size
                end_idx = min(start_idx + batch_size, len(profiles_to_screen))
                batch_profiles = profiles_to_screen[start_idx:end_idx]

                if batch_profiles:
                    with st.status(f"Processing batch {current_batch + 1} ({screen_mode} mode)...", expanded=True) as status:
                        # Screen this batch
                        batch_results = screen_profiles_batch(
                            batch_profiles,
                            job_desc,
                            openai_key,
                            extra_requirements=extra_req,
                            max_workers=min(10, len(batch_profiles)),
                            mode=screen_mode,
                            system_prompt=system_prompt
                        )
                        all_results.extend(batch_results)

                        # Update progress
                        st.session_state['screening_batch_state']['results'] = all_results
                        st.session_state['screening_batch_state']['current_batch'] = current_batch + 1
                        st.session_state['screening_batch_progress'] = {
                            'completed': len(all_results),
                            'total': len(profiles_to_screen)
                        }

                        # Show batch stats
                        strong = sum(1 for r in batch_results if r.get('fit') == 'Strong Fit')
                        good = sum(1 for r in batch_results if r.get('fit') == 'Good Fit')
                        errors_in_batch = sum(1 for r in batch_results if r.get('fit') in ('Error', 'Skipped'))
                        status.update(label=f"Batch {current_batch + 1}: +{strong} strong, +{good} good", state="complete")

                    # Abort early if entire batch failed (API key issue, quota, etc.)
                    if errors_in_batch == len(batch_results) and current_batch == 0:
                        st.session_state['screening_batch_mode'] = False
                        st.session_state['screening_results'] = all_results
                        error_sample = batch_results[0].get('summary', '') if batch_results else 'Unknown'
                        st.error(f"All profiles in first batch failed. Stopping to save credits.\n\nError: {error_sample}")
                        st.rerun()

                    # Check if more batches
                    if end_idx < len(profiles_to_screen):
                        time.sleep(0.5)  # Brief pause
                        st.rerun()  # Continue with next batch
                    else:
                        # All done - finalize
                        st.session_state['screening_batch_mode'] = False
                        st.session_state['screening_results'] = all_results

                        # Save to DB
                        db_saved = 0
                        if HAS_DATABASE:
                            try:
                                db_client = _get_db_client()
                                if db_client:
                                    for result in all_results:
                                        linkedin_url = result.get('linkedin_url') or result.get('public_url')
                                        if linkedin_url and result.get('fit') != 'Error':
                                            update_profile_screening(
                                                db_client, linkedin_url,
                                                score=result.get('score', 0),
                                                fit_level=result.get('fit', ''),
                                                summary=result.get('summary', ''),
                                                reasoning=result.get('reasoning', '')
                                            )
                                            db_saved += 1
                            except:
                                pass

                        db_msg = f" ({db_saved} saved to DB)" if db_saved > 0 else ""
                        st.success(f"✅ Screening complete! {len(all_results)} profiles{db_msg}")
                        send_notification("Screening Complete", f"Screened {len(all_results)} profiles")
                        save_session_state()  # Save for restore
                        st.rerun()

            if start_button or continue_button:
                # Validate OpenAI API key before starting
                try:
                    test_client = OpenAI(api_key=openai_key)
                    test_response = test_client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": "Say OK"}],
                        max_tokens=3
                    )
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
                if continue_button:
                    # Continue: only screen profiles not yet screened
                    existing_results = st.session_state.get('screening_results', [])
                    screened_urls = set(r.get('linkedin_url', '') for r in existing_results if r.get('linkedin_url'))
                    unscreened = [p for p in profiles if p.get('linkedin_url', '') not in screened_urls]
                    profiles_to_screen = unscreened[:screen_count]
                    # Keep existing results to append to
                    initial_results = existing_results.copy()
                else:
                    # Start fresh
                    profiles_to_screen = profiles[:screen_count]
                    initial_results = []

                st.session_state['screening_batch_mode'] = True
                st.session_state['screening_cancelled'] = False
                st.session_state['screening_batch_state'] = {
                    'profiles': profiles_to_screen,
                    'job_description': job_description,
                    'extra_requirements': extra_requirements if extra_requirements else "",
                    'mode': 'quick' if screening_mode == "Quick (cheaper)" else 'detailed',
                    'system_prompt': st.session_state.get('active_screening_prompt', active_prompt),
                    'current_batch': 0,
                    'results': initial_results,  # Start with existing results if continuing
                    'is_continue': continue_button  # Flag to know we're continuing
                }
                st.session_state['screening_batch_progress'] = {
                    'completed': len(initial_results),
                    'total': len(initial_results) + len(profiles_to_screen)
                }

                action = "Continuing" if continue_button else "Starting"
                st.info(f"{action} screening of {len(profiles_to_screen)} profiles in batches of 10...")
                st.rerun()
        else:
            st.warning("Please paste a job description to start screening")

        # Show screening results
        if 'screening_results' in st.session_state and st.session_state['screening_results']:
            st.divider()
            st.markdown("### Screening Results")

            screening_results = st.session_state['screening_results']

            # Summary stats
            stats_col1, stats_col2, stats_col3, stats_col4 = st.columns(4)
            strong_fit = sum(1 for r in screening_results if r.get('fit') == 'Strong Fit')
            good_fit = sum(1 for r in screening_results if r.get('fit') == 'Good Fit')
            partial_fit = sum(1 for r in screening_results if r.get('fit') == 'Partial Fit')
            not_fit = sum(1 for r in screening_results if r.get('fit') == 'Not a Fit')

            stats_col1.metric("Strong Fit", strong_fit, delta=None)
            stats_col2.metric("Good Fit", good_fit, delta=None)
            stats_col3.metric("Partial Fit", partial_fit, delta=None)
            stats_col4.metric("Not a Fit", not_fit, delta=None)

            # Filter by fit level
            fit_filter = st.multiselect(
                "Filter by fit level",
                options=["Strong Fit", "Good Fit", "Partial Fit", "Not a Fit", "Error"],
                default=["Strong Fit", "Good Fit"],
                key="fit_filter"
            )

            # Filter and sort results
            filtered_results = [r for r in screening_results if r.get('fit') in fit_filter]
            sorted_results = sorted(filtered_results, key=lambda x: x.get('score', 0), reverse=True)

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
                enriched_df_merge = st.session_state.get('enriched_df')
                if enriched_df_merge is not None and not enriched_df_merge.empty and 'linkedin_url' in df_display.columns:
                    merge_cols = [c for c in enriched_df_merge.columns if c not in df_display.columns]
                    if merge_cols and 'linkedin_url' in enriched_df_merge.columns:
                        df_display = df_display.merge(
                            enriched_df_merge[['linkedin_url'] + merge_cols],
                            on='linkedin_url', how='left'
                        )

                # Reorder columns to put important ones first
                priority_cols = ['score', 'fit', 'name', 'current_title', 'current_company', 'summary', 'why', 'strengths', 'concerns', 'skills', 'all_employers', 'all_titles', 'all_schools', 'location', 'linkedin_url']
                ordered_cols = [c for c in priority_cols if c in df_display.columns]
                other_cols = [c for c in df_display.columns if c not in priority_cols and c != 'index']
                df_display = df_display[ordered_cols + other_cols]

                st.dataframe(
                    df_display,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "score": st.column_config.NumberColumn("Score", format="%d/10"),
                        "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                    }
                )
                st.caption(f"Showing {len(df_display.columns)} columns")
            else:
                # Show summary view
                display_data = []
                for r in sorted_results:
                    display_data.append({
                        'Score': r.get('score', 0),
                        'Fit': r.get('fit', ''),
                        'Name': r.get('name', ''),
                        'LinkedIn': r.get('linkedin_url', ''),
                        'Title': r.get('current_title', '')[:40],
                        'Company': r.get('current_company', '')[:30],
                        'Summary': r.get('summary', '')[:100]
                    })

                df_display = pd.DataFrame(display_data)

                st.dataframe(
                    df_display,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Score": st.column_config.NumberColumn("Score", format="%d/10", width="small"),
                        "Fit": st.column_config.TextColumn("Fit", width="medium"),
                        "Name": st.column_config.TextColumn("Name", width="medium"),
                        "LinkedIn": st.column_config.LinkColumn("🔗", width="small", display_text="Open"),
                        "Title": st.column_config.TextColumn("Title", width="medium"),
                        "Company": st.column_config.TextColumn("Company", width="medium"),
                        "Summary": st.column_config.TextColumn("Summary", width="large")
                    }
                )

            # ===== SalesQL Email Enrichment =====
            st.markdown("### Email Enrichment (SalesQL)")
            salesql_key = load_salesql_key()
            if salesql_key:
                screening_df = pd.DataFrame(sorted_results)

                # Choose which candidates to enrich
                candidate_source = st.radio(
                    "Which candidates to enrich?",
                    ["Priority (Strong Fit + Good Fit)", "All candidates"],
                    key="candidate_source_tab5",
                    horizontal=True
                )

                if candidate_source == "Priority (Strong Fit + Good Fit)" and 'fit' in screening_df.columns:
                    enrich_df = screening_df[screening_df['fit'].isin(['Strong Fit', 'Good Fit'])].copy()
                    st.caption(f"Priority candidates: {len(enrich_df)} (Strong Fit + Good Fit)")
                else:
                    enrich_df = screening_df.copy()

                current_count = len(enrich_df)
                already_enriched = (enrich_df['salesql_email'].notna() & (enrich_df['salesql_email'] != '')).sum() if 'salesql_email' in enrich_df.columns else 0
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
                        st.dataframe(enriched_preview[available_cols], use_container_width=True, hide_index=True)
            else:
                st.caption("SalesQL not configured. Add 'salesql_api_key' to secrets.")

            # Export options
            st.markdown("### Export Results")
            export_col1, export_col2, export_col3 = st.columns(3)

            with export_col1:
                # Full results CSV
                full_df = pd.DataFrame(sorted_results)
                st.download_button(
                    "Download All Results (CSV)",
                    full_df.to_csv(index=False),
                    "screening_results_all.csv",
                    "text/csv"
                )

            with export_col2:
                # Strong + Good fit only
                top_candidates = [r for r in screening_results if r.get('fit') in ['Strong Fit', 'Good Fit']]
                top_df = pd.DataFrame(top_candidates)
                st.download_button(
                    f"Download Top Candidates ({len(top_candidates)})",
                    top_df.to_csv(index=False) if not top_df.empty else "",
                    "screening_results_top.csv",
                    "text/csv",
                    disabled=len(top_candidates) == 0
                )

            with export_col3:
                # Clear results
                if st.button("Clear Results", key="clear_screening"):
                    del st.session_state['screening_results']
                    st.rerun()

# ========== TAB 6: Database ==========
with tab_database:
    st.markdown("### Profile Database")
    st.caption("View and manage all profiles stored in Supabase")

    if not HAS_DATABASE:
        st.warning("Database module not available. Check db.py import.")
    else:
        try:
            db_client = _get_db_client()
            if not db_client:
                st.warning("Supabase not configured. Add 'supabase_url' and 'supabase_key' to secrets.")
            elif not check_connection(db_client):
                st.error("Cannot connect to Supabase. Check your credentials.")
            else:
                # Connection successful - show stats
                st.success("Connected to Supabase")

                # Pipeline stats - count directly from profiles table
                stats = {}
                try:
                    total = db_client.count('profiles')
                    enriched = db_client.count('profiles', {'enriched_at': 'not.is.null'})
                    screened = db_client.count('profiles', {'screening_score': 'not.is.null'})
                    stats = {'total': total, 'enriched': enriched, 'screened': screened, 'scraped': total, 'contacted': 0, 'stale_profiles': 0}
                except Exception as e:
                    st.warning(f"Could not load stats: {e}")
                if stats:
                    st.markdown("#### Pipeline Overview")
                    stat_cols = st.columns(6)
                    stat_cols[0].metric("Total", stats.get('total', 0))
                    stat_cols[1].metric("Scraped", stats.get('scraped', 0))
                    stat_cols[2].metric("Enriched", stats.get('enriched', 0))
                    stat_cols[3].metric("Screened", stats.get('screened', 0))
                    stat_cols[4].metric("Contacted", stats.get('contacted', 0))
                    stat_cols[5].metric("Stale (>6mo)", stats.get('stale_profiles', 0))

                st.divider()

                # View profiles by fit level
                st.markdown("#### Browse Profiles")

                view_options = ["All Profiles", "Strong Fit", "Good Fit", "Partial Fit", "Not a Fit", "Needs Enrichment", "Needs Screening"]
                selected_view = st.selectbox("View", view_options, key="db_view_select")

                # Fetch profiles based on selection
                profiles = []
                if selected_view == "All Profiles":
                    profiles = get_all_profiles(db_client, limit=500)
                elif selected_view in ["Strong Fit", "Good Fit", "Partial Fit", "Not a Fit"]:
                    profiles = get_profiles_by_fit_level(db_client, selected_view, limit=500)
                elif selected_view == "Needs Enrichment":
                    from db import get_profiles_needing_enrichment
                    profiles = get_profiles_needing_enrichment(db_client, limit=500)
                elif selected_view == "Needs Screening":
                    from db import get_profiles_needing_screening
                    profiles = get_profiles_needing_screening(db_client, limit=500)

                if profiles:
                    st.info(f"Showing **{len(profiles)}** profiles")
                    df = profiles_to_dataframe(profiles)

                    # Create combined name column for display
                    if 'first_name' in df.columns and 'last_name' in df.columns:
                        df['name'] = (df['first_name'].fillna('') + ' ' + df['last_name'].fillna('')).str.strip()

                    # Toggle to show all columns
                    show_all_db_cols = st.checkbox("Show all columns", value=False, key="db_show_all_cols")

                    if show_all_db_cols:
                        # Show all Crustdata columns + screening fields
                        all_cols = ['name', 'current_title', 'current_company', 'all_employers', 'all_titles', 'all_schools', 'skills', 'past_positions', 'headline', 'location', 'summary', 'connections_count', 'screening_score', 'screening_fit_level', 'email', 'status', 'enriched_at', 'linkedin_url']
                        available_cols = [c for c in all_cols if c in df.columns]
                        st.dataframe(
                            df[available_cols] if available_cols else df,
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                                "screening_score": st.column_config.NumberColumn("Score", format="%d"),
                                "enriched_at": st.column_config.DatetimeColumn("Enriched", format="YYYY-MM-DD"),
                            }
                        )
                        st.caption(f"{len(available_cols)} columns")
                    else:
                        # Simple preview: name, title, company, linkedin
                        preview_cols = ['name', 'current_title', 'current_company', 'linkedin_url']
                        available_cols = [c for c in preview_cols if c in df.columns]

                        st.dataframe(
                            df[available_cols] if available_cols else df,
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "name": st.column_config.TextColumn("Name"),
                                "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                                "current_company": st.column_config.TextColumn("Company"),
                                "current_title": st.column_config.TextColumn("Title"),
                            }
                        )

                    # Export button
                    st.download_button(
                        f"Download {selected_view} (CSV)",
                        df.to_csv(index=False),
                        f"database_{selected_view.lower().replace(' ', '_')}.csv",
                        "text/csv"
                    )
                else:
                    st.info(f"No profiles found for '{selected_view}'")

                # Load from database to session
                st.divider()
                st.markdown("#### Load from Database")
                st.caption("Load profiles from database into the current session for processing")

                load_options = ["Strong Fit", "Good Fit", "All Screened", "All Enriched"]
                load_selection = st.selectbox("Load profiles", load_options, key="db_load_select")

                if st.button("Load to Session", key="db_load_btn"):
                    load_profiles = []
                    if load_selection == "Strong Fit":
                        load_profiles = get_profiles_by_fit_level(db_client, "Strong Fit", limit=1000)
                    elif load_selection == "Good Fit":
                        load_profiles = get_profiles_by_fit_level(db_client, "Good Fit", limit=1000)
                    elif load_selection == "All Screened":
                        from db import get_profiles_by_status
                        load_profiles = get_profiles_by_status(db_client, "screened", limit=1000)
                    elif load_selection == "All Enriched":
                        from db import get_profiles_by_status
                        load_profiles = get_profiles_by_status(db_client, "enriched", limit=1000)

                    if load_profiles:
                        load_df = profiles_to_dataframe(load_profiles)
                        st.session_state['results'] = load_profiles
                        st.session_state['results_df'] = load_df
                        st.success(f"Loaded **{len(load_profiles)}** profiles from database!")
                        st.rerun()
                    else:
                        st.warning("No profiles found to load")

        except Exception as e:
            st.error(f"Database error: {e}")

# ========== TAB 7: Usage ==========
with tab_usage:
    st.markdown("### API Usage Dashboard")
    st.caption("Track API consumption across all providers")

    if not HAS_DATABASE:
        st.warning("Database module not available. Usage tracking requires Supabase.")
    else:
        try:
            db_client = _get_db_client()
            if not db_client:
                st.warning("Supabase not configured. Add 'supabase_url' and 'supabase_key' to secrets.")
            elif not check_connection(db_client):
                st.error("Cannot connect to Supabase. Check your credentials.")
            else:
                # Date range selector
                date_range = st.selectbox(
                    "Date Range",
                    ["Today", "7 Days", "30 Days", "All Time"],
                    index=1,
                    key="usage_date_range"
                )

                # Map selection to days
                days_map = {"Today": 1, "7 Days": 7, "30 Days": 30, "All Time": None}
                selected_days = days_map[date_range]

                # Fetch usage summary
                summary = get_usage_summary(db_client, days=selected_days)

                # Display metrics in columns
                st.markdown("#### Provider Summary")
                metric_cols = st.columns(4)

                with metric_cols[0]:
                    crustdata = summary.get('crustdata', {})
                    st.metric(
                        "Crustdata",
                        f"{int(crustdata.get('credits', 0)):,} credits",
                        help="3 credits per profile enriched"
                    )
                    st.caption(f"{crustdata.get('requests', 0)} requests")

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
                        help="gpt-4o-mini: $0.15/1M input, $0.60/1M output"
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
                if HAS_PLOTLY and selected_days:
                    st.markdown("#### Usage Over Time")

                    # Fetch daily usage data
                    daily_data = get_usage_by_date(db_client, days=selected_days or 365)

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

                        st.plotly_chart(fig_line, use_container_width=True)

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
                            st.plotly_chart(fig_cost, use_container_width=True)

                    else:
                        st.info("No usage data available for the selected period")

                    # Pie chart for cost breakdown
                    st.markdown("#### Cost Breakdown")
                    cost_data = {
                        'Provider': ['OpenAI'],
                        'Cost': [summary.get('openai', {}).get('cost_usd', 0)]
                    }
                    # Note: Crustdata and SalesQL costs would need pricing info to include

                    if cost_data['Cost'][0] > 0:
                        fig_pie = px.pie(
                            pd.DataFrame(cost_data),
                            values='Cost',
                            names='Provider',
                            title='Cost Distribution (USD)',
                            color_discrete_sequence=['#34d399', '#615fff', '#38bdf8', '#a78bfa']
                        )
                        fig_pie.update_layout(height=300, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#e2e8f0'))
                        st.plotly_chart(fig_pie, use_container_width=True)
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
                    if st.button("Refresh", key="usage_refresh"):
                        st.rerun()

                # Fetch logs
                logs = get_usage_logs(
                    db_client,
                    provider=provider_filter if provider_filter != "All" else None,
                    days=selected_days,
                    limit=log_limit
                )

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
                        use_container_width=True,
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
                else:
                    st.info("No usage logs found for the selected filters")

        except Exception as e:
            st.error(f"Usage dashboard error: {e}")
