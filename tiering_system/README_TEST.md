# CephFS 3-Tier Automated Tiering System (TEST MODE)

Production-grade storage tiering system for CephFS using eBPF tracking, RocksDB caching, and PostgreSQL persistence.

**TEST CONFIGURATION**: Uses 3-minute intervals instead of days for rapid testing.

## Quick Start (Testing)

```bash
# 1. Run automated setup
cd /home/vidit-pt7945/cephse/tiering_system
chmod +x scripts/*.sh
bash scripts/setup_test_environment.sh

# 2. Start services (3 terminals)
# Terminal 1: Policy Engine (runs every 60 seconds)
python3 src/policy_engine_test.py --interval 60

# Terminal 2: Migration Worker
python3 src/migration_worker.py --workers 5 --libcephfs-bin ../libcephfs_migrate

# Terminal 3: Monitor database
watch -n 10 'sudo -u postgres psql tiering -c "SELECT * FROM pool_statistics;"'

# 3. Test file migration (creates files in hot pool)
echo "test data" > /tiercephfs/test_tiering/myfile.txt
# Wait 3 minutes → Policy marks for warm pool
# Wait 6 minutes → Policy marks for cold pool
```

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│    3-Tier Storage Pools (3-minute intervals)             │
│                                                          │
│    Hot  (cephfs.tiercephfs.hot)  ← New files           │
│      ↓ 3 minutes                                        │
│    Warm (cephfs.tiercephfs.data) ← Active files        │
│      ↓ 3 minutes                                        │
│    Cold (cephfs.tiercephfs.cold) ← Archive files       │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│              eBPF Kernel Hooks (OPTIONAL)                │
│   (tracks ceph_read_iter + ceph_write_iter)             │
└────────────────────┬─────────────────────────────────────┘
                     ↓ Ring buffer (8MB, 1M events/sec)
┌──────────────────────────────────────────────────────────┐
│          Access Tracker (C++) - OPTIONAL                 │
│  • RocksDB: Hot storage (ns lookups, 100K writes/sec)   │
│  • PostgreSQL: Flushed every 60s for persistence        │
└────────────────────┬─────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│          PostgreSQL Database                             │
│  • file_metadata: Tracks files, pools, ages             │
│  • tiering_policies: hot→warm (3 min), warm→cold (3min)│
└────────────────────┬─────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│          Policy Engine (Python) - REQUIRED               │
│  • Runs every 60 seconds (test mode)                    │
│  • Marks files for migration based on age               │
└────────────────────┬─────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│          Migration Workers (Python) - REQUIRED           │
│  • 5-10 parallel workers                                │
│  • Calls libcephfs_migrate binary                       │
│  • Records success/failure in audit log                 │
└──────────────────────────────────────────────────────────┘
```

## Test Timeline

```
Time    Pool    Action
──────  ──────  ─────────────────────────────────────
0:00    HOT     File created, written to hot pool
3:00    HOT     Policy engine marks for WARM migration
3:05    WARM    Migration worker moves file to warm pool
6:05    WARM    Policy engine marks for COLD migration
6:10    COLD    Migration worker moves file to cold pool
```

## Components

### 1. CephFS Pools (3 tiers)
- **Hot** (`cephfs.tiercephfs.hot`): New files, default for writes
- **Warm** (`cephfs.tiercephfs.data`): Files older than 3 minutes
- **Cold** (`cephfs.tiercephfs.cold`): Files older than 6 minutes total

### 2. PostgreSQL Database (REQUIRED)
- **file_metadata**: Main table (inode, path, last_access, current_pool, needs_migration)
- **tiering_policies**: Configurable rules (TEST: 3-minute intervals)
- **migration_history**: Audit log with success/failure
- **tiering_stats**: Historical statistics

### 3. Policy Engine (REQUIRED)
- **Location**: [src/policy_engine_test.py](src/policy_engine_test.py)
- **Function**: Marks files for migration based on age
- **Schedule**: Every 60 seconds (configurable)
- **Database**: Calls `apply_tiering_policies()` stored procedure

### 4. Migration Worker (REQUIRED)
- **Location**: [src/migration_worker.py](src/migration_worker.py)
- **Function**: Executes actual file migrations
- **Parallelism**: 5-10 workers (configurable)
- **Locking**: `FOR UPDATE SKIP LOCKED` (no conflicts)
- **Binary**: Calls `libcephfs_migrate` for each file

### 5. eBPF Tracker (OPTIONAL)
- **Location**: [ebpf/cephfs_tracker.bpf.c](ebpf/cephfs_tracker.bpf.c)
- **Function**: Tracks real-time file access in kernel
- **Output**: Ring buffer with inode, timestamp, path
- **Note**: Can skip for testing, use manual file creation instead

### 6. Access Tracker (OPTIONAL)
- **Location**: [src/access_tracker.cpp](src/access_tracker.cpp)
- **Function**: Consumes eBPF events, updates RocksDB + PostgreSQL
- **Note**: Can skip for testing, use manual database updates

## Installation

### Prerequisites
- Ubuntu 24.04 (or similar)
- CephFS mounted (e.g., `/tiercephfs`)
- PostgreSQL 14+
- Python 3.8+
- gcc (for compiling libcephfs_migrate)

### Step 1: Create 3 Storage Pools

```bash
# Run automated pool setup
cd /home/vidit-pt7945/cephse/tiering_system
bash scripts/setup_3tier_pools.sh

# Verify pools
ceph fs ls
ceph osd pool ls | grep tiercephfs

# Set hot pool as default for new files
sudo setfattr -n ceph.dir.layout.pool -v cephfs.tiercephfs.hot /tiercephfs
```

### Step 2: Setup PostgreSQL Database

```bash
# Create database and user
sudo -u postgres createdb tiering
sudo -u postgres psql tiering -c "CREATE USER tiering_user WITH PASSWORD 'tiering_pass';"

# Load TEST schema (3-minute intervals)
sudo -u postgres psql tiering < sql/schema_test.sql

# Grant permissions
sudo -u postgres psql tiering -c "GRANT ALL PRIVILEGES ON DATABASE tiering TO tiering_user;"
sudo -u postgres psql tiering -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO tiering_user;"
sudo -u postgres psql tiering -c "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO tiering_user;"
```

### Step 3: Compile libcephfs_migrate

```bash
# Compile migration binary
cd /home/vidit-pt7945/cephse
gcc -o libcephfs_migrate libcephfs_migrate.c -lcephfs

# Test it
./libcephfs_migrate
# Should show usage
```

### Step 4: Install Python Dependencies

```bash
pip3 install psycopg2-binary
```

## Usage (Testing Mode)

### Simple Test (No eBPF)

For quick testing without eBPF overhead:

```bash
# Terminal 1: Start Policy Engine
cd /home/vidit-pt7945/cephse/tiering_system
python3 src/policy_engine_test.py --interval 60
# Output shows: policies applied every 60 seconds

# Terminal 2: Start Migration Worker
python3 src/migration_worker.py \
    --workers 5 \
    --libcephfs-bin /home/vidit-pt7945/cephse/libcephfs_migrate \
    --interval 30
# Output shows: files being migrated

# Terminal 3: Create test files
mkdir -p /tiercephfs/test_tiering
for i in {1..10}; do
    echo "Test file $i - $(date)" > /tiercephfs/test_tiering/file_$i.txt
done

# Manually insert into database (since no eBPF tracking)
sudo -u postgres psql tiering <<EOF
INSERT INTO file_metadata (inode, path, size_bytes, last_access, current_pool)
VALUES 
    (1001, '/tiercephfs/test_tiering/file_1.txt', 1024, NOW() - INTERVAL '5 minutes', 'cephfs.tiercephfs.hot'),
    (1002, '/tiercephfs/test_tiering/file_2.txt', 1024, NOW() - INTERVAL '5 minutes', 'cephfs.tiercephfs.hot'),
    (1003, '/tiercephfs/test_tiering/file_3.txt', 1024, NOW() - INTERVAL '8 minutes', 'cephfs.tiercephfs.data')
ON CONFLICT (inode) DO NOTHING;
EOF

# Wait 1-2 minutes and check pool statistics
sudo -u postgres psql tiering -c "SELECT * FROM pool_statistics;"
```

### Full System (With eBPF - Optional)

If you want real-time access tracking:

```bash
# Terminal 1: Access Tracker (requires root for eBPF)
cd /home/vidit-pt7945/cephse/tiering_system
# First compile eBPF and C++ (see Build section below)
sudo ./bin/access_tracker \
    ebpf/cephfs_tracker.bpf.o \
    /var/lib/tiering/rocks \
    'host=localhost dbname=tiering user=tiering_user password=tiering_pass'

# Terminal 2: Policy Engine
python3 src/policy_engine_test.py --interval 60

# Terminal 3: Migration Worker
python3 src/migration_worker.py --workers 5 --libcephfs-bin ../libcephfs_migrate
```

## Monitoring

### Check Pool Statistics

```bash
# Live view
watch -n 10 'sudo -u postgres psql tiering -c "SELECT * FROM pool_statistics;"'

# Manual query
sudo -u postgres psql tiering -c "
SELECT 
    current_pool,
    file_count,
    total_bytes / 1024 / 1024 as total_mb,
    avg_age_minutes,
    pending_migrations
FROM pool_statistics
ORDER BY current_pool;
"
```

### Check Migration History

```bash
# Recent migrations
sudo -u postgres psql tiering -c "
SELECT 
    path,
    from_pool,
    to_pool,
    status,
    duration_ms,
    completed_at
FROM migration_history
ORDER BY completed_at DESC
LIMIT 20;
"

# Migration success rate
sudo -u postgres psql tiering -c "
SELECT 
    status,
    COUNT(*) as count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER(), 2) as percentage
FROM migration_history
GROUP BY status;
"
```

### Check Active Policies

```bash
sudo -u postgres psql tiering -c "
SELECT 
    name,
    source_pool,
    target_pool,
    age_minutes || ' minutes' as age_threshold,
    enabled,
    priority
FROM tiering_policies
ORDER BY priority DESC;
"
```

### Monitor Files Pending Migration

```bash
sudo -u postgres psql tiering -c "
SELECT 
    COUNT(*) as pending_count,
    target_pool,
    MIN(last_access) as oldest_file
FROM file_metadata
WHERE needs_migration = TRUE
GROUP BY target_pool;
"
```

## Configuration

### Adjust Time Intervals

For faster/slower testing, edit [sql/schema_test.sql](sql/schema_test.sql):

```sql
-- Change from 3 minutes to 1 minute
INSERT INTO tiering_policies (name, source_pool, target_pool, age_minutes, priority) VALUES
    ('hot_to_warm', 'cephfs.tiercephfs.hot', 'cephfs.tiercephfs.data', 1, 200),
    ('warm_to_cold', 'cephfs.tiercephfs.data', 'cephfs.tiercephfs.cold', 1, 100);
```

Then reload:
```bash
sudo -u postgres psql tiering < sql/schema_test.sql
```

### Adjust Policy Check Frequency

```bash
# Run policy engine more frequently (every 30 seconds)
python3 src/policy_engine_test.py --interval 30
```

### Adjust Migration Workers

```bash
# Use more workers for faster migration
python3 src/migration_worker.py --workers 10
```

## Troubleshooting

### Files Not Migrating

```bash
# 1. Check if files are marked for migration
sudo -u postgres psql tiering -c "SELECT COUNT(*) FROM file_metadata WHERE needs_migration = TRUE;"

# 2. Check if policy engine is running
ps aux | grep policy_engine_test.py

# 3. Check if migration worker is running
ps aux | grep migration_worker.py

# 4. Check migration history for errors
sudo -u postgres psql tiering -c "SELECT * FROM migration_history WHERE status = 'failed' ORDER BY completed_at DESC LIMIT 10;"
```

### Database Connection Issues

```bash
# Test connection
psql -h localhost -U tiering_user -d tiering -c "SELECT 1;"

# Check PostgreSQL is running
sudo systemctl status postgresql

# Check pg_hba.conf allows local connections
sudo grep "local.*all.*all" /etc/postgresql/14/main/pg_hba.conf
```

### Pool Not Found

```bash
# List all pools
ceph osd pool ls

# Check filesystem pools
ceph fs ls

# Verify file is in expected pool
getfattr -n ceph.file.layout.pool /tiercephfs/test_file.txt
```

### Migration Binary Fails

```bash
# Test libcephfs_migrate directly
/home/vidit-pt7945/cephse/libcephfs_migrate /tiercephfs/test.txt cephfs.tiercephfs.data

# Check for missing libraries
ldd /home/vidit-pt7945/cephse/libcephfs_migrate
```

## Converting to Production

To use this system in production with day-based intervals:

1. Use [sql/schema.sql](sql/schema.sql) instead of schema_test.sql (7 days, 30 days)
2. Use [src/policy_engine.py](src/policy_engine.py) instead of policy_engine_test.py
3. Set policy engine interval to 5 minutes: `--interval 300`
4. Deploy eBPF tracker for real-time access tracking
5. Set up systemd services for automatic startup
6. Configure monitoring and alerting

## Files

| File | Purpose | Required |
|------|---------|----------|
| `sql/schema_test.sql` | PostgreSQL schema (3-minute intervals) | ✅ Yes |
| `src/policy_engine_test.py` | Marks files for migration (test mode) | ✅ Yes |
| `src/migration_worker.py` | Executes migrations | ✅ Yes |
| `../libcephfs_migrate` | Binary to migrate single file | ✅ Yes |
| `scripts/setup_3tier_pools.sh` | Creates hot/warm/cold pools | ✅ Yes |
| `scripts/setup_test_environment.sh` | Automated full setup | ✅ Yes |
| `ebpf/cephfs_tracker.bpf.c` | Real-time access tracking | ⚠️ Optional |
| `src/access_tracker.cpp` | Processes eBPF events | ⚠️ Optional |

## Support

For production deployment assistance, see:
- [POSTGRESQL_VS_ROCKSDB.md](POSTGRESQL_VS_ROCKSDB.md) - Architecture decisions
- [README.md](README.md) - Original production documentation

## Testing Checklist

- [x] 3 pools created (hot, warm, cold)
- [x] PostgreSQL database configured
- [x] libcephfs_migrate compiled
- [x] Python dependencies installed
- [ ] Policy engine running
- [ ] Migration worker running
- [ ] Test files created in hot pool
- [ ] Files appear in file_metadata table
- [ ] After 3 minutes: files marked for warm
- [ ] After 3 minutes: files migrated to warm
- [ ] After 6 minutes: files marked for cold
- [ ] After 6 minutes: files migrated to cold

🚀 **System ready for testing!**
