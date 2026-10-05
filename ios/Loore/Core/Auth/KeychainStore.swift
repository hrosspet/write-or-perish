import Foundation
import Security

/// Minimal key → data storage, so `CookieVault` can be tested with an in-memory fake.
protocol SecureStore: AnyObject {
    func data(for key: String) -> Data?
    func set(_ data: Data, for key: String) -> Bool
    func remove(_ key: String)
}

/// Generic-password Keychain items, readable after the first unlock and never
/// migrated to another device (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`).
final class KeychainStore: SecureStore {
    private let service: String

    init(service: String = "org.loore.app.auth") {
        self.service = service
    }

    private func baseQuery(_ key: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
    }

    func data(for key: String) -> Data? {
        var query = baseQuery(key)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess else { return nil }
        return result as? Data
    }

    @discardableResult
    func set(_ data: Data, for key: String) -> Bool {
        let query = baseQuery(key)
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecSuccess { return true }
        if status == errSecItemNotFound {
            var add = query
            add.merge(attributes) { _, new in new }
            return SecItemAdd(add as CFDictionary, nil) == errSecSuccess
        }
        return false
    }

    func remove(_ key: String) {
        SecItemDelete(baseQuery(key) as CFDictionary)
    }
}

/// In-memory `SecureStore` for tests and previews.
final class InMemorySecureStore: SecureStore {
    private(set) var items: [String: Data] = [:]

    func data(for key: String) -> Data? { items[key] }

    @discardableResult
    func set(_ data: Data, for key: String) -> Bool {
        items[key] = data
        return true
    }

    func remove(_ key: String) { items[key] = nil }
}
