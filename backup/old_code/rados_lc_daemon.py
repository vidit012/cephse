#!/usr/bin/env python3
"""
RADOS Lifecycle Daemon
Object-level tiering with access time tracking

Similar to RGW LC but for raw RADOS objects
"""

import os
import sys
import time
import rados
import json
import logging
import argparse
import rocksdb
from datetime import datetime

class RadosLifecycleDaemon:
    def __init__(self, config_file="/etc/ceph/rados_lc.conf"):
        self.load_config(config_file)
        self.setup_logging()
        self.setup_ceph()
        self.setup_database()
        
        self.stats = {
            'scanned': 0,
            'demoted': 0,
            'promoted': 0,
            'errors': 0,
            'last_run': None
        }
    
    def load_config(self, config_file):
        """Load configuration"""
        try:
            with open(config_file) as f:
                config = json.load(f)
        except FileNotFoundError:
            config = {
                "ceph_conf": "/etc/ceph/ceph.conf",
                "hot_pool": "hot_pool",
                "cold_pool": "cold_pool",
                "metadata_db": "/var/lib/rados_lc/metadata.db",
                "scan_interval": 3600,
                "cold_age_days": 30,
                "hot_age_days": 15,
                "log_file": "/var/log/ceph/rados_lc.log",
                "log_level": "INFO"
            }
        
        self.ceph_conf = config.get('ceph_conf', '/etc/ceph/ceph.conf')
        self.hot_pool = config.get('hot_pool', 'hot_pool')
        self.cold_pool = config.get('cold_pool', 'cold_pool')
        self.metadata_db = config.get('metadata_db', '/var/lib/rados_lc/metadata.db')
        self.scan_interval = config.get('scan_interval', 3600)
        self.cold_age_days = config.get('cold_age_days', 30)
        self.hot_age_days = config.get('hot_age_days', 15)
        self.log_file = config.get('log_file', '/var/log/ceph/rados_lc.log')
        self.log_level = config.get('log_level', 'INFO')
        
        self.cold_threshold = self.cold_age_days * 86400
        self.hot_threshold = self.hot_age_days * 86400
    
    def setup_logging(self):
        """Setup logging"""
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        
        logging.basicConfig(
            level=getattr(logging, self.log_level),
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[
                logging.FileHandler(self.log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger('rados_lc')
    
    def setup_ceph(self):
        """Connect to Ceph cluster"""
        try:
            self.cluster = rados.Rados(conffile=self.ceph_conf)
            self.cluster.connect()
            self.logger.info(f"Connected to Ceph cluster: {self.cluster.get_fsid()}")
        except Exception as e:
            self.logger.error(f"Failed to connect to Ceph: {e}")
            sys.exit(1)
    
    def setup_database(self):
        """Setup RocksDB for metadata"""
        try:
            os.makedirs(os.path.dirname(self.metadata_db), exist_ok=True)
            opts = rocksdb.Options()
            opts.create_if_missing = True
            self.db = rocksdb.DB(self.metadata_db, opts)
            self.logger.info(f"Opened metadata database: {self.metadata_db}")
        except Exception as e:
            self.logger.error(f"Failed to open database: {e}")
            sys.exit(1)
    
    def record_access(self, pool, object_name):
        """Record object access (called by external monitor)"""
        key = f"{pool}:{object_name}".encode()
        value = str(int(time.time())).encode()
        self.db.put(key, value)
    
    def get_object_age(self, pool, object_name):
        """Get object age in seconds"""
        key = f"{pool}:{object_name}".encode()
        try:
            atime_bytes = self.db.get(key)
            if atime_bytes:
                atime = float(atime_bytes.decode())
                return time.time() - atime
        except:
            pass
        
        # If no access time, assume very old
        return float('inf')
    
    def migrate_object(self, src_pool, dst_pool, object_name):
        """
        Migrate object from source pool to destination pool
        This is the KEY function - actual data movement!
        """
        src_ioctx = None
        dst_ioctx = None
        
        try:
            # 1. Open pool contexts
            src_ioctx = self.cluster.open_ioctx(src_pool)
            dst_ioctx = self.cluster.open_ioctx(dst_pool)
            
            # 2. Get object size
            stat_info = src_ioctx.stat(object_name)
            object_size = stat_info[0]
            
            self.logger.info(f"Migrating {object_name} ({object_size} bytes): {src_pool} → {dst_pool}")
            
            # 3. Read object data from source
            # Read in chunks if large
            CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks
            
            if object_size <= CHUNK_SIZE:
                # Small object - read all at once
                data = src_ioctx.read(object_name, object_size)
                dst_ioctx.write_full(object_name, data)
            else:
                # Large object - stream in chunks
                offset = 0
                while offset < object_size:
                    chunk_size = min(CHUNK_SIZE, object_size - offset)
                    chunk = src_ioctx.read(object_name, chunk_size, offset)
                    
                    if offset == 0:
                        dst_ioctx.write_full(object_name, chunk)
                    else:
                        dst_ioctx.write(object_name, chunk, offset)
                    
                    offset += chunk_size
            
            # 4. Copy extended attributes (metadata)
            try:
                xattrs = src_ioctx.get_xattrs(object_name)
                for attr_name, attr_value in xattrs:
                    dst_ioctx.set_xattr(object_name, attr_name, attr_value)
            except Exception as e:
                self.logger.warning(f"Could not copy xattrs for {object_name}: {e}")
            
            # 5. Copy OMAP (object metadata key-value store)
            try:
                omap_iter = src_ioctx.get_omap_vals(object_name, "", "", -1)
                omap_dict = {k: v for k, v in omap_iter}
                if omap_dict:
                    dst_ioctx.set_omap(object_name, omap_dict)
            except Exception as e:
                self.logger.warning(f"Could not copy OMAP for {object_name}: {e}")
            
            # 6. Verify destination
            dst_stat = dst_ioctx.stat(object_name)
            if dst_stat[0] != object_size:
                raise Exception(f"Size mismatch after copy: {dst_stat[0]} != {object_size}")
            
            # 7. Delete from source
            src_ioctx.remove_object(object_name)
            
            # 8. Update metadata database
            old_key = f"{src_pool}:{object_name}".encode()
            new_key = f"{dst_pool}:{object_name}".encode()
            
            try:
                atime = self.db.get(old_key)
                self.db.put(new_key, atime)
                self.db.delete(old_key)
            except:
                # No existing record, create new one
                self.db.put(new_key, str(int(time.time())).encode())
            
            self.logger.info(f"✓ Successfully migrated {object_name}")
            return True
            
        except Exception as e:
            self.logger.error(f"✗ Failed to migrate {object_name}: {e}")
            return False
            
        finally:
            if src_ioctx:
                src_ioctx.close()
            if dst_ioctx:
                dst_ioctx.close()
    
    def scan_pool(self, pool_name, check_for_demotion=True):
        """
        Scan pool for objects that need tiering
        check_for_demotion: True = hot→cold, False = cold→hot
        """
        ioctx = None
        try:
            ioctx = self.cluster.open_ioctx(pool_name)
            now = time.time()
            
            for obj in ioctx.list_objects():
                object_name = obj.key
                self.stats['scanned'] += 1
                
                # Get object age
                age = self.get_object_age(pool_name, object_name)
                
                if check_for_demotion:
                    # Check if should move to cold (hot → cold)
                    if age > self.cold_threshold:
                        self.logger.info(f"Object {object_name} age: {age/86400:.1f} days (threshold: {self.cold_age_days} days)")
                        if self.migrate_object(pool_name, self.cold_pool, object_name):
                            self.stats['demoted'] += 1
                else:
                    # Check if should move to hot (cold → hot)
                    if age < self.hot_threshold:
                        self.logger.info(f"Object {object_name} recently accessed: {age/86400:.1f} days ago")
                        if self.migrate_object(pool_name, self.hot_pool, object_name):
                            self.stats['promoted'] += 1
        
        except Exception as e:
            self.logger.error(f"Error scanning pool {pool_name}: {e}")
            self.stats['errors'] += 1
        
        finally:
            if ioctx:
                ioctx.close()
    
    def run_cycle(self):
        """Run one lifecycle cycle"""
        self.logger.info("=" * 60)
        self.logger.info("Starting RADOS lifecycle cycle")
        self.logger.info(f"Hot pool: {self.hot_pool}")
        self.logger.info(f"Cold pool: {self.cold_pool}")
        self.logger.info(f"Demotion threshold: {self.cold_age_days} days")
        self.logger.info(f"Promotion threshold: {self.hot_age_days} days")
        
        # Reset stats
        self.stats = {
            'scanned': 0,
            'demoted': 0,
            'promoted': 0,
            'errors': 0,
            'last_run': datetime.now().isoformat()
        }
        
        start_time = time.time()
        
        # Scan hot pool for old objects (demotion)
        self.logger.info(f"Scanning {self.hot_pool} for demotion candidates...")
        self.scan_pool(self.hot_pool, check_for_demotion=True)
        
        # Scan cold pool for recently accessed objects (promotion)
        self.logger.info(f"Scanning {self.cold_pool} for promotion candidates...")
        self.scan_pool(self.cold_pool, check_for_demotion=False)
        
        elapsed = time.time() - start_time
        
        # Log summary
        self.logger.info(f"Cycle complete in {elapsed:.2f}s")
        self.logger.info(f"Objects scanned: {self.stats['scanned']}")
        self.logger.info(f"Objects demoted: {self.stats['demoted']}")
        self.logger.info(f"Objects promoted: {self.stats['promoted']}")
        self.logger.info(f"Errors: {self.stats['errors']}")
        self.logger.info("=" * 60)
    
    def run(self):
        """Main daemon loop"""
        self.logger.info("RADOS Lifecycle Daemon starting...")
        self.logger.info(f"Scan interval: {self.scan_interval} seconds")
        
        try:
            while True:
                self.run_cycle()
                self.logger.info(f"Sleeping for {self.scan_interval} seconds...")
                time.sleep(self.scan_interval)
        
        except KeyboardInterrupt:
            self.logger.info("Daemon stopped by user")
        except Exception as e:
            self.logger.error(f"Fatal error: {e}")
            sys.exit(1)
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Cleanup resources"""
        if hasattr(self, 'cluster'):
            self.cluster.shutdown()
        self.logger.info("Daemon shutdown complete")

def main():
    parser = argparse.ArgumentParser(description='RADOS Lifecycle Daemon')
    parser.add_argument('-c', '--config',
                       default='/etc/ceph/rados_lc.conf',
                       help='Configuration file path')
    parser.add_argument('--once', action='store_true',
                       help='Run once and exit (no daemon mode)')
    parser.add_argument('--record-access',
                       metavar=('POOL', 'OBJECT'),
                       nargs=2,
                       help='Record object access time')
    
    args = parser.parse_args()
    
    daemon = RadosLifecycleDaemon(args.config)
    
    if args.record_access:
        # Record access mode
        pool, obj = args.record_access
        daemon.record_access(pool, obj)
        print(f"Recorded access: {pool}:{obj}")
    elif args.once:
        # Run once
        daemon.run_cycle()
    else:
        # Daemon mode
        daemon.run()

if __name__ == "__main__":
    main()
