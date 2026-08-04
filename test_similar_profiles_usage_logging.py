"""Tests for dashboard.log_similar_profiles_crustdata_usage() — the
boundary function that logs Crustdata usage for the "Find Similar
Profiles" tab's on-the-fly enrich call.

Added 2026-08-04 (Codex adversarial review of PR #127, MEDIUM finding):
this call site spent real Crustdata credits with zero usage record,
defeating the point of a cost-control PR. Rather than threading a
UsageTracker parameter through similar_profiles.search_similar()/
get_or_build_query_embedding() (out-of-scope signature churn), the fix
logs at the dashboard boundary: similar_profiles.SimilarProfileError now
carries crustdata_attempted/crustdata_fulfilled/crustdata_error
attributes (see that class's docstring), and this function reads them.

No real network calls or Streamlit context needed — this is a plain
function taking a fake tracker.
"""

import dashboard
from similar_profiles import SimilarProfileError


class FakeTracker:
    def __init__(self):
        self.calls = []

    def log_crustdata_sync_enrich(self, requested, fulfilled, status='success', error_message=None, response_time_ms=None):
        self.calls.append({
            'requested': requested, 'fulfilled': fulfilled, 'status': status,
            'error_message': error_message,
        })


class TestLogSimilarProfilesCrustdataUsage:
    def test_no_tracker_is_a_safe_noop(self):
        # Must not raise even with source="enriched" and no tracker.
        dashboard.log_similar_profiles_crustdata_usage(None, source="enriched")
        dashboard.log_similar_profiles_crustdata_usage(None, error=SimilarProfileError("x", crustdata_attempted=True))

    def test_source_enriched_logs_one_fulfilled_credit(self):
        tracker = FakeTracker()
        dashboard.log_similar_profiles_crustdata_usage(tracker, source="enriched")
        assert tracker.calls == [{'requested': 1, 'fulfilled': 1, 'status': 'success', 'error_message': None}]

    def test_source_cached_logs_nothing(self):
        tracker = FakeTracker()
        dashboard.log_similar_profiles_crustdata_usage(tracker, source="cached")
        assert tracker.calls == []

    def test_source_embedded_logs_nothing(self):
        tracker = FakeTracker()
        dashboard.log_similar_profiles_crustdata_usage(tracker, source="embedded")
        assert tracker.calls == []

    def test_error_not_attempted_logs_nothing(self):
        """No API key configured -> SimilarProfileError with
        crustdata_attempted=False (the default) -> nothing to log."""
        tracker = FakeTracker()
        err = SimilarProfileError("no key provided")
        dashboard.log_similar_profiles_crustdata_usage(tracker, error=err)
        assert tracker.calls == []

    def test_error_attempted_no_match_logs_zero_credits_as_success(self):
        tracker = FakeTracker()
        err = SimilarProfileError(
            "Crustdata couldn't find this LinkedIn profile.",
            crustdata_attempted=True, crustdata_fulfilled=False, crustdata_error=None,
        )
        dashboard.log_similar_profiles_crustdata_usage(tracker, error=err)
        assert tracker.calls == [{'requested': 1, 'fulfilled': 0, 'status': 'success', 'error_message': None}]

    def test_error_attempted_transport_failure_logs_as_error(self):
        tracker = FakeTracker()
        err = SimilarProfileError(
            "Crustdata enrichment failed: boom",
            crustdata_attempted=True, crustdata_fulfilled=False, crustdata_error="boom",
        )
        dashboard.log_similar_profiles_crustdata_usage(tracker, error=err)
        assert tracker.calls == [{'requested': 1, 'fulfilled': 0, 'status': 'error', 'error_message': 'boom'}]

    def test_error_attempted_and_fulfilled_logs_one_credit_despite_downstream_failure(self):
        """RPC failure after a successful enrich — see
        test_similar_profiles_auto_enrich.py's
        test_rpc_failure_after_successful_enrich_tags_fulfilled_true for
        where this attribute combination comes from."""
        tracker = FakeTracker()
        err = SimilarProfileError(
            "RPC unavailable",
            crustdata_attempted=True, crustdata_fulfilled=True, crustdata_error=None,
        )
        dashboard.log_similar_profiles_crustdata_usage(tracker, error=err)
        assert tracker.calls == [{'requested': 1, 'fulfilled': 1, 'status': 'success', 'error_message': None}]

    def test_plain_exception_without_crustdata_attributes_logs_nothing(self):
        """A non-SimilarProfileError (or a SimilarProfileError-like object
        with no crustdata_attempted attribute at all) must not crash —
        getattr(..., False) makes this a safe no-op."""
        tracker = FakeTracker()
        dashboard.log_similar_profiles_crustdata_usage(tracker, error=RuntimeError("unexpected"))
        assert tracker.calls == []
