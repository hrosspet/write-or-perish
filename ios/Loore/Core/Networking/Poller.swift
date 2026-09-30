import Foundation
import UIKit

/// A status payload the poller can read (`status` of llm-status, tts-status, …).
protocol PollableStatus {
    var pollStatus: TaskStatus? { get }
}

/// Mirrors the web's `useAsyncTaskPolling` (map B §6.1):
/// fetch immediately, then every `interval`; stop at completed/failed/cancelled;
/// request errors do not stop polling unless `maxConsecutiveErrors` is reached;
/// give up after `maxDuration`; poll again at once when the app returns to the
/// foreground (iOS suspends timers in the background).
///
/// Use it from a SwiftUI `.task {}` (cancelled with the view) or any `Task`:
/// ```swift
/// let outcome = await Poller.run(options: .init(interval: 2),
///     fetch: { try await api.get(APIPath.llmStatus(id), poll: true, as: LLMStatus.self) },
///     onUpdate: { status in … })
/// ```
enum Poller {
    struct Options: Sendable {
        var interval: TimeInterval = 2
        /// nil = no cap (for endpoints that are themselves authoritative, like profile-progress).
        var maxDuration: TimeInterval? = 30 * 60
        /// 0 = keep retrying forever.
        var maxConsecutiveErrors = 0
        /// Poll again right away when the app becomes active.
        var pollOnForeground = true

        init(interval: TimeInterval = 2, maxDuration: TimeInterval? = 30 * 60,
             maxConsecutiveErrors: Int = 0, pollOnForeground: Bool = true) {
            self.interval = interval
            self.maxDuration = maxDuration
            self.maxConsecutiveErrors = maxConsecutiveErrors
            self.pollOnForeground = pollOnForeground
        }
    }

    enum Outcome<T> {
        /// A terminal value (per `isTerminal`).
        case finished(T)
        /// `maxDuration` passed; carries the last value seen.
        case timedOut(last: T?)
        /// `maxConsecutiveErrors` failures in a row.
        case failed(Error)
        /// The surrounding task was cancelled.
        case cancelled
    }

    /// Polls until `isTerminal` returns true, time runs out, errors pile up, or
    /// the task is cancelled. `onUpdate` sees every value; `onError` every failure.
    @discardableResult
    static func run<T>(options: Options = Options(),
                       wake: WakeSignal? = .foreground,
                       fetch: @escaping () async throws -> T,
                       isTerminal: @escaping (T) -> Bool,
                       onUpdate: @escaping (T) async -> Void = { _ in },
                       onError: @escaping (Error) async -> Void = { _ in }) async -> Outcome<T> {
        let start = Date()
        var consecutiveErrors = 0
        var last: T?
        while !Task.isCancelled {
            do {
                let value = try await fetch()
                if Task.isCancelled { return .cancelled }
                consecutiveErrors = 0
                last = value
                await onUpdate(value)
                if isTerminal(value) { return .finished(value) }
            } catch {
                if Task.isCancelled || (error as? APIError)?.isCancelled == true { return .cancelled }
                consecutiveErrors += 1
                await onError(error)
                if options.maxConsecutiveErrors > 0, consecutiveErrors >= options.maxConsecutiveErrors {
                    return .failed(error)
                }
            }
            if let cap = options.maxDuration, Date().timeIntervalSince(start) >= cap {
                return .timedOut(last: last)
            }
            await sleep(options.interval, wake: options.pollOnForeground ? wake : nil)
        }
        return .cancelled
    }

    /// Polls a status endpoint until its `TaskStatus` is terminal.
    @discardableResult
    static func runStatus<T: PollableStatus>(options: Options = Options(),
                                             wake: WakeSignal? = .foreground,
                                             fetch: @escaping () async throws -> T,
                                             onUpdate: @escaping (T) async -> Void = { _ in },
                                             onError: @escaping (Error) async -> Void = { _ in }) async -> Outcome<T> {
        await run(options: options, wake: wake, fetch: fetch,
                  isTerminal: { $0.pollStatus?.isTerminal == true },
                  onUpdate: onUpdate, onError: onError)
    }

    /// Sleeps `seconds`, returning early when `wake` fires or the task is cancelled.
    static func sleep(_ seconds: TimeInterval, wake: WakeSignal?) async {
        let nanos = UInt64(max(seconds, 0) * 1_000_000_000)
        guard let wake else {
            try? await Task.sleep(nanoseconds: nanos)
            return
        }
        await withTaskGroup(of: Void.self) { group in
            group.addTask { try? await Task.sleep(nanoseconds: nanos) }
            group.addTask { await wake.wait() }
            _ = await group.next()
            group.cancelAll()
        }
    }
}

/// A broadcast "wake up" for sleeping pollers. `.foreground` fires whenever the
/// app becomes active.
final class WakeSignal: @unchecked Sendable {
    private let lock = NSLock()
    private var waiters: [UUID: CheckedContinuation<Void, Never>] = [:]
    private var observer: NSObjectProtocol?

    init() {}

    /// Fires on `UIApplication.didBecomeActiveNotification`.
    static let foreground: WakeSignal = {
        let signal = WakeSignal()
        signal.observer = NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification, object: nil, queue: nil
        ) { [weak signal] _ in
            signal?.fire()
        }
        return signal
    }()

    /// Suspends until the next `fire()` or until the task is cancelled.
    func wait() async {
        let id = UUID()
        await withTaskCancellationHandler {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                lock.lock()
                if Task.isCancelled {
                    lock.unlock()
                    continuation.resume()
                    return
                }
                waiters[id] = continuation
                lock.unlock()
            }
        } onCancel: {
            lock.lock()
            let continuation = waiters.removeValue(forKey: id)
            lock.unlock()
            continuation?.resume()
        }
    }

    /// Wakes every current waiter.
    func fire() {
        lock.lock()
        let pending = waiters
        waiters.removeAll()
        lock.unlock()
        for continuation in pending.values { continuation.resume() }
    }
}
