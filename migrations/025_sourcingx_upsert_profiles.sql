-- Migration 025: public.sourcingx_upsert_profiles — project-scoped
-- identity-aware profile upsert wrapper.
--
-- STATUS: APPLIED to the shared Supabase database on 2026-08-03, with
-- Alexey's go-ahead, after three real Codex (gpt-5.5) review rounds (see
-- PR #125). Written 2026-08-03, modelled on agent-kalamata's
-- public.israel_autopilot_upsert_profiles (that project's
-- migrations/023_israel_autopilot_upsert_profiles.sql, applied
-- 2026-08-02) after the same rejection bug was confirmed live against
-- SourcingX's own db.py.
--
-- Rollback if ever needed:
--   drop function public.sourcingx_upsert_profiles(jsonb);
--
-- WHY THIS EXISTS
-- db.py currently saves profiles via
--   client.upsert('profiles', data, on_conflict='linkedin_url')
--   client.upsert_batch('profiles', batch, on_conflict='linkedin_url')
-- PostgreSQL fires the BEFORE INSERT trigger
-- trg_profiles_identity_before_write (sourcing_core.profiles_identity_before_write)
-- on the PROPOSED row before the unique conflict is resolved, so ANY person
-- already present in public.profiles is rejected with "Canonical LinkedIn
-- identity already exists; use sourcing_core.upsert_profile". Only
-- brand-new people save. This is the same bug diagnosed in
-- agent-kalamata's docs/plans/engine-bottleneck-diagnosis-2026-07-31.md
-- (28/28, 32/32, 37/37 rejected on real runs) — SourcingX hits the identical
-- rejection on any re-save of an existing profile (re-enrichment, a
-- similar-profiles match that already exists, etc.), it has just gone
-- unnoticed here because a failed upsert silently falls back to per-row
-- upsert() (also rejected) and is swallowed into stats['errors'] without a
-- distinct signal.
--
-- sourcing_core.upsert_profile is the correct identity-aware API, but
-- PostgREST on this project exposes only `public` and `graphql_public`
-- (Content-Profile: sourcing_core -> PGRST106 "Invalid schema"). This
-- function is a public wrapper, modelled on the already-live
-- public.israel_autopilot_upsert_profiles (agent-kalamata migration 023)
-- and public.engine_upsert_profiles, adapted for SourcingX's identity:
--
-- 1. IDENTITY IS HARDCODED, not caller-supplied — same reasoning as
--    agent-kalamata's wrapper (defeats the approval-gate comparison inside
--    sourcing_core.upsert_profile otherwise). PROJECT-SCOPED:
--      writer    = 'sourcingx'
--      source    = 'crustdata-enrich'
--      provider  = 'crustdata'
--      approval_id = 'SOURCINGX-' || to_char(now(), 'YYYYMMDD')
-- 2. ONE RESULT ROW PER INPUT ELEMENT, including malformed ones (a
--    'rejected' row with a local-validation reason), so `received` and
--    `length(results)` always reconcile.
-- 3. HARD BATCH CAP of 100 profiles/call — matches
--    save_enriched_profiles_bulk's default batch_size=100.
-- 4. TIMEOUT ALIGNMENT: statement_timeout = '20s', comfortably under the
--    client's request timeout (db.py's SupabaseClient._request uses
--    timeout=90 for GET but this RPC path will use its own POST call — kept
--    at 20s to match agent-kalamata's proven value for this same RPC shape;
--    a 100-row batch through sourcing_core.upsert_profile is not
--    project-specific work).
-- 5. REJECTED IS HANDLED EXPLICITLY; any action string sourcing_core.upsert_profile
--    could return that is NOT one of inserted/updated/noop/rejected is
--    counted as `unknown_action` rather than silently folded into `rejected`.
-- 6. canonical_url is returned per row so the Python caller can key
--    downstream writes (e.g. screening_results, pipeline state) on the
--    identity-resolved URL instead of the URL it happened to send — see
--    agent-kalamata migration 023 finding #6 for the failure mode this
--    avoids (a merge into a different existing row silently losing the
--    candidate if the caller keeps using the URL it sent).
-- 7. POLICY DECISIONS (same as agent-kalamata's, adopted for consistency):
--      - enriched_at: always `now()`, matching engine_upsert_profiles /
--        israel_autopilot_upsert_profiles. A caller-supplied enriched_at is
--        never trusted.
--      - approval_id: generated inside the function, project-prefixed, not
--        caller-supplied.
--
-- MODELLED ON public.israel_autopilot_upsert_profiles (agent-kalamata
-- migration 023) and public.engine_upsert_profiles: SECURITY DEFINER, owner
-- postgres, search_path = public, pg_temp, EXECUTE granted to
-- postgres/service_role only (NOT anon/authenticated).
--
-- DOES NOT touch engine_profile_employers (an engine-owned side effect this
-- function must never trigger) and DOES preserve SQLERRM on a per-row
-- failure (unlike engine_upsert_profiles' own handler, which discards it —
-- see agent-kalamata migration 023's header for why that matters).
--
-- WHAT THIS FUNCTION READS FROM EACH PROFILE OBJECT — plus an OPTIONAL
-- top-level `alias_urls` array, which THIS wrapper (not
-- sourcing_core.upsert_profile) merges into public.profiles.original_urls
-- after the identity call resolves the row. That merge is ported from
-- agent-kalamata migration 024; the first draft of this file mirrored only
-- that project's migration 023 and therefore silently dropped every alias
-- db.py's _prepare_profile_payload sends — caught by the real Codex gpt-5.5
-- review of PR #125 and confirmed live against this database.
--
-- The rest, verified against sourcing_core.upsert_profile's
-- own SQL body: linkedin_url (the identity key, read separately as
-- input_linkedin_url — NOT from inside input_profile), name, current_title,
-- current_company, location, enrichment_status, schema_version, raw_data
-- (object), skills, all_titles, all_employers, all_schools (arrays),
-- skills_blob, titles_blob, github_url, normalized_title, seniority_level.
-- It does NOT read or write current_start_date, current_years_at_company,
-- email, or email_source — those `profiles` columns exist but
-- sourcing_core.upsert_profile's own INSERT/UPDATE statements never
-- reference them. Real, deliberate gap versus the old raw-upsert path — see
-- db.py's _prepare_profile_payload docstring for the full list.
--
-- SHARED DB: profiles, sourcing_core.*, and sourcing_raw.* are shared with
-- the two autopilot siblings (daily-sourcing-autopilot-e2e,
-- smartlead-sourcing-autopilot) and Supanova. Per this repo's CLAUDE.md
-- shared-DB rule, the two autopilots hit the exact same
-- profiles_identity_before_write rejection and need the equivalent fix
-- (their own project-scoped wrapper mirroring this one, or adoption of it)
-- — flagged here, not applied anywhere but this project. Supanova's own
-- refresh job hit a narrower case of the same bug and was fixed separately
-- (a plain UPDATE, not this RPC — Supanova only ever touches profiles
-- already known to exist, so it never needed insert-or-update semantics).

CREATE OR REPLACE FUNCTION public.sourcingx_upsert_profiles(
  p_profiles jsonb            -- array of profile objects, each with a top-level linkedin_url
                              -- and an OPTIONAL top-level alias_urls array
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
SET statement_timeout = '20s'
AS $function$
declare
  v_max_batch constant int  := 100;
  v_writer    constant text := 'sourcingx';
  v_source    constant text := 'crustdata-enrich';
  v_provider  constant text := 'crustdata';
  v_schema_default constant text := 'crustdata-2025-11-01';

  v_total    int;
  v_run      uuid := pg_catalog.gen_random_uuid();
  v_approval text := 'SOURCINGX-' || to_char(now(), 'YYYYMMDD');
  v_elem     jsonb;
  v_prof     jsonb;
  v_sent     text;
  v_pid      uuid;
  v_action   text;
  v_curl     text;
  v_err      text;
  v_aliases  text[];
  v_alias_n  int;
  v_results  jsonb := '[]'::jsonb;
  v_ins int := 0; v_upd int := 0; v_noop int := 0; v_rej int := 0;
  v_err_n int := 0; v_unk int := 0;
begin
  if p_profiles is null or jsonb_typeof(p_profiles) <> 'array' then
    raise exception 'sourcingx_upsert_profiles: p_profiles must be a jsonb array (got %)',
      coalesce(jsonb_typeof(p_profiles), 'null');
  end if;

  v_total := jsonb_array_length(p_profiles);
  if v_total = 0 then
    return jsonb_build_object('received', 0, 'written', 0, 'results', '[]'::jsonb);
  end if;
  if v_total > v_max_batch then
    raise exception 'sourcingx_upsert_profiles: batch of % profiles exceeds the % cap per call',
      v_total, v_max_batch;
  end if;

  insert into sourcing_raw.profile_ingestion_runs
         (id, approval_id, run_name, writer, source, provider, schema_version,
          status, input_count, started_at, notes)
  values (v_run, v_approval, v_writer || '-upsert-' || v_run::text,
          v_writer, v_source, v_provider, v_schema_default,
          'started', v_total, now(),
          'via public.sourcingx_upsert_profiles');

  -- Iterate the RAW array WITH ORDINALITY so every input element gets a
  -- result row, including malformed ones.
  for v_elem in
    select e.elem
      from jsonb_array_elements(p_profiles) with ordinality as e(elem, ord)
     order by e.ord
  loop
    v_pid := null; v_action := null; v_curl := null; v_err := null; v_sent := null;
    v_aliases := null; v_alias_n := null;

    if jsonb_typeof(v_elem) <> 'object' then
      v_action := 'rejected';
      v_err := 'local-validation: array element is not a JSON object';
    elsif coalesce(btrim(v_elem ->> 'linkedin_url'), '') = '' then
      v_action := 'rejected';
      v_err := 'local-validation: missing linkedin_url';
    else
      v_sent := v_elem ->> 'linkedin_url';
      v_prof := public.engine_strip_contact(v_elem);
      begin
        select t.profile_id, t.action, t.canonical_url
          into v_pid, v_action, v_curl
          from sourcing_core.upsert_profile(
                 v_run, v_sent, v_prof, v_writer, v_source, v_provider,
                 coalesce(nullif(btrim(v_prof ->> 'schema_version'), ''), v_schema_default),
                 now()) t;
      exception when others then
        v_err := SQLERRM;      -- preserved deliberately, not discarded
        v_action := 'error';
      end;

      -- Merge any secondary LinkedIn URLs for this SAME person (e.g. the
      -- obfuscated ACoAA search-result form, or a caller-supplied
      -- original_url) into original_urls, now that the identity call above
      -- has resolved which profile row this person actually is.
      -- sourcing_core.upsert_profile CANNOT do this itself: it derives the
      -- identity handle from the ONE input_linkedin_url it is given and only
      -- registers THAT url, and an ACoAA url yields a DIFFERENT handle than a
      -- vanity url — sending the obfuscated form as the identity would create
      -- a SECOND profile row for the same person instead of aliasing them.
      -- Ported from agent-kalamata migration 024, which added exactly this
      -- block after its migration 023 (the file THIS migration was modelled
      -- on) was found to silently drop aliases.
      --
      -- TRIGGER SAFETY: sourcing_core.profiles_identity_before_write gates its
      -- identity-collision branch on `tg_op = 'INSERT'`, so it never runs on
      -- UPDATE. The only UPDATE-time check is the immutability guard comparing
      -- linkedin_handle(old.linkedin_url) with linkedin_handle(new.linkedin_url).
      -- This statement sets ONLY original_urls and never assigns linkedin_url,
      -- so new.linkedin_url is byte-identical to old.linkedin_url and that
      -- exception can never fire from this path.
      if v_pid is not null and v_action in ('inserted', 'updated', 'noop')
         and jsonb_typeof(v_elem -> 'alias_urls') = 'array' then
        begin
          -- Ignore non-string elements and blanks rather than failing the row.
          select array_agg(distinct btrim(elem_val #>> '{}'))
            into v_aliases
            from jsonb_array_elements(v_elem -> 'alias_urls') as elem_val
           where jsonb_typeof(elem_val) = 'string'
             and nullif(btrim(elem_val #>> '{}'), '') is not null;

          if v_aliases is not null and array_length(v_aliases, 1) > 0 then
            -- aliases_merged = how many aliases this payload CARRIED into the
            -- merge (post-dedup), not a before/after diff of original_urls.
            v_alias_n := array_length(v_aliases, 1);

            update public.profiles p
               set original_urls = (
                 select array_agg(distinct a)
                   from unnest(coalesce(p.original_urls, '{}'::text[]) || v_aliases) as t(a)
                  where nullif(btrim(a), '') is not null)
             where p.id = v_pid;
          else
            v_alias_n := 0;
          end if;
        exception when others then
          -- An alias-merge failure must NEVER flip a successful profile save
          -- into a failure: the save itself already succeeded, and THAT is the
          -- outcome this RPC exists to guarantee. Record the reason only.
          v_err := coalesce(v_err || '; ', '') || 'alias-merge-failed: ' || SQLERRM;
          v_alias_n := 0;
        end;
      end if;
    end if;

    case coalesce(v_action, 'rejected')
      when 'inserted' then v_ins  := v_ins  + 1;
      when 'updated'  then v_upd  := v_upd  + 1;
      when 'noop'     then v_noop := v_noop + 1;
      when 'rejected' then v_rej  := v_rej  + 1;
      when 'error'    then v_err_n := v_err_n + 1;
      else v_unk := v_unk + 1;   -- never fold an unrecognised action into rejected
    end case;

    v_results := v_results || jsonb_build_object(
      'sent_url',       v_sent,
      'canonical_url',  v_curl,
      'profile_id',     v_pid,
      'action',         coalesce(v_action, 'rejected'),
      'error',          v_err,
      'aliases_merged', v_alias_n
    );
  end loop;

  update sourcing_raw.profile_ingestion_runs r
     set status = 'completed', finished_at = now(),
         inserted_count = v_ins, updated_count = v_upd,
         noop_count = v_noop, rejected_count = v_rej,
         conflict_count = v_err_n
   where r.id = v_run;

  return jsonb_build_object(
    'received',       v_total,
    'written',        v_ins + v_upd,
    'noop',           v_noop,
    'rejected',       v_rej,
    'errors',         v_err_n,
    'unknown_action', v_unk,
    'ingestion_run',  v_run,
    'results',        v_results
  );
end
$function$;

revoke all on function public.sourcingx_upsert_profiles(jsonb) from public;
grant execute on function public.sourcingx_upsert_profiles(jsonb) to service_role;

-- Rollback: drop function public.sourcingx_upsert_profiles(jsonb);
