"""Tests for crustdata_search.linkedin_profile_identity_matches() /
_linkedin_profile_slug() — the host-agnostic LinkedIn profile identity
comparison added 2026-08-04 (Codex adversarial review of PR #127, round 3,
HIGH).

Why this exists: normalize_linkedin_url() (db_core/normalizers.py, shared
verbatim with Supanova and the autopilots — deliberately NOT touched here)
preserves the hostname, so the same public profile represented as
https://il.linkedin.com/in/foo and https://www.linkedin.com/in/foo compares
UNEQUAL under straight string/normalize_linkedin_url() equality. The
identity gate this repo added in round 1/round 2
(_person_data_matches_url(), shared by both the sync and batch enrich
paths) used exactly that equality check, so a genuinely correct match
returned on a regional host was rejected, landed in `rejected`, and was
logged as zero fulfilled despite being a real paid match. This matters
specifically for this user: Israeli-candidate sourcing routinely sees
il.linkedin.com URLs.

This is also explicitly a SECURITY boundary, not just a correctness fix —
the host comparison must be an allowlist, not a loose pattern, so lookalike
domains (linkedin.com.evil.co, evil-linkedin.com) can never be mistaken
for a real LinkedIn profile URL.
"""

from crustdata_search import (
    linkedin_profile_identity_matches,
    _linkedin_profile_slug,
    _person_data_matches_url,
    _is_valid_person_data,
)


def _person_data(url, name="Someone", has_content=True):
    return {
        "basic_profile": {"name": name if has_content else ""},
        "experience": {"employment_details": {"current": [], "past": []}},
        "education": {"schools": []},
        "skills": {"professional_network_skills": ["Python"] if has_content else []},
        "social_handles": {"professional_network_identifier": {"profile_url": url}},
    }


class TestLinkedinProfileSlugParsing:
    def test_www_host_parses(self):
        assert _linkedin_profile_slug("https://www.linkedin.com/in/foo") == ("www.linkedin.com", "foo")

    def test_bare_host_parses(self):
        assert _linkedin_profile_slug("https://linkedin.com/in/foo") == ("linkedin.com", "foo")

    def test_regional_host_parses(self):
        assert _linkedin_profile_slug("https://il.linkedin.com/in/foo") == ("il.linkedin.com", "foo")
        assert _linkedin_profile_slug("https://fr.linkedin.com/in/foo") == ("fr.linkedin.com", "foo")
        assert _linkedin_profile_slug("https://de.linkedin.com/in/foo") == ("de.linkedin.com", "foo")

    def test_missing_scheme_and_www_prefix_parses(self):
        assert _linkedin_profile_slug("www.linkedin.com/in/foo") == ("www.linkedin.com", "foo")

    def test_trailing_slash_ignored(self):
        assert _linkedin_profile_slug("https://www.linkedin.com/in/foo/") == ("www.linkedin.com", "foo")

    def test_query_string_ignored(self):
        assert _linkedin_profile_slug("https://www.linkedin.com/in/foo?trk=abc") == ("www.linkedin.com", "foo")

    def test_percent_encoding_decoded(self):
        # %2D decodes to '-'
        assert _linkedin_profile_slug("https://www.linkedin.com/in/john%2Dsmith") == ("www.linkedin.com", "john-smith")

    def test_slug_lowercased(self):
        assert _linkedin_profile_slug("https://www.linkedin.com/in/FooBar") == ("www.linkedin.com", "foobar")

    def test_lookalike_host_suffix_rejected(self):
        assert _linkedin_profile_slug("https://linkedin.com.evil.co/in/foo") is None

    def test_lookalike_host_prefix_rejected(self):
        assert _linkedin_profile_slug("https://evil-linkedin.com/in/foo") is None
        assert _linkedin_profile_slug("https://notlinkedin.com/in/foo") is None

    def test_unrelated_host_rejected(self):
        assert _linkedin_profile_slug("https://example.com/in/foo") is None

    def test_sales_navigator_style_path_without_in_rejected(self):
        assert _linkedin_profile_slug("https://www.linkedin.com/sales/people/foo") is None

    def test_no_slug_rejected(self):
        assert _linkedin_profile_slug("https://www.linkedin.com/in/") is None

    def test_empty_or_none_rejected(self):
        assert _linkedin_profile_slug("") is None
        assert _linkedin_profile_slug(None) is None
        assert _linkedin_profile_slug(123) is None

    def test_deeper_subdomain_rejected(self):
        """Only bare / www / two-letter regional hosts are allowlisted —
        not arbitrary other LinkedIn subdomains."""
        assert _linkedin_profile_slug("https://mobile.linkedin.com/in/foo") is None
        assert _linkedin_profile_slug("https://news.linkedin.com/in/foo") is None


class TestLinkedinProfileIdentityMatches:
    def test_www_vs_regional_hosts_same_slug_match(self):
        www = "https://www.linkedin.com/in/foo"
        bare = "https://linkedin.com/in/foo"
        il = "https://il.linkedin.com/in/foo"
        fr = "https://fr.linkedin.com/in/foo"

        assert linkedin_profile_identity_matches(www, bare) is True
        assert linkedin_profile_identity_matches(www, il) is True
        assert linkedin_profile_identity_matches(www, fr) is True
        assert linkedin_profile_identity_matches(il, fr) is True

    def test_percent_encoded_and_trailing_slash_variants_match(self):
        a = "https://www.linkedin.com/in/john-smith"
        b = "https://www.linkedin.com/in/john%2Dsmith/"
        assert linkedin_profile_identity_matches(a, b) is True

    def test_case_differences_in_slug_match(self):
        a = "https://www.linkedin.com/in/FooBar"
        b = "https://www.linkedin.com/in/foobar"
        assert linkedin_profile_identity_matches(a, b) is True

    def test_lookalike_host_does_not_match(self):
        real = "https://www.linkedin.com/in/foo"
        evil = "https://linkedin.com.evil.co/in/foo"
        assert linkedin_profile_identity_matches(real, evil) is False

    def test_different_slugs_do_not_match(self):
        a = "https://www.linkedin.com/in/foo"
        b = "https://www.linkedin.com/in/bar"
        assert linkedin_profile_identity_matches(a, b) is False

    def test_invalid_url_never_matches_even_itself_twice(self):
        assert linkedin_profile_identity_matches("not a url", "not a url") is False
        assert linkedin_profile_identity_matches(None, None) is False


class TestIdentityGateUsesRegionalHostMatching:
    """Integration: the actual identity gate the sync AND batch enrich
    paths both call (_person_data_matches_url -> _is_valid_person_data)
    must accept a regional-host match, not just linkedin_profile_identity_
    matches() in isolation."""

    def test_person_data_matches_url_accepts_regional_host(self):
        requested = "https://il.linkedin.com/in/foo"
        data = _person_data("https://www.linkedin.com/in/foo")
        assert _person_data_matches_url(data, requested) is True

    def test_person_data_matches_url_rejects_lookalike_host(self):
        requested = "https://www.linkedin.com/in/foo"
        data = _person_data("https://linkedin.com.evil.co/in/foo")
        assert _person_data_matches_url(data, requested) is False

    def test_is_valid_person_data_accepts_regional_host_with_content(self):
        requested = "https://fr.linkedin.com/in/foo"
        data = _person_data("https://www.linkedin.com/in/foo", name="Real Person")
        assert _is_valid_person_data(data, requested) is True

    def test_is_valid_person_data_still_rejects_wrong_person_on_any_host(self):
        """The security fix must not weaken the round-2 wrong-person
        protection — a different slug on an allowlisted host still fails."""
        requested = "https://il.linkedin.com/in/foo"
        data = _person_data("https://www.linkedin.com/in/someone-else", name="Wrong Person")
        assert _is_valid_person_data(data, requested) is False
