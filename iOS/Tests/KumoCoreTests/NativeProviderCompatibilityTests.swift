import Foundation
import XCTest
@testable import KumoCore

final class NativeProviderCompatibilityTests: XCTestCase {
    private let profileFields = ["User-Agent", "originator", "version", "session_id"]

    private func assertProfile(_ request: URLRequest, file: StaticString = #filePath, line: UInt = #line) throws {
        XCTAssertEqual(request.value(forHTTPHeaderField: "User-Agent"),
                       "codex_cli_rs/0.46.0 (Ubuntu 22.4.0; x86_64) xterm-256color", file: file, line: line)
        XCTAssertEqual(request.value(forHTTPHeaderField: "originator"), "codex_cli_rs", file: file, line: line)
        XCTAssertEqual(request.value(forHTTPHeaderField: "version"), "0.46.0", file: file, line: line)
        let sessionID = try XCTUnwrap(request.value(forHTTPHeaderField: "session_id"), file: file, line: line)
        XCTAssertNotNil(UUID(uuidString: sessionID), file: file, line: line)
    }

    private func assertNoProfile(_ request: URLRequest, file: StaticString = #filePath, line: UInt = #line) {
        for field in profileFields {
            XCTAssertNil(request.value(forHTTPHeaderField: field), file: file, line: line)
        }
    }

    private func modelRequest(_ baseURL: String, format: NativeProviderFormat = .responses) throws -> NativeModelRequest {
        NativeModelRequest(baseURL: try XCTUnwrap(URL(string: baseURL)), apiKey: "test-credential",
                           format: format, model: "test-model", system: "Be helpful",
                           messages: [NativeModelMessage(role: "user", content: "Hello 🌍")], tools: [])
    }

    func testAppliesToExactHTTPSHostWithDefaultPortRegardlessOfPath() throws {
        for address in [
            "https://agentrouter.org", "https://agentrouter.org/v1/models",
            "https://agentrouter.org/v1/responses", "https://agentrouter.org/v1/chat/completions",
            "HTTPS://AGENTROUTER.ORG:443/v1/models",
            "https://agentrouter.org:443/other?model=test#fragment"
        ] {
            var request = URLRequest(url: try XCTUnwrap(URL(string: address)))
            NativeProviderCompatibility.apply(to: &request)
            try assertProfile(request)
        }
    }

    func testOtherHostsSchemesPortsAndUserInfoAreExactNoOps() throws {
        for address in [
            "https://freeinference.org/v1/responses", "https://api.freeinference.org/v1/models",
            "https://api.agentrouter.org/v1/models", "https://www.agentrouter.org/v1/models",
            "https://agentrouter.org.evil.example/v1/models", "https://evilagentrouter.org/v1/models",
            "https://agentrouter.org./v1/models", "https://evil.example/agentrouter.org?host=agentrouter.org",
            "https://localhost/v1/responses", "https://127.0.0.1/v1/responses", "https://[::1]/v1/models",
            "http://agentrouter.org/v1/models", "http://agentrouter.org:443/v1/models",
            "ftp://agentrouter.org/v1/models", "wss://agentrouter.org/v1/models",
            "https://agentrouter.org:80/v1/models", "https://agentrouter.org:444/v1/models",
            "https://agentrouter.org:8443/v1/models",
            "https://user@agentrouter.org/v1/models", "https://user:pass@agentrouter.org/v1/models",
            "https://:pass@agentrouter.org/v1/models", "https://@agentrouter.org/v1/models",
            "https://agentrouter.org@evil.example/v1/models"
        ] {
            var request = URLRequest(url: try XCTUnwrap(URL(string: address)))
            let withoutProfile = request
            NativeProviderCompatibility.apply(to: &request)
            XCTAssertEqual(request, withoutProfile, address)
            assertNoProfile(request)
            request.httpMethod = "POST"
            request.httpBody = Data("unchanged payload".utf8)
            request.setValue("Bearer test-credential", forHTTPHeaderField: "Authorization")
            for field in profileFields { request.setValue("existing-" + field, forHTTPHeaderField: field) }
            let original = request
            NativeProviderCompatibility.apply(to: &request)
            XCTAssertEqual(request, original, address)
        }
    }

    func testMissingURLIsANoOp() throws {
        var request = URLRequest(url: try XCTUnwrap(URL(string: "https://agentrouter.org")))
        request.url = nil
        let original = request
        NativeProviderCompatibility.apply(to: &request)
        XCTAssertEqual(request, original)
    }

    func testOnlyProfileHeadersChangeAndRepeatedApplicationIsIdempotent() throws {
        var request = URLRequest(url: try XCTUnwrap(URL(string: "https://agentrouter.org/v1/responses")))
        request.httpMethod = "POST"
        request.httpBody = Data([0, 255, 13, 10, 123, 125])
        request.timeoutInterval = 42
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("Bearer exact-test-credential", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue("keep-me", forHTTPHeaderField: "X-Custom")
        for field in profileFields { request.setValue("old-profile", forHTTPHeaderField: field.lowercased()) }
        let original = request

        NativeProviderCompatibility.apply(to: &request)
        try assertProfile(request)
        XCTAssertEqual(request.url, original.url)
        XCTAssertEqual(request.httpMethod, original.httpMethod)
        XCTAssertEqual(request.httpBody, original.httpBody)
        XCTAssertEqual(request.timeoutInterval, original.timeoutInterval)
        XCTAssertEqual(request.cachePolicy, original.cachePolicy)
        for field in ["Authorization", "Content-Type", "Accept", "X-Custom"] {
            XCTAssertEqual(request.value(forHTTPHeaderField: field), original.value(forHTTPHeaderField: field))
        }
        XCTAssertEqual(request.allHTTPHeaderFields?.count, original.allHTTPHeaderFields?.count)
        let applied = request
        NativeProviderCompatibility.apply(to: &request)
        XCTAssertEqual(request, applied)
    }

    func testBodyStreamIsPreserved() throws {
        var request = URLRequest(url: try XCTUnwrap(URL(string: "https://agentrouter.org/v1/responses")))
        let stream = InputStream(data: Data("streamed body".utf8))
        request.httpBodyStream = stream
        NativeProviderCompatibility.apply(to: &request)
        try assertProfile(request)
        XCTAssertTrue(request.httpBodyStream === stream)
        XCTAssertNil(request.httpBody)
    }

    func testBuilderAddsProfileWithoutChangingResponsesOrChatProtocol() throws {
        var sessionIDs: [String] = []
        for format in [NativeProviderFormat.responses, .chatCompletions] {
            let request = try modelRequest("https://agentrouter.org/v1/", format: format)
            let built = try NativeModelRequestBuilder.makeURLRequest(request)
            try assertProfile(built)
            sessionIDs.append(try XCTUnwrap(built.value(forHTTPHeaderField: "session_id")))
            XCTAssertEqual(built.url?.path, format == .responses ? "/v1/responses" : "/v1/chat/completions")
            XCTAssertEqual(built.httpMethod, "POST")
            XCTAssertEqual(built.value(forHTTPHeaderField: "Authorization"), "Bearer test-credential")
            XCTAssertEqual(built.value(forHTTPHeaderField: "Content-Type"), "application/json")
            XCTAssertEqual(built.value(forHTTPHeaderField: "Accept"), "text/event-stream")

            var neutral = request
            neutral.baseURL = try XCTUnwrap(URL(string: "https://provider.example/v1/"))
            let standard = try NativeModelRequestBuilder.makeURLRequest(neutral)
            assertNoProfile(standard)
            let actualBody = try JSONSerialization.jsonObject(with: XCTUnwrap(built.httpBody)) as? NSDictionary
            let standardBody = try JSONSerialization.jsonObject(with: XCTUnwrap(standard.httpBody)) as? NSDictionary
            XCTAssertNotNil(actualBody)
            XCTAssertEqual(actualBody, standardBody)
            let repeated = try NativeModelRequestBuilder.makeURLRequest(request)
            sessionIDs.append(try XCTUnwrap(repeated.value(forHTTPHeaderField: "session_id")))
        }
        XCTAssertEqual(Set(sessionIDs).count, 1, "Model requests must share one process session across turns and formats")
    }

    func testBuilderDoesNotAddProfileToOtherProvidersOrNonDefaultPort() throws {
        for address in ["https://freeinference.org/v1/", "https://api.agentrouter.org/v1/",
                        "https://agentrouter.org.evil.example/v1/", "https://agentrouter.org:8443/v1/"] {
            for format in [NativeProviderFormat.responses, .chatCompletions] {
                let built = try NativeModelRequestBuilder.makeURLRequest(modelRequest(address, format: format))
                assertNoProfile(built)
            }
        }
    }

    private final class Credentials {
        var values: [String: String] = [:]
        func read(_ id: String) throws -> String? { values[id] }
        func write(_ id: String, _ value: String?) throws { values[id] = value }
    }

    private struct NoNetworkModel: NativeModelClient {
        func stream(request: NativeModelRequest) -> AsyncThrowingStream<NativeModelEvent, Error> {
            XCTFail("Building a catalog request must not start model generation")
            return AsyncThrowingStream { $0.finish() }
        }
    }

    private func backend() throws -> (NativeAgentBackend, Credentials, URL) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("native-compatibility-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let credentials = Credentials()
        let backend = try NativeAgentBackend(root: root, modelClient: NoNetworkModel(),
                                             credentialRead: credentials.read, credentialWrite: credentials.write)
        addTeardownBlock {
            await backend.shutdown()
            try? FileManager.default.removeItem(at: root)
        }
        return (backend, credentials, root)
    }

    private func configure(_ backend: NativeAgentBackend, baseURL: String, npm: String = "@ai-sdk/openai") async throws {
        _ = try await backend.request(method: "PATCH", path: "/global/config", directory: nil, body: [
            "provider": ["test": ["name": "Compatibility fixture", "npm": npm,
                                    "options": ["baseURL": baseURL], "models": [String: [String: Any]]()]]
        ])
    }

    func testCatalogUsesDesktopProfileAndSharesProcessSessionWithModelRequests() async throws {
        let (backend, credentials, root) = try backend()
        credentials.values["test"] = UUID().uuidString
        let model = try NativeModelRequestBuilder.makeURLRequest(modelRequest("https://agentrouter.org/v1/"))
        let sessionID = try XCTUnwrap(model.value(forHTTPHeaderField: "session_id"))
        for npm in ["@ai-sdk/openai", "@ai-sdk/openai-compatible"] {
            for address in ["https://agentrouter.org/v1/", "https://AGENTROUTER.ORG:443/v1/"] {
                try await configure(backend, baseURL: address, npm: npm)
                let snapshot = root.appendingPathComponent("native-agent/state.json")
                let before = try Data(contentsOf: snapshot)
                let catalog = try await backend.modelCatalogRequest(providerID: "test")
                try assertProfile(catalog)
                XCTAssertEqual(catalog.value(forHTTPHeaderField: "session_id"), sessionID)
                XCTAssertEqual(catalog.url?.path, "/v1/models")
                XCTAssertEqual(catalog.httpMethod, "GET")
                XCTAssertEqual(catalog.value(forHTTPHeaderField: "Authorization"), "Bearer " + credentials.values["test"]!)
                XCTAssertEqual(catalog.value(forHTTPHeaderField: "Accept"), "application/json")
                XCTAssertEqual(catalog.cachePolicy, .reloadIgnoringLocalCacheData)
                XCTAssertNil(catalog.httpBody)
                XCTAssertNil(catalog.value(forHTTPHeaderField: "Content-Type"))
                XCTAssertEqual(try Data(contentsOf: snapshot), before)
            }
        }
    }

    func testCatalogDoesNotAddProfileToOtherProvidersOrNonDefaultPort() async throws {
        let (backend, credentials, _) = try backend()
        credentials.values["test"] = UUID().uuidString
        for address in ["https://freeinference.org/v1/", "https://api.agentrouter.org/v1/",
                        "https://agentrouter.org.evil.example/v1/", "https://agentrouter.org:8443/v1/"] {
            try await configure(backend, baseURL: address)
            let catalog = try await backend.modelCatalogRequest(providerID: "test")
            assertNoProfile(catalog)
            XCTAssertEqual(catalog.value(forHTTPHeaderField: "Authorization"), "Bearer " + credentials.values["test"]!)
            XCTAssertEqual(catalog.value(forHTTPHeaderField: "Accept"), "application/json")
            XCTAssertEqual(catalog.url?.path, "/v1/models")
        }
    }
}
