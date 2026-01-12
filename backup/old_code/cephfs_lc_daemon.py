#!/usr/bin/env python3
"""
CephFS Lifecycle Daemon (cephfs_lc)
Similar to RGW's LC daemon but for CephFS file systems

Features:
- Scans file system for old files
- Moves files between pools based on age
- Runs periodically (configurable interval)
- Systemd integration
"""

import os
import sys
import time
import json
import shutil
import logging
import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

class CephFSLifecycleDaemon:
    def __init__(self, config_file="/etc/ceph/cephfs_lc.conf"):
        self.load_config(config_file)
        self.setup_logging()
        self.stats = {
            'scanned': 0,
            'demoted': 0,
            'promoted': 0,
            'errors': 0,
            'last_run': None
        }
    
    def load_config(self, config_file):
        """Load configuration from file"""
        try:
            with open(config_file) as f:
                config = json.load(f)
        except FileNotFoundError:
            # Default configuration
            config = {
                "mount_point": "/cephfs",
                "scan_interval": 3600,  # 1 hour
                "cold_age_days": 30,
                "hot_age_days": 15,
                "hot_pool": "cephfs.tiering.data",
                "cold_pool": "cephfs.tiering.cold",
                "cold_dir": "/cephfs/.tiers/cold",
                "exclude_paths": [
                    "/cephfs/.tiers",
                    "/cephfs/.snapshot"
                ],
                "enable_promotion": True,
                "enable_demotion": True,
                "log_level": "INFO",
                "log_file": "/var/log/ceph/cephfs_lc.log"
            }
        
        self.mount_point = config.get('mount_point', '/cephfs')
        self.scan_interval = config.get('scan_interval', 3600)
        self.cold_age_days = config.get('cold_age_days', 30)
        self.hot_age_days = config.get('hot_age_days', 15)
        self.hot_pool = config.get('hot_pool', 'cephfs.tiering.data')
        self.cold_pool = config.get('cold_pool', 'cephfs.tiering.cold')
        self.cold_dir = config.get('cold_dir', '/cephfs/.tiers/cold')
        self.exclude_paths = config.get('exclude_paths', [])
        self.enable_promotion = config.get('enable_promotion', True)
        self.enable_demotion = config.get('enable_demotion', True)
        self.log_level = config.get('log_level', 'INFO')
        self.log_file = config.get('log_file', '/var/log/ceph/cephfs_lc.log')
        
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
        self.logger = logging.getLogger('cephfs_lc')
    
    def get_file_pool(self, filepath):
        """Get the pool where file is stored"""
        try:
            result = subprocess.run(
                ['getfattr', '-n', 'ceph.file.layout.pool', '--only-values', filepath],
                capture_output=True, text=True, check=True
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None
    
    def should_exclude(self, filepath):
        """Check if path should be excluded from processing"""
        for exclude_path in self.exclude_paths:
            if filepath.startswith(exclude_path):
                return True
        return False
    
    def demote_file(self, filepath):
        """Move file from HOT to COLD pool"""
        try:
            # Skip if already demoted (is symlink)
            if os.path.islink(filepath):
                return False
            
            # Create cold path
            relative_path = os.path.relpath(filepath, self.mount_point)
            cold_path = os.path.join(self.cold_dir, relative_path)
            os.makedirs(os.path.dirname(cold_path), exist_ok=True)
            
            # Copy to cold storage
            shutil.copy2(filepath, cold_path)
            
            # Verify it's in cold pool
            if self.get_file_pool(cold_path) != self.cold_pool:
                self.logger.error(f"Failed to move {filepath} to cold pool")
                os.remove(cold_path)
                return False
            
            # Replace with symlink
            os.remove(filepath)
            os.symlink(cold_path, filepath)
            
            self.logger.info(f"Demoted: {filepath} -> COLD")
            self.stats['demoted'] += 1
            return True
            
        except Exception as e:
            self.logger.error(f"Error demoting {filepath}: {e}")
            self.stats['errors'] += 1
            return False
    
    def promote_file(self, filepath):
        """Move file from COLD to HOT pool"""
        try:
            # Check if it's a symlink to cold storage
            if not os.path.islink(filepath):
                return False
            
            cold_path = os.readlink(filepath)
            if not cold_path.startswith(self.cold_dir):
                return False
            
            # Copy back to hot
            os.remove(filepath)  # Remove symlink
            shutil.copy2(cold_path, filepath)
            
            # Verify it's in hot pool
            if self.get_file_pool(filepath) != self.hot_pool:
                self.logger.error(f"Failed to move {filepath} to hot pool")
                os.symlink(cold_path, filepath)  # Restore symlink
                return False
            
            # Delete cold copy
            os.remove(cold_path)
            
            self.logger.info(f"Promoted: {filepath} -> HOT")
            self.stats['promoted'] += 1
            return True
            
        except Exception as e:
            self.logger.error(f"Error promoting {filepath}: {e}")
            self.stats['errors'] += 1
            return False
    
    def scan_directory(self, directory):
        """Recursively scan directory for files to tier"""
        now = time.time()
        
        try:
            for entry in os.scandir(directory):
                full_path = entry.path
                
                # Skip excluded paths
                if self.should_exclude(full_path):
                    continue
                
                if entry.is_dir(follow_symlinks=False):
                    # Recurse into subdirectory
                    self.scan_directory(full_path)
                elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
                    self.stats['scanned'] += 1
                    self.process_file(full_path, now)
                    
        except PermissionError:
            self.logger.warning(f"Permission denied: {directory}")
        except Exception as e:
            self.logger.error(f"Error scanning {directory}: {e}")
    
    def process_file(self, filepath, now):
        """Process a single file for tiering"""
        try:
            # Get file stats
            stat_info = os.lstat(filepath)
            
            # Use atime (access time) for tiering decision
            age_seconds = now - stat_info.st_atime
            
            # Demotion: File not accessed in cold_age_days
            if self.enable_demotion and age_seconds > self.cold_threshold:
                if not os.path.islink(filepath):  # Not already in cold
                    self.demote_file(filepath)
            
            # Promotion: File accessed recently
            elif self.enable_promotion and age_seconds < self.hot_threshold:
                if os.path.islink(filepath):  # Currently in cold
                    self.promote_file(filepath)
                    
        except Exception as e:
            self.logger.error(f"Error processing {filepath}: {e}")
            self.stats['errors'] += 1
    
    def setup_cold_directory(self):
        """Initialize cold storage directory"""
        try:
            os.makedirs(self.cold_dir, exist_ok=True)
            
            # Set directory layout to use cold pool
            subprocess.run([
                'setfattr', '-n', 'ceph.dir.layout.pool',
                '-v', self.cold_pool, self.cold_dir
            ], check=True)
            
            self.logger.info(f"Cold directory configured: {self.cold_dir}")
            
        except Exception as e:
            self.logger.error(f"Failed to setup cold directory: {e}")
            sys.exit(1)
    
    def run_cycle(self):
        """Run one lifecycle cycle"""
        self.logger.info("=" * 60)
        self.logger.info(f"Starting lifecycle cycle")
        self.logger.info(f"Mount point: {self.mount_point}")
        self.logger.info(f"Cold threshold: {self.cold_age_days} days")
        self.logger.info(f"Hot threshold: {self.hot_age_days} days")
        
        # Reset stats
        self.stats = {
            'scanned': 0,
            'demoted': 0,
            'promoted': 0,
            'errors': 0,
            'last_run': datetime.now().isoformat()
        }
        
        start_time = time.time()
        
        # Scan file system
        self.scan_directory(self.mount_point)
        
        elapsed = time.time() - start_time
        
        # Log summary
        self.logger.info(f"Cycle complete in {elapsed:.2f}s")
        self.logger.info(f"Files scanned: {self.stats['scanned']}")
        self.logger.info(f"Files demoted: {self.stats['demoted']}")
        self.logger.info(f"Files promoted: {self.stats['promoted']}")
        self.logger.info(f"Errors: {self.stats['errors']}")
        self.logger.info("=" * 60)
    
    def run(self):
        """Main daemon loop"""
        self.logger.info("CephFS Lifecycle Daemon starting...")
        self.logger.info(f"Configuration loaded")
        
        # Setup cold directory
        self.setup_cold_directory()
        
        self.logger.info(f"Scan interval: {self.scan_interval} seconds")
        self.logger.info("Daemon running. Press Ctrl+C to stop.")
        
        try:
            while True:
                self.run_cycle()
                
                # Sleep until next cycle
                self.logger.info(f"Sleeping for {self.scan_interval} seconds...")
                time.sleep(self.scan_interval)
                
        except KeyboardInterrupt:
            self.logger.info("Daemon stopped by user")
        except Exception as e:
            self.logger.error(f"Fatal error: {e}")
            sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description='CephFS Lifecycle Daemon')
    parser.add_argument('-c', '--config', 
                       default='/etc/ceph/cephfs_lc.conf',
                       help='Configuration file path')
    parser.add_argument('--once', action='store_true',
                       help='Run once and exit (no daemon mode)')
    
    args = parser.parse_args()
    
    daemon = CephFSLifecycleDaemon(args.config)
    
    if args.once:
        daemon.setup_cold_directory()
        daemon.run_cycle()
    else:
        daemon.run()

if __name__ == "__main__":
    main()
