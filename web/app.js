const $ = id => document.getElementById(id);
let report, analyzedPayload, selectedRow, pendingPlan, pendingRollback=false;
$('demo').onclick = () => {
  for(const id of ['src','dst']){if(!$(id).value){const option=new Option(id==='src'?'example-source':'example-target',id==='src'?'example-source':'example-target');$(id).append(option);$(id).value=option.value;}}
  setNamespaces(['demo'],['demo']); $('old').value = 'old.example.com'; $('new').value = 'new.example.com';
  const resource = {apiVersion:'apps/v1',kind:'Deployment',metadata:{name:'checkout',namespace:'demo'},spec:{replicas:2,template:{metadata:{labels:{app:'checkout'}},spec:{containers:[{name:'app',image:'old.example.com/checkout:v1'}]}}}};
  $('source').value = JSON.stringify([resource],null,2);
  const target = JSON.parse(JSON.stringify(resource)); target.spec.template.spec.containers[0].image='new.example.com/checkout:v1'; target.spec.replicas=1;
  $('target').value = JSON.stringify([target],null,2);
};
$('planner').onsubmit = async event => {
  event.preventDefault(); $('error').textContent=''; $('results').hidden=true; report=null; $('submit').disabled=true;
  try {
    if($('src').value.trim()===$('dst').value.trim()) throw Error('Choose distinct source and target labels.');
    if(Boolean($('old').value.trim())!==Boolean($('new').value.trim())) throw Error('Provide both registry addresses.');
    const live=$('mode').value==='live';
    if(live && (!loadedContexts.includes($('src').value)||!loadedContexts.includes($('dst').value))) throw Error('Load cluster contexts and select both actual context names.');
    const payload={source:live?[]:JSON.parse($('source').value),target:live?[]:JSON.parse($('target').value),source_context:$('src').value.trim(),target_context:$('dst').value.trim(),namespaces:[...selectedNamespaces].sort(),old_registry:$('old').value.trim(),new_registry:$('new').value.trim()};
    if(!payload.namespaces.length)throw Error('Select at least one namespace.');
    if(payload.namespaces.length>200)throw Error('Select no more than 200 namespaces per analysis.');
    const data=live?await analyzeWithProgress(payload):await clusterRequest('/api/analyze',payload);
    analyzedPayload=live?structuredClone(payload):null;
    report={...data,source_context:$('src').value,target_context:$('dst').value};
    $('report-notice').textContent=data.notice+(data.unavailable_kinds?' Unavailable API kinds: source: '+data.unavailable_kinds.source.join(', ')+ '; target: '+data.unavailable_kinds.target.join(', '):'');
    $('summary').replaceChildren(); $('rows').replaceChildren(); $('skipped').replaceChildren(); $('versus').hidden=true;
    for(const [status,count] of Object.entries(data.summary)){const box=document.createElement('div');box.className='metric';const n=document.createElement('strong');n.textContent=count;box.append(n,document.createTextNode(status.replaceAll('_',' ')));$('summary').append(box);}
    populateFilters(); renderResources();
    $('skip-count').textContent=`${data.skipped.length} excluded resources`;
    for(const entry of data.skipped){const li=document.createElement('li');li.textContent=`${entry.side}: ${entry.kind}/${entry.name} — ${entry.reason}`;$('skipped').append(li);}
    $('results').hidden=false;
  }catch(error){$('error').textContent=error.message;}finally{$('submit').disabled=false;}
};
$('download').onclick=()=>{if(!report)return;const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='migration-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};

$('mode').onchange=()=>{const live=$('mode').value==='live';$('json-inputs').hidden=live;for(const id of ['source','target'])$(id).required=!live;$('results').hidden=true;setNamespaces([]);$('namespace-from-json').hidden=live;if(!live)loadOfflineNamespaces();};
async function clusterRequest(path,payload,timeout){const options=payload?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{};if(timeout)options.signal=AbortSignal.timeout(timeout);const response=await fetch(path,options);const data=await response.json();if(!response.ok)throw Error(data.error);return data;}
let loadedContexts=[];
$('connect').onclick=async()=>{ $('connect').disabled=true;const previousSource=$('src').value;try{const data=await clusterRequest('/api/contexts');loadedContexts=data.contexts;
for(const id of ['src','dst']){const previous=$(id).value;$(id).replaceChildren(new Option(id==='src'?'Select source context':'Select target context',''));for(const name of loadedContexts)$(id).append(new Option(name,name));if(loadedContexts.includes(previous))$(id).value=previous;else if(id==='src'&&loadedContexts.includes('aks-dev'))$(id).value='aks-dev';}
if($('src').value!==previousSource)setNamespaces([]);
$('connection').textContent=loadedContexts.length+' contexts loaded. Select the exact source and target names. A cluster name may differ from its kubeconfig context name.';
}catch(error){$('connection').textContent=error.message;}finally{$('connect').disabled=false;}};
$('load-namespaces').onclick=async()=>{const context=$('src').value.trim();$('load-namespaces').disabled=true;try{const data=await clusterRequest('/api/namespaces',{context});if($('src').value!==context||$('mode').value!=='live')return;setNamespaces(data.namespaces);$('namespace-options').textContent=data.namespaces.length+' source namespaces available.';$('connection').textContent='Source cluster reachable. Choose namespaces from Migration scope.';}catch(error){$('connection').textContent=error.message;}finally{$('load-namespaces').disabled=false;}};


function flat(value,path='',out={}){if(value!==null && typeof value==='object'){const entries=Object.entries(value);if(!entries.length)out[path]=JSON.stringify(value);for(const [key,v] of entries)flat(v,path+'/'+key.replaceAll('~','~0').replaceAll('/','~1'),out);}else out[path]=JSON.stringify(value);return out;}
function showVersus(row){selectedRow=row;$('versus').hidden=false;$('versus-title').textContent=row.kind+' / '+row.namespace+' / '+row.name;$('sync-status').textContent='';$('sync').hidden=!canSync(row);$('diff-paths').textContent='Changed fields: '+(row.diff_paths.join(', ')||'None');
const a=row.manifest?flat(row.manifest):{}, b=row.target_manifest?flat(row.target_manifest):{};const paths=[...new Set([...Object.keys(a),...Object.keys(b)])].sort();
for(const [id,values] of [['source-preview',a],['target-preview',b]]){$(id).replaceChildren();if(!paths.length)$(id).textContent='Resource absent';for(const path of paths){const line=document.createElement('div');line.textContent=path+' = '+(values[path]??'— absent');if(a[path]!==b[path]||row.diff_paths.some(p=>path===p||path.startsWith(p+'/')))line.className='diff-line';$(id).append(line);}}
}
async function prepareSync(){if(!selectedRow||!canSync(selectedRow))return;$('sync').disabled=true;$('sync-status').textContent='Reading current resource and validating server-side dry-run…';try{pendingRollback=false;pendingPlan=await clusterRequest('/api/sync/plan',{...analyzedPayload,namespace:selectedRow.namespace,name:selectedRow.name,kind:selectedRow.kind,apiVersion:selectedRow.apiVersion,mode:$('sync-mode').value});$('sync-confirm').textContent='Confirm '+pendingPlan.mode; $('sync-warnings').textContent=(pendingPlan.warnings||[]).join(' ');$('sync-description').textContent=pendingPlan.mode.toUpperCase()+' · '+pendingPlan.operation.toUpperCase()+' '+selectedRow.kind+'/'+selectedRow.name+' → '+pendingPlan.target_context+' / '+selectedRow.namespace+' (preview expires in 5 minutes)';$('sync-manifest').textContent=JSON.stringify(pendingPlan.manifest,null,2);$('sync-dialog').showModal();$('sync-status').textContent='Server-side validation passed. Review before confirming.';}catch(error){$('sync-status').textContent=error.message;}finally{$('sync').disabled=false;}}
$('sync').onclick=prepareSync;
$('sync-cancel').onclick=()=>{pendingPlan=null;$('sync-dialog').close();};
$('sync-confirm').onclick=async()=>{
 if(!pendingPlan)return;$('sync-confirm').disabled=true;$('sync-cancel').disabled=true;
 try{const result=await clusterRequest(pendingRollback?'/api/rollback/execute':'/api/sync/execute',{token:pendingPlan.token,confirm:true});pendingPlan=null;$('sync-dialog').close();$('sync-status').textContent=result.status+'. Re-analyze to refresh the comparison.';await loadHistory();if(result.status==='Applied')trackReadiness(result.operation_id);}
 catch(error){$('sync-dialog').close();pendingPlan=null;$('sync-status').textContent=error.message;await loadHistory();}
 finally{$('sync-confirm').disabled=false;$('sync-cancel').disabled=false;}
};

for(const [id,label] of [['src','Offline source'],['dst','Offline target']]){$(id).append(new Option(label,label));$(id).value=label;}

async function analyzeWithProgress(payload){
  $('analysis-progress').hidden=false;
  $('progress-title').textContent='Analyzing clusters…';
  $('progress-current').textContent='Submitting analysis';
  $('progress-stats').textContent='';$('progress-wait').textContent='';$('progress-log').textContent='';
  let lastStatus=null,receivedAt=0;
  const paint=()=>{if(!lastStatus)return;const extra=lastStatus.state==='running'?(Date.now()-receivedAt)/1000:0;const elapsed=lastStatus.elapsed_seconds+extra,step=lastStatus.step_seconds+extra;
    $('progress-current').textContent=lastStatus.current;
    $('progress-stats').textContent=`Elapsed: ${Math.floor(elapsed)}s · Current step: ${Math.floor(step)}s · Completed kubectl requests: ${lastStatus.completed_commands}`;
    $('progress-wait').textContent=lastStatus.state==='running'&&lastStatus.phase==='waiting'?'Waiting for kubectl/API or authentication response. Each API request timeout: 15s. Resource kinds are read in a namespace batch; the whole batch has a timeout of 25s per kind.':lastStatus.phase==='comparing'?'Cluster reads finished; comparing manifests.':'';
    if(lastStatus.state==='running'&&extra>5)$('progress-wait').textContent+=' Progress updates delayed; checking the local application connection.';
  };
  const timer=setInterval(paint,500);
  try{
    const job=await clusterRequest('/api/live/jobs',payload,10000);
    while(true){lastStatus=await clusterRequest('/api/jobs/'+encodeURIComponent(job.job_id),null,5000);receivedAt=Date.now();paint();
      $('progress-log').textContent=lastStatus.events.map(e=>`[${e.elapsed.toFixed(1)}s] ${e.message}`).join('\n');$('progress-log').scrollTop=$('progress-log').scrollHeight;
      if(lastStatus.state==='completed'){$('progress-title').textContent='Analysis complete';$('progress-wait').textContent='';return lastStatus.result;}
      if(lastStatus.state==='failed')throw Error(lastStatus.error);
      await new Promise(resolve=>setTimeout(resolve,750));
    }
  }catch(error){$('progress-title').textContent=lastStatus?.state==='failed'?'Analysis failed':'Application connection interrupted';$('progress-wait').textContent=lastStatus?.state==='failed'?'Analysis stopped at the step shown above.':'The server analysis may still be running; check the local application connection.';throw error;}finally{clearInterval(timer);}
}

function populateFilters(){
  for(const [id,key,label] of [['filter-namespace','namespace','All namespaces'],['filter-kind','kind','All kinds'],['filter-status','status','All statuses']]){
    $(id).replaceChildren(new Option(label,''));for(const value of [...new Set(report.resources.map(row=>row[key]))].sort())$(id).append(new Option(value||'(cluster scope)',value));
  }
}
function filteredResources(){return report.resources.filter(row=>[['filter-namespace','namespace'],['filter-kind','kind'],['filter-status','status']].every(([id,key])=>!$(id).value||row[key]===$(id).value));}
function renderResources(){
  if(!report)return;
  const rows=filteredResources();$('rows').replaceChildren();$('versus').hidden=true;
  $('filtered-count').textContent=`Showing ${rows.length} of ${report.resources.length} resources · summary counts cover all results`;
  const fragment=document.createDocumentFragment();
  for(const row of rows){const tr=document.createElement('tr');for(const key of ['status','kind','namespace','name']){const td=document.createElement('td');if(key==='name'){const button=document.createElement('button');button.type='button';button.textContent=row.name;button.onclick=()=>showVersus(row);td.append(button);if(canSync(row)){const sync=document.createElement('button');sync.textContent='Sync to target';sync.onclick=()=>{showVersus(row);prepareSync();};td.append(sync);}}else td.textContent=row[key];tr.append(td);}fragment.append(tr);}
  if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=4;td.textContent='No resources match these filters.';tr.append(td);fragment.append(tr);}
  $('rows').append(fragment);
}
for(const id of ['filter-namespace','filter-kind','filter-status'])$(id).onchange=renderResources;
$('clear-filters').onclick=()=>{for(const id of ['filter-namespace','filter-kind','filter-status'])$(id).value='';renderResources();};

async function loadHistory(){
 try{const data=await clusterRequest('/api/history');$('history-rows').replaceChildren();for(const op of data.operations){const tr=document.createElement('tr');for(const text of [new Date(op.updated_at).toLocaleString()+' / '+op.mode,op.target+' / '+op.namespace+' / '+op.kind+'/'+op.name,op.status+(op.health?' · '+op.health.state+' — '+op.health.detail:'')]){const td=document.createElement('td');td.textContent=text;tr.append(td);}const actions=document.createElement('td');if(op.applied_uid&&op.status!=='RolledBack'){const health=document.createElement('button');health.textContent='Check readiness';health.onclick=async()=>{health.disabled=true;try{const result=await clusterRequest('/api/operations/health',{operation_id:op.id});$('history-status').textContent=result.state+' — '+result.detail;await loadHistory();}catch(error){$('history-status').textContent=error.message;}finally{health.disabled=false;}};actions.append(health);}if(op.rollback_available){const rollback=document.createElement('button');rollback.textContent='Preview rollback';rollback.onclick=()=>prepareRollback(op.id);actions.append(rollback);}tr.append(actions);$('history-rows').append(tr);}$('history-status').textContent=data.operations.length+' recent operations loaded.';}catch(error){$('history-status').textContent=error.message;}
}
$('load-history').onclick=loadHistory;
async function prepareRollback(id){try{pendingPlan=await clusterRequest('/api/rollback/plan',{operation_id:id});pendingRollback=true;$('sync-confirm').textContent='Confirm rollback';$('sync-description').textContent=pendingPlan.operation+' '+pendingPlan.kind+'/'+pendingPlan.name+' → '+pendingPlan.target_context+' / '+pendingPlan.namespace;$('sync-manifest').textContent=pendingPlan.manifest?JSON.stringify(pendingPlan.manifest,null,2):'Delete the resource created by this sync (UID and resourceVersion preconditions).';$('sync-warnings').textContent=pendingPlan.warning;$('sync-dialog').showModal();}catch(error){$('history-status').textContent=error.message;}}
async function trackReadiness(id){for(let attempt=0;attempt<12;attempt++){try{const result=await clusterRequest('/api/operations/health',{operation_id:id});$('sync-status').textContent=result.state+' — '+result.detail;await loadHistory();if(result.state!=='RollingOut')return;}catch(error){$('sync-status').textContent='Applied; readiness check failed: '+error.message;return;}await new Promise(resolve=>setTimeout(resolve,5000));}$('sync-status').textContent='Still rolling out. Use Check readiness in operation history to continue monitoring.';}

function canSync(row){return Boolean(analyzedPayload && row.manifest && row.kind!=='Namespace' && ['DIFF','SOURCE_ONLY'].includes(row.status));}

let availableNamespaces=[], selectedNamespaces=new Set();
function setNamespaces(names,selection){
  availableNamespaces=[...new Set(names)].sort();
  selectedNamespaces=new Set((selection??[...selectedNamespaces]).filter(name=>availableNamespaces.includes(name)));
  $('namespace-search').value='';renderNamespaces();
}
function renderNamespaces(){
  const search=$('namespace-search').value.trim().toLowerCase();
  const visible=availableNamespaces.filter(name=>name.toLowerCase().includes(search));
  $('namespace-list').replaceChildren();
  for(const name of visible){const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.value=name;check.checked=selectedNamespaces.has(name);check.onchange=()=>{if(check.checked)selectedNamespaces.add(name);else selectedNamespaces.delete(name);updateNamespaceCount();};label.append(check,document.createTextNode(name));$('namespace-list').append(label);}
  $('namespace-empty').hidden=visible.length>0;$('namespace-empty').textContent=availableNamespaces.length?'No namespaces match your search.':$('mode').value==='live'?'Load source namespaces to choose.':'Load example or load namespaces from your JSON manifests.';
  updateNamespaceCount();
}
function updateNamespaceCount(){
  const count=selectedNamespaces.size;
  $('namespace-summary-text').textContent=count?`${count} namespace${count===1?'':'s'} selected`:'Select namespaces';
  $('namespace-count').textContent=`${count} of ${availableNamespaces.length} selected · maximum 200`;
  $('namespace-all').checked=availableNamespaces.length>0&&count===availableNamespaces.length;
  $('namespace-all').indeterminate=count>0&&count<availableNamespaces.length;
  $('namespace-all').disabled=!availableNamespaces.length;
}
$('namespace-search').oninput=renderNamespaces;
$('namespace-all').onchange=()=>{selectedNamespaces=new Set($('namespace-all').checked?availableNamespaces:[]);renderNamespaces();};
$('namespace-clear').onclick=()=>{selectedNamespaces.clear();renderNamespaces();};
function loadOfflineNamespaces(){
 try{const names=[];for(const id of ['source','target']){if(!$(id).value.trim())continue;const data=JSON.parse($(id).value);const objects=Array.isArray(data)?data:data.kind==='List'?data.items:[data];for(const obj of objects){const name=obj.metadata?.namespace||(obj.kind==='Namespace'?obj.metadata?.name:null);if(name&&!['elastic-agent','kube-system','kube-public','kube-node-lease'].includes(name))names.push(name);}}setNamespaces(names);$('error').textContent='';}catch(error){$('error').textContent='Provide valid source and target JSON before loading namespaces.';}
}
$('namespace-from-json').onclick=loadOfflineNamespaces;
for(const id of ['source','target'])$(id).addEventListener('change',()=>{if($('mode').value==='offline')loadOfflineNamespaces();});
$('src').addEventListener('change',()=>{if($('mode').value==='live'){setNamespaces([]);$('namespace-options').textContent='Source changed. Load source namespaces again.';}});
document.addEventListener('click',event=>{if(!$('namespace-dropdown').contains(event.target))$('namespace-dropdown').open=false;});
$('namespace-dropdown').addEventListener('keydown',event=>{if(event.key==='Escape'){$('namespace-dropdown').open=false;$('namespace-summary').focus();}});
renderNamespaces();
