import Foundation

/// Thread-safe bounded ring log, mirroring the Android `LocalRuntime.logs` deque.
public final class KumoLog {
    public static let shared = KumoLog()

    private var _entries: [String] = []
    private let lock = NSLock()
    private let limit = 500

    /// Optional observer for streaming log lines to the UI / console.
    public var onLine: ((String) -> Void)?

    public var snapshot: String {
        lock.lock()
        defer { lock.unlock() }
        return _entries.joined(separator: "\n")
    }

    public func append(_ line: String) {
        lock.lock()
        _entries.append(line)
        if _entries.count > limit {
            _entries.removeFirst(_entries.count - limit)
        }
        lock.unlock()
        onLine?(line)
    }

    public init() {}

    func clear() {
        lock.lock()
        _entries.removeAll()
        lock.unlock()
    }
}