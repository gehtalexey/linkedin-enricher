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
import winsound
from pathlib import Path
from datetime import datetime
from plyer import notification
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials


# Page config
st.set_page_config(
    page_title="LinkedIn Enricher",
    page_icon="🔍",
    layout="wide"
)

# Load API keys
def load_config():
    config_path = Path(__file__).parent / 'config.json'
    if config_path.exists():
        with open(config_path, 'r') as f:
            return json.load(f)
    return {}

def load_api_key():
    config = load_config()
    return config.get('api_key')

def load_openai_key():
    config = load_config()
    return config.get('openai_api_key')


def load_phantombuster_key():
    config = load_config()
    return config.get('phantombuster_api_key')


@st.cache_data(ttl=300)  # Cache for 5 minutes
def fetch_phantombuster_agents(api_key: str) -> list[dict]:
    """Fetch list of all PhantomBuster agents."""
    try:
        response = requests.get(
            'https://api.phantombuster.com/api/v2/agents/fetch-all',
            headers={'X-Phantombuster-Key': api_key},
            timeout=30
        )
        if response.status_code == 200:
            agents = response.json()
            # Return list of {id, name} for dropdown
            return [{'id': a['id'], 'name': a['name']} for a in agents]
        return []
    except Exception as e:
        return []


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


def fetch_phantombuster_result_csv(api_key: str, agent_id: str) -> pd.DataFrame:
    """Fetch results from PhantomBuster agent using the result object API."""
    try:
        # Fetch containers for this agent
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

        if not containers:
            st.error("No runs found for this agent. Run the agent first.")
            return pd.DataFrame()

        # Get the most recent finished container
        finished_containers = [c for c in containers if c.get('status') == 'finished']
        if not finished_containers:
            st.error("No completed runs found. Wait for the agent to finish.")
            return pd.DataFrame()

        container_id = finished_containers[0]['id']

        # Fetch the result object using the container ID
        result_response = requests.get(
            'https://api.phantombuster.com/api/v2/containers/fetch-result-object',
            params={'id': container_id},
            headers={'X-Phantombuster-Key': api_key},
            timeout=60
        )

        if result_response.status_code != 200:
            st.error(f"Failed to fetch results: {result_response.status_code}")
            return pd.DataFrame()

        result_data = result_response.json()
        result_object = result_data.get('resultObject')

        if not result_object:
            st.error("No result object found in the response")
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


def extract_urls_from_phantombuster(df: pd.DataFrame) -> list[str]:
    """Extract LinkedIn URLs from PhantomBuster results."""
    urls = []
    # Try different column names PhantomBuster might use
    url_columns = ['linkedInProfileUrl', 'defaultProfileUrl', 'url', 'profileUrl', 'LinkedIn Sales Navigator profile URL']

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
    creds_file = config.get('google_credentials_file')
    if not creds_file:
        return None

    creds_path = Path(__file__).parent / creds_file
    if not creds_path.exists():
        return None

    scopes = [
        'https://www.googleapis.com/auth/spreadsheets.readonly',
        'https://www.googleapis.com/auth/drive.readonly'
    ]

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
        winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS)
        notification.notify(
            title=title,
            message=message,
            app_name="LinkedIn Enricher",
            timeout=10
        )
    except Exception:
        try:
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


def enrich_batch(urls: list[str], api_key: str) -> list[dict]:
    """Enrich a batch of URLs via Crust Data API."""
    batch_str = ','.join(urls)

    try:
        response = requests.get(
            'https://api.crustdata.com/screener/person/enrich',
            params={'linkedin_profile_url': batch_str},
            headers={'Authorization': f'Token {api_key}'},
            timeout=120
        )

        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                return data
            return [data]
        else:
            return [{'error': response.text, 'linkedin_url': u} for u in urls]

    except Exception as e:
        return [{'error': str(e), 'linkedin_url': u} for u in urls]


def flatten_for_csv(data: list[dict]) -> pd.DataFrame:
    """Flatten nested data for CSV export."""
    flat_records = []

    for record in data:
        flat = {}
        for key, value in record.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, (list, dict)):
                        flat[f"{key}_{sub_key}"] = json.dumps(sub_value)
                    else:
                        flat[f"{key}_{sub_key}"] = sub_value
            elif isinstance(value, list):
                flat[key] = json.dumps(value)
            else:
                flat[key] = value
        flat_records.append(flat)

    return pd.DataFrame(flat_records)


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

    # 4. Not relevant companies (past)
    if filters.get('not_relevant_past') and filters.get('not_relevant'):
        not_relevant = [c.lower().strip() for c in filters['not_relevant']]
        df['_past_not_relevant'] = df['past_positions'].apply(lambda x: matches_list_in_text(x, not_relevant))
        stats['not_relevant_past'] = df['_past_not_relevant'].sum()
        filtered_out['Not Relevant (Past)'] = df[df['_past_not_relevant']].drop(columns=['_past_not_relevant']).copy()
        df = df[~df['_past_not_relevant']].drop(columns=['_past_not_relevant'])

    # 5. Job hoppers filter - exempt priority candidates
    if filters.get('filter_job_hoppers'):
        def count_short_stints(past_positions):
            if pd.isna(past_positions) or not str(past_positions).strip():
                return 0
            years_pattern = r'\[(\d+\.?\d*)\s*yrs?\]'
            matches = re.findall(years_pattern, str(past_positions))
            return sum(1 for y in matches if float(y) < 1.0)

        df['_short_stints'] = df['past_positions'].apply(count_short_stints)
        df['_is_job_hopper'] = df['_short_stints'] >= 2
        stats['job_hoppers'] = df['_is_job_hopper'].sum()
        filtered_out['Job Hoppers'] = df[df['_is_job_hopper']].drop(columns=['_short_stints', '_is_job_hopper']).copy()
        df = df[~df['_is_job_hopper']].drop(columns=['_short_stints', '_is_job_hopper'])

    # 6. Consulting companies filter
    if filters.get('filter_consulting'):
        consulting = ['tikal', 'matrix', 'ness', 'sela', 'malam', 'bynet', 'sqlink', 'john bryce',
                      'experis', 'manpower', 'infosys', 'tata', 'wipro', 'cognizant', 'accenture', 'capgemini']
        df['_is_consulting'] = df['current_company'].apply(lambda x: matches_list(x, consulting))
        stats['consulting'] = df['_is_consulting'].sum()
        filtered_out['Consulting Companies'] = df[df['_is_consulting']].drop(columns=['_is_consulting']).copy()
        df = df[~df['_is_consulting']].drop(columns=['_is_consulting'])

    # 7. Long tenure filter
    if filters.get('filter_long_tenure'):
        df['_long_tenure'] = df['current_years_in_role'].apply(lambda x: x >= 8 if pd.notna(x) else False)
        stats['long_tenure'] = df['_long_tenure'].sum()
        filtered_out['Long Tenure (8+ years)'] = df[df['_long_tenure']].drop(columns=['_long_tenure']).copy()
        df = df[~df['_long_tenure']].drop(columns=['_long_tenure'])

    # 8. Management titles filter
    if filters.get('filter_management'):
        exclude_titles = ['director', 'head of', 'vp ', 'vice president', 'cto', 'ceo', 'coo',
                          'chief ', 'group manager', 'engineering manager', 'r&d manager', 'founder']
        keep_titles = ['team lead', 'tech lead', 'staff', 'principal', 'senior', 'architect']

        def is_management_title(title):
            if pd.isna(title) or not str(title).strip():
                return False
            title_lower = str(title).lower()
            for keep in keep_titles:
                if keep in title_lower:
                    return False
            for excl in exclude_titles:
                if excl in title_lower:
                    return True
            return False

        df['_is_management'] = df['current_title'].apply(is_management_title)
        stats['management_titles'] = df['_is_management'].sum()
        filtered_out['Management Titles'] = df[df['_is_management']].drop(columns=['_is_management']).copy()
        df = df[~df['_is_management']].drop(columns=['_is_management'])

    stats['original'] = original_count
    stats['final'] = len(df)
    stats['total_removed'] = original_count - len(df)

    return df, stats, filtered_out


def screen_profile(profile: dict, job_description: str, client: OpenAI) -> dict:
    """Screen a profile against a job description using OpenAI."""

    profile_text = json.dumps(profile, indent=2, ensure_ascii=False)

    prompt = f"""You are a recruiter screening candidates. Evaluate this LinkedIn profile against the job description.

JOB DESCRIPTION:
{job_description}

CANDIDATE PROFILE:
{profile_text}

Provide your assessment in this exact JSON format:
{{
    "score": <1-10 where 10 is perfect match>,
    "fit": "<Strong Fit / Good Fit / Partial Fit / Not a Fit>",
    "summary": "<2-3 sentence summary of the candidate>",
    "strengths": ["<strength 1>", "<strength 2>"],
    "gaps": ["<gap 1>", "<gap 2>"],
    "recommendation": "<Brief recommendation>"
}}

Return ONLY the JSON, no other text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )

        result = json.loads(response.choices[0].message.content)
        return result
    except Exception as e:
        return {
            "score": 0,
            "fit": "Error",
            "summary": f"Error screening: {str(e)}",
            "strengths": [],
            "gaps": [],
            "recommendation": "Could not screen"
        }


# Main UI
st.title("LinkedIn Profile Enricher")

# Check API keys
api_key = load_api_key()
has_crust_key = api_key and api_key != "YOUR_CRUSTDATA_API_KEY_HERE"

# Show data status in header
if 'results' in st.session_state and st.session_state['results']:
    st.info(f"📊 **{len(st.session_state['results'])}** profiles loaded")

# Create tabs
tab_upload, tab_filter, tab_results, tab_screening = st.tabs(["📤 Upload", "🔍 Filter", "📋 Results", "🤖 AI Screening"])

# ========== TAB 1: Upload ==========
with tab_upload:
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
                st.session_state['results'] = df_uploaded.to_dict('records')
                st.session_state['results_df'] = df_uploaded
                st.success(f"Loaded **{len(df_uploaded)}** profiles!")
        except Exception as e:
            st.error(f"Error: {e}")

    if 'results' in st.session_state and st.session_state['results']:
        df = st.session_state['results_df']
        display_cols = ['first_name', 'last_name', 'current_title', 'current_company', 'education', 'location']
        available_cols = [c for c in display_cols if c in df.columns]
        st.dataframe(df[available_cols].head(50) if available_cols else df.head(50), use_container_width=True, hide_index=True)
        st.caption(f"Showing first 50 of {len(df)} profiles")

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
            st.markdown("**Exclusion Filters:**")
            filter_not_relevant_past = st.checkbox("Also filter past positions for not relevant", value=True)
            filter_job_hoppers = st.checkbox("Filter job hoppers (2+ roles < 1 year)", value=True)
            filter_consulting = st.checkbox("Filter consulting/project companies", value=True)
            filter_long_tenure = st.checkbox("Filter 8+ years at one company", value=True)
            filter_management = st.checkbox("Filter Director/VP/Head titles", value=True)

        if st.button("Apply Filters", type="primary"):
            filters = {
                'filter_job_hoppers': filter_job_hoppers,
                'filter_consulting': filter_consulting,
                'filter_long_tenure': filter_long_tenure,
                'filter_management': filter_management,
                'not_relevant_past': filter_not_relevant_past,
            }

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
                if filter_sheets.get('universities'):
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

# ========== TAB 3: Results ==========
with tab_results:
    if 'filter_stats' not in st.session_state:
        st.info("Apply filters in the Filter tab first to see results.")
    elif 'passed_candidates_df' in st.session_state:
        passed_df = st.session_state['passed_candidates_df']
        stats = st.session_state['filter_stats']

        # Summary metrics
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Original", stats.get('original', 0))
        col2.metric("Removed", stats.get('total_removed', 0))
        col3.metric("Passed", stats.get('final', 0))
        col4.metric("Keep Rate", f"{round((stats.get('final', 0) / max(stats.get('original', 1), 1)) * 100)}%")

        st.divider()

        # Priority categories
        filter_sheets = get_filter_sheets_config()
        gspread_client = get_gspread_client()
        has_sheets = bool(filter_sheets.get('url')) and gspread_client is not None

        if has_sheets and st.button("Load Priority Categories", key="load_categories_results"):
            sheet_url = filter_sheets.get('url', '')

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
                    if c_norm and (company_norm == c_norm or (len(c_norm) >= 4 and len(company_norm) >= 4 and (company_norm.startswith(c_norm) or c_norm.startswith(company_norm)))):
                        return True
                return False

            def matches_list_in_text(text, items_list):
                if pd.isna(text) or not str(text).strip():
                    return False
                text_lower = str(text).lower()
                return any(str(item).lower().strip() in text_lower for item in items_list if len(str(item).strip()) >= 3)

            with st.spinner("Loading priority lists..."):
                if filter_sheets.get('target_companies'):
                    tc_df = load_sheet_as_df(sheet_url, filter_sheets['target_companies'])
                    if tc_df is not None:
                        target_companies = []
                        for col in tc_df.columns:
                            if 'company' in col.lower() or 'name' in col.lower():
                                target_companies.extend(tc_df[col].dropna().tolist())
                        target_list = [str(c).lower().strip() for c in target_companies if c]
                        passed_df['is_target_company'] = passed_df['current_company'].apply(lambda x: matches_list(x, target_list))
                        st.info(f"Target Companies: {passed_df['is_target_company'].sum()} matches")

                if filter_sheets.get('tech_alerts'):
                    ta_df = load_sheet_as_df(sheet_url, filter_sheets['tech_alerts'])
                    if ta_df is not None:
                        tech_alerts = []
                        for col in ta_df.columns:
                            if 'company' in col.lower() or 'name' in col.lower():
                                tech_alerts.extend(ta_df[col].dropna().tolist())
                        alerts_list = [str(c).lower().strip() for c in tech_alerts if c]
                        passed_df['is_layoff_company'] = passed_df['current_company'].apply(lambda x: matches_list(x, alerts_list))
                        st.info(f"Layoff Alerts: {passed_df['is_layoff_company'].sum()} matches")

                if filter_sheets.get('universities'):
                    uni_df = load_sheet_as_df(sheet_url, filter_sheets['universities'])
                    if uni_df is not None:
                        uni_list = uni_df.iloc[:, 0].dropna().tolist()
                        passed_df['is_top_university'] = passed_df['education'].apply(lambda x: matches_list_in_text(x, uni_list))
                        st.info(f"Top Universities: {passed_df['is_top_university'].sum()} matches")

            st.session_state['passed_candidates_df'] = passed_df
            st.rerun()

        # Filter checkboxes
        st.markdown("**Filter by category:**")
        fcol1, fcol2, fcol3, fcol4 = st.columns(4)
        show_all = fcol1.checkbox("All", value=True, key="res_filter_all")
        show_target = fcol2.checkbox(f"Target Co ({int(passed_df['is_target_company'].sum()) if 'is_target_company' in passed_df.columns else 0})", key="res_filter_target") if 'is_target_company' in passed_df.columns else False
        show_layoff = fcol3.checkbox(f"Layoffs ({int(passed_df['is_layoff_company'].sum()) if 'is_layoff_company' in passed_df.columns else 0})", key="res_filter_layoff") if 'is_layoff_company' in passed_df.columns else False
        show_uni = fcol4.checkbox(f"Top Uni ({int(passed_df['is_top_university'].sum()) if 'is_top_university' in passed_df.columns else 0})", key="res_filter_uni") if 'is_top_university' in passed_df.columns else False

        # Apply filter
        if show_all or (not show_target and not show_layoff and not show_uni):
            view_df = passed_df
        else:
            mask = pd.Series([False] * len(passed_df), index=passed_df.index)
            if show_target and 'is_target_company' in passed_df.columns:
                mask = mask | passed_df['is_target_company'].fillna(False)
            if show_layoff and 'is_layoff_company' in passed_df.columns:
                mask = mask | passed_df['is_layoff_company'].fillna(False)
            if show_uni and 'is_top_university' in passed_df.columns:
                mask = mask | passed_df['is_top_university'].fillna(False)
            view_df = passed_df[mask]

        st.success(f"**{len(view_df)}** candidates")

        display_cols = ['first_name', 'last_name', 'current_title', 'current_company', 'education', 'location', 'public_url']
        available_cols = [c for c in display_cols if c in view_df.columns]
        st.dataframe(view_df[available_cols], use_container_width=True, hide_index=True,
                    column_config={"public_url": st.column_config.LinkColumn("LinkedIn")})

        csv_data = view_df.to_csv(index=False)
        st.download_button("Download CSV", csv_data, "passed_candidates.csv", "text/csv")

# ========== TAB 4: AI Screening ==========
with tab_screening:
    openai_key = load_openai_key()
    if not openai_key:
        st.warning("OpenAI API key not configured. Add 'openai_api_key' to config.json")
    elif 'passed_candidates_df' not in st.session_state:
        st.info("Apply filters in the Filter tab first, then come back here to screen candidates.")
    else:
        results = st.session_state['passed_candidates_df'].to_dict('records')
        st.success(f"Ready to screen **{len(results)}** candidates")

        job_description = st.text_area("Paste Job Description", height=150, key="jd_screening")

        if job_description:
            screen_count = st.number_input("Number to screen", 1, len(results), min(5, len(results)), key="screen_count")

            if st.button("Screen Candidates", type="primary", key="start_screening"):
                client = OpenAI(api_key=openai_key)
                screening_results = []
                progress = st.progress(0)

                for i, profile in enumerate(results[:screen_count]):
                    screening = screen_profile(profile, job_description, client)
                    name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip() or f"Profile {i+1}"
                    screening_results.append({'name': name, 'linkedin_url': profile.get('public_url', ''), **screening})
                    progress.progress((i + 1) / screen_count)

                st.session_state['screening_results'] = screening_results
                st.rerun()

        # Show screening results
        if 'screening_results' in st.session_state:
            st.divider()
            st.subheader("Screening Results")
            sr = sorted(st.session_state['screening_results'], key=lambda x: x.get('score', 0), reverse=True)
            df_sr = pd.DataFrame([{'Name': r['name'], 'Score': f"{r.get('score',0)}/10", 'Fit': r.get('fit',''), 'Summary': r.get('summary','')[:80]} for r in sr])
            st.dataframe(df_sr, use_container_width=True)
            st.download_button("Download Results", pd.DataFrame(sr).to_csv(index=False), "screening_results.csv", "text/csv")

# ========== Enrich new profiles (optional) ==========
with st.expander("Enrich New Profiles (requires Crust Data API key)", expanded=False):
    if not has_crust_key:
        st.warning("Crust Data API key not configured. Add 'api_key' to config.json")
    else:
        st.success("Crust Data API key loaded")

        # Source selection tabs
        source_tab1, source_tab2 = st.tabs(["Upload File", "PhantomBuster"])

        urls_to_enrich = []

        with source_tab1:
            uploaded_file = st.file_uploader(
                "Upload CSV or JSON file with LinkedIn URLs",
                type=['csv', 'json'],
                key="enrich_upload"
            )

            if uploaded_file:
                urls = extract_urls(uploaded_file)
                if urls:
                    st.success(f"Found **{len(urls)}** LinkedIn URLs")
                    st.session_state['enrich_urls'] = urls
                    with st.expander("Preview URLs"):
                        for i, url in enumerate(urls[:10]):
                            st.text(f"{i+1}. {url}")
                        if len(urls) > 10:
                            st.text(f"... and {len(urls) - 10} more")
                else:
                    st.warning("No LinkedIn URLs found in file")

        with source_tab2:
            pb_key = load_phantombuster_key()
            has_pb_key = pb_key and pb_key != "YOUR_PHANTOMBUSTER_API_KEY_HERE"

            if not has_pb_key:
                st.warning("PhantomBuster API key not configured. Add 'phantombuster_api_key' to config.json")
            else:
                st.success("PhantomBuster API key loaded")

                # Fetch agents list
                agents = fetch_phantombuster_agents(pb_key)

                if agents:
                    # Create dropdown with agent names
                    agent_options = {a['name']: a['id'] for a in agents}
                    selected_agent = st.selectbox(
                        "Select Phantom",
                        options=list(agent_options.keys()),
                        index=0,
                        help="Select from your PhantomBuster agents"
                    )
                    agent_id = agent_options.get(selected_agent, '')
                else:
                    st.warning("No agents found or could not fetch agent list")
                    agent_id = st.text_input(
                        "PhantomBuster Agent ID",
                        placeholder="Enter your agent/phantom ID manually",
                    )

                if agent_id:
                    if st.button("Fetch from PhantomBuster", key="fetch_pb"):
                        with st.spinner("Fetching results from PhantomBuster..."):
                            pb_df = fetch_phantombuster_result_csv(pb_key, agent_id)

                            if not pb_df.empty:
                                st.success(f"Fetched **{len(pb_df)}** profiles from PhantomBuster")
                                st.session_state['pb_results'] = pb_df

                                # Show preview
                                preview_cols = ['fullName', 'title', 'companyName', 'location']
                                available = [c for c in preview_cols if c in pb_df.columns]
                                if available:
                                    st.dataframe(pb_df[available].head(10), use_container_width=True, hide_index=True)

                                # Extract URLs
                                urls = extract_urls_from_phantombuster(pb_df)
                                if urls:
                                    st.info(f"Found **{len(urls)}** LinkedIn URLs to enrich")
                                    st.session_state['enrich_urls'] = urls
                                else:
                                    st.warning("No LinkedIn URLs found in PhantomBuster results")
                            else:
                                st.error("No results found. Check agent ID and make sure agent has run.")

        # Enrichment controls (shared between both sources)
        st.divider()
        if 'enrich_urls' in st.session_state and st.session_state['enrich_urls']:
            urls = st.session_state['enrich_urls']
            st.info(f"**{len(urls)}** URLs ready for enrichment")

            col1, col2 = st.columns(2)
            with col1:
                max_profiles = st.number_input(
                    "Number of profiles to process",
                    min_value=1,
                    max_value=len(urls),
                    value=min(5, len(urls)),
                    help="Start with a few to test"
                )
            with col2:
                batch_size = st.slider("Batch size", min_value=1, max_value=25, value=10)

            if st.button("Start Enrichment", type="primary", key="start_enrich"):
                urls_to_process = urls[:max_profiles]
                results = []
                progress_bar = st.progress(0)
                status_text = st.empty()
                total_batches = (len(urls_to_process) + batch_size - 1) // batch_size

                for i in range(0, len(urls_to_process), batch_size):
                    batch = urls_to_process[i:i + batch_size]
                    batch_num = i // batch_size + 1
                    status_text.text(f"Processing batch {batch_num}/{total_batches}...")
                    batch_results = enrich_batch(batch, api_key)
                    results.extend(batch_results)
                    progress_bar.progress(min((i + batch_size) / len(urls_to_process), 1.0))
                    if i + batch_size < len(urls_to_process):
                        time.sleep(2)

                progress_bar.progress(1.0)
                status_text.text("Enrichment complete!")
                send_notification("Enrichment Complete", f"Processed {len(results)} profiles")
                st.session_state['results'] = results
                st.session_state['results_df'] = flatten_for_csv(results)
                # Clear enrich_urls
                del st.session_state['enrich_urls']
                st.rerun()
