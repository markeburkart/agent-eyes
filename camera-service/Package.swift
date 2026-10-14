// swift-tools-version:5.10
import Foundation
import PackageDescription

// The Info.plist is linked into the binary so macOS knows the camera usage string and
// reports iPhones as Continuity Cameras (instead of generic built-in cameras).
let infoPlist = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("Info.plist").path

let package = Package(
    name: "camera-service",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "camsvc",
            linkerSettings: [
                .unsafeFlags(["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist", "-Xlinker", infoPlist]),
            ]
        ),
    ]
)
