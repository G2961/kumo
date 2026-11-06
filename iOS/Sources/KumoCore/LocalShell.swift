import Foundation

/// Spawns shell commands in the local sandbox (the on-device agent shell), mirroring
/// the Android `termExec`. Uses the system `/bin/sh`; on a jailbroken iPad this is present.
public final class LocalShell {
    public struct Task {
        public let run: String
        public let process: KumoProcess
        public let cwd: String
    }

    private var tasks: [String: KumoProcess] = [:]
    private let lock = NSLock()
    private static let shellPath = FileManager.default.fileExists(atPath: "/bin/sh") ? "/bin/sh" : "/var/jb/usr/bin/sh"

    public init() {}

    /// Writes a `run` id to `onDelta`, and calls `onEnd(code, cwd)`.
    public func exec(cwd: String, command: String,
                     onDelta: @escaping (String, String) -> Void,
                     onEnd: @escaping (String, Int32, String) -> Void) -> String {
        let run = "t" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(8)
        let p = KumoProcess()
        p.executableURL = URL(fileURLWithPath: Self.shellPath)
        p.arguments = ["-c", "{ \(command); } ; printf '\n__KWD__%s' \"$(pwd)\""]
        let dir = URL(fileURLWithPath: cwd)
        if FileManager.default.fileExists(atPath: dir.path) {
            p.currentDirectoryURL = dir
        }
        let out = Pipe()
        p.standardOutput = out
        p.standardError = out

        lock.lock(); tasks[run] = p; lock.unlock()

        do {
            try p.run()
        } catch {
            onDelta(run, "spawn failed: \(error.localizedDescription)\n")
            onEnd(run, -1, cwd)
            return run
        }

        var buf = Data()
        let handle = out.fileHandleForReading
        handle.readabilityHandler = { h in
            let data = h.availableData
            guard !data.isEmpty else { return }
            // Split at the __KWD__ marker for the final cwd.
            if let range = data.range(of: Data("__KWD__".utf8)) {
                buf.append(data[..<range.lowerBound])
                if let s = String(data: buf, encoding: .utf8), !s.isEmpty {
                    onDelta(run, s)
                }
                let cwdTail = data[range.upperBound...]
                let cwdStr = String(data: cwdTail, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? cwd
                onEnd(run, -1, cwdStr)
                handle.readabilityHandler = nil
            } else {
                buf.append(data)
                // Flush in chunks to keep latency low.
                if buf.count >= 512, let s = String(data: buf, encoding: .utf8) {
                    onDelta(run, s)
                    buf = Data()
                }
            }
        }

        p.terminationHandler = { proc in
            self.lock.lock(); self.tasks.removeValue(forKey: run); self.lock.unlock()
            let cwdOut = String(data: buf, encoding: .utf8) ?? cwd
            onEnd(run, proc.terminationStatus, cwdOut)
        }
        return run
    }

    public func stop(_ run: String) {
        lock.lock()
        let p = tasks.removeValue(forKey: run)
        lock.unlock()
        p?.terminate()
    }

    public func stopAll() {
        lock.lock()
        let all = tasks.values
        tasks.removeAll()
        lock.unlock()
        all.forEach { $0.terminate() }
    }
}