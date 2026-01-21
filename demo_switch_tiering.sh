#!/bin/bash
# Demo Script: Switch Between CephFS Tiering Policy Modes
# Mode 1: Access Frequency-Based (score calculation)
# Mode 2: Last Access Time-Based (timestamp thresholds)
# Usage: ./demo_switch_tiering.sh [time|frequency|status]
# Run this script directly on the VM

show_status() {
    echo "=========================================="
    echo "CEPHFS TIERING POLICY STATUS"
    echo "=========================================="
    
    echo -e "\nServices:"
    systemctl is-active cephfs-tracker.service > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo "   eBPF Tracker: ✅ RUNNING"
    else
        echo "   eBPF Tracker: ❌ STOPPED"
    fi
    
    systemctl is-active cephfs-policy-engine.service > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo "   Policy Engine: ✅ RUNNING"
    else
        echo "   Policy Engine: ❌ STOPPED"
    fi
    
    systemctl is-active cephfs-migration-worker.service > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo "   Migration Worker: ✅ RUNNING"
    else
        echo "   Migration Worker: ❌ STOPPED"
    fi
    
    echo -e "\nPolicy Mode:"
    POLICY_FUNC=$(grep -oP '(?<=FROM )[a-z_]+(?=\(\))' /home/cephvm/tiering_system/src/policy_engine_optimized.py 2>/dev/null | head -1)
    
    if [ "$POLICY_FUNC" = "apply_tiering_policies" ]; then
        echo "   Current: 🎯 ACCESS FREQUENCY-BASED"
        echo "   Logic: Files tiered by access frequency scores"
    elif [ "$POLICY_FUNC" = "mark_files_for_migration" ]; then
        echo "   Current: ⏰ LAST ACCESS TIME-BASED"
        echo "   Logic: Files tiered by time since last access"
    else
        echo "   Current: ⚠️  UNKNOWN (Could not detect policy function)"
        echo "   File: /home/cephvm/tiering_system/src/policy_engine_optimized.py"
    fi
    
    echo -e "\n=========================================="
}

enable_frequency_based() {
    echo "=========================================="
    echo "ENABLING: Access Frequency-Based Policy"
    echo "=========================================="
    
    echo -e "\n1. Modifying policy_engine_optimized.py to use apply_tiering_policies()..."
    sed -i 's/mark_files_for_migration()/apply_tiering_policies()/g' /home/cephvm/tiering_system/src/policy_engine_optimized.py
    
    echo "2. Restarting policy engine service..."
    sudo systemctl restart cephfs-policy-engine.service
    sleep 2
}

enable_time_based() {
    echo "=========================================="
    echo "ENABLING: Last Access Time-Based Policy"
    echo "=========================================="
    
    echo -e "\n1. Modifying policy_engine_optimized.py to use mark_files_for_migration()..."
    sed -i 's/apply_tiering_policies()/mark_files_for_migration()/g' /home/cephvm/tiering_system/src/policy_engine_optimized.py
    
    echo "2. Restarting policy engine service..."
    sudo systemctl restart cephfs-policy-engine.service
    sleep 2
}

disable_all() {
    echo "=========================================="
    echo "DISABLING: CephFS Tiering Services"
    echo "=========================================="
    
    echo -e "\nStopping all services..."
    sudo systemctl stop cephfs-tracker.service
    sudo systemctl stop cephfs-policy-engine.service
    sudo systemctl stop cephfs-migration-worker.service
    
    echo -e "\n✅ CephFS tiering DISABLED"
    echo "   (Files remain in their current pools)"
    echo "   (Database retains historical data)"
}

case "$1" in
    time)
        enable_time_based
        ;;
    frequency)
        enable_frequency_based
        ;;
    status|"")
        show_status
        ;;
    off)
        disable_all
        ;;
    *)
        echo "Usage: $0 [time|frequency|status|off]"
        echo ""
        echo "Commands:"
        echo "  time      - Enable Last Access Time-Based Policy"
        echo "  frequency - Enable Access Frequency-Based Policy"
        echo "  status    - Show current configuration (default)"
        echo "  off       - Disable all CephFS tiering services"
        exit 1
        ;;
esac
