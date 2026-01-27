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


def apply_pre_filters(df: pd.DataFrame, filters: dict) -> tuple[pd.DataFrame, dict]:
    """Apply pre-filters to candidates. Returns filtered df and stats."""
    stats = {}
    original_count = len(df)

    # Helper functions
    def matches_list(company, company_list):
        if pd.isna(company) or not str(company).strip():
            return False
        company_lower = str(company).lower().strip()
        for c in company_list:
            if c in company_lower or company_lower in c:
                return True
        return False

    def matches_list_in_text(text, company_list):
        if pd.isna(text) or not str(text).strip():
            return False
        text_lower = str(text).lower()
        for c in company_list:
            if c in text_lower:
                return True
        return False

    # 1. Past candidates filter
    if filters.get('past_candidates_df') is not None:
        past_df = filters['past_candidates_df']
        if 'Name' in past_df.columns:
            past_names = set(str(name).lower().strip() for name in past_df['Name'].dropna())
            df['_full_name'] = (df['first_name'].fillna('').str.lower().str.strip() + ' ' +
                               df['last_name'].fillna('').str.lower().str.strip())
            df['_is_past'] = df['_full_name'].isin(past_names)
            stats['past_candidates'] = df['_is_past'].sum()
            df = df[~df['_is_past']].drop(columns=['_is_past', '_full_name'])

    # 2. Blacklist filter
    if filters.get('blacklist'):
        blacklist = [c.lower().strip() for c in filters['blacklist']]
        df['_blacklisted'] = df['current_company'].apply(lambda x: matches_list(x, blacklist))
        stats['blacklist'] = df['_blacklisted'].sum()
        df = df[~df['_blacklisted']].drop(columns=['_blacklisted'])

    # 3. Not relevant companies (current)
    if filters.get('not_relevant'):
        not_relevant = [c.lower().strip() for c in filters['not_relevant']]
        df['_not_relevant'] = df['current_company'].apply(lambda x: matches_list(x, not_relevant))
        stats['not_relevant_current'] = df['_not_relevant'].sum()
        df = df[~df['_not_relevant']].drop(columns=['_not_relevant'])

    # 4. Not relevant companies (past)
    if filters.get('not_relevant_past') and filters.get('not_relevant'):
        not_relevant = [c.lower().strip() for c in filters['not_relevant']]
        df['_past_not_relevant'] = df['past_positions'].apply(lambda x: matches_list_in_text(x, not_relevant))
        stats['not_relevant_past'] = df['_past_not_relevant'].sum()
        df = df[~df['_past_not_relevant']].drop(columns=['_past_not_relevant'])

    # 5. Job hoppers filter
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
        df = df[~df['_is_job_hopper']].drop(columns=['_short_stints', '_is_job_hopper'])

    # 6. Consulting companies filter
    if filters.get('filter_consulting'):
        consulting = ['tikal', 'matrix', 'ness', 'sela', 'malam', 'bynet', 'sqlink', 'john bryce',
                      'experis', 'manpower', 'infosys', 'tata', 'wipro', 'cognizant', 'accenture', 'capgemini']
        df['_is_consulting'] = df['current_company'].apply(lambda x: matches_list(x, consulting))
        stats['consulting'] = df['_is_consulting'].sum()
        df = df[~df['_is_consulting']].drop(columns=['_is_consulting'])

    # 7. Long tenure filter
    if filters.get('filter_long_tenure'):
        df['_long_tenure'] = df['current_years_in_role'].apply(lambda x: x >= 8 if pd.notna(x) else False)
        stats['long_tenure'] = df['_long_tenure'].sum()
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
        df = df[~df['_is_management']].drop(columns=['_is_management'])

    stats['original'] = original_count
    stats['final'] = len(df)
    stats['total_removed'] = original_count - len(df)

    return df, stats


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
st.markdown("Upload pre-enriched data for AI screening, or enrich new profiles")

# Check API keys
api_key = load_api_key()
has_crust_key = api_key and api_key != "YOUR_CRUSTDATA_API_KEY_HERE"

# ========== SECTION 1: Upload pre-enriched data ==========
st.subheader("1. Upload Pre-Enriched Data (for AI Screening)")
st.markdown("Already have enriched LinkedIn data? Upload it here.")

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
            else:
                st.error("JSON must be a list of profiles")
                pre_enriched_data = []
        else:
            df_uploaded = pd.read_csv(pre_enriched_file)
            pre_enriched_data = df_uploaded.to_dict('records')
            st.session_state['results'] = pre_enriched_data
            st.session_state['results_df'] = df_uploaded

        if pre_enriched_data:
            st.success(f"Loaded **{len(pre_enriched_data)}** profiles! Scroll down to AI Screening.")
    except Exception as e:
        st.error(f"Error loading file: {e}")

st.divider()

# ========== SECTION 2: Pre-Filter Candidates ==========
st.subheader("2. Pre-Filter Candidates")
st.markdown("Apply filters to remove irrelevant candidates before AI screening")

if 'results' in st.session_state and st.session_state['results']:
    # Check if data needs column filtering
    df = st.session_state['results_df']
    needs_filtering = 'job_1_job_title' in df.columns and 'current_title' not in df.columns

    if needs_filtering:
        if st.button("Convert to Screening Format"):
            with st.spinner("Converting columns..."):
                filtered_df = filter_csv_columns(df)
                st.session_state['results_df'] = filtered_df
                st.session_state['results'] = filtered_df.to_dict('records')
                st.success(f"Converted to {len(filtered_df.columns)} screening columns")
                st.rerun()

    # Pre-filter options
    with st.expander("Filter Options", expanded=True):
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Upload Filter Files:**")
            past_candidates_file = st.file_uploader("Past Candidates CSV", type=['csv'], key="past_candidates")
            blacklist_file = st.file_uploader("Blacklist Companies CSV", type=['csv'], key="blacklist")
            not_relevant_file = st.file_uploader("Not Relevant Companies CSV", type=['csv'], key="not_relevant")

        with col2:
            st.markdown("**Additional Filters:**")
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

            # Load filter files
            if past_candidates_file:
                filters['past_candidates_df'] = pd.read_csv(past_candidates_file)

            if blacklist_file:
                bl_df = pd.read_csv(blacklist_file)
                filters['blacklist'] = bl_df.iloc[:, 0].dropna().tolist()

            if not_relevant_file:
                nr_df = pd.read_csv(not_relevant_file)
                filters['not_relevant'] = nr_df.iloc[:, 0].dropna().tolist()

            # Apply filters
            with st.spinner("Applying filters..."):
                df = st.session_state['results_df']
                filtered_df, stats = apply_pre_filters(df, filters)

                st.session_state['results_df'] = filtered_df
                st.session_state['results'] = filtered_df.to_dict('records')
                st.session_state['filter_stats'] = stats

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
            for key, value in stats.items():
                if key not in ['original', 'final', 'total_removed'] and value > 0:
                    st.text(f"{key.replace('_', ' ').title()}: {value} removed")

else:
    st.info("Upload data above first to enable filtering.")

st.divider()

# ========== SECTION 3: Enrich new profiles (optional) ==========
with st.expander("Enrich New Profiles (requires Crust Data API key)", expanded=False):
    if not has_crust_key:
        st.warning("Crust Data API key not configured. Add 'api_key' to config.json")
    else:
        st.success("Crust Data API key loaded")

        uploaded_file = st.file_uploader(
            "Upload CSV or JSON file with LinkedIn URLs",
            type=['csv', 'json'],
            key="enrich_upload"
        )

        if uploaded_file:
            urls = extract_urls(uploaded_file)

            if urls:
                st.success(f"Found **{len(urls)}** LinkedIn URLs")

                with st.expander("Preview URLs"):
                    for i, url in enumerate(urls[:20]):
                        st.text(f"{i+1}. {url}")
                    if len(urls) > 20:
                        st.text(f"... and {len(urls) - 20} more")

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

                if st.button("Start Enrichment", type="primary"):
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
            else:
                st.warning("No LinkedIn URLs found in file")

st.divider()

# ========== SECTION 4: View loaded data ==========
if 'results' in st.session_state and st.session_state['results']:
    st.subheader("3. Loaded Data")
    results = st.session_state['results']
    df = st.session_state['results_df']
    st.success(f"**{len(results)}** profiles loaded")
    st.dataframe(df.head(20), use_container_width=True)

st.divider()

# ========== SECTION 5: AI Screening ==========
st.subheader("4. AI Screening")

openai_key = load_openai_key()
if not openai_key:
    st.warning("OpenAI API key not configured. Add 'openai_api_key' to config.json")
else:
    st.success("OpenAI API key loaded")

    if 'results' not in st.session_state or not st.session_state['results']:
        st.info("Upload data above first to enable screening.")
    else:
        results = st.session_state['results']

        job_description = st.text_area(
            "Paste Job Description",
            height=200,
            placeholder="Paste the full job description here..."
        )

        if job_description:
            screen_count = st.number_input(
                "Number of profiles to screen",
                min_value=1,
                max_value=len(results),
                value=min(5, len(results)),
                help="Start with a few to test"
            )

            if st.button("Screen Candidates", type="primary"):
                client = OpenAI(api_key=openai_key)
                screening_results = []
                progress_bar = st.progress(0)
                status_text = st.empty()

                for i, profile in enumerate(results[:screen_count]):
                    status_text.text(f"Screening profile {i+1}/{screen_count}...")
                    screening = screen_profile(profile, job_description, client)

                    # Get name from profile
                    first = profile.get('first_name', '')
                    last = profile.get('last_name', '')
                    if first or last:
                        name = f"{first} {last}".strip()
                    else:
                        name = profile.get('full_name') or profile.get('name') or f"Profile {i+1}"
                    linkedin_url = profile.get('public_url') or profile.get('linkedin_url') or profile.get('linkedin_profile_url') or ''

                    screening_results.append({
                        'name': name,
                        'linkedin_url': linkedin_url,
                        **screening
                    })

                    progress_bar.progress((i + 1) / screen_count)
                    time.sleep(0.5)

                progress_bar.progress(1.0)
                status_text.text("Screening complete!")
                send_notification("Screening Complete", f"Screened {len(screening_results)} candidates")
                st.session_state['screening_results'] = screening_results

# ========== SECTION 6: Screening Results ==========
if 'screening_results' in st.session_state and st.session_state['screening_results']:
    st.divider()
    st.subheader("5. Screening Results")

    screening_results = st.session_state['screening_results']
    screening_results_sorted = sorted(screening_results, key=lambda x: x.get('score', 0), reverse=True)

    # Filter options
    st.markdown("**Filter by Fit:**")
    filter_cols = st.columns(4)
    with filter_cols[0]:
        show_strong = st.checkbox("Strong Fit", value=True)
    with filter_cols[1]:
        show_good = st.checkbox("Good Fit", value=True)
    with filter_cols[2]:
        show_partial = st.checkbox("Partial Fit", value=True)
    with filter_cols[3]:
        show_not = st.checkbox("Not a Fit", value=True)

    fit_filters = []
    if show_strong:
        fit_filters.append("Strong Fit")
    if show_good:
        fit_filters.append("Good Fit")
    if show_partial:
        fit_filters.append("Partial Fit")
    if show_not:
        fit_filters.append("Not a Fit")

    filtered_results = [r for r in screening_results_sorted if r.get('fit', '') in fit_filters]
    st.markdown(f"Showing **{len(filtered_results)}** of {len(screening_results_sorted)} candidates")

    # Summary table
    summary_data = []
    for r in filtered_results:
        summary_data.append({
            'Name': r.get('name', ''),
            'Score': f"{r.get('score', 0)}/10",
            'Fit': r.get('fit', ''),
            'Summary': r.get('summary', '')[:100] + '...' if len(r.get('summary', '')) > 100 else r.get('summary', ''),
            'LinkedIn': r.get('linkedin_url', '')
        })

    st.dataframe(pd.DataFrame(summary_data), use_container_width=True)

    # Detailed view
    with st.expander("View Detailed Results"):
        for r in filtered_results:
            score = r.get('score', 0)
            color = "🟢" if score >= 7 else "🟡" if score >= 5 else "🔴"

            st.markdown(f"### {color} {r.get('name', 'Unknown')} - {r.get('score', 0)}/10 ({r.get('fit', '')})")
            st.markdown(f"**Summary:** {r.get('summary', '')}")

            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**Strengths:**")
                for s in r.get('strengths', []):
                    st.markdown(f"- {s}")
            with col2:
                st.markdown("**Gaps:**")
                for g in r.get('gaps', []):
                    st.markdown(f"- {g}")

            st.markdown(f"**Recommendation:** {r.get('recommendation', '')}")
            st.markdown(f"[LinkedIn Profile]({r.get('linkedin_url', '')})")
            st.divider()

    # Download buttons
    st.markdown("**Download Results:**")
    col1, col2 = st.columns(2)
    with col1:
        screening_df = pd.DataFrame(screening_results_sorted)
        csv_data = screening_df.to_csv(index=False)
        st.download_button(
            label="Download CSV",
            data=csv_data,
            file_name=f"screening_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )
    with col2:
        json_data = json.dumps(screening_results_sorted, indent=2, ensure_ascii=False)
        st.download_button(
            label="Download JSON",
            data=json_data,
            file_name=f"screening_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            mime="application/json"
        )
