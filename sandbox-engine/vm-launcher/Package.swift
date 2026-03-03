// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "VMLauncher",
    platforms: [.macOS(.v15)],
    targets: [
        .executableTarget(
            name: "vm-launcher",
            path: "Sources/VMLauncher",
            linkerSettings: [
                .linkedFramework("Virtualization"),
            ]
        ),
    ]
)
