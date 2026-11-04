package space.g2961.opencode

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.os.PowerManager

/** Foreground keep-alive: the bundled opencode server and the streaming WebView must
 *  keep working while the app is backgrounded. A partial wake lock keeps CPU/sockets
 *  alive in Doze so SSE chunks arrive instead of stalling until the next unlock. */
class KeepAliveService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Agent runtime", NotificationManager.IMPORTANCE_LOW))
        val notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_agent)
            .setContentTitle("Kumo agent is running")
            .setContentText("Streaming continues in the background")
            .setOngoing(true)
            .build()
        startForeground(ID, notification)
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "opencode:agent-stream")
            .apply { acquire() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
        super.onDestroy()
    }

    companion object {
        // v2: the first channel was created with MIN importance and channels are
        // immutable after creation — a new ID is the only way to make it visible.
        const val CHANNEL = "agent-runtime-v2"
        const val ID = 1
    }
}
