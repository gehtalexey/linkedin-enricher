-- Create screening_prompts table for role-specific AI screening prompts
CREATE TABLE IF NOT EXISTS screening_prompts (
    id SERIAL PRIMARY KEY,
    role_type TEXT UNIQUE NOT NULL,
    name TEXT,
    prompt_text TEXT NOT NULL,
    keywords TEXT[] DEFAULT '{}',
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create index for faster lookups
CREATE INDEX IF NOT EXISTS idx_screening_prompts_role ON screening_prompts(role_type);
CREATE INDEX IF NOT EXISTS idx_screening_prompts_default ON screening_prompts(is_default) WHERE is_default = TRUE;

-- Enable RLS
ALTER TABLE screening_prompts ENABLE ROW LEVEL SECURITY;

-- Allow all operations for authenticated users (adjust as needed)
CREATE POLICY "Allow all for authenticated" ON screening_prompts
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- Grant permissions
GRANT ALL ON screening_prompts TO authenticated;
GRANT ALL ON screening_prompts TO anon;
GRANT USAGE, SELECT ON SEQUENCE screening_prompts_id_seq TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE screening_prompts_id_seq TO anon;
