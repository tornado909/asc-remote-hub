'use strict';
let csrf = '';
async function api(path, options={}){
  const method=options.method||'GET';
  const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(method!=='GET'?{'X-ASC-CSRF':csrf}:{}),...(options.headers||{})},credentials:'same-origin'});
  const content=await response.json();
  if(!response.ok)throw new Error(content.error||('HTTP '+response.status));
  return content;
}
const $=id=>document.getElementById(id);
const text=(id,value)=>{$(id).textContent=String(value)};
function notice(id,message){text(id,message)}

const INSTALLER_URL=location.origin+'/cli-1.0.1.tgz';
const INSTALL_COMMANDS={
  windows:'npx --yes '+INSTALLER_URL,
  linux:'npx --yes '+INSTALLER_URL
};
let installPlatform='windows';
function chooseInstallPlatform(target){
  installPlatform=target;
  text('install-command',INSTALL_COMMANDS[target]);
  text('install-platform-hint',target==='windows'
    ?'Вставьте команду в PowerShell или терминал Windows.'
    :'Вставьте команду в терминал Linux.');
  for(const [id,platform] of [['install-win','windows'],['install-linux','linux']]){
    const button=$(id);
    button.classList.toggle('active',target===platform);
    button.setAttribute('aria-pressed',target===platform?'true':'false');
  }
}
async function copyInstallValue(value,success){
  try{
    if(!navigator.clipboard || !navigator.clipboard.writeText){
      throw new Error('Буфер обмена недоступен');
    }
    await navigator.clipboard.writeText(value);
    notice('install-notice',success);
  }catch(err){
    notice('install-notice','Не удалось скопировать: выделите команду или код вручную.');
  }
}
$('install-win').addEventListener('click',()=>chooseInstallPlatform('windows'));
$('install-linux').addEventListener('click',()=>chooseInstallPlatform('linux'));
$('install-copy').addEventListener('click',()=>copyInstallValue(
  INSTALL_COMMANDS[installPlatform],'Команда скопирована — вставьте её в терминал.'));
if($('install-copy-pair')) $('install-copy-pair').addEventListener('click',()=>copyInstallValue(
  $('install-pair-code').textContent,'Код скопирован. Вставьте его в установщик.'));
async function issuePairCode(){
  try{
    const x=await api('/api/devices/pair',{method:'POST',body:'{}'});
    if($('pair-code')) text('pair-code',x.pairing_code);
    if($('pairing')) $('pairing').hidden=false;
    text('install-pair-code',x.pairing_code);
    $('install-pair-display').hidden=false;
    notice('install-notice','Код получен, действителен 10 минут.');
  }catch(err){
    notice('install-notice','Не удалось получить код: '+err.message);
    if($('panel-notice')) notice('panel-notice',err.message);
  }
}
if($('install-create-pair')) $('install-create-pair').addEventListener('click',issuePairCode);
chooseInstallPlatform(typeof navigator!=='undefined' && /Linux/i.test(navigator.userAgent||'')
    ? 'linux' : 'windows');
