// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Kumo",
    platforms: [
        .iOS(.v16)
    ],
    products: [
        .library(name: "KumoCore", targets: ["KumoCore"]),
        .library(name: "KumoApp", targets: ["KumoApp"])
    ],
    targets: [
        .target(
            name: "KumoCore",
            path: "Sources/KumoCore"
        ),
        .target(
            name: "KumoApp",
            dependencies: ["KumoCore"],
            path: "Sources/KumoApp"
        ),
        .testTarget(
            name: "KumoCoreTests",
            dependencies: ["KumoCore"],
            path: "Tests/KumoCoreTests"
        )
    ]
)