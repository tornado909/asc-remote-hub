'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const fsp=fs.promises;
const os=require('node:os');
const path=require('node:path');
const {installVerifiedAgentFiles}=require('../server/installer_cli/connect.cjs');

async function run(){
  const root=await fsp.mkdtemp(path.join(os.tmpdir(),'asc-hub-cross-device-test-'));
  const foreign=(process.platform!=='win32'&&fs.existsSync('/dev/shm')&&
    fs.statSync('/dev/shm').dev!==fs.statSync(root).dev) ? '/dev/shm' : os.tmpdir();
  const external=await fsp.mkdtemp(path.join(foreign,'asc-hub-cli-source-test-'));
  try{
    const source=path.join(external,'asc-remote-hub');
    await fsp.mkdir(path.join(source,'client'),{recursive:true});
    await fsp.writeFile(path.join(source,'client','bridge.py'),'new source\n');
    await fsp.mkdir(path.join(source,'install'));
    await fsp.writeFile(path.join(source,'install','install_linux.sh'),'install stub\n');

    const dir=path.join(root,'home','.asc-remote-hub','agent');
    const result=await installVerifiedAgentFiles(source,dir);
    assert.equal(result,'installed');
    assert.equal(await fsp.readFile(path.join(dir,'client','bridge.py'),'utf8'),'new source\n');
    assert(fs.existsSync(path.join(source,'client','bridge.py')),
      'Original staging source must survive cross-device copy');
    assert.equal((await fsp.readdir(path.dirname(dir))).filter(
      n=>n.startsWith('.asc-hub-agent-stage-')).length,0,'No staging leftovers');
    console.log('FIRST_INSTALL_OK '+(foreign==='/dev/shm'?'CROSS_DEVICE_REAL':'SAME_DEVICE_FALLBACK'));

    const secret=path.join(dir,'client','credential.secret');
    const venv=path.join(dir,'.venv','important-state.txt');
    await fsp.mkdir(path.dirname(venv),{recursive:true});
    await fsp.writeFile(secret,'KEEP_CREDENTIALS');
    await fsp.writeFile(venv,'KEEP_VENV');
    await fsp.writeFile(path.join(source,'client','bridge.py'),'updated source\n');
    assert.equal(await installVerifiedAgentFiles(source,dir),'updated');
    assert.equal(await fsp.readFile(path.join(dir,'client','bridge.py'),'utf8'),'updated source\n');
    assert.equal(await fsp.readFile(secret,'utf8'),'KEEP_CREDENTIALS');
    assert.equal(await fsp.readFile(venv,'utf8'),'KEEP_VENV');
    console.log('EXISTING_AGENT_STATE_PRESERVED');

    const incomplete=path.join(external,'incomplete');
    await fsp.mkdir(incomplete);
    const emptyTarget=path.join(root,'new','agent');
    await assert.rejects(installVerifiedAgentFiles(incomplete,emptyTarget),/Недостаточно файлов/);
    assert.equal(fs.existsSync(emptyTarget),false);
    assert.equal((await fsp.readdir(path.dirname(emptyTarget))).filter(
      n=>n.startsWith('.asc-hub-agent-stage-')).length,0);
    console.log('FAILED_STAGING_CLEANUP_OK');

    if(process.platform!=='win32'){
      const link=path.join(root,'agent-link');
      await fsp.symlink(dir,link);
      await assert.rejects(installVerifiedAgentFiles(source,link),/символической ссылкой/);
      assert.equal(await fsp.readFile(secret,'utf8'),'KEEP_CREDENTIALS');
      console.log('SYMLINK_TARGET_REJECTED');
    }
  }finally{
    await Promise.all([fsp.rm(external,{recursive:true,force:true}),fsp.rm(root,{recursive:true,force:true})]);
  }
}
run().catch(e=>{console.error(e);process.exitCode=1});
