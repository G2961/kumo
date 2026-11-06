import Foundation
import XCTest
@testable import KumoCore

final class NativeProviderIntegrationTests: XCTestCase {
    private let fileName = "provider-created.txt"
    private let fileContent = "Written through the native agent 🌍\n"
    private let beforeText = "Preparing the file."
    private let afterText = "Waiting for the native tool."
    private let finalText = "Verified the file on disk."

    private struct WireTurn {
        let prefix: Data
        let terminal: Data
        let output: [[String: Any]]
        var complete: Data { prefix + terminal }
    }

    private struct Harness {
        let root: URL
        let project: URL
        let host: String
        let key: String
        let credentials: IntegrationCredentials
        let provider: IntegrationHTTPProvider
        let client: URLSessionNativeModelClient
        let backend: NativeAgentBackend
        let events: IntegrationEvents
        let sessionID: String
    }

    private func json(_ value: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    }

    private func frame(_ value: [String: Any], event: String? = nil) throws -> Data {
        let label = event.map { "event: \($0)\r\n" } ?? ""
        return Data((label + "data: " + String(decoding: try json(value), as: UTF8.self) + "\r\n\r\n").utf8)
    }

    private func chat(_ delta: [String: Any], finish: String? = nil) throws -> Data {
        try frame(["choices": [["index": 0, "delta": delta, "finish_reason": finish.map { $0 as Any } ?? NSNull()]]])
    }

    private func messageItem(_ id: String, _ text: String) -> [String: Any] {
        ["type": "message", "id": id, "role": "assistant", "status": "completed",
         "content": [["type": "output_text", "text": text, "annotations": []]]]
    }

    // Match the parser fixtures: item indexes refer to the authoritative completed output,
    // argument-done does not authorize execution, and Chat requires finish_reason AND [DONE].
    private func turn(_ format: NativeProviderFormat, step: Int, tool: String? = nil) throws -> WireTurn {
        let input = (step + 1) * 101
        let outputTokens = (step + 1) * 11
        let callID = tool.map { "call-\($0)" }
        let arguments: [String: Any] = tool == "write"
            ? ["filePath": fileName, "content": fileContent] : ["filePath": fileName]
        let argumentText = String(decoding: try json(arguments), as: UTF8.self)
        let split = argumentText.index(argumentText.startIndex, offsetBy: argumentText.count / 2)
        let fragments = [String(argumentText[..<split]), String(argumentText[split...])]
        var prefix = Data()
        var output: [[String: Any]] = []
        if format == .responses {
            if let tool, let callID {
                let itemID = "fc-\(tool)"
                prefix += try frame(["type": "response.reasoning_summary_text.delta", "delta": "Use native files."])
                prefix += try frame(["type": "response.output_text.delta", "delta": beforeText])
                prefix += try frame(["type": "response.output_item.added", "output_index": 2,
                                     "item": ["type": "function_call", "id": itemID, "call_id": callID,
                                              "name": tool, "arguments": "", "status": "in_progress"]])
                prefix += try frame(["type": "response.output_text.delta", "delta": afterText])
                for fragment in fragments {
                    prefix += try frame(["type": "response.function_call_arguments.delta", "output_index": 2,
                                         "item_id": itemID, "delta": fragment])
                }
                prefix += try frame(["type": "response.function_call_arguments.done", "output_index": 2,
                                     "item_id": itemID, "arguments": argumentText])
                let call: [String: Any] = ["type": "function_call", "id": itemID, "call_id": callID,
                                          "name": tool, "arguments": argumentText, "status": "completed"]
                prefix += try frame(["type": "response.output_item.done", "output_index": 2, "item": call])
                output = [["type": "reasoning", "id": "rs-\(step)", "summary": [
                    ["type": "summary_text", "text": "Use native files."]], "encrypted_content": "opaque-\(step)"],
                          messageItem("before-\(step)", beforeText), call, messageItem("after-\(step)", afterText)]
            } else {
                prefix += try frame(["type": "response.output_text.delta", "delta": finalText])
                output = [messageItem("final", finalText)]
            }
            let terminal = try frame(["type": "response.completed", "response": [
                "id": "response-\(step)", "status": "completed", "error": NSNull(),
                "incomplete_details": NSNull(), "output": output,
                "usage": ["input_tokens": input, "output_tokens": outputTokens,
                          "total_tokens": input + outputTokens]
            ]], event: "response.completed")
            return WireTurn(prefix: prefix, terminal: terminal, output: output)
        }
        if let tool, let callID {
            prefix += try chat(["reasoning_content": "Use native files."])
            prefix += try chat(["content": beforeText])
            prefix += try chat(["tool_calls": [["index": 0, "id": callID, "type": "function",
                                               "function": ["name": tool, "arguments": fragments[0]]]]])
            prefix += try chat(["content": afterText])
            prefix += try chat(["tool_calls": [["index": 0, "function": ["arguments": fragments[1]]]]])
        } else {
            prefix += try chat(["role": "assistant", "content": NSNull()])
            prefix += try chat(["content": finalText])
        }
        var terminal = try chat([:], finish: tool == nil ? "stop" : "tool_calls")
        terminal += try frame(["choices": [], "usage": ["prompt_tokens": input,
                                                        "completion_tokens": outputTokens,
                                                        "total_tokens": input + outputTokens]])
        terminal += Data("data: [DONE]\r\n\r\n".utf8)
        return WireTurn(prefix: prefix, terminal: terminal, output: [])
    }

    private func backend(root: URL, client: URLSessionNativeModelClient,
                         credentials: IntegrationCredentials) throws -> NativeAgentBackend {
        let backend = try NativeAgentBackend(root: root, modelClient: client,
                                             credentialRead: credentials.read, credentialWrite: credentials.write)
        addTeardownBlock { await backend.shutdown() }
        return backend
    }

    private func harness(_ format: NativeProviderFormat, replies: [IntegrationHTTPProvider.Reply],
                         allowWrite: Bool = false) async throws -> Harness {
        let root = FileManager.default.temporaryDirectory.resolvingSymlinksInPath()
            .appendingPathComponent("native-provider-\(UUID().uuidString)", isDirectory: true)
        let project = root.appendingPathComponent("project", isDirectory: true)
        try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let host = UUID().uuidString.lowercased() + ".example.test"
        let provider = IntegrationHTTPProvider(replies: replies)
        IntegrationProviderURLProtocol.register(provider, host: host)
        addTeardownBlock { IntegrationProviderURLProtocol.unregister(host: host) }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [IntegrationProviderURLProtocol.self]
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.timeoutIntervalForRequest = 10
        configuration.timeoutIntervalForResource = 15
        let client = URLSessionNativeModelClient(configuration: configuration)
        let credentials = IntegrationCredentials()
        let backend = try backend(root: root, client: client, credentials: credentials)
        let events = IntegrationEvents()
        await backend.setEventHandler(events.receive)
        let key = UUID().uuidString + UUID().uuidString
        _ = try await request(backend, "PATCH", "/global/config", body: [
            "model": "integration/file-agent", "provider": ["integration": [
                "name": "HTTP fixture", "npm": format == .responses ? "@ai-sdk/openai" : "@ai-sdk/openai-compatible",
                "options": ["baseURL": "https://\(host)/v1/"],
                "models": ["file-agent": ["name": "File agent", "limit": ["context": 100000, "output": 1000]]]
            ]]
        ])
        _ = try await request(backend, "PUT", "/auth/integration", body: ["type": "api", "key": key])
        let created = try await request(backend, "POST", "/session", directory: project.path, body: [
            "permission": [["permission": "read", "pattern": "*", "action": "allow"],
                           ["permission": "write", "pattern": "*", "action": allowWrite ? "allow" : "ask"]]
        ])
        let sessionID = try XCTUnwrap((created as? [String: Any])?["id"] as? String)
        return Harness(root: root, project: project, host: host, key: key, credentials: credentials,
                       provider: provider, client: client, backend: backend, events: events, sessionID: sessionID)
    }

    @discardableResult
    private func request(_ backend: NativeAgentBackend, _ method: String, _ path: String,
                         directory: String? = nil, body: [String: Any]? = nil) async throws -> Any {
        try await backend.request(method: method, path: path, directory: directory, body: body)
    }

    private func prompt(_ h: Harness) async throws {
        _ = try await request(h.backend, "POST", "/session/\(h.sessionID)/prompt_async", body: [
            "agent": "build", "model": ["providerID": "integration", "modelID": "file-agent"],
            "parts": [["type": "text", "text": "Create provider-created.txt, read it back, then confirm."]]
        ])
    }

    private func waitUntil(_ description: String, file: StaticString = #filePath, line: UInt = #line,
                           _ condition: () async throws -> Bool) async throws {
        let deadline = Date().addingTimeInterval(5)
        repeat {
            if try await condition() { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        } while Date() < deadline
        XCTFail("Timed out: \(description)", file: file, line: line)
        throw NSError(domain: "NativeProviderIntegrationTests.Timeout", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "\(file):\(line): \(description)"])
    }

    private func waitIdle(_ h: Harness) async throws {
        try await waitUntil("backend idle") {
            let statuses = try await self.request(h.backend, "GET", "/session/status")
            return (statuses as? [String: [String: Any]])?[h.sessionID]?["type"] as? String == "idle"
        }
    }

    private func messages(_ backend: NativeAgentBackend, _ sid: String) async throws -> [[String: Any]] {
        let result = try await request(backend, "GET", "/session/\(sid)/message")
        return try XCTUnwrap(result as? [[String: Any]])
    }

    private func permissions(_ h: Harness) async throws -> [[String: Any]] {
        let result = try await request(h.backend, "GET", "/permission")
        return try XCTUnwrap(result as? [[String: Any]])
    }

    private func history(_ backend: NativeAgentBackend, _ h: Harness) async throws -> [String: Any] {
        try await backend.historyAction("historyList", data: ["directory": h.project.path, "sessionID": h.sessionID])
    }

    private func eventPart(_ event: [String: Any]) -> [String: Any]? {
        guard event["type"] as? String == "message.part.updated" else { return nil }
        return (event["properties"] as? [String: Any])?["part"] as? [String: Any]
    }

    private func assertChronology(_ row: [String: Any], events: [[String: Any]], tool: String,
                                  status: String, file: StaticString = #filePath, line: UInt = #line) throws {
        let parts = try XCTUnwrap(row["parts"] as? [[String: Any]], file: file, line: line)
        XCTAssertEqual(parts.compactMap { $0["type"] as? String }, ["reasoning", "text", "tool", "text"], file: file, line: line)
        let toolPart = try XCTUnwrap(parts.first { $0["type"] as? String == "tool" }, file: file, line: line)
        XCTAssertEqual(toolPart["tool"] as? String, tool, file: file, line: line)
        XCTAssertEqual(toolPart["callID"] as? String, "call-\(tool)", file: file, line: line)
        XCTAssertEqual((toolPart["state"] as? [String: Any])?["status"] as? String, status, file: file, line: line)
        XCTAssertEqual(parts.filter { $0["type"] as? String == "text" }.compactMap { $0["text"] as? String },
                       [beforeText, afterText], file: file, line: line)
        let ids = parts.compactMap { $0["id"] as? String }
        var seen = Set<String>()
        let firstUpdates = events.compactMap(eventPart).compactMap { $0["id"] as? String }
            .filter { ids.contains($0) && seen.insert($0).inserted }
        XCTAssertEqual(firstUpdates, ids, "Streaming must preserve the toolStarted/text wire order", file: file, line: line)
        let preview = try XCTUnwrap(events.compactMap(eventPart).first {
            $0["id"] as? String == toolPart["id"] as? String
        }, file: file, line: line)
        let previewState = try XCTUnwrap(preview["state"] as? [String: Any], file: file, line: line)
        XCTAssertEqual(previewState["status"] as? String, "pending", file: file, line: line)
        XCTAssertEqual((previewState["input"] as? [String: Any])?.count, 0, file: file, line: line)
    }

    private func body(_ request: URLRequest) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: Any])
    }

    private func assertNoCredentialLeak(_ h: Harness, rows: [[String: Any]]) throws {
        for data in [try json(rows), try json(h.events.values),
                     try Data(contentsOf: h.root.appendingPathComponent("native-agent/state.json"))] {
            XCTAssertFalse(String(decoding: data, as: UTF8.self).contains(h.key))
        }
    }

    private func fileAgentFlow(_ format: NativeProviderFormat) async throws {
        let turns = try [turn(format, step: 0, tool: "write"), turn(format, step: 1, tool: "read"), turn(format, step: 2)]
        let h = try await harness(format, replies: turns.map { .init(body: $0.complete) })
        let snapshotBeforeCatalog = try Data(contentsOf: h.root.appendingPathComponent("native-agent/state.json"))
        let catalog = try await h.backend.modelCatalogRequest(providerID: "integration")
        XCTAssertEqual(catalog.url?.absoluteString, "https://\(h.host)/v1/models")
        XCTAssertEqual(catalog.httpMethod, "GET")
        XCTAssertEqual(catalog.value(forHTTPHeaderField: "Accept"), "application/json")
        XCTAssertEqual(catalog.cachePolicy, .reloadIgnoringLocalCacheData)
        XCTAssertNil(catalog.httpBody)
        XCTAssertTrue(catalog.value(forHTTPHeaderField: "Authorization") == "Bearer " + h.key)
        XCTAssertTrue(try h.credentials.read("integration") == h.key)
        XCTAssertTrue(h.provider.requests.isEmpty, "Preparing /models must not perform any networking")
        XCTAssertEqual(try Data(contentsOf: h.root.appendingPathComponent("native-agent/state.json")), snapshotBeforeCatalog)

        try await prompt(h)
        try await waitUntil("one write permission") { !(try await self.permissions(h)).isEmpty }
        let pending = try await permissions(h)
        XCTAssertEqual(pending.count, 1)
        let approval = try XCTUnwrap(pending.first)
        XCTAssertEqual(approval["permission"] as? String, "write")
        XCTAssertEqual(approval["sessionID"] as? String, h.sessionID)
        XCTAssertEqual((approval["tool"] as? [String: Any])?["callID"] as? String, "call-write")
        XCTAssertTrue(((approval["metadata"] as? [String: Any])?["diff"] as? String ?? "").contains("+Written through"))
        let target = h.project.appendingPathComponent(fileName)
        XCTAssertFalse(FileManager.default.fileExists(atPath: target.path), "Write must wait for approval")
        XCTAssertEqual(h.provider.requests.count, 1)
        let waitingRows = try await messages(h.backend, h.sessionID)
        try assertChronology(try XCTUnwrap(waitingRows.last), events: h.events.values, tool: "write", status: "running")
        let approvalID = try XCTUnwrap(approval["id"] as? String)
        _ = try await request(h.backend, "POST", "/permission/\(approvalID)/reply", body: ["reply": "once"])
        try await waitIdle(h)

        let requests = h.provider.requests
        XCTAssertEqual(requests.count, 3, "Exactly write, read, final: no retries or catalog HTTP")
        guard requests.count == 3 else { throw URLError(.badServerResponse) }
        XCTAssertEqual(h.provider.remainingReplies, 0)
        XCTAssertEqual(try String(contentsOf: target, encoding: .utf8), fileContent)
        let remainingPermissions = try await permissions(h)
        XCTAssertTrue(remainingPermissions.isEmpty)
        XCTAssertEqual(h.events.values.filter { $0["type"] as? String == "permission.asked" }.count, 1)
        let replies = h.events.values.filter { $0["type"] as? String == "permission.replied" }
        XCTAssertEqual(replies.count, 1)
        XCTAssertEqual((replies.first?["properties"] as? [String: Any])?["requestID"] as? String, approvalID)
        XCTAssertEqual((replies.first?["properties"] as? [String: Any])?["reply"] as? String, "once")
        XCTAssertFalse(h.events.values.contains { $0["type"] as? String == "session.error" })

        let rows = try await messages(h.backend, h.sessionID)
        XCTAssertEqual(rows.count, 4)
        guard rows.count == 4 else { throw URLError(.badServerResponse) }
        try assertChronology(rows[1], events: h.events.values, tool: "write", status: "completed")
        try assertChronology(rows[2], events: h.events.values, tool: "read", status: "completed")
        let finalParts = try XCTUnwrap(rows[3]["parts"] as? [[String: Any]])
        XCTAssertEqual(finalParts.compactMap { $0["type"] as? String }, ["text"])
        XCTAssertEqual(finalParts.first?["text"] as? String, finalText)
        let userID = try XCTUnwrap((rows[0]["info"] as? [String: Any])?["id"] as? String)
        for (step, row) in rows.dropFirst().enumerated() {
            let info = try XCTUnwrap(row["info"] as? [String: Any])
            XCTAssertNil(info["error"])
            XCTAssertEqual(info["parentID"] as? String, userID)
            XCTAssertEqual(info["finish"] as? String, step == 2 ? "stop" : "tool-calls")
            XCTAssertNotNil((info["time"] as? [String: Any])?["completed"])
            let tokens = try XCTUnwrap(info["tokens"] as? [String: Any])
            XCTAssertEqual(tokens["input"] as? Int, (step + 1) * 101)
            XCTAssertEqual(tokens["output"] as? Int, (step + 1) * 11)
        }
        let toolParts = rows.flatMap { $0["parts"] as? [[String: Any]] ?? [] }.filter { $0["type"] as? String == "tool" }
        let writeState = try XCTUnwrap(toolParts.first?["state"] as? [String: Any])
        let readState = try XCTUnwrap(toolParts.last?["state"] as? [String: Any])
        let writeOutput = try XCTUnwrap(writeState["output"] as? String)
        let readOutput = try XCTUnwrap(readState["output"] as? String)
        XCTAssertTrue(writeOutput.contains("Saved " + target.path))
        XCTAssertEqual(readOutput, "1: " + fileContent)
        XCTAssertEqual((writeState["metadata"] as? [String: Any])?["saved"] as? Bool, true)

        let bodies = try requests.map(body)
        for (step, outgoing) in requests.enumerated() {
            XCTAssertEqual(outgoing.url?.host, h.host)
            XCTAssertEqual(outgoing.url?.path, format == .responses ? "/v1/responses" : "/v1/chat/completions")
            XCTAssertEqual(outgoing.httpMethod, "POST")
            XCTAssertTrue(outgoing.value(forHTTPHeaderField: "Authorization") == catalog.value(forHTTPHeaderField: "Authorization"))
            XCTAssertEqual(outgoing.value(forHTTPHeaderField: "Accept"), "text/event-stream")
            XCTAssertEqual(outgoing.value(forHTTPHeaderField: "Content-Type"), "application/json")
            XCTAssertEqual(bodies[step]["model"] as? String, "file-agent")
            XCTAssertEqual(bodies[step]["stream"] as? Bool, true)
            XCTAssertEqual(bodies[step][format == .responses ? "max_output_tokens" : "max_completion_tokens"] as? Int, 1000)
            let tools = try XCTUnwrap(bodies[step]["tools"] as? [[String: Any]])
            let names = tools.compactMap { format == .responses ? $0["name"] as? String
                : ($0["function"] as? [String: Any])?["name"] as? String }
            XCTAssertTrue(names.contains("write") && names.contains("read"))
        }
        for step in 0..<3 {
            let input = try XCTUnwrap(bodies[step][format == .responses ? "input" : "messages"] as? [[String: Any]])
            let outputs = input.filter { format == .responses ? $0["type"] as? String == "function_call_output" : $0["role"] as? String == "tool" }
            XCTAssertEqual(outputs.count, step)
            guard outputs.count == step else { throw URLError(.badServerResponse) }
            if step > 0 {
                XCTAssertEqual(outputs[0][format == .responses ? "call_id" : "tool_call_id"] as? String, "call-write")
                XCTAssertEqual(outputs[0][format == .responses ? "output" : "content"] as? String, writeOutput)
            }
            if step > 1 {
                XCTAssertEqual(outputs[1][format == .responses ? "call_id" : "tool_call_id"] as? String, "call-read")
                XCTAssertEqual(outputs[1][format == .responses ? "output" : "content"] as? String, readOutput)
            }
            if format == .responses {
                XCTAssertEqual(bodies[step]["store"] as? Bool, false)
                XCTAssertEqual(bodies[step]["include"] as? [String], ["reasoning.encrypted_content"])
                for prior in 0..<step {
                    for item in turns[prior].output {
                        let id = try XCTUnwrap(item["id"] as? String)
                        let replayed = input.filter { $0["id"] as? String == id }
                        XCTAssertEqual(replayed.count, 1, "Replay must not duplicate assistant calls/text")
                        XCTAssertEqual(try json(try XCTUnwrap(replayed.first)), try json(item))
                    }
                }
            } else {
                XCTAssertNil(bodies[step]["store"])
                XCTAssertEqual((bodies[step]["stream_options"] as? [String: Any])?["include_usage"] as? Bool, true)
                XCTAssertEqual(input.first?["role"] as? String, "system")
                let calls = input.flatMap { $0["tool_calls"] as? [[String: Any]] ?? [] }
                XCTAssertEqual(calls.count, step)
                guard calls.count == step else { throw URLError(.badServerResponse) }
                for prior in 0..<step {
                    let tool = prior == 0 ? "write" : "read"
                    XCTAssertEqual(calls[prior]["id"] as? String, "call-\(tool)")
                    XCTAssertEqual(calls[prior]["type"] as? String, "function")
                    let function = try XCTUnwrap(calls[prior]["function"] as? [String: Any])
                    XCTAssertEqual(function["name"] as? String, tool)
                    let arguments = try XCTUnwrap(function["arguments"] as? String)
                    let expected: [String: Any] = prior == 0
                        ? ["filePath": fileName, "content": fileContent] : ["filePath": fileName]
                    XCTAssertEqual(try json(JSONSerialization.jsonObject(with: Data(arguments.utf8))), try json(expected))
                }
            }
        }

        let ledger = try await history(h.backend, h)
        let historyTurns = try XCTUnwrap(ledger["turns"] as? [[String: Any]])
        XCTAssertEqual(historyTurns.count, 1)
        XCTAssertEqual(historyTurns.first?["id"] as? String, userID)
        let commit = try XCTUnwrap(historyTurns.first?["commit"] as? String)
        let shown = try await h.backend.historyAction("historyShow", data: [
            "directory": h.project.path, "sessionID": h.sessionID, "commit": commit
        ])
        let changed = try XCTUnwrap(shown["files"] as? [[String: Any]])
        XCTAssertEqual(changed.count, 1)
        XCTAssertEqual(changed.first?["file"] as? String, target.path)
        XCTAssertEqual(changed.first?["status"] as? String, "added")
        XCTAssertTrue((changed.first?["patch"] as? String ?? "").contains("+Written through the native agent 🌍"))
        let patchURL = h.root.appendingPathComponent("patches.json")
        let patchData = try Data(contentsOf: patchURL)
        let patchStore = try XCTUnwrap(JSONSerialization.jsonObject(with: patchData) as? [String: Any])
        let records = try XCTUnwrap(patchStore["records"] as? [[String: Any]])
        XCTAssertEqual(records.count, 1, "History must come from one actual disk mutation")
        XCTAssertEqual(records.first?["after"] as? String, fileContent)
        XCTAssertEqual(records.first?["sessionID"] as? String, h.sessionID)
        XCTAssertEqual(records.first?["turnID"] as? String, userID)
        try assertNoCredentialLeak(h, rows: rows)
        XCTAssertFalse(String(decoding: patchData, as: UTF8.self).contains(h.key))

        await h.backend.shutdown()
        let snapshotURL = h.root.appendingPathComponent("native-agent/state.json")
        let snapshot = try Data(contentsOf: snapshotURL)
        let attributes = try FileManager.default.attributesOfItem(atPath: target.path)
        let reopened = try backend(root: h.root, client: h.client, credentials: h.credentials)
        let restored = try await messages(reopened, h.sessionID)
        XCTAssertEqual(try json(restored), try json(rows))
        let restoredHistory = try await history(reopened, h)
        XCTAssertEqual(try json(restoredHistory), try json(ledger))
        let restoredCatalog = try await reopened.modelCatalogRequest(providerID: "integration")
        XCTAssertEqual(restoredCatalog.url, catalog.url)
        XCTAssertTrue(restoredCatalog.value(forHTTPHeaderField: "Authorization") == catalog.value(forHTTPHeaderField: "Authorization"))
        let reopenedAttributes = try FileManager.default.attributesOfItem(atPath: target.path)
        XCTAssertEqual(reopenedAttributes[.modificationDate] as? Date, attributes[.modificationDate] as? Date)
        XCTAssertEqual(reopenedAttributes[.systemFileNumber] as? NSNumber, attributes[.systemFileNumber] as? NSNumber)
        XCTAssertEqual(try Data(contentsOf: target), Data(fileContent.utf8))
        XCTAssertEqual(try Data(contentsOf: snapshotURL), snapshot)
        XCTAssertEqual(try Data(contentsOf: patchURL), patchData)
        XCTAssertEqual(h.provider.requests.count, 3, "Reopen must not contact the provider or re-execute tools")
        await reopened.shutdown()
    }

    func testResponsesHTTPFileAgentPermissionReplayAndPersistence() async throws {
        try await fileAgentFlow(.responses)
    }

    func testChatCompletionsHTTPFileAgentPermissionReplayAndPersistence() async throws {
        try await fileAgentFlow(.chatCompletions)
    }

    private func assertFailure(_ h: Harness, expected: String) async throws {
        let rows = try await messages(h.backend, h.sessionID)
        let info = try XCTUnwrap(rows.last?["info"] as? [String: Any])
        let error = try XCTUnwrap(info["error"] as? [String: Any])
        XCTAssertEqual((error["data"] as? [String: Any])?["message"] as? String, expected)
        XCTAssertNotNil((info["time"] as? [String: Any])?["completed"])
        let errors = h.events.values.filter { $0["type"] as? String == "session.error" }
        XCTAssertEqual(errors.count, 1)
        let eventError = try XCTUnwrap((errors.first?["properties"] as? [String: Any])?["error"] as? [String: Any])
        XCTAssertEqual((eventError["data"] as? [String: Any])?["message"] as? String, expected)
        XCTAssertEqual(h.provider.requests.count, 1, "Failed/cancelled billable POSTs must not be retried")
        XCTAssertFalse(FileManager.default.fileExists(atPath: h.project.appendingPathComponent(fileName).path))
        let pending = try await permissions(h)
        XCTAssertTrue(pending.isEmpty)
        XCTAssertFalse(h.events.values.contains { $0["type"] as? String == "permission.asked" })
        let ledger = try await history(h.backend, h)
        XCTAssertEqual((ledger["turns"] as? [[String: Any]])?.count, 0)
        XCTAssertFalse(FileManager.default.fileExists(atPath: h.root.appendingPathComponent("patches.json").path))
        try assertNoCredentialLeak(h, rows: rows)
        await h.backend.shutdown()
        let reopened = try backend(root: h.root, client: h.client, credentials: h.credentials)
        let restored = try await messages(reopened, h.sessionID)
        XCTAssertEqual(try json(restored), try json(rows))
        XCTAssertEqual(h.provider.requests.count, 1, "Restoring an error must not replay it")
        await reopened.shutdown()
    }

    func testHTTP401UnauthorizedClientDiagnosisReachesBackendWithoutEchoingBody() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let discarded = UUID().uuidString
            let errorBody = try json(["error": ["type": "unauthorized_client_error", "message": discarded],
                                      "url": "https://\(UUID().uuidString.lowercased()).example.test/private"])
            let h = try await harness(format, replies: [.init(status: 401, body: errorBody, contentType: "application/json")])
            try await prompt(h)
            try await waitIdle(h)
            let rows = try await messages(h.backend, h.sessionID)
            XCTAssertFalse(String(decoding: try json(rows), as: UTF8.self).contains(discarded))
            try assertFailureBodyIsEmpty(rows)
            try await assertFailure(h, expected: "Model provider returned HTTP 401; provider rejected this client; contact provider support.")
        }
    }

    private func assertFailureBodyIsEmpty(_ rows: [[String: Any]]) throws {
        let parts = try XCTUnwrap(rows.last?["parts"] as? [[String: Any]])
        XCTAssertTrue(parts.isEmpty, "Rejected HTTP responses must not emit model events")
    }

    func testTruncatedHTTPStreamWithCompletedArgumentsNeverExecutesToolPreview() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let wire = try turn(format, step: 0, tool: "write")
            let h = try await harness(format, replies: [.init(body: wire.prefix)], allowWrite: true)
            try await prompt(h)
            try await waitIdle(h)
            let rows = try await messages(h.backend, h.sessionID)
            try assertChronology(try XCTUnwrap(rows.last), events: h.events.values, tool: "write", status: "error")
            try await assertFailure(h, expected: "Model event stream ended without its successful terminal event.")
        }
    }

    func testAbortHeldHTTPStreamStopsURLSessionAndNeverExecutesToolPreview() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let wire = try turn(format, step: 0, tool: "write")
            let h = try await harness(format, replies: [.init(body: wire.prefix, finish: false)], allowWrite: true)
            try await prompt(h)
            try await waitUntil("streaming text after toolStarted") {
                let rows = try await self.messages(h.backend, h.sessionID)
                return (rows.last?["parts"] as? [[String: Any]] ?? []).contains { $0["text"] as? String == self.afterText }
            }
            let streaming = try await messages(h.backend, h.sessionID)
            try assertChronology(try XCTUnwrap(streaming.last), events: h.events.values, tool: "write", status: "pending")
            XCTAssertEqual(h.provider.stoppedRequests, 0, "Fixture must remain open until abort")
            XCTAssertFalse(FileManager.default.fileExists(atPath: h.project.appendingPathComponent(fileName).path))
            _ = try await request(h.backend, "POST", "/session/\(h.sessionID)/abort")
            try await waitUntil("URLProtocol.stopLoading after abort") { h.provider.stoppedRequests == 1 }
            try await waitIdle(h)
            let stopped = try await messages(h.backend, h.sessionID)
            try assertChronology(try XCTUnwrap(stopped.last), events: h.events.values, tool: "write", status: "error")
            try await assertFailure(h, expected: "Stopped by the user.")
        }
    }
}

private final class IntegrationCredentials {
    private let lock = NSLock()
    private var values: [String: String] = [:]

    func read(_ id: String) throws -> String? {
        lock.lock(); defer { lock.unlock() }
        return values[id]
    }

    func write(_ id: String, _ key: String?) throws {
        lock.lock(); defer { lock.unlock() }
        values[id] = key
    }
}

private final class IntegrationEvents {
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

private final class IntegrationHTTPProvider {
    struct Reply {
        let status: Int
        let body: Data
        let contentType: String
        let finish: Bool

        init(status: Int = 200, body: Data, contentType: String = "text/event-stream", finish: Bool = true) {
            self.status = status
            self.body = body
            self.contentType = contentType
            self.finish = finish
        }
    }

    private let lock = NSLock()
    private var replies: [Reply]
    private var recorded: [URLRequest] = []
    private var stopped = Set<UUID>()

    init(replies: [Reply]) { self.replies = replies }

    var requests: [URLRequest] {
        lock.lock(); defer { lock.unlock() }
        return recorded
    }

    var remainingReplies: Int {
        lock.lock(); defer { lock.unlock() }
        return replies.count
    }

    var stoppedRequests: Int {
        lock.lock(); defer { lock.unlock() }
        return stopped.count
    }

    func next(_ request: URLRequest) throws -> Reply? {
        // URLSession may replace httpBody with a one-shot stream. Snapshot it while
        // the protocol owns the upload, not after session invalidation in an assertion.
        var captured = request
        if captured.httpBody == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while true {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count < 0 { throw stream.streamError ?? URLError(.cannotDecodeContentData) }
                if count == 0 { break }
                data.append(contentsOf: buffer.prefix(count))
            }
            captured.httpBody = data
        }
        lock.lock(); defer { lock.unlock() }
        recorded.append(captured)
        return replies.isEmpty ? nil : replies.removeFirst()
    }

    func stop(_ id: UUID) {
        lock.lock(); defer { lock.unlock() }
        stopped.insert(id)
    }
}

private final class IntegrationProviderURLProtocol: URLProtocol {
    private static let registryLock = NSLock()
    private static var providers: [String: IntegrationHTTPProvider] = [:]
    private let stateLock = NSLock()
    private let id = UUID()
    private var provider: IntegrationHTTPProvider?
    private var stopped = false

    static func register(_ provider: IntegrationHTTPProvider, host: String) {
        registryLock.lock(); defer { registryLock.unlock() }
        providers[host] = provider
    }

    static func unregister(host: String) {
        registryLock.lock(); defer { registryLock.unlock() }
        providers.removeValue(forKey: host)
    }

    private static func provider(for host: String) -> IntegrationHTTPProvider? {
        registryLock.lock(); defer { registryLock.unlock() }
        return providers[host]
    }

    // Claim every request in the injected session, including unknown hosts, to fail closed:
    // a missing script or unexpected destination must never fall through to real networking.
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let provider = Self.provider(for: request.url?.host ?? "") else {
            client?.urlProtocol(self, didFailWithError: URLError(.resourceUnavailable))
            return
        }
        stateLock.lock()
        self.provider = provider
        let alreadyStopped = stopped
        stateLock.unlock()
        if alreadyStopped { provider.stop(id); return }
        guard let reply = try? provider.next(request), let url = request.url,
              let response = HTTPURLResponse(url: url, statusCode: reply.status, httpVersion: "HTTP/1.1",
                                             headerFields: ["Content-Type": reply.contentType]) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        // Fragment SSE/UTF-8 across callbacks instead of bypassing the real byte parser.
        for offset in stride(from: 0, to: reply.body.count, by: 17) {
            stateLock.lock()
            let isStopped = stopped
            stateLock.unlock()
            if isStopped { return }
            client?.urlProtocol(self, didLoad: reply.body.subdata(in: offset..<min(offset + 17, reply.body.count)))
        }
        if reply.finish { client?.urlProtocolDidFinishLoading(self) }
    }

    override func stopLoading() {
        stateLock.lock()
        stopped = true
        let provider = self.provider
        stateLock.unlock()
        provider?.stop(id)
    }
}
