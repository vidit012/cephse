-- CephFS Storage Tiering Database Schema (TEST MODE - 3 minute intervals)
-- PostgreSQL 14+

-- Main file tracking table
CREATE TABLE IF NOT EXISTS file_metadata (
    inode BIGINT PRIMARY KEY,
    path TEXT NOT NULL,
    size_bytes BIGINT DEFAULT 0,
    last_access TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    current_pool TEXT NOT NULL DEFAULT 'cephfs.tiercephfs.data',
    target_pool TEXT,
    needs_migration BOOLEAN DEFAULT FALSE,
    migration_attempts INT DEFAULT 0,
    last_migration_attempt TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_needs_migration ON file_metadata(needs_migration, current_pool, last_access) 
    WHERE needs_migration = TRUE;
CREATE INDEX IF NOT EXISTS idx_last_access ON file_metadata(last_access);
CREATE INDEX IF NOT EXISTS idx_current_pool ON file_metadata(current_pool);
CREATE INDEX IF NOT EXISTS idx_path_gin ON file_metadata USING gin(path gin_trgm_ops);

-- Tiering policies table (TEST MODE - minutes instead of days)
CREATE TABLE IF NOT EXISTS tiering_policies (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    source_pool TEXT NOT NULL,
    target_pool TEXT NOT NULL,
    age_minutes INT NOT NULL,  -- CHANGED: Using minutes for testing
    enabled BOOLEAN DEFAULT TRUE,
    priority INT DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Insert TEST policies (3-minute intervals)
INSERT INTO tiering_policies (name, source_pool, target_pool, age_minutes, priority, enabled) VALUES
    ('data_to_warm', 'cephfs.tiercephfs.data', 'cephfs.tiercephfs.warm', 3, 200, TRUE),
    ('warm_to_cold', 'cephfs.tiercephfs.warm', 'cephfs.tiercephfs.cold', 3, 100, TRUE)
ON CONFLICT (name) DO UPDATE SET
    source_pool = EXCLUDED.source_pool,
    target_pool = EXCLUDED.target_pool,
    age_minutes = EXCLUDED.age_minutes,
    priority = EXCLUDED.priority,
    enabled = EXCLUDED.enabled;

-- Migration history table (audit log)
CREATE TABLE IF NOT EXISTS migration_history (
    id BIGSERIAL PRIMARY KEY,
    inode BIGINT NOT NULL,
    path TEXT NOT NULL,
    from_pool TEXT NOT NULL,
    to_pool TEXT NOT NULL,
    size_bytes BIGINT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed')),
    error_message TEXT,
    duration_ms INT
);

CREATE INDEX IF NOT EXISTS idx_migration_inode ON migration_history(inode);
CREATE INDEX IF NOT EXISTS idx_migration_status ON migration_history(status, completed_at);
CREATE INDEX IF NOT EXISTS idx_migration_completed ON migration_history(completed_at DESC);

-- Statistics table
CREATE TABLE IF NOT EXISTS tiering_stats (
    id BIGSERIAL PRIMARY KEY,
    pool_name TEXT NOT NULL,
    file_count BIGINT NOT NULL,
    total_bytes BIGINT NOT NULL,
    avg_age_minutes NUMERIC(10,2),  -- CHANGED: minutes instead of days
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stats_recorded ON tiering_stats(recorded_at DESC);

-- Auto-update timestamp trigger
CREATE OR REPLACE FUNCTION update_modified_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_file_metadata_modtime ON file_metadata;
CREATE TRIGGER update_file_metadata_modtime
    BEFORE UPDATE ON file_metadata
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();

-- Apply tiering policies (TEST MODE - uses minutes)
CREATE OR REPLACE FUNCTION apply_tiering_policies()
RETURNS TABLE(marked_count BIGINT) AS $$
DECLARE
    policy RECORD;
    marked BIGINT := 0;
BEGIN
    -- Process each enabled policy in priority order
    FOR policy IN 
        SELECT * FROM tiering_policies 
        WHERE enabled = TRUE 
        ORDER BY priority DESC
    LOOP
        -- Mark files that meet policy criteria
        WITH updated AS (
            UPDATE file_metadata
            SET 
                needs_migration = TRUE,
                target_pool = policy.target_pool
            WHERE 
                current_pool = policy.source_pool
                AND needs_migration = FALSE
                AND migration_attempts < 3
                AND last_access < NOW() - (policy.age_minutes || ' minutes')::INTERVAL  -- CHANGED
            RETURNING 1
        )
        SELECT COUNT(*) INTO marked FROM updated;
        
        RAISE NOTICE 'Policy %: marked % files for migration from % to %',
            policy.name, marked, policy.source_pool, policy.target_pool;
    END LOOP;
    
    -- Return total marked files
    SELECT COUNT(*) INTO marked FROM file_metadata WHERE needs_migration = TRUE;
    RETURN QUERY SELECT marked;
END;
$$ LANGUAGE plpgsql;

-- Get migration candidates (with row-level locking for parallel workers)
CREATE OR REPLACE FUNCTION get_migration_candidates(batch_size INT DEFAULT 10)
RETURNS TABLE(
    inode BIGINT,
    path TEXT,
    current_pool TEXT,
    target_pool TEXT,
    size_bytes BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        fm.inode,
        fm.path,
        fm.current_pool,
        fm.target_pool,
        fm.size_bytes
    FROM file_metadata fm
    WHERE fm.needs_migration = TRUE
      AND (fm.last_migration_attempt IS NULL 
           OR fm.last_migration_attempt < NOW() - INTERVAL '5 minutes')
    ORDER BY fm.last_access ASC
    LIMIT batch_size
    FOR UPDATE SKIP LOCKED;
END;
$$ LANGUAGE plpgsql;

-- Record successful migration
CREATE OR REPLACE FUNCTION record_migration_success(
    p_inode BIGINT,
    p_to_pool TEXT,
    p_duration_ms INT
)
RETURNS VOID AS $$
BEGIN
    -- Update file metadata
    UPDATE file_metadata
    SET 
        current_pool = p_to_pool,
        target_pool = NULL,
        needs_migration = FALSE,
        migration_attempts = 0,
        last_migration_attempt = NOW()
    WHERE inode = p_inode;
    
    -- Log to history
    UPDATE migration_history
    SET 
        status = 'success',
        completed_at = NOW(),
        duration_ms = p_duration_ms
    WHERE inode = p_inode 
      AND status = 'pending'
      AND completed_at IS NULL;
END;
$$ LANGUAGE plpgsql;

-- Record failed migration
CREATE OR REPLACE FUNCTION record_migration_failure(
    p_inode BIGINT,
    p_error TEXT
)
RETURNS VOID AS $$
BEGIN
    -- Update file metadata
    UPDATE file_metadata
    SET 
        migration_attempts = migration_attempts + 1,
        last_migration_attempt = NOW(),
        needs_migration = CASE 
            WHEN migration_attempts >= 2 THEN FALSE  -- Give up after 3 attempts
            ELSE TRUE 
        END
    WHERE inode = p_inode;
    
    -- Log to history
    UPDATE migration_history
    SET 
        status = 'failed',
        completed_at = NOW(),
        error_message = p_error
    WHERE inode = p_inode 
      AND status = 'pending'
      AND completed_at IS NULL;
END;
$$ LANGUAGE plpgsql;

-- Refresh statistics
CREATE OR REPLACE FUNCTION refresh_pool_statistics()
RETURNS VOID AS $$
BEGIN
    INSERT INTO tiering_stats (pool_name, file_count, total_bytes, avg_age_minutes)
    SELECT 
        current_pool,
        COUNT(*),
        COALESCE(SUM(size_bytes), 0),
        AVG(EXTRACT(EPOCH FROM (NOW() - last_access)) / 60.0)  -- CHANGED: minutes
    FROM file_metadata
    GROUP BY current_pool;
END;
$$ LANGUAGE plpgsql;

-- Materialized view for quick statistics (TEST MODE - minutes)
CREATE MATERIALIZED VIEW IF NOT EXISTS pool_statistics AS
SELECT 
    current_pool,
    COUNT(*) as file_count,
    COALESCE(SUM(size_bytes), 0) as total_bytes,
    ROUND(AVG(EXTRACT(EPOCH FROM (NOW() - last_access)) / 60.0), 2) as avg_age_minutes,  -- CHANGED
    COUNT(*) FILTER (WHERE needs_migration = TRUE) as pending_migrations
FROM file_metadata
GROUP BY current_pool;

CREATE UNIQUE INDEX IF NOT EXISTS idx_pool_stats ON pool_statistics(current_pool);

-- Grant permissions
GRANT SELECT, INSERT, UPDATE ON file_metadata TO tiering_user;
GRANT SELECT ON tiering_policies TO tiering_user;
GRANT SELECT, INSERT, UPDATE ON migration_history TO tiering_user;
GRANT SELECT, INSERT ON tiering_stats TO tiering_user;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO tiering_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO tiering_user;

-- Display test configuration
DO $$
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== TEST MODE CONFIGURATION ===';
    RAISE NOTICE 'Policies use MINUTES instead of days for rapid testing:';
    RAISE NOTICE '  data → warm: 3 minutes';
    RAISE NOTICE '  warm → cold: 3 minutes (6 min total from data)';
    RAISE NOTICE '';
    RAISE NOTICE 'Timeline:';
    RAISE NOTICE '  t=0:    File created in data pool';
    RAISE NOTICE '  t=3min: Policy marks for warm pool';
    RAISE NOTICE '  t=6min: Policy marks for cold pool';
    RAISE NOTICE '';
END $$;
