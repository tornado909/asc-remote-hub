package ru.ascdev.remotehub;
import java.net.URI;
import java.util.Locale;
final class HubAddress {
 static String normalize(String raw) {
  try {
   String value=raw.trim();
   if(value.isEmpty()||value.length()>2048)throw new IllegalArgumentException();
   if(!value.contains("://"))value="https://"+value;
   URI u=new URI(value);
   if(!"https".equalsIgnoreCase(u.getScheme())||u.getHost()==null||u.getRawUserInfo()!=null||u.getRawQuery()!=null||u.getRawFragment()!=null||!(u.getRawPath().isEmpty()||u.getRawPath().equals("/"))||u.getPort()==0||u.getPort()>65535)throw new IllegalArgumentException();
   return new URI("https",null,u.getHost().toLowerCase(Locale.ROOT),u.getPort()==443?-1:u.getPort(),null,null,null).toASCIIString();
  } catch(Exception e){throw new IllegalArgumentException("Укажите HTTPS-адрес сервера без пути, логина и параметров");}
 }
}
