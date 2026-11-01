package space.g2961.opencode;

import com.jcraft.jsch.*;
import org.json.JSONObject;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/** SSH operations shared by Android and the real JVM integration test. */
public final class RemoteSsh {
    public interface Events { void emit(JSONObject event); }
    public static final class Connection {
        public String id, name, host, user, directory, fingerprint, password, privateKey, passphrase;
        public int port;
        public Connection(JSONObject j) {
            id=j.optString("id"); name=j.optString("name"); host=j.optString("host");
            user=j.optString("user"); port=j.optInt("port",22); directory=j.optString("directory");
            fingerprint=j.optString("fingerprint"); password=j.optString("password");
            privateKey=j.optString("privateKey"); passphrase=j.optString("passphrase");
            if(id.isEmpty())id=UUID.randomUUID().toString(); if(name.isEmpty())name=user+"@"+host;
            if(!host.matches("[A-Za-z0-9._:\\-\\[\\]]+")||user.isEmpty()||port<1||port>65535||directory.contains("\n")||directory.contains("\0"))
                throw new IllegalArgumentException("Invalid SSH connection");
        }
        public JSONObject publicInfo() { return obj("id",id,"name",name,"host",host,"port",port,"user",user,"directory",directory,"fingerprint",fingerprint,"auth",privateKey.isEmpty()?"password":"key"); }
    }
    public static final class HostKeyRequired extends Exception {
        public final String fingerprint;
        HostKeyRequired(String value) { super("SSH host key needs confirmation");fingerprint=value; }
    }
    private final Map<String,Session> active=new ConcurrentHashMap<>();
    private final Map<String,String> sessions=new ConcurrentHashMap<>();
    private final Events events;
    public RemoteSsh(Events sink) { events=sink; }
    public static JSONObject obj(Object... pairs) {
        JSONObject j=new JSONObject();
        try { for(int i=0;i<pairs.length;i+=2)j.put((String)pairs[i],pairs[i+1]); }
        catch(Exception e){throw new IllegalArgumentException("Invalid JSON");} return j;
    }
    public static String fingerprint(byte[] key) {
        try { return "SHA256:"+Base64.getEncoder().withoutPadding().encodeToString(MessageDigest.getInstance("SHA-256").digest(key)); }
        catch(Exception e){throw new IllegalStateException(e);}
    }
    private Session connect(Connection c) throws Exception {
        JSch jsch=new JSch();
        // Android's Java provider lacks Ed25519 on older releases. These implementations use BC directly.
        JSch.setConfig("signature.ed25519","com.jcraft.jsch.bc.SignatureEd25519");
        JSch.setConfig("signature.ed448","com.jcraft.jsch.bc.SignatureEd448");
        final String[] observed={""};
        jsch.setHostKeyRepository(new HostKeyRepository() {
            public int check(String host,byte[] key){observed[0]=fingerprint(key);return observed[0].equals(c.fingerprint)?OK:NOT_INCLUDED;}
            public void add(HostKey key,UserInfo ui){} public void remove(String h,String t){}
            public void remove(String h,String t,byte[] k){} public String getKnownHostsRepositoryID(){return "mobile";}
            public HostKey[] getHostKey(){return new HostKey[0];} public HostKey[] getHostKey(String h,String t){return new HostKey[0];}
        });
        if(!c.privateKey.isEmpty())jsch.addIdentity("mobile",c.privateKey.getBytes(StandardCharsets.UTF_8),null,c.passphrase.isEmpty()?null:c.passphrase.getBytes(StandardCharsets.UTF_8));
        Session s=jsch.getSession(c.user,c.host,c.port);
        s.setConfig("StrictHostKeyChecking","yes");
        s.setConfig("PreferredAuthentications",c.privateKey.isEmpty()?"password,keyboard-interactive":"publickey");
        if(!c.password.isEmpty())s.setPassword(c.password);
        s.setTimeout(15000);s.setServerAliveInterval(15000);
        try{s.connect(15000);return s;}
        catch(Exception e){s.disconnect();if(!observed[0].isEmpty()&&!observed[0].equals(c.fingerprint))throw new HostKeyRequired(observed[0]);throw e;}
    }
    public void test(Connection c) throws Exception { Session s=connect(c);s.disconnect(); }
    public void cancelSession(String sid) { sessions.forEach((run,id)->{if(id.equals(sid)){Session s=active.get(run);if(s!=null)s.disconnect();}}); }
    public void close() { active.values().forEach(Session::disconnect); }
    public static String safeError(Throwable e) {
        String m=String.valueOf(e.getMessage()).toLowerCase(Locale.ROOT);
        if(m.contains("auth fail"))return "SSH authentication failed. Check your username and credentials.";
        if(m.contains("timeout")||m.contains("timed out"))return "SSH connection timed out.";
        if(m.contains("privatekey")||m.contains("private key"))return "SSH private key could not be opened. Check its format and passphrase.";
        return "SSH request failed ("+e.getClass().getSimpleName()+").";
    }
    private static String redact(String text,Connection c) {
        String out=text;
        if(!c.password.isEmpty())out=out.replace(c.password,"[redacted]");
        if(!c.passphrase.isEmpty())out=out.replace(c.passphrase,"[redacted]");
        return out.replaceAll("(?i)\\b(?:Bearer|Basic)\\s+[^\\s\\\"']+","[redacted]").replaceAll("\\bsk-[\\w-]+","[redacted]");
    }
    private void event(String type,String run,String sid,Connection c,Object... data) {
        JSONObject e=obj("type",type,"runID",run,"sessionID",sid,"connectionID",c.id);
        try{for(int i=0;i<data.length;i+=2)e.put((String)data[i],data[i+1]);}catch(Exception ignored){}
        events.emit(e);
    }
    private static String quote(String value){return "'"+value.replace("'","'\\''")+"'";}
    private Thread pump(InputStream stream,StringBuilder output,Connection c,String run,String sid,String kind) {
        Thread t=new Thread(()->{
            try(Reader reader=new InputStreamReader(stream,StandardCharsets.UTF_8)){
                char[] chars=new char[4096];int n;
                while((n=reader.read(chars))>=0){String delta=redact(new String(chars,0,n),c);synchronized(output){if(output.length()<1024*1024)output.append(delta,0,Math.min(delta.length(),1024*1024-output.length()));}event("ssh.command.delta",run,sid,c,"stream",kind,"delta",delta);}
            }catch(IOException ignored){}
        },"ssh-output");t.setDaemon(true);t.start();return t;
    }
    public JSONObject execute(Connection c,String sid,String command,String directory,int timeout) throws Exception {
        if(command.isEmpty()||command.indexOf('\0')>=0)throw new IllegalArgumentException("Command is empty");
        String run=UUID.randomUUID().toString();Session s=connect(c);active.put(run,s);sessions.put(run,sid);
        StringBuilder stdout=new StringBuilder(),stderr=new StringBuilder();
        try{
            ChannelExec ch=(ChannelExec)s.openChannel("exec");
            String cwd=directory.isEmpty()?c.directory:directory;
            ch.setCommand((cwd.isEmpty()?"":cwd.equals("~")?"cd && ":"cd "+quote(cwd)+" && ")+command);
            ch.setInputStream(null);InputStream out=ch.getInputStream(),err=ch.getErrStream();
            event("ssh.command.started",run,sid,c,"command",redact(command,c));ch.connect(15000);
            Thread a=pump(out,stdout,c,run,sid,"stdout"),b=pump(err,stderr,c,run,sid,"stderr");
            long deadline=System.nanoTime()+Math.max(1,Math.min(timeout,900))*1_000_000_000L;
            while(!ch.isClosed()){
                if(!s.isConnected())throw new IOException("Cancelled");
                if(System.nanoTime()>deadline)throw new java.net.SocketTimeoutException();Thread.sleep(30);
            }
            a.join(2000);b.join(2000);int exit=ch.getExitStatus();ch.disconnect();
            event("ssh.command.completed",run,sid,c,"exitCode",exit);
            return obj("runID",run,"command",redact(command,c),"stdout",stdout.toString(),"stderr",stderr.toString(),"exitCode",exit);
        }catch(Exception e){event("ssh.command.error",run,sid,c,"message",safeError(e));throw e;}
        finally{s.disconnect();active.remove(run);sessions.remove(run);}
    }
    private static String path(ChannelSftp ch,Connection c,String value) throws Exception {
        if(value.isEmpty()||value.indexOf('\0')>=0)throw new IllegalArgumentException("File path is empty");
        if(value.startsWith("/"))return value;
        String base=c.directory;
        if(base.isEmpty()||base.equals("~")||base.equals("."))base=ch.getHome();
        else if(!base.startsWith("/"))base=ch.getHome()+"/"+base;
        return base.replaceAll("/+$","")+"/"+value;
    }
    private static byte[] read(ChannelSftp ch,String path) throws Exception {
        try(InputStream in=ch.get(path);ByteArrayOutputStream out=new ByteArrayOutputStream()){
            byte[] b=new byte[8192];int n;while((n=in.read(b))>=0){if(out.size()+n>1024*1024)throw new IOException("File too large");out.write(b,0,n);}return out.toByteArray();
        }
    }
    public JSONObject readFile(Connection c,String name) throws Exception {
        Session s=connect(c);try{ChannelSftp ch=(ChannelSftp)s.openChannel("sftp");ch.connect(15000);String p=path(ch,c,name);
            return obj("path",p,"content",redact(new String(read(ch,p),StandardCharsets.UTF_8),c));
        }finally{s.disconnect();}
    }
    public JSONObject writeFile(Connection c,String sid,String name,String content) throws Exception {
        if(content.getBytes(StandardCharsets.UTF_8).length>1024*1024)throw new IOException("File too large");
        Session s=connect(c);try{
            ChannelSftp ch=(ChannelSftp)s.openChannel("sftp");ch.connect(15000);String p=path(ch,c,name),before="";int mode=0644;
            try{before=new String(read(ch,p),StandardCharsets.UTF_8);mode=ch.stat(p).getPermissions()&0777;}catch(SftpException e){if(e.id!=ChannelSftp.SSH_FX_NO_SUCH_FILE)throw e;}
            String temp=p+".codex-"+UUID.randomUUID();
            try{ch.put(new ByteArrayInputStream(content.getBytes(StandardCharsets.UTF_8)),temp);ch.chmod(mode,temp);ch.rename(temp,p);}
            finally{try{ch.rm(temp);}catch(SftpException ignored){}}
            JSONObject patch=patch(p,redact(before,c),redact(content,c));String run=UUID.randomUUID().toString();
            patch.put("runID",run);
            event("ssh.file.updated",run,sid,c,"path",p,"patch",patch.optString("patch"),"additions",patch.optInt("additions"),"deletions",patch.optInt("deletions"));
            return patch;
        }finally{s.disconnect();}
    }
    public static JSONObject patch(String file,String before,String after) {
        if(before.equals(after))return obj("path",file,"patch","","additions",0,"deletions",0);
        String[] a=lines(before),b=lines(after);List<String> diff=new ArrayList<>();int add=0,del=0;
        if((long)a.length*b.length<=1_000_000){
            int[][] lcs=new int[a.length+1][b.length+1];
            for(int i=a.length-1;i>=0;i--)for(int j=b.length-1;j>=0;j--)lcs[i][j]=a[i].equals(b[j])?lcs[i+1][j+1]+1:Math.max(lcs[i+1][j],lcs[i][j+1]);
            int i=0,j=0;while(i<a.length||j<b.length){
                if(i<a.length&&j<b.length&&a[i].equals(b[j])){diff.add(" "+a[i++]);j++;}
                else if(j<b.length&&(i==a.length||lcs[i][j+1]>lcs[i+1][j])){diff.add("+"+b[j++]);add++;}
                else{diff.add("-"+a[i++]);del++;}
            }
        }else{for(String l:a){diff.add("-"+l);del++;}for(String l:b){diff.add("+"+l);add++;}}
        return obj("path",file,"patch","--- "+file+"\n+++ "+file+"\n@@ -"+(a.length==0?0:1)+","+a.length+" +"+(b.length==0?0:1)+","+b.length+" @@\n"+String.join("\n",diff)+"\n","additions",add,"deletions",del);
    }
    private static String[] lines(String s){if(s.isEmpty())return new String[0];return s.replaceAll("\n$","").split("\n",-1);}
}
