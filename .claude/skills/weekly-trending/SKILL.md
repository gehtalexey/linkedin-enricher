---
name: weekly-trending
description: Weekly sweep of GitHub Trending for tools relevant to Alexey's sourcing/recruiting stack (Kalamata + SourcingX) — AI agent/orchestration frameworks, LLM screening tooling, Crustdata/LinkedIn/email enrichment, Supabase/Streamlit tooling. Use when Alexey asks "what's trending on GitHub", "any new tools this week", "run weekly trending", or via the Sunday routine. Appends findings to log.md so the same repo is never re-suggested.
---

# Weekly Trending — sourcing & AI-agent tooling radar

Once a week (or on demand), scan GitHub Trending, filter hard for anything
genuinely useful to Alexey's two live projects, and log the top 3 candidates
— with a one-line "why" and a concrete next step. This skill never installs
anything or touches pipeline code — it only writes to `log.md`.

## Relevance lens — what "relevant" means here

Score every trending repo against these two live projects. A tool only
qualifies if it plausibly upgrades one of these buckets:

**Kalamata** (`agent-kalamata`, aka israel-sourcing-autopilot) — Python.
Daily automated sourcing pipeline: Crustdata search → Google Sheets filter →
Claude screening → SalesQL/ContactOut email lookup → GEM/SmartLead push →
Slack. Runs as Claude Code Dynamic Workflows (`.claude/workflows/*.js`),
shares one Supabase DB across 4 sibling projects, has a homegrown
"sourcer's brain" cross-run memory system, worktree-isolated dev.

**SourcingX** (`sourcingx`) — Python/Streamlit. Interactive recruiter
dashboard: screens LinkedIn profiles against JDs via OpenAI/Claude,
Crustdata enrichment, PhantomBuster scraping, SalesQL email lookup, Supabase
storage. Codex reviews every Claude-authored PR via GitHub.

Concretely, flag repos that fall into:

1. **AI agent / multi-agent orchestration** — alternatives or complements to
   Claude Code Workflows/subagents/skills.
2. **LLM screening, structured output, eval/prompt tooling** — anything that
   could sharpen `structured_screening.py`, `screening_policy.py`,
   position-specific screening skills.
3. **Agent memory** — anything comparable to or better than the homegrown
   `sourcer_memory.py` / "sourcer's brain," or that could give SourcingX a
   persistence layer for screening results (today explicitly NOT stored).
4. **Outreach / email-writing quality** — anything that helps
   `email_generator.py`, `opener-writer`, `email-subject-line`,
   `email-personal-note` sound less like generic AI output.
5. **People/company enrichment, LinkedIn scraping, email-finding** — direct
   alternatives or complements to Crustdata/PhantomBuster/SalesQL/ContactOut.
6. **RAG / embeddings / vector search** — could improve `embeddings.py`,
   `similar_profiles.py`, `backfill_embeddings.py`.
7. **Supabase / Postgres tooling** — migrations, RLS, pgvector — both
   projects share one Supabase DB.
8. **Streamlit alternatives or power-ups** — `dashboard.py` is a single
   ~600KB Streamlit file; anything that helps componentize or speed it up.
9. **Python reliability patterns** — rate limiting, circuit breakers,
   retries (mirrors `error_handling.py`/`api_helpers.py`) — only if clearly
   better than what's already there.
10. **AI gateway / multi-provider routing / cost tracking** — both projects
    juggle OpenAI + Claude + several paid data APIs and track usage by hand
    (`usage_tracker.py`).

Ignore: generic web/mobile/game frameworks, unrelated languages, anything
under ~500 stars this week, and anything logged in `log.md` in the last 8
weeks unless it has since changed enough to matter.

## How to run it

1. Fetch GitHub Trending with `WebFetch` on `https://github.com/trending?since=weekly`
   (and `https://github.com/trending/python?since=weekly` — both projects
   are Python). If WebFetch is blocked, fall back to
   `mcp__github__search_repositories` sorted by stars with a
   `pushed:>N days ago` qualifier as a trending proxy.
2. Read `log.md` — don't re-surface anything logged in the last 8 weeks
   unless something materially changed.
3. Score every candidate against the relevance lens above. Be skeptical —
   most trending repos are not relevant. "Nothing new this week that clears
   the bar" is a perfectly good outcome — do not force 3 mediocre picks.
4. Pick the top 3 (fewer if fewer qualify) most useful repos not already
   logged.
5. For each: what it is (1 sentence), why it's relevant (which bucket +
   which specific file/pain point it touches), and a concrete next step
   (e.g. "worth a 30-min spike replacing X in Y").
6. Append a new dated entry to `log.md`, newest on top.
7. Report the same top 3 back in chat, short.

## log.md entry format

```
## YYYY-MM-DD
1. **repo-name** (~stars this week) — what it is. Relevant because: ...
   Next step: ...
2. ...
3. ...
(or: "Nothing new this week that clears the bar.")
```

## Notes

- Read-only against both projects — never installs a package or edits
  pipeline code, only appends to `log.md`.
- Runs via a weekly Routine (Sundays), or on demand when Alexey asks "what's
  trending" / "any new tools this week."
- If GitHub Trending is unreachable, say so plainly rather than guessing at
  repo names.
