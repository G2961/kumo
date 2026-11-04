package space.g2961.opencode

import android.Manifest
import android.annotation.SuppressLint
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
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

    private val requestAudio = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        appendRuntimeLog(if (granted) "voice: mic granted" else "voice: mic denied")
        if (granted) startVoiceRecognition()
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
        buildUi()
        startRuntime()
        // Foreground keep-alive: agent streaming + bundled server survive backgrounding.
        startForegroundService(Intent(this, KeepAliveService::class.java))
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (trustedUi && webview.visibility == View.VISIBLE) {
            webview.evaluateJavascript(
                "(() => { if (typeof isWorkSheetOpen === 'function' && isWorkSheetOpen()) { if (typeof requestCloseWorkSheet === 'function') requestCloseWorkSheet(); else closeWorkSheet(); return true; } return false; })()"
            ) { handled ->
                if (handled != "true") navigateBack()
            }
        } else navigateBack()
    }

    private fun navigateBack() {
        if (webview.visibility == View.VISIBLE && webview.canGoBack()) webview.goBack() else finish()
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
        runOnUiThread {
            statusScreen.visibility = View.VISIBLE
            webview.visibility = View.GONE
            progressBar.visibility = View.VISIBLE
            retryButton.visibility = View.GONE
            statusText.text = getString(R.string.starting)
        }
        runtime.start(this)
    }

    private fun buildUi() {
        val root = FrameLayout(this)
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
            visibility = View.GONE
            overScrollMode = View.OVER_SCROLL_NEVER
            setBackgroundColor(Color.parseColor("#090A0C"))
        }
        configureWebview(webview)
        root.addView(webview, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

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

    /** Voice input: Android speech recognition → window.__voiceResult(text) in the page. */
    fun startVoiceInput() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) startVoiceRecognition()
        else requestAudio.launch(Manifest.permission.RECORD_AUDIO)
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
        wv.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                val u = url?.let { Uri.parse(it) }
                trustedUi = u != null && u.host == "127.0.0.1" && u.port == runtime.authPort && u.path == "/_ui/index.html"
            }
            override fun onPageFinished(view: WebView?, url: String?) {
                // The insets listener can fire before the page exists; re-apply its vars now.
                lastInsetJs?.let { js -> if (trustedUi) view?.evaluateJavascript(js, null) }
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
                        "appearance" -> {
                            runOnUiThread {
                                val dark = data.optString("theme") == "dark"
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

    private fun showWeb(port: Int) {
        if (useCodexUi) {
            Thread {
                val dir = ProjectManager.currentDir(this)
                runtime.registerProject(dir.absolutePath)
                runOnUiThread {
                    webview.loadUrl(uiUrl(port, dir))
                    webview.visibility = View.VISIBLE
                    statusScreen.visibility = View.GONE
                }
            }.start()
            return
        }
        runOnUiThread {
            webview.loadUrl("http://127.0.0.1:$port/")
            webview.visibility = View.VISIBLE
            statusScreen.visibility = View.GONE
        }
    }


    override fun onState(state: LocalRuntime.State, message: String) {
        runOnUiThread {
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
