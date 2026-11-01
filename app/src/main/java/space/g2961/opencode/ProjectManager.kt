package space.g2961.opencode

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import java.io.File

object ProjectManager {

    private const val PREFS = "projects"
    private const val KEY_CURRENT = "current"

    fun projectsRoot(context: Context): File = File(context.filesDir, "projects").apply { mkdirs() }

    fun homeDir(context: Context): File = File(context.filesDir, "home").apply { mkdirs() }

    fun currentDir(context: Context): File {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val saved = prefs.getString(KEY_CURRENT, null)
        if (saved != null && File(saved).isDirectory) return File(saved)
        val def = File(projectsRoot(context), "default").apply { mkdirs() }
        prefs.edit().putString(KEY_CURRENT, def.absolutePath).apply()
        return def
    }

    fun setCurrent(context: Context, dir: File) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY_CURRENT, dir.absolutePath).apply()
    }

    fun createNew(context: Context, name: String): File {
        val safe = name.trim().replace(Regex("[^A-Za-z0-9._\\- ]"), "").ifEmpty { "project" }
        val root = homeDir(context)
        var target = File(root, safe)
        var i = 1
        while (target.exists()) {
            target = File(root, "${safe}-${i++}")
        }
        target.mkdirs()
        setCurrent(context, target)
        return target
    }

    fun resolveTreePath(context: Context, treeUri: Uri): File? {
        return try {
            val docId = DocumentsContract.getTreeDocumentId(treeUri)
            if (!docId.startsWith("primary:")) return resolveSecondary(context, treeUri, docId)
            val rel = docId.substringAfter("primary:", "")
            val base = android.os.Environment.getExternalStorageDirectory()
            val dir = if (rel.isEmpty() || rel == ":") base else File(base, rel)
            if (dir.isDirectory) dir else null
        } catch (_: Throwable) {
            null
        }
    }

    private fun resolveSecondary(context: Context, treeUri: Uri, docId: String): File? {
        return try {
            val parts = docId.split(":", limit = 2)
            if (parts.size != 2) return null
            val storageId = parts[0]
            val rel = parts[1]
            val candidate = File("/storage/$storageId", rel)
            if (candidate.isDirectory) candidate else null
        } catch (_: Throwable) {
            null
        }
    }

    /** Реально пробуем создать и удалить файл: canWrite() на FUSE-путях врёт. */
    fun canWrite(dir: File): Boolean {
        return try {
            val probe = File(dir, ".opencode-write-test-${System.nanoTime()}")
            val ok = probe.createNewFile()
            probe.delete()
            ok
        } catch (_: Throwable) {
            false
        }
    }

    /** Папка читается (а не просто существует): без разрешения listFiles() вернёт null. */
    fun canRead(dir: File): Boolean = dir.isDirectory && dir.listFiles() != null

    fun copyTree(context: Context, treeUri: Uri, dest: File): Boolean {
        return try {
            val root = DocumentsContract.buildDocumentUriUsingTree(
                treeUri, DocumentsContract.getTreeDocumentId(treeUri)
            )
            copyDocumentDir(context, root, dest)
            true
        } catch (_: Throwable) {
            false
        }
    }

    private fun copyDocumentDir(context: Context, dirUri: Uri, dest: File): Boolean {
        dest.mkdirs()
        val resolver = context.contentResolver
        val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(
            dirUri, DocumentsContract.getDocumentId(dirUri)
        )
        resolver.query(childrenUri, arrayOf(
            Document.COLUMN_DOCUMENT_ID,
            Document.COLUMN_DISPLAY_NAME,
            Document.COLUMN_MIME_TYPE
        ), null, null, null)?.use { c ->
            while (c.moveToNext()) {
                val docId = c.getString(0)
                val name = c.getString(1) ?: continue
                val mime = c.getString(2) ?: ""
                val docUri = DocumentsContract.buildDocumentUriUsingTree(dirUri, docId)
                if (mime == Document.MIME_TYPE_DIR) {
                    copyDocumentDir(context, docUri, File(dest, name))
                } else {
                    try {
                        resolver.openInputStream(docUri)?.use { input ->
                            File(dest, name).outputStream().use { output -> input.copyTo(output) }
                        }
                    } catch (_: Throwable) {
                    }
                }
            }
        }
        return true
    }
}
