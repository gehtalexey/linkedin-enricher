"""Tests for crustdata_search.sync_enrich_profile() — the synchronous
v2025-11-01 POST /person/enrich helper (1 credit/profile base, additive
pricing, same family as submit_batch_enrich()/batch_enrich_profiles()).

Added 2026-08-04 to give call sites that need exactly ONE profile right now
(a "re-enrich this URL" button, "find similar profiles" on-the-fly enrich)
a cheap path that doesn't pay for the async batch pipeline's poll/download
round trip, and doesn't pay the legacy GET /screener/person/enrich's flat
3-credits/profile rate.

No real network calls — requests.post is mocked throughout. The inner
`data` -> flat-legacy-shape translation is exercised via
enrich_profile_to_legacy_shape(), already covered end-to-end in
test_crustdata_batch_enrich.py against a REAL captured /person/enrich
response; this file focuses on sync_enrich_profile()'s own request
construction, header/auth handling, and its defensive parsing of the
sync endpoint's outer response envelope (NOT independently verified live —
see _extract_sync_enrich_record()'s docstring).
"""

from unittest.mock import MagicMock, patch

import pytest

from crustdata_search import (
    sync_enrich_profile,
    _extract_sync_enrich_record,
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


def _data_record(url, name="Jane Doe"):
    """One profile's `data` payload, same nested shape verified live for
    the batch endpoint (crustdata_search.py's _REAL_ENRICH_RESPONSE)."""
    return {
        "basic_profile": {"name": name, "current_title": "Engineer", "headline": "", "summary": "Builds things."},
        "experience": {"employment_details": {"current": [], "past": []}},
        "education": {"schools": []},
        "skills": {"professional_network_skills": ["Python"]},
        "social_handles": {"professional_network_identifier": {"profile_url": url}},
    }


class TestSyncEnrichProfileRequest:
    def test_sends_bearer_auth_and_version_header(self):
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data=[{"original_identifier": "u", "data": _data_record("u")}])
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
    def test_bare_list_response_returns_flat_shape(self):
        url = "https://www.linkedin.com/in/foo"
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(
                json_data=[{"original_identifier": url, "internal_id": 1, "data": _data_record(url)}]
            )
            flat = sync_enrich_profile(url, api_key="test-key")
        assert flat is not None
        assert flat["name"] == "Jane Doe"
        assert flat["skills"] == ["Python"]
        assert flat["linkedin_flagship_url"] == url

    def test_results_key_wrapped_response_returns_flat_shape(self):
        url = "https://www.linkedin.com/in/foo"
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(
                json_data={"results": [{"original_identifier": url, "data": _data_record(url)}]}
            )
            flat = sync_enrich_profile(url, api_key="test-key")
        assert flat["name"] == "Jane Doe"

    def test_single_unwrapped_record_returns_flat_shape(self):
        """Some v2025-11-01 endpoints answer a 1-item request with a bare
        record instead of a 1-element list — handle that too."""
        url = "https://www.linkedin.com/in/foo"
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(
                json_data={"original_identifier": url, "data": _data_record(url)}
            )
            flat = sync_enrich_profile(url, api_key="test-key")
        assert flat["name"] == "Jane Doe"

    def test_empty_list_returns_none(self):
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data=[])
            assert sync_enrich_profile("https://www.linkedin.com/in/ghost", api_key="test-key") is None

    def test_empty_dict_returns_none(self):
        with patch("crustdata_search.requests.post") as mock_post:
            mock_post.return_value = _mock_response(json_data={})
            assert sync_enrich_profile("https://www.linkedin.com/in/ghost", api_key="test-key") is None


class TestExtractSyncEnrichRecord:
    def test_multiple_records_matched_by_original_identifier(self):
        url = "https://www.linkedin.com/in/target"
        payload = [
            {"original_identifier": "https://www.linkedin.com/in/other", "data": {}},
            {"original_identifier": url, "data": {"name": "target"}},
        ]
        record = _extract_sync_enrich_record(payload, url)
        assert record["data"]["name"] == "target"

    def test_none_payload_returns_none(self):
        assert _extract_sync_enrich_record(None, "https://www.linkedin.com/in/x") is None

    def test_unrecognized_payload_type_returns_none(self):
        assert _extract_sync_enrich_record("not a dict or list", "https://www.linkedin.com/in/x") is None
