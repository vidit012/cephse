-- Simplified CephFS Storage Tiering Schema (TEST MODE - 3 minute intervals)
-- Single table design: RocksDB logs access → Writer thread updates PostgreSQL

-- Drop existing tables
DROP TABLE IF EXISTS migration_history CASCADE;
DROP TABLE IF EXISTS tiering_stats CASCADE;
DROP TABLE IF EXISTS tiering_policies CASCADE;
DROP MATERIALIZED VIEW IF EXISTS pool_statistics CASCADE;
DROP TABLE IF EXISTS file_metadata CASCADE;

-- Single main table with all necessary columns
CREATE TABLE file_metadata (
    inode BIGINT PRIMARY KEY,                          -- UID (unique file identifier)
    path TEXT NOT NULL,                                -- File path or dentry inode name
    current_pool TEXT NOT NULL DEFAULT 'cephfs.tiercephfs.data',  -- Current storage pool
    target_pool TEXT,                                  -- Target pool for migration
    last_access TIMESTAMPTZ NOT NULL DEFAULT NOW(),    -- Last access time
    needs_migration BOOLEAN DEFAULT FALSE              -- Migration flag
);

-- Indexes for fast queries
CREATE INDEX idx_needs_migration ON file_metadata(needs_migration, last_access) 
    WHERE needs_migration = TRUE;
CREATE INDEX idx_last_access ON file_metadata(last_access);
CREATE INDEX idx_current_pool ON file_metadata(current_pool);

-- Simple function to mark files for migration based on age
CREATE OR REPLACE FUNCTION mark_files_for_migration()
RETURNS TABLE(marked_count BIGINT) AS $$
DECLARE
    marked BIGINT := 0;
BEGIN
    -- Mark data → warm (files older than 3 minutes in data pool)
    WITH updated AS (
        UPDATE file_metadata
        SET needs_migration = TRUE,
            target_pool = 'cephfs.tiercephfs.warm'
        WHERE current_pool = 'cephfs.tiercephfs.data'
          AND needs_migration = FALSE
          AND last_access < NOW() - INTERVAL '3 minutes'
        RETURNING 1
    )
    SELECT COUNT(*) INTO marked FROM updated;
    
    -- Mark warm → cold (files older than 3 minutes in warm pool)
    WITH updated AS (
        UPDATE file_metadata
        SET needs_migration = TRUE,
            target_pool = 'cephfs.tiercephfs.cold'
        WHERE current_pool = 'cephfs.tiercephfs.warm'
          AND needs_migration = FALSE
          AND last_access < NOW() - INTERVAL '3 minutes'
        RETURNING 1
    )
    SELECT COUNT(*) INTO marked FROM updated;
    
    -- Return total files marked
    SELECT COUNT(*) INTO marked FROM file_metadata WHERE needs_migration = TRUE;
    RETURN QUERY SELECT marked;
END;
$$ LANGUAGE plpgsql;

-- Function to get files needing migration (with row-level locking)
CREATE OR REPLACE FUNCTION get_migration_batch(batch_size INT DEFAULT 10)
RETURNS TABLE(
    inode BIGINT,
    path TEXT,
    current_pool TEXT,
    target_pool TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        fm.inode,
        fm.path,
        fm.current_pool,
        fm.target_pool
    FROM file_metadata fm
    WHERE fm.needs_migration = TRUE
    ORDER BY fm.last_access ASC
    LIMIT batch_size
    FOR UPDATE SKIP LOCKED;
END;
$$ LANGUAGE plpgsql;

-- Function to mark migration complete
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
GRANT SELECT, INSERT, UPDATE, DELETE ON file_metadata TO tiering_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO tiering_user;

-- Display configuration
DO $$
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== SIMPLIFIED SCHEMA (TEST MODE) ===';
    RAISE NOTICE 'Single table: file_metadata';
    RAISE NOTICE 'Columns:';
    RAISE NOTICE '  - inode (UID)';
    RAISE NOTICE '  - path (file path or dentry inode name)';
    RAISE NOTICE '  - current_pool';
    RAISE NOTICE '  - target_pool';
    RAISE NOTICE '  - last_access';
    RAISE NOTICE '  - needs_migration';
    RAISE NOTICE '';
    RAISE NOTICE 'Migration timeline (3-minute intervals):';
    RAISE NOTICE '  t=0:    File in data pool';
    RAISE NOTICE '  t=3min: Marked for warm pool';
    RAISE NOTICE '  t=6min: Marked for cold pool';
    RAISE NOTICE '';
END $$;
