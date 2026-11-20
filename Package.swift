// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "LeapKoi",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "LeapKoi", targets: ["LeapKoi"])],
    targets: [
        .executableTarget(name: "LeapKoi", path: "Sources/LeapKoi"),
        .testTarget(name: "LeapKoiTests", dependencies: ["LeapKoi"], path: "Tests/LeapKoiTests")
    ],
    swiftLanguageModes: [.v5]
)
