# CephFS Automated Tiering System

Production-grade storage tiering system for CephFS with eBPF-based access tracking, PostgreSQL hot/cold tables, and automated migration between SSD and HDD pools.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│              eBPF Kernel Hooks                           │
│   (ceph_read_iter + ceph_write_iter)                    │
│   • Captures: inode, uid, filename                      │
│   • Deduplicates: 1-second window                       │
└────────────────────┬─────────────────────────────────────┘
                     ↓ BCC perf buffer
┌──────────────────────────────────────────────────────────┐
│      Access Tracker (Python/BCC)                         │
│  • Resolves full path: find /tiercephfs -inum <inode>   │
│  • HOT table (file_access_log): Append-only inserts     │
│  • Aggregates every 60s → COLD table (file_metadata)    │
│  • ID-based watermark: No data loss during aggregation  │
└────────────────────┬─────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│          Policy Engine (Python)                          │
│  • Test mode: 3 minutes = 30 days                       │
│  • Runs every 60 seconds                                │
│  • Policies:                                            │
│    - data → warm: After 3 min idle (30 days)           │
│    - warm → cold: After 6 min idle (60 days total)     │
│    - cold/warm → data: If accessed                      │
│  • Sets: needs_migration = TRUE, target_pool           │
└────────────────────┬─────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│          Migration Workers (Python, 5 threads)           │
│  • SELECT ... FOR UPDATE SKIP LOCKED                    │
│  • Calls libcephfs_migrate (C binary)                   │
│  • Shadow file technique: Creates new inode             │
│  • Tracks inode changes: old_inode → new_inode         │
│  • Preserves last_access timestamps                     │
└──────────────────────────────────────────────────────────┘
```

## Key Features

✅ **Full Path Support**: Works with files in subdirectories  
✅ **Zero Data Loss**: ID-based watermark prevents loss during aggregation  
✅ **Inode Tracking**: Handles inode changes from shadow file migration  
✅ **Timestamp Preservation**: last_access time maintained across migrations  
✅ **Minimal Logging**: Clean, concise output (3 lines per cycle)  
✅ **Test Mode**: 3 minutes = 30 days for rapid testing  

## Components

### 1. eBPF Tracker (`monitoring_ebpf_tracker.py`)
**Technology**: Python 3 with BCC (BPF Compiler Collection)

**What it does**:
- Attaches to CephFS kernel functions (`ceph_read_iter`, `ceph_write_iter`)
- Captures file access events (inode, uid, filename)
- Deduplicates events within 1-second window (reduces load)
- Skips root user (UID 0) to avoid tracking migration operations
- Skips hidden files (starting with `.`)

**Path Resolution**:
- eBPF captures only filename from `dentry->d_name.name`
- Python handler resolves full path using: `find /tiercephfs -inum <inode>`
- Removes `/tiercephfs/` prefix to get relative path (e.g., `key/tea.txt`)

**Database Flow**:
1. **HOT path**: Writes to `file_access_log` (append-only, fast)
2. **Aggregation**: Every 60 seconds, runs `aggregate_access_log()` function
3. **COLD path**: Updates `file_metadata` with latest access times
4. **Cleanup**: Deletes only processed entries (ID ≤ max_id captured before aggregation)

**Logging**:
```
Monitoring started
[17:52:23] Wrote 2 files to file_metadata. Sleeping for 60s
[17:53:23] Wrote 0 files to file_metadata. Sleeping for 60s
```

### 2. PostgreSQL Database
**Schema**:

**Hot Table** (`file_access_log`):
```sql
CREATE TABLE file_access_log (
    id BIGSERIAL PRIMARY KEY,
    uid INT,
    inode BIGINT,
    path TEXT,
    access_time TIMESTAMP
);
```

**Cold Table** (`file_metadata`):
```sql
CREATE TABLE file_metadata (
    inode BIGINT PRIMARY KEY,
    path TEXT,
    current_pool TEXT,
    target_pool TEXT,
    last_access TIMESTAMP,
    needs_migration BOOLEAN DEFAULT FALSE
);
CREATE INDEX idx_needs_migration ON file_metadata(needs_migration, target_pool);
```

**Stored Procedure** (`aggregate_access_log()`):
- Captures max ID before starting (watermark)
- Aggregates entries with `id ≤ max_id` (prevents data loss)
- Upserts latest access time per inode into `file_metadata`
- Deletes only processed entries
- Returns count of processed entries

### 3. Policy Engine (`policy_engine.py`)
**Test Mode**: 3 minutes = 30 days

**Policies**:
| Pool | Age Threshold | Action |
|------|---------------|--------|
| data | 3 min (30d) | Promote to warm |
| warm | 6 min (60d total) | Promote to cold |
| cold | If accessed | Demote to data |
| warm | If accessed | Demote to data |

**Logic**:
```python
# Promotion (idle files move to slower storage)
data → warm: last_access < NOW() - INTERVAL '3 minutes'
warm → cold: last_access < NOW() - INTERVAL '6 minutes'

# Demotion (accessed files return to fast storage)
cold → data: last_access > NOW() - INTERVAL '3 minutes'
warm → data: last_access > NOW() - INTERVAL '3 minutes'
```

**Output**: Marks files with `needs_migration = TRUE` and sets `target_pool`

### 4. Migration Worker (`migration_worker.py`)
**Parallelism**: 5 worker threads

**Migration Process**:
1. `SELECT ... FOR UPDATE SKIP LOCKED` (no conflicts between workers)
2. Get old inode: `old_inode = os.stat(file).st_ino`
3. Call C binary: `libcephfs_migrate /path/to/file target_pool`
4. Get new inode: `new_inode = os.stat(file).st_ino`
5. If inodes differ (shadow file created new inode):
   - `DELETE FROM file_metadata WHERE inode = old_inode`
   - `INSERT INTO file_metadata (inode, ...) VALUES (new_inode, ...)`
   - Preserve `last_access` from before migration
6. Set `needs_migration = FALSE`

**Why Inode Changes**:
- `libcephfs_migrate` uses shadow file technique
- Creates `filename.__tiering__` in target pool
- Atomic rename: `rename(shadow, original)`
- Rename across pools creates **new inode**

### 5. libcephfs_migrate (C Binary)
**Purpose**: Physical file migration using CephFS library

**Process**:
1. Open source file: `ceph_open(cmount, path, O_RDONLY, 0)`
2. Get file metadata (size, permissions, ownership)
3. Create shadow file: `path + ".__tiering__"` in target pool
4. Copy data in 4MB chunks
5. Set layout: `ceph_set_pool_layout(shadow_fd, pool_id)`
6. Copy timestamps: `ceph_futime(shadow_fd, times)`
7. Atomic rename: `ceph_rename(cmount, shadow, path)`
8. New inode created due to cross-pool rename

## CephFS Pool Configuration

**Mount**: `/tiercephfs`

**Data Pools**:
- `cephfs.tiercephfs.data` - SSD (hot, fast)
- `cephfs.tiercephfs.warm` - Mixed or HDD
- `cephfs.tiercephfs.cold` - HDD (archive)

**Pool Assignment**:
```bash
# Check file's current pool
getfattr -n ceph.file.layout /tiercephfs/myfile.txt

# Set pool manually (for testing)
setfattr -n ceph.file.layout.pool -v cephfs.tiercephfs.warm /tiercephfs/myfile.txt
```

## Performance

| Metric | Value |
|--------|-------|
| **eBPF Events** | ~1000/sec per client |
| **Hot Table Inserts** | Real-time, append-only |
| **Aggregation** | Every 60 seconds |
| **Migration Workers** | 5 parallel threads |
| **Migration Speed** | ~5-10 files/sec (depends on file size) |
| **Subdirectory Support** | ✅ Full path resolution |

## Installation

### Prerequisites

```bash
# Ubuntu 24.04
sudo apt update && sudo apt install -y \
    python3 \
    python3-pip \
    python3-bpfcc \
    bpfcc-tools \
    linux-headers-$(uname -r) \
    postgresql-14 \
    postgresql-client-14 \
    libcephfs-dev \
    gcc \
    make

# Python dependencies
pip3 install psycopg2-binary

# Verify eBPF/BTF support
ls /sys/kernel/btf/vmlinux  # Should exist (kernel 5.15+)
```

### Database Setup

```bash
# Create database and user
sudo -u postgres createdb tiering
sudo -u postgres psql <<EOF
CREATE USER tiering_user WITH PASSWORD '1';
GRANT ALL PRIVILEGES ON DATABASE tiering TO tiering_user;
EOF

# Load schema
sudo -u postgres psql tiering <<'EOSQL'
-- Hot table (append-only)
CREATE TABLE file_access_log (
    id BIGSERIAL PRIMARY KEY,
    uid INT NOT NULL,
    inode BIGINT NOT NULL,
    path TEXT NOT NULL,
    access_time TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Cold table (aggregated)
CREATE TABLE file_metadata (
    inode BIGINT PRIMARY KEY,
    path TEXT NOT NULL,
    current_pool TEXT NOT NULL DEFAULT 'cephfs.tiercephfs.data',
    target_pool TEXT,
    last_access TIMESTAMP NOT NULL,
    needs_migration BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_needs_migration ON file_metadata(needs_migration, target_pool);
CREATE INDEX idx_last_access ON file_metadata(last_access);

-- Aggregation function with ID-based watermark
CREATE OR REPLACE FUNCTION aggregate_access_log()
RETURNS TABLE(processed_count BIGINT)
LANGUAGE plpgsql
AS $$
DECLARE
    processed BIGINT := 0;
    max_id BIGINT;
BEGIN
    -- Get max ID before aggregation starts (watermark)
    SELECT COALESCE(MAX(id), 0) INTO max_id FROM file_access_log;
    
    IF max_id = 0 THEN
        RETURN QUERY SELECT 0::BIGINT;
        RETURN;
    END IF;
    
    -- Aggregate only entries up to max_id
    WITH latest_access AS (
        SELECT 
            inode,
            path,
            MAX(access_time) as last_access
        FROM file_access_log
        WHERE id <= max_id
        GROUP BY inode, path
    )
    INSERT INTO file_metadata (inode, path, last_access, current_pool)
    SELECT inode, path, last_access, 'cephfs.tiercephfs.data'
    FROM latest_access
    ON CONFLICT (inode) DO UPDATE 
    SET last_access = EXCLUDED.last_access,
        path = EXCLUDED.path;
    
    -- Count processed
    SELECT COUNT(*) INTO processed FROM file_access_log WHERE id <= max_id;
    
    -- Delete ONLY the entries we just processed
    DELETE FROM file_access_log WHERE id <= max_id;
    
    RETURN QUERY SELECT processed;
END;
$$;
EOSQL

# Grant permissions
sudo -u postgres psql tiering <<EOF
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO tiering_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO tiering_user;
GRANT EXECUTE ON FUNCTION aggregate_access_log() TO tiering_user;
EOF
```

### Build libcephfs_migrate

```bash
cd ~/cephse

# Compile migration binary
gcc -o libcephfs_migrate libcephfs_migrate.c -lcephfs

# Install system-wide
sudo cp libcephfs_migrate /usr/local/bin/
sudo chmod +x /usr/local/bin/libcephfs_migrate

# Test
/usr/local/bin/libcephfs_migrate --help
```

### Install Service Files

```bash
# Copy service files
sudo cp cephfs-tracker.service /etc/systemd/system/
sudo cp cephfs-policy-engine.service /etc/systemd/system/
sudo cp cephfs-migration-worker.service /etc/systemd/system/

# Edit paths in service files if needed
sudo nano /etc/systemd/system/cephfs-tracker.service
# Update WorkingDirectory and ExecStart paths

# Reload systemd
sudo systemctl daemon-reload
```

## Usage

### Start Services

```bash
# Start tracker (eBPF + aggregation)
sudo systemctl start cephfs-tracker
sudo systemctl enable cephfs-tracker

# Start policy engine
sudo systemctl start cephfs-policy-engine
sudo systemctl enable cephfs-policy-engine

# Start migration worker
sudo systemctl start cephfs-migration-worker
sudo systemctl enable cephfs-migration-worker

# Check status
sudo systemctl status cephfs-tracker
sudo systemctl status cephfs-policy-engine
sudo systemctl status cephfs-migration-worker
```

### Monitor Logs

```bash
# Tracker logs (should show every 60s)
sudo journalctl -u cephfs-tracker -f
# Output:
# Monitoring started
# [17:52:23] Wrote 5 files to file_metadata. Sleeping for 60s
# [17:53:23] Wrote 0 files to file_metadata. Sleeping for 60s

# Policy engine logs
sudo journalctl -u cephfs-policy-engine -f

# Migration worker logs
sudo journalctl -u cephfs-migration-worker -f
# Output:
# Processing 2 files with 5 workers
# Migrating inode 1099511629379: key/tea.txt -> cephfs.tiercephfs.warm
# ✓ Migrated key/tea.txt in 4998ms (inode: 1099511629379 → 1099511629380)
```

### Query Database

```bash
# Connect to database
sudo -u postgres psql tiering

# View all tracked files
SELECT inode, path, current_pool, last_access 
FROM file_metadata 
ORDER BY last_access DESC 
LIMIT 10;

# Files pending migration
SELECT inode, path, current_pool, target_pool, last_access
FROM file_metadata 
WHERE needs_migration = TRUE;

# Count files per pool
SELECT current_pool, COUNT(*) as file_count
FROM file_metadata 
GROUP BY current_pool;

# Hot table size (should stay small due to 60s aggregation)
SELECT COUNT(*) as pending_events FROM file_access_log;
```

### Manual Testing

```bash
# Create test file
sudo -u testuser1 echo "test data" > /tiercephfs/testfile.txt

# Wait 60s for aggregation
sleep 60

# Check if tracked
sudo -u postgres psql tiering -c \
  "SELECT * FROM file_metadata WHERE path = 'testfile.txt';"

# Wait 3+ minutes for promotion (test mode: 3 min = 30 days)
sleep 180

# Check migration status
sudo -u postgres psql tiering -c \
  "SELECT * FROM file_metadata WHERE path = 'testfile.txt';"

# Should show: target_pool = 'cephfs.tiercephfs.warm', needs_migration = TRUE

# Wait for migration worker
sleep 30

# Verify migrated
sudo -u postgres psql tiering -c \
  "SELECT * FROM file_metadata WHERE path = 'testfile.txt';"

# Should show: current_pool = 'cephfs.tiercephfs.warm', needs_migration = FALSE

# Verify file still accessible
cat /tiercephfs/testfile.txt  # Should work!

# Check actual pool
getfattr -n ceph.file.layout /tiercephfs/testfile.txt
```

## Configuration

### Adjust Test Mode Intervals

**Policy Engine** (`policy_engine.py`):
```python
# Current: 3 minutes = 30 days
TEST_MODE_MULTIPLIER = 10  # 1 minute = 10 days

# Change to real intervals (production):
TEST_MODE = False  # Disable test mode
# Then: 30 days = 30 days (real time)
```

### Change Aggregation Frequency

**Tracker** (`monitoring_ebpf_tracker.py`):
```python
# Line 17
AGGREGATE_INTERVAL = 60  # Change to 120 for 2 minutes, etc.
```

### Increase Migration Workers

**Migration Worker** (`migration_worker.py`):
```bash
# Edit service file
sudo nano /etc/systemd/system/cephfs-migration-worker.service

# Change --workers parameter
ExecStart=/usr/bin/python3 /path/to/migration_worker.py --workers 10

# Restart
sudo systemctl daemon-reload
sudo systemctl restart cephfs-migration-worker
```

### Custom Tiering Policies

Edit policy logic in `policy_engine.py`:
```python
# Example: More aggressive cold storage
COLD_THRESHOLD = timedelta(minutes=4)  # Was 6 minutes

# Example: Different promotion logic
if pool == 'data' and age > WARM_THRESHOLD and file_size > 100MB:
    mark_for_migration(inode, 'warm')
```

## Troubleshooting

### Tracker Not Starting

```bash
# Check eBPF/BCC installation
python3 -c "from bcc import BPF; print('BCC OK')"

# Check kernel BTF support
ls /sys/kernel/btf/vmlinux

# Check if CephFS functions exist
sudo bpftrace -l 'kprobe:ceph_read_iter'

# View detailed errors
sudo journalctl -u cephfs-tracker -n 50
```

### Files Not Being Tracked

```bash
# Check if eBPF is attached
sudo bpftool prog list | grep ceph

# Manually trigger aggregation
sudo -u postgres psql tiering -c "SELECT * FROM aggregate_access_log();"

# Check hot table
sudo -u postgres psql tiering -c "SELECT * FROM file_access_log LIMIT 10;"
```

### Migration Failures

```bash
# Check libcephfs_migrate binary
which libcephfs_migrate
libcephfs_migrate /tiercephfs/testfile.txt cephfs.tiercephfs.warm

# Check CephFS pools
ceph fs ls
ceph osd pool ls

# View migration errors
sudo journalctl -u cephfs-migration-worker | grep ERROR

# Common issues:
# - Pool doesn't exist
# - File path incorrect (missing subdirectory)
# - Permissions issue
# - CephFS mount not at /tiercephfs
```

### Database Growing Too Large

```bash
# Check table sizes
sudo -u postgres psql tiering -c "
    SELECT 
        schemaname, tablename,
        pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
    FROM pg_tables 
    WHERE schemaname = 'public' 
    ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
"

# If file_access_log is large (aggregation not running):
sudo systemctl status cephfs-tracker
sudo -u postgres psql tiering -c "SELECT COUNT(*) FROM file_access_log;"

# Manual aggregation
sudo -u postgres psql tiering -c "SELECT * FROM aggregate_access_log();"

# Vacuum database
sudo -u postgres psql tiering -c "VACUUM ANALYZE;"
```

### Subdirectory Files Not Migrating

```bash
# This should be fixed in current version!
# If issues persist, check:

# 1. Path in database should include subdirectory
sudo -u postgres psql tiering -c \
  "SELECT inode, path FROM file_metadata WHERE path LIKE '%/%';"

# Should show: key/tea.txt, not just: tea.txt

# 2. Tracker using find for path resolution
sudo journalctl -u cephfs-tracker | grep find
```

## Architecture Decisions

### Why Python + BCC Instead of C++ + libbpf?
- **Rapid development**: Python easier to modify and test
- **BCC handles BTF**: Automatic CO-RE (Compile Once, Run Everywhere)
- **Performance sufficient**: <1000 events/sec per client node

### Why Hot/Cold Tables Instead of RocksDB?
- **Simpler stack**: One less dependency
- **PostgreSQL sufficient**: Hot table is append-only (fast)
- **Easier operations**: Standard SQL for queries and maintenance
- **No stale cache issues**: Direct database updates

### Why Find for Path Resolution?
- **eBPF verifier constraints**: Complex dentry walking rejected
- **Reliability**: find guaranteed to return correct path
- **Performance acceptable**: Cached by filesystem, <50ms
- **Simplicity**: No complex kernel struct navigation

### Why Shadow File Migration?
- **Atomic operation**: Rename is atomic, no partial migration
- **Data integrity**: Original file untouched until rename
- **Pool change**: CephFS creates new inode on cross-pool rename
- **No downtime**: File briefly at old path + new path, then switched

## Production Deployment

### Multi-Client Setup
Run tracker on **each CephFS client node**:
```bash
# Node 1
sudo systemctl start cephfs-tracker

# Node 2  
sudo systemctl start cephfs-tracker

# Centralized database (PostgreSQL accepts connections from all)
# Edit postgresql.conf: listen_addresses = '*'
# Edit pg_hba.conf: Allow client IPs
```

### Monitoring
```bash
# Add monitoring checks
watch -n 10 "sudo -u postgres psql tiering -c \
  'SELECT needs_migration, COUNT(*) FROM file_metadata GROUP BY needs_migration'"

# Prometheus exporter (create custom script)
curl http://localhost:9090/metrics
# tiering_files_pending_migration{pool="warm"} 5
# tiering_files_tracked_total 1000
```

### Backup Strategy
```bash
# Backup PostgreSQL daily
sudo -u postgres pg_dump tiering > tiering_backup_$(date +%Y%m%d).sql

# Restore if needed
sudo -u postgres psql tiering < tiering_backup_20260112.sql
```

## Performance Tuning

### For High File Count (1M+ files)
```python
# Increase aggregation workers
AGGREGATE_INTERVAL = 30  # More frequent, smaller batches

# Partition file_metadata table
CREATE TABLE file_metadata_partition_0 PARTITION OF file_metadata 
  FOR VALUES FROM (0) TO (1000000000000);
```

### For High Access Rate
```python
# Increase eBPF deduplication window
# In monitoring_ebpf_tracker.py, eBPF code:
if (last && (now - *last) < 5000000000ULL) {  # 5 seconds instead of 1
```

### For Faster Migrations
```bash
# Increase workers
--workers 20

# Or use multiple migration worker instances
# Each with different worker counts
```

## System Requirements

- **OS**: Ubuntu 24.04 (kernel 6.8+ with BTF)
- **RAM**: 2GB minimum, 4GB recommended
- **Storage**: 10GB for PostgreSQL (scales with file count)
- **CephFS**: Reef 19.2.3+ with multiple data pools
- **Python**: 3.10+
- **PostgreSQL**: 14+

## License

MIT License - See LICENSE file

## Contributing

Contributions welcome! Please test with:
```bash
# Create test file in subdirectory
sudo -u testuser mkdir -p /tiercephfs/test/nested/deep
sudo -u testuser echo "test" > /tiercephfs/test/nested/deep/file.txt

# Verify full path tracked
sleep 60
sudo -u postgres psql tiering -c \
  "SELECT * FROM file_metadata WHERE path LIKE '%deep/file%';"
```

## Support

For issues:
1. Check logs: `sudo journalctl -u cephfs-* -n 100`
2. Verify database: `sudo -u postgres psql tiering`
3. Test manually: Create file, wait, check migration
4. Open GitHub issue with logs
