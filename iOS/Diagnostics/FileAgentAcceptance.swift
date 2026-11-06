import Foundation
import Security
import Darwin

// Compile this file with NativeAgentBackend.swift, NativeFileTools.swift,
// NativeModelClient.swift and NativeProviderCompatibility.swift, NOT ProviderDiagnostic.swift.
// Device CLI only (iOS 15+); requires the owner's existing Keychain entitlement.
// Each invocation has a 175-second hard deadline; a parent should use a 180-second timeout.
// `all` runs allow (including reopen), reject, stop and conflict. Crash recovery requires
// two separate invocations: crashPrepare, then crashReopen, with the same test-root.
// Live calls may incur provider costs/rate limits. Every scenario needs a fresh empty root
// unless running `all`; recovery never sends a prompt. No shell/subprocess API is needed.
// No user configuration, Keychain item or user session is written or migrated.
// Native tools are not an OS sandbox: read/edit preparation can read before permission
// evaluation. The policy below denies external results/execution, not those internal reads.
@available(iOS 15.0, macOS 12.0, *)
@main
struct FileAgentAcceptance {
    private static let before = Data("before-value\n".utf8)
    private static let after = Data("after-value\n".utf8)
    private static let externalEdit = Data("user-value\n".utf8)

    static func main() async {
        var report: AcceptanceReport?
        var watchdog: DispatchWorkItem?
        var active: NativeAgentBackend?
        var mode = "invalid"
        do {
            let args = CommandLine.arguments
            guard args.count == 4 || args.count == 5 else { throw AcceptanceFailure.usage }
            mode = args.count == 5 ? args[4] : "all"
            guard ["allow", "reject", "stop", "conflict", "reopen", "all", "crashPrepare", "crashReopen"].contains(mode) else {
                throw AcceptanceFailure.usage
            }
            let source = try Source(statePath: args[1], providerID: args[2])
            let root = try prepareRoot(args[3], source: source, recovering: ["reopen", "crashReopen"].contains(mode))
            let results = try AcceptanceReport(root: root, recovering: ["reopen", "crashReopen"].contains(mode))
            report = results
            let hardStop = DispatchWorkItem {
                try? results.finish(success: false, code: "process_timeout")
                print("CHECK process_deadline FAIL\nACCEPTANCE_ERROR process_timeout")
                fflush(nil)
                Darwin.exit(2)
            }
            watchdog = hardStop
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 175, execute: hardStop)
            let budget = GenerationBudget()
            if mode == "reopen" || mode == "crashReopen" {
                let record = try results.record(mode == "reopen" ? "allow" : "crashPrepare")
                let project = try recoveryProject(record, root: root)
                let snapshotURL = root.appendingPathComponent("native-agent/state.json")
                let snapshotBefore = try Data(contentsOf: snapshotURL)
                let ledgerBefore = try optionalData(root.appendingPathComponent("patches.json"))
                let persisted = try readObject(snapshotURL)
                let pendingActive = persisted["active"] as? [String: Any] ?? [:]
                try check(mode == "crashReopen" ? !pendingActive.isEmpty : pendingActive.isEmpty, "recovery_input_active_marker")
                let client = LiveClient(budget: budget, limit: 0)
                let backend = try source.backend(root: root, client: client)
                active = backend
                let events = AcceptanceEvents(project: project)
                await backend.setEventHandler(events.receive)
                if mode == "reopen" {
                    let savedRows = persisted["messages"] as? [String: [[String: Any]]] ?? [:]
                    let listed = try await backend.request(method: "GET", path: "/session", directory: project.path, body: nil)
                    guard let sid = (listed as? [[String: Any]])?.first?["id"] as? String,
                          let originalRows = savedRows[sid] else { throw AcceptanceFailure.recovery }
                    let reopenedRows = try await messages(backend, sid: sid, project: project)
                    try check(try json(originalRows) == json(reopenedRows), "reopen_preinit_message_content_bytes")
                }
                try await recover(backend, root: root, project: project, client: client,
                                  crash: mode == "crashReopen", results: results, baseline: record)
                if mode == "reopen" {
                    try check(try Data(contentsOf: snapshotURL) == snapshotBefore, "reopen_snapshot_byte_identical")
                }
                let ledgerAfter = try optionalData(root.appendingPathComponent("patches.json"))
                try check(ledgerAfter == ledgerBefore, "recovery_ledger_byte_identical")
                events.printCounts()
                await backend.shutdown()
                active = nil
                if mode == "crashReopen" {
                    let recoveredSnapshot = try Data(contentsOf: snapshotURL)
                    let againClient = LiveClient(budget: budget, limit: 0)
                    let again = try source.backend(root: root, client: againClient)
                    active = again
                    try check(try Data(contentsOf: snapshotURL) == recoveredSnapshot, "second_crash_reopen_is_idempotent")
                    try check(againClient.attempts == 0, "second_crash_reopen_no_generation")
                    await again.shutdown()
                    active = nil
                }
            } else {
                let scenarios = mode == "all" ? ["allow", "reject", "stop", "conflict"] : [mode]
                for scenario in scenarios {
                    let client = LiveClient(budget: budget, limit: scenario == "allow" ? 6 : scenario == "reject" ? 5 : 4)
                    let backend = try source.backend(root: root, client: client)
                    active = backend
                    _ = try await backend.request(method: "PATCH", path: "/global/config", directory: nil, body: source.config)
                    let projectName = "acceptance-" + scenario + "-" + UUID().uuidString.lowercased()
                    let project = root.appendingPathComponent(projectName, isDirectory: true)
                    try FileManager.default.createDirectory(at: project, withIntermediateDirectories: false)
                    try before.write(to: project.appendingPathComponent("seed.txt"), options: .atomic)
                    try results.begin(scenario, project: projectName)
                    let events = AcceptanceEvents(project: project)
                    await backend.setEventHandler(events.receive)
                    let sid = try await createSession(backend, project: project)
                    let deadline = ProcessInfo.processInfo.systemUptime + 120
                    try await prompt(backend, sid: sid, project: project, source: source)
                    let outcome = try await exercise(scenario, backend: backend, sid: sid, project: project,
                                                     deadline: deadline, client: client, events: events)
                    try await verify(backend, sid: sid, project: project, outcome: outcome, events: events)
                    try check(!client.exhausted, "generation_cap_not_exceeded")
                    let evidence = try await evidence(backend, sid: sid, project: project)
                    if scenario == "crashPrepare" {
                        let snapshot = try readObject(root.appendingPathComponent("native-agent/state.json"))
                        try check((snapshot["active"] as? [String: Any])?[sid] != nil, "persisted_active_marker")
                        try check(try Data(contentsOf: source.stateURL) == source.stateBytes, "user_state_unchanged")
                        try results.pass(scenario, counts: evidence.counts.merging([
                            "generations": client.count, "approvals": 0, "denials": 0
                        ], uniquingKeysWith: { _, new in new }), completed: false, events: events.counts)
                        events.printCounts()
                        try results.finish(success: true, code: "crash_prepared_not_recovered")
                        print("ACCEPTANCE_CRASH_PREPARED (no shutdown; run crashReopen in another process)")
                        hardStop.cancel()
                        fflush(nil)
                        // Intentionally leave the real active marker and pending tool on disk.
                        Darwin.exit(0)
                    }
                    await backend.shutdown()
                    active = nil
                    if scenario == "allow" {
                        // Keep allow pending until the automatic reopen checks also pass.
                        let snapshotURL = root.appendingPathComponent("native-agent/state.json")
                        let snapshotBefore = try Data(contentsOf: snapshotURL)
                        let ledgerBefore = try optionalData(root.appendingPathComponent("patches.json"))
                        let reopenClient = LiveClient(budget: budget, limit: 0)
                        let reopened = try source.backend(root: root, client: reopenClient)
                        active = reopened
                        let reopenedEvidence = try await self.evidence(reopened, sid: sid, project: project)
                        try check(evidence.bytes == reopenedEvidence.bytes, "reopen_messages_history_content_bytes")
                        try await recover(reopened, root: root, project: project, client: reopenClient,
                                          crash: false, results: results, baseline: ["counts": evidence.counts])
                        try check(try Data(contentsOf: snapshotURL) == snapshotBefore, "reopen_snapshot_byte_identical")
                        let ledgerAfter = try optionalData(root.appendingPathComponent("patches.json"))
                        try check(ledgerAfter == ledgerBefore, "reopen_ledger_byte_identical")
                        let reopenRecord = try await self.evidence(reopened, sid: sid, project: project)
                        await reopened.shutdown()
                        active = nil
                        // Record reopen after the scenario succeeds, below, to keep failure ownership.
                        try check(reopenRecord.bytes == evidence.bytes, "allow_recovery_all_evidence_identical")
                    }
                    try results.pass(scenario, counts: evidence.counts.merging([
                        "generations": client.count, "approvals": outcome.approvals, "denials": outcome.denials
                    ], uniquingKeysWith: { _, new in new }), completed: outcome.completed, events: events.counts)
                    if scenario == "allow" {
                        try results.begin("reopen", project: projectName)
                        try results.pass("reopen", counts: evidence.counts.merging(["generations": 0], uniquingKeysWith: { _, new in new }), completed: false, events: [:])
                    }
                    events.printCounts()
                    print("ACCEPTANCE_" + scenario.uppercased() + "_PASS completed=\(outcome.completed)")
                }
            }
            try check(try Data(contentsOf: source.stateURL) == source.stateBytes, "user_state_unchanged")
            try results.finish(success: true, code: "verified")
            watchdog?.cancel()
            print("ACCEPTANCE_" + mode.uppercased() + "_PASS generations=\(budget.count)")
            fflush(nil)
            Darwin.exit(0)
        } catch {
            if let backend = active { await backend.shutdown() }
            watchdog?.cancel()
            // Never print arbitrary NSError, provider/model text, URLs or underlying errors.
            let code = (error as? AcceptanceFailure)?.rawValue ?? "native_operation_failed_details_withheld"
            try? report?.finish(success: false, code: code)
            if error as? AcceptanceFailure == .usage {
                print("Usage: KumoFileAgentAcceptance <state.json> <providerID> <test-root> [allow|reject|stop|conflict|reopen|all|crashPrepare|crashReopen]")
            }
            print("ACCEPTANCE_ERROR " + code)
            fflush(nil)
            Darwin.exit(1)
        }
    }

    private struct Source {
        let stateURL: URL
        let stateBytes: Data
        let userRoot: URL
        let providerID: String
        let modelID: String
        let variant: String?
        let config: [String: Any]
        private let key: String

        init(statePath: String, providerID: String) throws {
            guard statePath.hasPrefix("/"), providerID.range(of: "^[A-Za-z0-9._-]{1,128}$", options: .regularExpression) != nil else {
                throw AcceptanceFailure.config
            }
            let stateURL = URL(fileURLWithPath: statePath)
            guard stateURL.lastPathComponent == "state.json", stateURL.deletingLastPathComponent().lastPathComponent == "native-agent" else {
                throw AcceptanceFailure.config
            }
            let userRoot = stateURL.deletingLastPathComponent().deletingLastPathComponent()
            let data = try FileAgentAcceptance.boundedData(stateURL, maximum: 32 * 1024 * 1024)
            // Only extract provider config. Never copy the user's messages, permissions or sessions.
            guard let state = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let config = state["config"] as? [String: Any],
                  var provider = (config["provider"] as? [String: [String: Any]])?[providerID],
                  let models = provider["models"] as? [String: [String: Any]], let modelID = models.keys.sorted().first,
                  var model = models[modelID], var limit = model["limit"] as? [String: Any],
                  let context = limit["context"] as? Int, context > 0, context < 100_000_000 else {
                throw AcceptanceFailure.config
            }
            if let whitelist = provider["whitelist"] as? [String], !whitelist.contains(modelID) { throw AcceptanceFailure.config }
            guard !(provider["blacklist"] as? [String] ?? []).contains(modelID) else { throw AcceptanceFailure.config }
            let output = limit["output"] as? Int ?? min(4096, context / 4)
            guard output > 0, output < context else { throw AcceptanceFailure.context }
            // Retain the saved, legitimate context window; only reduce the test's output reserve.
            limit["output"] = min(output, 4096)
            model["limit"] = limit
            provider["models"] = [modelID: model]
            let variants = model["variants"] as? [String: [String: Any]] ?? [:]
            let variant = ["none", "minimal", "low"].compactMap { effort in
                variants.keys.sorted().first { variants[$0]?["disabled"] as? Bool != true && variants[$0]?["reasoningEffort"] as? String == effort }
            }.first
            guard let key = try FileAgentAcceptance.readKey(root: userRoot, id: providerID), !key.isEmpty,
                  !key.unicodeScalars.contains(where: { $0.value == 0 || $0.value == 10 || $0.value == 13 }) else {
                throw AcceptanceFailure.keychain
            }
            try FileAgentAcceptance.rejectSecrets(provider, key: key)
            guard !statePath.contains(key), !providerID.contains(key) else { throw AcceptanceFailure.config }
            self.stateURL = stateURL
            self.stateBytes = data
            self.userRoot = userRoot
            self.providerID = providerID
            self.modelID = modelID
            self.variant = variant
            self.key = key
            self.config = ["provider": [providerID: provider], "model": providerID + "/" + modelID]
        }

        func validateRootPath(_ path: String) throws {
            // Compare credential bytes literally, without Foundation substring matching.
            try FileAgentAcceptance.rootCheck(!acceptanceContainsSecret(path, secret: key),
                "root_contains_no_secret", failure: .rootContainsSecret)
        }

        func backend(root: URL, client: LiveClient) throws -> NativeAgentBackend {
            try validateRootPath(root.path)
            let id = providerID, secret = key
            return try NativeAgentBackend(root: root, modelClient: client,
                credentialRead: { requested in requested == id ? secret : nil },
                credentialWrite: { _, _ in throw AcceptanceFailure.credentialWrite })
        }
    }

    private static func readKey(root: URL, id: String) throws -> String? {
        var accounts = [id, root.standardizedFileURL.path + ":" + id]
        let marker = root.appendingPathComponent("native-agent/legacy-keychain-root.txt")
        if let file = try? FileHandle(forReadingFrom: marker) {
            defer { try? file.close() }
            if let data = try file.read(upToCount: 4097), data.count <= 4096,
               let oldRoot = String(data: data, encoding: .utf8), oldRoot.hasPrefix("/"),
               !data.contains(where: { $0 == 0 || $0 == 10 || $0 == 13 }) {
                accounts.append(oldRoot + ":" + id)
            }
        }
        for account in accounts {
            let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: "kumo.native.provider", kSecAttrAccount as String: account,
                kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
            var result: CFTypeRef?
            let status = SecItemCopyMatching(query as CFDictionary, &result)
            if status == errSecItemNotFound { continue }
            guard status == errSecSuccess, let data = result as? Data, let key = String(data: data, encoding: .utf8) else {
                throw AcceptanceFailure.keychain
            }
            return key
        }
        return nil
    }

    private static func rejectSecrets(_ value: Any, key: String) throws {
        if let object = value as? [String: Any] {
            for (name, value) in object {
                let normalized = name.lowercased().filter { $0.isLetter }
                guard !["key", "apikey", "authorization", "headers", "password", "token", "secret", "credential"].contains(normalized),
                      !name.contains(key) else { throw AcceptanceFailure.config }
                try rejectSecrets(value, key: key)
            }
        } else if let array = value as? [Any] {
            for value in array { try rejectSecrets(value, key: key) }
        } else if let text = value as? String, text.contains(key) { throw AcceptanceFailure.config }
    }

    private static func prepareRoot(_ path: String, source: Source, recovering: Bool) throws -> URL {
        // Print only fixed guard names and outcomes, never a path or credential-derived value.
        try source.validateRootPath(path)
        try rootCheck(path.hasPrefix("/"), "root_absolute", failure: .rootNotAbsolute)
        try rootCheck(!acceptanceInvalidRootScalars(path), "root_input_characters", failure: .rootInvalidCharacters)
        let root = URL(fileURLWithPath: path, isDirectory: true).standardizedFileURL
        try source.validateRootPath(root.path)
        let canonical: String
        do { canonical = try acceptanceCanonical(root.path) }
        catch { throw AcceptanceFailure.rootCanonicalResolution }
        try source.validateRootPath(canonical)
        try rootCheck(!acceptanceInvalidRootScalars(canonical), "root_canonical_characters", failure: .rootCanonicalInvalidCharacters)
        let user: String
        do { user = try acceptanceCanonical(source.userRoot.path) }
        catch { throw AcceptanceFailure.userRootCanonicalResolution }
        try rootCheck(canonical != "/", "root_not_filesystem_root", failure: .rootFilesystemRoot)
        try rootCheck(!acceptanceContains(canonical, in: user), "root_not_inside_user_state", failure: .rootInsideUserState)
        try rootCheck(!acceptanceContains(user, in: canonical), "root_not_ancestor_of_user_state", failure: .rootAncestorOfUserState)
        if FileManager.default.fileExists(atPath: root.path) {
            let children: [String]
            do { children = try FileManager.default.contentsOfDirectory(atPath: root.path) }
            catch { throw AcceptanceFailure.rootDirectoryInspection }
            if !recovering && !children.isEmpty { throw AcceptanceFailure.rootNotEmpty }
            if recovering && !children.contains("acceptance-results.json") { throw AcceptanceFailure.recovery }
            if recovering {
                for name in ["acceptance-results.json", "native-agent", "native-agent/state.json", "patches.json"] {
                    let child = root.appendingPathComponent(name)
                    let resolved: String
                    do { resolved = try acceptanceCanonical(child.path) }
                    catch { throw AcceptanceFailure.rootRecoveryResolution }
                    try rootCheck(resolved == canonical + "/" + name,
                                  "root_recovery_storage_confined", failure: .rootRecoveryEscape)
                }
            }
        } else {
            guard !recovering else { throw AcceptanceFailure.recovery }
            do { try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true) }
            catch { throw AcceptanceFailure.rootDirectoryCreation }
        }
        print("CHECK root_ready PASS")
        return root
    }

    private static func recoveryProject(_ record: [String: Any], root: URL) throws -> URL {
        guard let name = record["project"] as? String,
              name.range(of: "^acceptance-(allow|crashPrepare)-[a-f0-9-]{36}$", options: .regularExpression) != nil,
              record["success"] as? Bool == true else { throw AcceptanceFailure.recovery }
        let project = root.appendingPathComponent(name, isDirectory: true)
        let resolved: String
        let rootCanonical: String
        do {
            resolved = try acceptanceCanonical(project.path)
            rootCanonical = try acceptanceCanonical(root.path)
        } catch { throw AcceptanceFailure.rootRecoveryResolution }
        try rootCheck(resolved == rootCanonical + "/" + name,
                      "root_recovery_project_confined", failure: .rootRecoveryEscape)
        return project
    }

    private static func rules(project: URL) throws -> [[String: String]] {
        let paths = Set([project.path, try acceptanceCanonical(project.path)])
        var rules = [["permission": "*", "pattern": "*", "action": "deny"]]
        for name in ["write", "edit"] { rules.append(["permission": name, "pattern": "*", "action": "ask"]) }
        for path in paths.sorted() {
            rules.append(["permission": "read", "pattern": path + "/seed.txt", "action": "allow"])
        }
        for name in ["glob", "grep", "list"] {
            for path in paths.sorted() {
                for pattern in [path, path + "/*"] {
                    rules.append(["permission": name, "pattern": pattern, "action": "allow"])
                }
            }
        }
        rules.append(["permission": "external_directory", "pattern": "*", "action": "deny"])
        return rules
    }

    private static func createSession(_ backend: NativeAgentBackend, project: URL) async throws -> String {
        let value = try await backend.request(method: "POST", path: "/session", directory: project.path,
            body: ["title": "Native file acceptance", "permission": try rules(project: project)])
        guard let sid = (value as? [String: Any])?["id"] as? String else { throw AcceptanceFailure.shape }
        return sid
    }

    private static func prompt(_ backend: NativeAgentBackend, sid: String, project: URL, source: Source) async throws {
        var body: [String: Any] = ["agent": "build", "model": ["providerID": source.providerID, "modelID": source.modelID],
            "parts": [["type": "text", "text": """
            Use the available native file tools only in the selected test project. Work only on seed.txt.
            First read seed.txt, which contains before-value followed by a newline. Then edit it to exactly
            after-value followed by a newline, using edit or write. Read seed.txt back after the change and
            conclude briefly. Do not access parent directories, absolute paths outside this project, other
            files, configuration or credentials. If a permission is rejected, conclude without retrying.
            If the file changed while permission was pending, do not retry or overwrite it; conclude.
            """]]]
        if let variant = source.variant { body["variant"] = variant }
        _ = try await backend.request(method: "POST", path: "/session/" + sid + "/prompt_async", directory: project.path, body: body)
    }

    private struct Outcome {
        var approvals = 0
        var denials = 0
        var completed = false
        var pendingCall: String?
        var denialCalls: [String] = []
        var stopped = false
        var conflicted = false
        var prepared = false
    }

    private static func exercise(_ scenario: String, backend: NativeAgentBackend, sid: String, project: URL,
                                 deadline: TimeInterval, client: LiveClient, events: AcceptanceEvents) async throws -> Outcome {
        var outcome = Outcome()
        var answered = Set<String>()
        var rejectDeadline = deadline
        let seed = project.appendingPathComponent("seed.txt")
        while ProcessInfo.processInfo.systemUptime < min(deadline, rejectDeadline) {
            if events.violation { throw AcceptanceFailure.externalPath }
            let rowsBeforeApproval = try await messages(backend, sid: sid, project: project)
            if try await negativeResult(scenario, outcome: outcome, rows: rowsBeforeApproval,
                                        backend: backend, sid: sid, project: project) {
                outcome.conflicted = scenario == "conflict"
                return outcome
            }
            if client.exhausted { throw AcceptanceFailure.generationLimit }
            let pending = try await permissions(backend, project: project)
            for approval in pending {
                guard let id = approval["id"] as? String, !answered.contains(id) else { continue }
                let scoped = approval["sessionID"] as? String == sid && ((try? safeApproval(approval, project: project)) ?? false)
                guard scoped else {
                    _ = try await reply(backend, id: id, project: project, value: "reject")
                    throw AcceptanceFailure.externalPath
                }
                if scenario == "reject", outcome.denials >= 3 {
                    // Never send a fourth rejection; let the third actual tool error settle.
                    break
                }
                if scenario == "conflict", outcome.approvals > 0 { break }
                try check(try Data(contentsOf: seed) == before, "diff_before_apply_disk_unchanged")
                let metadata = approval["metadata"] as? [String: Any] ?? [:]
                try check(metadata["before"] as? String == "before-value\n" && metadata["after"] as? String == "after-value\n"
                          && expectedDiff(metadata["diff"]), "approval_actual_diff")
                try check(events.counts["permission.asked", default: 0] > 0, "real_permission_asked_event")
                let readRows = try await messages(backend, sid: sid, project: project)
                try check(toolParts(readRows).contains { readOutput($0, expected: "1: before-value\n") }, "actual_read_before_approval")
                let storedSession = try await backend.request(method: "GET", path: "/session/" + sid, directory: project.path, body: nil)
                let permissionRules = (storedSession as? [String: Any])?["permission"] ?? []
                try check(try json(permissionRules) == json(rules(project: project)), "ask_policy_not_promoted_to_always")
                guard let call = (approval["tool"] as? [String: Any])?["callID"] as? String else { throw AcceptanceFailure.shape }
                outcome.pendingCall = call
                answered.insert(id)
                if scenario == "crashPrepare" {
                    outcome.prepared = true
                    return outcome
                }
                if scenario == "stop" {
                    try await abort(backend, sid: sid, project: project)
                    outcome.stopped = true
                    var lateAccepted = false
                    do { _ = try await reply(backend, id: id, project: project, value: "once"); lateAccepted = true } catch { }
                    try check(!lateAccepted, "stale_approval_cannot_resume")
                    return outcome
                }
                if scenario == "reject" {
                    outcome.denials += 1
                    outcome.denialCalls.append(call)
                    if outcome.denials == 1 { rejectDeadline = min(deadline, ProcessInfo.processInfo.systemUptime + 90) }
                    _ = try await reply(backend, id: id, project: project, value: "reject")
                } else {
                    try check(outcome.approvals == 0, "single_scoped_once_approval")
                    if scenario == "conflict" { try externalEdit.write(to: seed, options: .atomic) }
                    _ = try await reply(backend, id: id, project: project, value: "once")
                    outcome.approvals += 1
                }
            }
            let rows = try await messages(backend, sid: sid, project: project)
            if try await negativeResult(scenario, outcome: outcome, rows: rows,
                                        backend: backend, sid: sid, project: project) {
                outcome.conflicted = scenario == "conflict"
                return outcome
            }
            if client.exhausted { throw AcceptanceFailure.generationLimit }
            let isIdle = try await idle(backend, sid: sid, project: project)
            if isIdle {
                let finalRows = try await messages(backend, sid: sid, project: project)
                try check(actualFinal(finalRows), "model_finished_without_session_error")
                outcome.completed = true
                return outcome
            }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        throw AcceptanceFailure.scenarioTimeout
    }

    private static func negativeResult(_ scenario: String, outcome: Outcome, rows: [[String: Any]],
                                       backend: NativeAgentBackend, sid: String, project: URL) async throws -> Bool {
        if scenario == "conflict", let call = outcome.pendingCall,
           toolState(rows, call: call)?["status"] as? String == "error" {
            try check(toolState(rows, call: call)?["error"] as? String ==
                      "File changed since permission preparation; refused to overwrite. Prepare a new approval.", "actual_conflict_tool_error")
            try await abort(backend, sid: sid, project: project)
            return true
        }
        if scenario == "reject", outcome.denials == 3,
           outcome.denialCalls.allSatisfy({ toolState(rows, call: $0)?["error"] as? String == "Tool permission rejected by the user." }) {
            try await abort(backend, sid: sid, project: project)
            return true
        }
        return false
    }

    private static func safeApproval(_ approval: [String: Any], project: URL) throws -> Bool {
        guard ["write", "edit"].contains(approval["permission"] as? String ?? ""),
              let paths = approval["patterns"] as? [String], !paths.isEmpty,
              let metadata = approval["metadata"] as? [String: Any],
              let filepath = metadata["filepath"] as? String, let resolved = metadata["resolvedPath"] as? String else { return false }
        let projectRoot = try acceptanceCanonical(project.path)
        let seed = try acceptanceCanonical(project.appendingPathComponent("seed.txt").path)
        return try (paths + [filepath, resolved]).allSatisfy {
            let canonical = try acceptanceCanonical($0)
            return canonical == seed && acceptanceContains(canonical, in: projectRoot)
        }
    }

    private static func verify(_ backend: NativeAgentBackend, sid: String, project: URL,
                               outcome: Outcome, events: AcceptanceEvents) async throws {
        try check(!events.violation, "no_external_tool_paths")
        let rows = try await messages(backend, sid: sid, project: project)
        let turns = try await history(backend, sid: sid, project: project)
        let seed = project.appendingPathComponent("seed.txt")
        if outcome.prepared {
            try check(try Data(contentsOf: seed) == before && turns.isEmpty, "crash_pending_zero_writes_history")
            try check(!outcome.completed && outcome.pendingCall != nil, "crash_pending_not_completion")
            return
        }
        try check(try await idle(backend, sid: sid, project: project), "backend_idle")
        try check(try await permissions(backend, project: project).isEmpty, "no_pending_approvals")
        let finalSession = try await backend.request(method: "GET", path: "/session/" + sid, directory: project.path, body: nil)
        let finalRules = (finalSession as? [String: Any])?["permission"] ?? []
        try check(try json(finalRules) == json(rules(project: project)), "once_reject_stop_preserve_ask_rules")
        if outcome.stopped || outcome.conflicted || outcome.denials > 0 {
            let expected = outcome.conflicted ? externalEdit : before
            try check(try Data(contentsOf: seed) == expected && turns.isEmpty, "zero_agent_writes_zero_ledger_turns")
            if outcome.stopped, let call = outcome.pendingCall {
                try check(toolState(rows, call: call)?["status"] as? String == "error"
                          && toolState(rows, call: call)?["error"] as? String == "Stopped by the user.", "pending_tool_cancelled")
                try check(rows.contains { (($0["info"] as? [String: Any])?["error"] as? [String: Any])?["name"] as? String == "NativeAgentError" }, "stop_message_error_persisted")
            }
            if outcome.denials > 0 {
                try check(outcome.denials <= 3 && outcome.denialCalls.allSatisfy {
                    toolState(rows, call: $0)?["status"] as? String == "error"
                    && toolState(rows, call: $0)?["error"] as? String == "Tool permission rejected by the user."
                }, "actual_denied_tool_states")
            }
            try await Task.sleep(nanoseconds: 1_000_000_000)
            try check(try Data(contentsOf: seed) == expected, "no_late_disk_write_after_one_second")
            let lateHistory = try await history(backend, sid: sid, project: project)
            try check(lateHistory.isEmpty, "no_late_history_after_one_second")
        } else {
            try check(outcome.approvals == 1 && outcome.completed, "once_then_model_completion")
            try check(try Data(contentsOf: seed) == after, "actual_disk_after_value")
            let parts = toolParts(rows)
            guard let writeIndex = parts.firstIndex(where: { ["edit", "write"].contains($0["tool"] as? String ?? "") }) else {
                throw AcceptanceFailure.assertion
            }
            try check(parts.allSatisfy { ($0["state"] as? [String: Any])?["status"] as? String == "completed" }, "all_actual_tools_completed")
            try check(parts[..<writeIndex].contains { readOutput($0, expected: "1: before-value\n") }
                      && parts.dropFirst(writeIndex + 1).contains { readOutput($0, expected: "1: after-value\n") }, "real_read_edit_read_order")
            let state = parts[writeIndex]["state"] as? [String: Any] ?? [:]
            try check((state["metadata"] as? [String: Any])?["saved"] as? Bool == true, "native_write_saved")
            try check(turns.count == 1, "one_actual_ledger_turn")
            try check(actualFinal(rows), "actual_final_answer_no_error")
            let diff = try await backend.request(method: "GET", path: "/session/" + sid + "/diff", directory: project.path, body: nil)
            let files = diff as? [[String: Any]] ?? []
            try check(files.count == 1 && expectedDiff(files.first?["patch"]), "persisted_actual_patch_diff")
        }
    }

    private struct Evidence {
        let bytes: Data
        let counts: [String: Int]
    }

    private static func evidence(_ backend: NativeAgentBackend, sid: String, project: URL) async throws -> Evidence {
        let rows = try await messages(backend, sid: sid, project: project)
        let turns = try await history(backend, sid: sid, project: project)
        var patches: [[String: Any]] = []
        for turn in turns {
            guard let commit = turn["commit"] as? String else { throw AcceptanceFailure.shape }
            patches.append(try await backend.historyAction("historyShow", data: ["sessionID": sid, "directory": project.path, "commit": commit]))
        }
        let records = patches.reduce(0) { $0 + (($1["files"] as? [[String: Any]])?.count ?? 0) }
        return Evidence(bytes: try json(["messages": rows, "history": turns, "patches": patches]),
                        counts: ["messages": rows.count, "historyTurns": turns.count, "patchRecords": records])
    }

    private static func recover(_ backend: NativeAgentBackend, root: URL, project: URL, client: LiveClient,
                                crash: Bool, results: AcceptanceReport, baseline: [String: Any]) async throws {
        let listed = try await backend.request(method: "GET", path: "/session", directory: project.path, body: nil)
        guard let sessions = listed as? [[String: Any]], sessions.count == 1, let sid = sessions.first?["id"] as? String else {
            throw AcceptanceFailure.recovery
        }
        let rows = try await messages(backend, sid: sid, project: project)
        let current = try await evidence(backend, sid: sid, project: project)
        let counts = baseline["counts"] as? [String: Int] ?? [:]
        try check(current.counts == counts.filter { ["messages", "historyTurns", "patchRecords"].contains($0.key) }, "reopen_record_counts")
        let snapshotURL = root.appendingPathComponent("native-agent/state.json")
        let snapshot = try Data(contentsOf: snapshotURL)
        let ledgerURL = root.appendingPathComponent("patches.json")
        let ledger = try optionalData(ledgerURL)
        let seed = project.appendingPathComponent("seed.txt")
        let expected = crash ? before : after
        try check(try Data(contentsOf: seed) == expected, "reopen_disk_unchanged")
        let reopenedIdle = try await idle(backend, sid: sid, project: project)
        let reopenedPermissions = try await permissions(backend, project: project)
        try check(reopenedIdle && reopenedPermissions.isEmpty, "reopen_idle_no_approval")
        try check((try readObject(snapshotURL)["active"] as? [String: Any])?.isEmpty == true, "reopen_no_active_marker")
        if crash {
            let interrupted = rows.contains { row in
                let error = (row["info"] as? [String: Any])?["error"] as? [String: Any]
                return (error?["data"] as? [String: Any])?["message"] as? String == "Interrupted when the app stopped. No file operation was replayed."
            }
            try check(interrupted && toolParts(rows).contains {
                let state = $0["state"] as? [String: Any] ?? [:]
                return state["status"] as? String == "error" && state["error"] as? String == "Interrupted when the app stopped. No file operation was replayed."
            }, "real_crash_marked_interrupted")
            try check(current.counts["historyTurns"] == 0 && current.counts["patchRecords"] == 0, "crash_recovery_no_replayed_write")
        } else {
            // Compare API bytes with the actual pre-existing on-disk messages, not model claims.
            let persisted = try readObject(snapshotURL)
            let savedRows = (persisted["messages"] as? [String: [[String: Any]]])?[sid] ?? []
            try check(try json(savedRows) == json(rows), "reopen_persisted_message_content_bytes")
        }
        try await Task.sleep(nanoseconds: 1_000_000_000)
        try check(client.attempts == 0, "reopen_zero_generation_attempts")
        try check(try Data(contentsOf: seed) == expected, "reopen_no_late_file_mutation")
        let lateSnapshot = try Data(contentsOf: snapshotURL)
        let lateLedger = try optionalData(ledgerURL)
        try check(lateSnapshot == snapshot && lateLedger == ledger, "reopen_no_late_snapshot_or_history_mutation")
        let again = try await evidence(backend, sid: sid, project: project)
        try check(again.bytes == current.bytes, "reopen_stable_messages_history_bytes")
        let name = crash ? "crashReopen" : "reopen"
        // Keep the live scenario pending until its reopen checks also pass.
        if name != "reopen" || baseline["project"] != nil {
            try results.begin(name, project: project.lastPathComponent)
            try results.pass(name, counts: current.counts.merging(["generations": 0], uniquingKeysWith: { _, new in new }), completed: false, events: [:])
        }
        print("ACCEPTANCE_" + name.uppercased() + "_PASS messages=\(rows.count) historyTurns=\(current.counts["historyTurns", default: 0]) patchRecords=\(current.counts["patchRecords", default: 0]) generations=0")
    }

    private static func permissions(_ backend: NativeAgentBackend, project: URL) async throws -> [[String: Any]] {
        let value = try await backend.request(method: "GET", path: "/permission", directory: project.path, body: nil)
        guard let permissions = value as? [[String: Any]] else { throw AcceptanceFailure.shape }
        return permissions
    }

    private static func messages(_ backend: NativeAgentBackend, sid: String, project: URL) async throws -> [[String: Any]] {
        let value = try await backend.request(method: "GET", path: "/session/" + sid + "/message", directory: project.path, body: nil)
        guard let rows = value as? [[String: Any]] else { throw AcceptanceFailure.shape }
        return rows
    }

    private static func history(_ backend: NativeAgentBackend, sid: String, project: URL) async throws -> [[String: Any]] {
        let value = try await backend.historyAction("historyList", data: ["sessionID": sid, "directory": project.path])
        guard let turns = value["turns"] as? [[String: Any]] else { throw AcceptanceFailure.shape }
        return turns
    }

    private static func idle(_ backend: NativeAgentBackend, sid: String, project: URL) async throws -> Bool {
        let value = try await backend.request(method: "GET", path: "/session/status", directory: project.path, body: nil)
        guard let statuses = value as? [String: [String: Any]], let type = statuses[sid]?["type"] as? String else { throw AcceptanceFailure.shape }
        return type == "idle"
    }

    private static func abort(_ backend: NativeAgentBackend, sid: String, project: URL) async throws {
        _ = try await backend.request(method: "POST", path: "/session/" + sid + "/abort", directory: project.path, body: [:])
    }

    private static func reply(_ backend: NativeAgentBackend, id: String, project: URL, value: String) async throws -> Any {
        try await backend.request(method: "POST", path: "/permission/" + id + "/reply", directory: project.path, body: ["reply": value])
    }

    private static func toolParts(_ rows: [[String: Any]]) -> [[String: Any]] {
        rows.flatMap { $0["parts"] as? [[String: Any]] ?? [] }.filter { $0["type"] as? String == "tool" }
    }

    private static func toolState(_ rows: [[String: Any]], call: String) -> [String: Any]? {
        toolParts(rows).first { $0["callID"] as? String == call }?["state"] as? [String: Any]
    }

    private static func readOutput(_ part: [String: Any], expected: String) -> Bool {
        part["tool"] as? String == "read" && (part["state"] as? [String: Any])?["output"] as? String == expected
    }

    private static func actualFinal(_ rows: [[String: Any]]) -> Bool {
        let last = rows.last ?? [:], info = last["info"] as? [String: Any] ?? [:]
        return info["role"] as? String == "assistant" && info["finish"] as? String == "stop"
            && rows.allSatisfy { ($0["info"] as? [String: Any])?["error"] == nil }
            && (last["parts"] as? [[String: Any]] ?? []).contains {
                $0["type"] as? String == "text" && !($0["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            }
    }

    private static func expectedDiff(_ value: Any?) -> Bool {
        guard let diff = value as? String else { return false }
        let lines = diff.components(separatedBy: "\n")
        return lines.contains("-before-value") && lines.contains("+after-value")
    }

    private static func rootCheck(_ condition: Bool, _ name: String, failure: AcceptanceFailure) throws {
        print("CHECK " + name + (condition ? " PASS" : " FAIL"))
        if !condition { throw failure }
    }

    private static func check(_ condition: Bool, _ name: String) throws {
        print("CHECK " + name + (condition ? " PASS" : " FAIL"))
        if !condition { throw AcceptanceFailure.assertion }
    }

    private static func boundedData(_ url: URL, maximum: Int) throws -> Data {
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        let data = try file.read(upToCount: maximum + 1) ?? Data()
        guard data.count <= maximum else { throw AcceptanceFailure.storage }
        return data
    }

    private static func readObject(_ url: URL) throws -> [String: Any] {
        guard let object = try JSONSerialization.jsonObject(with: boundedData(url, maximum: 32 * 1024 * 1024)) as? [String: Any] else {
            throw AcceptanceFailure.shape
        }
        return object
    }

    private static func optionalData(_ url: URL) throws -> Data? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        return try boundedData(url, maximum: 64 * 1024 * 1024)
    }

    private static func json(_ value: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    }
}

private enum AcceptanceFailure: String, Error {
    case usage = "invalid_arguments"
    case config = "saved_provider_config_unavailable_or_unsafe"
    case context = "saved_model_limit_invalid"
    case keychain = "keychain_unavailable_unlock_device_and_check_entitlement"
    case credentialWrite = "unexpected_credential_write"
    case rootNotAbsolute = "test_root_not_absolute"
    case rootInvalidCharacters = "test_root_input_invalid_characters"
    case rootCanonicalInvalidCharacters = "test_root_canonical_invalid_characters"
    case rootCanonicalResolution = "test_root_canonical_resolution_failed"
    case userRootCanonicalResolution = "user_state_root_canonical_resolution_failed"
    case rootFilesystemRoot = "test_root_is_filesystem_root"
    case rootInsideUserState = "test_root_inside_user_state_root"
    case rootAncestorOfUserState = "test_root_ancestor_of_user_state_root"
    case rootContainsSecret = "test_root_contains_secret"
    case rootDirectoryInspection = "test_root_directory_inspection_failed"
    case rootDirectoryCreation = "test_root_directory_creation_failed"
    case rootRecoveryResolution = "test_root_recovery_storage_resolution_failed"
    case rootRecoveryEscape = "test_root_recovery_storage_escape"
    case rootNotEmpty = "test_root_must_be_empty_use_a_new_directory"
    case externalPath = "external_or_unexpected_tool_path_rejected"
    case generationLimit = "live_generation_limit_exceeded"
    case scenarioTimeout = "scenario_timeout"
    case assertion = "acceptance_check_failed"
    case shape = "unexpected_native_payload_shape"
    case recovery = "recovery_evidence_unavailable"
    case storage = "diagnostic_storage_limit_exceeded"
}

// Validate scalar values directly: NUL, LF, CR and permission wildcard syntax.
private func acceptanceInvalidRootScalars(_ path: String) -> Bool {
    path.unicodeScalars.contains { scalar in
        switch scalar.value {
        case 0, 10, 13, 42, 63: return true
        default: return false
        }
    }
}

private func acceptanceContainsSecret(_ path: String, secret: String) -> Bool {
    let bytes = Array(path.utf8), secretBytes = Array(secret.utf8)
    guard !secretBytes.isEmpty else { return true }
    guard bytes.count >= secretBytes.count else { return false }
    return (0...(bytes.count - secretBytes.count)).contains { offset in
        bytes[offset..<(offset + secretBytes.count)].elementsEqual(secretBytes)
    }
}

private func acceptanceContains(_ path: String, in root: String) -> Bool {
    path == root || path.hasPrefix(root.hasSuffix("/") ? root : root + "/")
}

private func acceptanceCanonical(_ path: String) throws -> String {
    guard path.hasPrefix("/"), !path.unicodeScalars.contains(where: { $0.value == 0 }) else { throw AcceptanceFailure.externalPath }
    if let resolved = realpath(path, nil) {
        defer { free(resolved) }
        return String(cString: resolved)
    }
    guard errno == ENOENT else { throw AcceptanceFailure.externalPath }
    var info = stat()
    guard lstat(path, &info) != 0 else { throw AcceptanceFailure.externalPath }
    let url = URL(fileURLWithPath: path), parent = url.deletingLastPathComponent().path
    guard parent != path, url.lastPathComponent != ".", url.lastPathComponent != ".." else { throw AcceptanceFailure.externalPath }
    let canonicalParent = try acceptanceCanonical(parent)
    return (canonicalParent == "/" ? "" : canonicalParent) + "/" + url.lastPathComponent
}

private final class GenerationBudget: @unchecked Sendable {
    private let lock = NSLock()
    private var generations = 0
    var count: Int { lock.lock(); defer { lock.unlock() }; return generations }
    func reserve() -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard generations < 16 else { return false }
        generations += 1
        return true
    }
}

// A metered passthrough, never a fixture: all accepted events come unchanged from URLSession.
@available(iOS 15.0, macOS 12.0, *)
private final class LiveClient: NativeModelClient, @unchecked Sendable {
    private let lock = NSLock()
    private let transport: URLSessionNativeModelClient
    private let budget: GenerationBudget
    private let limit: Int
    private var generations = 0
    private var requested = 0
    private var denied = false
    init(budget: GenerationBudget, limit: Int) {
        self.budget = budget
        self.limit = limit
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 120
        self.transport = URLSessionNativeModelClient(configuration: configuration)
    }
    var count: Int { lock.lock(); defer { lock.unlock() }; return generations }
    var attempts: Int { lock.lock(); defer { lock.unlock() }; return requested }
    var exhausted: Bool { lock.lock(); defer { lock.unlock() }; return denied }
    func stream(request: NativeModelRequest) -> AsyncThrowingStream<NativeModelEvent, Error> {
        lock.lock()
        requested += 1
        let allowed = generations < limit && budget.reserve()
        if allowed { generations += 1 } else { denied = true }
        lock.unlock()
        guard allowed else {
            return AsyncThrowingStream { $0.finish(throwing: AcceptanceFailure.generationLimit) }
        }
        return transport.stream(request: request)
    }
}

// No raw events, text, arguments, errors or model-controlled identifiers are retained/logged.
private final class AcceptanceEvents: @unchecked Sendable {
    private let lock = NSLock()
    private let project: URL
    private var eventCounts: [String: Int] = [:]
    private var statusCounts: [String: Int] = [:]
    private var unsafe = false
    private static let types: Set<String> = ["session.created", "session.updated", "session.status", "session.idle", "session.error",
        "message.updated", "message.part.updated", "message.part.delta", "permission.asked", "permission.replied"]
    init(project: URL) { self.project = project }
    var counts: [String: Int] { lock.lock(); defer { lock.unlock() }; return eventCounts }
    var violation: Bool { lock.lock(); defer { lock.unlock() }; return unsafe }
    func receive(_ event: [String: Any]) {
        lock.lock(); defer { lock.unlock() }
        let rawType = event["type"] as? String ?? "other"
        let type = Self.types.contains(rawType) ? rawType : "other"
        eventCounts[type, default: 0] += 1
        let properties = event["properties"] as? [String: Any] ?? [:]
        if type == "session.status", let status = (properties["status"] as? [String: Any])?["type"] as? String,
           ["busy", "idle"].contains(status) { statusCounts[status, default: 0] += 1 }
        guard type == "message.part.updated", let part = properties["part"] as? [String: Any], part["type"] as? String == "tool" else { return }
        // Partial provider tool previews have no executable arguments; validate only completed names.
        let state = part["state"] as? [String: Any] ?? [:]
        if state["status"] as? String != "pending", !["read", "write", "edit", "list", "glob", "grep"].contains(part["tool"] as? String ?? "") {
            unsafe = true
        }
        if let status = state["status"] as? String, ["pending", "running", "completed", "error"].contains(status) {
            statusCounts[status, default: 0] += 1
        }
        let input = state["input"] as? [String: Any] ?? [:]
        let metadata = state["metadata"] as? [String: Any] ?? [:]
        for path in [input["filePath"], input["path"], metadata["filepath"], metadata["resolvedPath"]].compactMap({ $0 as? String }) {
            let requested = path.hasPrefix("/") ? path : project.appendingPathComponent(path).path
            guard let canonical = try? acceptanceCanonical(requested), let root = try? acceptanceCanonical(project.path),
                  acceptanceContains(canonical, in: root) else { unsafe = true; continue }
            if ["read", "edit", "write"].contains(part["tool"] as? String ?? ""), canonical != root + "/seed.txt" { unsafe = true }
        }
    }
    func printCounts() {
        lock.lock(); let events = eventCounts, statuses = statusCounts; lock.unlock()
        for type in events.keys.sorted() { print("EVENT type=\(type) count=\(events[type, default: 0])") }
        for status in statuses.keys.sorted() { print("STATUS type=\(status) count=\(statuses[status, default: 0])") }
    }
}

// Fixed-schema, single-writer report also supplies only generated project names for recovery.
private final class AcceptanceReport: @unchecked Sendable {
    private let lock = NSLock()
    private let url: URL
    private var rows: [String: [String: Any]] = [:]
    private var running: String?
    init(root: URL, recovering: Bool) throws {
        url = root.appendingPathComponent("acceptance-results.json")
        if recovering {
            let file = try FileHandle(forReadingFrom: url)
            let data: Data
            do { data = try file.read(upToCount: 64 * 1024 + 1) ?? Data(); try file.close() }
            catch { try? file.close(); throw error }
            guard data.count <= 64 * 1024,
                  let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  object["version"] as? Int == 1, let saved = object["scenarios"] as? [String: [String: Any]] else {
                throw AcceptanceFailure.recovery
            }
            // Do not copy arbitrary JSON back to the report, even if its file was tampered with.
            for name in ["allow", "reject", "stop", "conflict", "reopen", "crashPrepare", "crashReopen"] {
                guard let row = saved[name], let project = row["project"] as? String,
                      project.range(of: "^acceptance-(allow|reject|stop|conflict|crashPrepare)-[a-f0-9-]{36}$", options: .regularExpression) != nil else { continue }
                let counts = row["counts"] as? [String: Int] ?? [:]
                rows[name] = ["project": project, "success": row["success"] as? Bool ?? false,
                    "completed": row["completed"] as? Bool ?? false,
                    "counts": counts.filter { ["messages", "historyTurns", "patchRecords", "generations", "approvals", "denials"].contains($0.key) && $0.value >= 0 }]
            }
        }
    }
    func begin(_ name: String, project: String) throws {
        lock.lock(); defer { lock.unlock() }
        running = name
        rows[name] = ["project": project, "success": false, "completed": false, "counts": [String: Int]()]
        try save(success: false, code: "running")
    }
    func record(_ name: String) throws -> [String: Any] {
        lock.lock(); defer { lock.unlock() }
        guard let record = rows[name] else { throw AcceptanceFailure.recovery }
        return record
    }
    func pass(_ name: String, counts: [String: Int], completed: Bool, events: [String: Int]) throws {
        lock.lock(); defer { lock.unlock() }
        guard rows[name] != nil else { throw AcceptanceFailure.shape }
        rows[name]?["success"] = true
        rows[name]?["completed"] = completed
        rows[name]?["counts"] = counts
        rows[name]?["eventCounts"] = events
        try save(success: false, code: "running")
        running = nil
    }
    func finish(success: Bool, code: String) throws {
        lock.lock(); defer { lock.unlock() }
        if !success, let name = running { rows[name]?["success"] = false }
        try save(success: success, code: code)
    }
    private func save(success: Bool, code: String) throws {
        let object: [String: Any] = ["version": 1, "success": success, "code": code, "scenarios": rows,
                                   "limits": ["totalGenerations": 16, "scenarioSeconds": 120, "processSeconds": 175]]
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        try data.write(to: url, options: .atomic)
    }
}
