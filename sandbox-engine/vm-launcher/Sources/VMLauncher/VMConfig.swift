import Foundation
import Virtualization

/// Configuration for a MicroVM instance.
struct VMConfig {
    let kernelPath: String
    let rootfsPath: String        // ext4 disk image (block device) — optional if initrdPath set
    let initrdPath: String        // initramfs cpio.gz — optional if rootfsPath set
    let cpuCount: Int
    let memoryGB: Int
    let vsockPort: UInt32
    let networkEnabled: Bool
    let sharedDirectories: [(hostPath: String, guestTag: String)]

    var memoryBytes: UInt64 {
        UInt64(memoryGB) * 1024 * 1024 * 1024
    }

    /// Create a VZVirtualMachineConfiguration from this config.
    /// Note: kernelPath must point to an uncompressed ARM64 Image file (not vmlinuz).
    func createVZConfig() throws -> VZVirtualMachineConfiguration {
        let config = VZVirtualMachineConfiguration()

        // Platform — generic for Linux VMs
        config.platform = VZGenericPlatformConfiguration()

        // Boot loader — Linux kernel direct boot (requires uncompressed ARM64 Image)
        let bootLoader = VZLinuxBootLoader(kernelURL: URL(fileURLWithPath: kernelPath))

        if !initrdPath.isEmpty {
            // Initramfs boot — OS runs entirely from RAM
            var cmdline = "console=hvc0"

            // Pass VirtioFS mount info via kernel cmdline
            if !sharedDirectories.isEmpty {
                let tags = sharedDirectories.map { "\($0.guestTag):/mnt/\($0.guestTag)" }.joined(separator: ",")
                cmdline += " virtiofs_tags=\(tags)"
            }

            bootLoader.commandLine = cmdline
            bootLoader.initialRamdiskURL = URL(fileURLWithPath: initrdPath)
        } else {
            // Disk boot — OS on virtio block device
            bootLoader.commandLine = "console=hvc0 root=/dev/vda rw"
        }
        config.bootLoader = bootLoader

        // CPU and memory
        config.cpuCount = max(1, min(cpuCount, VZVirtualMachineConfiguration.maximumAllowedCPUCount))
        config.memorySize = max(
            VZVirtualMachineConfiguration.minimumAllowedMemorySize,
            min(memoryBytes, VZVirtualMachineConfiguration.maximumAllowedMemorySize)
        )

        // Serial console — virtio console on hvc0
        // Always use stdin/stdout so the parent process can communicate
        // with the guest via the serial port (both interactive and programmatic).
        let serialPort = VZVirtioConsoleDeviceSerialPortConfiguration()
        serialPort.attachment = VZFileHandleSerialPortAttachment(
            fileHandleForReading: FileHandle.standardInput,
            fileHandleForWriting: FileHandle.standardOutput
        )
        config.serialPorts = [serialPort]

        // Root filesystem — virtio block device
        if !rootfsPath.isEmpty {
            let diskURL = URL(fileURLWithPath: rootfsPath)
            let diskAttachment = try VZDiskImageStorageDeviceAttachment(
                url: diskURL,
                readOnly: false
            )
            let disk = VZVirtioBlockDeviceConfiguration(attachment: diskAttachment)
            config.storageDevices = [disk]
        }

        // Entropy — needed for /dev/random in guest
        config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]

        // Memory balloon — allows host to reclaim unused guest memory
        config.memoryBalloonDevices = [VZVirtioTraditionalMemoryBalloonDeviceConfiguration()]

        // vsock — host-guest communication channel
        let vsockDevice = VZVirtioSocketDeviceConfiguration()
        config.socketDevices = [vsockDevice]

        // Network — optional NAT via vmnet
        if networkEnabled {
            let networkDevice = VZVirtioNetworkDeviceConfiguration()
            networkDevice.attachment = VZNATNetworkDeviceAttachment()
            config.networkDevices = [networkDevice]
        }

        // Shared directories — VirtioFS
        var shares: [VZVirtioFileSystemDeviceConfiguration] = []
        for dir in sharedDirectories {
            let shareConfig = VZVirtioFileSystemDeviceConfiguration(tag: dir.guestTag)
            let sharedDir = VZSharedDirectory(url: URL(fileURLWithPath: dir.hostPath), readOnly: true)
            shareConfig.share = VZSingleDirectoryShare(directory: sharedDir)
            shares.append(shareConfig)
        }
        config.directorySharingDevices = shares

        try config.validate()
        return config
    }
}
