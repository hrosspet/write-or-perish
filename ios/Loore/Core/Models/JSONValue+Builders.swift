import Foundation

extension JSONValue {
    /// An integer JSON number.
    static func int(_ value: Int) -> JSONValue { .number(Double(value)) }

    /// `.string` or `.null`.
    static func optional(_ value: String?) -> JSONValue { value.map(JSONValue.string) ?? .null }

    /// `.int` or `.null`.
    static func optional(_ value: Int?) -> JSONValue { value.map(JSONValue.int) ?? .null }
}
