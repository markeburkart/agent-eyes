import Foundation
import Network

struct HTTPRequest {
    let method: String
    let path: String
    let body: Data
}

struct HTTPResponse {
    var status: Int
    var json: Any

    func serialized() -> Data {
        let body = (try? JSONSerialization.data(withJSONObject: json)) ?? Data("{}".utf8)
        let reason = [200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error", 503: "Service Unavailable", 504: "Gateway Timeout"][status] ?? "Error"
        var head = "HTTP/1.1 \(status) \(reason)\r\n"
        head += "Content-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
        return Data(head.utf8) + body
    }
}

/// Minimal HTTP/1.1 server on loopback: one request per connection, JSON in and out.
final class HTTPServer {
    private let listener: NWListener
    private let handler: (HTTPRequest) -> HTTPResponse
    private let queue = DispatchQueue(label: "camsvc.http")

    init(port: UInt16, handler: @escaping (HTTPRequest) -> HTTPResponse) throws {
        let params = NWParameters.tcp
        params.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port) ?? .any)
        listener = try NWListener(using: params)
        self.handler = handler
    }

    func start(onReady: @escaping (UInt16) -> Void) {
        listener.stateUpdateHandler = { [listener] state in
            switch state {
            case .ready: onReady(listener.port?.rawValue ?? 0)
            case .failed(let e): log("listener failed: \(e)"); exit(1)
            default: break
            }
        }
        listener.newConnectionHandler = { [weak self] conn in self?.accept(conn) }
        listener.start(queue: queue)
    }

    private func accept(_ conn: NWConnection) {
        conn.start(queue: queue)
        receive(conn, buffer: Data())
    }

    private func receive(_ conn: NWConnection, buffer: Data) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, done, error in
            guard let self else { return }
            var buf = buffer
            if let data { buf.append(data) }
            if let req = Self.parse(buf) {
                // Captures can block for a few seconds while a stream warms up; keep them off the network queue.
                DispatchQueue.global().async {
                    let resp = self.handler(req)
                    conn.send(content: resp.serialized(), completion: .contentProcessed { _ in conn.cancel() })
                }
            } else if done || error != nil || buf.count > 1_000_000 {
                conn.cancel()
            } else {
                self.receive(conn, buffer: buf)
            }
        }
    }

    /// Returns a request once headers and the full Content-Length body have arrived.
    private static func parse(_ buf: Data) -> HTTPRequest? {
        guard let headerEnd = buf.range(of: Data("\r\n\r\n".utf8)),
              let head = String(data: buf[..<headerEnd.lowerBound], encoding: .utf8) else { return nil }
        let lines = head.components(separatedBy: "\r\n")
        let parts = lines[0].split(separator: " ")
        guard parts.count >= 2 else { return nil }
        let length = lines.dropFirst().compactMap { line -> Int? in
            let kv = line.split(separator: ":", maxSplits: 1)
            guard kv.count == 2, kv[0].lowercased() == "content-length" else { return nil }
            return Int(kv[1].trimmingCharacters(in: .whitespaces))
        }.first ?? 0
        let body = buf[headerEnd.upperBound...]
        guard body.count >= length else { return nil }
        return HTTPRequest(method: String(parts[0]), path: String(parts[1]), body: Data(body.prefix(length)))
    }
}
