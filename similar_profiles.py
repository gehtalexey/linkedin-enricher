"""
"Find Similar Profiles" — semantic search powered by embeddings.

Given a LinkedIn URL, this module:
1. Looks the profile up in Supabase.
2. If it isn't there, enriches it on the fly via Crustdata, saves it, and
   embeds it — so the *next* search for the same URL is instant.
3. If it is there but lacks an embedding, embeds it now.
4. Calls the ``match_profiles_by_embedding`` RPC to return ranked matches.

The Streamlit UI lives in ``dashboard.py``; this module exposes the
plumbing so the UI code stays thin and the logic is testable.
"""

from __future__ import annotations

from crustdata_search import sync_enrich_profile
from db import (
    SimilarityRPCError,
    SupabaseClient,
    find_similar_profiles_rpc,
    get_profile,
    save_enriched_profile,
    update_profile_embedding,
)
from embeddings import (
    EMBEDDING_MODEL,
    build_embedding_text,
    compute_input_hash,
    embed_text,
)
from geo_terms import expand_city, expand_country
from normalizers import normalize_linkedin_url


class SimilarProfileError(Exception):
    """Raised when we can't find or build an embedding to search against."""


# ---------------------------------------------------------------------------
# Crustdata helper
# ---------------------------------------------------------------------------
def _crustdata_enrich(linkedin_url: str, crustdata_key: str) -> dict | None:
    """Fetch a single profile from Crustdata via the cheap v2025-11-01 sync
    enrich endpoint (crustdata_search.sync_enrich_profile — 1 credit base),
    NOT the legacy GET /screener/person/enrich (3 credits/profile) this used
    before 2026-08-04.

    Returns the flat legacy-enrich-shape dict on success (same shape
    save_enriched_profile()/_prepare_profile_row() already read — see
    crustdata_search.enrich_profile_to_legacy_shape()), or ``None`` if
    Crustdata had no data for the URL. Raises ``SimilarProfileError`` for
    transport errors or non-2xx HTTP responses so the caller can surface
    them in the UI.
    """
    try:
        return sync_enrich_profile(linkedin_url, api_key=crustdata_key)
    except Exception as e:
        raise SimilarProfileError(f"Crustdata enrichment failed: {e}") from e


# ---------------------------------------------------------------------------
# Embedding resolution
# ---------------------------------------------------------------------------
def get_or_build_query_embedding(
    db_client: SupabaseClient,
    openai_client,
    linkedin_url: str,
    crustdata_key: str | None = None,
) -> tuple[list[float], dict, str]:
    """Return the query embedding for ``linkedin_url``.

    Resolution order:
      1. Profile is in the DB with an embedding → reuse it. ``source="cached"``.
      2. Profile is in the DB but missing an embedding → embed it now,
         write it back. ``source="embedded"``.
      3. Profile is not in the DB:
           - If ``crustdata_key`` is provided, enrich via Crustdata (costs
             ~1 credit), save, embed. ``source="enriched"``.
           - Otherwise raise ``SimilarProfileError``.

    Returns:
        Tuple of ``(embedding, profile_row, source)``.
        ``source`` is one of ``"cached"``, ``"embedded"``, ``"enriched"``.
    """
    normalized = normalize_linkedin_url(linkedin_url)
    if not normalized:
        raise SimilarProfileError(f"Not a valid LinkedIn URL: {linkedin_url}")

    profile = get_profile(db_client, normalized)

    # ----- Case 3: not in DB → enrich -------------------------------------
    if not profile:
        if not crustdata_key:
            raise SimilarProfileError(
                "This profile isn't in our database yet, and no Crustdata "
                "key was provided to enrich it on the fly."
            )

        crust_data = _crustdata_enrich(normalized, crustdata_key)
        if not crust_data:
            raise SimilarProfileError(
                "Crustdata couldn't find this LinkedIn profile. Check the "
                "URL is correct and reachable."
            )

        # Crustdata uses linkedin_flagship_url on enrich responses; fall
        # back to the input URL if absent.
        canonical_url = (
            crust_data.get("linkedin_flagship_url")
            or crust_data.get("linkedin_url")
            or normalized
        )
        canonical_url = normalize_linkedin_url(canonical_url) or normalized

        saved = save_enriched_profile(db_client, canonical_url, crust_data, normalized)
        # Re-fetch so we get the indexed columns (all_titles, all_employers,
        # skills, etc.) that save_enriched_profile extracted from raw_data.
        profile = get_profile(db_client, canonical_url) or saved
        if not profile:
            raise SimilarProfileError(
                "Profile was enriched but couldn't be reloaded from the database."
            )

        text = build_embedding_text(profile)
        if not text:
            raise SimilarProfileError(
                "This profile was enriched but has no usable text to embed."
            )
        vector = embed_text(openai_client, text, model=EMBEDDING_MODEL)
        if not vector:
            raise SimilarProfileError("OpenAI returned an empty embedding.")
        update_profile_embedding(
            db_client,
            profile.get("linkedin_url") or canonical_url,
            vector,
            EMBEDDING_MODEL,
            compute_input_hash(text),
        )
        return vector, profile, "enriched"

    # ----- Case 1: cached -------------------------------------------------
    existing = profile.get("embedding")
    if existing:
        return existing, profile, "cached"

    # ----- Case 2: in DB, missing embedding -------------------------------
    text = build_embedding_text(profile)
    if not text:
        raise SimilarProfileError(
            "This profile has no usable text to embed (missing title, "
            "headline, summary, employers, and education). Re-enrich it "
            "or check that raw_data is populated."
        )
    vector = embed_text(openai_client, text, model=EMBEDDING_MODEL)
    if not vector:
        raise SimilarProfileError("OpenAI returned an empty embedding.")
    update_profile_embedding(
        db_client,
        normalized,
        vector,
        EMBEDDING_MODEL,
        compute_input_hash(text),
    )
    return vector, profile, "embedded"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def search_similar(
    db_client: SupabaseClient,
    openai_client,
    linkedin_url: str,
    match_count: int = 20,
    min_similarity: float = 0.0,
    exclude_self: bool = True,
    crustdata_key: str | None = None,
    country: str | None = None,
    city: str | None = None,
) -> dict:
    """High-level "find similar profiles" entry point.

    ``country`` (a dropdown label like "Israel") and ``city`` (free text like
    "Tel Aviv") are optional location filters. The caller passes the raw user
    input; this function expands each into its related location terms via
    ``geo_terms`` and the database keeps matches whose location satisfies BOTH
    groups (country AND city) — so a city narrows within the country rather
    than widening the search. Either group may be omitted.

    Returns a dict::

        {
            "query_profile": <row>,    # the profile we searched FROM
            "source": <str>,           # "cached" | "embedded" | "enriched"
            "matches": [<row>, ...],   # ranked by similarity desc
        }

    ``matches`` rows always include a ``similarity`` float in [0, 1].
    """
    embedding, query_profile, source = get_or_build_query_embedding(
        db_client, openai_client, linkedin_url, crustdata_key=crustdata_key
    )

    country_terms = expand_country(country)
    city_terms = expand_city(city)

    # Ask for one extra so we can drop the self-match without coming up short.
    rpc_count = match_count + 1 if exclude_self else match_count

    try:
        raw_matches = find_similar_profiles_rpc(
            db_client,
            query_embedding=embedding,
            match_count=rpc_count,
            min_similarity=min_similarity,
            country_terms=country_terms,
            city_terms=city_terms,
        )
    except SimilarityRPCError as e:
        raise SimilarProfileError(str(e)) from e

    if exclude_self:
        target_url = (query_profile or {}).get("linkedin_url")
        raw_matches = [
            row for row in raw_matches
            if row.get("linkedin_url") != target_url
        ][:match_count]
    else:
        raw_matches = raw_matches[:match_count]

    return {
        "query_profile": query_profile,
        "source": source,
        "matches": raw_matches,
    }
