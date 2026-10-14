import Foundation

func log(_ s: String) {
    FileHandle.standardError.write(Data("[camsvc] \(s)\n".utf8))
    eventLog.append(s)
}

let eventLog = EventLog()

/// ISO 8601 timestamp with milliseconds.
func isoMillis(_ date: Date) -> String {
    let fmt = ISO8601DateFormatter()
    fmt.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return fmt.string(from: date)
}

/// The most recent log lines, numbered, so clients (the test UI) can follow helper events
/// (disconnects, interruptions, stalls, reopens) without reading stderr.
final class EventLog {
    let capacity = 500
    private let lock = NSLock()
    private var entries: [(seq: Int, at: Date, message: String)] = []
    private var lastSeq = 0

    func append(_ message: String) {
        lock.withLock {
            lastSeq += 1
            entries.append((lastSeq, Date(), message))
            if entries.count > capacity { entries.removeFirst(entries.count - capacity) }
        }
    }

    /// Entries with a sequence number greater than `seq`, oldest first, plus the newest sequence number.
    func since(_ seq: Int) -> (events: [[String: Any]], lastSeq: Int) {
        lock.withLock {
            (entries.filter { $0.seq > seq }.map { ["seq": $0.seq, "time": isoMillis($0.at), "message": $0.message] }, lastSeq)
        }
    }
}
