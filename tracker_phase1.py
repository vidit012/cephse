#!/usr/bin/env python3
"""
Phase 1: eBPF → PostgreSQL Hot Table → PostgreSQL Cold Table
Hot table (file_access_log): Fast append-only inserts (like RocksDB)
Cold table (file_metadata): Aggregated data updated by writer thread
"""

import os
import sys
import signal
import time
import threading
from datetime import datetime
from bcc import BPF
import psycopg2

# Configuration
AGGREGATE_INTERVAL = 240  # Aggregate log → metadata every 4 minutes (240 seconds)
DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'database': 'tiering',
    'user': 'tiering_user',
    'password': '1'
}

class TieringTracker:
    def __init__(self):
        self.running = True
        self.event_count = 0
        self.log_writes = 0
        self.aggregations = 0
        
        # Setup PostgreSQL
        self.setup_postgres()
        
        # Load eBPF program
        self.setup_ebpf()
        
        # Start aggregator thread (writer thread)
        self.aggregator_thread = threading.Thread(target=self.aggregator, daemon=True)
        self.aggregator_thread.start()
        
        # Signal handlers
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
    
    def setup_postgres(self):
        """Initialize PostgreSQL connection"""
        try:
            self.pg_conn = psycopg2.connect(**DB_CONFIG)
            self.pg_conn.autocommit = True
            print(f"✓ PostgreSQL connected: {DB_CONFIG['database']}")
        except Exception as e:
            print(f"✗ PostgreSQL connection failed: {e}")
            sys.exit(1)
    
    def setup_ebpf(self):
        """Load eBPF program using BCC"""
        print("Loading eBPF program...")
        
        # Simplified BPF code for BCC
        bpf_code = """
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>
#include <linux/dcache.h>



struct access_event {
    u64 inode;
    u64 timestamp_ns;
    u32 pid;
    u32 uid;
    char path[256];
};

BPF_PERF_OUTPUT(events);
BPF_HASH(dedup, u64, u64);  // Deduplication map

static int track_access(struct pt_regs *ctx, struct kiocb *iocb) {
    struct file *file = iocb->ki_filp;
    if (!file) return 0;
    
    struct inode *inode = file->f_inode;
    if (!inode) return 0;
    
    u64 ino = inode->i_ino;
    u64 now = bpf_ktime_get_ns();
    
    // Deduplicate: skip if accessed within last second
    u64 *last = dedup.lookup(&ino);
    if (last && (now - *last) < 1000000000ULL) {
        return 0;
    }
    dedup.update(&ino, &now);
    
    // Prepare event
    struct access_event event = {};
    event.inode = ino;
    event.timestamp_ns = now;
    event.pid = bpf_get_current_pid_tgid() >> 32;
    event.uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;
    
    // Get filename
    struct dentry *dentry = file->f_path.dentry;
    if (dentry) {
        bpf_probe_read_kernel_str(&event.path, sizeof(event.path), dentry->d_name.name);
    }
    
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

int trace_read(struct pt_regs *ctx, struct kiocb *iocb) {
    return track_access(ctx, iocb);
}

int trace_write(struct pt_regs *ctx, struct kiocb *iocb) {
    return track_access(ctx, iocb);
}
"""
        
        try:
            self.bpf = BPF(text=bpf_code)
            self.bpf.attach_kprobe(event="ceph_read_iter", fn_name="trace_read")
            self.bpf.attach_kprobe(event="ceph_write_iter", fn_name="trace_write")
            self.bpf["events"].open_perf_buffer(self.handle_event)
            print("✓ eBPF program loaded and attached to CephFS")
        except Exception as e:
            print(f"✗ Failed to load eBPF: {e}")
            print("\nNote: Requires kernel with CephFS and BTF support")
            sys.exit(1)
    
    def handle_event(self, cpu, data, size):
        """Handle eBPF event - write to HOT TABLE (file_access_log)"""
        event = self.bpf["events"].event(data)
        self.event_count += 1
        
        inode = event.inode
        uid = event.uid
        path = event.path.decode('utf-8', errors='ignore')
        # Use current time when event is received
        timestamp = datetime.now()
        
        # Write to HOT table (fast append-only insert)
        try:
            with self.pg_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO file_access_log (uid, inode, path, access_time)
                    VALUES (%s, %s, %s, %s)
                """, (uid, inode, path, timestamp))
                self.log_writes += 1
        except Exception as e:
            print(f"Database error: {e}")
            self.setup_postgres()
        
        if self.event_count % 100 == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Events: {self.event_count}, "
                  f"Log writes: {self.log_writes}, Aggregations: {self.aggregations} | {path}")
    
    def aggregator(self):
        """Background thread: Aggregate hot table → cold table every 60 seconds"""
        print(f"✓ Aggregator thread started (interval: {AGGREGATE_INTERVAL}s)")
        
        while self.running:
            time.sleep(AGGREGATE_INTERVAL)
            if not self.running:
                break
            
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Aggregating log → metadata...")
            self.aggregate_log()
    
    def aggregate_log(self):
        """Aggregate file_access_log → file_metadata"""
        start_time = time.time()
        
        try:
            with self.pg_conn.cursor() as cur:
                # Call stored procedure to aggregate
                cur.execute("SELECT * FROM aggregate_access_log()")
                result = cur.fetchone()
                processed = result[0] if result else 0
                
                self.aggregations += 1
                duration = time.time() - start_time
                print(f"✓ Aggregated {processed} log entries in {duration:.2f}s")
                
        except Exception as e:
            print(f"✗ Aggregation error: {e}")
            self.setup_postgres()
    
    def shutdown(self, signum, frame):
        """Graceful shutdown"""
        print(f"\n\nShutting down... (signal {signum})")
        self.running = False
        
        # Final aggregation
        print("Final aggregation...")
        self.aggregate_log()
        
        # Cleanup
        if hasattr(self, 'pg_conn'):
            self.pg_conn.close()
        
        print(f"\nFinal Statistics:")
        print(f"  eBPF events:      {self.event_count}")
        print(f"  Log writes:       {self.log_writes}")
        print(f"  Aggregations:     {self.aggregations}")
        print("\n✓ Shutdown complete")
        sys.exit(0)
    
    def run(self):
        """Main event loop"""
        print("\n" + "="*60)
        print("  CephFS Tiering Tracker - Phase 1")
        print("  eBPF → Hot Table (log) → Cold Table (metadata)")
        print("="*60)
        print(f"\nHot Table:  file_access_log (append-only)")
        print(f"Cold Table: file_metadata (aggregated)")
        print(f"PostgreSQL: {DB_CONFIG['database']}@{DB_CONFIG['host']}")
        print(f"Aggregate:  Every {AGGREGATE_INTERVAL} seconds")
        print("\nPress Ctrl+C to stop\n")
        
        try:
            while self.running:
                self.bpf.perf_buffer_poll(timeout=1000)
        except KeyboardInterrupt:
            self.shutdown(signal.SIGINT, None)

if __name__ == '__main__':
    # Check if running as root
    if os.geteuid() != 0:
        print("This program must be run as root (for eBPF)")
        sys.exit(1)
    
    tracker = TieringTracker()
    tracker.run()

class TieringTracker:
    def __init__(self):
        self.running = True
        self.event_count = 0
        self.cache_writes = 0
        self.db_writes = 0
        
        # In-memory cache (simulates RocksDB)
        # Key: inode, Value: {'timestamp_ns': ..., 'path': ...}
        self.cache = {}
        self.cache_lock = threading.Lock()
        
        # Setup PostgreSQL
        self.setup_postgres()
        
        # Load eBPF program
        self.setup_ebpf()
        
        # Start PostgreSQL flusher thread
        self.flusher_thread = threading.Thread(target=self.postgres_flusher, daemon=True)
        self.flusher_thread.start()
        
        # Signal handlers
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
    
    def setup_postgres(self):
        """Initialize PostgreSQL connection"""
        try:
            self.pg_conn = psycopg2.connect(**DB_CONFIG)
            self.pg_conn.autocommit = True
            print(f"✓ PostgreSQL connected: {DB_CONFIG['database']}")
        except Exception as e:
            print(f"✗ PostgreSQL connection failed: {e}")
            sys.exit(1)
    
    def setup_ebpf(self):
        """Load eBPF program using BCC"""
        print("Loading eBPF program...")
        
        # Simplified BPF code for BCC
        bpf_code = """
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>
#include <linux/dcache.h>



struct access_event {
    u64 inode;
    u64 timestamp_ns;
    u32 pid;
    u32 uid;
    char path[256];
};

BPF_PERF_OUTPUT(events);
BPF_HASH(dedup, u64, u64);  // Deduplication map

static int track_access(struct pt_regs *ctx, struct kiocb *iocb) {
    struct file *file = iocb->ki_filp;
    if (!file) return 0;
    
    struct inode *inode = file->f_inode;
    if (!inode) return 0;
    
    u64 ino = inode->i_ino;
    u64 now = bpf_ktime_get_ns();
    
    // Deduplicate: skip if accessed within last second
    u64 *last = dedup.lookup(&ino);
    if (last && (now - *last) < 1000000000ULL) {
        return 0;
    }
    dedup.update(&ino, &now);
    
    // Prepare event
    struct access_event event = {};
    event.inode = ino;
    event.timestamp_ns = now;
    event.pid = bpf_get_current_pid_tgid() >> 32;
    event.uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;
    
    // Get filename
    struct dentry *dentry = file->f_path.dentry;
    if (dentry) {
        bpf_probe_read_kernel_str(&event.path, sizeof(event.path), dentry->d_name.name);
    }
    
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

int trace_read(struct pt_regs *ctx, struct kiocb *iocb) {
    return track_access(ctx, iocb);
}

int trace_write(struct pt_regs *ctx, struct kiocb *iocb) {
    return track_access(ctx, iocb);
}
"""
        
        try:
            self.bpf = BPF(text=bpf_code)
            self.bpf.attach_kprobe(event="ceph_read_iter", fn_name="trace_read")
            self.bpf.attach_kprobe(event="ceph_write_iter", fn_name="trace_write")
            self.bpf["events"].open_perf_buffer(self.handle_event)
            print("✓ eBPF program loaded and attached to CephFS")
        except Exception as e:
            print(f"✗ Failed to load eBPF: {e}")
            print("\nNote: Requires kernel with CephFS and BTF support")
            sys.exit(1)
    
    def handle_event(self, cpu, data, size):
        """Handle eBPF event - write to in-memory cache (HOT PATH)"""
        import ctypes
        
        class AccessEvent(ctypes.Structure):
            _fields_ = [
                ("inode", ctypes.c_uint64),
                ("timestamp_ns", ctypes.c_uint64),
                ("pid", ctypes.c_uint32),
                ("uid", ctypes.c_uint32),
                ("path", ctypes.c_char * 256)
            ]
        
        event = self.bpf["events"].event(data)
        self.event_count += 1
        
        inode = event.inode
        timestamp_ns = event.timestamp_ns
        path = event.path.decode('utf-8', errors='ignore')
        
        # Write to in-memory cache (HOT PATH - microseconds)
        with self.cache_lock:
            self.cache[inode] = {
                'timestamp_ns': timestamp_ns,
                'path': path
            }
            self.cache_writes += 1
        
        if self.event_count % 100 == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Events: {self.event_count}, "
                  f"Cache: {self.cache_writes}, PostgreSQL: {self.db_writes} | Latest: {path}")
    
    def postgres_flusher(self):
        """Background thread: Flush cache → PostgreSQL every 60 seconds"""
        print(f"✓ PostgreSQL flusher thread started (interval: {FLUSH_INTERVAL}s)")
        
        while self.running:
            time.sleep(FLUSH_INTERVAL)
            if not self.running:
                break
            
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Flushing cache → PostgreSQL...")
            self.flush_to_postgres()
    
    def flush_to_postgres(self):
        """Flush all cache entries to PostgreSQL"""
        start_time = time.time()
        
        # Copy cache and clear (minimize lock time)
        with self.cache_lock:
            cache_snapshot = self.cache.copy()
            cache_size = len(cache_snapshot)
        
        if cache_size == 0:
            print("  (No data to flush)")
            return
        
        batch_count = 0
        try:
            with self.pg_conn.cursor() as cur:
                # Batch upsert
                for inode, data in cache_snapshot.items():
                    timestamp = datetime.fromtimestamp(data['timestamp_ns'] / 1e9)
                    path = data['path']
                    
                    cur.execute("""
                        INSERT INTO file_metadata (inode, path, last_access, current_pool)
                        VALUES (%s, %s, %s, 'cephfs.tiercephfs.data')
                        ON CONFLICT (inode) DO UPDATE 
                        SET last_access = EXCLUDED.last_access,
                            path = EXCLUDED.path
                    """, (inode, path, timestamp))
                    
                    batch_count += 1
                    self.db_writes += 1
            
            duration = time.time() - start_time
            print(f"✓ Flushed {batch_count} entries to PostgreSQL in {duration:.2f}s")
            
        except Exception as e:
            print(f"✗ Flush error: {e}")
            self.setup_postgres()  # Reconnect
    
    def shutdown(self, signum, frame):
        """Graceful shutdown"""
        print(f"\n\nShutting down... (signal {signum})")
        self.running = False
        
        # Final flush
        print("Final flush to PostgreSQL...")
        self.flush_to_postgres()
        
        # Cleanup
        if hasattr(self, 'pg_conn'):
            self.pg_conn.close()
        
        print(f"\nFinal Statistics:")
        print(f"  eBPF events:      {self.event_count}")
        print(f"  Cache writes:     {self.cache_writes}")
        print(f"  PostgreSQL syncs: {self.db_writes}")
        print(f"  Cache size:       {len(self.cache)} entries")
        print("\n✓ Shutdown complete")
        sys.exit(0)
    
    def run(self):
        """Main event loop"""
        print("\n" + "="*60)
        print("  CephFS Tiering Tracker - Phase 1")
        print("  eBPF → In-Memory Cache → PostgreSQL")
        print("="*60)
        print(f"\nCache:      In-memory (hot path)")
        print(f"PostgreSQL: {DB_CONFIG['database']}@{DB_CONFIG['host']}")
        print(f"Flush:      Every {FLUSH_INTERVAL} seconds")
        print("\nPress Ctrl+C to stop\n")
        
        try:
            while self.running:
                self.bpf.perf_buffer_poll(timeout=1000)
        except KeyboardInterrupt:
            self.shutdown(signal.SIGINT, None)

if __name__ == '__main__':
    # Check if running as root
    if os.geteuid() != 0:
        print("This program must be run as root (for eBPF)")
        sys.exit(1)
    
    tracker = TieringTracker()
    tracker.run()

class TieringTracker:
    def __init__(self):
        self.running = True
        self.event_count = 0
        self.db_writes = 0
        
        # Setup PostgreSQL
        self.setup_postgres()
        
        # Load eBPF program
        self.setup_ebpf()
        
        # Signal handlers
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
    
    def setup_postgres(self):
        """Initialize PostgreSQL connection"""
        try:
            self.pg_conn = psycopg2.connect(**DB_CONFIG)
            self.pg_conn.autocommit = True
            print(f"✓ PostgreSQL connected: {DB_CONFIG['database']}")
        except Exception as e:
            print(f"✗ PostgreSQL connection failed: {e}")
            sys.exit(1)
    
    def setup_ebpf(self):
        """Load eBPF program"""
        bpf_file = "/home/cephvm/tiering_system/src/cephfs_tracker.bpf.c"
        
        if not os.path.exists(bpf_file):
            print(f"✗ eBPF source file not found: {bpf_file}")
            sys.exit(1)
        
        print(f"Loading eBPF program from {bpf_file}...")
        
        # Read and modify BPF code for BCC
        with open(bpf_file, 'r') as f:
            bpf_code = f.read()
        
        # Remove vmlinux.h and BTF-specific includes (BCC uses different headers)
        bpf_code = bpf_code.replace('#include <vmlinux.h>', '')
        bpf_code = bpf_code.replace('#include <bpf/bpf_helpers.h>', '')
        bpf_code = bpf_code.replace('#include <bpf/bpf_tracing.h>', '')
        bpf_code = bpf_code.replace('#include <bpf/bpf_core_read.h>', '')
        bpf_code = bpf_code.replace('SEC("fentry/ceph_read_iter")', 'int trace_read(struct pt_regs *ctx, struct kiocb *iocb)')
        bpf_code = bpf_code.replace('SEC("fentry/ceph_write_iter")', 'int trace_write(struct pt_regs *ctx, struct kiocb *iocb)')
        bpf_code = bpf_code.replace('BPF_PROG(trace_read, struct kiocb *iocb)', '')
        bpf_code = bpf_code.replace('BPF_PROG(trace_write, struct kiocb *iocb)', '')
        bpf_code = bpf_code.replace('BPF_CORE_READ(', 'bpf_probe_read_kernel(&tmp, sizeof(tmp), &')
        bpf_code = bpf_code.replace('char LICENSE[]', '//char LICENSE[]')
        
        # Add BCC headers
        bpf_code = '#include <uapi/linux/ptrace.h>\n#include <linux/fs.h>\n' + bpf_code
        
        try:
            self.bpf = BPF(text=bpf_code)
            self.bpf.attach_kprobe(event="ceph_read_iter", fn_name="trace_read")
            self.bpf.attach_kprobe(event="ceph_write_iter", fn_name="trace_write")
            self.bpf["events"].open_ring_buffer(self.handle_event)
            print("✓ eBPF program loaded and attached")
        except Exception as e:
            print(f"✗ Failed to load eBPF: {e}")
            sys.exit(1)
    
    def handle_event(self, ctx, data, size):
        """Handle eBPF event - write to PostgreSQL"""
        import ctypes
        
        class AccessEvent(ctypes.Structure):
            _fields_ = [
                ("inode", ctypes.c_uint64),
                ("timestamp_ns", ctypes.c_uint64),
                ("pid", ctypes.c_uint32),
                ("uid", ctypes.c_uint32),
                ("path", ctypes.c_char * 256)
            ]
        
        event = ctypes.cast(data, ctypes.POINTER(AccessEvent)).contents
        self.event_count += 1
        
        inode = event.inode
        timestamp_ns = event.timestamp_ns
        path = event.path.decode('utf-8', errors='ignore')
        timestamp = datetime.fromtimestamp(timestamp_ns / 1e9)
        
        # Write to PostgreSQL
        try:
            with self.pg_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO file_metadata (inode, path, last_access, current_pool)
                    VALUES (%s, %s, %s, 'cephfs.tiercephfs.data')
                    ON CONFLICT (inode) DO UPDATE 
                    SET last_access = EXCLUDED.last_access,
                        path = EXCLUDED.path
                """, (inode, path, timestamp))
                self.db_writes += 1
        except Exception as e:
            print(f"Database error: {e}")
            self.setup_postgres()
        
        if self.event_count % 10 == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Events: {self.event_count}, DB writes: {self.db_writes}")
    
    def shutdown(self, signum, frame):
        """Graceful shutdown"""
        print(f"\n\nShutting down... (signal {signum})")
        self.running = False
        
        # Cleanup
        if hasattr(self, 'pg_conn'):
            self.pg_conn.close()
        
        print(f"\nFinal Statistics:")
        print(f"  eBPF events:      {self.event_count}")
        print(f"  PostgreSQL writes: {self.db_writes}")
        print("\n✓ Shutdown complete")
        sys.exit(0)
    
    def run(self):
        """Main event loop"""
        print("\n" + "="*60)
        print("  CephFS Tiering Tracker - Phase 1")
        print("  eBPF → PostgreSQL (Direct)")
        print("="*60)
        print(f"\nPostgreSQL: {DB_CONFIG['database']}@{DB_CONFIG['host']}")
        print("\nPress Ctrl+C to stop\n")
        
        try:
            while self.running:
                self.bpf.ring_buffer_poll(timeout=1000)
        except KeyboardInterrupt:
            self.shutdown(signal.SIGINT, None)

if __name__ == '__main__':
    # Check if running as root
    if os.geteuid() != 0:
        print("This program must be run as root (for eBPF)")
        sys.exit(1)
    
    tracker = TieringTracker()
    tracker.run()

class TieringTracker:
    def __init__(self):
        self.running = True
        self.event_count = 0
        self.rocks_writes = 0
        self.pg_writes = 0
        
        # Setup RocksDB
        self.setup_rocksdb()
        
        # Setup PostgreSQL
        self.setup_postgres()
        
        # Load eBPF program
        self.setup_ebpf()
        
        # Start PostgreSQL flusher thread
        self.flusher_thread = threading.Thread(target=self.postgres_flusher, daemon=True)
        self.flusher_thread.start()
        
        # Signal handlers
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
    
    def setup_rocksdb(self):
        """Initialize RocksDB"""
        os.makedirs(ROCKSDB_PATH, exist_ok=True)
        opts = rocksdb.Options()
        opts.create_if_missing = True
        opts.write_buffer_size = 64 * 1024 * 1024  # 64MB
        self.rocks_db = rocksdb.DB(ROCKSDB_PATH, opts)
        print(f"✓ RocksDB opened at {ROCKSDB_PATH}")
    
    def setup_postgres(self):
        """Initialize PostgreSQL connection"""
        try:
            self.pg_conn = psycopg2.connect(**DB_CONFIG)
            self.pg_conn.autocommit = True
            print(f"✓ PostgreSQL connected: {DB_CONFIG['database']}")
        except Exception as e:
            print(f"✗ PostgreSQL connection failed: {e}")
            sys.exit(1)
    
    def setup_ebpf(self):
        """Load eBPF program from compiled object file"""
        bpf_file = "/home/cephvm/tiering_system/ebpf/cephfs_tracker.bpf.o"
        
        if not os.path.exists(bpf_file):
            print(f"✗ eBPF object file not found: {bpf_file}")
            print("Please compile first:")
            print(f"  clang -g -O2 -target bpf -D__TARGET_ARCH_x86_64 \\")
            print(f"    -c cephfs_tracker.bpf.c -o cephfs_tracker.bpf.o")
            sys.exit(1)
        
        print(f"Loading eBPF program from {bpf_file}...")
        
        # Load with BCC
        try:
            self.bpf = BPF(src_file=bpf_file.replace('.o', '.c'))
            self.bpf["events"].open_ring_buffer(self.handle_event)
            print("✓ eBPF program loaded and attached")
        except Exception as e:
            print(f"✗ Failed to load eBPF: {e}")
            print("\nTrying alternative method...")
            # Try inline BPF code
            self.load_inline_bpf()
    
    def load_inline_bpf(self):
        """Fallback: Load eBPF code inline"""
        bpf_code = open("/home/cephvm/tiering_system/ebpf/cephfs_tracker.bpf.c").read()
        # Remove vmlinux.h and use BCC's includes
        bpf_code = bpf_code.replace('#include <vmlinux.h>', '')
        bpf_code = bpf_code.replace('#include <bpf/bpf_helpers.h>', '')
        bpf_code = bpf_code.replace('#include <bpf/bpf_tracing.h>', '')
        bpf_code = bpf_code.replace('#include <bpf/bpf_core_read.h>', '')
        bpf_code = '#include <uapi/linux/ptrace.h>\n' + bpf_code
        
        self.bpf = BPF(text=bpf_code)
        self.bpf["events"].open_ring_buffer(self.handle_event)
        print("✓ eBPF program loaded (inline mode)")
    
    def handle_event(self, ctx, data, size):
        """Handle eBPF event - write to RocksDB (hot path)"""
        import ctypes
        
        class AccessEvent(ctypes.Structure):
            _fields_ = [
                ("inode", ctypes.c_uint64),
                ("timestamp_ns", ctypes.c_uint64),
                ("pid", ctypes.c_uint32),
                ("uid", ctypes.c_uint32),
                ("path", ctypes.c_char * 256)
            ]
        
        event = ctypes.cast(data, ctypes.POINTER(AccessEvent)).contents
        self.event_count += 1
        
        inode = event.inode
        timestamp_ns = event.timestamp_ns
        path = event.path.decode('utf-8', errors='ignore')
        
        # Write to RocksDB (HOT PATH - sub-millisecond)
        key = str(inode).encode()
        value = f"{timestamp_ns}|{path}".encode()
        self.rocks_db.put(key, value)
        self.rocks_writes += 1
        
        if self.event_count % 100 == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Events: {self.event_count}, "
                  f"RocksDB: {self.rocks_writes}, PostgreSQL: {self.pg_writes}")
    
    def postgres_flusher(self):
        """Background thread: Flush RocksDB → PostgreSQL every 60 seconds"""
        print(f"✓ PostgreSQL flusher thread started (interval: {FLUSH_INTERVAL}s)")
        
        while self.running:
            time.sleep(FLUSH_INTERVAL)
            if not self.running:
                break
            
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Flushing RocksDB → PostgreSQL...")
            self.flush_to_postgres()
    
    def flush_to_postgres(self):
        """Flush all RocksDB entries to PostgreSQL"""
        start_time = time.time()
        batch_count = 0
        
        try:
            with self.pg_conn.cursor() as cur:
                # Iterate through RocksDB
                it = self.rocks_db.iteritems()
                it.seek_to_first()
                
                for key, value in it:
                    inode = int(key.decode())
                    parts = value.decode().split('|', 1)
                    timestamp_ns = int(parts[0])
                    path = parts[1] if len(parts) > 1 else ''
                    
                    # Convert nanoseconds to timestamp
                    timestamp = datetime.fromtimestamp(timestamp_ns / 1e9)
                    
                    # Upsert to PostgreSQL
                    cur.execute("""
                        INSERT INTO file_metadata (inode, path, last_access, current_pool)
                        VALUES (%s, %s, %s, 'cephfs.tiercephfs.data')
                        ON CONFLICT (inode) DO UPDATE 
                        SET last_access = EXCLUDED.last_access,
                            path = EXCLUDED.path
                    """, (inode, path, timestamp))
                    
                    batch_count += 1
                    self.pg_writes += 1
            
            duration = time.time() - start_time
            print(f"✓ Flushed {batch_count} entries to PostgreSQL in {duration:.2f}s")
            
        except Exception as e:
            print(f"✗ Flush error: {e}")
            self.setup_postgres()  # Reconnect
    
    def shutdown(self, signum, frame):
        """Graceful shutdown"""
        print(f"\n\nShutting down... (signal {signum})")
        self.running = False
        
        # Final flush
        print("Final flush to PostgreSQL...")
        self.flush_to_postgres()
        
        # Cleanup
        if hasattr(self, 'pg_conn'):
            self.pg_conn.close()
        
        print(f"\nFinal Statistics:")
        print(f"  eBPF events:      {self.event_count}")
        print(f"  RocksDB writes:   {self.rocks_writes}")
        print(f"  PostgreSQL syncs: {self.pg_writes}")
        print("\n✓ Shutdown complete")
        sys.exit(0)
    
    def run(self):
        """Main event loop"""
        print("\n" + "="*60)
        print("  CephFS Tiering Tracker - Phase 1")
        print("  eBPF → RocksDB → PostgreSQL")
        print("="*60)
        print(f"\nRocksDB:    {ROCKSDB_PATH}")
        print(f"PostgreSQL: {DB_CONFIG['database']}@{DB_CONFIG['host']}")
        print(f"Flush:      Every {FLUSH_INTERVAL} seconds")
        print("\nPress Ctrl+C to stop\n")
        
        try:
            while self.running:
                self.bpf.ring_buffer_poll(timeout=1000)
        except KeyboardInterrupt:
            self.shutdown(signal.SIGINT, None)

if __name__ == '__main__':
    # Check if running as root
    if os.geteuid() != 0:
        print("This program must be run as root (for eBPF)")
        sys.exit(1)
    
    tracker = TieringTracker()
    tracker.run()
