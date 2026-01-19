#!/bin/bash

# Script to check tiering status of a file
# Usage: ./check_file_status.sh <filename>

if [ -z "$1" ]; then
    echo "Usage: $0 <filename>"
    echo "Example: $0 a.txt"
    echo "Example: $0 /tiercephfs/a.txt"
    exit 1
fi

FILENAME="$1"

# Extract just the filename (basename) for search
BASENAME=$(basename "$FILENAME")

echo "Checking tiering status for: $BASENAME"
echo "========================================="

sudo -u postgres psql -d tiering << EOF
SELECT 
    path,
    current_pool,
    target_pool,
    access_freq,
    needs_migration,
    score,
    last_evaluation_time,
    creation_time,
    need_eval
FROM file_metadata 
WHERE path = '$BASENAME';
EOF
