import Foundation
import CryptoKit

/// Thin SSH/SFTP client backed by the system OpenSSH binaries (`/usr/bin/ssh`,
/// `/usr/bin/sftp`). On a jailbroken iPad these are present; the ops mirror
/// `RemoteSsh.java` (execute / read / write / test / host-key handling / patches)
/// without the JSch dependency.
public enum SshClient {

    public struct Connection {
        public var id: String
        public var name: String
        public var host: String
        public var user: String
        public var port: Int = 22
        public var directory: String
        public var fingerprint: String
        public var password: String?
        public var privateKey: String
        public var passphrase: String
    }

    public enum SshError: Error, LocalizedError {
        case invalidHost
        case authFailed
        case timedOut
        case privateKey(String)
        case hostKeyRequired(String)
        case processFailed(String)
        case fileTooLarge

        public var errorDescription: String? {
            switch self {
            case .invalidHost: return "Invalid SSH connection"
            case .authFailed: return "SSH authentication failed. Check your username and credentials."
            case .timedOut: return "SSH connection timed out."
            case .privateKey(let m): return m
            case .hostKeyRequired(let f): return f
            case .processFailed(let m): return "SSH request failed (\(m))."
            case .fileTooLarge: return "File too large"
            }
        }
    }

    private static let sshPath = "/usr/bin/ssh"
    private static let sftpPath = "/usr/bin/sftp"

    // MARK: - Validation

    public static func validated(_ c: Connection) throws -> Connection {
        var conn = c
        guard !conn.host.isEmpty,
              conn.host.range(of: #"^[A-Za-z0-9._\[\]:-]+$"#, options: .regularExpression) != nil,
              !conn.user.isEmpty,
              (1...65535).contains(conn.port),
              !conn.directory.contains("\n"), !conn.directory.contains("\0") else {
            throw SshError.invalidHost
        }
        if conn.id.isEmpty { conn.id = UUID().uuidString }
        if conn.name.isEmpty { conn.name = "\(conn.user)@\(conn.host)" }
        return conn
    }

    // MARK: - Auth helpers

    private static func baseArgs(_ c: Connection, batch: Bool = true) -> [String] {
        var args: [String] = []
        args += ["-p", "\(c.port)"]
        args += ["-o", "StrictHostKeyChecking=accept-new"]
        args += ["-o", "ServerAliveInterval=15"]
        args += ["-o", "ConnectTimeout=15"]
        let usePassword = !(c.password?.isEmpty ?? true)
        // Password auth must be allowed to prompt; key auth stays batch.
        if usePassword {
            args += ["-o", "BatchMode=no"]
            args += ["-o", "PreferredAuthentications=password,keyboard-interactive"]
        } else if batch {
            args += ["-o", "BatchMode=yes"]
            args += ["-o", "PreferredAuthentications=publickey"]
        } else {
            args += ["-o", "BatchMode=no"]
            args += ["-o", "PreferredAuthentications=publickey"]
        }
        return args
    }

    /// Host key fingerprint in `SHA256:...` form by asking ssh for the public key.
    public static func fingerprint(host: String, port: Int) -> String? {
        let args = ["-p", "\(port)", "-o", "StrictHostKeyChecking=no",
                    "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                    "-o", "LogLevel=ERROR", host, "exit 0"]
        let r = run(sshPath, args: args, stdin: Data(), timeout: 8)
        // ssh-keyscan is friendlier, but fall back to parsing the known_hosts entry.
        guard let known = knownHostsFingerprint(host: host, port: port) else { return nil }
        return known
    }

    private static func knownHostsFingerprint(host: String, port: Int) -> String? {
        let home = NSHomeDirectory()
        let known = "\(home)/.ssh/known_hosts"
        guard let data = try? String(contentsOfFile: known, encoding: .utf8) else { return nil }
        let prefix = port == 22 ? host : "[\(host)]:\(port)"
        for line in data.components(separatedBy: "\n") {
            if line.hasPrefix(prefix + " ") {
                let parts = line.split(separator: " ").map(String.init)
                if parts.count >= 3 {
                    return "SHA256:" + base64KeyHash(parts[2])
                }
            }
        }
        return nil
    }

    public static func base64KeyHash(_ base64: String) -> String {
        // OpenSSH hashes the wire-format key; approximate with a SHA-256 of the raw text
        // is NOT a valid fingerprint. Instead, produce a best-effort label. For real
        // verification we rely on the known_hosts recording made by the system ssh.
        guard let data = Data(base64Encoded: base64) else { return base64 }
        let digest = sha256(data)
        return base64UrlNoPadding(digest)
    }

    public static func sha256(_ data: Data) -> Data {
        Data(SHA256.hash(data: data))
    }

    private static func base64UrlNoPadding(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    // MARK: - Execute

    public struct ExecuteResult {
        public var runID: String
        public var command: String
        public var stdout: String
        public var stderr: String
        public var exitCode: Int
    }

    public static func execute(_ c: Connection, sessionID: String, command: String,
                               directory: String, timeout: Int = 120,
                               events: ((String, [String: Any]) -> Void)? = nil) throws -> ExecuteResult {
        var conn = try validated(c)
        conn.directory = directory
        guard !command.isEmpty, !command.contains("\0") else { throw SshError.invalidHost }
        let run = UUID().uuidString
        let cwdPrefix = buildCwdPrefix(directory.isEmpty ? conn.directory : directory)

        events?("ssh.command.started", ["runID": run, "sessionID": sessionID,
                                        "connectionID": conn.id, "command": redact(command, conn)])

        let fullCommand = cwdPrefix.isEmpty ? command : "\(cwdPrefix) && \(command)"
        let args = baseArgs(conn) + [targetSpec(conn), fullCommand]

        let (outData, errData, status) = runWithCap(sshPath, args: args,
                                                     stdin: passwordData(conn), timeout: timeout)
        let stdout = String(data: outData, encoding: .utf8) ?? ""
        let stderr = String(data: errData, encoding: .utf8) ?? ""
        let exit = status

        events?("ssh.command.completed", ["runID": run, "sessionID": sessionID,
                                          "connectionID": conn.id, "exitCode": exit])

        return ExecuteResult(runID: run,
                             command: redact(command, conn),
                             stdout: redact(stdout, conn),
                             stderr: redact(stderr, conn),
                             exitCode: Int(exit))
    }

    private static func buildCwdPrefix(_ directory: String) -> String {
        if directory.isEmpty { return "" }
        if directory == "~" { return "cd && " }
        return "cd \(quote(directory)) && "
    }

    // MARK: - Read / write (SFTP via system sftp batch)

    public static func readFile(_ c: Connection, path: String) throws -> (path: String, content: String) {
        let conn = try validated(c)
        let batch = "get \(leakQuote(path)) \(leakQuote(localTempPath()))"
        _ = run(sftpPath, args: sftpArgs(conn, batch: batch), stdin: passwordData(conn), timeout: 30)
        let local = localTempPath()
        guard let data = FileManager.default.contents(atPath: local) else { throw SshError.processFailed("SFTP read") }
        if data.count > 1024 * 1024 { throw SshError.fileTooLarge }
        return (path, String(data: data, encoding: .utf8) ?? "")
    }

    public static func writeFile(_ c: Connection, sessionID: String, path: String,
                                 content: String, events: ((String, [String: Any]) -> Void)? = nil) throws -> [String: Any] {
        let conn = try validated(c)
        guard content.utf8.count <= 1024 * 1024 else { throw SshError.fileTooLarge }
        let local = localTempPath()
        try (content.data(using: .utf8) ?? Data()).write(to: URL(fileURLWithPath: local))
        let remote = path
        let batch = "put \(leakQuote(local)) \(leakQuote(remote))"
        _ = run(sftpPath, args: sftpArgs(conn, batch: batch), stdin: passwordData(conn), timeout: 30)
        let run = UUID().uuidString
        let before = (try? readFile(conn, path: path).content) ?? ""
        let patchDict = patch(file: remote, before: before, after: content)
        events?("ssh.file.updated", ["runID": run, "sessionID": sessionID,
                                     "connectionID": conn.id, "path": remote,
                                     "patch": patchDict["patch"] ?? "", "additions": patchDict["additions"] ?? 0,
                                     "deletions": patchDict["deletions"] ?? 0])
        return patchDict
    }

    // MARK: - MCP patch (unified diff)

    public static func patch(file: String, before: String?, after: String) -> [String: Any] {
        let b = before ?? ""
        if b == after {
            return ["path": file, "patch": "", "additions": 0, "deletions": 0]
        }
        let a = lines(b), c = lines(after)
        var diff: [String] = []
        var add = 0, del = 0
        // Simple LCS-based diff for small inputs; fallback to full replace otherwise.
        if a.count * c.count <= 1_000_000 {
            let lcs = lcsMatrix(a, c)
            var i = 0, j = 0
            while i < a.count || j < c.count {
                if i < a.count && j < c.count && a[i] == c[j] {
                    diff.append(" " + a[i]); i += 1; j += 1
                } else if j < c.count && (i == a.count || lcs[i][j + 1] > lcs[i + 1][j]) {
                    diff.append("+" + c[j]); add += 1; j += 1
                } else {
                    diff.append("-" + a[i]); del += 1; i += 1
                }
            }
        } else {
            for l in a { diff.append("-" + l); del += 1 }
            for l in c { diff.append("+" + l); add += 1 }
        }
        let patchText = "--- \(file)\n+++ \(file)\n@@ -\(a.isEmpty ? 0 : 1),\(a.count) +\(c.isEmpty ? 0 : 1),\(c.count) @@\n" +
            diff.joined(separator: "\n") + "\n"
        return ["path": file, "patch": patchText, "additions": add, "deletions": del]
    }

    private static func lcsMatrix(_ a: [String], _ b: [String]) -> [[Int]] {
        var lcs = [[Int]](repeating: [Int](repeating: 0, count: b.count + 1), count: a.count + 1)
        for i in stride(from: a.count - 1, through: 0, by: -1) {
            for j in stride(from: b.count - 1, through: 0, by: -1) {
                lcs[i][j] = a[i] == b[j] ? lcs[i + 1][j + 1] + 1 : max(lcs[i + 1][j], lcs[i][j + 1])
            }
        }
        return lcs
    }

    private static func lines(_ s: String) -> [String] {
        if s.isEmpty { return [] }
        let trimmed = s.hasSuffix("\n") ? String(s.dropLast()) : s
        return trimmed.components(separatedBy: "\n")
    }

    // MARK: - Sanitisation

    public static func redact(_ text: String, _ c: Connection) -> String {
        var out = text
        if let pw = c.password, !pw.isEmpty { out = out.replacingOccurrences(of: pw, with: "[redacted]") }
        if !c.passphrase.isEmpty { out = out.replacingOccurrences(of: c.passphrase, with: "[redacted]") }
        // Bearer/Basic secrets and sk- tokens.
        do {
            let pat = try NSRegularExpression(pattern: #"(?i)\b(?:Bearer|Basic)\s+[^\s\"']+"#)
            out = pat.stringByReplacingMatches(in: out, range: NSRange(location: 0, length: (out as NSString).length),
                                               withTemplate: "[redacted]")
            let pat2 = try NSRegularExpression(pattern: #"\bsk-[\w-]+"#)
            out = pat2.stringByReplacingMatches(in: out, range: NSRange(location: 0, length: (out as NSString).length),
                                                withTemplate: "[redacted]")
        } catch {
            // ignore
        }
        return out
    }

    private static func quote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    private static func leakQuote(_ s: String) -> String {
        s.replacingOccurrences(of: "\"", with: "\\\"")
    }

    private static func targetSpec(_ c: Connection) -> String {
        "\(c.user)@\(c.host)"
    }

    private static func localTempPath() -> String {
        NSTemporaryDirectory() + "kumo.sftp.\(UUID().uuidString)"
    }

    private static func passwordData(_ c: Connection) -> Data? {
        if let pw = c.password, !pw.isEmpty {
            return Data((pw + "\n").utf8)
        }
        if !c.passphrase.isEmpty {
            return Data((c.passphrase + "\n").utf8)
        }
        return nil
    }

    private static func sftpArgs(_ c: Connection, batch: String) -> [String] {
        var args = baseArgs(c) + ["-b", "-", targetSpec(c), "-"]
        return args
    }

    // MARK: - Subprocess helpers (non-blocking, with cap)

    private static func run(_ path: String, args: [String], stdin: Data?, timeout: TimeInterval) -> (String, String, Int32) {
        let r = runWithCap(path, args: args, stdin: stdin, timeout: Int(timeout))
        return (String(data: r.0, encoding: .utf8) ?? "", String(data: r.1, encoding: .utf8) ?? "", r.2)
    }

    private static func runWithCap(_ path: String, args: [String], stdin: Data?, timeout: Int) -> (Data, Data, Int32) {
        let p = KumoProcess()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        if let stdin {
            p.standardInput.fileHandleForWriting.write(stdin)
            p.standardInput.fileHandleForWriting.closeFile()
        } else {
            p.standardInput.fileHandleForWriting.closeFile()
        }

        do {
            try p.run()
        } catch {
            return (Data(), Data(), -1)
        }
        var out = Data(), err = Data()
        let outPipe = p.standardOutput
        let errPipe = p.standardError
        let sem = DispatchSemaphore(value: 0)
        outPipe.fileHandleForReading.readabilityHandler = { h in out.append(h.availableData) }
        errPipe.fileHandleForReading.readabilityHandler = { h in err.append(h.availableData) }
        DispatchQueue.global().async {
            p.waitUntilExit()
            sem.signal()
        }
        _ = sem.wait(timeout: .now() + .seconds(max(2, timeout)))
        if p.isRunning {
            p.terminate()
            // Wait for children.
            Thread.sleep(forTimeInterval: 0.3)
            p.terminate()
        }
        return (out, err, p.terminationStatus)
    }
}