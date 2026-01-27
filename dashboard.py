"""
LinkedIn Profile Enricher Dashboard
Run with: streamlit run dashboard.py
"""

import streamlit as st
import pandas as pd
import json
import time
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

# ========== SECTION 2: Enrich new profiles (optional) ==========
with st.expander("Or: Enrich New Profiles (requires Crust Data API key)", expanded=False):
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

# ========== SECTION 3: View loaded data ==========
if 'results' in st.session_state and st.session_state['results']:
    st.subheader("2. Loaded Data")
    results = st.session_state['results']
    df = st.session_state['results_df']
    st.success(f"**{len(results)}** profiles loaded")
    st.dataframe(df.head(20), use_container_width=True)

st.divider()

# ========== SECTION 4: AI Screening ==========
st.subheader("3. AI Screening")

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

# ========== SECTION 5: Screening Results ==========
if 'screening_results' in st.session_state and st.session_state['screening_results']:
    st.divider()
    st.subheader("4. Screening Results")

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
