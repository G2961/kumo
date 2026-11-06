import Foundation
import XCTest
@testable import KumoCore

final class NativeFileBrowserTests: XCTestCase {
    private var root: URL!
    private let text = "Привет, 世界! 👋\nCafé — Unicode stays intact.\n"
    private let limit = 512 * 1024

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.resolvingSymlinksInPath()
            .appendingPathComponent("NativeFileBrowserTests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        if let root { try FileManager.default.removeItem(at: root) }
    }

    @discardableResult
    private func put(_ name: String, _ data: Data) throws -> URL {
        let file = root.appendingPathComponent(name)
        try data.write(to: file)
        return file
    }

    private func mkdir(_ name: String) throws -> URL {
        let directory = root.appendingPathComponent(name, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func assertBrowserError(
        _ operation: () throws -> [String: Any], path: String,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        XCTAssertThrowsError(try operation(), file: file, line: line) { error in
            XCTAssertTrue(error is NativeFileBrowserError, file: file, line: line)
            XCTAssertNotNil((error as? LocalizedError)?.errorDescription, file: file, line: line)
            XCTAssertTrue(error.localizedDescription.contains(path), file: file, line: line)
        }
    }

    func testListAndReadReflectTheSameNativeFolder() throws {
        let file = try put("test.txt", Data(text.utf8))
        let nested = try mkdir("nested")
        try Data(text.utf8).write(to: nested.appendingPathComponent("test.txt"))

        let listing = try NativeFileBrowser.list(path: root.path)
        XCTAssertEqual(Set(listing.keys), Set(["path", "dirs", "files"]))
        XCTAssertEqual(listing["path"] as? String, root.standardizedFileURL.path)
        XCTAssertEqual(listing["dirs"] as? [String], ["nested"])
        let files = try XCTUnwrap(listing["files"] as? [[String: Any]])
        XCTAssertEqual(files.count, 1)
        let entry = try XCTUnwrap(files.first)
        XCTAssertEqual(Set(entry.keys), Set(["name", "size"]))
        XCTAssertEqual(entry["name"] as? String, "test.txt")
        XCTAssertEqual(entry["size"] as? Int, text.utf8.count)

        let reading = try NativeFileBrowser.read(path: file.path)
        XCTAssertEqual(Set(reading.keys), Set(["content", "size"]))
        XCTAssertEqual(reading["content"] as? String, text)
        XCTAssertEqual(reading["size"] as? Int, entry["size"] as? Int)
        let nestedListing = try NativeFileBrowser.list(path: nested.path)
        XCTAssertEqual((nestedListing["files"] as? [[String: Any]])?.first?["name"] as? String, "test.txt")
        XCTAssertEqual(try NativeFileBrowser.read(path: nested.appendingPathComponent("test.txt").path)["content"] as? String, text)
    }

    func testListSortsNamesAndIncludesHiddenAndUnicodeEntries() throws {
        for name in ["z-folder", "目录", "a-folder", ".hidden-folder"] { _ = try mkdir(name) }
        for name in ["z.txt", "Привет.txt", "a.txt", ".hidden"] { try put(name, Data(name.utf8)) }

        let listing = try NativeFileBrowser.list(path: root.path)
        XCTAssertEqual(listing["dirs"] as? [String], ["z-folder", "目录", "a-folder", ".hidden-folder"].sorted())
        let files = try XCTUnwrap(listing["files"] as? [[String: Any]])
        XCTAssertEqual(files.compactMap { $0["name"] as? String }, ["z.txt", "Привет.txt", "a.txt", ".hidden"].sorted())
        for entry in files {
            let name = try XCTUnwrap(entry["name"] as? String)
            XCTAssertEqual(entry["size"] as? Int, name.utf8.count)
        }
    }

    func testEmptyDirectoryIsSuccessfulButMissingDirectoryAndFileAreErrors() throws {
        let listing = try NativeFileBrowser.list(path: root.path)
        XCTAssertEqual(listing["dirs"] as? [String], [])
        XCTAssertEqual((listing["files"] as? [[String: Any]])?.count, 0)

        let missing = root.appendingPathComponent("missing").path
        assertBrowserError({ try NativeFileBrowser.list(path: missing) }, path: missing)
        assertBrowserError({ try NativeFileBrowser.read(path: missing) }, path: missing)
        let file = try put("test.txt", Data(text.utf8))
        assertBrowserError({ try NativeFileBrowser.list(path: file.path) }, path: file.path)
        assertBrowserError({ try NativeFileBrowser.read(path: root.path) }, path: root.path)
    }

    func testEmptyFileReadsAsEmptyContentWithZeroBytes() throws {
        let file = try put("empty.txt", Data())
        let reading = try NativeFileBrowser.read(path: file.path)
        XCTAssertEqual(reading["content"] as? String, "")
        XCTAssertEqual(reading["size"] as? Int, 0)
    }

    func testAbsolutePathIsStandardizedWithoutChangingTheBrowsedFolder() throws {
        _ = try mkdir("nested")
        try put("test.txt", Data(text.utf8))
        let path = root.path + "/nested/../."
        let listing = try NativeFileBrowser.list(path: path)
        XCTAssertEqual(listing["path"] as? String, root.standardizedFileURL.path)
        XCTAssertEqual((listing["files"] as? [[String: Any]])?.first?["name"] as? String, "test.txt")
        XCTAssertEqual(try NativeFileBrowser.read(path: path + "/test.txt")["content"] as? String, text)
    }

    func testAbsolutePathOutsideTheListedFolderIsNotRestrictedToAProjectRoot() throws {
        let sibling = root.deletingLastPathComponent().appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: sibling, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: sibling) }
        let file = sibling.appendingPathComponent("test.txt")
        try Data(text.utf8).write(to: file)

        _ = try NativeFileBrowser.list(path: root.path)
        let listing = try NativeFileBrowser.list(path: sibling.path)
        XCTAssertEqual(listing["path"] as? String, sibling.standardizedFileURL.path)
        XCTAssertEqual((listing["files"] as? [[String: Any]])?.first?["name"] as? String, "test.txt")
        XCTAssertEqual(try NativeFileBrowser.read(path: file.path)["content"] as? String, text)
    }

    func testRejectsNonNativeRelativeAndNULPaths() {
        let invalidPaths = ["", "test.txt", "./test.txt", "../test.txt", "~/test.txt",
                            "file:///tmp/test.txt", "https://example.com/test.txt", "C:\\test.txt", root.path + "/test\0.txt"]
        for path in invalidPaths {
            XCTAssertThrowsError(try NativeFileBrowser.list(path: path)) { error in
                guard case NativeFileBrowserError.invalidPath = error else {
                    return XCTFail("Expected an invalid-path error")
                }
            }
            XCTAssertThrowsError(try NativeFileBrowser.read(path: path)) { error in
                guard case NativeFileBrowserError.invalidPath = error else {
                    return XCTFail("Expected an invalid-path error")
                }
            }
        }
    }

    func testWhitespaceInAnAbsolutePathIsPreserved() throws {
        let directory = try mkdir(" folder with spaces ")
        let file = directory.appendingPathComponent(" test.txt ")
        try Data(text.utf8).write(to: file)
        let listing = try NativeFileBrowser.list(path: directory.path)
        XCTAssertEqual(listing["path"] as? String, directory.path)
        XCTAssertEqual((listing["files"] as? [[String: Any]])?.first?["name"] as? String, " test.txt ")
        XCTAssertEqual(try NativeFileBrowser.read(path: file.path)["content"] as? String, text)
    }

    func testSymbolicLinksCanBrowseAndReadTheirNativeTargets() throws {
        let directory = try mkdir("target")
        let file = directory.appendingPathComponent("test.txt")
        try Data(text.utf8).write(to: file)
        let folderLink = root.appendingPathComponent("linked-folder")
        let fileLink = root.appendingPathComponent("linked-file.txt")
        try FileManager.default.createSymbolicLink(at: folderLink, withDestinationURL: directory)
        try FileManager.default.createSymbolicLink(at: fileLink, withDestinationURL: file)

        let listing = try NativeFileBrowser.list(path: root.path)
        XCTAssertEqual(listing["dirs"] as? [String], ["linked-folder", "target"])
        let files = try XCTUnwrap(listing["files"] as? [[String: Any]])
        XCTAssertEqual(files.first?["name"] as? String, "linked-file.txt")
        XCTAssertEqual(files.first?["size"] as? Int, text.utf8.count)
        let linkedListing = try NativeFileBrowser.list(path: folderLink.path)
        XCTAssertEqual(linkedListing["path"] as? String, folderLink.standardizedFileURL.path)
        XCTAssertEqual((linkedListing["files"] as? [[String: Any]])?.first?["name"] as? String, "test.txt")
        XCTAssertEqual(try NativeFileBrowser.read(path: fileLink.path)["content"] as? String, text)
    }

    func testReadAcceptsExactly512KiBAndDecodesAcrossChunkBoundaries() throws {
        var data = Data(repeating: 0x61, count: 64 * 1024 - 1)
        data.append(Data("👋".utf8))
        data.append(Data(repeating: 0x62, count: limit - data.count))
        let file = try put("boundary.txt", data)

        let reading = try NativeFileBrowser.read(path: file.path)
        XCTAssertEqual(reading["size"] as? Int, limit)
        XCTAssertEqual(reading["content"] as? String, String(data: data, encoding: .utf8))
    }

    func testReadRejectsOneByteOverTheLimit() throws {
        let file = try put("oversized.txt", Data(repeating: 0x61, count: limit + 1))
        XCTAssertThrowsError(try NativeFileBrowser.read(path: file.path)) { error in
            guard case NativeFileBrowserError.fileTooLarge = error else {
                return XCTFail("Expected the size-limit error")
            }
            XCTAssertTrue(error.localizedDescription.contains(file.path))
        }
    }

    func testLargeSparseFileIsRejectedBeforeLoadingItsContent() throws {
        let file = try put("sparse.txt", Data())
        let handle = try FileHandle(forWritingTo: file)
        defer { try? handle.close() }
        try handle.truncate(atOffset: 1_024 * 1_024 * 1_024)

        XCTAssertThrowsError(try NativeFileBrowser.read(path: file.path)) { error in
            guard case NativeFileBrowserError.fileTooLarge = error else {
                return XCTFail("Expected metadata size rejection, not binary-content rejection")
            }
        }
    }

    func testReadRejectsBinaryNULWithoutExposingContentInError() throws {
        let secret = "private-content-not-for-errors"
        let file = try put("binary.dat", Data((secret + "\0tail").utf8))
        XCTAssertThrowsError(try NativeFileBrowser.read(path: file.path)) { error in
            guard case NativeFileBrowserError.binaryFile = error else {
                return XCTFail("Expected a binary-file error")
            }
            XCTAssertTrue(error.localizedDescription.contains(file.path))
            XCTAssertFalse(error.localizedDescription.contains(secret))
        }
    }

    func testReadRejectsInvalidUTF8RatherThanReturningEmptyOrLossyText() throws {
        for (index, invalid) in [Data([0xff]), Data([0xc3, 0x28]), Data([0xf0, 0x9f, 0x91])].enumerated() {
            let file = try put("invalid-\(index).txt", invalid)
            XCTAssertThrowsError(try NativeFileBrowser.read(path: file.path)) { error in
                guard case NativeFileBrowserError.invalidUTF8 = error else {
                    return XCTFail("Expected a strict UTF-8 error")
                }
                XCTAssertTrue(error.localizedDescription.contains(file.path))
            }
        }
    }

    func testUnreadableDirectoryThrowsInsteadOfClaimingItIsEmpty() throws {
        for name in ["denied-empty", "denied-populated"] {
            let directory = try mkdir(name)
            if name == "denied-populated" {
                try Data(text.utf8).write(to: directory.appendingPathComponent("test.txt"))
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: directory.path)
            defer { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path) }
            if FileManager.default.isReadableFile(atPath: directory.path) {
                throw XCTSkip("This test process can bypass filesystem permissions (for example, uid 0)")
            }
            assertBrowserError({ try NativeFileBrowser.list(path: directory.path) }, path: directory.path)
        }
    }

    func testUnreadableFileThrowsInsteadOfReturningEmptyContent() throws {
        let file = try put("denied.txt", Data(text.utf8))
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: file.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path) }
        if FileManager.default.isReadableFile(atPath: file.path) {
            throw XCTSkip("This test process can bypass filesystem permissions (for example, uid 0)")
        }
        assertBrowserError({ try NativeFileBrowser.read(path: file.path) }, path: file.path)
    }

    func testBrowserPayloadsRoundTripThroughBridgeJSONSerialization() throws {
        let file = try put("test.txt", Data(text.utf8))
        _ = try mkdir("nested")
        let listing = try NativeFileBrowser.list(path: root.path)
        XCTAssertTrue(JSONSerialization.isValidJSONObject(listing))
        let serializedList = try JSONSerialization.data(withJSONObject: listing)
        let decodedList = try XCTUnwrap(JSONSerialization.jsonObject(with: serializedList) as? [String: Any])
        XCTAssertEqual(decodedList["path"] as? String, root.path)
        XCTAssertEqual(decodedList["dirs"] as? [String], ["nested"])
        let entry = try XCTUnwrap((decodedList["files"] as? [[String: Any]])?.first)
        XCTAssertEqual(entry["name"] as? String, "test.txt")
        XCTAssertEqual(entry["size"] as? Int, text.utf8.count)

        let reading = try NativeFileBrowser.read(path: file.path)
        XCTAssertTrue(JSONSerialization.isValidJSONObject(reading))
        let bridgeJSON = try JavaScriptJSON.encode(reading)
        let decodedRead = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(bridgeJSON.utf8)) as? [String: Any])
        XCTAssertEqual(decodedRead["content"] as? String, text)
        XCTAssertEqual(decodedRead["size"] as? Int, text.utf8.count)
    }
}
