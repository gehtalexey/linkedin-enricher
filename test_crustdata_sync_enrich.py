"""Tests for crustdata_search.sync_enrich_profile() — the synchronous
v2025-11-01 POST /person/enrich helper (1 credit/profile base, additive
pricing, same family as submit_batch_enrich()/batch_enrich_profiles()).

Added 2026-08-04 to give call sites that need exactly ONE profile right now
(a "re-enrich this URL" button, "find similar profiles" on-the-fly enrich)
a cheap path that doesn't pay for the async batch pipeline's poll/download
round trip, and doesn't pay the legacy GET /screener/person/enrich's flat
3-credits/profile rate.

No real network calls — requests.post is mocked throughout. The response
envelope fixtures below match the REAL shape confirmed via two live 1-credit
calls against POST /person/enrich, 2026-08-04:

    [
      {
        "match_type": <str>,
        "matched_on": ...,
        "matches": [
          {"confidence_score": <float>, "person_data": {...}},
          ...
        ]
      }
    ]

(An earlier version of this helper/test file assumed a
{"original_identifier", "data"} envelope like the batch endpoint's download
file — that assumption was WRONG for this sync endpoint and would have
silently broken the "Re-Enrich Profile" button and Find Similar Profiles
in production: the wrapper has no `basic_profile` at its own level, so the
old parser fed an empty dict into enrich_profile_to_legacy_shape() every
time. Fixed same day, before either call site shipped.)

The inner `person_data` -> flat-legacy-shape translation is exercised via
enrich_profile_to_legacy_shape(), already covered end-to-end in
test_crustdata_batch_enrich.py against a REAL captured /person/enrich
`data` payload (same nested shape person_data uses); this file focuses on
sync_enrich_profile()'s own request construction, header/auth handling, and
unwrapping the verified real envelope (list -> record -> matches ->
person_data), including match-ranking when more than one candidate comes
back.
"""

from unittest.mock import MagicMock, patch

import pytest

from crustdata_search import (
    sync_enrich_profile,
    _extract_sync_enrich_record,
    _select_person_data_from_matches,
    CRUSTDATA_SYNC_ENRICH_ENDPOINT,
    CRUSTDATA_API_VERSION,
    BATCH_ENRICH_FIELDS,
)


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    resp.headers = {}
    return resp


def _person_data(url, name="Jane Doe"):
    """One profile's `person_data` payload — same nested shape verified
    live for the batch endpoint (crustdata_search.py's _REAL_ENRICH_RESPONSE
    in test_crustdata_batch_enrich.py)."""
    return {
        "basic_profile": {"name": name, "current_title": "Engineer", "headline": "", "summary": "Builds things."},
        "experience": {"employment_details": {"current": [], "past": []}},
        "education": {"schools": []},
        "skills": {"professional_network_skills": ["Python"]},
        "social_handles": {"professional_network_identifier": {"profile_url": url}},
    }


def _real_shape_response(url, name="Jane Doe", confidence_score=0.98, match_type="exact"):
    """The verified top-level response shape for a single-URL sync enrich
    call: a list with one {match_type, matched_on, matches} record."""
    return [
        {
            "match_type": match_type,
            "matched_on": url,
            "matches": [
                {"confidence_score": confidence_score, "person_data": _person_data(url, name)},
            ],
        }
    ]


class TestSyncEnrichProfileRequest:
    def test_sends_bearer_auth_and_version_header(self):
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data=_real_shape_response("https://www.linkedin.com/in/foo"))
            sync_enrich_profile("https://www.linkedin.com/in/foo", api_key="test-key")
            headers = mock_post.call_args.kwargs["headers"]
            assert headers["Authorization"] == "Bearer test-key"
            assert headers["x-api-version"] == CRUSTDATA_API_VERSION

    def test_posts_to_sync_enrich_endpoint_with_url_and_base_fields(self):
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data=[])
            url = "https://www.linkedin.com/in/foo"
            sync_enrich_profile(url, api_key="test-key")

            args, kwargs = mock_post.call_args
            assert args[0] == CRUSTDATA_SYNC_ENRICH_ENDPOINT
            assert kwargs["json"]["professional_network_profile_urls"] == [url]
            # Base bundle only — no paid add-on fields (business_email,
            # personal emails, phones, dev-platform) requested.
            assert kwargs["json"]["fields"] == BATCH_ENRICH_FIELDS
            for paid_addon in ("business_email", "personal_contact_info", "phone_numbers", "github_profiles"):
                assert paid_addon not in kwargs["json"]["fields"]

    def test_empty_url_returns_none_without_calling_api(self):
        with patch("crustdata_search.requests.post") as mock_post:
            assert sync_enrich_profile("", api_key="test-key") is None
            assert sync_enrich_profile(None, api_key="test-key") is None
        mock_post.assert_not_called()

    def test_401_raises_authentication_error(self):
        from error_handling import AuthenticationError

        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(status_code=401)
            with pytest.raises(AuthenticationError):
                sync_enrich_profile("https://www.linkedin.com/in/foo", api_key="bad-key")

    def test_5xx_raises_service_unavailable(self, monkeypatch):
        from error_handling import ServiceUnavailableError

        # 503 is in sync_enrich_profile's retryable_exceptions, so
        # retry_with_backoff (error_handling.py) will retry it a few times
        # before giving up — patch its sleep so the test doesn't actually
        # wait out the exponential backoff.
        monkeypatch.setattr("error_handling.time.sleep", lambda s: None)

        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(status_code=503, text="down")
            with pytest.raises(ServiceUnavailableError):
                sync_enrich_profile("https://www.linkedin.com/in/foo", api_key="test-key")


class TestSyncEnrichProfileResponseParsing:
    """Against the VERIFIED real envelope: list -> {match_type, matched_on,
    matches} -> matches[].person_data."""

    def test_real_shape_returns_correctly_populated_profile(self):
        url = "https://www.linkedin.com/in/foo"
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data=_real_shape_response(url, name="David Hsu"))
            flat = sync_enrich_profile(url, api_key="test-key")

        assert flat is not None
        # Assert on real fields actually coming through, not just "not None"
        # — a parser that returns {} must fail this test.
        assert flat["name"] == "David Hsu"
        assert flat["skills"] == ["Python"]
        assert flat["linkedin_flagship_url"] == url
        assert flat["title"] == "Engineer"

    def test_empty_matches_returns_none(self):
        url = "https://www.linkedin.com/in/ghost"
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(
                json_data=[{"match_type": "none", "matched_on": url, "matches": []}]
            )
            assert sync_enrich_profile(url, api_key="test-key") is None

    def test_missing_matches_key_returns_none(self):
        url = "https://www.linkedin.com/in/ghost"
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(
                json_data=[{"match_type": "none", "matched_on": url}]
            )
            assert sync_enrich_profile(url, api_key="test-key") is None

    def test_empty_top_level_list_returns_none(self):
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data=[])
            assert sync_enrich_profile("https://www.linkedin.com/in/ghost", api_key="test-key") is None

    def test_wrapper_with_no_person_data_anywhere_returns_none_not_half_built(self):
        """A match entry present but with no usable person_data must never
        produce a half-built profile — same "genuine no-match" outcome as
        an empty matches list."""
        url = "https://www.linkedin.com/in/ghost"
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(
                json_data=[{
                    "match_type": "none", "matched_on": url,
                    "matches": [{"confidence_score": 0.1, "person_data": {}}],
                }]
            )
            assert sync_enrich_profile(url, api_key="test-key") is None

    def test_dict_wrapped_top_level_still_tolerated(self):
        """Defensive-only tolerance for a dict-keyed top level (not observed
        live, but other v2025-11-01 endpoints in this file use it)."""
        url = "https://www.linkedin.com/in/foo"
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(
                json_data={"results": _real_shape_response(url, name="Wrapped")}
            )
            flat = sync_enrich_profile(url, api_key="test-key")
        assert flat["name"] == "Wrapped"


class TestSelectPersonDataFromMatches:
    """Identity (requested-URL match) is verified on every candidate BEFORE
    ranking — not only as a confidence tie-break — and content is validated
    before trusting a match enough to translate it. A wrong or half-built
    profile must never come back, since this feeds straight into the
    shared Supabase `profiles` table (Codex review, PR #127, 2026-08-04)."""

    def test_valid_match_returns_fully_populated_profile(self):
        url = "https://www.linkedin.com/in/jane"
        match = {"confidence_score": 0.9, "person_data": _person_data(url, "Jane Doe")}
        result = _select_person_data_from_matches([match], url)
        assert result is not None
        assert result["basic_profile"]["name"] == "Jane Doe"
        assert result["skills"]["professional_network_skills"] == ["Python"]

    def test_wrong_url_returns_none_even_as_only_candidate(self):
        requested = "https://www.linkedin.com/in/requested"
        wrong = {"confidence_score": 0.99, "person_data": _person_data("https://www.linkedin.com/in/someone-else", "Someone Else")}
        assert _select_person_data_from_matches([wrong], requested) is None

    def test_empty_basic_profile_returns_none(self):
        """{"basic_profile": {}} is a truthy dict but carries no real
        identity/content — must not become a blank saved row."""
        url = "https://www.linkedin.com/in/jane"
        shell = {"confidence_score": 0.5, "person_data": {
            "basic_profile": {},
            "social_handles": {"professional_network_identifier": {"profile_url": url}},
        }}
        assert _select_person_data_from_matches([shell], url) is None

    def test_person_data_with_no_name_returns_none(self):
        """A name-less basic_profile, even with other sections present,
        fails the content gate."""
        url = "https://www.linkedin.com/in/jane"
        no_name = {"confidence_score": 0.9, "person_data": {
            "basic_profile": {"name": ""},
            "skills": {"professional_network_skills": ["Python"]},
            "social_handles": {"professional_network_identifier": {"profile_url": url}},
        }}
        assert _select_person_data_from_matches([no_name], url) is None

    def test_higher_confidence_wrong_url_loses_to_lower_confidence_right_url(self):
        """The exact scenario Codex flagged: a high-confidence match must
        NOT win just because of its score if it's the wrong person."""
        requested = "https://www.linkedin.com/in/right-person"
        wrong_but_confident = {
            "confidence_score": 0.99,
            "person_data": _person_data("https://www.linkedin.com/in/wrong-person", "Wrong Person"),
        }
        right_but_less_confident = {
            "confidence_score": 0.2,
            "person_data": _person_data(requested, "Right Person"),
        }
        result = _select_person_data_from_matches(
            [wrong_but_confident, right_but_less_confident], requested
        )
        assert result is not None
        assert result["basic_profile"]["name"] == "Right Person"

    def test_higher_confidence_wins_among_url_verified_candidates(self):
        """Once identity is verified, confidence_score still breaks a
        genuine ranking question between two records for the SAME url."""
        url = "https://www.linkedin.com/in/jane"
        low = {"confidence_score": 0.4, "person_data": _person_data(url, "Stale Data")}
        high = {"confidence_score": 0.9, "person_data": _person_data(url, "Fresh Data")}
        result = _select_person_data_from_matches([low, high], url)
        assert result["basic_profile"]["name"] == "Fresh Data"

    def test_no_candidate_matches_requested_url_returns_none(self):
        a = {"confidence_score": 0.8, "person_data": _person_data("https://www.linkedin.com/in/a", "A")}
        b = {"confidence_score": 0.8, "person_data": _person_data("https://www.linkedin.com/in/b", "B")}
        result = _select_person_data_from_matches([a, b], "https://www.linkedin.com/in/nobody")
        assert result is None

    def test_empty_list_returns_none(self):
        assert _select_person_data_from_matches([], "https://www.linkedin.com/in/x") is None

    def test_none_returns_none(self):
        assert _select_person_data_from_matches(None, "https://www.linkedin.com/in/x") is None

    def test_unnormalizable_requested_url_returns_none(self):
        url = "https://www.linkedin.com/in/jane"
        match = {"confidence_score": 0.9, "person_data": _person_data(url, "Jane Doe")}
        assert _select_person_data_from_matches([match], "not a linkedin url") is None

    def test_entries_without_person_data_are_skipped(self):
        no_data = {"confidence_score": 0.99}  # missing person_data entirely
        empty_data = {"confidence_score": 0.99, "person_data": {}}
        real = {"confidence_score": 0.1, "person_data": _person_data("https://www.linkedin.com/in/real", "Real")}
        result = _select_person_data_from_matches([no_data, empty_data, real], "https://www.linkedin.com/in/real")
        assert result["basic_profile"]["name"] == "Real"


class TestExtractSyncEnrichRecord:
    def test_multiple_records_matched_by_matched_on(self):
        url = "https://www.linkedin.com/in/target"
        payload = [
            {"match_type": "exact", "matched_on": "https://www.linkedin.com/in/other", "matches": []},
            {"match_type": "exact", "matched_on": url, "matches": [{"confidence_score": 1.0, "person_data": {"marker": "target"}}]},
        ]
        record = _extract_sync_enrich_record(payload, url)
        assert record["matched_on"] == url

    def test_none_payload_returns_none(self):
        assert _extract_sync_enrich_record(None, "https://www.linkedin.com/in/x") is None

    def test_unrecognized_payload_type_returns_none(self):
        assert _extract_sync_enrich_record("not a dict or list", "https://www.linkedin.com/in/x") is None
