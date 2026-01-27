"""
LinkedIn Profile Enricher Dashboard
Run with: streamlit run dashboard.py
"""

import streamlit as st
import pandas as pd
import json
import time
import os
import io
import requests
import platform
from pathlib import Path
from datetime import datetime
from openai import OpenAI

# Windows-specific imports (optional)
try:
    import winsound
    WINDOWS = True
except ImportError:
    WINDOWS = False

try:
    from plyer import notification
    HAS_PLYER = True
except ImportError:
    HAS_PLYER = False


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
    # Streamlit secrets > environment variable > config file
    try:
        if hasattr(st, 'secrets') and 'CRUSTDATA_API_KEY' in st.secrets:
            return st.secrets['CRUSTDATA_API_KEY']
    except Exception:
        pass
    return os.environ.get('CRUSTDATA_API_KEY') or load_config().get('api_key')

def load_openai_key():
    # Streamlit secrets > environment variable > config file
    try:
        if hasattr(st, 'secrets') and 'OPENAI_API_KEY' in st.secrets:
            return st.secrets['OPENAI_API_KEY']
    except Exception:
        pass
    return os.environ.get('OPENAI_API_KEY') or load_config().get('openai_api_key')


# Google Sheet URL for target companies (companies with layoffs to recruit from)
TARGET_COMPANIES_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSwZW1szbtKb7yYRU5hVE6FdMchLkRNd_jRff2eyYSSpD4R1V0USYsK_6uEBQLzlOdvUFMucJSh2bsv/pub?output=csv"


@st.cache_data(ttl=300)  # Cache for 5 minutes
def load_target_companies() -> set:
    """Load target companies from Google Sheet."""
    try:
        response = requests.get(TARGET_COMPANIES_SHEET_URL, timeout=10)
        if response.status_code == 200:
            import io
            df = pd.read_csv(io.StringIO(response.text), encoding='utf-8')
            companies = set()
            # Get all company names from both columns
            for col in df.columns:
                companies.update(df[col].dropna().str.strip().str.lower().tolist())
            # Remove empty strings
            companies.discard('')
            return companies
    except Exception as e:
        st.warning(f"Could not load target companies from Google Sheet: {e}")
    return set()


def send_notification(title, message):
    """Send desktop notification with sound (Windows only)."""
    # Skip notifications on non-Windows (cloud deployment)
    if not WINDOWS:
        return

    try:
        # Play success sound (more noticeable)
        winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS)

        # Show Windows notification
        if HAS_PLYER:
            notification.notify(
                title=title,
                message=message,
                app_name="LinkedIn Enricher",
                timeout=10
            )
    except Exception as e:
        # Fallback: try just the beep
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except:
            pass


def extract_profiles(uploaded_file, full_data=False) -> list[dict]:
    """Extract LinkedIn profiles from uploaded file.

    If full_data=True, preserve all original fields for screening.
    """
    profiles = []

    if uploaded_file.name.endswith('.json'):
        data = json.load(uploaded_file)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    url = (item.get('url') or item.get('linkedin_url') or
                           item.get('profile_url') or item.get('linkedinUrl') or '')
                    if url and 'linkedin.com' in str(url):
                        url = normalize_url(url)
                        if full_data:
                            # Keep all original data, just normalize URL
                            profile = dict(item)
                            profile['linkedin_url'] = url
                            profiles.append(profile)
                        else:
                            profiles.append({
                                'url': url,
                                'name': item.get('name') or item.get('full_name') or item.get('fullName') or '',
                                'title': item.get('title') or item.get('headline') or item.get('jobTitle') or '',
                                'company': item.get('company') or item.get('company_name') or item.get('companyName') or '',
                                'university': item.get('university') or item.get('school') or item.get('education') or ''
                            })
                elif isinstance(item, str) and 'linkedin.com' in item:
                    profiles.append({'url': normalize_url(item), 'linkedin_url': normalize_url(item), 'name': '', 'title': '', 'company': '', 'university': ''})

    elif uploaded_file.name.endswith('.csv'):
        df = pd.read_csv(uploaded_file)

        # Find URL column
        url_col = None
        for col in ['url', 'linkedin_url', 'profile_url', 'URL', 'LinkedIn URL', 'linkedinUrl', 'LinkedIn', 'linkedin', 'profileUrl']:
            if col in df.columns:
                url_col = col
                break
        if not url_col and len(df.columns) > 0:
            url_col = df.columns[0]

        # Find other columns
        def find_col(options):
            for opt in options:
                for col in df.columns:
                    if col.lower() == opt.lower():
                        return col
            return None

        name_col = find_col(['name', 'full_name', 'fullName', 'full name', 'Full Name'])
        first_name_col = find_col(['firstName', 'first_name', 'first name', 'First Name', 'firstname'])
        last_name_col = find_col(['lastName', 'last_name', 'last name', 'Last Name', 'lastname'])
        title_col = find_col(['title', 'headline', 'jobTitle', 'job_title', 'position'])
        company_col = find_col(['company', 'company_name', 'companyName', 'organization', 'employer'])
        university_col = find_col(['university', 'school', 'education', 'college'])

        for _, row in df.iterrows():
            url = str(row.get(url_col, '')) if url_col else ''
            if url and 'linkedin.com' in url:
                # Build name from full name or first + last
                name = ''
                if name_col and pd.notna(row.get(name_col)):
                    name = str(row.get(name_col))
                elif first_name_col or last_name_col:
                    first = str(row.get(first_name_col, '')) if first_name_col and pd.notna(row.get(first_name_col)) else ''
                    last = str(row.get(last_name_col, '')) if last_name_col and pd.notna(row.get(last_name_col)) else ''
                    name = f"{first} {last}".strip()

                if full_data:
                    # Keep all columns for screening
                    profile = {col: (str(row[col]) if pd.notna(row[col]) else '') for col in df.columns}
                    profile['linkedin_url'] = normalize_url(url)
                    profile['name'] = name
                    profile['full_name'] = name
                    profiles.append(profile)
                else:
                    profiles.append({
                        'url': normalize_url(url),
                        'name': name,
                        'title': str(row.get(title_col, '')) if title_col and pd.notna(row.get(title_col)) else '',
                        'company': str(row.get(company_col, '')) if company_col and pd.notna(row.get(company_col)) else '',
                        'university': str(row.get(university_col, '')) if university_col and pd.notna(row.get(university_col)) else ''
                    })

    return profiles


def normalize_url(url: str) -> str:
    """Normalize LinkedIn URL."""
    url = str(url).strip()
    if url.startswith('www.'):
        url = 'https://' + url
    elif not url.startswith('http'):
        url = 'https://' + url
    return url


def extract_urls(uploaded_file) -> list[str]:
    """Extract LinkedIn URLs from uploaded file (legacy wrapper)."""
    profiles = extract_profiles(uploaded_file)
    return [p['url'] for p in profiles]


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


def screen_profile(profile: dict, job_description: str, client: OpenAI) -> dict:
    """Screen a profile against a job description using OpenAI."""

    # Build profile summary for the AI
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
st.title("🔍 LinkedIn Profile Enricher")
st.markdown("Upload your LinkedIn URLs and enrich them with Crust Data API")

# Check API key
api_key = load_api_key()
if not api_key or api_key == "YOUR_CRUSTDATA_API_KEY_HERE":
    st.error("⚠️ API key not configured. Please edit config.json with your Crust Data API key.")
    st.stop()

st.success("✅ API key loaded")

# File upload
st.subheader("1. Upload URLs")
uploaded_file = st.file_uploader(
    "Upload CSV or JSON file with LinkedIn URLs",
    type=['csv', 'json']
)

if uploaded_file:
    uploaded_profiles = extract_profiles(uploaded_file)
    urls = [p['url'] for p in uploaded_profiles]

    if urls:
        st.success(f"Found **{len(urls)}** LinkedIn URLs")

        # LinkedIn logo URL
        LINKEDIN_LOGO = "https://cdn-icons-png.flaticon.com/512/174/174857.png"

        # Profile preview table
        st.markdown("### Uploaded Profiles Preview")

        # Header row
        header_cols = st.columns([3, 3, 3, 3, 1])
        with header_cols[0]:
            st.markdown("**Name**")
        with header_cols[1]:
            st.markdown("**Title**")
        with header_cols[2]:
            st.markdown("**Company**")
        with header_cols[3]:
            st.markdown("**University**")
        with header_cols[4]:
            st.markdown("**Link**")

        st.divider()

        for i, profile in enumerate(uploaded_profiles[:20]):
            col1, col2, col3, col4, col5 = st.columns([3, 3, 3, 3, 1])

            with col1:
                st.markdown(f"**{profile['name']}**" if profile['name'] else "-")
            with col2:
                st.markdown(profile['title'] if profile['title'] else "-")
            with col3:
                st.markdown(profile['company'] if profile['company'] else "-")
            with col4:
                st.markdown(profile['university'] if profile['university'] else "-")
            with col5:
                if profile['url']:
                    st.markdown(f'<a href="{profile["url"]}" target="_blank"><img src="{LINKEDIN_LOGO}" width="24"></a>', unsafe_allow_html=True)

        if len(uploaded_profiles) > 20:
            st.caption(f"... and {len(uploaded_profiles) - 20} more profiles")

        # Store uploaded profiles in session for filtering
        st.session_state['uploaded_profiles'] = uploaded_profiles

        # Mode selection
        st.subheader("2. Choose Mode")

        mode = st.radio(
            "What would you like to do?",
            ["Enrich profiles (fetch full data from API)", "Skip to screening (data already complete)"],
            horizontal=True
        )

        if mode == "Skip to screening (data already complete)":
            # Filter section
            st.subheader("3. Filter Profiles")

            # Load target companies from Google Sheet
            target_companies = load_target_companies()
            if target_companies:
                st.success(f"Loaded **{len(target_companies)}** target companies from Google Sheet")

            # Target companies filter (quick filter)
            use_target_filter = st.checkbox(
                "Only show profiles from Target Companies (Google Sheet)",
                value=False,
                help="Filter profiles to only show those from companies in your Google Sheet"
            )

            # Get unique values for filters
            all_companies = sorted(set(p['company'] for p in uploaded_profiles if p.get('company')))
            all_titles = sorted(set(p['title'] for p in uploaded_profiles if p.get('title')))
            all_universities = sorted(set(p['university'] for p in uploaded_profiles if p.get('university')))

            filter_col1, filter_col2, filter_col3 = st.columns(3)

            with filter_col1:
                selected_companies = st.multiselect("Filter by Company", all_companies, default=[])
            with filter_col2:
                selected_titles = st.multiselect("Filter by Title (contains)", all_titles, default=[])
            with filter_col3:
                selected_universities = st.multiselect("Filter by University", all_universities, default=[])

            # Apply filters
            filtered_profiles = uploaded_profiles.copy()

            # Target companies filter (from Google Sheet)
            if use_target_filter and target_companies:
                filtered_profiles = [
                    p for p in filtered_profiles
                    if p.get('company', '').strip().lower() in target_companies
                ]

            if selected_companies:
                filtered_profiles = [p for p in filtered_profiles if p.get('company') in selected_companies]
            if selected_titles:
                filtered_profiles = [p for p in filtered_profiles if any(t.lower() in p.get('title', '').lower() for t in selected_titles)]
            if selected_universities:
                filtered_profiles = [p for p in filtered_profiles if p.get('university') in selected_universities]

            # Show match info
            if use_target_filter and target_companies:
                st.info(f"**{len(filtered_profiles)}** profiles from target companies (out of {len(uploaded_profiles)} total)")
            else:
                st.info(f"**{len(filtered_profiles)}** profiles match your filters (out of {len(uploaded_profiles)} total)")

            if st.button("📋 Use These Profiles for Screening", type="primary"):
                # Re-read the file with full_data=True to get all fields
                uploaded_file.seek(0)
                full_profiles = extract_profiles(uploaded_file, full_data=True)

                # Filter full profiles based on URLs of filtered preview profiles
                filtered_urls = {p['url'] for p in filtered_profiles}
                results = [p for p in full_profiles if p.get('linkedin_url') in filtered_urls or p.get('url') in filtered_urls]

                st.session_state['results'] = results
                st.session_state['results_df'] = flatten_for_csv(results)
                st.success(f"Loaded **{len(results)}** profiles for screening!")
                st.rerun()

        else:
            # Enrichment settings
            st.subheader("3. Settings")

        col1, col2 = st.columns(2)
        with col1:
            max_profiles = st.number_input(
                "Number of profiles to process",
                min_value=1,
                max_value=len(urls),
                value=min(5, len(urls)),
                help="Start with a few to test, then increase"
            )
        with col2:
            batch_size = st.slider("Batch size", min_value=1, max_value=25, value=10,
                                       help="Number of profiles per API call")

            # Run enrichment
            st.subheader("4. Run Enrichment")

        if st.button("🚀 Start Enrichment", type="primary"):
            # Limit to selected number of profiles
            urls_to_process = urls[:max_profiles]

            results = []
            progress_bar = st.progress(0)
            status_text = st.empty()

            total_batches = (len(urls_to_process) + batch_size - 1) // batch_size

            for i in range(0, len(urls_to_process), batch_size):
                batch = urls_to_process[i:i + batch_size]
                batch_num = i // batch_size + 1

                status_text.text(f"Processing batch {batch_num}/{total_batches} ({len(batch)} profiles)...")

                batch_results = enrich_batch(batch, api_key)
                results.extend(batch_results)

                progress_bar.progress(min((i + batch_size) / len(urls_to_process), 1.0))

                # Rate limiting
                if i + batch_size < len(urls_to_process):
                    time.sleep(2)

            progress_bar.progress(1.0)
            status_text.text("✅ Enrichment complete!")

            # Send notification
            send_notification(
                "Enrichment Complete",
                f"Successfully processed {len(results)} LinkedIn profiles"
            )

            # Store results in session
            st.session_state['results'] = results
            st.session_state['results_df'] = flatten_for_csv(results)

st.divider()

# Display results
if 'results' in st.session_state and st.session_state['results']:
    st.subheader("4. Results")

    results = st.session_state['results']
    df = st.session_state['results_df']

    st.success(f"**{len(results)}** profiles enriched")

    # LinkedIn logo URL
    LINKEDIN_LOGO = "https://cdn-icons-png.flaticon.com/512/174/174857.png"

    # Profile cards preview
    st.markdown("### Profiles Preview")

    # Header row
    header_cols = st.columns([3, 3, 3, 3, 1])
    with header_cols[0]:
        st.markdown("**Name**")
    with header_cols[1]:
        st.markdown("**Title**")
    with header_cols[2]:
        st.markdown("**Company**")
    with header_cols[3]:
        st.markdown("**University**")
    with header_cols[4]:
        st.markdown("**Link**")

    st.divider()

    for i, profile in enumerate(results[:20]):
        name = profile.get('full_name') or profile.get('name') or 'Unknown'
        title = profile.get('title') or profile.get('headline') or ''
        company = profile.get('company') or profile.get('current_company') or profile.get('company_name') or ''

        # Try to get university from education
        education = profile.get('education') or profile.get('educations') or []
        university = ''
        if isinstance(education, list) and len(education) > 0:
            if isinstance(education[0], dict):
                university = education[0].get('school') or education[0].get('school_name') or ''
            elif isinstance(education[0], str):
                university = education[0]
        elif isinstance(education, str):
            university = education

        linkedin_url = profile.get('linkedin_url') or profile.get('linkedin_profile_url') or ''

        # Create card layout
        col1, col2, col3, col4, col5 = st.columns([3, 3, 3, 3, 1])

        with col1:
            st.markdown(f"**{name}**")
        with col2:
            st.markdown(f"{title}" if title else "-")
        with col3:
            st.markdown(f"{company}" if company else "-")
        with col4:
            st.markdown(f"{university}" if university else "-")
        with col5:
            if linkedin_url:
                st.markdown(f'<a href="{linkedin_url}" target="_blank"><img src="{LINKEDIN_LOGO}" width="24"></a>', unsafe_allow_html=True)

    if len(results) > 20:
        st.caption(f"... and {len(results) - 20} more profiles")

    st.divider()

    # Full data table
    with st.expander("View Full Data Table"):
        st.dataframe(df.head(50), use_container_width=True)

    # Download buttons
    st.subheader("5. Download")

    col1, col2 = st.columns(2)

    with col1:
        csv_data = df.to_csv(index=False)
        st.download_button(
            label="📥 Download CSV",
            data=csv_data,
            file_name=f"enriched_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )

    with col2:
        json_data = json.dumps(results, indent=2, ensure_ascii=False)
        st.download_button(
            label="📥 Download JSON",
            data=json_data,
            file_name=f"enriched_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            mime="application/json"
        )

    # AI Screening Section
    st.divider()
    st.subheader("6. AI Screening")

    openai_key = load_openai_key()
    if not openai_key:
        st.warning("⚠️ OpenAI API key not configured. Add 'openai_api_key' to config.json to enable AI screening.")
    else:
        st.success("✅ OpenAI API key loaded")

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

            if st.button("🤖 Screen Candidates", type="primary"):
                client = OpenAI(api_key=openai_key)
                screening_results = []

                progress_bar = st.progress(0)
                status_text = st.empty()

                for i, profile in enumerate(results[:screen_count]):
                    status_text.text(f"Screening profile {i+1}/{screen_count}...")

                    screening = screen_profile(profile, job_description, client)

                    # Get name from profile
                    name = profile.get('full_name') or profile.get('name') or f"Profile {i+1}"
                    linkedin_url = profile.get('linkedin_url') or profile.get('linkedin_profile_url') or ''

                    screening_results.append({
                        'name': name,
                        'linkedin_url': linkedin_url,
                        **screening
                    })

                    progress_bar.progress((i + 1) / screen_count)
                    time.sleep(0.5)  # Rate limiting

                progress_bar.progress(1.0)
                status_text.text("✅ Screening complete!")

                # Send notification
                send_notification(
                    "Screening Complete",
                    f"Screened {len(screening_results)} candidates"
                )

                # Store screening results
                st.session_state['screening_results'] = screening_results

    # Display screening results
    if 'screening_results' in st.session_state and st.session_state['screening_results']:
        st.subheader("7. Screening Results")

        screening_results = st.session_state['screening_results']

        # Sort by score descending
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

        # Build filter list
        fit_filters = []
        if show_strong:
            fit_filters.append("Strong Fit")
        if show_good:
            fit_filters.append("Good Fit")
        if show_partial:
            fit_filters.append("Partial Fit")
        if show_not:
            fit_filters.append("Not a Fit")

        # Apply filter
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

        # Detailed view in expanders
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

        # Download screening results
        st.markdown("**Download Filtered Results:**")
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            screening_df = pd.DataFrame(filtered_results)
            csv_data = screening_df.to_csv(index=False)
            st.download_button(
                label="📥 Filtered CSV",
                data=csv_data,
                file_name=f"screening_filtered_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )
        with col2:
            json_data = json.dumps(filtered_results, indent=2, ensure_ascii=False)
            st.download_button(
                label="📥 Filtered JSON",
                data=json_data,
                file_name=f"screening_filtered_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json"
            )

        st.markdown("**Download All Results:**")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            all_df = pd.DataFrame(screening_results_sorted)
            csv_all = all_df.to_csv(index=False)
            st.download_button(
                label="📥 All CSV",
                data=csv_all,
                file_name=f"screening_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )
        with col2:
            json_all = json.dumps(screening_results_sorted, indent=2, ensure_ascii=False)
            st.download_button(
                label="📥 All JSON",
                data=json_all,
                file_name=f"screening_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json"
            )
