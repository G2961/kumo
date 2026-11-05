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

    val changeHistory = ChangeHistory { ProjectManager.list(context) }

    @Volatile private var expectingExit = false

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
    internal val proxy = ConnectProxy { line -> appendLog("[net] $line") }
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
                // Non-fatal: bun powers the node/npm sandbox, its failure is logged
                // so the agent's tool errors have a visible cause in the server log.
                runProbe("", loaderCommand(binPaths.getValue("libbun.so"), "--version"))
                listener.onState(State.STARTING, "Starting the agent server…")
                port = freePort(14100)
                password = loadOrCreatePassword()
                authProxy = AuthProxy(port, "opencode", password) { line -> appendLog("[web] $line") }.apply { start(14101) }
                authPort = authProxy?.port ?: port
                // A cold start occasionally exits before becoming ready (port/FS/init races).
                // Retry a couple of times instead of failing the whole boot. expectingExit
                // stays true for the whole loop: intermediate deaths are logged, not shown.
                expectingExit = true
                var attempt = 0
                while (true) {
                    attempt++
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
                        expectingExit = false
                        listener.onState(State.READY, "Ready on 127.0.0.1:$port")
                        listener.onReady(authProxy?.port ?: port, password)
                        break
                    }
                    if (stopping) break
                    if (attempt >= 3) {
                        expectingExit = false
                        listener.onState(State.FAILED, "Server did not become ready")
                        break
                    }
                    appendLog("[retry] server not ready, restarting ($attempt/2)")
                    expectingExit = true
                    findDescendant(Process.myPid(), "libopencode")?.let { killTree(it) }
                    process = null
                    try { Thread.sleep(800) } catch (_: InterruptedException) {}
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
        "libldmusl.so", "libstdcxx.so", "libgcc.so", "libopencode.so", "librg.so", "libbun.so"
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
        // bun is musl-linked (PT_INTERP), so it must run through the musl loader
        // exactly like opencode; everything else rides on top of it.
        val bunCmd = loaderCommand(binPaths.getValue("libbun.so"))
            .joinToString(" ") { tok -> "'" + tok.replace("'", "'\\''") + "'" }
        shim(File(bin, "bun"), "exec $bunCmd \"\$@\"")
        shim(File(bin, "node"), "exec bun \"$@\"")
        val rootfs = extractRootfs()
        stageGitHelpers(rootfs)
        stageGitCredentialConfig()
        val npmCli = File(rootfs, "usr/lib/node_modules/npm/bin/npm-cli.js")
        if (npmCli.exists()) {
            shim(File(bin, "npm"), "exec bun '${npmCli.absolutePath.replace("'", "'\\''")}' \"$@\"")
            // npx semantics via the real npm CLI (bun x chokes on npm flags like -y)
            shim(File(bin, "npx"), "exec bun '" + File(rootfs, "usr/lib/node_modules/npm/bin/npx-cli.js").absolutePath.replace("'", "'\\''") + "' \"$@\"")
        }
        else appendLog("npm unavailable: npm-cli.js missing from rootfs")
        val git = File(bin, "git")
        val gitReal = File(rootfs, "usr/bin/git")
        if (gitReal.exists()) {
            val loader = binPaths.getValue("libldmusl.so")
            val libDir = loaderLibDir().absolutePath + ":" + File(rootfs, "usr/lib").absolutePath
            val q = { s: String -> "'" + s.replace("'", "'\\''") + "'" }
            shim(File(bin, "git"),
                "export GIT_EXEC_PATH=${q(File(rootfs, "usr/libexec/git-core").absolutePath)} " +
                "GIT_SSL_CAINFO=${q(File(rootfs, "etc/ssl/certs/ca-certificates.crt").absolutePath)} " +
                "GIT_TEMPLATE_DIR=${q(File(rootfs, "usr/share/git-core/templates").absolutePath)} " +
                "GIT_TERMINAL_PROMPT=0\n" +
                "exec ${q(loader)} --library-path ${q(libDir)} ${q(gitReal.absolutePath)} \"\$@\"")
        } else {
            git.writeText("#!/system/bin/sh\nexit 128\n")
            git.setExecutable(true, false)
            appendLog("git unavailable: usr/bin/git missing from rootfs")
        }
        val curlBin = File(rootfs, "usr/bin/curl")
        if (curlBin.exists()) {
            val loader = binPaths.getValue("libldmusl.so")
            val libDir = loaderLibDir().absolutePath + ":" + File(rootfs, "usr/lib").absolutePath
            val q = { s: String -> "'" + s.replace("'", "'\\''") + "'" }
            shim(File(bin, "curl"), "exec ${q(loader)} --library-path ${q(libDir)} ${q(curlBin.absolutePath)} \"\$@\"")
        }
        appendLog("bin dir: ${bin.absolutePath} (rg, sh, bash, opencode, bun, node, npm, npx, git)")
    }

    /** git-core helpers are musl ELF binaries; Android has no /lib interpreter, so each
     *  helper is wrapped into a loader-invoking script and the binary moves to *.bin. */
    private fun stageGitHelpers(rootfs: File) {
        val core = File(rootfs, "usr/libexec/git-core")
        if (!core.isDirectory) return
        val loader = binPaths.getValue("libldmusl.so")
        val libDir = loaderLibDir().absolutePath + ":" + File(rootfs, "usr/lib").absolutePath
        val q = { s: String -> "'" + s.replace("'", "'\\''") + "'" }
        val head = ByteArray(4)
        core.listFiles()?.forEach { f ->
            if (!f.isFile || f.name.endsWith(".bin") || f.name.endsWith(".sh")) return@forEach
            try {
                java.io.RandomAccessFile(f, "r").use { r ->
                    r.readFully(head)
                    if (!head.contentEquals(byteArrayOf(0x7F, 0x45, 0x4C, 0x46))) return@forEach
                }
                val real = File(f.parentFile, f.name + ".bin")
                if (!real.exists()) f.renameTo(real)
                f.writeText("#!/system/bin/sh\nexec ${q(loader)} --library-path ${q(libDir)} ${q(real.absolutePath)} \"\$@\"\n")
                f.setExecutable(true, false)
            } catch (e: Throwable) {
                appendLog("git-core wrap failed: ${f.name}: ${e.message}")
            }
        }
    }

    /** Local GitHub auth: a credential helper reads the PAT saved from Settings.
     *  The config file is written once; user edits survive restarts. */
    private fun stageGitCredentialConfig() {
        val bin = File(context.filesDir, "bin").apply { mkdirs() }
        val home = File(context.filesDir, "home").apply { mkdirs() }
        val helper = File(bin, "git-credential-codex")
        helper.writeText(
            "#!/system/bin/sh\n" +
            "if [ \"\$1\" = \"get\" ]; then\n" +
            " token=\$(cat ${home.absolutePath}/.github-token 2>/dev/null | tr -d '\\n')\n" +
            " if [ -n \"\$token\" ]; then echo 'username=x-access-token'; echo \"password=\$token\"; fi\n" +
            "fi\n"
        )
        helper.setExecutable(true, false)
        val config = File(home, ".gitconfig")
        if (!config.exists()) {
            config.writeText(
                "[credential]\n\thelper = ${helper.absolutePath}\n" +
                "[safe]\n\tdirectory = *\n" +
                "[init]\n\tdefaultBranch = main\n" +
                "[user]\n\tname = Codex Mobile\n\temail = agent@codex-mobile.invalid\n"
            )
        }
    }

    private fun shim(file: File, command: String) {
        file.writeText("#!/system/bin/sh\n$command\n")
        file.setExecutable(true, false)
    }

    /** Extracts assets/rootfs.tgz into filesDir/rootfs; re-extracts when the asset changes. */
    private fun extractRootfs(): File {
        val root = File(context.filesDir, "rootfs")
        try {
            val bytes = context.assets.open("rootfs.tgz").use { it.readBytes() }
            val hash = java.security.MessageDigest.getInstance("SHA-256").digest(bytes)
                .joinToString("") { "%02x".format(it) }
            val marker = File(context.filesDir, "rootfs.hash")
            if (marker.exists() && marker.readText() == hash && File(root, "usr").isDirectory) return root
            appendLog("extracting rootfs.tgz (${bytes.size} bytes)…")
            root.deleteRecursively()
            root.mkdirs()
            TarGz.extract(bytes, root)
            marker.writeText(hash)
            appendLog("rootfs ready: ${root.absolutePath}")
        } catch (e: Throwable) {
            appendLog("rootfs extraction failed: ${e.message}")
        }
        return root
    }

    /** Minimal ustar (with GNU longname fallback) reader for the npm rootfs archive. */
    private object TarGz {
        fun extract(bytes: ByteArray, dest: File) {
            java.util.zip.GZIPInputStream(bytes.inputStream()).use { gz ->
                val header = ByteArray(512)
                var longName: String? = null
                while (true) {
                    var read = 0
                    while (read < 512) {
                        val r = gz.read(header, read, 512 - read)
                        if (r < 0) return
                        read += r
                    }
                    if (header.all { it == 0.toByte() }) continue
                    val name = String(header, 0, 100).trimEnd('\u0000', ' ')
                    val prefix = String(header, 345, 155).trimEnd('\u0000', ' ')
                    val size = String(header, 124, 12).trimEnd('\u0000', ' ').toLongOrNull(8) ?: 0L
                    val type = header[156].toInt().toChar()
                    val data = if (size > 0) ByteArray(size.toInt()).also { buf ->
                        var off = 0
                        while (off < size) {
                            val r = gz.read(buf, off, (size - off).toInt())
                            if (r < 0) break
                            off += r
                        }
                    } else ByteArray(0)
                    // Tar blocks are 512-aligned: skip the zero padding after the data,
                    // or the next header parse desyncs into file contents.
                    var pad = (512 - (size % 512)) % 512
                    while (pad > 0) {
                        val r = gz.skip(pad)
                        if (r <= 0) break
                        pad -= r
                    }
                    when (type) {
                        'L' -> longName = data.toString(StandardCharsets.UTF_8).trimEnd('\u0000')
                        '0', '\u0000', '5' -> {
                            val path = longName ?: buildString {
                                append(name); if (prefix.isNotEmpty()) insert(0, "$prefix/")
                            }
                            longName = null
                            if (path.isBlank() || path.contains("..")) continue
                            val f = File(dest, path)
                            if (type == '5') {
                                f.mkdirs()
                            } else {
                                f.parentFile?.mkdirs()
                                f.writeBytes(data)
                                f.setReadable(true, false)
                                f.setWritable(true, true)
                            }
                        }
                        else -> longName = null
                    }
                }
            }
        }
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

    internal fun applyEnv(pb: ProcessBuilder, withAuth: Boolean) {
        val env = pb.environment()
        val home = File(context.filesDir, "home").apply { mkdirs() }.absolutePath
        val tmp = File(context.cacheDir, "tmp").apply { mkdirs() }.absolutePath
        val bin = File(context.filesDir, "bin").apply { mkdirs() }.absolutePath
        env["HOME"] = home
        env["TMPDIR"] = tmp
        env["PATH"] = "$bin:/system/bin:/system/xbin"
        env["LANG"] = "C.UTF-8"
        // Sandbox TLS: curl is built against /etc/ssl/cert.pem, git uses GIT_SSL_CAINFO;
        // SSL_CERT_FILE covers every other openssl-based tool in the sandbox.
        val ca = File(context.filesDir, "rootfs/etc/ssl/certs/ca-certificates.crt")
        if (ca.exists()) {
            env["SSL_CERT_FILE"] = ca.absolutePath
            env["CURL_CA_BUNDLE"] = ca.absolutePath
        }
        // The device runs near-zero free RAM and a full swap; an unbounded JSC heap
        // makes the server page-fault through swap and freeze the whole UI. Cap it.
        env["BUN_JSC_forceRAMSize"] = "201326592" // 192MB
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
            appendLog("[process exited] code=${runCatching { p.exitValue() }.getOrElse { -1 }}")
            if (!stopping && !expectingExit) {
                listener.onState(State.FAILED, "opencode process exited")
            }
        }, "runtime-log").start()
    }

    private fun awaitReady(): Boolean {
        val auth = "Basic " + Base64.getEncoder().encodeToString("opencode:$password".toByteArray(StandardCharsets.UTF_8))
        // 2 minutes: under swap/IO pressure the server boots well past a minute
        // and killing a healthy-but-slow process only restarts the same grind.
        repeat(240) {
            if (stopping) return false
            // Dead process means the next retry must start immediately, not after a full wait window.
            if (process?.isAlive == false) return false
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
            ProjectManager.remember(context, File(directory))
            changeHistory.register(directory)
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
