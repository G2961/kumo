import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

public struct NativeToolResult {
    public let output: String
    public let metadata: [String: Any]
}

public struct NativePreparedTool {
    public let name: String
    public let title: String
    public let modifies: Bool
    public let patterns: [String]
    public let metadata: [String: Any]
    private let operation: () throws -> NativeToolResult

    fileprivate init(name: String, title: String, modifies: Bool, patterns: [String],
                     metadata: [String: Any], operation: @escaping () throws -> NativeToolResult) {
        self.name = name
        self.title = title
        self.modifies = modifies
        self.patterns = patterns
        self.metadata = metadata
        self.operation = operation
    }

    public func execute() throws -> NativeToolResult {
        try Task.checkCancellation()
        return try operation()
    }
}

private struct NativeFileError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
    init(_ message: String) { self.message = message }
}

private enum NativeLimits {
    static let fileBytes = 1_048_576
    static let outputBytes = 65_536
    static let entries = 10_000
    static let matches = 1_000
    static let searchBytes = 32 * 1_048_576
    static let regexLineUnits = 16_384
    static let ledgerBytes = 64 * 1_048_576
}

private enum NativeFS {
    // Serializes this engine's mutations, not unrelated OS writers. Snapshot validation is
    // repeated immediately before rename; POSIX has no portable cross-process compare-and-swap.
    static let mutationLock = NSLock()

    struct Stamp: Equatable {
        let device: UInt64
        let inode: UInt64
        let size: Int64
        let mode: UInt32
        let modifiedSeconds: Int64
        let modifiedNanos: Int64
        let changedSeconds: Int64
        let changedNanos: Int64

        init(_ value: stat) {
            device = UInt64(value.st_dev)
            inode = UInt64(value.st_ino)
            size = Int64(value.st_size)
            mode = UInt32(value.st_mode)
            #if canImport(Darwin)
            modifiedSeconds = Int64(value.st_mtimespec.tv_sec)
            modifiedNanos = Int64(value.st_mtimespec.tv_nsec)
            changedSeconds = Int64(value.st_ctimespec.tv_sec)
            changedNanos = Int64(value.st_ctimespec.tv_nsec)
            #else
            modifiedSeconds = Int64(value.st_mtim.tv_sec)
            modifiedNanos = Int64(value.st_mtim.tv_nsec)
            changedSeconds = Int64(value.st_ctim.tv_sec)
            changedNanos = Int64(value.st_ctim.tv_nsec)
            #endif
        }
    }

    struct Snapshot: Equatable {
        let target: URL
        let data: Data?
        let stamp: Stamp?

        var text: String? { data.flatMap { String(data: $0, encoding: .utf8) } }
    }

    static func osError(_ operation: String, _ path: String, code: Int32 = errno) -> NSError {
        let underlying = NSError(domain: NSPOSIXErrorDomain, code: Int(code))
        return NSError(domain: NSPOSIXErrorDomain, code: Int(code), userInfo: [
            NSFilePathErrorKey: path,
            NSUnderlyingErrorKey: underlying,
            NSLocalizedDescriptionKey: "\(operation) \(path): \(underlying.localizedDescription)"
        ])
    }

    static func resolve(_ path: String, directory: String) throws -> URL {
        guard !path.isEmpty, !path.contains("\0"), !directory.contains("\0"), !directory.isEmpty else {
            throw NativeFileError("Paths and directory must be nonempty and contain no NUL bytes")
        }
        if (path as NSString).isAbsolutePath {
            return URL(fileURLWithPath: path)
        }
        // Keep the requested spelling: Foundation standardization may resolve symlinks,
        // which would erase the path that must be revalidated after approval.
        return URL(fileURLWithPath: directory, isDirectory: true).appendingPathComponent(path)
    }

    static func canonical(_ requested: URL) throws -> URL {
        if let resolved = realpath(requested.path, nil) {
            defer { free(resolved) }
            return URL(fileURLWithPath: String(cString: resolved))
        }
        let code = errno
        guard code == ENOENT else { throw osError("Resolve", requested.path, code: code) }
        // Resolve existing ancestors for a not-yet-created file. Do not treat dangling
        // symlinks as absent files, or silently replace the link during a write.
        var info = stat()
        if lstat(requested.path, &info) == 0 && (info.st_mode & mode_t(S_IFMT)) == mode_t(S_IFLNK) {
            throw osError("Resolve symlink target", requested.path, code: ENOENT)
        }
        let parent = requested.deletingLastPathComponent()
        guard parent.path != requested.path else { throw osError("Resolve", requested.path, code: code) }
        return try canonical(parent).appendingPathComponent(requested.lastPathComponent)
    }

    static func snapshot(_ requested: URL, allowMissing: Bool = false,
                         maxBytes: Int = NativeLimits.fileBytes, textOnly: Bool = true) throws -> Snapshot {
        try Task.checkCancellation()
        let target = try canonical(requested)
        let descriptor = open(target.path, O_RDONLY | O_NONBLOCK | O_NOFOLLOW)
        guard descriptor >= 0 else {
            let code = errno
            if allowMissing && code == ENOENT {
                return Snapshot(target: target, data: nil, stamp: nil)
            }
            throw osError("Read", target.path, code: code)
        }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        defer { try? handle.close() }
        var before = stat()
        guard fstat(descriptor, &before) == 0 else { throw osError("Inspect", target.path) }
        guard (before.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG) else {
            throw NativeFileError("Not a regular file: \(target.path)")
        }
        guard before.st_size <= maxBytes else {
            throw NativeFileError("File exceeds \(maxBytes)-byte limit; not read or modified: \(target.path)")
        }
        var data = Data()
        while data.count <= maxBytes {
            try Task.checkCancellation()
            let part = try handle.read(upToCount: min(65_536, maxBytes + 1 - data.count)) ?? Data()
            if part.isEmpty { break }
            data.append(part)
        }
        guard data.count <= maxBytes else {
            throw NativeFileError("File exceeds \(maxBytes)-byte limit; not read or modified: \(target.path)")
        }
        var after = stat()
        guard fstat(descriptor, &after) == 0 else { throw osError("Inspect", target.path) }
        guard Stamp(before) == Stamp(after) else {
            throw NativeFileError("File changed while being read: \(target.path)")
        }
        if textOnly && (data.contains(0) || String(data: data, encoding: .utf8) == nil) {
            throw NativeFileError("Binary or non-UTF-8 file; text tools will not read or overwrite it: \(target.path)")
        }
        return Snapshot(target: target, data: data, stamp: Stamp(after))
    }

    static func validate(_ requested: URL, against before: Snapshot) throws {
        let current = try snapshot(requested, allowMissing: true)
        guard current == before else {
            throw NativeFileError("File changed since permission preparation; refusing to overwrite: \(requested.path). Prepare a new approval.")
        }
    }

    static func atomicWrite(_ data: Data, requested: URL, before: Snapshot) throws {
        try validate(requested, against: before)
        try Task.checkCancellation()
        let target = before.target
        let temporary = target.deletingLastPathComponent().appendingPathComponent(".native-write-\(UUID().uuidString)")
        let descriptor = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode_t(0o600))
        guard descriptor >= 0 else { throw osError("Create atomic write", temporary.path) }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        defer {
            try? handle.close()
            // unlink does not follow a symlink if another actor replaced the temporary entry.
            _ = unlink(temporary.path)
        }
        try handle.write(contentsOf: data)
        if let stamp = before.stamp {
            guard fchmod(descriptor, mode_t(stamp.mode & 0o7777)) == 0 else {
                throw osError("Preserve file permissions", temporary.path)
            }
        }
        try handle.synchronize()
        try handle.close()
        try validate(requested, against: before)
        try Task.checkCancellation()
        if before.data == nil {
            // An exclusive hard-link publishes the complete new inode without overwriting
            // a file created by the user after the last validation.
            guard link(temporary.path, target.path) == 0 else { throw osError("Save new file", target.path) }
        } else {
            guard rename(temporary.path, target.path) == 0 else { throw osError("Save file", target.path) }
        }
    }

    static func lines(_ text: String) -> [String] {
        guard !text.isEmpty else { return [] }
        var lines = text.components(separatedBy: "\n")
        if text.hasSuffix("\n") { lines.removeLast() }
        return lines.enumerated().map { index, line in
            index < lines.count - 1 || text.hasSuffix("\n") ? line + "\n" : line
        }
    }
}

private struct NativeOutput {
    private(set) var text = ""
    private(set) var truncated = false
    private let notice = "\n[Truncated: output limit reached]\n"

    mutating func append(_ value: String) -> Bool {
        let remaining = NativeLimits.outputBytes - notice.utf8.count - text.utf8.count
        if value.utf8.count <= remaining {
            text += value
            return true
        }
        let prefix = Data(value.utf8.prefix(max(0, remaining)))
        // Drop an incomplete trailing UTF-8 scalar, not replace it with a misleading glyph.
        var valid = prefix
        while !valid.isEmpty && String(data: valid, encoding: .utf8) == nil { valid.removeLast() }
        text += String(data: valid, encoding: .utf8) ?? ""
        truncated = true
        return false
    }

    func result(metadata: [String: Any] = [:], incomplete: Bool = false) -> NativeToolResult {
        var values = metadata
        values["truncated"] = truncated || incomplete
        return NativeToolResult(output: text + (truncated ? notice : ""), metadata: values)
    }
}

private enum NativeDiff {
    struct Patch {
        let text: String
        let additions: Int
        let deletions: Int
    }

    private struct Line {
        let kind: String
        let text: String
    }

    private static func same(_ left: String, _ right: String) -> Bool {
        left.utf8.elementsEqual(right.utf8)
    }

    static func make(file: String, before: String?, after: String?) throws -> Patch {
        let old = NativeFS.lines(before ?? "")
        let new = NativeFS.lines(after ?? "")
        var prefix = 0
        while prefix < min(old.count, new.count) && same(old[prefix], new[prefix]) { prefix += 1 }
        var suffix = 0
        while suffix < min(old.count, new.count) - prefix && same(old[old.count - suffix - 1], new[new.count - suffix - 1]) {
            suffix += 1
        }
        let a = Array(old[prefix..<(old.count - suffix)])
        let b = Array(new[prefix..<(new.count - suffix)])
        var operations = old.prefix(prefix).map { Line(kind: " ", text: $0) }
        // Bounded LCS gives compact ordinary edits. For very large edits, replacing the
        // changed middle is still an exact, applicable unified patch, not a fabricated preview.
        if !a.isEmpty && !b.isEmpty && a.count <= 1_000_000 / b.count {
            let width = b.count + 1
            var table = [Int32](repeating: 0, count: (a.count + 1) * width)
            for i in stride(from: a.count - 1, through: 0, by: -1) {
                try Task.checkCancellation()
                for j in stride(from: b.count - 1, through: 0, by: -1) {
                    table[i * width + j] = same(a[i], b[j])
                        ? table[(i + 1) * width + j + 1] + 1
                        : max(table[(i + 1) * width + j], table[i * width + j + 1])
                }
            }
            var i = 0, j = 0
            while i < a.count || j < b.count {
                if i < a.count && j < b.count && same(a[i], b[j]) {
                    operations.append(Line(kind: " ", text: a[i])); i += 1; j += 1
                } else if i < a.count && (j == b.count || table[(i + 1) * width + j] >= table[i * width + j + 1]) {
                    operations.append(Line(kind: "-", text: a[i])); i += 1
                } else {
                    operations.append(Line(kind: "+", text: b[j])); j += 1
                }
            }
        } else {
            operations += a.map { Line(kind: "-", text: $0) }
            operations += b.map { Line(kind: "+", text: $0) }
        }
        operations += old.suffix(suffix).map { Line(kind: " ", text: $0) }
        let changed = operations.indices.filter { operations[$0].kind != " " }
        let additions = operations.filter { $0.kind == "+" }.count
        let deletions = operations.filter { $0.kind == "-" }.count
        if changed.isEmpty && (before == nil) == (after == nil) {
            return Patch(text: "", additions: 0, deletions: 0)
        }
        let label = quotePath(file)
        var patch = "--- \(before == nil ? "/dev/null" : label)\n+++ \(after == nil ? "/dev/null" : label)\n"
        var ranges: [Range<Int>] = []
        for index in changed {
            let start = max(0, index - 3), end = min(operations.count, index + 4)
            if let previous = ranges.last, start <= previous.upperBound {
                ranges[ranges.count - 1] = previous.lowerBound..<max(previous.upperBound, end)
            } else {
                ranges.append(start..<end)
            }
        }
        var oldAt = 1, newAt = 1, cursor = 0
        for range in ranges {
            try Task.checkCancellation()
            while cursor < range.lowerBound {
                if operations[cursor].kind != "+" { oldAt += 1 }
                if operations[cursor].kind != "-" { newAt += 1 }
                cursor += 1
            }
            let part = operations[range]
            let oldCount = part.filter { $0.kind != "+" }.count
            let newCount = part.filter { $0.kind != "-" }.count
            patch += "@@ -\(oldCount == 0 ? oldAt - 1 : oldAt),\(oldCount) +\(newCount == 0 ? newAt - 1 : newAt),\(newCount) @@\n"
            for line in part {
                patch += line.kind + line.text
                if !line.text.hasSuffix("\n") { patch += "\n\\ No newline at end of file\n" }
                if line.kind != "+" { oldAt += 1 }
                if line.kind != "-" { newAt += 1 }
            }
            cursor = range.upperBound
        }
        return Patch(text: patch, additions: additions, deletions: deletions)
    }

    private static func quotePath(_ path: String) -> String {
        guard path.contains(where: { $0 == "\n" || $0 == "\r" || $0 == "\t" || $0 == "\"" || $0 == "\\" }) else { return path }
        return "\"" + path.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
            .replacingOccurrences(of: "\t", with: "\\t")
            .replacingOccurrences(of: "\r", with: "\\r")
            .replacingOccurrences(of: "\n", with: "\\n") + "\""
    }
}

/// Local patch records, not a Git repository or execution history supplied by a client.
/// Instances for the same canonical root share a writer lock and reload under that lock.
public final class NativePatchLedger {
    private struct Record: Codable {
        let id: String
        let groupID: String
        let directory: String
        let sessionID: String
        let turnID: String
        let time: Int64
        let title: String
        let file: String
        let before: String?
        let after: String?
        let patch: String
        let additions: Int
        let deletions: Int

        func fileObject(includePatch: Bool) -> [String: Any] {
            var value: [String: Any] = [
                "file": file, "source": "local", "connectionID": "", "key": id,
                "additions": additions, "deletions": deletions,
                "status": before == nil ? "added" : (after == nil ? "deleted" : "modified")
            ]
            if includePatch { value["patch"] = patch }
            return value
        }
    }

    private struct Store: Codable {
        var version = 1
        var records: [Record] = []
    }

    private static let registryLock = NSLock()
    private static var locks: [String: NSLock] = [:]
    private let fileURL: URL
    private let lock: NSLock

    public init(root: URL) throws {
        guard root.isFileURL else { throw NativeFileError("Native patch history requires a file URL") }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let canonical = try NativeFS.canonical(root)
        fileURL = canonical.appendingPathComponent("patches.json")
        Self.registryLock.lock()
        let shared = Self.locks[canonical.path] ?? NSLock()
        Self.locks[canonical.path] = shared
        Self.registryLock.unlock()
        lock = shared
        lock.lock()
        defer { lock.unlock() }
        _ = try load()
    }

    private func load() throws -> Store {
        let snapshot = try NativeFS.snapshot(fileURL, allowMissing: true, maxBytes: NativeLimits.ledgerBytes, textOnly: false)
        guard let data = snapshot.data else { return Store() }
        let store = try JSONDecoder().decode(Store.self, from: data)
        guard store.version == 1 else { throw NativeFileError("Unsupported native patch history version") }
        return store
    }

    private static func directoryKey(_ directory: String) throws -> String {
        try NativeFS.canonical(NativeFS.resolve(directory, directory: FileManager.default.currentDirectoryPath)).path
    }

    public func append(directory: String, sessionID: String, turnID: String, title: String,
                       file: String, before: String?, after: String?, patch: String) throws {
        _ = try record(directory: directory, sessionID: sessionID, turnID: turnID, title: title,
                       file: file, before: before, after: after, patch: patch, mutation: nil)
    }

    // Encode and check storage limits before touching the user's file. Publish the record
    // only after a confirmed atomic file write. A persistence failure is explicitly reported
    // as "file saved, history failed", never a normal saved result or a fictitious patch.
    fileprivate func record(directory: String, sessionID: String, turnID: String, title: String,
                            file: String, before: String?, after: String?, patch: String,
                            mutation: (() throws -> Void)?) throws -> [String: Any] {
        guard !sessionID.isEmpty, !turnID.isEmpty, sessionID.utf8.count <= 512, turnID.utf8.count <= 512,
              file.utf8.count <= 16_384, title.utf8.count <= 16_384,
              (before?.utf8.count ?? 0) <= NativeLimits.fileBytes,
              (after?.utf8.count ?? 0) <= NativeLimits.fileBytes,
              patch.utf8.count <= 4 * NativeLimits.fileBytes else {
            throw NativeFileError("Invalid or oversized native patch record")
        }
        let key = try Self.directoryKey(directory)
        let counts = try NativeDiff.make(file: file, before: before, after: after)
        lock.lock()
        defer { lock.unlock() }
        var store = try load()
        let groupID = store.records.first {
            $0.directory == key && $0.sessionID == sessionID && $0.turnID == turnID
        }?.groupID ?? UUID().uuidString.lowercased()
        let record = Record(id: UUID().uuidString.lowercased(), groupID: groupID, directory: key,
                            sessionID: sessionID, turnID: turnID,
                            time: Int64(Date().timeIntervalSince1970 * 1000), title: title,
                            file: file, before: before, after: after, patch: patch,
                            additions: counts.additions, deletions: counts.deletions)
        store.records.append(record)
        guard store.records.count <= 20_000 else { throw NativeFileError("Native patch history record limit reached") }
        let encoded = try JSONEncoder().encode(store)
        guard encoded.count <= NativeLimits.ledgerBytes else { throw NativeFileError("Native patch history storage limit reached") }
        try Task.checkCancellation()
        try mutation?()
        do {
            try encoded.write(to: fileURL, options: .atomic)
        } catch {
            if mutation != nil {
                throw NativeFileError("File was saved, but native patch history could not be persisted: \(error.localizedDescription)")
            }
            throw error
        }
        return ["historyKey": record.id, "historyCommit": "native:\(groupID)", "historyKind": "native-patch"]
    }

    public func action(_ name: String, data: [String: Any]) throws -> [String: Any] {
        guard let directory = data["directory"] as? String else { throw NativeFileError("Missing history directory") }
        let key = try Self.directoryKey(directory)
        switch name {
        case "historyRegister":
            return ["registered": true, "historyKind": "native-patch"]
        case "historyHint", "historyRecord":
            // The bridge's hints/turn manifests are not evidence that a native tool ran.
            return ["saved": false, "ignored": true, "historyKind": "native-patch"]
        case "historyList", "historyShow":
            guard let session = data["sessionID"] as? String, !session.isEmpty else {
                throw NativeFileError("Missing history sessionID")
            }
            lock.lock()
            defer { lock.unlock() }
            let records = try load().records.filter { $0.directory == key && $0.sessionID == session }
            if name == "historyShow" {
                guard let token = data["commit"] as? String, token.hasPrefix("native:") else {
                    throw NativeFileError("Expected a native:<id> patch token, not a Git commit")
                }
                let group = String(token.dropFirst("native:".count))
                let matching = records.filter { $0.groupID == group }
                guard !matching.isEmpty else { throw NativeFileError("Native patch record not found in this directory/session") }
                return ["files": matching.map { $0.fileObject(includePatch: true) }, "historyKind": "native-patch"]
            }
            var groups: [String] = []
            var seen = Set<String>()
            for record in records.reversed() where seen.insert(record.groupID).inserted { groups.append(record.groupID) }
            let grouped = Dictionary(grouping: records, by: { $0.groupID })
            let turns: [[String: Any]] = groups.compactMap { group in
                guard let changes = grouped[group], let first = changes.first else { return nil }
                return ["id": first.turnID, "time": first.time, "title": first.title,
                        "commit": "native:\(group)", "files": changes.map { $0.fileObject(includePatch: false) }]
            }
            return ["turns": turns, "historyKind": "native-patch"]
        default:
            throw NativeFileError("Unknown native history action: \(name)")
        }
    }
}

/// Foundation/POSIX file tools. Paths are OS paths, not confined to the selected project.
/// Callers handle app permissions/security-scoped access; OS failures are not disguised.
public final class NativeFileTools {
    private let history: NativePatchLedger

    public init(history: NativePatchLedger) { self.history = history }

    public var definitions: [[String: Any]] {
        func string(_ description: String) -> [String: Any] { ["type": "string", "description": description] }
        func schema(_ properties: [String: Any], _ required: [String] = []) -> [String: Any] {
            ["type": "object", "properties": properties, "required": required, "additionalProperties": false]
        }
        let path = string("Optional search/list path; relative to directory or absolute. Defaults to directory.")
        let file = string("UTF-8 file path; relative to directory or absolute. OS access rules apply.")
        return [
            ["name": "list", "description": "List immediate directory entries natively, with bounded output.",
             "parameters": schema(["path": path])],
            ["name": "read", "description": "Read a bounded UTF-8 text snapshot with 1-based line numbers. Binary and oversized files are rejected.",
             "parameters": schema(["filePath": file,
                                   "offset": ["type": "integer", "minimum": 1, "description": "First line, 1-based; default 1."],
                                   "limit": ["type": "integer", "minimum": 1, "maximum": NativeLimits.matches, "description": "Maximum lines; default 1000."]], ["filePath"])],
            ["name": "glob", "description": "Find files using *, ?, ** and character classes. **/ includes zero directories. Skips .git and symlink traversal; reports limits.",
             "parameters": schema(["pattern": string("Glob matched against relative paths; a pattern without / also matches file basenames."), "path": path], ["pattern"])],
            ["name": "grep", "description": "Search UTF-8 files with NSRegularExpression, returning file:line:text. Skips .git, symlinks and binary files; reports incomplete searches.",
             "parameters": schema(["pattern": string("Regular expression, evaluated per line."), "path": path], ["pattern"])],
            ["name": "write", "description": "Atomically write bounded UTF-8 content. Approval contains the actual diff; refuses changed snapshots. Parent directory must exist. Records native patches, not Git commits.",
             "parameters": schema(["filePath": file, "content": string("Complete UTF-8 file content (at most 1 MiB, no NUL bytes).")], ["filePath", "content"])],
            ["name": "edit", "description": "Replace exact literal text. Requires a unique match unless replaceAll=true; snapshot-checked atomic write and native patch history.",
             "parameters": schema(["filePath": file, "oldString": string("Nonempty literal text to replace."),
                                   "newString": string("Replacement text."), "replaceAll": ["type": "boolean", "default": false]], ["filePath", "oldString", "newString"])]
        ]
    }

    public func prepare(name: String, arguments: [String: Any], directory: String,
                        sessionID: String, turnID: String) throws -> NativePreparedTool {
        try Task.checkCancellation()
        let allowed: [String: Set<String>] = [
            "list": ["path"], "read": ["filePath", "offset", "limit"],
            "glob": ["pattern", "path"], "grep": ["pattern", "path"],
            "write": ["filePath", "content"], "edit": ["filePath", "oldString", "newString", "replaceAll"]
        ]
        guard let keys = allowed[name] else { throw NativeFileError("Unknown native file tool: \(name)") }
        guard Set(arguments.keys).isSubset(of: keys) else { throw NativeFileError("Unexpected arguments for \(name)") }
        let path: String
        if name == "read" || name == "write" || name == "edit" {
            path = try Self.string("filePath", arguments)
        } else {
            path = arguments["path"] == nil ? "." : try Self.string("path", arguments)
        }
        let url = try NativeFS.resolve(path, directory: directory)
        let target = try NativeFS.canonical(url)
        let patterns = url.path == target.path ? [url.path] : [url.path, target.path]
        let title = "\(name.capitalized) \(url.path)"
        let base: [String: Any] = ["filepath": url.path, "resolvedPath": target.path]
        switch name {
        case "read":
            let offset = try Self.integer("offset", arguments, defaultValue: 1, maximum: Int.max)
            let limit = try Self.integer("limit", arguments, defaultValue: NativeLimits.matches, maximum: NativeLimits.matches)
            let snapshot = try NativeFS.snapshot(url)
            let lines = NativeFS.lines(snapshot.text ?? "")
            let start = min(offset - 1, lines.count)
            let end = start + min(limit, lines.count - start)
            var output = NativeOutput()
            for index in start..<end {
                try Task.checkCancellation()
                let line = lines[index]
                if !output.append("\(index + 1): " + line + (line.hasSuffix("\n") ? "" : "\n")) { break }
            }
            let paginated = end < lines.count
            if paginated { _ = output.append("[Truncated: more lines available; use offset \(end + 1)]\n") }
            var metadata = base
            metadata["offset"] = offset
            metadata["totalLines"] = lines.count
            let result = output.result(metadata: metadata, incomplete: paginated)
            return NativePreparedTool(name: name, title: title, modifies: false, patterns: patterns, metadata: result.metadata) { result }
        case "list", "glob", "grep":
            let pattern = name == "list" ? "" : try Self.string("pattern", arguments)
            guard pattern.utf8.count <= 4096 else { throw NativeFileError("Search pattern exceeds 4096-byte limit") }
            let regex: NSRegularExpression?
            if name == "glob" { regex = try Self.globRegex(pattern) }
            else if name == "grep" { regex = try NSRegularExpression(pattern: pattern) }
            else { regex = nil }
            return NativePreparedTool(name: name, title: title, modifies: false, patterns: patterns, metadata: base) {
                try Self.search(name: name, root: url, pattern: pattern, regex: regex, base: base)
            }
        case "write", "edit":
            let before = try NativeFS.snapshot(url, allowMissing: name == "write")
            let content: String
            if name == "write" {
                content = try Self.string("content", arguments)
            } else {
                let old = try Self.string("oldString", arguments)
                let new = try Self.string("newString", arguments)
                guard !old.isEmpty else { throw NativeFileError("oldString must not be empty") }
                guard old.utf8.count <= NativeLimits.fileBytes, new.utf8.count <= NativeLimits.fileBytes,
                      !old.contains("\0"), !new.contains("\0") else {
                    throw NativeFileError("Edit strings must be bounded UTF-8 text without NUL bytes")
                }
                let replaceAll = try Self.boolean("replaceAll", arguments, defaultValue: false)
                let text = (before.text ?? "") as NSString
                var ranges: [NSRange] = []
                var at = 0
                while at < text.length {
                    try Task.checkCancellation()
                    let range = text.range(of: old, options: .literal, range: NSRange(location: at, length: text.length - at))
                    if range.location == NSNotFound { break }
                    ranges.append(range)
                    if !replaceAll && ranges.count > 1 { throw NativeFileError("Ambiguous edit: oldString occurs more than once; use replaceAll=true") }
                    at = range.location + range.length
                }
                guard !ranges.isEmpty else { throw NativeFileError("oldString was not found; no file was changed") }
                let estimate = text.length + ranges.count * ((new as NSString).length - (old as NSString).length)
                guard estimate <= NativeLimits.fileBytes else { throw NativeFileError("Edited content exceeds file limit") }
                let changed = NSMutableString(string: before.text ?? "")
                for range in ranges.reversed() { changed.replaceCharacters(in: range, with: new) }
                content = changed as String
            }
            guard content.utf8.count <= NativeLimits.fileBytes, !content.contains("\0") else {
                throw NativeFileError("Content must be UTF-8 text, at most 1 MiB, with no NUL bytes")
            }
            guard !sessionID.isEmpty, !turnID.isEmpty else { throw NativeFileError("Writes require sessionID and user-message turnID for native patch history") }
            let patch = try NativeDiff.make(file: url.path, before: before.text, after: content)
            var metadata = base
            metadata["diff"] = patch.text
            metadata["before"] = before.text.map { $0 as Any } ?? NSNull()
            metadata["after"] = content
            metadata["additions"] = patch.additions
            metadata["deletions"] = patch.deletions
            metadata["historyKind"] = "native-patch"
            let preparedMetadata = metadata
            return NativePreparedTool(name: name, title: title, modifies: true, patterns: patterns, metadata: metadata) { [history] in
                NativeFS.mutationLock.lock()
                defer { NativeFS.mutationLock.unlock() }
                try NativeFS.validate(url, against: before)
                let bytes = Data(content.utf8)
                if before.data == bytes {
                    var values = preparedMetadata
                    values["saved"] = false
                    return NativeToolResult(output: "No changes; file already contains the requested content.", metadata: values)
                }
                let record = try history.record(directory: directory, sessionID: sessionID, turnID: turnID, title: title,
                                                file: url.path, before: before.text, after: content, patch: patch.text) {
                    try NativeFS.atomicWrite(bytes, requested: url, before: before)
                }
                var values = preparedMetadata.merging(record, uniquingKeysWith: { _, new in new })
                values["saved"] = true
                var output = NativeOutput()
                _ = output.append("Saved \(url.path). Recorded native patch (not a Git commit).\n" + patch.text)
                return output.result(metadata: values)
            }
        default:
            throw NativeFileError("Unknown native file tool: \(name)")
        }
    }

    private static func string(_ key: String, _ arguments: [String: Any]) throws -> String {
        guard let value = arguments[key] as? String else { throw NativeFileError("\(key) must be a string") }
        return value
    }

    private static func integer(_ key: String, _ arguments: [String: Any], defaultValue: Int, maximum: Int) throws -> Int {
        guard let raw = arguments[key] else { return defaultValue }
        guard let number = raw as? NSNumber, String(cString: number.objCType) != "c",
              number.doubleValue.isFinite, number.doubleValue >= 1,
              number.doubleValue < Double(Int.max), number.doubleValue.rounded(.towardZero) == number.doubleValue,
              number.doubleValue <= Double(maximum) else { throw NativeFileError("\(key) must be a positive integer (maximum \(maximum))") }
        return number.intValue
    }

    private static func boolean(_ key: String, _ arguments: [String: Any], defaultValue: Bool) throws -> Bool {
        guard let raw = arguments[key] else { return defaultValue }
        guard let number = raw as? NSNumber, String(cString: number.objCType) == "c",
              let value = raw as? Bool else { throw NativeFileError("\(key) must be a boolean") }
        return value
    }

    private static func globRegex(_ pattern: String) throws -> NSRegularExpression {
        guard !pattern.isEmpty else { throw NativeFileError("Glob pattern must not be empty") }
        let chars = Array(pattern)
        var expression = "^", i = 0
        while i < chars.count {
            switch chars[i] {
            case "*":
                if i + 1 < chars.count && chars[i + 1] == "*" {
                    i += 1
                    if i + 1 < chars.count && chars[i + 1] == "/" { expression += "(?:.*/)?"; i += 1 }
                    else { expression += ".*" }
                } else { expression += "[^/]*" }
            case "?": expression += "[^/]"
            case "[":
                var end = i + 1
                while end < chars.count && chars[end] != "]" { end += 1 }
                guard end < chars.count, end > i + 1 else { throw NativeFileError("Invalid glob character class") }
                var body = String(chars[(i + 1)..<end])
                if body.hasPrefix("!") { body = "^" + body.dropFirst() }
                expression += "(?!/)[" + body + "]"
                i = end
            case "\\":
                i += 1
                guard i < chars.count else { throw NativeFileError("Invalid trailing glob escape") }
                expression += NSRegularExpression.escapedPattern(for: String(chars[i]))
            default: expression += NSRegularExpression.escapedPattern(for: String(chars[i]))
            }
            i += 1
        }
        return try NSRegularExpression(pattern: expression + "$")
    }

    private struct Traversal {
        let urls: [URL]
        let incomplete: Bool
        let warnings: [String]
    }

    private static func traverse(_ root: URL, recursive: Bool) throws -> Traversal {
        let attributes = try FileManager.default.attributesOfItem(atPath: NativeFS.canonical(root).path)
        guard attributes[.type] as? FileAttributeType == .typeDirectory else {
            if recursive && attributes[.type] as? FileAttributeType == .typeRegular {
                return Traversal(urls: [root], incomplete: false, warnings: [])
            }
            throw NativeFileError("Not a directory: \(root.path)")
        }
        var warnings: [String] = []
        guard let enumerator = FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey],
                                                              options: [], errorHandler: { url, error in
            if warnings.count < 20 { warnings.append("Skipped \(url.path): \(error.localizedDescription)") }
            return true
        }) else { throw NativeFileError("Unable to enumerate directory: \(root.path)") }
        var urls: [URL] = [], count = 0, incomplete = false
        for case let url as URL in enumerator {
            try Task.checkCancellation()
            count += 1
            if count > NativeLimits.entries { incomplete = true; enumerator.skipDescendants(); break }
            let values: URLResourceValues
            do { values = try url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]) }
            catch {
                enumerator.skipDescendants()
                incomplete = true
                if warnings.count < 20 { warnings.append("Skipped \(url.path): \(error.localizedDescription)") }
                continue
            }
            if !recursive { enumerator.skipDescendants(); urls.append(url); continue }
            if url.lastPathComponent == ".git" || values.isSymbolicLink == true {
                enumerator.skipDescendants()
                continue
            }
            if values.isDirectory != true { urls.append(url) }
        }
        if incomplete { warnings.append("Truncated: traversal limit or inaccessible entries; search is incomplete") }
        return Traversal(urls: urls.sorted { $0.path < $1.path }, incomplete: incomplete || !warnings.isEmpty, warnings: warnings)
    }

    private static func search(name: String, root: URL, pattern: String, regex: NSRegularExpression?, base: [String: Any]) throws -> NativeToolResult {
        // Foundation enumeration can spell /var as /private/var on Apple platforms.
        // Use the same canonical root for traversal and relative search paths, while
        // preserving requested + resolved paths in the prepared permission metadata.
        let searchRoot = try NativeFS.canonical(root)
        let traversal = try traverse(searchRoot, recursive: name != "list")
        var output = NativeOutput()
        var incomplete = traversal.incomplete
        var skipped = 0, count = 0, bytes = 0
        var warnings = traversal.warnings
        let deadline = ProcessInfo.processInfo.systemUptime + 5
        for url in traversal.urls {
            try Task.checkCancellation()
            if ProcessInfo.processInfo.systemUptime >= deadline { incomplete = true; warnings.append("Truncated: search time limit reached"); break }
            let relative = url.path.hasPrefix(searchRoot.path + "/") ? String(url.path.dropFirst(searchRoot.path.count + 1)) : url.lastPathComponent
            if name == "list" {
                let values = try url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
                let suffix = values.isSymbolicLink == true ? "@" : (values.isDirectory == true ? "/" : "")
                count += 1
                if !output.append(relative + suffix + "\n") { break }
            } else if name == "glob", let regex = regex {
                let candidate = pattern.contains("/") ? relative : url.lastPathComponent
                if regex.firstMatch(in: candidate, range: NSRange(location: 0, length: (candidate as NSString).length)) != nil {
                    count += 1
                    if !output.append(relative + "\n") { break }
                }
            } else if let regex = regex {
                let snapshot: NativeFS.Snapshot
                do { snapshot = try NativeFS.snapshot(url) }
                catch is CancellationError { throw CancellationError() }
                catch {
                    skipped += 1; incomplete = true
                    if warnings.count < 20 { warnings.append("Skipped \(relative): \(error.localizedDescription)") }
                    continue
                }
                bytes += snapshot.data?.count ?? 0
                if bytes > NativeLimits.searchBytes { incomplete = true; warnings.append("Truncated: search byte limit reached"); break }
                for (index, line) in NativeFS.lines(snapshot.text ?? "").enumerated() {
                    try Task.checkCancellation()
                    let text = line.hasSuffix("\n") ? String(line.dropLast()) : line
                    let length = (text as NSString).length
                    if length > NativeLimits.regexLineUnits {
                        incomplete = true; skipped += 1
                        if warnings.count < 20 { warnings.append("Skipped \(relative):\(index + 1): line exceeds regex length limit") }
                        continue
                    }
                    var found = false, timedOut = false, cancelled = false
                    regex.enumerateMatches(in: text, options: .reportProgress, range: NSRange(location: 0, length: length)) { match, _, stop in
                        do { try Task.checkCancellation() } catch { cancelled = true; stop.pointee = true; return }
                        if ProcessInfo.processInfo.systemUptime >= deadline { timedOut = true; stop.pointee = true; return }
                        if match != nil { found = true; stop.pointee = true }
                    }
                    if cancelled { throw CancellationError() }
                    if timedOut { incomplete = true; warnings.append("Truncated: regex time limit reached"); break }
                    if found {
                        count += 1
                        if !output.append("\(relative):\(index + 1):\(text)\n") { break }
                    }
                    if count >= NativeLimits.matches { incomplete = true; break }
                }
            }
            if output.truncated { incomplete = true; break }
            if count >= NativeLimits.matches { incomplete = true; warnings.append("Truncated: result limit reached"); break }
        }
        for warning in warnings { if !output.append("[\(warning)]\n") { break } }
        if count == 0 && warnings.isEmpty { _ = output.append(name == "list" ? "Directory is empty.\n" : "No matches.\n") }
        var metadata = base
        metadata["count"] = count
        metadata["skipped"] = skipped
        metadata["warnings"] = warnings
        return output.result(metadata: metadata, incomplete: incomplete)
    }
}
