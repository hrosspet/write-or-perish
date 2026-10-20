import Foundation
import XCTest
@testable import Loore

/// Loads a JSON fixture from `LooreTests/Fixtures` (captured from the local
/// backend as test user 5, long strings trimmed, email scrubbed).
func fixture(_ name: String, file: StaticString = #filePath, line: UInt = #line) throws -> Data {
    let bundle = Bundle(for: StubURLProtocol.self)
    let base = (name as NSString).deletingPathExtension
    guard let url = bundle.url(forResource: base, withExtension: "json") else {
        XCTFail("Missing fixture \(name)", file: file, line: line)
        throw NSError(domain: "fixture", code: 1)
    }
    return try Data(contentsOf: url)
}

func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
    try APIClient.makeDecoder().decode(T.self, from: Data(json.utf8))
}

/// A canned HTTP answer, optionally delivered in several chunks (for SSE).
struct StubResponse {
    var status: Int = 200
    var headers: [String: String] = ["Content-Type": "application/json"]
    var chunks: [Data] = []
    /// Delay between chunks, seconds.
    var chunkDelay: TimeInterval = 0
    /// End with a network error instead of finishing normally.
    var failWith: URLError?

    static func json(_ status: Int, _ body: String, headers: [String: String] = [:]) -> StubResponse {
        var h = ["Content-Type": "application/json"]
        h.merge(headers) { _, new in new }
        return StubResponse(status: status, headers: h, chunks: [Data(body.utf8)])
    }

    static func html(_ status: Int, _ body: String = "<html><body>Not Found</body></html>") -> StubResponse {
        StubResponse(status: status, headers: ["Content-Type": "text/html; charset=utf-8"], chunks: [Data(body.utf8)])
    }

    static func eventStream(_ text: String, split: Int? = nil, delay: TimeInterval = 0) -> StubResponse {
        let data = Data(text.utf8)
        var chunks: [Data] = []
        if let split, split > 0 {
            var i = 0
            while i < data.count {
                chunks.append(data.subdata(in: i..<min(i + split, data.count)))
                i += split
            }
        } else {
            chunks = [data]
        }
        return StubResponse(status: 200, headers: ["Content-Type": "text/event-stream"], chunks: chunks, chunkDelay: delay)
    }
}

/// Intercepts every request of a session whose configuration lists it.
final class StubURLProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var _handler: ((URLRequest) -> StubResponse)?
    private static var _requests: [URLRequest] = []

    static func install(_ handler: @escaping (URLRequest) -> StubResponse) {
        lock.lock()
        _handler = handler
        _requests = []
        lock.unlock()
    }

    static var requests: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return _requests
    }

    static func reset() {
        lock.lock()
        _handler = nil
        _requests = []
        lock.unlock()
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    private var stopped = false

    override func startLoading() {
        Self.lock.lock()
        var recorded = request
        if recorded.httpBody == nil, let stream = request.httpBodyStream {
            recorded.httpBody = Self.readAll(stream)
        }
        Self._requests.append(recorded)
        let handler = Self._handler
        Self.lock.unlock()

        let stub = handler?(recorded) ?? .json(500, "{\"error\":\"no stub\"}")
        let response = HTTPURLResponse(url: request.url!, statusCode: stub.status, httpVersion: "HTTP/1.1",
                                       headerFields: stub.headers)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        let chunks = stub.chunks
        let delay = stub.chunkDelay
        let failure = stub.failWith
        DispatchQueue.global().async {
            for chunk in chunks {
                if self.stopped { return }
                if delay > 0 { Thread.sleep(forTimeInterval: delay) }
                self.client?.urlProtocol(self, didLoad: chunk)
            }
            if self.stopped { return }
            if let failure {
                self.client?.urlProtocol(self, didFailWithError: failure)
            } else {
                self.client?.urlProtocolDidFinishLoading(self)
            }
        }
    }

    override func stopLoading() {
        stopped = true
    }

    private static func readAll(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let n = stream.read(&buffer, maxLength: buffer.count)
            if n <= 0 { break }
            data.append(buffer, count: n)
        }
        return data
    }
}

/// An `APIClient` whose session goes through `StubURLProtocol`.
func makeStubbedClient(environment: AppEnvironment = .production) -> APIClient {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [StubURLProtocol.self]
    return APIClient(environment: environment, configuration: config, cookieStorage: config.httpCookieStorage!)
}
