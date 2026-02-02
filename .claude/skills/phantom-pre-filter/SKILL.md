---
name: phantom-pre-filter
description: Pre-filter raw PhantomBuster CSV before Crustdata enrichment. Saves API costs by filtering early.
argument-hint: [input-phantom-csv] [output-csv]
allowed-tools: Read, Bash(python:*), Write
---

# Pre-Filter PhantomBuster Data Before Enrichment

Filter raw PhantomBuster Sales Navigator export BEFORE sending to Crustdata enrichment to save API costs.

## Input

- **Phantom CSV**: `$ARGUMENTS[0]` - raw PhantomBuster export
- **Output CSV**: `$ARGUMENTS[1]` - filtered CSV ready for enrichment (default: `phantom_filtered.csv`)
- **Filter CSVs** (stored in Downloads folder):
  - `filters for Cloude Enricher - Blacklist.csv` - Companies to ALWAYS exclude
  - `filters for Cloude Enricher - NotRelevant Companies.csv` - Large corps/banks to exclude (optional)

## Available Phantom Columns

| Column | Description |
|--------|-------------|
| `firstName`, `lastName`, `fullName` | Name |
| `title` | Current job title (headline) |
| `companyName` | Current company |
| `location` | Location |
| `durationInRole` | Years in current role (e.g., "2 years 3 months") |
| `durationInCompany` | Years at current company (e.g., "5 years 1 month") |
| `industry` | Company industry |
| `connectionDegree` | 1st, 2nd, 3rd degree connection |
| `defaultProfileUrl` | LinkedIn profile URL |

**Note**: `pastExperience*` columns exist in headers but typically contain no data.

## Filtering Rules

### 1. BLACKLIST (Always Applied)
Exclude any candidate where `companyName` matches a blacklisted company.
- Match is case-insensitive and partial

### 2. NOT RELEVANT COMPANIES (Optional - Ask User)
Exclude candidates currently at large corporations, banks, consulting firms.
- Ask: "Filter out candidates at large corporations/banks/consulting?"

### 3. DURATION IN ROLE (Optional - Ask User)
Filter by time in current role using min/max range.
- Parses `durationInRole` field
- **Minimum**: Exclude candidates with less than X in current role (e.g., just started, not ready to move)
- **Maximum**: Exclude candidates with more than X in current role (e.g., stagnant, not progressing)
- Ask: "Duration in current role range? (e.g., min 6 months, max 5 years, or skip)"

### 4. DURATION AT COMPANY (Optional - Ask User)
Filter by time at current company using min/max range.
- Parses `durationInCompany` field
- **Minimum**: Exclude candidates with less than X at company (e.g., too new)
- **Maximum**: Exclude candidates with more than X at company (e.g., comfort zone, limited exposure)
- Ask: "Duration at current company range? (e.g., min 1 year, max 8 years, or skip)"

### 5. PROJECT/CONSULTING COMPANIES (Optional - Ask User)
Exclude candidates currently at consulting/outsourcing companies.
- Companies like: Tikal, Matrix, Ness, Sela, Malam Team, Bynet, etc.
- Ask: "Filter out candidates from consulting/project companies?"

### 6. NON-HANDS-ON TITLES (Optional - Ask User)
Exclude candidates with senior management titles that are typically not hands-on.
- Titles to exclude: Director, Head of, VP, Vice President, CTO, CEO, etc.
- Titles to KEEP: Team Lead, Tech Lead, Senior Engineer, Staff Engineer
- Ask: "Filter out Director/VP/Head level titles?"

## Output

- **Filtered CSV**: Ready for Crustdata enrichment
- **Summary**: Count of excluded candidates per rule
- **Columns preserved**: All original Phantom columns

## Python Implementation

```python
import pandas as pd
import re
import sys
sys.stdout.reconfigure(encoding='utf-8')

# Load Phantom CSV
df = pd.read_csv(input_file)
original_count = len(df)

# Helper: check if company matches any in list (case-insensitive, partial match)
def matches_list(company, company_list):
    if pd.isna(company) or not str(company).strip():
        return False
    company_lower = str(company).lower().strip()
    for c in company_list:
        if c in company_lower or company_lower in c:
            return True
    return False

# Helper: parse duration string to months
def parse_duration_to_months(duration_str):
    """Parse '2 years 3 months' or '6 months' to total months."""
    if pd.isna(duration_str) or not str(duration_str).strip():
        return None
    text = str(duration_str).lower()
    years = 0
    months = 0

    year_match = re.search(r'(\d+)\s*(?:year|yr)', text)
    if year_match:
        years = int(year_match.group(1))

    month_match = re.search(r'(\d+)\s*(?:month|mo)', text)
    if month_match:
        months = int(month_match.group(1))

    total = years * 12 + months
    return total if total > 0 else None

# 1. Load and apply blacklist (always)
blacklist_df = pd.read_csv(r"C:\Users\gehta\Downloads\filters for Cloude Enricher - Blacklist.csv")
blacklist = [c.lower().strip() for c in blacklist_df['Company'].dropna().tolist() if c.strip()]

df['_blacklisted'] = df['companyName'].apply(lambda x: matches_list(x, blacklist))
blacklist_count = df['_blacklisted'].sum()
df = df[~df['_blacklisted']].drop(columns=['_blacklisted'])
print(f"Blacklist: Removed {blacklist_count} candidates")

# 2. Optional: Not Relevant Companies
if apply_not_relevant:
    not_relevant_df = pd.read_csv(r"C:\Users\gehta\Downloads\filters for Cloude Enricher - NotRelevant Companies.csv")
    not_relevant = [c.lower().strip() for c in not_relevant_df['Company'].dropna().tolist() if c.strip()]
    df['_not_relevant'] = df['companyName'].apply(lambda x: matches_list(x, not_relevant))
    not_relevant_count = df['_not_relevant'].sum()
    df = df[~df['_not_relevant']].drop(columns=['_not_relevant'])
    print(f"Not Relevant Companies: Removed {not_relevant_count} candidates")

# 3. Optional: Duration in role (min/max range)
if min_role_months or max_role_months:
    df['_role_months'] = df['durationInRole'].apply(parse_duration_to_months)

    if min_role_months:
        df['_role_too_short'] = df['_role_months'].apply(lambda x: x < min_role_months if pd.notna(x) else False)
        short_count = df['_role_too_short'].sum()
        df = df[~df['_role_too_short']].drop(columns=['_role_too_short'])
        print(f"Role Duration (min): Removed {short_count} candidates with < {min_role_months} months in role")

    if max_role_months:
        df['_role_too_long'] = df['_role_months'].apply(lambda x: x > max_role_months if pd.notna(x) else False)
        long_count = df['_role_too_long'].sum()
        df = df[~df['_role_too_long']].drop(columns=['_role_too_long'])
        print(f"Role Duration (max): Removed {long_count} candidates with > {max_role_months} months in role")

    df = df.drop(columns=['_role_months'], errors='ignore')

# 4. Optional: Duration at company (min/max range)
if min_company_months or max_company_months:
    df['_company_months'] = df['durationInCompany'].apply(parse_duration_to_months)

    if min_company_months:
        df['_company_too_short'] = df['_company_months'].apply(lambda x: x < min_company_months if pd.notna(x) else False)
        short_count = df['_company_too_short'].sum()
        df = df[~df['_company_too_short']].drop(columns=['_company_too_short'])
        print(f"Company Duration (min): Removed {short_count} candidates with < {min_company_months} months at company")

    if max_company_months:
        df['_company_too_long'] = df['_company_months'].apply(lambda x: x > max_company_months if pd.notna(x) else False)
        long_count = df['_company_too_long'].sum()
        df = df[~df['_company_too_long']].drop(columns=['_company_too_long'])
        print(f"Company Duration (max): Removed {long_count} candidates with > {max_company_months} months at company")

    df = df.drop(columns=['_company_months'], errors='ignore')

# 5. Optional: Filter consulting/project companies
if filter_consulting:
    consulting_companies = [
        'tikal', 'matrix', 'ness', 'sela', 'malam', 'bynet', 'sqlink', 'john bryce',
        'experis', 'manpower', 'outsource', 'consulting', 'ness technologies',
        'matrix it', 'malam team', 'hpe', 'dxc', 'infosys', 'tata', 'wipro',
        'cognizant', 'accenture', 'capgemini', 'atos'
    ]
    df['_is_consulting'] = df['companyName'].apply(lambda x: matches_list(x, consulting_companies))
    consulting_count = df['_is_consulting'].sum()
    df = df[~df['_is_consulting']].drop(columns=['_is_consulting'])
    print(f"Consulting Companies: Removed {consulting_count} candidates")

# 6. Optional: Filter non-hands-on titles
if filter_management_titles:
    exclude_titles = [
        'director', 'head of', 'vp ', 'vice president', 'cto', 'ceo', 'coo', 'cfo',
        'chief ', 'group manager', 'engineering manager', 'r&d manager',
        'general manager', 'president', 'founder'
    ]
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

    df['_is_management'] = df['title'].apply(is_management_title)
    management_count = df['_is_management'].sum()
    df = df[~df['_is_management']].drop(columns=['_is_management'])
    print(f"Management Titles: Removed {management_count} Director/VP/Head level candidates")

# Save filtered results
df.to_csv(output_file, index=False)
print(f"\nFinal: {len(df)} candidates (from {original_count} original)")
print(f"Saved to: {output_file}")
print(f"\nReady for Crustdata enrichment!")
```

## Workflow

1. Load raw PhantomBuster CSV
2. Apply blacklist (always)
3. Ask user which optional filters to apply
4. Apply optional filters based on user choice
5. Save filtered CSV
6. Report summary statistics
7. Output file is ready for Crustdata enrichment

## Example Usage

```
/phantom-pre-filter "C:\Users\gehta\Downloads\phantom_export.csv" "phantom_filtered.csv"
```

This saves Crustdata API costs by filtering out irrelevant candidates before enrichment.
