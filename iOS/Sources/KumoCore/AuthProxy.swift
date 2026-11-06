import Foundation
import Network

/// Loopback HTTP proxy that forwards browser requests to the local opencode server while
/// injecting `Authorization: Basic` so the WebView never prompts for a password.
/// Mirrors `AuthProxy.kt`.
public final class AuthProxy {
    public private(set) var port: UInt16 = 0

    private let targetPort: UInt16
    private let basicAuth: String
    private let log: (String) -> Void
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "kumo.auth-proxy", attributes: .concurrent)
    private var running = false
    private let session = URLSession(configuration: .ephemeral)

    public init(targetPort: UInt16, user: String, password: String, log: @escaping (String) -> Void = { _ in }) {
        self.targetPort = targetPort
        let combined = "\(user):\(password)"
        self.basicAuth = "Basic " + Data(combined.utf8).base64EncodedString()
        self.log = log
    }

    public func start(preferredPort: UInt16 = 14101) throws {
        if running { return }
        do {
            listener = try NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: preferredPort) ?? .any)
        } catch {
            listener = try NWListener(using: .tcp, on: .any)
        }
        guard let listener else { throw NSError(domain: "AuthProxy", code: 1) }
        port = listener.port?.rawValue ?? 0
        running = true

        listener.newConnectionHandler = { [weak self] conn in
            self?.handle(conn)
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
        receiveHead(conn) { [weak self] head in
            guard let self else { conn.cancel(); return }
            let parts = head.line.split(separator: " ", maxSplits: 2).map(String.init)
            guard parts.count >= 2 else { conn.cancel(); return }
            let method = parts[0].uppercased()
            let rawTarget = parts[1]

            if self.isUpgrade(head.headers) {
                self.tunnelWithAuth(conn, requestLine: head.line, headers: head.headers, rawTarget: rawTarget)
                return
            }

            let target = self.toTarget(rawTarget)
            self.forward(conn, method: method, target: target, headers: head.headers)
        }
    }

    private func isUpgrade(_ headers: [String: String]) -> Bool {
        headers.keys.contains { $0 == "upgrade" && !(headers[$0] ?? "").isEmpty }
    }

    private func forward(_ conn: NWConnection, method: String, target: String, headers: [String: String]) {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(targetPort)\(target)")!)
        request.httpMethod = method
        request.setValue(basicAuth, forHTTPHeaderField: "Authorization")
        request.setValue("close", forHTTPHeaderField: "Connection")

        for (k, v) in headers {
            let lower = k.lowercased()
            if ["authorization", "connection", "proxy-connection", "expect", "host", "content-length",
                "transfer-encoding", "keep-alive"].contains(lower) { continue }
            request.setValue(v, forHTTPHeaderField: k)
        }

        conn.receive(minimumIncompleteLength: 1, maximumLength: Int.max) { [weak self] data, _, _, _ in
            guard let self, let data else { conn.cancel(); return }
            request.httpBody = data
            self.session.dataTask(with: request) { body, response, error in
                guard let http = response as? HTTPURLResponse else {
                    self.respond(conn, status: 502, headers: [:], body: Data())
                    return
                }
                var outHeaders = [String: String]()
                for (k, v) in http.allHeaderFields {
                    if let ks = k as? String {
                        let lower = ks.lowercased()
                        if ["transfer-encoding", "connection", "keep-alive", "proxy-connection"].contains(lower) { continue }
                        outHeaders[ks] = "\(v)"
                    }
                }
                self.respond(conn, status: http.statusCode, headers: outHeaders,
                             body: body ?? Data())
            }.resume()
        }
    }

    private func respond(_ conn: NWConnection, status: Int, headers: [String: String], body: Data) {
        var text = "HTTP/1.1 \(status) Response\r\n"
        text += "Content-Length: \(body.count)\r\n"
        for (k, v) in headers { text += "\(k): \(v)\r\n" }
        text += "Connection: close\r\n\r\n"
        var data = Data(text.utf8)
        data.append(body)
        conn.send(content: data, completion: .contentProcessed { _ in conn.cancel() })
    }

    private func toTarget(_ rawTarget: String) -> String {
        if rawTarget.hasPrefix("/") { return rawTarget }
        if let url = URL(string: rawTarget) {
            let path = url.path.isEmpty ? "/" : url.path
            var out = path
            if let query = url.query { out += "?\(query)" }
            return out
        }
        return "/"
    }

    private func tunnelWithAuth(_ conn: NWConnection, requestLine: String, headers: [String: String], rawTarget: String) {
        let remote = NWConnection(
            host: NWEndpoint.Host("127.0.0.1"),
            port: NWEndpoint.Port(rawValue: targetPort) ?? .http,
            using: .tcp
        )
        remote.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            if case .ready = state {
                var text = requestLine + "\r\n"
                for (k, v) in headers {
                    let lower = k.lowercased()
                    if ["authorization", "proxy-authorization"].contains(lower) { continue }
                    text += "\(k): \(v)\r\n"
                }
                text += "Authorization: \(self.basicAuth)\r\n\r\n"
                remote.send(content: Data(text.utf8), completion: .contentProcessed { _ in
                    self.pipe(conn, remote)
                    self.pipe(remote, conn)
                })
            }
        }
        remote.start(queue: queue)
    }

    private func pipe(_ a: NWConnection, _ b: NWConnection) {
        a.receiveMessage { [weak self] data, _, _, error in
            guard let self, let data else { b.cancel(); return }
            b.send(content: data, completion: .contentProcessed { _ in
                if error != nil { b.cancel() } else { self.pipe(a, b) }
            })
        }
    }

    private func receiveHead(_ conn: NWConnection, completion: @escaping (Head) -> Void) {
        var buffer = Data()
        func readMore() {
            conn.receive(minimumIncompleteLength: 1, maximumLength: 128 * 1024) { data, _, _, error in
                if let data {
                    buffer.append(data)
                    if let idx = buffer.range(of: Data("\r\n\r\n".utf8)) {
                        let headData = buffer[..<idx.lowerBound]
                        let text = String(data: headData, encoding: .isoLatin1) ?? ""
                        var lines = text.components(separatedBy: "\r\n")
                        let requestLine = lines.isEmpty ? "" : lines.removeFirst()
                        var headers = [String: String]()
                        for line in lines {
                            if let i = line.firstIndex(of: ":") {
                                let k = String(line[..<i]).trimmingCharacters(in: .whitespaces)
                                let v = String(line[line.index(after: i)...]).trimmingCharacters(in: .whitespaces)
                                headers[k] = v
                            }
                        }
                        completion(Head(line: requestLine, headers: headers))
                        return
                    }
                }
                if error == nil { readMore() } else { conn.cancel() }
            }
        }
        readMore()
    }

    private struct Head {
        let line: String
        let headers: [String: String]
    }
}