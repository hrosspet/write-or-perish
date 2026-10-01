import Foundation
import os

/// App-wide signals the client raises while handling answers. `AppState` turns
/// them into a sign-out, the spend-cap banner, or a re-check of the user.
enum APIEvent: Sendable, Equatable {
    case unauthorized
    case spendCapped(message: String)
    case notApproved
}

/// Decodes any body (or none) — for calls whose answer the app ignores.
struct EmptyResponse: Decodable, Sendable {
    init() {}
    init(from decoder: Decoder) throws {}
}

/// One request, described independently of `URLRequest` so SSE and uploads share it.
struct APIRequest: Sendable {
    enum Method: String, Sendable { case get = "GET", post = "POST", put = "PUT", patch = "PATCH", delete = "DELETE" }

    var method: Method = .get
    var path: String
    var query: [URLQueryItem] = []
    var body: Data?
    var contentType: String?
    /// Seconds; nil = the session default (60 s, the web's axios timeout).
    var timeout: TimeInterval?
    /// Status polls: `Cache-Control: no-cache` and a 10 s timeout (`useAsyncTaskPolling`).
    var isPoll = false
    var accept = "application/json"
    var extraHeaders: [String: String] = [:]

    init(_ method: Method = .get, _ path: String, query: [URLQueryItem] = []) {
        self.method = method
        self.path = path
        self.query = query
    }

    static func json(_ method: Method, _ path: String, _ value: JSONValue?) -> APIRequest {
        var request = APIRequest(method, path)
        if let value {
            request.body = try? JSONEncoder().encode(value)
            request.contentType = "application/json"
        }
        return request
    }

    static func json<B: Encodable>(_ method: Method, _ path: String, body: B) throws -> APIRequest {
        var request = APIRequest(method, path)
        request.body = try JSONEncoder().encode(body)
        request.contentType = "application/json"
        return request
    }
}

/// The app's HTTP client (design doc §3 "APIClient").
///
/// - One `URLSession` with an in-memory cookie jar (the Flask `session` and
///   `remember_token` cookies are the only auth). The Keychain (`CookieVault`)
///   is the only place they persist: `HTTPCookieStorage.shared` would write the
///   `remember_token` to `Library/Cookies`, a plaintext file in device backups.
/// - Memory-only `URLCache`: journal content never touches the disk cache.
/// - `X-Timezone` on every request; `Cache-Control: no-cache` on polls.
/// - Canonical trailing slashes (`APIPath.canonical`).
/// - Redirects are followed only for `/api/...` paths (the trailing-slash 308s);
///   every other path (`/auth/...`) stops at the redirect, because a signed-out
///   `/auth/logout` would otherwise walk into X OAuth (map B §3.5).
/// - Non-2xx answers become `APIError`; 401/402/403-not-approved also raise an `APIEvent`.
final class APIClient: @unchecked Sendable {
    let environment: AppEnvironment
    let session: URLSession
    let cookieStorage: HTTPCookieStorage
    let decoder: JSONDecoder

    private let delegate = APISessionDelegate()
    private let eventLock = NSLock()
    private var eventHandler: (@Sendable (APIEvent) -> Void)?
    private let log = Logger(subsystem: "org.loore.app", category: "api")

    static let defaultTimeout: TimeInterval = 60
    static let pollTimeout: TimeInterval = 10

    static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .looreTolerant
        return decoder
    }

    /// A cookie jar that lives only in memory (an ephemeral configuration's).
    static func makeCookieStorage() -> HTTPCookieStorage {
        URLSessionConfiguration.ephemeral.httpCookieStorage!
    }

    /// Earlier builds kept the auth cookies in `HTTPCookieStorage.shared`, which
    /// writes every cookie with an expiry to disk. Nothing in the app uses that
    /// jar (the voice uploader sets its `Cookie` header from the app's jar), so it
    /// is emptied at launch and at sign-out and refuses cookies from answers.
    static func clearSharedCookieStorage(_ storage: HTTPCookieStorage = .shared) {
        storage.cookieAcceptPolicy = .never
        storage.removeCookies(since: .distantPast)
    }

    static func makeConfiguration(cookieStorage: HTTPCookieStorage) -> URLSessionConfiguration {
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = cookieStorage
        config.httpShouldSetCookies = true
        config.httpCookieAcceptPolicy = .always
        config.urlCache = URLCache(memoryCapacity: 8 * 1024 * 1024, diskCapacity: 0, directory: nil)
        config.requestCachePolicy = .useProtocolCachePolicy
        config.timeoutIntervalForRequest = defaultTimeout
        config.timeoutIntervalForResource = 60 * 60 * 2
        return config
    }

    init(environment: AppEnvironment,
         configuration: URLSessionConfiguration? = nil,
         cookieStorage: HTTPCookieStorage = APIClient.makeCookieStorage()) {
        self.environment = environment
        self.cookieStorage = cookieStorage
        let config = configuration ?? Self.makeConfiguration(cookieStorage: cookieStorage)
        if config.httpCookieStorage == nil { config.httpCookieStorage = cookieStorage }
        session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        decoder = Self.makeDecoder()
    }

    deinit {
        session.finishTasksAndInvalidate()
    }

    /// Installs the handler for `APIEvent`s (AppState does this once).
    func setEventHandler(_ handler: (@Sendable (APIEvent) -> Void)?) {
        eventLock.lock()
        eventHandler = handler
        eventLock.unlock()
    }

    private func emit(_ event: APIEvent) {
        eventLock.lock()
        let handler = eventHandler
        eventLock.unlock()
        handler?(event)
    }

    // MARK: Building requests

    /// The `URLRequest` for `request`, with the app's standard headers.
    func urlRequest(for request: APIRequest) -> URLRequest {
        var components = URLComponents(url: environment.url(path: APIPath.canonical(request.path)),
                                       resolvingAgainstBaseURL: false)!
        if !request.query.isEmpty {
            components.queryItems = (components.queryItems ?? []) + request.query
            // `+` is a space in form decoding; encode it so cursors and ISO dates survive.
            components.percentEncodedQuery = components.percentEncodedQuery?
                .replacingOccurrences(of: "+", with: "%2B")
        }
        var urlRequest = URLRequest(url: components.url!)
        urlRequest.httpMethod = request.method.rawValue
        urlRequest.httpBody = request.body
        urlRequest.setValue(request.accept, forHTTPHeaderField: "Accept")
        urlRequest.setValue(TimeZone.current.identifier, forHTTPHeaderField: "X-Timezone")
        if let contentType = request.contentType {
            urlRequest.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        if request.isPoll {
            urlRequest.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
            urlRequest.cachePolicy = .reloadIgnoringLocalCacheData
        }
        for (name, value) in request.extraHeaders {
            urlRequest.setValue(value, forHTTPHeaderField: name)
        }
        urlRequest.timeoutInterval = request.timeout ?? (request.isPoll ? Self.pollTimeout : Self.defaultTimeout)
        return urlRequest
    }

    // MARK: Sending

    /// Sends `request`; returns the body and response of a 2xx answer, or a
    /// 3xx answer for non-/api paths (redirects are not followed there).
    /// Throws `APIError` otherwise.
    func data(for request: APIRequest) async throws -> (Data, HTTPURLResponse) {
        let urlRequest = urlRequest(for: request)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: urlRequest)
        } catch {
            let mapped = APIError.from(transport: error)
            if !mapped.isCancelled {
                log.info("\(request.method.rawValue, privacy: .public) \(request.path, privacy: .public) transport error \(mapped.status ?? -1)")
            }
            throw mapped
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport(code: -1, description: "Not an HTTP response")
        }
        log.debug("\(request.method.rawValue, privacy: .public) \(request.path, privacy: .public) → \(http.statusCode) (\(data.count) bytes)")
        noteSetCookie(http)
        if (200..<300).contains(http.statusCode) || (300..<400).contains(http.statusCode) {
            return (data, http)
        }
        let error = APIError.from(status: http.statusCode,
                                  contentType: http.value(forHTTPHeaderField: "Content-Type"),
                                  data: data)
        switch error {
        case .unauthorized: emit(.unauthorized)
        case .spendCap(let message): emit(.spendCapped(message: message))
        case .notApproved: emit(.notApproved)
        default: break
        }
        throw error
    }

    /// Sends `request` and decodes the answer as `T`.
    func send<T: Decodable>(_ request: APIRequest, as type: T.Type = T.self) async throws -> T {
        let (data, _) = try await data(for: request)
        return try decode(T.self, from: data, path: request.path)
    }

    func decode<T: Decodable>(_ type: T.Type, from data: Data, path: String = "") throws -> T {
        if T.self == EmptyResponse.self { return EmptyResponse() as! T }
        do {
            return try decoder.decode(T.self, from: data)
        } catch let error as DecodingError {
            throw APIError.decoding("\(path): \(Self.describe(error))")
        } catch {
            throw APIError.decoding("\(path): \(error.localizedDescription)")
        }
    }

    // MARK: Conveniences

    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = [], poll: Bool = false,
                           as type: T.Type = T.self) async throws -> T {
        var request = APIRequest(.get, path, query: query)
        request.isPoll = poll
        return try await send(request)
    }

    func post<T: Decodable>(_ path: String, json: JSONValue? = nil, as type: T.Type = T.self) async throws -> T {
        try await send(.json(.post, path, json))
    }

    func put<T: Decodable>(_ path: String, json: JSONValue?, as type: T.Type = T.self) async throws -> T {
        try await send(.json(.put, path, json))
    }

    func patch<T: Decodable>(_ path: String, json: JSONValue?, as type: T.Type = T.self) async throws -> T {
        try await send(.json(.patch, path, json))
    }

    func delete<T: Decodable>(_ path: String, query: [URLQueryItem] = [], as type: T.Type = T.self) async throws -> T {
        try await send(APIRequest(.delete, path, query: query))
    }

    /// Fire-and-forget calls (the web's `.catch(() => {})`): errors are dropped.
    func fireAndForget(_ request: APIRequest) {
        Task { _ = try? await self.data(for: request) }
    }

    /// Downloads a (possibly large) body to a temporary file, e.g. the export.
    func download(_ request: APIRequest) async throws -> (URL, HTTPURLResponse) {
        let urlRequest = urlRequest(for: request)
        let fileURL: URL
        let response: URLResponse
        do {
            (fileURL, response) = try await session.download(for: urlRequest)
        } catch {
            throw APIError.from(transport: error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport(code: -1, description: "Not an HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            let data = (try? Data(contentsOf: fileURL)) ?? Data()
            try? FileManager.default.removeItem(at: fileURL)
            let error = APIError.from(status: http.statusCode,
                                      contentType: http.value(forHTTPHeaderField: "Content-Type"), data: data)
            if case .unauthorized = error { emit(.unauthorized) }
            throw error
        }
        return (fileURL, http)
    }

    /// Sends a (possibly large) body from a file, streamed by `URLSession`
    /// instead of held in memory (imports). Same error mapping as `data(for:)`.
    func upload(_ request: APIRequest, fromFile fileURL: URL) async throws -> (Data, HTTPURLResponse) {
        var urlRequest = urlRequest(for: request)
        urlRequest.httpBody = nil
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.upload(for: urlRequest, fromFile: fileURL)
        } catch {
            throw APIError.from(transport: error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport(code: -1, description: "Not an HTTP response")
        }
        log.debug("\(request.method.rawValue, privacy: .public) \(request.path, privacy: .public) upload → \(http.statusCode)")
        noteSetCookie(http)
        if (200..<300).contains(http.statusCode) { return (data, http) }
        let error = APIError.from(status: http.statusCode,
                                  contentType: http.value(forHTTPHeaderField: "Content-Type"), data: data)
        switch error {
        case .unauthorized: emit(.unauthorized)
        case .spendCap(let message): emit(.spendCapped(message: message))
        case .notApproved: emit(.notApproved)
        default: break
        }
        throw error
    }

    /// Posted (object: the client) after an answer set cookies. The in-memory jar
    /// does not post `NSHTTPCookieManagerCookiesChanged`; `AuthService` listens to
    /// this instead to keep the Keychain copy current.
    static let cookiesChanged = Notification.Name("org.loore.app.APIClient.cookiesChanged")

    private func noteSetCookie(_ response: HTTPURLResponse) {
        guard response.value(forHTTPHeaderField: "Set-Cookie") != nil else { return }
        NotificationCenter.default.post(name: Self.cookiesChanged, object: self)
    }

    /// Cookies the app holds for its backend (used to seed web views and media requests).
    func backendCookies() -> [HTTPCookie] {
        cookieStorage.cookies(for: environment.backendOrigin) ?? []
    }

    private static func describe(_ error: DecodingError) -> String {
        switch error {
        case .keyNotFound(let key, let ctx):
            return "missing \(key.stringValue) at \(ctx.codingPath.map(\.stringValue).joined(separator: "."))"
        case .typeMismatch(_, let ctx), .valueNotFound(_, let ctx), .dataCorrupted(let ctx):
            return "\(ctx.debugDescription) at \(ctx.codingPath.map(\.stringValue).joined(separator: "."))"
        @unknown default:
            return "\(error)"
        }
    }
}

/// Follows redirects only for `/api/...` requests.
final class APISessionDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        let path = task.originalRequest?.url?.path ?? ""
        completionHandler(Self.shouldFollowRedirect(fromPath: path) ? request : nil)
    }

    static func shouldFollowRedirect(fromPath path: String) -> Bool {
        path.hasPrefix("/api/")
    }
}

/// `multipart/form-data` body builder for uploads (audio chunks in M3, imports in M4).
struct MultipartFormData {
    let boundary = "LooreBoundary-\(UUID().uuidString)"
    private(set) var body = Data()

    var contentType: String { "multipart/form-data; boundary=\(boundary)" }

    mutating func addField(_ name: String, _ value: String) {
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".utf8))
        body.append(Data("\(value)\r\n".utf8))
    }

    mutating func addFile(_ name: String, filename: String, mimeType: String, data: Data) {
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data("Content-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\n".utf8))
        body.append(Data("Content-Type: \(mimeType)\r\n\r\n".utf8))
        body.append(data)
        body.append(Data("\r\n".utf8))
    }

    func finalized() -> Data {
        var out = body
        out.append(Data("--\(boundary)--\r\n".utf8))
        return out
    }

    func request(_ method: APIRequest.Method = .post, _ path: String) -> APIRequest {
        var request = APIRequest(method, path)
        request.body = finalized()
        request.contentType = contentType
        return request
    }
}
