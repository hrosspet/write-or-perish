import XCTest
@testable import Loore

final class APIErrorMappingTests: XCTestCase {
    private func map(_ status: Int, _ body: String, contentType: String = "application/json") -> APIError {
        APIError.from(status: status, contentType: contentType, data: Data(body.utf8))
    }

    func testUnauthorized() {
        XCTAssertEqual(map(401, #"{"error":"Unauthorized"}"#), .unauthorized)
        XCTAssertEqual(map(401, "<html>", contentType: "text/html"), .unauthorized)
    }

    func testSpendCap() {
        let error = map(402, #"{"error":"monthly_spend_limit_reached","message":"You've reached your monthly usage limit for the free alpha. It resets at the start of next month."}"#)
        XCTAssertEqual(error, .spendCap(message: "You've reached your monthly usage limit for the free alpha. It resets at the start of next month."))
        XCTAssertEqual(map(402, #"{"error":"monthly_spend_limit_reached"}"#), .spendCap(message: APIError.defaultSpendCapMessage))
    }

    func testNotApprovedVersusOther403() {
        XCTAssertEqual(map(403, #"{"error":"Your account is not approved. Please wait for approval."}"#),
                       .notApproved(message: "Your account is not approved. Please wait for approval."))
        let other = map(403, #"{"error":"Not authorized"}"#)
        XCTAssertEqual(other.status, 403)
        XCTAssertEqual(other.serverMessage, "Not authorized")
    }

    func testHTMLBodiesAreNotDecoded() {
        // Flask's get_or_404 answers HTML (map B §8.9).
        let error = map(404, "<!doctype html><title>404 Not Found</title>", contentType: "text/html; charset=utf-8")
        XCTAssertEqual(error, .server(status: 404, body: nil))
        XCTAssertTrue(error.isNotFound)
        XCTAssertNil(error.serverMessage)
        XCTAssertEqual(error.userMessage(fallback: "fallback"), "fallback")
    }

    func testJSONBodyFields() {
        let error = map(400, #"{"error":"Could not parse init segment from first chunk","detail":"x","code":"init_parse_failed"}"#)
        XCTAssertEqual(error.code, "init_parse_failed")
        XCTAssertEqual(error.serverMessage, "Could not parse init segment from first chunk")
        let confirm = map(403, #"{"error":"This confirmation link was requested from a different Loore account.","reason":"other_account"}"#)
        XCTAssertEqual(confirm.reason, "other_account")
        let charCap = map(422, #"{"error":"Content exceeds…","char_cap":100000}"#)
        XCTAssertEqual(charCap.body?.extra["char_cap"]?.intValue, 100000)
    }

    func testTransportErrors() {
        let offline = APIError.from(transport: URLError(.notConnectedToInternet))
        XCTAssertTrue(offline.isOffline)
        XCTAssertNil(offline.status)
        XCTAssertTrue(APIError.from(transport: URLError(.cancelled)).isCancelled)
        XCTAssertEqual(APIError.from(transport: APIError.unauthorized), .unauthorized)
    }
}

final class APIPathTests: XCTestCase {
    func testSlashRootsGetTheirSlash() {
        XCTAssertEqual(APIPath.canonical("/api/dashboard"), "/api/dashboard/")
        XCTAssertEqual(APIPath.canonical("/api/nodes"), "/api/nodes/")
        XCTAssertEqual(APIPath.canonical("/api/drafts?parent_id=12"), "/api/drafts/?parent_id=12")
        XCTAssertEqual(APIPath.canonical("/api/todo"), "/api/todo/")
        XCTAssertEqual(APIPath.canonical("/api/voice"), "/api/voice/")
        XCTAssertEqual(APIPath.canonical("/api/dashboard/"), "/api/dashboard/")
    }

    func testNoSlashRootsLoseIt() {
        XCTAssertEqual(APIPath.canonical("/api/updates/"), "/api/updates")
        XCTAssertEqual(APIPath.canonical("/api/share/"), "/api/share")
        XCTAssertEqual(APIPath.canonical("/api/updates"), "/api/updates")
    }

    func testOtherPathsUntouched() {
        XCTAssertEqual(APIPath.canonical("/api/log"), "/api/log")
        XCTAssertEqual(APIPath.canonical("/api/nodes/12/llm-status"), "/api/nodes/12/llm-status")
        XCTAssertEqual(APIPath.canonical("/api/dashboard/user"), "/api/dashboard/user")
    }

    func testDeclaredConstantsAreCanonical() {
        for path in [APIPath.dashboard, APIPath.nodes, APIPath.drafts, APIPath.todo, APIPath.artifacts,
                     APIPath.profile, APIPath.prompts, APIPath.voice, APIPath.updates, APIPath.share] {
            XCTAssertEqual(APIPath.canonical(path), path)
        }
    }

    func testSessionIdsAreEscaped() {
        XCTAssertEqual(APIPath.streamingStatus("a b/c"), "/api/drafts/streaming/a%20b%2Fc/status")
    }

    func testRedirectPolicy() {
        XCTAssertTrue(APISessionDelegate.shouldFollowRedirect(fromPath: "/api/dashboard"))
        XCTAssertFalse(APISessionDelegate.shouldFollowRedirect(fromPath: "/auth/logout"))
        XCTAssertFalse(APISessionDelegate.shouldFollowRedirect(fromPath: "/auth/magic-link/verify"))
    }
}

final class APIClientTests: XCTestCase {
    override func tearDown() {
        StubURLProtocol.reset()
        super.tearDown()
    }

    func testStandardHeadersAndCanonicalURL() async throws {
        StubURLProtocol.install { _ in .json(200, #"{"timezone":"Europe/Prague"}"#) }
        let client = makeStubbedClient(environment: .staging)
        let _: TimezoneResponse = try await client.get("/api/dashboard")
        let request = try XCTUnwrap(StubURLProtocol.requests.first)
        XCTAssertEqual(request.url?.absoluteString, "https://staging.loore.org/api/dashboard/")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Timezone"), TimeZone.current.identifier)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "application/json")
        XCTAssertNil(request.value(forHTTPHeaderField: "Cache-Control"))
    }

    func testPollsSendNoCacheAndShortTimeout() async throws {
        StubURLProtocol.install { _ in .json(200, #"{"node_id":5,"status":"processing","progress":10,"warnings":[]}"#) }
        let client = makeStubbedClient()
        let status: LLMStatus = try await client.get(APIPath.llmStatus(5), poll: true)
        XCTAssertEqual(status.status, .processing)
        let request = try XCTUnwrap(StubURLProtocol.requests.first)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cache-Control"), "no-cache")
        XCTAssertEqual(client.urlRequest(for: { var r = APIRequest(.get, "/x"); r.isPoll = true; return r }()).timeoutInterval, 10)
    }

    func testJSONBodyAndQueryEncoding() async throws {
        StubURLProtocol.install { _ in .json(200, "{}") }
        let client = makeStubbedClient()
        let _: EmptyResponse = try await client.put(APIPath.user, json: ["craft_mode": true, "username": "a b"])
        let request = try XCTUnwrap(StubURLProtocol.requests.first)
        XCTAssertEqual(request.httpMethod, "PUT")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let body = try JSONDecoder().decode([String: JSONValue].self, from: try XCTUnwrap(request.httpBody))
        XCTAssertEqual(body["craft_mode"], .bool(true))
        XCTAssertEqual(body["username"], .string("a b"))

        let cursor = "2026-09-30T10:11:12.345678+00:00|98765"
        let url = client.urlRequest(for: APIRequest(.get, APIPath.log, query: [URLQueryItem(name: "cursor", value: cursor)])).url!
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(items?.first?.value, cursor)
        XCTAssertTrue(url.absoluteString.contains("%2B00:00"), url.absoluteString)
    }

    func testErrorsRaiseEvents() async {
        final class Box: @unchecked Sendable { var events: [APIEvent] = [] }
        let box = Box()
        let client = makeStubbedClient()
        client.setEventHandler { box.events.append($0) }

        StubURLProtocol.install { request in
            switch request.url?.path {
            case "/api/a": return .json(401, #"{"error":"Unauthorized"}"#)
            case "/api/b": return .json(402, #"{"error":"monthly_spend_limit_reached","message":"capped"}"#)
            case "/api/c": return .json(403, #"{"error":"Your account is not approved. Please wait for approval."}"#)
            default: return .html(404)
            }
        }
        for path in ["/api/a", "/api/b", "/api/c", "/api/d"] {
            do {
                let _: EmptyResponse = try await client.get(path)
                XCTFail("expected an error for \(path)")
            } catch {}
        }
        XCTAssertEqual(box.events, [.unauthorized, .spendCapped(message: "capped"), .notApproved])
    }

    func testDecodingFailureIsReported() async {
        StubURLProtocol.install { _ in .json(200, #"{"unexpected": true}"#) }
        let client = makeStubbedClient()
        do {
            let _: TimezoneResponse = try await client.get("/api/x")
            XCTFail("expected decoding error")
        } catch let error as APIError {
            guard case .decoding(let message) = error else { return XCTFail("\(error)") }
            XCTAssertTrue(message.contains("timezone"), message)
        } catch {
            XCTFail("\(error)")
        }
    }

    func testMultipartBody() {
        var form = MultipartFormData()
        form.addField("chunk_index", "3")
        form.addFile("chunk", filename: "chunk_3.mp4", mimeType: "audio/mp4", data: Data([1, 2, 3]))
        let request = form.request(.post, APIPath.streamingChunk("sid"))
        XCTAssertTrue(request.contentType?.hasPrefix("multipart/form-data; boundary=") == true)
        let text = String(decoding: request.body ?? Data(), as: UTF8.self)
        XCTAssertTrue(text.contains("name=\"chunk_index\"\r\n\r\n3\r\n"))
        XCTAssertTrue(text.contains("filename=\"chunk_3.mp4\""))
        XCTAssertTrue(text.hasSuffix("--\(form.boundary)--\r\n"))
    }
}
