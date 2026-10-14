// Continuity Camera capability probe.
// Lists video devices (Continuity Camera, Desk View, external, built-in), their formats,
// photo dimensions and focus/zoom controls, then captures one video frame and one
// max-resolution still per device into the output directory.
//
// Usage: camprobe [--list-only] [--iphone] [--out DIR] [--device UNIQUE_ID_SUBSTRING]
//   --iphone  only Continuity Camera and Desk View devices (built-in/USB cameras are never opened)

import AVFoundation
import CoreImage
import Foundation
import ImageIO
import UniformTypeIdentifiers

// MARK: - Args

var listOnly = false
var outDir = URL(fileURLWithPath: "spike/out")
var deviceFilter: String?
var iphoneOnly = false
do {
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let a = it.next() {
        switch a {
        case "--list-only": listOnly = true
        case "--iphone": iphoneOnly = true
        case "--out": outDir = URL(fileURLWithPath: it.next()!)
        case "--device": deviceFilter = it.next()
        default: fatalError("unknown arg \(a)")
        }
    }
}

func log(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

// MARK: - Permission

func ensureCameraAccess() -> Bool {
    switch AVCaptureDevice.authorizationStatus(for: .video) {
    case .authorized: return true
    case .notDetermined:
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        AVCaptureDevice.requestAccess(for: .video) { ok = $0; sem.signal() }
        sem.wait()
        return ok
    default: return false
    }
}

// MARK: - Description helpers

func fourCC(_ v: FourCharCode) -> String {
    let bytes = [24, 16, 8, 0].map { UInt8((v >> $0) & 0xff) }
    return String(bytes: bytes, encoding: .ascii) ?? "\(v)"
}

func dimsString(_ d: CMVideoDimensions) -> String { "\(d.width)x\(d.height)" }

func mp(_ d: CMVideoDimensions) -> String { String(format: "%.1fMP", Double(d.width) * Double(d.height) / 1e6) }

func focusModes(_ d: AVCaptureDevice) -> [String] {
    [(AVCaptureDevice.FocusMode.locked, "locked"), (.autoFocus, "auto"), (.continuousAutoFocus, "continuous")]
        .filter { d.isFocusModeSupported($0.0) }.map { $0.1 }
}

func exposureModes(_ d: AVCaptureDevice) -> [String] {
    [(AVCaptureDevice.ExposureMode.locked, "locked"), (.autoExpose, "auto"), (.continuousAutoExposure, "continuous"), (.custom, "custom")]
        .filter { d.isExposureModeSupported($0.0) }.map { $0.1 }
}

func describe(_ d: AVCaptureDevice) {
    print("=== \(d.localizedName)")
    print("  uniqueID:      \(d.uniqueID)")
    print("  modelID:       \(d.modelID)  manufacturer: \(d.manufacturer)")
    print("  deviceType:    \(d.deviceType.rawValue)")
    print("  connected:     \(d.isConnected)  suspended: \(d.isSuspended)")
    print("  focus modes:   \(focusModes(d))  focusPOI: \(d.isFocusPointOfInterestSupported)")
    print("  exposure:      \(exposureModes(d))  exposurePOI: \(d.isExposurePointOfInterestSupported)")
    print("  centerStage:   active=\(d.isCenterStageActive)  (AVCaptureDevice.isCenterStageEnabled=\(AVCaptureDevice.isCenterStageEnabled))")
    print("  zoom:          videoZoomFactor API unavailable on macOS; displayVideoZoomFactorMultiplier=\(d.displayVideoZoomFactorMultiplier)")
    print("  activeFormat:  \(formatLine(d.activeFormat))")
    print("  formats (\(d.formats.count)):")
    for f in d.formats { print("    - \(formatLine(f))") }
}

func formatLine(_ f: AVCaptureDevice.Format) -> String {
    let desc = f.formatDescription
    let dims = CMVideoFormatDescriptionGetDimensions(desc)
    let fps = f.videoSupportedFrameRateRanges.map { String(format: "%.0f-%.0f", $0.minFrameRate, $0.maxFrameRate) }.joined(separator: ",")
    let photo = f.supportedMaxPhotoDimensions.map(dimsString).joined(separator: ",")
    var extras: [String] = []
    if f.isCenterStageSupported { extras.append("centerStage(maxZoom \(f.videoMaxZoomFactorForCenterStage))") }
    if f.isHighPhotoQualitySupported { extras.append("highPhotoQuality") }
    if !f.secondaryNativeResolutionZoomFactors.isEmpty { extras.append("secondaryZooms \(f.secondaryNativeResolutionZoomFactors)") }
    extras.append("AF=\(f.autoFocusSystem.rawValue)")
    return "\(dimsString(dims)) \(fourCC(CMFormatDescriptionGetMediaSubType(desc))) fps[\(fps)] photo[\(photo)] \(extras.joined(separator: " "))"
}

// MARK: - Capture

final class Grabber: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate, AVCapturePhotoCaptureDelegate {
    let session = AVCaptureSession()
    let videoOut = AVCaptureVideoDataOutput()
    let photoOut = AVCapturePhotoOutput()
    let queue = DispatchQueue(label: "probe.video")
    var framesSeen = 0
    var frame: CVPixelBuffer?
    var frameSem = DispatchSemaphore(value: 0)
    var photoData: Data?
    var photoErr: Error?
    var photoSem = DispatchSemaphore(value: 0)

    func captureOutput(_ output: AVCaptureOutput, didOutput sb: CMSampleBuffer, from connection: AVCaptureConnection) {
        framesSeen += 1
        // Skip warm-up frames so auto-exposure/focus settle.
        if framesSeen == 45, let pb = CMSampleBufferGetImageBuffer(sb) {
            frame = pb
            frameSem.signal()
        }
    }

    func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        photoErr = error
        photoData = photo.fileDataRepresentation()
        photoSem.signal()
    }
}

func save(_ pb: CVPixelBuffer, to url: URL) {
    let ci = CIImage(cvPixelBuffer: pb)
    let ctx = CIContext()
    try! ctx.writeJPEGRepresentation(of: ci, to: url, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, options: [:])
}

func imageInfo(_ url: URL) -> String {
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any] else { return "?" }
    let w = props[kCGImagePropertyPixelWidth] ?? "?"
    let h = props[kCGImagePropertyPixelHeight] ?? "?"
    let exif = props[kCGImagePropertyExifDictionary] as? [CFString: Any]
    let lens = exif?[kCGImagePropertyExifLensModel] ?? "-"
    let focal = exif?[kCGImagePropertyExifFocalLength] ?? "-"
    return "\(w)x\(h) lens=\(lens) focal=\(focal)"
}

func capture(_ device: AVCaptureDevice, index: Int) {
    let tag = "\(index)-" + device.localizedName.replacingOccurrences(of: "[^A-Za-z0-9]+", with: "_", options: .regularExpression)
    print("\n--- capture: \(device.localizedName)")
    let g = Grabber()
    let s = g.session
    s.beginConfiguration()
    guard let input = try? AVCaptureDeviceInput(device: device), s.canAddInput(input) else {
        print("  cannot add input"); return
    }
    s.addInput(input)
    g.videoOut.setSampleBufferDelegate(g, queue: g.queue)
    g.videoOut.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
    if s.canAddOutput(g.videoOut) { s.addOutput(g.videoOut) }
    let canPhoto = s.canAddOutput(g.photoOut)
    if canPhoto { s.addOutput(g.photoOut) }

    // Pick the format offering the largest photo, ties broken by larger video.
    func photoArea(_ f: AVCaptureDevice.Format) -> Int64 {
        f.supportedMaxPhotoDimensions.map { Int64($0.width) * Int64($0.height) }.max() ?? 0
    }
    func videoArea(_ f: AVCaptureDevice.Format) -> Int64 {
        let d = CMVideoFormatDescriptionGetDimensions(f.formatDescription); return Int64(d.width) * Int64(d.height)
    }
    s.commitConfiguration()

    let t0 = Date()
    s.startRunning()
    print(String(format: "  startRunning:  %.2fs", Date().timeIntervalSince(t0)))

    // startRunning applies the session preset (resetting activeFormat), so pick the format afterwards.
    if let best = device.formats.max(by: { (photoArea($0), videoArea($0)) < (photoArea($1), videoArea($1)) }) {
        do {
            try device.lockForConfiguration()
            device.activeFormat = best
            if device.isFocusModeSupported(.continuousAutoFocus) { device.focusMode = .continuousAutoFocus }
            device.unlockForConfiguration()
        } catch { print("  lockForConfiguration failed: \(error)") }
    }
    g.queue.sync { g.framesSeen = 0 }
    var maxPhoto = CMVideoDimensions(width: 0, height: 0)
    if canPhoto, let d = device.activeFormat.supportedMaxPhotoDimensions.max(by: { $0.width * $0.height < $1.width * $1.height }) {
        g.photoOut.maxPhotoDimensions = d
        g.photoOut.maxPhotoQualityPrioritization = .quality
        maxPhoto = g.photoOut.maxPhotoDimensions
    }
    print("  session preset: \(s.sessionPreset.rawValue)  photo maxQualityPrioritization=\(g.photoOut.maxPhotoQualityPrioritization.rawValue)")
    print("  using format:  \(formatLine(device.activeFormat))")
    print("  photo output:  canAdd=\(canPhoto) maxPhotoDimensions=\(dimsString(maxPhoto)) (\(mp(maxPhoto))) codecs=\(g.photoOut.availablePhotoCodecTypes.map(\.rawValue))")

    if g.frameSem.wait(timeout: .now() + 10) == .success, let pb = g.frame {
        let url = outDir.appendingPathComponent("\(tag)-frame.jpg")
        save(pb, to: url)
        print(String(format: "  video frame:   %dx%d after %.2fs -> %@", CVPixelBufferGetWidth(pb), CVPixelBufferGetHeight(pb), Date().timeIntervalSince(t0), url.path))
    } else {
        print("  video frame:   TIMEOUT (frames seen: \(g.framesSeen))")
    }

    if canPhoto {
        let settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg])
        // Never request more than the format that is actually active right now (an invalid size throws).
        let supported = device.activeFormat.supportedMaxPhotoDimensions
        if !supported.contains(where: { $0.width == maxPhoto.width && $0.height == maxPhoto.height }) {
            print("  WARNING: active format changed to \(formatLine(device.activeFormat)); requesting its largest photo instead")
            maxPhoto = supported.max(by: { $0.width * $0.height < $1.width * $1.height }) ?? maxPhoto
        }
        settings.maxPhotoDimensions = maxPhoto
        settings.photoQualityPrioritization = g.photoOut.maxPhotoQualityPrioritization
        let t1 = Date()
        g.photoOut.capturePhoto(with: settings, delegate: g)
        if g.photoSem.wait(timeout: .now() + 15) == .success, let data = g.photoData {
            let url = outDir.appendingPathComponent("\(tag)-still.jpg")
            try! data.write(to: url)
            print(String(format: "  still:         %@ in %.2fs, %d KB -> %@", imageInfo(url), Date().timeIntervalSince(t1), data.count / 1024, url.path))
        } else {
            print("  still:         FAILED \(g.photoErr.map { "\($0)" } ?? "timeout")")
        }
    }
    s.stopRunning()
}

// MARK: - Main

guard ensureCameraAccess() else {
    log("Camera access denied. Grant it in System Settings > Privacy & Security > Camera for the terminal app running this probe.")
    exit(2)
}

let types: [AVCaptureDevice.DeviceType] = iphoneOnly
    ? [.continuityCamera, .deskViewCamera]
    : [.continuityCamera, .deskViewCamera, .external, .builtInWideAngleCamera]
let discovery = AVCaptureDevice.DiscoverySession(deviceTypes: types, mediaType: .video, position: .unspecified)
var devices = discovery.devices
if iphoneOnly {
    // Recent MacBooks expose their own Desk View camera; only keep Desk View when an iPhone is
    // present, and never one that belongs to the Mac.
    let hasIPhone = devices.contains { $0.deviceType == .continuityCamera }
    devices = devices.filter {
        $0.deviceType == .continuityCamera || (hasIPhone && !$0.modelID.hasPrefix("Mac") && !$0.localizedName.hasPrefix("Mac"))
    }
}
print("Found \(devices.count) video device(s)")
for d in devices { describe(d) }

// Run captures off the main thread: photo delegate callbacks may need the main run loop.
DispatchQueue.global().async {
    if !listOnly {
        try! FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
        for (i, d) in devices.enumerated() where deviceFilter.map({ d.uniqueID.contains($0) || d.localizedName.contains($0) }) ?? true {
            capture(d, index: i)
        }
    }
    exit(0)
}
dispatchMain()
