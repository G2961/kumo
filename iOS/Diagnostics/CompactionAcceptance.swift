import Foundation
import Security
import Darwin

// Standalone physical-iPad CLI (iOS 15+), never bundled in the application.
// Compile ONLY with NativeAgentBackend.swift, NativeContextCompaction.swift,
// NativeFileTools.swift, NativeModelClient.swift and NativeProviderCompatibility.swift.
// Usage: KumoCompactionAcceptance <state.json> <providerID> <fresh-absolute-test-root>
// Requires the owner's existing Keychain entitlement. Reads, never migrates, credentials.
// One scripted seed, then ONE live AgentRouter summary + continuation (two billable
// streams maximum, no retry). Live requests use URLSessionNativeModelClient unchanged.
// Only the isolated root is written; leave it for inspection.
// The 20000-byte conservative estimate window is diagnostic-local, not provider metadata.
// A parent should allow 180s; this process exits nonzero after a 175s hard deadline.
// Stdout is fixed CHECK categories and counts only. Backend NSLog stderr is suppressed
// because even a storage error could otherwise disclose a path. Never print model text.
@available(iOS 15.0, macOS 12.0, *)
@main
struct CompactionAcceptance {
    private static let context = 20_000
    private static let code = "context_compaction_lime_47"
    private static let seedAnswer = "Recorded."
    private static let latest = "What acceptance code did I ask you to remember? Return only it, no tools."
    private static let seed: String = {
        let prefix = "Remember this acceptance code for the next turn: " + code
            + ". The following padding has no meaning.\n"
        return prefix + String(repeating: "x", count: 9_000 - prefix.utf8.count)
    }()

    private enum Failure: String, Error {
        case arguments, device, output, source, config, keychain, root
        case credentialWrite = "credential_write"
        case fixture, streams, budget, checkpoint, history, answer, tools, reopen
        case userState = "user_state"
        case timeout, backend
        case verified
    }

    static func main() async {
        let marker: StaticString = "CHECK startup_enter PASS\n"
        marker.withUTF8Buffer { bytes in
            if let base = bytes.baseAddress { _ = Darwin.write(STDOUT_FILENO, base, bytes.count) }
        }
        // Installed before any disk or Keychain operation, not merely before networking.
        let watchdog = DispatchWorkItem {
            print("CHECK timeout FAIL")
            fflush(nil)
            Darwin.exit(2)
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 175, execute: watchdog)
        let deadline = ProcessInfo.processInfo.systemUptime + 170
        var active: NativeAgentBackend?
        do {
            let args = CommandLine.arguments
            try requireIPad()
            guard args.count == 4 else { throw Failure.arguments }
            let sink = open("/dev/null", O_WRONLY)
            guard sink >= 0 else { throw Failure.output }
            let redirected = dup2(sink, STDERR_FILENO)
            if sink != STDERR_FILENO { close(sink) }
            guard redirected >= 0 else { throw Failure.output }
            let source = try Source(statePath: args[1], providerID: args[2])
            let root = try prepareRoot(args[3], source: source)
            let project = root.appendingPathComponent("project", isDirectory: true)
            try FileManager.default.createDirectory(at: project, withIntermediateDirectories: false)
            let client = LiveModel()
            let backend = try source.backend(root: root, client: client)
            active = backend
            let events = Events()
            await backend.setEventHandler(events.receive)
            _ = try await backend.request(method: "PATCH", path: "/global/config", directory: nil, body: source.config)
            let created = try await backend.request(method: "POST", path: "/session", directory: project.path, body: [
                "permission": [["permission": "*", "pattern": "*", "action": "deny"]]
            ])
            guard let sid = (created as? [String: Any])?["id"] as? String else { throw Failure.backend }
            let ledgerBefore = try optionalData(root.appendingPathComponent("patches.json"))

            try await prompt(backend, sid: sid, project: project, source: source, text: seed)
            try await waitIdle(backend, sid: sid, deadline: deadline)
            let original = try Snapshot(root: root, sid: sid)
            let oldUI = try await messages(backend, sid: sid, project: project)
            let seeded = client.requests
            try check(seeded.count == 1 && client.requestCount == 1 && !client.denied, .fixture, count: seeded.count)
            guard let baseline = seeded.first else { throw Failure.fixture }
            try check(seed.utf8.count == 9_000 && original.transcript.count == 2 && oldUI.count == 2
                && original.transcript[0]["role"] as? String == "user"
                && original.transcript[0]["content"] as? String == seed
                && original.transcript[1]["role"] as? String == "assistant"
                && original.transcript[1]["content"] as? String == seedAnswer
                && original.session["compaction"] == nil && completed(oldUI)
                && events.errors == 0, .fixture, count: original.transcript.count)
            try check(try json(oldUI) == json(original.visible), .history, count: oldUI.count)

            // Use the backend's actual system/tools from the seed, not a reconstructed prompt.
            let full = try decode(original.transcript) + [NativeModelMessage(role: "user", content: latest)]
            let before = try budget(baseline, messages: full)
            try check(before >= context - context / 5 && before < context, .budget, count: before)
            try check(NativeContextCompaction.boundary(in: full, after: 0) == original.transcript.count,
                      .checkpoint, count: original.transcript.count)

            try await prompt(backend, sid: sid, project: project, source: source, text: latest)
            try await waitIdle(backend, sid: sid, deadline: deadline)
            client.printCounts()
            let requests = client.requests
            try check(requests.count == 3 && client.requestCount == 3
                && client.liveSummary == 1 && client.liveContinuation == 1
                && client.liveCount == 2 && !client.denied, .streams, count: client.requestCount)
            guard requests.count == 3 else { throw Failure.streams }
            let summaryRequest = requests[1], continuation = requests[2]
            let saved = try Snapshot(root: root, sid: sid)
            guard let object = saved.session["compaction"] as? [String: Any] else { throw Failure.checkpoint }
            let checkpoint = try JSONDecoder().decode(NativeContextCheckpoint.self, from: json(object))
            try check(checkpoint.count == 1 && checkpoint.boundary == original.transcript.count
                && checkpoint.created > 0 && !checkpoint.summary.isEmpty
                && checkpoint.summary.contains(code), .checkpoint, count: checkpoint.count)
            let info = try await backend.request(method: "GET", path: "/session/" + sid, directory: project.path, body: nil)
            guard let info = info as? [String: Any], let apiCheckpoint = info["compaction"] else { throw Failure.checkpoint }
            try check(try json(apiCheckpoint) == json(object), .checkpoint, count: checkpoint.boundary)

            let expectedSummary = try NativeContextCompaction.summaryRequestMessages(
                full: full, checkpoint: nil, boundary: original.transcript.count)
            try check(summaryRequest.system == NativeContextCompaction.summaryInstructions
                && summaryRequest.tools.isEmpty && summaryRequest.messages.count == 1
                && (try json(rows(summaryRequest.messages))) == json(rows(expectedSummary))
                && source.matches(summaryRequest) && summaryRequest.effort == baseline.effort,
                      .streams, count: client.liveSummary)
            let wire = try NativeModelRequestBuilder.makeURLRequest(summaryRequest)
            guard let wireBytes = wire.httpBody,
                  let wireBody = try JSONSerialization.jsonObject(with: wireBytes) as? [String: Any] else { throw Failure.streams }
            try check(wireBody["tools"] == nil && wire.httpMethod == "POST"
                && wire.url?.path.hasSuffix("/responses") == true
                && (try budget(summaryRequest)) < context, .streams, count: client.liveSummary)
            let expectedActive = NativeContextCompaction.activeMessages(full, checkpoint: checkpoint)
            try check(continuation.system == baseline.system && !continuation.tools.isEmpty
                && source.matches(continuation) && continuation.effort == baseline.effort
                && (try json(continuation.tools)) == json(baseline.tools)
                && (try json(rows(continuation.messages))) == json(rows(expectedActive))
                && continuation.messages.count == 2 && continuation.messages.last?.content == latest,
                      .streams, count: client.liveContinuation)
            let after = try budget(continuation)
            try check(after < before && after < context, .budget, count: after)
            try check(events.compactingUpdates > 0 && events.beforeBudget == before
                && events.checkpointBudget == after, .budget, count: events.compactingUpdates)
            guard let current = saved.session["nativeContext"] as? [String: Any],
                  let input = current["input"] as? Int else { throw Failure.budget }
            let finalEstimate = try budget(continuation, messages: NativeContextCompaction.activeMessages(
                try decode(saved.transcript), checkpoint: checkpoint))
            try check(current["estimated"] as? Bool == true && current["compacting"] as? Bool == false
                && current["reserve"] as? Int == source.output && current["limit"] as? Int == context
                && input + source.output == finalEstimate
                && finalEstimate < before && finalEstimate < context,
                      .budget, count: finalEstimate)

            let finalUI = try await messages(backend, sid: sid, project: project)
            try check(saved.transcript.count == original.transcript.count + 2 && finalUI.count == oldUI.count + 2
                && (try json(Array(saved.transcript.prefix(original.transcript.count)))) == json(original.transcript)
                && (try json(Array(finalUI.prefix(oldUI.count)))) == json(oldUI)
                && (try json(finalUI)) == json(saved.visible), .history, count: finalUI.count)
            // Byte equality refers to sorted-key JSON serialization of complete original
            // rows, including IDs, timestamps, replay and parts; not just their visible text.
            try check(completed(finalUI) && events.errors == 0 && saved.active.isEmpty, .answer, count: 1)
            guard let lastRow = saved.transcript.last, lastRow["role"] as? String == "assistant",
                  let finalText = lastRow["content"] as? String,
                  let lastVisible = finalUI.last,
                  let parts = lastVisible["parts"] as? [[String: Any]] else { throw Failure.answer }
            let visibleText = parts.filter { $0["type"] as? String == "text" }
                .compactMap { $0["text"] as? String }.joined()
            try check(finalText.contains(code) && visibleText == finalText
                && !visibleText.contains("UNTRUSTED HISTORICAL DATA"), .answer, count: 1)
            try check(saved.transcript.allSatisfy { ($0["toolCalls"] as? [[String: String]] ?? []).isEmpty }
                && finalUI.allSatisfy { row in
                    (row["parts"] as? [[String: Any]] ?? []).allSatisfy { $0["type"] as? String != "tool" }
                }
                && events.permissions == 0 && events.toolParts == 0
                && (try FileManager.default.contentsOfDirectory(atPath: project.path)).isEmpty
                && (try optionalData(root.appendingPathComponent("patches.json"))) == ledgerBefore,
                      .tools, count: events.toolParts)

            await backend.shutdown()
            active = nil
            let snapshotURL = root.appendingPathComponent("native-agent/state.json")
            let snapshotBefore = try boundedData(snapshotURL, maximum: 32 * 1024 * 1024)
            // Scripted zero-generation client: reopening cannot accidentally spend a key.
            let noGeneration = NoGenerationModel()
            let reopened = try source.backend(root: root, client: noGeneration)
            active = reopened
            let reopenedUI = try await messages(reopened, sid: sid, project: project)
            let reopenedInfo = try await reopened.request(method: "GET", path: "/session/" + sid, directory: project.path, body: nil)
            let reopenedSnapshot = try Snapshot(root: root, sid: sid)
            guard let reopenedObject = (reopenedInfo as? [String: Any])?["compaction"] else { throw Failure.reopen }
            try check(noGeneration.count == 0 && (try json(reopenedObject)) == json(object)
                && (try json(reopenedSnapshot.session["compaction"] ?? NSNull())) == json(object)
                && (try json(reopenedSnapshot.transcript)) == json(saved.transcript)
                && (try json(reopenedUI)) == json(finalUI)
                && (try boundedData(snapshotURL, maximum: 32 * 1024 * 1024)) == snapshotBefore,
                      .reopen, count: noGeneration.count)
            await reopened.shutdown()
            active = nil
            try check(try optionalData(root.appendingPathComponent("patches.json")) == ledgerBefore,
                      .reopen, count: noGeneration.count)
            try check(try boundedData(source.stateURL, maximum: 32 * 1024 * 1024) == source.stateBytes,
                      .userState, count: 1)
            watchdog.cancel()
            try check(true, .verified, count: client.liveCount)
            fflush(nil)
            Darwin.exit(0)
        } catch {
            if let backend = active { await backend.shutdown() }
            watchdog.cancel()
            let category = (error as? Failure) ?? .backend
            // No NSError descriptions, HTTP bodies, answers, identifiers, URLs or paths.
            print("CHECK " + category.rawValue + " FAIL")
            fflush(nil)
            Darwin.exit(1)
        }
    }

    private static func check(_ passed: Bool, _ category: Failure, count: Int) throws {
        guard passed else { throw category }
        print("CHECK " + category.rawValue + " PASS count=\(count)")
        fflush(nil)
    }

    private static func requireIPad() throws {
        #if os(iOS) && !targetEnvironment(simulator)
        var size = 0
        guard sysctlbyname("hw.machine", nil, &size, nil, 0) == 0, size > 0, size <= 128 else { throw Failure.device }
        var machine = [CChar](repeating: 0, count: size)
        guard sysctlbyname("hw.machine", &machine, &size, nil, 0) == 0,
              String(cString: machine).hasPrefix("iPad") else { throw Failure.device }
        try check(true, .device, count: 1)
        #else
        throw Failure.device
        #endif
    }

    private struct Source {
        let stateURL: URL
        let stateBytes: Data
        let userRoot: URL
        let providerID: String
        let modelID: String
        let variant: String?
        let output: Int
        let config: [String: Any]
        private let key: String
        private let baseURL: URL

        init(statePath: String, providerID: String) throws {
            guard validPath(statePath), providerID.range(of: "^[A-Za-z0-9._-]{1,128}$", options: .regularExpression) != nil,
                  !["__proto__", "constructor", "prototype"].contains(providerID) else { throw Failure.arguments }
            let stateURL = URL(fileURLWithPath: statePath).standardizedFileURL
            guard stateURL.lastPathComponent == "state.json",
                  stateURL.deletingLastPathComponent().lastPathComponent == "native-agent" else { throw Failure.source }
            let userRoot = stateURL.deletingLastPathComponent().deletingLastPathComponent()
            guard try canonical(stateURL.path) == canonical(userRoot.path) + "/native-agent/state.json" else { throw Failure.source }
            let data = try boundedData(stateURL, maximum: 32 * 1024 * 1024)
            // Copy ONLY the selected provider metadata, never user messages or permissions.
            guard let state = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let saved = state["config"] as? [String: Any],
                  var provider = (saved["provider"] as? [String: [String: Any]])?[providerID],
                  provider["npm"] as? String == "@ai-sdk/openai",
                  let options = provider["options"] as? [String: Any], Set(options.keys) == ["baseURL"],
                  let base = options["baseURL"] as? String, let url = URLComponents(string: base),
                  url.scheme?.lowercased() == "https", url.host?.lowercased() == "agentrouter.org",
                  url.port == nil || url.port == 443, url.user == nil, url.password == nil,
                  url.query == nil, url.fragment == nil, let baseURL = url.url,
                  let models = provider["models"] as? [String: [String: Any]] else { throw Failure.config }
            let enabled = models.keys.sorted().filter { modelID in
                !(provider["blacklist"] as? [String] ?? []).contains(modelID)
                    && ((provider["whitelist"] as? [String]).map { $0.contains(modelID) } ?? true)
            }
            let prefix = providerID + "/"
            let selected = (saved["model"] as? String).flatMap { $0.hasPrefix(prefix) ? String($0.dropFirst(prefix.count)) : nil }
            let modelID = selected ?? enabled.first
            guard let modelID, var model = models[modelID],
                  !(provider["blacklist"] as? [String] ?? []).contains(modelID),
                  (provider["whitelist"] as? [String]).map({ $0.contains(modelID) }) ?? true,
                  var limits = model["limit"] as? [String: Any],
                  let savedContext = limits["context"] as? Int,
                  savedContext >= context, savedContext < 100_000_000 else { throw Failure.config }
            let savedOutput = limits["output"] as? Int ?? min(4096, savedContext / 4)
            guard savedOutput > 0, savedOutput < savedContext else { throw Failure.config }
            let output = min(savedOutput, 1000)
            // Never expand a saved window. This smaller local estimate forces auto80%.
            limits["context"] = context
            limits["output"] = output
            if let input = limits["input"] as? Int { limits["input"] = min(input, context) }
            model["limit"] = limits
            provider["models"] = [modelID: model]
            let variants = model["variants"] as? [String: [String: Any]] ?? [:]
            let variant = ["none", "minimal", "low"].compactMap { effort in
                variants.keys.sorted().first {
                    variants[$0]?["disabled"] as? Bool != true && variants[$0]?["reasoningEffort"] as? String == effort
                }
            }.first
            guard let key = try readKey(root: userRoot, id: providerID), !key.isEmpty,
                  !key.unicodeScalars.contains(where: { [0, 10, 13].contains($0.value) }) else { throw Failure.keychain }
            try rejectSecrets(provider, key: key)
            guard !containsSecret(statePath, key: key), !containsSecret(providerID, key: key) else { throw Failure.config }
            self.stateURL = stateURL
            self.stateBytes = data
            self.userRoot = userRoot
            self.providerID = providerID
            self.modelID = modelID
            self.variant = variant
            self.output = output
            self.key = key
            self.baseURL = baseURL
            self.config = ["provider": [providerID: provider], "model": providerID + "/" + modelID]
        }

        func validateRoot(_ path: String) throws {
            guard !containsSecret(path, key: key) else { throw Failure.root }
        }

        func backend(root: URL, client: NativeModelClient) throws -> NativeAgentBackend {
            let id = providerID, secret = key
            return try NativeAgentBackend(root: root, modelClient: client,
                credentialRead: { requested in requested == id ? secret : nil },
                credentialWrite: { _, _ in throw Failure.credentialWrite })
        }

        func matches(_ request: NativeModelRequest) -> Bool {
            request.baseURL == baseURL && request.apiKey == key && request.model == modelID
                && request.format == .responses && request.maxOutputTokens == output
        }
    }

    private static func readKey(root: URL, id: String) throws -> String? {
        // Stable provider account first, then the two read-only legacy fallbacks.
        var accounts = [id, root.standardizedFileURL.path + ":" + id]
        let marker = root.appendingPathComponent("native-agent/legacy-keychain-root.txt")
        if let file = try? FileHandle(forReadingFrom: marker) {
            defer { try? file.close() }
            if let data = try file.read(upToCount: 4097), data.count <= 4096,
               let path = String(data: data, encoding: .utf8), path.hasPrefix("/"),
               !data.contains(where: { $0 == 0 || $0 == 10 || $0 == 13 }) {
                let legacy = path + ":" + id
                if !accounts.contains(legacy) { accounts.append(legacy) }
            }
        }
        for account in accounts {
            let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: "kumo.native.provider", kSecAttrAccount as String: account,
                kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
            var result: CFTypeRef?
            let status = SecItemCopyMatching(query as CFDictionary, &result)
            if status == errSecItemNotFound { continue }
            guard status == errSecSuccess, let data = result as? Data,
                  let key = String(data: data, encoding: .utf8) else { throw Failure.keychain }
            return key
        }
        return nil
    }

    private static func rejectSecrets(_ value: Any, key: String) throws {
        if let object = value as? [String: Any] {
            for (name, value) in object {
                let normalized = name.lowercased().filter { $0.isLetter }
                guard !["key", "apikey", "authorization", "headers", "password", "token", "secret", "credential"].contains(normalized),
                      !containsSecret(name, key: key) else { throw Failure.config }
                try rejectSecrets(value, key: key)
            }
        } else if let array = value as? [Any] {
            for value in array { try rejectSecrets(value, key: key) }
        } else if let text = value as? String, containsSecret(text, key: key) { throw Failure.config }
    }

    private static func containsSecret(_ text: String, key: String) -> Bool {
        Data(text.utf8).range(of: Data(key.utf8)) != nil
    }

    private static func validPath(_ path: String) -> Bool {
        path.hasPrefix("/") && path.utf8.count <= 16_384
            && !path.unicodeScalars.contains { $0.value < 32 || $0.value == 127 }
    }

    private static func canonical(_ path: String) throws -> String {
        guard validPath(path) else { throw Failure.root }
        if let resolved = realpath(path, nil) {
            defer { free(resolved) }
            return String(cString: resolved)
        }
        guard errno == ENOENT else { throw Failure.root }
        var info = stat()
        // Reject dangling symlinks rather than treating them as a fresh directory.
        guard lstat(path, &info) != 0 else { throw Failure.root }
        let url = URL(fileURLWithPath: path), parent = url.deletingLastPathComponent().path
        guard parent != path, ![".", ".."].contains(url.lastPathComponent) else { throw Failure.root }
        let resolvedParent = try canonical(parent)
        return (resolvedParent == "/" ? "" : resolvedParent) + "/" + url.lastPathComponent
    }

    private static func contains(_ path: String, root: String) -> Bool {
        path == root || path.hasPrefix(root.hasSuffix("/") ? root : root + "/")
    }

    private static func prepareRoot(_ path: String, source: Source) throws -> URL {
        guard validPath(path) else { throw Failure.root }
        try source.validateRoot(path)
        let root = URL(fileURLWithPath: path, isDirectory: true).standardizedFileURL
        let resolved = try canonical(root.path), user = try canonical(source.userRoot.path)
        try source.validateRoot(root.path)
        try source.validateRoot(resolved)
        guard validPath(resolved), resolved != "/", !contains(resolved, root: user),
              !contains(user, root: resolved) else { throw Failure.root }
        if FileManager.default.fileExists(atPath: root.path) {
            guard try FileManager.default.contentsOfDirectory(atPath: root.path).isEmpty else { throw Failure.root }
        } else {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        }
        guard try canonical(root.path) == resolved,
              try FileManager.default.contentsOfDirectory(atPath: root.path).isEmpty else { throw Failure.root }
        try check(true, .root, count: 1)
        return root
    }

    private static func boundedData(_ url: URL, maximum: Int) throws -> Data {
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        guard let data = try file.read(upToCount: maximum + 1), data.count <= maximum else { throw Failure.source }
        return data
    }

    private static func optionalData(_ url: URL) throws -> Data? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        return try boundedData(url, maximum: 32 * 1024 * 1024)
    }

    private struct Snapshot {
        let session: [String: Any]
        let transcript: [[String: Any]]
        let visible: [[String: Any]]
        let active: [String: Any]

        init(root: URL, sid: String) throws {
            let data = try boundedData(root.appendingPathComponent("native-agent/state.json"), maximum: 32 * 1024 * 1024)
            guard let state = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let session = (state["sessions"] as? [[String: Any]])?.first(where: { $0["id"] as? String == sid }),
                  let transcript = (state["transcripts"] as? [String: [[String: Any]]])?[sid],
                  let visible = (state["messages"] as? [String: [[String: Any]]])?[sid],
                  let active = state["active"] as? [String: Any] else { throw Failure.backend }
            self.session = session
            self.transcript = transcript
            self.visible = visible
            self.active = active
        }
    }

    private static func prompt(_ backend: NativeAgentBackend, sid: String, project: URL, source: Source, text: String) async throws {
        var body: [String: Any] = ["parts": [["type": "text", "text": text]], "agent": "build",
                                  "model": ["providerID": source.providerID, "modelID": source.modelID]]
        if let variant = source.variant { body["variant"] = variant }
        _ = try await backend.request(method: "POST", path: "/session/" + sid + "/prompt_async", directory: project.path, body: body)
    }

    private static func waitIdle(_ backend: NativeAgentBackend, sid: String, deadline: TimeInterval) async throws {
        while ProcessInfo.processInfo.systemUptime < deadline {
            let result = try await backend.request(method: "GET", path: "/session/status", directory: nil, body: nil)
            if (result as? [String: [String: Any]])?[sid]?["type"] as? String == "idle" { return }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        throw Failure.timeout
    }

    private static func messages(_ backend: NativeAgentBackend, sid: String, project: URL) async throws -> [[String: Any]] {
        let result = try await backend.request(method: "GET", path: "/session/" + sid + "/message", directory: project.path, body: nil)
        guard let rows = result as? [[String: Any]] else { throw Failure.backend }
        return rows
    }

    private static func completed(_ messages: [[String: Any]]) -> Bool {
        messages.allSatisfy { row in
            guard let info = row["info"] as? [String: Any], info["error"] == nil else { return false }
            if info["role"] as? String == "user" { return true }
            return info["role"] as? String == "assistant" && info["finish"] as? String == "stop"
                && (info["time"] as? [String: Any])?["completed"] != nil
        }
    }

    private static func json(_ value: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    }

    private static func decode(_ stored: [[String: Any]]) throws -> [NativeModelMessage] {
        try stored.map { row in
            guard let role = row["role"] as? String, let text = row["content"] as? String,
                  let calls = row["toolCalls"] as? [[String: String]], calls.isEmpty,
                  let replay = row["replayItems"] as? [[String: Any]] else { throw Failure.fixture }
            return NativeModelMessage(role: role, content: text, replayItems: replay)
        }
    }

    private static func rows(_ messages: [NativeModelMessage]) -> [[String: Any]] {
        messages.map { message in
            var row: [String: Any] = ["role": message.role, "content": message.content,
                "toolCalls": message.toolCalls.map { ["id": $0.id, "name": $0.name, "arguments": $0.arguments] },
                "replayItems": message.replayItems]
            if let id = message.toolCallID { row["toolCallID"] = id }
            return row
        }
    }

    private static func budget(_ request: NativeModelRequest, messages: [NativeModelMessage]? = nil) throws -> Int {
        guard let reserve = request.maxOutputTokens else { throw Failure.budget }
        return try NativeContextCompaction.budget(system: request.system, messages: messages ?? request.messages,
                                                 tools: request.tools, reserve: reserve)
    }

    private final class LiveModel: NativeModelClient, @unchecked Sendable {
        private let lock = NSLock()
        private let transport = URLSessionNativeModelClient()
        private var stored: [NativeModelRequest] = []
        private var attempts = 0
        private var summaries = 0
        private var continuations = 0
        private var rejected = false
        private let limit = 2

        var requests: [NativeModelRequest] { lock.lock(); defer { lock.unlock() }; return stored }
        var requestCount: Int { lock.lock(); defer { lock.unlock() }; return attempts }
        var liveSummary: Int { lock.lock(); defer { lock.unlock() }; return summaries }
        var liveContinuation: Int { lock.lock(); defer { lock.unlock() }; return continuations }
        var liveCount: Int { lock.lock(); defer { lock.unlock() }; return summaries + continuations }
        var denied: Bool { lock.lock(); defer { lock.unlock() }; return rejected }

        func printCounts() {
            lock.lock()
            let total = attempts, summary = summaries, continuation = continuations, denied = rejected
            lock.unlock()
            print("CHECK stream_counts requests=\(total) summary=\(summary) continuation=\(continuation) denied=\(denied ? 1 : 0)")
            fflush(nil)
        }

        func stream(request: NativeModelRequest) -> AsyncThrowingStream<NativeModelEvent, Error> {
            lock.lock()
            attempts += 1
            let index = attempts - 1
            let summary = request.system == NativeContextCompaction.summaryInstructions
            let scripted = index == 0 && !summary && request.messages.count == 1
                && request.messages.first?.content == seed
            let live = index > 0 && summaries + continuations < limit
                && ((index == 1 && summary) || (index == 2 && !summary))
            if scripted || live { stored.append(request) }
            if live {
                if summary { summaries += 1 } else { continuations += 1 }
            }
            if !scripted && !live { rejected = true }
            lock.unlock()
            if scripted {
                return AsyncThrowingStream { continuation in
                    continuation.yield(.text(seedAnswer))
                    continuation.yield(.finished)
                    continuation.finish()
                }
            }
            guard live else { return AsyncThrowingStream { $0.finish(throwing: Failure.streams) } }
            // Metered passthrough: production owns networking, decoding and cancellation.
            printCounts()
            return transport.stream(request: request)
        }
    }


    private final class NoGenerationModel: NativeModelClient, @unchecked Sendable {
        private let lock = NSLock()
        private var attempts = 0
        var count: Int { lock.lock(); defer { lock.unlock() }; return attempts }
        func stream(request: NativeModelRequest) -> AsyncThrowingStream<NativeModelEvent, Error> {
            lock.lock(); attempts += 1; lock.unlock()
            return AsyncThrowingStream { $0.finish(throwing: Failure.reopen) }
        }
    }

    private final class Events: @unchecked Sendable {
        private let lock = NSLock()
        private var errorCount = 0
        private var permissionCount = 0
        private var toolCount = 0
        private var compactingCount = 0
        private var before: Int?
        private var after: Int?
        var errors: Int { lock.lock(); defer { lock.unlock() }; return errorCount }
        var permissions: Int { lock.lock(); defer { lock.unlock() }; return permissionCount }
        var toolParts: Int { lock.lock(); defer { lock.unlock() }; return toolCount }
        var compactingUpdates: Int { lock.lock(); defer { lock.unlock() }; return compactingCount }
        var beforeBudget: Int? { lock.lock(); defer { lock.unlock() }; return before }
        var checkpointBudget: Int? { lock.lock(); defer { lock.unlock() }; return after }

        func receive(_ event: [String: Any]) {
            lock.lock(); defer { lock.unlock() }
            let type = event["type"] as? String
            let properties = event["properties"] as? [String: Any] ?? [:]
            if type == "session.error" { errorCount += 1 }
            if type == "permission.asked" { permissionCount += 1 }
            if type == "message.part.updated", (properties["part"] as? [String: Any])?["type"] as? String == "tool" { toolCount += 1 }
            guard type == "session.updated", let info = properties["info"] as? [String: Any],
                  let budget = info["nativeContext"] as? [String: Any],
                  let input = budget["input"] as? Int, let reserve = budget["reserve"] as? Int else { return }
            if budget["compacting"] as? Bool == true {
                compactingCount += 1
                if before == nil { before = input + reserve }
            }
            if after == nil, (info["compaction"] as? [String: Any])?["count"] as? Int == 1,
               budget["compacting"] as? Bool == false { after = input + reserve }
        }
    }
}
