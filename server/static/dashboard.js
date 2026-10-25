'use strict';

const HUB_VIEWS=['devices','connect','usage','apps','settings'];
const HUB_TITLES={devices:'Устройства',connect:'Подключение',
  usage:'Статистика',apps:'Приложения',settings:'Настройки'};
let currentView='devices';
function navigate(view,updateHash=true){
  if(!HUB_VIEWS.includes(view))view='devices';
  const changed=currentView!==view;
  currentView=view;
  for(const tab of HUB_VIEWS){
    const shown=tab===view;
    $('view-'+tab).hidden=!shown;
    const nav=$('nav-'+tab);
    nav.classList.toggle('active',shown);
    nav.setAttribute('aria-current',shown?'page':'false');
  }
  if(updateHash && location.hash!=='#'+view)location.hash='#'+view;
  document.title=HUB_TITLES[view]+' · ASC Remote Hub';
  if(changed && view==='apps')refreshOAuthConnections();
  if(changed && view==='usage')refreshUsage();
  if(changed && typeof window!=='undefined' && window.scrollTo)window.scrollTo(0,0);
}
for(const tab of HUB_VIEWS){
  $('nav-'+tab).addEventListener('click',event=>{event.preventDefault();navigate(tab);});
}
if(typeof window!=='undefined'){
  window.addEventListener('hashchange',()=>navigate((location.hash||'#devices').slice(1),false));
}
$('refresh').addEventListener('click',()=>refreshPanel());
$('usage-refresh').addEventListener('click',()=>refreshUsage());
$('pair').addEventListener('click',()=>navigate('connect'));
$('open-apps').addEventListener('click',()=>navigate('apps'));
$('copy-endpoint').addEventListener('click',async()=>{try{
  await navigator.clipboard.writeText(location.origin+'/mcp');
  notice('panel-notice','Адрес MCP скопирован.');
}catch(e){notice('panel-notice','Не удалось скопировать адрес.');}});
$('diagnostic').addEventListener('change',async()=>{try{await api('/api/account/diagnostics',{method:'POST',body:JSON.stringify({enabled:$('diagnostic').checked})});notice('panel-notice','Настройка диагностики сохранена')}catch(e){notice('panel-notice',e.message)}});
$('userbar').addEventListener('click',async e=>{
  if(e.target.id!=='logout')return;
  try{
    await api('/api/logout',{method:'POST',body:'{}'});
    csrf='';
    location.replace('/');
  }catch(error){notice('panel-notice',error.message)}
});
async function refreshOAuthConnections(){
  const target=$('oauth-connections');
  try {
    const data=await api('/api/oauth/connections');
    target.replaceChildren();
    if(!data.connections.length){
      const msg=document.createElement('p');msg.className='sub';
      msg.textContent='Авторизованных MCP-приложений пока нет.';
      target.append(msg);return;
    }
    for(const client of data.connections){
      const row=document.createElement('div');row.className='rowdevice';
      const info=document.createElement('div');info.className='device-body';
      const name=document.createElement('b');
      name.textContent=client.client_name||'MCP-приложение';
      const detail=document.createElement('small');
      detail.textContent='OAuth callback: '+client.callback_hosts.join(', ');
      info.append(name,detail);
      const revoke=document.createElement('button');revoke.className='outline';
      revoke.type='button';revoke.textContent='Отозвать доступ';
      revoke.addEventListener('click',async()=>{
        if(!confirm('Отозвать доступ приложения «'+name.textContent+'»?'))return;
        revoke.disabled=true;
        try{
          await api('/api/oauth/connections/revoke',{
            method:'POST',body:JSON.stringify({client_id:client.client_id})
          });
          await refreshOAuthConnections();
          notice('panel-notice','Доступ MCP-приложения отозван.');
        }catch(err){revoke.disabled=false;notice('panel-notice',err.message)}
      });
      row.append(info,revoke);target.append(row);
    }
  }catch(error){target.replaceChildren();
    const msg=document.createElement('p');msg.className='notice';
    msg.textContent='Ошибка получения MCP-подключений: '+error.message;
    target.append(msg);
  }
}
$('oauth-refresh').addEventListener('click',refreshOAuthConnections);

function deviceEntry(d){
  const node=document.createElement('div');node.className='rowdevice';
  const dot=document.createElement('span');dot.className='device-indicator'+(d.online?' online':'');
  dot.setAttribute('aria-hidden','true');
  const info=document.createElement('div');info.className='device-body';
  const title=document.createElement('b');title.textContent=d.name;
  const subtitle=document.createElement('small');
  const platform=(d.platform||'').toLowerCase().startsWith('windows')?'Windows':
    (d.platform||'').toLowerCase().startsWith('linux')?'Linux':(d.platform||'ОС неизвестна');
  const seen=Number(d.last_seen||0);
  const lastSeen=(!d.online && Number.isFinite(seen) && seen>0)
    ? ' · Последняя связь '+new Date(seen*1000).toLocaleString('ru-RU',{
        day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})
    : '';
  subtitle.textContent=platform+lastSeen+' · '+d.id;
  subtitle.title=d.platform||d.id;
  info.append(title,subtitle);
  const status=document.createElement('span');status.className='pill'+(d.online?' online':'');
  status.textContent=d.online?'В сети':'Не в сети';
  const menu=document.createElement('details');menu.className='device-menu';
  const summary=document.createElement('summary');summary.textContent='⋯';
  summary.setAttribute('aria-label','Действия с устройством '+d.name);
  const items=document.createElement('div');items.className='device-menu-panel';
  const revoke=document.createElement('button');revoke.type='button';
  revoke.textContent='Отозвать доступ';
  revoke.addEventListener('click',async()=>{
    menu.open=false;
    if(!confirm('Отозвать доступ устройства «'+d.name+'» на сервере? Для удаления локальной привязки используйте команду unpair в разделе «Подключение».'))return;
    revoke.disabled=true;
    try{
      await api('/api/devices/revoke',{method:'POST',body:JSON.stringify({device_id:d.id})});
      await refreshPanel();
      notice('panel-notice','Доступ устройства отозван.');
    }catch(e){revoke.disabled=false;notice('panel-notice',e.message);}
  });
  items.append(revoke);menu.append(summary,items);
  node.append(dot,info,status,menu);
  return node;
}
async function refreshPanel(){
  try{
    const dv=await api('/api/devices');
    const devices=dv.devices;
    text('device-count',devices.length);
    text('devices-total',devices.length);
    const elements=$('devices');
    elements.replaceChildren();
    if(!devices.length){
      const p=document.createElement('p');p.className='sub';
      p.textContent='Устройств пока нет. Нажмите «Добавить устройство», чтобы получить код привязки.';
      elements.append(p);
    }
    devices.forEach(d=>elements.append(deviceEntry(d)));
    errorFilterOptions('errors-device',devices.map(d=>({value:d.id,label:d.name})),'Все устройства');
    if(currentView==='usage')await refreshUsage();
    if(currentView==='apps')await refreshOAuthConnections();
  }catch(err){notice('panel-notice',err.message);}
}

async function refreshUsage(){
  try{
    const usage=await api('/api/usage');
    text('total-calls',usage.total.toLocaleString('ru-RU'));
    text('total-errors',usage.errors.toLocaleString('ru-RU'));
    text('errors-header-count',usage.errors.toLocaleString('ru-RU'));
    const rows=$('usage');rows.replaceChildren();
    if(!usage.daily.length){
      const p=document.createElement('p');p.className='sub';
      p.textContent='Первых вызовов ещё не было.';rows.append(p);
    }
    const maxCalls=Math.max(1,...usage.daily.map(row=>row.calls));
    usage.daily.slice(0,16).forEach(row=>{
      const el=document.createElement('div');el.className='usage-row';
      const label=document.createElement('span');label.className='usage-info';
      label.textContent=row.day+' · '+row.tool;
      const bar=document.createElement('span');bar.className='usage-bar';bar.setAttribute('aria-hidden','true');
      const fill=document.createElement('span');
      fill.style.width=Math.max(1,Math.round(row.calls/maxCalls*100))+'%';bar.append(fill);
      const num=document.createElement('span');num.className='usage-value';
      const count=document.createElement('strong');count.className='usage-count';
      count.textContent=row.calls.toLocaleString('ru-RU');num.append(count);
      if(row.errors){
        const err=document.createElement('span');err.className='usage-errors';
        err.textContent=row.errors===1?'1 ошибка':row.errors+' ош.';
        num.append(err);
      }
      el.append(label,bar,num);rows.append(el);
    });
    const errorTools=[...new Set(usage.daily.filter(x=>x.errors>0).map(x=>x.tool))].sort();
    errorFilterOptions('errors-tool',errorTools.map(x=>({value:x,label:x})),'Все инструменты');
    if(currentView==='usage' && $('errors-section').open)await refreshErrors();
  }catch(err){notice('panel-notice',err.message);}
}

const ERROR_PAGE_SIZE=15;
let errorOffset=0;
let currentErrorTotal=0;

function errorNode(tag,className,value){
  const el=document.createElement(tag);
  if(className)el.className=className;
  if(value!==undefined)el.textContent=String(value);
  return el;
}
function errorDate(epoch){
  return new Date(Number(epoch)*1000).toLocaleString('ru-RU',{
    day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'
  });
}
function errorFilterOptions(id,items,firstLabel){
  const select=$(id);
  const current=select.value;
  select.replaceChildren(new Option(firstLabel,''));
  items.forEach(item=>select.append(new Option(item.label,item.value)));
  select.value=Array.from(select.options).some(o=>o.value===current)?current:'';
}
function renderErrorRow(entry){
  const details=errorNode('details','error-row');
  const summary=errorNode('summary','error-summary');
  const top=errorNode('div','error-row-top');
  top.append(
    errorNode('span','error-date',errorDate(entry.created)),
    errorNode('strong','error-tool',entry.tool),
    errorNode('span','error-device-name',entry.device_name||'Хаб')
  );
  const reason=errorNode('div','error-message',entry.message);
  summary.append(top,reason);
  const body=errorNode('div','error-body');
  const meta=errorNode('div','error-meta');
  meta.append(
    errorNode('div','','Тип: '+entry.error_type),
    errorNode('div','','Источник: '+(entry.source==='agent'?'Агент устройства':'Хаб')),
    errorNode('div','','Ошибка: '+entry.id),
    errorNode('div','','Запрос: '+(entry.request_id||'нет идентификатора')),
    errorNode('div','','Задание агента: '+(entry.job_id||'нет'))
  );
  body.append(meta);
  const load=errorNode('button','outline','Показать запрос и ответ');
  load.type='button';
  const result=errorNode('div','error-trace-results');
  load.addEventListener('click',async()=>{
    load.disabled=true;
    result.replaceChildren(errorNode('p','sub','Загружаем полную трассировку…'));
    try{
      const data=await api('/api/errors/'+encodeURIComponent(entry.id)+'/trace');
      result.replaceChildren();
      if(!data.available){
        result.append(errorNode('p','sub',data.reason||'Сохранённая трассировка для этого вызова недоступна.'));
      } else {
        data.records.forEach((item,index)=>{
          const block=errorNode('details','trace-record');
          const label=errorNode('summary','',(index+1)+'. '+item.direction+' · '+item.route+' · '+errorDate(Date.parse(item.utc)/1000));
          const content=errorNode('pre','trace-payload',JSON.stringify(item.payload,null,2));
          block.append(label,content);
          result.append(block);
        });
        if(data.limited){
          result.append(errorNode('p','sub','Объём ответа ограничен. Полные данные сохранены в защищённом журнале на сервере.'));
        }
      }
    }catch(err){
      result.replaceChildren(errorNode('p','notice','Не удалось загрузить подробности: '+err.message));
    }finally{
      load.disabled=false;
    }
  });
  body.append(load,result);
  details.append(summary,body);
  return details;
}

async function refreshErrors(){
  const params=new URLSearchParams({
    days:$('errors-days').value,
    limit:String(ERROR_PAGE_SIZE),
    offset:String(errorOffset)
  });
  if($('errors-device').value)params.set('device',$('errors-device').value);
  if($('errors-tool').value)params.set('tool',$('errors-tool').value);
  const host=$('errors-list');
  try{
    const data=await api('/api/errors?'+params.toString());
    currentErrorTotal=data.total;
    host.replaceChildren();
    if(!data.events.length){
      host.append(errorNode('p','sub','Ошибок за выбранный период нет.'));
    }else{
      data.events.forEach(entry=>host.append(renderErrorRow(entry)));
    }
    const shown=data.events.length?(data.offset+1)+'–'+(data.offset+data.events.length):'0';
    text('errors-page-status',shown+' из '+data.total);
    $('errors-prev').disabled=errorOffset===0;
    $('errors-next').disabled=!data.has_more;
  }catch(err){
    host.replaceChildren(errorNode('p','notice','Ошибка получения журнала: '+err.message));
  }
}

$('goto-errors').addEventListener('click',()=>{navigate('usage');$('errors-section').open=true;$('errors-section').scrollIntoView({behavior:'smooth'});});
$('errors-section').addEventListener('toggle',()=>{if($('errors-section').open)refreshErrors();});
$('errors-refresh').addEventListener('click',refreshErrors);
for(const id of ['errors-device','errors-tool','errors-days']){
  $(id).addEventListener('change',()=>{errorOffset=0;refreshErrors();});
}
$('errors-prev').addEventListener('click',()=>{errorOffset=Math.max(0,errorOffset-ERROR_PAGE_SIZE);refreshErrors();});
$('errors-next').addEventListener('click',()=>{if(errorOffset+ERROR_PAGE_SIZE<currentErrorTotal){errorOffset+=ERROR_PAGE_SIZE;refreshErrors();}});

async function reload(){
  try {
    const me=await api('/api/me');
    if(!me.authenticated){location.replace('/');return;}
    csrf=me.csrf;
    text('email',me.email);
    text('account-email',me.email);
    $('diagnostic').checked=me.diagnostic;
    $('userbar').replaceChildren();
    const logout=document.createElement('button');
    logout.id='logout';logout.className='outline';logout.textContent='Выйти';
    $('userbar').append(logout);
    text('endpoint',location.origin+'/mcp');
    text('mcp-example','"asc-remote-hub": {"url": "'+location.origin+'/mcp"}');
    $('admin-panel').hidden=!me.is_admin;
    if(me.is_admin){
      const data=await api('/api/admin/summary');
      text('admin-stats','Пользователей: '+data.users+' · Устройств: '+data.devices+
        ' · В сети: '+data.online_devices+' · Вызовов: '+data.tool_calls+' · Ошибок: '+data.tool_errors);
    }
    await refreshPanel();
    navigate((location.hash||'#devices').replace(/^#/,''),false);
  } catch(err) {
    notice('panel-notice',err.message);
  }
}
reload();
