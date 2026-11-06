import Foundation
import XCTest
@testable import KumoCore

final class NativeModelClientTests: XCTestCase {
    private let neutralTool: [String: Any] = [
        "name": "read_file", "description": "Read a UTF-8 file",
        "parameters": ["type": "object", "properties": ["path": ["type": "string"]], "required": ["path"]]
    ]

    private func request(_ format: NativeProviderFormat = .responses) -> NativeModelRequest {
        NativeModelRequest(baseURL: URL(string: "https://provider.example/v1/")!,
                           apiKey: "secret-test-key", format: format, model: "test-model",
                           system: "Be helpful", messages: [NativeModelMessage(role: "user", content: "Hello 🌍")],
                           tools: [neutralTool], effort: "medium")
    }

    private func body(_ request: URLRequest) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: Any])
    }

    private func frame(_ object: [String: Any], event: String? = nil) throws -> NativeSSEFrame {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        return NativeSSEFrame(event: event, data: String(decoding: data, as: UTF8.self))
    }

    private func eventLabels(_ events: [NativeModelEvent]) -> [String] {
        events.map {
            switch $0 {
            case .text(let text): return "text:\(text)"
            case .reasoning(let text): return "reasoning:\(text)"
            case .toolStarted(let id, let name): return "started:\(id):\(name)"
            case .toolCall(let call): return "tool:\(call.id):\(call.name):\(call.arguments)"
            case .usage(let input, let output): return "usage:\(input):\(output)"
            case .replay(let items): return "replay:\(items.count)"
            case .finished: return "finished"
            }
        }
    }

    private func responseCompleted(output: [[String: Any]] = []) throws -> NativeSSEFrame {
        try frame(["type": "response.completed", "response": [
            "status": "completed", "output": output, "usage": ["input_tokens": 11, "output_tokens": 7]
        ]])
    }

    private func chatChunk(_ delta: [String: Any], finish: Any = NSNull()) throws -> NativeSSEFrame {
        try frame(["choices": [["index": 0, "delta": delta, "finish_reason": finish]]])
    }

    private func parse(_ text: String, fragmentSize: Int = 1) throws -> [NativeSSEFrame] {
        let bytes = Array(text.utf8)
        var parser = NativeSSEParser()
        var frames: [NativeSSEFrame] = []
        for start in stride(from: 0, to: bytes.count, by: fragmentSize) {
            for byte in bytes[start..<min(start + fragmentSize, bytes.count)] {
                if let frame = try parser.feed(byte) { frames.append(frame) }
            }
        }
        try parser.finish()
        return frames
    }

    func testPublicInterfaceAndFormatRawValues() throws {
        XCTAssertEqual(NativeProviderFormat.responses.rawValue, "responses")
        XCTAssertEqual(NativeProviderFormat.chatCompletions.rawValue, "chatCompletions")
        XCTAssertEqual(try JSONDecoder().decode(NativeProviderFormat.self, from: Data("\"responses\"".utf8)), .responses)
        var message = NativeModelMessage(role: "user", content: "x")
        XCTAssertTrue(message.toolCalls.isEmpty)
        XCTAssertTrue(message.replayItems.isEmpty)
        XCTAssertNil(message.toolCallID)
        message.role = "assistant"
        message.content = "y"
        message.toolCalls = [NativeToolCall(id: "c", name: "read_file", arguments: "{}")]
        message.toolCallID = "c"
        message.replayItems = [["type": "reasoning", "encrypted_content": "cipher"]]
        var modelRequest = request()
        modelRequest.messages = [message]
        modelRequest.effort = nil
        XCTAssertEqual(modelRequest.messages[0].toolCalls[0].id, "c")
    }

    func testOutputTokenCapUsesProviderFieldAndNilOmitsIt() throws {
        let capped = NativeModelRequest(
            baseURL: URL(string: "https://provider.example/v1/")!, apiKey: "secret-test-key",
            format: .responses, model: "test-model", system: "", messages: [], tools: [],
            maxOutputTokens: 2048
        )
        XCTAssertEqual(capped.maxOutputTokens, 2048)
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            var modelRequest = request(format)
            XCTAssertNil(modelRequest.maxOutputTokens)
            let uncapped = try body(NativeModelRequestBuilder.makeURLRequest(modelRequest))
            XCTAssertNil(uncapped["max_output_tokens"])
            XCTAssertNil(uncapped["max_completion_tokens"])
            XCTAssertNil(uncapped["max_tokens"])
            modelRequest.maxOutputTokens = capped.maxOutputTokens
            let json = try body(NativeModelRequestBuilder.makeURLRequest(modelRequest))
            let field = format == .responses ? "max_output_tokens" : "max_completion_tokens"
            let other = format == .responses ? "max_completion_tokens" : "max_output_tokens"
            XCTAssertEqual(json[field] as? Int, 2048)
            XCTAssertNil(json[other])
            XCTAssertNil(json["max_tokens"])
            for invalid in [0, -1] {
                modelRequest.maxOutputTokens = invalid
                XCTAssertThrowsError(try NativeModelRequestBuilder.makeURLRequest(modelRequest)) { error in
                    guard let safe = error as? NativeModelClientError,
                          case .invalidRequest = safe else { return XCTFail("Unexpected error: \(error)") }
                }
            }
        }
    }

    func testResponsesRequestUsesFlatToolsAndOnlyStandardHeaders() throws {
        let built = try NativeModelRequestBuilder.makeURLRequest(request())
        XCTAssertEqual(built.url?.absoluteString, "https://provider.example/v1/responses")
        XCTAssertEqual(built.httpMethod, "POST")
        XCTAssertEqual(built.value(forHTTPHeaderField: "Authorization"), "Bearer secret-test-key")
        XCTAssertEqual(built.value(forHTTPHeaderField: "Accept"), "text/event-stream")
        XCTAssertEqual(Set((built.allHTTPHeaderFields ?? [:]).keys.map { $0.lowercased() }),
                       Set(["authorization", "accept", "content-type"]))
        let json = try body(built)
        XCTAssertEqual(json["stream"] as? Bool, true)
        XCTAssertEqual(json["store"] as? Bool, false)
        XCTAssertEqual(json["instructions"] as? String, "Be helpful")
        XCTAssertEqual(json["include"] as? [String], ["reasoning.encrypted_content"])
        let reasoning = try XCTUnwrap(json["reasoning"] as? [String: Any])
        XCTAssertEqual(reasoning["effort"] as? String, "medium")
        XCTAssertEqual(reasoning["summary"] as? String, "auto")
        let tools = try XCTUnwrap(json["tools"] as? [[String: Any]])
        XCTAssertEqual(tools[0]["type"] as? String, "function")
        XCTAssertEqual(tools[0]["name"] as? String, "read_file")
        XCTAssertNil(tools[0]["function"])
        XCTAssertNotNil(tools[0]["parameters"])
        let input = try XCTUnwrap(json["input"] as? [[String: Any]])
        XCTAssertEqual(input[0]["content"] as? String, "Hello 🌍")
    }

    func testResponsesOptionalToolParametersRemainNonStrict() throws {
        var modelRequest = request()
        modelRequest.tools = [[
            "name": "read_file", "parameters": [
                "type": "object", "properties": [
                    "path": ["type": "string"], "limit": ["type": "integer"]
                ], "required": ["path"]
            ]
        ]]
        let tools = try XCTUnwrap(body(NativeModelRequestBuilder.makeURLRequest(modelRequest))["tools"] as? [[String: Any]])
        XCTAssertEqual(tools[0]["strict"] as? Bool, false)
        let parameters = try XCTUnwrap(tools[0]["parameters"] as? [String: Any])
        XCTAssertEqual(parameters["required"] as? [String], ["path"])
        XCTAssertNotNil((parameters["properties"] as? [String: Any])?["limit"])
    }

    func testChatRequestUsesNestedToolsMessagesAndUsageOptions() throws {
        var modelRequest = request(.chatCompletions)
        modelRequest.messages = [
            NativeModelMessage(role: "assistant", content: "", toolCalls: [
                NativeToolCall(id: "call_1", name: "read_file", arguments: "{\"path\":\"a\"}")
            ]),
            NativeModelMessage(role: "tool", content: "file contents", toolCallID: "call_1")
        ]
        let built = try NativeModelRequestBuilder.makeURLRequest(modelRequest)
        XCTAssertEqual(built.url?.path, "/v1/chat/completions")
        let json = try body(built)
        XCTAssertNil(json["store"])
        XCTAssertNil(json["input"])
        XCTAssertEqual(json["reasoning_effort"] as? String, "medium")
        XCTAssertEqual((json["stream_options"] as? [String: Bool])?["include_usage"], true)
        let tools = try XCTUnwrap(json["tools"] as? [[String: Any]])
        XCTAssertNil(tools[0]["name"])
        XCTAssertEqual((tools[0]["function"] as? [String: Any])?["name"] as? String, "read_file")
        let messages = try XCTUnwrap(json["messages"] as? [[String: Any]])
        XCTAssertEqual(messages.count, 3)
        XCTAssertEqual(messages[0]["role"] as? String, "system")
        XCTAssertTrue(messages[1]["content"] is NSNull)
        let calls = try XCTUnwrap(messages[1]["tool_calls"] as? [[String: Any]])
        XCTAssertEqual(calls[0]["id"] as? String, "call_1")
        XCTAssertEqual(calls[0]["type"] as? String, "function")
        let function = try XCTUnwrap(calls[0]["function"] as? [String: Any])
        XCTAssertEqual(function["name"] as? String, "read_file")
        XCTAssertEqual(function["arguments"] as? String, "{\"path\":\"a\"}")
        XCTAssertEqual(messages[2]["tool_call_id"] as? String, "call_1")
        XCTAssertEqual(messages[2]["content"] as? String, "file contents")
    }

    func testResponsesReplayFlattensReasoningMessagesAndCallsWithoutDuplicates() throws {
        let replay: [[String: Any]] = [
            ["type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-ciphertext"],
            ["type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
             "content": [["type": "output_text", "text": "Reading it", "annotations": []]]],
            ["type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read_file", "arguments": "{}"]
        ]
        var modelRequest = request()
        modelRequest.messages = [
            NativeModelMessage(role: "assistant", content: "Reading it", toolCalls: [
                NativeToolCall(id: "call_1", name: "read_file", arguments: "{}")
            ], replayItems: replay),
            NativeModelMessage(role: "tool", content: "result", toolCallID: "call_1"),
            NativeModelMessage(role: "user", content: "Continue")
        ]
        let input = try XCTUnwrap(body(NativeModelRequestBuilder.makeURLRequest(modelRequest))["input"] as? [[String: Any]])
        XCTAssertEqual(input.count, 5)
        XCTAssertEqual(input[0]["encrypted_content"] as? String, "opaque-ciphertext")
        XCTAssertEqual(input[1]["id"] as? String, "msg_1")
        XCTAssertEqual(input[2]["call_id"] as? String, "call_1")
        XCTAssertEqual(input[3]["type"] as? String, "function_call_output")
        XCTAssertEqual(input[3]["output"] as? String, "result")
        XCTAssertEqual(input[4]["role"] as? String, "user")
    }

    func testResponsesWithoutReplayFlattensCallsAndOmitsOptionalFields() throws {
        var modelRequest = request()
        modelRequest.system = ""
        modelRequest.tools = []
        modelRequest.effort = nil
        modelRequest.messages = [NativeModelMessage(role: "assistant", content: "", toolCalls: [
            NativeToolCall(id: "c", name: "read_file", arguments: "{}")
        ])]
        let json = try body(NativeModelRequestBuilder.makeURLRequest(modelRequest))
        XCTAssertNil(json["instructions"])
        XCTAssertNil(json["tools"])
        XCTAssertNil(json["reasoning"])
        let input = try XCTUnwrap(json["input"] as? [[String: Any]])
        XCTAssertEqual(input.count, 1)
        XCTAssertEqual(input[0]["type"] as? String, "function_call")
        XCTAssertEqual(input[0]["call_id"] as? String, "c")
        XCTAssertNil(input[0]["function"])
    }

    func testEndpointNormalizationAndRemoteOnlyValidation() throws {
        for suffix in ["responses", "chat/completions", "responses/"] {
            var modelRequest = request(.chatCompletions)
            modelRequest.baseURL = URL(string: "https://provider.example/v1/" + suffix)!
            XCTAssertEqual(try NativeModelRequestBuilder.makeURLRequest(modelRequest).url?.path, "/v1/chat/completions")
        }
        for endpoint in ["http://provider.example/v1", "http://localhost:8080", "http://127.0.0.1:9000/v1", "http://[::1]/v1",
                         "https://localhost:8080", "https://127.0.0.1/v1", "https://[::1]/v1", "https://api.localhost/v1",
                         "file:///tmp/model", "https://user:password@provider.example", "https://provider.example?key=secret-test-key"] {
            var modelRequest = request()
            modelRequest.baseURL = URL(string: endpoint)!
            XCTAssertThrowsError(try NativeModelRequestBuilder.makeURLRequest(modelRequest)) { error in
                XCTAssertFalse(error.localizedDescription.contains("secret-test-key"))
                XCTAssertFalse(error.localizedDescription.contains(endpoint))
            }
        }
    }

    func testInvalidJSONToolsAndMissingToolOutputIDAreRejected() {
        var modelRequest = request()
        modelRequest.tools = [["name": "oops"]]
        XCTAssertThrowsError(try NativeModelRequestBuilder.makeURLRequest(modelRequest))
        modelRequest.tools = [neutralTool]
        modelRequest.messages = [NativeModelMessage(role: "tool", content: "result")]
        XCTAssertThrowsError(try NativeModelRequestBuilder.makeURLRequest(modelRequest))
        modelRequest.messages = [NativeModelMessage(role: "assistant", content: "", replayItems: [
            ["type": "reasoning", "summary": Date()]
        ])]
        XCTAssertThrowsError(try NativeModelRequestBuilder.makeURLRequest(modelRequest))
        modelRequest = request()
        modelRequest.apiKey = "key\r\nX-Injected: yes"
        XCTAssertThrowsError(try NativeModelRequestBuilder.makeURLRequest(modelRequest))
    }

    func testAPIKeysWithAnyLineBreakAreRejectedBeforeHeaderConstruction() {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            for key in ["", "key\rX-Injected: yes", "key\nX-Injected: yes", "key\r\nX-Injected: yes",
                        "key\r", "key\n", "key\r\n"] {
                var modelRequest = request(format)
                modelRequest.apiKey = key
                XCTAssertThrowsError(try NativeModelRequestBuilder.makeURLRequest(modelRequest)) { error in
                    guard let safe = error as? NativeModelClientError,
                          case .invalidRequest = safe else { return XCTFail("Unexpected error: \(error)") }
                }
            }
        }
    }

    func testInvalidToolParametersAndNonFiniteSchemaValuesAreRejected() {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let invalidTools: [[String: Any]] = [
                ["name": "read_file", "parameters": "not an object"],
                ["name": "read_file", "parameters": [
                    "type": "object", "properties": [
                        "limit": ["type": "number", "default": Double.nan]
                    ]
                ]]
            ]
            for tool in invalidTools {
                var modelRequest = request(format)
                modelRequest.tools = [tool]
                XCTAssertThrowsError(try NativeModelRequestBuilder.makeURLRequest(modelRequest)) { error in
                    guard let safe = error as? NativeModelClientError,
                          case .invalidRequest = safe else { return XCTFail("Unexpected error: \(error)") }
                }
            }
        }
    }

    func testSSEFragmentationMultilineCRLFUnicodeAndComments() throws {
        let wire = ": heartbeat\r\nevent: response.output_text.delta\r\n"
            + "id: ignored\r\nretry: 1000\r\ndata: {\"type\":\"response.output_text.delta\",\r\n"
            + "data: \"delta\":\"Привет 🌍 café\"}\r\n\r\n"
            + "data: [DONE]\n\n"
        for size in [1, 2, 3, 7, 1024] {
            let frames = try parse(wire, fragmentSize: size)
            XCTAssertEqual(frames.count, 2)
            XCTAssertEqual(frames[0].event, "response.output_text.delta")
            XCTAssertTrue(frames[0].data.contains("\n"))
            var decoder = NativeModelStreamDecoder(format: .responses)
            XCTAssertEqual(eventLabels(try decoder.consume(frames[0])), ["text:Привет 🌍 café"])
            XCTAssertEqual(frames[1].data, "[DONE]")
        }
    }

    func testSSEBareCRAndEmptyDataDispatch() throws {
        let frames = try parse("event: ping\rdata:\r\rdata: a\rdata: b\r\r")
        XCTAssertEqual(frames.count, 2)
        XCTAssertEqual(frames[0].event, "ping")
        XCTAssertEqual(frames[0].data, "")
        XCTAssertEqual(frames[1].data, "a\nb")
    }

    func testSSEUTF8BOMIsIgnoredOnlyAtStart() throws {
        let frames = try parse("\u{FEFF}data: hi\n\ndata: \u{FEFF}kept\n\n")
        XCTAssertEqual(frames.map { $0.data }, ["hi", "\u{FEFF}kept"])
    }

    func testSSERejectsUnterminatedFrameInvalidUTF8AndOversizedFrame() throws {
        var partial = NativeSSEParser()
        for byte in "data: [DONE]\n".utf8 { _ = try partial.feed(byte) }
        XCTAssertThrowsError(try partial.finish())
        var invalid = NativeSSEParser()
        _ = try invalid.feed(0xFF)
        XCTAssertThrowsError(try invalid.feed(10))
        var bounded = NativeSSEParser(maximumFrameBytes: 16)
        XCTAssertThrowsError(try Array(repeating: UInt8(97), count: 17).forEach { _ = try bounded.feed($0) })
    }

    func testResponsesTextReasoningArgumentDeltasAndFinalReplay() throws {
        var decoder = NativeModelStreamDecoder(format: .responses)
        XCTAssertEqual(eventLabels(try decoder.consume(frame([
            "type": "response.reasoning_summary_text.delta", "delta": "Need a file"
        ]))), ["reasoning:Need a file"])
        XCTAssertEqual(eventLabels(try decoder.consume(frame([
            "type": "response.output_text.delta", "delta": "Reading 🌍"
        ]))), ["text:Reading 🌍"])
        let callItem: [String: Any] = ["type": "function_call", "id": "fc_1", "call_id": "call_1",
                                       "name": "read_file", "arguments": ""]
        XCTAssertEqual(eventLabels(try decoder.consume(frame([
            "type": "response.output_item.added", "output_index": 1, "item": callItem
        ]))), ["started:call_1:read_file"])
        for delta in ["{\"path\":", "\"a.swift\"}"] {
            XCTAssertTrue(try decoder.consume(frame([
                "type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "fc_1", "delta": delta
            ])).isEmpty)
        }
        var completedCall = callItem
        completedCall["arguments"] = "{\"path\":\"a.swift\"}"
        let output: [[String: Any]] = [
            ["type": "reasoning", "id": "rs_1", "encrypted_content": "cipher", "summary": []], completedCall,
            ["type": "message", "role": "assistant", "content": [["type": "output_text", "text": "Reading 🌍"]]]
        ]
        let events = try decoder.consume(responseCompleted(output: output))
        XCTAssertEqual(eventLabels(events), ["tool:call_1:read_file:{\"path\":\"a.swift\"}", "usage:11:7", "replay:3", "finished"])
        guard case .replay(let replay) = events[2] else { return XCTFail("Missing replay") }
        XCTAssertEqual(replay[0]["encrypted_content"] as? String, "cipher")
        XCTAssertEqual(replay[1]["id"] as? String, "fc_1")
        XCTAssertEqual(replay[2]["type"] as? String, "message")
        try decoder.finish()
        XCTAssertTrue(decoder.isFinished)
    }

    func testResponsesArgumentsDoneDoesNotAppendOrDuplicateTools() throws {
        var decoder = NativeModelStreamDecoder(format: .responses)
        let item: [String: Any] = ["type": "function_call", "id": "fc", "call_id": "c", "name": "read_file", "arguments": "{}"]
        _ = try decoder.consume(frame(["type": "response.output_item.added", "output_index": 0,
                                       "item": ["type": "function_call", "id": "fc", "call_id": "c", "name": "read_file", "arguments": ""]]))
        _ = try decoder.consume(frame(["type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc", "delta": "{}"] ))
        _ = try decoder.consume(frame(["type": "response.function_call_arguments.done", "output_index": 0, "item_id": "fc", "arguments": "{}"] ))
        XCTAssertTrue(try decoder.consume(frame(["type": "response.output_item.done", "output_index": 0, "item": item])).isEmpty)
        XCTAssertEqual(eventLabels(try decoder.consume(responseCompleted(output: [item]))).first, "tool:c:read_file:{}")
    }

    func testResponsesCompletedArgumentsCannotChangeWithoutDeltas() throws {
        for completion in ["response.function_call_arguments.done", "response.output_item.done"] {
            var decoder = NativeModelStreamDecoder(format: .responses)
            let call: [String: Any] = [
                "type": "function_call", "id": "fc", "call_id": "c", "name": "read_file", "arguments": "{}"
            ]
            _ = try decoder.consume(frame([
                "type": "response.output_item.added", "output_index": 0,
                "item": ["type": "function_call", "id": "fc", "call_id": "c", "name": "read_file", "arguments": ""]
            ]))
            if completion == "response.output_item.done" {
                _ = try decoder.consume(frame(["type": completion, "output_index": 0, "item": call]))
            } else {
                _ = try decoder.consume(frame([
                    "type": completion, "output_index": 0, "item_id": "fc", "arguments": "{}"
                ]))
            }
            var changed = call
            changed["arguments"] = "{\"changed\":true}"
            XCTAssertThrowsError(try decoder.consume(responseCompleted(output: [changed])))
            XCTAssertFalse(decoder.isFinished)
            XCTAssertThrowsError(try decoder.finish())
        }
    }

    func testResponsesFailuresIncompleteAndDoneWithoutCompletedNeverSucceed() throws {
        for type in ["response.failed", "response.incomplete", "response.cancelled", "error"] {
            var decoder = NativeModelStreamDecoder(format: .responses)
            XCTAssertThrowsError(try decoder.consume(frame([
                "type": type, "response": ["error": ["message": "secret-test-key"]], "message": "secret-test-key"
            ]))) { error in
                XCTAssertFalse(error.localizedDescription.contains("secret-test-key"))
            }
            XCTAssertFalse(decoder.isFinished)
        }
        var decoder = NativeModelStreamDecoder(format: .responses)
        XCTAssertThrowsError(try decoder.consume(NativeSSEFrame(event: nil, data: "[DONE]")))
        XCTAssertThrowsError(try decoder.finish())
        XCTAssertThrowsError(try decoder.consume(frame(["type": "response.completed", "response": ["status": "incomplete", "output": []]])))
        XCTAssertThrowsError(try decoder.consume(frame(["type": "response.completed", "response": ["status": "completed"]])))
    }

    func testResponsesCompletionRequiresSuccessfulStatusWithoutFailureDetails() throws {
        let invalidResponses: [[String: Any]] = [
            ["output": []],
            ["status": NSNull(), "output": []],
            ["status": "completed", "output": [], "error": ["message": "secret-test-key"]],
            ["status": "completed", "output": [], "incomplete_details": ["reason": "max_output_tokens"]]
        ]
        for response in invalidResponses {
            var decoder = NativeModelStreamDecoder(format: .responses)
            XCTAssertThrowsError(try decoder.consume(frame(["type": "response.completed", "response": response]))) { error in
                XCTAssertFalse(error.localizedDescription.contains("secret-test-key"))
            }
            XCTAssertFalse(decoder.isFinished)
            XCTAssertThrowsError(try decoder.finish())
        }
        var decoder = NativeModelStreamDecoder(format: .responses)
        XCTAssertEqual(eventLabels(try decoder.consume(frame([
            "type": "response.completed", "response": [
                "status": "completed", "output": [], "error": NSNull(), "incomplete_details": NSNull()
            ]
        ]))), ["replay:0", "finished"])
    }

    func testResponsesTruncatedToolNeverEmitsCallAndFinalCannotLoseTrackedCall() throws {
        var decoder = NativeModelStreamDecoder(format: .responses)
        XCTAssertEqual(eventLabels(try decoder.consume(frame([
            "type": "response.output_item.added", "output_index": 0,
            "item": ["type": "function_call", "id": "fc", "call_id": "c", "name": "read_file", "arguments": ""]
        ]))), ["started:c:read_file"])
        XCTAssertTrue(try decoder.consume(frame([
            "type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc", "delta": "{"
        ])).isEmpty)
        XCTAssertThrowsError(try decoder.finish())
        XCTAssertThrowsError(try decoder.consume(responseCompleted()))
        XCTAssertFalse(decoder.isFinished)
    }

    func testChatInterleavedToolsReasoningTextUsageAndDone() throws {
        var decoder = NativeModelStreamDecoder(format: .chatCompletions)
        XCTAssertEqual(eventLabels(try decoder.consume(chatChunk([
            "reasoning_content": "Think", "content": "Read", "tool_calls": [
                ["index": 1, "id": "c1", "type": "function", "function": ["name": "second", "arguments": "{\"b\":"]],
                ["index": 0, "id": "c0", "type": "function", "function": ["name": "first", "arguments": "{\"a\":"]]
            ]
        ]))), ["reasoning:Think", "text:Read", "started:c1:second", "started:c0:first"])
        XCTAssertTrue(try decoder.consume(chatChunk(["tool_calls": [
            ["index": 0, "function": ["arguments": "\"🌍\"}"]],
            ["id": "c1", "function": ["arguments": "2}"]]
        ]])).isEmpty)
        XCTAssertTrue(try decoder.consume(chatChunk([:], finish: "tool_calls")).isEmpty)
        XCTAssertFalse(decoder.isFinished)
        XCTAssertTrue(try decoder.consume(frame([
            "choices": [], "usage": ["prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46]
        ])).isEmpty)
        let events = try decoder.consume(NativeSSEFrame(event: nil, data: "[DONE]"))
        XCTAssertEqual(eventLabels(events), ["tool:c0:first:{\"a\":\"🌍\"}", "tool:c1:second:{\"b\":2}", "usage:12:34", "finished"])
        try decoder.finish()
    }

    func testChatFunctionNameDeltasAndResponsesArgumentMismatch() throws {
        var chat = NativeModelStreamDecoder(format: .chatCompletions)
        _ = try chat.consume(chatChunk(["tool_calls": [
            ["index": 0, "id": "c", "function": ["name": "read_", "arguments": "{"]]
        ]]))
        _ = try chat.consume(chatChunk(["tool_calls": [
            ["index": 0, "function": ["name": "file", "arguments": "}"]]
        ]], finish: "tool_calls"))
        XCTAssertEqual(eventLabels(try chat.consume(NativeSSEFrame(event: nil, data: "[DONE]"))),
                       ["tool:c:read_file:{}", "finished"])
        var responses = NativeModelStreamDecoder(format: .responses)
        _ = try responses.consume(frame(["type": "response.function_call_arguments.delta", "output_index": 0,
                                         "item_id": "fc", "delta": "{}"] ))
        XCTAssertThrowsError(try responses.consume(frame([
            "type": "response.function_call_arguments.done", "output_index": 0, "item_id": "fc", "arguments": "{\"changed\":true}"
        ])))
    }

    func testChatStopStillRequiresDoneAndNullContentIsAccepted() throws {
        var decoder = NativeModelStreamDecoder(format: .chatCompletions)
        _ = try decoder.consume(chatChunk(["role": "assistant", "content": NSNull()]))
        XCTAssertEqual(eventLabels(try decoder.consume(chatChunk(["content": "Hello"], finish: "stop"))), ["text:Hello"])
        XCTAssertThrowsError(try decoder.finish())
        XCTAssertEqual(eventLabels(try decoder.consume(NativeSSEFrame(event: nil, data: "[DONE]"))), ["finished"])
    }

    func testChatTruncationMalformedArgumentsAndUnsuccessfulFinishReasons() throws {
        var decoder = NativeModelStreamDecoder(format: .chatCompletions)
        XCTAssertThrowsError(try decoder.consume(NativeSSEFrame(event: nil, data: "[DONE]")))
        _ = try decoder.consume(chatChunk(["tool_calls": [
            ["index": 0, "id": "c", "function": ["name": "read_file", "arguments": "{"]]
        ]]))
        XCTAssertThrowsError(try decoder.finish())
        _ = try decoder.consume(chatChunk([:], finish: "tool_calls"))
        XCTAssertThrowsError(try decoder.consume(NativeSSEFrame(event: nil, data: "[DONE]")))
        XCTAssertFalse(decoder.isFinished)
        for reason in ["length", "content_filter", "function_call", "error", "unknown"] {
            var failed = NativeModelStreamDecoder(format: .chatCompletions)
            XCTAssertThrowsError(try failed.consume(chatChunk([:], finish: reason)))
            XCTAssertFalse(failed.isFinished)
        }
    }

    func testChatStopWithValidToolsRequiresDoneThenAuthorizesCalls() throws {
        var decoder = NativeModelStreamDecoder(format: .chatCompletions)
        XCTAssertEqual(eventLabels(try decoder.consume(chatChunk(["tool_calls": [
            ["index": 0, "id": "c", "function": ["name": "read_file", "arguments": "{}"]]
        ]], finish: "stop"))), ["started:c:read_file"])
        XCTAssertFalse(decoder.isFinished)
        XCTAssertThrowsError(try decoder.finish())
        XCTAssertEqual(eventLabels(try decoder.consume(NativeSSEFrame(event: nil, data: "[DONE]"))),
                       ["tool:c:read_file:{}", "finished"])
        XCTAssertTrue(decoder.isFinished)
        try decoder.finish()
    }

    func testResponsesToolPreviewPrecedesFollowingTextAndOnlyTerminalAuthorizesCall() throws {
        var decoder = NativeModelStreamDecoder(format: .responses)
        let item: [String: Any] = [
            "type": "function_call", "id": "fc", "call_id": "c", "name": "read_file", "arguments": ""
        ]
        let added = try frame(["type": "response.output_item.added", "output_index": 0, "item": item])
        var events = try decoder.consume(added)
        XCTAssertEqual(eventLabels(events), ["started:c:read_file"])
        XCTAssertTrue(try decoder.consume(added).isEmpty, "Repeated item must not duplicate preview")
        events += try decoder.consume(frame(["type": "response.output_text.delta", "delta": "After tool"]))
        events += try decoder.consume(frame([
            "type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc", "delta": "{}"
        ]))
        var completedItem = item
        completedItem["arguments"] = "{}"
        XCTAssertTrue(try decoder.consume(frame([
            "type": "response.output_item.done", "output_index": 0, "item": completedItem
        ])).isEmpty)
        XCTAssertEqual(eventLabels(events), ["started:c:read_file", "text:After tool"])
        XCTAssertFalse(decoder.isFinished)
        events += try decoder.consume(responseCompleted(output: [completedItem]))
        XCTAssertEqual(eventLabels(events), [
            "started:c:read_file", "text:After tool", "tool:c:read_file:{}", "usage:11:7", "replay:1", "finished"
        ])
        try decoder.finish()
    }

    func testChatToolPreviewWaitsForIdentityEmitsOnceAndTerminalUsesFullName() throws {
        for nameFirst in [false, true] {
            var decoder = NativeModelStreamDecoder(format: .chatCompletions)
            let identity: [String: Any] = nameFirst
                ? ["index": 0, "function": ["name": "read_"]]
                : ["index": 0, "id": "c"]
            XCTAssertTrue(try decoder.consume(chatChunk(["tool_calls": [identity]])).isEmpty)
            let remaining: [String: Any] = nameFirst
                ? ["index": 0, "id": "c", "function": ["arguments": "{"]]
                : ["index": 0, "function": ["name": "read_", "arguments": "{"]]
            var events = try decoder.consume(chatChunk(["tool_calls": [remaining]]))
            XCTAssertEqual(eventLabels(events), ["started:c:read_"])
            events += try decoder.consume(chatChunk(["content": "After tool"]))
            XCTAssertTrue(try decoder.consume(chatChunk(["tool_calls": [
                ["index": 0, "id": "c", "function": ["name": "file", "arguments": "}"]]
            ]], finish: "tool_calls")).isEmpty)
            XCTAssertEqual(eventLabels(events), ["started:c:read_", "text:After tool"])
            XCTAssertFalse(decoder.isFinished)
            events += try decoder.consume(NativeSSEFrame(event: nil, data: "[DONE]"))
            XCTAssertEqual(eventLabels(events), [
                "started:c:read_", "text:After tool", "tool:c:read_file:{}", "finished"
            ])
            try decoder.finish()
        }
    }

    func testTruncatedToolPreviewsNeverAuthorizeCalls() throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            var decoder = NativeModelStreamDecoder(format: format)
            let start: NativeSSEFrame
            let text: NativeSSEFrame
            if format == .responses {
                start = try frame([
                    "type": "response.output_item.added", "output_index": 0,
                    "item": ["type": "function_call", "id": "fc", "call_id": "c", "name": "read_file", "arguments": "{}"]
                ])
                text = try frame(["type": "response.output_text.delta", "delta": "After tool"])
            } else {
                start = try chatChunk(["tool_calls": [
                    ["index": 0, "id": "c", "function": ["name": "read_file", "arguments": "{}"]]
                ]])
                text = try chatChunk(["content": "After tool"], finish: "stop")
            }
            var events = try decoder.consume(start)
            events += try decoder.consume(text)
            XCTAssertEqual(eventLabels(events), ["started:c:read_file", "text:After tool"])
            XCTAssertThrowsError(try decoder.finish())
            XCTAssertFalse(decoder.isFinished)
        }
    }

    func testChatFailedFinishAfterPreviewNeverAuthorizesCalls() throws {
        for reason in ["length", "error"] {
            var decoder = NativeModelStreamDecoder(format: .chatCompletions)
            XCTAssertEqual(eventLabels(try decoder.consume(chatChunk(["tool_calls": [
                ["index": 0, "id": "c", "function": ["name": "read_file", "arguments": "{}"]]
            ]]))), ["started:c:read_file"])
            XCTAssertThrowsError(try decoder.consume(chatChunk([:], finish: reason)))
            XCTAssertThrowsError(try decoder.consume(NativeSSEFrame(event: nil, data: "[DONE]")))
            XCTAssertThrowsError(try decoder.finish())
            XCTAssertFalse(decoder.isFinished)
        }
        var decoder = NativeModelStreamDecoder(format: .chatCompletions)
        _ = try decoder.consume(chatChunk(["tool_calls": [
            ["index": 0, "id": "c", "function": ["name": "read_file", "arguments": "{}"]]
        ]]))
        XCTAssertThrowsError(try decoder.consume(frame(["error": ["message": "secret-test-key"]])))
        XCTAssertThrowsError(try decoder.consume(NativeSSEFrame(event: nil, data: "[DONE]")))
        XCTAssertFalse(decoder.isFinished)
    }

    func testConflictingChatIdentityAndOversizedToolArgumentsAreRejected() throws {
        var decoder = NativeModelStreamDecoder(format: .chatCompletions)
        _ = try decoder.consume(chatChunk(["tool_calls": [
            ["index": 0, "id": "c0", "function": ["name": "read_file", "arguments": ""]]
        ]]))
        XCTAssertThrowsError(try decoder.consume(chatChunk(["tool_calls": [["index": 0, "id": "different"]]])))
        var bounded = NativeModelStreamDecoder(format: .chatCompletions)
        XCTAssertThrowsError(try bounded.consume(chatChunk(["tool_calls": [
            ["index": 0, "id": "c", "function": ["name": "read_file", "arguments": String(repeating: "x", count: 1024 * 1024 + 1)]]
        ]])))
    }

    func testMalformedStreamAndProviderErrorsNeverEchoSecrets() throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            var decoder = NativeModelStreamDecoder(format: format)
            for raw in ["secret-test-key not JSON", "{\"error\":{\"message\":\"Bearer secret-test-key\"}}"] {
                XCTAssertThrowsError(try decoder.consume(NativeSSEFrame(event: nil, data: raw))) { error in
                    XCTAssertFalse(error.localizedDescription.contains("secret-test-key"))
                    XCTAssertLessThan(error.localizedDescription.count, 200)
                }
            }
        }
    }

    func testProviderHTTPReasonUsesOnlyExactAllowlistedFields() throws {
        let codes: [(String, NativeProviderHTTPReason)] = [
            ("unauthorized_client_error", .unauthorizedClient),
            ("invalid_api_key", .invalidCredential), ("invalid_api_key_error", .invalidCredential),
            ("model_not_found", .unsupportedModel), ("unsupported_model", .unsupportedModel),
            ("rate_limit_exceeded", .rateLimited), ("rate_limit_error", .rateLimited),
            ("unsupported_parameter", .invalidRequest), ("invalid_request_error", .invalidRequest)
        ]
        for (code, expected) in codes {
            for json in [["error": ["type": code]], ["error": ["code": code]],
                         ["type": code], ["code": code]] as [[String: Any]] {
                let data = try JSONSerialization.data(withJSONObject: json)
                let error = NativeProviderHTTPDiagnostic.error(status: 401, data: data)
                guard case .httpReason(let status, let reason) = error else {
                    return XCTFail("Allowlisted code was not classified: \(code)")
                }
                XCTAssertEqual(status, 401)
                XCTAssertEqual(reason, expected)
                XCTAssertEqual(NativeProviderHTTPDiagnostic.description(status: 401, data: data),
                               error.localizedDescription)
            }
        }
        // A specific nested code takes precedence over a generic nested type.
        let data = Data("{\"error\":{\"type\":\"invalid_request_error\",\"code\":\"model_not_found\"}}".utf8)
        guard case .httpReason(_, .unsupportedModel) = NativeProviderHTTPDiagnostic.error(status: 404, data: data) else {
            return XCTFail("Specific error code was lost")
        }
    }

    func testProviderHTTPReasonDoesNotInferFromStatusMessagesOrUnknownCodes() throws {
        let bodies: [Data?] = [
            nil, Data(), Data("not JSON: secret-test-key".utf8), Data("[]".utf8),
            Data("{\"error\":{\"type\":\"invalid_api_key\"}".utf8),
            Data("{\"message\":\"UNAUTHENTICATED\",\"success\":false}".utf8),
            Data("{\"error\":\"invalid_api_key\"}".utf8),
            Data("{\"message\":\"unauthorized_client_error invalid_api_key model_not_found\"}".utf8),
            Data("{\"error\":{\"message\":\"invalid_api_key\",\"type\":\"authentication_error\"}}".utf8),
            Data("{\"detail\":{\"code\":\"invalid_api_key\"}}".utf8),
            Data("{\"type\":401,\"code\":true}".utf8)
        ] + ["INVALID_API_KEY", " invalid_api_key", "invalid_api_key ", "invalid_api_key:secret-test-key",
             "unknown_provider_error", "UNAUTHENTICATED"].map { Data(("{\"code\":\"" + $0 + "\"}").utf8) }
        for status in [400, 401, 403, 404, 429, 500] {
            for data in bodies {
                let error = NativeProviderHTTPDiagnostic.error(status: status, data: data)
                guard case .http(let actual) = error else { return XCTFail("Unknown response was classified") }
                XCTAssertEqual(actual, status)
                XCTAssertEqual(error.localizedDescription, "Model provider returned HTTP \(status); response details withheld.")
            }
        }
    }

    func testProviderHTTPDiagnosticWithholdsAllUntrustedDetailsAndRejectsOversizedJSON() throws {
        let secret = "Bearer secret-test-key https://provider.example/?key=secret%2Dtest%2Dkey c2VjcmV0LXRlc3Qta2V5"
        let json: [String: Any] = [
            "error": ["message": secret], "message": "UNAUTHENTICATED " + secret,
            "success": false, "type": "unauthorized_client_error", "url": secret
        ]
        let data = try JSONSerialization.data(withJSONObject: json)
        let description = NativeProviderHTTPDiagnostic.description(status: 401, data: data)
        XCTAssertEqual(description, "Model provider returned HTTP 401; provider rejected this client; contact provider support.")
        for value in [secret, "secret-test-key", "secret%2Dtest%2Dkey", "c2VjcmV0LXRlc3Qta2V5", "provider.example", "UNAUTHENTICATED"] {
            XCTAssertFalse(description.contains(value))
        }
        XCTAssertLessThan(description.count, 200)
        let prefix = "{\"type\":\"unauthorized_client_error\"}"
        let exactLimit = Data((prefix + String(repeating: " ", count: 8192 - prefix.utf8.count)).utf8)
        XCTAssertEqual(NativeProviderHTTPDiagnostic.description(status: 401, data: exactLimit), description)
        let oversized = exactLimit + Data(" ".utf8)
        guard case .http(401) = NativeProviderHTTPDiagnostic.error(status: 401, data: oversized) else {
            return XCTFail("Oversized JSON was parsed")
        }
    }

    func testTransportErrorDescriptionsUseFixedCodeDiagnoses() {
        let expected: [(Int, String)] = [
            (-1003, "provider hostname could not be resolved; check DNS and connectivity."),
            (-1200, "secure connection failed; check TLS compatibility."),
            (-1202, "provider certificate is not trusted; check the certificate chain."),
            (-1001, "request timed out; check provider connectivity.")
        ]
        for (code, detail) in expected {
            XCTAssertEqual(NativeModelClientError.transport(code).localizedDescription,
                           "Model provider transport failed (code \(code)); " + detail)
        }
        XCTAssertEqual(NativeModelClientError.transport(-9999).localizedDescription,
                       "Model provider transport failed (code -9999); details withheld.")
    }

    @available(iOS 15.0, macOS 12.0, *)
    private func stubbed(_ fixture: NativeModelURLFixture, format: NativeProviderFormat = .responses)
        -> (URLSessionNativeModelClient, NativeModelRequest, String) {
        let host = UUID().uuidString.lowercased() + ".example.test"
        NativeModelTestURLProtocol.register(fixture, host: host)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NativeModelTestURLProtocol.self]
        let client = URLSessionNativeModelClient(configuration: configuration)
        var modelRequest = request(format)
        modelRequest.baseURL = URL(string: "https://" + host + "/v1")!
        return (client, modelRequest, host)
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testTransportHTTPErrorIsBoundedRedactedAndNotRetried() async throws {
        let stopped = expectation(description: "Non-JSON error body was cancelled without reading")
        let fixture = NativeModelURLFixture(status: 401, body: Data(String(repeating: "secret-test-key", count: 10000).utf8),
                                            finish: false, stopped: stopped)
        let (client, modelRequest, host) = stubbed(fixture)
        defer { NativeModelTestURLProtocol.unregister(host: host) }
        do {
            for try await _ in client.stream(request: modelRequest) { XCTFail("HTTP error must not emit events") }
            XCTFail("Expected HTTP error")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("401"))
            XCTAssertFalse(error.localizedDescription.contains(modelRequest.apiKey))
            XCTAssertLessThan(error.localizedDescription.count, 200)
        }
        await fulfillment(of: [stopped], timeout: 2)
        XCTAssertEqual(fixture.requestCount, 1)
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testTransportClassifiesOnlyTheActualJSONErrorResponseWithoutRetry() async throws {
        let json: [String: Any] = [
            "error": ["message": "unauthorized client detected, contact support secret-test-key"],
            "message": "UNAUTHENTICATED", "success": false, "type": "unauthorized_client_error"
        ]
        let data = try JSONSerialization.data(withJSONObject: json)
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            for contentType in ["application/json; charset=utf-8", "application/problem+json", "text/event-stream", "text/html", ""] {
                let fixture = NativeModelURLFixture(status: 401, body: data, contentType: contentType)
                let (client, modelRequest, host) = stubbed(fixture, format: format)
                defer { NativeModelTestURLProtocol.unregister(host: host) }
                do {
                    for try await _ in client.stream(request: modelRequest) { XCTFail("HTTP error emitted an event") }
                    XCTFail("Expected HTTP error")
                } catch {
                    guard let safe = error as? NativeModelClientError else { return XCTFail("Client error was not preserved") }
                    if contentType.hasPrefix("application/") {
                        guard case .httpReason(401, .unauthorizedClient) = safe else { return XCTFail("JSON error was not classified") }
                    } else {
                        guard case .http(401) = safe else { return XCTFail("Non-JSON error body was read") }
                    }
                    XCTAssertFalse(error.localizedDescription.contains(modelRequest.apiKey))
                }
                XCTAssertEqual(fixture.requestCount, 1)
            }
        }
        // A 401 from an authenticated request does not inherit the public /models diagnosis.
        let fixture = NativeModelURLFixture(status: 401, body: Data("{\"error\":{\"code\":\"invalid_api_key\"}}".utf8),
                                            contentType: "application/json")
        let (client, modelRequest, host) = stubbed(fixture)
        defer { NativeModelTestURLProtocol.unregister(host: host) }
        do {
            for try await _ in client.stream(request: modelRequest) { XCTFail("HTTP error emitted an event") }
            XCTFail("Expected HTTP error")
        } catch {
            guard let safe = error as? NativeModelClientError,
                  case .httpReason(401, .invalidCredential) = safe else { return XCTFail("Actual credential diagnosis was lost") }
        }
        XCTAssertEqual(fixture.requestCount, 1)
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testTransportErrorBodyReadIsBoundedByBytesAndDeclaredLength() async throws {
        let prefix = "{\"type\":\"unauthorized_client_error\"}"
        let atLimit = Data((prefix + String(repeating: " ", count: 8192 - prefix.utf8.count)).utf8)
        let cases: [(Data, Int64?, Bool, Bool)] = [
            (atLimit, nil, true, true), (atLimit + Data(" ".utf8), nil, true, false),
            (atLimit + Data(" ".utf8), nil, false, false), (Data(prefix.utf8), 8193, false, false),
            (Data(prefix.utf8), Int64(prefix.utf8.count), true, true),
            (Data("{".utf8), 100, true, false)
        ]
        for (data, length, finish, classified) in cases {
            let stopped = finish ? nil : expectation(description: "Bounded error body request was cancelled")
            let fixture = NativeModelURLFixture(status: 401, body: data, finish: finish,
                                                stopped: stopped,
                                                contentType: "application/json", expectedLength: length)
            let (client, modelRequest, host) = stubbed(fixture)
            defer { NativeModelTestURLProtocol.unregister(host: host) }
            do {
                for try await _ in client.stream(request: modelRequest) { XCTFail("HTTP error emitted an event") }
                XCTFail("Expected HTTP error")
            } catch {
                guard let safe = error as? NativeModelClientError else { return XCTFail("Client error was lost") }
                if classified {
                    guard case .httpReason(401, .unauthorizedClient) = safe else { return XCTFail("8 KiB JSON was rejected") }
                } else {
                    guard case .http(401) = safe else { return XCTFail("Oversized body was classified") }
                }
            }
            XCTAssertEqual(fixture.requestCount, 1)
            if let stopped { await fulfillment(of: [stopped], timeout: 3) }
        }
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testNeverEndingJSONErrorBodyTimesOutAndCancelsTheSession() async throws {
        let stopped = expectation(description: "Never-ending JSON error request stopped")
        let completed = expectation(description: "HTTP error delivered within two seconds")
        // Even a complete JSON prefix cannot be trusted before EOF.
        let fixture = NativeModelURLFixture(status: 401, body: Data("{\"type\":\"unauthorized_client_error\"}".utf8),
                                            finish: false, stopped: stopped, contentType: "application/json")
        let (client, modelRequest, host) = stubbed(fixture)
        defer { NativeModelTestURLProtocol.unregister(host: host) }
        let consumer = Task { () -> Bool in
            defer { completed.fulfill() }
            do {
                for try await _ in client.stream(request: modelRequest) { XCTFail("HTTP error emitted an event") }
            } catch {
                guard let safe = error as? NativeModelClientError, case .http(401) = safe else { return false }
                return true
            }
            return false
        }
        await fulfillment(of: [completed, stopped], timeout: 2)
        consumer.cancel()
        let receivedHTTPError = await consumer.value
        XCTAssertTrue(receivedHTTPError, "Timeout must retain HTTP status, not become a transport error")
        XCTAssertEqual(fixture.requestCount, 1)
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testConsumerCancellationDuringJSONErrorReadStopsTheSessionPromptly() async throws {
        let started = expectation(description: "JSON error headers delivered")
        let stopped = expectation(description: "Error body cancelled before its deadline")
        let completed = expectation(description: "Cancelled consumer returned promptly")
        let fixture = NativeModelURLFixture(status: 401, body: Data("{".utf8), finish: false,
                                            started: started, stopped: stopped, contentType: "application/json")
        let (client, modelRequest, host) = stubbed(fixture)
        defer { NativeModelTestURLProtocol.unregister(host: host) }
        let consumer = Task { () -> Bool in
            defer { completed.fulfill() }
            do {
                for try await _ in client.stream(request: modelRequest) { XCTFail("HTTP error emitted an event") }
            } catch { }
            return Task.isCancelled
        }
        await fulfillment(of: [started], timeout: 3)
        try await Task.sleep(nanoseconds: 50_000_000)
        consumer.cancel()
        await fulfillment(of: [completed, stopped], timeout: 0.75)
        let wasCancelled = await consumer.value
        XCTAssertTrue(wasCancelled)
        XCTAssertEqual(fixture.requestCount, 1)
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testTransportFailureMapsCodesWithoutEchoingUnderlyingError() async throws {
        for code in [-1003, -1200, -1202, -1001, -9999] {
            let underlying = NSError(domain: NSURLErrorDomain, code: code, userInfo: [
                NSLocalizedDescriptionKey: "Bearer secret-test-key https://provider.example/?key=secret%2Dtest%2Dkey",
                NSURLErrorFailingURLErrorKey: URL(string: "https://provider.example/?key=secret-test-key")!
            ])
            let fixture = NativeModelURLFixture(body: Data(), failure: underlying)
            let (client, modelRequest, host) = stubbed(fixture)
            defer { NativeModelTestURLProtocol.unregister(host: host) }
            do {
                for try await _ in client.stream(request: modelRequest) { XCTFail("Transport failure emitted an event") }
                XCTFail("Expected transport failure")
            } catch {
                guard let safe = error as? NativeModelClientError,
                      case .transport(let actualCode) = safe else { return XCTFail("Transport error was not preserved") }
                XCTAssertEqual(actualCode, code)
                XCTAssertEqual(error.localizedDescription, NativeModelClientError.transport(code).localizedDescription)
                for secret in [modelRequest.apiKey, "provider.example", "secret%2Dtest%2Dkey", "Bearer"] {
                    XCTAssertFalse(error.localizedDescription.contains(secret))
                }
            }
            XCTAssertEqual(fixture.requestCount, 1)
        }
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testTransportTruncatedAndCompletedStreams() async throws {
        let complete = try responseCompleted()
        for successful in [false, true] {
            let wire = successful ? "data: " + complete.data + "\r\n\r\n" : "data: {\"type\":\"response.output_text.delta\",\"delta\":\"partial\"}\n\n"
            let fixture = NativeModelURLFixture(body: Data(wire.utf8))
            let (client, modelRequest, host) = stubbed(fixture)
            defer { NativeModelTestURLProtocol.unregister(host: host) }
            var labels: [String] = []
            do {
                for try await event in client.stream(request: modelRequest) { labels += eventLabels([event]) }
                XCTAssertTrue(successful, "Truncated stream reported success")
                XCTAssertEqual(labels, ["usage:11:7", "replay:0", "finished"])
            } catch {
                XCTAssertFalse(successful, "Complete stream failed: \(error)")
                XCTAssertFalse(labels.contains("finished"))
                XCTAssertTrue(error is NativeModelClientError)
            }
            XCTAssertEqual(fixture.requestCount, 1)
        }
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testTransportToolPreviewOrderingAndTruncatedPreviewNeverAuthorizesCall() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let frames: [NativeSSEFrame]
            let terminal: NativeSSEFrame
            if format == .responses {
                let call: [String: Any] = [
                    "type": "function_call", "id": "fc", "call_id": "c", "name": "read_file", "arguments": "{}"
                ]
                frames = try [
                    frame(["type": "response.output_item.added", "output_index": 0, "item": call]),
                    frame(["type": "response.output_text.delta", "delta": "After tool"])
                ]
                terminal = try responseCompleted(output: [call])
            } else {
                frames = try [
                    chatChunk(["tool_calls": [
                        ["index": 0, "id": "c", "function": ["name": "read_file", "arguments": "{}"]]
                    ]]),
                    chatChunk(["content": "After tool"], finish: "stop")
                ]
                terminal = NativeSSEFrame(event: nil, data: "[DONE]")
            }
            for successful in [false, true] {
                let wireFrames = frames + (successful ? [terminal] : [])
                let wire = wireFrames.map { "data: " + $0.data + "\n\n" }.joined()
                let fixture = NativeModelURLFixture(body: Data(wire.utf8))
                let (client, modelRequest, host) = stubbed(fixture, format: format)
                defer { NativeModelTestURLProtocol.unregister(host: host) }
                var labels: [String] = []
                do {
                    for try await event in client.stream(request: modelRequest) { labels += eventLabels([event]) }
                    XCTAssertTrue(successful, "Truncated preview reported success")
                } catch {
                    XCTAssertFalse(successful, "Complete stream failed: \(error)")
                    guard let safe = error as? NativeModelClientError,
                          case .truncatedStream = safe else { return XCTFail("Unexpected error: \(error)") }
                }
                var expected = ["started:c:read_file", "text:After tool"]
                if successful {
                    expected.append("tool:c:read_file:{}")
                    if format == .responses { expected += ["usage:11:7", "replay:1"] }
                    expected.append("finished")
                }
                XCTAssertEqual(labels, expected)
                XCTAssertEqual(fixture.requestCount, 1)
            }
        }
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testTransportUnterminatedCompletionMarkersNeverSucceed() async throws {
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let prefix: String
            let terminal: String
            if format == .responses {
                prefix = ""
                terminal = try responseCompleted().data
            } else {
                prefix = "data: " + (try chatChunk([:], finish: "stop")).data + "\n\n"
                terminal = "[DONE]"
            }
            for ending in ["", "\n"] {
                let fixture = NativeModelURLFixture(body: Data((prefix + "data: " + terminal + ending).utf8))
                let (client, modelRequest, host) = stubbed(fixture, format: format)
                defer { NativeModelTestURLProtocol.unregister(host: host) }
                do {
                    for try await _ in client.stream(request: modelRequest) {
                        XCTFail("Unterminated completion marker must not emit events")
                    }
                    XCTFail("Unterminated completion marker reported success")
                } catch {
                    guard let safe = error as? NativeModelClientError,
                          case .truncatedStream = safe else { return XCTFail("Unexpected error: \(error)") }
                }
                XCTAssertEqual(fixture.requestCount, 1)
            }
        }
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testSlowConsumerFailsRatherThanSilentlyDroppingDeltas() async throws {
        let wire = String(repeating: "data: {\"type\":\"response.output_text.delta\",\"delta\":\"x\"}\n\n", count: 300)
            + "data: " + (try responseCompleted()).data + "\n\n"
        let stopped = expectation(description: "Producer stopped on bounded buffer overflow")
        let fixture = NativeModelURLFixture(body: Data(wire.utf8), finish: false, stopped: stopped)
        let (client, modelRequest, host) = stubbed(fixture)
        defer { NativeModelTestURLProtocol.unregister(host: host) }
        let stream = client.stream(request: modelRequest)
        await fulfillment(of: [stopped], timeout: 3)
        var count = 0
        do {
            for try await event in stream {
                count += 1
                if case .finished = event { XCTFail("Overflow must not report success") }
            }
            XCTFail("Expected bounded-buffer error")
        } catch {
            guard let safe = error as? NativeModelClientError,
                  case .slowConsumer = safe else { return XCTFail("Unexpected error: \(error)") }
        }
        XCTAssertEqual(count, 256)
        XCTAssertEqual(fixture.requestCount, 1)
    }

    @available(iOS 15.0, macOS 12.0, *)
    func testConsumerCancellationStopsUnderlyingURLSessionTask() async throws {
        let started = expectation(description: "URLProtocol started")
        let stopped = expectation(description: "URLProtocol stopped after cancellation")
        let fixture = NativeModelURLFixture(body: Data(": ready\n\n".utf8), finish: false,
                                           started: started, stopped: stopped)
        let (client, modelRequest, host) = stubbed(fixture)
        defer { NativeModelTestURLProtocol.unregister(host: host) }
        let consumer = Task { () -> Bool in
            do {
                for try await event in client.stream(request: modelRequest) {
                    if case .finished = event { return true }
                }
            } catch { }
            return false
        }
        await fulfillment(of: [started], timeout: 3)
        consumer.cancel()
        await fulfillment(of: [stopped], timeout: 3)
        let reportedSuccess = await consumer.value
        XCTAssertFalse(reportedSuccess)
        XCTAssertEqual(fixture.requestCount, 1)
    }
}

// Fixtures are keyed per request host and locked, so independent test requests
// never race through a shared, mutable global response handler.
private final class NativeModelURLFixture {
    let status: Int
    let body: Data
    let finish: Bool
    let contentType: String
    let expectedLength: Int64?
    let failure: NSError?
    let started: XCTestExpectation?
    let stopped: XCTestExpectation?
    private let lock = NSLock()
    private var count = 0

    init(status: Int = 200, body: Data, finish: Bool = true,
         started: XCTestExpectation? = nil, stopped: XCTestExpectation? = nil,
         contentType: String = "text/event-stream", expectedLength: Int64? = nil, failure: NSError? = nil) {
        self.status = status
        self.body = body
        self.finish = finish
        self.contentType = contentType
        self.expectedLength = expectedLength
        self.failure = failure
        self.started = started
        self.stopped = stopped
    }

    var requestCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return count
    }

    func recordRequest() {
        lock.lock()
        count += 1
        lock.unlock()
    }
}

private final class NativeModelTestURLProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var fixtures: [String: NativeModelURLFixture] = [:]
    private var fixture: NativeModelURLFixture?

    static func register(_ fixture: NativeModelURLFixture, host: String) {
        lock.lock()
        fixtures[host] = fixture
        lock.unlock()
    }

    static func unregister(host: String) {
        lock.lock()
        fixtures.removeValue(forKey: host)
        lock.unlock()
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lock.lock()
        let fixture = Self.fixtures[request.url?.host ?? ""]
        self.fixture = fixture
        Self.lock.unlock()
        if let failure = fixture?.failure {
            fixture?.recordRequest()
            client?.urlProtocol(self, didFailWithError: failure)
            fixture?.started?.fulfill()
            return
        }
        var headers = ["Content-Type": fixture?.contentType ?? ""]
        if let length = fixture?.expectedLength { headers["Content-Length"] = String(length) }
        guard let fixture, let url = request.url,
              let response = HTTPURLResponse(url: url, statusCode: fixture.status,
                                             httpVersion: "HTTP/1.1", headerFields: headers) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        fixture.recordRequest()
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: fixture.body)
        if fixture.finish { client?.urlProtocolDidFinishLoading(self) }
        fixture.started?.fulfill()
    }

    override func stopLoading() { fixture?.stopped?.fulfill() }
}
