import Foundation
import Network
import Observation

/// Online / offline, like the web's `useOnlineStatus` (`navigator.onLine`).
/// Forms disable Send while offline and say "You're offline".
@MainActor
@Observable
final class NetworkStatus {
    static let shared = NetworkStatus()

    private(set) var isOnline = true
    @ObservationIgnored private let monitor = NWPathMonitor()

    private init() {
        monitor.pathUpdateHandler = { [weak self] path in
            let online = path.status == .satisfied
            Task { @MainActor in self?.isOnline = online }
        }
        monitor.start(queue: DispatchQueue(label: "org.loore.app.network-status"))
    }
}
