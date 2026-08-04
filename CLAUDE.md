# SourcingX — AI-Powered Recruiting Automation

## Workflow Rules

- **Desktop (Claude Code CLI):** Before the **first** change on `master` in a session, confirm with Alexey once and offer a feature branch. After he confirms, treat subsequent edits, commits, and pushes to `master` in that same session as authorized — don't re-prompt for each one. The confirmation resets at the start of a new session.
- **Mobile (Claude app):** Branches are created automatically - safe by default.
- **Codex code review:** Codex (second AI coding agent) reviews all code Claude writes in this project. Flow: Claude opens a PR against `master` → Codex finds the latest open SourcingX PR and posts its review directly on GitHub → Claude reads the PR comments/reviews (via `gh pr view --comments`, `gh api`, or equivalent) and applies fixes on the same branch. Codex does not edit files; Alexey does not relay feedback by hand. Before applying Codex's fixes, re-read the file in case state has changed since the review.

## AI coding workflow

This repo uses GitHub PRs as the coordination layer between Claude (the code-writing agent), Codex (the reviewing agent), and Alexey. Default rules:

1. **Feature branches only.** Claude implements changes on a feature branch named `<type>/<short-slug>` (e.g. `fix/profiles-status`, `chore/ai-pr-workflow`). Direct commits to `master` are only allowed for the explicit session exception above.
2. **PR is the unit of review.** When a feature branch is ready, open a PR against `master`. The PR description states what changed, why, and what was tested. Codex reviews the PR, Claude applies fixes on the same branch, and Alexey merges.
3. **Reference REVIEW.md.** Both Claude (self-review before pushing) and Codex (reviewing the PR) use the checklist in `REVIEW.md` as the criteria. New PRs that touch production code paths should pass that checklist before merge.
4. **CI is required.** The GitHub Actions workflow under `.github/workflows/test.yml` runs focused tests on every push and PR targeting `master`. PRs do not merge until CI is green.
5. **Merge only on Alexey's direct say-so.** Default is still manual — Alexey reviews the final state and clicks merge himself. Claude may merge a PR itself (e.g. `gh pr merge --squash --delete-branch`, matching how recent PRs in this repo were merged) only when Alexey explicitly says to merge it in that chat turn (e.g. "merge now", "merge it"). Never merge on your own initiative just because CI is green or Codex review is clean — that's a signal it's ready, not permission to merge. (Confirmed 2026-07-21.)
6. **No broad write permissions for CI.** The workflow declares `permissions: contents: read` explicitly. Any future job that needs write access must be added narrowly and reviewed.
7. **No secrets in the repo.** Real API keys live in `config.json` (gitignored) or GitHub Actions secrets, never in source. Test code uses placeholder keys (`"test-key"`) which the workflow injects via env. Local agent state such as `.claude/settings.local.json`, `.claude/launch.json`, `.claude/scheduled_tasks.lock`, `.claude/worktrees/`, `.agent/`, `.agents/`, `.continue/`, and scratch JSON/CSV outputs is gitignored and must never be committed. Tracked project skill files under `.claude/skills/` are allowed only when intentionally maintained as repo instructions.

### Commit messages

Use Conventional Commits: `<type>(<scope>): <subject>`. Valid types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`. Subject ≤72 chars, lowercase, imperative, no trailing period.

## Project Overview

SourcingX is a Streamlit-based recruiting automation platform that screens LinkedIn profiles against job descriptions using AI. It integrates with Crustdata (profile enrichment), PhantomBuster (LinkedIn scraping), SalesQL (email lookup), and OpenAI (AI screening).

## Tech Stack

- **Frontend/Backend**: Streamlit (Python) — single-file app in `dashboard.py`
- **Database**: Supabase (PostgreSQL via REST API) — stores enriched profiles, NOT screening results
- **AI Screening**: OpenAI `gpt-4o-mini` (default), `gpt-4o`, or Claude Haiku (`anthropic`)
- **Profile Data**: Crustdata API (enrichment), PhantomBuster (scraping)
- **Email Lookup**: SalesQL API — **before touching SalesQL code or debugging email hit-rate, read `.claude/skills/salesql-api/SKILL.md`** (URL format rules — hash-suffix URLs are valid, don't strip them; coverage-vs-URL-bug distinction; credit accounting; gotchas)
- **Config**: Google Sheets (company lists, universities, blacklists) + local CSV filters

## Project Structure

```
sourcingX/
├── dashboard.py              # Main Streamlit app (UI, screening, batch processing)
├── db.py                     # Supabase REST client (profiles, queries, usage)
├── prompts.py                # 20 role-specific screening prompt templates
├── normalizers.py            # Data normalization (PhantomBuster/Crustdata formats)
├── helpers.py                # Display helpers, field extraction
├── usage_tracker.py          # API cost tracking across all providers
├── enrich.py                 # Standalone Crustdata enrichment CLI
├── error_handling.py         # Exception hierarchy, retry logic, circuit breaker
├── api_helpers.py            # Rate limiting (token bucket), safe API call wrappers
├── security.py               # Input validation, secrets protection
├── db_transactions.py        # Batch operation safety patterns
├── db_migrations.py          # Database migration management
├── pb_dedup.py               # PhantomBuster deduplication (post-scrape filtering)
├── config.json               # API keys (DO NOT commit — use config.example.json)
├── config.example.json       # Config template with placeholder keys
├── requirements.txt          # 10 dependencies
├── supabase_setup.sql        # Initial database schema
├── conftest.py               # Pytest fixtures and mock helpers
├── test_screening.py         # AI screening tests
├── test_mocking_services.py  # External service mocking tests
├── test_screening_fixes.py   # Regression tests for screening helpers
├── test_screening_parameterized.py  # Parameterized screening tests
├── .claude/skills/           # Claude Code skills (5 skills)
├── skills/                   # Additional skills (email-subject-line, email-personal-note)
├── .devcontainer/            # Dev container config (Python 3.11, port 8501)
├── .streamlit/               # Streamlit config (dark theme)
├── filters/                  # CSV filter files (blacklist, companies, universities)
├── migrations/               # SQL migration files (11 migrations)
├── sql/                      # SQL scripts (screening_prompts table)
└── docs/                     # Analysis documents
```

## Key Files

| File | Purpose | Lines |
|------|---------|-------|
| `dashboard.py` | Main Streamlit app — UI, screening logic, batch processing | ~7,300 |
| `db.py` | Supabase REST client — profile storage, queries, settings | ~1,100 |
| `prompts.py` | 20 role-specific screening prompts (Israel + Global) | ~1,400 |
| `error_handling.py` | Exception hierarchy, retry with backoff, circuit breaker | ~660 |
| `normalizers.py` | Data normalization between PhantomBuster/Crustdata formats | ~590 |
| `api_helpers.py` | Token bucket rate limiting, safe API call wrappers | ~570 |
| `security.py` | Input validation, secrets protection, rate limiting decorators | ~360 |
| `db_transactions.py` | Safe batch operations with error handling | ~340 |
| `usage_tracker.py` | API cost tracking (Crustdata, OpenAI, SalesQL, PhantomBuster) | ~280 |
| `helpers.py` | Display helpers, field extraction from Crustdata responses | ~270 |
| `db_migrations.py` | Migration management with checksum verification | ~250 |
| `pb_dedup.py` | PhantomBuster post-scrape deduplication against database | ~190 |
| `enrich.py` | Standalone Crustdata enrichment CLI utility | ~180 |
| `config.json` | API keys and Google Sheets URLs (DO NOT commit) | |

## Architecture Decisions

### Screening is ALWAYS fresh per JD
- Screening results are NOT stored in Supabase — each JD gets a fresh evaluation
- Profile enrichment data (raw Crustdata JSON) IS stored in Supabase
- Session-level results are kept in `st.session_state['screening_results']`

### Experience & durations are pre-computed in Python
- Total career experience is calculated in `dashboard.py` (search for `calculate_total_experience` or the experience limit check block)
- `compute_role_durations()` calculates per-role months from all `past_employers` + `current_employers` in raw Crustdata JSON
- Durations formatted as `Xy Ym` (e.g., `4y 2m`) or just `Xm` if under a year (e.g., `7m`)
- Company-level stability summary groups roles by company (handles promotions), flags short stints (<12mo)
- `STABILITY VERDICT` is a hard cap: FAIL (3+ short stints → max score 4, current <6mo → max score 5) or PASS
- `trim_raw_profile()` strips logos/IDs/long descriptions from raw JSON (54-64% token reduction)
- Military service (IDF) is detected and excluded from "reject >X years" checks
- A `EXPERIENCE LIMIT CHECK` verdict is pre-computed and injected into the prompt
- The AI should NEVER recalculate durations or experience — it must use the pre-computed values

### Israeli military service handling
- Israeli military is mandatory (age 18-21) and excluded from experience limits
- Military keywords: idf, mamram, unit 8200, talpiot, israeli air force, c4i, etc.
- Two metrics: TOTAL CAREER SPAN (with military) and INDUSTRY EXPERIENCE (without military)
- The AI uses INDUSTRY EXPERIENCE for "reject >X years" rules

### Error handling & resilience
- Custom exception hierarchy in `error_handling.py` (see section below)
- Retry with exponential backoff via `retry_with_backoff()` decorator
- Circuit breaker pattern prevents cascade failures to external services
- Token bucket rate limiting in `api_helpers.py` protects API quotas
- Input validation in `security.py` for LinkedIn URLs, text inputs, API keys

## Critical Functions in dashboard.py

| Function | Purpose |
|----------|---------|
| `screen_profile()` | Screens a single profile against JD via OpenAI/Claude |
| `screen_profiles_batch()` | Parallel screening with ThreadPoolExecutor |
| `compute_role_durations()` | Pre-calculates per-role durations (Xy Ym) + stability verdict from raw JSON |
| `trim_raw_profile()` | Strips logos/IDs/long descriptions from raw JSON (54-64% token savings) |
| `build_raw_data_index()` | Indexes raw data by LinkedIn URL for fast lookup |
| `fetch_raw_data_for_batch()` | Loads raw data from DB for a batch (memory efficient) |
| `enrich_batch()` | Batch enrich profiles via Crustdata API |
| `apply_pre_filters()` | Apply pre-filtering rules before screening |
| `normalize_phantombuster_columns()` | Normalize PhantomBuster CSV columns |
| `launch_phantombuster_agent()` | Launch a PhantomBuster scraping agent |

## Error Handling & Resilience Patterns

### Exception hierarchy (`error_handling.py`)
```
ApplicationError (base)
├── ExternalServiceError  — API errors (service name, status code, response body)
│   └── RateLimitError    — Rate limit errors (includes retry_after)
├── ValidationError       — Input validation errors
├── DatabaseError         — Database operation errors
└── CircuitBreakerError   — Circuit breaker is open (service temporarily unavailable)
```

### Retry decorator
Use `retry_with_backoff()` from `error_handling.py` for API calls that may fail transiently. It supports configurable max retries, base delay, and exception filtering.

### Circuit breaker
`get_service_circuit()` returns a circuit breaker per external service. After a threshold of failures, the circuit opens and fast-fails requests for a cooldown period, preventing cascade failures.

### Rate limiting
`api_helpers.py` provides a `RateLimiter` class (token bucket algorithm) and `rate_limited()` decorator. Use `get_rate_limiter()` to get a limiter for a specific provider.

### Input validation
`security.py` provides validation functions: `validate_linkedin_url()`, `validate_google_sheets_url()`, `validate_text_input()`, `validate_api_key_format()`, `validate_job_description()`. Use these at system boundaries (user input, external data).

## Screening Prompt Architecture

The screening prompt sent to OpenAI has these sections (in order):
1. **Role-specific rubric** from `prompts.py` (e.g., FULLSTACK_ISRAEL)
2. **Company description analysis** instructions
3. **Anti-hallucination rules**
4. **Assessment rules** (signal check, tiered scoring)
5. **User prompt** with JD + pre-calculated experience + raw JSON

### Pre-calculated sections injected into user prompt:
- **Role durations** from `compute_role_durations()` — every role with `Xy Ym` duration + stability verdict
- **Trimmed profile** from `trim_raw_profile()` — raw JSON with noise removed
- Current employer (title, company, start date, duration)
- Lead/management experience (months calculated, consulting excluded)
- Fullstack experience (title + skills analysis)
- DevOps experience (title + skills analysis)
- Total career experience with EXPERIENCE LIMIT CHECK verdict

## Common Pitfalls

### AI hallucinating experience/duration numbers
The AI (especially gpt-4o-mini) may try to recalculate experience or role durations from raw JSON and get it wrong. The fix: `compute_role_durations()` pre-calculates all durations in Python (formatted as `Xy Ym`), and a `STABILITY VERDICT` + `EXPERIENCE LIMIT CHECK` are injected as hard-cap verdicts the AI must follow. Additionally, `trim_raw_profile()` reduces token usage by 54-64%.

### "Already screened" profiles
Screening is always fresh per JD. Never skip profiles based on previous scores. Different JDs = different scores.

### Memory management
- Raw Crustdata JSON is 20-50KB per profile
- Batch processing loads raw data per-batch, not all at once
- Session state is cleaned after screening completes
- Max ~2-3 concurrent Streamlit users on free tier

### Overqualification rules
Only reject titles the JD EXPLICITLY lists. "Team Lead" is NOT overqualified unless the JD says so. Read the JD's reject list literally.

### Duplicate functions
`normalize_crustdata_profile()` exists in both `normalizers.py` (canonical) and `dashboard.py` (inline copy). The canonical version in `normalizers.py` is the source of truth.

### URL matching & "already enriched" counts
**READ `.claude/skills/url-flow/SKILL.md` FIRST** when debugging URL issues.

- **Crustdata echoes input**: `query_linkedin_profile_urn_or_slug` field returns our exact input URL — this is the PRIMARY matching method (Tier 1)
- **Problem**: Crustdata's `linkedin_flagship_url` is often different from input (e.g., `yderman` for `yoav-derman-365736152`)
- **Dual storage**: DB stores both `linkedin_url` (Crustdata canonical) and `original_url` (input URL)
- **Matching cascade**: 5-tier matching in `enrich_batch()` — query echo → username → base → reversed → hyphen-free → name-based
- **Multi-source limitation**: `original_url` is single TEXT field — if same profile uploaded from PhantomBuster AND Jam CSV with different URLs, only the latest is stored
- **Debug**: Check debug expander in Enrich tab, search DB by name, verify `original_url` is set
- **Fix pattern**: Update `original_url` in DB if profile was saved without it

## Skills (Claude Code)

| Skill | Path | Purpose |
|-------|------|---------|
| `/screen` | `.claude/skills/screen/SKILL.md` | Team leader: coordinates parallel screening agents |
| `/email-opener` | `.claude/skills/email-opener/SKILL.md` | Generate personalized outreach emails |
| `/pre-filter-candidates` | `.claude/skills/pre-filter-candidates/SKILL.md` | Pre-filter profiles before screening |
| `/filter-csv-columns` | `.claude/skills/filter-csv-columns/SKILL.md` | CSV column filtering for recruiter review |
| `/phantom-pre-filter` | `.claude/skills/phantom-pre-filter/SKILL.md` | Pre-filter raw PhantomBuster CSV before enrichment |
| (email-personal-note) | `skills/email-personal-note/SKILL.md` | Generate personal notes for follow-up emails |
| (email-subject-line) | `skills/email-subject-line/SKILL.md` | Generate email subject lines with angle variation |

### Reference Skills (Auto-Consult)

These skills are NOT user-invocable. Claude should automatically read them when working on related issues:

| Skill | Path | When to Consult |
|-------|------|-----------------|
| url-flow | `.claude/skills/url-flow/SKILL.md` | **URL matching bugs**, "already enriched" count wrong, profiles not deduping, enrichment URL mismatches, `original_url` issues |

## Prompt Templates (prompts.py)

### Israel Engineering
`BACKEND_ISRAEL`, `FRONTEND_ISRAEL`, `FULLSTACK_ISRAEL`, `DEVOPS_ISRAEL`, `TEAMLEAD_ISRAEL`, `PRODUCT_ISRAEL`, `MOBILE_ISRAEL`

### Global GTM
`SALES_GLOBAL`, `SDR_GLOBAL`, `MARKETING_GLOBAL`, `CUSTOMER_SUCCESS_GLOBAL`, `SOLUTIONS_ENGINEER_GLOBAL`

### Other
`ENGINEERING_GLOBAL`, `MANAGER`, `VP`, `DATASCIENCE`, `AUTOMATION`, `AI_ENGINEER`, `GENERAL`

### Prompt selection
- `DEFAULT_PROMPTS` dict maps role keys to prompt templates
- `DEFAULT_SCREENING_PROMPT` uses `GENERAL` as fallback
- `db.py:match_prompt_by_keywords()` finds the best prompt for a JD based on keyword matching
- Prompts can also be stored/customized in Supabase via `screening_prompts` table

## Shared Supabase database — write/read consistency (CRITICAL)

This Supabase database is shared by these ACTIVE projects *(list corrected 2026-08-04)*:
- **SourcingX** (this project) — the interactive sourcing app. **This is the canonical reference** for how profiles get written and read.
- **Supanova** — owns the shared `profiles` table's monthly refresh, and documents the agreed Crustdata endpoint/cost rules in its own `CLAUDE.md`. (Its growth job is stopped as of 2026-08-04.)
- **agent-kalamata** (+ `agent-kalamata-pipeline-api`) — daily sourcing pipeline.

**Retired, do NOT count these as live:** `daily-sourcing-autopilot-e2e`,
`smartlead-sourcing-autopilot`, `israel-sourcing-autopilot`. Confirmed 2026-08-04:
none has run a sourcing job since May 2026 and none has a scheduled workflow that
spends credits.

**The rule:** all three projects MUST write profiles to the `profiles` table the exact same way, and search/read them the exact same way. If they drift apart, one project silently corrupts or misreads another's data.

- The two autopilot projects' profile-saving code (`core/db.py`: `save_enriched_profile`, `_prepare_profile_row`, `_bulk_fetch_existing_original_urls`, `save_enriched_profiles_bulk`, `SupabaseClient.upsert` / `upsert_batch`; `core/normalizers.py`: `normalize_linkedin_url`, `pick_current_employer`, `_parse_start_date_sort_key`) is a **direct port of this project's** `db.py` / `normalizers.py`.
- **Before changing how SourcingX writes or searches profiles** (the `profiles` table — columns written, field extraction from the Crustdata response, timestamp format, URL normalization, `original_urls` array handling, search/filter query patterns), remember the change must be mirrored into both autopilot projects. Flag it so the ports stay in sync.
- SourcingX wins ties — it is the reference implementation. But a change here is not "done" until the autopilots match.

The global `~/.claude/CLAUDE.md` and each project's `CLAUDE.md` repeat this rule — keep all copies in agreement.

**This rule was violated and it cost us.** SourcingX's `semantic_profile_to_legacy_shape()` writes the Crustdata id as `crustdata_person_id`; Supanova's refresh looked only for `person_id`. Result: **9,929 profiles (~5% of the table) were invisible to Supanova's refresh** and never got updated. Measured against the live DB 2026-08-04, not estimated. The shared table now holds at least four different `raw_data` dialects. **Agree key names with the other projects before adding a fifth.**

## Crustdata endpoints — which to use, and what they cost

**Hard deadline: all legacy `/screener/*` endpoints stop being served 2026-09-30**
(confirmed in writing by Crustdata, 2026-07-11). Migration plan and live probe
findings live in agent-kalamata: `docs/plans/crustdata-api-migration-plan.md`,
`docs/crustdata/phase0-probe-findings.md`.

New endpoints need BOTH `Authorization: Bearer <key>` and
`x-api-version: 2025-11-01`. A missing version header returns 400.

### Person enrichment — always the NEW endpoint (1 credit, not 3)

| | Endpoint | Cost |
|---|---|---|
| ❌ legacy | `GET /screener/person/enrich` | **3 credits/profile**, flat |
| ✅ new, sync | `POST /person/enrich` (≤25 URLs) — `sync_enrich_profile()` | **1 credit/profile** base |
| ✅ new, batch | `POST /batch/person/enrich` (≤10,000, async) — `batch_enrich_profiles()` | **1 credit/profile** base |

Base credit covers `basic_profile` (incl. `summary`), `experience`, `education`,
`skills`, `professional_network`. Add-ons are additive and charged ONLY when named
in `fields`: +1 business email, +1 dev-platform, +2 personal emails, +2 phones
(max 7). **Never request an add-on you don't need — `fields` drives the cost.**

⚠️ **Without an explicit `fields` list the new endpoint returns only
`basic_profile` + `social_handles`.** Skills/experience/education are covered by
the base credit but are NOT returned unless asked for.

⚠️ The sync response envelope is
`[{match_type, matched_on, matches: [{confidence_score, person_data}]}]` — the
profile is at `matches[].person_data`, two levels deep (verified live 2026-08-04).

**Real cost of getting this wrong:** on 2026-08-04 the Upload-tab "Enrich N
profiles" button enriched 1,179 profiles on the legacy endpoint for **3,537
credits**. The same run on the new endpoint costs **1,179**.

### Identity checking is a safety gate, not a nicety

Enrichment responses must be verified against the URL we asked for before being
trusted — Crustdata can return a different person. Use
`linkedin_profile_identity_matches()`, which allows LinkedIn's regional hosts
(`il.`, `fr.`, …) but requires the path to begin with `/in/` and hold exactly one
slug. **Do not compare raw URL strings** (`il.linkedin.com` vs `www.linkedin.com`
are the same person) and **do not just search for `/in/` anywhere in the path**
(`/company/acme/in/target` is not a person). Both mistakes were made and caught in
review; the first silently rejected real paid matches, the second would have let a
non-profile URL through the wrong-person guard.

### Person search

Legacy `POST /screener/persondb/search` → new `POST /person/search`. Same cost
(3 credits/100). **The new search does NOT return `skills` or `summary`** — they
moved to enrichment. AI Screen's thin-profile detection keys on exactly those two
fields, so a profile saved without them gets auto-enriched on every screening run
unless the cooldown marker is respected.

## Database Schema (Supabase)

### `profiles` table (enrichment data only)
- `linkedin_url` (UNIQUE), `raw_data` (JSONB), `name`, `current_title`, `current_company`
- `all_employers[]`, `all_titles[]`, `all_schools[]`, `skills[]` (GIN-indexed arrays)
- `email`, `email_source`, `status` (enriched/screened/contacted/archived)
- `enriched_at`, `screened_at`, `contacted_at` (timestamps)
- Screening fields: `screening_score`, `screening_fit_level`, `screening_summary`, `screening_reasoning`

### `api_usage_logs` table
- Provider, operation, credits, tokens, cost tracking
- Indexed on provider and created_at

### `screening_prompts` table
- `role_type`, `name`, `prompt_text`, `keywords[]`, `is_default`
- Managed via `sql/screening_prompts.sql`

### Views
- `profiles_needing_screening` — enriched profiles awaiting screening
- `pipeline_stats` — funnel metrics (pending, screened, contacted, fit levels)

### Migrations
11 migration files in `migrations/` (002-010 + create_api_usage_logs). Managed by `db_migrations.py` with checksum verification. Initial schema in `supabase_setup.sql`.

## Testing

Run tests with: `pytest`

| File | Purpose |
|------|---------|
| `conftest.py` | Pytest fixtures: MockOpenAIResponse, MockOpenAIClient, shared profile/JD fixtures |
| `test_screening.py` | AI screening logic tests with sample JDs and profiles |
| `test_mocking_services.py` | External service mocking patterns, error handling, concurrency |
| `test_screening_fixes.py` | Regression tests for `format_past_positions()` and `format_education()` |
| `test_screening_parameterized.py` | Parameterized tests with `pytest.mark.parametrize` |

## Development Setup

1. **DevContainer** (recommended): Python 3.11, auto-forwards port 8501
2. **Dependencies**: `pip install -r requirements.txt` (streamlit, pandas, requests, openai, anthropic, gspread, google-auth, streamlit-authenticator, bcrypt, plotly)
3. **Config**: Copy `config.example.json` to `config.json` and fill in API keys (Crustdata, OpenAI, PhantomBuster, Google credentials)
4. **Run**: `streamlit run dashboard.py`
5. **Filter data**: CSV files in `filters/` (blacklist.csv, not_relevant_companies.csv, target_companies.csv, universities.csv)

## Conventions

- Screening happens through OpenAI API or Claude Haiku (anthropic) for the dashboard
- Claude Code skills (`/screen`, `/email-opener`) use Claude for agent-based workflows
- Config lives in `config.json` — never commit API keys
- CSV exports use UTF-8-BOM for Excel compatibility
- LinkedIn URLs are normalized before storage (`normalize_linkedin_url()` in `normalizers.py`)
- Security validation should use `security.py` functions for user input at system boundaries
- API calls to external services should use `api_helpers.py` wrappers for rate limiting
- Filter CSVs live in `filters/` directory
- Streamlit theme: dark mode with purple primary (`#615fff`)

## Scheduled Jobs / DB Refresh — RETIRED (moved to Supanova)

The old daily `db-refresh.yml` workflow and its scripts
(`scripts/reenrich_sparse_profiles.py`, `scripts/check_monthly_credit_budget.py`)
were **removed on 2026-06-08**. They re-enriched stale profiles at 3 credits *per
profile*, which is ~100× more expensive than needed.

Database refresh + growth now live in the **Supanova** repo
(`github.com/gehtalexey/supanova`). Supanova refreshes profiles by Crustdata
`person_id` via `people_search_db` (3 credits per *100*, no enrichment) and pulls
new profiles for active `pipeline_positions`, on a monthly schedule. Do not
re-add a refresh job here.

