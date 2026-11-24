package ru.ascdev.remotehub;
import android.accessibilityservice.*;import android.app.*;import android.content.*;import android.graphics.Rect;import android.graphics.Point;import android.graphics.Path;import android.view.Display;import android.view.WindowManager;import android.os.*;import android.view.accessibility.*;import org.json.*;import java.util.*;import java.util.concurrent.*;
public final class UiService extends AccessibilityService {
 static volatile UiService instance;final Handler main=new Handler(Looper.getMainLooper());final ArrayList<AccessibilityNodeInfo> nodes=new ArrayList<>();String snapshot="",pkg="";long expires;int window;int screenWidth,screenHeight,rotation;
 protected void onServiceConnected(){instance=this;}public void onAccessibilityEvent(AccessibilityEvent e){}public void onInterrupt(){}public void onDestroy(){instance=null;clear();super.onDestroy();}
 void clear(){for(AccessibilityNodeInfo n:nodes)n.recycle();nodes.clear();snapshot="";}
 void check(){if(!new Config(this).enabled())throw new SecurityException("Remote control stopped");if(getSystemService(KeyguardManager.class).isKeyguardLocked()||!getSystemService(PowerManager.class).isInteractive())throw new SecurityException("Device must be unlocked and screen on");}
 <T>T call(Callable<T> c)throws Exception{FutureTask<T> f=new FutureTask<>(c);main.post(f);try{return f.get(5,TimeUnit.SECONDS);}catch(Exception e){f.cancel(false);throw e;}}
 static final int MAX_NODES=200, MAX_DEPTH=100, MAX_VISITS=2400;
 static final class Candidate {final AccessibilityNodeInfo n;final int depth,rank;Candidate(AccessibilityNodeInfo n,int depth,int rank){this.n=AccessibilityNodeInfo.obtain(n);this.depth=depth;this.rank=rank;}}
 int visits;boolean limited;long walkDeadline;
 JSONObject tree()throws Exception{return call(()->{
  check();clear();AccessibilityNodeInfo root=getRootInActiveWindow();if(root==null)throw new IllegalStateException("No active window");
  pkg=String.valueOf(root.getPackageName());window=root.getWindowId();Point size=displaySize();screenWidth=size.x;screenHeight=size.y;rotation=display().getRotation();snapshot=UUID.randomUUID().toString();
  ArrayList<Candidate> candidates=new ArrayList<>();JSONArray a=new JSONArray();visits=0;limited=false;walkDeadline=SystemClock.elapsedRealtime()+3000;
  try{
   walk(root,candidates,0);
   if(candidates.size()>MAX_NODES){limited=true;ArrayList<Candidate> ranked=new ArrayList<>(candidates);ranked.sort(Comparator.comparingInt(c->c.rank));Set<Candidate> keep=new HashSet<>(ranked.subList(0,MAX_NODES));for(Candidate c:candidates)if(keep.contains(c))emit(c,a);}
   else for(Candidate c:candidates)emit(c,a);
  }finally{root.recycle();for(Candidate c:candidates)c.n.recycle();}
  expires=SystemClock.elapsedRealtime()+30000;
  return Api.obj("snapshot",snapshot,"package",pkg,"nodes",a,"truncated",limited,"visited",visits,"screen_width",screenWidth,"screen_height",screenHeight,"rotation",rotation);
 });}
 void walk(AccessibilityNodeInfo n,ArrayList<Candidate> out,int depth){
  if(depth>MAX_DEPTH||visits>=MAX_VISITS||SystemClock.elapsedRealtime()>walkDeadline){limited=true;return;}visits++;
  boolean password=n.isPassword();
  if(n.isVisibleToUser()){
   boolean editable=n.isEditable()&&!password, action=n.isClickable()||n.isLongClickable()||n.isScrollable();
   boolean labelled=!password&&(!shortText(n.getText()).isEmpty()||!shortText(n.getContentDescription()).isEmpty()||!shortText(n.getViewIdResourceName()).isEmpty());
   if(editable||action||labelled||password||depth==0)out.add(new Candidate(n,depth,editable?0:action?1:labelled?2:3));
  }
  if(password)return;
  for(int k=0;k<n.getChildCount();k++){if(visits>=MAX_VISITS||SystemClock.elapsedRealtime()>walkDeadline){limited=true;break;}AccessibilityNodeInfo child=n.getChild(k);if(child!=null)try{walk(child,out,depth+1);}finally{child.recycle();}}
 }
 void emit(Candidate c,JSONArray a)throws Exception{
  AccessibilityNodeInfo n=c.n;int i=nodes.size();nodes.add(AccessibilityNodeInfo.obtain(n));Rect r=new Rect();n.getBoundsInScreen(r);boolean password=n.isPassword();String text=password?"":shortText(n.getText()),desc=password?"":shortText(n.getContentDescription());
  a.put(Api.obj("id",i,"depth",c.depth,"class",shortText(n.getClassName()),"resource_id",shortText(n.getViewIdResourceName()),"text",text,"description",desc,"bounds",new JSONArray(new int[]{r.left,r.top,r.right,r.bottom}),"clickable",n.isClickable(),"editable",n.isEditable()&&!password,"scrollable",n.isScrollable(),"enabled",n.isEnabled(),"password",password));
 }
 String shortText(CharSequence c){if(c==null)return "";String s=c.toString();return s.substring(0,Math.min(300,s.length()));}
 JSONObject action(JSONObject a)throws Exception{return call(()->{check();String act=a.getString("action");if(act.equals("back")||act.equals("home")||act.equals("recents"))return Api.obj("ok",performGlobalAction(act.equals("back")?GLOBAL_ACTION_BACK:act.equals("home")?GLOBAL_ACTION_HOME:GLOBAL_ACTION_RECENTS));if(!snapshot.equals(a.optString("snapshot"))||SystemClock.elapsedRealtime()>expires)throw new IllegalArgumentException("Refresh snapshot");AccessibilityNodeInfo root=getRootInActiveWindow();if(root==null)throw new IllegalStateException("No active window");try{if(root.getWindowId()!=window||!pkg.equals(String.valueOf(root.getPackageName())))throw new IllegalStateException("Window changed; refresh snapshot");}finally{root.recycle();}AccessibilityNodeInfo n=nodes.get(a.getInt("node_id"));String oldText=shortText(n.getText()),oldDesc=shortText(n.getContentDescription()),oldId=shortText(n.getViewIdResourceName());if(!n.refresh()||!oldText.equals(shortText(n.getText()))||!oldDesc.equals(shortText(n.getContentDescription()))||!oldId.equals(shortText(n.getViewIdResourceName()))||!n.isVisibleToUser()||!n.isEnabled()||n.isPassword())throw new IllegalStateException("Node unavailable or changed");boolean ok;switch(act){case "click":ok=n.performAction(AccessibilityNodeInfo.ACTION_CLICK);break;case "long_click":ok=n.performAction(AccessibilityNodeInfo.ACTION_LONG_CLICK);break;case "scroll_forward":ok=n.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD);break;case "scroll_backward":ok=n.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD);break;case "set_text":if(!n.isEditable())throw new IllegalArgumentException("Not editable");Bundle b=new Bundle();String text=a.getString("text");if(text.length()>4000)throw new IllegalArgumentException("Text too long");b.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,text);ok=n.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,b);break;default:throw new IllegalArgumentException("Unknown action");}clear();return Api.obj("ok",ok);});}
 Display display(){return getSystemService(WindowManager.class).getDefaultDisplay();}
 Point displaySize(){Point p=new Point();display().getRealSize(p);return p;}
 void verifyGestureTarget(JSONObject a)throws Exception{
  check();
  if(snapshot.isEmpty()||!snapshot.equals(a.getString("snapshot"))||SystemClock.elapsedRealtime()>expires)throw new IllegalArgumentException("Refresh snapshot before gesture");
  Point size=displaySize();if(size.x!=screenWidth||size.y!=screenHeight||display().getRotation()!=rotation)throw new IllegalStateException("Screen geometry changed; refresh snapshot");
  AccessibilityNodeInfo root=getRootInActiveWindow();if(root==null)throw new IllegalStateException("No active window");
  try{if(root.getWindowId()!=window||!pkg.equals(String.valueOf(root.getPackageName())))throw new IllegalStateException("Active window changed; refresh snapshot");}finally{root.recycle();}
  if((getServiceInfo().getCapabilities()&AccessibilityServiceInfo.CAPABILITY_CAN_PERFORM_GESTURES)==0)throw new IllegalStateException("Gesture capability unavailable; re-enable accessibility service");
 }
 JSONObject gesture(JSONObject a)throws Exception{
  CompletableFuture<String> completion=new CompletableFuture<>();
  boolean accepted=call(()->{
   verifyGestureTarget(a);
   JSONArray strokes=a.getJSONArray("strokes");if(strokes.length()<1||strokes.length()>5)throw new IllegalArgumentException("Use 1 to 5 strokes");
   GestureDescription.Builder builder=new GestureDescription.Builder();
   for(int i=0;i<strokes.length();i++){
    JSONObject st=strokes.getJSONObject(i);JSONArray points=st.getJSONArray("points");
    if(points.length()<1||points.length()>64)throw new IllegalArgumentException("Use 1 to 64 points per stroke");
    long start=st.optLong("start_ms",0),duration=st.getLong("duration_ms");
    if(start<0||start>1000||duration<1||duration>3000||start+duration>4000)throw new IllegalArgumentException("Invalid gesture timing");
    Path path=new Path();
    for(int j=0;j<points.length();j++){
     JSONObject pt=points.getJSONObject(j);double x=pt.getDouble("x"),y=pt.getDouble("y");
     if(!Double.isFinite(x)||!Double.isFinite(y)||x<0||y<0||x>=screenWidth||y>=screenHeight)throw new IllegalArgumentException("Point outside current screen");
     if(j==0)path.moveTo((float)x,(float)y);else path.lineTo((float)x,(float)y);
    }
    builder.addStroke(new GestureDescription.StrokeDescription(path,start,duration));
   }
   GestureDescription g=builder.build();
   boolean ok=dispatchGesture(g,new GestureResultCallback(){
    @Override public void onCompleted(GestureDescription description){completion.complete("completed");}
    @Override public void onCancelled(GestureDescription description){completion.complete("cancelled");}
   },main);
   clear();return ok;
  });
  if(!accepted)return Api.obj("ok",false,"status","rejected");
  try{String status=completion.get(6,TimeUnit.SECONDS);return Api.obj("ok",status.equals("completed"),"status",status);}
  catch(TimeoutException e){return Api.obj("ok",false,"status","unknown_timeout","message","Do not retry blindly; refresh the UI first");}
 }
 JSONObject launch(Intent i)throws Exception{return call(()->{check();startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));return Api.obj("launch_requested",true);});}
}