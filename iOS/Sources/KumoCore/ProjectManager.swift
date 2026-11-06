import Foundation

/// Tracks app-owned projects and folders opened through security-scoped bookmarks.
/// Picked folders stay in place; their access is retained for the app's lifetime.
public final class ProjectManager {
    public struct ProjectInfo {
        public let path: String
        public let name: String

        public init(path: String, name: String) {
            self.path = path
            self.name = name
        }
    }

    private static let prefsKeyCurrent = "pm.current"
    private static let prefsKeyDirectoryList = "pm.directories"
    private static let prefsKeyBookmarks = "pm.bookmarks"
    private static let defaults = UserDefaults.standard
    private static let pickedFolders = PickedFolderBookmarks(defaults: defaults)

    // MARK: - Picked folders

    /// Persist the picker URL before the caller selects it with `setCurrent`.
    /// Successful security-scoped access belongs to this manager, not the picker callback.
    public static func rememberPickedFolder(_ url: URL) throws {
        try pickedFolders.remember(url)
    }

    /// Call at startup before tools use persisted paths. Invalid bookmarks remain saved
    /// so temporary provider failures do not destroy the user's folder selections.
    public static func restorePickedFolders() {
        for failure in pickedFolders.restore() {
            KumoLog.shared.append(failure)
        }
    }

    struct BookmarkAccess {
        var start: (URL) -> Bool
        var stop: (URL) -> Void
        var create: (URL) throws -> Data
        var resolve: (Data) throws -> (url: URL, isStale: Bool)

        static let live = BookmarkAccess(
            start: { $0.startAccessingSecurityScopedResource() },
            stop: { $0.stopAccessingSecurityScopedResource() },
            create: { url in
                #if os(macOS)
                let options: URL.BookmarkCreationOptions = [.withSecurityScope]
                #else
                // iOS picker URLs carry the grant; `.withSecurityScope` is macOS-only.
                let options: URL.BookmarkCreationOptions = [.minimalBookmark]
                #endif
                return try url.bookmarkData(options: options,
                                            includingResourceValuesForKeys: nil,
                                            relativeTo: nil)
            },
            resolve: { data in
                var stale = false
                #if os(macOS)
                let options: URL.BookmarkResolutionOptions = [.withoutUI, .withSecurityScope]
                #else
                let options: URL.BookmarkResolutionOptions = [.withoutUI]
                #endif
                let url = try URL(resolvingBookmarkData: data, options: options,
                                  relativeTo: nil, bookmarkDataIsStale: &stale)
                return (url, stale)
            }
        )
    }

    /// The shared instance is never released while the app runs. Isolated instances
    /// let tests exercise persistence and access balancing without global preferences.
    final class PickedFolderBookmarks {
        private let defaults: UserDefaults
        private let access: BookmarkAccess
        private let lock = NSRecursiveLock()
        private var scopes: [String: URL] = [:]
        private var resolvedURLs: [String: URL] = [:]

        init(defaults: UserDefaults, access: BookmarkAccess = .live) {
            self.defaults = defaults
            self.access = access
        }

        deinit {
            for url in scopes.values { access.stop(url) }
        }

        func remember(_ url: URL) throws {
            lock.lock()
            defer { lock.unlock() }
            try withAccess(to: url) {
                let data = try access.create(url)
                var bookmarks = defaults.dictionary(forKey: prefsKeyBookmarks) ?? [:]
                bookmarks[url.path] = data
                defaults.set(bookmarks, forKey: prefsKeyBookmarks)
                resolvedURLs[url.path] = url
            }
        }

        func restore() -> [String] {
            lock.lock()
            defer { lock.unlock() }
            let stored = defaults.dictionary(forKey: prefsKeyBookmarks) ?? [:]
            var failures: [String] = []
            for (path, value) in stored {
                do {
                    guard let data = value as? Data else {
                        throw CocoaError(.fileReadCorruptFile)
                    }
                    let resolved = try access.resolve(data)
                    try withAccess(to: resolved.url, replacingPath: path) {
                        let refreshed = resolved.isStale ? try access.create(resolved.url) : data
                        var bookmarks = defaults.dictionary(forKey: prefsKeyBookmarks) ?? [:]
                        bookmarks.removeValue(forKey: path)
                        bookmarks[resolved.url.path] = refreshed
                        defaults.set(bookmarks, forKey: prefsKeyBookmarks)
                        mapStoredPath(path, to: resolved.url)
                    }
                } catch {
                    failures.append("Could not restore picked folder \(path): \(error.localizedDescription)")
                }
            }
            return failures
        }

        func url(forPath path: String) -> URL {
            lock.lock()
            defer { lock.unlock() }
            return resolvedURLs[path] ?? URL(fileURLWithPath: path)
        }

        func currentPath() -> String? {
            lock.lock()
            defer { lock.unlock() }
            return defaults.string(forKey: prefsKeyCurrent)
        }

        func setCurrent(_ url: URL) {
            lock.lock()
            defer { lock.unlock() }
            let resolved = self.url(forPath: url.path)
            rememberDirectory(resolved)
            defaults.set(resolved.path, forKey: prefsKeyCurrent)
        }

        func rememberDirectory(_ url: URL) {
            lock.lock()
            defer { lock.unlock() }
            let path = self.url(forPath: url.path).path
            var list = directories()
            if !list.contains(path) { list.append(path) }
            defaults.set(list, forKey: prefsKeyDirectoryList)
        }

        func directories() -> [String] {
            lock.lock()
            defer { lock.unlock() }
            return defaults.stringArray(forKey: prefsKeyDirectoryList) ?? []
        }

        func saveDirectories(_ paths: [String]) {
            lock.lock()
            defer { lock.unlock() }
            // A provider may be offline; do not discard paths backed by saved bookmarks.
            let bookmarkPaths = Set((defaults.dictionary(forKey: prefsKeyBookmarks) ?? [:]).keys)
            let saved = directories().filter { bookmarkPaths.contains($0) }
            defaults.set(Array(Set(paths + saved)).sorted(), forKey: prefsKeyDirectoryList)
        }

        private func withAccess(to url: URL, replacingPath: String? = nil,
                                perform operation: () throws -> Void) throws {
            let started = access.start(url)
            var retained = false
            defer {
                if started && !retained { access.stop(url) }
            }
            // Ordinary app-owned URLs can return false from startAccessing. Directory
            // enumeration checks actual access, unlike existence/readability flags alone.
            guard url.isFileURL else {
                throw CocoaError(.fileReadUnsupportedScheme)
            }
            guard ProjectManager.isDirectory(url) else {
                throw CocoaError(.fileReadUnknown, userInfo: [
                    NSFilePathErrorKey: url.path,
                    NSLocalizedDescriptionKey: "The selected URL is not an accessible folder."
                ])
            }
            _ = try FileManager.default.contentsOfDirectory(at: url, includingPropertiesForKeys: nil)
            try operation()
            let key = url.standardizedFileURL.path
            if started && scopes[key] == nil {
                scopes[key] = url
                retained = true
            }
            if let replacingPath {
                let previousKey = URL(fileURLWithPath: replacingPath).standardizedFileURL.path
                if previousKey != key, let previous = scopes.removeValue(forKey: previousKey) {
                    if scopes[key] != nil {
                        access.stop(previous)
                    } else {
                        scopes[key] = previous
                    }
                }
            }
        }

        private func mapStoredPath(_ path: String, to url: URL) {
            // Preserve aliases for callers holding an old path after a rename/move.
            for alias in Array(resolvedURLs.keys) where resolvedURLs[alias]?.path == path {
                resolvedURLs[alias] = url
            }
            resolvedURLs[path] = url
            resolvedURLs[url.path] = url
            let list = directories().map { $0 == path ? url.path : $0 }
            defaults.set(Array(Set(list + [url.path])).sorted(), forKey: prefsKeyDirectoryList)
            if defaults.string(forKey: prefsKeyCurrent) == path {
                defaults.set(url.path, forKey: prefsKeyCurrent)
            }
        }
    }

    // MARK: - Root folders

    public static func applicationSupport() -> URL {
        let url = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    public static func documents() -> URL {
        let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    public static func homeDir() -> URL {
        let url = applicationSupport().appendingPathComponent("home", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    public static func projectsRoot() -> URL {
        let url = documents().appendingPathComponent("Kumo/projects", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    // MARK: - Current directory

    public static func currentDir() -> URL {
        let saved = pickedFolders.currentPath()
        if let saved {
            let u = pickedFolders.url(forPath: saved)
            if isDirectory(u) { return u }
        }
        let def = projectsRoot().appendingPathComponent("default", isDirectory: true)
        try? FileManager.default.createDirectory(at: def, withIntermediateDirectories: true)
        pickedFolders.setCurrent(def)
        return def
    }

    public static func setCurrent(_ dir: URL) {
        pickedFolders.setCurrent(dir)
    }

    public static func remember(_ dir: URL) {
        pickedFolders.rememberDirectory(dir)
    }

    private static func storedDirectories() -> [String] {
        pickedFolders.directories()
    }

    public static func list() -> [ProjectInfo] {
        var paths = storedDirectories()
        paths.append(currentDir().path)
        // Recover any sub-folders from the projects root that were created before bookkeeping.
        if let entries = try? FileManager.default.contentsOfDirectory(
            at: projectsRoot(),
            includingPropertiesForKeys: nil
        ) {
            for e in entries {
                var isDir: ObjCBool = false
                if FileManager.default.fileExists(atPath: e.path, isDirectory: &isDir), isDir.boolValue,
                   !e.lastPathComponent.hasPrefix(".") {
                    paths.append(e.path)
                }
            }
        }
        let distinct = Array(Set(paths.map { pickedFolders.url(forPath: $0).path })).sorted()
        let valid = distinct.filter { isDirectory(pickedFolders.url(forPath: $0)) }
        pickedFolders.saveDirectories(valid)
        return valid.map {
            ProjectInfo(path: $0, name: URL(fileURLWithPath: $0).lastPathComponent)
        }
    }

    public static func createNew(name: String) -> URL {
        let safe = sanitize(name) ?? "project"
        let target = uniqueDirectory(base: projectsRoot(), name: safe)
        try? FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)
        setCurrent(target)
        return target
    }

    @discardableResult
    public static func copyDirectory(from src: URL, to dest: URL) -> Bool {
        do {
            try FileManager.default.copyItem(at: src, to: dest)
            return true
        } catch {
            return false
        }
    }

    // MARK: - Helpers

    static func sanitize(_ name: String) -> String? {
        let cleaned = name.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "[^A-Za-z0-9. _-]", with: "", options: .regularExpression)
        return cleaned.isEmpty ? nil : cleaned
    }

    public static func uniqueDirectory(base: URL, name: String) -> URL {
        var candidate = base.appendingPathComponent(name, isDirectory: true)
        var i = 1
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = base.appendingPathComponent("\(name)-\(i)", isDirectory: true)
            i += 1
        }
        return candidate
    }

    public static func isDirectory(_ url: URL) -> Bool {
        var isDir: ObjCBool = false
        return FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) && isDir.boolValue
    }

    /// Write-probe, like the Android `canWrite` which is unreliable on FUSE paths.
    public static func canWrite(_ url: URL) -> Bool {
        let probe = url.appendingPathComponent(".kumo-write-test-\(UUID().uuidString)")
        do {
            try Data().write(to: probe)
            try? FileManager.default.removeItem(at: probe)
            return true
        } catch {
            return false
        }
    }
}