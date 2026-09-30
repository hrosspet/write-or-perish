import AVFoundation
import os

/// Sounds generated in code (no bundled recordings): the thinking cue and the
/// web's alert chimes, rendered to 16-bit mono WAV data for `AVAudioPlayer`.
enum ToneSynth {
    static let sampleRate = 44_100.0

    enum Wave { case sine, square, triangle }

    struct Tone {
        var frequency: Double
        var start: Double
        var duration: Double
        var gain: Double
        var wave: Wave
    }

    /// The web's chimes (Web Audio oscillators with `exponentialRampToValueAtTime(0.01)`).
    /// Square waves are softened to their first three odd harmonics.
    static func render(_ tones: [Tone], length: Double) -> Data {
        let count = Int(length * sampleRate)
        var samples = [Double](repeating: 0, count: count)
        for tone in tones {
            let first = Int(tone.start * sampleRate)
            let n = Int(tone.duration * sampleRate)
            // gain(t) = g * (0.01/g)^(t/d): the Web Audio exponential ramp to 0.01.
            let ratio = 0.01 / tone.gain
            for i in 0..<n where first + i < count {
                let t = Double(i) / sampleRate
                let env = tone.gain * pow(ratio, t / tone.duration)
                let ph = 2 * Double.pi * tone.frequency * t
                let v: Double
                switch tone.wave {
                case .sine: v = sin(ph)
                case .square: v = (sin(ph) + sin(3 * ph) / 3 + sin(5 * ph) / 5) * 4 / Double.pi
                case .triangle: v = (sin(ph) - sin(3 * ph) / 9 + sin(5 * ph) / 25) * 8 / (Double.pi * Double.pi)
                }
                // 5 ms attack so the tone does not click.
                let attack = min(1, t / 0.005)
                samples[first + i] += v * env * attack
            }
        }
        return wav(samples)
    }

    /// The looping thinking cue (design doc §9.4): a slow, soft swell on a low
    /// fifth (G3 + D4), 4 s per loop, no melody. Peak −24 dBFS in the file; the
    /// player's volume sets the level. Starts and ends at silence so it loops
    /// without a click.
    static func thinkingCue() -> Data {
        let length = 4.0
        let count = Int(length * sampleRate)
        let peak = pow(10, -24.0 / 20)
        var samples = [Double](repeating: 0, count: count)
        for i in 0..<count {
            let t = Double(i) / sampleRate
            // Swell over the first 2.4 s (sin² bump), rest silent.
            let swell = t < 2.4 ? pow(sin(Double.pi * t / 2.4), 2) : 0
            let tone = 0.6 * sin(2 * Double.pi * 196 * t) + 0.4 * sin(2 * Double.pi * 293.66 * t)
            samples[i] = tone * swell * peak
        }
        return wav(samples)
    }

    /// Web `playErrorSound`: 660 → 440 → 330 Hz square, gain 0.15.
    static func errorSound() -> Data {
        render([
            Tone(frequency: 660, start: 0, duration: 0.15, gain: 0.15, wave: .square),
            Tone(frequency: 440, start: 0.18, duration: 0.15, gain: 0.15, wave: .square),
            Tone(frequency: 330, start: 0.36, duration: 0.25, gain: 0.15, wave: .square),
        ], length: 0.7)
    }

    /// Web `playInterruptionAlert`: the error motif twice, gain 0.25.
    static func interruptionAlert() -> Data {
        render([0.0, 0.9].flatMap { o in [
            Tone(frequency: 660, start: o, duration: 0.15, gain: 0.25, wave: .square),
            Tone(frequency: 440, start: o + 0.18, duration: 0.15, gain: 0.25, wave: .square),
            Tone(frequency: 330, start: o + 0.36, duration: 0.25, gain: 0.25, wave: .square),
        ] }, length: 1.6)
    }

    /// Web `playWarningSound` (59 minutes): G5 → C6 triangle, twice, gain 0.2.
    static func longRecordingWarning() -> Data {
        render([
            Tone(frequency: 784, start: 0, duration: 0.18, gain: 0.2, wave: .triangle),
            Tone(frequency: 1047, start: 0.20, duration: 0.24, gain: 0.2, wave: .triangle),
            Tone(frequency: 784, start: 0.52, duration: 0.18, gain: 0.2, wave: .triangle),
            Tone(frequency: 1047, start: 0.72, duration: 0.30, gain: 0.2, wave: .triangle),
        ], length: 1.1)
    }

    /// Mono 16-bit PCM WAV.
    static func wav(_ samples: [Double]) -> Data {
        var data = Data()
        func u32(_ v: UInt32) { data.append(contentsOf: withUnsafeBytes(of: v.littleEndian, Array.init)) }
        func u16(_ v: UInt16) { data.append(contentsOf: withUnsafeBytes(of: v.littleEndian, Array.init)) }
        let byteCount = UInt32(samples.count * 2)
        data.append(Data("RIFF".utf8)); u32(36 + byteCount); data.append(Data("WAVE".utf8))
        data.append(Data("fmt ".utf8)); u32(16); u16(1); u16(1); u32(UInt32(sampleRate)); u32(UInt32(sampleRate) * 2)
        u16(2); u16(16)
        data.append(Data("data".utf8)); u32(byteCount)
        for s in samples {
            let v = Int16(max(-1, min(1, s)) * 32_767)
            data.append(contentsOf: withUnsafeBytes(of: v.littleEndian, Array.init))
        }
        return data
    }

    /// Peak level of 16-bit WAV data in dBFS (tests).
    static func peakDBFS(_ wavData: Data) -> Double {
        let pcm = wavData.dropFirst(44)
        var peak: Int32 = 0
        var i = pcm.startIndex
        while i + 1 < pcm.endIndex {
            let v = Int16(bitPattern: UInt16(pcm[i]) | UInt16(pcm[i + 1]) << 8)
            peak = max(peak, abs(Int32(v)))
            i += 2
        }
        return peak == 0 ? -.infinity : 20 * log10(Double(peak) / 32_767)
    }
}

/// Thinking-cue volume (Account → Voice, design doc §9.4). Stored per device.
enum ThinkingCueLevel: String, CaseIterable, Identifiable {
    case soft
    case verySoft = "very_soft"
    case off

    static let defaultsKey = "loore_thinking_cue"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .soft: return "Soft"
        case .verySoft: return "Very soft"
        case .off: return "Off"
        }
    }

    /// Player volume on the −24 dBFS file: Soft ≈ −30 dBFS, Very soft ≈ −38 dBFS.
    var volume: Float {
        switch self {
        case .soft: return 0.5
        case .verySoft: return 0.2
        case .off: return 0
        }
    }

    static var current: ThinkingCueLevel {
        get { UserDefaults.standard.string(forKey: defaultsKey).flatMap(ThinkingCueLevel.init(rawValue:)) ?? .soft }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: defaultsKey) }
    }

    static let offWarning = "Without it, iOS may pause Loore while you wait, and the reply will need a tap when your phone is locked."
}

/// Plays the looping thinking cue and the one-shot chimes on the app's audio session.
///
/// `AVAudioPlayer` set-up talks to the audio server and can block (a route
/// change, or the simulator waiting on the Mac's microphone consent), so every
/// player call runs on a private serial queue: a slow audio device never
/// freezes the UI or the voice turn.
@MainActor
final class SoundPlayer {
    private let worker = SoundWorker()
    private(set) var cuePlaying = false

    /// Starts the loop (no-op when already running or when the level is Off).
    func startCue(level: ThinkingCueLevel = .current) {
        guard level != .off else { return }
        cuePlaying = true
        worker.startCue(volume: level.volume)
    }

    func stopCue() {
        cuePlaying = false
        worker.stopCue()
    }

    func play(_ data: @escaping @Sendable () -> Data, volume: Float = 1) {
        worker.play(data, volume: volume)
    }

    func playError() { play { ToneSynth.errorSound() } }
    func playInterruptionAlert() { play { ToneSynth.interruptionAlert() } }
    func playLongRecordingWarning() { play { ToneSynth.longRecordingWarning() } }
}

/// The queue-confined side of `SoundPlayer`.
private final class SoundWorker: @unchecked Sendable {
    private let queue = DispatchQueue(label: "org.loore.audio.sounds", qos: .userInitiated)
    private var cue: AVAudioPlayer?
    private var oneShots: [AVAudioPlayer] = []
    private lazy var cueData = ToneSynth.thinkingCue()
    private let log = Logger(subsystem: "org.loore.app", category: "sounds")

    func startCue(volume: Float) {
        queue.async { [self] in
            if let cue {
                cue.volume = volume
                if !cue.isPlaying { cue.play() }
                return
            }
            do {
                let player = try AVAudioPlayer(data: cueData)
                player.numberOfLoops = -1
                player.volume = volume
                player.play()
                cue = player
                log.info("thinking cue on")
            } catch {
                log.error("cue could not start")
            }
        }
    }

    func stopCue() {
        queue.async { [self] in
            guard let cue else { return }
            cue.stop()
            self.cue = nil
            log.info("thinking cue off")
        }
    }

    func play(_ data: @escaping @Sendable () -> Data, volume: Float) {
        queue.async { [self] in
            guard let player = try? AVAudioPlayer(data: data()) else { return }
            player.volume = volume
            player.play()
            oneShots.removeAll { !$0.isPlaying }
            oneShots.append(player)
        }
    }
}
