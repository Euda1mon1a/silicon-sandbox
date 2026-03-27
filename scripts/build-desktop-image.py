#!/usr/bin/env python3
"""Build the desktop VM disk image for SiliconSandbox Tier B.

Uses the existing MicroVM (booted from initramfs) to create, format, and
populate an ext4 disk image with Alpine Linux + desktop packages.

Usage:
    python3 scripts/build-desktop-image.py [--size-mb 2048]

The resulting image at config/vm-images/alpine-desktop.img can boot
standalone with the --rootfs flag (no initramfs needed).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# Project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "sandbox-engine"))

from sandbox_engine.microvm import MicroVM, is_available, _PROJECT_ROOT

IMAGES_DIR = _PROJECT_ROOT / "config" / "vm-images"
DESKTOP_IMAGE = IMAGES_DIR / "alpine-desktop.img"
DESKTOP_INIT = PROJECT_ROOT / "sandbox-engine" / "guest-agent" / "desktop-init"

# Alpine packages for the desktop environment
DESKTOP_PACKAGES = [
    # Display
    "xvfb", "openbox", "xdotool",
    # Browser
    "chromium",
    # Screenshot
    "scrot",
    # Fonts (needed for readable rendering)
    "font-noto", "font-noto-emoji",
    # Chromium dependencies
    "dbus", "mesa-dri-gallium", "mesa-gl",
    # Networking tools (for curl-based CDP)
    "curl",
    # Utilities
    "coreutils", "bash",
    # Python for CDP WebSocket communication
    "python3",
]


def create_raw_image(path: Path, size_mb: int) -> None:
    """Create a zeroed raw disk image."""
    print(f"Creating {size_mb}MB raw disk image at {path}")
    subprocess.run(
        ["dd", "if=/dev/zero", f"of={path}", "bs=1m", f"count={size_mb}"],
        check=True,
        capture_output=True,
    )
    print(f"  Created: {path} ({path.stat().st_size / 1024 / 1024:.0f} MB)")


def build_image(size_mb: int = 2048) -> None:
    """Build the desktop disk image using a MicroVM as the builder."""
    if not is_available():
        print("ERROR: MicroVM infrastructure not available.")
        print("  Need: vm-launcher binary, kernel Image, initramfs.cpio.gz")
        sys.exit(1)

    if not DESKTOP_INIT.exists():
        print(f"ERROR: Desktop init script not found at {DESKTOP_INIT}")
        sys.exit(1)

    # Step 1: Create raw image
    create_raw_image(DESKTOP_IMAGE, size_mb)

    # Step 2: Boot MicroVM with initramfs + disk attached (builder mode)
    print("\nBooting builder MicroVM (initramfs + disk attached, network enabled)...")
    vm = MicroVM(
        cpus=4,
        memory_gb=4,
        allow_network=True,
        disk_image=str(DESKTOP_IMAGE),
        boot_from_disk=False,  # Boot from initramfs, disk is /dev/vda
    )

    try:
        vm.start(timeout=30)
        print("  MicroVM ready")

        # Step 3: Set up DNS and install e2fsprogs (mkfs.ext4)
        _exec(vm, "echo 'nameserver 8.8.8.8' > /etc/resolv.conf", "Setting up DNS for builder")
        _exec(
            vm,
            "echo 'https://dl-cdn.alpinelinux.org/alpine/v3.21/main' > /etc/apk/repositories && "
            "echo 'https://dl-cdn.alpinelinux.org/alpine/v3.21/community' >> /etc/apk/repositories && "
            "apk add --no-cache e2fsprogs",
            "Installing e2fsprogs in builder",
            timeout=60,
        )

        # Format the disk
        _exec(vm, "mkfs.ext4 -F /dev/vda", "Formatting disk as ext4", timeout=30)

        # Step 4: Mount and populate
        _exec(vm, "mkdir -p /mnt && mount -t ext4 /dev/vda /mnt", "Mounting disk")

        # Copy the base rootfs from initramfs to disk
        _exec(
            vm,
            "for d in bin etc home lib root sbin usr var opt; do "
            "  [ -d /$d ] && cp -a /$d /mnt/ 2>/dev/null; "
            "done && "
            "mkdir -p /mnt/proc /mnt/sys /mnt/dev /mnt/tmp /mnt/run /mnt/workspace /mnt/dev/pts /mnt/dev/shm",
            "Copying base rootfs to disk",
            timeout=60,
        )

        # Copy kernel modules
        _exec(vm, "cp -a /lib/modules /mnt/lib/", "Copying kernel modules")

        # Step 5: Configure Alpine repositories
        _exec(
            vm,
            "mkdir -p /mnt/etc/apk && "
            "echo 'https://dl-cdn.alpinelinux.org/alpine/v3.21/main' > /mnt/etc/apk/repositories && "
            "echo 'https://dl-cdn.alpinelinux.org/alpine/v3.21/community' >> /mnt/etc/apk/repositories",
            "Configuring Alpine repositories",
        )

        # Set up DNS
        _exec(vm, "echo 'nameserver 8.8.8.8' > /mnt/etc/resolv.conf", "Setting DNS")

        # Step 6: Install desktop packages
        pkg_list = " ".join(DESKTOP_PACKAGES)
        _exec(
            vm,
            f"apk --root /mnt --initdb add --no-cache {pkg_list}",
            f"Installing {len(DESKTOP_PACKAGES)} desktop packages",
            timeout=300,
        )

        # Step 7: Write the desktop init script (in chunks — serial protocol limits)
        # Write to temp path first, then replace (apk may create /sbin/init as busybox symlink)
        _write_file_chunked(vm, DESKTOP_INIT, "/tmp/desktop-init")
        _exec(
            vm,
            "rm -f /mnt/sbin/init && cp /tmp/desktop-init /mnt/sbin/init && chmod 755 /mnt/sbin/init",
            "Installing desktop init script",
        )

        # Step 8: Create the udhcpc default script (needed for DHCP in disk mode)
        _exec(
            vm,
            "mkdir -p /mnt/usr/share/udhcpc && "
            "[ -f /usr/share/udhcpc/default.script ] && "
            "cp /usr/share/udhcpc/default.script /mnt/usr/share/udhcpc/ || true",
            "Copying DHCP scripts",
        )

        # Step 9: Sync and unmount
        _exec(vm, "sync && umount /mnt", "Syncing and unmounting")

        print("\nDesktop image built successfully!")
        print(f"  Path: {DESKTOP_IMAGE}")
        print(f"  Size: {DESKTOP_IMAGE.stat().st_size / 1024 / 1024:.0f} MB")

    except Exception as e:
        print(f"\nERROR: Build failed: {e}")
        # Clean up partial image
        if DESKTOP_IMAGE.exists():
            DESKTOP_IMAGE.unlink()
            print("  Removed partial image")
        raise
    finally:
        print("\nShutting down builder VM...")
        vm.stop()


def _exec(vm: MicroVM, command: str, label: str, timeout: int = 60) -> str:
    """Execute a command in the VM, print status, and return stdout."""
    print(f"  [{label}]...", end="", flush=True)
    t0 = time.time()
    result = vm.exec_command(command, timeout=timeout)
    elapsed = time.time() - t0
    exit_code = result.get("exit_code", -1)

    if exit_code != 0:
        stderr = result.get("stderr", "")
        print(f" FAILED ({exit_code}, {elapsed:.1f}s)")
        print(f"    stderr: {stderr[:500]}")
        raise RuntimeError(f"{label} failed with exit code {exit_code}: {stderr[:200]}")

    print(f" OK ({elapsed:.1f}s)")
    stdout = result.get("stdout", "")
    if stdout.strip():
        # Print last few lines of output for visibility
        lines = stdout.strip().split("\n")
        for line in lines[-3:]:
            print(f"    {line[:120]}")
    return stdout


def _write_file_chunked(vm: MicroVM, src: Path, dest: str, chunk_size: int = 1500) -> None:
    """Write a file to the guest in base64 chunks (serial line limit ~4KB)."""
    import base64
    content = src.read_bytes()
    total = len(content)
    n_chunks = (total + chunk_size - 1) // chunk_size
    print(f"  [Writing {src.name} ({total} bytes, {n_chunks} chunks) to {dest}]...", end="", flush=True)

    for i in range(0, total, chunk_size):
        chunk_b64 = base64.b64encode(content[i:i + chunk_size]).decode("ascii")
        op = ">" if i == 0 else ">>"
        # Use echo without quotes (base64 has no shell-special chars except +/=)
        result = vm.exec_command(
            f"echo {chunk_b64} | base64 -d {op} {dest}",
            timeout=10,
        )
        if result.get("exit_code", -1) != 0:
            print(f" FAILED at offset {i}")
            raise RuntimeError(f"Failed to write chunk at offset {i}")

    # Verify size
    result = vm.exec_command(f"wc -c {dest}", timeout=5)
    stdout = result.get("stdout", "").strip()
    try:
        written = int(stdout.split()[0]) if stdout else 0
    except (ValueError, IndexError):
        written = 0
    if written != total:
        print(f" SIZE MISMATCH ({written} != {total})")
        raise RuntimeError(f"Size mismatch: wrote {written}, expected {total}")
    print(f" OK")


def main():
    parser = argparse.ArgumentParser(description="Build SiliconSandbox desktop VM image")
    parser.add_argument("--size-mb", type=int, default=2048, help="Disk image size in MB (default: 2048)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing image")
    args = parser.parse_args()

    if DESKTOP_IMAGE.exists() and not args.force:
        print(f"Desktop image already exists at {DESKTOP_IMAGE}")
        print(f"  Size: {DESKTOP_IMAGE.stat().st_size / 1024 / 1024:.0f} MB")
        print("Use --force to rebuild")
        sys.exit(0)

    build_image(size_mb=args.size_mb)


if __name__ == "__main__":
    main()
