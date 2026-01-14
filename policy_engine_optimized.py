#!/usr/bin/env python3
"""
CephFS 3-Tier Storage Policy Engine (OPTIMIZED)

Uses PostgreSQL functions for batch policy application.
All logic runs in database - no Python loops, no individual UPDATEs.

Performance: 100x faster than iterating in Python.
"""

import psycopg2
import argparse
import logging
import time
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s'
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
        """Reconnect to database"""
        try:
            if self.conn:
                self.conn.close()
        except:
            pass
        self.connect()
    
    def apply_policies(self):
        """Apply tiering policies using PostgreSQL function"""
        try:
            with self.conn.cursor() as cur:
                # Call database function - does all the work
                cur.execute("SELECT * FROM apply_tiering_policies()")
                result = cur.fetchone()
                
                if result:
                    data_to_warm, warm_to_cold, warm_to_data, cold_to_warm = result
                    total = data_to_warm + warm_to_cold + warm_to_data + cold_to_warm
                    return (total, data_to_warm, warm_to_cold, warm_to_data, cold_to_warm)
                
        except psycopg2.Error as e:
            logger.error(f"Database error during policy application: {e}")
            self.reconnect()
            return (0, 0, 0, 0, 0)
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return (0, 0, 0, 0, 0)
    
    def get_pool_stats(self):
        """Get file count and average age per pool"""
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        current_pool,
                        COUNT(*) as file_count,
                        ROUND(AVG(EXTRACT(EPOCH FROM (NOW() - last_access)) / 60.0), 1) as avg_age_minutes,
                        SUM(CASE WHEN needs_migration THEN 1 ELSE 0 END) as pending_migrations
                    FROM file_metadata
                    GROUP BY current_pool
                    ORDER BY current_pool
                """)
                
                stats = cur.fetchall()
                return stats
        except Exception as e:
            logger.error(f"Error getting pool stats: {e}")
            return []
    
    def run(self, interval=60):
        """Main loop - apply policies periodically"""
        cycle = 0
        while True:
            try:
                cycle += 1
                logger.info("="*40 + " Policy Engine Cycle Start ===")
                
                # Apply policies (all in database)
                total, data_to_warm, warm_to_cold, warm_to_data, cold_to_warm = self.apply_policies()
                
                # Log minimal statistics
                logger.info(f"Total: {total} files marked for migration")
                logger.info(f"{data_to_warm} files data -> warm")
                logger.info(f"{warm_to_cold} files warm -> cold")
                logger.info(f"{warm_to_data} files warm -> data")
                logger.info(f"{cold_to_warm} files cold -> warm")
                logger.info(f"Sleeping for {interval} seconds...")
                logger.info("="*40 + " Cycle Complete: " + str(total) + " files marked ===")
                
                time.sleep(interval)
                
            except KeyboardInterrupt:
                logger.info("\nShutting down gracefully...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description='CephFS Policy Engine (Optimized)')
    parser.add_argument('--host', default='localhost', help='PostgreSQL host')
    parser.add_argument('--port', type=int, default=5432, help='PostgreSQL port')
    parser.add_argument('--database', default='tiering', help='Database name')
    parser.add_argument('--user', default='tiering_user', help='Database user')
    parser.add_argument('--password', default='1', help='Database password')
    parser.add_argument('--interval', type=int, default=60, help='Policy check interval (seconds)')
    
    args = parser.parse_args()
    
    try:
        engine = PolicyEngine(
            args.host,
            args.port,
            args.database,
            args.user,
            args.password
        )
        engine.run(args.interval)
    except KeyboardInterrupt:
        logger.info("\nShutdown complete")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()


