import Foundation
import os

/// One server-sent event. The backend always sends `event: <name>` plus one
/// single-line JSON `data:` (map B §2.3.6), but the parser follows the SSE spec
/// (multi-line data, comments, CRLF) so a server change does not break it.
struct SSEEvent: Equatable, Sendable {
    var event: String
    var data: String
    var id: String?

    init(event: String, data: String, id: String? = nil) {
        self.event = event
        self.data = data
        self.id = id
    }

    func decode<T: Decodable>(_ type: T.Type, decoder: JSONDecoder = APIClient.makeDecoder()) throws -> T {
        try decoder.decode(T.self, from: Data(data.utf8))
    }

    var json: JSONValue? { try? JSONDecoder().decode(JSONValue.self, from: Data(data.utf8)) }
}

/// Incremental SSE parser. Feed it lines (without the terminator); it returns an
/// event when a blank line completes one. `finish()` flushes a trailing event.
struct SSEParser {
    private var eventName = ""
    private var dataLines: [String] = []
    private var lastId: String?
    private var hasField = false

    mutating func feed(line rawLine: String) -> SSEEvent? {
        let line = rawLine.hasSuffix("\r") ? String(rawLine.dropLast()) : rawLine
        if line.isEmpty {
            return dispatch()
        }
        if line.hasPrefix(":") {
            return nil // comment
        }
        let field: Substring
        var value: Substring
        if let colon = line.firstIndex(of: ":") {
            field = line[..<colon]
            value = line[line.index(after: colon)...]
            if value.hasPrefix(" ") { value = value.dropFirst() }
        } else {
            field = Substring(line)
            value = ""
        }
        switch field {
        case "event":
            eventName = String(value)
            hasField = true
        case "data":
            dataLines.append(String(value))
            hasField = true
        case "id":
            lastId = String(value)
        default:
            break // "retry" and unknown fields are ignored
        }
        return nil
    }

    /// Emits a pending event at end of stream (a server that closes without the blank line).
    mutating func finish() -> SSEEvent? {
        dispatch()
    }

    private mutating func dispatch() -> SSEEvent? {
        defer {
            eventName = ""
            dataLines = []
            hasField = false
        }
        guard hasField, !dataLines.isEmpty else { return nil }
        return SSEEvent(event: eventName.isEmpty ? "message" : eventName,
                        data: dataLines.joined(separator: "\n"),
                        id: lastId)
    }
}

/// Splits a byte stream into lines, keeping empty lines (which
/// `URLSession.AsyncBytes.lines` drops, and which terminate SSE events).
struct SSELineSplitter {
    private var buffer: [UInt8] = []
    private var lastWasCR = false

    /// Returns the complete lines `byte` finishes (zero or one).
    mutating func append(_ byte: UInt8) -> String? {
        if byte == 0x0A { // \n
            if lastWasCR {
                lastWasCR = false
                return nil // \r\n already emitted at \r
            }
            return flush()
        }
        if byte == 0x0D { // \r
            lastWasCR = true
            return flush()
        }
        lastWasCR = false
        buffer.append(byte)
        return nil
    }

    mutating func flushRemainder() -> String? {
        buffer.isEmpty ? nil : flush()
    }

    private mutating func flush() -> String {
        let line = String(decoding: buffer, as: UTF8.self)
        buffer.removeAll(keepingCapacity: true)
        return line
    }
}

/// What an SSE subscription yields.
enum SSEMessage: Sendable, Equatable {
    /// An event from the stream.
    case event(SSEEvent)
    /// The endpoint answered with JSON instead of a stream (pre-stream answers:
    /// e.g. the TTS stream's `{"status":"completed","tts_url":…}`, C §5.4).
    /// The subscription ends after this.
    case response(status: Int, body: Data)
    /// The stream dropped or stalled and the client is reconnecting.
    case reconnecting(attempt: Int)
}

/// SSE client on `URLSession.bytes(for:)` (design doc §3).
///
/// - Sends the app's cookies and `X-Timezone` (same `URLSession` as the API).
/// - Treats 45 s without a byte (three missed 15 s heartbeats) as a stall.
/// - Ends after a terminal event (`done`, `all_complete`, `error`, `close` by
///   default); the server closes the response after those.
/// - Reconnects after a drop or stall, asking `resumeQuery` for resume
///   parameters (`?last_chunk=` where the endpoint supports it).
/// - Checks the status and `Content-Type` before treating the body as a stream.
final class SSEClient: @unchecked Sendable {
    struct Options: Sendable {
        var stallTimeout: TimeInterval = 45
        var reconnectDelay: TimeInterval = 3
        /// Consecutive failed connections before giving up (reset by any event).
        var maxReconnects = 5
        var terminalEvents: Set<String> = ["done", "all_complete", "error", "close"]

        static let `default` = Options()
    }

    private let api: APIClient
    private let log = Logger(subsystem: "org.loore.app", category: "sse")

    init(api: APIClient) {
        self.api = api
    }

    /// Subscribes to `path`. Cancel the consuming task (or stop iterating) to close.
    /// - Parameter resumeQuery: evaluated before every reconnect; return e.g.
    ///   `[URLQueryItem(name: "last_chunk", value: "7")]`.
    func subscribe(path: String,
                   query: [URLQueryItem] = [],
                   options: Options = .default,
                   resumeQuery: (@Sendable () -> [URLQueryItem])? = nil) -> AsyncThrowingStream<SSEMessage, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                await self.run(path: path, query: query, options: options,
                               resumeQuery: resumeQuery, continuation: continuation)
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private enum ConnectionEnd {
        case terminal
        case jsonAnswer
        case dropped(Error?)
    }

    private func run(path: String, query: [URLQueryItem], options: Options,
                     resumeQuery: (@Sendable () -> [URLQueryItem])?,
                     continuation: AsyncThrowingStream<SSEMessage, Error>.Continuation) async {
        var failures = 0
        var attempt = 0
        var isReconnect = false
        while !Task.isCancelled {
            var items = query
            if isReconnect, let extra = resumeQuery?() {
                let names = Set(extra.map(\.name))
                items = items.filter { !names.contains($0.name) } + extra
            }
            var request = APIRequest(.get, path, query: items)
            request.accept = "text/event-stream"
            request.timeout = max(options.stallTimeout * 2, 90)
            request.extraHeaders["Cache-Control"] = "no-cache"

            let activity = ActivityClock()
            let end = await connect(request: request, options: options, activity: activity,
                                    continuation: continuation)
            if activity.sawEvent { failures = 0 }
            switch end {
            case .terminal, .jsonAnswer:
                continuation.finish()
                return
            case .dropped(let error):
                if Task.isCancelled { continuation.finish(); return }
                if let apiError = error as? APIError, apiError.status != nil {
                    // An HTTP error answer (401, 403, 404…): not something a retry fixes.
                    continuation.finish(throwing: apiError)
                    return
                }
                failures += 1
                attempt += 1
                if failures > options.maxReconnects {
                    continuation.finish(throwing: error ?? APIError.transport(code: NSURLErrorNetworkConnectionLost,
                                                                              description: "Stream lost"))
                    return
                }
                log.info("SSE \(path, privacy: .public) dropped; reconnect #\(attempt)")
                continuation.yield(.reconnecting(attempt: attempt))
                try? await Task.sleep(nanoseconds: UInt64(options.reconnectDelay * 1_000_000_000))
                isReconnect = true
            }
        }
        continuation.finish()
    }

    private func connect(request: APIRequest, options: Options, activity: ActivityClock,
                         continuation: AsyncThrowingStream<SSEMessage, Error>.Continuation) async -> ConnectionEnd {
        let urlRequest = api.urlRequest(for: request)

        do {
            return try await withThrowingTaskGroup(of: ConnectionEnd.self) { group in
                group.addTask {
                    let (bytes, response) = try await self.api.session.bytes(for: urlRequest)
                    activity.touch()
                    guard let http = response as? HTTPURLResponse else {
                        throw APIError.transport(code: -1, description: "Not an HTTP response")
                    }
                    let contentType = http.value(forHTTPHeaderField: "Content-Type")?.lowercased() ?? ""
                    if !(200..<300).contains(http.statusCode) {
                        var data = Data()
                        for try await byte in bytes { data.append(byte) }
                        throw APIError.from(status: http.statusCode, contentType: contentType, data: data)
                    }
                    if !contentType.contains("text/event-stream") {
                        var data = Data()
                        for try await byte in bytes { data.append(byte) }
                        continuation.yield(.response(status: http.statusCode, body: data))
                        return .jsonAnswer
                    }
                    var splitter = SSELineSplitter()
                    var parser = SSEParser()
                    for try await byte in bytes {
                        activity.touch()
                        guard let line = splitter.append(byte) else { continue }
                        if let event = parser.feed(line: line) {
                            activity.markEvent()
                            continuation.yield(.event(event))
                            if options.terminalEvents.contains(event.event) { return .terminal }
                        }
                    }
                    if let line = splitter.flushRemainder(), let event = parser.feed(line: line) {
                        continuation.yield(.event(event))
                        if options.terminalEvents.contains(event.event) { return .terminal }
                    }
                    if let event = parser.finish() {
                        continuation.yield(.event(event))
                        if options.terminalEvents.contains(event.event) { return .terminal }
                    }
                    return .dropped(nil)
                }
                group.addTask {
                    // Stall watchdog: no byte for `stallTimeout` seconds.
                    while !Task.isCancelled {
                        try await Task.sleep(nanoseconds: 1_000_000_000)
                        if activity.secondsSinceLastTouch() > options.stallTimeout {
                            return .dropped(APIError.transport(code: NSURLErrorTimedOut,
                                                               description: "Stream stalled"))
                        }
                    }
                    return .dropped(nil)
                }
                let first = try await group.next() ?? .dropped(nil)
                group.cancelAll()
                return first
            }
        } catch {
            return .dropped(APIError.from(transport: error))
        }
    }
}

/// Thread-safe "last activity" timestamp for the stall watchdog.
final class ActivityClock: @unchecked Sendable {
    private let lock = NSLock()
    private var last = Date()
    private var events = 0

    func markEvent() {
        lock.lock()
        events += 1
        lock.unlock()
    }

    var sawEvent: Bool {
        lock.lock()
        defer { lock.unlock() }
        return events > 0
    }

    func touch() {
        lock.lock()
        last = Date()
        lock.unlock()
    }

    func secondsSinceLastTouch() -> TimeInterval {
        lock.lock()
        defer { lock.unlock() }
        return Date().timeIntervalSince(last)
    }
}

/// Tracks the highest `chunk_index` seen on a stream, for `?last_chunk=` resumes.
final class LastChunkTracker: @unchecked Sendable {
    private let lock = NSLock()
    private var highest: Int?

    func observe(_ event: SSEEvent) {
        guard let index = event.json?["chunk_index"]?.intValue else { return }
        lock.lock()
        highest = max(highest ?? index, index)
        lock.unlock()
    }

    var lastChunk: Int? {
        lock.lock()
        defer { lock.unlock() }
        return highest
    }

    /// `[last_chunk=<n>]` once a chunk was seen, else nothing.
    func resumeQuery() -> [URLQueryItem] {
        guard let n = lastChunk else { return [] }
        return [URLQueryItem(name: "last_chunk", value: String(n))]
    }
}
