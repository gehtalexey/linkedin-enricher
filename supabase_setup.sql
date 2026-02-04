-- LinkedIn Enricher Database Schema
-- Run this in Supabase SQL Editor

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Main profiles table
CREATE TABLE profiles (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  linkedin_url TEXT UNIQUE NOT NULL,

  -- Basic info (from PhantomBuster or enrichment)
  first_name TEXT,
  last_name TEXT,
  headline TEXT,
  location TEXT,
  current_title TEXT,
  current_company TEXT,
  current_years_in_role NUMERIC,
  skills TEXT,
  summary TEXT,

  -- Raw data storage (full API responses)
  phantombuster_data JSONB,
  crustdata_data JSONB,

  -- Screening results (from OpenAI)
  screening_score INTEGER,
  screening_fit_level TEXT,  -- 'Strong Fit', 'Good Fit', 'Partial Fit', 'Not a Fit'
  screening_summary TEXT,
  screening_reasoning TEXT,

  -- Email enrichment
  email TEXT,
  email_source TEXT,  -- 'salesql', 'crustdata', 'manual'

  -- Pipeline status
  status TEXT DEFAULT 'scraped' CHECK (status IN ('scraped', 'enriched', 'screened', 'contacted', 'archived')),

  -- Timestamps
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  enriched_at TIMESTAMPTZ,  -- NULL = never enriched, used for 6-month refresh
  screened_at TIMESTAMPTZ,
  contacted_at TIMESTAMPTZ,

  -- Source tracking
  source_search_id UUID
);

-- Index for fast lookups
CREATE INDEX idx_profiles_linkedin_url ON profiles(linkedin_url);
CREATE INDEX idx_profiles_status ON profiles(status);
CREATE INDEX idx_profiles_enriched_at ON profiles(enriched_at);
CREATE INDEX idx_profiles_screening_fit ON profiles(screening_fit_level);
CREATE INDEX idx_profiles_current_company ON profiles(current_company);

-- Track searches/batches from PhantomBuster
CREATE TABLE searches (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name TEXT NOT NULL,
  phantombuster_agent_id TEXT,
  search_url TEXT,
  profiles_found INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Link profiles to searches (many-to-many, profile can appear in multiple searches)
CREATE TABLE profile_searches (
  profile_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  search_id UUID REFERENCES searches(id) ON DELETE CASCADE,
  found_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (profile_id, search_id)
);

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER profiles_updated_at
  BEFORE UPDATE ON profiles
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at();

-- View: Profiles needing enrichment (never enriched OR older than 6 months)
CREATE VIEW profiles_needing_enrichment AS
SELECT *
FROM profiles
WHERE enriched_at IS NULL
   OR enriched_at < NOW() - INTERVAL '6 months';

-- View: Profiles needing screening (enriched but not screened)
CREATE VIEW profiles_needing_screening AS
SELECT *
FROM profiles
WHERE status = 'enriched'
  AND screening_score IS NULL;

-- View: Pipeline funnel stats
CREATE VIEW pipeline_stats AS
SELECT
  COUNT(*) FILTER (WHERE status = 'scraped') AS scraped,
  COUNT(*) FILTER (WHERE status = 'enriched') AS enriched,
  COUNT(*) FILTER (WHERE status = 'screened') AS screened,
  COUNT(*) FILTER (WHERE status = 'contacted') AS contacted,
  COUNT(*) FILTER (WHERE enriched_at < NOW() - INTERVAL '6 months') AS stale_profiles,
  COUNT(*) AS total
FROM profiles;

-- Row Level Security (optional, enable if needed)
-- ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE searches ENABLE ROW LEVEL SECURITY;

-- Grant access (adjust based on your Supabase setup)
-- GRANT ALL ON profiles TO authenticated;
-- GRANT ALL ON searches TO authenticated;
-- GRANT ALL ON profile_searches TO authenticated;
