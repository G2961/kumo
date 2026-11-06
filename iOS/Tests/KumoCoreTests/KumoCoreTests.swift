import XCTest
@testable import KumoCore

final class KumoCoreTests: XCTestCase {

    func testLogRingBufferBounded() {
        let log = KumoLog()
        for i in 0..<600 {
            log.append("line \(i)")
        }
        // Bounded at 500 entries.
        let snapshot = log.snapshot
        XCTAssertFalse(snapshot.contains("line 0"))
        XCTAssertTrue(snapshot.contains("line 599"))
    }

    func testPasswordUrlSafe() {
        let pw = LocalRuntime.loadOrCreatePassword()
        XCTAssertFalse(pw.contains("+"))
        XCTAssertFalse(pw.contains("/"))
        XCTAssertFalse(pw.contains("="))
        XCTAssertFalse(pw.isEmpty)
    }

    func testProjectCreateAndList() {
        let root = ProjectManager.projectsRoot()
        let dir = ProjectManager.createNew(name: "test-\(UUID().uuidString.prefix(8))")
        XCTAssertTrue(ProjectManager.isDirectory(dir))
        XCTAssertTrue(dir.path.hasPrefix(root.path))
        let list = ProjectManager.list()
        XCTAssertTrue(list.contains { $0.path == dir.path })
    }

    func testConfigJSONIsValidJSON() {
        let json = LocalRuntime.configJSON()
        let data = json.data(using: .utf8)!
        let obj = try? JSONSerialization.jsonObject(with: data)
        XCTAssertNotNil(obj)
    }

    func testConfigJSONWithMcp() {
        let json = LocalRuntime.configJSON(mcpURL: "http://127.0.0.1:9999/mcp",
                                           mcpToken: "abc123")
        let obj = try? JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]
        XCTAssertNotNil(obj)
        let mcp = obj?["mcp"] as? [String: Any]
        XCTAssertNotNil(mcp)
    }

    func testSshPatchBasic() {
        let r = SshClient.patch(file: "a.txt", before: "one\ntwo\n", after: "one\nTWO\n")
        XCTAssertEqual(r["path"] as? String, "a.txt")
        let add = r["additions"] as? Int ?? -1
        let del = r["deletions"] as? Int ?? -1
        XCTAssertEqual(add, 1)
        XCTAssertEqual(del, 1)
    }
}