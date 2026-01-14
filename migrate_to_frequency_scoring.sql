-- ============================================================================
-- Schema Migration: Add Frequency-Based Scoring
-- ============================================================================

-- Step 1: Add new columns to file_metadata
ALTER TABLE file_metadata 
ADD COLUMN IF NOT EXISTS access_freq INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS score FLOAT DEFAULT 0.0;

-- Create index on score for fast sorting
CREATE INDEX IF NOT EXISTS idx_file_metadata_score ON file_metadata(score DESC);


-- ============================================================================
-- Step 2: Score Calculation Function (Easy to modify formula)
-- ============================================================================
CREATE OR REPLACE FUNCTION calculate_score(
    access_frequency INTEGER,
    last_access_time TIMESTAMP DEFAULT NULL
) RETURNS FLOAT AS $$
DECLARE
    max_freq INTEGER;
    normalized_freq FLOAT;
    recency_factor FLOAT;
    final_score FLOAT;
BEGIN
    -- Get global max frequency for normalization
    SELECT COALESCE(MAX(access_freq), 1) INTO max_freq FROM file_metadata;
    
    -- Normalize frequency (0 to 1)
    normalized_freq := access_frequency::FLOAT / GREATEST(max_freq, 1)::FLOAT;
    
    -- ========================================================================
    -- SCORE FORMULA (MODIFY HERE TO CHANGE SCORING)
    -- ========================================================================
    -- Current: score = 0.90 * frequency_normalized
    -- 
    -- To add recency later (10% weight):
    -- recency_factor := 1.0 / (1.0 + EXTRACT(EPOCH FROM (NOW() - last_access_time)) / 3600);
    -- final_score := (0.90 * normalized_freq) + (0.10 * recency_factor);
    -- ========================================================================
    
    final_score := 0.90 * normalized_freq;
    
    RETURN final_score;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- Step 3: Updated Aggregation Function with Frequency Tracking
-- ============================================================================
CREATE OR REPLACE FUNCTION aggregate_access_log()
RETURNS INTEGER AS $$
DECLARE
    processed_count INTEGER;
BEGIN
    -- Aggregate access logs and update file_metadata
    -- Count accesses per file and update frequency
    INSERT INTO file_metadata (inode, path, current_pool, last_access, access_freq, score)
    SELECT 
        inode,
        path,
        current_pool,
        MAX(access_time) as last_access,
        COUNT(*) as new_accesses,  -- Count accesses in this batch
        0.0 as score  -- Will be calculated next
    FROM file_access_log
    GROUP BY inode, path, current_pool
    ON CONFLICT (inode) DO UPDATE
    SET 
        last_access = EXCLUDED.last_access,
        path = EXCLUDED.path,
        current_pool = EXCLUDED.current_pool,
        -- CRITICAL: Only increment frequency if NOT being migrated
        access_freq = CASE 
            WHEN file_metadata.needs_migration = FALSE 
            THEN file_metadata.access_freq + EXCLUDED.access_freq
            ELSE file_metadata.access_freq  -- Don't increment during migration
        END;
    
    -- Recalculate scores for all updated files
    UPDATE file_metadata
    SET score = calculate_score(access_freq, last_access)
    WHERE inode IN (SELECT DISTINCT inode FROM file_access_log);
    
    -- Delete processed access logs
    DELETE FROM file_access_log;
    GET DIAGNOSTICS processed_count = ROW_COUNT;
    
    RETURN processed_count;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- Step 4: Updated Policy Function with Score-Based Tiering
-- ============================================================================
CREATE OR REPLACE FUNCTION apply_tiering_policies()
RETURNS TABLE(promoted_to_warm INT, promoted_to_cold INT, demoted_to_data INT) AS $$
DECLARE
    to_warm INT := 0;
    to_cold INT := 0;
    to_data INT := 0;
    
    -- Score thresholds (easy to tune)
    HIGH_SCORE_THRESHOLD FLOAT := 0.7;   -- Top 30% files
    LOW_SCORE_THRESHOLD FLOAT := 0.3;    -- Bottom 70% files
BEGIN
    -- ========================================================================
    -- PROMOTION: High-score files to faster pools
    -- ========================================================================
    
    -- data → warm (files with high access frequency)
    UPDATE file_metadata
    SET needs_migration = TRUE, 
        target_pool = 'cephfs.tiercephfs.warm'
    WHERE current_pool = 'cephfs.tiercephfs.data'
      AND score >= HIGH_SCORE_THRESHOLD
      AND needs_migration = FALSE;
    
    GET DIAGNOSTICS to_warm = ROW_COUNT;
    
    
    -- warm → cold (files with very low access frequency)
    UPDATE file_metadata
    SET needs_migration = TRUE,
        target_pool = 'cephfs.tiercephfs.cold'
    WHERE current_pool = 'cephfs.tiercephfs.warm'
      AND score < LOW_SCORE_THRESHOLD
      AND needs_migration = FALSE;
    
    GET DIAGNOSTICS to_cold = ROW_COUNT;
    
    
    -- ========================================================================
    -- DEMOTION: Low-score files to slower pools
    -- ========================================================================
    
    -- cold/warm → data (files with high access frequency - bring back to hot storage)
    UPDATE file_metadata
    SET needs_migration = TRUE,
        target_pool = 'cephfs.tiercephfs.data'
    WHERE current_pool IN ('cephfs.tiercephfs.warm', 'cephfs.tiercephfs.cold')
      AND score >= HIGH_SCORE_THRESHOLD
      AND needs_migration = FALSE;
    
    GET DIAGNOSTICS to_data = ROW_COUNT;
    
    
    RETURN QUERY SELECT to_warm, to_cold, to_data;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- Step 5: Function to Reset Frequency After Migration
-- ============================================================================
CREATE OR REPLACE FUNCTION reset_file_after_migration(
    old_inode_param BIGINT,
    new_inode_param BIGINT,
    new_pool_param TEXT,
    preserved_last_access TIMESTAMP
) RETURNS VOID AS $$
BEGIN
    -- Delete old inode entry (if inode changed)
    IF old_inode_param != new_inode_param THEN
        DELETE FROM file_metadata WHERE inode = old_inode_param;
    END IF;
    
    -- Update new inode entry
    -- CRITICAL: Reset access_freq to 0, recalculate score
    UPDATE file_metadata
    SET current_pool = new_pool_param,
        target_pool = NULL,
        needs_migration = FALSE,
        access_freq = 0,  -- Reset frequency after migration
        score = 0.0,      -- Reset score
        last_access = preserved_last_access  -- Preserve original access time
    WHERE inode = new_inode_param;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- Verification Queries
-- ============================================================================

-- Check schema
SELECT column_name, data_type, column_default 
FROM information_schema.columns 
WHERE table_name = 'file_metadata'
ORDER BY ordinal_position;

-- Test score calculation
SELECT calculate_score(100, NOW() - INTERVAL '1 hour') as test_score;

-- Show top scored files
SELECT inode, path, current_pool, access_freq, score, last_access
FROM file_metadata
ORDER BY score DESC
LIMIT 10;

COMMIT;
