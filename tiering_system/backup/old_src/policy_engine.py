#!/usr/bin/env python3
"""
CephFS Tiering System - Policy Engine
Applies tiering policies and marks files for migration
"""

import psycopg2
import time
import logging
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('policy_engine')

class PolicyEngine:
    def __init__(self, db_config):
        self.db_config = db_config
        self.conn = None
        
    def connect(self):
        self.conn = psycopg2.connect(**self.db_config)
        logger.info("Connected to PostgreSQL")
    
    def apply_policies(self):
        """Apply all enabled tiering policies"""
        cursor = self.conn.cursor()
        
        try:
            # Call PostgreSQL function to apply policies
            cursor.execute("SELECT * FROM apply_tiering_policies()")
            results = cursor.fetchall()
            
            self.conn.commit()
            
            logger.info(f"Applied policies, marked {len(results)} files for migration")
            
            for inode, target_pool, policy_name in results[:10]:  # Log first 10
                logger.debug(f"  File {inode} -> {target_pool} (policy: {policy_name})")
                
            return len(results)
            
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error applying policies: {e}")
            return 0
        finally:
            cursor.close()
    
    def get_statistics(self):
        """Get current tiering statistics"""
        cursor = self.conn.cursor()
        
        try:
            cursor.execute("""
                SELECT 
                    current_pool,
                    COUNT(*) as file_count,
                    pg_size_pretty(SUM(size_bytes)::bigint) as total_size,
                    ROUND(AVG(EXTRACT(EPOCH FROM (NOW() - last_access)) / 86400), 1) as avg_age_days
                FROM file_metadata
                GROUP BY current_pool
                ORDER BY current_pool
            """)
            
            stats = cursor.fetchall()
            
            logger.info("=== Current Pool Statistics ===")
            for pool, count, size, age in stats:
                logger.info(f"  {pool}: {count} files, {size}, avg age {age} days")
            
            # Files needing migration
            cursor.execute("""
                SELECT COUNT(*) FROM file_metadata WHERE needs_migration = TRUE
            """)
            pending = cursor.fetchone()[0]
            logger.info(f"  Files pending migration: {pending}")
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting statistics: {e}")
            return []
        finally:
            cursor.close()
    
    def refresh_statistics(self):
        """Refresh materialized view statistics"""
        cursor = self.conn.cursor()
        
        try:
            cursor.execute("SELECT refresh_pool_statistics()")
            self.conn.commit()
            logger.info("Statistics refreshed")
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error refreshing statistics: {e}")
        finally:
            cursor.close()
    
    def run_once(self):
        """Run one policy application cycle"""
        logger.info("=== Policy Engine Cycle ===")
        
        # Get current stats
        self.get_statistics()
        
        # Apply policies
        marked = self.apply_policies()
        
        # Refresh stats if changes were made
        if marked > 0:
            self.refresh_statistics()
        
        logger.info(f"Cycle complete, marked {marked} files")
        
    def run_forever(self, interval_seconds=300):
        """Run policy engine continuously"""
        logger.info(f"Starting policy engine (interval: {interval_seconds}s)")
        
        self.connect()
        
        try:
            while True:
                try:
                    self.run_once()
                except Exception as e:
                    logger.error(f"Error in policy cycle: {e}")
                    # Reconnect if connection lost
                    try:
                        self.connect()
                    except:
                        pass
                
                time.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            if self.conn:
                self.conn.close()

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='CephFS Tiering Policy Engine')
    parser.add_argument('--host', default='localhost', help='PostgreSQL host')
    parser.add_argument('--port', type=int, default=5432, help='PostgreSQL port')
    parser.add_argument('--database', default='tiering', help='Database name')
    parser.add_argument('--user', default='postgres', help='Database user')
    parser.add_argument('--password', default='', help='Database password')
    parser.add_argument('--interval', type=int, default=300, 
                       help='Policy application interval (seconds)')
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
    
    engine = PolicyEngine(db_config)
    
    if args.once:
        engine.connect()
        engine.run_once()
    else:
        engine.run_forever(args.interval)

if __name__ == '__main__':
    main()
