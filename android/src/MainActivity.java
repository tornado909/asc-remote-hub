package ru.ascdev.remotehub;

import android.app.*;
import android.content.*;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.view.*;
import android.widget.*;
import org.json.JSONObject;

public final class MainActivity extends Activity {
    private static final int BG=0xff10151c, CARD=0xff1b2430, TEXT=0xffedf3fa, MUTED=0xffa8b6c6, BLUE=0xff8dc8ff, GREEN=0xff75d9ad, AMBER=0xffffc47e;
    private Config cfg;
    private LinearLayout root, body, checks;
    private TextView state, summary;
    private Button primary, secondary;
    private EditText code, name, server;
    private String serverDraft="";
    private boolean busy, accessPage;
    private int step;
    private final Handler h=new Handler(Looper.getMainLooper());
    private final Runnable ticker=new Runnable(){public void run(){update();h.postDelayed(this,1000);}};
    private int dp(float v){return (int)(v*getResources().getDisplayMetrics().density+0.5f);}
    private GradientDrawable shape(int color,int radius){GradientDrawable g=new GradientDrawable();g.setColor(color);g.setCornerRadius(dp(radius));return g;}
    private LinearLayout column(){LinearLayout l=new LinearLayout(this);l.setOrientation(1);return l;}
    private TextView text(String value,int size,int color,boolean bold){TextView t=new TextView(this);t.setText(value);t.setTextSize(size);t.setTextColor(color);if(bold)t.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));return t;}
    private void gap(LinearLayout l,int n){View v=new View(this);l.addView(v,new LinearLayout.LayoutParams(1,dp(n)));}
    private void line(LinearLayout l,String s,int size,int c,boolean b){l.addView(text(s,size,c,b));}
    private LinearLayout card(LinearLayout l){LinearLayout v=column();v.setPadding(dp(18),dp(18),dp(18),dp(18));v.setBackground(shape(CARD,20));l.addView(v,new LinearLayout.LayoutParams(-1,-2));return v;}
    private Button button(LinearLayout l,String label,boolean accent,Runnable action){Button b=new Button(this);b.setAllCaps(false);b.setText(label);b.setTextSize(15);b.setTextColor(accent?BG:TEXT);b.setBackgroundTintList(null);b.setBackground(shape(accent?BLUE:0xff2a3645,14));b.setMinHeight(dp(52));b.setPadding(dp(12),dp(10),dp(12),dp(10));l.addView(b,new LinearLayout.LayoutParams(-1,-2));b.setOnClickListener(v->action.run());return b;}
    private void section(String title,String sub){line(body,title,30,TEXT,true);if(!sub.isEmpty()){gap(body,8);line(body,sub,16,MUTED,false);}gap(body,24);}
    private String deviceName(){return Build.MANUFACTURER.equalsIgnoreCase("google")?"Google "+Build.MODEL:Build.MANUFACTURER+" "+Build.MODEL;}
    private boolean ui(){return UiService.instance!=null;}
    private boolean files(){return Environment.isExternalStorageManager();}
    private boolean battery(){return getSystemService(PowerManager.class).isIgnoringBatteryOptimizations(getPackageName());}
    private boolean notifications(){return getSystemService(NotificationManager.class).areNotificationsEnabled()&&(Build.VERSION.SDK_INT<33||checkSelfPermission("android.permission.POST_NOTIFICATIONS")==0);}
    private boolean ready(){return ui()&&files()&&battery()&&notifications();}
    private void settings(String action,boolean pkg){try{startActivity(new Intent(action,pkg?Uri.parse("package:"+getPackageName()):null));}catch(Exception e){startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName())));}}
    private void notifyPermission(){if(Build.VERSION.SDK_INT>=33&&checkSelfPermission("android.permission.POST_NOTIFICATIONS")!=0){requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"},1);}else{startActivity(new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE,getPackageName()));}}
    @Override public void onCreate(Bundle saved){super.onCreate(saved);cfg=new Config(this);step=saved==null?0:saved.getInt("step");accessPage=saved!=null&&saved.getBoolean("access");serverDraft=saved==null?"":saved.getString("serverDraft","");getWindow().setDecorFitsSystemWindows(false);getWindow().setStatusBarColor(BG);getWindow().setNavigationBarColor(BG);getWindow().getDecorView().setSystemUiVisibility(0);render();if(cfg.paired()&&cfg.enabled())startHub();}
    @Override protected void onSaveInstanceState(Bundle out){super.onSaveInstanceState(out);out.putInt("step",step);out.putBoolean("access",accessPage);out.putString("serverDraft",server==null?serverDraft:server.getText().toString());}
    private void render(){
        state=null;summary=null;primary=null;secondary=null;checks=null;code=null;name=null;server=null;
        ScrollView sc=new ScrollView(this);sc.setFillViewport(true);sc.setBackgroundColor(BG);
        root=column();root.setPadding(dp(24),dp(20),dp(24),dp(24));sc.addView(root);
        sc.setOnApplyWindowInsetsListener((v,in)->{android.graphics.Insets z=in.getInsets(WindowInsets.Type.systemBars()|WindowInsets.Type.displayCutout());android.graphics.Insets ime=in.getInsets(WindowInsets.Type.ime());root.setPadding(dp(24)+z.left,dp(20)+z.top,dp(24)+z.right,dp(24)+Math.max(z.bottom,ime.bottom));return in;});
        setContentView(sc);sc.requestApplyInsets();
        LinearLayout top=new LinearLayout(this);top.setGravity(Gravity.CENTER_VERTICAL);root.addView(top);
        TextView mark=text("＋",24,BLUE,true);mark.setGravity(Gravity.CENTER);mark.setBackground(shape(0xff23394f,13));top.addView(mark,new LinearLayout.LayoutParams(dp(42),dp(42)));
        TextView brand=text("ASC Remote Hub",19,TEXT,true);LinearLayout.LayoutParams bp=new LinearLayout.LayoutParams(0,-2,1);bp.leftMargin=dp(12);top.addView(brand,bp);
        if(cfg.paired()&&!accessPage){TextView more=text("⋮",28,MUTED,true);more.setGravity(Gravity.CENTER);more.setContentDescription("Дополнительно");top.addView(more,new LinearLayout.LayoutParams(dp(44),dp(48)));more.setOnClickListener(v->menu(more));}
        gap(root,32);body=column();root.addView(body);
        if(cfg.paired()&&!accessPage){dashboard();}
        else if(accessPage||step==1){permissions();}
        else if(step==2){if(!ready()){step=1;permissions();}else pairing();}
        else welcome();
        update();
    }
    private void welcome(){line(body,"ШАГ 1 ИЗ 3",12,BLUE,true);gap(body,12);section("Ваше устройство.\nПод вашим управлением.","Подключите Android к ASC Remote Hub для работы с приложениями и файлами из вашей учётной записи.");LinearLayout c=card(body);line(c,"Удалённая работа",20,TEXT,true);gap(c,10);line(c,"Приложения • Файлы • Состояние устройства",15,MUTED,false);gap(c,12);line(c,"Доступ можно остановить в любой момент.",14,MUTED,false);gap(body,24);button(body,"Начать настройку",true,()->{step=1;render();});}
    private void permissions(){line(body,accessPage?"ДОСТУПЫ":"ШАГ 2 ИЗ 3",12,BLUE,true);gap(body,12);section("Настройка устройства","Предоставьте разрешения. После возврата из настроек они проверяются автоматически.");checks=column();body.addView(checks);renderChecks();gap(body,18);primary=button(body,accessPage?"Готово":"Продолжить",true,()->{if(accessPage){accessPage=false;render();}else if(ready()){step=2;render();}});gap(body,10);button(body,"Назад",false,()->{if(accessPage)accessPage=false;else step=0;render();});}
    private void permissionRow(String title,String description,boolean done,Runnable action){LinearLayout c=card(checks);line(c,(done?"✓  ":"○  ")+title,17,done?GREEN:TEXT,true);gap(c,5);line(c,description,14,MUTED,false);if(!done){gap(c,12);button(c,"Настроить",false,action);}gap(checks,10);}
    private int mask=-1;
    private void renderChecks(){if(checks==null)return;mask=(ui()?1:0)|(files()?2:0)|(battery()?4:0)|(notifications()?8:0);checks.removeAllViews();permissionRow("Управление приложениями","Включите ASC Remote Hub в специальных возможностях. Если Android блокирует разрешение, разрешите ограниченные настройки в сведениях приложения.",ui(),()->settings(Settings.ACTION_ACCESSIBILITY_SETTINGS,false));permissionRow("Общие файлы","Разрешите доступ к общим файлам устройства.",files(),()->settings(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,true));permissionRow("Фоновая работа","Выберите для ASC Remote Hub режим без ограничений батареи.",battery(),()->settings(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,true));permissionRow("Уведомления","Показывают, когда удалённое подключение активно.",notifications(),this::notifyPermission);}
    private EditText field(String hint,String value,int type){EditText e=new EditText(this);e.setSingleLine(true);e.setTextSize(17);e.setTextColor(TEXT);e.setHintTextColor(MUTED);e.setHint(hint);e.setInputType(type);e.setText(value);e.setPadding(dp(16),dp(12),dp(16),dp(12));e.setBackground(shape(CARD,14));body.addView(e,new LinearLayout.LayoutParams(-1,dp(60)));return e;}
    private void pairing(){line(body,"ШАГ 3 ИЗ 3",12,BLUE,true);gap(body,12);section("Подключение","Получите одноразовый код в личном кабинете Remote Hub.");server=field("https://hub.example.org",serverDraft,17);gap(body,6);line(body,"Адрес вашего сервера",14,MUTED,false);gap(body,14);name=field("Имя устройства",deviceName(),1);gap(body,12);code=field("Код привязки","",129);gap(body,20);primary=button(body,"Подключить устройство",true,()->{if(!ready()){step=1;render();return;}String addr=selectedServer();if(addr==null)return;new AlertDialog.Builder(this).setTitle("Подключить устройство?").setMessage(addr+"\n\nАгенты вашей учётной записи получат доступ к приложениям и разрешённым файлам. Вы можете отключить управление в любой момент.").setNegativeButton("Отмена",null).setPositiveButton("Подключить",(a,b)->enroll()).show();});gap(body,10);button(body,"Получить код",false,()->{String addr=selectedServer();if(addr!=null)startActivity(new Intent(Intent.ACTION_VIEW,Uri.parse(addr)));});gap(body,10);button(body,"Назад",false,()->{if(!busy){serverDraft=server.getText().toString();step=1;render();}});}
    private void dashboard(){section(deviceName(),"");LinearLayout c=card(body);line(c,"СОСТОЯНИЕ",12,MUTED,true);gap(c,12);state=text("",27,GREEN,true);c.addView(state);gap(c,8);summary=text("",14,MUTED,false);c.addView(summary);gap(body,22);primary=button(body,"Отключить",true,()->{if(cfg.enabled()){cfg.enabled(false);stopService(new Intent(this,HubService.class));}else{cfg.enabled(true);startHub();}update();});gap(body,10);secondary=button(body,"Управление доступами",false,()->{accessPage=true;render();});}
    private void menu(View anchor){PopupMenu p=new PopupMenu(this,anchor);p.getMenu().add("Сведения");p.getMenu().add("Отвязать устройство");p.setOnMenuItemClickListener(i->{if(i.getTitle().equals("Сведения"))new AlertDialog.Builder(this).setTitle("ASC Remote Hub").setMessage("Версия "+Config.VERSION+"\n"+cfg.server()).setPositiveButton("Закрыть",null).show();else new AlertDialog.Builder(this).setTitle("Отвязать устройство?").setMessage("Для повторного подключения потребуется новый код.").setNegativeButton("Отмена",null).setPositiveButton("Отвязать",(a,b)->unpair()).show();return true;});p.show();}
    private void update(){if(cfg==null)return;if(checks!=null){int now=(ui()?1:0)|(files()?2:0)|(battery()?4:0)|(notifications()?8:0);if(mask!=now)renderChecks();if(primary!=null)primary.setEnabled(accessPage||ready());}if(state!=null){String s=cfg.enabled()?HubService.status:"Отключено";state.setText(s);state.setTextColor(s.equals("Подключено")?GREEN:cfg.enabled()?AMBER:MUTED);summary.setText(ready()?"Устройство готово к удалённой работе":"Некоторые разрешения требуют настройки");primary.setText(cfg.enabled()?"Отключить":"Подключиться");primary.setEnabled(!busy);secondary.setEnabled(!busy);}else if(code!=null&&primary!=null){primary.setEnabled(!busy&&ready());primary.setText(busy?"Подключение…":"Подключить устройство");}}
    private void startHub(){try{startForegroundService(new Intent(this,HubService.class));}catch(Exception e){Toast.makeText(this,"Не удалось запустить подключение",Toast.LENGTH_LONG).show();}}
    @Override protected void onResume(){super.onResume();h.removeCallbacks(ticker);h.post(ticker);}
    @Override protected void onPause(){h.removeCallbacks(ticker);super.onPause();}
    @Override public void onRequestPermissionsResult(int r,String[] p,int[] g){super.onRequestPermissionsResult(r,p,g);update();}
    private String selectedServer(){try{return HubAddress.normalize(server.getText().toString());}catch(IllegalArgumentException e){server.setError(e.getMessage());return null;}}
    private void enroll(){if(busy||cfg.paired()||!ready())return;String addr=selectedServer();if(addr==null)return;String c=code.getText().toString().trim(),n=name.getText().toString().trim();if(c.isEmpty()||n.isEmpty()){Toast.makeText(this,"Укажите код и имя",Toast.LENGTH_SHORT).show();return;}busy=true;server.setEnabled(false);update();new Thread(()->{try{JSONObject r=Api.post(addr,"/agent/enroll",Api.obj("pairing_code",c,"name",n,"platform",Tools.platform(),"tools",Tools.names()),null,null);cfg.save(addr,r.getString("device_id"),r.getString("device_token"));runOnUiThread(()->{busy=false;accessPage=false;startHub();render();});}catch(Exception e){runOnUiThread(()->{busy=false;if(server!=null)server.setEnabled(true);update();Toast.makeText(this,"Не удалось подключить. Проверьте код и связь.",Toast.LENGTH_LONG).show();});}}).start();}
    private void unpair(){if(busy)return;busy=true;cfg.enabled(false);stopService(new Intent(this,HubService.class));update();new Thread(()->{try{Api.post(cfg.server(),"/agent/unpair",Api.obj("confirm_device_id",cfg.id()),cfg.id(),cfg.token());cfg.clear();}catch(Api.Http e){if(e.status==401)cfg.clear();else runOnUiThread(()->Toast.makeText(this,"Сервер не подтвердил отзыв",1).show());}catch(Exception e){runOnUiThread(()->Toast.makeText(this,"Сервер недоступен, подключение остановлено",1).show());}finally{runOnUiThread(()->{busy=false;step=0;accessPage=false;serverDraft="";render();});}}).start();}
}
