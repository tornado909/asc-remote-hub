'use strict';
let registerMode = false;
function switchMode(value){registerMode=value;$('tab-login').classList.toggle('active',!value);$('tab-register').classList.toggle('active',value);text('auth-submit',value?'Зарегистрироваться':'Войти');$('auth-password').autocomplete=value?'new-password':'current-password';notice('auth-notice','')}
$('tab-login').addEventListener('click',()=>switchMode(false));
$('tab-register').addEventListener('click',()=>switchMode(true));
$('auth-form').addEventListener('submit',async e=>{e.preventDefault();try{const data=await api(registerMode?'/api/register':'/api/login',{method:'POST',body:JSON.stringify({email:$('auth-email').value,password:$('auth-password').value})});location.replace('/')}catch(err){notice('auth-notice',err.message)}});
