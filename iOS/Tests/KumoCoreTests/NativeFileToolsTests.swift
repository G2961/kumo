import Foundation
import XCTest
@testable import KumoCore

final class NativeFileToolsTests: XCTestCase {
    private var root: URL!
    private var project: URL!
    private var ledgerRoot: URL!
    private var ledger: NativePatchLedger!
    private var tools: NativeFileTools!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.resolvingSymlinksInPath()
            .appendingPathComponent(UUID().uuidString)
        project = root.appendingPathComponent("project")
        ledgerRoot = root.appendingPathComponent("history")
        try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)
        ledger = try NativePatchLedger(root: ledgerRoot)
        tools = NativeFileTools(history: ledger)
    }

    override func tearDownWithError() throws {
        tools = nil
        ledger = nil
        try FileManager.default.removeItem(at: root)
    }

    @discardableResult
    private func put(_ name: String, _ content: String) throws -> URL {
        let url = project.appendingPathComponent(name)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(content.utf8).write(to: url)
        return url
    }

    private func prepare(_ name: String, _ arguments: [String: Any], turn: String = "user-1") throws -> NativePreparedTool {
        try tools.prepare(name: name, arguments: arguments, directory: project.path,
                          sessionID: "session-1", turnID: turn)
    }

    private func turns(_ history: NativePatchLedger? = nil, session: String = "session-1") throws -> [[String: Any]] {
        let result = try (history ?? ledger!).action("historyList", data: ["directory": project.path, "sessionID": session])
        return try XCTUnwrap(result["turns"] as? [[String: Any]])
    }

    private func show(_ turn: [String: Any], history: NativePatchLedger? = nil) throws -> [[String: Any]] {
        let token = try XCTUnwrap(turn["commit"] as? String)
        let result = try (history ?? ledger!).action("historyShow", data: [
            "directory": project.path, "sessionID": "session-1", "commit": token
        ])
        return try XCTUnwrap(result["files"] as? [[String: Any]])
    }

    func testDefinitionsAreNeutralJSONSchemasAndCannotExecuteCommands() throws {
        let definitions = tools.definitions
        XCTAssertEqual(Set(definitions.compactMap { $0["name"] as? String }),
                       Set(["list", "read", "glob", "grep", "write", "edit"]))
        for definition in definitions {
            XCTAssertEqual(Set(definition.keys), Set(["name", "description", "parameters"]))
            XCTAssertTrue(JSONSerialization.isValidJSONObject(definition))
            let schema = try XCTUnwrap(definition["parameters"] as? [String: Any])
            XCTAssertEqual(schema["type"] as? String, "object")
        }
        for name in ["exec", "bash", "shell", "OpenCode", "historyRecord"] {
            XCTAssertThrowsError(try prepare(name, ["command": "touch should-not-exist"]))
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: project.appendingPathComponent("should-not-exist").path))
    }

    func testReadPaginationAndPreparedSnapshot() throws {
        let file = try put("notes.txt", "one\ntwo\nthree\n")
        let prepared = try prepare("read", ["filePath": "notes.txt", "offset": 2, "limit": 1])
        XCTAssertFalse(prepared.modifies)
        try Data("user changed this\n".utf8).write(to: file)
        let result = try prepared.execute()
        XCTAssertTrue(result.output.contains("2: two"))
        XCTAssertFalse(result.output.contains("three"))
        XCTAssertEqual(result.metadata["truncated"] as? Bool, true)
    }

    func testAbsoluteAndParentRelativePathsAreNotProjectRestricted() throws {
        let outside = root.appendingPathComponent("outside.txt")
        try Data("outside\n".utf8).write(to: outside)
        XCTAssertTrue(try prepare("read", ["filePath": outside.path]).execute().output.contains("outside"))
        XCTAssertTrue(try prepare("read", ["filePath": "../outside.txt"]).execute().output.contains("outside"))
        _ = try prepare("write", ["filePath": outside.path, "content": "updated\n"]).execute()
        XCTAssertEqual(try String(contentsOf: outside, encoding: .utf8), "updated\n")
    }

    func testListGlobAndUnicodeRegexSkipGitAndSymlinkLoops() throws {
        _ = try put("a.txt", "hello café\n")
        _ = try put("nested/b.txt", "hello 世界\n")
        _ = try put("nested/no.md", "ignored\n")
        _ = try put(".git/secret.txt", "hello secret\n")
        try FileManager.default.createSymbolicLink(at: project.appendingPathComponent("nested/loop"), withDestinationURL: project)
        let list = try prepare("list", [:]).execute()
        XCTAssertTrue(list.output.contains("nested/"))
        let glob = try prepare("glob", ["pattern": "**/*.txt"]).execute()
        XCTAssertTrue(glob.output.contains("a.txt"))
        XCTAssertTrue(glob.output.contains("nested/b.txt"))
        XCTAssertFalse(glob.output.contains("secret.txt"))
        XCTAssertFalse(glob.output.contains("loop/"))
        let grep = try prepare("grep", ["pattern": "hello (café|世界)"]).execute()
        XCTAssertTrue(grep.output.contains("a.txt:1:hello café"))
        XCTAssertTrue(grep.output.contains("b.txt:1:hello 世界"))
        XCTAssertFalse(grep.output.contains("secret"))
        XCTAssertThrowsError(try prepare("grep", ["pattern": "["]))
    }

    func testEditApprovalContainsActualDiffAndOnlyExecutionRecordsHistory() throws {
        let file = try put("a.txt", "one\ntwo\nthree\n")
        let prepared = try prepare("edit", ["filePath": "a.txt", "oldString": "two", "newString": "TWO"])
        XCTAssertTrue(prepared.modifies)
        XCTAssertEqual(prepared.patterns, [file.standardizedFileURL.path])
        let patch = try XCTUnwrap(prepared.metadata["diff"] as? String)
        XCTAssertTrue(patch.contains("--- "))
        XCTAssertTrue(patch.contains("+++ "))
        XCTAssertTrue(patch.contains("@@ "))
        XCTAssertTrue(patch.contains("-two\n+TWO\n"))
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "one\ntwo\nthree\n")
        XCTAssertTrue(try turns().isEmpty)
        let result = try prepared.execute()
        XCTAssertEqual(result.metadata["saved"] as? Bool, true)
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "one\nTWO\nthree\n")
        let turn = try XCTUnwrap(try turns().first)
        let change = try XCTUnwrap(try show(turn).first)
        XCTAssertEqual(change["patch"] as? String, patch)
        XCTAssertEqual(change["source"] as? String, "local")
        XCTAssertEqual(change["connectionID"] as? String, "")
        XCTAssertEqual(change["additions"] as? Int, 1)
        XCTAssertEqual(change["deletions"] as? Int, 1)
    }

    func testAmbiguousMissingAndEmptyEditsAreRejected() throws {
        let file = try put("a.txt", "same same")
        XCTAssertThrowsError(try prepare("edit", ["filePath": "a.txt", "oldString": "same", "newString": "new"]))
        XCTAssertThrowsError(try prepare("edit", ["filePath": "a.txt", "oldString": "missing", "newString": "new"]))
        XCTAssertThrowsError(try prepare("edit", ["filePath": "a.txt", "oldString": "", "newString": "new"]))
        _ = try prepare("edit", ["filePath": "a.txt", "oldString": "same", "newString": "new", "replaceAll": true]).execute()
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "new new")
    }

    func testUserEditAfterApprovalRefusesWriteAndDoesNotRecord() throws {
        let file = try put("a.txt", "before\n")
        let approved = try prepare("write", ["filePath": "a.txt", "content": "agent\n"])
        try Data("user\n".utf8).write(to: file, options: .atomic)
        XCTAssertThrowsError(try approved.execute()) { error in
            XCTAssertTrue(error.localizedDescription.lowercased().contains("changed"))
        }
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "user\n")
        XCTAssertTrue(try turns().isEmpty)
    }

    func testNewFileAppearingDuringApprovalIsNotOverwritten() throws {
        let approved = try prepare("write", ["filePath": "new.txt", "content": "agent"])
        let file = try put("new.txt", "user")
        XCTAssertThrowsError(try approved.execute())
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "user")
        XCTAssertTrue(try turns().isEmpty)
    }

    func testSymlinkRetargetDuringApprovalIsRefused() throws {
        let first = try put("first.txt", "first")
        let second = try put("second.txt", "second")
        let link = project.appendingPathComponent("link.txt")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: first)
        let approved = try prepare("write", ["filePath": "link.txt", "content": "agent"])
        try FileManager.default.removeItem(at: link)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: second)
        XCTAssertThrowsError(try approved.execute())
        XCTAssertEqual(try String(contentsOf: first, encoding: .utf8), "first")
        XCTAssertEqual(try String(contentsOf: second, encoding: .utf8), "second")
    }

    func testDirectSymlinkWriteChangesTargetWithoutReplacingLink() throws {
        let file = try put("target.txt", "old")
        let link = project.appendingPathComponent("link.txt")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: file)
        _ = try prepare("write", ["filePath": "link.txt", "content": "new"]).execute()
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "new")
        XCTAssertEqual(try FileManager.default.destinationOfSymbolicLink(atPath: link.path), file.path)
    }

    func testFailedWriteLeavesNoBogusHistoryOrSavedResult() throws {
        let approved = try prepare("write", ["filePath": "missing-parent/new.txt", "content": "agent"])
        XCTAssertThrowsError(try approved.execute())
        XCTAssertTrue(try turns().isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: project.appendingPathComponent("missing-parent/new.txt").path))
    }

    func testReadOnlyParentReportsRealOSFailure() throws {
        let directory = project.appendingPathComponent("readonly")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        let approved = try prepare("write", ["filePath": "readonly/new.txt", "content": "agent"])
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: directory.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path) }
        if FileManager.default.isWritableFile(atPath: directory.path) {
            throw XCTSkip("This test process can bypass filesystem permissions")
        }
        XCTAssertThrowsError(try approved.execute())
        XCTAssertTrue(try turns().isEmpty)
    }

    func testAtomicWritePreservesPermissionsAndExistingOpenSnapshot() throws {
        let file = try put("a.txt", "before")
        try FileManager.default.setAttributes([.posixPermissions: 0o640], ofItemAtPath: file.path)
        let openSnapshot = try FileHandle(forReadingFrom: file)
        defer { try? openSnapshot.close() }
        let approved = try prepare("write", ["filePath": "a.txt", "content": "after"])
        _ = try approved.execute()
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "after")
        XCTAssertEqual(try openSnapshot.readToEnd(), Data("before".utf8))
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o640)
        XCTAssertFalse(try FileManager.default.contentsOfDirectory(atPath: project.path).contains { $0.hasPrefix(".native-write-") })
        XCTAssertThrowsError(try approved.execute())
        XCTAssertEqual(try turns().count, 1)
    }

    func testPersistenceAndTurnAggregationKeepEveryToolPatch() throws {
        _ = try put("a.txt", "one\n")
        _ = try prepare("edit", ["filePath": "a.txt", "oldString": "one", "newString": "two"]).execute()
        _ = try prepare("edit", ["filePath": "a.txt", "oldString": "two", "newString": "three"]).execute()
        _ = try prepare("write", ["filePath": "b.txt", "content": "other\n"], turn: "user-2").execute()
        let restarted = try NativePatchLedger(root: ledgerRoot)
        let stored = try turns(restarted)
        XCTAssertEqual(stored.count, 2)
        let first = try XCTUnwrap(stored.first { $0["id"] as? String == "user-1" })
        XCTAssertTrue(try XCTUnwrap(first["commit"] as? String).hasPrefix("native:"))
        let changes = try show(first, history: restarted)
        XCTAssertEqual(changes.count, 2)
        XCTAssertNotEqual(changes[0]["key"] as? String, changes[1]["key"] as? String)
        XCTAssertTrue(try XCTUnwrap(changes[0]["patch"] as? String).contains("+two"))
        XCTAssertTrue(try XCTUnwrap(changes[1]["patch"] as? String).contains("+three"))
        XCTAssertTrue(try turns(restarted, session: "another-session").isEmpty)
        XCTAssertThrowsError(try restarted.action("historyShow", data: [
            "directory": project.path, "sessionID": "another-session", "commit": first["commit"]!
        ]))
        XCTAssertFalse(FileManager.default.fileExists(atPath: project.appendingPathComponent(".git").path))
    }

    func testClientHistoryAndHintsCannotFabricatePatches() throws {
        XCTAssertEqual(try ledger.action("historyRegister", data: ["directory": project.path])["registered"] as? Bool, true)
        _ = try ledger.action("historyHint", data: ["directory": project.path, "sessionID": "session-1", "key": "fake", "patch": "+invented"])
        _ = try ledger.action("historyRecord", data: ["directory": project.path, "sessionID": "session-1", "turns": [
            ["id": "fake", "commit": "deadbeef", "files": [["file": "fake.txt", "patch": "+invented"]]]
        ]])
        XCTAssertTrue(try turns().isEmpty)
        XCTAssertThrowsError(try ledger.action("historyShow", data: ["directory": project.path, "sessionID": "session-1", "commit": "deadbeef"]))
    }

    func testMultipleLedgerInstancesDoNotLoseRecords() throws {
        let second = try NativePatchLedger(root: ledgerRoot)
        try ledger.append(directory: project.path, sessionID: "session-1", turnID: "user-1", title: "First",
                          file: "a.txt", before: nil, after: "a\n", patch: "--- /dev/null\n+++ a.txt\n@@ -0,0 +1,1 @@\n+a\n")
        try second.append(directory: project.path, sessionID: "session-1", turnID: "user-2", title: "Second",
                          file: "b.txt", before: nil, after: "b\n", patch: "--- /dev/null\n+++ b.txt\n@@ -0,0 +1,1 @@\n+b\n")
        XCTAssertEqual(try turns().count, 2)
    }

    func testBinaryAndOversizedFilesAreNeverDestructivelyEdited() throws {
        let binary = project.appendingPathComponent("binary")
        try Data([0, 255, 1]).write(to: binary)
        XCTAssertThrowsError(try prepare("read", ["filePath": "binary"]))
        XCTAssertThrowsError(try prepare("write", ["filePath": "binary", "content": "destroy"]))
        XCTAssertEqual(try Data(contentsOf: binary), Data([0, 255, 1]))
        _ = try put("large", String(repeating: "x", count: 1024 * 1024 + 1))
        XCTAssertThrowsError(try prepare("read", ["filePath": "large"]))
        XCTAssertThrowsError(try prepare("write", ["filePath": "large", "content": "destroy"]))
        let grep = try prepare("grep", ["pattern": "x"]).execute()
        XCTAssertEqual(grep.metadata["truncated"] as? Bool, true)
        XCTAssertTrue(grep.output.lowercased().contains("skipped"))
    }

    func testOutputIsBoundedAndExplicitlyTruncated() throws {
        _ = try put("long.txt", String(repeating: "a", count: 100_000))
        let read = try prepare("read", ["filePath": "long.txt"]).execute()
        XCTAssertLessThanOrEqual(read.output.utf8.count, 65_536)
        XCTAssertEqual(read.metadata["truncated"] as? Bool, true)
        XCTAssertTrue(read.output.lowercased().contains("truncated"))
        XCTAssertThrowsError(try prepare("read", ["filePath": "long.txt", "offset": 0]))
        XCTAssertThrowsError(try prepare("read", ["filePath": "long.txt", "limit": true]))
    }

    func testNoNewlineAndEmptyFileDiffsAreReal() throws {
        let prepared = try prepare("write", ["filePath": "empty", "content": ""])
        XCTAssertTrue(try XCTUnwrap(prepared.metadata["diff"] as? String).contains("+++ "))
        _ = try prepared.execute()
        let edit = try prepare("write", ["filePath": "empty", "content": "last line"])
        XCTAssertTrue(try XCTUnwrap(edit.metadata["diff"] as? String).contains("\\ No newline at end of file"))
        _ = try edit.execute()
        XCTAssertEqual(try turns().count, 1)
        XCTAssertEqual(try show(try XCTUnwrap(try turns().first)).count, 2)
    }

    func testCorruptLedgerPreventsWriteRatherThanLosingHistory() throws {
        let file = try put("a.txt", "before")
        let approved = try prepare("write", ["filePath": "a.txt", "content": "after"])
        let storage = ledgerRoot.appendingPathComponent("patches.json")
        try Data("not valid JSON".utf8).write(to: storage)
        XCTAssertThrowsError(try approved.execute())
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "before")
        XCTAssertEqual(try String(contentsOf: storage, encoding: .utf8), "not valid JSON")
        XCTAssertThrowsError(try NativePatchLedger(root: ledgerRoot))
    }

    func testConcurrentLedgersHaveOneWriter() throws {
        let second = try NativePatchLedger(root: ledgerRoot)
        let instances = [ledger!, second]
        let directory = project.path
        let errorsLock = NSLock()
        var errors: [Error] = []
        DispatchQueue.concurrentPerform(iterations: 12) { index in
            do {
                try instances[index % 2].append(directory: directory, sessionID: "session-1", turnID: "user-1",
                    title: "Concurrent edits", file: "file-\(index)", before: nil, after: "\(index)\n",
                    patch: "--- /dev/null\n+++ file-\(index)\n@@ -0,0 +1,1 @@\n+\(index)\n")
            } catch {
                errorsLock.lock()
                errors.append(error)
                errorsLock.unlock()
            }
        }
        XCTAssertTrue(errors.isEmpty, "\(errors)")
        let stored = try turns()
        XCTAssertEqual(stored.count, 1)
        let changes = try show(try XCTUnwrap(stored.first))
        XCTAssertEqual(changes.count, 12)
        XCTAssertEqual(Set(changes.compactMap { $0["key"] as? String }).count, 12)
    }

    func testUnifiedPatchAppliesExactlyForMultipleHunksAndUnicode() throws {
        let original = (1...30).map { "line \($0)\n" }.joined()
        _ = try put("patch.txt", original)
        let changed = original.replacingOccurrences(of: "line 2\n", with: "café\nextra\n")
            .replacingOccurrences(of: "line 25\n", with: "世界\n")
        let prepared = try prepare("write", ["filePath": "patch.txt", "content": changed])
        let patch = try XCTUnwrap(prepared.metadata["diff"] as? String)
        let lines = patch.components(separatedBy: "\n")
        let hunk = try NSRegularExpression(pattern: #"^@@ -(\d+),(\d+) \+(\d+),(\d+) @@$"#)
        let originalLines = original.components(separatedBy: "\n").dropLast().map { $0 + "\n" }
        var rebuilt: [String] = [], cursor = 0, hunkCount = 0
        for line in lines.dropFirst(2) {
            if let match = hunk.firstMatch(in: line, range: NSRange(location: 0, length: (line as NSString).length)) {
                hunkCount += 1
                let oldStart = try XCTUnwrap(Int((line as NSString).substring(with: match.range(at: 1)))) - 1
                rebuilt.append(contentsOf: originalLines[cursor..<oldStart])
                cursor = oldStart
            } else if line.hasPrefix(" ") {
                let text = String(line.dropFirst()) + "\n"
                XCTAssertEqual(originalLines[cursor], text)
                rebuilt.append(text)
                cursor += 1
            } else if line.hasPrefix("-") {
                XCTAssertEqual(originalLines[cursor], String(line.dropFirst()) + "\n")
                cursor += 1
            } else if line.hasPrefix("+") {
                rebuilt.append(String(line.dropFirst()) + "\n")
            }
        }
        rebuilt.append(contentsOf: originalLines[cursor...])
        XCTAssertEqual(hunkCount, 2)
        XCTAssertEqual(rebuilt.joined(), changed)
    }

    func testIdenticalContentIsNotRecordedAsAnEdit() throws {
        let file = try put("a.txt", "same")
        let approved = try prepare("write", ["filePath": "a.txt", "content": "same"])
        XCTAssertEqual(approved.metadata["diff"] as? String, "")
        let result = try approved.execute()
        XCTAssertEqual(result.metadata["saved"] as? Bool, false)
        XCTAssertTrue(try turns().isEmpty)
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "same")
    }

    func testCancellationBeforeWriteMakesNoChanges() async throws {
        let file = try put("a.txt", "before")
        let prepared = try prepare("write", ["filePath": "a.txt", "content": "after"])
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try prepared.execute()
        }
        do {
            _ = try await task.value
            XCTFail("A cancelled write must throw")
        } catch is CancellationError {
            // Expected: approval does not override task cancellation.
        }
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "before")
        XCTAssertTrue(try turns().isEmpty)
    }
}
