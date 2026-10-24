// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "AgentPlayCompanion",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "AgentPlayCompanion", targets: ["AgentPlayCompanion"]),
    ],
    targets: [
        // Pure logic: snapshot contract, alert policy, deep links, redaction. No UI, fully unit tested.
        .target(name: "AgentPlayCore", path: "Sources/AgentPlayCore"),
        .executableTarget(
            name: "AgentPlayCompanion",
            dependencies: ["AgentPlayCore"],
            path: "Sources/AgentPlayCompanion"
        ),
        .testTarget(name: "AgentPlayCoreTests", dependencies: ["AgentPlayCore"], path: "Tests/AgentPlayCoreTests"),
    ],
    swiftLanguageModes: [.v5]
)
