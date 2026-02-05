"""
LinkedIn Profile Enricher Dashboard
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
        get_usage_by_date, get_enriched_urls,
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
    page_title="LinkedIn Enricher",
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

# Professional UI styling
st.markdown("""
<style>
    /* Professional tab styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background-color: #F0F2F5;
        padding: 0.5rem;
        border-radius: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        background-color: white;
        border-radius: 6px;
        padding: 0.5rem 1rem;
        font-weight: 500;
    }
    .stTabs [aria-selected="true"] {
        background-color: #0077B5 !important;
        color: white !important;
    }

    /* Title styling */
    h1 { color: #0077B5; border-bottom: 3px solid #0077B5; padding-bottom: 0.5rem; }

    /* Metric cards */
    [data-testid="stMetricValue"] { font-size: 2rem; color: #0077B5; }

    /* Buttons */
    .stButton > button[kind="primary"] {
        background-color: #0077B5;
        border-radius: 24px;
        font-weight: 600;
    }

    /* Sidebar */
    [data-testid="stSidebar"] { background-color: #F8F9FA; }
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
            # Quick inline save
            try:
                session_data = {}
                for key in ['results', 'results_df', 'enriched_results', 'enriched_df', 'screening_results', 'passed_candidates_df']:
                    if key in st.session_state and st.session_state[key] is not None:
                        value = st.session_state[key]
                        if isinstance(value, pd.DataFrame):
                            session_data[key] = {'_type': 'dataframe', 'data': value.to_dict('records')}
                        else:
                            session_data[key] = {'_type': 'list', 'data': value}
                if session_data:
                    with open(SESSION_FILE, 'w') as f:
                        json.dump(session_data, f)
                    st.success("Saved!")
            except Exception as e:
                st.error(f"Failed: {e}")
    with col_clear:
        if st.button("Clear", key="sidebar_clear_session", help="Clear saved session"):
            from pathlib import Path
            SESSION_FILE = Path(__file__).parent / '.last_session.json'
            if SESSION_FILE.exists():
                SESSION_FILE.unlink()
            for key in ['results', 'results_df', 'enriched_results', 'enriched_df', 'screening_results', 'passed_candidates_df']:
                if key in st.session_state:
                    del st.session_state[key]
            st.success("Cleared!")
            st.rerun()
    st.divider()

# Load API keys
def load_config():
    """Load config from config.json or Streamlit secrets (for cloud deployment)."""
    config = {}

    # Try loading from config.json (local development)
    config_path = Path(__file__).parent / 'config.json'
    if config_path.exists():
        with open(config_path, 'r') as f:
            config = json.load(f)

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

def save_session_state():
    """Save current session state to a local file for persistence across refreshes."""
    try:
        session_data = {}
        keys_to_save = [
            'results', 'results_df', 'enriched_results', 'enriched_df',
            'screening_results', 'filtered_results', 'passed_candidates_df',
            'filter_stats', 'last_load_count', 'last_load_file'
        ]
        for key in keys_to_save:
            if key in st.session_state and st.session_state[key] is not None:
                value = st.session_state[key]
                # Convert DataFrames to dict for JSON serialization
                if isinstance(value, pd.DataFrame):
                    session_data[key] = {'_type': 'dataframe', 'data': value.to_dict('records')}
                elif isinstance(value, list):
                    session_data[key] = {'_type': 'list', 'data': value}
                elif isinstance(value, dict):
                    session_data[key] = {'_type': 'dict', 'data': value}
                else:
                    session_data[key] = {'_type': 'value', 'data': value}

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


def get_usage_tracker():
    """Get a UsageTracker instance with database connection."""
    if not HAS_USAGE_TRACKER or not HAS_DATABASE:
        return None
    try:
        db_client = get_supabase_client()
        if db_client and check_connection(db_client):
            return UsageTracker(db_client)
    except Exception:
        pass
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
    """Load search history from file.

    Args:
        agent_id: If provided, filter history to this agent only

    Returns list of dicts with keys: agent_id, csv_name, search_url, launched_at, profiles_requested
    """
    history_path = get_search_history_path()
    if not history_path.exists():
        return []

    try:
        with open(history_path, 'r') as f:
            history = json.load(f)

        # Filter by agent_id if provided
        if agent_id:
            history = [h for h in history if h.get('agent_id') == agent_id]

        # Sort by launched_at descending (most recent first)
        history.sort(key=lambda x: x.get('launched_at', ''), reverse=True)
        return history
    except Exception:
        return []


def save_search_to_history(agent_id: str, csv_name: str, search_url: str = None, profiles_requested: int = None, search_name: str = None) -> bool:
    """Save a search to history file.

    Returns True if saved successfully.
    """
    history_path = get_search_history_path()

    try:
        # Load existing history
        if history_path.exists():
            with open(history_path, 'r') as f:
                history = json.load(f)
        else:
            history = []

        # Add new entry
        entry = {
            'agent_id': agent_id,
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

        return True
    except Exception:
        return False


def delete_search_from_history(agent_id: str, csv_name: str, api_key: str = None, delete_file: bool = False) -> bool:
    """Delete a search from history and optionally delete the file from PhantomBuster.

    Returns True if deleted successfully.
    """
    history_path = get_search_history_path()

    try:
        # Load history
        if not history_path.exists():
            return False

        with open(history_path, 'r') as f:
            history = json.load(f)

        # Find and remove entry
        original_len = len(history)
        history = [h for h in history if not (h.get('agent_id') == agent_id and h.get('csv_name') == csv_name)]

        if len(history) == original_len:
            return False  # Entry not found

        # Save updated history
        with open(history_path, 'w') as f:
            json.dump(history, f, indent=2)

        # Delete file from PhantomBuster if requested
        if delete_file and api_key:
            delete_phantombuster_file(api_key, agent_id, f'{csv_name}.csv')
            delete_phantombuster_file(api_key, agent_id, f'{csv_name}.json')

        return True
    except Exception:
        return False


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
            # If specific filename provided, try it first
            files_to_try = []
            if filename:
                files_to_try = [f'{filename}.csv', f'{filename}.json']
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

        # If specific filename provided, ONLY try that file (strict mode)
        if filename:
            possible_files = [f'{filename}.csv', f'{filename}.json']
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

        files = []

        # Try to list files from agent's fileMgmt if available
        file_mgmt = agent_data.get('fileMgmt', {})
        if file_mgmt:
            for filename, info in file_mgmt.items():
                files.append({
                    'name': filename,
                    'size': info.get('size', 0),
                    'lastModified': info.get('lastModified', ''),
                })

        # Also try common file patterns via store API
        common_files = [
            'result.csv', 'result.json',
            'database-linkedin-sales-navigator-search-export.csv',
            'database-sales-navigator-search-export.csv',
        ]

        # Add timestamped patterns (recent dates)
        from datetime import datetime, timedelta
        for i in range(30):  # Check last 30 days
            date = datetime.now() - timedelta(days=i)
            date_str = date.strftime('%Y-%m-%d')
            common_files.append(f'result_{date_str}.csv')
            common_files.append(f'search_{date_str}.csv')

        for fname in common_files:
            try:
                check_response = requests.head(
                    'https://api.phantombuster.com/api/v2/store/fetch',
                    params={'id': agent_id, 'name': fname},
                    headers={'X-Phantombuster-Key': api_key},
                    timeout=5
                )
                if check_response.status_code == 200:
                    # File exists, add if not already in list
                    if not any(f['name'] == fname for f in files):
                        files.append({
                            'name': fname,
                            'size': int(check_response.headers.get('content-length', 0)),
                            'lastModified': check_response.headers.get('last-modified', ''),
                        })
            except:
                pass

        # Try cache URL listing if we have S3 info
        if s3_folder and org_s3_folder:
            try:
                cache_base = f'https://cache1.phantombooster.com/{org_s3_folder}/{s3_folder}/'
                # Check for result files
                for fname in ['result.csv', 'result.json']:
                    try:
                        check = requests.head(cache_base + fname, timeout=5)
                        if check.status_code == 200:
                            if not any(f['name'] == fname for f in files):
                                files.append({
                                    'name': fname,
                                    'size': int(check.headers.get('content-length', 0)),
                                    'lastModified': check.headers.get('last-modified', ''),
                                })
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
                return url
            return None
        df['linkedin_url'] = df['linkedin_url'].apply(clean_linkedin_url)

    # Create public_url as alias for linkedin_url (for display compatibility)
    if 'linkedin_url' in df.columns and 'public_url' not in df.columns:
        df['public_url'] = df['linkedin_url']

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


def get_gspread_client():
    """Get authenticated gspread client using service account."""
    config = load_config()

    scopes = [
        'https://www.googleapis.com/auth/spreadsheets.readonly',
        'https://www.googleapis.com/auth/drive.readonly'
    ]

    # Try using credentials from Streamlit secrets (cloud deployment)
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
                app_name="LinkedIn Enricher",
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

    # 1. Past candidates filter
    if filters.get('past_candidates_df') is not None:
        past_df = filters['past_candidates_df']
        if 'Name' in past_df.columns:
            past_names = set(str(name).lower().strip() for name in past_df['Name'].dropna())
            df['_full_name'] = (df['first_name'].fillna('').str.lower().str.strip() + ' ' +
                               df['last_name'].fillna('').str.lower().str.strip())
            df['_is_past'] = df['_full_name'].isin(past_names)
            stats['past_candidates'] = df['_is_past'].sum()
            filtered_out['Past Candidates'] = df[df['_is_past']].drop(columns=['_is_past', '_full_name']).copy()
            df = df[~df['_is_past']].drop(columns=['_is_past', '_full_name'])

    # 2. Blacklist filter
    if filters.get('blacklist'):
        blacklist = [c.lower().strip() for c in filters['blacklist']]
        df['_blacklisted'] = df['current_company'].apply(lambda x: matches_list(x, blacklist))
        stats['blacklist'] = df['_blacklisted'].sum()
        filtered_out['Blacklist Companies'] = df[df['_blacklisted']].drop(columns=['_blacklisted']).copy()
        df = df[~df['_blacklisted']].drop(columns=['_blacklisted'])

    # 3. Not relevant companies (current)
    if filters.get('not_relevant'):
        not_relevant = [c.lower().strip() for c in filters['not_relevant']]
        df['_not_relevant'] = df['current_company'].apply(lambda x: matches_list(x, not_relevant))
        stats['not_relevant_current'] = df['_not_relevant'].sum()
        filtered_out['Not Relevant (Current)'] = df[df['_not_relevant']].drop(columns=['_not_relevant']).copy()
        df = df[~df['_not_relevant']].drop(columns=['_not_relevant'])

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
            if pd.isna(education) or not str(education).strip():
                return False
            edu_lower = str(education).lower()
            return any(uni in edu_lower for uni in universities)

        df['_top_uni'] = df['education'].apply(has_top_university)
        not_top_uni = ~df['_top_uni']
        stats['not_top_university'] = not_top_uni.sum()
        filtered_out['Not Top University'] = df[not_top_uni].drop(columns=['_top_uni']).copy()
        df = df[df['_top_uni']].drop(columns=['_top_uni'])

    stats['original'] = original_count
    stats['final'] = len(df)
    stats['total_removed'] = original_count - len(df)

    return df, stats, filtered_out


# Screening system prompt based on the /screen skill
SCREENING_SYSTEM_PROMPT = """You are an expert senior technical recruiter with 15+ years of experience hiring for top tech companies.

## Scoring Rubric
- **9-10**: Exceptional match. Meets all requirements with bonus qualifications. Rare.
- **7-8**: Strong match. Meets all core requirements. Top 20% of candidates.
- **5-6**: Partial match. Missing 1-2 requirements but has potential.
- **3-4**: Weak match. Missing multiple requirements.
- **1-2**: Not a fit. Don't waste time.

## Score Boosters (+1 to +2 points)
Give higher scores when candidate has:

1. **Strong Company Background**: Currently or recently at well-known tech companies:
   - Top Israeli startups: Wiz, Snyk, Monday, Gong, AppsFlyer, Fireblocks, Rapyd, etc.
   - Award winners: RSA Innovation Sandbox, Y Combinator, top-tier VC backed
   - Big tech: Google, Meta, Amazon, Microsoft (in engineering roles)
   - Acquired startups at good valuations

2. **Relevant Education**: CS/Software Engineering degree from strong universities:
   - Israel: Technion, Tel Aviv University, Hebrew University, Ben-Gurion, Bar-Ilan, Weizmann
   - Global: MIT, Stanford, CMU, Berkeley, etc.
   - MSc/PhD in CS is a plus

**Important**: If candidate is from a strong company with relevant education, skills/description matter less - the company already vetted them.

## Auto-Disqualifiers (Score 3 or below)
- **Only .NET/C#**: No Node, Python, or modern backend stack
- **Group Manager/Director+**: Not hands-on (Team Lead is OK)
- **Embedded/Systems engineer**: Wrong domain (C++, firmware, kernel)
- **QA/Automation background**: Limited backend development depth
- **Consulting/project companies**: Tikal, Matrix, Ness, Sela, etc.

## Guidelines
1. **Be Direct**: Don't sugarcoat. Give honest evaluations.
2. **Use Evidence**: Reference specific profile data (years, skills, companies).
3. **Be Calibrated**: A 10/10 should be rare. Most good candidates are 6-8.
4. **Company > Skills**: Strong company pedigree compensates for skill list gaps."""


def screen_profile(profile: dict, job_description: str, client: OpenAI, extra_requirements: str = "", tracker: 'UsageTracker' = None) -> dict:
    """Screen a profile against a job description using OpenAI."""
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
Education: {safe_str(profile.get('education', 'N/A'))}
Skills: {safe_str(profile.get('skills', 'N/A'))}
Summary: {safe_str(profile.get('summary', 'N/A'))}
Past Positions: {safe_str(profile.get('past_positions', 'N/A'))}"""

    user_prompt = f"""Evaluate this candidate against the job description.

## Job Description:
{job_description}

{f"## Extra Requirements:{chr(10)}{extra_requirements}" if extra_requirements else ""}

## Candidate Profile:
{profile_summary}

Respond with ONLY valid JSON in this exact format:
{{"score": <1-10>, "fit": "<Strong Fit|Good Fit|Partial Fit|Not a Fit>", "summary": "<2-3 sentences about the candidate>", "why": "<2-3 sentences explaining the score>", "strengths": ["<strength1>", "<strength2>"], "concerns": ["<concern1>", "<concern2>"]}}"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SCREENING_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=500
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
            "summary": f"JSON parse error",
            "why": str(e)[:100],
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
            "summary": f"API error: {str(e)[:50]}",
            "why": str(e)[:100],
            "strengths": [],
            "concerns": []
        }


def screen_profiles_batch(profiles: list, job_description: str, openai_api_key: str,
                          extra_requirements: str = "", max_workers: int = 50,
                          progress_placeholder=None) -> list:
    """Screen multiple profiles in parallel using ThreadPoolExecutor.

    Args:
        profiles: List of profile dicts to screen
        job_description: The job description to screen against
        openai_api_key: OpenAI API key (we create client per thread for safety)
        extra_requirements: Additional screening criteria
        max_workers: Number of concurrent threads (default 50 for Tier 3)
        progress_placeholder: Streamlit placeholder for progress updates

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
        # Create client per thread to avoid thread-safety issues
        client = OpenAI(api_key=openai_api_key)
        try:
            result = screen_profile(profile, job_description, client, extra_requirements, tracker=tracker)
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
                "why": traceback.format_exc()[:200],
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
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                import traceback
                idx = future_to_index[future]
                error_msg = str(e) if str(e) else "Unknown error"
                results.append({
                    "score": 0,
                    "fit": "Error",
                    "summary": f"Thread error: {error_msg[:80]}",
                    "why": traceback.format_exc()[:200],
                    "strengths": [],
                    "concerns": [],
                    "name": f"Profile {idx}",
                    "current_title": "",
                    "current_company": "",
                    "linkedin_url": "",
                    "index": idx
                })

    # Sort by original index to maintain order
    results.sort(key=lambda x: x.get('index', 0))
    return results


# Main UI
st.title("LinkedIn Profile Enricher")

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
                    db_client = get_supabase_client()
                    if db_client and check_connection(db_client):
                        from db import get_profiles_by_status
                        enriched_count = db_client.count('profiles', {'status': 'eq.enriched'})
                        screened_count = db_client.count('profiles', {'status': 'eq.screened'})

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

                # Count valid LinkedIn URLs
                valid_urls = df_uploaded['linkedin_url'].notna().sum() if 'linkedin_url' in df_uploaded.columns else 0

                # PhantomBuster/CSV data stays in session state only (not saved to DB)
                # DB save happens after Crustdata enrichment
                st.session_state['results'] = df_uploaded.to_dict('records')
                st.session_state['results_df'] = df_uploaded

                # Show success message with details
                msg = f"Loaded **{len(df_uploaded)}** profiles"
                if valid_urls > 0:
                    msg += f" ({valid_urls} with LinkedIn URLs ready for enrichment)"
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
                # Load search history for this agent
                search_history = load_search_history(agent_id=selected_agent['id'])

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
                                        st.rerun()
                                    else:
                                        st.error("No results found. File may have been deleted from PhantomBuster.")
                else:
                    st.info("No search history found. Launch a search below to get started.")
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
                                app_name="LinkedIn Enricher",
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
                                app_name="LinkedIn Enricher",
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
            skip_count = st.session_state.get('pb_launch_skip_count', 0)
            if skip_count > 0:
                st.info(f"**Running** - {elapsed} (auto-refreshing every 10s)\n\n⏭️ Skipping **{skip_count}** profiles already in database")
            else:
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
                    db_client = get_supabase_client()
                    if db_client and check_connection(db_client):
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
        needs_filtering = 'job_1_job_title' in df.columns and 'current_title' not in df.columns

        if needs_filtering:
            if st.button("Convert to Screening Format"):
                filtered_df = filter_csv_columns(df)
                st.session_state['results_df'] = filtered_df
                st.session_state['results'] = filtered_df.to_dict('records')
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

        # Save to session state
        if user_sheet_url:
            st.session_state['user_sheet_url'] = user_sheet_url
            filter_sheets['url'] = user_sheet_url

        has_sheets = bool(filter_sheets.get('url')) and gspread_client is not None

        if has_sheets:
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

        if st.button("Apply Filters", type="primary"):
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

                # Blacklist
                if use_sheets_blacklist and filter_sheets.get('blacklist'):
                    bl_df = load_sheet_as_df(sheet_url, filter_sheets['blacklist'])
                    if bl_df is not None and len(bl_df.columns) > 0:
                        filters['blacklist'] = bl_df.iloc[:, 0].dropna().tolist()
                        st.info(f"Loaded {len(filters['blacklist'])} blacklist companies from Google Sheet")
                elif blacklist_file:
                    bl_df = pd.read_csv(blacklist_file)
                    filters['blacklist'] = bl_df.iloc[:, 0].dropna().tolist()

                # Not relevant
                if use_sheets_not_relevant and filter_sheets.get('not_relevant'):
                    nr_df = load_sheet_as_df(sheet_url, filter_sheets['not_relevant'])
                    if nr_df is not None and len(nr_df.columns) > 0:
                        filters['not_relevant'] = nr_df.iloc[:, 0].dropna().tolist()
                        st.info(f"Loaded {len(filters['not_relevant'])} not-relevant companies from Google Sheet")
                elif not_relevant_file:
                    nr_df = pd.read_csv(not_relevant_file)
                    filters['not_relevant'] = nr_df.iloc[:, 0].dropna().tolist()

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

                st.session_state['passed_candidates_df'] = filtered_df  # Store filtered results separately
                st.session_state['results_df'] = filtered_df
                st.session_state['results'] = filtered_df.to_dict('records')
                st.session_state['filter_stats'] = stats
                st.session_state['filtered_out'] = filtered_out

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
            exclude_keys = ['original', 'final', 'total_removed']
            for key, value in stats.items():
                if key not in exclude_keys and value > 0:
                    st.text(f"  ✗ {key.replace('_', ' ').title()}: {value} removed")

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
                if pd.isna(text) or not str(text).strip():
                    return False
                text_lower = str(text).lower()
                for item in items_list:
                    item_norm = str(item).lower().strip()
                    if not item_norm or len(item_norm) < 3:
                        continue
                    if item_norm in text_lower:
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

                # Universities
                if filter_sheets.get('universities') and 'education' in passed_df.columns:
                    uni_df = load_sheet_as_df(sheet_url, filter_sheets['universities'])
                    if uni_df is not None and len(uni_df.columns) > 0:
                        uni_list = uni_df.iloc[:, 0].dropna().tolist()
                        passed_df['is_top_university'] = passed_df['education'].apply(lambda x: matches_list_in_text(x, uni_list))
                        st.info(f"Top Universities: {len(uni_list)} loaded, {passed_df['is_top_university'].sum()} matches")

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

    if not has_crust_key:
        st.warning("Crust Data API key not configured. Add 'api_key' to config.json")
    elif 'results' not in st.session_state or not st.session_state['results']:
        st.info("Load profiles first (tab 1). Filtering (tab 2) is optional.")
    else:
        results_df = st.session_state.get('results_df')
        enriched_df = st.session_state.get('enriched_df')

        # Check if already enriched (enriched_df exists)
        is_enriched = enriched_df is not None and not enriched_df.empty

        if is_enriched:
            st.success(f"**{len(enriched_df)}** profiles enriched! Proceed to Filter+ or AI Screen tab.")

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
                # Check for already enriched profiles in database
                already_enriched = set()
                skip_enriched = False
                db_check_error = None
                if HAS_DATABASE:
                    try:
                        db_client = get_supabase_client()
                        if db_client and check_connection(db_client):
                            already_enriched = get_enriched_urls(db_client)
                    except Exception as e:
                        db_check_error = str(e)

                # Filter out already enriched URLs
                new_urls = []
                skipped_urls = []
                for url in urls:
                    normalized = normalize_linkedin_url(url) if HAS_DATABASE else url
                    if normalized in already_enriched:
                        skipped_urls.append(url)
                    else:
                        new_urls.append(url)

                # Debug info
                with st.expander("Debug: Enrichment check", expanded=False):
                    st.write(f"URLs in loaded data: {len(urls)}")
                    st.write(f"Enriched URLs in DB: {len(already_enriched)}")
                    st.write(f"New (not enriched): {len(new_urls)}")
                    st.write(f"Already enriched: {len(skipped_urls)}")
                    if db_check_error:
                        st.error(f"DB check error: {db_check_error}")
                    if already_enriched:
                        st.write("Sample enriched URLs from DB:", list(already_enriched)[:3])
                    if urls:
                        st.write("Sample URLs from loaded data (normalized):", [normalize_linkedin_url(u) for u in urls[:3]])

                # Show stats
                if skipped_urls:
                    st.info(f"**{len(new_urls)}** new profiles to enrich | **{len(skipped_urls)}** already enriched (will be skipped)")
                    skip_enriched = st.checkbox("Skip already-enriched profiles", value=True, key="skip_enriched_cb")
                    urls_for_enrichment = new_urls if skip_enriched else urls
                else:
                    st.info(f"**{len(urls)}** profiles ready for enrichment")
                    urls_for_enrichment = urls

                if not urls_for_enrichment:
                    st.warning("All profiles have already been enriched. Uncheck 'Skip already-enriched' to re-enrich them.")
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

                    st.caption("Each profile costs 1 Crust Data credit")

                    if st.button("Start Enrichment", type="primary", key="start_enrich_tab"):
                        urls_to_process = urls_for_enrichment[:max_profiles]
                        results = []
                        original_urls = []  # Track original URLs in order
                        progress_bar = st.progress(0)
                        status_text = st.empty()
                        total_batches = (len(urls_to_process) + batch_size - 1) // batch_size

                        # Get usage tracker for logging
                        tracker = get_usage_tracker()

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
                        status_text.text("Enrichment complete!")
                        send_notification("Enrichment Complete", f"Processed {len(results)} profiles")

                        # Use Crustdata's response directly - no URL matching needed
                        # Crustdata's linkedin_profile_url is the source of truth

                        # Check for errors in results
                        errors = [r for r in results if 'error' in r]
                        successful = [r for r in results if 'error' not in r]

                        if successful:
                            # Save enriched data separately - don't overwrite original loaded data
                            st.session_state['enriched_results'] = successful
                            st.session_state['enriched_df'] = flatten_for_csv(successful)

                            # Auto-save enrichment to Supabase database
                            db_saved = 0
                            if HAS_DATABASE:
                                try:
                                    db_client = get_supabase_client()
                                    if db_client and check_connection(db_client):
                                        for profile in successful:
                                            # Use linkedin_flagship_url (clean format) from Crustdata
                                            linkedin_url = profile.get('linkedin_flagship_url') or profile.get('linkedin_profile_url') or profile.get('linkedin_url')
                                            if linkedin_url:
                                                update_profile_enrichment(db_client, linkedin_url, profile)
                                                db_saved += 1
                                except Exception as e:
                                    st.warning(f"Database save failed: {e}")

                            # Store message to show after rerun
                            db_msg = f" (DB: {db_saved} saved)" if db_saved > 0 else ""
                            if errors:
                                st.session_state['enrichment_message'] = f"warning:Enriched {len(successful)} profiles{db_msg}. {len(errors)} failed: {errors[0].get('error', 'Unknown')[:150]}"
                            else:
                                st.session_state['enrichment_message'] = f"success:Enriched {len(successful)} profiles successfully!{db_msg}"
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

        # Show available enriched columns
        enriched_cols = [c for c in enriched_df.columns if c.startswith('job_') or c.startswith('education_') or 'skill' in c.lower()]
        if enriched_cols:
            with st.expander("Available enriched fields"):
                st.write(enriched_cols[:20])

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

        has_sheets = bool(filter_sheets.get('url')) and gspread_client is not None

        if has_sheets:
            st.success("Filter sheet connected")

            if st.button("Apply Filters", type="primary", key="apply_filters_enriched"):
                with st.spinner("Applying filters..."):
                    # Use same filtering logic as Filter tab
                    df = enriched_df.copy()
                    original_count = len(df)
                    removed = {}

                    # Load filter lists from sheets
                    sheet_url = filter_sheets.get('url', '')

                    # Determine URL column name
                    url_col = 'linkedin_url' if 'linkedin_url' in df.columns else 'public_url' if 'public_url' in df.columns else None

                    # Past candidates filter
                    if filter_sheets.get('past_candidates') and url_col:
                        pc_df = load_sheet_as_df(sheet_url, filter_sheets['past_candidates'])
                        if pc_df is not None:
                            past_urls = set()
                            for col in pc_df.columns:
                                if 'url' in col.lower() or 'linkedin' in col.lower():
                                    past_urls.update(pc_df[col].dropna().str.lower().tolist())
                            before = len(df)
                            df = df[~df[url_col].str.lower().isin(past_urls)]
                            removed['Past Candidates'] = before - len(df)

                    # Blacklist filter
                    if filter_sheets.get('blacklist') and url_col:
                        bl_df = load_sheet_as_df(sheet_url, filter_sheets['blacklist'])
                        if bl_df is not None:
                            bl_urls = set()
                            for col in bl_df.columns:
                                if 'url' in col.lower() or 'linkedin' in col.lower():
                                    bl_urls.update(bl_df[col].dropna().str.lower().tolist())
                            before = len(df)
                            df = df[~df[url_col].str.lower().isin(bl_urls)]
                            removed['Blacklist'] = before - len(df)

                    # Store results
                    st.session_state['passed_candidates_df'] = df
                    st.session_state['filter_stats'] = {
                        'original': original_count,
                        'total_removed': original_count - len(df),
                        'final': len(df),
                        'removed_by': removed
                    }

                    st.success(f"Filtered: {original_count} → {len(df)} profiles")
                    for reason, count in removed.items():
                        if count > 0:
                            st.caption(f"  - {reason}: {count} removed")

        # Show current data
        st.divider()
        display_df = st.session_state.get('passed_candidates_df', enriched_df)
        st.markdown(f"**{len(display_df)}** profiles")

        display_cols = ['first_name', 'last_name', 'current_title', 'current_company', 'location', 'linkedin_url', 'public_url']
        available_cols = [c for c in display_cols if c in display_df.columns]
        st.dataframe(display_df[available_cols].head(50), use_container_width=True, hide_index=True,
                    column_config={
                        "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                        "public_url": st.column_config.LinkColumn("LinkedIn")
                    })

        if len(display_df) > 0:
            csv_data = display_df.to_csv(index=False)
            st.download_button("Download CSV", csv_data, "filtered_profiles.csv", "text/csv", key="download_filtered")

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

        # Screening Configuration
        st.markdown("### Screening Configuration")

        screen_count = st.number_input(
            "Number of profiles to screen",
            min_value=1,
            max_value=len(profiles),
            value=min(100, len(profiles)),
            step=10,
            key="screen_count"
        )

        # Fixed concurrent workers (optimal for Tier 3)
        max_workers = 50

        # Cost estimate
        est_cost = (screen_count * 2500 * 0.15 / 1_000_000) + (screen_count * 400 * 0.60 / 1_000_000)
        est_time = (screen_count / max_workers) * 2  # ~2 seconds per batch
        st.caption(f"Estimated: ~${est_cost:.2f} cost, ~{est_time:.0f} seconds")

        # Debug: Show available fields and test single profile
        with st.expander("Debug: Profile Fields & Test"):
            if profiles:
                st.write("Available fields in profiles:", list(profiles[0].keys()))
                st.write("Sample profile:", {k: str(v)[:100] for k, v in profiles[0].items()})

                if job_description and st.button("Test Single Profile", key="test_single"):
                    try:
                        client = OpenAI(api_key=openai_key)
                        st.write("Testing with first profile...")
                        result = screen_profile(profiles[0], job_description, client, extra_requirements)
                        st.write("Result:", result)
                    except Exception as e:
                        import traceback
                        st.error(f"Error: {e}")
                        st.code(traceback.format_exc())

        # Screen Button
        if job_description:
            if st.button("Start Screening", type="primary", key="start_screening", disabled='screening_in_progress' in st.session_state and st.session_state['screening_in_progress']):
                st.session_state['screening_in_progress'] = True

                profiles_to_screen = profiles[:screen_count]

                # Progress tracking
                status_text = st.empty()
                status_text.info(f"Screening {len(profiles_to_screen)} profiles... This may take a minute.")
                start_time = time.time()

                # Run batch screening (pass API key, not client)
                screening_results = screen_profiles_batch(
                    profiles_to_screen,
                    job_description,
                    openai_key,
                    extra_requirements=extra_requirements if extra_requirements else "",
                    max_workers=max_workers
                )

                # Complete
                elapsed = time.time() - start_time

                # Auto-save screening results to Supabase database
                db_saved = 0
                if HAS_DATABASE:
                    try:
                        db_client = get_supabase_client()
                        if db_client and check_connection(db_client):
                            for result in screening_results:
                                linkedin_url = result.get('linkedin_url') or result.get('public_url')
                                if linkedin_url and 'error' not in result:
                                    update_profile_screening(
                                        db_client,
                                        linkedin_url,
                                        score=result.get('score', 0),
                                        fit_level=result.get('fit', ''),
                                        summary=result.get('summary', ''),
                                        reasoning=result.get('reasoning', '')
                                    )
                                    db_saved += 1
                    except Exception as e:
                        pass  # Don't interrupt flow for DB errors

                db_msg = f" (DB: {db_saved} saved)" if db_saved > 0 else ""
                status_text.success(f"Completed {len(screening_results)} profiles in {elapsed:.1f}s{db_msg}")

                st.session_state['screening_results'] = screening_results
                st.session_state['screening_in_progress'] = False

                # Send notification
                send_notification("Screening Complete", f"Screened {len(screening_results)} profiles")
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

            st.info(f"Showing **{len(sorted_results)}** of {len(screening_results)} screened profiles")

            # Create display dataframe
            display_data = []
            for r in sorted_results:
                display_data.append({
                    'Score': r.get('score', 0),
                    'Fit': r.get('fit', ''),
                    'Name': r.get('name', ''),
                    'Title': r.get('current_title', '')[:40],
                    'Company': r.get('current_company', '')[:30],
                    'Summary': r.get('summary', '')[:100],
                    'LinkedIn': r.get('linkedin_url', '')
                })

            df_display = pd.DataFrame(display_data)

            # Display table
            st.dataframe(
                df_display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Score": st.column_config.NumberColumn("Score", format="%d/10", width="small"),
                    "Fit": st.column_config.TextColumn("Fit", width="medium"),
                    "Name": st.column_config.TextColumn("Name", width="medium"),
                    "Title": st.column_config.TextColumn("Title", width="medium"),
                    "Company": st.column_config.TextColumn("Company", width="medium"),
                    "Summary": st.column_config.TextColumn("Summary", width="large"),
                    "LinkedIn": st.column_config.LinkColumn("LinkedIn", width="small")
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

                if candidate_source == "Priority (Strong Fit + Good Fit)":
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
            db_client = get_supabase_client()
            if not db_client:
                st.warning("Supabase not configured. Add 'supabase_url' and 'supabase_key' to secrets.")
            elif not check_connection(db_client):
                st.error("Cannot connect to Supabase. Check your credentials.")
            else:
                # Connection successful - show stats
                st.success("Connected to Supabase")

                # Pipeline stats
                stats = get_pipeline_stats(db_client)
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
            db_client = get_supabase_client()
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
                        help="1 credit per profile enriched"
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
                            line=dict(color='#0077B5')
                        ))

                        fig_line.add_trace(go.Scatter(
                            x=df_daily['date'],
                            y=df_daily['salesql'],
                            mode='lines+markers',
                            name='SalesQL (lookups)',
                            line=dict(color='#00A0DC')
                        ))

                        fig_line.add_trace(go.Scatter(
                            x=df_daily['date'],
                            y=df_daily['phantombuster'],
                            mode='lines+markers',
                            name='PhantomBuster (runs)',
                            line=dict(color='#6B5B95')
                        ))

                        fig_line.update_layout(
                            title='API Usage by Day',
                            xaxis_title='Date',
                            yaxis_title='Count',
                            hovermode='x unified',
                            legend=dict(orientation='h', yanchor='bottom', y=1.02),
                            height=350
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
                            fig_cost.update_traces(fill='tozeroy', line_color='#10A37F')
                            fig_cost.update_layout(height=250)
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
                            color_discrete_sequence=['#10A37F', '#0077B5', '#00A0DC', '#6B5B95']
                        )
                        fig_pie.update_layout(height=300)
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
