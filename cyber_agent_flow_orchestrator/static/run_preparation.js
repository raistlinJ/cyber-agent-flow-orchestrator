/* Preparation has an independent canvas and view. */
const prepColors={passed:'#19856b',failed:'#ba4848',running:'#3179d3',partial:'#d58b1f',warning:'#d58b1f',pending:'#89969c',skipped:'#89969c'};
const prepNames={passed:'Passed',failed:'Failed',running:'Checking',partial:'In progress',warning:'Warning',pending:'Pending',skipped:'Skipped'};
const prepSymbols={passed:'✓',failed:'×',running:'↻',partial:'◐',warning:'!',pending:'–',skipped:'–'};
function preparationOf(run){return run?.workflow_progress?.preparation||{nodes:[],edges:[]};}
function preparationItem(){const selection=visualState.prep;if(!selection)return null;return selection.kind==='edge'?visualState.preparation.edges.find(item=>item.id===selection.id):visualState.preparation.nodes.find(item=>item.id===selection.id);}
function selectPreparation(kind,id){if(visualState.moved)return;visualState.flowSelection=false;visualState.activitySelection=null;visualState.prep={kind,id};updateVisualSelection();renderVisualNode();setVisualInspector('details');}
function updateVisualSelection(){syncActivityFlow();for(const node of $('attack-graph').querySelectorAll('.attack-node'))node.setAttribute('aria-pressed',String(!visualState.prep&&!visualState.activitySelection&&!visualState.flowSelection&&node.dataset.nodeId===visualState.node));for(const item of $('preparation-graph').querySelectorAll('[data-prep-id]'))item.setAttribute('aria-pressed',String(!visualState.flowSelection&&visualState.prep?.kind===item.dataset.prepKind&&visualState.prep?.id===item.dataset.prepId));}
function prepSelectable(node,kind,item){node.setAttribute('role','button');node.setAttribute('tabindex','0');node.dataset.prepKind=kind;node.dataset.prepId=item.id;node.setAttribute('aria-label',(item.title||item.label)+' · '+prepNames[item.status]);node.addEventListener('click',()=>selectPreparation(kind,item.id));node.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();visualState.moved=false;selectPreparation(kind,item.id);}});}
function drawPreparationCanvas(){
 const svg=$('preparation-graph'),preparation=visualState.preparation,positions=new Map(preparation.nodes.map((node,index)=>[node.id,{x:40,y:35+index*185,w:250,h:110}]));
 const layout={width:360,height:Math.max(240,...[...positions.values()].map(p=>p.y+p.h+45))};visualState.prepLayout=layout;
 svg.replaceChildren(svgElement('title',{},'Lab preparation checks and connections'));
 if(!preparation.nodes.length){svg.append(svgElement('text',{x:40,y:90,class:'visual-lane-note'},'Waiting for saved lab preparation checks.'));fitPreparationGraph(false);return;}
 const group=svgElement('g',{class:'preparation-lane','aria-label':'Lab preparation and connections'}),defs=svgElement('defs');
 for(const [status,color] of Object.entries(prepColors)){const marker=svgElement('marker',{id:'prep-arrow-'+status,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'});marker.append(svgElement('path',{d:'M0 0 L10 5 L0 10 z',fill:color}));defs.append(marker);}group.append(defs);
 for(const edge of preparation.edges){const a=positions.get(edge.source),b=positions.get(edge.target);if(!a||!b)continue;const hitl=edge.interface_only,down=b.y>a.y,start=[a.x+a.w/2,down?a.y+a.h:a.y],end=[b.x+b.w/2,down?b.y-8:b.y+b.h+8],mid=(a.y+a.h+b.y)/2;
  const path=hitl?`M${a.x+a.w} ${a.y+a.h/2} H330 V${b.y+b.h/2} H${b.x+b.w+8}`:`M${start} L${end}`;
  const labelX=hitl?342:start[0],y=hitl?(a.y+b.y+a.h)/2:mid;
  const g=svgElement('g',{class:'preparation-edge '+edge.status});g.append(svgElement('title',{},edge.label+' — '+prepNames[edge.status]+'. '+edge.description),svgElement('path',{d:path,fill:'none',stroke:prepColors[edge.status],'stroke-width':2.5,'stroke-dasharray':hitl||edge.status==='pending'?'5 4':'none','marker-end':'url(#prep-arrow-'+edge.status+')'}),svgElement('path',{d:path,fill:'none',stroke:'transparent','stroke-width':16,class:'prep-edge-hit'}),svgElement('text',{x:labelX,y,'text-anchor':'middle',transform:hitl?`rotate(-90 ${labelX} ${y})`:'',class:'prep-edge-label'},prepSymbols[edge.status]+' '+edge.label));prepSelectable(g,'edge',edge);group.append(g);
 }
 for(const node of preparation.nodes){const p=positions.get(node.id),g=svgElement('g',{transform:`translate(${p.x},${p.y})`,class:'preparation-node '+node.status});
  g.append(svgElement('rect',{x:-6,y:-6,width:p.w+12,height:p.h+12,rx:17,class:'live-node-halo'}),svgElement('title',{},node.title+' · '+prepNames[node.status]+' · '+node.passed+' / '+node.total+' checks passed'),svgElement('rect',{width:p.w,height:p.h,rx:13,class:'prep-node-surface'}),svgElement('rect',{x:0,y:18,width:5,height:p.h-36,rx:2,fill:prepColors[node.status]}));
  g.append(svgElement('circle',{cx:22,cy:23,r:10,fill:prepColors[node.status]}),svgElement('text',{x:22,y:27,'text-anchor':'middle',class:'attack-node-symbol'},prepSymbols[node.status]),svgElement('text',{x:40,y:27,class:'attack-node-state'},prepNames[node.status].toUpperCase()),svgElement('text',{x:17,y:51,class:'prep-node-title'},node.title),svgElement('text',{x:17,y:69,class:'attack-node-meta'},node.description),svgElement('text',{x:p.w-16,y:27,'text-anchor':'end',class:'entity-working-label'},'WORKING'));
  g.append(svgElement('text',{x:17,y:94,class:'attack-node-meta'},node.passed+' / '+node.total+' checks passed · select for details'));prepSelectable(g,'node',node);group.append(g);
 }
 svg.append(group);updateVisualSelection();fitPreparationGraph(false);
}
function applyPreparationView(){const v=visualState.prepView;if(!v)return;$('preparation-graph').setAttribute('viewBox',`${v.x} ${v.y} ${v.w} ${v.h}`);$('prep-zoom-label').textContent=Math.round($('preparation-graph').clientWidth/v.w*100)+'%';}
function fitPreparationGraph(force){
 const svg=$('preparation-graph'),bounds=svg.getBoundingClientRect(),layout=visualState.prepLayout;if(!bounds.width||!bounds.height||!layout)return;
 if(force||!visualState.prepView||visualState.prepView.autoFit){const ratio=bounds.width/bounds.height,w=Math.max(layout.width,layout.height*ratio)*1.06,h=w/ratio;visualState.prepView={x:(layout.width-w)/2,y:-16,w,h,autoFit:true};}
 else{const v=visualState.prepView,h=v.w*bounds.height/bounds.width;v.y+=(v.h-h)/2;v.h=h;}applyPreparationView();
}
function zoomPreparationGraph(factor){const v=visualState.prepView,svg=$('preparation-graph');if(!v||!svg.clientWidth)return;const w=Math.max(250,Math.min(12000,v.w*factor)),h=w*svg.clientHeight/svg.clientWidth;visualState.prepView={x:v.x+(v.w-w)/2,y:v.y+(v.h-h)/2,w,h};applyPreparationView();}
function focusPreparationLane(){fitPreparationGraph(true);}

function renderPreparationDetail(){
 const item=preparationItem(),target=$('visual-node-detail'),selection=visualState.prep?.kind+'|'+item?.id,closed=target.dataset.prepSelection===selection?new Set([...target.querySelectorAll('details.prep-check-detail:not([open])')].map(row=>row.dataset.checkId)):new Set();target.dataset.prepSelection=selection;target.replaceChildren();if(!item)return;
 target.append(el('span',prepNames[item.status],'visual-state-chip prep-'+item.status),el('h3',item.title||item.label),el('p',item.description,'small'));
 if(item.vmid!=null)target.append(el('p','Guest reference: '+item.vmid,'small'));
 const checks=item.checks||[],passed=checks.filter(check=>check.status==='passed').length;target.append(el('p',passed+' / '+checks.length+' checks passed','visual-node-extent'));
 for(const check of checks){const row=el('details',null,'prep-check-detail'),summary=el('summary'),heading=el('h4'),active=el('span','ACTIVE','prep-check-active-marker');row.dataset.checkId=check.id;row.open=!closed.has(check.id);active.hidden=true;active.setAttribute('aria-hidden','true');heading.append(el('span',prepSymbols[check.status]+' ', 'prep-status-'+check.status),document.createTextNode(check.label),active);summary.append(heading);row.append(summary,el('p',prepNames[check.status]+(check.seconds==null?'':' · '+duration(check.seconds)),'small'),el('p',check.message));
  if(check.at)row.append(el('p','Recorded '+new Date(check.at).toLocaleString(),'small'));
  const phase=phasePages(visualState.run).findIndex(page=>page.id===check.phase),reached=lastReachedPhaseIndex(visualState.run);
  if(phase>0&&phase<=reached){const button=el('button','Open phase details');button.type='button';button.addEventListener('click',()=>{setProgressDisplay('phases');setProgressPhase(check.phase);});row.append(button);}target.append(row);
 }
 target.append(el('p','These are saved preparation checks. They do not mark any challenge solved or start a new VM probe.','small'));syncPreparationChecks();
}

function syncPreparationChecks(){
 const fresh=visualRunLive()&&performance.now()-visualState.receivedAt<45000,checks=new Map((preparationItem()?.checks||[]).map(check=>[check.id,check]));
 for(const row of $('visual-node-detail').querySelectorAll('.prep-check-detail')){const active=Boolean(fresh&&checks.get(row.dataset.checkId)?.status==='running');row.classList.toggle('is-active',active);row.setAttribute('aria-busy',String(active));row.querySelector('.prep-check-active-marker').hidden=!active;}
}
