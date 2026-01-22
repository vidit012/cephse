--
-- PostgreSQL database dump
--

\restrict XoPREVriwCP6qg5Hgan64NJpezfoiVn93plPjHmZRAGI9DhV3fXPiRkPNZTmtas

-- Dumped from database version 16.11 (Ubuntu 16.11-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.11 (Ubuntu 16.11-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA public;


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: aggregate_access_log(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.aggregate_access_log() RETURNS integer
    LANGUAGE plpgsql
    AS $$
DECLARE
    total_files INTEGER := 0;
    cancelled_migrations INTEGER := 0;
    duplicates_removed INTEGER := 0;
BEGIN
    -- Step 1: Accumulate new accesses and count unique files
    -- FIX: Group by inode only, use subquery to get latest path per inode
    WITH latest_paths AS (
        SELECT DISTINCT ON (inode)
            inode,
            path,
            access_time
        FROM file_access_log
        ORDER BY inode, access_time DESC
    ),
    upsert_result AS (
        INSERT INTO file_metadata (inode, path, last_access, access_freq, creation_time)
        SELECT 
            fal.inode,
            lp.path,  -- Use the latest path from renamed files
            MAX(fal.access_time) as last_access,
            GREATEST(1, COUNT(*) / 2) as new_accesses,
            MIN(fal.access_time) as creation_time
        FROM file_access_log fal
        JOIN latest_paths lp ON fal.inode = lp.inode
        GROUP BY fal.inode, lp.path  -- Still need lp.path in GROUP BY for PostgreSQL
        ON CONFLICT (inode) DO UPDATE
        SET 
            last_access = EXCLUDED.last_access,
            access_freq = file_metadata.access_freq + EXCLUDED.access_freq,
            path = EXCLUDED.path  -- Update to latest path
        RETURNING 1
    )
    SELECT COUNT(*) INTO total_files FROM upsert_result;
    
    -- Step 1.5: CANCEL MIGRATION for files that were accessed after being marked
    WITH cancelled AS (
        UPDATE file_metadata
        SET needs_migration = FALSE,
            target_pool = NULL,
            score = calculate_score(access_freq),
            last_evaluation_time = NOW(),
            need_eval = TRUE
        WHERE needs_migration = TRUE
          AND access_freq > 0
        RETURNING 1
    )
    SELECT COUNT(*) INTO cancelled_migrations FROM cancelled;
    
    IF cancelled_migrations > 0 THEN
        RAISE NOTICE '✓ Cancelled % migrations due to new accesses', cancelled_migrations;
    END IF;
    
    -- Step 1.6: CLEANUP DUPLICATES
    SELECT cleanup_duplicate_paths() INTO duplicates_removed;
    
    -- Step 2: Evaluate files based on their current pool
    
    -- For DATA pool: 3-minute rule
    UPDATE file_metadata
    SET 
        score = calculate_score(access_freq),
        access_freq = 0,
        last_evaluation_time = NOW(),
        need_eval = TRUE
    WHERE current_pool = 'cephfs.tiercephfs.data'
      AND needs_migration = FALSE
      AND (
          (last_evaluation_time IS NULL AND NOW() - creation_time >= INTERVAL '3 minutes')
          OR
          (last_evaluation_time IS NOT NULL AND NOW() - last_evaluation_time >= INTERVAL '3 minutes')
      );
    
    -- For WARM pool: immediate if accessed, 3-minute rule if not
    UPDATE file_metadata
    SET 
        score = calculate_score(access_freq),
        access_freq = 0,
        last_evaluation_time = NOW(),
        need_eval = TRUE
    WHERE current_pool = 'cephfs.tiercephfs.warm'
      AND needs_migration = FALSE
      AND (
          (access_freq > 0)
          OR
          (access_freq = 0 AND (
              (last_evaluation_time IS NULL AND NOW() - creation_time >= INTERVAL '3 minutes')
              OR
              (last_evaluation_time IS NOT NULL AND NOW() - last_evaluation_time >= INTERVAL '3 minutes')
          ))
      );
    
    -- For COLD pool: immediate if accessed
    UPDATE file_metadata
    SET 
        score = calculate_score(access_freq),
        access_freq = 0,
        last_evaluation_time = NOW(),
        need_eval = TRUE
    WHERE current_pool = 'cephfs.tiercephfs.cold'
      AND needs_migration = FALSE
      AND access_freq > 0;
    
    DELETE FROM file_access_log;
    
    RETURN total_files;
END;
$$;


--
-- Name: apply_tiering_policies(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.apply_tiering_policies() RETURNS TABLE(to_warm_from_data bigint, to_cold_from_warm bigint, to_data_from_warm bigint, to_warm_from_cold bigint, stayed_in_warm bigint)
    LANGUAGE plpgsql
    AS $$
DECLARE
    warm_from_data BIGINT := 0;
    cold_from_warm BIGINT := 0;
    data_from_warm BIGINT := 0;
    warm_from_cold BIGINT := 0;
    stayed_warm BIGINT := 0;
BEGIN
    -- Rule 1: DATA → WARM (score < 9)
    WITH updated AS (
        UPDATE file_metadata
        SET target_pool = 'cephfs.tiercephfs.warm',
            needs_migration = TRUE,
            need_eval = FALSE
        WHERE current_pool = 'cephfs.tiercephfs.data'
          AND score < 9
          AND needs_migration = FALSE
          AND need_eval = TRUE
        RETURNING 1
    )
    SELECT COUNT(*) INTO warm_from_data FROM updated;

    -- Rule 2: WARM → COLD (score < 4.5)
    -- REMOVED: 3-minute wait check - aggregator handles timing via need_eval flag
    WITH updated AS (
        UPDATE file_metadata
        SET target_pool = 'cephfs.tiercephfs.cold',
            needs_migration = TRUE,
            need_eval = FALSE
        WHERE current_pool = 'cephfs.tiercephfs.warm'
          AND score < 4.5
          AND needs_migration = FALSE
          AND need_eval = TRUE  -- Aggregator sets this when file is ready
        RETURNING 1
    )
    SELECT COUNT(*) INTO cold_from_warm FROM updated;

    -- Rule 3: WARM → DATA (score >= 9)
    WITH updated AS (
        UPDATE file_metadata
        SET target_pool = 'cephfs.tiercephfs.data',
            needs_migration = TRUE,
            need_eval = FALSE
        WHERE current_pool = 'cephfs.tiercephfs.warm'
          AND score >= 9
          AND needs_migration = FALSE
          AND need_eval = TRUE
        RETURNING 1
    )
    SELECT COUNT(*) INTO data_from_warm FROM updated;

    -- Rule 4: COLD → WARM (any access)
    WITH updated AS (
        UPDATE file_metadata
        SET target_pool = 'cephfs.tiercephfs.warm',
            needs_migration = TRUE,
            need_eval = FALSE
        WHERE current_pool = 'cephfs.tiercephfs.cold'
          AND score > 0
          AND needs_migration = FALSE
          AND need_eval = TRUE
        RETURNING 1
    )
    SELECT COUNT(*) INTO warm_from_cold FROM updated;

    -- Rule 5: WARM stays WARM (4.5 <= score < 9)
    WITH updated AS (
        UPDATE file_metadata
        SET need_eval = FALSE
        WHERE current_pool = 'cephfs.tiercephfs.warm'
          AND score >= 4.5
          AND score < 9
          AND needs_migration = FALSE
          AND need_eval = TRUE
        RETURNING 1
    )
    SELECT COUNT(*) INTO stayed_warm FROM updated;
    
    -- Acknowledge ALL remaining files with need_eval=TRUE
    UPDATE file_metadata
    SET need_eval = FALSE
    WHERE need_eval = TRUE
      AND needs_migration = FALSE;

    RETURN QUERY SELECT warm_from_data, cold_from_warm, data_from_warm, warm_from_cold, stayed_warm;
END;
$$;


--
-- Name: calculate_score(integer, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.calculate_score(access_frequency integer, last_access_time timestamp with time zone DEFAULT NULL::timestamp with time zone) RETURNS double precision
    LANGUAGE plpgsql IMMUTABLE
    AS $$
BEGIN
    RETURN 0.90 * access_frequency::FLOAT;
END;
$$;


--
-- Name: cleanup_duplicate_paths(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cleanup_duplicate_paths() RETURNS integer
    LANGUAGE plpgsql
    AS $$
DECLARE
    deleted_count INTEGER := 0;
BEGIN
    -- Delete older duplicate entries (keep the one with most recent last_access)
    WITH duplicates AS (
        SELECT path, inode, last_access,
               ROW_NUMBER() OVER (PARTITION BY path ORDER BY last_access DESC, inode DESC) as rn
        FROM file_metadata
    ),
    to_delete AS (
        SELECT inode
        FROM duplicates
        WHERE rn > 1  -- Keep only the first (most recent) entry per path
    )
    DELETE FROM file_metadata
    WHERE inode IN (SELECT inode FROM to_delete);
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    
    IF deleted_count > 0 THEN
        RAISE NOTICE '✓ Removed % duplicate path entries', deleted_count;
    END IF;
    
    RETURN deleted_count;
END;
$$;


--
-- Name: get_migration_batch(integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_migration_batch(batch_size integer DEFAULT 10) RETURNS TABLE(inode bigint, path text, current_pool text, target_pool text)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    SELECT fm.inode, fm.path, fm.current_pool, fm.target_pool
    FROM file_metadata fm
    WHERE fm.needs_migration = TRUE
    ORDER BY fm.last_access ASC
    LIMIT batch_size
    FOR UPDATE SKIP LOCKED;
END;
$$;


--
-- Name: get_migration_candidates(integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_migration_candidates(batch_size integer DEFAULT 10) RETURNS TABLE(inode bigint, path text, current_pool text, target_pool text, size_bytes bigint)
    LANGUAGE plpgsql
    AS $$
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
$$;


--
-- Name: mark_files_for_migration(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.mark_files_for_migration() RETURNS TABLE(data_to_warm_count bigint, warm_to_cold_count bigint, warm_to_data_count bigint, cold_to_warm_count bigint, stayed_in_warm_count bigint)
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_data_to_warm BIGINT := 0;
    v_warm_to_cold BIGINT := 0;
    v_warm_to_data BIGINT := 0;
    v_cold_to_warm BIGINT := 0;
    v_stayed_in_warm BIGINT := 0;
BEGIN
    -- PROMOTIONS (accessed files move to hotter pools)
    -- cold → data (accessed in last 3 minutes) - CHANGED: goes directly to data/hot pool
    WITH updated AS (
        UPDATE file_metadata
        SET needs_migration = TRUE,
            target_pool = 'cephfs.tiercephfs.data'
        WHERE current_pool = 'cephfs.tiercephfs.cold'
          AND needs_migration = FALSE
          AND last_access >= NOW() - INTERVAL '3 minutes'
        RETURNING 1
    )
    SELECT COUNT(*) INTO v_cold_to_warm FROM updated;
    
    -- warm → data (accessed in last 3 minutes)
    WITH updated AS (
        UPDATE file_metadata
        SET needs_migration = TRUE,
            target_pool = 'cephfs.tiercephfs.data'
        WHERE current_pool = 'cephfs.tiercephfs.warm'
          AND needs_migration = FALSE
          AND last_access >= NOW() - INTERVAL '3 minutes'
        RETURNING 1
    )
    SELECT COUNT(*) INTO v_warm_to_data FROM updated;
    
    -- DEMOTIONS (old files move to colder pools)
    -- data → warm (not accessed for 3 minutes)
    WITH updated AS (
        UPDATE file_metadata
        SET needs_migration = TRUE,
            target_pool = 'cephfs.tiercephfs.warm'
        WHERE current_pool = 'cephfs.tiercephfs.data'
          AND needs_migration = FALSE
          AND last_access < NOW() - INTERVAL '3 minutes'
        RETURNING 1
    )
    SELECT COUNT(*) INTO v_data_to_warm FROM updated;
    
    -- warm → cold (not accessed for 6 minutes)
    WITH updated AS (
        UPDATE file_metadata
        SET needs_migration = TRUE,
            target_pool = 'cephfs.tiercephfs.cold'
        WHERE current_pool = 'cephfs.tiercephfs.warm'
          AND needs_migration = FALSE
          AND last_access < NOW() - INTERVAL '6 minutes'
        RETURNING 1
    )
    SELECT COUNT(*) INTO v_warm_to_cold FROM updated;
    
    -- Count files staying in warm (no migration needed)
    SELECT COUNT(*) INTO v_stayed_in_warm
    FROM file_metadata
    WHERE current_pool = 'cephfs.tiercephfs.warm'
      AND needs_migration = FALSE;
    
    RETURN QUERY SELECT v_data_to_warm, v_warm_to_cold, v_warm_to_data, v_cold_to_warm, v_stayed_in_warm;
END;
$$;


--
-- Name: migration_complete(bigint, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.migration_complete(p_inode bigint, p_target_pool text) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE file_metadata
    SET current_pool = p_target_pool,
        target_pool = NULL,
        needs_migration = FALSE
    WHERE inode = p_inode;
END;
$$;


--
-- Name: record_migration_failure(bigint, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.record_migration_failure(p_inode bigint, p_error text) RETURNS void
    LANGUAGE plpgsql
    AS $$
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
$$;


--
-- Name: record_migration_success(bigint, text, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.record_migration_success(p_inode bigint, p_to_pool text, p_duration_ms integer) RETURNS void
    LANGUAGE plpgsql
    AS $$
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
$$;


--
-- Name: refresh_pool_statistics(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.refresh_pool_statistics() RETURNS void
    LANGUAGE plpgsql
    AS $$
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
$$;


--
-- Name: reset_file_after_migration(bigint, bigint, text, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reset_file_after_migration(old_inode_param bigint, new_inode_param bigint, new_pool_param text, preserved_last_access timestamp with time zone) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    old_creation_time TIMESTAMP WITH TIME ZONE;
    old_score DOUBLE PRECISION;
    old_path TEXT;
BEGIN
    -- Handle inode change (capture old values before deleting)
    IF old_inode_param != new_inode_param THEN
        SELECT creation_time, score, path
        INTO old_creation_time, old_score, old_path
        FROM file_metadata WHERE inode = old_inode_param;
        
        DELETE FROM file_metadata WHERE inode = old_inode_param;
        
        -- Insert new entry with preserved values
        INSERT INTO file_metadata (
            inode, path, current_pool, target_pool, last_access,
            needs_migration, access_freq, score, creation_time,
            last_evaluation_time, need_eval
        )
        VALUES (
            new_inode_param,
            old_path,
            new_pool_param,
            NULL,
            preserved_last_access,
            FALSE,
            0,
            COALESCE(old_score, 0.0),  -- PRESERVE SCORE!
            COALESCE(old_creation_time, NOW()),
            NOW(),
            FALSE  -- Reset handshake flag after migration
        )
        ON CONFLICT (inode) DO UPDATE SET
            current_pool = EXCLUDED.current_pool,
            target_pool = NULL,
            needs_migration = FALSE,
            access_freq = 0,
            last_access = EXCLUDED.last_access,
            score = EXCLUDED.score,  -- PRESERVE SCORE!
            creation_time = EXCLUDED.creation_time,
            last_evaluation_time = EXCLUDED.last_evaluation_time,
            need_eval = FALSE;
    ELSE
        -- Same inode case - just update pool and reset flags
        UPDATE file_metadata
        SET current_pool = new_pool_param,
            target_pool = NULL,
            needs_migration = FALSE,
            access_freq = 0,
            last_access = preserved_last_access,
            last_evaluation_time = NOW(),
            need_eval = FALSE  -- Reset handshake flag after migration
            -- SCORE stays constant!
        WHERE inode = new_inode_param;
    END IF;
END;
$$;


--
-- Name: update_modified_column(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_modified_column() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: file_access_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file_access_log (
    id bigint NOT NULL,
    uid integer NOT NULL,
    inode bigint NOT NULL,
    path text NOT NULL,
    access_time timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: file_access_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.file_access_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: file_access_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.file_access_log_id_seq OWNED BY public.file_access_log.id;


--
-- Name: file_metadata; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file_metadata (
    inode bigint NOT NULL,
    path text NOT NULL,
    current_pool text DEFAULT 'cephfs.tiercephfs.data'::text NOT NULL,
    target_pool text,
    last_access timestamp with time zone DEFAULT now() NOT NULL,
    needs_migration boolean DEFAULT false,
    access_freq integer DEFAULT 0,
    score double precision DEFAULT 0.0,
    creation_time timestamp with time zone DEFAULT now(),
    last_evaluation_time timestamp with time zone,
    need_eval boolean DEFAULT false
);


--
-- Name: file_access_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_access_log ALTER COLUMN id SET DEFAULT nextval('public.file_access_log_id_seq'::regclass);


--
-- Name: file_access_log file_access_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_access_log
    ADD CONSTRAINT file_access_log_pkey PRIMARY KEY (id);


--
-- Name: file_metadata file_metadata_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_metadata
    ADD CONSTRAINT file_metadata_pkey PRIMARY KEY (inode);


--
-- Name: idx_current_pool; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_current_pool ON public.file_metadata USING btree (current_pool);


--
-- Name: idx_file_metadata_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_file_metadata_score ON public.file_metadata USING btree (score DESC);


--
-- Name: idx_last_access; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_last_access ON public.file_metadata USING btree (last_access);


--
-- Name: idx_log_access_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_log_access_time ON public.file_access_log USING btree (access_time DESC);


--
-- Name: idx_log_inode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_log_inode ON public.file_access_log USING btree (inode);


--
-- Name: idx_need_eval; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_need_eval ON public.file_metadata USING btree (need_eval) WHERE (need_eval = true);


--
-- Name: idx_needs_migration; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_needs_migration ON public.file_metadata USING btree (needs_migration, last_access) WHERE (needs_migration = true);


--
-- PostgreSQL database dump complete
--

\unrestrict XoPREVriwCP6qg5Hgan64NJpezfoiVn93plPjHmZRAGI9DhV3fXPiRkPNZTmtas

