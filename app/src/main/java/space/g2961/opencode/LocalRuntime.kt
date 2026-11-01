package space.g2961.opencode

import android.content.Context
import android.os.Process
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.URL
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Paths
import java.util.Base64
import java.util.concurrent.TimeUnit

class LocalRuntime(private val context: Context, private val sshTools: SshToolsServer) {

    enum class State { PREPARING, STARTING, READY, STOPPED, FAILED }

    interface Listener {
        fun onState(state: State, message: String)
        fun onLog(line: String)
        fun onReady(port: Int, password: String)
    }

    @Volatile var port: Int = 0
        private set
    @Volatile var authPort: Int = 0
        private set
    @Volatile var password: String = ""
        private set

    private var process: java.lang.Process? = null
    private val proxy = ConnectProxy { line -> appendLog("[net] $line") }
    private var authProxy: AuthProxy? = null
    @Volatile private var stopping = false
    @Volatile private lateinit var binPaths: Map<String, String>

    private val logs = ArrayDeque<String>()
    private val logLock = Any()

    @Volatile var logHook: ((String) -> Unit)? = null

    fun logSnapshot(limit: Int = 200): String = synchronized(logLock) { logs.takeLast(limit).joinToString("\n") }

    fun appendLogPublic(line: String) = appendLog(line)

    private fun appendLog(line: String) {
        synchronized(logLock) {
            logs.addLast(line)
            while (logs.size > 500) logs.removeFirst()
        }
        try {
            logHook?.invoke(line)
        } catch (_: Throwable) {
        }
    }

    fun start(listener: Listener) {
        stopping = false
        Thread({
            try {
                listener.onState(State.PREPARING, "Preparing runtime…")
                binPaths = resolveRuntimeBinaries()
                logDiagnostics()
                stageBinDir()
                proxy.start()
                listener.onState(State.STARTING, "Self-test…")
                if (!runProbe("1.19.3", loaderCommand(binPaths.getValue("libopencode.so"), "--version"))) {
                    throw IOException("opencode probe failed")
                }
                listener.onState(State.STARTING, "Starting opencode server…")
                port = freePort(14100)
                password = loadOrCreatePassword()
                authProxy = AuthProxy(port, "opencode", password) { line -> appendLog("[web] $line") }.apply { start(14101) }
                authPort = authProxy?.port ?: port
                val cmd = buildCommand()
                appendLog("$ ${cmd.joinToString(" ")}")
                val pb = ProcessBuilder(cmd)
                pb.redirectErrorStream(true)
                applyEnv(pb, true)
                pb.directory(ProjectManager.currentDir(context))
                process = pb.start()
                pumpOutput(process!!, listener)
                val ready = awaitReady()
                if (ready && !stopping && process?.isAlive == true) {
                    listener.onState(State.READY, "Ready on 127.0.0.1:$port")
                    listener.onReady(authProxy?.port ?: port, password)
                } else if (!stopping) {
                    listener.onState(State.FAILED, "Server did not become ready")
                }
            } catch (e: Exception) {
                appendLog("runtime error: ${e.message}")
                if (!stopping) listener.onState(State.FAILED, e.message ?: e.javaClass.simpleName)
            }
        }, "local-runtime").start()
    }

    fun restart(listener: Listener) {
        stop()
        Thread {
            try {
                process?.waitFor()
            } catch (_: Throwable) {
            }
            try {
                Thread.sleep(300)
            } catch (_: InterruptedException) {
            }
            start(listener)
        }.start()
    }

    fun stop() {
        stopping = true
        proxy.stop()
        authProxy?.stop()
        val p = process ?: return
        process = null
        Thread {
            val rootPid = findDescendant(Process.myPid(), "libopencode") ?: 0
            try {
                if (rootPid > 0) killTree(rootPid)
            } catch (_: Throwable) {
            }
            try {
                p.destroy()
                p.waitFor()
            } catch (_: Throwable) {
            }
        }.start()
    }

    private fun nativeLibraryDir(): String = context.applicationInfo.nativeLibraryDir

    private val libNames = listOf(
        "libldmusl.so", "libstdcxx.so", "libgcc.so", "libopencode.so", "librg.so"
    )

    private fun resolveRuntimeBinaries(): Map<String, String> {
        val nl = nativeLibraryDir()
        if (libNames.all { File(nl, it).takeIf { f -> f.exists() }?.canExecute() == true }) {
            appendLog("exec mode: nativeLibraryDir")
            return libNames.associateWith { File(nl, it).absolutePath }
        }
        val bin = File(context.filesDir, "runtime").apply { mkdirs() }
        val map = mutableMapOf<String, String>()
        libNames.forEach { name ->
            val src = File(nl, name)
            val dst = File(bin, name)
            if (!dst.exists() || dst.length() != src.length()) {
                appendLog("copying $name to ${bin.absolutePath}")
                src.copyTo(dst, overwrite = true)
            }
            dst.setReadable(true, false)
            dst.setWritable(true, true)
            dst.setExecutable(true, false)
            map[name] = dst.absolutePath
        }
        appendLog("exec mode: copied binaries in ${bin.absolutePath}")
        return map
    }

    private fun stageBinDir() {
        val bin = File(context.filesDir, "bin").apply { mkdirs() }
        symlink(File(bin, "rg"), binPaths.getValue("librg.so"))
        symlink(File(bin, "sh"), "/system/bin/sh")
        symlink(File(bin, "bash"), "/system/bin/sh")
        symlink(File(bin, "opencode"), binPaths.getValue("libopencode.so"))
        val git = File(bin, "git")
        if (!git.exists()) {
            git.writeText("#!/system/bin/sh\nexit 128\n")
            git.setExecutable(true, false)
        }
        appendLog("bin dir: ${bin.absolutePath} (rg, sh, bash, opencode, git)")
    }

    private fun loaderLibDir(): File {
        val dir = File(context.filesDir, "runtime/lib").apply { mkdirs() }
        pairSymlinkOrCopy(File(dir, "libstdc++.so.6"), binPaths.getValue("libstdcxx.so"))
        pairSymlinkOrCopy(File(dir, "libgcc_s.so.1"), binPaths.getValue("libgcc.so"))
        return dir
    }

    private fun pairSymlinkOrCopy(link: File, targetPath: String) {
        link.delete()
        try {
            Files.createSymbolicLink(link.toPath(), Paths.get(targetPath))
        } catch (_: IOException) {
            try {
                val src = File(targetPath)
                if (!link.exists() || link.length() != src.length()) src.copyTo(link, overwrite = true)
            } catch (_: Throwable) {
            }
        }
    }

    private fun loaderCommand(vararg program: String): List<String> {
        return listOf(binPaths.getValue("libldmusl.so"), "--library-path", loaderLibDir().absolutePath) + program.toList()
    }

    private fun symlink(link: File, target: String) {
        link.delete()
        try {
            Files.createSymbolicLink(link.toPath(), Paths.get(target))
        } catch (e: IOException) {
            appendLog("symlink failed: ${link.name}: ${e.message}")
            link.writeText("#!/system/bin/sh\nexec \"$target\" \"$@\"\n")
            link.setExecutable(true, false)
        }
    }

    private fun runProbe(expect: String, cmd: List<String>): Boolean {
        return try {
            val pb = ProcessBuilder(cmd)
            pb.redirectErrorStream(true)
            applyEnv(pb, false)
            val p = pb.start()
            val out = StringBuilder()
            val pump = Thread {
                p.inputStream.bufferedReader(StandardCharsets.UTF_8).forEachLine { line ->
                    appendLog("[probe] $line")
                    out.appendLine(line)
                }
            }
            pump.isDaemon = true
            pump.start()
            val done = p.waitFor(60, TimeUnit.SECONDS)
            pump.join(2000)
            val exit = if (done) p.exitValue() else -999
            if (!done) p.destroy()
            val ok = expect in out.toString()
            appendLog("[probe-result] expect='$expect' exit=$exit ok=$ok")
            ok
        } catch (e: Exception) {
            appendLog("[probe] error: ${e.message}")
            false
        }
    }

    private fun logDiagnostics() {
        val nl = nativeLibraryDir()
        appendLog("nativeLibraryDir=$nl targetSdk=${context.applicationInfo.targetSdkVersion}")
        File(nl).listFiles()?.sortedBy { it.name }?.forEach {
            appendLog("  ${it.name} exec=${it.canExecute()} size=${it.length()}")
        }
    }

    private fun applyEnv(pb: ProcessBuilder, withAuth: Boolean) {
        val env = pb.environment()
        val home = File(context.filesDir, "home").apply { mkdirs() }.absolutePath
        val tmp = File(context.cacheDir, "tmp").apply { mkdirs() }.absolutePath
        val bin = File(context.filesDir, "bin").apply { mkdirs() }.absolutePath
        env["HOME"] = home
        env["TMPDIR"] = tmp
        env["PATH"] = "$bin:/system/bin:/system/xbin"
        env["LANG"] = "C.UTF-8"
        if (withAuth) {
            env["OPENCODE_SERVER_PASSWORD"] = password
            env["HTTPS_PROXY"] = "http://127.0.0.1:${proxy.port}"
            env["HTTP_PROXY"] = "http://127.0.0.1:${proxy.port}"
            env["NO_PROXY"] = "localhost,127.0.0.1,::1"
            env["OPENCODE_ENABLE_EXA"] = "true"
            env["OPENCODE_CONFIG_CONTENT"] = org.json.JSONObject()
                .put("shell", "/system/bin/sh")
                .put("mcp", org.json.JSONObject().put("mobile_ssh", org.json.JSONObject()
                    .put("type", "remote").put("url", sshTools.url()).put("oauth", false).put("timeout", 900000)
                    .put("headers", org.json.JSONObject().put("Authorization", "Bearer " + sshTools.token()))))
                .put("permission", org.json.JSONObject().put("mobile_ssh_ssh_exec", "ask").put("mobile_ssh_ssh_write_file", "ask"))
                .toString()
        }
    }

    private fun buildCommand(): List<String> {
        return loaderCommand(binPaths.getValue("libopencode.so"), "serve", "--hostname", "127.0.0.1", "--port", port.toString())
    }

    private fun pumpOutput(p: java.lang.Process, listener: Listener) {
        Thread({
            try {
                p.inputStream.bufferedReader(StandardCharsets.UTF_8).forEachLine { line ->
                    appendLog(line)
                    listener.onLog(line)
                }
            } catch (_: Throwable) {
            }
            if (!stopping) {
                appendLog("[process exited]")
                listener.onState(State.FAILED, "opencode process exited")
            }
        }, "runtime-log").start()
    }

    private fun awaitReady(): Boolean {
        val auth = "Basic " + Base64.getEncoder().encodeToString("opencode:$password".toByteArray(StandardCharsets.UTF_8))
        repeat(120) {
            if (stopping) return false
            try {
                val conn = URL("http://127.0.0.1:$port/").openConnection() as HttpURLConnection
                conn.connectTimeout = 1500
                conn.readTimeout = 1500
                conn.setRequestProperty("Authorization", auth)
                val code = conn.responseCode
                conn.disconnect()
                if (code in 200..299) return true
            } catch (_: IOException) {
            }
            try {
                Thread.sleep(500)
            } catch (_: InterruptedException) {
                return false
            }
        }
        return false
    }

    private fun findDescendant(parentPid: Int, marker: String): Int? {
        try {
            File("/proc").listFiles()?.forEach { f ->
                if (!f.name.all { it.isDigit() }) return@forEach
                try {
                    val stat = File(f, "stat").readText()
                    val close = stat.lastIndexOf(')')
                    val rest = stat.substring(close + 2).split(" ")
                    if (rest[1].toInt() != parentPid) return@forEach
                    val cmdline = File(f, "cmdline").readBytes().toString(StandardCharsets.UTF_8)
                    if (marker in cmdline) return f.name.toInt()
                } catch (_: Throwable) {
                }
            }
        } catch (_: Throwable) {
        }
        return null
    }

    private fun killTree(rootPid: Int) {
        val pids = HashMap<Int, Int>()
        try {
            File("/proc").listFiles()?.forEach { f ->
                if (!f.name.all { it.isDigit() }) return@forEach
                try {
                    val stat = File(f, "stat").readText()
                    val close = stat.lastIndexOf(')')
                    val rest = stat.substring(close + 2).split(" ")
                    pids[f.name.toInt()] = rest[1].toInt()
                } catch (_: Throwable) {
                }
            }
        } catch (_: Throwable) {
        }
        val toKill = mutableSetOf<Int>()
        var frontier = listOf(rootPid)
        while (frontier.isNotEmpty()) {
            toKill.addAll(frontier)
            frontier = pids.filter { it.value in toKill && it.key !in toKill }.keys.toList()
        }
        for (pid in toKill.sortedDescending()) {
            try {
                Process.killProcess(pid)
            } catch (_: Throwable) {
            }
        }
    }

    private fun loadOrCreatePassword(): String {
        val prefs = context.getSharedPreferences("runtime", Context.MODE_PRIVATE)
        var pw = prefs.getString("password", null)
        if (pw == null) {
            val bytes = ByteArray(18)
            java.security.SecureRandom().nextBytes(bytes)
            pw = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
            prefs.edit().putString("password", pw).apply()
        }
        return pw
    }

    private fun freePort(preferred: Int = 0): Int {
        if (preferred > 0) {
            for (p in preferred until preferred + 20) {
                try {
                    ServerSocket().use { it.bind(InetSocketAddress("127.0.0.1", p)) }
                    return p
                } catch (_: Throwable) {
                }
            }
        }
        ServerSocket().use { ss ->
            ss.bind(InetSocketAddress("127.0.0.1", 0))
            return ss.localPort
        }
    }

    fun registerProject(directory: String): Boolean {
        val p = port
        val pw = password
        if (p == 0) return false
        return try {
            val enc = java.net.URLEncoder.encode(directory, "UTF-8")
            val conn = URL("http://127.0.0.1:$p/project/current?directory=$enc")
                .openConnection() as HttpURLConnection
            conn.setRequestProperty(
                "Authorization",
                "Basic " + Base64.getEncoder().encodeToString("opencode:$pw".toByteArray(StandardCharsets.UTF_8))
            )
            conn.connectTimeout = 3000
            conn.readTimeout = 3000
            val code = conn.responseCode
            conn.disconnect()
            appendLog("[project] register $directory -> $code")
            code in 200..299
        } catch (e: Throwable) {
            appendLog("[project] register failed: ${e.message}")
            false
        }
    }
}
