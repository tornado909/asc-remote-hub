package ru.ascdev.remotehub;
public final class HubAddressTest {
 static void good(String input,String expected){if(!HubAddress.normalize(input).equals(expected))throw new AssertionError(input);}
 static void bad(String input){try{HubAddress.normalize(input);throw new AssertionError(input);}catch(IllegalArgumentException expected){}}
 public static void main(String[] args){
  good("hub.example.org","https://hub.example.org");good(" HTTPS://HUB.example.org:443/ ","https://hub.example.org");good("https://192.0.2.10:8443","https://192.0.2.10:8443");good("https://[::1]:8443/","https://[::1]:8443");
  for(String s:new String[]{"","http://hub.example.org","https://user:pass@hub.example.org","https://hub.example.org/path","https://hub.example.org?q=x","https://hub.example.org/#x","https://hub.example.org:65536","https://hub.example.org:0","https://","https://bad host"})bad(s);
  System.out.println("14 address tests passed");
 }
}
