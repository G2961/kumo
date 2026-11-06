import Foundation
import Security

/// Secure storage of SSH connection metadata. Uses the iOS Keychain for encryption rather
/// than Android Keystore: passwords / keys are protected at rest. Only non-secret fields
/// are returned to the web UI by default (see `publicInfo`).
public final class SshConnectionStore {
    public struct Connection: Codable {
        public var id: String
        public var name: String
        public var host: String
        public var user: String
        public var port: Int = 22
        public var directory: String = ""
        public var fingerprint: String = ""
        public var password: String = ""
        public var privateKey: String = ""
        public var passphrase: String = ""

        /// Full unredacted object used for native SSH operations.
        public var dict: [String: Any] {
            [
                "id": id, "name": name, "host": host, "user": user, "port": port,
                "directory": directory, "fingerprint": fingerprint, "password": password,
                "privateKey": privateKey, "passphrase": passphrase
            ]
        }

        /// Non-secret projection safe for the WebView.
        public func publicInfo() -> [String: Any] {
            [
                "id": id, "name": name, "host": host, "user": user, "port": port,
                "directory": directory, "fingerprint": fingerprint,
                "auth": privateKey.isEmpty ? "password" : "key"
            ]
        }

        public init(from d: [String: Any]) {
            id = d["id"] as? String ?? ""
            name = d["name"] as? String ?? ""
            host = d["host"] as? String ?? ""
            user = d["user"] as? String ?? ""
            port = d["port"] as? Int ?? 22
            directory = d["directory"] as? String ?? ""
            fingerprint = d["fingerprint"] as? String ?? ""
            password = d["password"] as? String ?? ""
            privateKey = d["privateKey"] as? String ?? ""
            passphrase = d["passphrase"] as? String ?? ""
            if id.isEmpty { id = UUID().uuidString }
            if name.isEmpty { name = "\(user)@\(host)" }
        }
    }

    public init() {}

    private let service = "kumo.ssh"

    // MARK: - Public store API

    public func list() -> [[String: Any]] {
        bucket.map { $0.publicInfo() }
    }

    public func get(_ id: String) -> Connection? {
        bucket.first { $0.id == id }
    }

    public func save(_ d: [String: Any]) {
        var conn = Connection(from: d)
        // Keep previously stored secrets if the UI omitted them.
        if conn.password.isEmpty || conn.privateKey.isEmpty {
            if let old = get(conn.id) {
                if conn.password.isEmpty { conn.password = old.password }
                if conn.privateKey.isEmpty { conn.privateKey = old.privateKey }
                if conn.passphrase.isEmpty { conn.passphrase = old.passphrase }
                if conn.name.isEmpty { conn.name = old.name }
            }
        }
        if conn.authIsKey() {
            conn.password = ""
        } else {
            conn.privateKey = ""
            conn.passphrase = ""
        }
        var all = bucket.filter { $0.id != conn.id }
        all.append(conn)
        saveBucket(all)
    }

    public func remove(_ id: String) {
        saveBucket(bucket.filter { $0.id != id })
    }

    // MARK: - Keychain-backed persistence

    private var bucket: [Connection] {
        get {
            guard let data = Keychain.read(key: service, account: "connections"),
                  let decoded = try? JSONDecoder().decode([Connection].self, from: data) else {
                return []
            }
            return decoded
        }
    }

    private func saveBucket(_ list: [Connection]) {
        if let data = try? JSONEncoder().encode(list) {
            Keychain.write(key: service, account: "connections", data: data)
        }
    }
}

private extension SshConnectionStore.Connection {
    func authIsKey() -> Bool {
        !privateKey.isEmpty
    }
}

private enum Keychain {
    static func read(key: String, account: String) -> Data? {
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: key,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess, let data = item as? Data else {
            return nil
        }
        return data
    }

    static func write(key: String, account: String, data: Data) {
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: key,
            kSecAttrAccount as String: account
        ]
        SecItemDelete(q as CFDictionary)
        var attrs = q
        attrs[kSecValueData as String] = data
        SecItemAdd(attrs as CFDictionary, nil)
    }
}