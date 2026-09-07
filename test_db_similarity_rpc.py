"""
Tests for find_similar_profiles_rpc error handling.

The original implementation swallowed every exception and returned `[]`,
making genuine failures (e.g. migration 019 not applied) look like "no
similar profiles". These tests pin the new behavior: real errors raise
``SimilarityRPCError``, while a truly empty result still returns `[]`.
"""

from __future__ import annotations

import json
import types

import pytest
import requests

import db


# NOTE: these patch ``requests.request``, not ``requests.post``.
# find_similar_profiles_rpc now routes through db._request_with_retry, so a
# transient TLS drop or 5xx on this READ-ONLY RPC no longer silently loses the
# result (Codex round 2 on the transient-retry PR), and that helper calls
# requests.request. Every assertion below is unchanged -- only the transport
# being intercepted moved one level.


class FakeClient:
    url = "https://fake.supabase.co"
    headers = {"apikey": "fake", "Authorization": "Bearer fake"}


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def test_returns_empty_list_for_genuine_no_matches(monkeypatch):
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResponse(200, []))
    result = db.find_similar_profiles_rpc(FakeClient(), [0.1] * 1536, match_count=5)
    assert result == []


def test_raises_on_http_error_with_postgres_body(monkeypatch):
    body = (
        '{"code":"42703","message":"column profiles.embedding does not exist"}'
    )
    monkeypatch.setattr(
        requests, "request",
        lambda *a, **k: FakeResponse(400, body, text=body),
    )
    with pytest.raises(db.SimilarityRPCError) as exc:
        db.find_similar_profiles_rpc(FakeClient(), [0.1] * 1536)
    assert "column profiles.embedding does not exist" in str(exc.value)


def test_raises_on_network_failure(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("no route to host")
    monkeypatch.setattr(requests, "request", boom)
    with pytest.raises(db.SimilarityRPCError) as exc:
        db.find_similar_profiles_rpc(FakeClient(), [0.1] * 1536)
    assert "Network error" in str(exc.value)


def test_raises_on_non_list_response(monkeypatch):
    monkeypatch.setattr(
        requests, "request",
        lambda *a, **k: FakeResponse(200, {"error": "weird shape"}),
    )
    with pytest.raises(db.SimilarityRPCError) as exc:
        db.find_similar_profiles_rpc(FakeClient(), [0.1] * 1536)
    assert "unexpected shape" in str(exc.value)


def test_returns_matches_when_rpc_succeeds(monkeypatch):
    payload = [
        {"linkedin_url": "https://www.linkedin.com/in/a/", "similarity": 0.91},
        {"linkedin_url": "https://www.linkedin.com/in/b/", "similarity": 0.83},
    ]
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResponse(200, payload))
    result = db.find_similar_profiles_rpc(FakeClient(), [0.1] * 1536, match_count=2)
    assert result == payload
