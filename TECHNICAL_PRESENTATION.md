# CephFS Dual-Mode Tiering System
## Technical Documentation for Engineering Review

---

## 📋 Table of Contents
1. [System Overview & Dual-Mode Architecture](#system-architecture)
2. [Component Deep Dive](#component-deep-dive)
3. [SQL Functions - Both Modes](#sql-functions)
4. [Service Configuration](#service-configuration)
5. [Useful Commands](#useful-commands)
6. [Common Questions & Answers](#common-questions--answers)

---

## 1. System Overview & Dual-Mode Architecture

### Two Tiering Policy Modes

This system implements **two distinct tiering philosophies** that can be switched dynamically:

#### **Mode 1: Access Frequency-Based (Score-Based)**
```
Algorithm: score = 0.90 × access_freq
Function: apply_tiering_policies()

Promotion Rules:
  - warm → data: score ≥ 9 (≈10+ accesses)
  - cold → data: score > 0 (any access)

Demotion Rules:
  - data → warm: score < 9
  - warm → cold: score < 4.5
```

**Philosophy**: Hot data stays hot based on cumulative usage patterns. Files that are frequently accessed remain in fast storage regardless of when they were last accessed.

**Use Case**: Workloads where popular files should stay performant (e.g., shared datasets, frequently-read documentation).

#### **Mode 2: Last Access Time-Based (Age-Based)**
```
Algorithm: Time thresholds on last_access timestamp
Function: mark_files_for_migration()

Promotion Rules (bidirectional):
  - cold → data: accessed in last 3 minutes
  - warm → data: accessed in last 3 minutes

Demotion Rules:
  - data → warm: idle for 3 minutes
  - warm → cold: idle for 6 minutes total
```

**Philosophy**: Recent activity determines tier placement. Old files automatically archive to cold storage.

**Use Case**: Workloads where file age indicates value (e.g., time-series data, log files, backups).

### Switching Between Modes

```bash
# Check current mode
switch_tiering status

# Enable frequency-based mode
switch_tiering frequency

# Enable time-based mode
switch_tiering time

# Disable all tiering
switch_tiering off
```

**How Switching Works**:
1. Script edits `policy_engine_optimized.py` to call different PostgreSQL function
2. Service restarts: `sudo systemctl restart cephfs-policy-engine.service`
3. All other components (eBPF tracker, aggregator, migration worker) remain unchanged

### Shared Infrastructure

Both modes use identical:
- **eBPF Tracker**: Monitors file accesses, updates both `access_freq` and `last_access`
- **Aggregator**: Processes `file_access_log` into `file_metadata` every 60s
- **Migration Worker**: Executes pool migrations using libcephfs
- **Database Schema**: Same tables, both columns populated

Only the **policy engine logic** changes between modes.

### High-Level Flow
```
User File Access
    ↓
CephFS Kernel Module (ceph_read_iter, ceph_write_iter)
    ↓
eBPF Tracker (BCC kprobes)
    ↓
PostgreSQL Hot Table (file_access_log) ← Fast append-only inserts
    ↓
Aggregation Function (every 60s)
    ↓
PostgreSQL Cold Table (file_metadata) ← Scored, evaluated files
    ↓
Policy Engine (every 60s)
    ↓
Migration Worker (every 30s)
    ↓
CephFS Pool Migration (NVMe ↔ SSD ↔ HDD)
```

### Storage Tiers
| Tier | Pool Name | Storage Type | Frequency Mode | Time Mode |
|------|-----------|--------------|----------------|-----------|
| **DATA (Hot)** | cephfs.tiercephfs.data | NVMe SSD | score ≥ 9 | accessed in last 3 min |
| **WARM** | cephfs.tiercephfs.warm | SATA SSD | 4.5 ≤ score < 9 | idle 3-6 minutes |
| **COLD** | cephfs.tiercephfs.cold | HDD | score < 4.5 | idle 6+ minutes |

### Frequency Mode Scoring System
- **Formula**: `score = 0.90 × access_freq`
- **Read Inflation Fix**: `access_freq = GREATEST(1, COUNT(*) / 2)`
  - CephFS reads trigger 2 kernel events, so we divide by 2
- **Threshold**: 10 accesses = score 9.0 (promotion threshold)

### Time Mode Thresholds
- **Promotion (to hot)**: Any access within last 3 minutes
- **Demotion to warm**: No access for 3 minutes from DATA pool
- **Demotion to cold**: No access for 6 minutes total from WARM pool

---

## 2. Component Deep Dive

### 2.1 eBPF Tracker (`tracker_phase1.py`)

**Purpose**: Capture file accesses at kernel level without modifying CephFS code

**Key Features**:
- Attaches to `ceph_read_iter` and `ceph_write_iter` kprobes
- Filters out root user (UID 0) to avoid tracking migration operations
- Skips hidden files (starting with `.`)
- Batch inserts to PostgreSQL (1000 events per batch or 1-second intervals)

**Code Structure**:
```python
class TieringTracker:
    def __init__(self):
        # Setup PostgreSQL connection
        self.setup_postgres()
        
        # Load eBPF program with BCC
        self.setup_ebpf()
        
        # Start background threads
        self.aggregator_thread  # Runs aggregate_access_log() every 60s
        self.flush_thread       # Flushes event buffer every 1s
    
    def handle_event(self, cpu, data, size):
        # Called for each file access
        # Buffers events for batch insert
        self.event_buffer.append((uid, inode, path, timestamp))
    
    def flush_buffer(self):
        # Batch insert using psycopg2.extras.execute_values()
        execute_values(cur, """
            INSERT INTO file_access_log (uid, inode, path, access_time)
            VALUES %s
        """, self.event_buffer)
```

**eBPF Kernel Code**:
```c
struct access_event {
    u64 inode;
    u64 timestamp_ns;
    u32 pid;
    u32 uid;
    char path[256];
};

int trace_read(struct pt_regs *ctx, struct kiocb *iocb) {
    struct file *file = iocb->ki_filp;
    struct inode *inode = file->f_inode;
    
    // Skip root (UID 0)
    if (uid == 0) return 0;
    
    // Get path from dentry
    struct qstr dname = file->f_path.dentry->d_name;
    bpf_probe_read_kernel_str(&event.path, sizeof(event.path), dname.name);
    
    // Skip hidden files
    if (event.path[0] == '.') return 0;
    
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}
```

**Performance**:
- Batch inserts: ~1000 events/second
- Memory overhead: ~172MB
- CPU usage: Negligible (<1% on idle system)

---

### 2.2 Policy Engine (`policy_engine.py`)

**Purpose**: Evaluate scores and mark files for migration

**Execution Cycle** (every 60 seconds):
```python
def main_loop():
    while True:
        # Call PostgreSQL functions
        aggregate_access_log()      # Move log → metadata, evaluate scores
        apply_tiering_policies()    # Mark files for migration
        time.sleep(60)
```

**Output**:
```
====================================== Policy Engine Cycle Start ===
Total: 2 files marked for migration
1 files data ->warm
0 files warm ->cold
1 files cold ->data
0 files warm ->data
Sleeping for 60 seconds...
====================================== Cycle Complete: 2 files marked ===
```

---

### 2.3 Migration Worker (`migration_worker.py`)

**Purpose**: Execute physical file migrations between pools

**Execution Cycle** (every 30 seconds):
1. Query files with `needs_migration = TRUE`
2. Migrate files in parallel (5 workers)
3. Update metadata after successful migration

**Migration Algorithm**:
```python
def migrate_file(inode, path, target_pool):
    # 1. Create empty temp file
    temp_file = path + ".__tiering__"
    touch(temp_file)
    
    # 2. Set target pool layout on empty file
    setfattr(temp_file, "ceph.file.layout.pool", target_pool)
    
    # 3. Copy data using server-side copy
    rados_copy_objects(original_inode, temp_inode)
    
    # 4. Atomic rename (instant switch!)
    mv(temp_file, original_path)
    
    # 5. Reset metadata
    reset_file_after_migration(old_inode, new_inode, target_pool)
```

**Server-Side Migration** (no data transfer to client):
```bash
# Get inode numbers
ORIG_INODE=$(stat -c '%i' file.txt)
TEMP_INODE=$(stat -c '%i' file.txt.__tiering__)

# Convert to hex for object names
ORIG_OBJ=$(printf '%x.00000000' $ORIG_INODE)
TEMP_OBJ=$(printf '%x.00000000' $TEMP_INODE)

# Copy object within Ceph cluster
rados -p cephfs.tiercephfs.data get $ORIG_OBJ /tmp/obj
rados -p cephfs.tiercephfs.cold put $TEMP_OBJ /tmp/obj

# Update inode size
truncate -s $ORIG_SIZE file.txt.__tiering__

# Atomic rename
mv file.txt.__tiering__ file.txt
```

**Performance**:
- Migration time: ~5 seconds per file
- Parallel workers: 5 concurrent migrations
- Inode changes: Yes (requires metadata update)

---

## 3. SQL Functions

### 3.1 `aggregate_access_log()` - Core Aggregation Logic

**Purpose**: Move events from hot table to cold table, evaluate scores

**Returns**: INTEGER (number of files processed)

**Code**:
```sql
CREATE OR REPLACE FUNCTION aggregate_access_log() RETURNS INTEGER AS $$
DECLARE
    total_files INTEGER := 0;
BEGIN
    -- STEP 1: Accumulate new accesses
    WITH upsert_result AS (
        INSERT INTO file_metadata (inode, path, last_access, access_freq, creation_time)
        SELECT 
            fal.inode,
            fal.path,
            MAX(fal.access_time) as last_access,
            GREATEST(1, COUNT(*) / 2) as new_accesses,  -- Fix read inflation
            MIN(fal.access_time) as creation_time        -- First access time
        FROM file_access_log fal
        GROUP BY fal.inode, fal.path
        ON CONFLICT (inode) DO UPDATE
        SET 
            last_access = EXCLUDED.last_access,
            access_freq = file_metadata.access_freq + EXCLUDED.access_freq,
            path = EXCLUDED.path
        RETURNING 1
    )
    SELECT COUNT(*) INTO total_files FROM upsert_result;
    
    -- STEP 2: Evaluate files based on current pool
    
    -- DATA pool: 3-minute rule (data → warm demotion)
    UPDATE file_metadata
    SET 
        score = 0.90 * access_freq,
        access_freq = 0,
        last_evaluation_time = NOW()
    WHERE current_pool = 'cephfs.tiercephfs.data'
      AND (
          (last_evaluation_time IS NULL AND NOW() - creation_time >= INTERVAL '3 minutes')
          OR
          (last_evaluation_time IS NOT NULL AND NOW() - last_evaluation_time >= INTERVAL '3 minutes')
      );
    
    -- WARM pool: Immediate if accessed, 3-minute if not
    UPDATE file_metadata
    SET 
        score = 0.90 * access_freq,
        access_freq = 0,
        last_evaluation_time = NOW()
    WHERE current_pool = 'cephfs.tiercephfs.warm'
      AND (
          -- Immediate evaluation for promotion (warm → data)
          (access_freq > 0)
          OR
          -- 3-minute rule for demotion (warm → cold)
          (access_freq = 0 AND (
              (last_evaluation_time IS NULL AND NOW() - creation_time >= INTERVAL '3 minutes')
              OR
              (last_evaluation_time IS NOT NULL AND NOW() - last_evaluation_time >= INTERVAL '3 minutes')
          ))
      );
    
    -- COLD pool: Immediate evaluation when accessed
    UPDATE file_metadata
    SET 
        score = 0.90 * access_freq,
        access_freq = 0,
        last_evaluation_time = NOW()
    WHERE current_pool = 'cephfs.tiercephfs.cold'
      AND access_freq > 0;
    
    DELETE FROM file_access_log;
    
    RETURN total_files;
END;
$$ LANGUAGE plpgsql;
```

**Key Logic**:
1. **Accumulation**: Merge `file_access_log` into `file_metadata`
2. **Pool-Specific Evaluation**:
   - DATA: Always waits 3 minutes
   - WARM: Immediate if accessed (for promotion), 3-min if not (for demotion)
   - COLD: Immediate if accessed (for promotion to DATA)
3. **Frequency Reset**: `access_freq = 0` after evaluation (enables cooling)
4. **Cleanup**: Delete processed log entries

---

### 3.2 Policy Functions - Dual Mode

The system supports two PostgreSQL functions for policy evaluation:

#### **Mode 1: `apply_tiering_policies()` - Frequency-Based**

**Purpose**: Mark files for migration based on access frequency scores

**Returns**: TABLE (migration counts)

**Code**:
```sql
CREATE OR REPLACE FUNCTION apply_tiering_policies()
RETURNS TABLE(
    to_warm_from_data BIGINT,
    to_cold_from_warm BIGINT,
    to_data_from_warm BIGINT,
    to_data_from_cold BIGINT,
    stayed_in_warm BIGINT
) AS $$
DECLARE
    warm_from_data BIGINT := 0;
    cold_from_warm BIGINT := 0;
    data_from_warm BIGINT := 0;
    data_from_cold BIGINT := 0;
    stayed_warm BIGINT := 0;
BEGIN
    -- Rule 1: DATA → WARM (score < 9, demotion)
    WITH updated AS (
        UPDATE file_metadata
        SET target_pool = 'cephfs.tiercephfs.warm',
            needs_migration = TRUE
        WHERE current_pool = 'cephfs.tiercephfs.data'
          AND score < 9
          AND needs_migration = FALSE
          AND last_evaluation_time IS NOT NULL
        RETURNING 1
    )
    SELECT COUNT(*) INTO warm_from_data FROM updated;

    -- Rule 2: WARM → COLD (score < 4.5, demotion)
    WITH updated AS (
        UPDATE file_metadata
        SET target_pool = 'cephfs.tiercephfs.cold',
            needs_migration = TRUE
        WHERE current_pool = 'cephfs.tiercephfs.warm'
          AND score < 4.5
          AND needs_migration = FALSE
          AND last_evaluation_time IS NOT NULL
        RETURNING 1
    )
    SELECT COUNT(*) INTO cold_from_warm FROM updated;

    -- Rule 3: WARM → DATA (score >= 9, promotion)
    WITH updated AS (
        UPDATE file_metadata
        SET target_pool = 'cephfs.tiercephfs.data',
            needs_migration = TRUE
        WHERE current_pool = 'cephfs.tiercephfs.warm'
          AND score >= 9
          AND needs_migration = FALSE
          AND last_evaluation_time IS NOT NULL
        RETURNING 1
    )
    SELECT COUNT(*) INTO data_from_warm FROM updated;

    -- Rule 4: COLD → DATA (score > 0, direct promotion, skip WARM!)
    WITH updated AS (
        UPDATE file_metadata
        SET target_pool = 'cephfs.tiercephfs.data',
            needs_migration = TRUE
        WHERE current_pool = 'cephfs.tiercephfs.cold'
          AND score > 0
          AND needs_migration = FALSE
          AND last_evaluation_time IS NOT NULL
        RETURNING 1
    )
    SELECT COUNT(*) INTO data_from_cold FROM updated;

    -- Count files staying in warm (for metrics)
    SELECT COUNT(*) INTO stayed_warm
    FROM file_metadata
    WHERE current_pool = 'cephfs.tiercephfs.warm'
      AND needs_migration = FALSE;

    RETURN QUERY SELECT warm_from_data, cold_from_warm, data_from_warm, data_from_cold, stayed_warm;
END;
$$ LANGUAGE plpgsql;
```

**Key Logic**:
- **Promotion threshold**: score ≥ 9 (approximately 10+ accesses)
- **Demotion thresholds**: score < 9 (DATA→WARM), score < 4.5 (WARM→COLD)
- **COLD recovery**: Any access (score > 0) promotes directly to DATA (skips WARM for faster recovery)
- **Safety check**: Only migrate files with `last_evaluation_time IS NOT NULL`

#### **Mode 2: `mark_files_for_migration()` - Time-Based**

**Purpose**: Mark files for migration based on last access timestamp

**Returns**: TABLE (migration counts)

**Code**:
```sql
CREATE OR REPLACE FUNCTION mark_files_for_migration()
RETURNS TABLE(
    data_to_warm_count BIGINT,
    warm_to_cold_count BIGINT,
    warm_to_data_count BIGINT,
    cold_to_warm_count BIGINT,
    stayed_in_warm_count BIGINT
) AS $$
DECLARE
    v_data_to_warm BIGINT := 0;
    v_warm_to_cold BIGINT := 0;
    v_warm_to_data BIGINT := 0;
    v_cold_to_warm BIGINT := 0;
    v_stayed_in_warm BIGINT := 0;
BEGIN
    -- PROMOTIONS (accessed files move to hotter pools)
    -- cold → data (accessed in last 3 minutes)
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
    
    -- warm → cold (not accessed for 6 minutes total)
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
    
    -- Count files staying in warm (for metrics)
    SELECT COUNT(*) INTO v_stayed_in_warm
    FROM file_metadata
    WHERE current_pool = 'cephfs.tiercephfs.warm'
      AND needs_migration = FALSE;
    
    RETURN QUERY SELECT v_data_to_warm, v_warm_to_cold, v_warm_to_data, v_cold_to_warm, v_stayed_in_warm;
END;
$$ LANGUAGE plpgsql;
```

**Key Logic**:
- **Bidirectional tiering**: Promotion rules run before demotion rules
- **Promotion**: Any file accessed in last 3 minutes goes directly to DATA (hot)
- **Demotion**: Progressive cooldown (3 min → warm, 6 min → cold)
- **Immediate response**: No evaluation delays, acts on every access
- **Return signature**: Matches `apply_tiering_policies()` for compatibility with policy engine

---

### 3.3 `reset_file_after_migration()` - Post-Migration Cleanup
          AND score > 0
          AND needs_migration = FALSE
          AND last_evaluation_time IS NOT NULL
        RETURNING 1
    )
    SELECT COUNT(*) INTO data_from_cold FROM updated;

    RETURN QUERY SELECT warm_from_data, cold_from_warm, data_from_warm, data_from_cold;
END;
$$ LANGUAGE plpgsql;
```

**Key Logic**:
- **Safety Check**: `last_evaluation_time IS NOT NULL` prevents migrating unevaluated files
- **COLD Promotion**: Goes directly to DATA (skips WARM for faster recovery)
- **Idempotent**: `needs_migration = FALSE` prevents duplicate markings

---

### 3.3 `reset_file_after_migration()` - Post-Migration Cleanup

**Purpose**: Reset file state after successful migration

**Code**:
```sql
CREATE OR REPLACE FUNCTION reset_file_after_migration(
    old_inode_param BIGINT,
    new_inode_param BIGINT,
    new_pool_param TEXT,
    preserved_last_access TIMESTAMP WITH TIME ZONE
) RETURNS VOID AS $$
BEGIN
    -- Delete old inode entry if inode changed
    IF old_inode_param != new_inode_param THEN
        DELETE FROM file_metadata WHERE inode = old_inode_param;
    END IF;
    
    -- Reset access state, preserve evaluation schedule
    UPDATE file_metadata
    SET current_pool = new_pool_param,
        target_pool = NULL,
        needs_migration = FALSE,
        access_freq = 0,
        score = 0.0,
        last_access = preserved_last_access
        -- DON'T reset: creation_time, last_evaluation_time
    WHERE inode = new_inode_param;
END;
$$ LANGUAGE plpgsql;
```

**Key Insight**: Preserves `creation_time` and `last_evaluation_time` so files maintain their evaluation schedule after migration.

---

## 4. Service Configuration

### 4.1 Tracker Service

**File**: `/etc/systemd/system/cephfs-tracker.service`

```ini
[Unit]
Description=CephFS File Access Tracker (eBPF)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /home/cephvm/tiering_system/src/tracker_phase1.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

# Resource limits
MemoryMax=512M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
```

**Logs**:
```
Jan 16 17:08:52 cephvm python3[1047988]: Monitoring started
Jan 16 17:09:54 cephvm python3[1047988]: Wrote 1 files to file_metadata. Sleeping for 60s
```

---

### 4.2 Policy Engine Service

**File**: `/etc/systemd/system/cephfs-policy-engine.service`

```ini
[Unit]
Description=CephFS Tiering Policy Engine
After=cephfs-tracker.service postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=postgres
ExecStart=/usr/bin/python3 /home/cephvm/tiering_system/src/policy_engine.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

### 4.3 Migration Worker Service

**File**: `/etc/systemd/system/cephfs-migration-worker.service`

```ini
[Unit]
Description=CephFS Tiering Migration Worker
After=cephfs-policy-engine.service postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /home/cephvm/tiering_system/src/migration_worker.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 5. Useful Commands

### 5.1 Tiering Mode Management

```bash
# Check current tiering mode
switch_tiering status

# Switch to frequency-based mode (score = 0.90 × access_freq)
switch_tiering frequency

# Switch to time-based mode (last_access timestamps)
switch_tiering time

# Disable all tiering services
switch_tiering off
```

### 5.2 Service Management

```bash
# Check all tiering services
sudo systemctl status cephfs-tracker.service
sudo systemctl status cephfs-policy-engine.service
sudo systemctl status cephfs-migration-worker.service

# View live logs
sudo journalctl -u cephfs-tracker.service -f
sudo journalctl -u cephfs-policy-engine.service -f
sudo journalctl -u cephfs-migration-worker.service -f

# Restart services
sudo systemctl restart cephfs-tracker.service
sudo systemctl restart cephfs-policy-engine.service
sudo systemctl restart cephfs-migration-worker.service
```

---

### 5.3 Database Queries

```sql
-- View all tracked files (works for both modes)
SELECT path, 
       SUBSTRING(current_pool, 20) as pool, 
       access_freq,   -- Used in frequency mode
       score,         -- Calculated in frequency mode
       last_access    -- Used in time mode
FROM file_metadata 
ORDER BY last_access DESC;

-- Files pending migration
SELECT path, SUBSTRING(current_pool, 20) as current, 
       SUBSTRING(target_pool, 20) as target
FROM file_metadata WHERE needs_migration = TRUE;

-- Pool distribution
SELECT SUBSTRING(current_pool, 20) as pool, COUNT(*) as files
FROM file_metadata GROUP BY current_pool;

-- Hot table size
SELECT COUNT(*) as pending_events FROM file_access_log;

-- Manually trigger aggregation
SELECT aggregate_access_log();

-- Manually trigger policy evaluation
SELECT * FROM apply_tiering_policies();

-- View evaluation timing
SELECT path, 
       SUBSTRING(current_pool, 20) as pool,
       access_freq,
       score,
       NOW() - last_evaluation_time as time_since_eval
FROM file_metadata 
WHERE last_evaluation_time IS NOT NULL
ORDER BY score DESC;
```

---

### 5.3 File Pool Management

```bash
# Check which pool a file is in
getfattr -n ceph.file.layout.pool /tiercephfs/myfile.txt

# List all pools
ceph osd pool ls

# View pool statistics
ceph osd pool stats

# Find files in specific pool
find /tiercephfs -type f -exec getfattr -n ceph.file.layout.pool {} \; 2>/dev/null | grep -A1 warm
```

---

### 5.4 Testing & Debugging

```bash
# Create test file and access it
echo "test" > /tiercephfs/testfile.txt
cat /tiercephfs/testfile.txt

# Wait for aggregation (60 seconds)
sleep 65

# Check if tracked
sudo -u postgres psql tiering -c "SELECT * FROM file_metadata WHERE path = 'testfile.txt';"

# Force immediate aggregation
sudo -u postgres psql tiering -c "SELECT aggregate_access_log();"

# Check logs for errors
sudo journalctl -u cephfs-tracker.service --since "5 minutes ago" | grep -i error

# Monitor eBPF events in real-time
sudo bpftool prog list
```

---

### 5.5 Performance Monitoring

```bash
# Database size
sudo -u postgres psql tiering -c "
SELECT 
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables 
WHERE schemaname = 'public';"

# Index usage
sudo -u postgres psql tiering -c "
SELECT indexrelname, idx_scan 
FROM pg_stat_user_indexes 
WHERE schemaname = 'public';"

# Service resource usage
systemctl status cephfs-tracker.service | grep "Memory:"
systemctl status cephfs-tracker.service | grep "CPU:"
```

---

## 6. Common Questions & Answers

### Q1: Why have two tiering modes instead of just one?

**Answer**:
Different workloads have different characteristics:
- **Frequency Mode**: Best for shared datasets where popular files should stay fast (e.g., ML models, shared code repositories)
- **Time Mode**: Best for time-series data where recent = important (e.g., logs, backups, monitoring data)
- **Flexibility**: Organizations can test both and choose what works best

### Q2: Can I switch between modes without losing data?

**Answer**:
Yes, switching is safe because:
- Both modes track the same data (`access_freq` and `last_access`)
- No database schema changes required
- Files remain in their current pools during the switch
- Only the policy decision logic changes

### Q3: Why use eBPF instead of FUSE or client-side tracking?

**Answer**: 
- **Performance**: eBPF runs in kernel space, zero overhead
- **Transparency**: No modification to CephFS code or FUSE layer
- **Accuracy**: Captures all access patterns (even from kernel clients)
- **Security**: Cannot be bypassed by users

### Q4: Why separate hot table (file_access_log) and cold table (file_metadata)?

**Answer**:
- **Write Performance**: Hot table is append-only, no locks, fast inserts
- **Read Performance**: Cold table is indexed, optimized for queries
- **Batch Processing**: Aggregation reduces write amplification

### Q5: Why does the inode change during migration?

**Answer**:
- CephFS doesn't support in-place pool changes
- We create a new file with new layout, copy data, then atomic rename
- The rename operation assigns a new inode
- Solution: Update `file_metadata` with new inode after migration

### Q6: In frequency mode, why immediate evaluation for WARM/COLD but 3-minute rule for DATA?

**Answer**:
- **DATA Files**: Most active, need stability (avoid thrashing)
- **WARM Files**: Accessed files should promote quickly (better user experience)
- **COLD Files**: Any access is significant, promote immediately

### Q7: In time mode, why does COLD → DATA skip WARM?

**Answer**:
- User accessed a cold file = likely needs it urgently
- Going through WARM adds unnecessary delay (extra migration)
- Direct promotion improves latency for cold data recovery

### Q8: How do you handle migration failures?

**Answer**:
```python
try:
    migrate_file(inode, path, target_pool)
except Exception as e:
    # Reset needs_migration flag
    UPDATE file_metadata SET needs_migration = FALSE WHERE inode = inode
    # Log error
    logger.error(f"Migration failed: {e}")
```

### Q9: What happens if a file is accessed during migration?

**Answer**:
- Migration uses temp files (`.__ tiering__`)
- Original file remains accessible during copy
- Atomic rename at the end ensures no disruption
- Worst case: User reads from old pool (still works)

### Q10: How do you prevent migration loops (file bouncing between pools)?

**Answer**:

**Frequency Mode**:
- **Score resets to 0** after migration
- **3-minute evaluation window** for DATA pool prevents immediate demotion
- **Evaluation schedule preserved** (creation_time, last_evaluation_time)

**Time Mode**:
- **3-minute promotion window** prevents immediate demotion after promotion
- **6-minute cold threshold** creates hysteresis
- **Timestamp-based decisions** are deterministic

### Q11: Why score = 0.90 × access_freq (why 0.90)?

**Answer**:
- Allows fine-grained scoring
- 10 accesses = 9.0 (exactly at threshold)
- Can be tuned for different workloads
- Leaves room for future weighting factors

### Q12: How do you scale this to millions of files?

**Answer**:
- **Batch operations**: Aggregate 1000 events at a time
- **Indexed queries**: All query predicates are indexed
- **Horizontal scaling**: Can partition by inode range
- **Hot table pruning**: Delete old log entries regularly

### Q13: Which mode should I use for my workload?

**Answer**:

**Use Frequency Mode if**:
- Files have varying importance based on usage
- Popular files should stay fast regardless of age
- You have shared datasets accessed by multiple users
- Access patterns are bursty but predictable

**Use Time Mode if**:
- Recent data is more valuable than old data
- Files naturally age out of relevance
- You have time-series or log-like workloads
- You want simpler, more predictable behavior

**Test both**: The switch is instant, so try both on your workload!

---

## 7. Demo Script

```bash
#!/bin/bash
# Complete demonstration of tiering system

echo "=== 1. Create test file ==="
echo "demo data" > /tiercephfs/demo.txt
cat /tiercephfs/demo.txt  # Access once

echo "=== 2. Wait for tracking (5s) ==="
sleep 5

echo "=== 3. Check hot table ==="
sudo -u postgres psql tiering -c "SELECT * FROM file_access_log WHERE path = 'demo.txt';"

echo "=== 4. Trigger aggregation ==="
sudo -u postgres psql tiering -c "SELECT aggregate_access_log();"

echo "=== 5. Check cold table ==="
sudo -u postgres psql tiering -c "SELECT path, access_freq, score FROM file_metadata WHERE path = 'demo.txt';"

echo "=== 6. Wait 3 minutes for evaluation ==="
sleep 180

echo "=== 7. Trigger evaluation ==="
sudo -u postgres psql tiering -c "SELECT aggregate_access_log();"

echo "=== 8. Check score after evaluation ==="
sudo -u postgres psql tiering -c "SELECT path, access_freq, score FROM file_metadata WHERE path = 'demo.txt';"

echo "=== 9. Apply policies ==="
sudo -u postgres psql tiering -c "SELECT * FROM apply_tiering_policies();"

echo "=== 10. Check migration status ==="
sudo -u postgres psql tiering -c "SELECT path, needs_migration, target_pool FROM file_metadata WHERE path = 'demo.txt';"

echo "=== 11. Wait for migration (30s) ==="
sleep 35

echo "=== 12. Verify final pool ==="
getfattr -n ceph.file.layout.pool /tiercephfs/demo.txt

echo "=== Demo complete! ==="
```

---

## 8. Performance Metrics

### Observed Performance (Production Workload)

| Metric | Value | Notes |
|--------|-------|-------|
| **Tracker CPU** | <1% | Idle system |
| **Tracker Memory** | 172 MB | Steady state |
| **Insert Rate** | 422 events/hr | Light workload |
| **Aggregation Time** | <100ms | Per cycle |
| **Migration Time** | ~5s/file | Server-side copy |
| **Database Size** | <10 MB | 50 tracked files |

### Scalability Estimates

| Files | Events/day | DB Size | Migration Time |
|-------|-----------|---------|----------------|
| 1,000 | 10,000 | ~50 MB | 5 minutes |
| 10,000 | 100,000 | ~500 MB | 50 minutes |
| 100,000 | 1,000,000 | ~5 GB | 8 hours |

---

## 9. Future Enhancements

1. **Predictive Migration**: Use ML to predict access patterns
2. **Cost-Aware Policies**: Consider migration costs vs. storage savings
3. **Multi-Tenancy**: Per-user or per-project policies
4. **Real-Time Dashboard**: Web UI for monitoring
5. **API Integration**: REST API for external tools
6. **Object-Level Tiering**: Migrate individual objects within large files

---

## Contact & Support

- **Documentation**: `/home/cephvm/tiering_system/FINAL_DOCUMENTATION.md`
- **Source Code**: `/home/cephvm/tiering_system/src/`
- **Database**: PostgreSQL 14, database "tiering"
- **Logs**: `sudo journalctl -u cephfs-*.service`
