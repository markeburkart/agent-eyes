// voicesvc: on-device speech transcription and speech output for Agent Eyes.
//
// Listens to the default microphone (while switched on) and streams what it hears as JSON lines on stdout, and
// speaks text it is sent. The microphone stays on while speaking: the Python side uses the
// word-by-word progress events to recognize (and ignore) the assistant's own voice.
// Wake-word and dictation logic live in the Python side.
//
// Usage: voicesvc [--locale en-US] [--context "hey eyes,Raspberry Pi"] [--file speech.aiff] [--speech-volume 1] [--mic-off]
//                 [--voice com.apple.voice.premium.en-US.Ava] [--list-voices]
//   --locale ID          speech locale for listening and speaking (default: the system locale)
//   --voice REF          speaking voice: an identifier or a name ("Ava", "Ava (Premium)"); default: the best
//                        installed natural voice for the locale (premium, then enhanced, then compact; novelty
//                        and Eloquence voices only when chosen)
//   --list-voices        print one "voices" event and exit (no speech model or microphone needed)
//   --context LIST       comma-separated words and phrases to bias recognition towards (e.g. the wake word)
//   --file PATH          transcribe an audio file played in real time, then silence, instead of the
//                        microphone (for testing without speaking)
//   --speech-volume V    0..1 (default 1); 0 still reports progress, for testing
//   --mic-off            start with the microphone closed (speaking only, until "mic on")
//
// stdout, one JSON object per line:
//   {"event":"status","message":"..."}                  progress, e.g. a speech model download
//   {"event":"ready","locale":"en-US","voice":VOICE,"voices":[VOICE,...]}
//       started (preceded by a "mic" event unless --mic-off). `voice` is the speaking voice, `voices` the
//       installed ones for the locale's language (e.g. all en-*), best first, each
//       VOICE = {"identifier":"...","name":"Ava (Premium)","language":"en-US","quality":"premium|enhanced|compact",
//                "natural":true}   (natural=false: a novelty or Eloquence voice)
//   {"event":"voices","locale":"en-US","voice":VOICE,"voices":[...]}   with --list-voices: what "ready" would report
//   {"event":"voice","voice":VOICE}                     the voice was changed ({"event":"voice","error":"..."} if not)
//   {"event":"mic","on":true}                           the microphone was opened or closed;
//       {"event":"mic","on":false,"error":"..."} if it could not be opened (not fatal)
//   {"event":"text","final":false,"text":"...","start":1.2,"end":2.5}
//       final=false: a volatile (in-progress) guess for that audio range; later results replace it.
//       final=true: the settled transcript for that range.
//   {"event":"speaking","id":3,"spoken":"OK, first connect"}
//       utterance 3 is about to say the last word of `spoken` (its text so far)
//   {"event":"spoken","id":3,"cancelled":false}         utterance 3 finished, or was cut off
//   {"event":"error","error":"code","message":"..."}    fatal; the helper exits after it
//
// stdin, one command per line:
//   finalize            settle all pending volatile results now (as if the speaker had paused for long)
//   speak ID TEXT       queue TEXT (one line) to be spoken as utterance ID
//   stop_speaking       cut off the current utterance and drop the queued ones (each reports "spoken")
//   rate R              speaking rate for utterances queued from now on, 0..1 (AVSpeechUtterance's; default 0.5)
//   voice REF           voice for utterances queued from now on: an identifier or a name (replies with a "voice" event)
//   mic on|off          open or close the microphone (replies with a "mic" event)
// The helper exits when stdin closes, so it never outlives its parent. Logs go to stderr.

import AVFoundation
import CoreMedia
import Foundation
import Speech

let emitLock = NSLock()

func emit(_ obj: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: obj)
    emitLock.withLock { FileHandle.standardOutput.write(data + Data("\n".utf8)) }
}

func log(_ s: String) {
    FileHandle.standardError.write(Data("[voicesvc] \(s)\n".utf8))
}

func fail(_ code: String, _ message: String) -> Never {
    emit(["event": "error", "error": code, "message": message])
    exit(2)
}

var localeID = Locale.current.identifier
var contextualStrings: [String] = []
var audioFile: String?
var speechVolume: Float = 1
var micAtStart = true
var voiceRef: String?
var listVoices = false
do {
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let a = it.next() {
        switch a {
        case "--locale": localeID = it.next() ?? localeID
        case "--context": contextualStrings = (it.next() ?? "").split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
        case "--file": audioFile = it.next()
        case "--speech-volume": speechVolume = Float(it.next() ?? "") ?? 1
        case "--mic-off": micAtStart = false
        case "--voice": voiceRef = it.next()
        case "--list-voices": listVoices = true
        default: log("unknown argument \(a)"); exit(64)
        }
    }
}

// ---- voices -------------------------------------------------------------------

/// Novelty voices (Bubbles, Zarvox, ...), the old MacinTalk ones and Eloquence's (Eddy, Grandma, ...) sound robotic.
func isNatural(_ v: AVSpeechSynthesisVoice) -> Bool {
    !v.voiceTraits.contains(.isNoveltyVoice)
        && !v.identifier.hasPrefix("com.apple.speech.synthesis.") && !v.identifier.hasPrefix("com.apple.eloquence.")
}

func qualityName(_ q: AVSpeechSynthesisVoiceQuality) -> String {
    switch q {
    case .premium: "premium"
    case .enhanced: "enhanced"
    default: "compact"
    }
}

func describe(_ v: AVSpeechSynthesisVoice) -> [String: Any] {
    ["identifier": v.identifier, "name": v.name, "language": v.language, "quality": qualityName(v.quality), "natural": isNatural(v)]
}

func languageCode(_ bcp47: String) -> String? { Locale(identifier: bcp47).language.languageCode?.identifier }

/// Installed voices for `language`'s language (e.g. all en-* for en-US), best first: natural ones, by quality,
/// those for `language` itself before other regions, the system's own choice first, then by name.
func voices(for language: String) -> [AVSpeechSynthesisVoice] {
    let code = languageCode(language)
    let systemChoice = AVSpeechSynthesisVoice(language: language)?.identifier
    return AVSpeechSynthesisVoice.speechVoices()
        .filter { languageCode($0.language) == code }
        .sorted { a, b in
            func key(_ v: AVSpeechSynthesisVoice) -> (Int, Int, Int, Int) {
                (isNatural(v) ? 0 : 1, -v.quality.rawValue, v.language == language ? 0 : 1, v.identifier == systemChoice ? 0 : 1)
            }
            let (ka, kb) = (key(a), key(b))
            return ka != kb ? ka < kb : a.name.localizedStandardCompare(b.name) == .orderedAscending
        }
}

/// The best installed natural voice for `language` itself; the system's choice if there is none.
func bestVoice(for language: String) -> AVSpeechSynthesisVoice? {
    voices(for: language).first { isNatural($0) && $0.language == language } ?? AVSpeechSynthesisVoice(language: language)
}

/// A voice by identifier, or by name ("Ava" or "Ava (Premium)", any case; the best match for `language` first).
func findVoice(_ ref: String, language: String) -> AVSpeechSynthesisVoice? {
    if let v = AVSpeechSynthesisVoice(identifier: ref) { return v }
    let wanted = ref.lowercased()
    let base = { (name: String) in String(name.split(separator: " (", maxSplits: 1).first ?? "").lowercased() }
    let candidates = voices(for: language) + AVSpeechSynthesisVoice.speechVoices()
    return candidates.first { $0.name.lowercased() == wanted } ?? candidates.first { base($0.name) == wanted }
}

/// The voice to start with: `voiceRef` if given and installed, else the best one for `language`.
func startingVoice(language: String) -> AVSpeechSynthesisVoice? {
    if let ref = voiceRef {
        if let v = findVoice(ref, language: language) { return v }
        log("voice \(ref) is not installed; using the best one for \(language)")
    }
    return bestVoice(for: language)
}

if listVoices {
    let language = Locale(identifier: localeID).identifier(.bcp47)
    emit(["event": "voices", "locale": language, "voice": startingVoice(language: language).map(describe) ?? NSNull(),
          "voices": voices(for: language).map(describe)])
    exit(0)
}

/// Speaks queued utterances and reports their progress word by word.
final class Speaker: NSObject, AVSpeechSynthesizerDelegate, @unchecked Sendable {
    private let synth = AVSpeechSynthesizer()
    private var voice: AVSpeechSynthesisVoice?
    private let volume: Float
    private let lock = NSLock()
    private var pending: [ObjectIdentifier: Int] = [:]  // utterance -> id, until it reports "spoken"
    private var rate = AVSpeechUtteranceDefaultSpeechRate

    init(voice: AVSpeechSynthesisVoice?, volume: Float) {
        self.voice = voice
        self.volume = volume
        super.init()
        synth.delegate = self
    }

    func speak(id: Int, text: String) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.volume = volume
        lock.withLock {
            utterance.voice = voice
            utterance.rate = rate
            pending[ObjectIdentifier(utterance)] = id
        }
        synth.speak(utterance)
    }

    func setRate(_ r: Float) {
        lock.withLock { rate = min(max(r, AVSpeechUtteranceMinimumSpeechRate), AVSpeechUtteranceMaximumSpeechRate) }
    }

    var currentVoice: AVSpeechSynthesisVoice? {
        get { lock.withLock { voice } }
        set { lock.withLock { voice = newValue } }
    }

    func stop() {
        let ids = lock.withLock { () -> [Int] in
            defer { pending.removeAll() }
            return Array(pending.values)
        }
        synth.stopSpeaking(at: .immediate)
        for id in ids.sorted() { emit(["event": "spoken", "id": id, "cancelled": true]) }
    }

    private func id(of utterance: AVSpeechUtterance, remove: Bool = false) -> Int? {
        lock.withLock { remove ? pending.removeValue(forKey: ObjectIdentifier(utterance)) : pending[ObjectIdentifier(utterance)] }
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, willSpeakRangeOfSpeechString characterRange: NSRange, utterance: AVSpeechUtterance) {
        guard let id = id(of: utterance) else { return }
        let spoken = (utterance.speechString as NSString).substring(to: min(characterRange.upperBound, utterance.speechString.utf16.count))
        emit(["event": "speaking", "id": id, "spoken": spoken])
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        if let id = id(of: utterance, remove: true) { emit(["event": "spoken", "id": id, "cancelled": false]) }
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        if let id = id(of: utterance, remove: true) { emit(["event": "spoken", "id": id, "cancelled": true]) }
    }
}

/// Converts one buffer to the analyzer's format (the mic is usually 48 kHz float; the model wants 16 kHz int).
func convert(_ buffer: AVAudioPCMBuffer, with converter: AVAudioConverter) -> AVAudioPCMBuffer? {
    let ratio = converter.outputFormat.sampleRate / buffer.format.sampleRate
    let capacity = AVAudioFrameCount((Double(buffer.frameLength) * ratio).rounded(.up)) + 1
    guard let out = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: capacity) else { return nil }
    var consumed = false
    var error: NSError?
    converter.convert(to: out, error: &error) { _, status in
        if consumed {
            status.pointee = .noDataNow
            return nil
        }
        consumed = true
        status.pointee = .haveData
        return buffer
    }
    if let error {
        log("audio conversion failed: \(error)")
        return nil
    }
    return out
}

guard SpeechTranscriber.isAvailable else {
    fail("speech_unavailable", "On-device speech transcription is not available on this Mac.")
}
guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: localeID)) else {
    fail("unsupported_locale", "Speech transcription does not support locale \(localeID).")
}
let transcriber = SpeechTranscriber(
    locale: locale,
    transcriptionOptions: [],
    reportingOptions: [.volatileResults, .fastResults],
    attributeOptions: []
)

do {
    if await AssetInventory.status(forModules: [transcriber]) != .installed,
       let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
        emit(["event": "status", "message": "downloading the speech model for \(locale.identifier) (first run only)"])
        try await request.downloadAndInstall()
    }
} catch {
    fail("model_install_failed", "Could not install the speech model for \(locale.identifier): \(error)")
}

guard let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
    fail("no_audio_format", "No audio format is compatible with the speech model.")
}
let analyzer = SpeechAnalyzer(modules: [transcriber], options: .init(priority: .userInitiated, modelRetention: .processLifetime))
if !contextualStrings.isEmpty {
    let context = AnalysisContext()
    context.contextualStrings[.general] = contextualStrings
    try await analyzer.setContext(context)
}
try await analyzer.prepareToAnalyze(in: analyzerFormat)

let (inputs, inputContinuation) = AsyncStream.makeStream(of: AnalyzerInput.self)

let resultsTask = Task {
    do {
        for try await result in transcriber.results {
            emit([
                "event": "text",
                "final": result.isFinal,
                "text": String(result.text.characters),
                "start": result.range.start.seconds,
                "end": result.range.end.seconds,
            ])
        }
    } catch {
        fail("transcription_failed", "\(error)")
    }
}

try await analyzer.start(inputSequence: inputs)

/// Where the transcriber's audio comes from: the microphone, or a test file played in real time.
/// Switched on and off with the `mic` command; while off the microphone is closed.
final class AudioInput: @unchecked Sendable {
    private let lock = NSLock()
    private var isOn = false
    private let engine = AVAudioEngine()
    private var tapInstalled = false
    private let fromFile: Bool
    private let format: AVAudioFormat
    private let sink: AsyncStream<AnalyzerInput>.Continuation

    init(fromFile: Bool, format: AVAudioFormat, sink: AsyncStream<AnalyzerInput>.Continuation) {
        self.fromFile = fromFile
        self.format = format
        self.sink = sink
    }

    var on: Bool { lock.withLock { isOn } }

    /// Returns an error message if the microphone could not be opened.
    func set(on: Bool) async -> String? {
        if fromFile || !on {
            if !fromFile { engine.stop() }
            lock.withLock { isOn = on }
            return nil
        }
        var allowed = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            allowed = await AVCaptureDevice.requestAccess(for: .audio)
        }
        guard allowed else {
            return "Grant microphone access to the terminal app in System Settings > Privacy & Security > Microphone."
        }
        if !tapInstalled {
            let micFormat = engine.inputNode.outputFormat(forBus: 0)
            guard micFormat.sampleRate > 0, let converter = AVAudioConverter(from: micFormat, to: format) else {
                return "No usable microphone input (format \(micFormat))."
            }
            let sink = sink
            engine.inputNode.installTap(onBus: 0, bufferSize: 4096, format: micFormat) { buffer, _ in
                if let out = convert(buffer, with: converter) { sink.yield(AnalyzerInput(buffer: out)) }
            }
            tapInstalled = true
            engine.prepare()
        }
        do { try engine.start() } catch { return "Could not start the microphone: \(error)" }
        lock.withLock { isOn = true }
        return nil
    }

    func stop() {
        if !fromFile { engine.stop() }
    }
}

let input = AudioInput(fromFile: audioFile != nil, format: analyzerFormat, sink: inputContinuation)
if let path = audioFile {
    let file: AVAudioFile
    do { file = try AVAudioFile(forReading: URL(fileURLWithPath: path)) } catch { fail("bad_file", "Cannot read \(path): \(error)") }
    guard let converter = AVAudioConverter(from: file.processingFormat, to: analyzerFormat) else {
        fail("no_audio_format", "Cannot convert \(path) to the speech model's format.")
    }
    // Feed the file in real time, then silence forever, so pauses behave as they would from a mic.
    Task {
        let chunk = AVAudioFrameCount(file.processingFormat.sampleRate / 10)
        while true {
            try? await Task.sleep(for: .milliseconds(100))
            guard input.on else { continue }
            guard let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: chunk) else { return }
            if file.framePosition < file.length {
                try? file.read(into: buffer, frameCount: chunk)
            } else {
                buffer.frameLength = chunk  // zero-filled
            }
            if let out = convert(buffer, with: converter) { inputContinuation.yield(AnalyzerInput(buffer: out)) }
        }
    }
}

func setMic(_ on: Bool) async {
    if let error = await input.set(on: on) {
        emit(["event": "mic", "on": false, "error": error])
    } else {
        emit(["event": "mic", "on": on])
    }
}

if micAtStart { await setMic(true) }

let language = locale.identifier(.bcp47)
let speaker = Speaker(voice: startingVoice(language: language), volume: speechVolume)

emit(["event": "ready", "locale": language, "voice": speaker.currentVoice.map(describe) ?? NSNull(),
      "voices": voices(for: language).map(describe)])
log("ready (\(locale.identifier), \(audioFile ?? "microphone") \(input.on ? "on" : "off"), voice \(speaker.currentVoice?.identifier ?? "none"))")

func setVoice(_ ref: String) {
    guard let v = findVoice(ref, language: language) else {
        emit(["event": "voice", "error": "voice \(ref) is not installed"])
        return
    }
    speaker.currentVoice = v
    emit(["event": "voice", "voice": describe(v)])
}

do {
    for try await line in FileHandle.standardInput.bytes.lines {
        let parts = line.split(separator: " ", maxSplits: 2).map(String.init)
        switch parts.first ?? "" {
        case "finalize": try await analyzer.finalize(through: nil)
        case "speak" where parts.count == 3 && Int(parts[1]) != nil: speaker.speak(id: Int(parts[1])!, text: parts[2])
        case "stop_speaking": speaker.stop()
        case "rate" where parts.count == 2 && Float(parts[1]) != nil: speaker.setRate(Float(parts[1])!)
        case "voice" where parts.count >= 2: setVoice(String(line.dropFirst("voice ".count)).trimmingCharacters(in: .whitespaces))
        case "mic" where parts.count == 2 && ["on", "off"].contains(parts[1]): await setMic(parts[1] == "on")
        case "": break
        default: log("unknown command \(line)")
        }
    }
} catch {
    log("stdin: \(error)")
}

speaker.stop()
input.stop()
inputContinuation.finish()
await analyzer.cancelAndFinishNow()
resultsTask.cancel()
exit(0)
