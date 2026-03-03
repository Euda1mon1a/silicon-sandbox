import Foundation
import Virtualization

// MARK: - CLI Argument Parsing

struct CLIArgs {
    var command: String = "boot"  // boot, status, version
    var kernelPath: String = ""
    var rootfsPath: String = ""
    var initrdPath: String = ""
    var cpuCount: Int = 2
    var memoryGB: Int = 2
    var vsockPort: UInt32 = 1024
    var networkEnabled: Bool = false
    var sharedDirs: [(String, String)] = []  // (hostPath, guestTag)
}

func parseArgs() -> CLIArgs {
    var args = CLIArgs()
    let argv = CommandLine.arguments

    if argv.count < 2 {
        printUsage()
        exit(1)
    }

    args.command = argv[1]

    var i = 2
    while i < argv.count {
        switch argv[i] {
        case "--kernel":
            i += 1; args.kernelPath = argv[i]
        case "--rootfs":
            i += 1; args.rootfsPath = argv[i]
        case "--initrd":
            i += 1; args.initrdPath = argv[i]
        case "--cpus":
            i += 1; args.cpuCount = Int(argv[i]) ?? 2
        case "--memory":
            i += 1; args.memoryGB = Int(argv[i]) ?? 2
        case "--vsock-port":
            i += 1; args.vsockPort = UInt32(argv[i]) ?? 1024
        case "--allow-net":
            args.networkEnabled = true
        case "--share":
            i += 1
            let parts = argv[i].split(separator: ":", maxSplits: 1)
            if parts.count == 2 {
                args.sharedDirs.append((String(parts[0]), String(parts[1])))
            }
        default:
            fputs("Unknown argument: \(argv[i])\n", stderr)
        }
        i += 1
    }

    return args
}

func printUsage() {
    fputs("""
    vm-launcher — Apple Silicon MicroVM manager for SiliconSandbox

    Usage:
      vm-launcher boot --kernel <path> [--rootfs <path> | --initrd <path>] [options]
      vm-launcher version

    Options:
      --kernel <path>       Path to Linux kernel (vmlinuz)
      --rootfs <path>       Path to root filesystem image (ext4/raw block device)
      --initrd <path>       Path to initramfs (cpio.gz, boots from RAM)
      --cpus <n>            CPU cores (default: 2)
      --memory <n>          Memory in GB (default: 2)
      --vsock-port <n>      vsock port for guest agent (default: 1024)
      --allow-net           Enable NAT networking
      --share <host:tag>    Share host directory into guest via VirtioFS

    Examples:
      vm-launcher boot --kernel ./vmlinuz --initrd ./initramfs.cpio.gz
      vm-launcher boot --kernel ./vmlinuz --rootfs ./alpine.img --cpus 4 --memory 4 --allow-net

    """, stderr)
}

// MARK: - VM Lifecycle

@MainActor
func bootVM(args: CLIArgs) async throws {
    guard !args.kernelPath.isEmpty else {
        fputs("Error: --kernel is required\n", stderr)
        exit(1)
    }
    guard !args.rootfsPath.isEmpty || !args.initrdPath.isEmpty else {
        fputs("Error: --rootfs or --initrd is required\n", stderr)
        exit(1)
    }

    // Verify files exist
    guard FileManager.default.fileExists(atPath: args.kernelPath) else {
        fputs("Error: kernel not found at \(args.kernelPath)\n", stderr)
        exit(1)
    }
    if !args.rootfsPath.isEmpty {
        guard FileManager.default.fileExists(atPath: args.rootfsPath) else {
            fputs("Error: rootfs not found at \(args.rootfsPath)\n", stderr)
            exit(1)
        }
    }
    if !args.initrdPath.isEmpty {
        guard FileManager.default.fileExists(atPath: args.initrdPath) else {
            fputs("Error: initrd not found at \(args.initrdPath)\n", stderr)
            exit(1)
        }
    }

    let config = VMConfig(
        kernelPath: args.kernelPath,
        rootfsPath: args.rootfsPath,
        initrdPath: args.initrdPath,
        cpuCount: args.cpuCount,
        memoryGB: args.memoryGB,
        vsockPort: args.vsockPort,
        networkEnabled: args.networkEnabled,
        sharedDirectories: args.sharedDirs
    )

    let bootMode = !args.initrdPath.isEmpty ? "initramfs" : "disk"
    FileHandle.standardError.write(Data("Booting VM: \(args.cpuCount) CPUs, \(args.memoryGB) GB RAM, net=\(args.networkEnabled), boot=\(bootMode)\n".utf8))

    let vzConfig: VZVirtualMachineConfiguration
    do {
        vzConfig = try config.createVZConfig()
        FileHandle.standardError.write(Data("VM config validated\n".utf8))
    } catch {
        FileHandle.standardError.write(Data("VM config error: \(error)\n".utf8))
        throw error
    }

    let vm = VZVirtualMachine(configuration: vzConfig)

    // Set up signal handler for clean shutdown
    let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
    signal(SIGINT, SIG_IGN)
    sigintSource.setEventHandler {
        FileHandle.standardError.write(Data("\nShutting down VM...\n".utf8))
        if vm.canRequestStop {
            try? vm.requestStop()
        } else {
            exit(0)
        }
    }
    sigintSource.resume()

    // Start the VM
    FileHandle.standardError.write(Data("Starting VM...\n".utf8))
    try await vm.start()
    FileHandle.standardError.write(Data("VM started successfully (state: \(vm.state.rawValue))\n".utf8))

    // Wait for VM to stop
    while vm.state != .stopped && vm.state != .error {
        try await Task.sleep(nanoseconds: 100_000_000)  // 100ms
    }

    if vm.state == .error {
        fputs("VM stopped with error\n", stderr)
        exit(1)
    }

    fputs("VM stopped\n", stderr)
}

// MARK: - Entry Point

let args = parseArgs()

switch args.command {
case "boot":
    Task { @MainActor in
        do {
            try await bootVM(args: args)
        } catch {
            FileHandle.standardError.write(Data("Error: \(error)\n".utf8))
            exit(1)
        }
        exit(0)
    }
    dispatchMain()  // Runs the main dispatch queue, allowing @MainActor tasks to execute

case "version":
    print("vm-launcher 0.1.0 (SiliconSandbox)")
    print("Swift \(swiftVersion())")
    print("Virtualization.framework available: true")

default:
    printUsage()
    exit(1)
}

func swiftVersion() -> String {
    #if swift(>=6.0)
    return "6.x"
    #else
    return "5.x"
    #endif
}
