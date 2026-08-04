"""Tests for dashboard.enrich_thin_profiles_for_batch() — the screening-time
auto-enrichment step that tops up profiles missing skills/summary (e.g. from
the new Crustdata search endpoints) via the new batch-enrich pipeline before
AI Screen sees them.

All network-free: batch_enrich_profiles() and save_enriched_profiles_bulk()
are monkeypatched at the dashboard module level.
"""

import dashboard
from crustdata_search import semantic_profile_to_legacy_shape


def _thin_profile(url, name="Thin Person"):
    return {
        "linkedin_url": url,
        "name": name,
        "raw_crustdata": {"name": name, "skills": [], "summary": ""},
    }


def _rich_profile(url, name="Rich Person"):
    return {
        "linkedin_url": url,
        "name": name,
        "raw_crustdata": {
            "name": name,
            "skills": ["Python"],
            "summary": "Already has everything.",
        },
    }


def _flat_enriched(url, name):
    return {
        "name": name,
        "skills": ["Python", "Kubernetes"],
        "summary": "Filled in via batch enrich.",
        "linkedin_flagship_url": url,
        "current_employers": [],
        "past_employers": [],
    }


class TestEnrichThinProfilesForBatch:
    def test_only_thin_profiles_are_submitted(self, monkeypatch):
        thin = _thin_profile("https://www.linkedin.com/in/thin")
        rich = _rich_profile("https://www.linkedin.com/in/rich")

        captured_urls = {}

        def fake_batch_enrich(urls, api_key=None):
            captured_urls["urls"] = urls
            return {
                "by_url": {urls[0]: _flat_enriched(urls[0], "Thin Person")},
                "requested": 1, "fulfilled": 1, "unmatched": [], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        monkeypatch.setattr(dashboard, "save_enriched_profiles_bulk", lambda *a, **k: {"saved": 1, "errors": 0, "error_messages": []})

        stats = dashboard.enrich_thin_profiles_for_batch([thin, rich], api_key="test-key", db_client=None)

        assert captured_urls["urls"] == ["https://www.linkedin.com/in/thin"]
        assert stats == {
            "thin_found": 1, "enriched": 1, "unmatched": 0, "unknown": 0,
            "on_cooldown": 0, "on_cooldown_urls": [], "credits_used": 1,
        }
        # The already-rich profile must be untouched.
        assert rich["raw_crustdata"]["name"] == "Rich Person"
        assert rich["raw_crustdata"]["skills"] == ["Python"]

    def test_thin_profile_gets_merged_with_enriched_data(self, monkeypatch):
        thin = _thin_profile("https://www.linkedin.com/in/thin")

        def fake_batch_enrich(urls, api_key=None):
            return {
                "by_url": {urls[0]: _flat_enriched(urls[0], "Thin Person")},
                "requested": 1, "fulfilled": 1, "unmatched": [], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        monkeypatch.setattr(dashboard, "save_enriched_profiles_bulk", lambda *a, **k: {"saved": 1, "errors": 0, "error_messages": []})

        dashboard.enrich_thin_profiles_for_batch([thin], api_key="test-key", db_client=None)

        assert thin["raw_crustdata"]["skills"] == ["Python", "Kubernetes"]
        assert thin["raw_crustdata"]["summary"] == "Filled in via batch enrich."
        assert "raw_data" not in thin  # stale key cleared, screen_profile() must read raw_crustdata

    def test_save_receives_flat_profiles_not_a_wrapper(self, monkeypatch):
        """Regression test for the Codex-caught bug (2026-07-20): the save
        step must pass the translator's flat dicts directly to
        save_enriched_profiles_bulk(), NOT wrapped as
        {linkedin_url, raw_data, original_url} — a wrapper would make
        _prepare_profile_row() silently write near-empty rows."""
        thin = _thin_profile("https://www.linkedin.com/in/thin")
        flat = _flat_enriched(thin["linkedin_url"], "Thin Person")

        def fake_batch_enrich(urls, api_key=None):
            return {
                "by_url": {urls[0]: flat},
                "requested": 1, "fulfilled": 1, "unmatched": [], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        saved_args = {}

        def fake_save(db_client, profiles, original_url_map=None, batch_size=100, **kwargs):
            saved_args["profiles"] = profiles
            saved_args["original_url_map"] = original_url_map
            return {"saved": len(profiles), "errors": 0, "error_messages": []}

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        monkeypatch.setattr(dashboard, "save_enriched_profiles_bulk", fake_save)

        dashboard.enrich_thin_profiles_for_batch([thin], api_key="test-key", db_client=object())

        assert saved_args["profiles"] == [flat]
        # Every saved item must have name/skills directly at the top level —
        # exactly what _prepare_profile_row() reads — not nested under a
        # "raw_data" key.
        assert saved_args["profiles"][0]["name"] == "Thin Person"
        assert saved_args["profiles"][0]["skills"] == ["Python", "Kubernetes"]
        assert thin["linkedin_url"] in saved_args["original_url_map"].values()

    def test_unmatched_profiles_left_thin_and_counted_not_blocked(self, monkeypatch):
        thin = _thin_profile("https://www.linkedin.com/in/nomatch")

        monkeypatch.setattr(
            dashboard, "batch_enrich_profiles",
            lambda urls, api_key=None: {
                "by_url": {}, "requested": 1, "fulfilled": 0,
                "unmatched": urls, "credits_used": 0, "batch_ids": ["b1"],
            },
        )
        monkeypatch.setattr(dashboard, "save_enriched_profiles_bulk", lambda *a, **k: {"saved": 0, "errors": 0, "error_messages": []})

        stats = dashboard.enrich_thin_profiles_for_batch([thin], api_key="test-key", db_client=None)

        assert stats["unmatched"] == 1
        assert stats["enriched"] == 0
        # Left as-is — still screenable, not dropped from the caller's list.
        assert thin["raw_crustdata"]["skills"] == []

    def test_enrichment_failure_does_not_raise_screening_proceeds(self, monkeypatch):
        thin = _thin_profile("https://www.linkedin.com/in/thin")

        def boom(urls, api_key=None):
            raise RuntimeError("Crustdata is down")

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", boom)

        stats = dashboard.enrich_thin_profiles_for_batch([thin], api_key="test-key", db_client=None)

        assert stats["thin_found"] == 1
        assert stats["unmatched"] == 1
        assert stats["enriched"] == 0

    def test_no_thin_profiles_skips_the_api_call_entirely(self, monkeypatch):
        rich = _rich_profile("https://www.linkedin.com/in/rich")
        called = {"n": 0}
        monkeypatch.setattr(dashboard, "batch_enrich_profiles", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

        stats = dashboard.enrich_thin_profiles_for_batch([rich], api_key="test-key", db_client=None)

        assert called["n"] == 0
        assert stats == {
            "thin_found": 0, "enriched": 0, "unmatched": 0, "unknown": 0,
            "on_cooldown": 0, "on_cooldown_urls": [], "credits_used": 0,
        }

    def test_flat_top_level_skills_and_summary_prevent_false_thin_classification(self, monkeypatch):
        """Regression test for a Codex-caught bug (2026-07-20): a profile
        with no raw_crustdata/raw_data at all (e.g. a CSV-imported row) but
        usable flat top-level skills/summary must NOT be treated as thin —
        screen_profile() already falls back to these same flat fields, so
        enriching it would just be an unnecessary paid Crustdata call."""
        flat_only = {
            "linkedin_url": "https://www.linkedin.com/in/flatonly",
            "name": "Flat Only",
            "skills": "Python, Go",
            "summary": "Already has a summary, just no nested raw blob.",
        }
        called = {"n": 0}
        monkeypatch.setattr(dashboard, "batch_enrich_profiles", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

        stats = dashboard.enrich_thin_profiles_for_batch([flat_only], api_key="test-key", db_client=None)

        assert called["n"] == 0
        assert stats["thin_found"] == 0

    def test_nan_skills_and_summary_still_counted_as_thin(self, monkeypatch):
        """Regression test for a Codex-caught bug (2026-07-20): CSV/DataFrame
        rows often carry literal float('nan') for a missing string column.
        bool(float('nan')) is True in Python, so a naive truthiness check on
        the flat fallback would treat a genuinely-empty skills/summary as
        present and skip enrichment — exactly backwards."""
        import math

        nan_profile = {
            "linkedin_url": "https://www.linkedin.com/in/nanrow",
            "name": "Nan Row",
            "skills": math.nan,
            "summary": math.nan,
        }
        captured = {}

        def fake_batch_enrich(urls, api_key=None):
            captured["urls"] = urls
            return {"by_url": {}, "requested": 1, "fulfilled": 0, "unmatched": urls, "credits_used": 0, "batch_ids": []}

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)

        stats = dashboard.enrich_thin_profiles_for_batch([nan_profile], api_key="test-key", db_client=None)

        assert captured.get("urls") == ["https://www.linkedin.com/in/nanrow"]
        assert stats["thin_found"] == 1

    def test_missing_either_skill_or_summary_alone_is_not_thin(self):
        """Alexey's call, 2026-07-20: only enrich when BOTH are missing, not
        just one — cheaper than an OR check."""
        partial = {
            "linkedin_url": "https://www.linkedin.com/in/partial",
            "raw_crustdata": {"name": "Partial", "skills": ["Python"], "summary": ""},
        }
        stats = dashboard.enrich_thin_profiles_for_batch([partial], api_key="test-key", db_client=None)
        assert stats["thin_found"] == 0


class _FakeTracker:
    """Records log_crustdata_batch_enrich() calls without a real
    db_client, so tests can assert exactly what got logged."""

    def __init__(self):
        self.calls = []

    def log_crustdata_batch_enrich(self, requested, fulfilled, status='success', error_message=None, response_time_ms=None):
        self.calls.append({'requested': requested, 'fulfilled': fulfilled, 'status': status, 'error_message': error_message})


class TestEnrichThinProfilesUnknownBillingHandling:
    """Codex adversarial review of PR #127, round 2 (2026-08-04, HIGH):
    batch_enrich_profiles() can return `unknown` — a job accepted by
    Crustdata whose outcome we couldn't confirm (poll/download failure),
    which may have already billed. This must count separately from
    `unmatched` and must flip the usage log to 'error', never a clean
    zero-credit success — the whole point of this pipeline existing is
    accurate spend tracking."""

    def test_unknown_profile_counted_separately_from_unmatched(self, monkeypatch):
        thin = _thin_profile("https://www.linkedin.com/in/thin")

        def fake_batch_enrich(urls, api_key=None):
            return {
                "by_url": {}, "requested": 1, "fulfilled": 0,
                "unmatched": [], "rejected": [], "unknown": [urls[0]],
                "unknown_diagnostics": [{"batch_id": "b1", "reason": "poll_error"}],
                "credits_used": 0, "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)

        stats = dashboard.enrich_thin_profiles_for_batch([thin], api_key="test-key", db_client=None)

        assert stats["unknown"] == 1
        assert stats["unmatched"] == 0
        assert stats["enriched"] == 0
        # Left as-is — still screenable, not dropped from the caller's list.
        assert thin["raw_crustdata"]["skills"] == []

    def test_unknown_batch_logs_as_error_not_clean_success(self, monkeypatch):
        thin = _thin_profile("https://www.linkedin.com/in/thin")
        tracker = _FakeTracker()

        def fake_batch_enrich(urls, api_key=None):
            return {
                "by_url": {}, "requested": 1, "fulfilled": 0,
                "unmatched": [], "rejected": [], "unknown": [urls[0]],
                "unknown_diagnostics": [{"batch_id": "b1", "reason": "download_error"}],
                "credits_used": 0, "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)

        dashboard.enrich_thin_profiles_for_batch([thin], api_key="test-key", db_client=None, tracker=tracker)

        assert len(tracker.calls) == 1
        assert tracker.calls[0]['status'] == 'error'
        assert 'unknown' in tracker.calls[0]['error_message'].lower()
        assert 'b1' in tracker.calls[0]['error_message']

    def test_no_unknown_still_logs_success(self, monkeypatch):
        thin = _thin_profile("https://www.linkedin.com/in/thin")
        tracker = _FakeTracker()

        def fake_batch_enrich(urls, api_key=None):
            return {
                "by_url": {urls[0]: _flat_enriched(urls[0], "Thin Person")},
                "requested": 1, "fulfilled": 1, "unmatched": [], "rejected": [],
                "unknown": [], "unknown_diagnostics": [], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)

        dashboard.enrich_thin_profiles_for_batch([thin], api_key="test-key", db_client=None, tracker=tracker)

        assert len(tracker.calls) == 1
        assert tracker.calls[0]['status'] == 'success'


def _flat_still_thin(url, name):
    """A validated enrichment result — passed the identity/content gate,
    so it's real data for the right person — that STILL has no
    skills/summary. This is the exact scenario Codex observed live: a
    person with 50 skills stored got zero skills back from one enrich
    call."""
    return {
        "name": name,
        "skills": [],
        "summary": "",
        "linkedin_flagship_url": url,
        "current_employers": [{"employee_title": "Engineer", "employer_name": "Acme"}],
        "past_employers": [],
    }


class TestThinProfileReenrichCooldown:
    """Codex adversarial review of PR #127, round 3 (HIGH): a profile that
    passes the identity/content gate (real data, right person) but still
    has no skills/summary was being resubmitted for enrichment on every
    single screening run forever, since thin-detection only looks at
    current skills/summary state — which never changes. Observed live:
    someone with 50 skills stored got zero skills back from one call.

    The fix stamps a _last_enrichment_attempt marker (inside the flat
    dict that becomes profiles.raw_data — no new column, no db.py
    changes) on an accepted-but-still-thin result, and thin-detection
    skips re-submitting a profile whose marker is within
    THIN_PROFILE_REENRICH_COOLDOWN_HOURS."""

    def test_still_thin_result_gets_marked(self, monkeypatch):
        thin = _thin_profile("https://www.linkedin.com/in/thin")

        def fake_batch_enrich(urls, api_key=None):
            return {
                "by_url": {urls[0]: _flat_still_thin(urls[0], "Thin Person")},
                "requested": 1, "fulfilled": 1, "unmatched": [], "rejected": [],
                "unknown": [], "unknown_diagnostics": [], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        monkeypatch.setattr(dashboard, "save_enriched_profiles_bulk", lambda *a, **k: {"saved": 1, "errors": 0, "error_messages": []})

        stats = dashboard.enrich_thin_profiles_for_batch([thin], api_key="test-key", db_client=None)

        assert stats["enriched"] == 1
        assert dashboard._THIN_LAST_ENRICH_ATTEMPT_KEY in thin["raw_crustdata"]

    def test_marked_profile_not_resubmitted_within_cooldown(self, monkeypatch):
        """Simulates the SECOND screening run: the profile's raw_crustdata
        already carries a fresh marker (as if reloaded from the DB row
        saved by the first run) and is still thin. Must be skipped, not
        resubmitted."""
        from datetime import datetime as real_datetime

        url = "https://www.linkedin.com/in/thin"
        already_tried = {
            "linkedin_url": url,
            "name": "Thin Person",
            "raw_crustdata": {
                "name": "Thin Person", "skills": [], "summary": "",
                dashboard._THIN_LAST_ENRICH_ATTEMPT_KEY: real_datetime.utcnow().isoformat(),
            },
        }

        called = {"n": 0}
        monkeypatch.setattr(dashboard, "batch_enrich_profiles", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

        stats = dashboard.enrich_thin_profiles_for_batch([already_tried], api_key="test-key", db_client=None)

        assert called["n"] == 0, "must not call batch_enrich_profiles for a profile on cooldown"
        assert stats["thin_found"] == 0
        assert stats["on_cooldown"] == 1
        assert stats["on_cooldown_urls"] == [url]

    def test_marked_profile_eligible_again_after_cooldown_expires(self, monkeypatch):
        """Same profile as above, but the marker is older than
        THIN_PROFILE_REENRICH_COOLDOWN_HOURS — must be treated as thin
        and eligible for re-enrichment again."""
        from datetime import datetime as real_datetime, timedelta as real_timedelta

        url = "https://www.linkedin.com/in/thin"
        stale_timestamp = (
            real_datetime.utcnow()
            - real_timedelta(hours=dashboard.THIN_PROFILE_REENRICH_COOLDOWN_HOURS + 1)
        ).isoformat()
        expired_marker = {
            "linkedin_url": url,
            "name": "Thin Person",
            "raw_crustdata": {
                "name": "Thin Person", "skills": [], "summary": "",
                dashboard._THIN_LAST_ENRICH_ATTEMPT_KEY: stale_timestamp,
            },
        }

        def fake_batch_enrich(urls, api_key=None):
            return {
                "by_url": {urls[0]: _flat_still_thin(urls[0], "Thin Person")},
                "requested": 1, "fulfilled": 1, "unmatched": [], "rejected": [],
                "unknown": [], "unknown_diagnostics": [], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        monkeypatch.setattr(dashboard, "save_enriched_profiles_bulk", lambda *a, **k: {"saved": 1, "errors": 0, "error_messages": []})

        stats = dashboard.enrich_thin_profiles_for_batch([expired_marker], api_key="test-key", db_client=None)

        assert stats["on_cooldown"] == 0
        assert stats["thin_found"] == 1
        assert stats["enriched"] == 1

    def test_never_enriched_thin_profile_still_enriched_immediately(self, monkeypatch):
        """No marker at all (genuinely never attempted) — must never be
        held back by cooldown logic, regardless of how the gate evolves."""
        thin = _thin_profile("https://www.linkedin.com/in/never-tried")
        captured = {}

        def fake_batch_enrich(urls, api_key=None):
            captured["urls"] = urls
            return {
                "by_url": {urls[0]: _flat_enriched(urls[0], "Filled Now")},
                "requested": 1, "fulfilled": 1, "unmatched": [], "rejected": [],
                "unknown": [], "unknown_diagnostics": [], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        monkeypatch.setattr(dashboard, "save_enriched_profiles_bulk", lambda *a, **k: {"saved": 1, "errors": 0, "error_messages": []})

        stats = dashboard.enrich_thin_profiles_for_batch([thin], api_key="test-key", db_client=None)

        assert captured["urls"] == [thin["linkedin_url"]]
        assert stats["on_cooldown"] == 0
        assert stats["enriched"] == 1

    def test_result_with_skills_is_not_marked_at_all(self, monkeypatch):
        """A profile that comes back WITH skills/summary must not carry
        the cooldown marker — it's no longer thin, so there's nothing to
        remember, and stamping it anyway would be a meaningless no-op at
        best and confusing at worst."""
        thin = _thin_profile("https://www.linkedin.com/in/thin")

        def fake_batch_enrich(urls, api_key=None):
            return {
                "by_url": {urls[0]: _flat_enriched(urls[0], "Now Has Skills")},
                "requested": 1, "fulfilled": 1, "unmatched": [], "rejected": [],
                "unknown": [], "unknown_diagnostics": [], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        monkeypatch.setattr(dashboard, "save_enriched_profiles_bulk", lambda *a, **k: {"saved": 1, "errors": 0, "error_messages": []})

        stats = dashboard.enrich_thin_profiles_for_batch([thin], api_key="test-key", db_client=None)

        assert stats["enriched"] == 1
        assert dashboard._THIN_LAST_ENRICH_ATTEMPT_KEY not in thin["raw_crustdata"]

    def test_malformed_marker_treated_as_never_attempted(self):
        """An unparseable marker value must never suppress enrichment —
        ambiguity here means "attempt it", not "skip it forever"."""
        raw = {"skills": [], "summary": "", dashboard._THIN_LAST_ENRICH_ATTEMPT_KEY: "not-a-timestamp"}
        assert dashboard._thin_profile_on_cooldown(raw) is False

    def test_missing_marker_treated_as_never_attempted(self):
        assert dashboard._thin_profile_on_cooldown({"skills": [], "summary": ""}) is False
        assert dashboard._thin_profile_on_cooldown({}) is False
        assert dashboard._thin_profile_on_cooldown(None) is False


class TestSemanticSearchProfilesGetAutoFilled:
    """End-to-end proof of the actual ask: the description-search beta
    (search_people_semantic) is already live in production and never returns
    skills/summary. This confirms a profile straight out of its translator
    (semantic_profile_to_legacy_shape) — the exact shape that gets saved to
    Supabase and later loaded for screening — is correctly caught as thin
    and gets auto-filled, with no changes needed to the search itself."""

    def test_semantic_search_result_is_detected_and_filled(self, monkeypatch):
        # A real description-search result, run through the SAME translator
        # already live in production — no v2 filter-search code involved.
        raw_semantic_result = {
            "basic_profile": {"name": "Jane Doe", "headline": "Founding Engineer", "summary": ""},
            "experience": {"employment_details": {"current": [], "past": []}},
            "skills": {"professional_network_skills": []},
            "social_handles": {
                "professional_network_identifier": {"profile_url": "https://www.linkedin.com/in/janedoe"}
            },
        }
        shimmed = semantic_profile_to_legacy_shape(raw_semantic_result)
        assert shimmed["skills"] == [] and shimmed["summary"] == ""  # confirms it's genuinely thin

        # Shape it the way a saved-then-reloaded profile arrives at screening.
        profile = {
            "linkedin_url": "https://www.linkedin.com/in/janedoe",
            "name": "Jane Doe",
            "raw_crustdata": shimmed,
        }

        def fake_batch_enrich(urls, api_key=None):
            return {
                "by_url": {urls[0]: _flat_enriched(urls[0], "Jane Doe")},
                "requested": 1, "fulfilled": 1, "unmatched": [], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        monkeypatch.setattr(dashboard, "save_enriched_profiles_bulk", lambda *a, **k: {"saved": 1, "errors": 0, "error_messages": []})

        stats = dashboard.enrich_thin_profiles_for_batch([profile], api_key="test-key", db_client=None)

        assert stats["thin_found"] == 1
        assert stats["enriched"] == 1
        assert profile["raw_crustdata"]["skills"] == ["Python", "Kubernetes"]


class TestEnrichHookedInUnconditionally:
    """Regression guard (Codex review, 2026-07-20): the call to
    enrich_thin_profiles_for_batch() in the screening batch loop must NOT be
    nested inside the `if missing_before > 0:` block, or profiles that are
    already thin in memory (with nothing upstream flagging them as missing)
    silently skip enrichment. A full Streamlit-driven test of that tab isn't
    practical here, so this checks the source structure directly."""

    def test_enrich_call_is_not_nested_inside_missing_before_block(self):
        import inspect

        lines = inspect.getsource(dashboard).splitlines()

        if_line_no = next(i for i, l in enumerate(lines) if "if missing_before > 0:" in l)
        call_line_no = next(i for i, l in enumerate(lines) if "enrich_thin_profiles_for_batch(" in l and "def " not in l)
        assert call_line_no > if_line_no, "expected the enrich call to appear after the fetch guard"

        if_indent = len(lines[if_line_no]) - len(lines[if_line_no].lstrip())

        # Find where the `if missing_before > 0:` block actually ends: the
        # first subsequent non-blank line whose indentation is <= the if's
        # own indentation (i.e. a dedent back out of the block).
        block_end_line_no = len(lines)
        for i in range(if_line_no + 1, len(lines)):
            stripped = lines[i].strip()
            if not stripped:
                continue
            indent = len(lines[i]) - len(lines[i].lstrip())
            if indent <= if_indent:
                block_end_line_no = i
                break

        assert call_line_no >= block_end_line_no, (
            "enrich_thin_profiles_for_batch() call appears to be nested "
            "inside `if missing_before > 0:` — it must run unconditionally, "
            "not gated behind the raw-data fetch step reporting something missing"
        )
