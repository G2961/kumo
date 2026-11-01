package space.g2961.opencode

import java.io.File
import java.nio.charset.StandardCharsets.UTF_8
import org.eclipse.jgit.api.Git
import org.eclipse.jgit.lib.CommitBuilder
import org.eclipse.jgit.lib.Constants
import org.eclipse.jgit.lib.FileMode
import org.eclipse.jgit.lib.ObjectId
import org.eclipse.jgit.lib.PersonIdent
import org.eclipse.jgit.lib.RefUpdate
import org.eclipse.jgit.lib.Repository
import org.eclipse.jgit.lib.TreeFormatter
import org.eclipse.jgit.revwalk.RevWalk
import org.eclipse.jgit.treewalk.TreeWalk
import org.json.JSONArray
import org.json.JSONObject

/** Git commits containing agent patches only; never stages the user's worktree/index. */
class ChangeHistory(private val registered: () -> List<File>) {
    private val identity get() = PersonIdent("Codex Mobile", "agent@codex-mobile.invalid")
    private val sessionPattern = Regex("ses_[A-Za-z0-9_-]+")
    private val hunkPattern = Regex("^@@ -(\\d+)(?:,(\\d+))? \\+(\\d+)(?:,(\\d+))? @@.*")

    private fun project(directory: String): File {
        val root = File(directory).canonicalFile
        require(root.isDirectory && registered().any { it.canonicalFile == root }) { "Project is not registered" }
        val dotGit = File(root, ".git")
        require(!dotGit.exists() || (dotGit.isDirectory && dotGit.canonicalFile.toPath().startsWith(root.toPath()))) {
            "History requires a Git directory inside the registered project"
        }
        return root
    }

    @Synchronized fun register(directory: String): JSONObject {
        val root = project(directory)
        if (!File(root, ".git").exists()) {
            Git.init().setDirectory(root).call().use { git ->
                git.repository.config.apply {
                    setString("user", null, "name", "Codex Mobile")
                    setString("user", null, "email", "agent@codex-mobile.invalid")
                    save()
                }
                // Empty identity commit makes OpenCode recognize a distinct project;
                // existing user files remain untracked and are never snapshotted here.
                git.repository.newObjectInserter().use { objects ->
                    val commit = CommitBuilder().apply {
                        setTreeId(objects.insert(TreeFormatter()))
                        author = identity; committer = author; message = "Initialize mobile project"
                    }
                    val id = objects.insert(commit); objects.flush()
                    // Plumbing avoids user/global commit hooks and signing settings.
                    val update = git.repository.updateRef(Constants.HEAD).apply {
                        setExpectedOldObjectId(ObjectId.zeroId()); setNewObjectId(id)
                    }.update()
                    require(update == RefUpdate.Result.NEW) { "Could not initialize project identity" }
                }
            }
        }
        return JSONObject().put("registered", true)
    }

    private fun open(directory: String): Git {
        register(directory)
        return Git.open(project(directory))
    }

    private fun ref(session: String): String {
        require(sessionPattern.matches(session)) { "Invalid history session" }
        return "refs/codex/changes/$session"
    }

    private fun blob(repo: Repository, commit: ObjectId, path: String): String {
        RevWalk(repo).use { walk ->
            val tree = walk.parseCommit(commit).tree
            TreeWalk.forPath(repo, path, tree)?.use { entry ->
                return repo.open(entry.getObjectId(0)).getBytes(2 * 1024 * 1024).toString(UTF_8)
            }
        }
        throw IllegalArgumentException("History entry not found")
    }

    private fun manifest(repo: Repository, commit: ObjectId): JSONObject = JSONObject(blob(repo, commit, "turn.json"))

    /** Keep at most three unchanged context lines beside each actual changed hunk. */
    private fun compact(patch: String): String {
        require(patch.toByteArray(UTF_8).size <= 1024 * 1024) { "Patch exceeds history limit" }
        val lines = patch.split('\n')
        val result = mutableListOf<String>()
        var header = emptyList<String>()
        var i = 0
        while (i < lines.size) {
            val line = lines[i]
            if (line.startsWith("--- ") && i + 1 < lines.size && lines[i + 1].startsWith("+++ ")) {
                header = listOf(line, lines[++i]); i++; continue
            }
            val match = hunkPattern.matchEntire(line)
            if (match == null) { i++; continue }
            i++
            val body = mutableListOf<String>()
            var oldRemaining = match.groupValues[2].ifEmpty { "1" }.toInt()
            var newRemaining = match.groupValues[4].ifEmpty { "1" }.toInt()
            while (oldRemaining > 0 || newRemaining > 0) {
                require(i < lines.size) { "Incomplete patch hunk" }
                val text = lines[i++]
                when (text.firstOrNull()) {
                    ' ' -> { oldRemaining--; newRemaining-- }
                    '-' -> oldRemaining--
                    '+' -> newRemaining--
                    '\\' -> require(text.startsWith("\\ No newline")) { "Invalid patch annotation" }
                    else -> throw IllegalArgumentException("Invalid patch hunk")
                }
                require(oldRemaining >= 0 && newRemaining >= 0) { "Invalid patch line count" }
                body.add(text)
            }
            if (i < lines.size && lines[i].startsWith("\\ No newline")) body.add(lines[i++])
            val changes = body.indices.filter { body[it].startsWith("+") || body[it].startsWith("-") }
            if (changes.isEmpty()) continue
            require(header.size == 2) { "Patch has no file header" }
            result.addAll(header)
            val ranges = mutableListOf<IntRange>()
            for (changed in changes) {
                val start = maxOf(0, changed - 3)
                var end = minOf(body.lastIndex, changed + 3)
                if (end < body.lastIndex && body[end + 1].startsWith("\\")) end++
                if (ranges.isNotEmpty() && start <= ranges.last().last + 1) {
                    ranges[ranges.lastIndex] = ranges.last().first..maxOf(end, ranges.last().last)
                } else ranges.add(start..end)
            }
            var old = match.groupValues[1].toInt()
            var new = match.groupValues[3].toInt()
            val oldAt = IntArray(body.size)
            val newAt = IntArray(body.size)
            body.forEachIndexed { n, text ->
                oldAt[n] = old; newAt[n] = new
                if (text.startsWith(" ") || text.startsWith("-")) old++
                if (text.startsWith(" ") || text.startsWith("+")) new++
            }
            for (range in ranges) {
                val part = body.slice(range)
                val oldCount = part.count { it.startsWith(" ") || it.startsWith("-") }
                val newCount = part.count { it.startsWith(" ") || it.startsWith("+") }
                val oldStart = if (oldCount == 0) maxOf(0, oldAt[range.first] - 1) else oldAt[range.first]
                val newStart = if (newCount == 0) maxOf(0, newAt[range.first] - 1) else newAt[range.first]
                result.add("@@ -$oldStart,$oldCount +$newStart,$newCount @@")
                result.addAll(part)
            }
        }
        return if (result.isEmpty()) "" else result.joinToString("\n") + "\n"
    }

    private fun counts(patch: String): Pair<Int, Int> {
        var add = 0; var del = 0; var old = 0; var new = 0
        for (line in patch.lines()) {
            val hunk = hunkPattern.matchEntire(line)
            if (hunk != null) {
                old = hunk.groupValues[2].ifEmpty { "1" }.toInt()
                new = hunk.groupValues[4].ifEmpty { "1" }.toInt()
            } else if (old > 0 || new > 0) when (line.firstOrNull()) {
                '+' -> { add++; new-- }
                '-' -> { del++; old-- }
                ' ' -> { old--; new-- }
            }
        }
        return add to del
    }

    private fun hintFile(root: File, sid: String): File = File(root, ".git/codex-hints/$sid.json")

    @Synchronized fun action(request: JSONObject): JSONObject {
        val data = request.getJSONObject("data")
        val directory = data.getString("directory")
        val action = request.getString("action")
        if (action == "historyRegister") return register(directory)
        val sid = data.getString("sessionID")
        val branch = ref(sid)
        open(directory).use { git ->
            val repo = git.repository
            val hintsFile = hintFile(project(directory), sid)
            require(hintsFile.canonicalFile.toPath().startsWith(repo.directory.canonicalFile.toPath())) { "Invalid history cache path" }
            val hints = if (hintsFile.exists()) JSONObject(hintsFile.readText(UTF_8)) else JSONObject()
            if (action == "historyHint") {
                val key = data.getString("key")
                require(key.length in 1..256 && hints.length() < 1024) { "Invalid change hint" }
                hints.put(key, compact(data.getString("patch")))
                require(hints.toString().toByteArray(UTF_8).size <= 2 * 1024 * 1024) { "History hint cache is full" }
                hintsFile.parentFile?.mkdirs(); hintsFile.writeText(hints.toString(), UTF_8)
                return JSONObject().put("saved", true)
            }
            val head = repo.resolve(branch)
            val previous = if (head == null) emptyList() else git.log().add(head).call().toList()
            if (action == "historyRecord") {
                val known = previous.map { manifest(repo, it.id).getString("id") }.toMutableSet()
                var parent = head
                val turns = data.getJSONArray("turns")
                require(turns.length() <= 1000) { "Too many turns in a history request" }
                for (n in 0 until turns.length()) {
                    val turn = turns.getJSONObject(n)
                    val turnID = turn.getString("id")
                    require(turnID.length in 1..256) { "Invalid turn ID" }
                    if (turnID in known) continue
                    val files = JSONArray()
                    val patches = mutableListOf<String>()
                    val input = turn.getJSONArray("files")
                    require(input.length() <= 256) { "Too many patches in a turn" }
                    for (f in 0 until input.length()) {
                        val item = input.getJSONObject(f)
                        val source = item.getString("source")
                        require(source == "local" || source == "ssh") { "Invalid change source" }
                        val path = item.getString("file")
                        require(path.length in 1..4096 && !path.contains('\u0000')) { "Invalid change path" }
                        val file = if (source == "local") {
                            val root = project(directory)
                            val target = File(path).let { if (it.isAbsolute) it else File(root, path) }.canonicalFile
                            require(target.toPath().startsWith(root.toPath()) && !target.toPath().startsWith(File(root, ".git").toPath())) {
                                "Local change is outside the registered project"
                            }
                            root.toPath().relativize(target.toPath()).toString().replace('\\', '/')
                        } else path // Remote paths are metadata; never opened on this device.
                        val text = item.optString("patch").ifEmpty { hints.optString(item.optString("key")) }
                        val patch = compact(text)
                        if (patch.isEmpty()) continue
                        val (additions, deletions) = counts(patch)
                        files.put(JSONObject().put("key", item.optString("key")).put("file", file).put("source", source)
                            .put("connectionID", item.optString("connectionID"))
                            .put("additions", additions).put("deletions", deletions)
                            .put("patchPath", "patches/${patches.size.toString().padStart(4, '0')}.patch"))
                        patches.add(patch)
                    }
                    if (files.length() == 0) continue
                    val metadata = JSONObject().put("id", turnID).put("sessionID", sid)
                        .put("title", turn.optString("title").take(160)).put("time", turn.optLong("time"))
                        .put("files", files)
                    repo.newObjectInserter().use { objects ->
                        val patchTree = TreeFormatter()
                        patches.forEachIndexed { index, patch ->
                            patchTree.append("${index.toString().padStart(4, '0')}.patch", FileMode.REGULAR_FILE,
                                objects.insert(Constants.OBJ_BLOB, patch.toByteArray(UTF_8)))
                        }
                        val tree = TreeFormatter()
                        tree.append("patches", FileMode.TREE, objects.insert(patchTree))
                        tree.append("turn.json", FileMode.REGULAR_FILE, objects.insert(Constants.OBJ_BLOB, metadata.toString().toByteArray(UTF_8)))
                        val commit = CommitBuilder().apply {
                            setTreeId(objects.insert(tree)); if (parent != null) setParentId(parent)
                            author = identity; committer = author; message = "Agent turn $turnID"
                        }
                        val id = objects.insert(commit); objects.flush()
                        val update = repo.updateRef(branch).apply {
                            setExpectedOldObjectId(parent ?: ObjectId.zeroId()); setNewObjectId(id)
                        }.update()
                        require(update in listOf(RefUpdate.Result.NEW, RefUpdate.Result.FAST_FORWARD)) { "History changed concurrently" }
                        parent = id
                    }
                    known.add(turnID)
                    for (f in 0 until input.length()) hints.remove(input.getJSONObject(f).optString("key"))
                }
                if (hintsFile.exists()) hintsFile.writeText(hints.toString(), UTF_8)
                return JSONObject().put("saved", true)
            }
            if (action == "historyList") {
                return JSONObject().put("turns", JSONArray(previous.take(200).map {
                    manifest(repo, it.id).put("commit", it.id.name)
                }))
            }
            if (action == "historyShow") {
                val commit = data.getString("commit")
                require(ObjectId.isId(commit) && previous.any { it.id.name == commit }) { "Commit is outside this session history" }
                val id = ObjectId.fromString(commit)
                val result = manifest(repo, id)
                val files = result.getJSONArray("files")
                for (i in 0 until files.length()) {
                    val item = files.getJSONObject(i)
                    item.put("patch", blob(repo, id, item.getString("patchPath")))
                }
                return result.put("commit", commit)
            }
            throw IllegalArgumentException("Unknown history action")
        }
    }
}
