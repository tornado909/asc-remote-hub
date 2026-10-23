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

const INSTALLER_URL=location.origin+'/cli.tgz';
const INSTALL_COMMANDS={
  windows:'npx --yes '+INSTALLER_URL,
  linux:'npx --yes '+INSTALLER_URL
};
const DEPENDENCY_COMMANDS={
  windows:'winget install --id OpenJS.NodeJS.LTS --exact --source winget --accept-package-agreements --accept-source-agreements; winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements',
  linux:'if [ "$(id -u)" -eq 0 ]; then apt-get update && apt-get install -y nodejs npm python3 python3-venv tar curl; else sudo apt-get update && sudo apt-get install -y nodejs npm python3 python3-venv tar curl; fi'
};
let installPlatform='windows';
function chooseInstallPlatform(target){
  installPlatform=target;
  text('install-command',INSTALL_COMMANDS[target]);
  text('install-deps-command',DEPENDENCY_COMMANDS[target]);
  text('install-deps-hint',target==='windows'
    ?'Windows 10/11: установка через WinGet. После неё переоткройте PowerShell перед npx. Может потребоваться UAC.'
    :'Debian/Ubuntu: установка через apt и sudo. Для Fedora/RHEL/SUSE используйте скачиваемый скрипт.');
  const bootstrap=$('install-bootstrap-script');
  bootstrap.href=target==='windows'?'/bootstrap.ps1':'/bootstrap.sh';
  bootstrap.setAttribute('download',target==='windows'?'bootstrap.ps1':'bootstrap.sh');
  text('install-bootstrap-run-hint',target==='windows'
    ?'Скачанный bootstrap.ps1: powershell -NoProfile -ExecutionPolicy Bypass -File .\\bootstrap.ps1 (PowerShell в папке загрузок).'
    :'Скачанный bootstrap.sh: bash ./bootstrap.sh (терминал в папке загрузок).');
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
$('install-deps-copy').addEventListener('click',()=>copyInstallValue(
  DEPENDENCY_COMMANDS[installPlatform],
  'Команда установки зависимостей скопирована. После неё запустите npx.'));
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


// The site supplies commands for local execution only; never remotely invokes
// an uninstall or exposes user credentials.
const MANAGEMENT_COMMANDS={
  unpair:'npx --yes '+INSTALLER_URL+' unpair',
  uninstall:'npx --yes '+INSTALLER_URL+' uninstall'
};
for(const operation of ['unpair','uninstall']){
  const output=$('manage-'+operation+'-command');
  const button=$('manage-'+operation+'-copy');
  if(output&&button){
    output.textContent=MANAGEMENT_COMMANDS[operation];
    button.addEventListener('click',async()=>{
      try{
        if(!navigator.clipboard?.writeText)throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(MANAGEMENT_COMMANDS[operation]);
        notice('manage-notice','Команда скопирована. Выполните её на нужном компьютере.');
      }catch(e){
        notice('manage-notice','Буфер обмена недоступен. Выделите текст команды вручную.');
      }
    });
  }
}
