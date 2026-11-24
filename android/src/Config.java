package ru.ascdev.remotehub;
import android.content.*;import android.security.keystore.*;import android.util.Base64;import java.security.KeyStore;import javax.crypto.*;import javax.crypto.spec.GCMParameterSpec;
final class Config {
 static final String VERSION="0.4.0"; final SharedPreferences p;
 Config(Context c){p=c.getSharedPreferences("hub",0);if(!id().isEmpty()&&!p.contains("server"))p.edit().clear().commit();}
 String server(){return p.getString("server","");}
 String id(){return p.getString("id","");} boolean enabled(){return p.getBoolean("enabled",false);} boolean paired(){return !id().isEmpty();}
 void enabled(boolean b){p.edit().putBoolean("enabled",b).commit();}
 private synchronized javax.crypto.SecretKey key() throws Exception {KeyStore ks=KeyStore.getInstance("AndroidKeyStore");ks.load(null);if(!ks.containsAlias("asc-hub-key")){KeyGenerator g=KeyGenerator.getInstance("AES","AndroidKeyStore");g.init(new KeyGenParameterSpec.Builder("asc-hub-key",KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT).setBlockModes("GCM").setEncryptionPaddings("NoPadding").build());g.generateKey();}return (javax.crypto.SecretKey)ks.getKey("asc-hub-key",null);}
 void save(String server,String id,String token) throws Exception {server=HubAddress.normalize(server);Cipher c=Cipher.getInstance("AES/GCM/NoPadding");c.init(Cipher.ENCRYPT_MODE,key());p.edit().putString("server",server).putString("id",id).putString("iv",Base64.encodeToString(c.getIV(),2)).putString("token",Base64.encodeToString(c.doFinal(token.getBytes("UTF-8")),2)).putBoolean("enabled",true).commit();}
 String token() throws Exception {Cipher c=Cipher.getInstance("AES/GCM/NoPadding");c.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Base64.decode(p.getString("iv",""),2)));return new String(c.doFinal(Base64.decode(p.getString("token",""),2)),"UTF-8");}
 void clear(){p.edit().clear().commit();}
}