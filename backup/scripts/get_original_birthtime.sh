#!/bin/bash
# Helper script to get original birth time of migrated files

FILE="$1"

if [ -z "$FILE" ]; then
    echo "Usage: $0 <filepath>"
    exit 1
fi

echo "=== File Timestamps for: $FILE ==="
echo

# Current timestamps
echo "Current timestamps (after migration):"
stat "$FILE" | grep -E "Access:|Modify:|Change:|Birth:"

echo
echo "---"
echo

# Original birth time from xattr
if getfattr -n user.original_birthtime "$FILE" 2>/dev/null | grep -q user.original_birthtime; then
    ORIGINAL_BTIME=$(getfattr --only-values -n user.original_birthtime "$FILE" 2>/dev/null)
    if [ -n "$ORIGINAL_BTIME" ]; then
        echo "Original birth time (before migration):"
        echo "  Timestamp: $ORIGINAL_BTIME"
        echo "  Human-readable: $(date -d @$ORIGINAL_BTIME '+%Y-%m-%d %H:%M:%S %z')"
    fi
else
    echo "No original birth time stored (file not migrated with btime preservation)"
fi
