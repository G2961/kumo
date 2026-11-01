import org.json.*;
import space.g2961.opencode.*;
import java.nio.file.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;

/** Real JVM version of the Android SSH/MCP service; test credentials are held in memory. */
public class MobileSshHarness {
    public static void main(String[] args) throws Exception {
        ConcurrentHashMap<String,JSONObject> entries=new ConcurrentHashMap<>();
        SshToolsServer.Store store=new SshToolsServer.Store(){
            public JSONArray list(){JSONArray a=new JSONArray();entries.values().forEach(c->a.put(new RemoteSsh.Connection(c).publicInfo()));return a;}
            public JSONObject get(String id){return entries.get(id);}
            public void save(JSONObject c){entries.put(c.getString("id"),new JSONObject(c.toString()));}
            public void remove(String id){entries.remove(id);}
        };
        SshToolsServer server=new SshToolsServer(store,e->{},true);server.start();
        Runtime.getRuntime().addShutdownHook(new Thread(server::stop));
        Files.writeString(Path.of(args[0]),new JSONObject().put("port",server.port()).put("url",server.url()).put("token",server.token()).toString());
        new CountDownLatch(1).await();
    }
}
