package ru.ascdev.remotehub;
import org.json.*;import java.net.*;import java.io.*;import java.nio.charset.StandardCharsets;
final class Api {
 static JSONObject obj(Object... kv) throws JSONException {JSONObject o=new JSONObject();for(int i=0;i<kv.length;i+=2)o.put((String)kv[i],kv[i+1]);return o;}
 static class Http extends IOException{final int status;Http(int s){super("HTTP "+s);status=s;}}
 static JSONObject post(String server,String path,JSONObject body,String id,String token) throws Exception {
 HttpURLConnection c=(HttpURLConnection)new URL(HubAddress.normalize(server)+path).openConnection();
 try{c.setInstanceFollowRedirects(false);c.setConnectTimeout(10000);c.setReadTimeout(28000);c.setRequestMethod("POST");c.setDoOutput(true);c.setRequestProperty("Content-Type","application/json");if(id!=null){c.setRequestProperty("X-ASC-Device",id);c.setRequestProperty("Authorization","Bearer "+token);}
 byte[] b=body.toString().getBytes(StandardCharsets.UTF_8);c.setFixedLengthStreamingMode(b.length);try(OutputStream s=c.getOutputStream()){s.write(b);}int status=c.getResponseCode();if(status<200||status>=300)throw new Http(status);
 try(InputStream s=c.getInputStream();ByteArrayOutputStream out=new ByteArrayOutputStream()){byte[] buf=new byte[8192];int n;while((n=s.read(buf))!=-1){if(out.size()+n>9*1024*1024)throw new IOException("Response too large");out.write(buf,0,n);}return new JSONObject(new String(out.toByteArray(),StandardCharsets.UTF_8));}}finally{c.disconnect();}}
}