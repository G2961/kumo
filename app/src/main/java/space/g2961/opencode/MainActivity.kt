package space.g2961.opencode

import android.Manifest
import android.annotation.SuppressLint
import androidx.activity.ComponentActivity
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        sshTools = SshToolsServer(SshConnectionStore(this)) { event ->
            runOnUiThread { if (trustedUi) webview.evaluateJavascript("window.dispatchEvent(new CustomEvent('codex-remote-event',{detail:$event}));", null) }
        }
        sshTools.start()
        runtime = LocalRuntime(this, sshTools)
        buildUi()
        startRuntime()
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webview.visibility == View.VISIBLE && webview.canGoBack()) webview.goBack() else finish()
    }

    override fun onDestroy() {
        runtime.stop()
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
            setBackgroundColor(Color.parseColor("#090A0C"))
        }
        configureWebview(webview)
        root.addView(webview, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        setContentView(root)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebview(wv: WebView) {
        wv.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = true
            cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
        }
        wv.addJavascriptInterface(MobileBridge(), "CodexNative")
        wv.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                val u = url?.let { Uri.parse(it) }
                trustedUi = u != null && u.host == "127.0.0.1" && u.port == runtime.authPort && u.path == "/_ui/index.html"
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
                if (u.host == "127.0.0.1" && u.path == "/_ui/index.html") {
                    return try {
                        WebResourceResponse(
                            "text/html", "utf-8", 200, "OK",
                            mapOf("Cache-Control" to "no-store"),
                            assets.open("codex.html")
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
        @JavascriptInterface fun request(raw: String) {
            if (!trustedUi || raw.length > 2 * 1024 * 1024) return
            Thread {
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
                            folderRequest = id; runOnUiThread { openFolderPicker() }; return@Thread
                        }
                        "appearance" -> {
                            runOnUiThread {
                                val dark = data.optString("theme") == "dark"
                                val color = if (dark) Color.BLACK else Color.WHITE
                                webview.setBackgroundColor(color); window.statusBarColor = color; window.navigationBarColor = color
                                window.decorView.systemUiVisibility = if (dark) 0 else View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR or View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
                            }
                            JSONObject().put("saved", true)
                        }
                        else -> if (request.getString("action").startsWith("history")) runtime.changeHistory.action(request) else sshTools.action(request)
                    }
                    nativeResult(id, result)
                } catch (e: Exception) { if (id.isNotEmpty()) nativeResult(id, JSONObject().put("error", if (action.startsWith("history")) "Git history request failed (${e.javaClass.simpleName})." else RemoteSsh.safeError(e))) }
            }.start()
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

    override fun onLog(line: String) {}

    override fun onReady(port: Int, password: String) {
        showWeb(port)
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}
