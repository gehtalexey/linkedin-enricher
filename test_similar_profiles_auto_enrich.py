"""
Tests for similar_profiles.py auto-enrichment path.

All network-free: fake Supabase client, fake OpenAI, monkeypatched
crustdata_search.sync_enrich_profile (the cheap v2025-11-01 sync enrich
call this module rewired to on 2026-08-04 — 1 credit base, replacing the
legacy GET /screener/person/enrich at 3 credits/profile). We verify:
  1. Profile in DB with embedding         → source="cached", no enrich, no embed
  2. Profile in DB without embedding      → source="embedded", embeds, no enrich
  3. Profile NOT in DB + crustdata_key    → source="enriched", calls Crustdata, saves, embeds
  4. Profile NOT in DB + no crustdata_key → SimilarProfileError, not attempted
  5. Crustdata returns no data            → SimilarProfileError, attempted but not fulfilled
  6. Crustdata call raises                → SimilarProfileError wrapping the cause, attempted + error
  7. A downstream failure AFTER a successful enrich (the similarity RPC
     itself failing) still tags the raised error as fulfilled=True, since
     the credit was genuinely spent before that point

Also covers the crustdata_attempted/crustdata_fulfilled/crustdata_error
attributes SimilarProfileError now carries (Codex review, PR #127,
2026-08-04) — dashboard.log_similar_profiles_crustdata_usage() reads these
to log usage at the dashboard boundary without this module needing a
UsageTracker parameter threaded through it; see
test_similar_profiles_usage_logging.py for that function's own tests.
"""

from __future__ import annotations

import types

import pytest

import similar_profiles
from similar_profiles import SimilarProfileError, search_similar
from db import SimilarityRPCError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeSupabaseClient:
    url = "https://fake.supabase.co"
    headers = {"apikey": "k", "Authorization": "Bearer k"}

    def __init__(self, initial_profile=None):
        self._profiles = {}
        if initial_profile:
            self._profiles[initial_profile["linkedin_url"]] = dict(initial_profile)
        self.updates: list[dict] = []
        self.saves: list[dict] = []

    def update(self, table, data, filters):
        url = filters.get("linkedin_url")
        if url and url in self._profiles:
            self._profiles[url].update(data)
        self.updates.append({"url": url, "data": data})

    def upsert(self, table, data, on_conflict=None):
        url = data.get("linkedin_url")
        if url:
            self._profiles[url] = {**self._profiles.get(url, {}), **data}
        self.saves.append({"url": url, "data": data})
        return [self._profiles.get(url, data)]


class FakeOpenAI:
    def __init__(self, dim=1536):
        self.dim = dim
        self.calls = 0

    @property
    def embeddings(self):
        return self

    def create(self, model, input):
        self.calls += 1
        return types.SimpleNamespace(
            data=[
                types.SimpleNamespace(embedding=[0.1] * self.dim)
                for _ in input
            ]
        )


def _profile_row(**overrides):
    base = {
        "linkedin_url": "https://www.linkedin.com/in/jane",
        "name": "Jane Doe",
        "current_title": "Backend Engineer",
        "current_company": "Acme",
        "all_titles": ["Backend Engineer"],
        "all_employers": ["Acme"],
        "skills": ["Python"],
        "raw_data": {"headline": "Backend dev", "summary": "Ten years."},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Stub the db.* functions similar_profiles depends on.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def stub_db(monkeypatch):
    """Replace db.* helpers with in-memory equivalents on the fake client."""
    import db

    def fake_get_profile(client, url):
        return client._profiles.get(url)

    def fake_update_embedding(client, url, embedding, model, h):
        client.update("profiles", {
            "embedding": embedding,
            "embedding_model": model,
            "embedding_input_hash": h,
            "embedded_at": "2026-05-19T00:00:00+00:00",
        }, {"linkedin_url": url})
        return True

    def fake_save_enriched_profile(client, url, response, original=None):
        # Build a minimal saved row from the Crustdata response shape.
        row = {
            "linkedin_url": url,
            "name": response.get("name"),
            "current_title": (response.get("current_employers") or [{}])[0].get("title"),
            "current_company": (response.get("current_employers") or [{}])[0].get("employer_name"),
            "all_titles": response.get("all_titles") or [],
            "all_employers": response.get("all_employers") or [],
            "skills": response.get("skills") or [],
            "raw_data": response,
        }
        client.upsert("profiles", row)
        return row

    def fake_find_similar(client, query_embedding, match_count, min_similarity, country_terms=None, city_terms=None):
        # Return two stub matches plus the query row, so exclude_self has work.
        return [
            {"linkedin_url": "https://www.linkedin.com/in/jane", "similarity": 1.0},
            {"linkedin_url": "https://www.linkedin.com/in/match-a/", "name": "A", "similarity": 0.9},
            {"linkedin_url": "https://www.linkedin.com/in/match-b/", "name": "B", "similarity": 0.8},
        ]

    monkeypatch.setattr(similar_profiles, "get_profile", fake_get_profile)
    monkeypatch.setattr(similar_profiles, "update_profile_embedding", fake_update_embedding)
    monkeypatch.setattr(similar_profiles, "save_enriched_profile", fake_save_enriched_profile)
    monkeypatch.setattr(similar_profiles, "find_similar_profiles_rpc", fake_find_similar)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_cached_path_no_enrich_no_embed(monkeypatch):
    profile = _profile_row(embedding=[0.5] * 1536)
    client = FakeSupabaseClient(initial_profile=profile)
    oai = FakeOpenAI()

    # Crustdata MUST NOT be called.
    monkeypatch.setattr(similar_profiles, "sync_enrich_profile", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("Crustdata called in cached path!")
    ))

    result = search_similar(client, oai, profile["linkedin_url"], crustdata_key="kc")

    assert result["source"] == "cached"
    assert oai.calls == 0
    assert client.updates == []
    assert client.saves == []
    assert len(result["matches"]) == 2


def test_in_db_no_embedding_embeds_no_enrich(monkeypatch):
    profile = _profile_row()  # no embedding field
    client = FakeSupabaseClient(initial_profile=profile)
    oai = FakeOpenAI()

    monkeypatch.setattr(similar_profiles, "sync_enrich_profile", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("Crustdata called when profile already in DB!")
    ))

    result = search_similar(client, oai, profile["linkedin_url"], crustdata_key="kc")

    assert result["source"] == "embedded"
    assert oai.calls == 1
    assert len(client.updates) == 1
    assert client.saves == []


def test_not_in_db_with_key_enriches_then_embeds(monkeypatch):
    client = FakeSupabaseClient()  # empty DB
    oai = FakeOpenAI()
    calls = []

    def fake_sync_enrich(linkedin_url, api_key=None, fields=None):
        calls.append({"linkedin_url": linkedin_url, "api_key": api_key})
        return {
            "linkedin_flagship_url": "https://www.linkedin.com/in/jane",
            "name": "Jane Doe",
            "current_employers": [{"employee_title": "Backend Engineer", "employer_name": "Acme"}],
            "all_titles": ["Backend Engineer"],
            "all_employers": ["Acme"],
            "skills": ["Python"],
            "headline": "Backend dev",
            "summary": "Ten years.",
        }

    monkeypatch.setattr(similar_profiles, "sync_enrich_profile", fake_sync_enrich)

    result = search_similar(
        client, oai, "https://www.linkedin.com/in/jane", crustdata_key="kc"
    )

    assert result["source"] == "enriched"
    assert len(calls) == 1, "Crustdata should be called exactly once"
    assert calls[0]["linkedin_url"] == "https://www.linkedin.com/in/jane"
    assert calls[0]["api_key"] == "kc"
    assert len(client.saves) == 1, "Enriched profile should be saved"
    assert oai.calls == 1, "Embedding should be generated"
    assert len(client.updates) == 1, "Embedding should be written back"


def test_not_in_db_no_key_raises():
    client = FakeSupabaseClient()
    oai = FakeOpenAI()
    with pytest.raises(SimilarProfileError) as exc:
        search_similar(client, oai, "https://www.linkedin.com/in/unknown/", crustdata_key=None)
    assert "isn't in our database" in str(exc.value)
    # No API key means no Crustdata call was ever attempted — nothing to log.
    assert exc.value.crustdata_attempted is False


def test_crustdata_no_data_raises(monkeypatch):
    client = FakeSupabaseClient()
    oai = FakeOpenAI()

    monkeypatch.setattr(similar_profiles, "sync_enrich_profile", lambda *a, **k: None)

    with pytest.raises(SimilarProfileError) as exc:
        search_similar(client, oai, "https://www.linkedin.com/in/ghost/", crustdata_key="kc")
    assert "couldn't find" in str(exc.value).lower()
    # Genuine no-match: attempted, but Crustdata's no-charge-on-no-match
    # policy means it cost nothing and it's NOT an error.
    assert exc.value.crustdata_attempted is True
    assert exc.value.crustdata_fulfilled is False
    assert exc.value.crustdata_error is None


def test_crustdata_error_raises(monkeypatch):
    """Any exception out of sync_enrich_profile() (auth failure, 5xx,
    timeout, connection error — crustdata_search raises typed
    error_handling exceptions for all of these) must surface as a
    SimilarProfileError wrapping the cause, not propagate raw."""
    client = FakeSupabaseClient()
    oai = FakeOpenAI()

    def boom(*a, **k):
        raise RuntimeError("Crustdata is temporarily unavailable")

    monkeypatch.setattr(similar_profiles, "sync_enrich_profile", boom)

    with pytest.raises(SimilarProfileError) as exc:
        search_similar(client, oai, "https://www.linkedin.com/in/x/", crustdata_key="kc")
    assert "Crustdata is temporarily unavailable" in str(exc.value)
    # A real transport/HTTP failure: attempted, and logged as an error.
    assert exc.value.crustdata_attempted is True
    assert exc.value.crustdata_fulfilled is False
    assert exc.value.crustdata_error is not None
    assert "Crustdata is temporarily unavailable" in exc.value.crustdata_error


def test_rpc_failure_after_successful_enrich_tags_fulfilled_true(monkeypatch):
    """A real credit was already spent (enrichment + save + embed all
    succeeded) before the similarity RPC itself failed — the raised error
    must still say fulfilled=True so the caller logs the true spend,
    not a silent miss."""
    client = FakeSupabaseClient()
    oai = FakeOpenAI()

    def fake_sync_enrich(linkedin_url, api_key=None, fields=None):
        return {
            "linkedin_flagship_url": "https://www.linkedin.com/in/jane",
            "name": "Jane Doe",
            "current_employers": [],
            "all_titles": [], "all_employers": [], "skills": ["Python"],
            "headline": "Backend dev", "summary": "Ten years.",
        }

    def fake_find_similar_boom(client, query_embedding, match_count, min_similarity, country_terms=None, city_terms=None):
        raise SimilarityRPCError("RPC unavailable")

    monkeypatch.setattr(similar_profiles, "sync_enrich_profile", fake_sync_enrich)
    monkeypatch.setattr(similar_profiles, "find_similar_profiles_rpc", fake_find_similar_boom)

    with pytest.raises(SimilarProfileError) as exc:
        search_similar(client, oai, "https://www.linkedin.com/in/jane", crustdata_key="kc")
    assert exc.value.crustdata_attempted is True
    assert exc.value.crustdata_fulfilled is True
    assert exc.value.crustdata_error is None
    # And the enrichment/save/embed side effects genuinely happened.
    assert len(client.saves) == 1
    assert oai.calls == 1
