package space.g2961.opencode

import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.ServerSocket
import java.net.Socket
import java.net.URI
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

class ConnectProxy {

    @Volatile var port: Int = 0
        private set

    private var server: ServerSocket? = null
    private val open = ConcurrentHashMap.newKeySet<Socket>()
    private val pool: ExecutorService = Executors.newCachedThreadPool { r ->
        Thread(r).apply { isDaemon = true }
    }

    fun start() {
        val ss = ServerSocket()
        ss.bind(InetSocketAddress("127.0.0.1", 0))
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
        client.use { c ->
            c.tcpNoDelay = true
            val input = c.getInputStream()
            val head = readHead(input) ?: return
            val requestLine = head.first
            val headers = head.second
            val parts = requestLine.split(" ")
            if (parts.size < 2) return
            val target = parts[1]
            if (requestLine.startsWith("CONNECT")) {
                val hostport = target.substringAfter("//")
                val idx = hostport.lastIndexOf(':')
                if (idx <= 0) return
                val host = hostport.substring(0, idx).trim('[', ']')
                val port = hostport.substring(idx + 1).toIntOrNull() ?: 443
                tunnel(c, host, port)
            } else {
                try {
                    c.getOutputStream().write("HTTP/1.1 502 Bad Gateway\r\n\r\n".toByteArray())
                    c.getOutputStream().flush()
                } catch (_: Throwable) {
                }
            }
        }
    }

    private fun readHead(input: InputStream): Pair<String, Map<String, String>>? {
        val buf = StringBuilder()
        val ba = java.io.ByteArrayOutputStream()
        val tmp = ByteArray(1)
        while (true) {
            val n = input.read(tmp)
            if (n < 0) return null
            ba.write(tmp, 0, n)
            buf.append(tmp[0].toInt().toChar())
            if (buf.endsWith("\r\n\r\n")) break
            if (ba.size() > 65536) return null
        }
        val text = ba.toString("ISO_8859_1")
        val lines = text.split("\r\n")
        val requestLine = lines.firstOrNull() ?: return null
        val headers = mutableMapOf<String, String>()
        lines.drop(1).forEach { line ->
            val i = line.indexOf(':')
            if (i > 0) headers[line.substring(0, i).trim().lowercase()] = line.substring(i + 1).trim()
        }
        return requestLine to headers
    }

    private fun tunnel(client: Socket, host: String, port: Int) {
        val upstream = try {
            val addr = InetAddress.getAllByName(host).firstOrNull() ?: throw IOException("dns fail $host")
            Socket().apply {
                connect(InetSocketAddress(addr, port), 15000)
                tcpNoDelay = true
            }
        } catch (e: Exception) {
            try {
                client.getOutputStream().write("HTTP/1.1 502 Bad Gateway\r\n\r\n".toByteArray())
                client.close()
            } catch (_: Throwable) {
            }
            return
        }
        try {
            client.getOutputStream().write("HTTP/1.1 200 Connection established\r\n\r\n".toByteArray())
            client.getOutputStream().flush()
            open.add(upstream)
            pool.execute { pipe(client, upstream) }
            pipe(upstream, client)
        } finally {
            closeQuietly(client)
            closeQuietly(upstream)
            open.remove(upstream)
        }
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
