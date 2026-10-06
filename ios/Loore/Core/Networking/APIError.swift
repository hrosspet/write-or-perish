import Foundation

/// The JSON error body the backend uses: `{"error": …}` plus, on some routes,
/// `message`, `code`, `reason`, `details` and route-specific extras
/// (`char_cap`, `missing_chunks`, `supported_models`, …) kept in `extra`.
struct ServerErrorBody: Equatable, Sendable {
    var error: String?
    var message: String?
    var code: String?
    var reason: String?
    var details: String?
    var extra: [String: JSONValue]

    init(error: String? = nil, message: String? = nil, code: String? = nil,
         reason: String? = nil, details: String? = nil, extra: [String: JSONValue] = [:]) {
        self.error = error
        self.message = message
        self.code = code
        self.reason = reason
        self.details = details
        self.extra = extra
    }

    /// Parses a JSON object body; nil when the body is not a JSON object.
    init?(data: Data) {
        guard let object = try? JSONDecoder().decode([String: JSONValue].self, from: data) else { return nil }
        error = object["error"]?.stringValue
        message = object["message"]?.stringValue
        code = object["code"]?.stringValue
        reason = object["reason"]?.stringValue
        details = object["details"]?.stringValue
        extra = object
    }
}

/// Every failure the API client reports (design doc §3 "APIClient").
enum APIError: Error, Equatable, Sendable {
    /// 401: signed out. The app signs out when it sees this.
    case unauthorized
    /// 403 with the approval message: a waitlisted user called a gated API.
    case notApproved(message: String)
    /// 402 `monthly_spend_limit_reached`.
    case spendCap(message: String)
    /// Any other non-2xx answer. `body` is nil when the body was not JSON
    /// (Flask's HTML 404/403 pages, B §8.9).
    case server(status: Int, body: ServerErrorBody?)
    /// No HTTP answer: offline, timeout, TLS, cancelled.
    case transport(code: Int, description: String)
    /// A 2xx answer that did not decode into the expected type.
    case decoding(String)

    static let notApprovedPrefix = "Your account is not approved"
    static let spendCapCode = "monthly_spend_limit_reached"
    static let defaultSpendCapMessage =
        "You've reached your monthly usage limit for the free alpha. It resets at the start of next month."

    /// Maps a non-2xx HTTP answer. Checks `Content-Type` before reading the body.
    static func from(status: Int, contentType: String?, data: Data) -> APIError {
        let isJSON = (contentType ?? "").lowercased().contains("json")
        let body = isJSON ? ServerErrorBody(data: data) : nil
        switch status {
        case 401:
            return .unauthorized
        case 402 where body?.error == spendCapCode:
            return .spendCap(message: body?.message ?? defaultSpendCapMessage)
        case 403:
            if let text = body?.error, text.hasPrefix(notApprovedPrefix) {
                return .notApproved(message: text)
            }
            return .server(status: status, body: body)
        default:
            return .server(status: status, body: body)
        }
    }

    static func from(transport error: Error) -> APIError {
        if let apiError = error as? APIError { return apiError }
        let ns = error as NSError
        return .transport(code: ns.code, description: ns.localizedDescription)
    }

    var status: Int? {
        switch self {
        case .unauthorized: return 401
        case .notApproved: return 403
        case .spendCap: return 402
        case .server(let status, _): return status
        case .transport, .decoding: return nil
        }
    }

    var body: ServerErrorBody? {
        if case .server(_, let body) = self { return body }
        return nil
    }

    /// Server `error` text (or `message`) when there is one.
    var serverMessage: String? {
        switch self {
        case .notApproved(let message), .spendCap(let message): return message
        case .server(_, let body): return body?.error ?? body?.message
        default: return nil
        }
    }

    var code: String? { body?.code }
    var reason: String? { body?.reason }

    var isNotFound: Bool { status == 404 }
    var isCancelled: Bool {
        if case .transport(let code, _) = self { return code == NSURLErrorCancelled }
        return false
    }
    var isOffline: Bool {
        if case .transport(let code, _) = self {
            return [NSURLErrorNotConnectedToInternet, NSURLErrorNetworkConnectionLost,
                    NSURLErrorCannotConnectToHost, NSURLErrorCannotFindHost,
                    NSURLErrorTimedOut, NSURLErrorDataNotAllowed].contains(code)
        }
        return false
    }

    /// Text for a toast or inline error: the server's words, else `fallback`.
    func userMessage(fallback: String) -> String {
        serverMessage ?? fallback
    }
}

extension APIError: LocalizedError {
    var errorDescription: String? {
        switch self {
        case .unauthorized: return "You are signed out."
        case .notApproved(let m), .spendCap(let m): return m
        case .server(let status, let body): return body?.error ?? body?.message ?? "Server error (\(status))."
        case .transport(_, let d): return d
        case .decoding(let d): return "Unexpected answer from the server (\(d))."
        }
    }
}
