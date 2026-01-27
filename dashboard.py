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
        # Play success sound (more noticeable)
        winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS)

        # Show Windows notification
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
        # Try common column names
        for col in ['url', 'linkedin_url', 'profile_url', 'URL', 'LinkedIn URL', 'linkedinUrl', 'LinkedIn', 'linkedin', 'public_url']:
            if col in df.columns:
                urls = df[col].dropna().tolist()
                break
        # If no matching column, try first column
        if not urls and len(df.columns) > 0:
            urls = df.iloc[:, 0].dropna().tolist()

    # Filter to only LinkedIn URLs and normalize them
    normalized = []
    for u in urls:
        if u and 'linkedin.com' in str(u):
            u = str(u).strip()
            # Add https:// if missing
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
    urls = extract_urls(uploaded_file)

    if urls:
        st.success(f"Found **{len(urls)}** LinkedIn URLs")

        # Preview
        with st.expander("Preview URLs"):
            for i, url in enumerate(urls[:20]):
                st.text(f"{i+1}. {url}")
            if len(urls) > 20:
                st.text(f"... and {len(urls) - 20} more")

        # Enrichment settings
        st.subheader("2. Settings")

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
        st.subheader("3. Run Enrichment")

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

# Display results
if 'results' in st.session_state and st.session_state['results']:
    st.subheader("4. Results")

    results = st.session_state['results']
    df = st.session_state['results_df']

    st.success(f"**{len(results)}** profiles enriched")

    # Preview results table
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
