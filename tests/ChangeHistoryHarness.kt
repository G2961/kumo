import java.io.File
import org.json.JSONObject
import space.g2961.opencode.ChangeHistory

/** Runs the exact Android history core on a JVM; no mock Git implementation. */
fun main(args: Array<String>) {
    val history = ChangeHistory { args.map { File(it) } }
    generateSequence(::readLine).forEach { raw ->
        val result = try { history.action(JSONObject(raw)) }
        catch (_: Exception) { JSONObject().put("error", "History request rejected") }
        println(result.toString())
        System.out.flush()
    }
}
