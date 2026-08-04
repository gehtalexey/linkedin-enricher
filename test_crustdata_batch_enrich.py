"""Tests for the new v2025-11-01 batch enrichment pipeline:
submit_batch_enrich -> get_batch_status -> batch_enrich_profiles
(submit/poll/download orchestrator) -> enrich_profile_to_legacy_shape
(nested v2 -> flat legacy translator).

This is the fix for the new Crustdata search endpoints not returning
skills/summary: fill them back in, right before AI Screen, via
POST /batch/person/enrich (up to 10,000 URLs/job, 1 credit/profile base —
see crustdata_search.py's module docstring section for the full context).

No real network calls. The translator field mapping is verified against a
REAL /person/enrich response captured live 2026-07-20 (same nested shape
the batch endpoint returns) — see _REAL_ENRICH_RESPONSE below.
"""

from unittest.mock import MagicMock, patch

import pytest

import gzip
import json

from crustdata_search import (
    submit_batch_enrich,
    get_batch_status,
    batch_enrich_profiles,
    enrich_profile_to_legacy_shape,
    _download_batch_results,
    _status_fulfilled_hint,
    _submit_definitely_never_started,
    CRUSTDATA_BATCH_ENRICH_ENDPOINT,
    CRUSTDATA_API_VERSION,
)


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    resp.headers = {}
    return resp


# ---------------------------------------------------------------------------
# submit_batch_enrich
# ---------------------------------------------------------------------------


class TestSubmitBatchEnrich:
    def test_sends_bearer_auth_and_version_header(self):
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data={"batch_id": "b1"})
            submit_batch_enrich(["https://www.linkedin.com/in/foo"], api_key="test-key")
            headers = mock_post.call_args.kwargs["headers"]
            assert headers["Authorization"] == "Bearer test-key"
            assert headers["x-api-version"] == CRUSTDATA_API_VERSION

    def test_posts_to_batch_enrich_endpoint_with_urls_and_fields(self):
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data={"batch_id": "b1"})
            urls = ["https://www.linkedin.com/in/foo", "https://www.linkedin.com/in/bar"]
            submit_batch_enrich(urls, api_key="test-key")

            args, kwargs = mock_post.call_args
            assert args[0] == CRUSTDATA_BATCH_ENRICH_ENDPOINT
            assert kwargs["json"]["professional_network_profile_urls"] == urls
            assert "basic_profile" in kwargs["json"]["fields"]
            assert "skills" in kwargs["json"]["fields"]

    def test_chunk_size_clamped_10_to_1000(self):
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data={"batch_id": "b1"})
            submit_batch_enrich(["https://www.linkedin.com/in/foo"], chunk_size=5, api_key="test-key")
            assert mock_post.call_args.kwargs["json"]["chunk_size"] == 10

            submit_batch_enrich(["https://www.linkedin.com/in/foo"], chunk_size=5000, api_key="test-key")
            assert mock_post.call_args.kwargs["json"]["chunk_size"] == 1000

    def test_over_10000_urls_raises_without_calling_api(self):
        with patch("crustdata_search.requests.post") as mock_post:
            with pytest.raises(ValueError):
                submit_batch_enrich(["u"] * 10001, api_key="test-key")
            mock_post.assert_not_called()

    def test_empty_list_raises_without_calling_api(self):
        with patch("crustdata_search.requests.post") as mock_post:
            with pytest.raises(ValueError):
                submit_batch_enrich([], api_key="test-key")
            mock_post.assert_not_called()

    def test_returns_batch_id_from_either_key_name(self):
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data={"id": "b2"})
            batch_id = submit_batch_enrich(["https://www.linkedin.com/in/foo"], api_key="test-key")
            assert batch_id == "b2"

    def test_401_raises_authentication_error(self):
        from error_handling import AuthenticationError

        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(status_code=401)
            with pytest.raises(AuthenticationError):
                submit_batch_enrich(["https://www.linkedin.com/in/foo"], api_key="bad-key")


class TestGetBatchStatus:
    def test_gets_batch_status_url(self):
        with patch("crustdata_search.requests.get") as mock_get:
            mock_get.return_value = _mock_response(json_data={"status": "processing"})
            get_batch_status("b1", api_key="test-key")
            args, kwargs = mock_get.call_args
            assert args[0].endswith("/batch/b1")
            assert kwargs["headers"]["x-api-version"] == CRUSTDATA_API_VERSION


# ---------------------------------------------------------------------------
# batch_enrich_profiles — submit/poll/download orchestrator
# ---------------------------------------------------------------------------


def _enrich_record(url, name="Jane Doe"):
    return {
        "original_identifier": url,
        "internal_id": 1,
        "data": {
            "basic_profile": {"name": name, "current_title": "Engineer", "headline": "", "summary": "Builds things."},
            "experience": {"employment_details": {"current": [], "past": []}},
            "education": {"schools": []},
            "skills": {"professional_network_skills": ["Python"]},
            "social_handles": {"professional_network_identifier": {"profile_url": url}},
        },
    }


class TestBatchEnrichProfilesOrchestrator:
    def test_happy_path_all_matched(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        urls = [
            "https://www.linkedin.com/in/a",
            "https://www.linkedin.com/in/b",
            "https://www.linkedin.com/in/c",
        ]
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1") as mock_submit, \
             patch("crustdata_search.get_batch_status") as mock_status, \
             patch("crustdata_search._download_batch_results") as mock_download:
            mock_status.return_value = {"status": "completed"}
            mock_download.return_value = [_enrich_record(u) for u in urls]

            result = batch_enrich_profiles(urls, api_key="test-key")

        assert result["requested"] == 3
        assert result["fulfilled"] == 3
        assert result["unmatched"] == []
        assert result["credits_used"] == 3
        assert set(result["by_url"].keys()) >= set(urls)
        for u in urls:
            assert result["by_url"][u]["skills"] == ["Python"]
        mock_submit.assert_called_once()

    def test_partial_no_match_lands_in_unmatched(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        urls = ["https://www.linkedin.com/in/a", "https://www.linkedin.com/in/b", "https://www.linkedin.com/in/c"]
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status") as mock_status, \
             patch("crustdata_search._download_batch_results") as mock_download:
            mock_status.return_value = {"status": "completed", "entities_requested": 3, "entities_fulfilled": 2}
            mock_download.return_value = [_enrich_record(urls[0]), _enrich_record(urls[1])]

            result = batch_enrich_profiles(urls, api_key="test-key")

        assert result["fulfilled"] == 2
        assert result["unmatched"] == [urls[2]]
        assert result["credits_used"] == 2
        # Never raises over a partial no-match — screening proceeds with
        # whatever data is available (Alexey's decision, 2026-07-20).

    def test_poll_timeout_returns_done_so_far_no_exception(self, monkeypatch):
        """A job accepted by Crustdata that never reaches a terminal status
        within max_wait_s is UNKNOWN billing, not a genuine no-match (Codex
        review, PR #127, round 2, 2026-08-04) — it may already be running
        (and billing) on Crustdata's side. Still doesn't raise (screening
        proceeds as-is), but must not be reported as free/successful."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        urls = ["https://www.linkedin.com/in/a"]
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status") as mock_status:
            mock_status.return_value = {"status": "processing"}  # never terminal

            result = batch_enrich_profiles(urls, api_key="test-key", poll_interval_s=1, max_wait_s=3)

        assert result["fulfilled"] == 0
        assert result["unmatched"] == []  # NOT reported as a genuine no-match
        assert result["unknown"] == urls
        assert len(result["unknown_diagnostics"]) == 1
        assert result["unknown_diagnostics"][0]["reason"] == "poll_timeout"
        assert result["unknown_diagnostics"][0]["batch_id"] == "batch1"

    def test_over_10000_urls_splits_into_multiple_jobs(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        urls = [f"https://www.linkedin.com/in/u{i}" for i in range(10001)]
        with patch("crustdata_search.submit_batch_enrich", return_value="batchX") as mock_submit, \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[]):
            batch_enrich_profiles(urls, api_key="test-key")

        assert mock_submit.call_count == 2
        first_call_urls = mock_submit.call_args_list[0].args[0]
        assert len(first_call_urls) == 10000

    def test_submit_failure_unrecognized_exception_is_unknown_not_unmatched(self, monkeypatch):
        """Codex review, PR #127, round 3: a generic/unrecognized submit
        failure is AMBIGUOUS (we can't confirm Crustdata never received
        the request) — must land in `unknown`, not be silently reported
        as a free no-match. See TestSubmitBatchEnrichFailureClassification
        for the full ambiguous-vs-unambiguous matrix."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        urls = ["https://www.linkedin.com/in/a"]
        with patch("crustdata_search.submit_batch_enrich", side_effect=Exception("boom")):
            result = batch_enrich_profiles(urls, api_key="test-key")

        assert result["fulfilled"] == 0
        assert result["unmatched"] == []
        assert result["unknown"] == urls
        assert result["unknown_diagnostics"][0]["reason"] == "submit_error"
        assert result["unknown_diagnostics"][0]["batch_id"] is None

    def test_submit_failure_explicit_rejection_is_unmatched_not_unknown(self, monkeypatch):
        """A confidently-classified "job never started" submit failure
        (e.g. auth rejection) stays a genuine, free no-match."""
        from error_handling import AuthenticationError
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        urls = ["https://www.linkedin.com/in/a"]
        with patch("crustdata_search.submit_batch_enrich", side_effect=AuthenticationError("Crustdata")):
            result = batch_enrich_profiles(urls, api_key="test-key")

        assert result["fulfilled"] == 0
        assert result["unmatched"] == urls
        assert result["unknown"] == []
        assert result["credits_used"] == 0

    def test_empty_input_returns_zeroed_result_without_any_call(self):
        with patch("crustdata_search.submit_batch_enrich") as mock_submit:
            result = batch_enrich_profiles([], api_key="test-key")
        mock_submit.assert_not_called()
        assert result == {
            "by_url": {}, "requested": 0, "fulfilled": 0, "unmatched": [],
            "rejected": [], "unknown": [], "unknown_diagnostics": [],
            "credits_used": 0, "batch_ids": [],
        }


class TestBatchEnrichProfilesDedup:
    """Codex review, PR #127, 2026-08-04: a caller-supplied list containing
    the same URL twice was submitted to Crustdata unchanged (paying for it
    twice) while requested/fulfilled/credits_used were derived from a
    de-duplicated set — silently UNDER-counting the true spend. Must
    dedupe by normalized URL BEFORE submitting, while still resolving
    every original duplicate position via by_url."""

    def test_verbatim_duplicate_submitted_only_once(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/dupe"
        urls = [url, url]
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1") as mock_submit, \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[_enrich_record(url)]):
            result = batch_enrich_profiles(urls, api_key="test-key")

        submitted_urls = mock_submit.call_args.args[0]
        assert submitted_urls == [url]  # sent once, not twice
        assert result["requested"] == 1
        assert result["fulfilled"] == 1
        assert result["credits_used"] == 1  # NOT 2 — this is the actual bill

    def test_by_url_resolves_at_every_duplicate_input_position(self, monkeypatch):
        """Even though only one representative gets submitted, a caller
        indexing results against their own (duplicate-containing) input
        list must still get a hit at every position — same contract
        dashboard.enrich_batch() relies on."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/dupe"
        urls = [url, url, url]
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[_enrich_record(url)]):
            result = batch_enrich_profiles(urls, api_key="test-key")

        for input_url in urls:
            assert result["by_url"].get(input_url) is not None
            assert result["by_url"][input_url]["skills"] == ["Python"]

    def test_different_raw_strings_same_normalized_identity_dedup_together(self, monkeypatch):
        """A trailing slash (or other cosmetic difference) still counts as
        the same profile for billing purposes, even though the two input
        strings aren't byte-identical."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        bare = "https://www.linkedin.com/in/dupe"
        with_slash = "https://www.linkedin.com/in/dupe/"
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1") as mock_submit, \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[_enrich_record(bare)]):
            result = batch_enrich_profiles([bare, with_slash], api_key="test-key")

        assert mock_submit.call_args.args[0] == [bare]  # only the first-seen form submitted
        assert result["requested"] == 1
        assert result["credits_used"] == 1
        assert result["by_url"].get(bare) is not None
        assert result["by_url"].get(with_slash) is not None

    def test_duplicate_among_otherwise_distinct_urls_only_that_one_deduped(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        a = "https://www.linkedin.com/in/a"
        b = "https://www.linkedin.com/in/b"
        urls = [a, b, a]  # a appears twice
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1") as mock_submit, \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[_enrich_record(a), _enrich_record(b)]):
            result = batch_enrich_profiles(urls, api_key="test-key")

        submitted_urls = mock_submit.call_args.args[0]
        assert submitted_urls == [a, b]  # a submitted once, b once — 2 total, not 3
        assert result["requested"] == 2
        assert result["fulfilled"] == 2
        assert result["credits_used"] == 2
        assert result["by_url"][a]["name"] == "Jane Doe"
        assert result["by_url"][b]["name"] == "Jane Doe"

    def test_duplicate_url_with_no_match_reports_unmatched_once(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/ghost"
        urls = [url, url]
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[]):
            result = batch_enrich_profiles(urls, api_key="test-key")

        assert result["requested"] == 1
        assert result["fulfilled"] == 0
        assert result["unmatched"] == [url]
        assert result["credits_used"] == 0


class TestBatchEnrichProfilesIdentityValidation:
    """Codex adversarial review of PR #127, round 2 (2026-08-04, HIGH):
    round 1 hardened only the sync path's match selection
    (_select_person_data_from_matches -> _is_valid_person_data). The batch
    path trusted any non-empty `data` payload that echoed the right
    `original_identifier`, without ever checking the payload's OWN claimed
    LinkedIn URL against what was actually requested for it — the exact
    wrong-person risk round 1 fixed for sync, unfixed here. Batch is the
    highest-volume call site (the "Enrich N profiles" button), so this is
    real exposure, not a theoretical one."""

    def _record_with_url(self, identifier, data_url, name="Someone", has_content=True):
        data = {
            "basic_profile": {"name": name if has_content else "", "current_title": "Engineer", "headline": "", "summary": ""},
            "experience": {"employment_details": {"current": [], "past": []}},
            "education": {"schools": []},
            "skills": {"professional_network_skills": ["Python"] if has_content else []},
            "social_handles": {"professional_network_identifier": {"profile_url": data_url}},
        }
        return {"original_identifier": identifier, "internal_id": 1, "data": data}

    def test_identifier_echoed_but_payload_url_is_a_different_person_rejected(self, monkeypatch):
        """The exact Codex regression case: original_identifier says URL A,
        but data.social_handles...profile_url says URL B. Must not map,
        must not count fulfilled."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url_a = "https://www.linkedin.com/in/person-a"
        url_b = "https://www.linkedin.com/in/person-b"
        record = self._record_with_url(identifier=url_a, data_url=url_b, name="Wrong Person")

        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[record]):
            result = batch_enrich_profiles([url_a], api_key="test-key")

        assert result["by_url"].get(url_a) is None
        assert result["fulfilled"] == 0
        assert result["credits_used"] == 0
        assert result["rejected"] == [url_a]
        assert result["unmatched"] == []  # distinct from a genuine no-match

    def test_empty_shell_data_rejected_not_fulfilled(self, monkeypatch):
        """{"basic_profile": {}}-style empty shell, but wrapped with a
        matching original_identifier — content gate must still catch it."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/ghost-shell"
        record = self._record_with_url(identifier=url, data_url=url, has_content=False)

        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[record]):
            result = batch_enrich_profiles([url], api_key="test-key")

        assert result["by_url"].get(url) is None
        assert result["fulfilled"] == 0
        assert result["rejected"] == [url]

    def test_valid_matching_record_still_fulfilled(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/real-person"
        record = self._record_with_url(identifier=url, data_url=url, name="Real Person")

        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[record]):
            result = batch_enrich_profiles([url], api_key="test-key")

        assert result["by_url"][url]["name"] == "Real Person"
        assert result["fulfilled"] == 1
        assert result["rejected"] == []
        assert result["credits_used"] == 1

    def test_record_with_no_original_identifier_is_uncounted_not_trusted(self, monkeypatch):
        """No independent identity to verify against at all — must not be
        trusted on its own say-so, and (since we can't attribute it to any
        requested identity) it's simply uncounted rather than force-mapped
        into rejected or unmatched."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/no-identifier"
        record = {
            "internal_id": 1,
            "data": {
                "basic_profile": {"name": "Someone"},
                "skills": {"professional_network_skills": ["Python"]},
                "social_handles": {"professional_network_identifier": {"profile_url": url}},
            },
        }  # no "original_identifier" key at all

        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[record]):
            result = batch_enrich_profiles([url], api_key="test-key")

        assert result["by_url"].get(url) is None
        assert result["fulfilled"] == 0
        # Falls through to unmatched (nothing usable was ever mapped for it).
        assert result["unmatched"] == [url]

    def test_record_echoing_unrequested_identifier_is_ignored(self, monkeypatch):
        """A record whose original_identifier doesn't correspond to
        anything we actually submitted must not pollute by_url or counts."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        requested_url = "https://www.linkedin.com/in/requested"
        stray_url = "https://www.linkedin.com/in/never-asked-for"
        stray_record = self._record_with_url(identifier=stray_url, data_url=stray_url, name="Stray")

        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[stray_record]):
            result = batch_enrich_profiles([requested_url], api_key="test-key")

        assert result["by_url"] == {}
        assert result["fulfilled"] == 0
        assert result["unmatched"] == [requested_url]

    def test_mixed_batch_valid_rejected_and_unmatched_counted_distinctly(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        valid_url = "https://www.linkedin.com/in/valid"
        wrong_person_url = "https://www.linkedin.com/in/wrong-person-requested"
        wrong_person_actual = "https://www.linkedin.com/in/wrong-person-actual"
        no_match_url = "https://www.linkedin.com/in/no-match"

        records = [
            self._record_with_url(identifier=valid_url, data_url=valid_url, name="Valid"),
            self._record_with_url(identifier=wrong_person_url, data_url=wrong_person_actual, name="Impostor"),
            # no_match_url gets no record at all
        ]

        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=records):
            result = batch_enrich_profiles([valid_url, wrong_person_url, no_match_url], api_key="test-key")

        assert result["requested"] == 3
        assert result["fulfilled"] == 1
        assert result["by_url"][valid_url]["name"] == "Valid"
        assert result["rejected"] == [wrong_person_url]
        assert result["unmatched"] == [no_match_url]
        assert result["credits_used"] == 1


class TestStatusFulfilledHint:
    def test_known_key_extracted(self):
        assert _status_fulfilled_hint({"entities_fulfilled": 7}) == 7
        assert _status_fulfilled_hint({"fulfilled_count": 3}) == 3
        assert _status_fulfilled_hint({"fulfilled": 5}) == 5

    def test_first_matching_key_wins(self):
        assert _status_fulfilled_hint({"entities_fulfilled": 7, "fulfilled": 99}) == 7

    def test_no_known_key_returns_none(self):
        assert _status_fulfilled_hint({"status": "processing"}) is None

    def test_non_numeric_value_ignored(self):
        assert _status_fulfilled_hint({"entities_fulfilled": "seven"}) is None

    def test_bool_value_ignored(self):
        """bool is technically an int subclass in Python — must not be
        mistaken for a real count."""
        assert _status_fulfilled_hint({"entities_fulfilled": True}) is None

    def test_non_dict_payload_returns_none(self):
        assert _status_fulfilled_hint(None) is None
        assert _status_fulfilled_hint("not a dict") is None


class TestBatchEnrichProfilesUnknownBilling:
    """Codex adversarial review of PR #127, round 2 (2026-08-04, HIGH):
    a job accepted by Crustdata (and possibly already billing) whose
    outcome we then fail to read — a poll error, a poll timeout, or a
    download failure after the job completed — was previously collapsed
    into `records = []`, reported as `fulfilled=0` and logged as a clean,
    free, successful run. That hides real spend in exactly the direction
    that flatters the whole point of this PR. These cases must land in
    the distinct `unknown` bucket, never silently treated as a genuine
    no-match."""

    def test_polling_error_after_acceptance_is_unknown_not_fulfilled_zero(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/a"
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", side_effect=Exception("network blip")):
            result = batch_enrich_profiles([url], api_key="test-key")

        assert result["fulfilled"] == 0
        assert result["unmatched"] == []  # NOT a genuine no-match
        assert result["unknown"] == [url]
        assert result["unknown_diagnostics"][0]["reason"] == "poll_error"
        assert result["unknown_diagnostics"][0]["batch_id"] == "batch1"
        assert result["credits_used"] == 0  # conservative — never guessed positive

    def test_download_failure_after_completed_job_is_unknown(self, monkeypatch):
        """The job reached a terminal 'completed' status (Crustdata's side
        is done, plausibly billed) but we couldn't fetch/parse the results
        file — this is NOT the same as Crustdata finding nothing."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/a"
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", side_effect=Exception("s3 fetch failed")):
            result = batch_enrich_profiles([url], api_key="test-key")

        assert result["fulfilled"] == 0
        assert result["unmatched"] == []
        assert result["unknown"] == [url]
        assert result["unknown_diagnostics"][0]["reason"] == "download_error"
        assert result["unknown_diagnostics"][0]["last_status_payload"] == {"status": "completed"}

    def test_status_payload_fulfilled_count_is_surfaced_not_discarded(self, monkeypatch):
        """When Crustdata's own status payload carries a fulfilled count
        for a job we otherwise couldn't fully read, it must be surfaced
        (fulfilled_hint) rather than thrown away — even though it doesn't
        get folded into the conservative credits_used number."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/a"
        status_with_count = {"status": "completed", "entities_requested": 1, "entities_fulfilled": 1}
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value=status_with_count), \
             patch("crustdata_search._download_batch_results", side_effect=Exception("s3 fetch failed")):
            result = batch_enrich_profiles([url], api_key="test-key")

        assert result["unknown_diagnostics"][0]["fulfilled_hint"] == 1
        assert result["unknown_diagnostics"][0]["last_status_payload"] == status_with_count
        # Still conservative: not silently added to the billed count.
        assert result["credits_used"] == 0

    def test_genuine_no_match_still_distinct_from_unknown(self, monkeypatch):
        """A job that completed and downloaded cleanly with zero matching
        records is a REAL no-match — must stay in `unmatched`, and
        `unknown` must stay empty. The two states must never blur."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/ghost"
        with patch("crustdata_search.submit_batch_enrich", return_value="batch1"), \
             patch("crustdata_search.get_batch_status", return_value={"status": "completed"}), \
             patch("crustdata_search._download_batch_results", return_value=[]):
            result = batch_enrich_profiles([url], api_key="test-key")

        assert result["fulfilled"] == 0
        assert result["unmatched"] == [url]
        assert result["unknown"] == []
        assert result["unknown_diagnostics"] == []
        assert result["credits_used"] == 0

    def test_mixed_jobs_one_unknown_one_fulfilled(self, monkeypatch):
        """Multiple jobs (>10,000 URLs splits into separate jobs) can have
        independent outcomes — one job's poll failure must not contaminate
        another job's genuinely fulfilled result. The first 10,000 URLs
        land in one job (which fails to poll), the 10,001st alone in a
        second job (which succeeds)."""
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        urls = [f"https://www.linkedin.com/in/u{i}" for i in range(10001)]
        good_url = urls[10000]  # alone in the second, single-URL job
        good_record = {
            "original_identifier": good_url,
            "data": {
                "basic_profile": {"name": "Good"},
                "skills": {"professional_network_skills": ["Python"]},
                "social_handles": {"professional_network_identifier": {"profile_url": good_url}},
            },
        }

        def fake_get_batch_status(batch_id, api_key=None):
            if batch_id == "batchGood":
                return {"status": "completed"}
            raise Exception("network blip")

        def fake_submit(job_urls, api_key=None, chunk_size=None, fields=None):
            return "batchGood" if job_urls == [good_url] else "batchBad"

        def fake_download(status_payload, api_key):
            return [good_record]

        with patch("crustdata_search.submit_batch_enrich", side_effect=fake_submit), \
             patch("crustdata_search.get_batch_status", side_effect=fake_get_batch_status), \
             patch("crustdata_search._download_batch_results", side_effect=fake_download):
            result = batch_enrich_profiles(urls, api_key="test-key")

        assert result["by_url"][good_url]["name"] == "Good"
        assert result["fulfilled"] == 1
        assert good_url not in result["unknown"]
        assert len(result["unknown"]) == 10000  # the whole failed-poll job
        assert len(result["unknown_diagnostics"]) == 1
        assert result["unknown_diagnostics"][0]["reason"] == "poll_error"
        assert result["unknown_diagnostics"][0]["batch_id"] == "batchBad"


class TestSubmitBatchEnrichFailureClassification:
    """Codex adversarial review of PR #127, round 3 (2026-08-04, HIGH):
    round 2 fixed post-acceptance ambiguity (poll/download failures after
    a batch_id was obtained) but a client-side timeout or connection loss
    during the submit POST itself was still collapsed to "job never
    started" — even though Crustdata may have already received and
    started the job before the response was lost. _submit_definitely_
    never_started() is a narrow allowlist of the ONLY submit failures
    safe to treat as genuine no-spend; everything else must land in
    `unknown`. Directly exercises the classifier, then confirms
    batch_enrich_profiles() routes each case correctly end-to-end."""

    def test_value_error_is_unambiguous(self):
        assert _submit_definitely_never_started(ValueError("bad input")) is True

    def test_auth_error_is_unambiguous(self):
        from error_handling import AuthenticationError
        assert _submit_definitely_never_started(AuthenticationError("Crustdata")) is True

    def test_rate_limit_error_is_unambiguous(self):
        from error_handling import RateLimitError
        assert _submit_definitely_never_started(RateLimitError("Crustdata")) is True

    def test_generic_4xx_is_unambiguous(self):
        from error_handling import ExternalServiceError
        exc = ExternalServiceError("Crustdata", message="bad request", status_code=400)
        assert _submit_definitely_never_started(exc) is True

    def test_timeout_translated_exception_is_ambiguous(self):
        """submit_batch_enrich() converts requests.exceptions.Timeout into
        ExternalServiceError(status_code=504) — Codex's exact example of
        an ambiguous submit failure."""
        from error_handling import ExternalServiceError
        exc = ExternalServiceError("Crustdata", message="Batch enrich submit timed out", status_code=504)
        assert _submit_definitely_never_started(exc) is False

    def test_connection_error_translated_exception_is_ambiguous(self):
        """submit_batch_enrich() converts requests.exceptions.ConnectionError
        into ServiceUnavailableError with no real HTTP status_code —
        Codex's other exact example of an ambiguous submit failure."""
        from error_handling import ServiceUnavailableError
        exc = ServiceUnavailableError("Crustdata", message="Connection error: refused")
        assert _submit_definitely_never_started(exc) is False

    def test_real_5xx_response_is_ambiguous(self):
        """A genuine 5xx HTTP response means Crustdata's server DID
        receive and attempt to process the request — the opposite of
        "definitely never started"."""
        from error_handling import ServiceUnavailableError
        exc = ServiceUnavailableError("Crustdata", status_code=502, response_body="bad gateway")
        assert _submit_definitely_never_started(exc) is False

    def test_200_with_no_batch_id_is_ambiguous(self):
        from error_handling import ExternalServiceError
        exc = ExternalServiceError("Crustdata", message="Batch enrich submit returned no batch id: {}")
        assert _submit_definitely_never_started(exc) is False

    def test_unrecognized_exception_type_is_ambiguous(self):
        assert _submit_definitely_never_started(RuntimeError("mystery failure")) is False

    def test_end_to_end_ambiguous_submit_failure_lands_in_unknown(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        from error_handling import ServiceUnavailableError
        url = "https://www.linkedin.com/in/a"
        with patch(
            "crustdata_search.submit_batch_enrich",
            side_effect=ServiceUnavailableError("Crustdata", message="Connection error: refused"),
        ):
            result = batch_enrich_profiles([url], api_key="test-key")

        assert result["unmatched"] == []
        assert result["unknown"] == [url]
        assert result["credits_used"] == 0
        assert result["unknown_diagnostics"][0]["reason"] == "submit_error"

    def test_end_to_end_unambiguous_submit_failure_lands_in_unmatched(self, monkeypatch):
        monkeypatch.setattr("crustdata_search.time.sleep", lambda s: None)
        url = "https://www.linkedin.com/in/a"
        with patch("crustdata_search.submit_batch_enrich", side_effect=ValueError("bad input")):
            result = batch_enrich_profiles([url], api_key="test-key")

        assert result["unmatched"] == [url]
        assert result["unknown"] == []


# ---------------------------------------------------------------------------
# enrich_profile_to_legacy_shape — verified against a REAL live response
# (captured 2026-07-20 via crustdata_people_enrich_v2, linkedin.com/in/dvdhsu)
# ---------------------------------------------------------------------------

_REAL_ENRICH_RESPONSE = {
    "basic_profile": {
        "current_title": "Founder, CEO",
        "first_name": None,
        "headline": "Founder, CEO @ Retool",
        "languages": [],
        "last_name": None,
        "location": {
            "city": "", "continent": "North America", "country": "United States",
            "raw": "San Francisco Bay Area", "state": "California",
        },
        "name": "David Hsu",
        "professional_network_name": "David Hsu",
        "profile_picture_permalink": "https://example.com/pic.jpg",
        "summary": "",
    },
    "education": {
        "schools": [{
            "activities_and_societies": "", "degree": "Bachelor of Arts (B.A.)",
            "end_year": None, "field_of_study": "Philosophy and Computer Science",
            "institute_logo_url": "https://example.com/logo.jpg",
            "professional_network_id": "4477", "school": "University of Oxford",
            "start_year": None,
        }]
    },
    "experience": {
        "employment_details": {
            "current": [{
                "company_professional_network_profile_url": "https://linkedin.com/company/11869260",
                "company_profile_picture_permalink": "https://example.com/logo2.jpg",
                "company_website_domain": "retool.com", "crustdata_company_id": 633593,
                "description": "", "employment_type": "", "end_date": None,
                "function_category": "", "is_default": True, "location": {"raw": ""},
                "name": "Retool", "position_id": 1041554620, "professional_network_id": "11869260",
                "seniority_level": "CXO", "start_date": "2017-01-01T00:00:00+00:00",
                "title": "Founder, CEO", "years_at_company": "6 to 10 years",
                "years_at_company_raw": 9.0,
            }],
            "past": [],
        }
    },
    "professional_network": {
        "connections": 701, "current_title": "Founder, CEO", "followers": 14621,
        "headline": "Founder, CEO @ Retool",
        "location": {"city": "", "continent": "North America", "country": "United States",
                      "raw": "San Francisco Bay Area", "state": "California"},
        "metadata": {"last_scraped_source": None}, "name": "David Hsu",
        "profile_picture_permalink": "https://example.com/pic.jpg", "pronoun": "", "summary": "",
    },
    "skills": {"professional_network_skills": []},
    "social_handles": {
        "dev_platform_identifier": {"profile_url": None},
        "professional_network_identifier": {"profile_url": "https://www.linkedin.com/in/dvdhsu"},
        "twitter_identifier": {"slug": ""},
    },
    "crustdata_person_id": 14540,
}


def _gzip_response(records, content_type="application/gzip", encoding_header=None):
    """Build a MagicMock response matching what a real Crustdata batch
    results file looks like: gzip-compressed BODY BYTES with no
    Content-Encoding header (verified live 2026-07-20) — requests will NOT
    auto-decompress this."""
    body = "\n".join(json.dumps(r) for r in records).encode("utf-8")
    compressed = gzip.compress(body)
    resp = MagicMock()
    resp.status_code = 200
    resp.content = compressed
    resp.headers = {"Content-Type": content_type}
    if encoding_header:
        resp.headers["Content-Encoding"] = encoding_header
    resp.raise_for_status = MagicMock()
    return resp


class TestDownloadBatchResults:
    """Regression tests for a real bug found via live testing 2026-07-20
    (not a Codex finding — Codex can't make network calls): Crustdata's
    results files are gzip-compressed but the server doesn't set
    Content-Encoding, so a naive `.text` read silently produced zero
    records even when the job fully succeeded — the whole feature would
    have done nothing in production."""

    def test_gzip_compressed_single_file_is_decompressed(self):
        records = [{"original_identifier": "https://www.linkedin.com/in/a", "data": {"basic_profile": {"name": "A"}}}]
        with patch("crustdata_search.requests.get", return_value=_gzip_response(records)):
            result = _download_batch_results({"download_url": "https://s3.example.com/results.jsonl.gz"}, api_key="test-key")
        assert result == records

    def test_multiple_part_files_all_fetched(self):
        """Larger jobs split into part-000.jsonl.gz, part-001.jsonl.gz, etc.
        — download_urls (plural) must ALL be fetched, not just the first."""
        part0 = [{"original_identifier": "https://www.linkedin.com/in/a", "data": {"basic_profile": {"name": "A"}}}]
        part1 = [{"original_identifier": "https://www.linkedin.com/in/b", "data": {"basic_profile": {"name": "B"}}}]
        responses = [_gzip_response(part0), _gzip_response(part1)]
        with patch("crustdata_search.requests.get", side_effect=responses):
            result = _download_batch_results(
                {"download_urls": ["https://s3.example.com/part-000.jsonl.gz", "https://s3.example.com/part-001.jsonl.gz"]},
                api_key="test-key",
            )
        assert result == part0 + part1

    def test_download_urls_preferred_over_singular_when_both_present(self):
        part0 = [{"original_identifier": "https://www.linkedin.com/in/a", "data": {}}]
        with patch("crustdata_search.requests.get", return_value=_gzip_response(part0)) as mock_get:
            _download_batch_results(
                {"download_url": "https://s3.example.com/single.jsonl.gz",
                 "download_urls": ["https://s3.example.com/part-000.jsonl.gz"]},
                api_key="test-key",
            )
        assert mock_get.call_args.args[0] == "https://s3.example.com/part-000.jsonl.gz"

    def test_no_bearer_header_sent_to_s3_presigned_url(self):
        """S3 presigned URLs authenticate via the query string — sending our
        own Bearer header would be at best redundant, at worst rejected."""
        with patch("crustdata_search.requests.get", return_value=_gzip_response([])) as mock_get:
            _download_batch_results({"download_url": "https://s3.example.com/results.jsonl.gz"}, api_key="test-key")
        assert mock_get.call_args.kwargs["headers"] is None

    def test_inline_results_still_short_circuits_before_any_download(self):
        with patch("crustdata_search.requests.get") as mock_get:
            result = _download_batch_results({"results": [{"data": {"basic_profile": {"name": "Inline"}}}]}, api_key="test-key")
        mock_get.assert_not_called()
        assert result == [{"data": {"basic_profile": {"name": "Inline"}}}]

    def test_no_download_url_returns_empty_list(self):
        with patch("crustdata_search.requests.get") as mock_get:
            result = _download_batch_results({"status": "completed"}, api_key="test-key")
        mock_get.assert_not_called()
        assert result == []


class TestEnrichProfileToLegacyShape:
    def test_maps_real_response_to_flat_shape(self):
        flat = enrich_profile_to_legacy_shape(_REAL_ENRICH_RESPONSE)

        assert flat["name"] == "David Hsu"
        assert flat["title"] == "Founder, CEO"
        assert flat["headline"] == "Founder, CEO @ Retool"
        assert flat["linkedin_flagship_url"] == "https://www.linkedin.com/in/dvdhsu"
        assert flat["num_of_connections"] == 701
        assert flat["crustdata_person_id"] == 14540

    def test_current_employer_uses_flat_legacy_keys(self):
        flat = enrich_profile_to_legacy_shape(_REAL_ENRICH_RESPONSE)
        emp = flat["current_employers"][0]
        assert emp["employee_title"] == "Founder, CEO"
        assert emp["employer_name"] == "Retool"
        assert emp["start_date"] == "2017-01-01T00:00:00+00:00"
        assert emp["end_date"] is None  # current role — compute_role_durations() relies on this
        assert "title" not in emp  # must be the FLAT shape, not the raw v2 keys
        assert "name" not in emp

    def test_education_uses_flat_legacy_keys(self):
        flat = enrich_profile_to_legacy_shape(_REAL_ENRICH_RESPONSE)
        school = flat["education_background"][0]
        assert school["institute_name"] == "University of Oxford"
        assert school["degree_name"] == "Bachelor of Arts (B.A.)"
        assert school["field_of_study"] == "Philosophy and Computer Science"

    def test_derived_all_lists(self):
        flat = enrich_profile_to_legacy_shape(_REAL_ENRICH_RESPONSE)
        assert flat["all_employers"] == ["Retool"]
        assert flat["all_titles"] == ["Founder, CEO"]
        assert flat["all_schools"] == ["University of Oxford"]
        assert flat["all_degrees"] == ["Bachelor of Arts (B.A.)"]

    def test_empty_input_returns_empty_dict(self):
        assert enrich_profile_to_legacy_shape({}) == {}
        assert enrich_profile_to_legacy_shape(None) == {}

    def test_missing_sections_do_not_raise(self):
        flat = enrich_profile_to_legacy_shape({"crustdata_person_id": 1})
        assert flat["name"] == ""
        assert flat["current_employers"] == []
        assert flat["skills"] == []

    def test_skills_as_bare_strings(self):
        data = {"skills": {"professional_network_skills": ["Python", "Go"]}}
        flat = enrich_profile_to_legacy_shape(data)
        assert flat["skills"] == ["Python", "Go"]

    def test_skills_as_objects_coerced_to_strings(self):
        """Element type wasn't confirmed live (sample profiles had empty
        skills lists) — must handle both shapes defensively."""
        data = {"skills": {"professional_network_skills": [{"name": "Python"}, {"name": "Go"}]}}
        flat = enrich_profile_to_legacy_shape(data)
        assert flat["skills"] == ["Python", "Go"]

    def test_output_feeds_trim_raw_profile_and_compute_role_durations(self):
        """End-to-end guarantee: the translator's output keys actually match
        what the screening pipeline reads. Imports dashboard directly rather
        than duplicating its field list here."""
        import dashboard

        flat = enrich_profile_to_legacy_shape(_REAL_ENRICH_RESPONSE)
        trimmed = dashboard.trim_raw_profile(flat)
        assert trimmed["title"] == "Founder, CEO"
        assert trimmed["current_employers"][0]["employee_title"] == "Founder, CEO"
        assert trimmed["current_employers"][0]["employer_name"] == "Retool"

        durations_text = dashboard.compute_role_durations(flat)
        assert "Founder, CEO at Retool" in durations_text
        assert "CURRENT ROLE" in durations_text
