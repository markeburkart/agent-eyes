import AVFoundation

struct Frame {
    let pixelBuffer: CVPixelBuffer
    let capturedAt: Date
}

/// An open capture session for one device that keeps a short ring buffer of recent frames.
/// Frames are deep-copied so the ring never starves the capture pipeline's buffer pool.
final class CameraStream: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let device: AVCaptureDevice
    let session = AVCaptureSession()
    private let output = AVCaptureVideoDataOutput()
    private let queue: DispatchQueue

    /// Keep every `sampleEvery`-th frame; `capacity` frames cover roughly capacity*sampleEvery/fps seconds.
    let capacity: Int
    let sampleEvery: Int
    /// Frames dropped after start so auto-exposure and focus can settle.
    let warmupFrames: Int

    private let lock = NSLock()
    private var ring: [Frame] = []
    private var frameCount = 0
    private var _lastFrameAt: Date?
    private(set) var startedAt = Date()

    /// Arrival time of the most recent frame (any frame, sampled or not); nil before the first one.
    var lastFrameAt: Date? { lock.withLock { _lastFrameAt } }

    init(device: AVCaptureDevice, capacity: Int = 12, sampleEvery: Int = 3, warmupFrames: Int = 20) {
        self.device = device
        self.capacity = capacity
        self.sampleEvery = sampleEvery
        self.warmupFrames = warmupFrames
        self.queue = DispatchQueue(label: "camsvc.stream.\(device.uniqueID)")
    }

    func start() throws {
        let input = try AVCaptureDeviceInput(device: device)
        session.beginConfiguration()
        guard session.canAddInput(input) else { session.commitConfiguration(); throw ServiceError.unavailable(device.localizedName) }
        session.addInput(input)
        // Bi-planar YUV: plane 0 is luma, which the sharpness metric reads directly.
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange]
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        guard session.canAddOutput(output) else { session.commitConfiguration(); throw ServiceError.unavailable(device.localizedName) }
        session.addOutput(output)
        session.commitConfiguration()
        startedAt = Date()
        session.startRunning()
        // startRunning applies the session preset, which resets activeFormat, so choose the format afterwards.
        selectLargestFormat()
        log("stream started: \(device.localizedName) \(dims(device.activeFormat))")
    }

    func stop() {
        session.stopRunning()
        lock.withLock { ring.removeAll() }
    }

    var isRunning: Bool { session.isRunning }

    /// Frames captured within `maxAge` seconds, oldest first.
    func recentFrames(maxAge: TimeInterval) -> [Frame] {
        let cutoff = Date().addingTimeInterval(-maxAge)
        return lock.withLock { ring.filter { $0.capturedAt >= cutoff } }
    }

    /// Blocks until at least `count` fresh frames are buffered or the timeout passes.
    func waitForFrames(_ count: Int, maxAge: TimeInterval, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if recentFrames(maxAge: maxAge).count >= count { return true }
            Thread.sleep(forTimeInterval: 0.05)
        }
        return false
    }

    var lastFrameSize: (width: Int, height: Int)? {
        lock.withLock { ring.last.map { (CVPixelBufferGetWidth($0.pixelBuffer), CVPixelBufferGetHeight($0.pixelBuffer)) } }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        frameCount += 1
        lock.withLock { _lastFrameAt = Date() }
        guard frameCount > warmupFrames, frameCount % sampleEvery == 0,
              let pb = CMSampleBufferGetImageBuffer(sampleBuffer), let copy = deepCopy(pb) else { return }
        let frame = Frame(pixelBuffer: copy, capturedAt: Date())
        lock.withLock {
            ring.append(frame)
            if ring.count > capacity { ring.removeFirst(ring.count - capacity) }
        }
    }

    private func selectLargestFormat() {
        func area(_ f: AVCaptureDevice.Format) -> Int32 {
            let d = CMVideoFormatDescriptionGetDimensions(f.formatDescription); return d.width * d.height
        }
        func maxFPS(_ f: AVCaptureDevice.Format) -> Double { f.videoSupportedFrameRateRanges.map(\.maxFrameRate).max() ?? 0 }
        // Largest frame; among equals prefer the lower max frame rate (better exposure in bench lighting).
        guard let best = device.formats.max(by: { (area($0), -maxFPS($0)) < (area($1), -maxFPS($1)) }),
              best != device.activeFormat else { return }
        do {
            try device.lockForConfiguration()
            device.activeFormat = best
            device.unlockForConfiguration()
        } catch {
            log("could not set format on \(device.localizedName): \(error)")
        }
    }

    private func dims(_ f: AVCaptureDevice.Format) -> String {
        let d = CMVideoFormatDescriptionGetDimensions(f.formatDescription); return "\(d.width)x\(d.height)"
    }
}

private func deepCopy(_ src: CVPixelBuffer) -> CVPixelBuffer? {
    var dst: CVPixelBuffer?
    let attrs = [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary
    guard CVPixelBufferCreate(nil, CVPixelBufferGetWidth(src), CVPixelBufferGetHeight(src),
                              CVPixelBufferGetPixelFormatType(src), attrs, &dst) == kCVReturnSuccess,
          let dst else { return nil }
    CVPixelBufferLockBaseAddress(src, .readOnly)
    CVPixelBufferLockBaseAddress(dst, [])
    defer {
        CVPixelBufferUnlockBaseAddress(dst, [])
        CVPixelBufferUnlockBaseAddress(src, .readOnly)
    }
    if CVPixelBufferIsPlanar(src) {
        for p in 0..<CVPixelBufferGetPlaneCount(src) {
            copyRows(from: CVPixelBufferGetBaseAddressOfPlane(src, p), srcStride: CVPixelBufferGetBytesPerRowOfPlane(src, p),
                     to: CVPixelBufferGetBaseAddressOfPlane(dst, p), dstStride: CVPixelBufferGetBytesPerRowOfPlane(dst, p),
                     rows: CVPixelBufferGetHeightOfPlane(src, p))
        }
    } else {
        copyRows(from: CVPixelBufferGetBaseAddress(src), srcStride: CVPixelBufferGetBytesPerRow(src),
                 to: CVPixelBufferGetBaseAddress(dst), dstStride: CVPixelBufferGetBytesPerRow(dst),
                 rows: CVPixelBufferGetHeight(src))
    }
    CVBufferPropagateAttachments(src, dst)
    return dst
}

private func copyRows(from src: UnsafeMutableRawPointer?, srcStride: Int, to dst: UnsafeMutableRawPointer?, dstStride: Int, rows: Int) {
    guard let src, let dst else { return }
    let n = min(srcStride, dstStride)
    for r in 0..<rows { memcpy(dst + r * dstStride, src + r * srcStride, n) }
}
