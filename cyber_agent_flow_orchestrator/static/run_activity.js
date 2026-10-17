/* Leased activity highlights: the browser stops pulsing even if polling stalls. */
function visualRunLive(){const run=visualState.run;if(['completed','completed_with_errors','failed','cancelled','interrupted','ready'].includes(run?.recorded_status))return false;return Boolean(run?.coordinator_active||run?.sample_progress?.active||['queued','preparing','evaluating','running','stopping'].includes(run?.recorded_status));}
function visualActivityLive(){return visualRunLive()&&visualState.activity?.live&&performance.now()<visualState.activityDeadline&&performance.now()-visualState.receivedAt<45000;}
function activityFingerprint(activity){if(!activity)return null;const {observed_at,expires_at,event_at,...stable}=activity;return stable;}
function setVisualActivity(activity,received=true){visualState.activity=activity||null;const now=performance.now(),age=received?0:Math.max(0,now-visualState.receivedAt);if(received)visualState.receivedAt=now;if(visualState.activitySelection&&!activity?.targets?.some(target=>target.address===visualState.activitySelection))visualState.activitySelection=null;const key=visualState.trial+'|'+(activity?.expires_at||'none');if(!activity?.live){visualState.activityDeadline=0;visualState.activityLeaseKey=null;}else if(visualState.activityLeaseKey!==key){visualState.activityLeaseKey=key;const remaining=Date.parse(activity.expires_at)-Date.parse(activity.observed_at);visualState.activityDeadline=now+Math.max(0,(Number.isFinite(remaining)?Math.max(0,Math.min(45000,remaining)):0)-age);}renderActivityBar();}
function syncLiveHighlights(){
 const fresh=visualRunLive()&&performance.now()-visualState.receivedAt<45000,live=visualActivityLive(),activity=visualState.activity,steps=new Set(live?activity.step_ids:[]),matched=new Set(live?(activity.targets||[]).flatMap(target=>target.step_ids):[]);
 for(const node of $('attack-graph').querySelectorAll('.attack-node')){node.classList.toggle('live-interaction',steps.has(node.dataset.nodeId));node.classList.toggle('live-target-matched',matched.has(node.dataset.nodeId));}
 const run=visualState.run,phase=run?.workflow_progress?.current,monitorWorking=fresh&&visualState.graph?.status==='running'&&visualState.graph?.monitor?.latest?.status==='running',workingRoles=new Set();
 if(fresh){
  for(const node of visualState.preparation.nodes)if(node.status==='running')workingRoles.add(node.id);
  if(phase?.active&&phase.status==='running'){
   for(const role of run.workflow_progress.roles||[])if(role.vmid!=null&&role.vmid===phase.vmid)workingRoles.add(role.role);
   if(phase.id==='deploy'){workingRoles.add('scenarioforge');workingRoles.add('core');}
  }
  if(live)workingRoles.add('participant');
 }
 if(monitorWorking)workingRoles.delete('participant');
 for(const node of $('preparation-graph').querySelectorAll('.preparation-node')){const working=workingRoles.has(node.dataset.prepId);node.classList.toggle('live-interaction',working);node.setAttribute('aria-busy',String(working));}
 for(const edge of $('preparation-graph').querySelectorAll('.preparation-edge'))edge.classList.toggle('live-interaction',fresh&&edge.classList.contains('running'));
 const monitorLine=$('visual-monitor-status').closest('.visual-monitor-line');monitorLine.classList.toggle('is-working',Boolean(monitorWorking));monitorLine.setAttribute('aria-busy',String(Boolean(monitorWorking)));
 for(const chip of $('visual-live-targets').querySelectorAll('button')){chip.classList.toggle('live-interaction',live);chip.setAttribute('aria-label',chip.dataset.address+' · '+(live?'active '+(activity.kind==='scan'?'scan':'interaction'):'recorded target'));}
 $('visual-live-activity').classList.toggle('has-live-activity',Boolean(live));
 const mode=activity?.phase,tool=activity?.tool||'tool',label=live?(mode==='thinking'?(activity.context_step_ids?.length?'Agent working · monitor context':'Agent working · awaiting next tool'):mode==='recent'?'Recent interaction · '+tool:activity.kind==='scan'?'Scanning · '+tool:'Interacting · '+tool):mode==='reviewing'?'Monitor reviewing trajectory · agent paused':mode==='catching-up'?'Transcript catching up · live highlighting paused':mode==='finished'?'Recorded interactions · run finished':activity?.live||mode==='stale'?'Live activity signal expired':'No active tool interaction';
 $('visual-live-status').textContent=live&&mode==='thinking'?flowStateLabel():label;syncActivityFlow();syncVisualTranscriptActivity();syncPreparationChecks();
}
function renderActivityBar(){
 const activity=visualState.activity,holder=$('visual-live-targets'),signature=JSON.stringify({targets:activity?.targets||[],tool:activity?.tool});
 if(holder.dataset.signature!==signature){holder.dataset.signature=signature;holder.replaceChildren();for(const target of activity?.targets||[]){const button=el('button',target.address+(target.range?' · range':''),'live-target-chip');button.type='button';button.dataset.address=target.address;button.title='Target from recorded '+(activity.tool||'tool')+' arguments. Select for challenge context and source.';button.addEventListener('click',()=>chooseActivityTarget(target));holder.append(button);}}
 syncLiveHighlights();
}
function renderActivityTarget(){
 const address=visualState.activitySelection,target=$('visual-node-detail'),activity=visualState.activity;target.replaceChildren();if(!address)return;
 target.append(el('span',visualActivityLive()?'Active interaction':'Recorded interaction','visual-state-chip targeted'),el('h3',address),el('p',(activity?.tool||'Tool')+' · '+(activity?.kind==='scan'?'scan target':'interaction target'),'visual-node-extent'),el('p','This target appears in recorded tool arguments. No challenge association is verified yet.','small'));
 appendActivitySource(target,activity);target.append(el('p','An attempted interaction does not prove reachability or completion.','small'));
}
function appendActivitySource(target,activity){if(activity?.event_at)target.append(el('p','Action recorded '+new Date(activity.event_at).toLocaleTimeString(),'small'));if(activity?.source){const citation=activity.source;target.append(el('code',citation.file+' · bytes '+citation.offset+'–'+(citation.offset+citation.limit)));}}
function appendNodeActivity(target,node){const activity=visualState.activity;if(!activity?.step_ids?.includes(node.id))return;const targets=activity.targets.filter(item=>item.step_ids.includes(node.id));target.append(el('p',(visualActivityLive()?'Active: ':'Last observed: ')+(activity.phase==='thinking'?'agent working in monitor challenge context':(activity.kind==='scan'?'scan':'interaction')+' via '+(activity.tool||'tool'))+(targets.length?' · '+targets.map(item=>item.address).join(', '):''),'visual-active-note'));appendActivitySource(target,activity);}

/* The live action lane uses recorded tool targets, separate from dependency edges. */
function activityFlowLayout(trial){
 const action=visualState.activity||{},thinking=action.phase==='thinking',reviewing=action.phase==='reviewing',tool=Boolean(action.tool)&&!thinking&&!reviewing;
 const targets=tool?(action.targets?.length?action.targets:[{address:null,step_ids:[]}]):[],columns=targets.length>6?2:1,rows=Math.ceil(targets.length/columns),width=columns===2?620:340,center=width/2;
 return {top:0,height:visualEditor.activity?130:Math.max(370,365+rows*70),width:visualEditor.activity?240:width,collapsed:visualEditor.activity,tool,thinking,reviewing,targets:visualEditor.activity?[]:targets,
  actor:{x:center-115,y:75,w:230,h:100},action:{x:center-115,y:225,w:230,h:100},
  targetPositions:targets.map((target,index)=>({x:45+(index%columns)*280,y:380+Math.floor(index/columns)*70,w:250,h:52,target}))};
}
function flowIcon(group,x,y,kind){
 const icon=svgElement('g',{transform:`translate(${x},${y})`,class:'flow-icon '+(kind==='thinking'?'flow-thinking-icon':''),'aria-hidden':'true'});
 if(kind==='thinking'){
  icon.append(svgElement('path',{d:'M3 2 H29 Q34 2 34 7 V20 Q34 25 29 25 H13 L6 32 V25 H3 Q-2 25 -2 20 V7 Q-2 2 3 2 Z',class:'flow-icon-outline'}));
  for(let i=0;i<3;i++)icon.append(svgElement('circle',{cx:7+i*8,cy:13,r:2.3,class:'flow-thinking-dot',style:'animation-delay:'+i*.2+'s'}));
 }else if(kind==='tool')icon.append(svgElement('rect',{x:0,y:0,width:32,height:28,rx:5,class:'flow-icon-outline'}),svgElement('path',{d:'M7 8 L13 14 L7 20 M18 20 H25',class:'flow-icon-outline'}));
 else if(kind==='monitor')icon.append(svgElement('circle',{cx:12,cy:12,r:10,class:'flow-icon-outline'}),svgElement('path',{d:'M20 20 L30 30 M8 12 L11 15 L17 8',class:'flow-icon-outline'}));
 else icon.append(svgElement('circle',{cx:16,cy:5,r:4,class:'flow-icon-outline'}),svgElement('circle',{cx:4,cy:25,r:4,class:'flow-icon-outline'}),svgElement('circle',{cx:28,cy:25,r:4,class:'flow-icon-outline'}),svgElement('path',{d:'M14 9 L6 21 M18 9 L26 21 M8 25 H24',class:'flow-icon-outline'}));
 group.append(icon);
}
function flowSelectable(group,label,choose){
 group.setAttribute('role','button');group.setAttribute('tabindex','0');group.setAttribute('aria-label',label);
 group.addEventListener('click',()=>{if(!visualState.moved)choose();});group.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();visualState.moved=false;choose();}});
}
function chooseVisualFlow(){visualState.prep=null;visualState.activitySelection=null;visualState.flowSelection=true;updateVisualSelection();renderVisualNode();setVisualInspector('details');}
function chooseActivityTarget(target){
 visualState.prep=null;visualState.flowSelection=false;const node=visualState.graph?.nodes.find(node=>(target.step_ids||[]).includes(node.id));
 if(node){if(visualEditor.challenges)setVisualEditorCollapsed('challenges',false);visualState.activitySelection=null;visualState.node=node.id;focusVisualNode(node.id);updateVisualSelection();renderVisualNode();setVisualInspector('details');}
 else{visualState.activitySelection=target.address;updateVisualSelection();renderActivityTarget();setVisualInspector('details');}
}
function flowArrow(group,from,to,attrs={}){
 const path=svgElement('path',{d:from.y<to.y&&Math.abs(from.x-to.x)<180?`M${from.x} ${from.y} C${from.x} ${(from.y+to.y)/2},${to.x} ${(from.y+to.y)/2},${to.x} ${to.y}`:`M${from.x} ${from.y} C${from.x+40} ${from.y},${to.x-40} ${to.y},${to.x} ${to.y}`,class:'activity-flow-arrow','marker-end':'url(#flow-arrow)',...attrs});group.append(path);return path;
}
function drawActivityFlow(svg,layout){
 const flow=layout.flow;if(!flow)return;const action=visualState.activity||{};
 const group=svgElement('g',{class:'activity-flow-lane','aria-label':'Cyber-Agent-Flow activity and recorded targets'}),defs=svgElement('defs'),marker=svgElement('marker',{id:'flow-arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto'});
 marker.append(svgElement('path',{d:'M0 0 L10 5 L0 10 z',class:'flow-arrow-head'}));const contextMarker=marker.cloneNode(true);contextMarker.id='flow-context-arrow';contextMarker.querySelector('path').setAttribute('class','flow-context-arrow-head');defs.append(marker,contextMarker);group.append(defs);
 drawVisualFoldHeader(group,40,25,'Agent activity','activity');if(!flow.collapsed)group.append(svgElement('text',{x:45,y:flow.top+48,class:'visual-lane-note'},'CAF → action → recorded targets'));
 if(!flow.collapsed){
 const a=flow.actor,b=flow.action;
 flowArrow(group,{x:a.x+a.w/2,y:a.y+a.h+3},{x:b.x+b.w/2,y:b.y-8},{'data-flow-kind':flow.tool?'tool':'state'});
 const links=[];
 for(const [index,p] of flow.targetPositions.entries()){
  const target=p.target;
  flowArrow(group,{x:b.x+b.w/2,y:b.y+b.h+3},{x:p.x+p.w/2,y:p.y-8},{'data-flow-kind':'target'});
  for(const key of target.step_ids||[]){const destination=layout.positions.get(key);if(destination)links.push({source:p,destination,step:key,address:target.address,index});}
 }
 for(const link of links){
  const from=link.source,to=link.destination,mid=(from.x+from.w+to.x)/2;
  const path=svgElement('path',{d:`M${from.x+from.w} ${from.y+from.h/2} C${mid} ${from.y+from.h/2},${mid} ${to.y+to.h/2},${to.x-8} ${to.y+to.h/2}`,class:'activity-challenge-arrow','marker-end':'url(#flow-arrow)','data-step-id':link.step,'data-address':link.address});path.append(svgElement('title',{},link.address+' → '+link.step+' · recorded target match'));group.append(path);
 }
 // Context-only links remain distinct from recorded tool/IP interactions.
 if(!flow.targets.some(target=>target.address))for(const key of action.context_step_ids||[]){const p=layout.positions.get(key);if(p){const mid=(b.x+b.w+p.x)/2;group.append(svgElement('path',{d:`M${b.x+b.w} ${b.y+b.h/2} C${mid} ${b.y+b.h/2},${mid} ${p.y+p.h/2},${p.x-8} ${p.y+p.h/2}`,class:'activity-context-arrow','marker-end':'url(#flow-context-arrow)'}));}}
 const actor=svgElement('g',{class:'flow-card flow-actor',transform:`translate(${a.x},${a.y})`,'data-flow-role':'actor'});
 actor.append(svgElement('rect',{width:a.w,height:a.h,rx:13,class:'flow-card-surface'}));flowIcon(actor,17,25,flow.thinking?'thinking':'network');
 actor.append(svgElement('text',{x:65,y:33,class:'flow-card-title'},'Cyber-Agent-Flow'),svgElement('text',{x:17,y:79,class:'flow-state-line'},''));flowSelectable(actor,'Cyber-Agent-Flow current activity',chooseVisualFlow);group.append(actor);
 const card=svgElement('g',{class:'flow-card flow-action',transform:`translate(${b.x},${b.y})`,'data-flow-role':'action'});
 card.append(svgElement('rect',{width:b.w,height:b.h,rx:13,class:'flow-card-surface'}));flowIcon(card,17,25,flow.thinking?'thinking':flow.reviewing?'monitor':'tool');
 const title=flow.tool?action.tool:flow.reviewing?'LLM Monitor':flow.thinking?(action.agent_state==='reasoning'?'Reasoning':action.agent_state==='responding'?'Responding':'Model response'):'Activity status';
 nodeTitleLines(title).forEach((line,index)=>card.append(svgElement('text',{x:65,y:33+index*17,class:'flow-card-title'},line.length>21?line.slice(0,20)+'…':line)));
 card.append(svgElement('text',{x:17,y:79,class:'flow-action-caption'},flow.tool?(action.kind==='scan'?'Scan tool':'Tool call'):flow.reviewing?'Reviewing saved evidence':flow.thinking?(action.agent_state==='reasoning'?'Provider emitted reasoning':action.agent_state==='responding'?'Response is streaming':'Waiting for model output'):'Waiting for recorded activity'));
 flowSelectable(card,title+' · select for recorded activity details',chooseVisualFlow);group.append(card);
 for(const p of flow.targetPositions){
  const target=p.target,card=svgElement('g',{class:'flow-card flow-target'+(target.address?'':' flow-generic'),transform:`translate(${p.x},${p.y})`,'data-flow-role':'target','data-address':target.address||''});
  card.append(svgElement('rect',{width:p.w,height:p.h,rx:10,class:'flow-card-surface'}),svgElement('text',{x:13,y:22,class:'flow-target-address',style:target.address?.length>32?'font-size:9px':''},target.address||'Tool action'),svgElement('text',{x:13,y:40,class:'flow-target-caption'},target.address?(target.step_ids.length?target.step_ids.length+' challenge match'+(target.step_ids.length===1?'':'es'):'No known challenge match'):'No literal IP in recorded arguments'));
  flowSelectable(card,target.address||'Tool action without a recorded IP',()=>target.address?chooseActivityTarget(target):chooseVisualFlow());group.append(card);
 }
 }
 group.append(svgElement('line',{x1:flow.width+40,y1:18,x2:flow.width+40,y2:layout.height-18,class:'visual-lane-divider'}),svgElement('text',{x:flow.width+90,y:48,class:'visual-lane-note'},'Solid arrows: recorded targets · dotted purple: Monitor context'));
 drawVisualFoldHeader(group,flow.width+85,25,'Challenges','challenges');svg.append(group);syncActivityFlow();
}
function flowStateLabel(){
 const action=visualState.activity;
 if(visualActivityLive())return action.phase==='thinking'?(action.agent_state==='reasoning'?'Thinking · reasoning received':action.agent_state==='responding'?'Responding':'Thinking · awaiting model'):action.phase==='recent'?'Tool action completed':action.kind==='scan'?'Scanning':'Running tool';
 return action?.phase==='reviewing'?'Paused · Monitor reviewing':action?.phase==='finished'?'Run finished':action?.live||action?.phase==='stale'?'Activity signal expired':action?.phase==='catching-up'?'Transcript catching up':'Waiting for activity';
}
function syncActivityFlow(){
 const lane=$('attack-graph').querySelector('.activity-flow-lane');if(!lane)return;const live=visualActivityLive(),fresh=visualRunLive()&&performance.now()-visualState.receivedAt<45000,reviewing=fresh&&visualState.activity?.phase==='reviewing';
 lane.classList.toggle('flow-live',Boolean(live));lane.classList.toggle('flow-reviewing',Boolean(reviewing));const line=lane.querySelector('.flow-state-line');if(line)line.textContent=flowStateLabel();
 for(const card of lane.querySelectorAll('.flow-card')){const working=live&&card.dataset.flowRole!=='target'||reviewing&&card.dataset.flowRole==='action';card.setAttribute('aria-busy',String(Boolean(working)));card.setAttribute('aria-pressed',String(Boolean(visualState.flowSelection&&card.dataset.flowRole!=='target'||visualState.activitySelection&&card.dataset.address===visualState.activitySelection)));}
}
function renderVisualFlowDetail(){
 const holder=$('visual-node-detail'),action=visualState.activity;holder.replaceChildren(el('span',flowStateLabel(),'visual-state-chip targeted'),el('h3',action?.tool||'Cyber-Agent-Flow activity'));
 holder.append(el('p',action?.agent_state==='reasoning'?'CAF emitted a reasoning event. The thinking indicator does not infer unreported private reasoning.':action?.phase==='thinking'?'CAF is generating or waiting for model output.':action?.phase==='reviewing'?'CAF is paused while the Monitor reviews saved execution evidence.':'Arrows connect the recorded tool to literal IPs/ranges and their matching challenges. They describe attempted actions, not verified reachability or completion.'));
 const targets=action?.targets||[];if(targets.length){const list=el('ul');for(const target of targets)list.append(el('li',target.address+(target.step_ids.length?' · '+target.step_ids.join(', '):' · no known challenge match')));holder.append(list);}else holder.append(el('p','No literal IP target recorded for this action.','small'));
 if(action?.context_step_ids?.length)holder.append(el('p','Dashed links show Monitor challenge context, not observed network contact.','small'));appendActivitySource(holder,action);
 const speaker=action?.phase==='reviewing'?'judge':'caf',button=el('button',speaker==='judge'?'View judge transcript':'View agent transcript');button.type='button';button.addEventListener('click',()=>{setVisualInspector('transcripts');setVisualEditorCollapsed(speaker,false);const panel=$('visual-'+speaker+'-panel'),grid=panel.parentElement;grid.scrollTop=panel.offsetTop-grid.offsetTop;$('visual-'+speaker+'-transcript').focus({preventScroll:true});});holder.append(button);
}

function focusVisualActivity(){
 if(visualEditor.activity)setVisualEditorCollapsed('activity',false);
 const flow=visualState.layout?.flow,svg=$('attack-graph');if(!flow||!svg.clientWidth)return false;const w=Math.max(flow.width+60,flow.height*svg.clientWidth/svg.clientHeight),h=w*svg.clientHeight/svg.clientWidth;visualState.view={x:(flow.width-w)/2,y:(flow.height-h)/2,w,h};applyGraphView();return true;
}
