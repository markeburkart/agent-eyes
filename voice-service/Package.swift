// swift-tools-version:5.10
import Foundation
import PackageDescription

// The Info.plist is linked into the binary so macOS knows the microphone and speech usage strings.
let infoPlist = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("Info.plist").path

let package = Package(
    name: "voice-service",
    // SpeechAnalyzer (on-device, long-form transcription) is macOS 26+.
    platforms: [.macOS("26.0")],
    targets: [
        .executableTarget(
            name: "voicesvc",
            linkerSettings: [
                .unsafeFlags(["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist", "-Xlinker", infoPlist]),
            ]
        ),
    ]
)
