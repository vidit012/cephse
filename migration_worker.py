#!/usr/bin/env python3
"""
CephFS Tiering System - Migration Worker
Executes file migrations using libcephfs
"""

import psycopg2
import subprocess
import time
import logging
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format='- %(message)s'
)
logger = logging.getLogger('migration_worker')

class MigrationWorker:
    def __init__(self, db_config, libcephfs_bin, cephfs_mount, num_workers=10):
        self.db_config = db_config
        self.libcephfs_bin = libcephfs_bin
        self.cephfs_mount = cephfs_mount
        self.num_workers = num_workers
        self.total_migrated = 0
        self.total_failed = 0
        
    def get_connection(self):
        """Get a new database connection (one per thread)"""
        return psycopg2.connect(**self.db_config)
    
    def get_candidates(self, conn, batch_size=100):
        """Get files that need migration"""
        cursor = conn.cursor()



        try:
            cursor.execute("""
                SELECT inode, path, current_pool, target_pool, last_access
                FROM file_metadata
                WHERE needs_migration = TRUE
                ORDER BY last_access ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            """, (batch_size,))
            
            candidates = cursor.fetchall()
            conn.commit()
            
            return [
                {
                    'inode': row[0],
                    'path': row[1],
                    'current_pool': row[2],
                    'target_pool': row[3],
                    'last_access': row[4]
                }
                for row in candidates
            ]
            
        except Exception as e:
            logger.error(f"Error getting candidates: {e}")
            conn.rollback()
            return []
        finally:
            cursor.close()
    
    def migrate_file(self, file_info):
        """Migrate a single file using libcephfs"""
        old_inode = file_info['inode']
        path = file_info['path']
        target_pool = file_info['target_pool']
        last_access = file_info['last_access']
        
        # Ensure path starts with / for CephFS root
        if not path.startswith('/'):
            cephfs_path = f"/{path}"
        else:
            cephfs_path = path
        
        # Full filesystem path for stat
        full_path = os.path.join(self.cephfs_mount, path.lstrip('/'))
        
        logger.info(f"Migrating inode {old_inode}: {path} -> {target_pool}")
        
        start_time = time.time()
        
        try:
            # Call libcephfs_migrate binary
            result = subprocess.run(
                [self.libcephfs_bin, cephfs_path, target_pool],
                capture_output=True,
                timeout=300,  # 5 minute timeout
                text=True
            )
            
            duration_ms = int((time.time() - start_time) * 1000)
            
            if result.returncode == 0:
                # CRITICAL: Get NEW inode after migration
                # The shadow file technique creates a new inode when renaming
                try:
                    stat_info = os.stat(full_path)
                    new_inode = stat_info.st_ino
                    
                    if new_inode != old_inode:
                        logger.info(f"✓ Migrated {path} in {duration_ms}ms (inode: {old_inode} → {new_inode})")
                    else:
                        logger.info(f"✓ Migrated {path} in {duration_ms}ms (inode unchanged: {new_inode})")
                    
                    return {
                        'success': True,
                        'old_inode': old_inode,
                        'new_inode': new_inode,
                        'path': path,
                        'target_pool': target_pool,
                        'last_access': last_access,
                        'duration_ms': duration_ms
                    }
                except FileNotFoundError:
                    logger.error(f"✗ File disappeared after migration: {path}")
                    return {'success': False, 'old_inode': old_inode, 'error': 'File not found after migration'}
                except Exception as e:
                    logger.error(f"✗ Error checking new inode for {path}: {e}")
                    return {'success': False, 'old_inode': old_inode, 'error': f'Stat failed: {e}'}
            else:
                error = result.stderr.strip() or "Unknown error"
                logger.error(f"✗ Failed {path}: {error}")
                return {'success': False, 'old_inode': old_inode, 'error': error}
                
        except subprocess.TimeoutExpired:
            logger.error(f"✗ Timeout migrating {path}")
            return {'success': False, 'old_inode': old_inode, 'error': 'Timeout after 5 minutes'}
        except Exception as e:
            logger.error(f"✗ Exception migrating {path}: {e}")
            return {'success': False, 'old_inode': old_inode, 'error': str(e)}
    
    def record_result(self, conn, result):
        """Record migration result in database"""
        cursor = conn.cursor()
        
        try:
            if result['success']:
                old_inode = result['old_inode']
                new_inode = result['new_inode']
                path = result['path']
                target_pool = result['target_pool']
                last_access = result['last_access']
                
                if new_inode != old_inode:
                    # Inode changed - need to update database entries
                    # 1. Delete old inode entry
                    cursor.execute("""
                        DELETE FROM file_metadata
                        WHERE inode = %s
                    """, (old_inode,))
                    
                    # 2. Insert or update new inode entry
                    # CRITICAL: Preserve original last_access time!
                    # Migration should NOT change access time - only user access should
                    cursor.execute("""
                        INSERT INTO file_metadata 
                            (inode, path, current_pool, target_pool, needs_migration, last_access)
                        VALUES 
                            (%s, %s, %s, NULL, FALSE, %s)
                        ON CONFLICT (inode) DO UPDATE
                        SET path = EXCLUDED.path,
                            current_pool = EXCLUDED.current_pool,
                            target_pool = NULL,
                            needs_migration = FALSE,
                            last_access = EXCLUDED.last_access
                    """, (new_inode, path, target_pool, last_access))
                    
                    logger.debug(f"Database updated: inode {old_inode} → {new_inode} (preserved last_access)")
                else:
                    # Inode unchanged - simple update
                    # Don't touch last_access!
                    cursor.execute("""
                        UPDATE file_metadata
                        SET current_pool = %s,
                            target_pool = NULL,
                            needs_migration = FALSE
                        WHERE inode = %s
                    """, (target_pool, new_inode))
                
                self.total_migrated += 1
            else:
                # Keep needs_migration=TRUE so it can be retried
                # Log the error but don't block
                logger.warning(f"Migration failed for inode {result['old_inode']}: {result['error']}")
                self.total_failed += 1
            
            conn.commit()
            
        except Exception as e:
            logger.error(f"Error recording result: {e}")
            conn.rollback()
        finally:
            cursor.close()
    
    def run_once(self):
        """Run one migration batch"""
        conn = self.get_connection()
        
        try:
            # Get candidates
            candidates = self.get_candidates(conn, batch_size=self.num_workers * 10)
            
            if not candidates:
                logger.info("No files need migration")
                return 0
            
            logger.info(f"Processing {len(candidates)} files with {self.num_workers} workers")
            
            # Migrate in parallel
            with ThreadPoolExecutor(max_workers=self.num_workers) as executor:
                futures = {
                    executor.submit(self.migrate_file, candidate): candidate
                    for candidate in candidates
                }
                
                for future in as_completed(futures):
                    result = future.result()
                    
                    # Each thread gets its own connection to record results
                    result_conn = self.get_connection()
                    try:
                        self.record_result(result_conn, result)
                    finally:
                        result_conn.close()
            
            logger.info(f"Batch complete: {self.total_migrated} success, {self.total_failed} failed")
            return len(candidates)
            
        finally:
            conn.close()
    
    def run_forever(self, interval_seconds=60):
        """Run migration worker continuously"""
        logger.info(f"Starting migration worker ({self.num_workers} parallel threads)")
        
        try:
            while True:
                try:
                    processed = self.run_once()
                    
                    if processed == 0:
                        # No work, sleep longer
                        time.sleep(interval_seconds)
                    else:
                        # More work might be available, minimal sleep
                        time.sleep(5)
                        
                except Exception as e:
                    logger.error(f"Error in migration cycle: {e}")
                    time.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            logger.info(f"Total: {self.total_migrated} migrated, {self.total_failed} failed")

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='CephFS Tiering Migration Worker')
    parser.add_argument('--host', default='localhost', help='PostgreSQL host')
    parser.add_argument('--port', type=int, default=5432, help='PostgreSQL port')
    parser.add_argument('--database', default='tiering', help='Database name')
    parser.add_argument('--user', default='tiering_user', help='Database user')
    parser.add_argument('--password', default='1', help='Database password')
    parser.add_argument('--libcephfs-bin', default='/home/cephvm/tiering_system/libcephfs_migrate',
                       help='Path to libcephfs_migrate binary')
    parser.add_argument('--cephfs-mount', default='/tiercephfs',
                       help='CephFS mount point for stat operations')
    parser.add_argument('--workers', type=int, default=10,
                       help='Number of parallel migration threads')
    parser.add_argument('--interval', type=int, default=60,
                       help='Polling interval when no work (seconds)')
    parser.add_argument('--once', action='store_true',
                       help='Run once and exit')
    
    args = parser.parse_args()
    
    db_config = {
        'host': args.host,
        'port': args.port,
        'database': args.database,
        'user': args.user,
        'password': args.password
    }
    
    worker = MigrationWorker(db_config, args.libcephfs_bin, args.cephfs_mount, args.workers)
    
    if args.once:
        worker.run_once()
    else:
        worker.run_forever(args.interval)

if __name__ == '__main__':
    main()
