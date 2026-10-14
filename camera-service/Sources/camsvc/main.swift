// camsvc: local camera service for Agent Eyes.
//
// Keeps video streams open with a short ring buffer, returns the sharpest recent frame
// (optionally cropped to a region) as JPEG over a loopback HTTP API.
//
// Usage: camsvc [--port N] [--open front,desk] [--allow-builtin]
//   --port N         listen on 127.0.0.1:N (default 0 = pick a free port)
//   --open ROLES     comma-separated cameras to open at startup (roles, IDs or names)
//   --allow-builtin  also expose the Mac's own cameras (hidden by default)
//
// On startup prints one JSON line to stdout: {"event":"ready","port":N}. Logs go to stderr.
//
// API (JSON):
//   GET  /health
//   GET  /cameras
//   POST /open     {"camera": "front"}
//   POST /capture  {"camera": "desk", "region": {"x":0.4,"y":0.4,"w":0.2,"h":0.2}, "max_edge": 1568, "quality": 0.85}
//                  region is normalized, top-left origin; omitted = full frame.
//   GET  /preview?camera=ID&max_edge=640
//                  newest buffered frame as image/jpeg (header X-Frame-Age-Ms), for live preview.
//                  Never opens or reopens a stream; 409 not_open if the camera isn't streaming.
//   GET  /events?after=SEQ
//                  recent log lines {"events":[{"seq","time","message"}],"last_seq":N}

import AVFoundation
import Foundation

func emit(_ obj: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: obj)
    FileHandle.standardOutput.write(data + Data("\n".utf8))
}

var port: UInt16 = 0
var openAtStart: [String] = []
var allowBuiltIn = false
do {
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let a = it.next() {
        switch a {
        case "--port": port = UInt16(it.next() ?? "") ?? 0
        case "--open": openAtStart = (it.next() ?? "").split(separator: ",").map(String.init)
        case "--allow-builtin": allowBuiltIn = true
        default: log("unknown argument \(a)"); exit(64)
        }
    }
}

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

guard ensureCameraAccess() else {
    emit(["event": "error", "error": "camera_access_denied",
          "message": "Grant camera access to the terminal app in System Settings > Privacy & Security > Camera."])
    exit(2)
}

let service = CaptureService(registry: CameraRegistry(allowBuiltIn: allowBuiltIn))

func errorResponse(_ e: Error) -> HTTPResponse {
    if let e = e as? ServiceError { return HTTPResponse(status: e.status, json: ["error": e.code, "message": e.message]) }
    return HTTPResponse(status: 500, json: ["error": "internal", "message": "\(e)"])
}

func parseCapture(_ body: Data) throws -> CaptureRequest {
    guard let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
          let camera = obj["camera"] as? String else { throw ServiceError.badRequest("body must be JSON with a 'camera' string") }
    var req = CaptureRequest(camera: camera)
    if let r = obj["region"] as? [String: Any] {
        guard let x = r["x"] as? Double, let y = r["y"] as? Double, let w = r["w"] as? Double, let h = r["h"] as? Double,
              w > 0, h > 0 else { throw ServiceError.badRequest("region needs numeric x, y, w, h with w, h > 0") }
        req.region = (x, y, w, h)
    }
    if let m = obj["max_edge"] as? Int { req.maxEdge = max(64, m) }
    if let q = obj["quality"] as? Double { req.quality = max(0.1, min(1.0, q)) }
    return req
}

let server = try HTTPServer(port: port) { req in
    do {
        switch (req.method, req.path) {
        case ("GET", "/health"):
            return HTTPResponse(status: 200, json: ["ok": true])
        case ("GET", "/cameras"):
            return HTTPResponse(status: 200, json: ["cameras": service.cameras()])
        case ("POST", "/open"):
            let obj = try? JSONSerialization.jsonObject(with: req.body) as? [String: Any]
            guard let camera = obj?["camera"] as? String else { throw ServiceError.badRequest("missing 'camera'") }
            try service.open(camera)
            return HTTPResponse(status: 200, json: ["ok": true])
        case ("POST", "/capture"):
            return HTTPResponse(status: 200, json: try service.capture(parseCapture(req.body)))
        case ("GET", "/preview"):
            guard let camera = req.query["camera"] else { throw ServiceError.badRequest("missing 'camera' query parameter") }
            let maxEdge = max(64, min(2048, Int(req.query["max_edge"] ?? "") ?? 640))
            let p = try service.preview(camera, maxEdge: maxEdge)
            return HTTPResponse(status: 200, contentType: "image/jpeg", body: p.jpeg,
                                headers: ["X-Frame-Age-Ms": "\(p.ageMs)", "X-Source-Size": "\(p.width)x\(p.height)"])
        case ("GET", "/events"):
            let r = eventLog.since(Int(req.query["after"] ?? "") ?? 0)
            return HTTPResponse(status: 200, json: ["events": r.events, "last_seq": r.lastSeq])
        default:
            return HTTPResponse(status: 404, json: ["error": "not_found", "message": "\(req.method) \(req.path)"])
        }
    } catch {
        return errorResponse(error)
    }
}

server.start { actualPort in
    emit(["event": "ready", "port": Int(actualPort)])
    log("listening on 127.0.0.1:\(actualPort)")
    for ref in openAtStart {
        DispatchQueue.global().async {
            do { try service.open(ref) } catch { log("could not open '\(ref)' at startup: \(error)") }
        }
    }
}

dispatchMain()
