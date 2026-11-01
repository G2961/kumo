package space.g2961.opencode

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import org.json.JSONArray
import org.json.JSONObject
import java.security.KeyStore
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Credentials stay in Android Keystore encrypted preferences, outside WebView storage. */
class SshConnectionStore(context: Context) : SshToolsServer.Store {
    private val prefs = context.getSharedPreferences("ssh_connections", Context.MODE_PRIVATE)
    private val alias = "codex_mobile_ssh"
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    @Synchronized override fun get(id: String): JSONObject? {
        val saved = prefs.getString(id, null) ?: return null
        val parts = saved.split(":", limit = 2)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.getDecoder().decode(parts[0])))
        return JSONObject(String(cipher.doFinal(Base64.getDecoder().decode(parts[1])), Charsets.UTF_8))
    }
    @Synchronized override fun list(): JSONArray {
        val result = JSONArray()
        prefs.all.keys.forEach { id -> get(id)?.let { result.put(RemoteSsh.Connection(it).publicInfo()) } }
        return result
    }
    @Synchronized override fun save(data: JSONObject) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val encrypted = cipher.doFinal(data.toString().toByteArray(Charsets.UTF_8))
        val saved = Base64.getEncoder().encodeToString(cipher.iv) + ":" + Base64.getEncoder().encodeToString(encrypted)
        prefs.edit().putString(data.getString("id"), saved).apply()
    }
    @Synchronized override fun remove(id: String) { prefs.edit().remove(id).apply() }
}
