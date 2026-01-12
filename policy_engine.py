#!/usr/bin/env python3
"""
CephFS 3-Tier Storage Policy Engine (TEST MODE)

Applies tiering policies based on file age and access patterns.
Handles both PROMOTION (hot→warm→cold) and DEMOTION (warm/cold→hot).

Test Configuration (3 minutes = 30 days in production):
  - PROMOTION: data → warm after 3 minutes idle
  - PROMOTION: warm → cold after 6 minutes idle (total)
  - DEMOTION: warm/cold → data if accessed within 3 minutes
"""

import psycopg2
import argparse
import logging
import time
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PolicyEngine:
    def __init__(self, db_host, db_port, db_name, db_user, db_password):
        self.conn_params = {
            'host': db_host,
            'port': db_port,
            'dbname': db_name,
            'user': db_user,
            'password': db_password
        }
        self.conn = None
        self.connect()
    
    def connect(self):
        """Establish database connection"""
        try:
            self.conn = psycopg2.connect(**self.conn_params)
            self.conn.autocommit = True
            logger.info("Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise
    
    def reconnect(self):
        """Reconnect if connection is lost"""
        if self.conn:
            try:
                self.conn.close()
            except:
                pass
        self.connect()
    
    def apply_policies(self):
        """
        Apply tiering policies to mark files for migration.
        All policy logic in Python for visibility and ease of modification.
        
        DEMOTION (priority): warm/cold → data if accessed recently
        PROMOTION: data → warm → cold based on age
        """
        try:
            with self.conn.cursor() as cur:
                marked_total = 0
                
                # DEMOTION POLICY (Higher Priority)
                # Any file in warm/cold accessed within last 3 minutes → move to data
                logger.debug("Applying DEMOTION policy (warm/cold → data)")
                cur.execute("""
                    UPDATE file_metadata
                    SET needs_migration = TRUE,
                        target_pool = 'cephfs.tiercephfs.data'
                    WHERE current_pool IN ('cephfs.tiercephfs.warm', 'cephfs.tiercephfs.cold')
                      AND needs_migration = FALSE
                      AND last_access >= NOW() - INTERVAL '3 minutes'
                """)
                demoted = cur.rowcount
                marked_total += demoted
                if demoted > 0:
                    logger.info(f"  Demotion: {demoted} files marked (warm/cold → data)")
                
                # PROMOTION POLICY 1: data → warm
                # Files in data pool older than 3 minutes → move to warm
                logger.debug("Applying PROMOTION policy (data → warm)")
                cur.execute("""
                    UPDATE file_metadata
                    SET needs_migration = TRUE,
                        target_pool = 'cephfs.tiercephfs.warm'
                    WHERE current_pool = 'cephfs.tiercephfs.data'
                      AND needs_migration = FALSE
                      AND last_access < NOW() - INTERVAL '3 minutes'
                """)
                promoted_warm = cur.rowcount
                marked_total += promoted_warm
                if promoted_warm > 0:
                    logger.info(f"  Promotion: {promoted_warm} files marked (data → warm)")
                
                # PROMOTION POLICY 2: warm → cold
                # Files in warm pool older than 6 minutes total → move to cold
                logger.debug("Applying PROMOTION policy (warm → cold)")
                cur.execute("""
                    UPDATE file_metadata
                    SET needs_migration = TRUE,
                        target_pool = 'cephfs.tiercephfs.cold'
                    WHERE current_pool = 'cephfs.tiercephfs.warm'
                      AND needs_migration = FALSE
                      AND last_access < NOW() - INTERVAL '6 minutes'
                """)
                promoted_cold = cur.rowcount
                marked_total += promoted_cold
                if promoted_cold > 0:
                    logger.info(f"  Promotion: {promoted_cold} files marked (warm → cold)")
                
                logger.info(f"Total: {marked_total} files marked for migration")
                return marked_total
                
        except Exception as e:
            logger.error(f"Error applying policies: {e}")
            self.reconnect()
            return 0
    
    def get_statistics(self):
        """Get current pool statistics with demotion/promotion breakdown"""
        try:
            with self.conn.cursor() as cur:
                # Overall stats
                cur.execute("""
                    SELECT 
                        current_pool,
                        COUNT(*) as file_count,
                        ROUND(AVG(EXTRACT(EPOCH FROM (NOW() - last_access)) / 60.0), 2) as avg_age_minutes,
                        COUNT(*) FILTER (WHERE needs_migration = TRUE) as pending_migrations,
                        COUNT(*) FILTER (WHERE needs_migration = TRUE AND target_pool = 'cephfs.tiercephfs.data') as demotions,
                        COUNT(*) FILTER (WHERE needs_migration = TRUE AND target_pool != 'cephfs.tiercephfs.data') as promotions
                    FROM file_metadata
                    GROUP BY current_pool
                    ORDER BY 
                        CASE current_pool
                            WHEN 'cephfs.tiercephfs.data' THEN 1
                            WHEN 'cephfs.tiercephfs.warm' THEN 2
                            WHEN 'cephfs.tiercephfs.cold' THEN 3
                            ELSE 4
                        END
                """)
                
                stats = cur.fetchall()
                if stats:
                    logger.info("=== Pool Statistics ===")
                    for row in stats:
                        pool, count, avg_age, pending, demotions, promotions = row
                        pool_name = pool.split('.')[-1].upper()
                        logger.info(f"  {pool_name} ({pool}):")
                        logger.info(f"    Files: {count}, Avg age: {avg_age:.1f} min")
                        logger.info(f"    Pending: {pending} (↓demote: {demotions}, ↑promote: {promotions})")
                return stats
        except Exception as e:
            logger.error(f"Error getting statistics: {e}")
            return []
    
    def get_policy_config(self):
        """Display current policy configuration"""
        logger.info("=== Tiering Policies (TEST MODE: 3 min = 30 days) ===")
        logger.info("  PROMOTION:")
        logger.info("    data → warm: After 3 minutes idle")
        logger.info("    warm → cold: After 6 minutes idle (total)")
        logger.info("  DEMOTION:")
        logger.info("    warm → data: If accessed within 3 minutes")
        logger.info("    cold → data: If accessed within 3 minutes")
    
    def refresh_statistics(self):
        """Refresh statistics (placeholder for future materialized views)"""
        pass
    
    def run_once(self):
        """Run one policy application cycle"""
        logger.info("=== Policy Engine Cycle Start ===")
        
        # Show policy configuration
        self.get_policy_config()
        
        # Apply policies
        marked = self.apply_policies()
        
        # Show statistics
        self.get_statistics()
        
        # Refresh materialized view
        self.refresh_statistics()
        
        logger.info(f"=== Cycle Complete: {marked} files marked ===")
        return marked
    
    def run_forever(self, interval_seconds=60):
        """
        Run policy engine in daemon mode.
        
        For testing with 3-minute intervals, run every 60 seconds.
        Handles both promotion (aging) and demotion (reaccess).
        """
        logger.info(f"Starting policy engine daemon (interval: {interval_seconds}s)")
        logger.warning("TEST MODE: 3 minutes = 30 days in production!")
        logger.info("Policies: PROMOTION (data→warm→cold) + DEMOTION (warm/cold→data)")
        
        try:
            while True:
                try:
                    self.run_once()
                except Exception as e:
                    logger.error(f"Error in policy cycle: {e}")
                    self.reconnect()
                
                logger.info(f"Sleeping for {interval_seconds} seconds...\n")
                time.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("Shutting down policy engine...")
        finally:
            if self.conn:
                self.conn.close()


def main():
    parser = argparse.ArgumentParser(
        description='CephFS Tiering Policy Engine (TEST MODE - Minutes)'
    )
    parser.add_argument('--host', default='localhost',
                       help='PostgreSQL host')
    parser.add_argument('--port', type=int, default=5432,
                       help='PostgreSQL port')
    parser.add_argument('--database', default='tiering',
                       help='Database name')
    parser.add_argument('--user', default='tiering_user',
                       help='Database user')
    parser.add_argument('--password', default='1',
                       help='Database password')
    parser.add_argument('--interval', type=int, default=60,
                       help='Check interval in seconds (default: 60 for testing)')
    parser.add_argument('--once', action='store_true',
                       help='Run once and exit')
    
    args = parser.parse_args()
    
    engine = PolicyEngine(
        args.host,
        args.port,
        args.database,
        args.user,
        args.password
    )
    
    if args.once:
        engine.run_once()
    else:
        engine.run_forever(args.interval)


if __name__ == '__main__':
    main()
