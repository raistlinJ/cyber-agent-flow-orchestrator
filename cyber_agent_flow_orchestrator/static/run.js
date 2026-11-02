/* Independent read-only window: no dashboard polling or guest operations. */
const $=id=>document.getElementById(id);
const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!=null)node.textContent=text;if(cls)node.className=cls;return node;};
const params=new URLSearchParams(location.search),view=params.get('view'),runId=params.get('run');
let loading=false,poll=null,workflow=null,logs=[],retryAllowed=true,updatedAt=null,csrfToken=null,analysisRunning=false;
/* One server-sent feed per progress window; snapshots resynchronize after reconnects. */
let runStream=null,streamState='off',streamRevision=null,streamLastAt=0,streamRetryAt=0,streamWatchdog=null,streamGeneration=0;
function stopRunStream(){
 const source=runStream;runStream=null;if(source)source.close();clearTimeout(streamWatchdog);streamWatchdog=null;streamRevision=null;streamState='off';
}
function applyRunChanges(current,operations){
 if(!Array.isArray(operations)||operations.length>20000)throw Error('Invalid stream update');
 let result=structuredClone(current);
 for(const change of operations){
  if(!change||!Array.isArray(change.path)||change.path.length>64)throw Error('Invalid stream path');
  let target=result;const path=change.path;
  for(const key of path.slice(0,-1)){
   if(!target||typeof target!=='object'||!Object.hasOwn(target,key))throw Error('Stream path missing');
   target=target[key];
  }
  const key=path.at(-1);
  if(change.op==='append'){
   const list=path.length?target?.[key]:result;
   if(!Array.isArray(list)||!Array.isArray(change.value))throw Error('Invalid stream append');
   list.push(...change.value);
  }else if(change.op==='set'){
   if(!path.length)result=change.value;
   else{
    if(!target||typeof target!=='object')throw Error('Invalid stream target');
    Object.defineProperty(target,key,{value:change.value,writable:true,enumerable:true,configurable:true});
   }
  }else if(change.op==='remove'){
   if(!path.length||!target||typeof target!=='object'||!Object.hasOwn(target,key))throw Error('Invalid stream removal');
   delete target[key];
  }else throw Error('Unknown stream operation');
 }
 return result;
}
function receiveStreamRun(value,revision){
 if(!value||typeof value!=='object'||!Number.isInteger(revision)||revision<1||typeof value.recorded_status!=='string')throw Error('Invalid run stream snapshot');
 streamGeneration++;workflow=value;streamRevision=revision;streamLastAt=Date.now();streamState='live';clearTimeout(poll);
 renderProgress(workflow);renderVisualProgress(workflow);$('run-error').hidden=true;updatedAt=new Date().toLocaleTimeString();renderConsole();
 if(!runIsActive()){stopRunStream();scheduleRunRefresh();}
 showRefreshStatus();
}
function streamFallback(source){
 if(source&&source!==runStream)return;
 stopRunStream();streamState='fallback';streamRetryAt=Date.now()+30000;
 if(workflow){log('stream','Live stream unavailable; using five-second polling and retrying automatically.');showRefreshStatus();scheduleRunRefresh();}
}
function startRunStream(){
 if(view!=='progress'||!runIsActive()||runStream||Date.now()<streamRetryAt)return;
 if(typeof EventSource==='undefined'){streamState='fallback';return;}
 const source=runStream=new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream`);
 streamState='connecting';streamRevision=null;streamLastAt=Date.now();clearTimeout(poll);
 const watchdog=()=>{
  if(source!==runStream)return;
  if(Date.now()-streamLastAt>=35000){streamFallback(source);return;}
  streamWatchdog=setTimeout(watchdog,10000);
 };
 streamWatchdog=setTimeout(watchdog,10000);
 source.addEventListener('snapshot',event=>{
  if(source!==runStream)return;
  try{const data=JSON.parse(event.data);receiveStreamRun(data.value,data.revision);}catch{streamFallback(source);}
 });
 source.addEventListener('update',event=>{
  if(source!==runStream)return;
  try{const data=JSON.parse(event.data);if(data.base!==streamRevision)throw Error('Stream revision mismatch');receiveStreamRun(applyRunChanges(workflow,data.changes),data.revision);}catch{streamFallback(source);}
 });
 source.addEventListener('heartbeat',()=>{
  if(source!==runStream)return;
  streamLastAt=Date.now();
  // Keep feed freshness separate from the tool activity lease; no replay/blinking reset.
  if(streamState==='live')visualState.receivedAt=performance.now();
 });
 source.addEventListener('complete',()=>{if(source===runStream){stopRunStream();showRefreshStatus();scheduleRunRefresh();}});
 source.addEventListener('access-error',event=>{
  if(source!==runStream)return;
  let status=403;try{status=JSON.parse(event.data).status;}catch{}
  stopRunStream();clearRun();retryAllowed=false;clearTimeout(poll);$('run-error').hidden=false;$('run-error').textContent=status===401?'Session expired. Sign in through the main dashboard, then Refresh.':'Access not granted.';$('run-status').textContent='Run view unavailable. Refresh to retry.';
 });
 source.addEventListener('unavailable',()=>streamFallback(source));
 source.onerror=()=>{
  if(source!==runStream)return;
  streamRevision=null;
  if(source.readyState===EventSource.CLOSED){streamFallback(source);return;}
  streamState='reconnecting';showRefreshStatus();scheduleRunRefresh();
 };
 showRefreshStatus();
}
window.addEventListener('pagehide',()=>{stopRunStream();clearTimeout(poll);});
window.addEventListener('pageshow',event=>{if(event.persisted&&workflow){startRunStream();scheduleRunRefresh();showRefreshStatus();}});
async function session(){if(csrfToken)return csrfToken;const response=await fetch('/api/session',{cache:'no-store'});if(!response.ok)throw Error('Session expired. Sign in through the main dashboard, then retry.');csrfToken=(await dashboardJSON(response)).csrf;return csrfToken;}
function refreshMinutes(){try{const value=localStorage.getItem('caf-refresh-minutes');return ['0','1','2','5','10'].includes(value)?Number(value):1;}catch{return 1;}}
function runIsActive(){if(['completed','completed_with_errors','failed','interrupted','cancelled','ready'].includes(workflow?.recorded_status))return false;return workflow?.coordinator_active||workflow?.sample_progress?.active||['queued','preparing','evaluating','running','stopping'].includes(workflow?.recorded_status);}
function refreshDelay(){return runIsActive()?5000:refreshMinutes()*60000;}
function showRefreshStatus(){if(!workflow||loading||!$('run-error').hidden)return;const delay=refreshDelay();$('run-status').textContent=workflow.recorded_status+' · Updated '+updatedAt+' · '+(runIsActive()?(view==='progress'&&streamState==='live'?'Live updates · streaming':view==='progress'&&streamState==='connecting'?'Connecting live stream…':view==='progress'&&streamState==='reconnecting'?'Reconnecting live stream · polling every 5 seconds':'Live updates · polling every 5 seconds'):delay?'Refreshes every '+refreshMinutes()+' minute(s)':'Automatic refresh off');}
function scheduleRunRefresh(){clearTimeout(poll);if(view==='progress'&&runIsActive()&&runStream&&['connecting','live'].includes(streamState))return;const delay=refreshDelay();if(!loading&&retryAllowed){if(delay)poll=setTimeout(()=>{if(refreshDelay())refreshRun({silent:true});},delay);}}
window.addEventListener('storage',event=>{if(event.key==='caf-refresh-minutes'){scheduleRunRefresh();showRefreshStatus();}});
function log(kind,message){logs.push({at:new Date().toISOString(),kind,message});logs=logs.slice(-100);renderConsole();}
function renderConsole(){const entries=[...logs,...(workflow?.sample_progress?.events||[])].sort((a,b)=>a.at.localeCompare(b.at));$('console-output').textContent=entries.slice(-300).map(e=>`${e.at} [${e.kind||'sample'}] ${String(e.message||'').replaceAll('Judge endpoint and host credentials','Test LLM Judge').replaceAll('Testing judge is working','Test LLM Judge')}`).join('\n')||'Waiting for activity…';}
function clearRun(){streamGeneration++;stopRunStream();$('window-notice').hidden=true;$('window-notice').replaceChildren();workflow=null;renderedConfiguration=null;scenarioPreviews.clear();$('run-configuration').replaceChildren();resetProgressView();resetVisualProgress();$('result-summary').replaceChildren();$('result-content').textContent='';$('download-dataset').hidden=true;$('download-report-md').hidden=true;$('download-report-html').hidden=true;logs=[];renderConsole();}
async function refreshRun({silent=false}={}){
 if(loading)return;if(!silent){stopRunStream();streamRetryAt=0;}const generation=streamGeneration;if(!silent)loadingModal.set('run',true,view==='results'?'Loading experiment results…':'Loading experiment progress…');clearTimeout(poll);loading=true;$('refresh-run').disabled=true;$('run-status').textContent='Loading saved run data…';
 const endpoint=`/api/runs/${encodeURIComponent(runId)}/${view==='results'?'results':'status'}`;
 let retry=true;
 try{
  log('request',`GET ${endpoint}`);const response=await fetch(endpoint,{cache:'no-store'});log('response',`HTTP ${response.status}`);
  if(!response.ok){if([401,403,404].includes(response.status)){clearRun();retry=false;}throw Error(response.status===401?'Session expired. Sign in through the main dashboard, then Refresh.':response.status===403?'Access not granted.':response.status===404?'Run not found or results unavailable.':`Unable to load run: HTTP ${response.status}`);}
  const data=await dashboardJSON(response);if(silent&&streamGeneration!==generation)return;workflow=view==='results'?data.workflow:data;
  if(view==='results'){for(const [id,artifact] of [['download-report-md','report-markdown'],['download-report-html','report-html']]){$(id).hidden=false;$(id).href=artifactURL(artifact);}renderResultSummary(data);$('result-content').textContent=JSON.stringify(data,null,2);$('download-dataset').hidden=!data.evaluation;if(data.evaluation)$('download-dataset').href=`/api/runs/${encodeURIComponent(runId)}/dataset.csv`;const eligible=Boolean(data.evaluation&&workflow.scenario_experiment&&['completed','completed_with_errors','failed','interrupted','cancelled'].includes(workflow.recorded_status));$('post-execution-actions').hidden=!eligible;$('analyze-rerun').disabled=analysisRunning;}
  else{renderProgress(workflow);renderVisualProgress(workflow);}
  $('run-error').hidden=true;updatedAt=new Date().toLocaleTimeString();renderConsole();
 }catch(error){$('run-error').hidden=false;$('run-error').textContent=error.message;$('run-status').textContent='Run view unavailable. Refresh to retry.';}
 finally{loadingModal.set('run',false);loading=false;retryAllowed=retry;$('refresh-run').disabled=false;if(retry&&workflow)startRunStream();showRefreshStatus();scheduleRunRefresh();}
}
$('close-window').addEventListener('click',()=>{window.close();$('run-status').textContent='You can close this browser tab.';});
$('refresh-run').addEventListener('click',refreshRun);
async function analyzeAndRerun(){if(analysisRunning)return;analysisRunning=true;$('analyze-rerun').disabled=true;const requestId=crypto.randomUUID().replaceAll('-',''),launchRequestId=crypto.randomUUID().replaceAll('-','');let finished=false,last='Submitting post-execution analysis…',timer=null;const render=status=>{if(status){last=`Step ${status.step}/${status.total} · ${status.message}`;if(status.error)last+=' · '+status.error;}const elapsed=Math.floor((Date.now()-started)/1000);$('analysis-status').textContent=last+` · ${elapsed}s elapsed`;loadingModal.set('analysis',true,$('analysis-status').textContent,status&&status.total?100*status.step/status.total:null);};const started=Date.now();render();const heartbeat=setInterval(()=>render(),1000);const check=async()=>{try{const response=await fetch('/api/experiments/analysis-status?request_id='+encodeURIComponent(requestId),{cache:'no-store'});if(response.ok&&!finished)render(await dashboardJSON(response));}catch{}if(!finished)timer=setTimeout(check,2000);};timer=setTimeout(check,300);try{const token=await session();const response=await fetch('/api/experiments/analyze-rerun',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({run_id:runId,request_id:requestId,launch_request_id:launchRequestId})});const data=await dashboardJSON(response);if(!response.ok)throw Error(data.error||'Post-execution analysis failed');$('analysis-status').replaceChildren(document.createTextNode('Generated-helper comparison queued. '));const link=el('a','Open progress ↗');link.href=`/run?view=progress&run=${encodeURIComponent(data.run_id)}`;link.target='_blank';link.rel='noopener';$('analysis-status').append(link);log('analysis',`Derived experiment ${data.run_id} queued from ${runId}`);}catch(error){$('analysis-status').textContent=error.message;log('error','Post-execution analysis: '+error.message);}finally{finished=true;clearTimeout(timer);clearInterval(heartbeat);loadingModal.set('analysis',false);analysisRunning=false;$('analyze-rerun').disabled=false;}}
$('analyze-rerun').addEventListener('click',analyzeAndRerun);
new ResizeObserver(()=>document.body.style.setProperty('--console-height',$('debug-console').getBoundingClientRect().height+'px')).observe($('debug-console'));
try{$('debug-console').open=localStorage.getItem('caf-console-open')!=='false';}catch{}
$('debug-console').addEventListener('toggle',()=>{try{localStorage.setItem('caf-console-open',String($('debug-console').open));}catch{}});
$('download-console').addEventListener('click',()=>{const url=URL.createObjectURL(new Blob([$('console-output').textContent],{type:'text/plain'})),link=el('a');link.href=url;link.download=`${runId}-console.log`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
setInterval(()=>{for(const clock of document.querySelectorAll('[data-seconds]')){let seconds=Number(clock.dataset.seconds);if(clock.dataset.live==='yes'&&clock.dataset.observed)seconds+=Math.max(0,(Date.now()-Date.parse(clock.dataset.observed))/1000);clock.textContent=duration(seconds);}},1000);
if(!['results','progress'].includes(view)||!runId||!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(runId)){$('run-error').hidden=false;$('run-error').textContent='Invalid run window URL.';$('refresh-run').disabled=true;}
else{if(view==='progress'){document.body.classList.add('phase-workspace');initProgressNavigation();initProgressVisualization();}$('window-title').textContent=view==='results'?'Experiment results':'Experiment progress';document.title=`${view==='results'?'Results':'Progress'} · ${runId}`;$('run-id').textContent=runId;$(view==='results'?'result-panel':'sample-activity-panel').hidden=false;refreshRun();}
