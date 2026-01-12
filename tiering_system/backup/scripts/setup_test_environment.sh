#!/bin/bash
# Complete setup script for CephFS 3-tier tiering system (TEST MODE)
# Automates: pool creation, database setup, service verification

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIERING_DIR="$(dirname "$SCRIPT_DIR")"
FSNAME="tiercephfs"

echo "========================================"
echo "  CephFS 3-Tier Storage Setup (TEST)"
echo "========================================"
echo ""
echo "Configuration:"
echo "  - 3 pools: hot → warm → cold"
echo "  - 3-minute intervals for testing"
echo "  - PostgreSQL database backend"
echo ""

# Step 1: Create 3 pools in CephFS
echo "[Step 1/6] Creating storage pools..."
bash "$SCRIPT_DIR/setup_3tier_pools.sh"

# Step 2: Verify data pool is default
echo ""
echo "[Step 2/6] Verifying data pool configuration..."
echo "Data pool (cephfs.${FSNAME}.data) is the default for new files"
echo "No additional configuration needed"

# Step 3: Setup PostgreSQL database
echo ""
echo "[Step 3/6] Setting up PostgreSQL database..."
DB_NAME="tiering"
DB_USER="tiering_user"
DB_PASS="tiering_pass"

# Check if database exists
sudo -u postgres psql -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME" || {
    echo "Creating database: $DB_NAME"
    sudo -u postgres createdb "$DB_NAME"
}

# Create user if not exists
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 || {
    echo "Creating user: $DB_USER"
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';"
}

# Load test schema
echo "Loading test schema (3-minute intervals)..."
sudo -u postgres psql "$DB_NAME" < "$TIERING_DIR/sql/schema_test.sql"

echo "Granting permissions..."
sudo -u postgres psql "$DB_NAME" -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"
sudo -u postgres psql "$DB_NAME" -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO $DB_USER;"
sudo -u postgres psql "$DB_NAME" -c "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;"

# Step 4: Verify libcephfs_migrate binary
echo ""
echo "[Step 4/6] Checking libcephfs_migrate binary..."
MIGRATE_BIN="$TIERING_DIR/../libcephfs_migrate"

if [ ! -f "$MIGRATE_BIN" ]; then
    echo "ERROR: libcephfs_migrate not found at $MIGRATE_BIN"
    echo "Please compile it first:"
    echo "  cd $(dirname $TIERING_DIR)"
    echo "  gcc -o libcephfs_migrate libcephfs_migrate.c -lcephfs"
    exit 1
else
    echo "Found: $MIGRATE_BIN"
fi

# Step 5: Install Python dependencies
echo ""
echo "[Step 5/6] Installing Python dependencies..."
pip3 install psycopg2-binary --quiet || echo "Note: psycopg2 may already be installed"

# Step 6: Create test files
echo ""
echo "[Step 6/6] Creating test files in hot pool..."
TEST_DIR="/tiercephfs/test_tiering"
sudo mkdir -p "$TEST_DIR" 2>/dev/null || mkdir -p "$TEST_DIR"

for i in {1..5}; do
    TEST_FILE="$TEST_DIR/test_file_${i}.txt"
    echo "Test file $i created at $(date)" | sudo tee "$TEST_FILE" > /dev/null 2>&1 || \
        echo "Test file $i created at $(date)" > "$TEST_FILE"
    echo "  Created: $TEST_FILE"
done

# Verify files are in data pool
echo ""
echo "Verifying file placement..."
sudo getfattr -n ceph.file.layout.pool "$TEST_DIR/test_file_1.txt" 2>/dev/null || \
    getfattr -n ceph.file.layout.pool "$TEST_DIR/test_file_1.txt"

echo ""
echo "========================================"
echo "  Setup Complete!"
echo "========================================"
echo ""
echo "Next steps for testing:"
echo ""
echo "1. Start Access Tracker (Terminal 1):"
echo "   cd $TIERING_DIR"
echo "   # Compile and run access_tracker (requires eBPF)"
echo ""
echo "2. Start Policy Engine (Terminal 2):"
echo "   python3 src/policy_engine_test.py --interval 60"
echo ""
echo "3. Start Migration Worker (Terminal 3):"
echo "   python3 src/migration_worker.py --workers 5 --libcephfs-bin $MIGRATE_BIN"
echo ""
echo "4. Monitor progress:"
echo "   sudo -u postgres psql tiering -c \"SELECT * FROM pool_statistics;\""
echo ""
echo "Timeline (3-minute intervals):"
echo "  t=0:    Files created in data pool"
echo "  t=3min: Policy marks files for warm pool"
echo "  t=6min: Policy marks files for cold pool"
echo ""
echo "To manually trigger policy:"
echo "  python3 src/policy_engine_test.py --once"
echo ""
