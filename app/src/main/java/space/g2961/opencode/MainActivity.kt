package space.g2961.opencode

import android.Manifest
import android.annotation.SuppressLint
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import java.io.File
import java.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import org.json.JSONArray
import org.json.JSONObject

class MainActivity : ComponentActivity(), LocalRuntime.Listener {

    private val pickFolder = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        if (uri == null) { finishFolderRequest(true); return@registerForActivityResult }
        onFolderPicked(uri)
    }
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val pickFiles = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        fileCallback?.onReceiveValue(android.webkit.WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data))
        fileCallback = null
    }
    // Native photo picker: the WebView file-chooser path silently drops results on this
    // device (Google Photo picker + system Uri-grant hiccup), so photos ride the bridge.
    private var photoRequest: String? = null
    private val pickPhotos = registerForActivityResult(ActivityResultContracts.PickMultipleVisualMedia(8)) { uris ->
        val req = photoRequest
        photoRequest = null
        if (req != null) {
            if (uris.isEmpty()) {
                nativeResult(req, JSONObject().put("files", JSONArray()))
            } else Thread {
                val files = JSONArray()
                for (u in uris) {
                    try {
                        val mime = contentResolver.getType(u) ?: "image/jpeg"
                        var name = "photo.jpg"
                        contentResolver.query(u, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                            if (c.moveToFirst() && !c.isNull(0)) name = c.getString(0) ?: name
                        }
                        val bytes = contentResolver.openInputStream(u)?.use { it.readBytes() } ?: continue
                        val b64 = android.util.Base64.encodeToString(bytes, android.util.Base64.NO_WRAP)
                        files.put(JSONObject().put("type", "file").put("mime", mime).put("filename", name)
                            .put("url", "data:$mime;base64,$b64").put("size", bytes.size))
                    } catch (_: Throwable) {
                    }
                }
                nativeResult(req, JSONObject().put("files", files))
            }.start()
        }
    }
    private var folderRequest: String? = null
    @Volatile private var trustedUi = false
    private lateinit var sshTools: SshToolsServer

    private val requestStorage = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
        val granted = result.values.all { it }
        appendRuntimeLog(if (granted) "storage: granted" else "storage: denied $result")
        pickFolder.launch(null)
    }

    /** Папка, на которую ждём «Доступ ко всем файлам» (вернулись из настроек). */
    private var pendingDir: File? = null

    private lateinit var runtime: LocalRuntime
    private lateinit var statusScreen: LinearLayout
    private lateinit var statusText: TextView
    private lateinit var progressBar: ProgressBar
    private lateinit var retryButton: Button
    private lateinit var webview: WebView
    private var recognizer: android.speech.SpeechRecognizer? = null
    private var lastInsetJs: String? = null
    private var splashActive = true
    private lateinit var rootLayout: FrameLayout
    private var pendingCrossFade: (() -> Unit)? = null

    private val requestAudio = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        appendRuntimeLog(if (granted) "voice: mic granted" else "voice: mic denied")
        if (granted) startVoiceInput()
    }

    private val bridge = MobileBridge()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Draw behind the gesture bar so sheets/dim reach the true screen edge;
        // the page handles insets itself via env(safe-area-inset-*).
        enableEdgeToEdge()
        window.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
        sshTools = SshToolsServer(SshConnectionStore(this)) { event ->
            runOnUiThread { if (trustedUi) webview.evaluateJavascript("window.dispatchEvent(new CustomEvent('codex-remote-event',{detail:$event}));", null) }
        }
        sshTools.start()
        runtime = LocalRuntime(this, sshTools)
        // The in-memory log is invisible to adb; mirror server stdout into logcat.
        runtime.logHook = { line -> android.util.Log.i("kumo-runtime", line) }
        buildUi()
        startRuntime()
        // Foreground keep-alive: agent streaming + bundled server survive backgrounding.
        startForegroundService(Intent(this, KeepAliveService::class.java))
        // Ask once for battery-optimization exemption — the hard guarantee that Doze
        // never freezes the agent mid-stream on a phone this starved for RAM.
        val power = getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
        val prefs = getSharedPreferences("app", Context.MODE_PRIVATE)
        if (!power.isIgnoringBatteryOptimizations(packageName) && !prefs.getBoolean("batteryAsked", false)) {
            prefs.edit().putBoolean("batteryAsked", true).apply()
            try {
                startActivity(Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:$packageName")))
            } catch (_: Throwable) {
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (trustedUi && webview.visibility == View.VISIBLE) {
            webview.evaluateJavascript(
                "(() => { if (window.__appBack) return window.__appBack(); if (typeof isWorkSheetOpen === 'function' && isWorkSheetOpen()) { if (typeof requestCloseWorkSheet === 'function') requestCloseWorkSheet(); else closeWorkSheet(); return true; } return false; })()"
            ) { handled ->
                if (handled != "true") closeTopmostOrMoveBack()
            }
        } else navigateBack()
    }

    private fun navigateBack() {
        if (webview.visibility == View.VISIBLE && webview.canGoBack()) webview.goBack() else moveTaskToBack(true)
    }

    override fun onDestroy() {
        bridge.shutdown()
        runtime.stop()
        recognizer?.destroy(); recognizer = null
        sshTools.stop()
        fileCallback?.onReceiveValue(null)
        super.onDestroy()
    }

    private fun startRuntime() {
        if (splashActive) {
            // The splash page is the status surface: keep it visible, just reset its state.
            webview.evaluateJavascript("window.__splashState&&window.__splashState(${JSONObject.quote(getString(R.string.starting))})", null)
            runtime.start(this)
            return
        }
        runOnUiThread {
            statusScreen.visibility = View.VISIBLE
            webview.visibility = View.GONE
            progressBar.visibility = View.VISIBLE
            retryButton.visibility = View.GONE
            statusText.text = getString(R.string.starting)
        }
        runtime.start(this)
    }

    /** Called from the splash page's «Повторить» through the CodexSplash bridge. */
    fun splashRetry() {
        if (splashActive) runtime.restart(this) else startRuntime()
    }

    private fun buildUi() {
        val root = FrameLayout(this)
        rootLayout = root
        root.setBackgroundColor(Color.parseColor("#090A0C"))

        statusScreen = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(24), dp(48), dp(24), dp(24))
        }
        val logo = TextView(this).apply {
            text = "opencode"
            textSize = 42f
            setTextColor(Color.parseColor("#2A2D31"))
            letterSpacing = -0.02f
            typeface = android.graphics.Typeface.create("sans-serif-black", android.graphics.Typeface.BOLD)
        }
        progressBar = ProgressBar(this).apply {
            val p = dp(8)
            setPadding(p, p, p, p)
        }
        statusText = TextView(this).apply {
            textSize = 14f
            setTextColor(Color.parseColor("#B4B4B4"))
            setPadding(0, dp(16), 0, dp(8))
        }
        retryButton = Button(this).apply {
            text = getString(R.string.retry)
            visibility = View.GONE
            setOnClickListener { startRuntime() }
        }
        val buttons = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            addView(retryButton)
        }
        statusScreen.addView(logo, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        statusScreen.addView(progressBar)
        statusScreen.addView(statusText)
        statusScreen.addView(buttons)
        root.addView(statusScreen, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        webview = WebView(this).apply {
            visibility = View.VISIBLE
            overScrollMode = View.OVER_SCROLL_NEVER
            setBackgroundColor(splashBackground())
        }
        configureWebview(webview)
        root.addView(webview, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        // The new splash doubles as the status screen while the server boots.
        // The theme rides in the URL: the app's chosen theme, not the system's.
        val themePref = getSharedPreferences("app", Context.MODE_PRIVATE).getString("uiTheme", null)
        webview.loadUrl("file:///android_asset/splash.html" + if (themePref != null) "?theme=$themePref" else "")

        setContentView(root)
        // With edge-to-edge, adjustResize no longer resizes for the IME; apply the
        // keyboard inset as bottom padding so the composer stays above the keyboard.
        // System bar insets are handed to the page as CSS vars: WebView's env(safe-area-*)
        // covers the cutout but not the gesture bar, so sheets would touch the nav area.
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val ime = androidx.core.view.WindowInsetsCompat.Type.ime().let { insets.getInsets(it) }
            val bars = androidx.core.view.WindowInsetsCompat.Type.systemBars().let { insets.getInsets(it) }
            v.setPadding(0, 0, 0, ime.bottom)
            // Insets arrive in physical pixels; CSS px are density-independent — convert,
            // or the header padding becomes density× too tall.
            val density = resources.displayMetrics.density
            val js = "document.documentElement.style.setProperty('--sat','${(bars.top / density).toInt()}px');document.documentElement.style.setProperty('--sab','${(bars.bottom / density).toInt()}px');"
            if (js != lastInsetJs) {
                lastInsetJs = js
                runOnUiThread { if (trustedUi) webview.evaluateJavascript(js, null) }
            }
            insets
        }
    }

    private fun splashBackground(): Int {
        return when (getSharedPreferences("app", Context.MODE_PRIVATE).getString("uiTheme", null)
            ?: if ((resources.configuration.uiMode and android.content.res.Configuration.UI_MODE_NIGHT_MASK) ==
                android.content.res.Configuration.UI_MODE_NIGHT_YES) "dark" else "light") {
            "dark" -> Color.parseColor("#1B1B1B")
            else -> Color.WHITE
        }
    }

    /** Voice input: Android speech recognition → window.__voiceResult(text) in the page. */
    fun startVoiceInput() {
        val prefs = getSharedPreferences("stt", Context.MODE_PRIVATE)
        val mode = prefs.getString("mode", "native") ?: "native"
        val key = prefs.getString("key", "") ?: ""
        if (mode != "native" && key.isNotEmpty()) { startSttRecording(); return }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) startVoiceRecognition()
        else requestAudio.launch(Manifest.permission.RECORD_AUDIO)
    }

    private var sttRecorder: android.media.AudioRecord? = null
    private var sttThread: Thread? = null
    @Volatile private var sttActive = false
    private val exposeSockets = java.util.concurrent.CopyOnWriteArrayList<java.net.ServerSocket>()

    /** Whisper-style STT: record PCM from the mic, wrap it into a WAV, POST it to the chosen provider. */
    private fun startSttRecording() {
        if (sttActive) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            android.util.Log.i("stt", "no mic permission, requesting")
            requestAudio.launch(Manifest.permission.RECORD_AUDIO); return
        }
        val sampleRate = 16000
        val minBuf = android.media.AudioRecord.getMinBufferSize(sampleRate, android.media.AudioFormat.CHANNEL_IN_MONO, android.media.AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) {
            android.util.Log.i("stt", "getMinBufferSize failed: $minBuf")
            if (trustedUi) webview.evaluateJavascript("typeof window.__voiceError==='function'&&window.__voiceError('AudioRecord is unavailable on this device')", null)
            return
        }
        val rec = android.media.AudioRecord(android.media.MediaRecorder.AudioSource.MIC, sampleRate,
            android.media.AudioFormat.CHANNEL_IN_MONO, android.media.AudioFormat.ENCODING_PCM_16BIT, minBuf * 2)
        val pcm = java.io.ByteArrayOutputStream()
        sttActive = true
        android.util.Log.i("stt", "recording started (state=${rec.state})")
        rec.startRecording()
        sttRecorder = rec
        sttThread = Thread {
            val startedAt = android.os.SystemClock.uptimeMillis()
            val buf = ByteArray(3200) // ~100ms of 16kHz mono 16-bit
            try {
                while (sttActive) {
                    val n = rec.read(buf, 0, buf.size)
                    if (n > 0) pcm.write(buf, 0, n)
                    if (android.os.SystemClock.uptimeMillis() - startedAt > 90_000) break // hard cap: 90s per utterance
                }
            } catch (_: Throwable) {
            }
            try { rec.stop() } catch (_: Throwable) {
            }
            try { rec.release() } catch (_: Throwable) {
            }
            sttRecorder = null
            android.util.Log.i("stt", "recording stopped, pcm=${pcm.size()} bytes")
            // sttActive is false by now even on a normal stop (the JS tap cleared it),
            // so the check below must NOT depend on it — otherwise the POST never fires.
            val wav = pcmToWav(pcm.toByteArray(), sampleRate)
            if (wav.size >= sampleRate / 10) { // at least ~100ms of speech
                val file = java.io.File(cacheDir, "stt.wav")
                file.writeBytes(wav)
                transcribeWav(file)
            } else {
                android.util.Log.i("stt", "wav too small (${wav.size} bytes)")
                runOnUiThread {
                    if (trustedUi) webview.evaluateJavascript("typeof window.__voiceError==='function'&&window.__voiceError('Did not catch anything')", null)
                }
            }
        }.also { it.start() }
    }

    private fun stopSttRecording() {
        sttActive = false
        // rec.read is blocking; stop() kicks it out of the wait so the thread sees sttActive=false
        try { sttRecorder?.stop() } catch (_: Throwable) {
        }
    }

    private fun pcmToWav(pcm: ByteArray, sampleRate: Int): ByteArray {
        val out = java.io.ByteArrayOutputStream(pcm.size + 44)
        val le = { v: Int, n: Int -> for (i in 0 until n) out.write((v shr (i * 8)) and 0xFF) }
        out.write("RIFF".toByteArray()); le(36 + pcm.size, 4); out.write("WAVE".toByteArray())
        out.write("fmt ".toByteArray()); le(16, 4); le(1, 2); le(1, 2) // PCM, mono
        le(sampleRate, 4); le(sampleRate * 2, 4); le(2, 2); le(16, 2)
        out.write("data".toByteArray()); le(pcm.size, 4); out.write(pcm)
        return out.toByteArray()
    }

    private fun transcribeWav(file: java.io.File) {
        val prefs = getSharedPreferences("stt", Context.MODE_PRIVATE)
        val mode = prefs.getString("mode", "native") ?: "native"
        val key = prefs.getString("key", "") ?: ""
        val model = (prefs.getString("model", "") ?: "").ifEmpty { "whisper-large-v3-turbo" }
        Thread {
            android.util.Log.i("stt", "transcribing via $mode (model=$model)")
            try {
                val text = when (mode) {
                    "openrouter" -> openrouterTranscribe(file, key, model)
                    else -> {
                        // Groq and any custom OpenAI-compatible endpoint share /audio/transcriptions.
                        val base = if (mode == "custom") (prefs.getString("base", "") ?: "").trim().trimEnd('/')
                        else "https://api.groq.com/openai/v1"
                        if (base.isEmpty()) throw java.io.IOException("Custom base URL is not set")
                        android.util.Log.i("stt", "POST $base/audio/transcriptions (${file.length()} bytes)")
                        openAiTranscribe("$base/audio/transcriptions", key, model, file)
                    }
                }
                android.util.Log.i("stt", "transcribed ${text.length} chars")
                runOnUiThread {
                    if (trustedUi) webview.evaluateJavascript("typeof window.__voiceResult==='function'&&window.__voiceResult(${JSONObject.quote(text)})", null)
                }
            } catch (e: Throwable) {
                // WebView methods are main-thread only; the crash log proved the hard way.
                android.util.Log.i("stt", "transcribe failed: $e")
                val msg = e.message ?: e.javaClass.simpleName
                runOnUiThread {
                    if (trustedUi) webview.evaluateJavascript("typeof window.__voiceError==='function'&&window.__voiceError(${JSONObject.quote(msg)})", null)
                }
            } finally {
                file.delete()
            }
        }.start()
    }

    private fun openAiTranscribe(url: String, key: String, model: String, file: java.io.File): String {
        val boundary = "----kumo" + System.currentTimeMillis()
        val conn = (java.net.URL(url).openConnection() as java.net.HttpURLConnection).apply {
            requestMethod = "POST"; doOutput = true; connectTimeout = 10_000; readTimeout = 90_000
            setRequestProperty("Authorization", "Bearer $key")
            setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
        }
        conn.outputStream.use { os ->
            fun field(name: String, value: String) {
                os.write("--$boundary\r\nContent-Disposition: form-data; name=\"$name\"\r\n\r\n$value\r\n".toByteArray())
            }
            field("model", model)
            field("response_format", "json")
            os.write("--$boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"stt.wav\"\r\nContent-Type: audio/wav\r\n\r\n".toByteArray())
            os.write(file.readBytes())
            os.write("\r\n--$boundary--\r\n".toByteArray())
        }
        val code = conn.responseCode
        android.util.Log.i("stt", "transcribe response: $code")
        val body = (if (code in 200..299) conn.inputStream else conn.errorStream)?.readBytes()?.toString(Charsets.UTF_8) ?: ""
        if (code !in 200..299) throw java.io.IOException("STT $code: ${body.take(160)}")
        return JSONObject(body).optString("text")
    }

    private fun openrouterTranscribe(file: java.io.File, key: String, model: String): String {
        val conn = (java.net.URL("https://openrouter.ai/api/v1/chat/completions").openConnection() as java.net.HttpURLConnection).apply {
            requestMethod = "POST"; doOutput = true; connectTimeout = 10_000; readTimeout = 120_000
            setRequestProperty("Authorization", "Bearer $key")
            setRequestProperty("Content-Type", "application/json")
        }
        val b64 = android.util.Base64.encodeToString(file.readBytes(), android.util.Base64.NO_WRAP)
        val body = JSONObject()
            .put("model", model)
            .put("messages", JSONArray().put(JSONObject().put("role", "user").put("content", JSONArray()
                .put(JSONObject().put("type", "text").put("text", "Transcribe the audio verbatim. Output only the transcription text."))
                .put(JSONObject().put("type", "input_audio").put("input_audio", JSONObject().put("data", b64).put("format", "wav"))))))
        conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
        val code = conn.responseCode
        val resp = (if (code in 200..299) conn.inputStream else conn.errorStream)?.readBytes()?.toString(Charsets.UTF_8) ?: ""
        if (code !in 200..299) throw java.io.IOException("STT $code: ${resp.take(160)}")
        val choices = JSONObject(resp).optJSONArray("choices")
        return choices?.optJSONObject(0)?.optJSONObject("message")?.optString("content") ?: ""
    }

    private fun startVoiceRecognition() {
        if (!android.speech.SpeechRecognizer.isRecognitionAvailable(this)) {
            if (trustedUi) webview.evaluateJavascript("typeof window.__voiceError==='function'&&window.__voiceError('Speech recognition is not available on this device')", null)
            return
        }
        recognizer?.destroy()
        val intent = android.content.Intent(android.speech.RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(android.speech.RecognizerIntent.EXTRA_LANGUAGE_MODEL, android.speech.RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        }
        recognizer = android.speech.SpeechRecognizer.createSpeechRecognizer(this).apply {
            setRecognitionListener(object : android.speech.RecognitionListener {
                override fun onReadyForSpeech(params: Bundle?) {}
                override fun onBeginningOfSpeech() {}
                override fun onRmsChanged(rmsdB: Float) {}
                override fun onBufferReceived(buffer: ByteArray?) {}
                override fun onEndOfSpeech() {}
                override fun onError(error: Int) {
                    val message = when (error) {
                        android.speech.SpeechRecognizer.ERROR_NO_MATCH, android.speech.SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "Did not catch anything"
                        android.speech.SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "Microphone permission denied"
                        else -> "Recognition failed ($error)"
                    }
                    if (trustedUi) webview.evaluateJavascript("typeof window.__voiceError==='function'&&window.__voiceError(${JSONObject.quote(message)})", null)
                }
                override fun onResults(results: Bundle?) {
                    val text = results?.getStringArrayList(android.speech.SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull() ?: return
                    if (trustedUi) webview.evaluateJavascript("typeof window.__voiceResult==='function'&&window.__voiceResult(${JSONObject.quote(text)})", null)
                }
                override fun onPartialResults(partialResults: Bundle?) {}
                override fun onEvent(eventType: Int, params: Bundle?) {}
            })
            startListening(intent)
        }
    }

    private inner class SplashBridge {
        @JavascriptInterface fun retry() {
            runOnUiThread { splashRetry() }
        }
    }
    private val splashBridge = SplashBridge()

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebview(wv: WebView) {
        WebView.setWebContentsDebuggingEnabled(true)
        wv.overScrollMode = View.OVER_SCROLL_NEVER
        wv.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = true
            cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
            setSupportZoom(false)
            builtInZoomControls = false
            displayZoomControls = false
        }
        wv.addJavascriptInterface(bridge, "CodexNative")
        wv.addJavascriptInterface(splashBridge, "CodexSplash")
        wv.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                val u = url?.let { Uri.parse(it) }
                trustedUi = u != null && u.host == "127.0.0.1" && u.port == runtime.authPort && u.path == "/_ui/index.html"
            }
            override fun onPageFinished(view: WebView?, url: String?) {
                // The insets listener can fire before the page exists; re-apply its vars now.
                lastInsetJs?.let { js -> if (trustedUi) view?.evaluateJavascript(js, null) }
                if (url != null && url.contains("splash.html")) {
                    runOnUiThread {
                        splashActive = true
                        webview.visibility = View.VISIBLE
                        statusScreen.visibility = View.GONE
                    }
                } else if (url != null && url.startsWith("http")) {
                    // The app page has painted: let it settle for a beat, then cross-fade the splash away.
                    val fade = pendingCrossFade
                    pendingCrossFade = null
                    if (fade != null) runOnUiThread { view?.postDelayed(fade, 180) }
                }
            }
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val url = request?.url ?: return false
                if (url.host == "127.0.0.1" || url.host == "localhost") return false
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, url))
                } catch (_: Throwable) {
                }
                return true
            }

            override fun shouldInterceptRequest(view: WebView?, request: WebResourceRequest?): WebResourceResponse? {
                val u = request?.url ?: return null
                if (u.host == "127.0.0.1" && u.port == runtime.authPort && u.path == "/_ui/thread-preview.html") {
                    return WebResourceResponse(
                        "text/html", "utf-8", 200, "OK",
                        mapOf("Cache-Control" to "no-store"), assets.open("thread-preview.html")
                    )
                }
                if (u.host == "127.0.0.1" && u.port == runtime.authPort && u.path != null && u.path!!.startsWith("/_ui/kumo_")) {
                    val name = u.path!!.removePrefix("/_ui/")
                    if (Regex("^kumo_[a-z]+\\.png$").matches(name)) {
                        return try {
                            WebResourceResponse(
                                "image/png", null, 200, "OK",
                                mapOf("Cache-Control" to "no-store"), assets.open(name)
                            )
                        } catch (_: Throwable) {
                            null
                        }
                    }
                }
                if (u.host == "127.0.0.1" && u.path == "/_ui/index.html") {
                    return try {
                        val devFile = getExternalFilesDir(null)?.resolve("codex.html")
                        val stream = if (devFile != null && devFile.exists() && devFile.isFile && devFile.canRead()) {
                            devFile.inputStream()
                        } else {
                            assets.open("codex.html")
                        }
                        WebResourceResponse(
                            "text/html", "utf-8", 200, "OK",
                            mapOf("Cache-Control" to "no-store"),
                            stream
                        )
                    } catch (_: Throwable) {
                        null
                    }
                }
                return null
            }

            override fun onReceivedHttpError(view: WebView?, request: WebResourceRequest?, errorResponse: WebResourceResponse?) {
                runtime.appendLogPublic("[http] ${errorResponse?.statusCode} ${request?.url}")
            }

            override fun onReceivedError(view: WebView?, request: WebResourceRequest?, error: WebResourceError?) {
                if (request?.isForMainFrame == true) {
                    runtime.appendLogPublic("[webview] ${error?.errorCode} ${error?.description} ${request?.url}")
                }
            }
        }
        wv.webChromeClient = object : android.webkit.WebChromeClient() {
            override fun onShowFileChooser(view: WebView?, callback: ValueCallback<Array<Uri>>?, params: android.webkit.WebChromeClient.FileChooserParams?): Boolean {
                if (!trustedUi || callback == null || params == null) return false
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                try { pickFiles.launch(params.createIntent()) } catch (_: Throwable) { callback.onReceiveValue(null); fileCallback = null }
                return true
            }
        }
    }

    /** System back closes the topmost in-page layer first; only when nothing is open
     *  the app is moved to background (kept alive) instead of being destroyed. */
    private fun closeTopmostOrMoveBack() {
        if (trustedUi) {
            webview.evaluateJavascript("String(window.__appBack?window.__appBack():false)") { v ->
                if (v != "\"true\"") moveTaskToBack(true)
            }
        } else moveTaskToBack(true)
    }

    private fun appendRuntimeLog(line: String) {
        runtime.appendLogPublic(line)
    }

    private fun nativeResult(id: String, result: JSONObject) {
        runOnUiThread { if (trustedUi) webview.evaluateJavascript("window.codexNativeResult(${JSONObject.quote(id)},$result);", null) }
    }
    private fun finishFolderRequest(cancelled: Boolean = false) {
        folderRequest?.let { nativeResult(it, JSONObject().put("cancelled", cancelled).put("directory", ProjectManager.currentDir(this).absolutePath)) }
        folderRequest = null
    }
    private inner class MobileBridge {
        private val bgExecutor = java.util.concurrent.Executors.newSingleThreadExecutor { r ->
            Thread(r, "mobile-bridge-worker").apply { isDaemon = true }
        }

        fun shutdown() {
            bgExecutor.shutdown()
        }

        @JavascriptInterface fun voiceAvailable(): Boolean = android.speech.SpeechRecognizer.isRecognitionAvailable(this@MainActivity)

        @JavascriptInterface fun request(raw: String) {
            if (!trustedUi || raw.length > 2 * 1024 * 1024) return
            bgExecutor.execute {
                var id = ""
                var action = ""
                try {
                    val request = JSONObject(raw); id = request.getString("id")
                    action = request.getString("action")
                    val data = request.optJSONObject("data") ?: JSONObject()
                    val result = when (request.getString("action")) {
                        "projects" -> JSONObject().put("projects", JSONArray(ProjectManager.list(this@MainActivity).map { it.absolutePath }))
                        "createProject" -> {
                            val dir = ProjectManager.createNew(this@MainActivity, data.getString("name"))
                            runtime.registerProject(dir.absolutePath)
                            JSONObject().put("directory", dir.absolutePath)
                        }
                        "selectProject" -> {
                            val dir = File(data.getString("directory"))
                            require(dir.isDirectory && ProjectManager.canRead(dir))
                            ProjectManager.setCurrent(this@MainActivity, dir)
                            require(runtime.registerProject(dir.absolutePath)) { "Could not register project" }
                            JSONObject().put("directory", dir.absolutePath)
                        }
                        "openFolder" -> {
                            folderRequest = id; runOnUiThread { openFolderPicker() }; return@execute
                        }
                        "pickPhotos" -> {
                            photoRequest = id
                            try {
                                pickPhotos.launch(androidx.activity.result.PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
                            } catch (_: Throwable) {
                                nativeResult(id, JSONObject().put("files", JSONArray()))
                            }
                            return@execute
                        }
                        "appearance" -> {
                            runOnUiThread {
                                val dark = data.optString("theme") == "dark"
                                // Remember the user's chosen theme for the splash on the next cold start.
                                getSharedPreferences("app", Context.MODE_PRIVATE).edit()
                                    .putString("uiTheme", if (dark) "dark" else "light").apply()
                                // Paint the system bars with the page's own surface color: the
                                // legacy scrim must match --base or it reads as a foreign strip.
                                val color = try {
                                    Color.parseColor(data.optString("base", if (dark) "#212121" else "#FFFFFF"))
                                } catch (_: IllegalArgumentException) {
                                    if (dark) Color.parseColor("#212121") else Color.WHITE
                                }
                                webview.setBackgroundColor(color); window.statusBarColor = color; window.navigationBarColor = color
                                window.decorView.systemUiVisibility = if (dark) 0 else View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR or View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
                            }
                            JSONObject().put("saved", true)
                        }
                        "voiceInput" -> {
                            runOnUiThread { startVoiceInput() }
                            JSONObject().put("started", true)
                        }
                        "githubToken" -> {
                            val token = data.optString("token").trim()
                            val home = File(filesDir, "home").apply { mkdirs() }
                            val file = File(home, ".github-token")
                            if (token.isEmpty()) file.delete()
                            else {
                                file.writeText(token)
                                file.setReadable(false, false); file.setWritable(false, false)
                                file.setReadable(true); file.setWritable(true)
                            }
                            appendRuntimeLog("github: token ${if (token.isEmpty()) "cleared" else "saved"}")
                            JSONObject().put("saved", true)
                        }
                        "sttSave" -> {
                            val prefs = getSharedPreferences("stt", Context.MODE_PRIVATE).edit()
                            val mode = data.optString("mode", "native")
                            prefs.putString("mode", mode)
                            val key = data.optString("key").trim()
                            if (key.isNotEmpty()) prefs.putString("key", key)
                            val model = data.optString("model").trim()
                            if (model.isNotEmpty()) prefs.putString("model", model)
                            prefs.putString("base", data.optString("base").trim())
                            prefs.apply()
                            appendRuntimeLog("voice: stt mode=$mode")
                            JSONObject().put("saved", true)
                        }
                        "sttGet" -> {
                            val p = getSharedPreferences("stt", Context.MODE_PRIVATE)
                            val k = p.getString("key", "") ?: ""
                            val mask = if (k.length > 10) k.take(6) + "…" + k.takeLast(4) else ""
                            JSONObject().put("mode", p.getString("mode", "native") ?: "native")
                                .put("model", p.getString("model", "") ?: "")
                                .put("base", p.getString("base", "") ?: "")
                                .put("hasKey", k.isNotEmpty())
                                .put("keyMask", mask)
                        }
                        "sttRecord" -> {
                            val on = data.optBoolean("on", true)
                            if (on) startSttRecording() else stopSttRecording()
                            JSONObject().put("recording", on)
                        }
                        "toast" -> {
                            val msg = data.optString("msg", "")
                            runOnUiThread { android.widget.Toast.makeText(this@MainActivity, msg, android.widget.Toast.LENGTH_LONG).show() }
                            JSONObject().put("shown", true)
                        }
                        "exposePort" -> {
                            val port = data.optInt("port", 0)
                            if (port > 0 && exposeSockets.none { it.localPort == port + 10_000 }) Thread {
                                try {
                                    val ss = java.net.ServerSocket()
                                    ss.reuseAddress = true
                                    // The sandbox already binds 127.0.0.1:<port>, so 0.0.0.0:<port> would
                                    // collide (EADDRINUSE). Listen outside on <port>+10000 and forward in.
                                    ss.bind(java.net.InetSocketAddress("0.0.0.0", port + 10_000))
                                    exposeSockets.add(ss)
                                    android.util.Log.i("expose", "forwarding 0.0.0.0:${port + 10_000} -> 127.0.0.1:$port")
                                    while (true) {
                                        val client = ss.accept()
                                        android.util.Log.i("expose", "accepted ${client.inetAddress?.hostAddress ?: '?'}:${client.port}")
                                        Thread {
                                            try {
                                                client.tcpNoDelay = true
                                                val upstream = java.net.Socket("127.0.0.1", port)
                                                android.util.Log.i("expose", "upstream connected")
                                                Thread {
                                                    try { upstream.getInputStream().copyTo(client.getOutputStream()) } catch (_: Throwable) {
                                                    }
                                                    try { client.close(); upstream.close() } catch (_: Throwable) {
                                                    }
                                                }.start()
                                                try { client.getInputStream().copyTo(upstream.getOutputStream()) } catch (e: Throwable) {
                                                    android.util.Log.i("expose", "c->u copy failed: $e")
                                                }
                                                try { client.close(); upstream.close() } catch (_: Throwable) {
                                                }
                                            } catch (e: Throwable) {
                                                android.util.Log.i("expose", "upstream connect failed: $e")
                                                try { client.close() } catch (_: Throwable) {
                                                }
                                            }
                                        }.start()
                                    }
                                } catch (e: Throwable) {
                                    android.util.Log.i("expose", "forwarder failed: $e")
                                }
                            }.start()
                            val ip = java.net.NetworkInterface.getNetworkInterfaces().asSequence()
                                .flatMap { nif -> nif.inetAddresses.asSequence() }
                                .firstOrNull { !it.isLoopbackAddress && it is java.net.Inet4Address && it.hostAddress?.contains(':') == false }
                                ?.hostAddress ?: ""
                            JSONObject().put("ip", ip).put("port", port + 10_000)
                        }
                        "logs" -> JSONObject().put("log", runtime.logSnapshot(400))
                        else -> if (request.getString("action").startsWith("history")) runtime.changeHistory.action(request) else sshTools.action(request)
                    }
                    nativeResult(id, result)
                } catch (e: Exception) { if (id.isNotEmpty()) nativeResult(id, JSONObject().put("error", if (action.startsWith("history")) "Git history request failed (${e.javaClass.simpleName})." else RemoteSsh.safeError(e))) }
            }
        }
    }

    private fun openFolderPicker() {
        val perms = arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.WRITE_EXTERNAL_STORAGE)
        val granted = perms.all { ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED }
        if (granted) {
            pickFolder.launch(null)
        } else {
            requestStorage.launch(perms)
        }
    }

    private fun onFolderPicked(uri: android.net.Uri) {
        Thread {
            val resolved = ProjectManager.resolveTreePath(this, uri)
            when {
                resolved != null && ProjectManager.canRead(resolved) && ProjectManager.canWrite(resolved) -> {
                    appendRuntimeLog("project folder: ${resolved.absolutePath} (rw)")
                    openProjectDir(resolved)
                }
                resolved != null -> {
                    appendRuntimeLog("no rw access to ${resolved.absolutePath} (read=${ProjectManager.canRead(resolved)})")
                    runOnUiThread { askAllFilesAccess(resolved, uri) }
                }
                else -> copyFromTree(uri)
            }
        }.start()
    }

    private fun copyFromTree(uri: android.net.Uri) {
        appendRuntimeLog("cannot resolve path, copying via SAF…")
        val target = ProjectManager.createNew(this, "imported")
        val ok = ProjectManager.copyTree(this, uri, target)
        appendRuntimeLog(if (ok) "copied into ${target.absolutePath}" else "SAF copy failed")
        if (ok) openProjectDir(target)
    }

    private fun askAllFilesAccess(dir: File, uri: android.net.Uri) {
        android.app.AlertDialog.Builder(this)
            .setTitle("Нет доступа к папке")
            .setMessage("Агенту нужен доступ на чтение и запись в ${dir.absolutePath}. Включи «Доступ ко всем файлам» для OpenCode и вернись в приложение.")
            .setPositiveButton("Открыть настройки") { _, _ ->
                pendingDir = dir
                val intent = Intent(
                    android.provider.Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                    Uri.parse("package:$packageName")
                )
                try {
                    startActivity(intent)
                } catch (_: Throwable) {
                    try {
                        startActivity(Intent(android.provider.Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
                    } catch (_: Throwable) {
                        Toast.makeText(this, "Открой настройки приложения → Разрешения → Файлы", Toast.LENGTH_LONG).show()
                    }
                }
            }
            .setNeutralButton("Скопировать в приложение") { _, _ -> Thread { copyFromTree(uri) }.start() }
            .setNegativeButton("Отмена", null)
            .show()
    }

    override fun onResume() {
        super.onResume()
        val dir = pendingDir ?: return
        pendingDir = null
        Thread {
            if (ProjectManager.canRead(dir) && ProjectManager.canWrite(dir)) {
                appendRuntimeLog("project folder: ${dir.absolutePath} (rw after settings)")
                openProjectDir(dir)
            } else {
                appendRuntimeLog("still no rw access to ${dir.absolutePath}")
                runOnUiThread { Toast.makeText(this, "Доступ не выдан", Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }

    // true: Codex-style UI from assets/codex.html; false: stock opencode web UI
    private val useCodexUi = true

    private fun uiUrl(port: Int, dir: File): String {
        val b64 = Base64.getUrlEncoder().withoutPadding()
            .encodeToString(dir.absolutePath.toByteArray(Charsets.UTF_8))
        return if (useCodexUi) "http://127.0.0.1:$port/_ui/index.html#d=$b64"
        else "http://127.0.0.1:$port/$b64/session"
    }

    private fun openProjectDir(dir: File) {
        ProjectManager.setCurrent(this, dir)
        val auth = runtime.authPort
        if (auth == 0) {
            restartServer()
            return
        }
        Thread {
            runtime.registerProject(dir.absolutePath)
            finishFolderRequest()
            runOnUiThread {
                webview.loadUrl(uiUrl(auth, dir))
                Toast.makeText(this, dir.absolutePath, Toast.LENGTH_SHORT).show()
            }
        }.start()
    }

    private fun restartServer() {
        runOnUiThread {
            Toast.makeText(this, "Перезапуск сервера…", Toast.LENGTH_SHORT).show()
            statusScreen.visibility = View.VISIBLE
            webview.visibility = View.GONE
            progressBar.visibility = View.VISIBLE
            runtime.restart(this)
        }
    }

    /** Cross-fade out of the splash: freeze the current page into a snapshot overlay,
     *  load the app underneath, then fade the snapshot away once the app has painted. */
    private fun transitionToWeb(url: String) {
        runOnUiThread {
            try {
                val snap = android.graphics.Bitmap.createBitmap(
                    webview.width.coerceAtLeast(1), webview.height.coerceAtLeast(1),
                    android.graphics.Bitmap.Config.ARGB_8888)
                webview.draw(android.graphics.Canvas(snap))
                val overlay = android.widget.ImageView(this).apply {
                    setImageBitmap(snap)
                    importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
                }
                rootLayout.addView(overlay, FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
                pendingCrossFade = {
                    overlay.animate().alpha(0f).setDuration(350)
                        .withEndAction { rootLayout.removeView(overlay); snap.recycle() }.start()
                }
            } catch (_: Throwable) {
            }
            webview.loadUrl(url)
            webview.visibility = View.VISIBLE
            statusScreen.visibility = View.GONE
            splashActive = false
        }
    }

    private fun showWeb(port: Int) {
        if (useCodexUi) {
            Thread {
                val dir = ProjectManager.currentDir(this)
                runtime.registerProject(dir.absolutePath)
                transitionToWeb(uiUrl(port, dir))
            }.start()
            return
        }
        transitionToWeb("http://127.0.0.1:$port/")
    }


    override fun onState(state: LocalRuntime.State, message: String) {
        runOnUiThread {
            if (splashActive) {
                webview.evaluateJavascript(
                    "window.__splashState&&window.__splashState(${JSONObject.quote(message)})", null)
                if (state == LocalRuntime.State.FAILED || state == LocalRuntime.State.STOPPED) {
                    webview.evaluateJavascript(
                        "window.__splashError&&window.__splashError(${JSONObject.quote(message)})", null)
                }
                return@runOnUiThread
            }
            when (state) {
                LocalRuntime.State.PREPARING, LocalRuntime.State.STARTING, LocalRuntime.State.READY -> {
                    statusText.text = message
                    progressBar.visibility = View.VISIBLE
                    retryButton.visibility = View.GONE
                }
                LocalRuntime.State.FAILED, LocalRuntime.State.STOPPED -> {
                    statusText.text = getString(R.string.failed, message)
                    progressBar.visibility = View.GONE
                    retryButton.visibility = View.VISIBLE
                    webview.visibility = View.GONE
                    statusScreen.visibility = View.VISIBLE
                }
            }
        }
    }

    override fun onLog(line: String) {
        runtime.appendLogPublic("[opencode] $line")
    }

    override fun onReady(port: Int, password: String) {
        showWeb(port)
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}
