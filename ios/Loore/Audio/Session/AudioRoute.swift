import AVFoundation

/// The ports of an audio route, as far as recording cares (#423): which mic is in
/// use, and whether it is one the user wears or plugs in.
struct AudioRoute: Equatable, Sendable {
    struct Port: Equatable, Sendable {
        var type: AVAudioSession.Port
        var name: String
        var uid: String

        init(type: AVAudioSession.Port, name: String, uid: String) {
            self.type = type
            self.name = name
            self.uid = uid
        }

        init(_ description: AVAudioSessionPortDescription) {
            self.init(type: description.portType, name: description.portName, uid: description.uid)
        }

        /// Bluetooth ports of one device share the address before the profile suffix
        /// (`…-tsco` for hands-free, `…-tacl` for A2DP).
        var deviceAddress: String {
            uid.split(separator: "-").first.map(String.init) ?? uid
        }
    }

    var inputs: [Port]
    var outputs: [Port]

    init(inputs: [Port], outputs: [Port]) {
        self.inputs = inputs
        self.outputs = outputs
    }

    init(_ description: AVAudioSessionRouteDescription) {
        self.init(inputs: description.inputs.map(Port.init), outputs: description.outputs.map(Port.init))
    }

    /// Mics the user wears or plugs in. When one goes away mid-recording, iOS
    /// carries on with the phone's own mic, which in a pocket records almost
    /// nothing of the voice (#423).
    static let headsetInputTypes: Set<AVAudioSession.Port> = [.bluetoothHFP, .headsetMic, .usbAudio, .carAudio]
    static let bluetoothOutputTypes: Set<AVAudioSession.Port> = [.bluetoothA2DP, .bluetoothHFP, .bluetoothLE]

    var input: Port? { inputs.first }

    var hasHeadsetInput: Bool {
        input.map { Self.headsetInputTypes.contains($0.type) } ?? false
    }

    /// The recording's mic was a headset's and no longer is.
    static func headsetMicLost(from old: AudioRoute, to new: AudioRoute) -> Bool {
        old.hasHeadsetInput && !new.hasHeadsetInput
    }

    /// The mic a recording should keep (set as the preferred input, so iOS returns
    /// to it after a glitch, as Safari does for the web app): the current input when
    /// it is a headset's, else the hands-free mic of the Bluetooth device that plays
    /// the audio (right after activation iOS may not have moved the input yet).
    static func preferredHeadsetInput(current: AudioRoute, available: [Port]) -> Port? {
        if let input = current.input, headsetInputTypes.contains(input.type) {
            return available.first { $0.uid == input.uid } ?? input
        }
        guard let output = current.outputs.first(where: { bluetoothOutputTypes.contains($0.type) }) else { return nil }
        return available.first {
            $0.type == .bluetoothHFP && ($0.deviceAddress == output.deviceAddress || $0.name == output.name)
        }
    }

    /// One line for the recording log: port types and names, no addresses.
    var summary: String {
        func describe(_ ports: [Port]) -> String {
            ports.isEmpty ? "none" : ports.map { "\($0.type.rawValue) “\($0.name)”" }.joined(separator: ", ")
        }
        return "in \(describe(inputs)) · out \(describe(outputs))"
    }
}

extension AVAudioSession.RouteChangeReason {
    /// For the recording log.
    var name: String {
        switch self {
        case .unknown: return "unknown"
        case .newDeviceAvailable: return "newDeviceAvailable"
        case .oldDeviceUnavailable: return "oldDeviceUnavailable"
        case .categoryChange: return "categoryChange"
        case .override: return "override"
        case .wakeFromSleep: return "wakeFromSleep"
        case .noSuitableRouteForCategory: return "noSuitableRouteForCategory"
        case .routeConfigurationChange: return "routeConfigurationChange"
        @unknown default: return "reason \(rawValue)"
        }
    }
}
