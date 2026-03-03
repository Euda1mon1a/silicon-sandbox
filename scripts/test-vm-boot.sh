#!/bin/bash
# Test VM boot — runs vm-launcher with a timeout, captures all output
set -e

PROJ_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VM_LAUNCHER="$PROJ_DIR/sandbox-engine/vm-launcher/.build/release/vm-launcher"
KERNEL="$PROJ_DIR/config/vm-images/vmlinuz-virt"
INITRD="$PROJ_DIR/config/vm-images/initramfs.cpio.gz"
LOG="/tmp/vm-boot-test.log"

echo "=== SiliconSandbox VM Boot Test ===" | tee "$LOG"
echo "Kernel: $KERNEL" | tee -a "$LOG"
echo "Initrd: $INITRD" | tee -a "$LOG"
echo "Time: $(date)" | tee -a "$LOG"
echo "---" | tee -a "$LOG"

# Run VM with a kill timer
"$VM_LAUNCHER" boot \
    --kernel "$KERNEL" \
    --initrd "$INITRD" \
    --cpus 2 \
    --memory 1 \
    </dev/null \
    >> "$LOG" 2>&1 &
VM_PID=$!

echo "VM PID: $VM_PID" | tee -a "$LOG"

# Wait up to 15 seconds for output
for i in $(seq 1 15); do
    sleep 1
    if ! kill -0 $VM_PID 2>/dev/null; then
        echo "VM exited after ${i}s" | tee -a "$LOG"
        break
    fi
    # Check if we got the SANDBOX_READY marker
    if grep -q "SANDBOX_READY" "$LOG" 2>/dev/null; then
        echo "VM booted successfully! (${i}s)" | tee -a "$LOG"
        break
    fi
done

# Show output
echo "=== VM Output ===" | tee -a "$LOG"
cat "$LOG"

# Cleanup
kill $VM_PID 2>/dev/null
wait $VM_PID 2>/dev/null
echo "=== Test complete ==="
