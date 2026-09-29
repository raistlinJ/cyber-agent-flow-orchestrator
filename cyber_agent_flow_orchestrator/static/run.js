/* Independent read-only window: no dashboard polling or guest operations. */
const $=id=>document.getElementById(id);
const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!=null)node.textContent=text;if(cls)node.className=cls;return node;};
const params=new URLSearchParams(location.search),view=params.get('view'),runId=params.get('run');
let loading=false,poll=null,workflow=null,logs=[];
function log(kind,message){logs.push({at:new Date().toISOString(),kind,message});logs=logs.slice(-100);renderConsole();}
function renderConsole(){const entries=[...logs,...(workflow?.sample_progress?.events||[])].sort((a,b)=>a.at.localeCompare(b.at));$('console-output').textContent=entries.slice(-300).map(e=>`${e.at} [${e.kind||'sample'}] ${e.message}`).join('\n')||'Waiting for activity…';}
function clearRun(){$('window-notice').hidden=true;$('window-notice').replaceChildren();workflow=null;renderedConfiguration=null;scenarioPreviews.clear();$('run-configuration').replaceChildren();$('sample-activity').replaceChildren();$('result-summary').replaceChildren();$('result-content').textContent='';$('download-dataset').hidden=true;logs=[];renderConsole();}
async function refreshRun(){
 if(loading)return;loadingModal.set('run',true,view==='results'?'Loading experiment results…':'Loading experiment progress…');clearTimeout(poll);loading=true;$('refresh-run').disabled=true;$('run-status').textContent='Loading saved run data…';
 const endpoint=`/api/runs/${encodeURIComponent(runId)}/${view==='results'?'results':'status'}`;
 let retry=true;
 try{
  log('request',`GET ${endpoint}`);const response=await fetch(endpoint,{cache:'no-store'});log('response',`HTTP ${response.status}`);
  if(!response.ok){if([401,403,404].includes(response.status)){clearRun();retry=false;}throw Error(response.status===401?'Session expired. Sign in through the main dashboard, then Refresh.':response.status===403?'Access not granted.':response.status===404?'Run not found or results unavailable.':`Unable to load run: HTTP ${response.status}`);}
  const data=await dashboardJSON(response);workflow=view==='results'?data.workflow:data;
  if(view==='results'){renderResultSummary(data);$('result-content').textContent=JSON.stringify(data,null,2);$('download-dataset').hidden=!data.evaluation;if(data.evaluation)$('download-dataset').href=`/api/runs/${encodeURIComponent(runId)}/dataset.csv`;}
  else renderProgress(workflow);
  $('run-error').hidden=true;const active=workflow.coordinator_active||workflow.sample_progress?.active||['queued','stopping'].includes(workflow.recorded_status);
  $('run-status').textContent=`${workflow.recorded_status} · Updated ${new Date().toLocaleTimeString()} · Refreshes ${active?'every 5 seconds':'every minute'}`;renderConsole();
 }catch(error){$('run-error').hidden=false;$('run-error').textContent=error.message;$('run-status').textContent='Run view unavailable. Refresh to retry.';}
 finally{loadingModal.set('run',false);loading=false;$('refresh-run').disabled=false;if(retry){const active=workflow?.coordinator_active||workflow?.sample_progress?.active||['queued','stopping'].includes(workflow?.recorded_status);poll=setTimeout(refreshRun,active?5000:60000);}}
}
$('close-window').addEventListener('click',()=>{window.close();$('run-status').textContent='You can close this browser tab.';});
$('refresh-run').addEventListener('click',refreshRun);
window.addEventListener('focus',()=>{if(['results','progress'].includes(view)&&runId&&/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(runId))refreshRun();});
new ResizeObserver(()=>document.body.style.setProperty('--console-height',$('debug-console').getBoundingClientRect().height+'px')).observe($('debug-console'));
try{$('debug-console').open=localStorage.getItem('caf-console-open')!=='false';}catch{}
$('debug-console').addEventListener('toggle',()=>{try{localStorage.setItem('caf-console-open',String($('debug-console').open));}catch{}});
$('download-console').addEventListener('click',()=>{const url=URL.createObjectURL(new Blob([$('console-output').textContent],{type:'text/plain'})),link=el('a');link.href=url;link.download=`${runId}-console.log`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
setInterval(()=>{for(const clock of document.querySelectorAll('[data-seconds]')){let seconds=Number(clock.dataset.seconds);if(clock.dataset.live==='yes'&&clock.dataset.observed)seconds+=Math.max(0,(Date.now()-Date.parse(clock.dataset.observed))/1000);clock.textContent=duration(seconds);}},1000);
if(!['results','progress'].includes(view)||!runId||!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(runId)){$('run-error').hidden=false;$('run-error').textContent='Invalid run window URL.';$('refresh-run').disabled=true;}
else{$('window-title').textContent=view==='results'?'Experiment results':'Experiment progress';document.title=`${view==='results'?'Results':'Progress'} · ${runId}`;$('run-id').textContent=runId;$(view==='results'?'result-panel':'sample-activity-panel').hidden=false;refreshRun();}
