import Foundation

/// Encodes bridge arguments, including scalar IDs, without Objective-C JSON exceptions.
public enum JavaScriptJSON {
    public static func encode(_ value: Any) throws -> String {
        guard JSONSerialization.isValidJSONObject([value]) else {
            throw NSError(domain: "KumoBridgeJSON", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Unsupported JavaScript argument."])
        }
        let data = try JSONSerialization.data(withJSONObject: value, options: .fragmentsAllowed)
        return String(decoding: data, as: UTF8.self)
    }
}
