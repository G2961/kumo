import Foundation
import XCTest
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif
@testable import KumoCore

final class NativeAgentBackendTests: XCTestCase {
    private final class Credentials {
        var values: [String: String] = [:]
        func read(_ id: String) throws -> String? { values[id] }
        func write(_ id: String, _ value: String?) throws { values[id] = value }
    }

    private final class ScriptedModel: NativeModelClient {
        var scripts: [[NativeModelEvent]]
        private let lock = NSLock()
        private var recordedRequests: [NativeModelRequest] = []
        private var streamTerminated = false
        var requests: [NativeModelRequest] {
            lock.lock(); defer { lock.unlock() }
            return recordedRequests
        }
        var terminated: Bool {
            get { lock.lock(); defer { lock.unlock() }; return streamTerminated }
            set { lock.lock(); defer { lock.unlock() }; streamTerminated = newValue }
        }
        var failure: Error?
        var holdOpen = false
        var continuation: AsyncThrowingStream<NativeModelEvent, Error>.Continuation?

        init(_ scripts: [[NativeModelEvent]] = []) { self.scripts = scripts }

        func stream(request: NativeModelRequest) -> AsyncThrowingStream<NativeModelEvent, Error> {
            lock.lock()
            recordedRequests.append(request)
            lock.unlock()
            return AsyncThrowingStream { continuation in
                self.continuation = continuation
                continuation.onTermination = { _ in self.terminated = true }
                if !self.scripts.isEmpty {
                    for event in self.scripts.removeFirst() { continuation.yield(event) }
                }
                if let failure = self.failure { continuation.finish(throwing: failure) }
                else if !self.holdOpen { continuation.finish() }
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

    private func temporaryRoot() throws -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("native-backend-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return root
    }

    private func canonicalPath(_ path: String) throws -> String {
        guard let resolved = realpath(path, nil) else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        defer { free(resolved) }
        return String(cString: resolved)
    }

    private func backend(_ root: URL, _ model: ScriptedModel, _ credentials: Credentials = Credentials()) throws -> NativeAgentBackend {
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

    private func configure(_ backend: NativeAgentBackend, format: String = "@ai-sdk/openai") async throws {
        _ = try await request(backend, "PATCH", "/global/config", body: [
            "model": "test/scripted",
            "provider": ["test": ["name": "Scripted", "npm": format,
                                    "options": ["baseURL": "https://example.invalid/v1"],
                                    "models": ["scripted": ["name": "Scripted", "limit": ["context": 100000, "output": 1000]]]]]
        ])
        // A generated placeholder exercises auth without ever storing a real credential in git.
        _ = try await request(backend, "PUT", "/auth/test", body: ["type": "api", "key": UUID().uuidString])
    }

    private func session(_ backend: NativeAgentBackend, _ root: URL, rules: [[String: Any]]? = nil) async throws -> String {
        let result = try await request(backend, "POST", "/session", directory: root.path,
                                       body: rules.map { ["permission": $0] } ?? [:]) as! [String: Any]
        return result["id"] as! String
    }

    private func prompt(_ backend: NativeAgentBackend, _ id: String, text: String = "Read, change, verify.", agent: String = "build") async throws {
        _ = try await request(backend, "POST", "/session/\(id)/prompt_async", body: [
            "parts": [["type": "text", "text": text]], "agent": agent,
            "model": ["providerID": "test", "modelID": "scripted"],
            "system": "Android has bun and npm. Read the GitHub token from disk."
        ])
    }

    private func waitUntil(_ condition: () async throws -> Bool, file: StaticString = #filePath, line: UInt = #line) async throws {
        for _ in 0..<300 {
            if try await condition() { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        throw NSError(domain: "NativeAgentBackendTests.WaitTimeout", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "\(file):\(line): Timed out waiting for backend state"])
    }

    private func waitIdle(_ backend: NativeAgentBackend, _ id: String) async throws {
        try await waitUntil {
            let statuses = try await self.request(backend, "GET", "/session/status") as! [String: [String: Any]]
            return statuses[id]?["type"] as? String == "idle"
        }
    }

    private func messages(_ backend: NativeAgentBackend, _ id: String) async throws -> [[String: Any]] {
        try await request(backend, "GET", "/session/\(id)/message") as! [[String: Any]]
    }

    private func call(_ id: String, _ name: String, _ args: [String: Any]) throws -> NativeModelEvent {
        let json = try JSONSerialization.data(withJSONObject: args, options: .sortedKeys)
        return .toolCall(NativeToolCall(id: id, name: name, arguments: String(decoding: json, as: UTF8.self)))
    }

    func testWaitTimeoutThrowsInsteadOfContinuingWithInvalidState() async throws {
        do {
            try await waitUntil({ false }, file: "timeout-regression", line: 123)
            XCTFail("Timeout must stop the caller before it indexes missing state")
        } catch {
            XCTAssertEqual((error as NSError).domain, "NativeAgentBackendTests.WaitTimeout")
            XCTAssertTrue(error.localizedDescription.contains("timeout-regression:123"))
        }
    }

    func testSymlinkWorkspaceLocalPathsNeedOnlyWriteApprovalAndOutsideStillAsks() async throws {
        let root = try temporaryRoot()
        let project = root.appendingPathComponent("project", isDirectory: true)
        let alias = root.appendingPathComponent("project-alias", isDirectory: true)
        let sibling = root.appendingPathComponent("project-other", isDirectory: true)
        try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: sibling, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: project)
        let local = project.appendingPathComponent(".env.example")
        let outside = sibling.appendingPathComponent(".env.example")
        let written = project.appendingPathComponent("created.txt")
        try "local example".write(to: local, atomically: true, encoding: .utf8)
        try "outside example".write(to: outside, atomically: true, encoding: .utf8)
        let canonicalProject = try canonicalPath(project.path)
        let canonicalLocal = try canonicalPath(local.path)
        let model = ScriptedModel([
            [try call("local-read", "read", ["filePath": canonicalLocal]),
             try call("local-write", "write", ["filePath": "created.txt", "content": "approved local write"]),
             try call("outside-read", "read", ["filePath": outside.path]), .finished],
            [.text("External boundary respected."), .finished]
        ])
        let backend = try backend(root, model)
        let events = Events()
        await backend.setEventHandler(events.receive)
        try await configure(backend)
        let sid = try await session(backend, alias)
        let sessionInfo = try await request(backend, "GET", "/session/\(sid)") as! [String: Any]
        let storedDirectory = try XCTUnwrap(sessionInfo["directory"] as? String)
        XCTAssertEqual(try canonicalPath(storedDirectory), canonicalProject)
        // Preserve the native tools' requested/resolved spellings, including macOS /var aliases.
        let prepared = try NativeFileTools(history: NativePatchLedger(root: root)).prepare(
            name: "read", arguments: ["filePath": canonicalLocal], directory: storedDirectory, sessionID: sid, turnID: "probe")
        XCTAssertTrue(prepared.patterns.contains(canonicalLocal))

        try await prompt(backend, sid)
        try await waitUntil { !(try await self.request(backend, "GET", "/permission") as! [[String: Any]]).isEmpty }
        let permissions = try await request(backend, "GET", "/permission") as! [[String: Any]]
        let write = try XCTUnwrap(permissions.first)
        XCTAssertEqual(write["permission"] as? String, "write", "Local canonical read must not ask for external access")
        XCTAssertEqual((write["tool"] as? [String: Any])?["callID"] as? String, "local-write")
        XCTAssertFalse(FileManager.default.fileExists(atPath: written.path))
        let writeID = try XCTUnwrap(write["id"] as? String)
        _ = try await request(backend, "POST", "/permission/\(writeID)/reply", body: ["reply": "once"])
        try await waitUntil { !(try await self.request(backend, "GET", "/permission") as! [[String: Any]]).isEmpty }
        let pending = try await request(backend, "GET", "/permission") as! [[String: Any]]
        let external = try XCTUnwrap(pending.first)
        XCTAssertEqual(external["permission"] as? String, "external_directory")
        XCTAssertEqual((external["tool"] as? [String: Any])?["callID"] as? String, "outside-read", "A sibling sharing the project prefix is still external")
        XCTAssertEqual(try String(contentsOf: written), "approved local write")
        let externalID = try XCTUnwrap(external["id"] as? String)
        _ = try await request(backend, "POST", "/permission/\(externalID)/reply", body: ["reply": "reject"])
        try await waitIdle(backend, sid)
        XCTAssertEqual(model.requests.count, 2)
        XCTAssertTrue(model.requests[1].messages.contains { $0.toolCallID == "local-read" && $0.content.contains("local example") })
        XCTAssertFalse(model.requests[1].messages.contains { $0.content.contains("outside example") })
        XCTAssertEqual(events.values.filter { $0["type"] as? String == "permission.asked" }.compactMap {
            ($0["properties"] as? [String: Any])?["permission"] as? String
        }, ["write", "external_directory"])
    }

    func testSymlinkInsideWorkspaceToOutsideStillRequiresExternalApproval() async throws {
        let root = try temporaryRoot()
        let project = root.appendingPathComponent("project", isDirectory: true)
        let outside = root.appendingPathComponent("outside", isDirectory: true)
        try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
        let target = outside.appendingPathComponent("example.txt")
        let alias = project.appendingPathComponent("local-looking.txt")
        try "external evidence".write(to: target, atomically: true, encoding: .utf8)
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: target)
        let model = ScriptedModel([[try call("escape-read", "read", ["filePath": "local-looking.txt"]), .finished],
                                   [.text("Denied outside read."), .finished]])
        let backend = try backend(root, model)
        try await configure(backend)
        let sid = try await session(backend, project)
        try await prompt(backend, sid)
        try await waitUntil { !(try await self.request(backend, "GET", "/permission") as! [[String: Any]]).isEmpty }
        let pending = try await request(backend, "GET", "/permission") as! [[String: Any]]
        let permission = try XCTUnwrap(pending.first)
        XCTAssertEqual(permission["permission"] as? String, "external_directory")
        XCTAssertTrue((permission["patterns"] as? [String] ?? []).contains(try canonicalPath(target.path)), "The resolved target must not be ignored because the requested path is local")
        let permissionID = try XCTUnwrap(permission["id"] as? String)
        _ = try await request(backend, "POST", "/permission/\(permissionID)/reply", body: ["reply": "reject"])
        try await waitIdle(backend, sid)
        XCTAssertEqual(model.requests.count, 2)
        XCTAssertFalse(model.requests[1].messages.contains { $0.content.contains("external evidence") })
    }

    func testGlobalSessionSearchCrossesBridgeDirectoryAndFiltersLiteralTitles() async throws {
        let root = try temporaryRoot()
        let other = try temporaryRoot()
        let backend = try backend(root, ScriptedModel())
        let first = try await request(backend, "POST", "/session", directory: root.path,
                                      body: ["title": "Fix Cache 100% + UI"]) as! [String: Any]
        try await Task.sleep(nanoseconds: 10_000_000)
        let second = try await request(backend, "POST", "/session", directory: other.path,
                                       body: ["title": "Review CACHE 100% + UI"]) as! [String: Any]
        _ = try await request(backend, "POST", "/session", directory: other.path, body: ["title": "Unrelated"])
        let firstID = first["id"] as! String, secondID = second["id"] as! String
        let snapshot = root.appendingPathComponent("native-agent/state.json")
        let before = try Data(contentsOf: snapshot)
        let doc = try await request(backend, "GET", "/doc") as! [String: Any]
        let operation = ((doc["paths"] as? [String: [String: Any]])?["/experimental/session"]?["get"] as? [String: Any])
        XCTAssertNotNil(operation, "UI discovers global search through /doc")
        XCTAssertEqual((operation?["parameters"] as? [[String: Any]])?.compactMap { $0["name"] as? String }, ["directory", "search"])

        let all = try await request(backend, "GET", "/experimental/session", directory: root.path) as! [[String: Any]]
        XCTAssertEqual(all.count, 3, "The bridge's current project must not scope global search")
        let matches = try await request(backend, "GET", "/experimental/session?search=%20cAcHe%20100%25%20%2B%20UI%20",
                                        directory: root.path) as! [[String: Any]]
        XCTAssertEqual(matches.compactMap { $0["id"] as? String }, [secondID, firstID])
        XCTAssertTrue(matches.allSatisfy { ($0["project"] as? [String: Any])?["worktree"] as? String == $0["directory"] as? String })
        let scoped = try await request(backend, "GET", "/experimental/session?search=cache&directory=\(other.path)",
                                       directory: root.path) as! [[String: Any]]
        XCTAssertEqual(scoped.compactMap { $0["id"] as? String }, [secondID])
        let local = try await request(backend, "GET", "/session?search=cache", directory: root.path) as! [[String: Any]]
        XCTAssertEqual(local.compactMap { $0["id"] as? String }, [firstID])
        let blank = try await request(backend, "GET", "/experimental/session?search=%20%20", directory: root.path) as! [[String: Any]]
        XCTAssertEqual(blank.count, 3)
        let absent = try await request(backend, "GET", "/experimental/session?search=missing", directory: root.path) as! [[String: Any]]
        XCTAssertTrue(absent.isEmpty)
        for path in ["/experimental/session?limit=1", "/experimental/session?search=cache&search=other", "/config?search=cache"] {
            do { _ = try await request(backend, "GET", path); XCTFail("Unsupported or ambiguous query accepted: \(path)") }
            catch { XCTAssertTrue(error.localizedDescription.contains("query")) }
        }
        XCTAssertEqual(try Data(contentsOf: snapshot), before, "Search must never change persisted sessions")
        await backend.shutdown()
    }

    func testManualModelWithoutContextRejectsBeforeAdmittingTurn() async throws {
        let root = try temporaryRoot()
        let model = ScriptedModel([[.text("Configured."), .finished]])
        let backend = try backend(root, model)
        try await configure(backend)
        _ = try await request(backend, "PATCH", "/global/config", body: [
            "provider": ["test": ["models": ["scripted": ["limit": NSNull()]]]]
        ])
        let id = try await session(backend, root)
        let snapshot = root.appendingPathComponent("native-agent/state.json")
        let before = try Data(contentsOf: snapshot)
        do { try await prompt(backend, id); XCTFail("Manual models must supply context rather than guessing") }
        catch { XCTAssertTrue(error.localizedDescription.contains("limit.context")) }
        let saved = try await messages(backend, id)
        XCTAssertTrue(saved.isEmpty)
        XCTAssertTrue(model.requests.isEmpty)
        XCTAssertEqual(try Data(contentsOf: snapshot), before)
        _ = try await request(backend, "PATCH", "/global/config", body: [
            "provider": ["test": ["models": ["scripted": ["limit": ["context": 100000]]]]]
        ])
        try await prompt(backend, id)
        try await waitIdle(backend, id)
        XCTAssertEqual(model.requests.count, 1)
        await backend.shutdown()
    }

    func testToolPreviewKeepsWireOrderAndExecutesOnlyAfterVerifiedFinish() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("preview.txt")
        let model = ScriptedModel([[.reasoning("Before tool."), .toolStarted(id: "write-preview", name: "wri"),
                                    .text("After tool start.")]])
        model.holdOpen = true
        let backend = try backend(root, model)
        let events = Events()
        await backend.setEventHandler(events.receive)
        try await configure(backend)
        let sid = try await session(backend, root, rules: [["permission": "*", "pattern": "*", "action": "allow"]])
        try await prompt(backend, sid)
        try await waitUntil {
            (try await self.messages(backend, sid)).flatMap { $0["parts"] as! [[String: Any]] }.contains { $0["text"] as? String == "After tool start." }
        }
        let previewMessages = try await messages(backend, sid)
        let previewParts = previewMessages.last?["parts"] as! [[String: Any]]
        XCTAssertEqual(previewParts.compactMap { $0["type"] as? String }, ["reasoning", "tool", "text"])
        let preview = previewParts[1]
        let previewID = preview["id"] as! String
        let pending = preview["state"] as! [String: Any]
        XCTAssertEqual(pending["status"] as? String, "pending")
        XCTAssertEqual(preview["tool"] as? String, "wri", "Partial provider names are display-only until verification")
        XCTAssertTrue((pending["input"] as! [String: Any]).isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        let snapshot = root.appendingPathComponent("native-agent/state.json")
        let state = try JSONSerialization.jsonObject(with: Data(contentsOf: snapshot)) as! [String: Any]
        let transcript = (state["transcripts"] as! [String: [[String: Any]]])[sid]!
        XCTAssertTrue(transcript.allSatisfy { ($0["toolCalls"] as! [[String: String]]).isEmpty })
        let stream = try XCTUnwrap(model.continuation)
        stream.yield(try call("write-preview", "write", ["filePath": file.path, "content": "verified write"]))
        try await waitUntil {
            (try await self.messages(backend, sid)).flatMap { $0["parts"] as! [[String: Any]] }.contains {
                $0["id"] as? String == previewID && ($0["state"] as? [String: Any])?["status"] as? String == "running"
            }
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path), "Verified arguments alone must not execute before stream finish")
        model.scripts = [[.text("Confirmed."), .finished]]
        model.holdOpen = false
        stream.yield(.finished)
        stream.finish()
        try await waitIdle(backend, sid)
        XCTAssertEqual(try String(contentsOf: file), "verified write")
        XCTAssertEqual(model.requests.count, 2)
        XCTAssertTrue(model.requests.allSatisfy { $0.maxOutputTokens == 1000 })
        let saved = try await messages(backend, sid)
        let parts = saved.flatMap { $0["parts"] as! [[String: Any]] }
        let tools = parts.filter { $0["type"] as? String == "tool" }
        XCTAssertEqual(tools.count, 1)
        XCTAssertEqual(tools[0]["id"] as? String, previewID)
        XCTAssertEqual(tools[0]["tool"] as? String, "write")
        XCTAssertEqual((tools[0]["state"] as? [String: Any])?["status"] as? String, "completed")
        XCTAssertEqual(((tools[0]["state"] as? [String: Any])?["input"] as? [String: Any])?["content"] as? String, "verified write")
        XCTAssertEqual(parts.compactMap { $0["type"] as? String }, ["text", "reasoning", "tool", "text", "text"])
        XCTAssertEqual(model.requests[1].messages.flatMap { $0.toolCalls }.map(\.id), ["write-preview"])
        XCTAssertEqual(model.requests[1].messages.filter { $0.toolCallID == "write-preview" }.count, 1)
        let wireParts = events.values.compactMap { event -> [String: Any]? in
            guard event["type"] as? String == "message.part.updated" else { return nil }
            return (event["properties"] as? [String: Any])?["part"] as? [String: Any]
        }
        var seen = Set<String>()
        let wireOrder = wireParts.filter { seen.insert($0["id"] as! String).inserted }
        XCTAssertEqual(wireOrder.compactMap { $0["type"] as? String }, ["text", "reasoning", "tool", "text", "text"], "Live Work log boundaries must follow wire order, not terminal tool-call order")
        let statuses = wireParts.compactMap { part -> String? in
            guard part["id"] as? String == previewID else { return nil }
            return (part["state"] as? [String: Any])?["status"] as? String
        }
        XCTAssertEqual(statuses.first, "pending")
        XCTAssertTrue(statuses.contains("running"))
        XCTAssertEqual(statuses.last, "completed")
        await backend.shutdown()
    }

    func testUnverifiedPreviewInterruptionMarksErrorWithoutCallableHistoryOrWrites() async throws {
        for interruption in ["truncated", "abort", "background", "unverified-finish"] {
            let root = try temporaryRoot()
            let file = root.appendingPathComponent("never.txt")
            let script: [NativeModelEvent] = [.toolStarted(id: "preview-only", name: "write"), .text("After preview.")]
                + (interruption == "unverified-finish" ? [.finished] : [])
            let model = ScriptedModel([script])
            model.holdOpen = ["abort", "background"].contains(interruption)
            let backend = try backend(root, model)
            let events = Events()
            await backend.setEventHandler(events.receive)
            try await configure(backend)
            let sid = try await session(backend, root, rules: [["permission": "*", "pattern": "*", "action": "allow"]])
            try await prompt(backend, sid)
            if model.holdOpen {
                try await waitUntil {
                    (try await self.messages(backend, sid)).flatMap { $0["parts"] as! [[String: Any]] }.contains { $0["text"] as? String == "After preview." }
                }
                if interruption == "background" { await backend.interruptActiveRuns() }
                else { _ = try await request(backend, "POST", "/session/\(sid)/abort") }
            }
            try await waitIdle(backend, sid)
            XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
            XCTAssertEqual(model.requests.count, 1)
            let saved = try await messages(backend, sid)
            XCTAssertNotNil((saved.last?["info"] as? [String: Any])?["error"])
            let tool = (saved.last?["parts"] as! [[String: Any]]).first { $0["type"] as? String == "tool" }!
            XCTAssertEqual((tool["state"] as? [String: Any])?["status"] as? String, "error", interruption)
            XCTAssertNotNil(((tool["state"] as? [String: Any])?["time"] as? [String: Any])?["end"])
            XCTAssertTrue(events.values.contains {
                guard $0["type"] as? String == "message.part.updated",
                      let part = ($0["properties"] as? [String: Any])?["part"] as? [String: Any] else { return false }
                return part["callID"] as? String == "preview-only" && (part["state"] as? [String: Any])?["status"] as? String == "pending"
            })
            model.scripts = [[.text("New turn."), .finished]]
            model.holdOpen = false
            try await prompt(backend, sid, text: "Do not replay the preview.")
            try await waitIdle(backend, sid)
            XCTAssertEqual(model.requests.count, 2)
            XCTAssertTrue(model.requests[1].messages.allSatisfy { $0.toolCalls.isEmpty && $0.toolCallID == nil })
            XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
            await backend.shutdown()
        }
    }

    func testCrashRecoveryMarksPreviewErrorWithoutInventingCallableToolResult() async throws {
        let root = try temporaryRoot()
        let model = ScriptedModel([[.toolStarted(id: "crash-preview", name: "write"), .text("Preview only.")]])
        model.holdOpen = true
        let credentials = Credentials()
        let backend = try backend(root, model, credentials)
        try await configure(backend)
        let sid = try await session(backend, root)
        try await prompt(backend, sid)
        try await waitUntil {
            (try await self.messages(backend, sid)).flatMap { $0["parts"] as! [[String: Any]] }.contains { $0["text"] as? String == "Preview only." }
        }
        let snapshot = root.appendingPathComponent("native-agent/state.json")
        let crashData = try Data(contentsOf: snapshot)
        await backend.shutdown()
        try crashData.write(to: snapshot, options: .atomic)
        let reopened = try self.backend(root, model, credentials)
        let saved = try await messages(reopened, sid)
        let tool = (saved.last?["parts"] as! [[String: Any]]).first { $0["type"] as? String == "tool" }!
        XCTAssertEqual((tool["state"] as? [String: Any])?["status"] as? String, "error")
        model.holdOpen = false
        model.scripts = [[.text("Recovered."), .finished]]
        try await prompt(reopened, sid, text: "Continue without executing the preview.")
        try await waitIdle(reopened, sid)
        XCTAssertEqual(model.requests.count, 2)
        XCTAssertTrue(model.requests[1].messages.allSatisfy { $0.toolCalls.isEmpty && $0.toolCallID == nil })
        await reopened.shutdown()
    }

    func testOutputReserveIsPassedWhenModelOutputLimitIsOmitted() async throws {
        let root = try temporaryRoot()
        let model = ScriptedModel([[.text("ok"), .finished]])
        let backend = try backend(root, model)
        try await configure(backend)
        _ = try await request(backend, "PATCH", "/global/config", body: [
            "provider": ["test": ["models": ["scripted": ["limit": ["output": NSNull()]]]]]
        ])
        let sid = try await session(backend, root)
        try await prompt(backend, sid)
        try await waitIdle(backend, sid)
        XCTAssertEqual(model.requests.first?.maxOutputTokens, 4096)
        await backend.shutdown()
    }

    func testLiveEventsReconstructChronologyAndIdleCompletesRunningConsumer() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("read.txt")
        try "evidence".write(to: file, atomically: true, encoding: .utf8)
        let model = ScriptedModel([
            [.reasoning("first"), try call("read", "read", ["filePath": file.path]),
             .reasoning("second"), .text("commentary"), .reasoning("third"), .finished],
            [.text("answer"), .finished]
        ])
        let backend = try backend(root, model)
        let events = Events()
        await backend.setEventHandler(events.receive)
        try await configure(backend)
        let id = try await session(backend, root)
        try await prompt(backend, id)
        try await waitIdle(backend, id)
        var liveParts: [String: [String: Any]] = [:]
        var order: [String] = []
        var running = false, completedTurns = 0
        for event in events.values {
            let properties = event["properties"] as! [String: Any]
            switch event["type"] as? String {
            case "message.part.updated":
                let part = properties["part"] as! [String: Any]
                let partID = part["id"] as! String
                if liveParts[partID] == nil { order.append(partID) }
                if let previous = liveParts[partID], let end = (previous["time"] as? [String: Any])?["end"] as? Int64 {
                    XCTAssertEqual(part["text"] as? String, previous["text"] as? String, "Completed segments must stay stable")
                    XCTAssertEqual((part["time"] as? [String: Any])?["end"] as? Int64, end)
                }
                liveParts[partID] = part
            case "message.part.delta":
                let partID = properties["partID"] as! String
                XCTAssertNotNil(liveParts[partID], "An ordered, typed part must precede its first delta")
                let field = properties["field"] as! String
                let updated = (liveParts[partID]?[field] as? String ?? "") + (properties["delta"] as! String)
                liveParts[partID]?[field] = updated
            case "session.status":
                running = (properties["status"] as! [String: Any])["type"] as? String != "idle"
            case "session.idle":
                if running { completedTurns += 1 }
                running = false
            default: break
            }
        }
        XCTAssertEqual(completedTurns, 1, "Staged UI runs its completion effects only if session.idle observes a running turn")
        XCTAssertFalse(running)
        let parts = order.compactMap { liveParts[$0] }
        XCTAssertEqual(parts.compactMap { $0["type"] as? String }, ["text", "reasoning", "tool", "reasoning", "text", "reasoning", "text"])
        XCTAssertEqual(parts.filter { $0["type"] as? String == "reasoning" }.compactMap { $0["text"] as? String }, ["first", "second", "third"])
        let saved = try await messages(backend, id)
        let savedParts = saved.flatMap { $0["parts"] as! [[String: Any]] }
        XCTAssertEqual(try JSONSerialization.data(withJSONObject: parts, options: .sortedKeys),
                       try JSONSerialization.data(withJSONObject: savedParts, options: .sortedKeys))
        await backend.shutdown()
    }

    func testUpdatedDenyAfterPendingApprovalCannotWriteAndResolvesPermissionOnce() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("denied.txt")
        let model = ScriptedModel([[try call("write", "write", ["filePath": file.path, "content": "denied"]), .finished],
                                   [.text("Denied."), .finished]])
        let backend = try backend(root, model)
        let events = Events()
        await backend.setEventHandler(events.receive)
        try await configure(backend)
        let id = try await session(backend, root)
        try await prompt(backend, id)
        try await waitUntil { !(try await self.request(backend, "GET", "/permission") as! [[String: Any]]).isEmpty }
        let approval = (try await request(backend, "GET", "/permission") as! [[String: Any]])[0]
        let approvalID = approval["id"] as! String
        _ = try await request(backend, "PATCH", "/session/\(id)", body: [
            "permission": [["permission": "write", "pattern": "*", "action": "deny"]]
        ])
        _ = try await request(backend, "POST", "/permission/\(approvalID)/reply", body: ["reply": "once"])
        try await waitIdle(backend, id)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        let pending = try await request(backend, "GET", "/permission") as! [[String: Any]]
        XCTAssertTrue(pending.isEmpty)
        XCTAssertTrue(model.requests[1].messages.contains { $0.toolCallID == "write" && $0.content.contains("updated session permission") })
        XCTAssertEqual(events.values.filter {
            $0["type"] as? String == "permission.replied" && ($0["properties"] as? [String: Any])?["requestID"] as? String == approvalID
        }.count, 1)
        await backend.shutdown()
    }

    func testReadWriteReadLoopReplaysResultsAndPersistsChronology() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("hello.txt")
        try "before".write(to: file, atomically: true, encoding: .utf8)
        let replay: [[String: Any]] = [
            ["type": "reasoning", "id": "opaque-state", "encrypted_content": "opaque"],
            ["type": "message", "role": "assistant", "content": [["type": "output_text", "text": "Checking."]]],
            ["type": "function_call", "call_id": "read-1", "name": "read", "arguments": "{\"filePath\":\"hello.txt\"}"]
        ]
        let model = ScriptedModel([
            [.text("Checking."), try call("read-1", "read", ["filePath": file.path]), .replay(replay), .finished],
            [.reasoning("supplied reasoning"), try call("write-1", "write", ["filePath": file.path, "content": "after"]), .text("Changed."), .finished],
            [try call("read-2", "read", ["filePath": file.path]), .finished],
            [.text("Verified."), .usage(input: 120, output: 9), .finished],
            [.text("History retained."), .finished]
        ])
        let credentials = Credentials()
        let backend = try backend(root, model, credentials)
        let events = Events()
        await backend.setEventHandler(events.receive)
        try await configure(backend)
        let id = try await session(backend, root, rules: [["permission": "*", "pattern": "*", "action": "allow"]])
        try await prompt(backend, id)
        try await waitIdle(backend, id)
        XCTAssertEqual(try String(contentsOf: file), "after")
        XCTAssertEqual(model.requests.count, 4)
        XCTAssertTrue(model.requests.allSatisfy { $0.maxOutputTokens == 1000 }, "Scripted clients without tool previews still use the reserved output limit")
        XCTAssertFalse(model.requests[0].system.contains("bun"))
        XCTAssertFalse(model.requests[0].system.contains("GitHub token"))
        XCTAssertTrue(model.requests[0].system.contains("Communication"))
        XCTAssertTrue(model.requests[1].messages.contains { $0.toolCallID == "read-1" && $0.content.contains("before") })
        XCTAssertTrue(model.requests[2].messages.contains { $0.toolCallID == "write-1" })
        XCTAssertTrue(model.requests[3].messages.contains { $0.toolCallID == "read-2" && $0.content.contains("after") })
        XCTAssertTrue(model.requests[3].messages.contains { $0.replayItems.first?["id"] as? String == "opaque-state" })
        let saved = try await messages(backend, id)
        let parts = saved.flatMap { $0["parts"] as! [[String: Any]] }
        XCTAssertEqual(parts.map { $0["type"] as! String }, ["text", "text", "tool", "reasoning", "tool", "text", "tool", "text"])
        XCTAssertEqual(parts.filter { $0["type"] as? String == "tool" }.count, 3)
        XCTAssertTrue(events.values.contains { $0["type"] as? String == "message.part.delta" })
        XCTAssertTrue(events.values.contains { $0["type"] as? String == "session.idle" })
        let userID = (saved[0]["info"] as! [String: Any])["id"] as! String
        for message in saved.dropFirst() {
            XCTAssertEqual((message["info"] as! [String: Any])["parentID"] as? String, userID)
        }
        let diff = try await request(backend, "GET", "/session/\(id)/diff") as! [[String: Any]]
        XCTAssertEqual(diff.count, 1)
        XCTAssertEqual(diff[0]["before"] as? String, "before")
        XCTAssertEqual(diff[0]["after"] as? String, "after")
        XCTAssertTrue((diff[0]["patch"] as? String ?? "").contains("+after"))
        await backend.shutdown()
        let reopened = try self.backend(root, model, credentials)
        let reopenedMessages = try await messages(reopened, id)
        XCTAssertTrue((reopenedMessages.flatMap { $0["parts"] as! [[String: Any]] }).filter { $0["type"] as? String == "tool" }.allSatisfy { ($0["state"] as? [String: Any])?["status"] as? String == "completed" })
        XCTAssertEqual(reopenedMessages.count, saved.count)
        XCTAssertEqual(model.requests.count, 4, "Opening persistence must never rerun a write")
        try await prompt(reopened, id, text: "Remember the verification.")
        try await waitIdle(reopened, id)
        XCTAssertTrue(model.requests[4].messages.contains { $0.toolCallID == "read-2" })
        XCTAssertTrue(model.requests[4].messages.contains { $0.replayItems.first?["id"] as? String == "opaque-state" })
        await reopened.shutdown()
    }

    func testPermissionRejectReturnsToolErrorToModelWithoutWriting() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("keep.txt")
        try "keep".write(to: file, atomically: true, encoding: .utf8)
        let model = ScriptedModel([[try call("write", "write", ["filePath": file.path, "content": "bad"]), .finished], [.text("Permission declined."), .finished]])
        let backend = try backend(root, model)
        try await configure(backend)
        let id = try await session(backend, root)
        try await prompt(backend, id)
        try await waitUntil { !(try await self.request(backend, "GET", "/permission") as! [[String: Any]]).isEmpty }
        let permission = (try await request(backend, "GET", "/permission") as! [[String: Any]])[0]
        XCTAssertEqual(permission["sessionID"] as? String, id)
        XCTAssertNotNil((permission["metadata"] as? [String: Any])?["diff"])
        let permissionID = permission["id"] as! String
        _ = try await request(backend, "POST", "/permission/\(permissionID)/reply", body: ["reply": "reject"])
        try await waitIdle(backend, id)
        XCTAssertEqual(try String(contentsOf: file), "keep")
        XCTAssertTrue(model.requests[1].messages.contains { $0.toolCallID == "write" && $0.content.lowercased().contains("reject") })
        do {
            _ = try await request(backend, "POST", "/permission/\(permissionID)/reply", body: ["reply": "once"])
            XCTFail("A permission may only be answered once")
        } catch { XCTAssertTrue((error as NSError).localizedDescription.contains("permission")) }
        await backend.shutdown()
    }

    func testStopPendingApprovalRejectsAndDoesNotResumeModel() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("never.txt")
        let model = ScriptedModel([[try call("write", "write", ["filePath": file.path, "content": "never"]), .finished]])
        let backend = try backend(root, model)
        let events = Events()
        await backend.setEventHandler(events.receive)
        try await configure(backend)
        let id = try await session(backend, root)
        try await prompt(backend, id)
        try await waitUntil { !(try await self.request(backend, "GET", "/permission") as! [[String: Any]]).isEmpty }
        _ = try await request(backend, "POST", "/session/\(id)/abort")
        try await waitIdle(backend, id)
        let pending = try await request(backend, "GET", "/permission") as! [[String: Any]]
        XCTAssertTrue(pending.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        XCTAssertEqual(model.requests.count, 1)
        XCTAssertTrue(events.values.contains { $0["type"] as? String == "permission.replied" && ($0["properties"] as? [String: Any])?["reply"] as? String == "reject" })
        await backend.shutdown()
    }

    func testBackgroundInterruptRejectsPendingApprovalsAndAllowsForegroundTurns() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("background.txt")
        let model = ScriptedModel([
            [try call("write-first", "write", ["filePath": file.path, "content": "must not run"]), .finished],
            [try call("write-second", "write", ["filePath": file.path, "content": "must not run either"]), .finished],
            [.text("Foreground turn works."), .finished]
        ])
        let backend = try backend(root, model)
        let events = Events()
        await backend.setEventHandler(events.receive)
        try await configure(backend)
        let first = try await session(backend, root)
        let second = try await session(backend, root)
        let idle = try await session(backend, root)
        try await prompt(backend, first)
        try await waitUntil { (try await self.request(backend, "GET", "/permission") as! [[String: Any]]).count == 1 }
        try await prompt(backend, second)
        try await waitUntil { (try await self.request(backend, "GET", "/permission") as! [[String: Any]]).count == 2 }
        let approvals = try await request(backend, "GET", "/permission") as! [[String: Any]]
        let interruption = "Interrupted because iOS moved the app to the background. Resume with a new turn; no operation was replayed."

        await backend.interruptActiveRuns()
        let snapshot = root.appendingPathComponent("native-agent/state.json")
        let interrupted = try Data(contentsOf: snapshot)
        let state = try JSONSerialization.jsonObject(with: interrupted) as! [String: Any]
        XCTAssertTrue((state["active"] as! [String: Any]).isEmpty)
        let statuses = try await request(backend, "GET", "/session/status") as! [String: [String: Any]]
        XCTAssertTrue(statuses.values.allSatisfy { $0["type"] as? String == "idle" })
        let pending = try await request(backend, "GET", "/permission") as! [[String: Any]]
        XCTAssertTrue(pending.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        XCTAssertEqual(model.requests.count, 2)
        for sid in [first, second] {
            let saved = try await messages(backend, sid)
            let info = saved.last?["info"] as! [String: Any]
            XCTAssertEqual(((info["error"] as? [String: Any])?["data"] as? [String: Any])?["message"] as? String, interruption)
            XCTAssertNotNil((info["time"] as? [String: Any])?["completed"])
            let tool = (saved.last?["parts"] as! [[String: Any]]).first { $0["type"] as? String == "tool" }!
            XCTAssertEqual((tool["state"] as? [String: Any])?["status"] as? String, "error")
            XCTAssertTrue(events.values.contains { $0["type"] as? String == "session.error" && ($0["properties"] as? [String: Any])?["sessionID"] as? String == sid })
        }
        let idleMessages = try await messages(backend, idle)
        XCTAssertTrue(idleMessages.isEmpty)
        for approval in approvals {
            let approvalID = approval["id"] as! String
            XCTAssertEqual(events.values.filter {
                $0["type"] as? String == "permission.replied" && ($0["properties"] as? [String: Any])?["requestID"] as? String == approvalID
                    && ($0["properties"] as? [String: Any])?["reply"] as? String == "reject"
            }.count, 1)
            do {
                _ = try await request(backend, "POST", "/permission/\(approvalID)/reply", body: ["reply": "once"])
                XCTFail("Background interruption must invalidate the approval")
            } catch { XCTAssertTrue(error.localizedDescription.contains("permission")) }
        }
        await backend.interruptActiveRuns()
        XCTAssertEqual(try Data(contentsOf: snapshot), interrupted, "Interrupting idle runs is a no-op")
        try await prompt(backend, first, text: "Resume in the foreground.")
        try await waitIdle(backend, first)
        XCTAssertEqual(model.requests.count, 3)
        XCTAssertEqual(model.requests[2].messages.filter { $0.role == "tool" }.count, 1)
        let resumed = try await messages(backend, first)
        XCTAssertNil((resumed.last?["info"] as? [String: Any])?["error"])
        XCTAssertTrue((resumed.last?["parts"] as! [[String: Any]]).contains { $0["text"] as? String == "Foreground turn works." })
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        await backend.shutdown()
    }

    func testReplayCredentialIsRejectedWithoutRedactingOrExecutingTools() async throws {
        for field in ["id", "encrypted_content", "nested"] {
            let root = try temporaryRoot()
            let file = root.appendingPathComponent("never-replayed.txt")
            let model = ScriptedModel()
            let credentials = Credentials()
            let backend = try backend(root, model, credentials)
            let events = Events()
            await backend.setEventHandler(events.receive)
            try await configure(backend)
            let secret = credentials.values["test"]!
            var item: [String: Any] = ["type": "reasoning", "id": "opaque-id", "encrypted_content": "opaque-content"]
            if field == "nested" { item[field] = ["items": [["value": "prefix-\(secret)-suffix"]]] }
            else { item[field] = "prefix-\(secret)-suffix" }
            model.scripts = [[.text("Visible prefix-\(secret)-suffix"),
                              try call("write", "write", ["filePath": file.path, "content": "must not run"]), .replay([item]), .finished],
                             [.text("New turn works."), .finished]]
            let sid = try await session(backend, root, rules: [["permission": "*", "pattern": "*", "action": "allow"]])
            try await prompt(backend, sid)
            try await waitIdle(backend, sid)
            XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
            XCTAssertEqual(model.requests.count, 1)
            let saved = try await messages(backend, sid)
            let info = saved.last?["info"] as! [String: Any]
            let message = ((info["error"] as? [String: Any])?["data"] as? [String: Any])?["message"] as? String ?? ""
            XCTAssertTrue(message.contains("opaque replay"))
            let disk = try Data(contentsOf: root.appendingPathComponent("native-agent/state.json"))
            let state = try JSONSerialization.jsonObject(with: disk) as! [String: Any]
            let rows = (state["transcripts"] as! [String: [[String: Any]]])[sid]!
            XCTAssertTrue(rows.allSatisfy { ($0["replayItems"] as! [[String: Any]]).isEmpty })
            XCTAssertTrue(rows.contains { ($0["content"] as? String ?? "").contains("Visible prefix-[REDACTED]-suffix") }, "Non-opaque transcript text must remain redacted")
            XCTAssertFalse(String(decoding: disk, as: UTF8.self).contains(secret))
            XCTAssertFalse(String(decoding: try JSONSerialization.data(withJSONObject: events.values), as: UTF8.self).contains(secret))
            try await prompt(backend, sid, text: "Continue without repeating the write.")
            try await waitIdle(backend, sid)
            XCTAssertEqual(model.requests.count, 2)
            XCTAssertTrue(model.requests[1].messages.allSatisfy { $0.replayItems.isEmpty })
            XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
            await backend.shutdown()
        }
    }

    func testSafeReplaySurvivesReopenAndLaterCredentialCollisionRejectsPersistence() async throws {
        let root = try temporaryRoot()
        let opaque = UUID().uuidString
        let replay: [[String: Any]] = [["type": "reasoning", "id": "opaque-\(UUID().uuidString)",
                                       "encrypted_content": "prefix-\(opaque)-suffix"]]
        let model = ScriptedModel([[.replay(replay), .text("Saved."), .finished], [.text("Reopened."), .finished]])
        let credentials = Credentials()
        let backend = try backend(root, model, credentials)
        try await configure(backend)
        let sid = try await session(backend, root)
        try await prompt(backend, sid)
        try await waitIdle(backend, sid)
        await backend.shutdown()
        let reopened = try self.backend(root, model, credentials)
        try await prompt(reopened, sid, text: "Continue with exact replay.")
        try await waitIdle(reopened, sid)
        let restoredReplay = model.requests[1].messages.flatMap { $0.replayItems }
        XCTAssertEqual(try JSONSerialization.data(withJSONObject: restoredReplay, options: .sortedKeys),
                       try JSONSerialization.data(withJSONObject: replay, options: .sortedKeys))
        let snapshot = root.appendingPathComponent("native-agent/state.json")
        let before = try Data(contentsOf: snapshot)
        _ = try await request(reopened, "PUT", "/auth/test", body: ["type": "api", "key": opaque])
        do {
            _ = try await request(reopened, "PATCH", "/global/config", body: ["permission": "ask"])
            XCTFail("A newly known credential collision must reject persistence, not mutate opaque replay")
        } catch { XCTAssertTrue(error.localizedDescription.contains("opaque replay")) }
        XCTAssertEqual(try Data(contentsOf: snapshot), before)
        let config = try await request(reopened, "GET", "/global/config") as! [String: Any]
        XCTAssertNil(config["permission"], "Failed persistence must roll back the config patch")
        await reopened.shutdown()
    }

    func testStopStreamingCancelsStreamAndLateCompletionCannotOverwriteNextRun() async throws {
        let root = try temporaryRoot()
        let model = ScriptedModel([[.text("partial")]])
        model.holdOpen = true
        let backend = try backend(root, model)
        try await configure(backend)
        let id = try await session(backend, root)
        try await prompt(backend, id)
        try await waitUntil { model.requests.count == 1 }
        model.terminated = false
        try await waitUntil {
            let saved = try await self.messages(backend, id)
            return saved.flatMap { $0["parts"] as! [[String: Any]] }.contains { $0["text"] as? String == "partial" }
        }
        let oldStream = model.continuation
        _ = try await request(backend, "POST", "/session/\(id)/abort")
        try await waitUntil { model.terminated }
        model.scripts = [[.text("replacement"), .finished]]
        model.holdOpen = false
        try await prompt(backend, id, text: "Try again.")
        oldStream?.yield(.text("late garbage"))
        oldStream?.finish()
        try await waitIdle(backend, id)
        let json = try JSONSerialization.data(withJSONObject: try await messages(backend, id))
        let text = String(decoding: json, as: UTF8.self)
        XCTAssertTrue(text.contains("replacement"))
        XCTAssertFalse(text.contains("late garbage"))
        await backend.shutdown()
    }

    func testFailedStreamPersistsErrorAndIdleWithoutLeakingCredential() async throws {
        let root = try temporaryRoot()
        let model = ScriptedModel([[.text("partial")]])
        let credentials = Credentials()
        let backend = try backend(root, model, credentials)
        let events = Events()
        await backend.setEventHandler(events.receive)
        try await configure(backend)
        let secret = credentials.values["test"]!
        model.failure = NSError(domain: "Mock", code: 1, userInfo: [NSLocalizedDescriptionKey: "Authorization: Bearer \(secret)"])
        let id = try await session(backend, root)
        try await prompt(backend, id)
        try await waitIdle(backend, id)
        let saved = try await messages(backend, id)
        XCTAssertNotNil((saved.last?["info"] as? [String: Any])?["error"])
        XCTAssertTrue(events.values.contains { $0["type"] as? String == "session.error" })
        let json = try JSONSerialization.data(withJSONObject: events.values)
        XCTAssertFalse(String(decoding: json, as: UTF8.self).contains(secret))
        for file in FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.isRegularFileKey])!.allObjects.compactMap({ $0 as? URL }) {
            if (try? file.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true {
                XCTAssertFalse(String(decoding: try Data(contentsOf: file), as: UTF8.self).contains(secret))
            }
        }
        await backend.shutdown()
        let reopened = try self.backend(root, model, credentials)
        let restored = try await messages(reopened, id)
        XCTAssertNotNil((restored.last?["info"] as? [String: Any])?["error"])
        XCTAssertEqual(model.requests.count, 1)
        await reopened.shutdown()
    }

    func testValidationRejectsAttachmentsMissingAuthAndUnsupportedRoutesBeforeStorage() async throws {
        let root = try temporaryRoot()
        let model = ScriptedModel()
        let backend = try backend(root, model)
        let id = try await session(backend, root)
        do { try await prompt(backend, id); XCTFail("Missing config must fail") } catch {}
        try await configure(backend)
        _ = try await request(backend, "DELETE", "/auth/test")
        do { try await prompt(backend, id); XCTFail("Missing auth must fail") } catch {}
        _ = try await request(backend, "PUT", "/auth/test", body: ["type": "api", "key": UUID().uuidString])
        do {
            _ = try await request(backend, "POST", "/session/\(id)/prompt_async", body: ["parts": [["type": "file", "url": "data:text/plain;base64,YQ=="]]])
            XCTFail("Attachments must not disappear silently")
        } catch { XCTAssertTrue(error.localizedDescription.contains("attachment")) }
        let saved = try await messages(backend, id)
        XCTAssertTrue(saved.isEmpty)
        XCTAssertTrue(model.requests.isEmpty)
        for path in ["/mcp", "/skill", "/session/\(id)/fork"] {
            do { _ = try await request(backend, "GET", path); XCTFail("Unsupported route must fail") } catch {}
        }
        let doc = try await request(backend, "GET", "/doc") as! [String: Any]
        let paths = doc["paths"] as! [String: Any]
        XCTAssertNil(paths["/mcp"])
        XCTAssertNil(paths["/skill"])
        XCTAssertNil(paths["/question"])
        XCTAssertNil(paths["/event"])
        let questions = try await request(backend, "GET", "/question") as! [Any]
        XCTAssertTrue(questions.isEmpty)
        await backend.shutdown()
    }

    func testProviderConfigMergeCatalogFormatsAndSecretRejection() async throws {
        let root = try temporaryRoot()
        let model = ScriptedModel([[.text("ok"), .finished]])
        let backend = try backend(root, model)
        try await configure(backend, format: "@ai-sdk/openai-compatible")
        _ = try await request(backend, "PATCH", "/global/config", body: ["provider": ["test": ["models": ["scripted": ["variants": ["low": ["reasoningEffort": "low"]]]]]]])
        let providers = try await request(backend, "GET", "/config/providers") as! [String: Any]
        let provider = (providers["providers"] as! [[String: Any]])[0]
        XCTAssertEqual(provider["id"] as? String, "test")
        XCTAssertNotNil((provider["models"] as? [String: [String: Any]])?["scripted"]?["variants"])
        let catalog = try await request(backend, "GET", "/provider") as! [String: Any]
        XCTAssertEqual(catalog["connected"] as? [String], ["test"])
        for patch: [String: Any] in [
            ["provider": ["test": ["npm": "@ai-sdk/anthropic"]]],
            ["provider": ["test": ["options": ["baseURL": "http://example.invalid"]]]],
            ["provider": ["test": ["options": ["baseURL": "https://user:pass@example.invalid"]]]],
            ["provider": ["test": ["options": ["apiKey": UUID().uuidString]]]],
            ["mcp": ["server": [:]]]
        ] {
            do { _ = try await request(backend, "PATCH", "/global/config", body: patch); XCTFail("Invalid config accepted") } catch {}
        }
        let id = try await session(backend, root)
        try await prompt(backend, id)
        try await waitIdle(backend, id)
        XCTAssertEqual(model.requests[0].format, .chatCompletions)
        await backend.shutdown()
    }

    func testPlanDeniesWritesEvenWithAllowAndProcessesEveryToolCall() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("safe.txt")
        try "safe".write(to: file, atomically: true, encoding: .utf8)
        let model = ScriptedModel([[try call("denied", "write", ["filePath": file.path, "content": "unsafe"]), try call("read", "read", ["filePath": file.path]), .finished], [.text("Plan only."), .finished]])
        let backend = try backend(root, model)
        try await configure(backend)
        let id = try await session(backend, root, rules: [["permission": "*", "pattern": "*", "action": "allow"]])
        try await prompt(backend, id, agent: "plan")
        try await waitIdle(backend, id)
        XCTAssertEqual(try String(contentsOf: file), "safe")
        XCTAssertEqual(model.requests[1].messages.filter { $0.role == "tool" }.count, 2)
        XCTAssertTrue(model.requests[1].messages.contains { $0.toolCallID == "denied" && $0.content.lowercased().contains("plan") })
        await backend.shutdown()
    }

    func testConcurrentEditWhileApprovalWaitsIsNotOverwritten() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("shared.txt")
        try "original".write(to: file, atomically: true, encoding: .utf8)
        let model = ScriptedModel([[try call("write", "write", ["filePath": file.path, "content": "agent"]), .finished], [.text("Conflict handled."), .finished]])
        let backend = try backend(root, model)
        try await configure(backend)
        let id = try await session(backend, root)
        try await prompt(backend, id)
        try await waitUntil { !(try await self.request(backend, "GET", "/permission") as! [[String: Any]]).isEmpty }
        let permission = (try await request(backend, "GET", "/permission") as! [[String: Any]])[0]
        try "human".write(to: file, atomically: true, encoding: .utf8)
        _ = try await request(backend, "POST", "/permission/\(permission["id"] as! String)/reply", body: ["reply": "once"])
        try await waitIdle(backend, id)
        XCTAssertEqual(try String(contentsOf: file), "human")
        let toolParts = (try await messages(backend, id)).flatMap { $0["parts"] as! [[String: Any]] }.filter { $0["type"] as? String == "tool" }
        XCTAssertEqual((toolParts[0]["state"] as! [String: Any])["status"] as? String, "error")
        await backend.shutdown()
    }

    func testCrashSnapshotRecoveryMarksInterruptedAndNeverRepeatsCompletedWrite() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("crash.txt")
        let model = ScriptedModel([[try call("saved-write", "write", ["filePath": file.path, "content": "saved once"]), .finished], [.text("unfinished")]])
        let credentials = Credentials()
        let backend = try backend(root, model, credentials)
        try await configure(backend)
        model.holdOpen = true
        let id = try await session(backend, root, rules: [["permission": "*", "pattern": "*", "action": "allow"]])
        try await prompt(backend, id)
        try await waitUntil {
            (try await self.messages(backend, id)).flatMap { $0["parts"] as! [[String: Any]] }.contains { $0["text"] as? String == "unfinished" }
        }
        let snapshot = root.appendingPathComponent("native-agent/state.json")
        let crashData = try Data(contentsOf: snapshot)
        let state = try JSONSerialization.jsonObject(with: crashData) as! [String: Any]
        XCTAssertFalse((state["active"] as! [String: Any]).isEmpty)
        await backend.shutdown()
        // Simulate the disk image from an abrupt termination, with no second live writer.
        try crashData.write(to: snapshot, options: .atomic)
        let reopened = try self.backend(root, model, credentials)
        let saved = try await messages(reopened, id)
        XCTAssertNotNil((saved.last?["info"] as? [String: Any])?["error"])
        XCTAssertEqual(try String(contentsOf: file), "saved once")
        XCTAssertEqual(model.requests.count, 2)
        let statuses = try await request(reopened, "GET", "/session/status") as! [String: [String: Any]]
        XCTAssertEqual(statuses[id]?["type"] as? String, "idle")
        let pending = try await request(reopened, "GET", "/permission") as! [[String: Any]]
        XCTAssertTrue(pending.isEmpty)
        model.holdOpen = false
        model.scripts = [[.text("Recovered."), .finished]]
        try await prompt(reopened, id, text: "Continue, do not repeat the edit.")
        try await waitIdle(reopened, id)
        XCTAssertTrue(model.requests[2].messages.contains { $0.content == "unfinished" })
        XCTAssertEqual(model.requests[2].messages.filter { $0.toolCallID == "saved-write" }.count, 1)
        XCTAssertEqual(try String(contentsOf: file), "saved once")
        await reopened.shutdown()
    }

    func testAlwaysIsPersistedAndAppliesToNextCallWithSamePath() async throws {
        let root = try temporaryRoot()
        let file = root.appendingPathComponent("approved.txt")
        let model = ScriptedModel([[try call("first-write", "write", ["filePath": file.path, "content": "first"]), .finished], [try call("second-write", "write", ["filePath": file.path, "content": "second"]), .finished], [.text("Done."), .finished]])
        let credentials = Credentials()
        let backend = try backend(root, model, credentials)
        let events = Events()
        await backend.setEventHandler(events.receive)
        try await configure(backend)
        let id = try await session(backend, root)
        try await prompt(backend, id)
        try await waitUntil { !(try await self.request(backend, "GET", "/permission") as! [[String: Any]]).isEmpty }
        let permission = (try await request(backend, "GET", "/permission") as! [[String: Any]])[0]
        _ = try await request(backend, "POST", "/permission/\(permission["id"] as! String)/reply", body: ["reply": "always"])
        try await waitIdle(backend, id)
        XCTAssertEqual(try String(contentsOf: file), "second")
        XCTAssertEqual(events.values.filter { $0["type"] as? String == "permission.asked" }.count, 1)
        await backend.shutdown()
        let reopened = try self.backend(root, model, credentials)
        let session = try await request(reopened, "GET", "/session/\(id)") as! [String: Any]
        XCTAssertTrue((session["permission"] as! [[String: Any]]).contains { $0["permission"] as? String == "write" && $0["pattern"] as? String == file.path && $0["action"] as? String == "allow" })
        await reopened.shutdown()
    }

    func testLastMatchingReadRuleGatesEnvButAllowsEnvExample() async throws {
        let root = try temporaryRoot()
        let env = root.appendingPathComponent(".env")
        let example = root.appendingPathComponent(".env.example")
        try "private setting".write(to: env, atomically: true, encoding: .utf8)
        try "example setting".write(to: example, atomically: true, encoding: .utf8)
        let model = ScriptedModel([[try call("env-example", "read", ["filePath": example.path]), try call("env-private", "read", ["filePath": env.path]), .finished], [.text("Denied read respected."), .finished]])
        let backend = try backend(root, model)
        let events = Events()
        await backend.setEventHandler(events.receive)
        try await configure(backend)
        let id = try await session(backend, root)
        try await prompt(backend, id)
        try await waitUntil { !(try await self.request(backend, "GET", "/permission") as! [[String: Any]]).isEmpty }
        let permission = (try await request(backend, "GET", "/permission") as! [[String: Any]])[0]
        XCTAssertEqual(permission["permission"] as? String, "read")
        XCTAssertEqual((permission["tool"] as? [String: Any])?["callID"] as? String, "env-private")
        _ = try await request(backend, "POST", "/permission/\(permission["id"] as! String)/reply", body: ["reply": "reject"])
        try await waitIdle(backend, id)
        XCTAssertEqual(events.values.filter { $0["type"] as? String == "permission.asked" }.count, 1)
        XCTAssertTrue(model.requests[1].messages.contains { $0.toolCallID == "env-example" && $0.content.contains("example setting") })
        XCTAssertFalse(model.requests[1].messages.contains { $0.content.contains("private setting") })
        await backend.shutdown()
    }

    func testBusyAndContextBudgetRejectWithoutTrimmingOrStoringNewTurn() async throws {
        let root = try temporaryRoot()
        let model = ScriptedModel([[.text("partial")]])
        model.holdOpen = true
        let backend = try backend(root, model)
        try await configure(backend)
        let id = try await session(backend, root)
        try await prompt(backend, id)
        do { try await prompt(backend, id, text: "second while busy"); XCTFail("Busy session accepted another turn") }
        catch { XCTAssertTrue(error.localizedDescription.contains("busy")) }
        _ = try await request(backend, "POST", "/session/\(id)/abort")
        let before = try await messages(backend, id)
        _ = try await request(backend, "PATCH", "/global/config", body: ["provider": ["test": ["models": ["scripted": ["limit": ["context": 1100, "output": 1000]]]]]])
        do { try await prompt(backend, id, text: "context overflow"); XCTFail("Context overflow accepted") }
        catch { XCTAssertTrue(error.localizedDescription.contains("No history was trimmed")) }
        let after = try await messages(backend, id)
        XCTAssertEqual(after.count, before.count)
        let statuses = try await request(backend, "GET", "/session/status") as! [String: [String: Any]]
        XCTAssertEqual(statuses[id]?["type"] as? String, "idle")
        await backend.shutdown()
    }
}
