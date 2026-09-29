'use strict';
const form=document.getElementById('login-form'), error=document.getElementById('login-error'), button=document.getElementById('sign-in');
const otpSection=document.getElementById('otp-section'), restart=document.getElementById('restart-login');
let challenge=null, submitting=false, navigating=false;
const controls=document.getElementById('login-controls'), status=document.getElementById('login-status');
function resetChallenge(){
 challenge=null; form.elements.username.disabled=false;
 form.elements.password.disabled=false; form.elements.password.hidden=false;
 document.getElementById('password-label').hidden=false;
 form.elements.otp.required=false; form.elements.otp.value='';
 document.getElementById('otp-label').textContent='One-time code (if required by your realm)';
 restart.hidden=true; button.textContent='Sign in';
}
restart.addEventListener('click',()=>{if(submitting)return;resetChallenge();error.hidden=true;form.elements.password.focus();});
loadingModal.set('auth',true,'Loading sign-in settings…');
fetch('/api/auth',{credentials:'same-origin'}).then(r=>r.json()).then(info=>{
 if(info.provider==='pve'){
  document.getElementById('login-intro').textContent='Sign in with your Proxmox account.';
  form.elements.username.placeholder='researcher@pve';
  document.getElementById('login-note').textContent='Your account must belong to the designated Proxmox orchestrator group.';
  otpSection.hidden=false;
 }
}).catch(()=>{}).finally(()=>loadingModal.set('auth',false));
form.addEventListener('submit',async(event)=>{
 event.preventDefault(); if(submitting)return;submitting=true;controls.disabled=true;form.setAttribute('aria-busy','true');button.disabled=true; button.textContent=challenge?'Verifying code…':'Signing in…'; error.hidden=true;status.hidden=false;document.getElementById('login-status-label').textContent=challenge?'Verifying your authenticator code…':'Checking your account and access with the server…';
 loadingModal.set('login',true,document.getElementById('login-status-label').textContent);
 try {
  const data=challenge ? {challenge_id:challenge,otp:form.elements.otp.value.trim()} :
   {username:form.elements.username.value,password:form.elements.password.value,otp:form.elements.otp.value.trim()};
  const response=await fetch(challenge?'/api/login/totp':'/api/login',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify(data)});
  const body=await response.json().catch(()=>({}));
  form.elements.password.value=''; form.elements.otp.value='';
  if(response.status===202 && body.requires_totp){
   challenge=body.challenge_id; form.elements.username.disabled=true;
   form.elements.password.disabled=true; form.elements.password.hidden=true;
   document.getElementById('password-label').hidden=true;
   otpSection.hidden=false; form.elements.otp.required=true;
   document.getElementById('otp-label').textContent='Authenticator code (TOTP)';
   restart.hidden=false; return;
  }
  if(response.ok){navigating=true;document.getElementById('login-status-label').textContent='Signed in. Opening your dashboard…';location.replace('/'); return;}
  resetChallenge();
  error.textContent=body.error||'Unable to sign in. Please try again.'; error.hidden=false;
 } catch {resetChallenge();error.textContent='Unable to reach the server. Please try again.'; error.hidden=false;}
 finally {loadingModal.set('login',navigating,'Opening your dashboard…');if(!navigating){submitting=false;controls.disabled=false;form.setAttribute('aria-busy','false');status.hidden=true;button.disabled=false;button.textContent=challenge?'Verify code':'Sign in';if(challenge)form.elements.otp.focus();}}
});
