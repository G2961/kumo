import XCTest
@testable import KumoCore

// MARK: - SshClient (diff / redact / validation)

final class SshClientTests: XCTestCase {

    func testPatchIdenticalFiles() {
        let r = SshClient.patch(file: "x.txt", before: "hello\n", after: "hello\n")
        XCTAssertEqual(r["additions"] as? Int, 0)
        XCTAssertEqual(r["deletions"] as? Int, 0)
        XCTAssertEqual((r["patch"] as? String) ?? "", "")
    }

    func testPatchEmptyBefore() {
        let r = SshClient.patch(file: "new.txt", before: "", after: "line1\nline2\n")
        XCTAssertEqual(r["additions"] as? Int, 2)
        XCTAssertEqual(r["deletions"] as? Int, 0)
        let patch = r["patch"] as? String ?? ""
        XCTAssertTrue(patch.hasPrefix("--- new.txt"))
        XCTAssertTrue(patch.contains("@@" ))
    }

    func testPatchEmptyAfter() {
        let r = SshClient.patch(file: "gone.txt", before: "a\nb\n", after: "")
        XCTAssertEqual(r["additions"] as? Int, 0)
        XCTAssertEqual(r["deletions"] as? Int, 2)
    }

    func testPatchSingleChange() {
        let r = SshClient.patch(file: "a.txt", before: "one\ntwo\nthree\n",
                                after: "one\nTWO\nthree\n")
        XCTAssertEqual(r["additions"] as? Int, 1)
        XCTAssertEqual(r["deletions"] as? Int, 1)
        let patch = r["patch"] as? String ?? ""
        XCTAssertTrue(patch.contains("+TWO"))
        XCTAssertTrue(patch.contains("-two"))
    }

    func testPatchLargeInputFallsBackToReplace() {
        let a = (0..<2000).map { "row-\($0)-aaa" }.joined(separator: "\n") + "\n"
        let b = (0..<2000).map { "row-\($0)-bbb" }.joined(separator: "\n") + "\n"
        let r = SshClient.patch(file: "big.txt", before: a, after: b)
        XCTAssertEqual(r["deletions"] as? Int, 2000)
        XCTAssertEqual(r["additions"] as? Int, 2000)
    }

    // MARK: Redact

    func testRedactPassword() {
        let conn = makeConn(password: "s3cret", passphrase: "")
        let out = SshClient.redact("connect with s3cret now", conn)
        XCTAssertFalse(out.contains("s3cret"))
        XCTAssertTrue(out.contains("connect with [redacted] now"))
    }

    func testRedactBearerAndSk() {
        let conn = makeConn(password: "", passphrase: "")
        let out = SshClient.redact("auth: Bearer abc.def ghi; key sk-12345abcdef", conn)
        XCTAssertFalse(out.contains("Bearer abc.def"))
        XCTAssertFalse(out.contains("sk-12345abcdef"))
        XCTAssertTrue(out.contains("[redacted]"))
    }

    // MARK: Validation

    func testValidationAcceptsValidHost() {
        let c = makeConn(host: "192.168.1.10", user: "root")
        XCTAssertNoThrow(try SshClient.validated(c))
    }

    func testValidationRejectsBadHost() {
        let c = makeConn(host: "bad host with space", user: "u")
        XCTAssertThrowsError(try SshClient.validated(c))
    }

    func testValidationFillsDefaults() throws {
        var c = makeConn(host: "example.com", user: "alice")
        c.id = ""
        c.name = ""
        let v = try SshClient.validated(c)
        XCTAssertFalse(v.id.isEmpty)
        XCTAssertEqual(v.name, "alice@example.com")
    }

    func testValidationRejectsBadPort() {
        var c = makeConn(host: "h", user: "u")
        c.port = 99999
        XCTAssertThrowsError(try SshClient.validated(c))
    }

    func testValidationRejectsNewlineInDirectory() {
        var c = makeConn(host: "h", user: "u")
        c.directory = "foo\nbar"
        XCTAssertThrowsError(try SshClient.validated(c))
    }

    private func makeConn(host: String = "h", user: String = "u",
                          password: String = "", passphrase: String = "") -> SshClient.Connection {
        SshClient.Connection(id: "id", name: "n", host: host, user: user, port: 22,
                             directory: "", fingerprint: "",
                             password: password.isEmpty ? nil : password,
                             privateKey: "", passphrase: passphrase)
    }
}

// MARK: - ProjectManager

final class ProjectManagerTests: XCTestCase {

    func testSanitizePreservesSafeName() {
        let clean = ProjectManager.sanitize("my-project_2.v1")
        XCTAssertEqual(clean ?? "", "my-project_2.v1")
    }

    func testUniqueDirectoryAppendsNumber() {
        let base = ProjectManager.projectsRoot()
        let name = "collision"
        let dir1 = ProjectManager.uniqueDirectory(base: base, name: name)
        try? FileManager.default.createDirectory(at: dir1, withIntermediateDirectories: true)
        let dir2 = ProjectManager.uniqueDirectory(base: base, name: name)
        XCTAssertNotEqual(dir1.path, dir2.path)
        // Remove the dir1 we created so we don't clutter the projects list.
        try? FileManager.default.removeItem(at: dir1)
        XCTAssertTrue(dir1.lastPathComponent.hasPrefix("collision"))
    }

    func testCanWriteIsTrueInSandbox() {
        let root = ProjectManager.projectsRoot()
        XCTAssertTrue(ProjectManager.canWrite(root))
    }

    func testHomeDirExists() {
        let home = ProjectManager.homeDir()
        var isDir: ObjCBool = false
        XCTAssertTrue(FileManager.default.fileExists(atPath: home.path, isDirectory: &isDir))
        XCTAssertTrue(isDir.boolValue)
    }
}

// MARK: - KumoLog extras

final class KumoLogExtrasTests: XCTestCase {

    func testLogInvokesObserver() {
        let log = KumoLog()
        var seen: [String] = []
        log.onLine = { seen.append($0) }
        log.append("a")
        log.append("b")
        XCTAssertEqual(seen, ["a", "b"])
    }

    func testLogClear() {
        let log = KumoLog()
        log.append("x")
        log.clear()
        XCTAssertTrue(log.snapshot.isEmpty)
    }
}

// MARK: - ConfigJSON

final class ConfigJSONTests: XCTestCase {

    func testConfigWithoutMcpHasNoMcp() {
        let json = LocalRuntime.configJSON()
        let obj = try! JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any]
        XCTAssertNil(obj["mcp"])
    }

    func testConfigMcpCarriesBearerAuth() {
        let json = LocalRuntime.configJSON(mcpURL: "http://127.0.0.1:1234/mcp", mcpToken: "tok")
        let obj = try! JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any]
        let mcp = obj["mcp"] as! [String: Any]
        let mobile = mcp["mobile_ssh"] as! [String: Any]
        let headers = mobile["headers"] as! [String: Any]
        XCTAssertEqual(headers["Authorization"] as? String, "Bearer tok")
        XCTAssertEqual(mobile["type"] as? String, "remote")
        XCTAssertEqual(mobile["timeout"] as? Int, 900000)
    }

    func testConfigPermissionsForSsh() {
        let json = LocalRuntime.configJSON(mcpURL: "x", mcpToken: "y")
        let obj = try! JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any]
        let permission = obj["permission"] as! [String: Any]
        XCTAssertEqual(permission["mobile_ssh_ssh_exec"] as? String, "ask")
        XCTAssertEqual(permission["mobile_ssh_ssh_write_file"] as? String, "ask")
    }
}