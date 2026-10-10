'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const root=path.resolve(__dirname,'../server/static');

class FakeNode {
  constructor(id=''){this.id=id;this.textContent='';this.hidden=false;this.checked=false;
    this.listeners={};this.children=[];this.value='';this.style={};this.attrs={};
    this.className='';this.classList={toggle:(key,value)=>{this.attrs[key]=value;}};
    this.autocomplete='';this.disabled=false;this.open=false;}
  addEventListener(name,callback){this.listeners[name]=callback;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  get options(){return this.children;}
  setAttribute(name,value){this.attrs[name]=value;}
  scrollIntoView(){}
}

function setup(view,{session=true,badLogin=false}={}){
  const ids=new Map();
  const get=id=>{if(!ids.has(id))ids.set(id,new FakeNode(id));return ids.get(id);};
  get('errors-days').value='30';
  get('auth-email').value='person@example.test';
  get('auth-password').value='test-password';
  const replaced=[],requests=[];
  const fetch=async(endpoint,options={})=>{
    requests.push({endpoint,options});
    let status=200,data={ok:true};
    if(endpoint==='/api/me') data={authenticated:session,email:'person@example.test',
      csrf:'csrf-abc',diagnostic:false,is_admin:false};
    else if(endpoint==='/api/devices') data={devices:[]};
    else if(endpoint==='/api/usage') data={total:4,errors:1,daily:[]};
    else if(endpoint.startsWith('/api/errors?')) data={total:1,events:[],offset:0,has_more:false};
    else if(endpoint==='/api/login' && badLogin){status=401;data={error:'Invalid credentials'};}
    else if(endpoint==='/api/register' || endpoint==='/api/login')data={ok:true,csrf:'newcsrf'};
    else if(endpoint==='/api/logout')data={ok:true};
    else if(endpoint==='/api/devices/pair')data={pairing_code:'TEST_TEST_TEST'};
    else if(endpoint==='/api/account/diagnostics')data={diagnostic:true};
    else throw new Error('Unexpected mocked endpoint '+endpoint);
    return {status,ok:status<400,json:async()=>data};
  };
  const context={
    fetch,navigator:{userAgent:'Windows',clipboard:{writeText:async()=>{}}},
    location:{origin:'https://hub.example.net',replace:target=>replaced.push(target)},
    document:{getElementById:get,createElement:kind=>new FakeNode(kind)},
    URLSearchParams,Option:function(label,value){this.label=label;this.value=value;},
    Date,Set,JSON,String,Number,console,confirm:()=>true
  };
  const load=filename=>vm.runInNewContext(fs.readFileSync(path.join(root,filename),'utf8'),context,
    {timeout:2000,filename});
  load('common.js');
  load(view==='auth'?'auth.js':'dashboard.js');
  return {get,replaced,requests};
}

async function run(){
  const guest=setup('auth');
  await guest.get('auth-form').listeners.submit({preventDefault(){}});
  assert.deepEqual(guest.replaced,['/'],'Successful login must navigate to server-rendered view');
  assert(guest.requests.some(r=>r.endpoint==='/api/login'));
  assert(!guest.requests.some(r=>r.endpoint==='/api/devices'));

  const bad=setup('auth',{badLogin:true});
  await bad.get('auth-form').listeners.submit({preventDefault(){}});
  assert.equal(bad.replaced.length,0);
  assert.equal(bad.get('auth-notice').textContent,'Invalid credentials');

  const signedIn=setup('dashboard',{session:true});
  await new Promise(r=>setTimeout(r,80));
  assert.equal(signedIn.get('email').textContent,'person@example.test');
  assert.equal(signedIn.get('install-command').textContent,'npx.cmd --yes https://hub.example.net/cli-1.0.0.tgz');
  assert.equal(signedIn.get('total-calls').textContent,'4');
  assert.equal(signedIn.get('userbar').children[0].textContent,'Выйти');
  await signedIn.get('userbar').listeners.click({target:{id:'logout'}});
  assert.deepEqual(signedIn.replaced,['/']);
  assert(signedIn.requests.some(r=>r.endpoint==='/api/logout'
    && r.options.headers['X-ASC-CSRF']==='csrf-abc'));

  const expired=setup('dashboard',{session:false});
  await new Promise(r=>setTimeout(r,50));
  assert.deepEqual(expired.replaced,['/']);
  assert(!expired.requests.some(r=>r.endpoint==='/api/devices'));

  console.log('UI_SCRIPTS_OK guest_login failure dashboard_logout expired_session installer');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
