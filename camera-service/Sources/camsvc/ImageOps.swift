import CoreImage
import CoreVideo
import ImageIO

/// Pixel rectangle in top-left-origin image coordinates.
struct PixelRect {
    var x: Int, y: Int, width: Int, height: Int

    /// Converts a normalized (0...1, top-left origin) region into pixels, clamped to the frame.
    static func from(normalized r: (x: Double, y: Double, w: Double, h: Double)?, width: Int, height: Int) -> PixelRect {
        guard let r else { return PixelRect(x: 0, y: 0, width: width, height: height) }
        let x0 = max(0, min(1, r.x)), y0 = max(0, min(1, r.y))
        let x1 = max(x0, min(1, r.x + r.w)), y1 = max(y0, min(1, r.y + r.h))
        let px = Int((x0 * Double(width)).rounded()), py = Int((y0 * Double(height)).rounded())
        let pw = max(8, Int((x1 * Double(width)).rounded()) - px), ph = max(8, Int((y1 * Double(height)).rounded()) - py)
        return PixelRect(x: min(px, width - pw), y: min(py, height - ph), width: min(pw, width), height: min(ph, height))
    }
}

/// Variance of the 4-neighbour Laplacian over the luma plane, sampled on a grid so cost stays
/// bounded (~250k samples) regardless of region size. Neighbours are always adjacent pixels,
/// so the score reflects full-resolution sharpness.
func laplacianVariance(_ pb: CVPixelBuffer, in rect: PixelRect) -> Double {
    CVPixelBufferLockBaseAddress(pb, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
    let planar = CVPixelBufferIsPlanar(pb)
    guard let base = planar ? CVPixelBufferGetBaseAddressOfPlane(pb, 0) : CVPixelBufferGetBaseAddress(pb) else { return 0 }
    let stride = planar ? CVPixelBufferGetBytesPerRowOfPlane(pb, 0) : CVPixelBufferGetBytesPerRow(pb)
    let px = base.assumingMemoryBound(to: UInt8.self)
    let step = max(1, Int((Double(rect.width * rect.height) / 250_000).squareRoot()))
    var sum = 0.0, sumSq = 0.0, n = 0.0
    var y = rect.y + 1
    while y < rect.y + rect.height - 1 {
        let row = px + y * stride
        var x = rect.x + 1
        while x < rect.x + rect.width - 1 {
            let c = Int(row[x]) * 4
            let l = c - Int(row[x - 1]) - Int(row[x + 1]) - Int(row[x - stride]) - Int(row[x + stride])
            let v = Double(l)
            sum += v; sumSq += v * v; n += 1
            x += step
        }
        y += step
    }
    guard n > 0 else { return 0 }
    let mean = sum / n
    return sumSq / n - mean * mean
}

private let ciContext = CIContext(options: [.cacheIntermediates: false])
private let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!

/// Crops `rect` out of the frame, downscales so the long edge is at most `maxEdge` (never upscales),
/// and encodes JPEG.
func encodeJPEG(_ pb: CVPixelBuffer, crop rect: PixelRect, maxEdge: Int, quality: Double) -> (data: Data, width: Int, height: Int)? {
    let image = CIImage(cvPixelBuffer: pb)
    let frameHeight = CVPixelBufferGetHeight(pb)
    // Core Image uses a bottom-left origin.
    let ciRect = CGRect(x: rect.x, y: frameHeight - rect.y - rect.height, width: rect.width, height: rect.height)
    var out = image.cropped(to: ciRect).transformed(by: CGAffineTransform(translationX: -ciRect.minX, y: -ciRect.minY))
    let scale = min(1.0, Double(maxEdge) / Double(max(rect.width, rect.height)))
    if scale < 1.0 {
        let f = CIFilter(name: "CILanczosScaleTransform")!
        f.setValue(out, forKey: kCIInputImageKey)
        f.setValue(scale, forKey: kCIInputScaleKey)
        f.setValue(1.0, forKey: kCIInputAspectRatioKey)
        out = f.outputImage!
    }
    let w = Int((Double(rect.width) * scale).rounded()), h = Int((Double(rect.height) * scale).rounded())
    out = out.cropped(to: CGRect(x: 0, y: 0, width: w, height: h))
    let opts = [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): quality]
    guard let data = ciContext.jpegRepresentation(of: out, colorSpace: sRGB, options: opts) else { return nil }
    return (data, w, h)
}
