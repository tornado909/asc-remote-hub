#!/usr/bin/env node
'use strict';
const os=require('node:os');
const path=require('node:path');
const fs=require('node:fs');
const fsp=fs.promises;
const https=require('node:https');
const crypto=require('node:crypto');
const cp=require('node:child_process');
const readline=require('node:readline/promises');
const {once}=require('node:events');

const ORIGIN='__HUB_ORIGIN__';
const VERSION='1.0.1';
const ARCHIVE='asc-remote-hub-agent-1.0.1.tar.gz';
const CHECKSUM='__ARCHIVE_SHA256__';
const WIN=process.platform==='win32';
const HOME=os.homedir();

function help(){
  const npx='npx';
  console.log([
    'ASC Remote Hub '+VERSION+' — подключение устройства',
    '',
    'Команда: '+npx+' --yes '+ORIGIN+'/cli-1.0.0.tgz',
    '',
    '1. Зарегистрируйтесь на '+ORIGIN,
    '2. В кабинете нажмите «+ Добавить устройство»',
    '3. Вставьте одноразовый код в установщик',
    '4. Дождитесь запуска фонового агента',
    '',
    '--dry-run       Проверить загрузку и окружение без установки',
    '--read-only     Разрешить только чтение',
    '--full          Разрешить запись и выполнение команд (Windows: GUI)',
    '--name ИМЯ      Имя компьютера для кабинета',
    '--roots ПУТИ    Каталоги через ; (по умолчанию домашняя папка)',
    '--help          Справка',
    '--version       Версия',
    '',
    'Требуется Node.js >=18 (для npx), Python >=3.10 и tar.',
    'Рабочий агент не использует Node.js, платный туннель или OpenAI Platform API.'
  ].join('\n'));
}
function fail(text){throw new Error(text);}
function options(argv){
  const out={mode:null,name:os.hostname(),roots:HOME,dryRun:false};
  for(let i=0;i<argv.length;i++){
    const arg=argv[i];
    if(arg==='-h'||arg==='--help')out.help=true;
    else if(arg==='-v'||arg==='--version')out.version=true;
    else if(arg==='--dry-run')out.dryRun=true;
    else if(arg==='--full')out.mode='full';
    else if(arg==='--read-only')out.mode='read-only';
    else if(arg==='--name'||arg==='--roots'){
      if(i+1>=argv.length)fail('Не указано значение '+arg);
      out[arg==='--name'?'name':'roots']=argv[++i];
    }else if(arg!=='connect')fail('Неизвестный аргумент: '+arg);
  }
  if(argv.includes('--full')&&argv.includes('--read-only'))fail('Нельзя сочетать --full и --read-only.');
  if(!out.name||out.name.length>80)fail('Имя устройства должно содержать от 1 до 80 символов.');
  return out;
}
function exec(program,args,opts={}){
  const res=cp.spawnSync(program,args,{stdio:'inherit',shell:false,windowsHide:false,...opts});
  if(res.error)fail('Не удаётся запустить '+program+': '+res.error.message);
  if(res.signal)fail('Программа '+program+' прервана сигналом '+res.signal);
  if(res.status!==0)fail(program+' завершился с кодом '+res.status);
}
function environment(){
  const py=WIN?'python.exe':'python3';
  const r=cp.spawnSync(py,['-c','import sys;print(str(sys.version_info.major)+"."+str(sys.version_info.minor))'],{
    encoding:'utf8',timeout:12000,windowsHide:true
  });
  if(r.error||r.status!==0)fail('Не найден Python 3.10+. Установите Python с https://www.python.org/downloads/.');
  const [major,minor]=(r.stdout||'').trim().split('.').map(Number);
  if(major!==3||minor<10)fail('Требуется Python >=3.10, найден '+r.stdout.trim());
  const tar=WIN?'tar.exe':'tar';
  const t=cp.spawnSync(tar,['--version'],{encoding:'utf8',timeout:7000,windowsHide:true});
  if(t.error||t.status!==0)fail('Не найден tar (включён в Windows 10/11 и большинство Linux).');
  return {py,tar};
}
function roots(text){
  const list=text.split(';').map(x=>x.trim()).filter(Boolean);
  if(list.length<1||list.length>12)fail('Укажите от 1 до 12 каталогов.');
  for(const dir of list){
    if(!path.isAbsolute(dir)||!fs.existsSync(dir)||!fs.statSync(dir).isDirectory()){
      fail('Нет каталога или путь не абсолютный: '+dir);
    }
  }
  return list.join(';');
}
function credentialPath(){
  if(WIN)return path.join(process.env.APPDATA||path.join(HOME,'AppData','Roaming'),
    'ASC-Remote-Hub','agent.json');
  return path.join(HOME,'.config','asc-remote-hub','agent.json');
}
async function download(file){
  const url=new URL('/downloads/'+ARCHIVE,ORIGIN);
  const output=fs.createWriteStream(file,{flags:'wx',mode:0o600});
  const hasher=crypto.createHash('sha256');
  let count=0;
  try{
    const req=https.get(url,{timeout:35000,headers:{'User-Agent':'ASC-Hub-Installer/'+VERSION}});
    req.on('timeout',()=>req.destroy(new Error('HTTPS timeout')));
    const [response]=await once(req,'response');
    if(response.statusCode!==200)fail('Сервер вернул HTTP '+response.statusCode+' при загрузке.');
    for await(const chunk of response){
      count+=chunk.length;
      if(count>20*1024*1024)fail('Архив слишком большой.');
      hasher.update(chunk);
      if(!output.write(chunk))await once(output,'drain');
    }
    await new Promise((resolve,reject)=>output.end(err=>err?reject(err):resolve()));
    if(hasher.digest('hex')!==CHECKSUM)fail('Контрольная сумма SHA-256 не совпала. Установка прервана.');
    return count;
  }catch(error){
    output.destroy();
    await fsp.rm(file,{force:true}).catch(()=>{});
    throw error;
  }
}
async function acquireAgentArchive(file){
  // The official CLI release includes an SHA256-verified agent. No second
  // Cloudflare/GitHub/npm download is needed when installing a local .tgz.
  const bundled=path.join(__dirname,'..','assets',ARCHIVE);
  if(fs.existsSync(bundled)){
    const data=await fsp.readFile(bundled);
    if(data.length>20*1024*1024)fail('Встроенный архив агента слишком большой.');
    if(crypto.createHash('sha256').update(data).digest('hex')!==CHECKSUM)
      fail('Контрольная сумма встроенного архива агента не совпала.');
    await fsp.writeFile(file,data,{flag:'wx',mode:0o600});
    console.log('✓ Агент включён в установщик: дополнительная загрузка не требуется.');
    return data.length;
  }
  // Compatibility with older, unbundled distributions.
  return download(file);
}

function archiveCheck(file,tar){
  const res=cp.spawnSync(tar,['-tzf',file],{encoding:'utf8',timeout:12000,maxBuffer:1024*1024});
  if(res.error||res.status!==0)fail('Архив повреждён или не поддерживается программой tar.');
  const members=res.stdout.split(/\r?\n/).filter(Boolean);
  if(!members.length)fail('Архив пуст.');
  for(const name of members){
    if(!name.startsWith('asc-remote-hub/')||name.includes('..')||name.includes('\\')||
       name.includes('\0')||path.posix.isAbsolute(name)){
      fail('Небезопасный путь в архиве.');
    }
  }
}
async function question(prompt){
  if(!process.stdin.isTTY)fail('Установку нужно запустить в интерактивном терминале.');
  const rl=readline.createInterface({input:process.stdin,output:process.stdout});
  try{return (await rl.question(prompt)).trim();}
  finally{rl.close();}
}
function secret(prompt){
  return new Promise((resolve,reject)=>{
    if(!process.stdin.isTTY||!process.stdin.setRawMode){
      reject(new Error('Для ввода кода нужен интерактивный терминал.'));return;
    }
    let value='';
    process.stdout.write(prompt);
    process.stdin.setRawMode(true);
    process.stdin.resume();
    process.stdin.setEncoding('utf8');
    function clear(){
      process.stdin.off('data',handle);
      process.stdin.setRawMode(false);
      process.stdin.pause();
      process.stdout.write('\n');
    }
    function handle(input){
      // A paste can contain both the code and Enter in one data chunk.
      for(const ch of input){
        if(ch==='\u0003'){clear();reject(new Error('Отменено пользователем'));return;}
        if(ch==='\r'||ch==='\n'){clear();resolve(value.trim());return;}
        if(ch==='\u007f'||ch==='\b'){value=value.slice(0,-1);continue;}
        if(/[a-zA-Z0-9_-]/.test(ch)&&value.length<256)value+=ch;
      }
    }
    process.stdin.on('data',handle);
  });
}
async function main(){
  const opt=options(process.argv.slice(2));
  if(opt.help){help();return;}
  if(opt.version){console.log(VERSION);return;}
  if(!['win32','linux'].includes(process.platform))fail('Поддерживаются только Windows и Linux.');
  console.log('\nASC Remote Hub '+VERSION+' — '+(WIN?'Windows':'Linux'));
  console.log('Хаб: '+ORIGIN+' (только исходящий HTTPS)\n');
  const {py,tar}=environment();
  const allowed=roots(opt.roots);
  const dir=path.join(HOME,'.asc-remote-hub','agent');
  // An interrupted install may leave the agent directory but no credential.
  // Never destroy or re-enroll an already configured device.
  const configured=fs.existsSync(credentialPath());
  if(configured){
    let existing;
    try{existing=JSON.parse(fs.readFileSync(credentialPath(),'utf8'));}
    catch(_e){fail('Настройки устройства повреждены. Не перезаписываю действующую привязку.');}
    if(existing.hub_url!==ORIGIN)
      fail('Устройство привязано к другому хабу. Существующий токен сохранён.');
    if(!existing.device_id||!existing.token_file||!fs.existsSync(existing.token_file))
      fail('В настройках отсутствует ключ устройства. Исправьте привязку в кабинете без удаления других устройств.');
  } else if(fs.existsSync(dir)){
    console.log('Обнаружена незавершённая установка. Продолжаю, сохраняя существующие файлы и окружение.');
    console.log('Если устройство уже появилось в кабинете как офлайн, отвяжите только его и создайте новый код.');
  }
  const tmp=await fsp.mkdtemp(path.join(os.tmpdir(),'asc-hub-setup-'));
  try{
    console.log('Скачиваю проверенный пакет с собственного хаба...');
    const file=path.join(tmp,ARCHIVE);
    let size;
    for(let attempt=1;attempt<=3;attempt++){
      try{size=await acquireAgentArchive(file);break;}
      catch(error){
        const problems=Array.isArray(error.errors)?error.errors:[error];
        const detail=problems.map(e=>[e.code,e.address,e.message].filter(Boolean).join(' ')).join('; ');
        if(attempt===3)fail('Не удалось скачать пакет за 3 попытки: '+detail+
          '. Проверьте доступ к '+ORIGIN+' и настройки сети/прокси.');
        console.log('Ошибка загрузки ('+detail+'). Повтор через '+attempt+' сек...');
        await new Promise(resolve=>setTimeout(resolve,1000*attempt));
      }
    }
    archiveCheck(file,tar);
    console.log('✓ SHA-256 подтверждён; архив '+Math.ceil(size/1024)+' КБ.');
    console.log('✓ Python 3.10+ обнаружен.');
    if(opt.dryRun){
      console.log('✓ Проверка пройдена. Ничего не установлено и не зарегистрировано.');
      return;
    }
    if(!opt.mode&&!configured){
      console.log('\nВыберите разрешения:');
      console.log('  1 — Только чтение (рекомендуется)');
      console.log('  2 — Полный доступ: файлы, терминал'+(WIN?', GUI':''));
      const answer=await question('Режим [1/2]: ');
      if(answer!==''&&answer!=='1'&&answer!=='2')fail('Выбран неверный режим.');
      opt.mode=answer==='2'?'full':'read-only';
    }
    console.log('Каталоги: '+allowed);
    console.log('Разрешения: '+(configured?'Существующие (без изменений)':(opt.mode==='full'?'Полные':'Только чтение')));
    const tmpdir=path.join(tmp,'unpacked');
    await fsp.mkdir(tmpdir,{recursive:true});
    exec(tar,['-xzf',file,'-C',tmpdir],{stdio:'pipe'});
    const source=path.join(tmpdir,'asc-remote-hub');
    if(!fs.existsSync(path.join(source,'client','bridge.py'))){
      fail('В пакете отсутствует агент.');
    }
    await fsp.mkdir(path.dirname(dir),{recursive:true});
    if(fs.existsSync(dir)){
      // Copy only verified release files; retain the existing .venv and
      // credentials. Never remove the directory on retries.
      await fsp.cp(source,dir,{recursive:true,force:true});
      console.log('✓ Файлы агента обновлены, данные существующего подключения сохранены.');
    }else{
      await fsp.rename(source,dir);
    }
    if(WIN){
      console.log('Создаю изолированное Python-окружение агента...');
      const venv=path.join(dir,'.venv');
      if(!fs.existsSync(path.join(venv,'Scripts','python.exe')))
        exec(py,['-m','venv',venv]);
      if(opt.mode==='full'&&!configured){
        console.log('Устанавливаю GUI-зависимости в отдельное окружение...');
        exec(path.join(venv,'Scripts','python.exe'),
          ['-m','pip','install','--disable-pip-version-check',
           'Pillow>=10,<13','PyAutoGUI>=0.9.54,<0.10']);
      }
    }
    let pairing='';
    if(configured){
      const checker=cp.spawnSync(WIN?path.join(dir,'.venv','Scripts','python.exe'):py,
        [path.join(dir,'client','bridge.py'),'check','--config',credentialPath()],
        {encoding:'utf8',timeout:20000,windowsHide:true});
      if(checker.error||checker.status!==0)
        fail('Не удалось проверить существующий зашифрованный ключ. Не перезаписываю его. Проверьте Windows-профиль или состояние привязки.');
      console.log('✓ Действующая привязка подтверждена. Восстанавливаю автозапуск.');
    } else{
      console.log('\nОткройте '+ORIGIN+' → «+ Добавить устройство».');
      pairing=await secret('Одноразовый код (ввод скрыт): ');
      if(!/^[A-Za-z0-9_-]{24,256}$/.test(pairing))fail('Некорректный код привязки.');
      console.log('Привязываю устройство '+opt.name+' ...');
    }
    const env={...process.env};
    if(configured)env.ASC_HUB_SKIP_ENROLL='1';
    else env.ASC_HUB_PAIRING_CODE=pairing;
    if(WIN){
      const args=['-NoProfile','-ExecutionPolicy','Bypass','-File',
        path.join(dir,'install','install_windows.ps1'),
        '-HubUrl',ORIGIN,'-DeviceName',opt.name,'-Roots',allowed];
      if(opt.mode==='full'&&!configured)args.push('-FullAccess');
      if(configured)args.push('-SkipEnrollment');
      exec('powershell.exe',args,{env});
    }else{
      exec('bash',[path.join(dir,'install','install_linux.sh'),
        ORIGIN,opt.name,allowed,opt.mode],{env});
    }
    console.log('\n✓ Установка завершена! Проверьте статус устройства на '+ORIGIN);
    console.log('Node.js больше не нужен для работы агента.');
  }finally{
    await fsp.rm(tmp,{recursive:true,force:true}).catch(()=>{});
  }
}
main().catch(e=>{
  console.error('\nОшибка установки: '+(e&&e.message||String(e)));
  process.exitCode=1;
});
