'use strict';
const form=document.getElementById('login-form'), error=document.getElementById('login-error'), button=document.getElementById('sign-in');
const otpSection=document.getElementById('otp-section'), restart=document.getElementById('restart-login');
let challenge=null;
function resetChallenge(){
 challenge=null; form.elements.username.disabled=false;
 form.elements.password.disabled=false; form.elements.password.hidden=false;
 document.getElementById('password-label').hidden=false;
 form.elements.otp.required=false; form.elements.otp.value='';
 document.getElementById('otp-label').textContent='One-time code (if required by your realm)';
 restart.hidden=true; button.textContent='Sign in';
}
restart.addEventListener('click',()=>{resetChallenge();error.hidden=true;form.elements.password.focus();});
fetch('/api/auth',{credentials:'same-origin'}).then(r=>r.json()).then(info=>{
 if(info.provider==='pve'){
  document.getElementById('login-intro').textContent='Sign in with your Proxmox account.';
  form.elements.username.placeholder='researcher@pve';
  document.getElementById('login-note').textContent='Your account must belong to the designated Proxmox orchestrator group.';
  otpSection.hidden=false;
 }
}).catch(()=>{});
form.addEventListener('submit',async(event)=>{
 event.preventDefault(); button.disabled=true; button.textContent='Signing in…'; error.hidden=true;
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
   restart.hidden=false; form.elements.otp.focus(); return;
  }
  if(response.ok){location.replace('/'); return;}
  resetChallenge();
  error.textContent=body.error||'Unable to sign in. Please try again.'; error.hidden=false;
 } catch {resetChallenge();error.textContent='Unable to reach the server. Please try again.'; error.hidden=false;}
 finally {button.disabled=false;button.textContent=challenge?'Verify code':'Sign in';}
});
