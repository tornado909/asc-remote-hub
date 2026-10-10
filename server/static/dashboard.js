'use strict';
$('refresh').addEventListener('click',()=>refreshPanel());
$('pair').addEventListener('click',issuePairCode);
$('diagnostic').addEventListener('change',async()=>{try{await api('/api/account/diagnostics',{method:'POST',body:JSON.stringify({enabled:$('diagnostic').checked})});notice('panel-notice','Настройка диагностики сохранена')}catch(e){notice('panel-notice',e.message)}});
$('userbar').addEventListener('click',async e=>{
  if(e.target.id!=='logout')return;
  try{
    await api('/api/logout',{method:'POST',body:'{}'});
    csrf='';
    location.replace('/');
  }catch(error){notice('panel-notice',error.message)}
});
function deviceEntry(d){const node=document.createElement('div');node.className='rowdevice';const icon=document.createElement('div');icon.textContent='▣';icon.style.fontSize='28px';icon.style.color='#8bc5ff';const info=document.createElement('div');info.className='device-body';const title=document.createElement('b');title.textContent=d.name;const subtitle=document.createElement('small');subtitle.textContent=d.id+' · '+d.platform;info.append(title,subtitle);const status=document.createElement('span');status.className='pill'+(d.online?' online':'');status.textContent=d.online?'В сети':'Не в сети';const revoke=document.createElement('button');revoke.textContent='Отвязать';revoke.className='outline';revoke.addEventListener('click',async()=>{if(!confirm('Отключить устройство '+d.name+'?'))return;try{await api('/api/devices/revoke',{method:'POST',body:JSON.stringify({device_id:d.id})});await refreshPanel()}catch(e){notice('panel-notice',e.message)}});node.append(icon,info,status,revoke);return node;}
async function refreshPanel(){try{
 const [dv,usage]=await Promise.all([api('/api/devices'),api('/api/usage')]);text('total-calls',usage.total.toLocaleString('ru-RU'));text('total-errors',usage.errors.toLocaleString('ru-RU'));text('errors-header-count',usage.errors.toLocaleString('ru-RU'));text('device-count',dv.devices.length);
 const elements=$('devices');elements.replaceChildren();if(!dv.devices.length){const p=document.createElement('p');p.textContent='Устройств пока нет. Нажми «Добавить устройство», чтобы получить одноразовый код привязки.';elements.append(p);}dv.devices.forEach(d=>elements.append(deviceEntry(d)));
 const rows=$('usage');rows.replaceChildren();if(!usage.daily.length){const p=document.createElement('p');p.textContent='Первых вызовов ещё не было.';rows.append(p);}usage.daily.slice(0,40).forEach(row=>{const el=document.createElement('div');el.className='usage-row';const a=document.createElement('span');a.textContent=row.day+' · '+row.tool;const b=document.createElement('b');b.textContent=row.calls+(row.errors?' / ошибок: '+row.errors:'');el.append(a,b);rows.append(el);});
 errorFilterOptions('errors-device',dv.devices.map(d=>({value:d.id,label:d.name})),'Все устройства');
 const errorTools=[...new Set(usage.daily.filter(x=>x.errors>0).map(x=>x.tool))].sort();
 errorFilterOptions('errors-tool',errorTools.map(x=>({value:x,label:x})),'Все инструменты');
 await refreshErrors();
 }catch(err){notice('panel-notice',err.message)}}

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

$('goto-errors').addEventListener('click',()=>{$('errors-section').open=true;$('errors-section').scrollIntoView({behavior:'smooth'});});
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
    $('diagnostic').checked=me.diagnostic;
    $('userbar').replaceChildren();
    const logout=document.createElement('button');
    logout.id='logout';logout.className='outline';logout.textContent='Выйти';
    $('userbar').append(logout);
    text('endpoint',location.origin+'/mcp');
    $('admin-panel').hidden=!me.is_admin;
    if(me.is_admin){
      const data=await api('/api/admin/summary');
      text('admin-stats','Пользователей: '+data.users+' · Устройств: '+data.devices+
        ' · В сети: '+data.online_devices+' · Вызовов: '+data.tool_calls+' · Ошибок: '+data.tool_errors);
    }
    await refreshPanel();
  } catch(err) {
    notice('panel-notice',err.message);
  }
}
reload();
