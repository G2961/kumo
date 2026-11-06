import Foundation

/// Git commits containing agent patches only; never stages the user's worktree/index.
/// iOS counterpart of `ChangeHistory.kt`, implemented on top of the system `git` binary
/// instead of JGit. Operates purely on refs under `refs/codex/changes/<session>` and
/// plumbing commands so no user hooks / index are touched.
public final class ChangeHistory {

    private let registered: () -> [String]
    private let gitPath = "/usr/bin/git"
    private static let identityName = "Codex Mobile"
    private static let identityEmail = "agent@codex-mobile.invalid"

    /// Regexes transferred from Kotlin.
    private let sessionPattern = #"ses_[A-Za-z0-9_-]+"#
    private let hunkPattern = #"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*"#

    public init(registered: @escaping () -> [String]) {
        self.registered = registered
    }

    // MARK: - Git plumbing

    private struct GitError: Error, LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }

    private enum Result {
        case success(String)
        case failure(String)
    }

    private func git(_ args: [String], in dir: URL) throws -> String {
        let p = KumoProcess()
        p.executableURL = URL(fileURLWithPath: gitPath)
        p.arguments = ["-c", "user.name=\(Self.identityName)",
                       "-c", "user.email=\(Self.identityEmail)"] + args
        p.currentDirectoryURL = dir
        p.environment = ProcessInfo.processInfo.environment
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        try p.run()
        p.waitUntilExit()
        let outData = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        if p.terminationStatus != 0 {
            throw GitError(String(data: errData, encoding: .utf8) ?? "git failed")
        }
        return String(data: outData, encoding: .utf8) ?? ""
    }

    // MARK: - Project validation

    private func project(_ directory: String) throws -> URL {
        let root = URL(fileURLWithPath: directory).standardizedFileURL
        guard registered().contains(root.path) else { throw GitError("Project is not registered") }
        let dotGit = root.appendingPathComponent(".git")
        if FileManager.default.fileExists(atPath: dotGit.path) {
            var isDir: ObjCBool = false
            _ = FileManager.default.fileExists(atPath: dotGit.path, isDirectory: &isDir)
            guard isDir.boolValue else { throw GitError("History requires a Git directory inside the registered project") }
        }
        return root
    }

    @discardableResult
    public func register(_ directory: String) throws -> [String: Any] {
        let root = try project(directory)
        if !FileManager.default.fileExists(atPath: root.appendingPathComponent(".git").path) {
            _ = try git(["init", "-q"], in: root)
            // Empty identity commit so opencode recognizes the project.
            let args = ["-c", "user.name=\(Self.identityName)",
                        "-c", "user.email=\(Self.identityEmail)",
                        "commit", "-q", "--allow-empty", "-m", "Initialize mobile project"]
            _ = try git(args, in: root)
        }
        return ["registered": true]
    }

    private func open(_ directory: String) throws -> URL {
        _ = try register(directory)
        return try project(directory)
    }

    private func ref(_ session: String) -> String {
        guard session.range(of: sessionPattern, options: .regularExpression) != nil else {
            assertionFailure("Invalid history session")
            return "refs/codex/changes/invalid"
        }
        return "refs/codex/changes/\(session)"
    }

    // MARK: - Patch compaction (transferred from Kotlin)

    private func compact(_ patch: String) throws -> String {
        guard patch.utf8.count <= 1024 * 1024 else { throw GitError("Patch exceeds history limit") }
        let lines = patch.components(separatedBy: "\n")
        var result: [String] = []
        var header: [String] = []
        var i = 0

        while i < lines.count {
            let line = lines[i]
            if line.hasPrefix("--- ") && i + 1 < lines.count && lines[i + 1].hasPrefix("+++ ") {
                header = [line, lines[i + 1]]
                i += 2
                continue
            }
            guard parseHunk(line) != nil else {
                i += 1
                continue
            }
            i += 1

            var body: [String] = []
            var oldRemaining = hunkCount(line, which: .old)
            var newRemaining = hunkCount(line, which: .new)
            while oldRemaining > 0 || newRemaining > 0 {
                guard i < lines.count else { throw GitError("Incomplete patch hunk") }
                let text = lines[i]
                i += 1
                switch text.first {
                case " ":
                    oldRemaining -= 1; newRemaining -= 1
                case "-":
                    oldRemaining -= 1
                case "+":
                    newRemaining -= 1
                case "\\":
                    guard text.hasPrefix("\\ No newline") else { throw GitError("Invalid patch annotation") }
                default:
                    throw GitError("Invalid patch hunk")
                }
                guard oldRemaining >= 0 && newRemaining >= 0 else { throw GitError("Invalid patch line count") }
                body.append(text)
            }
            if i < lines.count && lines[i].hasPrefix("\\ No newline") {
                body.append(lines[i]); i += 1
            }
            let changes = body.indices.filter { body[$0].hasPrefix("+") || body[$0].hasPrefix("-") }
            guard !changes.isEmpty, header.count == 2 else { continue }

            result.append(contentsOf: header)
            // Merge adjacent changed hunks into ranges (context 3 lines).
            var ranges: [Range<Int>] = []
            for changed in changes {
                let start = max(0, changed - 3)
                var end = min(body.count - 1, changed + 3)
                if end < body.count - 1 && body[end + 1].hasPrefix("\\") { end += 1 }
                if let last = ranges.last, start <= last.upperBound {
                    ranges[ranges.count - 1] = last.lowerBound..<max(end + 1, last.upperBound)
                } else {
                    ranges.append(start..<(end + 1))
                }
            }
            let oldBase = hunkLineNo(line, which: .old)
            let newBase = hunkLineNo(line, which: .new)
            var oldAt = [Int](repeating: 0, count: body.count)
            var newAt = [Int](repeating: 0, count: body.count)
            var oldCursor = oldBase, newCursor = newBase
            for (n, t) in body.enumerated() {
                oldAt[n] = oldCursor; newAt[n] = newCursor
                if t.hasPrefix(" ") || t.hasPrefix("-") { oldCursor += 1 }
                if t.hasPrefix(" ") || t.hasPrefix("+") { newCursor += 1 }
            }
            for range in ranges {
                let part = Array(body[range])
                let oldCount = part.filter { $0.hasPrefix(" ") || $0.hasPrefix("-") }.count
                let newCount = part.filter { $0.hasPrefix(" ") || $0.hasPrefix("+") }.count
                let oldStart = oldCount == 0 ? max(0, oldAt[range.lowerBound] - 1) : oldAt[range.lowerBound]
                let newStart = newCount == 0 ? max(0, newAt[range.lowerBound] - 1) : newAt[range.lowerBound]
                result.append("@@ -\(oldStart),\(oldCount) +\(newStart),\(newCount) @@")
                result.append(contentsOf: part)
            }
        }
        if result.isEmpty { return "" }
        return result.joined(separator: "\n") + "\n"
    }

    private enum HunkSide { case old, new }

    private func hunkPatternRegex() -> NSRegularExpression {
        try! NSRegularExpression(pattern: hunkPattern)
    }

    private func hunkRange(_ line: String) -> NSRange? {
        let r = hunkPatternRegex().firstMatch(in: line, range: NSRange(location: 0, length: (line as NSString).length))
        return r?.range
    }

    private func parseHunk(_ line: String) -> (Int, Int, Int, Int)? {
        guard let m = hunkPatternRegex().firstMatch(in: line, range: NSRange(location: 0, length: (line as NSString).length)) else {
            return nil
        }
        func g(_ i: Int) -> String {
            (line as NSString).substring(with: m.range(at: i))
        }
        let old1 = Int(g(1)) ?? 0
        let old2 = Int(g(2)) ?? 1
        let new1 = Int(g(3)) ?? 0
        let new2 = Int(g(4)) ?? 1
        return (old1, old2, new1, new2)
    }

    private func hunkCount(_ line: String, which: HunkSide) -> Int {
        guard let (_, oc, _, nc) = parseHunk(line) else { return 0 }
        return which == .old ? oc : nc
    }

    private func hunkLineNo(_ line: String, which: HunkSide) -> Int {
        guard let (o, _, n, _) = parseHunk(line) else { return 0 }
        return which == .old ? o : n
    }

    // MARK: - Actions

    public struct Turn {
        public let id: String
        public let title: String
        public let time: Int64
        public let files: [[String: Any]]
    }

    @discardableResult
    public func record(directory: String, sessionID: String, turns: [Turn], hints: [String: String]) throws -> [String: Any] {
        let root = try open(directory)
        let session = ref(sessionID)

        // Resolve current ref head.
        let head = try? git(["rev-parse", "--verify", session], in: root).trimmingCharacters(in: .whitespacesAndNewlines)
        let previous = try logCommits(root, ref: session)
        var known = Set(previous.compactMap { manifestID($0) })

        var parent: String? = (head?.isEmpty == false) ? head : nil
        for turn in turns {
            guard !known.contains(turn.id) else { continue }
            var filesMeta: [[String: Any]] = []
            var patches: [String] = []
            for f in turn.files {
                let source = f["source"] as? String ?? "local"
                var file = f["file"] as? String ?? ""
                if source == "local" {
                    guard let rel = try? relativize(root, path: file, inputIsAbsolute: (file as NSString).isAbsolutePath) else {
                        continue
                    }
                    file = rel
                }
                let text = (f["patch"] as? String)
                    ?? hints[(f["key"] as? String) ?? ""]
                    ?? ""
                let patch = try compact(text)
                if patch.isEmpty { continue }
                let (add, del) = counts(patch)
                let patchPath = "patches/\(String(format: "%04d", patches.count)).patch"
                filesMeta.append([
                    "key": f["key"] ?? "", "file": file, "source": source,
                    "connectionID": f["connectionID"] ?? "",
                    "additions": add, "deletions": del, "patchPath": patchPath
                ])
                patches.append(patch)
            }
            guard !filesMeta.isEmpty else { continue }
            let metadata: [String: Any] = [
                "id": turn.id, "sessionID": sessionID,
                "title": String(turn.title.prefix(160)), "time": turn.time, "files": filesMeta
            ]
            parent = try commitPatches(root, patches: patches, metadata: metadata,
                                       session: session, parent: parent, message: "Agent turn \(turn.id)")
            known.insert(turn.id)
        }
        return ["saved": true]
    }

    public func hint(directory: String, sessionID: String, key: String, patch: String) throws -> [String: Any] {
        let root = try open(directory)
        let file = root.appendingPathComponent(".git/codex-hints/\(sessionID).json")
        var hints = [String: String]()
        if let data = try? Data(contentsOf: file),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: String] {
            hints = obj
        }
        hints[key] = try compact(patch)
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(withJSONObject: hints) {
            try data.write(to: file)
        }
        return ["saved": true]
    }

    public func list(directory: String, sessionID: String) throws -> [[String: Any]] {
        let root = try open(directory)
        let session = ref(sessionID)
        let commits = try logCommits(root, ref: session, limit: 200)
        return commits
    }

    public func show(directory: String, sessionID: String, commit: String) throws -> [String: Any] {
        let root = try open(directory)
        let session = ref(sessionID)
        let commits = try logCommits(root, ref: session)
        guard commits.contains(where: { ($0["commit"] as? String) == commit }) else {
            throw GitError("Commit is outside this session history")
        }
        var manifest = manifestForCommit(root, commit: commit)
        if let files = manifest["files"] as? [[String: Any]] {
            var expanded: [[String: Any]] = []
            for f in files {
                var item = f
                if let patchPath = f["patchPath"] as? String {
                    item["patch"] = blobAt(root, commit: commit, path: patchPath)
                }
                expanded.append(item)
            }
            manifest["files"] = expanded
        }
        manifest["commit"] = commit
        return manifest
    }

    // MARK: - Git internals

    private func relativize(_ root: URL, path: String, inputIsAbsolute: Bool) throws -> String {
        let target = inputIsAbsolute
            ? URL(fileURLWithPath: path)
            : root.appendingPathComponent(path)
        let standard = target.standardizedFileURL
        let parts = standard.pathComponents
        let rootParts = root.standardizedFileURL.pathComponents
        guard parts.prefix(rootParts.count) == rootParts.prefix(rootParts.count) else {
            throw GitError("Local change is outside the registered project")
        }
        return parts.suffix(from: rootParts.count).joined(separator: "/")
    }

    private struct CommitInfo {
        let id: String
        let metadata: [String: Any]
    }

    private func manifestID(_ c: CommitInfo) -> String {
        c.metadata["id"] as? String ?? ""
    }

    private func manifestID(_ meta: [String: Any]) -> String {
        meta["id"] as? String ?? ""
    }

    private func manifestForCommit(_ root: URL, commit: String) -> [String: Any] {
        let json = blobAt(root, commit: commit, path: "turn.json")
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return [:]
        }
        return obj
    }

    private func blobAt(_ root: URL, commit: String, path: String) -> String {
        (try? git(["show", "\(commit):\(path)"], in: root)) ?? ""
    }

    private func logCommits(_ root: URL, ref: String, limit: Int = 200) -> [[String: Any]] {
        let out = (try? git(["log", "--format=COMMIT %H", "-n", "\(limit)", ref], in: root)) ?? ""
        var result: [[String: Any]] = []
        for line in out.components(separatedBy: "\n") where line.hasPrefix("COMMIT ") {
            let commit = String(line.dropFirst("COMMIT ".count))
            var meta = manifestForCommit(root, commit: commit)
            meta["commit"] = commit
            result.append(meta)
        }
        return result
    }

    private func counts(_ patch: String) -> (Int, Int) {
        var add = 0, del = 0, old = 0, new = 0
        for line in patch.components(separatedBy: "\n") {
            if hunkRange(line) != nil {
                old = hunkCount(line, which: .old)
                new = hunkCount(line, which: .new)
            } else if old > 0 || new > 0 {
                switch line.first {
                case "+": add += 1; new -= 1
                case "-": del += 1; old -= 1
                case " ": old -= 1; new -= 1
                default: break
                }
            }
        }
        return (add, del)
    }

    private func commitPatches(_ root: URL, patches: [String], metadata: [String: Any],
                               session: String, parent: String?, message: String) throws -> String {
        // Use a temporary index so our commit never touches the user's worktree/index.
        let tmpIndex = "\(NSTemporaryDirectory())\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: tmpIndex) }

        let vars = ["-c", "user.name=\(Self.identityName)", "-c", "user.email=\(Self.identityEmail)",
                    "-c", "core.bare=false"]
        let base = vars + ["--git-dir=\(root.appendingPathComponent(".git").path)", "--work-tree=/"]

        // Build patch blobs.
        var args = base + ["hash-object", "-w", "--stdin"]
        var treeLines: [String] = []
        var entries: [[String: String]] = []
        _ = args
        for (idx, patch) in patches.enumerated() {
            let blob = try hashStdin(root, content: patch, tmpIndex: tmpIndex)
            entries.append(["path": "patches/\(String(format: "%04d", idx)).patch", "blob": blob])
        }
        let metaBlob = try hashStdin(root, content: jsonString(metadata), tmpIndex: tmpIndex)
        entries.append(["path": "turn.json", "blob": metaBlob])

        let patchesTree = try mktree(root, entries: entries.filter { $0["path"]?.hasPrefix("patches/") == true },
                                     tmpIndex: tmpIndex)
        let fullEntries = [
            ["path": "patches", "mode": "040000", "tree": patchesTree],
            ["path": "turn.json", "mode": "100644", "blob": metaBlob]
        ]
        let tree = try mktree(root, entries: fullEntries, tmpIndex: tmpIndex)

        var commitArgs = base + ["commit-tree", tree]
        if let parent {
            commitArgs += ["-p", parent]
        }
        commitArgs += ["-m", message]
        let commit = try git(commitArgs, in: root).trimmingCharacters(in: .whitespacesAndNewlines)

        var updateRef: [String] = base + ["update-ref", session, commit]
        if let parent {
            updateRef += [parent]
        }
        _ = try git(updateRef, in: root)

        // Write commit to refs.
        return commit
    }

    private func hashStdin(_ root: URL, content: String, tmpIndex: String) throws -> String {
        let p = KumoProcess()
        p.executableURL = URL(fileURLWithPath: gitPath)
        p.arguments = ["--git-dir=\(root.appendingPathComponent(".git").path)",
                       "hash-object", "-w", "--stdin"]
        p.currentDirectoryURL = root
        let inPipe = Pipe(), out = Pipe(), err = Pipe()
        p.standardInput = inPipe
        p.standardOutput = out
        p.standardError = err
        try p.run()
        inPipe.fileHandleForWriting.write(Data(content.utf8))
        inPipe.fileHandleForWriting.closeFile()
        p.waitUntilExit()
        return String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    private func mktree(_ root: URL, entries: [[String: String]], tmpIndex: String) throws -> String {
        // Build tree from entries using `git mktree --missing` with input lines.
        var lines: [String] = []
        for e in entries {
            let mode = e["mode"] ?? "100644"
            let path = e["path"] ?? ""
            if let blob = e["blob"] {
                lines.append("\(mode) blob \(blob)\t\(path)")
            } else if let tree = e["tree"] {
                lines.append("040000 tree \(tree)\t\(path)")
            }
        }
        let input = lines.joined(separator: "\n") + "\n"
        let p = KumoProcess()
        p.executableURL = URL(fileURLWithPath: gitPath)
        p.arguments = ["--git-dir=\(root.appendingPathComponent(".git").path)", "mktree", "--missing"]
        p.currentDirectoryURL = root
        let inPipe = Pipe(), out = Pipe(), err = Pipe()
        p.standardInput = inPipe
        p.standardOutput = out
        p.standardError = err
        try p.run()
        inPipe.fileHandleForWriting.write(Data(input.utf8))
        inPipe.fileHandleForWriting.closeFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else {
            throw GitError("mktree failed")
        }
        return String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    private func jsonString(_ value: Any) -> String {
        (try? String(data: JSONSerialization.data(withJSONObject: value, options: []), encoding: .utf8)) ?? "{}"
    }
}