-- API Usage Logs Table for LinkedIn Enricher
-- Run this SQL in your Supabase SQL Editor to create the usage tracking table

CREATE TABLE IF NOT EXISTS api_usage_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_count INTEGER DEFAULT 1,
    credits_used NUMERIC,
    tokens_input INTEGER,
    tokens_output INTEGER,
    cost_usd NUMERIC(10, 6),
    status TEXT DEFAULT 'success',
    error_message TEXT,
    response_time_ms INTEGER,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for common queries
CREATE INDEX IF NOT EXISTS idx_usage_provider_date ON api_usage_logs(provider, created_at);

-- Index for filtering by status
CREATE INDEX IF NOT EXISTS idx_usage_status ON api_usage_logs(status);

-- Comments for documentation
COMMENT ON TABLE api_usage_logs IS 'Tracks API usage across all providers (Crustdata, SalesQL, OpenAI, PhantomBuster)';
COMMENT ON COLUMN api_usage_logs.provider IS 'API provider: crustdata, salesql, openai, phantombuster';
COMMENT ON COLUMN api_usage_logs.operation IS 'Type of operation: enrich, email_lookup, screen, scrape, launch';
COMMENT ON COLUMN api_usage_logs.credits_used IS 'Credits consumed (for credit-based APIs like Crustdata, SalesQL)';
COMMENT ON COLUMN api_usage_logs.tokens_input IS 'Input tokens (for OpenAI)';
COMMENT ON COLUMN api_usage_logs.tokens_output IS 'Output tokens (for OpenAI)';
COMMENT ON COLUMN api_usage_logs.cost_usd IS 'Calculated cost in USD';
COMMENT ON COLUMN api_usage_logs.response_time_ms IS 'API response time in milliseconds';
COMMENT ON COLUMN api_usage_logs.metadata IS 'Additional JSON metadata (profiles_enriched, emails_found, model, etc.)';
