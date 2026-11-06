import Foundation
import Network

/// Loopback HTTP CONNECT proxy. The embedded opencode server routes outbound provider
/// traffic through this proxy so DNS resolution uses the OS (no resolv.conf shaping needed).
/// Mirrors `ConnectProxy.kt`.
public final class ConnectProxy {
    public private(set) var port: UInt16 = 0

    private let log: (String) -> Void
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "kumo.connect-proxy", attributes: .concurrent)
    private var running = false

    public init(log: @escaping (String) -> Void = { _ in }) {
        self.log = log
    }

    public func start(preferredPort: UInt16 = 14150) throws {
        if running { return }
        do {
            listener = try NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: preferredPort) ?? .any)
        } catch {
            listener = try NWListener(using: .tcp, on: .any)
        }
        guard let listener else { throw NSError(domain: "ConnectProxy", code: 1) }
        port = listener.port?.rawValue ?? 0
        running = true

        listener.newConnectionHandler = { [weak self] conn in
            guard let self else { return }
            self.handle(conn)
        }
        listener.stateUpdateHandler = { [weak self] state in
            self?.log("[proxy] listener state \(state)")
        }
        listener.start(queue: queue)
    }

    public func stop() {
        running = false
        listener?.cancel()
        listener = nil
    }

    private func handle(_ conn: NWConnection) {
        conn.start(queue: queue)
        receiveHead(conn) { [weak self] request in
            guard let self else { return }
            let parts = request.line.split(separator: " ", maxSplits: 2).map(String.init)
            guard parts.count >= 2 else { conn.cancel(); return }
            let method = parts[0].uppercased()
            let target = parts[1]
            if method == "CONNECT" {
                let hostPort = parseHostPort(target)
                self.log("[proxy] CONNECT \(hostPort.host):\(hostPort.port)")
                self.tunnel(from: conn, toHost: hostPort.host, port: hostPort.port)
            } else {
                self.sendError(conn, code: 502, message: "Bad Gateway")
                conn.cancel()
            }
        }
    }

    private func tunnel(from conn: NWConnection, toHost host: String, port: UInt16) {
        // Let the OS do DNS resolution: pass the hostname straight to NWConnection.
        let cleanHost = host.replacingOccurrences(of: "[", with: "").replacingOccurrences(of: "]", with: "")
        let remote = NWConnection(
            host: NWEndpoint.Host(cleanHost),
            port: NWEndpoint.Port(rawValue: port) ?? .https,
            using: .tcp
        )
        remote.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                let ok = "HTTP/1.1 200 Connection established\r\n\r\n"
                conn.send(content: Data(ok.utf8), completion: .contentProcessed { _ in
                    self?.pipe(conn, remote)
                    self?.pipe(remote, conn)
                })
            case .failed(let e):
                self?.log("[proxy] CONNECT failed \(e)")
            default:
                break
            }
        }
        remote.start(queue: queue)
    }

    private func pipe(_ a: NWConnection, _ b: NWConnection) {
        a.receiveMessage { [weak self] data, _, complete, error in
            guard let self, let data else { b.cancel(); return }
            b.send(content: data, completion: .contentProcessed { _ in
                if error != nil { b.cancel() }
                else { self.pipe(a, b) }
            })
            _ = complete
        }
    }

    private func receiveHead(_ conn: NWConnection, completion: @escaping (Head) -> Void) {
        var buffer = Data()
        func readMore() {
            conn.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { data, _, _, error in
                if let data {
                    buffer.append(data)
                    if let idx = buffer.range(of: Data("\r\n\r\n".utf8)) {
                        let headData = buffer[..<idx.lowerBound]
                        let text = String(data: headData, encoding: .isoLatin1) ?? ""
                        var lines = text.components(separatedBy: "\r\n")
                        let requestLine = lines.isEmpty ? "" : lines.removeFirst()
                        var headers: [String: String] = [:]
                        for line in lines {
                            if let i = line.firstIndex(of: ":") {
                                let k = line[..<i].trimmingCharacters(in: .whitespaces).lowercased()
                                let v = line[line.index(after: i)...].trimmingCharacters(in: .whitespaces)
                                headers[k] = v
                            }
                        }
                        completion(Head(line: requestLine, headers: headers))
                        return
                    }
                }
                if error == nil {
                    readMore()
                } else {
                    conn.cancel()
                }
            }
        }
        readMore()
    }

    private func parseHostPort(_ target: String) -> (host: String, port: UInt16) {
        var t = target
        if t.hasPrefix("//") { t.removeFirst(2) }
        if t.hasPrefix("[") {
            if let end = t.firstIndex(of: "]") {
                let host = String(t[t.index(after: t.startIndex)..<end])
                let rest = t[t.index(after: end)...]
                if rest.hasPrefix(":") {
                    let p = UInt16(rest.dropFirst()) ?? 443
                    return ("[\(host)]", p)
                }
                return ("[\(host)]", 443)
            }
        }
        if let i = t.lastIndex(of: ":") {
            let host = String(t[..<i]).trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
            let p = UInt16(t[t.index(after: i)...]) ?? 443
            return (host, p)
        }
        return (t, 443)
    }

    private func sendError(_ conn: NWConnection, code: Int, message: String) {
        let body = "HTTP/1.1 \(code) \(message)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        conn.send(content: Data(body.utf8), completion: .contentProcessed { _ in conn.cancel() })
    }

    private struct Head {
        let line: String
        let headers: [String: String]
    }
}