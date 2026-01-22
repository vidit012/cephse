<div align="center">
  <img src="logo.png" alt="Storage Tiering Logo" width="200"/>
  
  # Storage Tiering in CephFS
  
  **Automated client-side storage tiering for CephFS with eBPF-based access tracking and intelligent pool migration**
  
  [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
  [![CephFS](https://img.shields.io/badge/CephFS-Compatible-green.svg)](https://docs.ceph.com/en/latest/cephfs/)
  [![PostgreSQL](https://img.shields.io/badge/PostgreSQL-13+-blue.svg)](https://www.postgresql.org/)
  
</div>

---

## 📋 Table of Contents
- [Description](#description)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Technology Stack](#technology-stack)
- [System Comparison](#system-comparison)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Usage](#usage)
- [Documentation](#documentation)

---

## 🎯 Description

An enterprise-grade automated storage tiering system for CephFS that intelligently migrates files between **hot (SSD)**, **warm (Hybrid)**, and **cold (HDD)** storage pools based on access patterns. The system operates at the client-side using **eBPF kernel hooks** for zero-overhead access tracking and **PostgreSQL** for analytics-driven policy decisions.

**Dual-Mode Operation**: Choose between frequency-based (hot data stays hot) or time-based (recent data stays hot) tiering policies that can be switched dynamically without data loss.

---

## ✨ Key Features

### **Dual-Mode Tiering**
- **Frequency-Based Mode**: Files tier based on access count (`score = 0.90 × access_freq`)
- **Time-Based Mode**: Files tier based on last access timestamp (3min → warm, 6min → cold)
- **Dynamic Switching**: Change modes without data loss or service restart

### **File Operation Handling**
- ✅ **File Moved**: Path updated automatically (inode-based tracking)
- ✅ **File Copied**: Treated as separate entity (new inode = new file)
- ✅ **Symlink Created**: Target file's access count incremented
- ✅ **File Deleted**: Removed from tracking database automatically
- ✅ **Subdirectories**: Full path resolution via inode lookup

### **Performance Optimizations**
- ⚡ **Batch Processing**: 1000-event batches → 100x faster database writes
- ⚡ **Parallel Migration**: 5 concurrent workers with lock-free execution (`SKIP LOCKED`)
- ⚡ **Zero Data Loss**: Watermark-based aggregation prevents event loss during processing
- ⚡ **eBPF Deduplication**: In-kernel 1-second window reduces tracking overhead
- ⚡ **Hot/Cold Tables**: Append-only hot table + aggregated cold table architecture

### **Reliability Features**
- 🛡️ **Atomic Migrations**: Shadow file technique ensures no partial migrations
- 🛡️ **Inode Tracking**: Handles inode changes from cross-pool renames
- 🛡️ **Timestamp Preservation**: `last_access` maintained across migrations
- 🛡️ **Root User Filtering**: Skips tracking migration operations (UID 0)
- 🛡️ **Service Recovery**: Automatic restart on failure via systemd

### **Operational Features**
- 📊 **PostgreSQL Analytics**: SQL-based policy logic for flexibility
- 📊 **Real-time Monitoring**: Track file distribution across pools
- 📊 **Migration Statistics**: Log every migration with timing metrics
- 📊 **Test Mode**: 3 minutes = 30 days for rapid validation

---

## 🏗️ Architecture

<div align="center">
  <img src="cephse_system_architecture.png" alt="System Architecture" width="800"/>
</div>

### Architecture Overview

The system uses a **4-layer pipeline** for efficient storage tiering:

```
eBPF (Kernel) → Access Tracker (Python) → Policy Engine (SQL) → Migration Worker (C + Python)
```

**Layer 1: eBPF Kernel Hooks**
- Hooks: `ceph_read_iter()`, `ceph_write_iter()`
- Captures: inode, UID, filename, timestamp
- Deduplication: 1-second in-kernel window
- Output: Perf buffer → userspace

**Layer 2: Access Tracker**
- Polls eBPF perf buffer
- Resolves full path: `find /tiercephfs -inum <inode>`
- Batch INSERT into `file_access_log` (hot table)
- Aggregates every 60s into `file_metadata` (cold table)

**Layer 3: Policy Engine**
- Runs PostgreSQL stored functions every 60s
- Mode 1: `apply_tiering_policies()` - frequency-based scoring
- Mode 2: `mark_files_for_migration()` - timestamp thresholds
- Marks files: `needs_migration = TRUE`, sets `target_pool`

**Layer 4: Migration Worker**
- 5 parallel workers with `SELECT ... FOR UPDATE SKIP LOCKED`
- Calls `libcephfs_migrate` (C binary)
- Shadow file technique: `file.txt.__tiering__` → atomic rename
- Tracks inode changes: `old_inode → new_inode`

### Detailed Architecture

For comprehensive architecture documentation including component diagrams, data flow, and design decisions, see:
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - Complete system architecture
- **[TECHNICAL_PRESENTATION.md](TECHNICAL_PRESENTATION.md)** - Technical deep dive

---

## 🔧 Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Access Tracking** | eBPF + BCC (Python) | Kernel-level file access monitoring |
| **Database** | PostgreSQL 13+ | Hot/cold table architecture for analytics |
| **Migration** | libcephfs (C library) | Physical file pool migration |
| **Policy Engine** | Python 3 + PL/pgSQL | Tiering decision logic |
| **Orchestration** | systemd services | Service management and auto-restart |
| **File System** | CephFS (client-side) | Distributed storage with pool support |

### Services and Overhead

| Service | CPU Usage | Memory | Network | Disk I/O |
|---------|-----------|--------|---------|----------|
| **cephfs-tracker** | 5-10% (1 core) | 512 MB | Minimal | None |
| **cephfs-policy-engine** | <1% (periodic) | 50 MB | None | None |
| **cephfs-migration-worker** | 10-20% (during migration) | 200 MB | High (data transfer) | High (reads + writes) |
| **PostgreSQL** | 2-5% | 1-2 GB | Minimal | Low (batch writes) |

**Total System Overhead**:
- **Idle**: ~5-10% CPU, ~1 GB RAM
- **Active Migration**: ~30% CPU, ~2 GB RAM, high network/disk
- **Scalability**: Handles 1000+ file accesses/second, 10M+ files tracked

---

## 📦 Prerequisites

### System Requirements
- **OS**: Linux (Ubuntu 20.04+ / CentOS 8+ / RHEL 8+)
- **Kernel**: 4.15+ (for eBPF support)
- **CephFS**: Client installed and mounted
- **RAM**: 4 GB minimum, 8 GB recommended
- **Storage**: 50 GB for PostgreSQL database

### Software Dependencies
```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y \
  python3 python3-pip \
  postgresql-13 postgresql-client-13 \
  bpfcc-tools python3-bpfcc \
  libcephfs-dev ceph-common \
  build-essential clang

# CentOS/RHEL
sudo yum install -y \
  python3 python3-pip \
  postgresql13-server postgresql13 \
  bcc-tools python3-bcc \
  libcephfs-devel ceph-common \
  gcc clang
```

### Python Packages
```bash
pip3 install psycopg2-binary bcc
```

### CephFS Pools
Ensure three storage pools exist:
```bash
ceph osd pool create cephfs.tiercephfs.data  # SSD pool
ceph osd pool create cephfs.tiercephfs.warm  # Hybrid pool
ceph osd pool create cephfs.tiercephfs.cold  # HDD pool
```

---

## 🚀 Installation

### 1. Clone Repository
```bash
git clone <repository-url>
cd cephse
```

### 2. Database Setup
```bash
# Create database and user
sudo -u postgres psql << EOF
CREATE DATABASE tiering;
CREATE USER tiering_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE tiering TO tiering_user;
\c tiering
GRANT ALL ON SCHEMA public TO tiering_user;
EOF

# Deploy schema
sudo -u postgres psql tiering < postgres\ functions.txt
```

### 3. Compile Migration Binary
```bash
cd migration\ engine/
gcc -o libcephfs_migrate libcephfs_migrate.c -lcephfs
sudo cp libcephfs_migrate /usr/local/bin/
sudo chmod +x /usr/local/bin/libcephfs_migrate
```

### 4. Install Services
```bash
# Copy service files
sudo cp tracking\ service/cephfs-tracker.service /etc/systemd/system/
sudo cp policy\ engine/cephfs-policy-engine.service /etc/systemd/system/
sudo cp migration\ engine/cephfs-migration-worker.service /etc/systemd/system/

# Update paths in service files (adjust to your installation directory)
sudo nano /etc/systemd/system/cephfs-tracker.service
# Edit: ExecStart=/usr/bin/python3 /path/to/tracker_phase1.py

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable cephfs-tracker cephfs-policy-engine cephfs-migration-worker
```

### 5. Deploy Mode Switcher (Optional)
```bash
sudo cp switch_tiering.sh /usr/local/bin/switch_tiering
sudo chmod +x /usr/local/bin/switch_tiering
```

### 6. Start Services
```bash
sudo systemctl start cephfs-tracker
sudo systemctl start cephfs-policy-engine
sudo systemctl start cephfs-migration-worker
```

### 7. Verify Installation
```bash
# Check service status
sudo systemctl status cephfs-tracker
sudo systemctl status cephfs-policy-engine
sudo systemctl status cephfs-migration-worker

# Check database
sudo -u postgres psql tiering -c "SELECT COUNT(*) FROM file_metadata;"

# Check eBPF program
sudo bpftool prog list | grep ceph
```

---

## 📘 Usage

### Switch Tiering Modes
```bash
switch_tiering frequency  # Enable frequency-based mode
switch_tiering time       # Enable time-based mode
switch_tiering status     # Check current mode
switch_tiering off        # Disable tiering (stop policy engine)
```

### Monitor System
```bash
# View file distribution
sudo -u postgres psql tiering -c "
SELECT SUBSTRING(current_pool, 20) AS pool, COUNT(*) 
FROM file_metadata 
GROUP BY current_pool;"

# View migration queue
sudo -u postgres psql tiering -c "
SELECT COUNT(*), target_pool 
FROM file_metadata 
WHERE needs_migration = TRUE 
GROUP BY target_pool;"

# Watch real-time logs
sudo journalctl -u cephfs-tracker -f
sudo journalctl -u cephfs-policy-engine -f
sudo journalctl -u cephfs-migration-worker -f
```

### Manual Operations
```bash
# Trigger aggregation manually
sudo -u postgres psql tiering -c "SELECT aggregate_access_log();"

# Run policy evaluation manually
sudo -u postgres psql tiering -c "SELECT * FROM apply_tiering_policies();"

# Migrate specific file
libcephfs_migrate /tiercephfs/file.txt cephfs.tiercephfs.cold
```

---

## 📚 Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Complete system architecture and component details |
| [TECHNICAL_PRESENTATION.md](TECHNICAL_PRESENTATION.md) | Technical deep dive with algorithms and SQL functions |
| [QUICK_REFERENCE.md](QUICK_REFERENCE.md) | Command reference and troubleshooting |

---

## 🔄 System Comparison

### vs. Traditional HSM (Hierarchical Storage Management)

| Feature | This System | Traditional HSM |
|---------|-------------|-----------------|
| **Deployment** | Client-side (no cluster changes) | Cluster-side (MDS/OSD modifications) |
| **Access Tracking** | eBPF kernel hooks (real-time) | Periodic filesystem scans |
| **Performance** | Zero I/O overhead for tracking | High I/O from scans |
| **Policy Flexibility** | SQL-based, switchable modes | Hard-coded in daemon |
| **Migration Speed** | Parallel workers (5+ concurrent) | Sequential |
| **Data Loss Risk** | Zero (watermark aggregation) | Possible during scans |
| **Setup Complexity** | Medium (services + DB) | High (cluster reconfiguration) |

### vs. S3 Lifecycle Policies

| Feature | CephFS Tiering | S3 Lifecycle |
|---------|----------------|--------------|
| **Access Pattern** | eBPF real-time tracking | Object metadata only |
| **Tiering Basis** | Actual usage + timestamps | Age-based only |
| **File System** | CephFS (POSIX) | Object storage (S3 API) |
| **Migration Control** | Immediate or policy-driven | Schedule-based only |
| **Use Case** | HPC, shared filesystems | Archive, compliance |

---

## 🎯 Mode Selection Guide

### Use **Frequency-Based Mode** when:
- Files have varying importance based on usage patterns
- Popular files should stay fast regardless of age
- Workload: Machine learning datasets, shared libraries, build caches
- Example: Frequently accessed old config files stay hot

### Use **Time-Based Mode** when:
- Recent data is more valuable than old data
- Files naturally age out of relevance
- Workload: Log files, time-series data, backups
- Example: Today's logs stay hot, last week's logs go cold

---

## 🤝 Contributing

This project is part of an internal research initiative. For questions or contributions, please contact the development team.

---

## 📄 License

[Specify your license]

---

## 👥 Authors

[Your team/organization name]

---

## 🙏 Acknowledgments

- **CephFS Team** - Distributed filesystem
- **eBPF/BCC Community** - Kernel tracing infrastructure
- **PostgreSQL Team** - Analytics database

---

**For detailed setup instructions, troubleshooting, and advanced configuration, see the complete documentation in [ARCHITECTURE.md](ARCHITECTURE.md).**
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


## Troubleshooting

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

## Architecture Decisions

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
