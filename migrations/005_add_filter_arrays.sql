-- Migration: Add pre-flattened arrays for filtering
-- Crustdata provides these arrays ready to use: all_employers, all_titles, all_schools, skills
-- TEXT[] columns with GIN indexes enable fast filtering queries
--
-- Run in Supabase SQL Editor

-- Add array columns for filtering
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS all_employers TEXT[];
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS all_titles TEXT[];
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS all_schools TEXT[];
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS skills TEXT[];

-- Create GIN indexes for array queries
-- Example query: WHERE 'Google' = ANY(all_employers)
-- Example query: WHERE all_employers @> ARRAY['Google', 'Meta']
CREATE INDEX IF NOT EXISTS idx_profiles_all_employers ON profiles USING GIN (all_employers);
CREATE INDEX IF NOT EXISTS idx_profiles_all_titles ON profiles USING GIN (all_titles);
CREATE INDEX IF NOT EXISTS idx_profiles_all_schools ON profiles USING GIN (all_schools);
CREATE INDEX IF NOT EXISTS idx_profiles_skills ON profiles USING GIN (skills);

-- Backfill existing profiles from raw_data
-- This extracts the arrays from raw_data JSONB into the new columns
UPDATE profiles
SET
  all_employers = (
    SELECT array_agg(elem)::TEXT[]
    FROM jsonb_array_elements_text(COALESCE(raw_data->'all_employers', '[]'::jsonb)) AS elem
    WHERE elem IS NOT NULL AND elem != ''
  ),
  all_titles = (
    SELECT array_agg(elem)::TEXT[]
    FROM jsonb_array_elements_text(COALESCE(raw_data->'all_titles', '[]'::jsonb)) AS elem
    WHERE elem IS NOT NULL AND elem != ''
  ),
  all_schools = (
    SELECT array_agg(elem)::TEXT[]
    FROM jsonb_array_elements_text(COALESCE(raw_data->'all_schools', '[]'::jsonb)) AS elem
    WHERE elem IS NOT NULL AND elem != ''
  ),
  skills = (
    SELECT array_agg(elem)::TEXT[]
    FROM jsonb_array_elements_text(COALESCE(raw_data->'skills', '[]'::jsonb)) AS elem
    WHERE elem IS NOT NULL AND elem != ''
  )
WHERE raw_data IS NOT NULL;
