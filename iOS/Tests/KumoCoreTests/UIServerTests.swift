import Foundation
import XCTest
@testable import KumoCore

final class UIServerTests: XCTestCase {

    func testStartBindsEphemeralPortAndServesMissingUIRoute() async throws {
        let server = UIServer(targetPort: 0, user: "test", password: "test")
        defer { server.stop() }
        XCTAssertEqual(server.port, 0)

        try server.start(preferredPort: 0)

        XCTAssertGreaterThan(server.port, 0)
        try await assertMissingUIRoute(server)
    }

    func testRepeatedStartKeepsReadyListener() async throws {
        let server = UIServer(targetPort: 0, user: "test", password: "test")
        defer { server.stop() }
        try server.start(preferredPort: 0)
        let readyPort = server.port

        try server.start(preferredPort: 0)

        XCTAssertGreaterThan(readyPort, 0)
        XCTAssertEqual(server.port, readyPort)
        try await assertMissingUIRoute(server)
    }

    func testStopResetsPortAndServerCanRestart() async throws {
        let server = UIServer(targetPort: 0, user: "test", password: "test")
        defer { server.stop() }
        server.stop()
        XCTAssertEqual(server.port, 0)
        try server.start(preferredPort: 0)
        XCTAssertGreaterThan(server.port, 0)
        try await assertMissingUIRoute(server)

        server.stop()
        XCTAssertEqual(server.port, 0)
        server.stop()
        XCTAssertEqual(server.port, 0)

        try server.start(preferredPort: 0)

        XCTAssertGreaterThan(server.port, 0)
        try await assertMissingUIRoute(server)
    }

    private func assertMissingUIRoute(_ server: UIServer,
                                      file: StaticString = #filePath, line: UInt = #line) async throws {
        let url = try XCTUnwrap(URL(string: "http://127.0.0.1:\(server.port)/_ui/missing"),
                                file: file, line: line)
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: 3)
        request.httpMethod = "GET"
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }

        let (data, response) = try await session.data(for: request)

        let http = try XCTUnwrap(response as? HTTPURLResponse, file: file, line: line)
        XCTAssertEqual(http.statusCode, 404, file: file, line: line)
        XCTAssertEqual(String(data: data, encoding: .utf8), "missing: missing", file: file, line: line)
    }
}
