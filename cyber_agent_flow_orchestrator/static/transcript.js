/* Server-sent events read only the owner-scoped host transcript mirror. */
const transcriptView=(()=>{
 let source=null,cursor=0,run=null,checking=false,lastCheck=0;
 const get=id=>document.getElementById(id);
 const node=(tag,text,cls)=>{const item=document.createElement(tag);if(text!=null)item.textContent=text;if(cls)item.className=cls;return item;};
 const labels={prompt:'Prompt',system_prompt:'System prompt',reasoning:'Model reasoning',response:'Assistant reply',tool_call:'Tool call',tool_result:'Tool result',progressive_hint:'Hint',error:'Error',status:'Status',chat_done:'Turn completed'};
 function append(record){
  if(!Number.isInteger(record.id)||record.id<=cursor)return;cursor=record.id;
  const output=get('transcript-events'),follow=get('transcript-follow').checked&&output.scrollHeight-output.scrollTop-output.clientHeight<80;
  const event=record.event||{},card=node('article',null,'transcript-event '+(event.type==='error'?'event-error':'')),heading=node('div',null,'transcript-heading');
  heading.append(node('strong',labels[event.type]||event.type||'Agent output'),node('time',record.at?new Date(record.at).toLocaleTimeString():''));card.append(heading);
  card.append(node('p',`${record.trial_id||'Trial'}${record.condition_id?' · '+record.condition_id:''}${record.attempt?' · attempt '+record.attempt:''}`,'small'));
  if(event.tool)card.append(node('p','Tool: '+event.tool));
  if(event.args!=null)card.append(node('pre',JSON.stringify(event.args,null,2),'transcript-text'));
  for(const key of ['text','message','result'])if(event[key]!=null&&event[key]!=='')card.append(node('pre',typeof event[key]==='string'?event[key]:JSON.stringify(event[key],null,2),'transcript-text'));
  if(event.exit_code!=null)card.append(node('p','Exit code: '+event.exit_code+(event.duration_ms!=null?' · '+event.duration_ms+' ms':''),'small'));
  if(event.stderr)card.append(node('pre',event.stderr,'transcript-text'));
  if(!['text','message','result'].some(key=>event[key]!=null))card.append(node('pre',JSON.stringify(event,null,2),'transcript-text'));
  if(event.truncated)card.append(node('p','Live preview truncated; full output is in the run bundle.','small'));
  output.append(card);get('transcript-empty').hidden=true;
  while(output.children.length>400)output.firstChild.remove();
  if(follow)output.scrollTop=output.scrollHeight;
 }
 function connect(){
  if(source)source.close();get('run-error').hidden=true;get('run-status').textContent='Connecting to live agent transcript…';
  const connection=source=new EventSource('/api/runs/'+encodeURIComponent(run)+'/transcript-stream?after='+cursor);
  connection.addEventListener('state',event=>{if(connection!==source)return;const state=JSON.parse(event.data),trial=state.current_trial,activity=trial?.transport?.activity||state.message||state.phase||'';get('run-status').textContent=`${state.status} · ${activity}${state.vmid?' · VM '+state.vmid:''}`;if(trial?.transport?.live_transcript_error){get('run-error').hidden=false;get('run-error').textContent=trial.transport.live_transcript_error;}});
  connection.addEventListener('transcript',event=>{if(connection===source)append(JSON.parse(event.data));});
  connection.addEventListener('complete',event=>{if(connection!==source)return;const state=JSON.parse(event.data);connection.close();get('run-status').textContent=state.status+' · Transcript complete. Closing this window does not change the run.';if(!cursor)get('transcript-empty').textContent='No live transcript was recorded for this run. Open Results to inspect any saved logs.';});
  connection.addEventListener('access-error',event=>{if(connection!==source)return;connection.close();cursor=0;get('transcript-events').replaceChildren();get('run-error').hidden=false;get('run-error').textContent=JSON.parse(event.data).error;});
  connection.onerror=async()=>{
   if(connection!==source)return;
   get('run-status').textContent='Transcript connection interrupted; reconnecting automatically. The experiment continues.';
   if(checking||Date.now()-lastCheck<5000)return;checking=true;lastCheck=Date.now();
   try{const response=await fetch('/api/runs/'+encodeURIComponent(run)+'/status',{cache:'no-store'});
    if(connection===source&&[401,403,404].includes(response.status)){connection.close();cursor=0;get('transcript-events').replaceChildren();get('run-error').hidden=false;get('run-error').textContent=response.status===401?'Session expired. Sign in through the dashboard, then Refresh.':response.status===403?'Access not granted.':'Run not found. Open a current run from the queue.';}
   }catch{}finally{checking=false;}
  };
 }
 get('transcript-latest').addEventListener('click',()=>{get('transcript-follow').checked=true;get('transcript-events').scrollTop=get('transcript-events').scrollHeight;});
 window.addEventListener('pagehide',()=>source?.close());
 return {open(id){run=id;get('transcript-panel').hidden=false;connect();},refresh:connect};
})();
