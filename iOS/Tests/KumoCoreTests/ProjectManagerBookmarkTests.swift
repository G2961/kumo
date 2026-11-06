import Foundation
import XCTest
@testable import KumoCore

final class ProjectManagerBookmarkTests: XCTestCase {
    private var suiteName = ""
    private var defaults: UserDefaults!
    private var folder: URL!
    private var started: [URL] = []
    private var stopped: [URL] = []
    private var created: [URL] = []
    private var startResult = true
    private var creationError: Error?
    private let freshData = Data("fresh bookmark".utf8)
    private var resolutions: [Data: (url: URL, isStale: Bool)] = [:]

    override func setUpWithError() throws {
        suiteName = "ProjectManagerBookmarkTests.\(UUID().uuidString)"
        defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        folder = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        started = []
        stopped = []
        created = []
        startResult = true
        creationError = nil
        resolutions = [:]
    }

    override func tearDownWithError() throws {
        defaults?.removePersistentDomain(forName: suiteName)
        if let folder { try FileManager.default.removeItem(at: folder) }
    }

    private func makeStore() -> ProjectManager.PickedFolderBookmarks {
        var access = ProjectManager.BookmarkAccess.live
        access.start = { url in
            self.started.append(url)
            return self.startResult
        }
        access.stop = { self.stopped.append($0) }
        access.create = { url in
            self.created.append(url)
            if let error = self.creationError { throw error }
            return self.freshData
        }
        access.resolve = { data in
            guard let result = self.resolutions[data] else {
                throw CocoaError(.fileReadCorruptFile)
            }
            return result
        }
        return ProjectManager.PickedFolderBookmarks(defaults: defaults, access: access)
    }

    func testRememberPersistsBookmarkWithoutSelectingFolder() throws {
        defaults.set("previous project", forKey: "pm.current")
        let store = makeStore()

        try store.remember(folder)

        XCTAssertEqual(defaults.dictionary(forKey: "pm.bookmarks")?[folder.path] as? Data, freshData)
        XCTAssertEqual(defaults.string(forKey: "pm.current"), "previous project")
        XCTAssertEqual(started, [folder])
        XCTAssertTrue(stopped.isEmpty)
    }

    func testFalseStartStillAllowsActuallyReadableFolderAndRestoration() throws {
        startResult = false
        let store = makeStore()
        try store.remember(folder)
        resolutions[freshData] = (folder, false)
        let restoredStore = makeStore()

        XCTAssertTrue(restoredStore.restore().isEmpty)
        XCTAssertEqual(restoredStore.url(forPath: folder.path), folder)
        XCTAssertEqual(started.count, 2)
        XCTAssertTrue(stopped.isEmpty)
    }

    func testFalseStartDoesNotAcceptMissingFolder() {
        startResult = false
        defaults.set("previous project", forKey: "pm.current")
        let missing = folder.appendingPathComponent("missing", isDirectory: true)

        XCTAssertThrowsError(try makeStore().remember(missing))
        XCTAssertNil(defaults.dictionary(forKey: "pm.bookmarks"))
        XCTAssertEqual(defaults.string(forKey: "pm.current"), "previous project")
        XCTAssertTrue(created.isEmpty)
        XCTAssertTrue(stopped.isEmpty)
    }

    func testRegularFileIsNotAcceptedAsFolder() throws {
        let file = folder.appendingPathComponent("file.txt")
        try Data("hello".utf8).write(to: file)

        XCTAssertThrowsError(try makeStore().remember(file))
        XCTAssertNil(defaults.dictionary(forKey: "pm.bookmarks"))
        XCTAssertEqual(stopped, [file])
    }

    func testBookmarkCreationFailureDoesNotSelectOrLeakScope() throws {
        defaults.set("previous project", forKey: "pm.current")
        creationError = CocoaError(.fileWriteUnknown)
        let store = makeStore()

        XCTAssertThrowsError(try store.remember(folder))
        XCTAssertNil(defaults.dictionary(forKey: "pm.bookmarks"))
        XCTAssertEqual(defaults.string(forKey: "pm.current"), "previous project")
        XCTAssertEqual(stopped, [folder])

        creationError = nil
        try store.remember(folder)
        XCTAssertEqual(started.count - stopped.count, 1)
    }

    func testConcurrentDuplicatePicksAndRestoreRetainOnlyOneScope() throws {
        let store = makeStore()
        try store.remember(folder)
        resolutions[freshData] = (folder, false)

        DispatchQueue.concurrentPerform(iterations: 20) { _ in
            do {
                try store.remember(self.folder)
            } catch {
                XCTFail("Readable folder was rejected: \(error)")
            }
        }
        XCTAssertTrue(store.restore().isEmpty)

        XCTAssertEqual(started.count, 22)
        XCTAssertEqual(stopped.count, 21)
        XCTAssertEqual(defaults.dictionary(forKey: "pm.bookmarks")?.count, 1)
    }

    func testConcurrentDifferentPicksKeepEveryBookmarkAndScope() {
        let store = makeStore()
        let folders = (0..<12).map { folder.appendingPathComponent("project-\($0)", isDirectory: true) }
        for url in folders {
            do {
                try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
            } catch {
                XCTFail("Could not create fixture: \(error)")
                return
            }
        }

        DispatchQueue.concurrentPerform(iterations: folders.count) { index in
            do {
                try store.remember(folders[index])
            } catch {
                XCTFail("Readable folder was rejected: \(error)")
            }
        }

        XCTAssertEqual(Set(defaults.dictionary(forKey: "pm.bookmarks")?.keys.map { $0 } ?? []),
                       Set(folders.map(\.path)))
        XCTAssertEqual(started.count, folders.count)
        XCTAssertTrue(stopped.isEmpty)
    }

    func testRestoreSkipsInvalidBookmarksWithoutDeletingUserData() {
        let invalidData = Data("invalid".utf8)
        let saved: [String: Any] = ["broken": invalidData, "wrong type": "keep me"]
        defaults.set(saved, forKey: "pm.bookmarks")
        defaults.set("broken", forKey: "pm.current")
        defaults.set(["broken", "wrong type"], forKey: "pm.directories")

        let failures = makeStore().restore()

        XCTAssertEqual(failures.count, 2)
        XCTAssertTrue(failures.contains { $0.contains("broken") })
        XCTAssertTrue(failures.contains { $0.contains("wrong type") })
        XCTAssertEqual(defaults.dictionary(forKey: "pm.bookmarks")?["broken"] as? Data, invalidData)
        XCTAssertEqual(defaults.dictionary(forKey: "pm.bookmarks")?["wrong type"] as? String, "keep me")
        XCTAssertEqual(defaults.string(forKey: "pm.current"), "broken")
        XCTAssertEqual(defaults.stringArray(forKey: "pm.directories"), ["broken", "wrong type"])
        XCTAssertTrue(started.isEmpty)
    }

    func testStaleRenamedBookmarkRefreshesAndMapsStoredPaths() {
        let oldPath = folder.appendingPathComponent("old name").path
        let staleData = Data("stale bookmark".utf8)
        defaults.set([oldPath: staleData], forKey: "pm.bookmarks")
        defaults.set(oldPath, forKey: "pm.current")
        defaults.set([oldPath, "another project"], forKey: "pm.directories")
        resolutions[staleData] = (folder, true)
        resolutions[freshData] = (folder, false)
        let store = makeStore()

        XCTAssertTrue(store.restore().isEmpty)

        let bookmarks = defaults.dictionary(forKey: "pm.bookmarks")
        XCTAssertNil(bookmarks?[oldPath])
        XCTAssertEqual(bookmarks?[folder.path] as? Data, freshData)
        XCTAssertEqual(defaults.string(forKey: "pm.current"), folder.path)
        XCTAssertEqual(Set(defaults.stringArray(forKey: "pm.directories") ?? []),
                       Set([folder.path, "another project"]))
        XCTAssertEqual(store.url(forPath: oldPath), folder)
        XCTAssertEqual(store.url(forPath: folder.path), folder)
        XCTAssertEqual(created, [folder])
        XCTAssertTrue(store.restore().isEmpty)
        XCTAssertEqual(created.count, 1)
        XCTAssertEqual(started.count - stopped.count, 1)
    }

    func testRestoreMapsRenamedFolderEvenWhenBookmarkIsNotStale() {
        let oldPath = folder.appendingPathComponent("old name").path
        defaults.set([oldPath: freshData], forKey: "pm.bookmarks")
        defaults.set(oldPath, forKey: "pm.current")
        resolutions[freshData] = (folder, false)
        let store = makeStore()

        XCTAssertTrue(store.restore().isEmpty)
        XCTAssertEqual(defaults.string(forKey: "pm.current"), folder.path)
        XCTAssertNil(defaults.dictionary(forKey: "pm.bookmarks")?[oldPath])
        XCTAssertEqual(defaults.dictionary(forKey: "pm.bookmarks")?[folder.path] as? Data, freshData)
        XCTAssertTrue(created.isEmpty)
        store.setCurrent(URL(fileURLWithPath: oldPath))
        XCTAssertEqual(store.currentPath(), folder.path)
    }

    func testDirectoryBookkeepingPreservesUnavailableBookmarkedFolders() {
        let oldPath = folder.appendingPathComponent("offline").path
        defaults.set([oldPath: Data("unavailable".utf8)], forKey: "pm.bookmarks")
        defaults.set([oldPath, "missing unbookmarked folder"], forKey: "pm.directories")
        let store = makeStore()

        store.saveDirectories([folder.path])

        XCTAssertEqual(Set(store.directories()), Set([oldPath, folder.path]))
        XCTAssertNotNil(defaults.dictionary(forKey: "pm.bookmarks")?[oldPath])
    }

    func testRestorationRejectsMissingFolderAndKeepsItsBookmark() {
        let missing = folder.appendingPathComponent("missing", isDirectory: true)
        defaults.set([missing.path: freshData], forKey: "pm.bookmarks")
        resolutions[freshData] = (missing, false)
        let store = makeStore()

        XCTAssertEqual(store.restore().count, 1)
        XCTAssertEqual(defaults.dictionary(forKey: "pm.bookmarks")?[missing.path] as? Data, freshData)
        XCTAssertEqual(stopped, [missing])
    }

    func testStaleRefreshFailurePreservesOriginalBookmarkAndPaths() {
        let oldPath = folder.appendingPathComponent("old name").path
        let staleData = Data("stale bookmark".utf8)
        defaults.set([oldPath: staleData], forKey: "pm.bookmarks")
        defaults.set(oldPath, forKey: "pm.current")
        defaults.set([oldPath], forKey: "pm.directories")
        resolutions[staleData] = (folder, true)
        creationError = CocoaError(.fileWriteUnknown)

        XCTAssertEqual(makeStore().restore().count, 1)
        XCTAssertEqual(defaults.dictionary(forKey: "pm.bookmarks")?[oldPath] as? Data, staleData)
        XCTAssertNil(defaults.dictionary(forKey: "pm.bookmarks")?[folder.path])
        XCTAssertEqual(defaults.string(forKey: "pm.current"), oldPath)
        XCTAssertEqual(defaults.stringArray(forKey: "pm.directories"), [oldPath])
        XCTAssertEqual(stopped, [folder])
    }

    func testRestoreAfterMoveBalancesPreviouslyRetainedScope() throws {
        let store = makeStore()
        let originalPath = folder.path
        try store.remember(folder)
        let moved = folder.appendingPathComponent("new location", isDirectory: true)
        try FileManager.default.createDirectory(at: moved, withIntermediateDirectories: true)
        resolutions[freshData] = (moved, false)

        XCTAssertTrue(store.restore().isEmpty)
        XCTAssertEqual(store.url(forPath: originalPath), moved)
        XCTAssertEqual(started.count - stopped.count, 1)
        XCTAssertEqual(stopped, [folder])
    }

    func testFalseStartAfterMoveKeepsOriginalGrantUntilStoreRelease() throws {
        var store: ProjectManager.PickedFolderBookmarks? = makeStore()
        try store?.remember(folder)
        let moved = folder.appendingPathComponent("new location", isDirectory: true)
        try FileManager.default.createDirectory(at: moved, withIntermediateDirectories: true)
        resolutions[freshData] = (moved, false)
        startResult = false

        XCTAssertTrue(try XCTUnwrap(store).restore().isEmpty)
        XCTAssertTrue(stopped.isEmpty)
        store = nil
        XCTAssertEqual(stopped, [folder])
    }

    func testIsolatedStoreReleaseBalancesRetainedAccess() throws {
        var store: ProjectManager.PickedFolderBookmarks? = makeStore()
        try store?.remember(folder)
        XCTAssertTrue(stopped.isEmpty)

        store = nil

        XCTAssertEqual(stopped, [folder])
    }

    func testRealBookmarkRoundTripForOrdinaryFolder() throws {
        let store = ProjectManager.PickedFolderBookmarks(defaults: defaults)
        try store.remember(folder)
        XCTAssertNotNil(defaults.dictionary(forKey: "pm.bookmarks")?[folder.path] as? Data)
        let restoredStore = ProjectManager.PickedFolderBookmarks(defaults: defaults)

        XCTAssertTrue(restoredStore.restore().isEmpty)
        XCTAssertEqual(restoredStore.url(forPath: folder.path).resolvingSymlinksInPath(),
                       folder.resolvingSymlinksInPath())
        XCTAssertNoThrow(try FileManager.default.contentsOfDirectory(
            at: restoredStore.url(forPath: folder.path), includingPropertiesForKeys: nil
        ))
    }
}
