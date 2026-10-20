import XCTest
@testable import Loore

final class SSEParserTests: XCTestCase {
    private func parse(_ text: String) -> [SSEEvent] {
        var splitter = SSELineSplitter()
        var parser = SSEParser()
        var events: [SSEEvent] = []
        for byte in Array(text.utf8) {
            if let line = splitter.append(byte), let event = parser.feed(line: line) {
                events.append(event)
            }
        }
        if let line = splitter.flushRemainder(), let event = parser.feed(line: line) { events.append(event) }
        if let event = parser.finish() { events.append(event) }
        return events
    }

    func testBackendWireFormat() {
        // format_sse_message: "event: <name>\ndata: <json>\n\n"
        let events = parse("event: chunk_ready\ndata: {\"chunk_index\": 0, \"audio_url\": \"/media/a.mp3\"}\n\nevent: heartbeat\ndata: {\"timestamp\": 1.5}\n\n")
        XCTAssertEqual(events.count, 2)
        XCTAssertEqual(events[0].event, "chunk_ready")
        XCTAssertEqual(events[0].json?["chunk_index"]?.intValue, 0)
        XCTAssertEqual(events[1].event, "heartbeat")
    }

    func testMultiLineDataCommentsAndDefaults() {
        let events = parse(": comment\ndata: line one\ndata: line two\n\nid: 7\ndata:no-space\n\n")
        XCTAssertEqual(events, [
            SSEEvent(event: "message", data: "line one\nline two"),
            SSEEvent(event: "message", data: "no-space", id: "7"),
        ])
    }

    func testCRLFAndCRLineEndings() {
        let events = parse("event: delta\r\ndata: {\"text\":\"a\"}\r\n\r\nevent: done\rdata: {}\r\r")
        XCTAssertEqual(events.map(\.event), ["delta", "done"])
    }

    func testEventWithoutDataIsNotDispatched() {
        XCTAssertEqual(parse("event: ping\n\n"), [])
    }

    func testTrailingEventWithoutBlankLineIsFlushed() {
        XCTAssertEqual(parse("event: done\ndata: {}").map(\.event), ["done"])
    }

    func testFieldResetBetweenEvents() {
        let events = parse("event: snapshot\ndata: {\"text\":\"x\"}\n\ndata: plain\n\n")
        XCTAssertEqual(events.map(\.event), ["snapshot", "message"])
    }

    func testUTF8SplitAcrossBytes() {
        let events = parse("event: delta\ndata: {\"text\":\"čeština – ok\"}\n\n")
        XCTAssertEqual(events.first?.json?["text"]?.stringValue, "čeština – ok")
    }

    func testTypedPayloads() {
        XCTAssertEqual(LLMStreamEvent(SSEEvent(event: "delta", data: #"{"text":"ab"}"#)), .delta("ab"))
        XCTAssertEqual(LLMStreamEvent(SSEEvent(event: "snapshot", data: #"{"text":"all"}"#)), .snapshot("all"))
        XCTAssertEqual(LLMStreamEvent(SSEEvent(event: "done", data: #"{"status":"completed","continuation_node_id":42,"error":null}"#)),
                       .done(status: .completed, continuationNodeId: 42, error: nil))
        XCTAssertEqual(LLMStreamEvent(SSEEvent(event: "error", data: #"{"error":"Node not found"}"#)), .error("Node not found"))

        let chunk = TTSStreamEvent(SSEEvent(event: "chunk_ready", data: #"{"chunk_index":3,"audio_url":"/media/x.mp3?v=1","status":"ready","duration":4.2,"section_index":1,"section_title":null}"#))
        guard case .chunkReady(let c) = chunk else { return XCTFail("\(chunk)") }
        XCTAssertEqual(c.chunkIndex, 3)
        XCTAssertEqual(c.duration, 4.2)
        XCTAssertEqual(c.sectionIndex, 1)
        XCTAssertEqual(TTSStreamEvent(SSEEvent(event: "all_complete", data: #"{"message":"m","tts_url":"/media/t.mp3","continuation_node_id":null,"preview":"p"}"#)),
                       .allComplete(.init(ttsURL: "/media/t.mp3", continuationNodeId: nil, preview: "p")))

        XCTAssertEqual(TranscriptionStreamEvent(SSEEvent(event: "content_update", data: #"{"content":"hello","completed_chunks":2}"#)),
                       .contentUpdate(content: "hello", completedChunks: 2))
        XCTAssertEqual(TranscriptionStreamEvent(SSEEvent(event: "all_complete", data: #"{"message":"Transcription complete","content":"c","draft_id":9,"llm_node_id":77}"#)),
                       .allComplete(.init(content: "c", draftId: 9, llmNodeId: 77, warning: nil)))
    }

    func testLastChunkTracker() {
        let tracker = LastChunkTracker()
        XCTAssertEqual(tracker.resumeQuery(), [])
        tracker.observe(SSEEvent(event: "chunk_ready", data: #"{"chunk_index": 2}"#))
        tracker.observe(SSEEvent(event: "chunk_ready", data: #"{"chunk_index": 1}"#))
        tracker.observe(SSEEvent(event: "heartbeat", data: #"{"timestamp": 1}"#))
        XCTAssertEqual(tracker.resumeQuery(), [URLQueryItem(name: "last_chunk", value: "2")])
    }
}

final class SSEClientTests: XCTestCase {
    override func tearDown() {
        StubURLProtocol.reset()
        super.tearDown()
    }

    private func collect(_ stream: AsyncThrowingStream<SSEMessage, Error>) async -> ([SSEMessage], Error?) {
        var messages: [SSEMessage] = []
        do {
            for try await message in stream { messages.append(message) }
            return (messages, nil)
        } catch {
            return (messages, error)
        }
    }

    func testStreamEndsAfterTerminalEvent() async {
        StubURLProtocol.install { _ in
            .eventStream("event: snapshot\ndata: {\"text\":\"Hel\"}\n\nevent: delta\ndata: {\"text\":\"lo\"}\n\nevent: done\ndata: {\"status\":\"completed\"}\n\n", split: 7)
        }
        let client = makeStubbedClient()
        let sse = SSEClient(api: client)
        let (messages, error) = await collect(sse.subscribe(path: APIPath.sseLLMStream(5)))
        XCTAssertNil(error)
        XCTAssertEqual(messages.compactMap { if case .event(let e) = $0 { return e.event } else { return nil } },
                       ["snapshot", "delta", "done"])
        let request = StubURLProtocol.requests.first
        XCTAssertEqual(request?.value(forHTTPHeaderField: "Accept"), "text/event-stream")
        XCTAssertEqual(request?.url?.path, "/api/sse/nodes/5/llm-stream")
        XCTAssertEqual(StubURLProtocol.requests.count, 1)
    }

    func testJSONAnswerInsteadOfStream() async {
        // The TTS stream answers JSON when the node already has its audio (C §5.4).
        StubURLProtocol.install { _ in .json(200, #"{"status":"completed","tts_url":"/media/t.mp3"}"#) }
        let sse = SSEClient(api: makeStubbedClient())
        let (messages, error) = await collect(sse.subscribe(path: APIPath.sseNodeTTS(9)))
        XCTAssertNil(error)
        guard case .response(let status, let body)? = messages.first else { return XCTFail("\(messages)") }
        XCTAssertEqual(status, 200)
        XCTAssertEqual(try? JSONDecoder().decode(JSONValue.self, from: body)["tts_url"], .string("/media/t.mp3"))
    }

    func testHTTPErrorEndsWithoutRetry() async {
        StubURLProtocol.install { _ in .json(400, #"{"error":"TTS not in progress for this node"}"#) }
        let sse = SSEClient(api: makeStubbedClient())
        let (_, error) = await collect(sse.subscribe(path: APIPath.sseNodeTTS(9)))
        XCTAssertEqual((error as? APIError)?.status, 400)
        XCTAssertEqual(StubURLProtocol.requests.count, 1)
    }

    func testDropReconnectsWithResumeQuery() async {
        final class Counter: @unchecked Sendable { var n = 0; let lock = NSLock()
            func next() -> Int { lock.lock(); defer { lock.unlock() }; n += 1; return n } }
        let counter = Counter()
        StubURLProtocol.install { _ in
            if counter.next() == 1 {
                // Stream cut without a terminal event.
                return .eventStream("event: chunk_ready\ndata: {\"chunk_index\": 0, \"audio_url\": \"/a\"}\n\n")
            }
            return .eventStream("event: chunk_ready\ndata: {\"chunk_index\": 1, \"audio_url\": \"/b\"}\n\nevent: all_complete\ndata: {\"tts_url\": \"/t\"}\n\n")
        }
        let tracker = LastChunkTracker()
        var options = SSEClient.Options()
        options.reconnectDelay = 0.05
        let sse = SSEClient(api: makeStubbedClient())
        var events: [String] = []
        var reconnects = 0
        do {
            for try await message in sse.subscribe(path: APIPath.sseNodeTTS(9), options: options,
                                                   resumeQuery: { tracker.resumeQuery() }) {
                switch message {
                case .event(let e):
                    tracker.observe(e)
                    events.append(e.event)
                case .reconnecting: reconnects += 1
                case .response: XCTFail("unexpected JSON answer")
                }
            }
        } catch {
            XCTFail("\(error)")
        }
        XCTAssertEqual(events, ["chunk_ready", "chunk_ready", "all_complete"])
        XCTAssertEqual(reconnects, 1)
        XCTAssertEqual(StubURLProtocol.requests.count, 2)
        let second = StubURLProtocol.requests[1].url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }
        XCTAssertEqual(second?.queryItems, [URLQueryItem(name: "last_chunk", value: "0")])
    }

    func testStallTriggersReconnect() async {
        final class Counter: @unchecked Sendable { var n = 0; let lock = NSLock()
            func next() -> Int { lock.lock(); defer { lock.unlock() }; n += 1; return n } }
        let counter = Counter()
        StubURLProtocol.install { _ in
            if counter.next() == 1 {
                // One event, then silence far longer than the stall timeout.
                var stub = StubResponse.eventStream("event: heartbeat\ndata: {}\n\n")
                stub.chunks.append(Data("event: heartbeat\ndata: {}\n\n".utf8))
                stub.chunkDelay = 3
                return stub
            }
            return .eventStream("event: done\ndata: {}\n\n")
        }
        var options = SSEClient.Options()
        options.stallTimeout = 1.2
        options.reconnectDelay = 0.05
        let sse = SSEClient(api: makeStubbedClient())
        let (messages, error) = await collect(sse.subscribe(path: APIPath.sseLLMStream(1), options: options))
        XCTAssertNil(error)
        XCTAssertTrue(messages.contains(.reconnecting(attempt: 1)), "\(messages)")
        XCTAssertEqual(messages.last, .event(SSEEvent(event: "done", data: "{}")))
    }

    func testGivesUpAfterRepeatedFailures() async {
        StubURLProtocol.install { _ in
            var stub = StubResponse.eventStream("")
            stub.failWith = URLError(.networkConnectionLost)
            return stub
        }
        var options = SSEClient.Options()
        options.reconnectDelay = 0.01
        options.maxReconnects = 2
        let sse = SSEClient(api: makeStubbedClient())
        let (messages, error) = await collect(sse.subscribe(path: APIPath.sseLLMStream(1), options: options))
        XCTAssertNotNil(error)
        XCTAssertEqual(messages.filter { if case .reconnecting = $0 { return true } else { return false } }.count, 2)
        XCTAssertEqual(StubURLProtocol.requests.count, 3)
    }
}

final class PollerTests: XCTestCase {
    struct Status: PollableStatus { var pollStatus: TaskStatus? }

    final class Script: @unchecked Sendable {
        private let lock = NSLock()
        private var items: [Result<Status, Error>]
        private(set) var calls = 0
        init(_ items: [Result<Status, Error>]) { self.items = items }
        func next() throws -> Status {
            lock.lock(); defer { lock.unlock() }
            calls += 1
            let item = items.count > 1 ? items.removeFirst() : items[0]
            return try item.get()
        }
    }

    func testStopsAtTerminalStatus() async {
        let script = Script([.success(Status(pollStatus: .pending)), .success(Status(pollStatus: .processing)),
                             .success(Status(pollStatus: .completed))])
        var seen: [TaskStatus?] = []
        let outcome = await Poller.runStatus(options: .init(interval: 0.01), wake: nil,
                                             fetch: { try script.next() },
                                             onUpdate: { seen.append($0.pollStatus) })
        guard case .finished(let last) = outcome else { return XCTFail("\(outcome)") }
        XCTAssertEqual(last.pollStatus, .completed)
        XCTAssertEqual(seen, [.pending, .processing, .completed])
        XCTAssertEqual(script.calls, 3)
    }

    func testFailedAndCancelledAreTerminal() async {
        for terminal in [TaskStatus.failed, .cancelled] {
            let script = Script([.success(Status(pollStatus: terminal))])
            let outcome = await Poller.runStatus(options: .init(interval: 0.01), wake: nil, fetch: { try script.next() })
            guard case .finished = outcome else { return XCTFail("\(terminal): \(outcome)") }
        }
    }

    func testErrorsDoNotStopPollingByDefault() async {
        let script = Script([.failure(URLError(.timedOut)), .failure(URLError(.timedOut)),
                             .success(Status(pollStatus: .completed))])
        var errors = 0
        let outcome = await Poller.runStatus(options: .init(interval: 0.01), wake: nil,
                                             fetch: { try script.next() }, onError: { _ in errors += 1 })
        guard case .finished = outcome else { return XCTFail("\(outcome)") }
        XCTAssertEqual(errors, 2)
    }

    func testMaxConsecutiveErrors() async {
        let script = Script([.failure(URLError(.timedOut))])
        let outcome = await Poller.runStatus(options: .init(interval: 0.01, maxConsecutiveErrors: 3), wake: nil,
                                             fetch: { try script.next() })
        guard case .failed = outcome else { return XCTFail("\(outcome)") }
        XCTAssertEqual(script.calls, 3)
    }

    func testTimesOut() async {
        let script = Script([.success(Status(pollStatus: .processing))])
        let outcome = await Poller.runStatus(options: .init(interval: 0.02, maxDuration: 0.1), wake: nil,
                                             fetch: { try script.next() })
        guard case .timedOut(let last) = outcome else { return XCTFail("\(outcome)") }
        XCTAssertEqual(last?.pollStatus, .processing)
    }

    func testWakeSkipsTheWait() async {
        let wake = WakeSignal()
        let script = Script([.success(Status(pollStatus: .processing)), .success(Status(pollStatus: .completed))])
        let start = Date()
        let task = Task {
            await Poller.runStatus(options: .init(interval: 30), wake: wake, fetch: { try script.next() })
        }
        try? await Task.sleep(nanoseconds: 200_000_000)
        wake.fire()
        let outcome = await task.value
        guard case .finished = outcome else { return XCTFail("\(outcome)") }
        XCTAssertLessThan(Date().timeIntervalSince(start), 5)
    }

    func testCancellation() async {
        let script = Script([.success(Status(pollStatus: .processing))])
        let task = Task {
            await Poller.runStatus(options: .init(interval: 30), wake: WakeSignal(), fetch: { try script.next() })
        }
        try? await Task.sleep(nanoseconds: 100_000_000)
        task.cancel()
        let outcome = await task.value
        guard case .cancelled = outcome else { return XCTFail("\(outcome)") }
    }
}
