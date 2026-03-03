#!/bin/bash
# Prepare VM images for SiliconSandbox
# Downloads Alpine rootfs and kernel, extracts uncompressed Image,
# includes VirtioFS kernel modules in initramfs.
set -e

PROJ_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMG_DIR="$PROJ_DIR/config/vm-images"
GUEST_AGENT="$PROJ_DIR/sandbox-engine/guest-agent/init"
ALPINE_VERSION="3.21"
ARCH="aarch64"

echo "=== SiliconSandbox VM Image Preparation ==="
echo "Target: $IMG_DIR"
mkdir -p "$IMG_DIR"

# 1. Download Alpine minirootfs if needed
ROOTFS="$IMG_DIR/alpine-minirootfs.tar.gz"
if [ ! -f "$ROOTFS" ]; then
    echo "Downloading Alpine minirootfs..."
    ROOTFS_URL="https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/releases/${ARCH}/alpine-minirootfs-${ALPINE_VERSION}.3-${ARCH}.tar.gz"
    curl -L -o "$ROOTFS" "$ROOTFS_URL"
    echo "Downloaded: $(ls -lh "$ROOTFS" | awk '{print $5}')"
else
    echo "Alpine minirootfs already present"
fi

# 2. Download Alpine linux-virt kernel APK (kernel + modules)
APK_FILE="$IMG_DIR/linux-virt.apk"
IMAGE="$IMG_DIR/Image"
if [ ! -f "$IMAGE" ] || [ "$1" = "--rebuild" ]; then
    echo "Finding latest linux-virt kernel..."
    KERNEL_PKG=$(curl -sL "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/main/${ARCH}/" | \
        python3 -c "import sys; data=sys.stdin.read(); [print(l.split('\"')[1]) for l in data.split('\n') if 'linux-virt' in l and 'dev' not in l]" | head -1)

    if [ -z "$KERNEL_PKG" ]; then
        echo "Error: Could not find linux-virt package"
        exit 1
    fi

    echo "Downloading $KERNEL_PKG..."
    APK_URL="https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/main/${ARCH}/${KERNEL_PKG}"
    curl -L -o "$APK_FILE" "$APK_URL"

    echo "Extracting from APK..."
    APK_TMP="$IMG_DIR/apk-tmp"
    rm -rf "$APK_TMP"
    mkdir -p "$APK_TMP"
    cd "$APK_TMP"
    tar xzf "$APK_FILE" 2>/dev/null
    cp boot/vmlinuz-virt "$IMG_DIR/vmlinuz-virt"
    cd "$IMG_DIR"

    echo "Extracting uncompressed ARM64 Image from vmlinuz-virt..."
    python3 << 'PYEOF'
import struct, zlib, sys

with open('vmlinuz-virt', 'rb') as f:
    data = f.read()

# Find gzip stream (compressed kernel payload)
for i in range(len(data) - 2):
    if data[i] == 0x1f and data[i+1] == 0x8b and data[i+2] == 0x08:
        try:
            dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
            result = dec.decompress(data[i:])
            if len(result) > 0x40:
                magic = struct.unpack_from('<I', result, 0x38)[0]
                if magic == 0x644d5241:  # ARM\x64
                    with open('Image', 'wb') as out:
                        out.write(result)
                    print(f"Extracted ARM64 Image: {len(result)} bytes")
                    sys.exit(0)
        except:
            continue

print("Error: Could not extract kernel Image")
sys.exit(1)
PYEOF
    echo "Kernel Image: $(ls -lh "$IMAGE" | awk '{print $5}')"
else
    echo "Kernel Image already present"
fi

# 3. Build initramfs from rootfs + guest agent + kernel modules
INITRD="$IMG_DIR/initramfs.cpio.gz"
if [ ! -f "$INITRD" ] || [ "$1" = "--rebuild" ]; then
    echo "Building initramfs..."
    BUILD_DIR="$IMG_DIR/rootfs-build"
    rm -rf "$BUILD_DIR"
    mkdir -p "$BUILD_DIR"
    cd "$BUILD_DIR"
    tar xzf "$ROOTFS"

    # Copy guest agent init script
    if [ -f "$GUEST_AGENT" ]; then
        cp "$GUEST_AGENT" init
        chmod +x init
        echo "Using guest agent from $GUEST_AGENT"
    else
        echo "Warning: Guest agent not found at $GUEST_AGENT"
        exit 1
    fi

    # Include kernel modules from the APK (VirtioFS + networking)
    APK_TMP="$IMG_DIR/apk-tmp"
    if [ -d "$APK_TMP/lib/modules" ]; then
        KVER=$(ls "$APK_TMP/lib/modules/" | head -1)
        echo "Including kernel modules for $KVER"
        SRC="$APK_TMP/lib/modules/$KVER"
        DST="$BUILD_DIR/lib/modules/$KVER"
        mkdir -p "$DST/kernel/fs/fuse" "$DST/kernel/drivers/net" "$DST/kernel/net/core" "$DST/kernel/net/packet"

        # VirtioFS: fuse + virtiofs
        cp "$SRC/kernel/fs/fuse/fuse.ko.gz" "$DST/kernel/fs/fuse/" 2>/dev/null || true
        cp "$SRC/kernel/fs/fuse/virtiofs.ko.gz" "$DST/kernel/fs/fuse/" 2>/dev/null || true

        # Networking: af_packet (DHCP), failover chain, virtio_net
        cp "$SRC/kernel/net/packet/af_packet.ko.gz" "$DST/kernel/net/packet/" 2>/dev/null || true
        cp "$SRC/kernel/net/core/failover.ko.gz" "$DST/kernel/net/core/" 2>/dev/null || true
        cp "$SRC/kernel/drivers/net/net_failover.ko.gz" "$DST/kernel/drivers/net/" 2>/dev/null || true
        cp "$SRC/kernel/drivers/net/virtio_net.ko.gz" "$DST/kernel/drivers/net/" 2>/dev/null || true

        # Copy module metadata files
        for f in modules.dep modules.dep.bin modules.alias modules.alias.bin modules.symbols modules.symbols.bin modules.order modules.builtin modules.builtin.bin modules.builtin.modinfo; do
            [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DST/"
        done
    fi

    find . | cpio -o -H newc 2>/dev/null | gzip > "$INITRD"
    cd "$IMG_DIR"
    rm -rf "$BUILD_DIR" "$APK_TMP"
    echo "Initramfs: $(ls -lh "$INITRD" | awk '{print $5}')"
else
    echo "Initramfs already present"
fi

# Clean up APK file (no longer needed)
rm -f "$APK_FILE"

echo ""
echo "=== VM Images Ready ==="
echo "  Kernel:   $IMAGE ($(ls -lh "$IMAGE" | awk '{print $5}'))"
echo "  Initrd:   $INITRD ($(ls -lh "$INITRD" | awk '{print $5}'))"
echo "  Rootfs:   $ROOTFS ($(ls -lh "$ROOTFS" | awk '{print $5}'))"
