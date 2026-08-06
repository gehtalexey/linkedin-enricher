# Weekly Trending Log

Newest entries on top. Anything logged here in the last 8 weeks should not
be re-suggested unless something materially changed.

---

## 2026-07-15

Scanned `github.com/trending?since=weekly`. Most of this week's list is
generic AI-agent/design tooling with no clear tie to sourcing — skipped
`abseil-cpp`, `OfficeCLI`, `CubeSandbox`, `claude-video`, `impeccable`,
`astryx`, `bun`, `argo-cd`, `pentagi`, `meetily`, `archify` (no concrete
Kalamata/SourcingX pain point).

1. **codex-plugin-cc** (~2,063 stars) — lets Codex run directly inside
   Claude Code for code review, instead of round-tripping through GitHub PR
   comments. Relevant because: SourcingX's `CLAUDE.md` already documents
   exactly this loop by hand ("Claude opens a PR → Codex reviews on GitHub →
   Claude reads PR comments and fixes"). This plugin could collapse that
   into one session instead of a GitHub round-trip.
   Next step: try it on one SourcingX PR and see if it saves the
   context-switch without losing review quality.
2. **hallmark** (~2,274 stars) — a skill that flags AI-generated-content
   "tells" (patterns, tone) in Claude/Cursor output. Relevant because: both
   projects generate candidate-facing outreach text — `email_generator.py`,
   `opener-writer` (Kalamata), `email-subject-line` /
   `email-personal-note` (SourcingX) — where sounding like generic AI slop
   directly hurts reply rates.
   Next step: run it over a batch of recent generated openers/emails and
   see what patterns it flags before shipping the next batch.
3. **TencentDB-Agent-Memory** (~1,790 stars) — a local long-term memory
   system for AI agents with no external dependencies. Relevant because:
   Kalamata already built a bespoke cross-run memory system
   (`sourcer_memory.py` / "sourcer's brain"), and SourcingX explicitly does
   NOT persist screening results today ("each JD gets a fresh evaluation").
   Worth comparing against the homegrown system, or as a lighter-weight
   option for giving SourcingX itself a screening-memory layer.
   Next step: skim its memory model against `sourcer_memory.py`'s — worth
   adopting only if it's genuinely simpler, not just different.

Honorable mention (didn't make top 3): **OmniRoute** (~4,297 stars) — AI
gateway with multi-provider routing + token-cost optimization. Both
projects juggle OpenAI + Claude + several paid data APIs and track usage by
hand (`usage_tracker.py`) — worth a second look if the top 3 don't pan out.
