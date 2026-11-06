import Foundation
import Network

/// Loopback HTTP front-end for the WebView: serves the bundled Kumo UI (codex.html,
/// xterm assets, mascot images) and reverse-proxies everything else — including
/// Server-Sent-Events streams — to the runtime's AuthProxy port. Keeping the UI and the
/// API on one origin mirrors Android's `/_ui/index.html` substitution (shouldInterceptRequest)
/// and keeps CORS moot.
public final class UIServer: NSObject, URLSessionDataDelegate {
    public private(set) var port: UInt16 = 0

    private let targetPort: UInt16
    private let basicAuth: String
    private let log: (String) -> Void
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "kumo.ui-server", attributes: .concurrent)
    private var running = false
    private lazy var session: URLSession = URLSession(configuration: .ephemeral, delegate: self, delegateQueue: nil)

    private struct Head { let line: String; var headers: [String: String] = [:] }

    private let lock = NSLock()
    private var conns: [ObjectIdentifier: NWConnection] = [:]
    private var connQueues: [ObjectIdentifier: DispatchQueue] = [:]

    public init(targetPort: UInt16, user: String, password: String, log: @escaping (String) -> Void = { _ in }) {
        self.targetPort = targetPort
        let combined = "\(user):\(password)"
        self.basicAuth = "Basic " + Data(combined.utf8).base64EncodedString()
        self.log = log
        super.init()
    }

    public func start(preferredPort: UInt16 = 14102) throws {
        if running { return }
        // Try a few ports like ConnectProxy does: stale listeners from previous launches
        // may hold the preferred one.
        var lastError: Error?
        for candidate in [preferredPort, preferredPort + 1, preferredPort + 2, preferredPort + 3, preferredPort + 4] {
            do { listener = try NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: candidate) ?? .any) }
            catch { lastError = error; continue }
            guard let listener else { lastError = NSError(domain: "UIServer", code: 1); continue }
            port = listener.port?.rawValue ?? 0
            running = true
            listener.newConnectionHandler = { [weak self] conn in self?.handle(conn) }
            listener.start(queue: queue)
            // NWListener.start is async: the port is assigned after the listener becomes ready.
            // Without this poll the caller reads port=0 and the WebView gets a broken URL.
            let deadline = Date().addingTimeInterval(2)
            while (port == 0 || listener.state == .setup) && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.02)
            }
            if port != 0 { return }
            lastError = NSError(domain: "UIServer", code: 2,
                userInfo: [NSLocalizedDescriptionKey: "listener did not start on \(candidate)"])
            listener.cancel(); listener = nil; running = false; port = 0
        }
        throw lastError ?? NSError(domain: "UIServer", code: 1)
    }

    public func stop() {
        running = false
        listener?.cancel()
        listener = nil
        lock.lock()
        conns.values.forEach { $0.cancel() }
        conns.removeAll()
        connQueues.removeAll()
        lock.unlock()
    }

    /// Points the proxy at a fresh runtime port after a server restart.
    public func retarget(_ newTargetPort: UInt16) { /* targetPort is let; recreate UIServer instead */ _ = newTargetPort }

    // MARK: request handling

    private func handle(_ conn: NWConnection) {
        let id = ObjectIdentifier(conn)
        let cq = DispatchQueue(label: "kumo.ui-conn")
        lock.lock()
        conns[id] = conn
        connQueues[id] = cq
        lock.unlock()
        conn.start(queue: cq)
        receiveHead(conn, Data()) { [weak self] head, leftover in
            guard let self else { conn.cancel(); return }
            let parts = head.line.split(separator: " ", maxSplits: 2).map(String.init)
            guard parts.count >= 2 else { conn.cancel(); return }
            let method = parts[0].uppercased()
            let rawTarget = parts[1]
            let contentLength = Int(head.headers.first { $0.key.lowercased() == "content-length" }?.value ?? "0") ?? 0
            self.receiveBody(conn, leftover, remaining: max(0, contentLength - leftover.count)) { body in
                self.route(conn, method: method, target: rawTarget, headers: head.headers, body: body)
            }
        }
    }

    private func receiveHead(_ conn: NWConnection, _ acc: Data, done: @escaping (Head, Data) -> Void) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 16384) { [weak self] data, _, _, error in
            guard let self, error == nil, let data else { conn.cancel(); return }
            var buf = acc
            buf.append(data)
            guard let range = buf.range(of: Data("\r\n\r\n".utf8)) else {
                if buf.count > 65536 { conn.cancel(); return }
                self.receiveHead(conn, buf, done: done)
                return
            }
            let headData = buf[..<range.lowerBound]
            guard let text = String(data: headData, encoding: .utf8) else { conn.cancel(); return }
            var lines = text.components(separatedBy: "\r\n")
            let line = lines.isEmpty ? "" : lines.removeFirst()
            var headers: [String: String] = [:]
            for l in lines {
                guard let colon = l.firstIndex(of: ":") else { continue }
                let k = String(l[..<colon]).trimmingCharacters(in: .whitespaces)
                let v = String(l[l.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
                headers[k] = v
            }
            done(Head(line: line, headers: headers), Data(buf[range.upperBound...]))
        }
    }

    private func receiveBody(_ conn: NWConnection, _ acc: Data, remaining: Int, done: @escaping (Data) -> Void) {
        if remaining <= 0 || acc.count >= remaining + 65536 { done(acc); return }
        conn.receive(minimumIncompleteLength: 1, maximumLength: 262144) { [weak self] data, _, _, error in
            guard let self, error == nil, let data else { done(acc); return }
            var buf = acc
            buf.append(data)
            if buf.count >= remaining { done(buf); return }
            self.receiveBody(conn, buf, remaining: remaining, done: done)
        }
    }

    private func route(_ conn: NWConnection, method: String, target: String, headers: [String: String], body: Data) {
        let path = target.split(separator: "?").first.map(String.init) ?? target
        if path == "/" || path == "/_ui/" || path == "/_ui/index.html" {
            if let data = uiOverride() {
                respond(conn, status: 200, reason: "OK", mime: "text/html; charset=utf-8", body: data)
                return
            }
            if let data = bundleFile("codex.html") {
                respond(conn, status: 200, reason: "OK", mime: "text/html; charset=utf-8", body: data)
                return
            }
            respond(conn, status: 404, reason: "Not Found", mime: "text/plain", body: Data("codex.html missing from bundle".utf8))
            return
        }
        if path.hasPrefix("/_ui/") && path != "/_ui/" {
            let rel = String(path.dropFirst("/_ui/".count))
            let cache = rel.hasSuffix(".js") || rel.hasSuffix(".css") ? "Cache-Control: max-age=86400\r\n" : ""
            if let data = bundleFile(rel) {
                respond(conn, status: 200, reason: "OK", mime: Self.mime(rel), body: data, extra: cache)
                return
            }
            respond(conn, status: 404, reason: "Not Found", mime: "text/plain", body: Data("missing: \(rel)".utf8))
            return
        }
        if path.hasPrefix("/kumo_") {
            if let data = bundleFile(path) {
                respond(conn, status: 200, reason: "OK", mime: Self.mime(path), body: data, extra: "Cache-Control: max-age=86400\r\n")
                return
            }
        }
        proxy(conn, method: method, target: target, headers: headers, body: body)
    }

    // MARK: proxying

    private func proxy(_ conn: NWConnection, method: String, target: String, headers: [String: String], body: Data) {
        guard let url = URL(string: "http://127.0.0.1:\(targetPort)\(target)") else {
            respond(conn, status: 400, reason: "Bad Request", mime: "text/plain", body: Data())
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue(basicAuth, forHTTPHeaderField: "Authorization")
        for (k, v) in headers {
            let lower = k.lowercased()
            if ["authorization", "connection", "proxy-connection", "expect", "host", "content-length",
                "transfer-encoding", "keep-alive", "accept-encoding"].contains(lower) { continue }
            request.setValue(v, forHTTPHeaderField: k)
        }
        if !body.isEmpty { request.httpBody = body }
        let task = session.dataTask(with: request)
        lock.lock()
        conns[ObjectIdentifier(task)] = conn
        lock.unlock()
        task.resume()
    }

    // MARK: URLSessionDataDelegate (streaming, preserves order via the serial delegate queue)

    public func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                           didReceive response: URLResponse,
                           completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse, let conn = connFor(dataTask) else {
            completionHandler(.cancel)
            return
        }
        var head = "HTTP/1.1 \(http.statusCode) \(HTTPURLResponse.localizedString(forStatusCode: http.statusCode))\r\n"
        for (k, v) in http.allHeaderFields {
            let lower = (k as? String ?? "").lowercased()
            if ["connection", "transfer-encoding", "content-length", "keep-alive"].contains(lower) { continue }
            head += "\(k): \(v)\r\n"
        }
        head += "Connection: close\r\n\r\n"
        send(conn, Data(head.utf8))
        completionHandler(.allow)
    }

    public func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        if let conn = connFor(dataTask) { send(conn, data) }
    }

    public func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let id = ObjectIdentifier(task)
        lock.lock()
        let conn = conns.removeValue(forKey: id)
        lock.unlock()
        conn?.cancel()
    }

    // MARK: plumbing

    private func connFor(_ task: URLSessionTask) -> NWConnection? {
        lock.lock()
        let conn = conns[ObjectIdentifier(task)]
        lock.unlock()
        return conn
    }

    private func send(_ conn: NWConnection, _ data: Data) {
        lock.lock()
        let cq = connQueues[ObjectIdentifier(conn)] ?? queue
        lock.unlock()
        cq.async { conn.send(content: data, completion: .contentProcessed { _ in }) }
    }

    private func respond(_ conn: NWConnection, status: Int, reason: String, mime: String, body: Data, extra: String = "") {
        let head = "HTTP/1.1 \(status) \(reason)\r\nContent-Type: \(mime)\r\nContent-Length: \(body.count)\r\n\(extra)Connection: close\r\n\r\n"
        send(conn, Data(head.utf8) + body)
    }

    // MARK: files

    private func bundleFile(_ name: String) -> Data? {
        let ext = (name as NSString).pathExtension
        let base = (name as NSString).deletingPathExtension
        if let url = Bundle.main.url(forResource: base, withExtension: ext, subdirectory: "AppHarness-Resources") {
            return try? Data(contentsOf: url)
        }
        if let url = Bundle.main.url(forResource: base, withExtension: ext) {
            return try? Data(contentsOf: url)
        }
        return nil
    }

    /// Developer override: drop a file at Documents/Kumo/codex.html to iterate on the UI
    /// without rebuilding the app (mirrors Android's getExternalFilesDir/codex.html).
    private func uiOverride() -> Data? {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
        guard let docs else { return nil }
        let url = docs.appendingPathComponent("Kumo/codex.html")
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        return try? Data(contentsOf: url)
    }

    static func mime(_ name: String) -> String {
        switch (name as NSString).pathExtension.lowercased() {
        case "html": return "text/html; charset=utf-8"
        case "js": return "application/javascript"
        case "css": return "text/css"
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "svg": return "image/svg+xml"
        case "json": return "application/json"
        case "webp": return "image/webp"
        case "gif": return "image/gif"
        default: return "application/octet-stream"
        }
    }
}
