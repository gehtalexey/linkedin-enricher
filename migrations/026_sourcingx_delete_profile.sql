-- Migration 026: public.sourcingx_delete_profile — safe profile deletion
-- around the identity-guard system's ON DELETE RESTRICT foreign keys.
--
-- STATUS: APPLIED to the shared Supabase database on 2026-08-03, with
-- Alexey's go-ahead, after two real Codex (gpt-5.5) review rounds (see
-- PR #126) — round 1 found the two deletes weren't atomic (a blocked
-- profile delete could permanently orphan the identity delete that ran
-- before it); fixed by merging both into one begin/exception block.
-- Same day as migrations/025 (the identity-aware upsert wrapper).
--
-- Rollback if ever needed:
--   drop function public.sourcingx_delete_profile(text);
--
-- WHY THIS EXISTS
-- db.py's delete_profile() does a plain `client.delete('profiles', {'linkedin_url': ...})`.
-- Since migrations/025 went live, every profile saved through
-- public.sourcingx_upsert_profiles gets a row in sourcing_core.profile_identity
-- (canonical_profile_id -> profiles.id, ON DELETE RESTRICT — see
-- profile_identity_canonical_profile_id_fkey). Deleting a profiles row that
-- has one now fails with a foreign-key violation, which delete_profile's
-- own try/except silently swallows into a `return False`. Confirmed live
-- 2026-08-03 via a real Opus-agent test after migrations/025 was applied:
-- deleting a freshly-inserted test profile failed until its
-- sourcing_core.profile_identity row was removed first. 12,147 of ~200k
-- profiles already have such a row (from other systems already using
-- sourcing_core.upsert_profile); every profile saved from now on will too,
-- since the identity-aware path is now SourcingX's only way to save.
--
-- WHAT THIS DOES
-- 1. Resolves the profile's id from linkedin_url.
-- 2. Deletes any sourcing_core.profile_identity row(s) whose
--    canonical_profile_id is that id — this is the row that blocks nearly
--    every deletion now, and it is pure identity bookkeeping (a
--    handle -> profile_id mapping with alias URLs), not PII or a record of
--    real-world activity. Safe to remove alongside the profile it points at.
-- 3. Deletes the public.profiles row.
--
-- WHAT THIS DELIBERATELY DOES NOT DO
-- sourcing_core.personal_contacts and sourcing_core.personal_contact_reconciliation
-- ALSO have ON DELETE RESTRICT foreign keys to profiles(id) (28 and 0 rows
-- respectively, live-checked 2026-08-03) — but this function does NOT touch
-- them. Unlike profile_identity, those tables can hold real contact records
-- (an "engine"-owned system SourcingX doesn't control), and RESTRICT there
-- looks like a deliberate guard against silently destroying tracked contact
-- data, not an incidental side effect of the identity system. If a
-- deletion still fails after this function runs, it is almost certainly
-- because of one of those two tables — this function returns that failure
-- to the Python caller explicitly (blocked=true, reason=SQLERRM) rather than
-- attempting to force it through, so a real (rare — 28 rows total) case
-- surfaces for a human decision instead of being silently worked around.
--
-- SHARED DB: sourcing_core.profile_identity is shared with the same
-- siblings as migrations/025 (daily-sourcing-autopilot-e2e,
-- smartlead-sourcing-autopilot, Supanova) — none of them currently call
-- delete_profile()-equivalent code as far as this repo's own db.py port
-- shows, but if they grow one, it hits the identical RESTRICT and needs
-- the same fix (or to call this same function).

CREATE OR REPLACE FUNCTION public.sourcingx_delete_profile(
  p_linkedin_url text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
SET statement_timeout = '10s'
AS $function$
declare
  v_id uuid;
  v_identity_rows_deleted int := 0;
begin
  if coalesce(btrim(p_linkedin_url), '') = '' then
    return jsonb_build_object('deleted', false, 'reason', 'missing linkedin_url');
  end if;

  select id into v_id from public.profiles where linkedin_url = p_linkedin_url;
  if v_id is null then
    return jsonb_build_object('deleted', false, 'reason', 'no profile found for this linkedin_url');
  end if;

  -- Both deletes live in ONE begin/exception block (real Codex gpt-5.5 finding,
  -- PR #126 round 1): the first draft used TWO separate blocks, so if the
  -- profiles delete failed (e.g. blocked by sourcing_core.personal_contacts),
  -- the identity delete from the FIRST block had already committed and was
  -- never rolled back — permanently orphaning a surviving profile's identity
  -- mapping while reporting deleted=false. A single block means a failure at
  -- EITHER statement rolls back BOTH via the implicit savepoint, so a blocked
  -- delete leaves everything exactly as it was.
  begin
    delete from sourcing_core.profile_identity where canonical_profile_id = v_id;
    get diagnostics v_identity_rows_deleted = row_count;

    delete from public.profiles where id = v_id;
  exception when others then
    return jsonb_build_object(
      'deleted', false,
      'blocked', true,
      'reason', SQLERRM,
      'identity_rows_deleted', 0
    );
  end;

  return jsonb_build_object(
    'deleted', true,
    'identity_rows_deleted', v_identity_rows_deleted
  );
end
$function$;

revoke all on function public.sourcingx_delete_profile(text) from public;
grant execute on function public.sourcingx_delete_profile(text) to service_role;

-- Rollback: drop function public.sourcingx_delete_profile(text);
