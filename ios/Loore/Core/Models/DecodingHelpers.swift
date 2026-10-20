import Foundation

extension KeyedDecodingContainer {
    /// Decodes `key` if present and well-typed, else returns `fallback`.
    /// Used for fields the backend may omit or rename without notice, so one
    /// odd field never fails a whole payload (design doc §3 "decoders tolerant").
    func tolerant<T: Decodable>(_ key: Key, default fallback: T) -> T {
        (try? decodeIfPresent(T.self, forKey: key)) ?? fallback
    }

    /// Decodes `key` if present and well-typed, else `nil`.
    func tolerant<T: Decodable>(_ key: Key) -> T? {
        (try? decodeIfPresent(T.self, forKey: key)) ?? nil
    }

    /// An `Int` that may arrive as a number or a numeric string.
    func flexibleInt(_ key: Key) -> Int? {
        if let i = try? decodeIfPresent(Int.self, forKey: key) { return i }
        if let s = try? decodeIfPresent(String.self, forKey: key) { return Int(s) }
        if let d = try? decodeIfPresent(Double.self, forKey: key) { return Int(d) }
        return nil
    }
}

/// A string key for decoding dictionaries keyed by ids ("123": …).
struct AnyCodingKey: CodingKey, Hashable {
    let stringValue: String
    let intValue: Int?

    init(_ string: String) {
        stringValue = string
        intValue = Int(string)
    }

    init?(stringValue: String) { self.init(stringValue) }

    init?(intValue: Int) {
        stringValue = String(intValue)
        self.intValue = intValue
    }
}

/// Decodes a JSON object keyed by numeric strings (`{"12": {...}, "13": null}`)
/// into `[Int: Value?]`. Non-numeric keys are skipped; `null` values are kept as nil.
struct IntKeyedMap<Value: Decodable & Sendable>: Decodable, Sendable {
    var values: [Int: Value?]

    init(values: [Int: Value?] = [:]) { self.values = values }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        var result: [Int: Value?] = [:]
        for key in container.allKeys {
            guard let id = Int(key.stringValue) else { continue }
            if (try? container.decodeNil(forKey: key)) == true {
                result[id] = .some(nil)
            } else {
                result[id] = .some(try? container.decode(Value.self, forKey: key))
            }
        }
        values = result
    }

    subscript(id: Int) -> Value? { values[id] ?? nil }

    /// True when the key was present (even with a null value).
    func contains(_ id: Int) -> Bool { values.keys.contains(id) }
}
