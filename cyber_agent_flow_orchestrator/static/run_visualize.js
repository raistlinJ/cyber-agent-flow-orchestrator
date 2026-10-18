/* Owner-only visual monitor. All labels and evidence are inserted as text. */
const visualState={flowSelection:false,releaseSeen:new Map(),releaseFlashes:new Map(),activity:null,activitySelection:null,receivedAt:0,activityDeadline:0,preparation:{nodes:[],edges:[]},prep:null,run:null,trial:null,node:null,fingerprint:null,prepView:null,view:null,graph:null,positions:new Map(),views:new Map(),drag:null,moved:false};
const graphColors={solved:'#19856b',partial:'#d58b1f',targeted:'#3179d3',other:'#89969c'};
const graphNames={solved:'Solved',partial:'Partial',targeted:'Targeted',other:'Other'};
const svgElement=(tag,attrs={},text)=>{const node=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,String(value));if(text!=null)node.textContent=text;return node;};
function visualTrialKey(trial){return [trial.trial_id||'planned',trial.task_id,trial.attempt].join('|');}
function visualTrials(run){return run?.sample_progress?.visualization?.trials||[];}
function setProgressDisplay(display){
 const visual=display==='visualize',opening=visual&&$('visualize-view-panel').hidden;if(opening)visualActionFollow.actionKey=null;document.body.classList.toggle('visual-workspace',visual);(visual?document.querySelector('#attack-graph-wrap .graph-toolbar>div'):document.querySelector('main>.heading')).append($('refresh-run'));$('phase-view-panel').hidden=visual;$('visualize-view-panel').hidden=!visual;
 for(const button of $('progress-view-tabs').querySelectorAll('[role=tab]')){const selected=(button.dataset.progressDisplay==='visualize')===visual;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;}
 const url=new URL(location.href);url.searchParams.set('display',visual?'visualize':'phases');history.replaceState(null,'',url.pathname+url.search+url.hash);
 if(visual&&visualState.run){renderVisualProgress(visualState.run,true,false);requestAnimationFrame(()=>{resizeVisualEditor();fitVisualGraph(false);});}
}
function initProgressVisualization(){
 initVisualTranscripts();initVisualInspector();initVisualEditor();
 for(const button of $('progress-view-tabs').querySelectorAll('[role=tab]')){button.addEventListener('click',()=>setProgressDisplay(button.dataset.progressDisplay));button.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const buttons=[...$('progress-view-tabs').querySelectorAll('[role=tab]')],index=buttons.indexOf(button),next=event.key==='Home'?0:event.key==='End'?1:1-index;buttons[next].click();buttons[next].focus();}});}
 setInterval(()=>{syncLiveHighlights();syncHintHighlights();},1000);
 $('visual-hint-open').addEventListener('click',openLatestVisualHint);
 $('visual-trial').addEventListener('change',()=>{visualState.flowSelection=false;visualState.activitySelection=null;visualState.trial=$('visual-trial').value;visualState.node=null;renderVisualProgress(visualState.run,true,false);});
 $('graph-preparation').addEventListener('click',focusPreparationLane);$('graph-challenges').addEventListener('click',focusVisualChallenges);
 $('graph-fit').addEventListener('click',()=>fitVisualGraph(true));
 $('graph-focus').addEventListener('click',()=>{if(!focusVisualActivity())focusVisualNode(visualState.graph?.nodes.find(node=>node.focus)?.id||visualState.node);});
 for(const [id,factor] of [['graph-zoom-in',.8],['graph-zoom-out',1.25]])$(id).addEventListener('click',()=>zoomVisualGraph(factor));
 initVisualCanvas('attack-graph',false);initVisualCanvas('preparation-graph',true);
 const display=new URL(location.href).searchParams.get('display');setProgressDisplay(display==='phases'||(!display&&requestedPhase())?'phases':'visualize');
}
function resetVisualProgress(){resetVisualTranscripts();visualActionFollow.actionKey=null;visualActionFollow.releases.clear();clearVisualActionTarget();renderVisualStepCard(null);visualEventView.scope=null;visualEventView.top=0;visualEventView.follow=true;$('visual-events-follow').checked=true;$('visual-events-output').textContent='Waiting for run events…';visualState.flowSelection=false;visualState.releaseSeen.clear();visualState.releaseFlashes.clear();$('visual-hint-unlock').hidden=true;visualState.activity=null;visualState.activitySelection=null;visualState.activityDeadline=0;visualState.activityLeaseKey=null;visualState.receivedAt=0;delete $('visual-live-targets').dataset.signature;$('visual-live-targets').replaceChildren();visualState.preparation={nodes:[],edges:[]};visualState.prep=null;visualState.run=null;visualState.graph=null;visualState.trial=null;visualState.node=null;visualState.fingerprint=null;visualState.view=null;visualState.prepView=null;visualState.layout=null;$('preparation-graph').replaceChildren();visualState.positions.clear();visualState.views.clear();$('attack-graph').replaceChildren();$('visual-trial').replaceChildren();$('visual-node-detail').replaceChildren();$('visual-assistance').replaceChildren();$('visual-monitor-status').textContent='Waiting for run data.';}
function renderVisualProgress(run,force=false,received=true){
 visualState.run=run;visualState.preparation=preparationOf(run);if(visualState.prep&&!preparationItem())visualState.prep=null;$('graph-preparation').disabled=!visualState.preparation.nodes.length;const trials=visualTrials(run),select=$('visual-trial');
 const current=run.sample_progress?.current_trial;
 if(!trials.some(trial=>visualTrialKey(trial)===visualState.trial))visualState.trial=visualTrialKey(trials.find(trial=>trial.trial_id===current?.trial_id&&trial.attempt===current?.attempt)||trials.at(-1)||{});
 const options=JSON.stringify(trials.map(trial=>[visualTrialKey(trial),trial.trial_id,trial.condition_id,trial.task_id,trial.attempt]));
 if(select.dataset.options!==options){select.replaceChildren();for(const trial of trials)select.add(new Option((trial.trial_id||'Planned task')+' · '+(trial.condition_id||trial.task_id)+' · '+(trial.attempt?'attempt '+trial.attempt:'not started'),visualTrialKey(trial)));select.dataset.options=options;}select.value=visualState.trial;select.disabled=!trials.length;
 const trial=trials.find(item=>visualTrialKey(item)===visualState.trial),fingerprint=JSON.stringify({trial:trial?{...trial,activity:activityFingerprint(trial.activity)}:null,preparation:visualState.preparation});setVisualActivity(trial?.activity,received);observeVisualReleases(trial);renderHintUnlock(trial);renderVisualTranscripts(run,trial);renderVisualWorkspace(run);

 if(!trial){visualState.node=null;visualState.graph={nodes:[],edges:[]};$('visual-assistance').replaceChildren();$('visual-assistance-count').textContent='';for(const state of Object.keys(graphNames))$('visual-count-'+state).textContent='0';$('visual-monitor-status').textContent='Waiting for the saved challenge plan.';if(force||visualState.fingerprint!==fingerprint){visualState.fingerprint=fingerprint;if(!visualState.prep&&visualState.preparation.nodes.length)visualState.prep={kind:'node',id:visualState.preparation.nodes.find(node=>['running','failed'].includes(node.status))?.id||visualState.preparation.nodes[0].id};drawAttackGraph(visualState.graph);renderVisualNode();requestAnimationFrame(()=>fitVisualGraph(false));}return;}
 visualState.graph=trial;
 const monitor=trial.monitor||{},latest=monitor.latest;
 $('visual-monitor-status').textContent=latest?.status==='running'?'Monitor reviewing turn '+latest.turn+'…':latest?.status==='error'?'Monitor check failed · showing the last supported observations':monitor.checks?'Monitor · '+monitor.checks+' checks · last turn '+(latest?.turn??'—')+(monitor.updated_at?' · '+new Date(monitor.updated_at).toLocaleTimeString():''):monitor.enabled?'Monitor waiting for the first evidence checkpoint':'No intermediate monitor observations recorded';
 $('visual-monitor-status').className='visual-monitor-status '+(latest?.status==='error'?'monitor-error':'');

 for(const state of Object.keys(graphNames))$('visual-count-'+state).textContent=trial.counts[state];
 if(!force&&visualState.fingerprint===fingerprint){syncHintHighlights();return;}
 visualState.fingerprint=fingerprint;
 if(!trial.nodes.some(node=>node.id===visualState.node))visualState.node=trial.nodes.find(node=>node.focus)?.id||trial.nodes.find(node=>node.state==='partial'||node.state==='targeted')?.id||trial.nodes[0]?.id;
 drawAttackGraph(trial);renderVisualNode();renderVisualAssistance(trial);requestAnimationFrame(()=>fitVisualGraph(false));
}
function graphLayout(trial){
 const nodes=trial.nodes,byId=new Map(nodes.map(node=>[node.id,node])),ranks=new Map();
 function rank(id,seen=new Set()){if(ranks.has(id))return ranks.get(id);if(seen.has(id))return 0;const node=byId.get(id);if(!node)return 0;seen.add(id);const value=Math.max(0,...(node.requires||[]).filter(key=>byId.has(key)).map(key=>rank(key,new Set(seen))+1));ranks.set(id,value);return value;}
 const groups=new Map();for(const node of nodes){const level=rank(node.id);if(!groups.has(level))groups.set(level,[]);groups.get(level).push(node);}
 const largest=Math.max(1,...[...groups.values()].map(group=>group.length)),positions=new Map();
 for(const [level,group] of groups)group.forEach((node,index)=>positions.set(node.id,{x:60+level*290,y:60+(largest-group.length)*92+index*184,w:230,h:148}));
 // Independent challenges use the width of the persistent canvas rather than a tall column.
 if(groups.size===1&&nodes.length>1){const columns=Math.min(4,nodes.length);nodes.forEach((node,index)=>positions.set(node.id,{x:60+(index%columns)*290,y:60+Math.floor(index/columns)*184,w:230,h:148}));}
 if(visualEditor.challenges)positions.clear();
 const flow=activityFlowLayout(trial),offset=flow.width+90;for(const p of positions.values()){p.x+=offset;p.y+=20;}
 return {positions,flow,width:Math.max(offset+360,...[...positions.values()].map(p=>p.x+p.w+50)),height:Math.max(flow.height,300,...[...positions.values()].map(p=>p.y+p.h+50))};
}
function nodeTitleLines(title){const words=String(title).split(/\s+/),lines=[''];for(const word of words){let last=lines.length-1;if((lines[last]+' '+word).trim().length>27&&lines[last]){if(lines.length===2){lines[last]=lines[last].slice(0,23)+'…';break;}lines.push(word);}else lines[last]=(lines[last]+' '+word).trim();}return lines.map(line=>line.length>27?line.slice(0,26)+'…':line);}
function drawAttackGraph(trial){
 const svg=$('attack-graph'),layout=graphLayout(trial);visualState.positions=layout.positions;visualState.layout=layout;svg.replaceChildren(svgElement('title',{},'Attack graph monitored by the judge'),svgElement('desc',{},'Challenges are connected by prerequisites. Select a node for observed evidence and released hints. Green solved, amber partial, blue targeted, grey other.'));
 const defs=svgElement('defs');for(const [state,color] of Object.entries(graphColors)){const marker=svgElement('marker',{id:'graph-arrow-'+state,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'});marker.append(svgElement('path',{d:'M0 0 L10 5 L0 10 z',fill:color}));defs.append(marker);}svg.append(defs);drawPreparationCanvas();drawActivityFlow(svg,layout);
 if(!trial.nodes.length||visualEditor.challenges)svg.append(svgElement('text',{x:layout.flow.width+120,y:108,class:'visual-lane-note'},visualEditor.challenges?trial.nodes.length+' challenges collapsed':'Waiting for the saved challenge plan.'));
 const byId=new Map(trial.nodes.map(node=>[node.id,node]));
 for(const edge of trial.edges){const a=layout.positions.get(edge.source),b=layout.positions.get(edge.target);if(!a||!b)continue;const state=byId.get(edge.source).state,color=graphColors[state];svg.append(svgElement('path',{d:`M${a.x+a.w} ${a.y+a.h/2} C${a.x+a.w+35} ${a.y+a.h/2},${b.x-35} ${b.y+b.h/2},${b.x-8} ${b.y+b.h/2}`,fill:'none',stroke:color,'stroke-width':state==='solved'?3:2,'stroke-dasharray':state==='other'?'5 5':'none','marker-end':'url(#graph-arrow-'+state+')',class:'attack-edge'}));}
 for(const node of trial.nodes){const p=layout.positions.get(node.id);if(!p)continue;const g=svgElement('g',{transform:`translate(${p.x},${p.y})`,class:'attack-node '+node.state+(node.focus?' monitor-focus':''),role:'button',tabindex:0,'aria-label':node.title+' · '+graphNames[node.state]+' · '+node.satisfied+' of '+node.total+' criteria · '+node.hints+' hints · '+node.solutions+' solutions','aria-pressed':String(!visualState.prep&&!visualState.activitySelection&&!visualState.flowSelection&&node.id===visualState.node),'data-node-id':node.id});
  g.append(svgElement('rect',{x:-6,y:-6,width:p.w+12,height:p.h+12,rx:17,class:'live-node-halo'}),svgElement('title',{},node.title),svgElement('rect',{x:0,y:0,width:p.w,height:p.h,rx:13,class:'attack-node-surface'}),svgElement('rect',{x:0,y:16,width:5,height:p.h-32,rx:2,fill:graphColors[node.state]}));
  g.append(svgElement('circle',{cx:23,cy:23,r:10,fill:graphColors[node.state]}),svgElement('text',{x:23,y:27,'text-anchor':'middle',class:'attack-node-symbol'},node.state==='solved'?'✓':node.state==='partial'?'◐':node.state==='targeted'?'◎':'–'));
  g.append(svgElement('text',{x:41,y:27,class:'attack-node-state'},graphNames[node.state].toUpperCase()));g.append(svgElement('text',{x:215,y:27,'text-anchor':'end',class:'attack-live-label'},'ACTIVE'));if(node.focus)g.append(svgElement('text',{x:215,y:27,'text-anchor':'end',class:'attack-focus-label'},'CAF FOCUS'));
  nodeTitleLines(node.title).forEach((line,index)=>g.append(svgElement('text',{x:17,y:49+index*17,class:'attack-node-title'},line)));
  g.append(svgElement('text',{x:17,y:91,class:'attack-node-meta attack-target-label'},node.target||('Node '+(node.node_id||node.id))));
  const total=node.total||1,barWidth=100;for(let n=0;n<Math.min(total,32);n++)g.append(svgElement('rect',{x:17+n*barWidth/total,y:103,width:Math.max(1,barWidth/total-2),height:5,rx:2,fill:n<node.satisfied?graphColors.solved:'#dce5e3'}));
  const latestHelp=node.releases.at(-1);if(latestHelp)g.append(svgElement('text',{x:215,y:109,'text-anchor':'end',class:'attack-node-meta hint-turn '+(latestHelp.solution?'solution':'hint')},'Help · turn '+latestHelp.turn));
  drawHintUnlock(g,node,p);
  const choose=()=>{if(visualState.moved)return;visualState.flowSelection=false;visualState.activitySelection=null;visualState.prep=null;visualState.node=node.id;updateVisualSelection();renderVisualNode();setVisualInspector('details');};g.addEventListener('click',choose);g.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();visualState.moved=false;choose();}});svg.append(g);
 }
 visualState.view=visualState.views.get(visualState.trial)||null;if(visualState.view)applyGraphView();syncLiveHighlights();syncHintHighlights();
}
function applyGraphView(){const v=visualState.view;if(!v)return;$('attack-graph').setAttribute('viewBox',`${v.x} ${v.y} ${v.w} ${v.h}`);visualState.views.set(visualState.trial,{...v});const width=$('attack-graph').clientWidth;$('graph-zoom-label').textContent=Math.round(width/v.w*100)+'%';}
function fitVisualGraph(force){
 const svg=$('attack-graph'),bounds=svg.getBoundingClientRect(),layout=visualState.layout;if(!bounds.width||!bounds.height||!layout)return;
 if(!visualState.view||force||visualState.view.autoFit){const ratio=bounds.width/bounds.height,w=Math.max(layout.width,layout.height*ratio,bounds.width/.85)*1.1,h=w/ratio;visualState.view={x:(layout.width-w)/2,y:(layout.height-h)/2,w,h,autoFit:true};}
 else{const height=visualState.view.w*bounds.height/bounds.width;if(Math.abs(visualState.view.h-height)>visualState.view.w/bounds.width){visualState.view.y+=(visualState.view.h-height)/2;visualState.view.h=height;}}applyGraphView();
}
function zoomVisualGraph(factor){const v=visualState.view;if(!v)return;const w=Math.max(250,Math.min(12000,v.w*factor)),h=w*$('attack-graph').clientHeight/$('attack-graph').clientWidth;visualState.view={x:v.x+(v.w-w)/2,y:v.y+(v.h-h)/2,w,h};applyGraphView();}
function focusVisualNode(id){if(!id){focusPreparationLane();return;}const p=visualState.positions.get(id),svg=$('attack-graph');if(!p){focusPreparationLane();return;}if(!svg.clientWidth)return;const w=Math.max(500,svg.clientWidth),h=w*svg.clientHeight/svg.clientWidth;visualState.view={x:p.x+p.w/2-w/2,y:p.y+p.h/2-h/2,w,h};applyGraphView();}
function renderVisualNode(){
 if(visualState.flowSelection){renderVisualFlowDetail();return;}
 if(visualState.activitySelection){renderActivityTarget();return;}
 if(visualState.prep){renderPreparationDetail();return;}
 const node=visualState.graph?.nodes.find(item=>item.id===visualState.node),target=$('visual-node-detail'),opened=openReleaseKeys(target);target.replaceChildren();if(!node)return;
 const status=el('span',graphNames[node.state],'visual-state-chip '+node.state);target.append(status,el('h3',node.title),el('p',node.target||'Scenario node '+(node.node_id||node.id),'small'));appendNodeActivity(target,node);
 target.append(el('p',`${node.satisfied} / ${node.total} criteria supported`,'visual-node-extent'));const progress=el('progress');progress.max=Math.max(1,node.total);progress.value=node.satisfied;progress.setAttribute('aria-label','Supported criteria for '+node.title);target.append(progress);
 if(node.focus)target.append(el('p','Latest monitor focus'+(node.observation_turn?' · turn '+node.observation_turn:''),'visual-focus-note'));
 if(node.deferred)target.append(el('p','Deferred; completion remains unverified.','notice'));else if(!node.eligible)target.append(el('p','Prerequisites have not been verified complete.','small'));
 if(node.observation)target.append(el('p',node.observation));
 target.append(el('h4','Discovered evidence'));
 if(node.discoveries.length){const list=el('ul');for(const item of node.discoveries)list.append(el('li',item));target.append(list);}else target.append(el('p','No explicit discovery observations recorded.','small'));
 for(const citation of node.evidence)target.append(el('code',citation.file+' · bytes '+citation.offset+'–'+(citation.offset+citation.limit)));
 if(node.audit)target.append(el('p','Monitor audit: '+node.audit,'small'));
 target.append(el('h4','Completion criteria'));
 for(const criterion of node.criteria){const details=el('details',null,'visual-criterion');details.append(el('summary',(criterion.status==='satisfied'?'✓ ':criterion.status==='unmet'?'○ ':'? ')+criterion.requirement),el('p',criterion.status+(criterion.essential?' · required':' · optional'),'small'));if(criterion.reason)details.append(el('p',criterion.reason));for(const citation of criterion.evidence)details.append(el('code',citation.file+' · bytes '+citation.offset+'–'+(citation.offset+citation.limit)));target.append(details);}
 target.append(el('h4','Assistance given'));
 if(!node.releases.length)target.append(el('p','No hints or solutions released for this challenge.','small'));
 for(const release of node.releases)target.append(visualReleaseDetails(release,'visual-hint',(release.solution?'Solution':'Hint')+' · turn '+release.turn,opened));
 syncHintHighlights();
}
function renderVisualAssistance(trial){
 const target=$('visual-assistance'),opened=openReleaseKeys(target);target.replaceChildren();const events=trial.releases||[];$('visual-assistance-count').textContent=events.filter(event=>!event.solution).length+' hints · '+events.filter(event=>event.solution).length+' solutions';if(!events.length){target.append(el('p','No assistance has been released.','small'));return;}
 const byId=new Map(trial.nodes.map(node=>[node.id,node]));for(const event of events)target.append(visualReleaseDetails(event,'visual-assistance-event',(event.solution?'Solution':'Hint')+' · turn '+event.turn+' · '+(byId.get(event.step_id)?.title||event.node_id||'Task-wide guidance'),opened));syncHintHighlights();
}

/* Release indicators reflect delivered assistance only, never planned/private hints. */
function visualReleaseKey(event){return JSON.stringify([event.id,event.step_id,event.node_id,event.turn,event.at,Boolean(event.solution),event.text]);}
function visualReleaseScope(trial){return (visualState.run?.output||visualState.run?.workflow_id||'run')+'|'+visualTrialKey(trial);}
function observeVisualReleases(trial){
 if(!trial)return;const scope=visualReleaseScope(trial),events=trial.releases||[],seen=visualState.releaseSeen.get(scope),now=performance.now();
 for(const [key,flash] of visualState.releaseFlashes)if(flash.until<=now)visualState.releaseFlashes.delete(key);
 if(seen){for(const event of events){const key=visualReleaseKey(event),at=Date.parse(event.at),age=Date.now()-at;if(!seen.has(key)&&visualRunLive()&&trial.status==='running'&&(!Number.isFinite(at)||(age>=-15000&&age<=30000)))visualState.releaseFlashes.set(scope+'|'+key,{scope,key,solution:Boolean(event.solution),until:now+8000});seen.add(key);}}
 else visualState.releaseSeen.set(scope,new Set(events.map(visualReleaseKey)));
}
function liveHintFlashes(){const trial=visualState.graph;if(!trial?.releases||!visualRunLive()||trial.status!=='running')return [];const scope=visualReleaseScope(trial);return [...visualState.releaseFlashes.values()].filter(flash=>flash.scope===scope&&flash.until>performance.now());}
function syncHintHighlights(){
 const flashes=liveHintFlashes(),keys=new Set(flashes.map(flash=>flash.key));
 for(const group of $('attack-graph').querySelectorAll('.attack-node')){const node=visualState.graph?.nodes.find(node=>node.id===group.dataset.nodeId),released=(node?.releases||[]).filter(event=>keys.has(visualReleaseKey(event)));group.classList.toggle('assistance-new',released.length>0);group.classList.toggle('assistance-new-solution',released.some(event=>event.solution));}
 for(const detail of document.querySelectorAll('#visual-node-detail [data-release-key],#visual-assistance [data-release-key]'))detail.classList.toggle('new-release',keys.has(detail.dataset.releaseKey));
 const banner=$('visual-hint-unlock'),latest=visualState.graph?.releases?.at(-1);banner.classList.toggle('new-release',Boolean(latest&&keys.has(visualReleaseKey(latest))));
}
function unlockIconSVG(open=true){const icon=svgElement('svg',{viewBox:'0 0 20 20',width:20,height:20,'aria-hidden':'true',class:'hint-unlock-icon'});icon.append(svgElement('rect',{x:4,y:9,width:12,height:9,rx:2}),svgElement('path',{d:open?'M8 9 V5 a4 4 0 0 1 8 0':'M6 9 V5 a4 4 0 0 1 8 0 V9'}),svgElement('path',{d:'M10 12 v3'}));return icon;}
function drawHintUnlock(group,node,p){
 const released=node.hints||node.solutions,kind=node.solutions?'solution':released?'hint':'none',badge=svgElement('g',{class:'attack-assistance-badge '+kind,'aria-hidden':'true'});
 badge.append(svgElement('path',{d:`M0 120 H${p.w} V${p.h-13} Q${p.w} ${p.h} ${p.w-13} ${p.h} H13 Q0 ${p.h} 0 ${p.h-13} Z`,class:'assistance-band'}),svgElement('line',{x1:0,y1:120,x2:p.w,y2:120,class:'assistance-band-divider'}));
 const lock=unlockIconSVG(Boolean(released));lock.setAttribute('x',13);lock.setAttribute('y',124);lock.setAttribute('width',17);lock.setAttribute('height',17);badge.append(lock);
 const counts=[node.hints?node.hints+' hint'+(node.hints===1?'':'s'):'',node.solutions?node.solutions+' solution'+(node.solutions===1?'':'s'):''].filter(Boolean).join(' · ');
 badge.append(svgElement('text',{x:38,y:138,class:'assistance-band-label'},released?counts+' unlocked':'No guidance released'));group.append(svgElement('rect',{x:-2,y:-2,width:p.w+4,height:p.h+4,rx:15,class:'hint-release-halo'}),badge);
}
function renderHintUnlock(trial){
 const banner=$('visual-hint-unlock'),event=trial?.releases?.at(-1);banner.hidden=!event;if(!event)return;
 const node=trial.nodes.find(node=>node.releases.some(item=>visualReleaseKey(item)===visualReleaseKey(event))),label=(event.solution?'Solution':'Hint')+' unlocked · turn '+event.turn;
 banner.classList.toggle('solution',Boolean(event.solution));const heading=$('visual-hint-message');if(heading.textContent!==label)heading.textContent=label;
 $('visual-hint-context').textContent=(node?.title||'Task-wide guidance')+' · Guidance released; challenge completion is checked separately.';
 $('visual-hint-open').textContent='View released '+(event.solution?'solution':'hint');
 if(!banner.querySelector('.hint-unlock-icon'))banner.prepend(unlockIconSVG());
}
function openLatestVisualHint(){
 const event=visualState.graph?.releases?.at(-1);if(!event)return;const key=visualReleaseKey(event),node=visualState.graph.nodes.find(node=>node.releases.some(item=>visualReleaseKey(item)===key));let holder=$('visual-assistance');setVisualInspector(node?'details':'hints');
 if(node){if(visualEditor.challenges)setVisualEditorCollapsed('challenges',false);visualState.flowSelection=false;visualState.prep=null;visualState.activitySelection=null;visualState.node=node.id;focusVisualNode(node.id);updateVisualSelection();renderVisualNode();holder=$('visual-node-detail');}
 const detail=[...holder.querySelectorAll('[data-release-key]')].find(item=>item.dataset.releaseKey===key);if(detail){detail.open=true;detail.querySelector('summary').focus({preventScroll:true});detail.scrollIntoView({block:'nearest',behavior:'instant'});}
}
function openReleaseKeys(holder){return new Set([...holder.querySelectorAll('details[open][data-release-key]')].map(item=>item.dataset.releaseKey));}
function visualReleaseDetails(event,className,label,opened){
 const key=visualReleaseKey(event),details=el('details',null,className+' '+(event.solution?'solution':'hint'));details.dataset.releaseKey=key;details.open=opened.has(key);const summary=el('summary'),tier=event.tier?' · '+event.tier+' assistance':'';summary.append(unlockIconSVG(),el('span',label+tier));details.append(summary);
 if(event.solution||!event.next_action){details.append(el('pre',event.text+(event.truncated?'\n[Shortened; full released text is in the run bundle.]':'')));return details;}
 const body=el('div',null,'released-coaching');
 const observation=event.monitor_observation?.summary;if(observation)body.append(el('p','What I observed: '+observation));
 if(event.interpretation)body.append(el('p','What that suggests: '+event.interpretation));
 body.append(el('p','What I would do next: '+event.next_action));
 if(event.success_signal)body.append(el('p','What would confirm progress: '+event.success_signal));
 if(event.truncated)body.append(el('p','Shortened here; the complete release is in the run bundle.','small'));
 details.append(body);return details;
}

/* A persistent canvas with independently scrollable inspection panels. Updates never
   change the reader's chosen panel; explicit graph selections open their details. */
const visualEventView={scope:null,top:0,follow:true};
const visualInspectorPanels={details:'visual-node-detail',transcripts:'visual-transcripts',hints:'visual-pane-hints',events:'visual-pane-events'};
function setVisualInspector(name){
 if(!visualInspectorPanels[name])return;setVisualEditorCollapsed('panel',false);
 for(const [key,id] of Object.entries(visualInspectorPanels)){$(id).hidden=key!==name;const tab=$('visual-tab-'+key);tab.setAttribute('aria-selected',String(key===name));tab.tabIndex=key===name?0:-1;}
 if(name==='events'){$('visual-events-output').scrollTop=visualEventView.follow?$('visual-events-output').scrollHeight:visualEventView.top;}
 if(visualState.run)renderVisualTranscripts(visualState.run,visualTrials(visualState.run).find(trial=>visualTrialKey(trial)===visualState.trial));

}
function initVisualInspector(){
 const buttons=[...$('visual-inspector-tabs').querySelectorAll('[role=tab]')];
 for(const button of buttons){button.addEventListener('click',()=>setVisualInspector(button.dataset.inspector));button.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const n=buttons.indexOf(button),next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(n+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;buttons[next].click();buttons[next].focus();});}
 $('visual-events-output').addEventListener('scroll',()=>{const node=$('visual-events-output');if(!node.clientHeight)return;visualEventView.top=node.scrollTop;visualEventView.follow=node.scrollHeight-node.clientHeight-node.scrollTop<30;$('visual-events-follow').checked=visualEventView.follow;});
 $('visual-events-follow').addEventListener('change',()=>{visualEventView.follow=$('visual-events-follow').checked;if(visualEventView.follow)$('visual-events-output').scrollTop=$('visual-events-output').scrollHeight;});
}
function renderVisualWorkspace(run){
 renderVisualStepCard(run);
 const phase=run.workflow_progress?.current,label=phase?.label||run.recorded_status||'Waiting for run data',phaseLabel=label+(phase?.status?' · '+phase.status:'');if($('visual-now-phase').textContent!==phaseLabel)$('visual-now-phase').textContent=phaseLabel;
 const trial=visualTrials(run).find(trial=>visualTrialKey(trial)===visualState.trial);$('visual-inspector-context').textContent=trial?.trial_id?(trial.condition_id||trial.task_id)+' · attempt '+(trial.attempt||'—'):'Lab preparation';
 const scope=run.output||run.workflow_id;if(visualEventView.scope!==scope){visualEventView.scope=scope;visualEventView.top=0;visualEventView.follow=true;$('visual-events-follow').checked=true;}
 const target=$('visual-events-output'),text=(run.sample_progress?.events||[]).slice(-200).map(event=>[event.at,event.kind?'['+event.kind+']':'',event.message||''].filter(Boolean).join(' ')).join('\n')||'No saved run events yet.';
 if(target.textContent!==text){target.textContent=text;if(target.clientHeight)target.scrollTop=visualEventView.follow?target.scrollHeight:visualEventView.top;}
}

function focusVisualChallenges(){
 if(visualEditor.challenges)setVisualEditorCollapsed('challenges',false);
 const points=[...visualState.positions.values()],svg=$('attack-graph');if(!points.length||!svg.clientWidth){focusPreparationLane();return;}
 const left=Math.min(...points.map(p=>p.x))-35,top=Math.min(...points.map(p=>p.y))-35,right=Math.max(...points.map(p=>p.x+p.w))+35,bottom=Math.max(...points.map(p=>p.y+p.h))+35,ratio=svg.clientWidth/svg.clientHeight,w=Math.max(right-left,(bottom-top)*ratio,500),h=w/ratio;
 visualState.view={x:(left+right-w)/2,y:(top+bottom-h)/2,w,h};applyGraphView();
}

/* Each canvas has its own view; panning preparation never moves the execution graph. */
function initVisualCanvas(id,preparation){
 const svg=$(id);let drag=null;
 const getView=()=>preparation?visualState.prepView:visualState.view;
 const setView=view=>{if(preparation){visualState.prepView=view;applyPreparationView();}else{visualState.view=view;applyGraphView();}};
 svg.addEventListener('pointerdown',event=>{if(event.button!==0||!getView())return;drag={x:event.clientX,y:event.clientY,view:{...getView()}};visualState.moved=false;});
 svg.addEventListener('pointermove',event=>{if(!drag)return;const dx=event.clientX-drag.x,dy=event.clientY-drag.y;if(Math.hypot(dx,dy)<=4&&!visualState.moved)return;visualState.moved=true;if(!svg.hasPointerCapture(event.pointerId))svg.setPointerCapture(event.pointerId);const bounds=svg.getBoundingClientRect();setView({...drag.view,x:drag.view.x-dx/bounds.width*drag.view.w,y:drag.view.y-dy/bounds.height*drag.view.h,autoFit:false});});
 const end=()=>{drag=null;};svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);
 svg.addEventListener('wheel',event=>{event.preventDefault();if(preparation)zoomPreparationGraph(event.deltaY>0?1.12:.89);else zoomVisualGraph(event.deltaY>0?1.12:.89);},{passive:false});
 new ResizeObserver(()=>{if(!$('visualize-view-panel').hidden){if(preparation)fitPreparationGraph(false);else if(visualState.graph)fitVisualGraph(false);}}).observe(svg);
 if(preparation)for(const [button,factor] of [['prep-zoom-in',.8],['prep-zoom-out',1.25]])$(button).addEventListener('click',()=>zoomPreparationGraph(factor));
}

/* Owner-side workbench preferences affect presentation only, never experiment inputs. */
const visualEditorDefaults={preparation:false,execution:false,panel:false,activity:false,challenges:false,caf:false,judge:false,prepWidth:260,panelHeight:260,followActions:false};
const visualEditor={...visualEditorDefaults};
const visualEditorLabels={preparation:'preparation pane',execution:'execution canvas',panel:'output panel',activity:'agent activity',challenges:'challenges',caf:'CAF transcript',judge:'judge transcript'};
function saveVisualEditor(){try{localStorage.setItem('caf-visual-editor-layout-v1',JSON.stringify(visualEditor));}catch{}}
function initVisualEditor(){
 try{const saved=JSON.parse(localStorage.getItem('caf-visual-editor-layout-v1')||'{}');for(const key of Object.keys(visualEditorDefaults)){if(typeof visualEditorDefaults[key]==='boolean'&&typeof saved[key]==='boolean')visualEditor[key]=saved[key];}if(Number.isFinite(saved.prepWidth))visualEditor.prepWidth=Math.max(120,Math.min(480,saved.prepWidth));if(Number.isFinite(saved.panelHeight))visualEditor.panelHeight=Math.max(180,Math.min(500,saved.panelHeight));}catch{}
 $('visual-follow-actions').checked=visualEditor.followActions;$('visual-follow-actions').addEventListener('change',()=>{visualEditor.followActions=$('visual-follow-actions').checked;saveVisualEditor();visualActionFollow.actionKey=null;visualActionFollow.releases.clear();clearVisualActionTarget();scheduleVisualActionFollow();});
 for(const button of document.querySelectorAll('[data-editor-toggle]'))button.addEventListener('click',()=>setVisualEditorCollapsed(button.dataset.editorToggle,!visualEditor[button.dataset.editorToggle]));
 $('visual-step-open').addEventListener('click',()=>{const phase=currentVisualStep(visualState.run);if(phase){setProgressDisplay('phases');setProgressPhase(phase.id,{follow:true});}});
 initEditorSash('editor-sidebar-sash',true);initEditorSash('editor-panel-sash',false);
 new ResizeObserver(resizeVisualEditor).observe($('debug-console'));new ResizeObserver(resizeVisualEditor).observe(document.querySelector('.visual-header'));window.addEventListener('resize',resizeVisualEditor);
 const status=()=>{const text=$('run-status').textContent;if($('visual-editor-status').textContent!==text)$('visual-editor-status').textContent=text;};new MutationObserver(status).observe($('run-status'),{childList:true,subtree:true,characterData:true});$('visual-trial').addEventListener('change',status);
 applyVisualEditor();status();
}
function setVisualEditorCollapsed(name,collapsed){
 if(!(name in visualEditorLabels)||visualEditor[name]===collapsed)return;
 if(collapsed&&['caf','judge','panel'].includes(name))for(const kind of name==='panel'?['caf','judge']:[name]){const view=visualTranscriptView(kind),output=$('visual-'+kind+'-transcript');if(view){if(output.clientHeight)view.top=output.scrollTop;view.visible=false;}}
 visualEditor[name]=collapsed;saveVisualEditor();applyVisualEditor();
 if(['activity','challenges'].includes(name)&&visualState.graph){const focus=document.activeElement?.dataset.editorFold===name;drawAttackGraph(visualState.graph);renderVisualNode();fitVisualGraph(true);if(focus)$('attack-graph').querySelector('[data-editor-fold='+name+']')?.focus({preventScroll:true});}
 if(['caf','judge','panel'].includes(name)&&!collapsed&&visualState.run)renderVisualTranscripts(visualState.run,visualTrials(visualState.run).find(trial=>visualTrialKey(trial)===visualState.trial));
}
function applyVisualEditor(){
 const workbench=$('visual-editor');for(const name of ['preparation','execution','panel'])workbench.classList.toggle('is-'+name+'-collapsed',visualEditor[name]);
 for(const kind of ['caf','judge'])$('visual-'+kind+'-panel').classList.toggle('is-collapsed',visualEditor[kind]);
 for(const button of document.querySelectorAll('[data-editor-toggle]')){const name=button.dataset.editorToggle,open=!visualEditor[name],label=(open?'Collapse ':'Expand ')+visualEditorLabels[name];button.setAttribute('aria-expanded',String(open));button.setAttribute('aria-label',label);button.title=label;if(button.classList.contains('editor-pane-toggle'))button.textContent=name==='preparation'?(open?'‹':'›'):(open?'⌄':'›');else button.setAttribute('aria-pressed',String(open));}
 resizeVisualEditor();
}
function resizeVisualEditor(){
 if($('visualize-view-panel').hidden)return;
 const editor=$('visual-editor'),top=editor.getBoundingClientRect().top+scrollY,height=Math.max(480,innerHeight-top-12),width=Math.min(visualEditor.prepWidth,Math.max(120,editor.clientWidth*.38)),panel=Math.min(visualEditor.panelHeight,Math.max(180,height-280));
 editor.style.setProperty('--editor-height',height+'px');editor.style.setProperty('--editor-prep-width',(visualEditor.preparation?32:width)+'px');editor.style.setProperty('--editor-sidebar-sash',visualEditor.preparation?'0px':'4px');editor.style.setProperty('--editor-canvas-track',visualEditor.execution&&!visualEditor.panel?'32px':'minmax(0,1fr)');editor.style.setProperty('--editor-panel-track',visualEditor.panel?'34px':visualEditor.execution?'minmax(0,1fr)':panel+'px');editor.style.setProperty('--editor-panel-sash',visualEditor.panel?'0px':'4px');
 for(const [id,value,collapsed] of [['editor-sidebar-sash',Math.round(width),visualEditor.preparation],['editor-panel-sash',Math.round(panel),visualEditor.panel]]){const sash=$(id);sash.setAttribute('aria-valuenow',String(value));sash.setAttribute('aria-disabled',String(collapsed));sash.tabIndex=collapsed?-1:0;}
}
function initEditorSash(id,sidebar){
 const sash=$(id),key=sidebar?'prepWidth':'panelHeight';let drag=null;
 const update=value=>{const max=sidebar?Math.min(480,$('visual-editor').clientWidth*.38):Math.min(500,$('visual-editor').clientHeight-220);visualEditor[key]=Math.max(sidebar?120:180,Math.min(max,value));applyVisualEditor();};
 sash.addEventListener('pointerdown',event=>{if(event.button!==0)return;event.preventDefault();sash.setPointerCapture(event.pointerId);drag={at:sidebar?event.clientX:event.clientY,value:Number(sash.getAttribute('aria-valuenow'))};sash.classList.add('is-dragging');});
 sash.addEventListener('pointermove',event=>{if(drag)update(drag.value+(sidebar?event.clientX-drag.at:drag.at-event.clientY));});
 const end=()=>{if(drag)saveVisualEditor();drag=null;sash.classList.remove('is-dragging');};sash.addEventListener('pointerup',end);sash.addEventListener('pointercancel',end);
 sash.addEventListener('dblclick',()=>{update(visualEditorDefaults[key]);saveVisualEditor();});
 sash.addEventListener('keydown',event=>{const keys=sidebar?['ArrowLeft','ArrowRight']:['ArrowDown','ArrowUp'];if(!keys.includes(event.key))return;event.preventDefault();update(Number(sash.getAttribute('aria-valuenow'))+(event.key===keys[0]?-16:16));saveVisualEditor();});
}
function drawVisualFoldHeader(group,x,y,label,name){
 const collapsed=visualEditor[name],heading=svgElement('g',{class:'visual-fold-heading','aria-expanded':String(!collapsed),'data-editor-fold':name});heading.append(svgElement('rect',{x:x-8,y:y-19,width:240,height:28,rx:3,fill:'transparent'}),svgElement('text',{x,y,class:'visual-lane-title'},(collapsed?'▸ ':'▾ ')+label.toUpperCase()));flowSelectable(heading,(collapsed?'Expand ':'Collapse ')+label.toLowerCase(),()=>setVisualEditorCollapsed(name,!visualEditor[name]));group.append(heading);
}

/* The step card follows the coordinator, independently of inspected nodes/trials. */
function currentVisualStep(run){
 const progress=run?.workflow_progress,steps=progress?.steps||[],id=progress?.current?.id||progress?.current_step;
 const saved=steps.find(step=>step.id===id);
 return progress?.current?{...saved,...progress.current}:saved||steps.find(step=>step.active)||null;
}
function renderVisualStepCard(run){
 const phase=currentVisualStep(run),progress=run?.workflow_progress,steps=progress?.steps||[],index=phase?steps.findIndex(step=>step.id===phase.id):-1,total=steps.length||progress?.total_steps||0;
 const terminal=['completed','completed_with_errors','failed','interrupted','cancelled'].includes(run?.recorded_status),status=phase?.status==='running'&&terminal?run.recorded_status:phase?.status||run?.recorded_status||'pending';
 const names={running:'Running',evaluating:'Running',preparing:'Preparing',queued:'Queued',ready:'Ready',pending:'Pending',completed:'Completed',completed_with_errors:'Completed with errors',failed:'Failed',interrupted:'Interrupted',cancelled:'Cancelled',skipped:'Skipped'};
 const text={
  'visual-step-number':index>=0?'Step '+(index+1)+' of '+total:total?'Not started · '+total+' steps':'Waiting',
  'visual-step-status':names[status]||String(status).replaceAll('_',' '),
  'visual-step-title':phase?String(phase.label||phase.id).replace(/^\d+\.\s*/,''):terminal?'No recorded workflow step':run?'Waiting to start':'Waiting for run data',
  'visual-step-description':phase?.description||(phase?'No description recorded for this step.':run?.error||run?.message||'The current workflow step will appear here.'),
  'visual-step-operation':String(phase?.error||phase?.operation||'').slice(0,1000)
 };
 for(const [id,value] of Object.entries(text))if($(id).textContent!==value)$(id).textContent=value;
 $('visual-step-operation').hidden=!text['visual-step-operation'];$('visual-step-card').dataset.status=status;
 $('visual-step-open').disabled=!phase||index<0||index+1>lastReachedPhaseIndex(run);
}

/* Dock Follow Actions changes the visible dock, never keyboard focus or transcript scroll policy. */
const visualActionFollow={queued:false,applying:false,actionKey:null,releases:new Map()};
function clearVisualActionTarget(){for(const panel of document.querySelectorAll('.visual-transcript-panel.is-follow-target'))panel.classList.remove('is-follow-target');}
function scheduleVisualActionFollow(){
 if(!visualEditor.followActions||visualActionFollow.queued||visualActionFollow.applying)return;
 visualActionFollow.queued=true;requestAnimationFrame(()=>{visualActionFollow.queued=false;followVisualAction();});
}
function followVisualAction(){
 const run=visualState.run;
 if(!visualEditor.followActions||$('visualize-view-panel').hidden||!run||visualActionFollow.applying)return;
 if(!visualRunLive()||performance.now()-visualState.receivedAt>=45000){clearVisualActionTarget();return;}
 visualActionFollow.applying=true;
 try{
  // A completed/older trial must not hide the current worker while following is enabled.
  const current=run.sample_progress?.current_trial,liveTrial=visualTrials(run).find(trial=>trial.trial_id===current?.trial_id&&trial.attempt===current?.attempt);
  if(liveTrial&&visualTrialKey(liveTrial)!==visualState.trial){visualState.trial=visualTrialKey(liveTrial);visualState.flowSelection=false;visualState.activitySelection=null;visualState.prep=null;visualState.node=null;renderVisualProgress(run,true,false);}
  const caf=visualTranscriptPanels.caf,judge=visualTranscriptPanels.judge,row=caf?.row,phase=currentVisualStep(run),scope=[run.output,visualState.trial].join('|');
  const preparing=['preparing','uploading','starting','stopping','collecting'].includes(row?.transport?.phase);
  const judgeActive=$('visual-judge-panel').getAttribute('aria-busy')==='true',cafActive=$('visual-caf-panel').getAttribute('aria-busy')==='true';
  let target;
  if(judgeActive)target={tab:'transcripts',speaker:'judge',event:judge?.transcript?.events?.at(-1)};
  else if(cafActive&&!preparing)target={tab:'transcripts',speaker:'caf',event:caf?.transcript?.events?.at(-1)};
  else if(phase?.active||preparing||run.recorded_status==='queued')target={tab:'events',event:[phase?.id,phase?.operation,row?.transport?.phase,run.sample_progress?.events?.at(-1)]};
  if(!target){clearVisualActionTarget();return;}
  const key=JSON.stringify([scope,target.tab,target.speaker,target.event]),release=visualState.graph?.releases?.at(-1),releaseKey=release?visualReleaseKey(release):null,seen=visualActionFollow.releases.has(scope),previous=visualActionFollow.releases.get(scope);
  visualActionFollow.releases.set(scope,releaseKey);if(visualActionFollow.releases.size>60)visualActionFollow.releases.delete(visualActionFollow.releases.keys().next().value);
  if(seen&&releaseKey&&previous!==releaseKey){
   visualActionFollow.actionKey=key;clearVisualActionTarget();setVisualInspector('hints');
   const detail=[...$('visual-assistance').querySelectorAll('[data-release-key]')].find(item=>item.dataset.releaseKey===releaseKey);
   if(detail){detail.open=true;const holder=$('visual-pane-hints');holder.scrollTop=Math.max(0,holder.scrollTop+detail.getBoundingClientRect().top-holder.getBoundingClientRect().top-8);}
   return;
  }
  if(visualActionFollow.actionKey===key)return;
  visualActionFollow.actionKey=key;clearVisualActionTarget();setVisualInspector(target.tab);
  if(target.speaker){setVisualEditorCollapsed(target.speaker,false);const panel=$('visual-'+target.speaker+'-panel'),grid=panel.parentElement;panel.classList.add('is-follow-target');grid.scrollTop=panel.offsetTop-grid.offsetTop;}
 }finally{visualActionFollow.applying=false;}
}
