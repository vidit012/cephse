-- CephFS Tiering System Database Schema
-- PostgreSQL 14+

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "btree_gin";

-- Main file metadata table
CREATE TABLE IF NOT EXISTS file_metadata (
    inode BIGINT PRIMARY KEY,
    path TEXT NOT NULL,
    last_access TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    current_pool TEXT NOT NULL DEFAULT 'data',
    target_pool TEXT,
    size_bytes BIGINT DEFAULT 0,
    access_count BIGINT DEFAULT 1,
    needs_migration BOOLEAN DEFAULT FALSE,
    migration_attempts INT DEFAULT 0,
    last_migration_attempt TIMESTAMPTZ,
    last_migration_success TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_needs_migration 
    ON file_metadata(needs_migration, last_access) 
    WHERE needs_migration = TRUE;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_last_access 
    ON file_metadata(last_access);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_current_pool 
    ON file_metadata(current_pool);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_path_gin 
    ON file_metadata USING gin(path gin_trgm_ops);

-- Tiering policies table
CREATE TABLE IF NOT EXISTS tiering_policies (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    source_pool TEXT NOT NULL,
    target_pool TEXT NOT NULL,
    age_days INT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    priority INT DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Insert default policies
INSERT INTO tiering_policies (name, source_pool, target_pool, age_days, priority)
VALUES 
    ('hot_to_warm', 'hot', 'data', 7, 200),
    ('warm_to_cold', 'data', 'cold', 30, 100)
ON CONFLICT (name) DO NOTHING;

-- Migration history (audit log)
CREATE TABLE IF NOT EXISTS migration_history (
    id BIGSERIAL PRIMARY KEY,
    inode BIGINT NOT NULL,
    path TEXT NOT NULL,
    from_pool TEXT NOT NULL,
    to_pool TEXT NOT NULL,
    size_bytes BIGINT,
    success BOOLEAN NOT NULL,
    error_message TEXT,
    duration_ms INT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_migration_history_inode 
    ON migration_history(inode);

CREATE INDEX IF NOT EXISTS idx_migration_history_completed 
    ON migration_history(completed_at DESC);

-- Statistics table (for monitoring)
CREATE TABLE IF NOT EXISTS tiering_stats (
    id SERIAL PRIMARY KEY,
    pool_name TEXT NOT NULL,
    file_count BIGINT NOT NULL,
    total_bytes BIGINT NOT NULL,
    avg_age_days NUMERIC(10,2),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tiering_stats_recorded 
    ON tiering_stats(recorded_at DESC);

-- Function: Update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger: Auto-update updated_at
CREATE TRIGGER update_file_metadata_updated_at
    BEFORE UPDATE ON file_metadata
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Function: Apply tiering policies
CREATE OR REPLACE FUNCTION apply_tiering_policies()
RETURNS TABLE(inode BIGINT, target_pool TEXT, policy_name TEXT) AS $$
BEGIN
    RETURN QUERY
    UPDATE file_metadata fm
    SET 
        needs_migration = TRUE,
        target_pool = p.target_pool
    FROM tiering_policies p
    WHERE 
        fm.current_pool = p.source_pool
        AND EXTRACT(EPOCH FROM (NOW() - fm.last_access)) / 86400 > p.age_days
        AND p.enabled = TRUE
        AND (fm.needs_migration = FALSE OR fm.target_pool != p.target_pool)
    RETURNING fm.inode, fm.target_pool, p.name;
END;
$$ LANGUAGE plpgsql;

-- Function: Get migration candidates (with locking for workers)
CREATE OR REPLACE FUNCTION get_migration_candidates(batch_size INT DEFAULT 100)
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
           OR fm.last_migration_attempt < NOW() - INTERVAL '10 minutes')
      AND fm.migration_attempts < 3
    ORDER BY fm.last_access ASC
    LIMIT batch_size
    FOR UPDATE SKIP LOCKED;
END;
$$ LANGUAGE plpgsql;

-- Function: Record successful migration
CREATE OR REPLACE FUNCTION record_migration_success(
    p_inode BIGINT,
    p_target_pool TEXT,
    p_duration_ms INT
) RETURNS VOID AS $$
BEGIN
    -- Update file metadata
    UPDATE file_metadata
    SET 
        current_pool = p_target_pool,
        needs_migration = FALSE,
        target_pool = NULL,
        migration_attempts = 0,
        last_migration_success = NOW()
    WHERE inode = p_inode;
    
    -- Record history
    INSERT INTO migration_history (inode, path, from_pool, to_pool, success, duration_ms)
    SELECT inode, path, current_pool, p_target_pool, TRUE, p_duration_ms
    FROM file_metadata WHERE inode = p_inode;
END;
$$ LANGUAGE plpgsql;

-- Function: Record failed migration
CREATE OR REPLACE FUNCTION record_migration_failure(
    p_inode BIGINT,
    p_error TEXT
) RETURNS VOID AS $$
BEGIN
    UPDATE file_metadata
    SET 
        migration_attempts = migration_attempts + 1,
        last_migration_attempt = NOW()
    WHERE inode = p_inode;
    
    INSERT INTO migration_history (inode, path, from_pool, to_pool, success, error_message)
    SELECT inode, path, current_pool, target_pool, FALSE, p_error
    FROM file_metadata WHERE inode = p_inode;
END;
$$ LANGUAGE plpgsql;

-- Materialized view: Current pool statistics
CREATE MATERIALIZED VIEW IF NOT EXISTS pool_statistics AS
SELECT 
    current_pool,
    COUNT(*) as file_count,
    SUM(size_bytes) as total_bytes,
    AVG(EXTRACT(EPOCH FROM (NOW() - last_access)) / 86400) as avg_age_days,
    MIN(last_access) as oldest_access,
    MAX(last_access) as newest_access
FROM file_metadata
GROUP BY current_pool;

CREATE UNIQUE INDEX ON pool_statistics (current_pool);

-- Refresh statistics periodically
CREATE OR REPLACE FUNCTION refresh_pool_statistics()
RETURNS VOID AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY pool_statistics;
    
    INSERT INTO tiering_stats (pool_name, file_count, total_bytes, avg_age_days)
    SELECT current_pool, file_count, total_bytes, avg_age_days
    FROM pool_statistics;
END;
$$ LANGUAGE plpgsql;

COMMENT ON TABLE file_metadata IS 'Main table tracking all files and their tiering status';
COMMENT ON TABLE tiering_policies IS 'Configurable policies for automatic tiering';
COMMENT ON TABLE migration_history IS 'Audit log of all migration attempts';
COMMENT ON TABLE tiering_stats IS 'Historical statistics for monitoring and analytics';
