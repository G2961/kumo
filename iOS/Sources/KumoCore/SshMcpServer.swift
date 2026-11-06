import Foundation
import Network

/// Local authenticated MCP tools server, mirroring `SshToolsServer.java`. Serves JSON-RPC
/// on `/mcp` and bridge actions on `/bridge`, both authenticated with a bearer token that
/// is wired into the opencode config as a remote MCP. The agent receives connection IDs,
/// never credentials.
public final class SshMcpServer {

    public struct Request {
        public let action: String
        public let data: [String: Any]
    }

    public struct MCPCall {
        public let name: String
        public let arguments: [String: Any]
    }

    public private(set) var port: UInt16 = 0
    public private(set) var token = ""

    private let store: SshConnectionStore
    private let log: (String) -> Void
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "kumo.ssh-mcp", attributes: .concurrent)
    private var running = false
    private var historyLock = NSLock()
    private var history: [[String: Any]] = []
    private var selectedConnections: [String: String] = [:]
    private var testBridge = true

    public init(store: SshConnectionStore, log: @escaping (String) -> Void = { _ in }) {
        self.store = store
        self.log = log
        var bytes = [UInt8](repeating: 0, count: 32)
        SecRandomCopyBytes(kSecRandomDefault, 32, &bytes)
        token = Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    public var url: String { "http://127.0.0.1:\(port)/mcp" }

    public func start(preferredPort: UInt16 = 0) throws {
        if running { return }
        do {
            listener = try NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: preferredPort) ?? .any)
        } catch {
            listener = try NWListener(using: .tcp, on: .any)
        }
        guard let listener else { throw NSError(domain: "SshMcpServer", code: 1) }
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

    // MARK: - Connection handling

    private func handle(_ conn: NWConnection) {
        conn.start(queue: queue)
        receiveHead(conn) { [weak self] head in
            guard let self else { conn.cancel(); return }
            let parts = head.line.split(separator: " ", maxSplits: 2).map(String.init)
            guard parts.count >= 2 else { conn.cancel(); return }
            let method = parts[0].uppercased()
            let path = parts[1].split(separator: "?", maxSplits: 1).first.map(String.init) ?? ""
            let authHeader = head.headers["authorization"]

            // Auth.
            guard authHeader == "Bearer \(self.token)" else {
                self.respond(conn, status: 401, body: "", contentType: "application/json")
                return
            }

            guard method == "POST" else {
                self.respond(conn, status: 405, body: "", contentType: "application/json")
                return
            }

            let bodyLen = Int(head.headers["content-length"] ?? "0") ?? 0
            guard bodyLen <= 4 * 1024 * 1024 else {
                self.respond(conn, status: 413, body: "", contentType: "application/json")
                return
            }
            self.readBody(conn, count: bodyLen) { body in
                guard let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
                    self.respond(conn, status: 400, body: "", contentType: "application/json")
                    return
                }
                let result: Any?
                switch path {
                case "/mcp":
                    result = self.rpc(json)
                case "/bridge":
                    result = self.action(json) ?? ["error": "unknown action"]
                default:
                    result = nil
                }
                if result == nil {
                    self.respond(conn, status: 202, body: "", contentType: "application/json")
                } else {
                    let data = (try? JSONSerialization.data(withJSONObject: result ?? [:])) ?? Data()
                    self.respond(conn, status: 200, body: String(data: data, encoding: .utf8) ?? "{}",
                                 contentType: "application/json")
                }
            }
        }
    }

    // MARK: - RPC (MCP)

    private func rpc(_ request: [String: Any]) -> [String: Any]? {
        let method = request["method"] as? String ?? ""
        let id = request["id"]
        let params = (request["params"] as? [String: Any]) ?? [:]
        guard id != nil else { return nil }

        let result: [String: Any]
        switch method {
        case "initialize":
            result = [
                "protocolVersion": params["protocolVersion"] as? String ?? "2024-11-05",
                "capabilities": ["tools": ["listChanged": false]],
                "serverInfo": ["name": "Mobile SSH", "version": "1.0.0"]
            ]
        case "ping":
            result = [:]
        case "tools/list":
            result = ["tools": self.tools()]
        case "tools/call":
            let name = params["name"] as? String ?? ""
            let args = (params["arguments"] as? [String: Any]) ?? [:]
            do {
                let value = try self.call(name: name, args: args)
                result = ["content": [["type": "text", "text": jsonString(value)]], "isError": false]
            } catch {
                result = ["content": [["type": "text", "text": safeError(error)]], "isError": true]
            }
        default:
            return [
                "jsonrpc": "2.0", "id": id!,
                "error": ["code": -32601, "message": "Method not found"]
            ]
        }
        return ["jsonrpc": "2.0", "id": id!, "result": result]
    }

    private func tools() -> [[String: Any]] {
        [
            ["name": "ssh_connections",
             "description": "List saved SSH destinations without credentials.",
             "inputSchema": ["type": "object", "properties": [:], "additionalProperties": false]],
            ["name": "ssh_exec",
             "description": "Run a shell command on the selected SSH server. Stream stdout and stderr. Use the current session_id supplied in the prompt.",
             "inputSchema": ["type": "object",
                             "properties": [
                                "connection_id": ["type": "string"],
                                "session_id": ["type": "string"],
                                "command": ["type": "string"],
                                "directory": ["type": "string"],
                                "timeout_seconds": ["type": "integer", "minimum": 1, "maximum": 900]
                             ],
                             "required": ["connection_id", "session_id", "command"],
                             "additionalProperties": false]],
            ["name": "ssh_read_file",
             "description": "Read a UTF-8 file using SFTP on the selected SSH server.",
             "inputSchema": ["type": "object",
                             "properties": [
                                "connection_id": ["type": "string"],
                                "session_id": ["type": "string"],
                                "path": ["type": "string"]
                             ],
                             "required": ["connection_id", "session_id", "path"],
                             "additionalProperties": false]],
            ["name": "ssh_write_file",
             "description": "Create or replace a UTF-8 file using SFTP. Return the actual before/after patch.",
             "inputSchema": ["type": "object",
                             "properties": [
                                "connection_id": ["type": "string"],
                                "session_id": ["type": "string"],
                                "path": ["type": "string"],
                                "content": ["type": "string"]
                             ],
                             "required": ["connection_id", "session_id", "path", "content"],
                             "additionalProperties": false]]
        ]
    }

    private func call(name: String, args: [String: Any]) throws -> [String: Any] {
        if name == "ssh_connections" {
            return ["connections": store.list()]
        }
        guard let connectionId = args["connection_id"] as? String,
              let sessionId = args["session_id"] as? String,
              sessionId.hasPrefix("ses_") else {
            throw McpError("A valid connection_id and session_id are required")
        }
        guard selectedConnections[sessionId] == connectionId else {
            throw McpError("SSH session is not bound to this connection")
        }
        guard let raw = store.get(connectionId) else {
            throw McpError("SSH connection not found")
        }
        let conn = SshClient.Connection(id: raw.id, name: raw.name, host: raw.host,
                                        user: raw.user, port: raw.port, directory: raw.directory,
                                        fingerprint: raw.fingerprint, password: raw.password.isEmpty ? nil : raw.password,
                                        privateKey: raw.privateKey, passphrase: raw.passphrase)
        switch name {
        case "ssh_exec":
            let cmd = args["command"] as? String ?? ""
            let dir = args["directory"] as? String ?? ""
            let timeout = (args["timeout_seconds"] as? Int) ?? 120
            return try self.execToJSON(conn, sessionID: sessionId, command: cmd,
                                       directory: dir, timeout: timeout)
        case "ssh_read_file":
            let path = args["path"] as? String ?? ""
            let r = try SshClient.readFile(conn, path: path)
            return ["path": r.path, "content": SshClient.redact(r.content, conn)]
        case "ssh_write_file":
            let path = args["path"] as? String ?? ""
            let content = args["content"] as? String ?? ""
            return try SshClient.writeFile(conn, sessionID: sessionId, path: path,
                                           content: content, events: self.emit)
        default:
            throw McpError("Unknown SSH tool")
        }
    }

    private func execToJSON(_ conn: SshClient.Connection, sessionID: String, command: String,
                            directory: String, timeout: Int) throws -> [String: Any] {
        let r = try SshClient.execute(conn, sessionID: sessionID, command: command,
                                      directory: directory, timeout: timeout, events: self.emit)
        return ["runID": r.runID, "command": r.command, "stdout": r.stdout,
                "stderr": r.stderr, "exitCode": r.exitCode]
    }

    // MARK: - Bridge actions (SshToolsServer.action mirror)

    public func action(_ request: [String: Any]) -> [String: Any]? {
        let action = request["action"] as? String ?? ""
        let data = (request["data"] as? [String: Any]) ?? [:]
        do {
            switch action {
            case "sessionContext":
                guard let sid = data["sessionID"] as? String,
                      sid.hasPrefix("ses_"),
                      let conn = data["connectionID"] as? String,
                      store.get(conn) != nil else {
                    throw McpError("Invalid SSH session context")
                }
                selectedConnections[sid] = conn
                return ["saved": true]
            case "connections":
                return ["connections": store.list()]
            case "revealSecrets":
                guard let raw = data["id"] as? String, let c = store.get(raw) else {
                    throw McpError("SSH connection not found")
                }
                return ["password": c.password, "hasKey": !c.privateKey.isEmpty,
                        "passphrase": c.passphrase]
            case "removeConnection":
                if let id = data["id"] as? String { store.remove(id) }
                return ["connections": store.list()]
            case "saveConnection":
                guard let c = data["connection"] as? [String: Any] else {
                    throw McpError("Missing connection data")
                }
                store.save(c)
                return ["saved": true, "connections": store.list()]
            case "remoteHistory":
                let sid = data["sessionID"] as? String ?? ""
                historyLock.lock()
                defer { historyLock.unlock() }
                return ["events": history.filter { ($0["sessionID"] as? String) == sid }]
            case "cancelRemote":
                selectedConnections[data["sessionID"] as? String ?? ""] = nil
                return ["cancelled": true]
            default:
                return nil
            }
        } catch {
            return ["error": safeError(error)]
        }
    }

    // MARK: - Events

    private func emit(type: String, detail: [String: Any]) {
        historyLock.lock()
        history.append(detail)
        if history.count > 300 { history.removeFirst(history.count - 300) }
        historyLock.unlock()
        log("[ssh] \(type)")
    }

    // MARK: - JSON / http helpers

    private func jsonString(_ value: Any) -> String {
        (try? String(data: JSONSerialization.data(withJSONObject: value, options: []), encoding: .utf8)) ?? "{}"
    }

    private func safeError(_ e: Error) -> String {
        if let m = e as? SshClient.SshError {
            return m.errorDescription ?? "SSH request failed."
        }
        return "SSH request failed (\(String(describing: e)))."
    }

    private func respond(_ conn: NWConnection, status: Int, body: String, contentType: String) {
        let data = Data(body.utf8)
        var text = "HTTP/1.1 \(status) Response\r\n"
        text += "Content-Type: \(contentType)\r\n"
        text += "Content-Length: \(data.count)\r\n"
        text += "Connection: close\r\n\r\n"
        var out = Data(text.utf8)
        out.append(data)
        conn.send(content: out, completion: .contentProcessed { _ in conn.cancel() })
    }

    private func readBody(_ conn: NWConnection, count: Int, completion: @escaping (Data) -> Void) {
        conn.receive(minimumIncompleteLength: count, maximumLength: max(count, 1)) { data, _, _, _ in
            completion(data ?? Data())
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
                                let k = String(line[..<i]).trimmingCharacters(in: .whitespaces).lowercased()
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

private struct McpError: Error {
    let message: String
    init(_ m: String) { message = m }
}