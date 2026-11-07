import Foundation
import XCTest
@testable import KumoCore

final class NativeAgentCompactionTests: XCTestCase {
    private static let context = 20_000
    private static let reserve = 1_000
    private static let summary = "Confirmed: the older request was inspected. Preserve its constraints and continue the latest request."

    private final class Credentials {
        private let lock = NSLock()
        private var values: [String: String] = [:]

        func read(_ id: String) throws -> String? {
            lock.lock(); defer { lock.unlock() }
            return values[id]
        }

        func write(_ id: String, _ value: String?) throws {
            lock.lock(); defer { lock.unlock() }
            values[id] = value
        }
    }

    private struct Script {
        var events: [NativeModelEvent]
        var failure: Error? = nil
        var holdOpen = false
    }

    private final class ScriptedModel: NativeModelClient {
        private let lock = NSLock()
        private var normalScripts: [Script] = []
        private var summaryScripts: [Script] = []
        private var recordedRequests: [NativeModelRequest] = []
        private var continuations: [Int: AsyncThrowingStream<NativeModelEvent, Error>.Continuation] = [:]
        private var terminatedRequests = Set<Int>()

        var requests: [NativeModelRequest] {
            lock.lock(); defer { lock.unlock() }
            return recordedRequests
        }

        func enqueue(_ script: Script, summary: Bool = false) {
            lock.lock(); defer { lock.unlock() }
            if summary { summaryScripts.append(script) }
            else { normalScripts.append(script) }
        }

        func continuation(at index: Int) -> AsyncThrowingStream<NativeModelEvent, Error>.Continuation? {
            lock.lock(); defer { lock.unlock() }
            return continuations[index]
        }

        func terminated(at index: Int) -> Bool {
            lock.lock(); defer { lock.unlock() }
            return terminatedRequests.contains(index)
        }

        private func recordTermination(at index: Int) {
            lock.lock(); defer { lock.unlock() }
            terminatedRequests.insert(index)
            continuations.removeValue(forKey: index)
        }

        // Select the script under one lock; test-side polling never races queue mutation.
        private func next(_ request: NativeModelRequest) -> (Int, Script) {
            lock.lock(); defer { lock.unlock() }
            let index = recordedRequests.count
            recordedRequests.append(request)
            let isSummary = request.system == NativeContextCompaction.summaryInstructions
            let script: Script
            if isSummary, !summaryScripts.isEmpty { script = summaryScripts.removeFirst() }
            else if !isSummary, !normalScripts.isEmpty { script = normalScripts.removeFirst() }
            else { script = Script(events: [], failure: NativeModelClientError.providerFailure) }
            return (index, script)
        }

        func stream(request: NativeModelRequest) -> AsyncThrowingStream<NativeModelEvent, Error> {
            let (index, script) = next(request)
            return AsyncThrowingStream { continuation in
                self.lock.lock()
                self.continuations[index] = continuation
                self.lock.unlock()
                continuation.onTermination = { _ in self.recordTermination(at: index) }
                for event in script.events { continuation.yield(event) }
                if let failure = script.failure { continuation.finish(throwing: failure) }
                else if !script.holdOpen { continuation.finish() }
            }
        }
    }

    private final class Events {
        private let lock = NSLock()
        private var received: [[String: Any]] = []

        var values: [[String: Any]] {
            lock.lock(); defer { lock.unlock() }
            return received
        }

        func receive(_ event: [String: Any]) {
            lock.lock(); defer { lock.unlock() }
            received.append(event)
        }
    }

    private struct Fixture {
        let root: URL
        let credentials: Credentials
        let model: ScriptedModel
        let backend: NativeAgentBackend
        let id: String
    }

    private func temporaryRoot() throws -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("native-compaction-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return root
    }

    private func backend(_ root: URL, _ model: ScriptedModel, _ credentials: Credentials) throws -> NativeAgentBackend {
        let backend = try NativeAgentBackend(root: root, modelClient: model,
                                             credentialRead: credentials.read, credentialWrite: credentials.write)
        addTeardownBlock { await backend.shutdown() }
        return backend
    }

    @discardableResult
    private func request(_ backend: NativeAgentBackend, _ method: String, _ path: String,
                         directory: String? = nil, body: [String: Any]? = nil) async throws -> Any {
        try await backend.request(method: method, path: path, directory: directory, body: body)
    }

    private func fixture(format: NativeProviderFormat = .responses) async throws -> Fixture {
        let root = try temporaryRoot()
        let credentials = Credentials()
        let model = ScriptedModel()
        let backend = try backend(root, model, credentials)
        let npm = format == .responses ? "@ai-sdk/openai" : "@ai-sdk/openai-compatible"
        _ = try await request(backend, "PATCH", "/global/config", body: [
            "model": "test/scripted",
            "provider": ["test": ["name": "Scripted", "npm": npm,
                                    "options": ["baseURL": "https://example.invalid/v1"],
                                    "models": ["scripted": ["name": "Scripted", "limit": ["context": 100_000, "output": Self.reserve]]]]]
        ])
        // Only injected, per-fixture credentials are used, never the device Keychain.
        _ = try await request(backend, "PUT", "/auth/test", body: ["type": "api", "key": UUID().uuidString])
        let result = try await request(backend, "POST", "/session", directory: root.path, body: [
            "permission": [["permission": "*", "pattern": "*", "action": "allow"]]
        ])
        let session = try XCTUnwrap(result as? [String: Any])
        return Fixture(root: root, credentials: credentials, model: model, backend: backend,
                       id: try XCTUnwrap(session["id"] as? String))
    }

    private func setContext(_ fixture: Fixture, _ context: Int = NativeAgentCompactionTests.context) async throws {
        _ = try await request(fixture.backend, "PATCH", "/global/config", body: [
            "provider": ["test": ["models": ["scripted": ["limit": ["context": context]]]]]
        ])
    }

    private func prompt(_ backend: NativeAgentBackend, _ id: String, _ text: String) async throws {
        _ = try await request(backend, "POST", "/session/\(id)/prompt_async", body: [
            "parts": [["type": "text", "text": text]], "agent": "build",
            "model": ["providerID": "test", "modelID": "scripted"]
        ])
    }

    private func waitUntil(_ condition: () async throws -> Bool,
                           file: StaticString = #filePath, line: UInt = #line) async throws {
        for _ in 0..<500 {
            if try await condition() { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        throw NSError(domain: "NativeAgentCompactionTests.WaitTimeout", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "\(file):\(line): Timed out waiting for compaction state"])
    }

    private func waitIdle(_ backend: NativeAgentBackend, _ id: String) async throws {
        try await waitUntil {
            let statuses = try await self.request(backend, "GET", "/session/status") as? [String: [String: Any]]
            return statuses?[id]?["type"] as? String == "idle"
        }
    }

    private func messages(_ backend: NativeAgentBackend, _ id: String) async throws -> [[String: Any]] {
        let result = try await request(backend, "GET", "/session/\(id)/message")
        return try XCTUnwrap(result as? [[String: Any]])
    }

    private func sessionInfo(_ backend: NativeAgentBackend, _ id: String) async throws -> [String: Any] {
        let result = try await request(backend, "GET", "/session/\(id)")
        return try XCTUnwrap(result as? [String: Any])
    }

    private func snapshot(_ root: URL) throws -> [String: Any] {
        let url = root.appendingPathComponent("native-agent/state.json")
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    }

    private func transcript(_ fixture: Fixture) throws -> [[String: Any]] {
        let state = try snapshot(fixture.root)
        let transcripts = try XCTUnwrap(state["transcripts"] as? [String: [[String: Any]]])
        return try XCTUnwrap(transcripts[fixture.id])
    }

    private func storedSession(_ fixture: Fixture) throws -> [String: Any] {
        let sessions = try XCTUnwrap(try snapshot(fixture.root)["sessions"] as? [[String: Any]])
        return try XCTUnwrap(sessions.first { $0["id"] as? String == fixture.id })
    }

    private func checkpoint(_ fixture: Fixture) throws -> [String: Any] {
        try XCTUnwrap(try storedSession(fixture)["compaction"] as? [String: Any])
    }

    private func checkpointData(_ fixture: Fixture) throws -> Data {
        try json(["compaction": try storedSession(fixture)["compaction"] ?? NSNull()])
    }

    private func json(_ value: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    }

    private func rows(_ messages: [NativeModelMessage]) -> [[String: Any]] {
        messages.map { message in
            var row: [String: Any] = [
                "role": message.role, "content": message.content,
                "toolCalls": message.toolCalls.map { ["id": $0.id, "name": $0.name, "arguments": $0.arguments] },
                "replayItems": message.replayItems
            ]
            if let id = message.toolCallID { row["toolCallID"] = id }
            return row
        }
    }

    private func decoded(_ rows: [[String: Any]]) throws -> [NativeModelMessage] {
        try rows.map { row in
            let calls = try XCTUnwrap(row["toolCalls"] as? [[String: String]])
            return NativeModelMessage(role: try XCTUnwrap(row["role"] as? String),
                                      content: try XCTUnwrap(row["content"] as? String),
                                      toolCalls: try calls.map {
                                          NativeToolCall(id: try XCTUnwrap($0["id"]), name: try XCTUnwrap($0["name"]),
                                                         arguments: try XCTUnwrap($0["arguments"]))
                                      }, toolCallID: row["toolCallID"] as? String,
                                      replayItems: try XCTUnwrap(row["replayItems"] as? [[String: Any]]))
        }
    }

    private func budget(_ request: NativeModelRequest, messages: [NativeModelMessage]? = nil) throws -> Int {
        try NativeContextCompaction.budget(system: request.system, messages: messages ?? request.messages,
                                          tools: request.tools, reserve: try XCTUnwrap(request.maxOutputTokens))
    }

    private func largeText(_ label: String, size: Int = 9_000) -> String {
        label + "\n" + String(repeating: "x", count: size)
    }

    private func call(_ id: String, _ name: String, _ args: [String: Any]) throws -> NativeToolCall {
        NativeToolCall(id: id, name: name, arguments: String(decoding: try json(args), as: UTF8.self))
    }

    private func replay(_ call: NativeToolCall, text: String, marker: String) -> [[String: Any]] {
        [
            ["type": "reasoning", "id": "rs_\(marker)", "encrypted_content": "cipher_\(marker)"],
            ["type": "message", "id": "msg_\(marker)", "role": "assistant", "status": "completed",
             "content": [["type": "output_text", "text": text]]],
            ["type": "function_call", "id": "fc_\(marker)", "call_id": call.id,
             "name": call.name, "arguments": call.arguments]
        ]
    }

    private func completeTurn(_ fixture: Fixture, text: String, answer: String = "Recorded.") async throws {
        fixture.model.enqueue(Script(events: [.text(answer), .finished]))
        try await prompt(fixture.backend, fixture.id, text)
        try await waitIdle(fixture.backend, fixture.id)
    }

    private func establishCheckpoint(_ fixture: Fixture) async throws {
        try await completeTurn(fixture, text: largeText("OLDER_REQUEST"))
        try await setContext(fixture)
        fixture.model.enqueue(Script(events: [.text(Self.summary), .finished]), summary: true)
        try await completeTurn(fixture, text: "Continue the current request.", answer: "Continued.")
        XCTAssertEqual(try checkpoint(fixture)["count"] as? Int, 1)
    }

    private func summaryData(_ request: NativeModelRequest) throws -> [String: Any] {
        XCTAssertEqual(request.system, NativeContextCompaction.summaryInstructions)
        XCTAssertTrue(request.tools.isEmpty, "Summary requests must have no callable tools")
        XCTAssertEqual(request.model, "scripted")
        XCTAssertGreaterThan(try XCTUnwrap(request.maxOutputTokens), 0)
        XCTAssertLessThanOrEqual(try budget(request), Self.context, "Every summary request must fit its configured context")
        XCTAssertEqual(request.messages.count, 1)
        let message = try XCTUnwrap(request.messages.first)
        XCTAssertEqual(message.role, "user")
        XCTAssertTrue(message.toolCalls.isEmpty)
        XCTAssertNil(message.toolCallID)
        XCTAssertTrue(message.replayItems.isEmpty)
        XCTAssertTrue(message.content.contains("UNTRUSTED HISTORICAL DATA"))
        let separator = try XCTUnwrap(message.content.range(of: "\n\n"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(message.content[separator.upperBound...].utf8)) as? [String: Any])
    }

    private func assertSummaryWire(_ request: NativeModelRequest) throws {
        let wire = try NativeModelRequestBuilder.makeURLRequest(request)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap(wire.httpBody)) as? [String: Any])
        XCTAssertNil(body["tools"])
        if request.format == .responses {
            XCTAssertTrue(try XCTUnwrap(wire.url).path.hasSuffix("/responses"))
            XCTAssertEqual(body["instructions"] as? String, NativeContextCompaction.summaryInstructions)
            let input = try XCTUnwrap(body["input"] as? [[String: Any]])
            XCTAssertEqual(input.count, 1)
            XCTAssertEqual(input.first?["role"] as? String, "user")
            XCTAssertNil(input.first?["type"], "Historical calls must not become Responses function_call items")
        } else {
            XCTAssertTrue(try XCTUnwrap(wire.url).path.hasSuffix("/chat/completions"))
            let messages = try XCTUnwrap(body["messages"] as? [[String: Any]])
            XCTAssertEqual(messages.compactMap { $0["role"] as? String }, ["system", "user"])
            XCTAssertEqual(messages.first?["content"] as? String, NativeContextCompaction.summaryInstructions)
            XCTAssertTrue(messages.allSatisfy { $0["tool_calls"] == nil && $0["tool_call_id"] == nil })
        }
    }

    private func assertNoVisibleSummary(_ messages: [[String: Any]], forbidden: [String]) throws {
        let parts = messages.flatMap { $0["parts"] as? [[String: Any]] ?? [] }
        let encoded = String(decoding: try json(parts), as: UTF8.self)
        for text in forbidden { XCTAssertFalse(encoded.contains(text), "Summary data must never enter visible chat: \(text.prefix(80))") }
    }

    private func assertContext(_ info: [String: Any], compacting: Bool, fits: Bool = true) throws {
        let context = try XCTUnwrap(info["nativeContext"] as? [String: Any])
        let input = try XCTUnwrap(context["input"] as? Int)
        XCTAssertGreaterThan(input, 0)
        XCTAssertEqual(context["reserve"] as? Int, Self.reserve)
        XCTAssertEqual(context["limit"] as? Int, Self.context)
        XCTAssertEqual(context["estimated"] as? Bool, true)
        XCTAssertEqual(context["compacting"] as? Bool, compacting)
        if fits { XCTAssertLessThan(input + Self.reserve, Self.context) }
    }

    func testAutomaticCompactionPreservesHistoryAndReopensWithSummaryInBothFormats() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let fixture = try await fixture(format: format)
            let file = fixture.root.appendingPathComponent("evidence.txt")
            try "confirmed file contents".write(to: file, atomically: true, encoding: .utf8)
            let oldCall = try call("old-read", "read", ["filePath": "evidence.txt"])
            let oldReplay = format == .responses ? replay(oldCall, text: "Inspecting older evidence.", marker: "old_private") : []
            fixture.model.enqueue(Script(events: [.text("Inspecting older evidence."), .toolCall(oldCall), .replay(oldReplay), .finished]))
            fixture.model.enqueue(Script(events: [.text("Older inspection complete."), .finished]))
            let older = largeText("OLDER_REQUEST")
            try await prompt(fixture.backend, fixture.id, older)
            try await waitIdle(fixture.backend, fixture.id)
            XCTAssertEqual(fixture.model.requests.count, 2, "The 100000 context must not compact these completed turns")
            let originalTranscript = try transcript(fixture)
            let originalMessages = try await messages(fixture.backend, fixture.id)
            try await setContext(fixture)

            let latest = "LATEST_REQUEST: read the evidence again without modifying it."
            let baseline = try XCTUnwrap(fixture.model.requests.first)
            let originalBudget = try budget(baseline, messages: try decoded(originalTranscript) + [NativeModelMessage(role: "user", content: latest)])
            XCTAssertGreaterThanOrEqual(originalBudget, Self.context * 80 / 100)
            XCTAssertLessThan(originalBudget, Self.context, "Exercise the 80% trigger, not only a hard context overflow")
            let currentCall = try call("current-read", "read", ["filePath": "evidence.txt"])
            let currentReplay = format == .responses ? replay(currentCall, text: "Inspecting current evidence.", marker: "keep_private") : []
            fixture.model.enqueue(Script(events: [.text(Self.summary), .finished]), summary: true)
            fixture.model.enqueue(Script(events: [.text("Inspecting current evidence."), .toolCall(currentCall), .replay(currentReplay), .finished]))
            fixture.model.enqueue(Script(events: [.text("Current inspection complete."), .finished]))
            try await prompt(fixture.backend, fixture.id, latest)
            try await waitIdle(fixture.backend, fixture.id)

            let requests = fixture.model.requests
            XCTAssertEqual(requests.count, 5)
            let summaryRequest = try XCTUnwrap(requests.dropFirst(2).first)
            let history = try summaryData(summaryRequest)
            try assertSummaryWire(summaryRequest)
            XCTAssertEqual(summaryRequest.format, format)
            XCTAssertEqual(summaryRequest.apiKey, baseline.apiKey)
            XCTAssertNil(history["priorSummary"])
            let historicalRows = try XCTUnwrap(history["messages"] as? [[String: Any]])
            XCTAssertEqual(historicalRows.first?["content"] as? String, older)
            XCTAssertTrue(historicalRows.contains { $0["toolCallID"] as? String == oldCall.id && ($0["content"] as? String ?? "").contains("confirmed file contents") })
            XCTAssertFalse(summaryRequest.messages[0].content.contains(latest))
            for marker in ["rs_old_private", "cipher_old_private", "msg_old_private", "fc_old_private"] {
                XCTAssertFalse(summaryRequest.messages[0].content.contains(marker), "Opaque replay must not be included in plain textual history")
            }
            let firstContinuation = try XCTUnwrap(requests.dropFirst(3).first)
            XCTAssertFalse(firstContinuation.tools.isEmpty)
            XCTAssertEqual(firstContinuation.messages.count, 2)
            XCTAssertEqual(firstContinuation.messages.first?.role, "user")
            XCTAssertTrue(firstContinuation.messages.first?.content.contains("UNTRUSTED HISTORICAL DATA") == true)
            XCTAssertTrue(firstContinuation.messages.first?.content.hasSuffix(Self.summary) == true)
            XCTAssertEqual(firstContinuation.messages.last?.content, latest)

            let savedCheckpoint = try checkpoint(fixture)
            let boundary = try XCTUnwrap(savedCheckpoint["boundary"] as? Int)
            XCTAssertEqual(boundary, originalTranscript.count)
            XCTAssertEqual(savedCheckpoint["summary"] as? String, Self.summary)
            XCTAssertEqual(savedCheckpoint["count"] as? Int, 1)
            XCTAssertGreaterThan(try XCTUnwrap(savedCheckpoint["created"] as? Int64), 0)
            let savedTranscript = try transcript(fixture)
            XCTAssertEqual(try json(Array(savedTranscript.prefix(originalTranscript.count))), try json(originalTranscript))
            let finalContinuation = try XCTUnwrap(requests.last)
            XCTAssertEqual(try json(rows(Array(finalContinuation.messages.dropFirst()))),
                           try json(Array(savedTranscript.dropFirst(boundary).dropLast())), "Keep the complete current user/tool chain byte-for-byte")
            XCTAssertTrue(finalContinuation.messages.contains { $0.toolCallID == currentCall.id && $0.content.contains("confirmed file contents") })
            XCTAssertLessThan(try budget(firstContinuation), originalBudget)
            XCTAssertLessThan(try budget(finalContinuation), Self.context)
            let savedMessages = try await messages(fixture.backend, fixture.id)
            XCTAssertEqual(try json(Array(savedMessages.prefix(originalMessages.count))), try json(originalMessages))
            try assertNoVisibleSummary(savedMessages, forbidden: [Self.summary, "UNTRUSTED HISTORICAL DATA"])
            let info = try await sessionInfo(fixture.backend, fixture.id)
            XCTAssertEqual(try json(try XCTUnwrap(info["compaction"] as? [String: Any])), try json(savedCheckpoint))
            try assertContext(info, compacting: false)

            await fixture.backend.shutdown()
            let reopenedModel = ScriptedModel()
            let reopened = try backend(fixture.root, reopenedModel, fixture.credentials)
            let reopenedMessages = try await messages(reopened, fixture.id)
            XCTAssertEqual(try json(reopenedMessages), try json(savedMessages))
            XCTAssertTrue(reopenedModel.requests.isEmpty, "Opening a session must not make a billable request")
            reopenedModel.enqueue(Script(events: [.text("Resumed using the saved handoff."), .finished]))
            try await prompt(reopened, fixture.id, "Continue after reopening.")
            try await waitIdle(reopened, fixture.id)
            XCTAssertEqual(reopenedModel.requests.count, 1)
            let reopenedRequest = try XCTUnwrap(reopenedModel.requests.first)
            XCTAssertFalse(reopenedRequest.tools.isEmpty)
            XCTAssertTrue(reopenedRequest.messages.first?.content.hasSuffix(Self.summary) == true)
            XCTAssertFalse(reopenedRequest.messages.contains { $0.content == older })
            XCTAssertTrue(reopenedRequest.messages.contains { $0.toolCallID == currentCall.id })
            let reopenedInfo = try await sessionInfo(reopened, fixture.id)
            XCTAssertEqual(try json(try XCTUnwrap(reopenedInfo["compaction"] as? [String: Any])), try json(savedCheckpoint))
            await reopened.shutdown()
        }
    }

    func testCompactionBetweenToolStepsRetainsLatestFullUserTurnAndSafePair() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let fixture = try await fixture(format: format)
            let contents = String(repeating: "e", count: 4_500)
            try contents.write(to: fixture.root.appendingPathComponent("chain.txt"), atomically: true, encoding: .utf8)
            try await completeTurn(fixture, text: largeText("OLDER_SMALLER_TURN", size: 6_000))
            let originalTranscript = try transcript(fixture)
            let originalMessages = try await messages(fixture.backend, fixture.id)
            try await setContext(fixture)
            let latest = "LATEST_TOOL_TURN: inspect chain.txt and report its actual contents."
            let read = try call("retained-read", "read", ["filePath": "chain.txt"])
            let items = format == .responses ? replay(read, text: "Reading current evidence.", marker: "retained") : []
            fixture.model.enqueue(Script(events: [.text("Reading current evidence."), .toolCall(read), .replay(items), .finished]))
            fixture.model.enqueue(Script(events: [.text(Self.summary), .finished]), summary: true)
            fixture.model.enqueue(Script(events: [.text("The current evidence was read."), .finished]))
            try await prompt(fixture.backend, fixture.id, latest)
            try await waitIdle(fixture.backend, fixture.id)

            let requests = fixture.model.requests
            XCTAssertEqual(requests.count, 4, "One initial tool step, one summary, one continuation")
            let initialStep = try XCTUnwrap(requests.dropFirst().first)
            XCTAssertFalse(initialStep.tools.isEmpty)
            XCTAssertLessThan(try budget(initialStep), Self.context * 80 / 100)
            let summaryRequest = try XCTUnwrap(requests.dropFirst(2).first)
            let history = try summaryData(summaryRequest)
            let historicalRows = try XCTUnwrap(history["messages"] as? [[String: Any]])
            XCTAssertEqual(historicalRows.count, originalTranscript.count)
            XCTAssertFalse(summaryRequest.messages[0].content.contains(latest))
            XCTAssertFalse(summaryRequest.messages[0].content.contains(read.id))
            let savedTranscript = try transcript(fixture)
            let continuation = try XCTUnwrap(requests.last)
            let boundary = try XCTUnwrap(try checkpoint(fixture)["boundary"] as? Int)
            XCTAssertEqual(boundary, originalTranscript.count)
            let retained = Array(savedTranscript.dropFirst(boundary).dropLast())
            XCTAssertEqual(retained.first?["role"] as? String, "user")
            XCTAssertEqual(retained.first?["content"] as? String, latest)
            XCTAssertEqual(try json(rows(Array(continuation.messages.dropFirst()))), try json(retained))
            let assistant = try XCTUnwrap(continuation.messages.first { $0.toolCalls.contains { $0.id == read.id } })
            XCTAssertEqual(assistant.toolCalls.count, 1)
            XCTAssertEqual(assistant.toolCalls[0].arguments, read.arguments)
            XCTAssertEqual(try json(assistant.replayItems), try json(items))
            XCTAssertTrue(continuation.messages.contains { $0.role == "tool" && $0.toolCallID == read.id && $0.content.contains(contents) })
            let uncompacted = try budget(initialStep, messages: try decoded(Array(savedTranscript.dropLast())))
            XCTAssertGreaterThanOrEqual(uncompacted, Self.context * 80 / 100)
            XCTAssertLessThan(try budget(continuation), uncompacted)
            XCTAssertLessThan(try budget(continuation), Self.context)
            XCTAssertEqual(try json(Array(savedTranscript.prefix(originalTranscript.count))), try json(originalTranscript))
            let savedMessages = try await messages(fixture.backend, fixture.id)
            XCTAssertEqual(try json(Array(savedMessages.prefix(originalMessages.count))), try json(originalMessages))
            try assertNoVisibleSummary(savedMessages, forbidden: [Self.summary])
        }
    }

    func testInvalidSummariesNeverCommitOrContinueWithOrWithoutPriorCheckpoint() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            for hasPriorCheckpoint in [false, true] {
                for mode in ["failure", "tool", "tool-preview", "replay-tool", "empty", "whitespace", "truncated"] {
                    let fixture = try await fixture(format: format)
                    if hasPriorCheckpoint {
                        try await establishCheckpoint(fixture)
                        try await setContext(fixture, 100_000)
                    }
                    try await completeTurn(fixture, text: largeText("OLDER_FAILURE_HISTORY"))
                    try await setContext(fixture)
                    let priorCheckpoint = try checkpointData(fixture)
                    let originalTranscript = try transcript(fixture)
                    let originalMessages = try await messages(fixture.backend, fixture.id)
                    let count = fixture.model.requests.count
                    let forbidden = "INVALID_SUMMARY_\(mode)"
                    let write = try call("summary-write", "write", ["filePath": "must-not-exist.txt", "content": "unauthorized summary write"])
                    let script: Script
                    switch mode {
                    case "failure": script = Script(events: [.text(forbidden)], failure: NativeModelClientError.providerFailure)
                    case "tool": script = Script(events: [.text(forbidden), .toolCall(write), .finished])
                    case "tool-preview": script = Script(events: [.text(forbidden), .toolStarted(id: write.id, name: write.name), .finished])
                    case "replay-tool": script = Script(events: [.text(forbidden), .replay(replay(write, text: forbidden, marker: "invalid")), .finished])
                    case "empty": script = Script(events: [.finished])
                    case "whitespace": script = Script(events: [.text(" \n\t "), .finished])
                    default: script = Script(events: [.text(forbidden)])
                    }
                    fixture.model.enqueue(script, summary: true)
                    fixture.model.enqueue(Script(events: [.text("UNEXPECTED_NORMAL_CONTINUATION"), .finished]))
                    try await prompt(fixture.backend, fixture.id, "LATEST_FAILURE_TURN: preserve the history.")
                    try await waitIdle(fixture.backend, fixture.id)

                    XCTAssertEqual(fixture.model.requests.count, count + 1, "\(format.rawValue)/\(mode)/prior=\(hasPriorCheckpoint): no normal request or retry after a failed summary")
                    _ = try summaryData(try XCTUnwrap(fixture.model.requests.dropFirst(count).first))
                    XCTAssertEqual(try checkpointData(fixture), priorCheckpoint, "Even an existing checkpoint must remain byte-for-byte unchanged")
                    let savedTranscript = try transcript(fixture)
                    XCTAssertEqual(try json(Array(savedTranscript.prefix(originalTranscript.count))), try json(originalTranscript))
                    XCTAssertEqual(savedTranscript.count, originalTranscript.count + 2)
                    XCTAssertEqual(savedTranscript.last?["content"] as? String, "", "Partial summary text is not an assistant transcript")
                    XCTAssertTrue(try XCTUnwrap(savedTranscript.last?["toolCalls"] as? [[String: String]]).isEmpty)
                    let savedMessages = try await messages(fixture.backend, fixture.id)
                    XCTAssertEqual(try json(Array(savedMessages.prefix(originalMessages.count))), try json(originalMessages))
                    XCTAssertNotNil((savedMessages.last?["info"] as? [String: Any])?["error"])
                    try assertNoVisibleSummary(savedMessages, forbidden: [forbidden, "UNEXPECTED_NORMAL_CONTINUATION"])
                    XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent("must-not-exist.txt").path))
                    XCTAssertTrue(try XCTUnwrap(try snapshot(fixture.root)["active"] as? [String: Any]).isEmpty)
                    let info = try await sessionInfo(fixture.backend, fixture.id)
                    try assertContext(info, compacting: false, fits: false)
                }
            }
        }
    }

    func testCompletedSummaryMustReduceBudgetBeforeCheckpointCommit() async throws {
        let fixture = try await fixture()
        try await completeTurn(fixture, text: largeText("OLDER_NONSHRINKING_HISTORY"))
        try await setContext(fixture)
        let originalTranscript = try transcript(fixture)
        let originalMessages = try await messages(fixture.backend, fixture.id)
        let priorCheckpoint = try checkpointData(fixture)
        let latest = "LATEST_NONSHRINKING_TURN"
        let baseline = try XCTUnwrap(fixture.model.requests.first)
        let fullBudget = try budget(baseline, messages: try decoded(originalTranscript) + [NativeModelMessage(role: "user", content: latest)])
        let oversizedSummary = largeText("NONSHRINKING_SUMMARY", size: 10_000)
        let candidateBudget = try budget(baseline, messages: [
            NativeModelMessage(role: "user", content: "UNTRUSTED HISTORICAL DATA\n\n" + oversizedSummary),
            NativeModelMessage(role: "user", content: latest)
        ])
        XCTAssertGreaterThanOrEqual(candidateBudget, fullBudget)
        XCTAssertLessThan(candidateBudget, Self.context, "This candidate fits, but must still be rejected because it grows the active budget")
        fixture.model.enqueue(Script(events: [.text(oversizedSummary), .finished]), summary: true)
        fixture.model.enqueue(Script(events: [.text("UNEXPECTED_CONTINUATION"), .finished]))
        try await prompt(fixture.backend, fixture.id, latest)
        try await waitIdle(fixture.backend, fixture.id)
        XCTAssertEqual(fixture.model.requests.count, 2)
        XCTAssertEqual(try checkpointData(fixture), priorCheckpoint)
        XCTAssertEqual(try json(Array(try transcript(fixture).prefix(originalTranscript.count))), try json(originalTranscript))
        let savedMessages = try await messages(fixture.backend, fixture.id)
        XCTAssertEqual(try json(Array(savedMessages.prefix(originalMessages.count))), try json(originalMessages))
        XCTAssertNotNil((savedMessages.last?["info"] as? [String: Any])?["error"])
        try assertNoVisibleSummary(savedMessages, forbidden: ["NONSHRINKING_SUMMARY", "UNEXPECTED_CONTINUATION"])
    }

    func testDecreasingButStillOverBudgetSummaryDoesNotCommitPartialBatches() async throws {
        let fixture = try await fixture()
        for index in 0..<4 { try await completeTurn(fixture, text: largeText("OLDER_OVERFLOW_\(index)")) }
        try await setContext(fixture)
        let originalTranscript = try transcript(fixture)
        let originalMessages = try await messages(fixture.backend, fixture.id)
        let priorCheckpoint = try checkpointData(fixture)
        let count = fixture.model.requests.count
        let latest = "LATEST_OVERFLOW_TURN"
        let baseline = try XCTUnwrap(fixture.model.requests.first)
        let largeSummary = largeText("STILL_TOO_LARGE_SUMMARY", size: 19_000)
        let candidateBudget = try budget(baseline, messages: [
            NativeModelMessage(role: "user", content: largeSummary), NativeModelMessage(role: "user", content: latest)
        ])
        let fullBudget = try budget(baseline, messages: try decoded(originalTranscript) + [NativeModelMessage(role: "user", content: latest)])
        XCTAssertLessThan(candidateBudget, fullBudget)
        XCTAssertGreaterThan(candidateBudget, Self.context)
        for _ in 0..<8 { fixture.model.enqueue(Script(events: [.text(largeSummary), .finished]), summary: true) }
        fixture.model.enqueue(Script(events: [.text("UNEXPECTED_CONTINUATION"), .finished]))
        try await prompt(fixture.backend, fixture.id, latest)
        try await waitIdle(fixture.backend, fixture.id)
        let requests = Array(fixture.model.requests.dropFirst(count))
        XCTAssertFalse(requests.isEmpty)
        for request in requests { _ = try summaryData(request) }
        XCTAssertEqual(try checkpointData(fixture), priorCheckpoint, "No intermediate checkpoint may escape an unsuccessful batch sequence")
        XCTAssertEqual(try json(Array(try transcript(fixture).prefix(originalTranscript.count))), try json(originalTranscript))
        let savedMessages = try await messages(fixture.backend, fixture.id)
        XCTAssertEqual(try json(Array(savedMessages.prefix(originalMessages.count))), try json(originalMessages))
        XCTAssertNotNil((savedMessages.last?["info"] as? [String: Any])?["error"])
        try assertNoVisibleSummary(savedMessages, forbidden: ["STILL_TOO_LARGE_SUMMARY", "UNEXPECTED_CONTINUATION"])
    }

    func testStopWhileSummaryIsStreamingNeverCommitsAndReloadUsesPriorCheckpoint() async throws {
        let fixture = try await fixture()
        try await establishCheckpoint(fixture)
        try await setContext(fixture, 100_000)
        try await completeTurn(fixture, text: largeText("OLDER_STOP_HISTORY"))
        try await setContext(fixture)
        let priorCheckpoint = try checkpointData(fixture)
        let originalTranscript = try transcript(fixture)
        let originalMessages = try await messages(fixture.backend, fixture.id)
        let events = Events()
        await fixture.backend.setEventHandler(events.receive)
        let index = fixture.model.requests.count
        let partial = "PARTIAL_SUMMARY_MUST_NEVER_COMMIT"
        fixture.model.enqueue(Script(events: [.text(partial)], holdOpen: true), summary: true)
        fixture.model.enqueue(Script(events: [.text("UNEXPECTED_CONTINUATION"), .finished]))
        try await prompt(fixture.backend, fixture.id, "LATEST_STOP_TURN")
        try await waitUntil {
            let info = try await self.sessionInfo(fixture.backend, fixture.id)
            return fixture.model.continuation(at: index) != nil && (info["nativeContext"] as? [String: Any])?["compacting"] as? Bool == true
        }
        _ = try summaryData(try XCTUnwrap(fixture.model.requests.dropFirst(index).first))
        XCTAssertEqual(try checkpointData(fixture), priorCheckpoint)
        let streamingMessages = try await messages(fixture.backend, fixture.id)
        try assertNoVisibleSummary(streamingMessages, forbidden: [partial])
        try assertContext(try await sessionInfo(fixture.backend, fixture.id), compacting: true)
        let stream = try XCTUnwrap(fixture.model.continuation(at: index))
        _ = try await request(fixture.backend, "POST", "/session/\(fixture.id)/abort")
        // A late successful terminal event must not let the cancelled run commit.
        stream.yield(.text(" late summary data"))
        stream.yield(.finished)
        stream.finish()
        try await waitIdle(fixture.backend, fixture.id)
        try await waitUntil { fixture.model.terminated(at: index) }
        XCTAssertEqual(fixture.model.requests.count, index + 1)
        XCTAssertEqual(try checkpointData(fixture), priorCheckpoint)
        XCTAssertEqual(try json(Array(try transcript(fixture).prefix(originalTranscript.count))), try json(originalTranscript))
        let stoppedMessages = try await messages(fixture.backend, fixture.id)
        XCTAssertEqual(try json(Array(stoppedMessages.prefix(originalMessages.count))), try json(originalMessages))
        XCTAssertNotNil((stoppedMessages.last?["info"] as? [String: Any])?["error"])
        try assertNoVisibleSummary(stoppedMessages, forbidden: [partial, "late summary data", "UNEXPECTED_CONTINUATION"])
        XCTAssertFalse(String(decoding: try json(events.values), as: UTF8.self).contains(partial), "Private summary deltas must not be published to chat")
        try assertContext(try await sessionInfo(fixture.backend, fixture.id), compacting: false)

        await fixture.backend.shutdown()
        let reopenedModel = ScriptedModel()
        let reopened = try backend(fixture.root, reopenedModel, fixture.credentials)
        XCTAssertTrue(reopenedModel.requests.isEmpty)
        let reopenedMessages = try await messages(reopened, fixture.id)
        XCTAssertEqual(try json(reopenedMessages), try json(stoppedMessages))
        let info = try await sessionInfo(reopened, fixture.id)
        XCTAssertEqual(try json(["compaction": info["compaction"] ?? NSNull()]), priorCheckpoint)
        _ = try await request(reopened, "PATCH", "/global/config", body: [
            "provider": ["test": ["models": ["scripted": ["limit": ["context": 100_000]]]]]
        ])
        reopenedModel.enqueue(Script(events: [.text("Resumed without the interrupted summary."), .finished]))
        try await prompt(reopened, fixture.id, "Resume with the last committed checkpoint.")
        try await waitIdle(reopened, fixture.id)
        XCTAssertEqual(reopenedModel.requests.count, 1)
        let resumed = try XCTUnwrap(reopenedModel.requests.first)
        XCTAssertTrue(resumed.messages.first?.content.hasSuffix(Self.summary) == true)
        XCTAssertTrue(resumed.messages.contains { $0.content.contains("OLDER_STOP_HISTORY") })
        XCTAssertFalse(resumed.messages.contains { $0.content.contains(partial) })
        await reopened.shutdown()
    }

    func testSummaryIsCommittedAtomicallyOnlyAfterCompletionAndBeforeContinuation() async throws {
        let fixture = try await fixture()
        try await completeTurn(fixture, text: largeText("OLDER_ATOMIC_HISTORY"))
        try await setContext(fixture)
        let originalTranscript = try transcript(fixture)
        let priorCheckpoint = try checkpointData(fixture)
        let index = fixture.model.requests.count
        fixture.model.enqueue(Script(events: [.text(Self.summary)], holdOpen: true), summary: true)
        fixture.model.enqueue(Script(events: [], holdOpen: true))
        try await prompt(fixture.backend, fixture.id, "LATEST_ATOMIC_TURN")
        try await waitUntil { fixture.model.continuation(at: index) != nil }
        XCTAssertEqual(try checkpointData(fixture), priorCheckpoint, "Nonempty streamed text alone cannot commit a checkpoint")
        let summaryStream = try XCTUnwrap(fixture.model.continuation(at: index))
        summaryStream.yield(.finished)
        summaryStream.finish()
        try await waitUntil { fixture.model.continuation(at: index + 1) != nil }
        let checkpoint = try checkpoint(fixture)
        XCTAssertEqual(checkpoint["boundary"] as? Int, originalTranscript.count)
        XCTAssertEqual(checkpoint["summary"] as? String, Self.summary)
        XCTAssertEqual(checkpoint["count"] as? Int, 1)
        let continuation = try XCTUnwrap(fixture.model.requests.last)
        XCTAssertFalse(continuation.tools.isEmpty)
        let baseline = try XCTUnwrap(fixture.model.requests.first)
        let full = try decoded(Array(try transcript(fixture).dropLast()))
        XCTAssertLessThan(try budget(continuation), try budget(baseline, messages: full))
        XCTAssertLessThan(try budget(continuation), Self.context)
        try assertContext(try await sessionInfo(fixture.backend, fixture.id), compacting: false)
        XCTAssertEqual(try json(Array(try transcript(fixture).prefix(originalTranscript.count))), try json(originalTranscript))
        let normalStream = try XCTUnwrap(fixture.model.continuation(at: index + 1))
        normalStream.yield(.text("Atomic continuation complete."))
        normalStream.yield(.finished)
        normalStream.finish()
        try await waitIdle(fixture.backend, fixture.id)
        XCTAssertEqual(fixture.model.requests.count, index + 2)
    }

    func testOversizedLatestTurnRejectsBeforeStorageOrAnyBillableRequest() async throws {
        for hasPriorCheckpoint in [false, true] {
            let fixture = try await fixture()
            if hasPriorCheckpoint { try await establishCheckpoint(fixture) }
            else { try await completeTurn(fixture, text: "Small existing history.") }
            try await setContext(fixture)
            let url = fixture.root.appendingPathComponent("native-agent/state.json")
            let before = try Data(contentsOf: url)
            let originalMessages = try await messages(fixture.backend, fixture.id)
            let count = fixture.model.requests.count
            fixture.model.enqueue(Script(events: [.text(Self.summary), .finished]), summary: true)
            fixture.model.enqueue(Script(events: [.text("UNEXPECTED_BILLABLE_REQUEST"), .finished]))
            do {
                try await prompt(fixture.backend, fixture.id, largeText("OVERSIZED_LATEST_TURN", size: 30_000))
                XCTFail("The latest user turn alone cannot fit, so admission must fail synchronously")
            } catch {
                XCTAssertTrue(error.localizedDescription.lowercased().contains("context") || error.localizedDescription.lowercased().contains("budget"))
            }
            XCTAssertEqual(fixture.model.requests.count, count, "Neither summarization nor normal generation may be billed")
            XCTAssertEqual(try Data(contentsOf: url), before, "Rejected admission must not change any stored state")
            let savedMessages = try await messages(fixture.backend, fixture.id)
            XCTAssertEqual(try json(savedMessages), try json(originalMessages))
            try await waitIdle(fixture.backend, fixture.id)
        }
    }

    func testRepeatedCompactionAdvancesCheckpointAndUsesPriorSummaryWithoutResummarizingPrefix() async throws {
        let fixture = try await fixture()
        try await establishCheckpoint(fixture)
        let firstCheckpoint = try checkpoint(fixture)
        let firstBoundary = try XCTUnwrap(firstCheckpoint["boundary"] as? Int)
        try await setContext(fixture, 100_000)
        try await completeTurn(fixture, text: largeText("OLDER_SECOND_CYCLE"))
        try await setContext(fixture)
        let originalTranscript = try transcript(fixture)
        let originalMessages = try await messages(fixture.backend, fixture.id)
        let count = fixture.model.requests.count
        let secondSummary = "SECOND_COMMITTED_SUMMARY: retain the first handoff and the confirmed second-cycle history."
        fixture.model.enqueue(Script(events: [.text(secondSummary), .finished]), summary: true)
        try await completeTurn(fixture, text: "LATEST_SECOND_CYCLE", answer: "Second cycle complete.")
        XCTAssertEqual(fixture.model.requests.count, count + 2)
        let request = try XCTUnwrap(fixture.model.requests.dropFirst(count).first)
        let history = try summaryData(request)
        XCTAssertEqual(history["priorSummary"] as? String, Self.summary)
        let historicalRows = try XCTUnwrap(history["messages"] as? [[String: Any]])
        let expectedRows = Array(originalTranscript.dropFirst(firstBoundary))
        XCTAssertEqual(historicalRows.count, expectedRows.count)
        XCTAssertEqual(historicalRows.compactMap { $0["content"] as? String }, expectedRows.compactMap { $0["content"] as? String })
        XCTAssertFalse(request.messages[0].content.contains("OLDER_REQUEST"), "The already-compacted prefix must not be summarized again")
        XCTAssertFalse(request.messages[0].content.contains("LATEST_SECOND_CYCLE"))
        let secondCheckpoint = try checkpoint(fixture)
        XCTAssertEqual(secondCheckpoint["boundary"] as? Int, originalTranscript.count)
        XCTAssertGreaterThan(try XCTUnwrap(secondCheckpoint["boundary"] as? Int), firstBoundary)
        XCTAssertEqual(secondCheckpoint["count"] as? Int, 2)
        XCTAssertEqual(secondCheckpoint["summary"] as? String, secondSummary)
        XCTAssertGreaterThanOrEqual(try XCTUnwrap(secondCheckpoint["created"] as? Int64), try XCTUnwrap(firstCheckpoint["created"] as? Int64))
        let continuation = try XCTUnwrap(fixture.model.requests.last)
        XCTAssertTrue(continuation.messages.first?.content.hasSuffix(secondSummary) == true)
        XCTAssertEqual(continuation.messages.last?.content, "LATEST_SECOND_CYCLE")
        XCTAssertLessThan(try budget(continuation), Self.context)
        XCTAssertEqual(try json(Array(try transcript(fixture).prefix(originalTranscript.count))), try json(originalTranscript))
        let savedMessages = try await messages(fixture.backend, fixture.id)
        XCTAssertEqual(try json(Array(savedMessages.prefix(originalMessages.count))), try json(originalMessages))
        try assertNoVisibleSummary(savedMessages, forbidden: [Self.summary, secondSummary])
    }

    private func assertFinalContext(_ fixture: Fixture, request: NativeModelRequest,
                                    file: StaticString = #filePath, line: UInt = #line) async throws -> Int {
        let saved = try transcript(fixture)
        let final = try XCTUnwrap(try decoded(saved).last, file: file, line: line)
        // The request already contains the active checkpoint projection, if any.
        // Its completed or interrupted final assistant row is no longer a placeholder.
        let expectedInput = try budget(request, messages: request.messages + [final]) - Self.reserve
        let info = try await sessionInfo(fixture.backend, fixture.id)
        let context = try XCTUnwrap(info["nativeContext"] as? [String: Any], file: file, line: line)
        XCTAssertEqual(context["input"] as? Int, expectedInput, file: file, line: line)
        XCTAssertEqual(context["reserve"] as? Int, Self.reserve, file: file, line: line)
        XCTAssertEqual(context["estimated"] as? Bool, true, file: file, line: line)
        XCTAssertEqual(context["compacting"] as? Bool, false, file: file, line: line)
        let stored = try XCTUnwrap(try storedSession(fixture)["nativeContext"] as? [String: Any], file: file, line: line)
        XCTAssertEqual(try json(stored), try json(context), file: file, line: line)
        XCTAssertTrue(try XCTUnwrap(try snapshot(fixture.root)["active"] as? [String: Any]).isEmpty, file: file, line: line)
        return expectedInput
    }

    func testCrashWhileSummaryStreamsResetsCompactingAndPreservesCheckpoint() async throws {
        for hasPriorCheckpoint in [false, true] {
            let fixture = try await fixture()
            if hasPriorCheckpoint {
                try await establishCheckpoint(fixture)
                try await setContext(fixture, 100_000)
            }
            try await completeTurn(fixture, text: largeText("OLDER_CRASH_HISTORY"))
            try await setContext(fixture)
            let priorCheckpoint = try checkpointData(fixture)
            let originalMessages = try await messages(fixture.backend, fixture.id)
            let index = fixture.model.requests.count
            let partial = "CRASHED_PRIVATE_SUMMARY_MUST_NOT_COMMIT"
            fixture.model.enqueue(Script(events: [.text(partial)], holdOpen: true), summary: true)
            try await prompt(fixture.backend, fixture.id, "LATEST_CRASH_TURN")
            try await waitUntil {
                let info = try await self.sessionInfo(fixture.backend, fixture.id)
                return fixture.model.continuation(at: index) != nil
                    && (info["nativeContext"] as? [String: Any])?["compacting"] as? Bool == true
            }
            let url = fixture.root.appendingPathComponent("native-agent/state.json")
            let crashBytes = try Data(contentsOf: url)
            let crashTranscript = try transcript(fixture)
            let crashContext = try XCTUnwrap(try storedSession(fixture)["nativeContext"] as? [String: Any])
            XCTAssertEqual(crashContext["compacting"] as? Bool, true)
            XCTAssertNotNil(try XCTUnwrap(try snapshot(fixture.root)["active"] as? [String: Any])[fixture.id])
            XCTAssertEqual(try checkpointData(fixture), priorCheckpoint)
            _ = try summaryData(try XCTUnwrap(fixture.model.requests.dropFirst(index).first))

            // Restore exactly what was on disk before graceful shutdown could clear the run.
            await fixture.backend.shutdown()
            try crashBytes.write(to: url, options: .atomic)
            let reopenedModel = ScriptedModel()
            let reopened = try backend(fixture.root, reopenedModel, fixture.credentials)
            try await waitIdle(reopened, fixture.id)
            XCTAssertTrue(reopenedModel.requests.isEmpty, "Recovery must not bill a summary or continuation request")
            let info = try await sessionInfo(reopened, fixture.id)
            XCTAssertEqual(try json(["compaction": info["compaction"] ?? NSNull()]), priorCheckpoint)
            var expectedContext = crashContext
            expectedContext["compacting"] = false
            XCTAssertEqual(try json(try XCTUnwrap(info["nativeContext"] as? [String: Any])), try json(expectedContext))
            XCTAssertEqual(try json(try XCTUnwrap(try storedSession(fixture)["nativeContext"] as? [String: Any])), try json(expectedContext))
            XCTAssertEqual(try checkpointData(fixture), priorCheckpoint)
            XCTAssertEqual(try json(try transcript(fixture)), try json(crashTranscript))
            XCTAssertTrue(try XCTUnwrap(try snapshot(fixture.root)["active"] as? [String: Any]).isEmpty)
            let recoveredMessages = try await messages(reopened, fixture.id)
            XCTAssertEqual(try json(Array(recoveredMessages.prefix(originalMessages.count))), try json(originalMessages))
            let lastInfo = try XCTUnwrap(recoveredMessages.last?["info"] as? [String: Any])
            XCTAssertNotNil(lastInfo["error"])
            XCTAssertNotNil((lastInfo["time"] as? [String: Any])?["completed"])
            try assertNoVisibleSummary(recoveredMessages, forbidden: [partial])
            XCTAssertEqual(fixture.model.requests.count, index + 1)

            // Opening the recovered snapshot a second time must not repeat recovery writes.
            await reopened.shutdown()
            let recoveredBytes = try Data(contentsOf: url)
            let secondModel = ScriptedModel()
            let second = try backend(fixture.root, secondModel, fixture.credentials)
            XCTAssertEqual(try Data(contentsOf: url), recoveredBytes)
            XCTAssertTrue(secondModel.requests.isEmpty)
            await second.shutdown()
        }
    }

    func testReplayOnlyCompletedResponseFinalContextIncludesPersistedReplayBytes() async throws {
        for hasPriorCheckpoint in [false, true] {
            let fixture = try await fixture()
            if hasPriorCheckpoint { try await establishCheckpoint(fixture) }
            try await setContext(fixture, 100_000)
            let priorCheckpoint = try checkpointData(fixture)
            let originalTranscript = try transcript(fixture)
            let encrypted = String(repeating: "r", count: 12_000)
            let items: [[String: Any]] = [
                ["type": "reasoning", "id": "rs_final_replay", "encrypted_content": encrypted],
                ["type": "message", "id": "msg_final_replay", "role": "assistant", "status": "completed",
                 "content": [["type": "output_text", "text": "Completed replay-only answer."]]]
            ]
            let index = fixture.model.requests.count
            fixture.model.enqueue(Script(events: [.replay(items), .finished]))
            try await prompt(fixture.backend, fixture.id, "Count a completed replay-only response.")
            try await waitIdle(fixture.backend, fixture.id)
            XCTAssertEqual(fixture.model.requests.count, index + 1)
            let request = try XCTUnwrap(fixture.model.requests.dropFirst(index).first)
            let saved = try transcript(fixture)
            XCTAssertEqual(try json(Array(saved.prefix(originalTranscript.count))), try json(originalTranscript))
            let final = try XCTUnwrap(try decoded(saved).last)
            XCTAssertEqual(final.role, "assistant")
            XCTAssertEqual(final.content, "", "No text delta was emitted; opaque replay alone is still a completed response")
            XCTAssertTrue(final.toolCalls.isEmpty)
            XCTAssertEqual(try json(final.replayItems), try json(items))
            let visible = try await messages(fixture.backend, fixture.id)
            XCTAssertEqual((visible.last?["info"] as? [String: Any])?["finish"] as? String, "stop")
            XCTAssertNil((visible.last?["info"] as? [String: Any])?["error"])
            let input = try await assertFinalContext(fixture, request: request)
            XCTAssertGreaterThan(input, try budget(request) - Self.reserve + encrypted.utf8.count,
                                 "The completed last row is not a streaming placeholder: count its replay bytes and overhead")
            var withoutReplay = final
            withoutReplay.replayItems = []
            XCTAssertGreaterThan(input, try budget(request, messages: request.messages + [withoutReplay]) - Self.reserve)
            XCTAssertEqual(try checkpointData(fixture), priorCheckpoint)
        }
    }

    func testFailedAndCancelledStreamingFinalContextIncludesPersistedPartialOutput() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            for mode in ["failed", "truncated", "cancelled"] {
                let fixture = try await fixture(format: format)
                try await establishCheckpoint(fixture)
                try await setContext(fixture, 100_000)
                let priorCheckpoint = try checkpointData(fixture)
                let originalTranscript = try transcript(fixture)
                let partial = largeText("PERSISTED_PARTIAL_\(mode)", size: 4_000)
                let items: [[String: Any]] = [
                    ["type": "reasoning", "id": "rs_partial_\(mode)", "encrypted_content": String(repeating: "p", count: 2_000)]
                ]
                let index = fixture.model.requests.count
                fixture.model.enqueue(Script(events: [.text(partial), .replay(items)], holdOpen: true))
                try await prompt(fixture.backend, fixture.id, "Keep the streamed evidence even if this turn ends early.")
                try await waitUntil {
                    let saved = try self.transcript(fixture)
                    return saved.last?["content"] as? String == partial
                        && (saved.last?["replayItems"] as? [[String: Any]])?.count == items.count
                }
                let stream = try XCTUnwrap(fixture.model.continuation(at: index))
                switch mode {
                case "failed": stream.finish(throwing: NativeModelClientError.providerFailure)
                case "truncated": stream.finish()
                default:
                    _ = try await request(fixture.backend, "POST", "/session/\(fixture.id)/abort")
                    stream.yield(.text("LATE_OUTPUT_MUST_NOT_PERSIST"))
                    stream.yield(.finished)
                    stream.finish()
                }
                try await waitIdle(fixture.backend, fixture.id)
                try await waitUntil { fixture.model.terminated(at: index) }
                XCTAssertEqual(fixture.model.requests.count, index + 1, "Interrupted output must not be retried")
                let request = try XCTUnwrap(fixture.model.requests.dropFirst(index).first)
                let saved = try transcript(fixture)
                XCTAssertEqual(try json(Array(saved.prefix(originalTranscript.count))), try json(originalTranscript))
                let final = try XCTUnwrap(try decoded(saved).last)
                XCTAssertEqual(final.content, partial)
                XCTAssertEqual(try json(final.replayItems), try json(items))
                let input = try await assertFinalContext(fixture, request: request)
                XCTAssertGreaterThan(input, try budget(request) - Self.reserve + partial.utf8.count,
                                     "Failed/cancelled final counters must include the persisted partial assistant row")
                var withoutPartial = final
                withoutPartial.content = ""
                XCTAssertGreaterThan(input, try budget(request, messages: request.messages + [withoutPartial]) - Self.reserve)
                let info = try await sessionInfo(fixture.backend, fixture.id)
                XCTAssertEqual((info["nativeContext"] as? [String: Any])?["limit"] as? Int, 100_000)
                let visible = try await messages(fixture.backend, fixture.id)
                XCTAssertNotNil((visible.last?["info"] as? [String: Any])?["error"])
                let text = (visible.last?["parts"] as? [[String: Any]] ?? []).compactMap { $0["text"] as? String }.joined()
                XCTAssertEqual(text, partial)
                XCTAssertEqual(try checkpointData(fixture), priorCheckpoint)
            }
        }
    }

    func testPersistedIntMaxBoundaryOrCountRejectsBeforeCrashRecoveryOverwritesSnapshot() async throws {
        for field in ["boundary", "count"] {
            let fixture = try await fixture()
            try await establishCheckpoint(fixture)
            try await setContext(fixture, 100_000)
            try await completeTurn(fixture, text: largeText("OLDER_INVALID_CHECKPOINT_HISTORY"))
            try await setContext(fixture)
            let index = fixture.model.requests.count
            fixture.model.enqueue(Script(events: [.text("PRIVATE_INVALID_CHECKPOINT_SUMMARY")], holdOpen: true), summary: true)
            try await prompt(fixture.backend, fixture.id, "LATEST_INVALID_CHECKPOINT_TURN")
            try await waitUntil {
                let info = try await self.sessionInfo(fixture.backend, fixture.id)
                return fixture.model.continuation(at: index) != nil
                    && (info["nativeContext"] as? [String: Any])?["compacting"] as? Bool == true
            }
            let url = fixture.root.appendingPathComponent("native-agent/state.json")
            let crashBytes = try Data(contentsOf: url)
            await fixture.backend.shutdown()
            try crashBytes.write(to: url, options: .atomic)
            var corrupted = try snapshot(fixture.root)
            XCTAssertNotNil(try XCTUnwrap(corrupted["active"] as? [String: Any])[fixture.id],
                            "An active marker would trigger a recovery overwrite if validation ran too late")
            let originalTranscript = try json(try XCTUnwrap(corrupted["transcripts"] as? [String: [[String: Any]]]))
            let originalMessages = try json(try XCTUnwrap(corrupted["messages"] as? [String: [[String: Any]]]))
            var sessions = try XCTUnwrap(corrupted["sessions"] as? [[String: Any]])
            let sessionIndex = try XCTUnwrap(sessions.firstIndex { $0["id"] as? String == fixture.id })
            var checkpoint = try XCTUnwrap(sessions[sessionIndex]["compaction"] as? [String: Any])
            checkpoint[field] = Int.max
            sessions[sessionIndex]["compaction"] = checkpoint
            corrupted["sessions"] = sessions
            let invalidBytes = try json(corrupted)
            try invalidBytes.write(to: url, options: .atomic)
            XCTAssertEqual(try self.checkpoint(fixture)[field] as? Int, Int.max)

            let reopenedModel = ScriptedModel()
            do {
                let reopened = try backend(fixture.root, reopenedModel, fixture.credentials)
                XCTFail("Persisted compaction \(field)=Int.max must reject initialization before recovery or overwrite")
                await reopened.shutdown()
            } catch {
                XCTAssertTrue(error.localizedDescription.contains("invalid compaction boundary"))
                XCTAssertTrue(error.localizedDescription.contains("history was not overwritten"))
            }
            XCTAssertEqual(try Data(contentsOf: url), invalidBytes, "Leave the invalid snapshot byte-for-byte intact for recovery from backup")
            XCTAssertEqual(try json(try XCTUnwrap(try snapshot(fixture.root)["transcripts"] as? [String: [[String: Any]]])), originalTranscript)
            XCTAssertEqual(try json(try XCTUnwrap(try snapshot(fixture.root)["messages"] as? [String: [[String: Any]]])), originalMessages)
            XCTAssertTrue(reopenedModel.requests.isEmpty)
            XCTAssertEqual(fixture.model.requests.count, index + 1)
        }
    }

    func testLargeOlderHistoryUsesBoundedCompleteTurnSummaryBatches() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let fixture = try await fixture(format: format)
            let older = (0..<5).map { largeText("BATCH_TURN_\($0)") }
            for (index, text) in older.enumerated() {
                try await completeTurn(fixture, text: text, answer: "BATCH_ANSWER_\(index)")
            }
            try await setContext(fixture)
            let originalTranscript = try transcript(fixture)
            let originalMessages = try await messages(fixture.backend, fixture.id)
            let count = fixture.model.requests.count
            let summaries = (0..<5).map { "BATCH_HANDOFF_\($0): confirmed older goals, evidence and remaining constraints." }
            for summary in summaries { fixture.model.enqueue(Script(events: [.text(summary), .finished]), summary: true) }
            fixture.model.enqueue(Script(events: [.text("Bounded compaction complete."), .finished]))
            try await prompt(fixture.backend, fixture.id, "LATEST_BATCH_TURN")
            try await waitIdle(fixture.backend, fixture.id)
            let newRequests = Array(fixture.model.requests.dropFirst(count))
            let summaryRequests = newRequests.filter { $0.system == NativeContextCompaction.summaryInstructions }
            XCTAssertGreaterThan(summaryRequests.count, 1, "The full older history cannot fit in a single summary request")
            XCTAssertLessThanOrEqual(summaryRequests.count, older.count)
            guard summaryRequests.count > 1, summaryRequests.count <= summaries.count else {
                throw NSError(domain: "NativeAgentCompactionTests.InvalidBatchCount", code: summaryRequests.count)
            }
            XCTAssertEqual(newRequests.count, summaryRequests.count + 1)
            var summarizedUsers: [String] = []
            var summarizedAnswers: [String] = []
            for (index, request) in summaryRequests.enumerated() {
                XCTAssertEqual(request.format, format)
                let data = try summaryData(request)
                try assertSummaryWire(request)
                if index == 0 { XCTAssertNil(data["priorSummary"]) }
                else { XCTAssertEqual(data["priorSummary"] as? String, summaries[index - 1]) }
                let batch = try XCTUnwrap(data["messages"] as? [[String: Any]])
                XCTAssertFalse(batch.isEmpty)
                XCTAssertEqual(batch.first?["role"] as? String, "user", "Never split a turn across batches")
                XCTAssertEqual(batch.last?["role"] as? String, "assistant", "A batch must include the turn's completed response")
                XCTAssertEqual(batch.count % 2, 0)
                for pair in stride(from: 0, to: batch.count - 1, by: 2) {
                    XCTAssertEqual(batch[pair]["role"] as? String, "user")
                    XCTAssertEqual(batch[pair + 1]["role"] as? String, "assistant")
                    summarizedUsers.append(try XCTUnwrap(batch[pair]["content"] as? String))
                    summarizedAnswers.append(try XCTUnwrap(batch[pair + 1]["content"] as? String))
                }
                XCTAssertFalse(request.messages[0].content.contains("LATEST_BATCH_TURN"))
            }
            XCTAssertEqual(summarizedUsers, older, "Every original turn must be summarized exactly once, in order")
            XCTAssertEqual(summarizedAnswers, (0..<5).map { "BATCH_ANSWER_\($0)" })
            let checkpoint = try checkpoint(fixture)
            XCTAssertEqual(checkpoint["boundary"] as? Int, originalTranscript.count)
            XCTAssertEqual(checkpoint["summary"] as? String, summaries[summaryRequests.count - 1])
            let continuation = try XCTUnwrap(newRequests.last)
            XCTAssertFalse(continuation.tools.isEmpty)
            XCTAssertTrue(continuation.messages.first?.content.hasSuffix(summaries[summaryRequests.count - 1]) == true)
            XCTAssertEqual(continuation.messages.last?.content, "LATEST_BATCH_TURN")
            XCTAssertLessThan(try budget(continuation), Self.context)
            XCTAssertEqual(try json(Array(try transcript(fixture).prefix(originalTranscript.count))), try json(originalTranscript))
            let savedMessages = try await messages(fixture.backend, fixture.id)
            XCTAssertEqual(try json(Array(savedMessages.prefix(originalMessages.count))), try json(originalMessages))
            try assertNoVisibleSummary(savedMessages, forbidden: summaries)
            try assertContext(try await sessionInfo(fixture.backend, fixture.id), compacting: false)
        }
    }
}
