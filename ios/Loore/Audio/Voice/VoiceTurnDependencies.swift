import Foundation

// What a voice turn needs from the outside world, as protocols so the turn's
// state machine is unit-tested with fakes (design doc §12).

/// The network calls of a voice turn (map C §9).
@MainActor
protocol VoiceBackend: AnyObject {
    func startSession(parentId: Int?, aiUsage: String) async throws -> StreamingInitResponse
    func uploadURL(sessionId: String) -> URL
    /// `POST …/finalize`. A 400 "not in recording state" (a retried finalize
    /// whose first answer was lost) must be treated as success by the caller.
    func finalize(sessionId: String, totalChunks: Int, parentId: Int?, model: String?) async throws
    func sessionStatus(sessionId: String) async throws -> StreamingSessionStatus
    /// Legacy `POST /api/voice` when finalize created no reply node.
    func legacyVoice(content: String, model: String?, aiUsage: String?, parentId: Int?,
                     sessionId: String?) async throws -> VoiceSessionResponse
    func llmStatus(nodeId: Int) async throws -> LLMStatus
    /// `POST /api/nodes/<id>/tts`: `.ready(url)` for 200, `.started` for 202.
    func requestTTS(nodeId: Int) async throws -> TTSTriggerOutcome
    func ttsStatus(nodeId: Int) async throws -> TTSStatus
    /// `GET /api/sse/nodes/<id>/tts-stream[?last_chunk=N]`, reconnecting with the
    /// highest chunk seen.
    func ttsStream(nodeId: Int, lastChunk: Int?) -> AsyncThrowingStream<SSEMessage, Error>
    func discard(sessionId: String) async
    /// Server clock (`GET /api/voice/timing/clock`, seconds).
    func timingClock() async throws -> Double
    func postTiming(nodeId: Int, marks: [String: Double], offsetMs: Double, rttMs: Double) async
}

enum TTSTriggerOutcome: Equatable {
    case ready(url: String)
    case started
}

/// The recorder as the turn sees it (live: `VoiceRecorder`).
@MainActor
protocol VoiceRecording: AnyObject {
    /// Seconds recorded, including an offset for a resumed session.
    var elapsed: Double { get }
    /// Debug file source reached its end (acts as "stop and send").
    var onSourceEnded: (() -> Void)? { get set }
    /// An upload failed fatally (`init_parse_failed`): the session is dead.
    var onFatal: ((String) -> Void)? { get set }
    /// The microphone stopped and could not be restarted (after a route change):
    /// capture is held, as after an interruption.
    var onSourceFailed: (() -> Void)? { get set }
    func start(sessionId: String, uploadURL: URL, firstChunkIndex: Int, elapsedOffset: Double) throws
    /// User pause: flushes the current segment; capture keeps the session alive.
    func pause()
    func resume() throws
    /// System interruption: flush what was captured and hold.
    func interrupt()
    /// Stops capture, uploads everything, reports what the server stored.
    func stop() async -> ChunkUploader.Outcome
    /// Abandons the recording (uploads already stored stay on the server).
    func cancel()
    /// The session is finished: forget its local upload state.
    func forget(sessionId: String)
}

/// The queue player as the turn sees it (live: `ChunkQueuePlayer`).
@MainActor
protocol VoiceQueue: AnyObject {
    var hasAudio: Bool { get }
    var entryCount: Int { get }
    /// The user wants audio to play (false after a pause).
    var isPlaying: Bool { get }
    /// The queue ran out and waits for more chunks.
    var waitingForChunks: Bool { get }
    var generatingTTS: Bool { get set }
    func loadFirst(url: String, duration: Double?, chapterTitle: String?, onPlaying: @escaping () -> Void)
    @discardableResult func append(url: String, duration: Double?, chapterTitle: String?) -> Bool
    func renameChapter(atChunk index: Int, to title: String)
    func stop()
    func close()
}

/// Audio session, cue and chimes as the turn sees them (live: `AudioCenter`).
@MainActor
protocol VoiceAudio: AnyObject {
    var queue: VoiceQueue { get }
    func activateForRecording() throws
    func activateForReply()
    func reactivate() throws
    func switchToPlayback()
    func deactivate()
    func startCue()
    func stopCue()
    func playErrorSound()
    func playInterruptionAlert()
    func playLongRecordingWarning()
    /// Something the lock screen shows changed.
    func refreshNowPlaying()
    /// The conversation is over (the Voice screen closed): its Live Activity goes.
    func voiceConversationEnded()
}

/// User-facing side effects: toasts and local notifications.
@MainActor
protocol VoiceNotices: AnyObject {
    @discardableResult func toast(_ text: String, duration: TimeInterval) -> Int
    func dismissToast(_ id: Int)
    func notify(_ notice: LocalNotice)
    func withdraw(_ notice: LocalNotice)
    func spendCapped()
}

enum LocalNotice: String {
    case recordingPaused = "org.loore.voice.recording-paused"
    case longRecording = "org.loore.voice.long-recording"
    case resumeFailed = "org.loore.voice.resume-failed"
    /// A lock-screen Record that could not start (the reason goes in the body).
    case recordFailed = "org.loore.voice.record-failed"

    var title: String {
        switch self {
        case .recordingPaused: return "Recording paused — tap to resume"
        case .longRecording: return "You’ve been recording for 59 minutes"
        case .resumeFailed: return "The recording could not resume"
        case .recordFailed: return "Loore could not start recording"
        }
    }

    var body: String {
        switch self {
        case .recordingPaused:
            return "The microphone stopped (a call, or another app or device took it). Everything up to the interruption is saved."
        case .longRecording:
            return "Consider stopping soon and continuing in a new recording."
        case .resumeFailed:
            return "The microphone did not restart. Open Loore and press Resume; everything up to the pause is saved."
        case .recordFailed:
            return "Open Loore and try again."
        }
    }
}

// MARK: Live backend

/// `VoiceBackend` over the app's `APIClient` and `SSEClient`.
@MainActor
final class LiveVoiceBackend: VoiceBackend {
    private let api: () -> APIClient
    private let sse: () -> SSEClient

    init(api: @escaping () -> APIClient, sse: @escaping () -> SSEClient) {
        self.api = api
        self.sse = sse
    }

    func startSession(parentId: Int?, aiUsage: String) async throws -> StreamingInitResponse {
        var body: [String: JSONValue] = [
            "privacy_level": .string("private"),
            "ai_usage": .string(aiUsage),
            "label": .string("Voice"),
        ]
        body["parent_id"] = parentId.map { .int($0) } ?? .null
        return try await api().post(APIPath.streamingInit, json: .object(body))
    }

    func uploadURL(sessionId: String) -> URL {
        api().environment.url(path: APIPath.streamingChunk(sessionId))
    }

    func finalize(sessionId: String, totalChunks: Int, parentId: Int?, model: String?) async throws {
        var body: [String: JSONValue] = ["total_chunks": .int(totalChunks), "label": .string("Voice")]
        if let parentId { body["parent_id"] = .int(parentId) }
        if let model { body["model"] = .string(model) }
        var request = APIRequest.json(.post, APIPath.streamingFinalize(sessionId), .object(body))
        request.timeout = 120
        _ = try await api().data(for: request)
    }

    func sessionStatus(sessionId: String) async throws -> StreamingSessionStatus {
        try await api().get(APIPath.streamingStatus(sessionId), poll: true)
    }

    func legacyVoice(content: String, model: String?, aiUsage: String?, parentId: Int?,
                     sessionId: String?) async throws -> VoiceSessionResponse {
        var body: [String: JSONValue] = ["content": .string(content)]
        if let model { body["model"] = .string(model) }
        if let aiUsage { body["ai_usage"] = .string(aiUsage) }
        if let parentId { body["parent_id"] = .int(parentId) }
        if let sessionId { body["session_id"] = .string(sessionId) }
        return try await api().post(APIPath.voice, json: .object(body))
    }

    func llmStatus(nodeId: Int) async throws -> LLMStatus {
        try await api().get(APIPath.llmStatus(nodeId), poll: true)
    }

    func requestTTS(nodeId: Int) async throws -> TTSTriggerOutcome {
        let (data, http) = try await api().data(for: .json(.post, APIPath.nodeTTS(nodeId), nil))
        let answer = try api().decode(TTSRequestResponse.self, from: data)
        if http.statusCode == 200, let url = answer.ttsURL { return .ready(url: url) }
        return .started
    }

    func ttsStatus(nodeId: Int) async throws -> TTSStatus {
        try await api().get(APIPath.nodeTTSStatus(nodeId), poll: true)
    }

    func ttsStream(nodeId: Int, lastChunk: Int?) -> AsyncThrowingStream<SSEMessage, Error> {
        let tracker = LastChunkTracker()
        let initial = lastChunk.map { [URLQueryItem(name: "last_chunk", value: String($0))] } ?? []
        let stream = sse().subscribe(path: APIPath.sseNodeTTS(nodeId), query: initial,
                                     resumeQuery: { tracker.resumeQuery() })
        // Track chunk indices so a dropped connection resumes after the last one.
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    for try await message in stream {
                        if case .event(let e) = message { tracker.observe(e) }
                        continuation.yield(message)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    func discard(sessionId: String) async {
        _ = try? await api().data(for: APIRequest(.delete, APIPath.streamingDiscard(sessionId)))
    }

    func timingClock() async throws -> Double {
        struct Clock: Decodable { var t: Double }
        let clock: Clock = try await api().get(APIPath.voiceTimingClock, poll: true)
        return clock.t
    }

    func postTiming(nodeId: Int, marks: [String: Double], offsetMs: Double, rttMs: Double) async {
        let body: JSONValue = .object([
            "node_id": .int(nodeId),
            "marks": .object(marks.mapValues { .double($0) }),
            "offset_ms": .double(offsetMs),
            "rtt_ms": .double(rttMs),
        ])
        _ = try? await api().data(for: .json(.post, APIPath.voiceTiming, body))
    }
}

private extension JSONValue {
    static func double(_ value: Double) -> JSONValue { .number(value) }
}
