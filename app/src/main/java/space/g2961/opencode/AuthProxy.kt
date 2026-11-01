package space.g2961.opencode

import java.io.InputStream
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URI
import java.util.Base64
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

class AuthProxy(
    private val targetPort: Int,
    user: String,
    private val password: String,
    private val log: (String) -> Unit = {}
) {

    private val basic = "Basic " + Base64.getEncoder()
        .encodeToString("$user:$password".toByteArray(Charsets.UTF_8))

    @Volatile var port: Int = 0
        private set

    private var server: ServerSocket? = null
    private val open = ConcurrentHashMap.newKeySet<Socket>()
    private val pool = Executors.newCachedThreadPool { r -> Thread(r).apply { isDaemon = true } }

    fun start(preferredPort: Int = 0) {
        val ss = ServerSocket()
        if (preferredPort > 0) {
            for (p in preferredPort until preferredPort + 20) {
                try {
                    ss.bind(InetSocketAddress("127.0.0.1", p))
                    break
                } catch (_: Throwable) {
                }
            }
        }
        if (!ss.isBound) ss.bind(InetSocketAddress("127.0.0.1", 0))
        server = ss
        port = ss.localPort
        pool.execute {
            try {
                while (!ss.isClosed) {
                    val client = ss.accept()
                    open.add(client)
                    pool.execute { handle(client) }
                }
            } catch (_: Throwable) {
            }
        }
    }

    fun stop() {
        try {
            server?.close()
        } catch (_: Throwable) {
        }
        open.forEach {
            try {
                it.close()
            } catch (_: Throwable) {
            }
        }
        open.clear()
    }

    private fun handle(client: Socket) {
        var upgrade = false
        try {
            client.tcpNoDelay = true
            val head = readHead(client.getInputStream()) ?: return
            val (requestLine, headers, _) = head
            log(requestLine)
            val parts = requestLine.split(" ")
            if (parts.size < 2) return
            val method = parts[0].uppercase()
            val rawTarget = parts[1]
            upgrade = headers.entries.any { it.key.equals("upgrade", true) && it.value.isNotBlank() }

            val contentLength = headers.entries
                .firstOrNull { it.key.equals("content-length", true) }?.value?.toLongOrNull() ?: 0
            val body = if (contentLength > 0) {
                val b = ByteArray(contentLength.coerceAtMost(64L * 1024 * 1024).toInt())
                var off = 0
                val input = client.getInputStream()
                while (off < b.size) {
                    val n = input.read(b, off, b.size - off)
                    if (n < 0) break
                    off += n
                }
                b
            } else ByteArray(0)

            if (upgrade) {
                tunnelWithAuth(client, requestLine, headers, body)
                return
            }

            val target = toTarget(rawTarget)
            val conn = java.net.URL("http://127.0.0.1:$targetPort$target")
                .openConnection() as HttpURLConnection
            conn.requestMethod = if (method == "HEAD") "HEAD" else method
            conn.connectTimeout = 10000
            conn.readTimeout = 0
            conn.instanceFollowRedirects = false
            headers.forEach { (k, v) ->
                val lower = k.lowercase()
                if (lower in setOf("authorization", "connection", "proxy-connection", "expect", "host", "content-length", "transfer-encoding", "keep-alive")) return@forEach
                try {
                    conn.setRequestProperty(k, v)
                } catch (_: Throwable) {
                }
            }
            conn.setRequestProperty("Authorization", basic)
            try {
                conn.setRequestProperty("Connection", "close")
            } catch (_: Throwable) {
            }
            if (body.isNotEmpty() && method in setOf("POST", "PUT", "PATCH")) {
                conn.doOutput = true
                conn.setFixedLengthStreamingMode(body.size)
                conn.outputStream.write(body)
                conn.outputStream.flush()
            }
            val code = conn.responseCode
            val message = conn.responseMessage ?: ""
            val resp = conn
            val out = client.getOutputStream()
            val sb = StringBuilder("HTTP/1.1 $code $message\r\n")
            val skip = setOf("transfer-encoding", "connection", "keep-alive", "proxy-connection")
            var hasLength = false
            for ((k, v) in resp.headerFields) {
                if (k == null) continue
                if (k.lowercase() in skip) continue
                if (k.equals("Content-Length", true)) hasLength = true
                for (value in v) sb.append("$k: $value\r\n")
            }
            if (!hasLength) sb.append("Connection: close\r\n")
            sb.append("\r\n")
            out.write(sb.toString().toByteArray(Charsets.ISO_8859_1))
            out.flush()
            val stream: InputStream? = try {
                if (code in 200..399) resp.inputStream else resp.errorStream
            } catch (_: Throwable) {
                null
            }
            var total = 0
            if (stream != null) {
                val buf = ByteArray(65536)
                while (true) {
                    val n = stream.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    out.flush()
                    total += n
                }
            }
            log("  -> $code ($total bytes)")
        } catch (e: Throwable) {
            log("  -> error: ${e.message}")
        } finally {
            try {
                client.close()
            } catch (_: Throwable) {
            }
            if (!upgrade) {
                // HttpURLConnection keeps its own pool; nothing to close explicitly per-call here
            }
        }
    }

    private fun toTarget(rawTarget: String): String {
        if (rawTarget.startsWith("/")) return rawTarget
        return try {
            val u = URI(rawTarget)
            (u.rawPath ?: "/") + (u.rawQuery?.let { "?$it" } ?: "")
        } catch (_: Exception) {
            "/"
        }
    }

    private fun tunnelWithAuth(client: Socket, requestLine: String, headers: Map<String, String>, body: ByteArray) {
        val upstream = try {
            Socket().apply {
                connect(InetSocketAddress("127.0.0.1", targetPort), 10000)
                tcpNoDelay = true
            }
        } catch (e: Throwable) {
            log("  -> tunnel upstream failed: ${e.message}")
            return
        }
        open.add(upstream)
        try {
            val sb = StringBuilder("$requestLine\r\n")
            for ((k, v) in headers) {
                val lower = k.lowercase()
                if (lower in setOf("authorization", "proxy-authorization")) continue
                sb.append("$k: $v\r\n")
            }
            sb.append("Authorization: ").append(basic).append("\r\n")
            if (body.isNotEmpty()) sb.append("Content-Length: ").append(body.size).append("\r\n")
            sb.append("\r\n")
            val out = upstream.getOutputStream()
            out.write(sb.toString().toByteArray(Charsets.ISO_8859_1))
            if (body.isNotEmpty()) out.write(body)
            out.flush()
            open.add(client)
            pool.execute { pipe(client, upstream) }
            pipe(upstream, client)
        } catch (_: Throwable) {
        } finally {
            closeQuietly(client)
            closeQuietly(upstream)
            open.remove(upstream)
        }
    }

    private fun readHead(input: InputStream): Triple<String, Map<String, String>, ByteArray>? {
        val ba = java.io.ByteArrayOutputStream()
        var tail = 0
        while (true) {
            val b = input.read()
            if (b < 0) return null
            ba.write(b)
            tail = ((tail shl 8) or b)
            if (ba.size() > 128 * 1024) return null
            if (tail == 0x0D0A0D0A) break
        }
        val raw = ba.toByteArray()
        val text = String(raw, Charsets.ISO_8859_1)
        val lines = text.split("\r\n")
        val requestLine = lines.firstOrNull() ?: return null
        val headers = mutableMapOf<String, String>()
        lines.drop(1).forEach { line ->
            val i = line.indexOf(':')
            if (i > 0) headers[line.substring(0, i).trim()] = line.substring(i + 1).trim()
        }
        return Triple(requestLine, headers, raw)
    }

    private fun pipe(a: Socket, b: Socket) {
        val buf = ByteArray(65536)
        try {
            val input = a.getInputStream()
            val output = b.getOutputStream()
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                output.write(buf, 0, n)
                output.flush()
            }
        } catch (_: Throwable) {
        } finally {
            closeQuietly(b)
        }
    }

    private fun closeQuietly(s: Socket?) {
        try {
            s?.close()
        } catch (_: Throwable) {
        }
    }
}
