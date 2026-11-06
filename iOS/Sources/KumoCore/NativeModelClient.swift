import Foundation

public enum NativeProviderFormat: String, Codable {
    case responses, chatCompletions
}

public struct NativeToolCall {
    public let id: String
    public let name: String
    public let arguments: String

    public init(id: String, name: String, arguments: String) {
        self.id = id
        self.name = name
        self.arguments = arguments
    }
}

public struct NativeModelMessage {
    public var role: String
    public var content: String
    public var toolCalls: [NativeToolCall]
    public var toolCallID: String?
    public var replayItems: [[String: Any]]

    public init(role: String, content: String, toolCalls: [NativeToolCall] = [],
                toolCallID: String? = nil, replayItems: [[String: Any]] = []) {
        self.role = role
        self.content = content
        self.toolCalls = toolCalls
        self.toolCallID = toolCallID
        self.replayItems = replayItems
    }
}

public struct NativeModelRequest {
    public var baseURL: URL
    public var apiKey: String
    public var format: NativeProviderFormat
    public var model: String
    public var system: String
    public var messages: [NativeModelMessage]
    public var tools: [[String: Any]]
    public var effort: String?
    public var maxOutputTokens: Int?

    public init(baseURL: URL, apiKey: String, format: NativeProviderFormat, model: String,
                system: String, messages: [NativeModelMessage], tools: [[String: Any]],
                effort: String? = nil, maxOutputTokens: Int? = nil) {
        self.baseURL = baseURL
        self.apiKey = apiKey
        self.format = format
        self.model = model
        self.system = system
        self.messages = messages
        self.tools = tools
        self.effort = effort
        self.maxOutputTokens = maxOutputTokens
    }
}

public enum NativeModelEvent {
    case text(String)
    case reasoning(String)
    /// Display-only preview, possibly with a partial name; never authorizes execution or claims success.
    case toolStarted(id: String, name: String)
    /// A validated call emitted only after successful provider termination; may now be executed.
    case toolCall(NativeToolCall)
    case usage(input: Int, output: Int)
    case replay([[String: Any]])
    case finished
}

public protocol NativeModelClient {
    func stream(request: NativeModelRequest) -> AsyncThrowingStream<NativeModelEvent, Error>
}

// Never put provider-controlled bodies, URLs, headers, or underlying error descriptions
// in errors: any of those can echo credentials, including encoded versions of the key.
enum NativeModelClientError: Error, LocalizedError {
    case invalidRequest(String)
    case http(Int)
    case invalidResponse
    case malformedStream(String)
    case truncatedStream
    case providerFailure
    case limitExceeded
    case slowConsumer
    case transport(Int)

    var errorDescription: String? {
        switch self {
        case .invalidRequest(let detail): return "Invalid native model request: \(detail)."
        case .http(let status): return "Model provider returned HTTP \(status); response details withheld."
        case .invalidResponse: return "Model provider did not return an HTTP event stream."
        case .malformedStream(let detail): return "Invalid model event stream: \(detail)."
        case .truncatedStream: return "Model event stream ended without its successful terminal event."
        case .providerFailure: return "Model provider reported a failed or incomplete response."
        case .limitExceeded: return "Model stream exceeded the bounded output or tool argument limit."
        case .slowConsumer: return "Model stream consumer fell behind; events were not silently discarded."
        case .transport(let code): return "Model provider transport failed (code \(code)); details withheld."
        }
    }
}

private enum NativeModelLimits {
    static let frameBytes = 8 * 1024 * 1024
    static let outputBytes = 8 * 1024 * 1024
    static let argumentBytes = 1024 * 1024
    static let identifierBytes = 1024
    static let toolCalls = 128
    static let outputItems = 1024
    static let requestBytes = 16 * 1024 * 1024
}

/// Pure request construction, shared by the transport and isolated native-core tests.
enum NativeModelRequestBuilder {
    static func makeURLRequest(_ request: NativeModelRequest) throws -> URLRequest {
        guard var url = URLComponents(url: request.baseURL, resolvingAgainstBaseURL: false),
              url.scheme?.lowercased() == "https",
              let host = url.host?.lowercased(), !host.isEmpty,
              !isLoopback(host), url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil else {
            throw NativeModelClientError.invalidRequest("a remote HTTPS provider base URL is required")
        }
        // CRLF is a single Swift Character; inspect scalars so paired line breaks
        // cannot bypass header-injection validation.
        guard !request.model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !request.apiKey.isEmpty,
              !request.apiKey.unicodeScalars.contains(where: { $0.value == 13 || $0.value == 10 }) else {
            throw NativeModelClientError.invalidRequest("model and a valid API key are required")
        }
        if let cap = request.maxOutputTokens, cap <= 0 {
            throw NativeModelClientError.invalidRequest("maxOutputTokens must be positive")
        }
        var path = url.path
        while path.hasSuffix("/") { path.removeLast() }
        for suffix in ["/chat/completions", "/responses"] where path.hasSuffix(suffix) {
            path.removeLast(suffix.count)
            break
        }
        url.path = path + (request.format == .responses ? "/responses" : "/chat/completions")
        guard let endpoint = url.url else {
            throw NativeModelClientError.invalidRequest("invalid provider endpoint")
        }
        let tools = try request.tools.map { try tool($0, format: request.format) }
        var body: [String: Any] = ["model": request.model, "stream": true]
        if !tools.isEmpty { body["tools"] = tools }
        switch request.format {
        case .responses:
            if let cap = request.maxOutputTokens { body["max_output_tokens"] = cap }
            body["store"] = false
            // store:false reasoning must be explicitly requested for stateless replay.
            body["include"] = ["reasoning.encrypted_content"]
            if !request.system.isEmpty { body["instructions"] = request.system }
            body["input"] = try responseInput(request.messages)
            if let effort = request.effort {
                body["reasoning"] = ["effort": effort, "summary": "auto"]
            }
        case .chatCompletions:
            if let cap = request.maxOutputTokens { body["max_completion_tokens"] = cap }
            var messages: [[String: Any]] = []
            if !request.system.isEmpty { messages.append(["role": "system", "content": request.system]) }
            messages += try request.messages.map(chatMessage)
            body["messages"] = messages
            body["stream_options"] = ["include_usage": true]
            if let effort = request.effort { body["reasoning_effort"] = effort }
        }
        guard JSONSerialization.isValidJSONObject(body) else {
            throw NativeModelClientError.invalidRequest("request must contain valid JSON values")
        }
        let data: Data
        do { data = try JSONSerialization.data(withJSONObject: body) }
        catch { throw NativeModelClientError.invalidRequest("request JSON could not be encoded") }
        guard data.count <= NativeModelLimits.requestBytes else { throw NativeModelClientError.limitExceeded }
        var result = URLRequest(url: endpoint)
        result.httpMethod = "POST"
        result.httpBody = data
        result.timeoutInterval = 120
        result.cachePolicy = .reloadIgnoringLocalCacheData
        result.setValue("Bearer " + request.apiKey, forHTTPHeaderField: "Authorization")
        result.setValue("application/json", forHTTPHeaderField: "Content-Type")
        result.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        return result
    }

    private static func isLoopback(_ host: String) -> Bool {
        let value = host.trimmingCharacters(in: CharacterSet(charactersIn: "[]")).lowercased()
        return value == "localhost" || value.hasSuffix(".localhost") || value == "::1"
            || value == "0.0.0.0" || value.hasPrefix("127.")
            || value.hasPrefix("::ffff:127.") || value == "::ffff:7f00:1"
    }

    private static func tool(_ definition: [String: Any], format: NativeProviderFormat) throws -> [String: Any] {
        guard let name = definition["name"] as? String, !name.isEmpty,
              let parameters = definition["parameters"] as? [String: Any] else {
            throw NativeModelClientError.invalidRequest("tools require a name and parameters object")
        }
        var function: [String: Any] = ["name": name, "parameters": parameters]
        if let description = definition["description"] {
            guard let text = description as? String else {
                throw NativeModelClientError.invalidRequest("tool descriptions must be strings")
            }
            function["description"] = text
        }
        if format == .responses {
            function["type"] = "function"
            // Responses defaults to strict schemas, which would make optional
            // parameters required. Native tool definitions use ordinary JSON Schema.
            function["strict"] = false
            return function
        }
        return ["type": "function", "function": function]
    }

    private static func validateRole(_ role: String) throws {
        guard ["system", "developer", "user", "assistant", "tool"].contains(role) else {
            throw NativeModelClientError.invalidRequest("unsupported message role")
        }
    }

    private static func responseInput(_ messages: [NativeModelMessage]) throws -> [[String: Any]] {
        var input: [[String: Any]] = []
        for message in messages {
            try validateRole(message.role)
            if message.role == "tool" {
                guard let id = message.toolCallID, !id.isEmpty else {
                    throw NativeModelClientError.invalidRequest("tool output requires a call ID")
                }
                input.append(["type": "function_call_output", "call_id": id, "output": message.content])
            } else if !message.replayItems.isEmpty {
                // Replay is already provider-native output, not a message wrapper. Do not
                // duplicate the assistant text/calls represented by these same items.
                for item in message.replayItems {
                    guard let type = item["type"] as? String,
                          ["message", "function_call", "reasoning", "function_call_output"].contains(type) else {
                        throw NativeModelClientError.invalidRequest("unsupported Responses replay item")
                    }
                }
                input += message.replayItems
            } else {
                if !message.content.isEmpty || message.toolCalls.isEmpty {
                    input.append(["role": message.role, "content": message.content])
                }
                guard message.toolCalls.isEmpty || message.role == "assistant" else {
                    throw NativeModelClientError.invalidRequest("only assistants may call tools")
                }
                for call in message.toolCalls {
                    try validateCall(call)
                    input.append(["type": "function_call", "call_id": call.id,
                                  "name": call.name, "arguments": call.arguments])
                }
            }
        }
        return input
    }

    private static func chatMessage(_ message: NativeModelMessage) throws -> [String: Any] {
        try validateRole(message.role)
        var result: [String: Any] = ["role": message.role, "content": message.content]
        if message.role == "tool" {
            guard let id = message.toolCallID, !id.isEmpty else {
                throw NativeModelClientError.invalidRequest("tool output requires a call ID")
            }
            result["tool_call_id"] = id
        }
        if !message.toolCalls.isEmpty {
            guard message.role == "assistant" else {
                throw NativeModelClientError.invalidRequest("only assistants may call tools")
            }
            result["tool_calls"] = try message.toolCalls.map { call -> [String: Any] in
                try validateCall(call)
                return ["id": call.id, "type": "function",
                        "function": ["name": call.name, "arguments": call.arguments]]
            }
            if message.content.isEmpty { result["content"] = NSNull() }
        }
        return result
    }

    private static func validateCall(_ call: NativeToolCall) throws {
        guard !call.id.isEmpty, !call.name.isEmpty else {
            throw NativeModelClientError.invalidRequest("tool calls require an ID and name")
        }
    }
}

struct NativeSSEFrame {
    let event: String?
    let data: String
}

/// Byte-oriented framing preserves UTF-8 across arbitrary network fragmentation.
/// Only a complete blank-line-delimited event is dispatched, including at EOF.
struct NativeSSEParser {
    private var line: [UInt8] = []
    private var dataLines: [String] = []
    private var event: String?
    private var skipLF = false
    private var frameBytes = 0
    private var isFirstLine = true
    private let maximumFrameBytes: Int

    init(maximumFrameBytes: Int = 8 * 1024 * 1024) {
        self.maximumFrameBytes = maximumFrameBytes
    }

    mutating func feed(_ byte: UInt8) throws -> NativeSSEFrame? {
        if skipLF {
            skipLF = false
            if byte == 10 { return nil }
        }
        frameBytes += 1
        guard frameBytes <= maximumFrameBytes else { throw NativeModelClientError.limitExceeded }
        if byte == 13 || byte == 10 {
            skipLF = byte == 13
            return try endLine()
        }
        line.append(byte)
        return nil
    }

    func finish() throws {
        guard line.isEmpty, dataLines.isEmpty, event == nil else {
            throw NativeModelClientError.truncatedStream
        }
    }

    private mutating func endLine() throws -> NativeSSEFrame? {
        if line.isEmpty {
            let frame = dataLines.isEmpty ? nil : NativeSSEFrame(event: event, data: dataLines.joined(separator: "\n"))
            dataLines.removeAll(keepingCapacity: true)
            event = nil
            frameBytes = 0
            isFirstLine = false
            return frame
        }
        guard var text = String(bytes: line, encoding: .utf8) else {
            throw NativeModelClientError.malformedStream("invalid UTF-8")
        }
        if isFirstLine {
            isFirstLine = false
            if text.hasPrefix("\u{FEFF}") { text.removeFirst() }
        }
        line.removeAll(keepingCapacity: true)
        if text.hasPrefix(":") { return nil }
        let split = text.firstIndex(of: ":")
        let field = split.map { String(text[..<$0]) } ?? text
        var value = split.map { String(text[text.index(after: $0)...]) } ?? ""
        if value.hasPrefix(" ") { value.removeFirst() }
        switch field {
        case "data": dataLines.append(value)
        case "event": event = value
        default: break // id/retry are intentionally not used: billable POSTs are never replayed.
        }
        return nil
    }
}

private struct NativePendingToolCall {
    var id = ""
    var itemID = ""
    var name = ""
    var arguments = ""
    private(set) var argumentBytes = 0
    private var nameBytes = 0
    private(set) var hasArgumentDeltas = false
    private(set) var argumentsAreComplete = false
    private var didEmitPreview = false

    mutating func identify(id: String?, itemID: String?, name: String?) throws {
        if let id { try Self.setIdentifier(id, current: &self.id) }
        if let itemID { try Self.setIdentifier(itemID, current: &self.itemID) }
        if let name { try Self.setIdentifier(name, current: &self.name) }
    }

    private static func setIdentifier(_ value: String, current: inout String) throws {
        guard !value.isEmpty, value.utf8.count <= NativeModelLimits.identifierBytes,
              current.isEmpty || current == value else {
            throw NativeModelClientError.malformedStream("conflicting tool identity")
        }
        current = value
    }

    mutating func appendName(_ delta: String) throws {
        nameBytes += delta.utf8.count
        guard nameBytes <= NativeModelLimits.identifierBytes else { throw NativeModelClientError.limitExceeded }
        name += delta
    }

    mutating func appendArguments(_ delta: String) throws {
        guard !argumentsAreComplete else {
            throw NativeModelClientError.malformedStream("argument delta after completed arguments")
        }
        hasArgumentDeltas = true
        argumentBytes += delta.utf8.count
        guard argumentBytes <= NativeModelLimits.argumentBytes else { throw NativeModelClientError.limitExceeded }
        arguments += delta
    }

    mutating func setArguments(_ value: String, complete: Bool = false) throws {
        let count = value.utf8.count
        guard count <= NativeModelLimits.argumentBytes else { throw NativeModelClientError.limitExceeded }
        guard !(hasArgumentDeltas || argumentsAreComplete) || value == arguments else {
            throw NativeModelClientError.malformedStream("final tool arguments disagree with streamed arguments")
        }
        arguments = value
        argumentBytes = count
        argumentsAreComplete = argumentsAreComplete || complete
    }

    mutating func preview() -> NativeModelEvent? {
        guard !didEmitPreview, !id.isEmpty, !name.isEmpty else { return nil }
        didEmitPreview = true
        return .toolStarted(id: id, name: name)
    }

    func completed() throws -> NativeToolCall {
        guard !id.isEmpty, !name.isEmpty,
              let data = arguments.data(using: .utf8),
              (try? JSONSerialization.jsonObject(with: data)) is [String: Any] else {
            throw NativeModelClientError.malformedStream("incomplete tool call or invalid arguments object")
        }
        return NativeToolCall(id: id, name: name, arguments: arguments)
    }
}

/// Provider semantics are independent of networking, so truncation and interleaving
/// can be exercised deterministically without a live API or billable requests.
struct NativeModelStreamDecoder {
    let format: NativeProviderFormat
    private(set) var isFinished = false
    private var outputBytes = 0
    private var responseCalls: [Int: NativePendingToolCall] = [:]
    private var chatCalls: [Int: NativePendingToolCall] = [:]
    private var chatFinishReason: String?
    private var chatUsage: (Int, Int)?

    init(format: NativeProviderFormat) { self.format = format }

    mutating func consume(_ frame: NativeSSEFrame) throws -> [NativeModelEvent] {
        guard !isFinished else { return [] }
        let payload = frame.data.trimmingCharacters(in: .whitespacesAndNewlines)
        if payload.isEmpty { return [] }
        if payload == "[DONE]" {
            guard format == .chatCompletions else { throw NativeModelClientError.truncatedStream }
            return try completeChat()
        }
        guard frame.data.utf8.count <= NativeModelLimits.frameBytes else { throw NativeModelClientError.limitExceeded }
        guard let json = try? JSONSerialization.jsonObject(with: Data(frame.data.utf8)),
              let object = json as? [String: Any] else {
            throw NativeModelClientError.malformedStream("event is not a JSON object")
        }
        if object["error"] != nil || frame.event == "error" { throw NativeModelClientError.providerFailure }
        switch format {
        case .responses: return try consumeResponse(object, event: frame.event)
        case .chatCompletions: return try consumeChat(object)
        }
    }

    func finish() throws {
        guard isFinished else { throw NativeModelClientError.truncatedStream }
    }

    private mutating func recordOutput(_ text: String) throws {
        outputBytes += text.utf8.count
        guard outputBytes <= NativeModelLimits.outputBytes else { throw NativeModelClientError.limitExceeded }
    }

    private func index(_ object: [String: Any], key: String, maximum: Int) throws -> Int {
        guard let value = object[key] as? Int, value >= 0, value < maximum else {
            throw NativeModelClientError.malformedStream("missing or invalid stream index")
        }
        return value
    }

    private func usage(_ value: Any?, inputKey: String, outputKey: String) throws -> (Int, Int)? {
        guard let value, !(value is NSNull) else { return nil }
        guard let object = value as? [String: Any],
              let input = object[inputKey] as? Int, let output = object[outputKey] as? Int,
              input >= 0, output >= 0 else {
            throw NativeModelClientError.malformedStream("invalid usage counts")
        }
        return (input, output)
    }

    private mutating func consumeResponse(_ object: [String: Any], event: String?) throws -> [NativeModelEvent] {
        guard let type = object["type"] as? String ?? event else {
            throw NativeModelClientError.malformedStream("missing Responses event type")
        }
        var events: [NativeModelEvent] = []
        switch type {
        case "error", "response.failed", "response.incomplete", "response.cancelled":
            throw NativeModelClientError.providerFailure
        case "response.output_text.delta", "response.refusal.delta":
            guard let delta = object["delta"] as? String else {
                throw NativeModelClientError.malformedStream("missing text delta")
            }
            try recordOutput(delta)
            return [.text(delta)]
        case "response.reasoning_summary_text.delta", "response.reasoning_text.delta":
            guard let delta = object["delta"] as? String else {
                throw NativeModelClientError.malformedStream("missing reasoning delta")
            }
            try recordOutput(delta)
            return [.reasoning(delta)]
        case "response.output_item.added", "response.output_item.done":
            guard let item = object["item"] as? [String: Any] else {
                throw NativeModelClientError.malformedStream("missing output item")
            }
            if item["type"] as? String == "function_call" {
                let i = try index(object, key: "output_index", maximum: NativeModelLimits.outputItems)
                guard responseCalls[i] != nil || responseCalls.count < NativeModelLimits.toolCalls else {
                    throw NativeModelClientError.limitExceeded
                }
                try responseCalls[i, default: NativePendingToolCall()].identify(
                    id: item["call_id"] as? String, itemID: item["id"] as? String, name: item["name"] as? String)
                if let arguments = item["arguments"] as? String {
                    try responseCalls[i, default: NativePendingToolCall()].setArguments(
                        arguments, complete: type == "response.output_item.done")
                }
                if type == "response.output_item.added",
                   let preview = responseCalls[i, default: NativePendingToolCall()].preview() {
                    events.append(preview)
                }
            }
        case "response.function_call_arguments.delta", "response.function_call_arguments.done":
            let i = try index(object, key: "output_index", maximum: NativeModelLimits.outputItems)
            guard responseCalls[i] != nil || responseCalls.count < NativeModelLimits.toolCalls else {
                throw NativeModelClientError.limitExceeded
            }
            try responseCalls[i, default: NativePendingToolCall()].identify(
                id: nil, itemID: object["item_id"] as? String, name: nil)
            if type.hasSuffix(".delta") {
                guard let delta = object["delta"] as? String else {
                    throw NativeModelClientError.malformedStream("missing argument delta")
                }
                try recordOutput(delta)
                try responseCalls[i, default: NativePendingToolCall()].appendArguments(delta)
            } else {
                guard let arguments = object["arguments"] as? String else {
                    throw NativeModelClientError.malformedStream("missing completed arguments")
                }
                try responseCalls[i, default: NativePendingToolCall()].setArguments(arguments, complete: true)
            }
        case "response.completed":
            return try completeResponse(object)
        default: break // Lifecycle, annotations and non-text parts do not produce UI events.
        }
        guard responseCalls.values.reduce(0, { $0 + $1.argumentBytes }) <= NativeModelLimits.outputBytes else {
            throw NativeModelClientError.limitExceeded
        }
        return events
    }

    private mutating func completeResponse(_ object: [String: Any]) throws -> [NativeModelEvent] {
        guard let response = object["response"] as? [String: Any] else {
            throw NativeModelClientError.malformedStream("missing completed response")
        }
        guard response["status"] as? String == "completed",
              response["error"] == nil || response["error"] is NSNull,
              response["incomplete_details"] == nil || response["incomplete_details"] is NSNull else {
            throw NativeModelClientError.providerFailure
        }
        guard let output = response["output"] as? [[String: Any]],
              output.count <= NativeModelLimits.outputItems,
              JSONSerialization.isValidJSONObject(output) else {
            throw NativeModelClientError.malformedStream("missing final output array")
        }
        let replay = try JSONSerialization.data(withJSONObject: output)
        guard replay.count <= NativeModelLimits.outputBytes else { throw NativeModelClientError.limitExceeded }
        var events: [NativeModelEvent] = []
        var ids = Set<String>()
        for (i, item) in output.enumerated() where item["type"] as? String == "function_call" {
            guard let id = item["call_id"] as? String, let name = item["name"] as? String,
                  let arguments = item["arguments"] as? String,
                  ids.insert(id).inserted, ids.count <= NativeModelLimits.toolCalls else {
                throw NativeModelClientError.malformedStream("invalid final tool call")
            }
            var call = NativePendingToolCall()
            try call.identify(id: id, itemID: item["id"] as? String, name: name)
            try call.setArguments(arguments)
            if let pending = responseCalls[i] {
                if pending.hasArgumentDeltas || pending.argumentsAreComplete, pending.arguments != arguments {
                    throw NativeModelClientError.malformedStream("final tool arguments disagree with streamed arguments")
                }
                try call.identify(id: pending.id.isEmpty ? nil : pending.id,
                                  itemID: pending.itemID.isEmpty ? nil : pending.itemID,
                                  name: pending.name.isEmpty ? nil : pending.name)
            }
            events.append(.toolCall(try call.completed()))
        }
        // A tracked function cannot vanish from the authoritative final output.
        for i in responseCalls.keys {
            guard output.indices.contains(i), output[i]["type"] as? String == "function_call" else {
                throw NativeModelClientError.malformedStream("final response lost a tool call")
            }
        }
        if let counts = try usage(response["usage"], inputKey: "input_tokens", outputKey: "output_tokens") {
            events.append(.usage(input: counts.0, output: counts.1))
        }
        events.append(.replay(output))
        events.append(.finished)
        isFinished = true
        responseCalls.removeAll()
        return events
    }

    private mutating func consumeChat(_ object: [String: Any]) throws -> [NativeModelEvent] {
        if let counts = try usage(object["usage"], inputKey: "prompt_tokens", outputKey: "completion_tokens") {
            chatUsage = counts
        }
        guard let choices = object["choices"] as? [[String: Any]] else {
            throw NativeModelClientError.malformedStream("missing chat choices")
        }
        var events: [NativeModelEvent] = []
        for choice in choices {
            let choiceIndex = try index(choice, key: "index", maximum: NativeModelLimits.outputItems)
            if choiceIndex != 0 { continue } // Requests never ask for n > 1.
            guard chatFinishReason == nil else {
                throw NativeModelClientError.malformedStream("chat delta after finish reason")
            }
            guard let delta = choice["delta"] as? [String: Any] else {
                throw NativeModelClientError.malformedStream("missing chat delta")
            }
            for (key, reasoning) in [("reasoning_content", true), ("content", false)] {
                if let value = delta[key], !(value is NSNull) {
                    guard let text = value as? String else {
                        throw NativeModelClientError.malformedStream("invalid chat text delta")
                    }
                    try recordOutput(text)
                    if !text.isEmpty { events.append(reasoning ? .reasoning(text) : .text(text)) }
                }
            }
            if let value = delta["tool_calls"], !(value is NSNull) {
                guard let calls = value as? [[String: Any]] else {
                    throw NativeModelClientError.malformedStream("invalid chat tool deltas")
                }
                for part in calls {
                    if let preview = try accumulateChatCall(part) { events.append(preview) }
                }
            }
            if delta["function_call"] != nil {
                throw NativeModelClientError.malformedStream("legacy function_call is not supported; use tools")
            }
            if let reason = choice["finish_reason"] as? String {
                guard ["stop", "tool_calls"].contains(reason) else {
                    throw NativeModelClientError.providerFailure
                }
                chatFinishReason = reason
            }
        }
        return events
    }

    private mutating func accumulateChatCall(_ part: [String: Any]) throws -> NativeModelEvent? {
        let i: Int
        if part["index"] != nil {
            i = try index(part, key: "index", maximum: NativeModelLimits.toolCalls)
        } else if let id = part["id"] as? String,
                  let existing = chatCalls.first(where: { $0.value.id == id }) {
            i = existing.key
        } else {
            throw NativeModelClientError.malformedStream("tool delta has no index or known ID")
        }
        if let type = part["type"] as? String, type != "function" {
            throw NativeModelClientError.malformedStream("unsupported tool call type")
        }
        let id = part["id"] as? String
        if let id, chatCalls.contains(where: { $0.key != i && $0.value.id == id }) {
            throw NativeModelClientError.malformedStream("duplicate tool call ID")
        }
        let function = part["function"] as? [String: Any]
        try chatCalls[i, default: NativePendingToolCall()].identify(id: id, itemID: nil, name: nil)
        if let value = function?["name"] {
            guard let name = value as? String else {
                throw NativeModelClientError.malformedStream("invalid tool name delta")
            }
            try chatCalls[i, default: NativePendingToolCall()].appendName(name)
        }
        if let value = function?["arguments"] {
            guard let arguments = value as? String else {
                throw NativeModelClientError.malformedStream("invalid tool argument delta")
            }
            try recordOutput(arguments)
            try chatCalls[i, default: NativePendingToolCall()].appendArguments(arguments)
        }
        return chatCalls[i, default: NativePendingToolCall()].preview()
    }

    private mutating func completeChat() throws -> [NativeModelEvent] {
        guard let reason = chatFinishReason else { throw NativeModelClientError.truncatedStream }
        if reason == "tool_calls" && chatCalls.isEmpty {
            throw NativeModelClientError.malformedStream("tool finish reason without tool calls")
        }

        var calls: [NativeToolCall] = []
        for i in chatCalls.keys.sorted() {
            guard let call = chatCalls[i] else { continue }
            calls.append(try call.completed())
        }
        var events = calls.map { NativeModelEvent.toolCall($0) }
        if let counts = chatUsage { events.append(.usage(input: counts.0, output: counts.1)) }
        events.append(.finished)
        isFinished = true
        chatCalls.removeAll()
        return events
    }
}

// Redirects could leak a bearer key or replay a billable POST. Surface their HTTP
// status instead; callers must configure the actual remote provider endpoint.
private final class NativeModelRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

@available(iOS 15.0, macOS 12.0, *)
public final class URLSessionNativeModelClient: NativeModelClient {
    private let configuration: URLSessionConfiguration

    public init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = 120
        configuration.timeoutIntervalForResource = 600
        self.configuration = configuration
    }

    // URLProtocol injection for cancellation/HTTP tests; no production gateway.
    init(configuration: URLSessionConfiguration) { self.configuration = configuration }

    public func stream(request: NativeModelRequest) -> AsyncThrowingStream<NativeModelEvent, Error> {
        let configuration = self.configuration
        return AsyncThrowingStream(bufferingPolicy: .bufferingOldest(256)) { continuation in
            let task = Task {
                let session = URLSession(configuration: configuration,
                                         delegate: NativeModelRedirectDelegate(), delegateQueue: nil)
                defer { session.invalidateAndCancel() }
                do {
                    try await withTaskCancellationHandler(operation: {
                        try Task.checkCancellation()
                        let urlRequest = try NativeModelRequestBuilder.makeURLRequest(request)
                        let (bytes, response) = try await session.bytes(for: urlRequest)
                        guard let http = response as? HTTPURLResponse else {
                            throw NativeModelClientError.invalidResponse
                        }
                        guard (200..<300).contains(http.statusCode) else {
                            // No error-body read: bounded, fully redacted, and cancellation
                            // stops even a malicious never-ending HTTP error body.
                            throw NativeModelClientError.http(http.statusCode)
                        }
                        guard http.mimeType?.lowercased() == "text/event-stream" else {
                            throw NativeModelClientError.invalidResponse
                        }
                        var parser = NativeSSEParser()
                        var decoder = NativeModelStreamDecoder(format: request.format)
                        for try await byte in bytes {
                            try Task.checkCancellation()
                            guard let frame = try parser.feed(byte) else { continue }
                            let events = try decoder.consume(frame)
                            for event in events {
                                switch continuation.yield(event) {
                                case .enqueued: break
                                case .dropped: throw NativeModelClientError.slowConsumer
                                case .terminated: throw CancellationError()
                                @unknown default: throw NativeModelClientError.slowConsumer
                                }
                            }
                            if decoder.isFinished {
                                continuation.finish()
                                return
                            }
                        }
                        try parser.finish()
                        try decoder.finish()
                        continuation.finish()
                    }, onCancel: {
                        session.invalidateAndCancel()
                    })
                } catch {
                    if Task.isCancelled || error is CancellationError {
                        continuation.finish(throwing: CancellationError())
                    } else if let safe = error as? NativeModelClientError {
                        continuation.finish(throwing: safe)
                    } else {
                        continuation.finish(throwing: NativeModelClientError.transport((error as NSError).code))
                    }
                }
            }
            continuation.onTermination = { @Sendable _ in task.cancel() }
        }
    }
}
