// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "AgentPlayCompanion",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "AgentPlayCompanion", targets: ["AgentPlayCompanion"]),
    ],
    dependencies: [
        // Updates from the GitHub release's appcast, signed with the project's EdDSA key. Pinned.
        .package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.10.1"),
    ],
    targets: [
        // Pure logic: snapshot contract, alert policy, deep links, redaction. No UI, fully unit tested.
        .target(name: "AgentPlayCore", path: "Sources/AgentPlayCore"),
        .executableTarget(
            name: "AgentPlayCompanion",
            dependencies: ["AgentPlayCore", .product(name: "Sparkle", package: "Sparkle")],
            path: "Sources/AgentPlayCompanion"
        ),
        .testTarget(name: "AgentPlayCoreTests", dependencies: ["AgentPlayCore"], path: "Tests/AgentPlayCoreTests"),
    ],
    swiftLanguageModes: [.v5]
)
