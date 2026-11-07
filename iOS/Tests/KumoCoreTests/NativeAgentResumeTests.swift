import Foundation
import XCTest
@testable import KumoCore

final class NativeAgentResumeTests: XCTestCase {
    private struct Script {
        var events: [NativeModelEvent] = []
        var failure: Error? = nil
        var holdOpen = false
    }

    private final class ScriptedModel: NativeModelClient {
        private let lock = NSLock()
        private let snapshotURL: URL
        private var scripts: [Script] = []
        private var recorded: [(NativeModelRequest, Data?)] = []
        private var continuation: AsyncThrowingStream<NativeModelEvent, Error>.Continuation?

        init(_ root: URL) { snapshotURL = root.appendingPathComponent("native-agent/state.json") }
        var requests: [NativeModelRequest] {
            lock.lock(); defer { lock.unlock() }; return recorded.map { $0.0 }
        }
        func snapshot(at index: Int) throws -> [String: Any] {
            lock.lock(); defer { lock.unlock() }
            return try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(recorded[index].1)) as? [String: Any])
        }
        func enqueue(_ script: Script) { lock.lock(); defer { lock.unlock() }; scripts.append(script) }
        func complete() {
            lock.lock(); let stream = continuation; lock.unlock()
            stream?.yield(.text("Recovered.")); stream?.yield(.finished); stream?.finish()
        }
        func stream(request: NativeModelRequest) -> AsyncThrowingStream<NativeModelEvent, Error> {
            lock.lock()
            // Observe persisted state before the first delta, not merely after failure.
            recorded.append((request, try? Data(contentsOf: snapshotURL)))
            let script = scripts.isEmpty ? Script(failure: NativeModelClientError.providerFailure) : scripts.removeFirst()
            lock.unlock()
            return AsyncThrowingStream { stream in
                self.lock.lock(); self.continuation = stream; self.lock.unlock()
                for event in script.events { stream.yield(event) }
                if let error = script.failure { stream.finish(throwing: error) }
                else if !script.holdOpen { stream.finish() }
            }
        }
    }

    private final class Credentials {
        private let lock = NSLock()
        private var values: [String: String] = [:]
        func read(_ id: String) throws -> String? { lock.lock(); defer { lock.unlock() }; return values[id] }
        func write(_ id: String, _ value: String?) throws { lock.lock(); defer { lock.unlock() }; values[id] = value }
    }

    private struct Fixture {
        let root: URL
        let credentials: Credentials
        let model: ScriptedModel
        let backend: NativeAgentBackend
        let id: String
    }

    private func backend(_ root: URL, _ model: ScriptedModel, _ credentials: Credentials) throws -> NativeAgentBackend {
        let backend = try NativeAgentBackend(root: root, modelClient: model,
                                             credentialRead: credentials.read, credentialWrite: credentials.write)
        addTeardownBlock { await backend.shutdown() }
        return backend
    }
    @discardableResult
    private func request(_ f: Fixture, _ method: String, _ suffix: String, body: [String: Any]? = nil) async throws -> Any {
        try await f.backend.request(method: method, path: "/session/\(f.id)\(suffix)", directory: f.root.path, body: body)
    }
    private func fixture(_ format: NativeProviderFormat = .responses) async throws -> Fixture {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("native-resume-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let credentials = Credentials(), model = ScriptedModel(root)
        let backend = try backend(root, model, credentials)
        _ = try await backend.request(method: "PATCH", path: "/global/config", directory: nil, body: [
            "model": "test/scripted", "provider": ["test": ["name": "Scripted",
                "npm": format == .responses ? "@ai-sdk/openai" : "@ai-sdk/openai-compatible",
                "options": ["baseURL": "https://example.invalid/v1"], "models": [
                    "scripted": ["name": "Scripted", "limit": ["context": 100_000, "output": 1_000],
                                 "variants": ["careful": ["reasoningEffort": "high"]]],
                    "other": ["name": "Other", "limit": ["context": 100_000, "output": 1_000]]]]]])
        _ = try await backend.request(method: "PUT", path: "/auth/test", directory: nil,
                                      body: ["type": "api", "key": UUID().uuidString])
        let session = try await backend.request(method: "POST", path: "/session", directory: root.path,
                                               body: ["permission": [["permission": "*", "pattern": "*", "action": "allow"]]])
        let id = try XCTUnwrap((session as? [String: Any])?["id"] as? String)
        return Fixture(root: root, credentials: credentials, model: model, backend: backend, id: id)
    }
    private func prompt(_ f: Fixture, _ text: String = "Continue the task.", agent: String = "build") async throws {
        _ = try await request(f, "POST", "/prompt_async", body: ["parts": [["type": "text", "text": text]],
            "agent": agent, "variant": "careful", "model": ["providerID": "test", "modelID": "scripted"]])
    }
    private func waitUntil(_ condition: () async throws -> Bool) async throws {
        for _ in 0..<500 {
            if try await condition() { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        throw NSError(domain: "NativeAgentResumeTests.WaitTimeout", code: 1)
    }
    private func idle(_ f: Fixture) async throws {
        try await waitUntil {
            let result = try await f.backend.request(method: "GET", path: "/session/status", directory: nil, body: nil)
            return (result as? [String: [String: Any]])?[f.id]?["type"] as? String == "idle"
        }
    }
    private func messages(_ f: Fixture) async throws -> [[String: Any]] {
        let result = try await request(f, "GET", "/message")
        return try XCTUnwrap(result as? [[String: Any]])
    }
    private func snapshot(_ f: Fixture) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: f.root.appendingPathComponent("native-agent/state.json"))) as? [String: Any])
    }
    private func session(_ f: Fixture, in state: [String: Any]? = nil) throws -> [String: Any] {
        let snapshot = try state ?? self.snapshot(f)
        let sessions = try XCTUnwrap(snapshot["sessions"] as? [[String: Any]])
        return try XCTUnwrap(sessions.first { $0["id"] as? String == f.id })
    }
    private func transcript(_ f: Fixture, in state: [String: Any]? = nil) throws -> [[String: Any]] {
        let snapshot = try state ?? self.snapshot(f)
        return try XCTUnwrap((snapshot["transcripts"] as? [String: [[String: Any]]])?[f.id])
    }
    private func checkpoint(_ f: Fixture, in state: [String: Any]? = nil) throws -> [String: Any] {
        try XCTUnwrap(try session(f, in: state)["resume"] as? [String: Any])
    }
    private func json(_ value: Any) throws -> Data { try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) }
    private func rows(_ messages: [NativeModelMessage]) -> [[String: Any]] {
        messages.map { message in
            var row: [String: Any] = ["role": message.role, "content": message.content, "replayItems": message.replayItems,
                "toolCalls": message.toolCalls.map { ["id": $0.id, "name": $0.name, "arguments": $0.arguments] }]
            if let id = message.toolCallID { row["toolCallID"] = id }; return row
        }
    }
    private func history(_ rows: [[String: Any]], ui: Bool = false) throws -> Data {
        try json(rows.map { row -> [String: Any] in
            var row = row
            if ui, var info = row["info"] as? [String: Any] { info.removeValue(forKey: "nativeResume"); row["info"] = info }
            else { row.removeValue(forKey: "excludedFromContext") }
            return row
        })
    }
    private func failed(_ f: Fixture) async throws -> [String: Any] {
        let visible = try await messages(f)
        let info = try XCTUnwrap(visible.last?["info"] as? [String: Any])
        XCTAssertNotNil(info["error"]); XCTAssertEqual(info["nativeResume"] as? Bool, true)
        XCTAssertEqual(visible.compactMap { ($0["info"] as? [String: Any])?["nativeResume"] as? Bool }.filter { $0 }.count, 1)
        return info
    }
    private func resume(_ f: Fixture, _ info: [String: Any]) async throws {
        _ = try await request(f, "POST", "/resume", body: ["messageID": try XCTUnwrap(info["id"] as? String)])
    }
    private func reject(_ f: Fixture, _ body: [String: Any], directory: String? = nil, sid: String? = nil) async throws {
        let url = f.root.appendingPathComponent("native-agent/state.json"), before = try Data(contentsOf: url)
        let count = f.model.requests.count
        do {
            _ = try await f.backend.request(method: "POST", path: "/session/\(sid ?? f.id)/resume",
                                            directory: directory ?? f.root.path, body: body)
            XCTFail("Resume must reject this request")
        } catch { XCTAssertFalse(error.localizedDescription.contains("Unsupported native backend route"), "Reject the contract, not a missing endpoint") }
        XCTAssertEqual(try Data(contentsOf: url), before, "Rejected resume must not write storage")
        XCTAssertEqual(f.model.requests.count, count)
    }
    private func call(_ id: String, _ name: String, _ args: [String: Any]) throws -> NativeToolCall {
        NativeToolCall(id: id, name: name, arguments: String(decoding: try json(args), as: UTF8.self))
    }

    func testResumeReplaysCompletedWriteExactlyOnceWithoutOverwritingManualEditInBothFormats() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let f = try await fixture(format), file = f.root.appendingPathComponent("result.txt")
            let write = try call("write-once", "write", ["filePath": "result.txt", "content": "Original write"])
            let replay: [[String: Any]] = format == .responses ? [
                ["type": "reasoning", "id": "rs_write", "encrypted_content": "cipher_write"],
                ["type": "message", "id": "msg_write", "role": "assistant", "status": "completed",
                 "content": [["type": "output_text", "text": "Original answer"]]],
                ["type": "function_call", "id": "fc_write", "call_id": write.id, "name": write.name, "arguments": write.arguments]] : []
            f.model.enqueue(Script(events: [.text("Original answer"), .toolCall(write), .replay(replay), .finished]))
            f.model.enqueue(Script(events: [.text("FAILED_PARTIAL")], failure: NativeModelClientError.transport(-1200)))
            try await prompt(f); try await idle(f)
            XCTAssertEqual(f.model.requests.count, 2, "Transport failure must not retry automatically")
            XCTAssertEqual(try String(contentsOf: file), "Original write")
            let info = try await failed(f), visible = try await messages(f), full = try transcript(f)
            let atRequest = try f.model.snapshot(at: 1), saved = try checkpoint(f)
            let boundary = try XCTUnwrap(saved["transcriptBoundary"] as? Int)
            let storedMessages = try XCTUnwrap((atRequest["messages"] as? [String: [[String: Any]]])?[f.id])
            XCTAssertEqual((storedMessages.last?["info"] as? [String: Any])?["id"] as? String, info["id"] as? String)
            XCTAssertTrue(try XCTUnwrap(storedMessages.last?["parts"] as? [[String: Any]]).isEmpty)
            XCTAssertEqual(boundary, try transcript(f, in: atRequest).count - 1)
            XCTAssertEqual(try transcript(f, in: atRequest).last?["content"] as? String, "")
            XCTAssertEqual(try json(saved), try json(try checkpoint(f, in: atRequest)), "Checkpoint predates failed deltas")
            XCTAssertEqual(saved["messageID"] as? String, info["id"] as? String)
            XCTAssertEqual(saved["userID"] as? String, info["parentID"] as? String)
            XCTAssertEqual(saved["step"] as? Int, 1)
            XCTAssertEqual(try json(Array(full.prefix(boundary))), try json(rows(f.model.requests[1].messages)))
            try "Manual edit after failure".write(to: file, atomically: true, encoding: .utf8)
            f.model.enqueue(Script(events: [.text("Recovered."), .finished]))
            try await resume(f, info); try await idle(f)
            let input = try XCTUnwrap(f.model.requests.last).messages, after = try await messages(f)
            XCTAssertEqual(f.model.requests.count, 3)
            XCTAssertEqual(try json(rows(input)), try json(rows(f.model.requests[1].messages)))
            XCTAssertEqual(input.filter { $0.content == "Original answer" }.count, 1)
            XCTAssertEqual(input.flatMap(\.toolCalls).filter { $0.id == write.id }.count, 1)
            XCTAssertEqual(input.filter { $0.toolCallID == write.id }.count, 1)
            XCTAssertFalse(input.contains { $0.content.contains("FAILED_PARTIAL") })
            XCTAssertEqual(try String(contentsOf: file), "Manual edit after failure")
            XCTAssertEqual(after.count, visible.count + 1)
            XCTAssertEqual(try history(Array(after.prefix(visible.count)), ui: true), try history(visible, ui: true))
            XCTAssertEqual((after.last?["info"] as? [String: Any])?["parentID"] as? String, info["parentID"] as? String)
            XCTAssertEqual(after.filter { ($0["info"] as? [String: Any])?["role"] as? String == "user" }.count, 1)
            let retained = try transcript(f)
            XCTAssertEqual(try json(Array(retained.prefix(boundary))), try json(Array(full.prefix(boundary))))
            XCTAssertEqual(try history(Array(retained.prefix(full.count))), try history(full))
            XCTAssertEqual(retained[boundary]["excludedFromContext"] as? Bool, true)
            XCTAssertNil(try session(f)["resume"] as? [String: Any])
            XCTAssertFalse(after.contains { ($0["info"] as? [String: Any])?["nativeResume"] as? Bool == true })
        }
    }

    func testRecoverableErrorsKeepOnlyLatestMarkerAndExcludeAllFailedPartialsFromFutureInput() async throws {
        let errors: [Error] = [NativeModelClientError.transport(-1200), NativeModelClientError.http(503),
            NativeModelClientError.httpReason(status: 429, reason: .rateLimited), NativeModelClientError.truncatedStream,
            NativeModelClientError.providerFailure,
            NativeModelClientError.malformedStream("chat delta after finish reason"),
            NSError(domain: NSURLErrorDomain, code: -1200)]
        for error in errors {
            let f = try await fixture()
            f.model.enqueue(Script(events: [.text("FAILED_ONE")], failure: error))
            try await prompt(f); try await idle(f)
            let first = try await failed(f)
            XCTAssertEqual(f.model.requests.count, 1)
            f.model.enqueue(Script(events: [.text("FAILED_TWO")], failure: error))
            try await resume(f, first); try await idle(f)
            let second = try await failed(f)
            XCTAssertNotEqual(first["id"] as? String, second["id"] as? String)
            XCTAssertEqual(f.model.requests.count, 2)
            try await reject(f, ["messageID": try XCTUnwrap(first["id"] as? String)])
            f.model.enqueue(Script(events: [.text("Recovered."), .finished]))
            try await resume(f, second); try await idle(f)
            f.model.enqueue(Script(events: [.text("Next answer"), .finished]))
            try await prompt(f, "A genuinely new prompt."); try await idle(f)
            for modelRequest in f.model.requests.dropFirst() {
                XCTAssertFalse(modelRequest.messages.contains { $0.content.contains("FAILED_") })
            }
            let retained = try transcript(f), visible = try await messages(f)
            XCTAssertEqual(retained.filter { ($0["content"] as? String)?.hasPrefix("FAILED_") == true }.count, 2)
            XCTAssertTrue(retained.filter { ($0["content"] as? String)?.hasPrefix("FAILED_") == true }
                .allSatisfy { $0["excludedFromContext"] as? Bool == true })
            XCTAssertEqual(visible.flatMap { $0["parts"] as? [[String: Any]] ?? [] }
                .filter { ($0["text"] as? String)?.hasPrefix("FAILED_") == true }.count, 2)
            XCTAssertNil(try session(f)["resume"] as? [String: Any])
            try await reject(f, ["messageID": try XCTUnwrap(second["id"] as? String)])
        }
    }

    func testReopenResumesOriginalModelVariantAndAgentWithNewPlaceholderAndNoNewUser() async throws {
        let f = try await fixture()
        f.model.enqueue(Script(events: [.text("Persistent partial")], failure: NativeModelClientError.transport(-1200)))
        try await prompt(f, agent: "plan"); try await idle(f)
        let info = try await failed(f), saved = try checkpoint(f), old = try await messages(f)
        XCTAssertEqual(saved["providerID"] as? String, "test"); XCTAssertEqual(saved["modelID"] as? String, "scripted")
        XCTAssertEqual(saved["variant"] as? String, "careful"); XCTAssertEqual(saved["agent"] as? String, "plan")
        _ = try await f.backend.request(method: "PATCH", path: "/global/config", directory: nil, body: ["model": "test/other"])
        await f.backend.shutdown()
        let model = ScriptedModel(f.root), url = f.root.appendingPathComponent("native-agent/state.json")
        let before = try Data(contentsOf: url)
        let reopened = try backend(f.root, model, f.credentials)
        let restored = Fixture(root: f.root, credentials: f.credentials, model: model, backend: reopened, id: f.id)
        XCTAssertEqual(try Data(contentsOf: url), before); XCTAssertTrue(model.requests.isEmpty)
        XCTAssertEqual(try json(try checkpoint(restored)), try json(saved))
        model.enqueue(Script(holdOpen: true))
        try await resume(restored, info); try await waitUntil { model.requests.count == 1 }
        let input = try XCTUnwrap(model.requests.first), visible = try await messages(restored)
        XCTAssertEqual(input.model, "scripted"); XCTAssertEqual(input.effort, "high")
        XCTAssertEqual(input.system, f.model.requests[0].system)
        XCTAssertEqual(try json(rows(input.messages)), try json(rows(f.model.requests[0].messages)))
        XCTAssertEqual(visible.count, old.count + 1)
        XCTAssertNotEqual((visible.last?["info"] as? [String: Any])?["id"] as? String, info["id"] as? String)
        XCTAssertEqual(visible.filter { ($0["info"] as? [String: Any])?["role"] as? String == "user" }.count, 1)
        XCTAssertEqual(try history(Array(visible.prefix(old.count)), ui: true), try history(old, ui: true))
        XCTAssertTrue(try XCTUnwrap(visible.last?["parts"] as? [[String: Any]]).isEmpty)
        XCTAssertEqual((visible.last?["info"] as? [String: Any])?["parentID"] as? String, saved["userID"] as? String)
        model.complete(); try await idle(restored)
        await reopened.shutdown()
        let finalModel = ScriptedModel(f.root)
        let again = try backend(f.root, finalModel, f.credentials)
        let final = Fixture(root: f.root, credentials: f.credentials, model: finalModel, backend: again, id: f.id)
        XCTAssertEqual(try transcript(final)[1]["excludedFromContext"] as? Bool, true)
        XCTAssertEqual(try transcript(final)[1]["content"] as? String, "Persistent partial")
        XCTAssertNil(try session(final)["resume"] as? [String: Any])
    }

    func testInvalidBodyScopeMissingMarkerStaleAndBusyRejectWithoutStorageWrites() async throws {
        let f = try await fixture()
        try await reject(f, ["messageID": "missing"])
        f.model.enqueue(Script(failure: NativeModelClientError.providerFailure))
        try await prompt(f); try await idle(f)
        let info = try await failed(f), mid = try XCTUnwrap(info["id"] as? String)
        for body: [String: Any] in [[:], ["messageID": 7], ["messageID": ""], ["messageID": "stale"],
            ["messageID": mid, "parts": []], ["messageID": mid, "model": ["providerID": "test", "modelID": "other"]],
            ["messageID": mid, "variant": "careful"], ["messageID": mid, "agent": "plan"]] {
            try await reject(f, body)
        }
        try await reject(f, ["messageID": mid], directory: f.root.appendingPathComponent("native-agent").path)
        let other = try await f.backend.request(method: "POST", path: "/session", directory: f.root.path, body: [:])
        try await reject(f, ["messageID": mid], sid: XCTUnwrap((other as? [String: Any])?["id"] as? String))
        f.model.enqueue(Script(holdOpen: true))
        try await resume(f, info); try await waitUntil { f.model.requests.count == 2 }
        try await reject(f, ["messageID": mid])
        f.model.complete(); try await idle(f)
    }

    func testStopAndNonrecoverableFailureHaveNoResumeWhileTruncatedPreviewNeverExecutes() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            for mode in ["stop", "cancel", "invalid", "truncated"] {
                let f = try await fixture(format), file = f.root.appendingPathComponent("preview.txt")
                let error: Error? = mode == "truncated" ? NativeModelClientError.truncatedStream
                    : mode == "cancel" ? CancellationError() : mode == "invalid" ? NativeModelClientError.invalidRequest("bad") : nil
                f.model.enqueue(Script(events: [.text("PREVIEW_PARTIAL"), .toolStarted(id: "preview-write", name: "write")],
                                       failure: error, holdOpen: mode == "stop"))
                try await prompt(f)
                if mode == "stop" {
                    try await waitUntil { try self.transcript(f).last?["content"] as? String == "PREVIEW_PARTIAL" }
                    try await request(f, "POST", "/abort")
                }
                try await idle(f)
                XCTAssertEqual(f.model.requests.count, 1); XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
                let visible = try await messages(f), info = try XCTUnwrap(visible.last?["info"] as? [String: Any])
                XCTAssertNotNil(info["error"])
                if mode == "truncated" {
                    let preview = try XCTUnwrap((visible.last?["parts"] as? [[String: Any]])?.first { $0["callID"] as? String == "preview-write" })
                    XCTAssertEqual((preview["state"] as? [String: Any])?["status"] as? String, "error")
                    XCTAssertEqual(info["nativeResume"] as? Bool, true)
                    f.model.enqueue(Script(events: [.text("Recovered."), .finished]))
                    try await resume(f, info); try await idle(f)
                    let input = try XCTUnwrap(f.model.requests.last).messages
                    XCTAssertFalse(input.contains { $0.content.contains("PREVIEW_PARTIAL") })
                    XCTAssertFalse(input.flatMap(\.toolCalls).contains { $0.id == "preview-write" })
                    XCTAssertFalse(input.contains { $0.toolCallID == "preview-write" })
                } else {
                    XCTAssertNotEqual(info["nativeResume"] as? Bool, true)
                    XCTAssertNil(try session(f)["resume"] as? [String: Any])
                    try await reject(f, ["messageID": try XCTUnwrap(info["id"] as? String)])
                }
                XCTAssertEqual(try transcript(f).filter { $0["content"] as? String == "PREVIEW_PARTIAL" }.count, 1)
            }
        }
    }

    func testNewPromptAndManualCompactionInvalidateResumeWithoutTrimmingFailedHistory() async throws {
        for compact in [false, true] {
            let f = try await fixture()
            f.model.enqueue(Script(events: [.text("Older completed answer"), .finished]))
            try await prompt(f, "Older request"); try await idle(f)
            f.model.enqueue(Script(events: [.text("FAILED_COMPACTION_PARTIAL")], failure: NativeModelClientError.transport(-1200)))
            try await prompt(f); try await idle(f)
            let info = try await failed(f), original = try transcript(f), visible = try await messages(f)
            if compact {
                f.model.enqueue(Script(events: [.text("Preserve the older request constraints."), .finished]))
                let result = try await request(f, "POST", "/compact")
                XCTAssertEqual((result as? [String: Any])?["started"] as? Bool, true)
                try await idle(f)
                XCTAssertNil(try session(f)["resume"] as? [String: Any])
                XCTAssertFalse(try String(decoding: json(rows(XCTUnwrap(f.model.requests.last).messages)), as: UTF8.self)
                    .contains("FAILED_COMPACTION_PARTIAL"))
                let compactedUI = try await messages(f)
                XCTAssertEqual(try history(compactedUI, ui: true), try history(visible, ui: true))
            }
            f.model.enqueue(Script(events: [.text("Fresh answer"), .finished]))
            try await prompt(f, "New request"); try await idle(f)
            XCTAssertFalse(try XCTUnwrap(f.model.requests.last).messages.contains { $0.content.contains("FAILED_COMPACTION_PARTIAL") })
            XCTAssertEqual(try history(Array(transcript(f).prefix(original.count))), try history(original))
            XCTAssertEqual(try transcript(f)[original.count - 1]["excludedFromContext"] as? Bool, true)
            XCTAssertNil(try session(f)["resume"] as? [String: Any])
            let currentUI = try await messages(f)
            XCTAssertFalse(currentUI.contains { ($0["info"] as? [String: Any])?["nativeResume"] as? Bool == true })
            try await reject(f, ["messageID": try XCTUnwrap(info["id"] as? String)])
        }
    }

    func testResumeKeepsCumulativeThirtyTwoStepLimitInsteadOfStartingAnotherBudget() async throws {
        let f = try await fixture(), file = f.root.appendingPathComponent("evidence.txt")
        try "Evidence".write(to: file, atomically: true, encoding: .utf8)
        for step in 0..<31 {
            f.model.enqueue(Script(events: [.toolCall(try call("read-\(step)", "read", ["filePath": "evidence.txt"])), .finished]))
        }
        f.model.enqueue(Script(events: [.text("FAILED_BUDGET_PARTIAL")], failure: NativeModelClientError.transport(-1200)))
        try await prompt(f); try await idle(f)
        let info = try await failed(f)
        XCTAssertEqual(f.model.requests.count, 32); XCTAssertEqual(try checkpoint(f)["step"] as? Int, 31)
        f.model.enqueue(Script(events: [.toolCall(try call("read-last", "read", ["filePath": "evidence.txt"])), .finished]))
        try await resume(f, info); try await idle(f)
        XCTAssertEqual(f.model.requests.count, 33, "Only the final remaining step may run")
        let input = try XCTUnwrap(f.model.requests.last).messages, visible = try await messages(f)
        XCTAssertEqual(input.filter { $0.role == "tool" }.count, 31)
        XCTAssertFalse(input.contains { $0.content.contains("FAILED_BUDGET_PARTIAL") })
        let error = try XCTUnwrap((visible.last?["info"] as? [String: Any])?["error"] as? [String: Any])
        XCTAssertTrue(try String(decoding: json(error), as: UTF8.self).contains("32-step"))
        XCTAssertFalse(visible.contains { ($0["info"] as? [String: Any])?["nativeResume"] as? Bool == true })
        XCTAssertEqual(visible.filter { ($0["info"] as? [String: Any])?["role"] as? String == "user" }.count, 1)
        XCTAssertNil(try session(f)["resume"] as? [String: Any])
    }

    func testCancelledAndNonrecoverablePartialTextStaysVisibleButIsExcludedFromNewPrompt() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let failures: [Error?] = [nil, CancellationError(), NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled),
                                     NativeModelClientError.invalidRequest("bad request")]
            for failure in failures {
                let f = try await fixture(format)
                f.model.enqueue(Script(events: [.text("CANCELLED_OR_INVALID_PARTIAL")], failure: failure, holdOpen: failure == nil))
                try await prompt(f)
                if failure == nil {
                    try await waitUntil { try self.transcript(f).last?["content"] as? String == "CANCELLED_OR_INVALID_PARTIAL" }
                    _ = try await request(f, "POST", "/abort")
                }
                try await idle(f)
                let visible = try await messages(f), full = try transcript(f)
                let info = try XCTUnwrap(visible.last?["info"] as? [String: Any])
                XCTAssertNotNil(info["error"]); XCTAssertNotEqual(info["nativeResume"] as? Bool, true)
                XCTAssertNil(try session(f)["resume"] as? [String: Any])
                XCTAssertEqual(full.last?["excludedFromContext"] as? Bool, true)
                XCTAssertEqual(f.model.requests.count, 1, "Cancellation and invalid requests must not auto-retry")
                f.model.enqueue(Script(events: [.text("Fresh answer"), .finished]))
                try await prompt(f, "New request after interruption"); try await idle(f)
                let input = try XCTUnwrap(f.model.requests.last).messages, after = try await messages(f)
                XCTAssertEqual(f.model.requests.count, 2)
                XCTAssertEqual(input.filter { $0.role == "user" }.count, 2)
                XCTAssertFalse(input.contains { $0.content.contains("CANCELLED_OR_INVALID_PARTIAL") })
                XCTAssertEqual(try json(Array(after.prefix(visible.count))), try json(visible), "Failed UI message remains intact")
                XCTAssertEqual(try json(Array(transcript(f).prefix(full.count))), try json(full))
                try await reject(f, ["messageID": try XCTUnwrap(info["id"] as? String)])
            }
        }
    }

    func testCrashBeforeProviderTerminalPreservesPartialButExcludesUnverifiedCallOnRecovery() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let f = try await fixture(format), file = f.root.appendingPathComponent("crash-write.txt")
            let write = try call("unverified-crash-write", "write", ["filePath": "crash-write.txt", "content": "Must not execute"])
            // Deliberately fake a call before provider termination; no .finished authorizes execution.
            f.model.enqueue(Script(events: [.text("CRASH_PARTIAL"), .toolCall(write)], holdOpen: true))
            try await prompt(f)
            try await waitUntil {
                (try self.transcript(f).last?["toolCalls"] as? [[String: String]])?.first?["id"] == write.id
            }
            let visible = try await messages(f), full = try transcript(f), saved = try checkpoint(f)
            let info = try XCTUnwrap(visible.last?["info"] as? [String: Any])
            let beforeRequest = try f.model.snapshot(at: 0)
            XCTAssertEqual(try json(saved), try json(try checkpoint(f, in: beforeRequest)))
            XCTAssertEqual(saved["messageID"] as? String, info["id"] as? String)
            XCTAssertEqual(saved["transcriptBoundary"] as? Int, full.count - 1)
            XCTAssertEqual(try transcript(f, in: beforeRequest).last?["content"] as? String, "")
            XCTAssertTrue(try XCTUnwrap(try transcript(f, in: beforeRequest).last?["toolCalls"] as? [[String: String]]).isEmpty)
            XCTAssertFalse(full.contains { $0["toolCallID"] as? String == write.id })
            XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
            let url = f.root.appendingPathComponent("native-agent/state.json"), crash = try Data(contentsOf: url)
            XCTAssertNotNil((try snapshot(f)["active"] as? [String: [String: Any]])?[f.id])
            // Stop the old writer before restoring its pre-terminal snapshot to simulate a crash.
            await f.backend.shutdown()
            try crash.write(to: url, options: .atomic)
            let model = ScriptedModel(f.root), reopened = try backend(f.root, model, f.credentials)
            let restored = Fixture(root: f.root, credentials: f.credentials, model: model, backend: reopened, id: f.id)
            try await idle(restored)
            XCTAssertTrue(model.requests.isEmpty, "Recovery must not automatically request or replay anything")
            let recovered = try transcript(restored), recoveredUI = try await messages(restored)
            XCTAssertEqual(recovered.count, full.count, "Do not synthesize an output for an unverified call")
            XCTAssertEqual(try history(recovered), try history(full), "All historical content and the original call stay on disk")
            XCTAssertEqual(recovered.last?["excludedFromContext"] as? Bool, true)
            XCTAssertFalse(recovered.contains { $0["toolCallID"] as? String == write.id })
            XCTAssertNil(try session(restored)["resume"] as? [String: Any])
            XCTAssertTrue((try snapshot(restored)["active"] as? [String: [String: Any]] ?? [:]).isEmpty)
            XCTAssertEqual(recoveredUI.count, visible.count)
            let recoveredInfo = try XCTUnwrap(recoveredUI.last?["info"] as? [String: Any])
            XCTAssertEqual(recoveredInfo["id"] as? String, info["id"] as? String)
            XCTAssertEqual(recoveredInfo["parentID"] as? String, info["parentID"] as? String)
            XCTAssertNotNil(recoveredInfo["error"]); XCTAssertNotEqual(recoveredInfo["nativeResume"] as? Bool, true)
            let oldParts = try XCTUnwrap(visible.last?["parts"] as? [[String: Any]])
            let parts = try XCTUnwrap(recoveredUI.last?["parts"] as? [[String: Any]])
            XCTAssertEqual(parts.compactMap { $0["id"] as? String }, oldParts.compactMap { $0["id"] as? String })
            XCTAssertEqual(parts.compactMap { $0["text"] as? String }, ["CRASH_PARTIAL"])
            let tool = try XCTUnwrap(parts.first { $0["callID"] as? String == write.id })
            XCTAssertEqual((tool["state"] as? [String: Any])?["status"] as? String, "error")
            model.enqueue(Script(events: [.text("Fresh answer"), .finished]))
            try await prompt(restored, "New request after crash"); try await idle(restored)
            XCTAssertEqual(model.requests.count, 1)
            let input = try XCTUnwrap(model.requests.first).messages
            XCTAssertEqual(input.filter { $0.role == "user" }.count, 2)
            XCTAssertFalse(input.contains { $0.content.contains("CRASH_PARTIAL") || $0.role == "tool" })
            XCTAssertTrue(input.flatMap(\.toolCalls).isEmpty)
            XCTAssertTrue(input.flatMap(\.replayItems).isEmpty)
            XCTAssertEqual(try json(Array(transcript(restored).prefix(recovered.count))), try json(recovered))
            let after = try await messages(restored)
            XCTAssertEqual(try json(Array(after.prefix(recoveredUI.count))), try json(recoveredUI))
            XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
            try await reject(restored, ["messageID": try XCTUnwrap(info["id"] as? String)])
        }
    }
}
