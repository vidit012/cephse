-- Two-table schema: Hot log + Cold metadata
-- file_access_log: Fast append-only writes from eBPF
-- file_metadata: Aggregated data updated by writer thread

-- Drop existing
DROP TABLE IF EXISTS file_access_log CASCADE;
DROP TABLE IF EXISTS file_metadata CASCADE;

-- HOT TABLE: Append-only log of file accesses (like RocksDB)
CREATE TABLE file_access_log (
    id BIGSERIAL PRIMARY KEY,
    uid INTEGER NOT NULL,
    inode BIGINT NOT NULL,
    path TEXT NOT NULL,
    access_time TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_log_inode ON file_access_log(inode);
CREATE INDEX idx_log_access_time ON file_access_log(access_time DESC);

-- COLD TABLE: Aggregated file metadata (main table)
CREATE TABLE file_metadata (
    inode BIGINT PRIMARY KEY,
    path TEXT NOT NULL,
    current_pool TEXT NOT NULL DEFAULT 'cephfs.tiercephfs.data',
    target_pool TEXT,
    last_access TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    needs_migration BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_needs_migration ON file_metadata(needs_migration, last_access) 
    WHERE needs_migration = TRUE;
CREATE INDEX idx_last_access ON file_metadata(last_access);
CREATE INDEX idx_current_pool ON file_metadata(current_pool);

-- Function: Aggregate log → metadata (called by writer thread)
CREATE OR REPLACE FUNCTION aggregate_access_log()
RETURNS TABLE(processed_count BIGINT) AS $$
DECLARE
    processed BIGINT := 0;
BEGIN
    -- Upsert latest access time from log to metadata
    WITH latest_access AS (
        SELECT 
            inode,
            path,
            MAX(access_time) as last_access
        FROM file_access_log
        GROUP BY inode, path
    )
    INSERT INTO file_metadata (inode, path, last_access, current_pool)
    SELECT inode, path, last_access, 'cephfs.tiercephfs.data'
    FROM latest_access
    ON CONFLICT (inode) DO UPDATE 
    SET last_access = EXCLUDED.last_access,
        path = EXCLUDED.path;
    
    -- Count processed
    SELECT COUNT(*) INTO processed FROM file_access_log;
    
    -- Clear log after aggregation
    TRUNCATE file_access_log;
    
    RETURN QUERY SELECT processed;
END;
$$ LANGUAGE plpgsql;

-- Function: Mark files for migration (PROMOTION + DEMOTION)
CREATE OR REPLACE FUNCTION mark_files_for_migration()
RETURNS TABLE(marked_count BIGINT) AS $$
DECLARE
    marked BIGINT := 0;
BEGIN
    -- DEMOTION: warm/cold → data (if accessed recently)
    -- Any file in warm/cold accessed within 3 minutes should move to data
    UPDATE file_metadata
    SET needs_migration = TRUE,
        target_pool = 'cephfs.tiercephfs.data'
    WHERE current_pool IN ('cephfs.tiercephfs.warm', 'cephfs.tiercephfs.cold')
      AND needs_migration = FALSE
      AND last_access >= NOW() - INTERVAL '3 minutes';
    
    -- PROMOTION: data → warm (older than 3 minutes, not recently accessed)
    UPDATE file_metadata
    SET needs_migration = TRUE,
        target_pool = 'cephfs.tiercephfs.warm'
    WHERE current_pool = 'cephfs.tiercephfs.data'
      AND needs_migration = FALSE
      AND last_access < NOW() - INTERVAL '3 minutes';
    
    -- PROMOTION: warm → cold (older than 6 minutes total)
    UPDATE file_metadata
    SET needs_migration = TRUE,
        target_pool = 'cephfs.tiercephfs.cold'
    WHERE current_pool = 'cephfs.tiercephfs.warm'
      AND needs_migration = FALSE
      AND last_access < NOW() - INTERVAL '6 minutes';
    
    SELECT COUNT(*) INTO marked FROM file_metadata WHERE needs_migration = TRUE;
    RETURN QUERY SELECT marked;
END;
$$ LANGUAGE plpgsql;

-- Function: Get migration batch
CREATE OR REPLACE FUNCTION get_migration_batch(batch_size INT DEFAULT 10)
RETURNS TABLE(
    inode BIGINT,
    path TEXT,
    current_pool TEXT,
    target_pool TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT fm.inode, fm.path, fm.current_pool, fm.target_pool
    FROM file_metadata fm
    WHERE fm.needs_migration = TRUE
    ORDER BY fm.last_access ASC
    LIMIT batch_size
    FOR UPDATE SKIP LOCKED;
END;
$$ LANGUAGE plpgsql;

-- Function: Mark migration complete
CREATE OR REPLACE FUNCTION migration_complete(p_inode BIGINT, p_target_pool TEXT)
RETURNS VOID AS $$
BEGIN
    UPDATE file_metadata
    SET current_pool = p_target_pool,
        target_pool = NULL,
        needs_migration = FALSE
    WHERE inode = p_inode;
END;
$$ LANGUAGE plpgsql;

-- Grant permissions
GRANT SELECT, INSERT, DELETE, TRUNCATE ON file_access_log TO tiering_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON file_metadata TO tiering_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO tiering_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO tiering_user;

-- Info
DO $$
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== TWO-TABLE ARCHITECTURE ===';
    RAISE NOTICE 'Hot Table: file_access_log (append-only, like RocksDB)';
    RAISE NOTICE 'Cold Table: file_metadata (aggregated)';
    RAISE NOTICE '';
    RAISE NOTICE 'Workflow:';
    RAISE NOTICE '  1. eBPF writes to file_access_log (fast inserts)';
    RAISE NOTICE '  2. Writer thread calls aggregate_access_log() every 60s';
    RAISE NOTICE '  3. Policy engine marks files for migration';
    RAISE NOTICE '  4. Migration workers move files between pools';
    RAISE NOTICE '';
END $$;
