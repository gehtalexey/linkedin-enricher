"""Tests for the cost fix on dashboard.enrich_batch() — the "Enrich N
profiles" button (Upload File tab -> Enrich with Crustdata), rewired
2026-08-04 to call crustdata_search.batch_enrich_profiles() (POST
/batch/person/enrich, 1 credit/profile base) instead of the legacy GET
/screener/person/enrich (flat 3 credits/profile). This is the button that
spent 3,537 credits on 1,179 profiles the morning this fix was written; on
the cheap path the same run costs 1,179.

Also covers usage_tracker.UsageTracker.log_crustdata_sync_enrich() — the
new logging method for the sync single-profile call site (dashboard's
Re-Enrich Profile button and similar_profiles._crustdata_enrich both route
through crustdata_search.sync_enrich_profile(), tested directly in
test_crustdata_sync_enrich.py).

No real network calls: dashboard.batch_enrich_profiles is monkeypatched at
the dashboard module level, same pattern as
test_enrich_thin_profiles_integration.py.
"""

import dashboard
from usage_tracker import UsageTracker, CRUSTDATA_PRICING, CRUSTDATA_PRICING_V2_ENRICH


class FakeTracker:
    """Records log_crustdata_batch_enrich()/log_crustdata() calls without a
    real db_client, so tests can assert exactly what got logged."""

    def __init__(self):
        self.batch_enrich_calls = []
        self.legacy_calls = []

    def log_crustdata_batch_enrich(self, requested, fulfilled, status='success',
                                    error_message=None, response_time_ms=None):
        self.batch_enrich_calls.append({
            'requested': requested, 'fulfilled': fulfilled, 'status': status,
            'error_message': error_message,
        })

    def log_crustdata(self, profiles_enriched, status='success', error_message=None,
                       response_time_ms=None):
        # Must never be called by the rewired path — it hardcodes the wrong
        # (3 credits/profile) rate for this endpoint.
        self.legacy_calls.append({'profiles_enriched': profiles_enriched, 'status': status})


def _flat(url, name="Person"):
    return {
        "name": name,
        "linkedin_flagship_url": url,
        "linkedin_url": url,
        "skills": ["Python"],
        "current_employers": [],
        "past_employers": [],
        "all_employers": [],
        "all_titles": [],
        "all_schools": [],
    }


class TestEnrichBatchCheapRewire:
    def test_all_matched_returns_flat_shape_with_original_url(self, monkeypatch):
        urls = ["https://www.linkedin.com/in/a", "https://www.linkedin.com/in/b"]
        captured = {}

        def fake_batch_enrich(u, api_key=None):
            captured['urls'] = u
            captured['api_key'] = api_key
            return {
                "by_url": {u[0]: _flat(u[0], "A"), u[1]: _flat(u[1], "B")},
                "requested": 2, "fulfilled": 2, "unmatched": [], "credits_used": 2,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        tracker = FakeTracker()

        result = dashboard.enrich_batch(urls, "test-key", tracker=tracker)

        assert captured['urls'] == urls
        assert captured['api_key'] == "test-key"
        assert len(result) == 2
        assert all('error' not in r for r in result)
        assert result[0]['_original_url'] == urls[0]
        assert result[1]['_original_url'] == urls[1]
        assert result[0]['name'] == "A"
        assert result[0]['linkedin_flagship_url'] == urls[0]

        # Correct operation/credits logged — NOT the legacy 3x-overstated path.
        assert tracker.legacy_calls == []
        assert tracker.batch_enrich_calls == [
            {'requested': 2, 'fulfilled': 2, 'status': 'success', 'error_message': None}
        ]

    def test_unmatched_url_becomes_error_dict_with_linkedin_url_key(self, monkeypatch):
        urls = ["https://www.linkedin.com/in/found", "https://www.linkedin.com/in/missing"]

        def fake_batch_enrich(u, api_key=None):
            return {
                "by_url": {u[0]: _flat(u[0])},
                "requested": 2, "fulfilled": 1, "unmatched": [u[1]], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        tracker = FakeTracker()

        result = dashboard.enrich_batch(urls, "test-key", tracker=tracker)

        errors = [r for r in result if 'error' in r]
        successes = [r for r in result if 'error' not in r]
        assert len(errors) == 1
        assert len(successes) == 1
        # Downstream code (dashboard.py ~7874) reads _e.get('linkedin_url')
        # for failed-enrichment bookkeeping — must still be present.
        assert errors[0]['linkedin_url'] == urls[1]
        assert tracker.batch_enrich_calls == [
            {'requested': 2, 'fulfilled': 1, 'status': 'success', 'error_message': None}
        ]

    def test_batch_enrich_profiles_exception_returns_error_per_url_and_logs_error(self, monkeypatch):
        urls = ["https://www.linkedin.com/in/a", "https://www.linkedin.com/in/b"]

        def boom(u, api_key=None):
            raise RuntimeError("Crustdata is temporarily unavailable")

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", boom)
        tracker = FakeTracker()

        result = dashboard.enrich_batch(urls, "test-key", tracker=tracker)

        assert len(result) == 2
        assert all('error' in r for r in result)
        assert all(r['linkedin_url'] == u for r, u in zip(result, urls))
        assert tracker.legacy_calls == []
        assert len(tracker.batch_enrich_calls) == 1
        assert tracker.batch_enrich_calls[0]['status'] == 'error'
        assert tracker.batch_enrich_calls[0]['requested'] == 2
        assert tracker.batch_enrich_calls[0]['fulfilled'] == 0

    def test_matching_falls_back_to_normalized_url(self, monkeypatch):
        """by_url may be keyed on a normalized form of the input URL (e.g.
        a trailing slash difference) — batch_enrich_profiles() already
        double-writes both raw and normalized keys, but enrich_batch()
        must not assume an exact string match either."""
        raw_url = "https://www.linkedin.com/in/trailing-slash/"

        def fake_batch_enrich(u, api_key=None):
            norm = dashboard.normalize_linkedin_url(u[0])
            return {
                "by_url": {norm: _flat(norm)},
                "requested": 1, "fulfilled": 1, "unmatched": [], "credits_used": 1,
                "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)

        result = dashboard.enrich_batch([raw_url], "test-key", tracker=None)

        assert len(result) == 1
        assert 'error' not in result[0]
        assert result[0]['_original_url'] == raw_url


class TestEnrichBatchUnknownAndRejectedHandling:
    """Codex adversarial review of PR #127, round 2 (2026-08-04, HIGH):
    batch_enrich_profiles() can now return `unknown` (job accepted but
    outcome unreadable — may have already billed) and `rejected` (data
    came back but failed identity/content validation) alongside the
    existing `unmatched`. dashboard.enrich_batch() must not crash on
    these new keys, must produce a distinct error message per case, and
    — critically — must never log an `unknown` batch as a clean,
    zero-credit success."""

    def test_unknown_url_gets_distinct_error_message_and_error_log(self, monkeypatch):
        urls = ["https://www.linkedin.com/in/a"]

        def fake_batch_enrich(u, api_key=None):
            return {
                "by_url": {}, "requested": 1, "fulfilled": 0,
                "unmatched": [], "rejected": [], "unknown": [u[0]],
                "unknown_diagnostics": [{"batch_id": "b1", "reason": "poll_timeout"}],
                "credits_used": 0, "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        tracker = FakeTracker()

        result = dashboard.enrich_batch(urls, "test-key", tracker=tracker)

        assert len(result) == 1
        assert 'error' in result[0]
        assert 'unknown' in result[0]['error'].lower()
        assert result[0]['linkedin_url'] == urls[0]

        # Must be logged as an ERROR, never a clean success — a job we
        # lost track of may have already consumed real credits.
        assert len(tracker.batch_enrich_calls) == 1
        assert tracker.batch_enrich_calls[0]['status'] == 'error'
        assert 'unknown' in tracker.batch_enrich_calls[0]['error_message'].lower()
        assert 'b1' in tracker.batch_enrich_calls[0]['error_message']

    def test_rejected_url_gets_distinct_error_message_but_normal_success_log(self, monkeypatch):
        """Rejected (identity/content validation failure) is a CONFIRMED
        outcome — Crustdata answered, we just don't trust the answer — so
        unlike `unknown` it doesn't need to flip the log to 'error'."""
        urls = ["https://www.linkedin.com/in/a"]

        def fake_batch_enrich(u, api_key=None):
            return {
                "by_url": {}, "requested": 1, "fulfilled": 0,
                "unmatched": [], "rejected": [u[0]], "unknown": [],
                "unknown_diagnostics": [], "credits_used": 0, "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        tracker = FakeTracker()

        result = dashboard.enrich_batch(urls, "test-key", tracker=tracker)

        assert 'error' in result[0]
        assert 'validation' in result[0]['error'].lower()
        assert tracker.batch_enrich_calls[0]['status'] == 'success'

    def test_mixed_matched_unknown_and_unmatched_each_handled_distinctly(self, monkeypatch):
        urls = [
            "https://www.linkedin.com/in/matched",
            "https://www.linkedin.com/in/unknown-one",
            "https://www.linkedin.com/in/truly-missing",
        ]

        def fake_batch_enrich(u, api_key=None):
            return {
                "by_url": {u[0]: _flat(u[0], "Matched")},
                "requested": 3, "fulfilled": 1,
                "unmatched": [u[2]], "rejected": [], "unknown": [u[1]],
                "unknown_diagnostics": [{"batch_id": "b1", "reason": "download_error"}],
                "credits_used": 1, "batch_ids": ["b1"],
            }

        monkeypatch.setattr(dashboard, "batch_enrich_profiles", fake_batch_enrich)
        tracker = FakeTracker()

        result = dashboard.enrich_batch(urls, "test-key", tracker=tracker)

        by_input_url = {r.get('linkedin_url') or r.get('_original_url'): r for r in result}
        assert 'error' not in by_input_url[urls[0]]
        assert 'unknown' in by_input_url[urls[1]]['error'].lower()
        assert by_input_url[urls[2]]['error'] == 'Not found via Crustdata batch enrich'

        # Any unknown at all flips the whole batch's log to error, even
        # though most of it resolved cleanly.
        assert tracker.batch_enrich_calls[0]['status'] == 'error'
        assert tracker.batch_enrich_calls[0]['fulfilled'] == 1


class TestLogCrustdataSyncEnrich:
    """The new usage_tracker method for the sync single-profile call sites
    (dashboard's Re-Enrich Profile button, similar_profiles._crustdata_enrich)."""

    def test_logs_correct_credits_and_operation_on_match(self):
        captured = []

        class FakeDbClient:
            def insert(self, table, data):
                captured.append((table, data))
                return [data]

        tracker = UsageTracker(db_client=FakeDbClient())
        tracker.log_crustdata_sync_enrich(requested=1, fulfilled=1, status='success')

        assert len(captured) == 1
        table, data = captured[0]
        assert table == 'api_usage_logs'
        assert data['provider'] == 'crustdata'
        assert data['operation'] == 'sync_enrich'
        # 1 credit base, NOT the legacy 3-credit rate.
        assert data['credits_used'] == 1 * CRUSTDATA_PRICING_V2_ENRICH['credits_per_profile_base']
        assert data['credits_used'] == 1
        assert data['cost_usd'] == 1 * CRUSTDATA_PRICING['cost_per_credit']

    def test_no_match_bills_zero_credits(self):
        captured = []

        class FakeDbClient:
            def insert(self, table, data):
                captured.append(data)
                return [data]

        tracker = UsageTracker(db_client=FakeDbClient())
        tracker.log_crustdata_sync_enrich(requested=1, fulfilled=0, status='success')

        assert captured[0]['credits_used'] == 0
        assert captured[0]['metadata']['unmatched'] == 1

    def test_error_status_still_logs_operation_name(self):
        captured = []

        class FakeDbClient:
            def insert(self, table, data):
                captured.append(data)
                return [data]

        tracker = UsageTracker(db_client=FakeDbClient())
        tracker.log_crustdata_sync_enrich(
            requested=1, fulfilled=0, status='error', error_message='boom',
        )

        assert captured[0]['operation'] == 'sync_enrich'
        assert captured[0]['status'] == 'error'
        assert captured[0]['error_message'] == 'boom'
