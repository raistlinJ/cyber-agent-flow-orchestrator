const $=id=>document.getElementById(id),params=new URLSearchParams(location.search),selection=params.get('selection'),kind=params.get('kind');
const titles={'attack-graph':'Attack graph','participant-guide':'Participant guide','facilitator-guide':'Facilitator guide'};
const links=[];
function markdownGuide(text){
 const root=document.createElement('article');root.className='markdown-guide';let list=null,code=null;
 const add=(tag,value,parent=root)=>{const node=document.createElement(tag);node.textContent=value;parent.append(node);return node;};
 for(const line of text.split(/\r?\n/)){
  if(/^\s*```/.test(line)){if(code)code=null;else code=add('pre','');list=null;continue;}
  if(code){code.textContent+=line+'\n';continue;}
  const heading=line.match(/^(#{1,6})\s+(.+)$/),item=line.match(/^\s*(?:[-*+]\s+|\d+\.\s+)(.+)$/);
  if(heading){list=null;add('h'+heading[1].length,heading[2]);}
  else if(item){if(!list)list=add(/^\s*\d+\./.test(line)?'ol':'ul','');add('li',item[1],list);}
  else{list=null;if(line.trim())add('p',line);}
 }
 $('reference-content').append(root);
}
function download(label,name,content,mime){const url=URL.createObjectURL(new Blob([content],{type:mime}));links.push(url);const a=document.createElement('a');a.href=url;a.download=name;a.textContent=label;$('reference-downloads').append(a);}
function graphView(graph){
 const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg'),nodes=Array.isArray(graph.nodes)?graph.nodes:[],positions=new Map();
 svg.setAttribute('viewBox',`0 0 1000 ${Math.max(200,nodes.length*130)}`);svg.setAttribute('role','img');svg.setAttribute('aria-label','ScenarioForge attack graph');svg.classList.add('scenario-graph');
 const make=(tag,attrs,text)=>{const n=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);if(text!=null)n.textContent=text;return n;};
 const defs=make('defs',{}),marker=make('marker',{id:'arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto'});marker.append(make('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#70cbb3'}));defs.append(marker);svg.append(defs);
 nodes.forEach((node,index)=>positions.set(String(node.id),{x:140,y:30+index*130}));
 for(const edge of graph.edges||[]){const a=positions.get(String(edge.source)),b=positions.get(String(edge.target));if(a&&b)svg.append(make('path',{d:`M ${a.x+330} ${a.y+70} L ${b.x+330} ${b.y}`,fill:'none',stroke:'#70cbb3','stroke-width':2,'marker-end':'url(#arrow)'}));}
 nodes.forEach((node,index)=>{const p=positions.get(String(node.id)),g=make('g',{});g.append(make('rect',{x:p.x,y:p.y,width:660,height:70,rx:10,fill:'#142329',stroke:'#70cbb3'}),make('text',{x:p.x+15,y:p.y+27,fill:'#eef5f6','font-size':17},`${index+1}. ${node.name||node.label||node.id}`),make('text',{x:p.x+15,y:p.y+51,fill:'#bacbd0','font-size':13},`Node ${node.id}${node.ipv4?' · '+node.ipv4:''}`));svg.append(g);});
 $('reference-content').append(svg);
 const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='Graph nodes, dependencies and required facts';pre.textContent=JSON.stringify(graph,null,2);details.append(summary,pre);$('reference-content').append(details);
}
async function loadReference(){
 $('reload-reference').disabled=true;$('reference-error').hidden=true;$('reference-content').replaceChildren();$('reference-downloads').replaceChildren();for(const url of links.splice(0))URL.revokeObjectURL(url);
 loadingModal.set('reference',true,'Loading '+titles[kind].toLowerCase()+' · reusing saved documents and persistent cache…');
 try{
  const sessionResponse=await fetch('/api/session',{cache:'no-store'});if(!sessionResponse.ok)throw Error('Sign in through the main dashboard, then reload this reference.');const session=await dashboardJSON(sessionResponse);
  const response=await fetch('/api/scenarios/references',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf},body:JSON.stringify({selection_id:selection,kind})});const data=await dashboardJSON(response);if(!response.ok)throw Error(data.error||'Unable to load the scenario reference');
  $('reference-title').textContent=titles[kind]+' · '+data.scenario;$('reference-source').textContent=data.reference_file||data.source;$('reference-status').textContent=(data.reference_origin==='uploaded-bundle'?'Loaded an existing document from the uploaded bundle. ':data.reference_origin==='saved-export'?'Loaded a saved ScenarioForge export. ':'Loaded the persistent reference cache. ')+(kind==='facilitator-guide'?'This guide includes solutions.':'');
  if(kind==='attack-graph'){graphView(data.graph);download('Download graph JSON','attack-graph.json',JSON.stringify(data.graph,null,2),'application/json');if(data.dot)download('Download Graphviz DOT','attack-graph.dot',data.dot,'text/vnd.graphviz');}
  else if(data.markdown!=null){markdownGuide(data.markdown);download('Download guide Markdown',kind+'.md',data.markdown,'text/markdown');}
  else{const frame=document.createElement('iframe');frame.title=titles[kind];frame.className='scenario-guide';frame.setAttribute('sandbox','');frame.srcdoc=data.html.replace(/<head[^>]*>/i,'$&<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; img-src data:;">');$('reference-content').append(frame);download('Download guide HTML',kind+'.html',data.html,'text/html');}
 }catch(error){$('reference-error').textContent=error.message;$('reference-error').hidden=false;$('reference-status').textContent='Reference unavailable. Your experiment draft is unchanged.';}
 finally{loadingModal.set('reference',false);$('reload-reference').disabled=false;}
}
$('close-reference').addEventListener('click',()=>window.close());$('reload-reference').addEventListener('click',loadReference);
if(!/^[0-9a-f]{64}$/.test(selection||'')||!titles[kind]){$('reference-error').hidden=false;$('reference-error').textContent='Choose a scenario and reference from the New experiment modal.';$('reload-reference').disabled=true;}else loadReference();
