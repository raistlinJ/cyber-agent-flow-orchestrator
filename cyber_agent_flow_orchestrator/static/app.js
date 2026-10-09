/* Text-only rendering keeps guest command lines and journal labels inert. */
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => {const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node;};
// Retain mounted cards and rows so refreshes preserve focus and scroll state.
function patchDisplayNode(current, next){
 if(current.nodeType!==next.nodeType||current.nodeName!==next.nodeName){current.replaceWith(next);return;}
 if(current.nodeType===Node.TEXT_NODE){if(current.nodeValue!==next.nodeValue)current.nodeValue=next.nodeValue;return;}
 for(const attr of [...current.attributes])if(!next.hasAttribute(attr.name))current.removeAttribute(attr.name);
 for(const attr of next.attributes)if(current.getAttribute(attr.name)!==attr.value)current.setAttribute(attr.name,attr.value);
 const children=[...next.childNodes];
 for(let i=0;i<children.length;i++){
  if(current.childNodes[i])patchDisplayNode(current.childNodes[i],children[i]);else current.append(children[i]);
 }
 while(current.childNodes.length>children.length)current.lastChild.remove();
}
function updateDisplayList(container, next){
 const existing=new Map([...container.children].map(node=>[node.dataset.displayKey,node]));
 let cursor=container.firstChild;
 for(const node of [...next.children]){
  const mounted=existing.get(node.dataset.displayKey);
  if(mounted){patchDisplayNode(mounted,node);existing.delete(node.dataset.displayKey);}
  const item=mounted||node;
  if(item!==cursor)container.insertBefore(item,cursor);
  cursor=item.nextSibling;
 }
 for(const node of existing.values())node.remove();
}
let snapshot = null, fetching = false, failed = false, rolesDirty = false, rolesSaving = false, sampleStarting = false;
let initialized=false, operation=null, waitingForObservation=false, waitingForMaintenance=false, redirecting=false, maintenanceStarting=false;
let refreshPromise=null, sessionPromise=null, csrfToken=null, wasBusy=false, busySince=performance.now();
let experimentCreating=false;
let pendingLaunch=null;
let blockingRefresh=false, foregroundChecks=true, pollTimer=null;
const clientLog=[];
const pages={overview:['Overview','Live machines, application processes, and workflow activity.'],experiments:['Experiments','Run a sample, follow its progress, and explore or export results.'],applications:['Applications','Check versions, update application source, and review maintenance.'],setup:['Lab setup','Choose your virtual machines and set your refresh preferences.']};
function routePage(focus=false){
 const route=location.hash.slice(1),page=Object.hasOwn(pages,route)?route:'overview';
 if(page!==route)history.replaceState(null,'','#'+page);
 for(const section of document.querySelectorAll('[data-page]'))section.hidden=section.dataset.page!==page;
 for(const link of document.querySelectorAll('[data-route]')){if(link.dataset.route===page)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');}
 $('page-title').textContent=pages[page][0];$('page-intro').textContent=pages[page][1];document.title=pages[page][0]+' · CAF Orchestrator';
 if(focus){$('page-title').focus({preventScroll:true});window.scrollTo(0,0);}
}
window.addEventListener('hashchange',()=>routePage(true));routePage();
// Keep one console node mounted across routes; reserve its measured height so
// the last control on every page remains reachable above the fixed dock.
new ResizeObserver(()=>document.body.style.setProperty('--console-height',$('debug-console').getBoundingClientRect().height+'px')).observe($('debug-console'));
function logClient(kind,message){clientLog.push({at:new Date().toISOString(),kind,message});if(clientLog.length>100)clientLog.shift();renderConsole();}
function renderConsole(){
 const entries=[...clientLog];
 for(const run of (snapshot?.runs||[]).filter(r=>r.sample_progress).sort((a,b)=>(b.sample_progress.started_at||'').localeCompare(a.sample_progress.started_at||'')).slice(0,3))for(const event of run.sample_progress?.events||[])entries.push({...event,message:`[${run.sample_id||run.scenario_experiment?.scenario} ${run.output.split('/').pop()}] ${event.message}`});
 for(const job of snapshot?.updates?.jobs||[])for(const event of job.console?.events||[])entries.push({...event,message:`[${job.role} VM ${job.vmid} ${job.action} ${job.id.slice(0,8)}] ${event.message}`});
 entries.sort((a,b)=>a.at.localeCompare(b.at));
 const output=$('console-output'),follow=output.scrollHeight-output.scrollTop-output.clientHeight<40;
 output.textContent=entries.slice(-300).map(event=>`${event.at} [${event.kind}] ${event.message}`).join('\n')||'Waiting for activity…';
 if(follow)output.scrollTop=output.scrollHeight;
}
async function apiFetch(path,options={}){
 const start=performance.now(),label=`${options.method||'GET'} ${path}`;logClient('request',label);
 try{const response=await fetch(path,options);logClient('response',`${label} → HTTP ${response.status} (${((performance.now()-start)/1000).toFixed(2)}s)`);return response;}
 catch(error){logClient('error',`${label} → network request failed (${((performance.now()-start)/1000).toFixed(2)}s)`);throw error;}
}
try{$('debug-console').open=localStorage.getItem('caf-console-open')!=='false';}catch{}
document.querySelector('#debug-console summary').addEventListener('click',event=>{event.preventDefault();$('debug-console').open=!$('debug-console').open;try{localStorage.setItem('caf-console-open',String($('debug-console').open));}catch{}});
$('debug-console').addEventListener('toggle',()=>{try{localStorage.setItem('caf-console-open',String($('debug-console').open));}catch{}});
$('download-console').addEventListener('click',()=>{const url=URL.createObjectURL(new Blob([$('console-output').textContent],{type:'text/plain'})),link=el('a');link.href=url;link.download='orchestrator-console.log';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
const refreshPeriods=['0','1','2','5','10'];
try{const saved=localStorage.getItem('caf-refresh-minutes');if(refreshPeriods.includes(saved))$('refresh-period').value=saved;}catch{}
function autoRefreshEnabled(){return Number($('refresh-period').value)>0;}
function isBusy(){return !initialized||blockingRefresh||Boolean(operation)||(autoRefreshEnabled()&&(foregroundChecks||waitingForMaintenance))||redirecting;}
function syncBusy(){
 const busy=isBusy(), loading=busy||fetching||(autoRefreshEnabled()&&waitingForObservation);if(loading&&!wasBusy)busySince=performance.now();wasBusy=loading;
 $('experiment-controls').disabled=busy;$('cancel-experiment').disabled=Boolean(operation)||experimentCreating;
 $('dashboard-controls').disabled=busy;$('dashboard-controls').setAttribute('aria-busy',String(busy));$('sign-out').disabled=busy;
 const lastChange=(snapshot?.updates?.jobs||[]).find(job=>job.action!=='inspect'&&(snapshot.updates.applications||[]).some(app=>app.role===job.role&&app.vmid===job.vmid));
 const maintenanceFailed=['failed','interrupted'].includes(lastChange?.status);
 const bar=$('loading-progress');bar.hidden=false;$('loading-status').classList.toggle('complete',!loading&&!failed&&!maintenanceFailed);
 $('loading-status').classList.toggle('maintenance-failed',!loading&&maintenanceFailed);
 let label, percent=null;
 if(redirecting)label='Opening sign-in…';
 else if(operation)label=operation;
 else if(autoRefreshEnabled()&&waitingForMaintenance){const job=(snapshot?.updates?.jobs||[]).find(job=>['queued','running'].includes(job.status));label=job?.message||'Retrieving application state…';const transfer=job?.console?.transfer;if(transfer&&!transfer.verified&&label.startsWith('Transferring verified'))label+=` · ${transfer.percent}% of file acknowledged (${transfer.sent_bytes}/${transfer.total_bytes} bytes)`;}
 else if(autoRefreshEnabled()&&waitingForObservation){if(snapshot?.loading){const info=snapshot.loading;percent=info.percent;label=`${info.status} · ${info.completed}/${info.total} VM checks complete · ${percent}%`;}else label='Checking VM power, guest access and applications…';}
 else if(fetching||!initialized)label=csrfToken?'Retrieving dashboard and verifying VM access…':'Checking your login session…';
 else if(failed){label='Loading failed. Use Refresh view to try again.';bar.hidden=true;}
 else{label=maintenanceFailed?`Dashboard loaded · ${lastChange.role} ${lastChange.action} ${lastChange.status}; see Applications`:'Dashboard loaded';bar.hidden=true;}
 if(percent==null)bar.removeAttribute('value');else bar.value=percent;
 $('loading-label').textContent=!busy&&loading?`Background refresh · ${label}`:label;$('loading-elapsed').textContent=loading?`${Math.floor((performance.now()-busySince)/1000)}s elapsed`:'';
 loadingModal.set('dashboard',loading&&busy,label,percent,Boolean(pendingLaunch));
 syncUpdateControls(busy?label:null);
 syncCreateExperiment();
}
function sessionExpired(){redirecting=true;clearPrivateView();syncBusy();location.replace('/login');throw Error('Your session expired. Sign in again.');}
async function loadSession(){
 if(sessionPromise)return sessionPromise;
 sessionPromise=(async()=>{const response=await apiFetch('/api/session',{cache:'no-store'});if(response.status===401)sessionExpired();if(!response.ok)throw Error('Unable to verify your session. Refresh to retry.');const session=await dashboardJSON(response);csrfToken=session.csrf;$('username-label').textContent=session.username;$('sign-out').hidden=session.provider==='desktop';})();
 try{await sessionPromise;}finally{sessionPromise=null;}
}
function duration(seconds) {if (seconds == null || !Number.isFinite(Number(seconds))) return 'Unknown'; seconds=Math.max(0, Math.floor(seconds)); const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=seconds%60; return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;}
function badge(text, tone='') {return el('span', text, 'badge '+tone);}
function timer(seconds, observed, live, sample=false) {const node=el('span',duration(seconds),'clock'); if(seconds!=null){node.dataset.seconds=seconds;node.dataset.observed=observed||'';node.dataset.live=live?'yes':'no';if(sample)node.dataset.source='sample';} return node;}
function detail(list,label,value) {const row=el('div');row.append(el('dt',label),el('dd',value));list.append(row);}
function render(data) {
 snapshot=data; renderRoles(data); renderModelConfigs(data); renderSamples(data); renderSampleActivity(data); renderUpdates(data); renderConsole();$('workflow').textContent=data.workflow_id||'';
 const notice=$('notice'); const errors=(data.errors||[]).map(x=>x.error);notice.hidden=!errors.length;notice.textContent=errors.join(' · ');
 const cards=el('div');
 if(!data.vms.length)cards.append(el('div','Checking configured machines…','empty'));
 for(const vm of data.vms){
  const card=el('article',null,'card'), top=el('div',null,'card-top'), label=el('div');
  card.dataset.displayKey=vm.role;
  label.append(el('h3',vm.label),el('div',vm.vmid ? `VM ${vm.vmid}${vm.name?' · '+vm.name:''}`:'No VM selected','vm-id'));
  const power=vm.qmp_status==='paused'?'paused':vm.power;
  top.append(el('span',vm.role==='core'?'◈':'▤','vm-icon'),label,badge(power,power==='running'?'good':power==='unknown'?'warn':''));card.append(top);
  if(vm.guest_cached){const note=el('p',null,'small');note.append(document.createTextNode('Cached guest observation · '),timer(0,vm.observed_at,true),document.createTextNode(' ago · Recheck VMs for a fresh scan'));card.append(note);}
  const list=el('dl');detail(list,'Guest agent',vm.guest_access);if(vm.qmp_status && vm.qmp_status!==vm.power)detail(list,'Emulator state',vm.qmp_status);detail(list,'Application',vm.application_present==null?'Not checked':vm.application_present?'Present':'Not found at configured location');
  detail(list,'Processes observed',vm.guest_access==='reachable'?String(vm.processes.length):'Unknown');
  card.append(list);
  const agentRun=(data.runs||[]).find(run=>runActive(run)&&vm.role==='participant'&&run.saved_settings?.participant_vmid===vm.vmid);
  if(agentRun)card.append(agentTranscriptButton(agentRun,data.backend_type));
  for(const service of vm.services||[]){if(service.unit===vm.unit){card.append(el('p',`${service.unit} · ${service.error?'Check unavailable':service.LoadState==='not-found'?'not found':service.ActiveState+' / '+service.SubState}`,'small'));}}
  if(vm.error||vm.guest_error)card.append(el('div',vm.error||vm.guest_error,'error'));
  if(!vm.vmid)card.append(el('p',data.roles?'Choose this VM on the Lab setup page.':'Set monitoring.core_vmid in your workflow YAML.','small'));
  card.append(el('div','APPLICATION & TOOL PROCESSES','process-heading'));
  const processes=el('div',null,'processes');
  for(const proc of vm.processes){const box=el('div',null,'process'), meta=el('div',null,'process-top');meta.append(el('span',`PID ${proc.pid}`),timer(proc.elapsed_seconds,vm.observed_at||data.checked_at,true));box.append(meta,el('code',proc.command));processes.append(box);}
  if(!vm.processes.length)processes.append(el('p',vm.guest_access==='reachable'?'No matching processes observed.':'Process state not available.','empty'));
  if(vm.processes_truncated)processes.append(el('p','Showing the first 40 matching processes.','small'));
  card.append(processes);cards.append(card);
 }
 updateDisplayList($('machines'),cards);
 const jobs=(data.vms||[]).flatMap(vm=>(vm.jobs||[]).map(job=>({...job,observed_at:vm.observed_at||data.checked_at})));
 $('command-count').textContent=`${jobs.length} recorded job${jobs.length===1?'':'s'}`; const commands=el('div');
 for(const job of jobs){const row=el('article',null,'command'),head=el('div',null,'command-head');row.dataset.displayKey=job.unit+'|'+job.run+'|'+job.stage;head.append(badge(job.live_state,job.live_state==='running'?'good':job.live_state==='unconfirmed'?'warn':''),el('span',`${job.run} / ${job.stage}`,'command-title'),timer(job.live_state==='finished'?null:job.elapsed_seconds,job.observed_at,job.live_state==='running'));row.append(head,el('code',job.command),el('div',`VM ${job.vmid} · ${job.unit} · ${job.live_state==='finished'?'No longer running; journal awaits cleanup':job.elapsed_source||'Last recorded job; live state unconfirmed'}`,'small'));commands.append(row);}
 if(!jobs.length)commands.append(el('p','No unfinished workflow commands recorded. Applications may still be running above.','empty'));
 updateDisplayList($('commands'),commands);
 renderExperiments(data);
 waitingForMaintenance=Boolean((data.updates?.jobs||[]).some(job=>['queued','running'].includes(job.status)));waitingForObservation=Boolean(data.refreshing&&(data.loading?data.loading.total>0:(!data.checked_at||data.vms.length>0)));if(!waitingForObservation)foregroundChecks=false;tick();syncBusy();
}
function tick(){const age=snapshot?.checked_at?Math.max(0,(Date.now()-Date.parse(snapshot.checked_at))/1000):null;const stale=failed||(age!=null&&age>Math.max(60,Number($('refresh-period').value)*120));$('connection').textContent=failed?'Dashboard unavailable':stale?'Observation is stale':snapshot?.checked_at?'Monitoring lab':'Checking machines…';$('pulse').className='dot'+(stale||!snapshot?.checked_at?' muted':'');$('checked').textContent=age==null?'Waiting for first check':`Checked ${duration(age)} ago${snapshot.refreshing?' · refreshing':''}`;for(const clock of document.querySelectorAll('[data-seconds]')){let seconds=Number(clock.dataset.seconds);if(clock.dataset.live==='yes'&&(!stale||clock.dataset.source==='sample')&&clock.dataset.observed)seconds+=Math.max(0,(Date.now()-Date.parse(clock.dataset.observed))/1000);clock.textContent=duration(seconds);clock.title=stale&&clock.dataset.source!=='sample'?'Last observation; refresh required':clock.dataset.source==='sample'?'Elapsed time since sample or trial start':'Elapsed time since process start';}}
async function refresh(force=false, background=false, fresh=false){
 if(refreshPromise)return refreshPromise;
 clearTimeout(pollTimer);fetching=true;blockingRefresh=!background;if(force&&!background)foregroundChecks=true;syncBusy();
 refreshPromise=(async()=>{
  try{
   if(!csrfToken){await loadSession();syncBusy();}
   const response=await apiFetch(force?'/api/status?refresh=1'+(fresh?'&fresh=1':''):'/api/status?refresh=0',{cache:'no-store'});
   if(response.status===401)sessionExpired();
   if(response.status===403)clearPrivateView();
   if(!response.ok)throw Error(response.status===504?'The dashboard request timed out (HTTP 504)':response.status===403?'Access not granted':`HTTP ${response.status}`);
   const data=await dashboardJSON(response);failed=false;render(data);return true;
  }catch(error){failed=true;foregroundChecks=false;waitingForObservation=false;if(!snapshot)clearPrivateView();$('notice').hidden=false;$('notice').textContent=`Dashboard unavailable: ${error.message}. Refresh to try again.`;tick();return false;}
  finally{fetching=false;blockingRefresh=false;initialized=true;syncBusy();}
 })();
 try{return await refreshPromise;}finally{refreshPromise=null;schedulePoll();}
}
async function refreshView(fresh=false){
 if(isBusy())return;operation=fresh?'Rechecking selected VMs…':null;syncBusy();
 try{await finishDashboardRead(true);await refresh(true,!fresh,fresh);}
 catch(error){if(!redirecting){$('notice').hidden=false;$('notice').textContent=error.message;}}
 finally{operation=null;syncBusy();schedulePoll();}
}
$('refresh').addEventListener('click',()=>refreshView());
$('recheck-vms').addEventListener('click',()=>refreshView(true));
async function finishDashboardRead(allowFailure=false){
 if(refreshPromise&&!await refreshPromise&&!allowFailure)throw Error('Dashboard refresh failed. Refresh the view before retrying.');
 if(redirecting)throw Error('Your session expired. Sign in again.');
}
$('dashboard-controls').addEventListener('click',event=>{if(isBusy()&&!event.target.closest('summary')){event.preventDefault();event.stopImmediatePropagation();}},true);
$('sign-out').addEventListener('click',async()=>{
 if(isBusy())return;operation='Signing out…';syncBusy();
 try{await finishDashboardRead();const response=await apiFetch('/api/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:'{}'});if(response.ok||response.status===401){redirecting=true;clearPrivateView();location.replace('/login');return;}throw Error('Sign out failed');}
 catch(error){$('notice').hidden=false;$('notice').textContent='Unable to sign out. Please try again.';}
 finally{operation=null;syncBusy();schedulePoll();}
});

function clearPrivateView(){pendingLaunch=null;$('window-notice').hidden=true;$('window-notice').replaceChildren();$('sample-global-status').hidden=true;$('sample-global-status').replaceChildren();snapshot=null;modelDrafts={};$('model-config-cards').replaceChildren();$('model-config-panel').hidden=true;clientLog.length=0;renderConsole();waitingForObservation=false;waitingForMaintenance=false;$('updates-panel').hidden=true;$('update-message').textContent='';$('update-cards').replaceChildren();$('update-jobs').replaceChildren();$('machines').replaceChildren();$('commands').replaceChildren();$('runs').replaceChildren();$('experiment-dialog').close();$('experiment-sample').replaceChildren();$('new-experiment').disabled=true;for(const role of ['scenarioforge','participant','core'])$('role-'+role).replaceChildren();$('role-panel').hidden=true;}
function renderRoles(data){
 $('roles-unavailable').hidden=Boolean(data.roles);
 const panel=$('role-panel');panel.hidden=!data.roles;if(!data.roles)return;
 for(const role of ['scenarioforge','participant','core']){
  const select=$('role-'+role),wanted=rolesDirty?select.value:String(data.roles[role]??'');
  const options=[new Option('Not selected',''),...(data.available_vms||[]).map(vm=>new Option(`VM ${vm.vmid} · ${vm.name||'Unnamed'} · ${vm.status||'unknown'}${vm.pool?' · '+vm.pool:''}`,String(vm.vmid)))];
  if(JSON.stringify([...select.options].map(o=>[o.value,o.text]))!==JSON.stringify(options.map(o=>[o.value,o.text])))select.replaceChildren(...options);
  select.value=[...select.options].some(o=>o.value===wanted)?wanted:'';
 }
 if(!rolesDirty&&!rolesSaving)$('roles-message').textContent=data.available_vms.length?'Selections are private to your account. Changes apply to subsequent runs.':'No available lab VMs. Check the configured inventory and your access.';
}
for(const role of ['scenarioforge','participant','core'])$('role-'+role).addEventListener('change',()=>{rolesDirty=true;});
$('role-form').addEventListener('submit',async event=>{
 event.preventDefault();if(isBusy()||rolesSaving)return;rolesSaving=true;operation='Saving VM roles and checking access…';syncBusy();
 try{
  await finishDashboardRead();
  if(!csrfToken)await loadSession();
  const roles=Object.fromEntries(['scenarioforge','participant','core'].map(role=>[role,$('role-'+role).value?Number($('role-'+role).value):null]));
  const response=await apiFetch('/api/roles',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(roles)});
  const body=await dashboardJSON(response);if(!response.ok)throw Error(response.status===504?'Saving VM roles timed out. Refresh to check whether they were saved.':body.error||'Unable to save roles');
  rolesDirty=false;operation='Loading your updated VM selections…';syncBusy();$('roles-message').textContent='VM roles saved. Refreshing your dashboard…';await refresh(true);
 }catch(error){$('roles-message').textContent=error.message;}finally{rolesSaving=false;operation=null;syncBusy();schedulePoll();}
});
let creationSteps=[];
function creationProgress(index,state,message){
 creationSteps[index]={state,message};const done=creationSteps.filter(step=>['complete','skipped'].includes(step.state)).length;
 $('experiment-save-progress').hidden=false;$('experiment-save-bar').value=done;
 $('experiment-save-status').textContent=state==='failed'?`Creation stopped · ${done} of 3 steps completed`:`${Math.round(done/3*100)}% · ${done} of 3 steps completed`;
 for(const [i,id] of ['local','push','record'].entries()){const step=creationSteps[i];if(step){$('experiment-save-'+id).textContent=step.message;$('experiment-save-'+id).dataset.state=step.state;}}
}
function startCreationProgress(saveModel){
 creationSteps=[{state:'pending',message:'Save file locally on orchestrator · pending'},{state:'pending',message:'Push settings to VM · pending'},{state:'pending',message:'Save experiment · pending'}];
 $('experiment-save-note').textContent=saveModel?'VM transfer uses one small guest-agent request: 0% until acknowledged, then 100%. Overall progress counts completed steps, not elapsed time.':'Using saved defaults; no model file needs to be pushed.';
 creationProgress(0,saveModel?'running':'skipped',saveModel?'Saving file locally on orchestrator…':'Local settings · using saved defaults');
 if(!saveModel)creationProgress(1,'skipped','VM push · not needed');
}
let modelDrafts={};
function modelFormSettings(role){
 return {provider:$(`model-${role}-provider`).value,url:$(`model-${role}-url`).value.trim(),model:$(`model-${role}-model`).value.trim(),ssl_verify:$(`model-${role}-ssl`).checked};
}
function modelSettingsChanged(role){
 const saved=modelDrafts[role]?.settings;if(!saved)return false;
 const current=modelFormSettings(role);
 return Object.keys(current).some(key=>current[key]!==saved[key])||Boolean($(`model-${role}-key`).value)||$(`model-${role}-clear`).checked;
}
function renderModelConfigs(data){
 $('model-config-panel').hidden=!data.owner;
 if(!data.owner)return;
 for(const role of ['participant']){
  const vmid=data.roles?.[role];let card=$('model-config-'+role);
  if(card&&card.dataset.vmid===String(vmid??'')){syncModelControls(role);continue;}
  delete modelDrafts[role];
  const form=el('form',null,'card model-config');form.id='model-config-'+role;form.dataset.vmid=vmid??'';
  const heading=el('div',null,'model-card-heading'),pull=el('button','Pull from VM');pull.type='button';pull.id=`model-${role}-read`;pull.addEventListener('click',()=>modelConfigAction(role,'read'));heading.append(el('h3','Cyber-agent-flow'),pull);
  form.append(heading,el('p',vmid?`VM ${vmid} · configs/cli.json`:'Select and save a VM first.','small'));
  const fields=[['provider','Provider'],['url','API base URL'],['model','Model name']];
  for(const [key,title] of fields){const label=el('label',title);const input=el(key==='provider'?'select':'input');input.id=`model-${role}-${key}`;input.name=key;input.required=true;
   if(key==='provider')for(const value of ['openai','litellm','ollama_direct','claude'])input.add(new Option(value==='openai'?'OpenAI / compatible':value,value));
   else{input.type=key==='url'?'url':'text';input.maxLength=2048;input.placeholder=key==='url'?'https://model-server.example/v1':'Model ID from your server';}
   label.append(input);form.append(label);
  }
  const tls=el('label','Verify TLS certificates '),checkbox=el('input');checkbox.type='checkbox';checkbox.checked=true;checkbox.id=`model-${role}-ssl`;tls.append(checkbox);form.append(tls);
  const keyLabel=el('label','Replacement API key'),key=el('input');key.type='password';key.autocomplete='new-password';key.maxLength=8192;key.placeholder='Leave blank to preserve the guest key';key.id=`model-${role}-key`;keyLabel.append(key);form.append(keyLabel);
  const clearLabel=el('label','Clear stored API key '),clear=el('input');clear.type='checkbox';clear.id=`model-${role}-clear`;clearLabel.append(clear);form.append(clearLabel);
  const actions=el('div',null,'result-actions'),apply=el('button','Apply settings');apply.type='button';apply.id=`model-${role}-apply`;apply.addEventListener('click',()=>{if(validateExperimentFields(form))modelConfigAction(role,'save');});actions.append(apply);form.append(actions);
  const message=el('p','Edit the saved experiment defaults, then select Apply settings. Pull from VM is optional and replaces the displayed values.','small');message.id=`model-${role}-message`;message.setAttribute('role','status');form.append(message);
  const changed=()=>{if(modelDrafts[role]){modelDrafts[role].dirty=modelSettingsChanged(role);delete modelDrafts[role].saveError;}syncModelControls(role);syncCreateExperiment();};
  form.addEventListener('input',changed);form.addEventListener('change',changed);
  form.addEventListener('submit',event=>{event.preventDefault();if(validateExperimentFields(form))modelConfigAction(role,'save');});
  if(card)card.replaceWith(form);else $('model-config-cards').append(form);
  const defaults=data.samples?.model_settings;
  if(defaults){for(const key of ['provider','url','model'])$(`model-${role}-${key}`).value=defaults[key]??'';$(`model-${role}-ssl`).checked=defaults.ssl_verify!==false;}
  modelDrafts[role]={vmid,token:null,dirty:false,settings:modelFormSettings(role)};
  syncModelControls(role);
 }
}
function syncModelControls(role){
 const form=$('model-config-'+role);if(!form)return;const available=Boolean(form.dataset.vmid),draft=modelDrafts[role],canWrite=Boolean(snapshot?.model_config_writable??snapshot?.updates?.can_update);
 for(const input of form.querySelectorAll('input,select'))input.disabled=!available||!canWrite;
 $(`model-${role}-read`).disabled=!available;
 $(`model-${role}-apply`).disabled=!available||!draft||(!draft.dirty&&!draft.saveError&&Boolean(draft.token))||!canWrite;
 if(!canWrite)$(`model-${role}-message`).textContent=`Model settings are read-only. You can create experiments using saved settings. Applying changes requires the ${snapshot?.updates?.group||'caf-maintainers'} group and enabled application maintenance.`;
}
async function modelConfigAction(role,action,creating=false){
 if(isBusy()&&!creating)return;
 if(rolesDirty){const message='Save VM role selections before changing model settings.';if(creating)throw Error(message);$(`model-${role}-message`).textContent=message;return;}
 if(action==='read'&&!confirm('Pull from VM will replace all model settings currently shown in this form. Continue?'))return;
 const body={role,action};if(action!=='read')body.token=modelDrafts[role]?.token;
 if(action==='save'){
  body.settings=modelFormSettings(role);
  const key=$(`model-${role}-key`);if($(`model-${role}-clear`).checked)body.api_key='';else if(key.value)body.api_key=key.value;key.value='';
 }
 operation=action==='read'?'Reading model configuration from the VM…':role==='participant'?'Saving model settings, synchronizing the participant route and checking connectivity…':'Saving model settings to the VM and experiments…';syncBusy();$(`model-${role}-message`).textContent=operation;
 try{
  await finishDashboardRead();
  if(action==='save'&&!modelDrafts[role]?.token){
   // Acquire the guest revision for optimistic concurrency without replacing
   // any form settings or API key captured before this background read.
   const current=await apiFetch('/api/model-config',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({role,action:'read'})});
   const fresh=await dashboardJSON(current);if(!current.ok)throw Error(fresh.error||'Unable to read the VM configuration before applying settings');
   body.token=fresh.token;modelDrafts[role].token=fresh.token;
  }
  if(creating){
   creationProgress(0,'running','Saving file locally on orchestrator…');
   const staged=await apiFetch('/api/model-config',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({role,action:'stage',token:body.token,settings:body.settings})});
   const stagedData=await dashboardJSON(staged);if(!staged.ok)throw Error(stagedData.error||'Unable to save local model draft');
   creationProgress(0,'complete','File saved locally on orchestrator · complete');
   creationProgress(1,'running','Pushing to VM · 0% acknowledged · awaiting guest save');
  }
  const response=await apiFetch('/api/model-config',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(body)});delete body.api_key;
  const data=await dashboardJSON(response);if(!response.ok)throw Error(data.error||'Model configuration operation failed');
  if(creating)creationProgress(1,'complete','VM push · 100% · guest save acknowledged');
  modelDrafts[role]={token:data.token,vmid:data.vmid,dirty:false};
  if(role==='participant'&&data.network_scope)applyModelNetworkScope(data.network_scope);
  for(const key of ['provider','url','model'])$(`model-${role}-${key}`).value=data.settings[key];$(`model-${role}-ssl`).checked=data.settings.ssl_verify;$(`model-${role}-key`).value='';$(`model-${role}-clear`).checked=false;
  modelDrafts[role].settings=modelFormSettings(role);
  $(`model-${role}-message`).textContent=(action==='save'?'Applied. These values are saved on the VM and will be used by new experiments. ':data.exists?'Pulled from VM. Edit the values, then select Apply settings. ':'No configuration file exists yet. Enter values, then select Apply settings. ')+(data.api_key_set?'A stored API key is present; its value stays in the VM. ':`No key stored in this file. Environment key: ${data.api_key_env}. `)+(data.routing?'Route: '+(data.routing.status==='updated'?`${data.routing.destination} through ${data.routing.interface}${data.routing.gateway?' via '+data.routing.gateway:''}. `:data.routing.message+'. '):'')+(data.routing?.helper_updated?`Routing helper updated; backup: ${data.routing.helper_backup}. `:'')+(data.routing?.dns?`DNS: ${data.routing.dns.domain} through ${data.routing.dns.server}. `:'')+(data.connectivity?data.connectivity.message+'. ':'')+(data.backup?'Backup: '+data.backup:'');
 }catch(error){$(`model-${role}-message`).textContent=error.message;if(action==='save'&&modelDrafts[role])modelDrafts[role].saveError=error.message;if(creating)throw error;}
 finally{delete body.api_key;if(!creating)operation=null;syncModelControls(role);syncBusy();if(!creating)schedulePoll();}
}

function showResults(id){return openRunWindow('results',id);}

function runActive(run){return run.coordinator_active||run.sample_progress?.active||['queued','stopping'].includes(run.recorded_status);}
function renderSamples(data){
 const samples=data.samples,vm=samples?.participant_vmid;
 $('new-experiment').disabled=!samples?.items?.length&&!data.scenarios?.enabled;
 if(scenarioChoiceVM!==null&&scenarioChoiceVM!==data.roles?.scenarioforge){scenarioChoices=[];scenarioChoiceVM=null;taskScenarioChanged();$('scenario-selection').replaceChildren(new Option('Reload scenarios for the selected VM',''));}
 $('samples-context').textContent=vm?`New experiment defaults: Participant VM ${vm} · ${samples.provider} / ${samples.model}. Saved experiments retain their own settings.`:'Save a participant VM on Lab setup before running an experiment.';
}
function iconAction(label,icon,disabled,callback){const button=el('button',null,'icon-action');button.type='button';button.title=label;button.setAttribute('aria-label',label);const glyph=el('span',icon);glyph.setAttribute('aria-hidden','true');button.append(glyph);button.disabled=disabled;button.addEventListener('click',callback);return button;}
function renderExperiments(data){
 const runs=el('tbody');$('no-runs').hidden=Boolean(data.runs.length);
 const active=data.runs.some(runActive),items=data.samples?.items||[];
 for(const run of [...data.runs].sort((a,b)=>(b.sample_progress?.started_at||'').localeCompare(a.sample_progress?.started_at||''))){
  const id=run.output?.split('/').pop()||run.workflow_id||'',sample=items.find(s=>s.id===run.sample_id),live=runActive(run),ready=run.recorded_status==='ready',p=run.sample_progress,summary=run.evaluation?.summary;
  const row=el('tr');row.dataset.runId=id;row.dataset.displayKey=id;const title=el('td');title.append(el('strong',sample?.name||run.scenario_experiment?.scenario||run.workflow_id||id),el('div',id,'small'));
  if(run.saved_settings){const settings=run.saved_settings;title.append(el('div',`VM ${settings.participant_vmid} · ${settings.provider} / ${settings.model}`,'small'));}
  const state=el('td');state.append(badge(run.recorded_status||'unavailable',run.recorded_status==='completed'?'good':['failed','cancelled','interrupted','completed_with_errors'].includes(run.recorded_status)?'warn':''));if(run.message)state.append(el('div',run.message,'small'));
  if(run.trial_failures?.length)state.append(el('div',`${run.trial_failures.length} trial error(s) · Open Progress or Results for details`,'error'));
  const actions=el('td',null,'experiment-actions');
  actions.append(iconAction(run.scenario_experiment?(ready?'Deploy and run':'Deploy and run again'):(ready?'Run experiment':'Run again'),'▶',!data.owner||!(sample||run.scenario_experiment)||!(run.saved_settings?.participant_vmid||data.samples.participant_vmid)||active||sampleStarting,()=>experimentAction('run',id)));
  actions.append(iconAction(run.scenario_experiment?'Stop after current stage or trial':'Stop after current trial','■',!data.owner||!(run.sample_id||run.scenario_experiment)||!live||run.recorded_status==='stopping',()=>experimentAction('stop',id)));
  actions.append(iconAction('View results','▤',!data.owner||ready,()=>showResults(id)));
  actions.append(iconAction('Open progress','◴',!p||ready,()=>openProgress(id)));
  if(live)actions.append(agentTranscriptButton(run,data.backend_type));
  row.append(title,state,el('td',p?`${p.finished_trials} / ${p.planned_trials}`:run.evaluation?`${summary.trials_observed} / ${run.evaluation.planned_trials}`:'—'),el('td',p?.verified_successes??summary?.verified_successes??'—'),actions);runs.append(row);
 }
 updateDisplayList($('runs'),runs);
}
function openProgress(id){return openRunWindow('progress',id);}
function experimentMissingFields(){
 if(!snapshot||!$('experiment-sample').value)return 'Choose an experiment type.';
 if(rolesDirty)return 'Save your VM role selections in Lab setup.';
 const scenario=$('experiment-sample').value==='scenarioforge-xml';
 const roles=scenario?['scenarioforge','participant']:['scenarioforge','participant','core'];
 if(roles.some(role=>!snapshot.roles?.[role]))return 'Select and save '+(scenario?'ScenarioForge and participant':'ScenarioForge, participant and CoreVM')+' roles in Lab setup.';
 if(scenario){
  const choice=scenarioChoices.find(item=>item.id===$('scenario-selection').value);
  if(scenarioChoiceVM!==snapshot.roles.scenarioforge||!choice?.resolved_chain)return 'Choose a resolved scenario on the ScenarioForge tab.';
  if(!$('scenario-allowed').value.trim())return 'Enter allowed target IPs or CIDRs on the Cyber-agent-flow tab.';
 }
 const taskError=taskValidationMessage();if(taskError)return 'Evaluation: '+taskError;
 const invalid=[...$('experiment-form').elements].find(input=>input.willValidate&&(!input.validity.valid||(input.required&&typeof input.value==='string'&&!input.value.trim())));
 if(invalid){const panel=invalid.closest('[role="tabpanel"]');return 'Complete the required fields on the '+(panel?$(panel.getAttribute('aria-labelledby')).textContent:'Experiment')+' tab.';}
 if(modelDrafts.participant){
  const form=$('model-config-participant');
  if(!form||[...form.elements].some(input=>input.willValidate&&(!input.validity.valid||(input.required&&!input.value.trim()))))return 'Complete the required model fields on the Cyber-agent-flow tab.';
  // Pulling settings is read-only. Maintenance access is needed only when
  // applying edits, not when creating an experiment with saved defaults.
  if(modelDrafts.participant.dirty||modelDrafts.participant.saveError){
   if(!(snapshot.model_config_writable??snapshot.updates?.can_update))return 'Applying changed model settings requires application maintenance access.';
   if(modelDrafts.participant.saveError)return 'Model settings were not applied: '+modelDrafts.participant.saveError;
   return 'Apply the changed model settings on the Cyber-agent-flow tab.';
  }
 }
 return '';
}
function syncScenarioReferences(){
 for(const button of document.querySelectorAll('[data-scenario-reference]'))button.disabled=!(scenarioChoiceVM===snapshot?.roles?.scenarioforge&&$('scenario-selection').value&&$('experiment-sample').value==='scenarioforge-xml');
}
for(const button of document.querySelectorAll('[data-scenario-reference]'))button.addEventListener('click',()=>{
 const selection=$('scenario-selection').value,kind=button.dataset.scenarioReference;
 const url='/scenario-reference?selection='+encodeURIComponent(selection)+'&kind='+encodeURIComponent(kind);
 const opened=window.open(url,'caf_reference_'+kind,'popup,width=1100,height=850,resizable=yes,scrollbars=yes');
 if(opened){opened.focus();$('scenario-reference-notice').hidden=true;}else{const notice=$('scenario-reference-notice');notice.hidden=false;notice.replaceChildren(el('span','Allow popups for this site, or '));const link=el('a','open the scenario reference');link.href=url;link.target='_blank';link.rel='noopener';notice.append(link);}
});
function syncCreateExperiment(){
 syncJudge();
 syncScenarioReferences();
 const busy=isBusy()||experimentCreating;
 const reason=busy?'Wait for the current operation to finish.':experimentMissingFields();
 $('create-experiment').disabled=Boolean(reason);
 const hint=$('create-experiment-hint');if(hint.textContent!==reason)hint.textContent=reason;
 hint.hidden=!reason;
}
$('experiment-dialog').addEventListener('input',syncCreateExperiment);
$('experiment-dialog').addEventListener('change',syncCreateExperiment);

function selectExperimentTab(name,{focus=false}={}){
 for(const button of document.querySelectorAll('[data-experiment-tab]')){
  const selected=button.dataset.experimentTab===name;
  button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;
  $('experiment-panel-'+button.dataset.experimentTab).hidden=!selected;
  if(selected&&focus)button.focus();
 }
 document.querySelector('.experiment-tab-content').scrollTop=0;
}
const experimentTabs=[...document.querySelectorAll('[data-experiment-tab]')];
for(const button of experimentTabs){
 button.addEventListener('click',()=>selectExperimentTab(button.dataset.experimentTab));
 button.addEventListener('keydown',event=>{
  const index=experimentTabs.indexOf(button);
  const target=event.key==='ArrowRight'?(index+1)%experimentTabs.length:event.key==='ArrowLeft'?(index+experimentTabs.length-1)%experimentTabs.length:event.key==='Home'?0:event.key==='End'?experimentTabs.length-1:null;
  if(target!==null){event.preventDefault();selectExperimentTab(experimentTabs[target].dataset.experimentTab,{focus:true});}
 });
}
function validateExperimentFields(form){
 const invalid=[...form.elements].find(input=>input.willValidate&&!input.validity.valid);
 if(!invalid)return true;
 const panel=invalid.closest('[role="tabpanel"]');
 if(panel)selectExperimentTab(panel.id.replace('experiment-panel-',''));
 invalid.reportValidity();invalid.focus();return false;
}

function judgeSettings(){
 const enabled=$('judge-enabled').checked;
 if(!enabled)return {enabled:false};
 const settings={enabled:true,use_participant_model:$('judge-inherit').checked,...Object.fromEntries(['max_turns','timeout_seconds','max_tokens'].map(key=>[key,Number($('judge-'+key).value)]))};
 if(!settings.use_participant_model)settings.model={provider:$('judge-provider').value,url:$('judge-url').value.trim(),name:$('judge-name').value.trim(),ssl_verify:$('judge-ssl').checked};
 return settings;
}
function syncJudge(){
 const enabled=$('judge-enabled').checked;
 $('judge-inherit').disabled=!enabled;
 $('judge-model-settings').disabled=!enabled||$('judge-inherit').checked;
 if(enabled&&$('judge-inherit').checked&&$('model-config-participant')){const current=modelFormSettings('participant');$('judge-provider').value=current.provider;$('judge-url').value=current.url;$('judge-name').value=current.model;$('judge-ssl').checked=current.ssl_verify;}
 $('judge-limits').disabled=!enabled;
}
function resetJudge(){
 const config=snapshot?.experiment_defaults?.judge;
 $('judge-enabled').checked=config?.enabled!==false;
 $('judge-inherit').checked=config?.use_participant_model??!config?.model;
 const model=config?.model||snapshot?.experiment_defaults?.model||{};
 $('judge-provider').value=['openai','litellm','ollama_direct'].includes(model.provider)?model.provider:'openai';
 $('judge-url').value=model.url||'';$('judge-name').value=model.name||'';$('judge-ssl').checked=model.ssl_verify!==false;
 for(const [key,value] of Object.entries({max_turns:6,timeout_seconds:120,max_tokens:2048}))$('judge-'+key).value=config?.[key]??value;
 syncJudge();
}
for(const id of ['judge-enabled','judge-inherit'])$(id).addEventListener('change',()=>{syncJudge();syncCreateExperiment();});

let customEvaluation={};
const evaluationKeys=['repetitions','max_turns','wall_seconds','tool_timeout','context_window','max_tries_before_solution'];
function renderEvaluationSettings(scenario,sample){
 const defaults={max_tries_before_solution:6,...(snapshot?.experiment_defaults?.evaluation||{repetitions:1,max_turns:snapshot?.scenarios?.max_turns||20,wall_seconds:snapshot?.scenarios?.wall_seconds||300,tool_timeout:60,context_window:8192})};
 const preset=scenario?defaults:{...defaults,repetitions:sample?.profile?.repetitions||1,max_turns:sample?.max_turns||3,wall_seconds:sample?.wall_seconds||120,tool_timeout:sample?.profile?.tool_timeout||30};
 const values=customEvaluation[$('experiment-sample').value]||preset;
 for(const key of evaluationKeys)$('eval-'+key).value=values[key];
 $('evaluation-settings').disabled=false;
 configureEvaluationTasks(scenario,sample);
 $('sample-scenario-info').hidden=scenario;
 if(!scenario){
  $('sample-scenario-xml').href='/demo-'+sample.id+'.xml';
  $('sample-scenario-bundle').href='/demo-'+sample.id+'.zip';
  $('sample-sf-use').value=sample?.profile?.scenarioforge_used?'Required · deploy, readiness check and evaluation export':'Not used by this bundled sample';
  $('sample-sf-companion').value=(sample?.profile?.companion_xml||'Unavailable')+' · fixed sample XML';
  $('sample-environment').value=sample?.profile?.environment||'Sample-defined environment';
  $('sample-prompt').value=sample?.profile?.prompt||'Sample-defined prompt';
 }
 $('eval-settings-note').textContent=scenario?'These settings are saved with this experiment. Reruns reuse them.':'Sample defaults are prefilled. Trial settings are editable and saved with this experiment; reruns reuse them. Sample tasks and tools remain fixed.';
 $('eval-tools').textContent='Tools / conditions: '+(scenario?'Baseline — nmap, curl, python3. Custom tool conditions are not editable here yet.':sample?.profile?.tools||'Sample-defined');
 $('eval-task-source').textContent=scenario?'ScenarioForge exports the selected task definitions after deployment. Prompts and private verifiers are captured in Results.':'The deployed host address and fresh token/flags are resolved at run time. Exact prompts and XML are captured in Results.';
}
function updateEvaluationBudget(){
 const selection=$('experiment-sample').value;
 $('experiment-budget').textContent=selection==='scenarioforge-xml'?'Baseline tools · configure repetitions and trial limits below':`${Number($('eval-repetitions').value)*(selection==='tools-vs-helper'?2:1)} trials · up to ${$('eval-max_turns').value} turns and ${$('eval-wall_seconds').value}s per trial`;
}
$('evaluation-settings').addEventListener('input',()=>{if(!$('evaluation-settings').disabled){customEvaluation[$('experiment-sample').value]=Object.fromEntries(evaluationKeys.map(key=>[key,Number($('eval-'+key).value)]));updateEvaluationBudget();}});

function describeSample(){
 const scenario=$('experiment-sample').value==='scenarioforge-xml',sample=snapshot?.samples?.items.find(s=>s.id===$('experiment-sample').value);
 $('scenario-options').hidden=!scenario;
 $('scenario-caf-scope').hidden=!scenario;
 $('scenario-allowed').required=scenario;
 renderEvaluationSettings(scenario,sample);
 $('experiment-description').textContent=scenario?'Deploy and evaluate an existing ScenarioForge scenario.':sample?.description||'';
 updateEvaluationBudget();
}

$('new-experiment').addEventListener('click',async()=>{if(isBusy())return;customEvaluation={};resetJudge();lastUploadedScenarioFile=null;$('scenario-file').value='';syncScenarioUploadButton();$('provide-progressive-hints').checked=false;resetTaskEditor();const select=$('experiment-sample');select.replaceChildren();for(const sample of snapshot?.samples?.items||[])select.add(new Option('Sample - '+sample.name,sample.id));if(snapshot?.scenarios?.enabled)select.add(new Option('ScenarioForge XML or bundle','scenarioforge-xml'));$('scenario-allowed').value='';$('scenario-disallowed').value=snapshot?.scenarios?.disallowed_targets||'';$('experiment-error').textContent='';$('experiment-save-progress').hidden=true;creationSteps=[];describeSample();selectExperimentTab('overview');$('experiment-dialog').showModal();describeScenarioSelection();syncCreateExperiment();await loadModelNetworkScope();});
let scenarioChoices=[],scenarioChoiceVM=null,lastUploadedScenarioFile=null;
let autoExcludedTargets=[];
function applyModelNetworkScope(scope){
 const input=$('scenario-disallowed');
 const existing=input.value.split(',').map(value=>value.trim()).filter(Boolean).filter(value=>!autoExcludedTargets.includes(value));
 autoExcludedTargets=scope.excluded_targets||[];
 input.value=[...new Set([...existing,...autoExcludedTargets])].join(', ');
 $('scenario-scope-info').textContent=`LLM provider: ${scope.provider_host} · excluded: ${autoExcludedTargets.join(', ')||'unresolved'}. ${(scope.warnings||[]).join(' ')}`;
 syncCreateExperiment();
}
async function loadModelNetworkScope(){
 autoExcludedTargets=[];
 if(!snapshot?.roles?.participant)return;
 operation='Resolving the applied LLM provider and route on the participant VM…';syncBusy();
 try{
  const response=await apiFetch('/api/model-network-scope',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:'{}'});
  const scope=await dashboardJSON(response);if(!response.ok)throw Error(scope.error||'Unable to resolve the LLM route');
  applyModelNetworkScope(scope);
 }catch(error){$('scenario-scope-info').textContent='Review exclusions: '+error.message;}
 finally{operation=null;syncBusy();schedulePoll();}
}
function scenarioModifiedTime(item){const epoch=Number(item?.modified_epoch);if(Number.isFinite(epoch))return epoch*1000;const parsed=Date.parse(item?.modified_at||'');return Number.isFinite(parsed)?parsed:0;}
function scenarioModifiedLabel(item){const value=scenarioModifiedTime(item);return value?new Date(value).toLocaleString([], {dateStyle:'short',timeStyle:'short'}):'timestamp unavailable';}
function scenarioFileIdentity(file){return file?file.name+'\0'+file.size+'\0'+file.lastModified:'';}
function syncScenarioUploadButton(){const file=$('scenario-file').files[0];$('upload-scenario').disabled=!file||scenarioFileIdentity(file)===lastUploadedScenarioFile;}
for(const choice of document.querySelectorAll('input[name="scenario-source"]'))choice.addEventListener('change',()=>{
 const upload=document.querySelector('input[name="scenario-source"]:checked').value==='upload';
 $('scenario-upload-options').hidden=!upload;$('scenario-find-options').hidden=upload;
 scenarioChoices=[];scenarioChoiceVM=null;taskScenarioChanged();
 $('scenario-selection').replaceChildren(new Option(upload?'Upload a scenario to choose an XML':'Load scenarios to choose an XML',''));
 $('scenario-allowed').value='';$('scenario-selection-info').textContent='';$('scenario-upload-info').textContent='Import saves the scenario on the selected VM; it does not deploy it. Maximum 32 MiB (128 MiB expanded).';
 $('scenario-file').value='';syncScenarioUploadButton();syncCreateExperiment();
});
function describeScenarioSelection(){
 const choice=scenarioChoices.find(item=>item.id===$('scenario-selection').value);
 $('scenario-allowed').value=(choice?.target_subnets||[]).join(', ');
 syncCreateExperiment();
 $('scenario-selection-info').textContent=choice?choice.path+' · '+choice.scenario+' · modified '+scenarioModifiedLabel(choice)+' · '+choice.chain_length+' Flow steps':'Select a saved scenario with a resolved Flow chain.';
}
$('scenario-selection').addEventListener('change',()=>{describeScenarioSelection();taskScenarioChanged();});
$('load-scenarios').addEventListener('click',async()=>{
 if(isBusy())return;
 operation='Loading saved scenarios from the ScenarioForge VM…';syncBusy();
 try{
  const response=await apiFetch('/api/scenarios/list',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({query:''})});
  const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Unable to load scenarios');
  showScenarioChoices(result);
  $('scenario-selection-info').textContent=result.items.length?(result.truncated?'Showing the 50 most recently modified scenarios found on VM '+result.vmid+'.':'Choose a scenario from VM '+result.vmid+'; newest files are listed first.'):'No saved XML found in '+result.roots.join(', ')+'.';
 }catch(error){$('scenario-selection-info').textContent=error.message;}
 finally{operation=null;syncBusy();schedulePoll();}
});

function showScenarioChoices(result){
 scenarioChoices=[...result.items].sort((left,right)=>scenarioModifiedTime(right)-scenarioModifiedTime(left)||String(left.scenario||'').localeCompare(String(right.scenario||''))||String(left.path||'').localeCompare(String(right.path||'')));scenarioChoiceVM=result.vmid;taskScenarioChanged();
 const select=$('scenario-selection');select.replaceChildren(new Option('Choose a saved scenario',''));
 $('scenario-allowed').value='';
 for(const item of scenarioChoices){const option=new Option(scenarioModifiedLabel(item)+' — '+item.scenario+' — '+item.path.split('/').pop()+(item.resolved_chain?'':' (resolve Flow chain first)'),item.id);option.disabled=!item.resolved_chain;select.add(option);}
}
async function sendScenario(file){
 if(isBusy())return;
 if(rolesDirty||!snapshot?.roles?.scenarioforge){$('scenario-upload-info').textContent='Select and save a ScenarioForge VM on Lab setup first.';return;}
 if(!file||file.size===0||file.size>32*1024*1024){$('scenario-upload-info').textContent='Choose an XML or ScenarioForge reproduction ZIP up to 32 MiB.';return;}
 operation='Uploading and importing scenario on the ScenarioForge VM…';syncBusy();
 try{
  await finishDashboardRead();
  const response=await apiFetch('/api/scenarios/upload',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':csrfToken},body:file});
  const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Scenario import failed');
  showScenarioChoices(result);
  const ready=result.items.filter(item=>item.resolved_chain);
  if(ready.length===1)$('scenario-selection').value=ready[0].id;
  describeScenarioSelection();
  $('scenario-upload-info').textContent='Imported to VM '+result.vmid+' · '+result.kind+' · '+result.fidelity+'. '+(ready.length?'Select the scenario below, review scope, then create the experiment.':'Open the imported XML in ScenarioForge to resolve and save its Flow chain, then reload it here.');
  lastUploadedScenarioFile=scenarioFileIdentity(file);
  $('scenario-file').value='';
 }catch(error){$('scenario-upload-info').textContent=error.message;}
 finally{operation=null;syncScenarioUploadButton();syncBusy();schedulePoll();}
}
$('scenario-file').addEventListener('change',syncScenarioUploadButton);
$('upload-scenario').addEventListener('click',()=>sendScenario($('scenario-file').files[0]));

$('experiment-sample').addEventListener('change',describeSample);
$('cancel-experiment').addEventListener('click',()=>{if(!experimentCreating&&!operation)$('experiment-dialog').close();});
$('experiment-dialog').addEventListener('cancel',event=>{if(experimentCreating||operation)event.preventDefault();});
$('experiment-form').addEventListener('submit',async event=>{
 event.preventDefault();if(isBusy()||experimentCreating)return;
 const missing=experimentMissingFields();if(missing){syncCreateExperiment();return;}
 const scenario=$('experiment-sample').value==='scenarioforge-xml';
 if(!validateExperimentFields($('experiment-form')))return;
 if(scenario&&(!scenarioChoices.some(item=>item.id===$('scenario-selection').value)||rolesDirty)){selectExperimentTab('scenario');$('experiment-error').textContent=rolesDirty?'Save VM roles before creating an experiment.':'Load and choose a saved scenario first.';return;}
 $('experiment-error').textContent='';
 startCreationProgress(false);
 experimentCreating=true;operation='Creating experiment…';syncBusy();for(const input of $('experiment-form').querySelectorAll('button,select'))input.disabled=true;
 try{creationProgress(2,'running','Waiting for earlier dashboard reads to finish…');operation='Creating experiment · Waiting for earlier dashboard reads to finish…';syncBusy();await finishDashboardRead();const response=await experimentRequestWithProgress({request_id:crypto.randomUUID().replaceAll('-',''),judge:judgeSettings(),...($('provide-progressive-hints').checked?{provide_progressive_hints:true}:{}),...(scenario?{selection_id:$('scenario-selection').value,allowed_targets:$('scenario-allowed').value,disallowed_targets:$('scenario-disallowed').value,...(experimentTasks()!==null?{tasks:experimentTasks()}:{} )}:{sample_id:$('experiment-sample').value}),evaluation:Object.fromEntries(evaluationKeys.map(key=>[key,Number($('eval-'+key).value)]))});const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Unable to create experiment');creationProgress(2,'complete','Experiment saved · complete');$('experiment-dialog').close();$('sample-message').textContent='Experiment created. Press its Run icon when ready.';await refresh();}
 catch(error){const current=creationSteps.findIndex(step=>step.state==='running');if(current>=0)creationProgress(current,'failed',creationSteps[current].message+' · failed: '+error.message);$('experiment-error').textContent=error.message;}
 finally{experimentCreating=false;operation=null;for(const input of $('experiment-form').querySelectorAll('button,select'))input.disabled=false;syncBusy();schedulePoll();}
});
async function experimentRequestWithProgress(payload,action="create"){
 let finished=false,timer=null,lastStatus='Submitting request and checking access…';
 const started=Date.now(),title=action==='run'?'Starting experiment':'Creating experiment';
 const render=()=>{if(finished)return;const label=`${title} · ${lastStatus} · ${Math.floor((Date.now()-started)/1000)}s elapsed`;operation=label;if(action==='create')creationProgress(2,'running',label);else if(pendingLaunch){pendingLaunch.message=label;renderSampleActivity(snapshot||{runs:[]});}syncBusy();};
 render();
 const heartbeat=setInterval(render,1000);
 const poll=async()=>{
  const controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),10000);
  try{
   const response=await apiFetch('/api/experiments/'+(action==='run'?'start-status':'creation-status')+'?request_id='+encodeURIComponent(payload.request_id),{cache:'no-store',signal:controller.signal});
   if(!response.ok)throw Error(`Progress status HTTP ${response.status}`);
   const status=await dashboardJSON(response);
   if(!finished){lastStatus=(status.total?`Step ${status.step}/${status.total}`:'Awaiting server checkpoint')+' · '+status.message;render();}
  }catch(error){if(!finished){lastStatus=`Progress update unavailable (${error.name==='AbortError'?'request timed out':error.message}); retrying. Creation/launch request is still pending`;render();}}
  finally{clearTimeout(deadline);}
  if(!finished)timer=setTimeout(poll,2000);
 };
 timer=setTimeout(poll,250);
 try{return await apiFetch('/api/experiments/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(payload)});}
 finally{finished=true;clearTimeout(timer);clearInterval(heartbeat);}
}

async function experimentAction(action,id){
 if(isBusy()||sampleStarting)return;
 if(action==='run'&&rolesDirty){$('sample-message').textContent='Save VM role changes on Lab setup before running.';return;}
 if(action==='run'){const saved=snapshot?.runs.find(run=>run.output.split('/').pop()===id);pendingLaunch={...saved,output:saved?.output||id,recorded_status:'starting',message:'Submitting experiment to the coordinator…'};renderSampleActivity(snapshot);}
 sampleStarting=true;operation=action==='stop'?'Requesting experiment stop…':'Starting experiment…';syncBusy();
 try{await finishDashboardRead();const payload={run_id:id,...(action==='run'?{request_id:crypto.randomUUID().replaceAll('-','')}:{})};const response=action==='run'?await experimentRequestWithProgress(payload,'run'):await apiFetch('/api/experiments/stop',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(payload)});const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Experiment action failed');$('sample-message').textContent=action==='stop'?'Stop requested. The current stage or trial will finish and results will be collected.':'Experiment started.';const link=el('a',' Open progress ↗');link.href=runWindowURL('progress',result.run_id);link.addEventListener('click',event=>{event.preventDefault();openProgress(result.run_id);});$('sample-message').append(link);await refresh();}
 catch(error){$('sample-message').textContent=error.message;}
 finally{sampleStarting=false;operation=null;pendingLaunch=null;if(snapshot){renderExperiments(snapshot);renderSampleActivity(snapshot);}syncBusy();schedulePoll();}
}
function renderSampleActivity(data){
 const active=(data.runs||[]).filter(runActive),target=$('sample-global-status');
 if(pendingLaunch&&!active.some(run=>run.output===pendingLaunch.output))active.unshift(pendingLaunch);
 target.hidden=!active.length;
 const next=el('div'),heading=el('h2','Run queue');heading.dataset.displayKey='heading';next.append(heading);
 for(const run of active){const id=run.output.split('/').pop(),p=run.sample_progress,card=el('article',null,'queued-run');card.dataset.displayKey=id;
  const name=p?.name||run.scenario_experiment?.scenario||run.workflow_id||id;
  card.append(el('strong',name),badge(run.recorded_status),el('p',run.message||p?.current_trial?.transport?.activity||'Waiting for the next stage.','small'));
  const agent=agentTranscriptButton(run,data.backend_type);agent.disabled=run===pendingLaunch;card.append(agent);
  const progress=el('a','Open progress');progress.href=runWindowURL('progress',id);progress.target='_blank';progress.rel='noopener';card.append(progress,el('p','Closing a window keeps this run in the queue.','small'));next.append(card);
 }
 updateDisplayList(target,next);
}

const maintenanceRefs={};
function syncUpdateControls(loadingLabel){
 const updates=snapshot?.updates;if(!updates)return;
 for(const app of updates.applications){
  const status=$(`update-status-${app.role}`);if(!status)continue;
  const permission=updates.can_update?'':`Update permission required: your signed-in account must belong to the ${updates.group} PVE group. Orchestration access alone does not grant update access.`;
  const selection=app.vmid?'':'Select and save a VM for this application on Lab setup.';
  const waiting=loadingLabel?`Please wait: ${loadingLabel}`:'';
  status.textContent=[permission,selection,waiting].filter(Boolean).join(' ')||'Ready to check, update or roll back this application.';
  status.classList.toggle('error',Boolean(permission));
  const input=document.querySelector(`[data-update-ref="${app.role}"]`);
  input.disabled=Boolean(permission||selection||waiting);
  for(const button of document.querySelectorAll(`[data-update-role="${app.role}"]`)){
   const reason=[button.dataset.updateAction==='inspect'?'':permission,selection,waiting].filter(Boolean).join(' ');
   button.disabled=Boolean(reason);button.title=reason;
  }
 }
}
function renderUpdates(data){
 const updates=data.updates;$('updates-panel').hidden=!updates;$('applications-unavailable').hidden=Boolean(updates);if(!updates)return;
 $('maintenance-count').textContent=`${updates.jobs?.length||0} recent jobs`;
 $('updates-context').textContent=updates.can_update?'Updates preserve local data and reuse installed dependencies. The application must be idle; its configured service is restarted when necessary.':`Check installed versions here. Updates and rollback require membership in the ${updates.group} PVE group.`;
 const cards=$('update-cards');
 // Preserve input focus while the dashboard polls.
 if(!document.activeElement?.matches('#update-cards input[data-update-ref]')){
  cards.replaceChildren();
  for(const app of updates.applications){
   const card=el('article',null,'card update-card');card.append(el('h3',app.role==='participant'?'Cyber-agent-flow':'ScenarioForge'),el('p',app.vmid?`VM ${app.vmid}`:'Choose and save a VM on Lab setup','small'));
   const known=(updates.jobs||[]).find(job=>job.role===app.role&&job.vmid===app.vmid&&job.installed);
   if(known){const info=known.installed;card.append(el('p',`Installed revision (last checked): ${info.revision.slice(0,12)}${info.modified?' · local edits':''}`,'small'));if(info.missing_controls?.length)card.append(el('p',`Missing evaluator controls: ${info.missing_controls.join(', ')}`,'error'));
    if(info.processes?.length){card.append(el('p','Processes referencing this checkout (up to 12):','small'));for(const process of info.processes)card.append(el('p',`PID ${process.pid} · ${process.name} · ${process.reason}`,'small'));card.append(el('p','Update asks for confirmation before stopping these processes. A configured service is restarted after activation.','small'));}
    if(info.modified){card.append(el('p',info.tools_config_replaceable?'Legacy kali_tools.json change detected. Update will back up and replace this runtime catalog with shipped defaults.':'Tracked local edits block updates and rollback. Preserve/commit them in the VM, then check again.','error'));if(info.modified_files?.length){card.append(el('pre',info.modified_files.join('\n'),'small'));card.append(el('p',`Showing ${info.modified_files.length} of ${info.modified_file_count??info.modified_files.length} changed files (paths limited to 300 characters).`,'small'));}}
   }
   const label=el('label','Branch, tag or commit','small'),input=el('input');input.type='text';input.value=maintenanceRefs[app.role]||app.ref;input.dataset.updateRef=app.role;input.addEventListener('input',()=>{maintenanceRefs[app.role]=input.value;});label.append(input);card.append(label);
   const status=el('p',null,'small');status.id=`update-status-${app.role}`;card.append(status);input.setAttribute('aria-describedby',status.id);
   const actions=el('div',null,'update-actions');
   for(const [action,title] of [['inspect','Check version'],['update','Update'],['rollback','Roll back']]){const button=el('button',title);button.type='button';button.dataset.updateRole=app.role;button.dataset.updateAction=action;button.setAttribute('aria-describedby',status.id);button.addEventListener('click',()=>maintain(app.role,action,input.value,action==='update'&&known?.installed.processes?.length?known:null));actions.append(button);}
   card.append(actions);cards.append(card);
  }
 }
 const jobs=$('update-jobs');jobs.replaceChildren();
 const latest=updates.jobs?.[0];
 if(latest){$('update-message').textContent=`Latest maintenance · ${latest.role} · VM ${latest.vmid} · ${latest.action}: ${latest.status}. ${latest.error||latest.message}`;$('update-message').classList.toggle('error',['failed','interrupted'].includes(latest.status));}
 for(const job of updates.jobs||[]){const row=el('article',null,'command');row.append(badge(job.status,job.status==='completed'?'good':job.status==='failed'?'warn':''),el('span',` ${job.role} · VM ${job.vmid} · ${job.action}`,'command-title'),el('p',job.message,'small'));if(job.console?.transfer){const transfer=job.console.transfer,progress=el('progress');progress.max=100;progress.value=transfer.percent;progress.setAttribute('aria-label',`${job.role} source bundle upload`);row.append(el('p',`${transfer.sent_bytes.toLocaleString()} / ${transfer.total_bytes.toLocaleString()} bytes acknowledged · ${transfer.percent}% · ${(transfer.bytes_per_second/1024).toFixed(1)} KiB/s · ${transfer.verified?'Upload checksum verified; see activation outcome below':['queued','running'].includes(job.status)?'Awaiting verified completion':'Transfer stopped; file not verified'}`,'small'),progress);if(!transfer.verified)row.append(el('p',`${transfer.sent_bytes?'Last acknowledgement':'Transfer started'}: ${transfer.updated_at}. Open the troubleshooting console below for command history.`,'small'));}if(job.revision)row.append(el('p',`Target revision: ${job.revision}`,'small'));if(job.action!=='inspect')row.append(el('p',job.status==='completed'?'Activation completed':['failed','interrupted'].includes(job.status)?'Activation not confirmed. Check version for the current installed revision.':'Activation pending','small'));if(job.error)row.append(el('p',job.error,'error'));const details=el('details'),summary=el('summary','Details');details.append(summary,el('pre',JSON.stringify(job,null,2)));row.append(details);jobs.append(row);}
 if(!(updates.jobs||[]).length)jobs.append(el('p','No application maintenance recorded for your account.','empty'));
}
async function maintain(role,action,ref,confirmation=null){
 if(isBusy()||maintenanceStarting)return;
 if(rolesDirty){$('update-message').textContent='Save VM role changes before application maintenance.';return;}
 if(confirmation&&!confirmation.installed.processes.every(p=>/^[0-9a-f]{64}$/.test(p.identity||''))){$('update-message').textContent='Click Check version to refresh the process list, then click Update to confirm stopping it.';return;}
 if(confirmation&&!window.confirm(`Stop these ${role} processes on VM ${confirmation.vmid} and update to ${ref}?\n\n${confirmation.installed.processes.map(p=>`PID ${p.pid} · ${p.name} · ${p.reason}`).join('\n')}\n\nThis sends SIGTERM. Listed terminal windows/sessions may close and unsaved work may be lost. Unmanaged applications will not restart automatically. Changed processes require a fresh confirmation.`))return;
 maintenanceStarting=true;operation='Submitting application maintenance…';syncBusy();if(snapshot)renderUpdates(snapshot);
 try{
  await finishDashboardRead();
  if(!csrfToken)await loadSession();
  const response=await apiFetch('/api/applications',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({role,action,ref,request_id:crypto.randomUUID().replaceAll('-',''),...(confirmation?{process_confirmation:confirmation.id}:{})})});
  const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||`Maintenance request failed (HTTP ${response.status})`);
  $('update-message').textContent=`Maintenance ${result.id} submitted. Follow its status below.`;await refresh();
 }catch(error){$('update-message').textContent=error.message;}
 finally{maintenanceStarting=false;operation=null;syncBusy();schedulePoll();}
}

// Progress reads collect an existing check/job without starting another VM probe.
// Idle automatic refreshes start a new batch at the selected cadence.
function schedulePoll(){
 clearTimeout(pollTimer);
 if(fetching||operation||redirecting)return;
 const active=Boolean(snapshot?.refreshing||waitingForMaintenance||(snapshot?.runs||[]).some(runActive));
 const minutes=Number($('refresh-period').value);
 if(!minutes){if((snapshot?.runs||[]).some(runActive))pollTimer=setTimeout(checkExperimentCompletion,5000);return;}
 pollTimer=setTimeout(()=>{if(autoRefreshEnabled()&&!fetching&&!operation&&!redirecting)refresh(!active,true);},active?5000:minutes*60000);
}
$('refresh-period').addEventListener('change',()=>{try{localStorage.setItem('caf-refresh-minutes',$('refresh-period').value);}catch{}schedulePoll();tick();syncBusy();});
setInterval(()=>{tick();syncBusy();},1000);
initTaskEditor();syncBusy();refresh(true);

window.addEventListener('storage',event=>{if(event.key==='caf-refresh-minutes'&&refreshPeriods.includes(event.newValue)){$('refresh-period').value=event.newValue;schedulePoll();tick();syncBusy();}});

async function checkExperimentCompletion(){
 if(fetching||operation||redirecting){schedulePoll();return;}
 const active=(snapshot?.runs||[]).filter(runActive);let changed=false;
 try{
  for(const previous of active){
   const id=previous.output?.split('/').pop()||previous.workflow_id;
   const response=await apiFetch('/api/runs/'+encodeURIComponent(id)+'/status',{cache:'no-store'});
   if(response.status===401){sessionExpired();return;}
   if(!response.ok)continue;
   const current=await dashboardJSON(response);
   const index=snapshot?.runs.indexOf(previous)??-1;
   if(index>=0){snapshot.runs[index]=current;changed=true;}
  }
  if(changed){renderExperiments(snapshot);renderSampleActivity(snapshot);for(const button of document.querySelectorAll('[data-transcript-run]')){const run=snapshot.runs.find(run=>run.output.split('/').pop()===button.dataset.transcriptRun);button.classList.toggle('is-executing',Boolean(run&&agentIsExecuting(run)));}syncBusy();}
 }catch(error){console.warn('Experiment completion check failed',error.message);}
 finally{schedulePoll();}
}
