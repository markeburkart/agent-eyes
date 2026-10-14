import AVFoundation

enum ServiceError: Error {
    case badRequest(String)
    case unknownCamera(String)
    case unavailable(String)
    case noFrames(String)
    case encodeFailed

    var status: Int {
        switch self {
        case .badRequest: return 400
        case .unknownCamera: return 404
        case .unavailable: return 503
        case .noFrames: return 504
        case .encodeFailed: return 500
        }
    }

    var code: String {
        switch self {
        case .badRequest: return "bad_request"
        case .unknownCamera: return "unknown_camera"
        case .unavailable: return "camera_unavailable"
        case .noFrames: return "no_frames"
        case .encodeFailed: return "encode_failed"
        }
    }

    var message: String {
        switch self {
        case .badRequest(let m): return m
        case .unknownCamera(let r): return "No camera matches '\(r)'. It may be disconnected."
        case .unavailable(let n): return "Camera '\(n)' could not be opened."
        case .noFrames(let n): return "Camera '\(n)' delivered no fresh frames."
        case .encodeFailed: return "Failed to encode image."
        }
    }
}

struct CaptureRequest {
    var camera: String
    var region: (x: Double, y: Double, w: Double, h: Double)?
    var maxEdge: Int = 1568
    var quality: Double = 0.85
}

/// Owns one CameraStream per device, opening them on demand and keeping them open.
///
/// Recovery: a Continuity Camera that drops (phone picked up, unlocked, out of range) can leave its
/// session "running" with no frames and no notification, so a watchdog restarts any wanted stream
/// that has gone quiet. Device/session notifications are logged and also trigger recovery.
final class CaptureService {
    let registry: CameraRegistry
    /// Only frames this recent are eligible, so a stalled stream never returns an old picture.
    let maxFrameAge: TimeInterval = 1.5
    /// A wanted stream with no frames for this long is torn down and reopened.
    let stallTimeout: TimeInterval = 3.0

    private let lock = NSLock()
    private var streams: [String: CameraStream] = [:]
    /// uniqueIDs that should be streaming; survives disconnects so we can reopen.
    private var wanted: Set<String> = []
    private var observers: [NSObjectProtocol] = []
    private var watchdog: DispatchSourceTimer?
    private let recoveryQueue = DispatchQueue(label: "camsvc.recovery")

    init(registry: CameraRegistry) {
        self.registry = registry
        let nc = NotificationCenter.default
        let deviceEvents: [(Notification.Name, String)] = [
            (AVCaptureDevice.wasDisconnectedNotification, "disconnected"),
            (AVCaptureDevice.wasConnectedNotification, "connected"),
        ]
        for (name, label) in deviceEvents {
            observers.append(nc.addObserver(forName: name, object: nil, queue: nil) { [weak self] note in
                guard let d = note.object as? AVCaptureDevice else { return }
                log("device \(label): \(d.localizedName)")
                self?.recoveryQueue.async { self?.recover(d.uniqueID) }
            })
        }
        let sessionEvents: [(Notification.Name, String)] = [
            (AVCaptureSession.runtimeErrorNotification, "runtime error"),
            (AVCaptureSession.wasInterruptedNotification, "interrupted"),
            (AVCaptureSession.interruptionEndedNotification, "interruption ended"),
            (AVCaptureSession.didStopRunningNotification, "stopped"),
        ]
        for (name, label) in sessionEvents {
            observers.append(nc.addObserver(forName: name, object: nil, queue: nil) { [weak self] note in
                guard let self, let session = note.object as? AVCaptureSession,
                      let id = self.lock.withLock({ self.streams.first { $0.value.session === session }?.key }) else { return }
                let err = (note.userInfo?[AVCaptureSessionErrorKey] as? Error).map { ": \($0)" } ?? ""
                log("session \(label) on \(id)\(err)")
                self.recoveryQueue.async {
                    // Ignore events from a session that has already been replaced (including our own stops).
                    guard self.lock.withLock({ self.streams[id]?.session === session }) else { return }
                    self.recover(id)
                }
            })
        }
        let timer = DispatchSource.makeTimerSource(queue: recoveryQueue)
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { [weak self] in self?.checkStalls() }
        timer.resume()
        watchdog = timer
    }

    func cameras() -> [[String: Any]] {
        let open = lock.withLock { streams }
        return registry.devices().map { d in
            var info: [String: Any] = [
                "id": d.uniqueID,
                "name": d.localizedName,
                "kind": registry.kind(of: d).rawValue,
                "connected": d.isConnected,
                "streaming": open[d.uniqueID]?.isRunning ?? false,
            ]
            if let role = registry.role(of: d) { info["role"] = role }
            if let size = open[d.uniqueID]?.lastFrameSize { info["frame_width"] = size.width; info["frame_height"] = size.height }
            if let last = open[d.uniqueID]?.lastFrameAt { info["last_frame_age_ms"] = Int(Date().timeIntervalSince(last) * 1000) }
            info["suspended"] = d.isSuspended
            return info
        }
    }

    /// Opens (or returns) the stream for a camera reference without capturing.
    @discardableResult
    func open(_ ref: String) throws -> CameraStream {
        guard let device = registry.resolve(ref) else { throw ServiceError.unknownCamera(ref) }
        return try stream(for: device)
    }

    func capture(_ req: CaptureRequest) throws -> [String: Any] {
        guard let device = registry.resolve(req.camera) else { throw ServiceError.unknownCamera(req.camera) }
        var stream = try stream(for: device)
        if !stream.waitForFrames(3, maxAge: maxFrameAge, timeout: 4) {
            // Don't wait for the watchdog: reopen now and give the camera one more chance.
            log("no fresh frames from \(device.localizedName) on capture; reopening")
            recoveryQueue.sync { recover(device.uniqueID, force: true) }
            stream = try self.stream(for: device)
            guard stream.waitForFrames(3, maxAge: maxFrameAge, timeout: 6) else { throw ServiceError.noFrames(device.localizedName) }
        }
        let frames = stream.recentFrames(maxAge: maxFrameAge)
        guard let first = frames.first else { throw ServiceError.noFrames(device.localizedName) }

        let width = CVPixelBufferGetWidth(first.pixelBuffer), height = CVPixelBufferGetHeight(first.pixelBuffer)
        let rect = PixelRect.from(normalized: req.region, width: width, height: height)
        // Score each buffered frame on the requested region only: the region is what has to be sharp.
        let scored = frames
            .filter { CVPixelBufferGetWidth($0.pixelBuffer) == width && CVPixelBufferGetHeight($0.pixelBuffer) == height }
            .map { ($0, laplacianVariance($0.pixelBuffer, in: rect)) }
        guard let (best, sharpness) = scored.max(by: { $0.1 < $1.1 }) else { throw ServiceError.noFrames(device.localizedName) }
        guard let jpeg = encodeJPEG(best.pixelBuffer, crop: rect, maxEdge: req.maxEdge, quality: req.quality) else {
            throw ServiceError.encodeFailed
        }
        return [
            "camera": ["id": device.uniqueID, "name": device.localizedName, "kind": registry.kind(of: device).rawValue],
            "source": ["width": width, "height": height],
            "crop": ["x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height],
            "region": [
                "x": Double(rect.x) / Double(width), "y": Double(rect.y) / Double(height),
                "w": Double(rect.width) / Double(width), "h": Double(rect.height) / Double(height),
            ],
            "image": ["width": jpeg.width, "height": jpeg.height, "mime": "image/jpeg", "base64": jpeg.data.base64EncodedString()],
            "sharpness": sharpness,
            "candidate_sharpness": scored.map(\.1),
            "frame_age_ms": Int(Date().timeIntervalSince(best.capturedAt) * 1000),
            "captured_at": ISO8601DateFormatter().string(from: best.capturedAt),
        ]
    }

    private func stream(for device: AVCaptureDevice) throws -> CameraStream {
        lock.lock()
        defer { lock.unlock() }
        wanted.insert(device.uniqueID)
        if let s = streams[device.uniqueID], s.isRunning { return s }
        streams.removeValue(forKey: device.uniqueID)?.stop()
        let s = CameraStream(device: device)
        try s.start()
        streams[device.uniqueID] = s
        return s
    }

    // MARK: - Recovery (runs on recoveryQueue)

    /// Minimum spacing between reopen attempts per camera, so a missing phone isn't hammered.
    private let retryInterval: TimeInterval = 2.0
    private var lastAttempt: [String: Date] = [:]

    private func checkStalls() {
        let now = Date()
        let ids = lock.withLock { wanted }
        for id in ids {
            let s = lock.withLock { streams[id] }
            guard let s else { recover(id); continue }  // previous reopen failed; keep trying
            let quietFor = now.timeIntervalSince(s.lastFrameAt ?? s.startedAt)
            if quietFor > stallTimeout {
                log("stream stalled (\(String(format: "%.1f", quietFor))s without frames): \(s.device.localizedName)")
                recover(id)
            }
        }
    }

    /// Tears down the stream for a wanted camera and reopens it if the device is present.
    private func recover(_ id: String, force: Bool = false) {
        guard lock.withLock({ wanted.contains(id) }) else { return }
        if !force, let last = lastAttempt[id], Date().timeIntervalSince(last) < retryInterval { return }
        lastAttempt[id] = Date()
        if let old = lock.withLock({ streams.removeValue(forKey: id) }) { old.stop() }
        guard let device = registry.devices().first(where: { $0.uniqueID == id }), device.isConnected else {
            return  // still gone; checkStalls retries
        }
        do {
            let s = try stream(for: device)
            log("stream reopened: \(s.device.localizedName)")
        } catch {
            log("reopen failed for \(device.localizedName): \(error)")
        }
    }
}
