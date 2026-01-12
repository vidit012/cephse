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
            print(f"✓ PostgreSQL connected: {DB_CONFIG['database']}", flush=True)

        except Exception as e:
            print(f"✗ PostgreSQL connection failed: {e}", flush=True)

            sys.exit(1)
    
    def setup_ebpf(self):
        """Load eBPF program using BCC"""
        print("Loading eBPF program...", flush=True)

        
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
    
    // SKIP ROOT (UID 0) - Avoid tracking migration operations
    if (event.uid == 0) {
        return 0;
    }
    
    // Get filename
    struct dentry *dentry = file->f_path.dentry;
    if (dentry) {
        bpf_probe_read_kernel_str(&event.path, sizeof(event.path), dentry->d_name.name);
    }
    
    // SKIP HIDDEN FILES (starting with .) - Ignore .swp, .tmp, etc.
    if (event.path[0] == '.') {
        return 0;
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
            self.bpf = BPF(text=bpf_code, cflags=["-Wno-duplicate-decl-specifier"])
            self.bpf.attach_kprobe(event="ceph_read_iter", fn_name="trace_read")
            self.bpf.attach_kprobe(event="ceph_write_iter", fn_name="trace_write")
            self.bpf["events"].open_perf_buffer(self.handle_event)
            print("✓ eBPF program loaded and attached to CephFS", flush=True)

        except Exception as e:
            print(f"✗ Failed to load eBPF: {e}", flush=True)

            print("\nNote: Requires kernel with CephFS and BTF support", flush=True)

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
            print(f"Database error: {e}", flush=True)

            self.setup_postgres()
        
        if self.event_count % 100 == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Events: {self.event_count}, "
                  f"Log writes: {self.log_writes}, Aggregations: {self.aggregations} | {path}")
    
    def aggregator(self):
        """Background thread: Aggregate hot table → cold table every 60 seconds"""
        print(f"✓ Aggregator thread started (interval: {AGGREGATE_INTERVAL}s)", flush=True)

        
        while self.running:
            time.sleep(AGGREGATE_INTERVAL)
            if not self.running:
                break
            
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Aggregating log → metadata...", flush=True)

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
                print(f"✓ Aggregated {processed} log entries in {duration:.2f}s", flush=True)

                
        except Exception as e:
            print(f"✗ Aggregation error: {e}", flush=True)

            self.setup_postgres()
    
    def shutdown(self, signum, frame):
        """Graceful shutdown"""
        print(f"\n\nShutting down... (signal {signum})", flush=True)

        self.running = False
        
        # Final aggregation
        print("Final aggregation...", flush=True)

        self.aggregate_log()
        
        # Cleanup
        if hasattr(self, 'pg_conn'):
            self.pg_conn.close()
        
        print(f"\nFinal Statistics:", flush=True)

        print(f"  eBPF events:      {self.event_count}", flush=True)

        print(f"  Log writes:       {self.log_writes}", flush=True)

        print(f"  Aggregations:     {self.aggregations}", flush=True)

        print("\n✓ Shutdown complete", flush=True)

        sys.exit(0)
    
    def run(self):
        """Main event loop"""
        print("\n" + "="*60, flush=True)

        print("  CephFS Tiering Tracker - Phase 1", flush=True)

        print("  eBPF → Hot Table (log) → Cold Table (metadata)", flush=True)

        print("="*60, flush=True)

        print(f"\nHot Table:  file_access_log (append-only)", flush=True)

        print(f"Cold Table: file_metadata (aggregated)", flush=True)

        print(f"PostgreSQL: {DB_CONFIG['database']}@{DB_CONFIG['host']}", flush=True)

        print(f"Aggregate:  Every {AGGREGATE_INTERVAL} seconds", flush=True)

        print("\nPress Ctrl+C to stop\n", flush=True)

        
        try:
            while self.running:
                self.bpf.perf_buffer_poll(timeout=1000)
        except KeyboardInterrupt:
            self.shutdown(signal.SIGINT, None)

if __name__ == '__main__':
    # Check if running as root
    if os.geteuid() != 0:
        print("This program must be run as root (for eBPF)", flush=True)

        sys.exit(1)
    
    tracker = TieringTracker()
