package space.g2961.opencode;

import org.json.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.*;
import java.util.concurrent.*;

/** Local authenticated MCP tools. The agent receives connection IDs, never credentials. */
public final class SshToolsServer {
    public interface Store {
        JSONArray list() throws Exception;
        JSONObject get(String id) throws Exception;
        void save(JSONObject data) throws Exception;
        void remove(String id) throws Exception;
    }
    private final Store store;
    private final RemoteSsh ssh;
    private final RemoteSsh.Events listener;
    private final String token;
    private final boolean testBridge;
    private final ExecutorService pool=Executors.newCachedThreadPool(r->{Thread t=new Thread(r,"ssh-tools");t.setDaemon(true);return t;});
    private final Set<Socket> sockets=ConcurrentHashMap.newKeySet();
    private final Set<OutputStream> streams=ConcurrentHashMap.newKeySet();
    private final Deque<JSONObject> history=new ArrayDeque<>();
    private final Map<String,String> selectedConnections=new ConcurrentHashMap<>();
    private ServerSocket server;
    public SshToolsServer(Store data,RemoteSsh.Events events){this(data,events,false);}
    public SshToolsServer(Store data,RemoteSsh.Events events,boolean allowTestBridge){
        store=data;listener=events;testBridge=allowTestBridge;byte[] b=new byte[32];new SecureRandom().nextBytes(b);
        token=Base64.getUrlEncoder().withoutPadding().encodeToString(b);ssh=new RemoteSsh(this::emit);
    }
    public String token(){return token;}
    public String url(){return "http://127.0.0.1:"+server.getLocalPort()+"/mcp";}
    public int port(){return server.getLocalPort();}
    public void start() throws IOException {
        server=new ServerSocket();server.bind(new InetSocketAddress("127.0.0.1",0));
        pool.execute(()->{while(!server.isClosed()){try{Socket s=server.accept();sockets.add(s);pool.execute(()->handle(s));}catch(IOException ignored){break;}}});
    }
    public void stop(){ssh.close();try{server.close();}catch(Exception ignored){}for(Socket s:sockets)try{s.close();}catch(IOException ignored){}pool.shutdownNow();}
    private void emit(JSONObject event){
        synchronized(history){history.addLast(event);while(history.size()>300)history.removeFirst();}
        listener.emit(event);byte[] bytes=("data: "+event+"\n\n").getBytes(StandardCharsets.UTF_8);
        for(OutputStream out:streams)try{synchronized(out){out.write(bytes);out.flush();}}catch(IOException e){streams.remove(out);}
    }
    public JSONObject action(JSONObject request) throws Exception {
        String action=request.optString("action");JSONObject data=request.optJSONObject("data");if(data==null)data=new JSONObject();
        switch(action){
            case "sessionContext":{
                String sid=data.getString("sessionID"),connection=data.getString("connectionID");
                if(!sid.startsWith("ses_")||store.get(connection)==null)throw new IllegalArgumentException("Invalid SSH session context");
                selectedConnections.put(sid,connection);return RemoteSsh.obj("saved",true);
            }
            case "connections":return RemoteSsh.obj("connections",store.list());
            case "removeConnection":store.remove(data.getString("id"));return RemoteSsh.obj("connections",store.list());
            case "saveConnection":{
                String id=data.optString("id");JSONObject old=id.isEmpty()?null:store.get(id);
                if(old!=null){
                    String auth=data.optString("auth",old.optString("auth","password"));
                    for(String field:Arrays.asList("password","privateKey","passphrase")){
                        if(data.optString(field).isEmpty()&&auth.equals(old.optString("auth","password")))data.put(field,old.optString(field));
                    }
                }
                if(data.optString("auth","password").equals("key"))data.put("password","");else{data.put("privateKey","");data.put("passphrase","");}
                RemoteSsh.Connection c=new RemoteSsh.Connection(data);
                if(c.password.isEmpty()&&c.privateKey.isEmpty())throw new IllegalArgumentException("Enter SSH credentials");
                try{ssh.test(c);}catch(RemoteSsh.HostKeyRequired e){return RemoteSsh.obj("requiresTrust",true,"fingerprint",e.fingerprint,"id",c.id,"host",c.host);}
                data.put("id",c.id);data.put("name",c.name);store.save(data);
                return RemoteSsh.obj("connection",c.publicInfo(),"connections",store.list());
            }
            case "cancelRemote":ssh.cancelSession(data.getString("sessionID"));return RemoteSsh.obj("cancelled",true);
            case "remoteHistory":{
                JSONArray list=new JSONArray();synchronized(history){for(JSONObject e:history)if(e.optString("sessionID").equals(data.optString("sessionID")))list.put(e);}
                return RemoteSsh.obj("events",list);
            }
            default:throw new IllegalArgumentException("Unknown mobile action");
        }
    }
    private JSONObject call(String name,JSONObject args) throws Exception {
        if(name.equals("ssh_connections"))return RemoteSsh.obj("connections",store.list());
        String connectionId=args.getString("connection_id");
        if(!connectionId.equals(selectedConnections.get(args.optString("session_id"))))throw new IllegalArgumentException("SSH session is not bound to this connection");
        JSONObject raw=store.get(connectionId);if(raw==null)throw new IllegalArgumentException("SSH connection not found");
        RemoteSsh.Connection c=new RemoteSsh.Connection(raw);String sid=args.optString("session_id");
        if(!sid.startsWith("ses_"))throw new IllegalArgumentException("A session_id is required");
        switch(name){
            case "ssh_exec":return ssh.execute(c,sid,args.getString("command"),args.optString("directory"),args.optInt("timeout_seconds",120));
            case "ssh_read_file":return ssh.readFile(c,args.getString("path"));
            case "ssh_write_file":return ssh.writeFile(c,sid,args.getString("path"),args.getString("content"));
            default:throw new IllegalArgumentException("Unknown SSH tool");
        }
    }
    private static JSONObject string(){return RemoteSsh.obj("type","string");}
    private static JSONObject tool(String name,String description,JSONObject properties,String... required){
        return RemoteSsh.obj("name",name,"description",description,"inputSchema",RemoteSsh.obj("type","object","properties",properties,"required",new JSONArray(Arrays.asList(required)),"additionalProperties",false));
    }
    private JSONArray tools(){
        return new JSONArray(Arrays.asList(
            tool("ssh_connections","List saved SSH destinations without credentials.",new JSONObject()),
            tool("ssh_exec","Run a shell command on the selected SSH server. Stream stdout and stderr. Use the current session_id supplied in the prompt.",RemoteSsh.obj("connection_id",string(),"session_id",string(),"command",string(),"directory",string(),"timeout_seconds",RemoteSsh.obj("type","integer","minimum",1,"maximum",900)),"connection_id","session_id","command"),
            tool("ssh_read_file","Read a UTF-8 file using SFTP on the selected SSH server.",RemoteSsh.obj("connection_id",string(),"session_id",string(),"path",string()),"connection_id","session_id","path"),
            tool("ssh_write_file","Create or replace a UTF-8 file using SFTP. Return the actual before/after patch.",RemoteSsh.obj("connection_id",string(),"session_id",string(),"path",string(),"content",string()),"connection_id","session_id","path","content")
        ));
    }
    private JSONObject rpc(JSONObject request) throws Exception {
        String method=request.optString("method");Object id=request.opt("id");JSONObject params=request.optJSONObject("params");if(params==null)params=new JSONObject();
        if(id==null)return null;
        JSONObject result;
        switch(method){
            case "initialize":result=RemoteSsh.obj("protocolVersion",params.optString("protocolVersion","2024-11-05"),"capabilities",RemoteSsh.obj("tools",RemoteSsh.obj("listChanged",false)),"serverInfo",RemoteSsh.obj("name","Mobile SSH","version","1.0.0"));break;
            case "ping":result=new JSONObject();break;
            case "tools/list":result=RemoteSsh.obj("tools",tools());break;
            case "tools/call":{
                try{JSONObject value=call(params.getString("name"),params.getJSONObject("arguments"));result=RemoteSsh.obj("content",new JSONArray().put(RemoteSsh.obj("type","text","text",value.toString())),"isError",false);}
                catch(Exception e){result=RemoteSsh.obj("content",new JSONArray().put(RemoteSsh.obj("type","text","text",RemoteSsh.safeError(e))),"isError",true);}
                break;
            }
            default:return RemoteSsh.obj("jsonrpc","2.0","id",id,"error",RemoteSsh.obj("code",-32601,"message","Method not found"));
        }
        return RemoteSsh.obj("jsonrpc","2.0","id",id,"result",result);
    }
    private void handle(Socket socket){
        try(Socket s=socket){
            s.setSoTimeout(20000);s.setTcpNoDelay(true);InputStream in=s.getInputStream();ByteArrayOutputStream head=new ByteArrayOutputStream();int tail=0;
            while(head.size()<32768){int b=in.read();if(b<0)return;head.write(b);tail=(tail<<8)|b;if(tail==0x0d0a0d0a)break;}
            String[] lines=head.toString("ISO-8859-1").split("\r\n"),first=lines[0].split(" ");if(first.length<2)return;
            String method=first[0],path=first[1].split("\\?",2)[0];Map<String,String> headers=new HashMap<>();
            for(int i=1;i<lines.length;i++){int colon=lines[i].indexOf(':');if(colon>0)headers.put(lines[i].substring(0,colon).toLowerCase(Locale.ROOT),lines[i].substring(colon+1).trim());}
            String origin=headers.getOrDefault("origin","");String cors="";
            if(testBridge&&!origin.isEmpty()){URI u=URI.create(origin);if("127.0.0.1".equals(u.getHost()))cors="Access-Control-Allow-Origin: "+origin+"\r\nAccess-Control-Allow-Headers: Authorization, Content-Type\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\n";}
            if(method.equals("OPTIONS")&&testBridge&&!cors.isEmpty()){respond(s,204,"",cors);return;}
            if(!("Bearer "+token).equals(headers.get("authorization"))){respond(s,401,"",cors);return;}
            if(path.equals("/events")&&testBridge&&method.equals("GET")){
                OutputStream out=s.getOutputStream();out.write(("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n"+cors+"\r\n").getBytes(StandardCharsets.UTF_8));out.flush();streams.add(out);
                try{while(!server.isClosed()&&!s.isClosed()){Thread.sleep(1000);synchronized(out){out.write(": keepalive\n\n".getBytes(StandardCharsets.UTF_8));out.flush();}}}finally{streams.remove(out);}return;
            }
            if(!method.equals("POST")||(!path.equals("/mcp")&&!(testBridge&&path.equals("/bridge")))){respond(s,405,"",cors);return;}
            int length=Integer.parseInt(headers.getOrDefault("content-length","0"));if(length<0||length>4*1024*1024){respond(s,413,"",cors);return;}
            byte[] body=new byte[length];int at=0;while(at<length){int n=in.read(body,at,length-at);if(n<0)throw new EOFException();at+=n;}
            JSONObject request=new JSONObject(new String(body,StandardCharsets.UTF_8));
            JSONObject result;
            if(path.equals("/bridge")){try{result=action(request);}catch(Exception e){result=RemoteSsh.obj("error",RemoteSsh.safeError(e));}}
            else result=rpc(request);
            respond(s,result==null?202:200,result==null?"":result.toString(),cors);
        }catch(Exception ignored){}finally{sockets.remove(socket);}
    }
    private static void respond(Socket s,int status,String value,String extra) throws IOException {
        byte[] bytes=value.getBytes(StandardCharsets.UTF_8);OutputStream out=s.getOutputStream();
        out.write(("HTTP/1.1 "+status+" Response\r\nContent-Type: application/json\r\nContent-Length: "+bytes.length+"\r\nConnection: close\r\n"+extra+"\r\n").getBytes(StandardCharsets.UTF_8));out.write(bytes);out.flush();
    }
}
