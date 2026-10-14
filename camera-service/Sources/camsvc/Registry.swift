import AVFoundation

enum CameraKind: String {
    case continuity
    case deskView = "desk_view"
    case external
    case builtIn = "built_in"
}

/// Discovers video devices and resolves camera references ("front", "desk", a uniqueID, or a name).
/// The Mac's own cameras are hidden unless `allowBuiltIn` is set.
final class CameraRegistry {
    let allowBuiltIn: Bool

    init(allowBuiltIn: Bool) { self.allowBuiltIn = allowBuiltIn }

    func devices() -> [AVCaptureDevice] {
        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.continuityCamera, .deskViewCamera, .external, .builtInWideAngleCamera],
            mediaType: .video, position: .unspecified)
        return discovery.devices.filter { allowBuiltIn || !isMacCamera($0) }
    }

    func kind(of d: AVCaptureDevice) -> CameraKind {
        switch d.deviceType {
        case .continuityCamera: return .continuity
        case .deskViewCamera: return .deskView
        case .external: return .external
        default: return .builtIn
        }
    }

    /// Roles are stable aliases the agent can use without knowing device IDs.
    func role(of d: AVCaptureDevice) -> String? {
        let all = devices()
        if kind(of: d) == .continuity, all.first(where: { kind(of: $0) == .continuity }) == d { return "front" }
        if kind(of: d) == .deskView, all.first(where: { kind(of: $0) == .deskView }) == d { return "desk" }
        return nil
    }

    func resolve(_ ref: String) -> AVCaptureDevice? {
        let all = devices()
        switch ref {
        case "front": return all.first { kind(of: $0) == .continuity }
        case "desk": return all.first { kind(of: $0) == .deskView }
        default: return all.first { $0.uniqueID == ref } ?? all.first { $0.localizedName == ref }
        }
    }

    /// Recent MacBooks expose their own Desk View camera alongside the built-in one.
    private func isMacCamera(_ d: AVCaptureDevice) -> Bool {
        if d.deviceType == .builtInWideAngleCamera { return true }
        return d.deviceType == .deskViewCamera && (d.modelID.hasPrefix("Mac") || d.localizedName.hasPrefix("Mac"))
    }
}
