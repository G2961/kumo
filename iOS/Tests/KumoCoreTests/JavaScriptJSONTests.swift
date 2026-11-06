import Foundation
import XCTest
@testable import KumoCore

final class JavaScriptJSONTests: XCTestCase {
    func testBridgeRequestIDIsAJSONString() throws {
        let id = "request-\"one\"\\two\nПривет"
        let encoded = try JavaScriptJSON.encode(id)
        let decoded = try JSONSerialization.jsonObject(with: Data(encoded.utf8), options: .fragmentsAllowed)
        XCTAssertEqual(decoded as? String, id)
        XCTAssertTrue(encoded.hasPrefix("\""))
    }

    func testScalarArgumentsRoundTrip() throws {
        let values: [Any] = [true, 42, NSNull()]
        for value in values {
            let encoded = try JavaScriptJSON.encode(value)
            let decoded = try JSONSerialization.jsonObject(with: Data(encoded.utf8), options: .fragmentsAllowed)
            if value is NSNull {
                XCTAssertTrue(decoded is NSNull)
            } else {
                XCTAssertEqual(decoded as? NSNumber, value as? NSNumber)
            }
        }
    }

    func testBridgeReplyPreservesNestedValues() throws {
        let encoded = try JavaScriptJSON.encode(["projects": ["/project"], "saved": true])
        let decoded = try JSONSerialization.jsonObject(with: Data(encoded.utf8)) as? [String: Any]
        XCTAssertEqual(decoded?["projects"] as? [String], ["/project"])
        XCTAssertEqual(decoded?["saved"] as? Bool, true)
    }

    func testUnsupportedValueThrowsInsteadOfRaisingAnException() {
        XCTAssertThrowsError(try JavaScriptJSON.encode(URL(string: "https://example.com")!))
    }
}
