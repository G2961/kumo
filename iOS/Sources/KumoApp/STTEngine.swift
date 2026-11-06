import Foundation
import AVFoundation

/// Voice-to-text using the device microphone, mirroring the Android STT:
/// - "native" mode is a stub (iOS has no direct OpenAI-style on-device recognizer here),
/// - provider modes (Groq / custom OpenAI-compatible / OpenRouter) send a recorded WAV
///   to the HTTP transcription endpoint, exactly like `MainActivity.transcribeWav`.
public final class STTEngine: NSObject {
    public enum Mode: String {
        case native
        case groq
        case openrouter
        case custom
    }

    public struct Config {
        public var mode: Mode = .native
        public var key = ""
        public var model = "whisper-large-v3-turbo"
        public var baseURL = "https://api.groq.com/openai/v1"
    }

    public var config = Config()
    public var onResult: ((String) -> Void)?
    public var onError: ((String) -> Void)?

    private var engine: AVAudioEngine?
    private var recording = false

    public override init() {}

    public func isRecording() -> Bool { recording }

    /// Starts a recording of up to `maxSeconds`.
    public func start(maxSeconds: TimeInterval = 90) {
        guard !recording else { return }
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.record, mode: .default)
            try session.setActive(true)
        } catch {
            onError?("Microphone unavailable: \(error.localizedDescription)")
            return
        }

        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        let sampleRate = Int(format.sampleRate)
        var pcm = Data()
        let startedAt = Date()

        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 3200, format: format) { [weak self] buffer, _ in
            guard let self, self.recording else { return }
            let float = buffer.floatChannelData![0]
            let frames = Int(buffer.frameLength)
            var ints = [Int16](repeating: 0, count: frames)
            for i in 0..<frames {
                let v = float[i]
                let scaled = max(-1, min(1, v))
                ints[i] = Int16(Int(scaled * 32768))
            }
            let raw = Data(bytes: ints, bytesCount: ints.count * MemoryLayout<Int16>.size)
            pcm.append(raw)
        }

        self.engine = engine
        recording = true
        do {
            try engine.start()
        } catch {
            onError?("Could not start recording: \(error.localizedDescription)")
            stop()
            return
        }

        DispatchQueue.global().async { [weak self] in
            while self?.recording == true && Date().timeIntervalSince(startedAt) < maxSeconds {
                Thread.sleep(forTimeInterval: 0.1)
            }
            guard let self else { return }
            let wav = self.pcmToWav(pcm, sampleRate: sampleRate)
            self.stop()
            if wav.count >= sampleRate / 10 {
                self.transcribe(wav, sampleRate: sampleRate)
            } else {
                self.onError?("Did not catch anything")
            }
        }
    }

    public func stop() {
        recording = false
        engine?.stop()
        engine?.inputNode.removeTap(onBus: 0)
        engine = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func pcmToWav(_ pcm: Data, sampleRate: Int) -> Data {
        var out = Data()
        func le(_ v: Int, _ n: Int) {
            var x = v
            for _ in 0..<n {
                out.append(UInt8(x & 0xFF))
                x >>= 8
            }
        }
        out.append(contentsOf: "RIFF".utf8)
        le(36 + pcm.count, 4)
        out.append(contentsOf: "WAVE".utf8)
        out.append(contentsOf: "fmt ".utf8)
        le(16, 4); le(1, 2); le(1, 2)
        le(sampleRate, 4); le(sampleRate * 2, 4); le(2, 2); le(16, 2)
        out.append(contentsOf: "data".utf8)
        le(pcm.count, 4)
        out.append(pcm)
        return out
    }

    private func transcribe(_ wav: Data, sampleRate: Int) {
        switch config.mode {
        case .native:
            onError?("Voice input is not available (native mode unsupported on iOS)")
        case .openrouter:
            openRouter(wav)
        case .groq, .custom:
            openAICompatible(wav)
        }
    }

    private func openAICompatible(_ wav: Data) {
        let base = config.mode == .custom
            ? (config.baseURL.isEmpty ? "https://api.groq.com/openai/v1" : config.baseURL.trimmingCharacters(in: .whitespaces))
            : "https://api.groq.com/openai/v1"
        let url = URL(string: base + "/audio/transcriptions")!
        requestAudit(to: url, wav: wav, key: config.key, model: config.model)
    }

    private func openRouter(_ wav: Data) {
        guard let url = URL(string: "https://openrouter.ai/api/v1/chat/completions") else {
            onError?("Invalid URL"); return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(config.key)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let b64 = wav.base64EncodedString()
        let payload: [String: Any] = [
            "model": config.model,
            "messages": [
                ["role": "user", "content": [
                    ["type": "text", "text": "Transcribe the audio verbatim. Output only the transcription text."],
                    ["type": "input_audio", "input_audio": ["data": b64, "format": "wav"]]
                ]]
            ]
        ]
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else {
            onError?("Encoding error"); return
        }
        request.httpBody = body
        URLSession.shared.dataTask(with: request) { [weak self] data, resp, error in
            guard let self else { return }
            if let error {
                self.onError?(error.localizedDescription); return
            }
            guard let data, let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let choices = json["choices"] as? [[String: Any]],
                  let first = choices.first,
                  let msg = first["message"] as? [String: Any],
                  let text = msg["content"] as? String else {
                self.onError?("Transcription parse failed")
                return
            }
            self.onResult?(text)
        }.resume()
    }

    private func requestAudit(to url: URL, wav: Data, key: String, model: String) {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        let boundary = "----kumo" + String(Int(Date().timeIntervalSince1970))
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var body = Data()
        func field(_ name: String, _ value: String) {
            body.append(contentsOf: "--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".utf8)
        }
        field("model", model)
        field("response_format", "json")
        body.append(contentsOf: "--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"stt.wav\"\r\nContent-Type: audio/wav\r\n\r\n".utf8)
        body.append(wav)
        body.append(contentsOf: "\r\n--\(boundary)--\r\n".utf8)
        request.httpBody = body

        URLSession.shared.dataTask(with: request) { [weak self] data, resp, error in
            guard let self else { return }
            if let error {
                self.onError?(error.localizedDescription); return
            }
            guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                self.onError?("STT \(code): \(body.prefix(160))"); return
            }
            guard let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let text = obj["text"] as? String else {
                self.onError?("Transcription parse failed"); return
            }
            self.onResult?(text)
        }.resume()
    }
}