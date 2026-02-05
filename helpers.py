"""
Display helpers for extracting fields from Crustdata raw responses.

This is the ONE place where Crustdata field names are mapped to display fields.
If Crustdata changes their API, update only this file.
"""


def extract_display_fields(raw_data: dict) -> dict:
    """Extract display fields from Crustdata raw response.

    Args:
        raw_data: The raw Crustdata API response

    Returns:
        Dict with normalized display fields
    """
    cd = raw_data or {}

    # Current employer (first in list)
    current_employers = cd.get('current_employers') or []
    emp = current_employers[0] if current_employers else {}

    # Name
    name = cd.get('name', '')
    first_name = cd.get('first_name', '')
    last_name = cd.get('last_name', '')
    if not first_name and name:
        parts = name.split(' ', 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ''

    # Education - get first school
    all_schools = cd.get('all_schools') or []
    education_background = cd.get('education_background') or []
    education = all_schools[0] if all_schools else ''

    return {
        # Basic info
        'name': name,
        'first_name': first_name,
        'last_name': last_name,
        'headline': cd.get('headline', ''),
        'location': cd.get('location', ''),
        'summary': cd.get('summary', ''),

        # Current position
        'current_title': emp.get('employee_title') or emp.get('title') or '',
        'current_company': emp.get('employer_name') or emp.get('company_name') or '',

        # Lists for filtering
        'all_schools': all_schools,
        'all_employers': cd.get('all_employers') or [],
        'all_titles': cd.get('all_titles') or [],
        'past_employers': cd.get('past_employers') or [],
        'current_employers': current_employers,
        'education_background': education_background,
        'skills': cd.get('skills') or [],

        # Single values for display
        'education': education,
        'skills_str': ', '.join(cd.get('skills') or [])[:200],
        'connections': cd.get('num_of_connections') or cd.get('connections_count') or 0,
        'followers': cd.get('followers_count') or 0,
        'profile_picture': cd.get('profile_pic_url') or cd.get('profile_picture_url') or '',

        # For job hopping analysis
        'num_positions': len(cd.get('past_employers') or []) + len(current_employers),
        'num_employers': len(cd.get('all_employers') or []),
    }


def extract_for_screening(raw_data: dict) -> dict:
    """Extract fields needed for AI screening.

    Returns a clean dict to pass to the AI screener.
    """
    display = extract_display_fields(raw_data)
    cd = raw_data or {}

    return {
        'name': display['name'],
        'headline': display['headline'],
        'location': display['location'],
        'summary': display['summary'],
        'current_title': display['current_title'],
        'current_company': display['current_company'],
        'all_employers': display['all_employers'],
        'all_titles': display['all_titles'],
        'all_schools': display['all_schools'],
        'skills': display['skills'],
        'past_employers': display['past_employers'],
        'current_employers': display['current_employers'],
        'education_background': display['education_background'],
        'connections': display['connections'],
    }


def format_past_positions(positions: list) -> str:
    """Format positions list into a readable string for AI screening."""
    if not positions:
        return ''

    parts = []
    for pos in positions[:10]:  # Limit to 10 positions
        if isinstance(pos, dict):
            title = pos.get('employee_title') or pos.get('title') or ''
            company = pos.get('employer_name') or pos.get('company_name') or ''
            if title and company:
                parts.append(f"{title} at {company}")
            elif title or company:
                parts.append(title or company)

    return ' | '.join(parts)


def profile_to_display_row(profile: dict) -> dict:
    """Convert a DB profile record to a display row.

    Combines DB fields (screening results, status) with extracted raw_data fields.
    """
    raw = profile.get('raw_data') or {}
    display = extract_display_fields(raw)

    # Past positions as full JSON from Crustdata
    import json
    past_employers = display.get('past_employers') or []
    past_positions = json.dumps(past_employers, ensure_ascii=False) if past_employers else ''

    return {
        'linkedin_url': profile.get('linkedin_url', ''),
        'name': display['name'],
        'first_name': display['first_name'],
        'last_name': display['last_name'],
        'current_title': profile.get('current_title') or display['current_title'],
        'current_company': profile.get('current_company') or display['current_company'],
        'headline': display['headline'],
        'location': display['location'],
        'summary': display['summary'],
        'education': display['education'],
        'skills': display['skills_str'],
        'connections': display['connections'],
        'past_positions': past_positions,

        # From DB columns (screening results)
        'screening_score': profile.get('screening_score'),
        'screening_fit_level': profile.get('screening_fit_level'),
        'screening_summary': profile.get('screening_summary'),
        'screening_reasoning': profile.get('screening_reasoning'),

        # Status and email
        'status': profile.get('status'),
        'email': profile.get('email'),

        # Timestamps
        'enriched_at': profile.get('enriched_at'),
        'screened_at': profile.get('screened_at'),

        # For filtering (lists)
        'all_schools': display['all_schools'],
        'all_employers': display['all_employers'],
        'num_positions': display['num_positions'],
    }


def profiles_to_display_df(profiles: list) -> 'pd.DataFrame':
    """Convert list of DB profiles to display DataFrame."""
    import pandas as pd

    if not profiles:
        return pd.DataFrame()

    rows = [profile_to_display_row(p) for p in profiles]
    df = pd.DataFrame(rows)

    # Column order for display
    priority_cols = [
        'name', 'current_title', 'current_company', 'location',
        'screening_score', 'screening_fit_level', 'email',
        'education', 'connections', 'status', 'linkedin_url'
    ]

    existing = [c for c in priority_cols if c in df.columns]
    other = [c for c in df.columns if c not in priority_cols]

    return df[existing + other]
