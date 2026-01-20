#!/usr/bin/env python3
"""
Phase 1: eBPF → PostgreSQL Hot Table → PostgreSQL Cold Table
Hot table (file_access_log): Fast append-only inserts
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
AGGREGATE_INTERVAL = 60  # Aggregate log → metadata every 60 seconds
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
        
        # Batch buffer for inserts
        self.event_buffer = []
        self.buffer_max_size = 1000  # Flush after 1000 events
        self.last_flush = time.time()
        self.buffer_lock = threading.Lock()
        
        # Setup PostgreSQL
        self.setup_postgres()
        
        # Load eBPF program
        self.setup_ebpf()
        
        # Start aggregator thread
        self.aggregator_thread = threading.Thread(target=self.aggregator, daemon=True)
        self.aggregator_thread.start()
        
        # Start flush thread
        self.flush_thread = threading.Thread(target=self.periodic_flush, daemon=True)
        self.flush_thread.start()
        
        # Signal handlers
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
    
    def setup_postgres(self):
        """Initialize PostgreSQL connection"""
        try:
            self.pg_conn = psycopg2.connect(**DB_CONFIG)
            self.pg_conn.autocommit = True
        except Exception as e:
            print(f"✗ PostgreSQL connection failed: {e}", flush=True)
            sys.exit(1)
    
    def setup_ebpf(self):
        """Load eBPF program using BCC"""
        # Simplified eBPF code - builds path using d_path helper
        bpf_code = """
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>
#include <linux/dcache.h>
#include <linux/path.h>

struct access_event {
    u64 inode;
    u64 timestamp_ns;
    u32 pid;
    u32 uid;
    char path[256];
};

struct delete_event {
    u64 inode;
    u32 pid;
    u32 uid;
    char path[256];
};

BPF_PERF_OUTPUT(events);
BPF_PERF_OUTPUT(deletions);

static int track_access(struct pt_regs *ctx, struct kiocb *iocb) {
    struct file *file = iocb->ki_filp;
    if (!file) return 0;
    
    struct inode *inode = file->f_inode;
    if (!inode) return 0;
    
    u64 ino = inode->i_ino;
    u64 now = bpf_ktime_get_ns();
    
    // Prepare event
    struct access_event event = {};
    event.inode = ino;
    event.timestamp_ns = now;
    event.pid = bpf_get_current_pid_tgid() >> 32;
    event.uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;
    
    // SKIP ROOT (UID 0)
    if (event.uid == 0) {
        return 0;
    }
    
    // Get path - use BCC's d_path wrapper
    struct qstr dname = file->f_path.dentry->d_name;
    bpf_probe_read_kernel_str(&event.path, sizeof(event.path), dname.name);
    
    // SKIP HIDDEN FILES
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

// Track file deletions via unlink/unlinkat syscalls
TRACEPOINT_PROBE(syscalls, sys_enter_unlink) {
    struct delete_event event = {};
    u32 uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;
    
    // Skip root (migration operations)
    if (uid == 0) return 0;
    
    event.uid = uid;
    event.pid = bpf_get_current_pid_tgid() >> 32;
    event.inode = 0;
    
    // Get filename from syscall argument
    bpf_probe_read_user_str(&event.path, sizeof(event.path), (void *)args->pathname);
    
    // Skip hidden files
    if (event.path[0] == '.') return 0;
    
    deletions.perf_submit(args, &event, sizeof(event));
    return 0;
}

TRACEPOINT_PROBE(syscalls, sys_enter_unlinkat) {
    struct delete_event event = {};
    u32 uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;
    
    // Skip root (migration operations)
    if (uid == 0) return 0;
    
    event.uid = uid;
    event.pid = bpf_get_current_pid_tgid() >> 32;
    event.inode = 0;
    
    // Get filename from syscall argument
    bpf_probe_read_user_str(&event.path, sizeof(event.path), (void *)args->pathname);
    
    // Skip hidden files
    if (event.path[0] == '.') return 0;
    
    deletions.perf_submit(args, &event, sizeof(event));
    return 0;
}
"""
        
        try:
            self.bpf = BPF(text=bpf_code, cflags=["-Wno-duplicate-decl-specifier"])
            self.bpf.attach_kprobe(event="ceph_read_iter", fn_name="trace_read")
            self.bpf.attach_kprobe(event="ceph_write_iter", fn_name="trace_write")
            self.bpf["events"].open_perf_buffer(self.handle_event)
            self.bpf["deletions"].open_perf_buffer(self.handle_deletion)
            print("✓ eBPF program loaded with deletion tracking", flush=True)
        except Exception as e:
            print(f"✗ Failed to load eBPF: {e}", flush=True)
            sys.exit(1)
    
    def handle_event(self, cpu, data, size):
        """Handle eBPF event - Buffer for batch insert"""
        event = self.bpf["events"].event(data)
        self.event_count += 1
        
        inode = event.inode
        uid = event.uid
        filename = event.path.decode('utf-8', errors='ignore')
        timestamp = datetime.now()
        
        # Build full path by querying CephFS for this inode
        full_path = self.get_full_path(inode)
        if not full_path:
            full_path = filename  # Fallback to filename only
        
        # Add to buffer instead of immediate insert
        with self.buffer_lock:
            self.event_buffer.append((uid, inode, full_path, timestamp))
            
            # Flush if buffer is full
            if len(self.event_buffer) >= self.buffer_max_size:
                self.flush_buffer()
    
    def handle_deletion(self, cpu, data, size):
        """Handle file deletion event - Remove from database immediately"""
        event = self.bpf["deletions"].event(data)
        
        filename = event.path.decode('utf-8', errors='ignore')
        
        # Delete from BOTH tables to handle race condition
        try:
            with self.pg_conn.cursor() as cur:
                # Delete from file_metadata (if already aggregated)
                cur.execute("""
                    DELETE FROM file_metadata 
                    WHERE path = %s
                    RETURNING path
                """, (filename,))
                
                deleted_metadata = cur.fetchone()
                
                # Also delete from file_access_log (if pending aggregation)
                cur.execute("""
                    DELETE FROM file_access_log 
                    WHERE path = %s
                    RETURNING path
                """, (filename,))
                
                deleted_log = cur.fetchone()
                
                if deleted_metadata or deleted_log:
                    print(f"✓ Removed deleted file: {filename} (metadata: {bool(deleted_metadata)}, log: {bool(deleted_log)})", flush=True)
            
            self.pg_conn.commit()
            
        except Exception as e:
            print(f"✗ Error removing deleted file {filename}: {e}", flush=True)
            self.setup_postgres()
    
    def flush_buffer(self):
        """Flush buffered events to database (batch insert)"""
        if not self.event_buffer:
            return
        
        try:
            from psycopg2.extras import execute_values
            
            with self.pg_conn.cursor() as cur:
                # Single INSERT with multiple rows
                execute_values(cur, """
                    INSERT INTO file_access_log (uid, inode, path, access_time)
                    VALUES %s
                """, self.event_buffer)
                
                self.log_writes += len(self.event_buffer)
            
            self.event_buffer = []
            self.last_flush = time.time()
            
        except Exception as e:
            print(f"Batch insert error: {e}", flush=True)
            self.event_buffer = []  # Clear buffer to prevent infinite retry
            self.setup_postgres()
    
    def periodic_flush(self):
        """Background thread: Flush buffer every 1 second"""
        while self.running:
            time.sleep(1.0)
            
            with self.buffer_lock:
                if self.event_buffer and (time.time() - self.last_flush) >= 1.0:
                    self.flush_buffer()
    
    def get_full_path(self, inode):
        """Query CephFS to get full path for an inode"""
        try:
            import subprocess
            result = subprocess.run(
                ['find', '/tiercephfs', '-inum', str(inode), '-print', '-quit'],
                capture_output=True, text=True, timeout=0.5
            )
            if result.returncode == 0 and result.stdout:
                abs_path = result.stdout.strip()
                # Remove /tiercephfs/ prefix to get relative path
                if abs_path.startswith('/tiercephfs/'):
                    return abs_path[12:]  # Remove "/tiercephfs/"
                return abs_path
        except:
            pass
        return None
    
    def aggregator(self):
        """Background thread: Aggregate hot table → cold table"""
        while self.running:
            time.sleep(AGGREGATE_INTERVAL)
            if not self.running:
                break
            
            self.aggregate_log()
    
    def aggregate_log(self):
        """Aggregate file_access_log → file_metadata"""
        start_time = time.time()
        
        try:
            with self.pg_conn.cursor() as cur:
                cur.execute("SELECT * FROM aggregate_access_log()")
                result = cur.fetchone()
                processed = result[0] if result else 0
                
                self.aggregations += 1
                print(f"Wrote {processed} files to file_metadata. Sleeping for {AGGREGATE_INTERVAL}s", flush=True)
                
        except Exception as e:
            print(f"✗ Aggregation error: {e}", flush=True)
            self.setup_postgres()
    
    def shutdown(self, signum, frame):
        """Graceful shutdown"""
        print(f"\n\nShutting down... (signal {signum})", flush=True)
        self.running = False
        
        print("Final aggregation...", flush=True)
        self.aggregate_log()
        
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
        print("Monitoring started", flush=True)
        
        try:
            while self.running:
                self.bpf.perf_buffer_poll(timeout=1000)
        except KeyboardInterrupt:
            self.shutdown(signal.SIGINT, None)

if __name__ == '__main__':
    if os.geteuid() != 0:
        print("This program must be run as root (for eBPF)", flush=True)
        sys.exit(1)
    
    tracker = TieringTracker()
    tracker.run()
