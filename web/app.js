const $ = id => document.getElementById(id);
let report, analyzedPayload, selectedRow, pendingPlan;
$('demo').onclick = () => {
  for(const id of ['src','dst']){if(!$(id).value){const option=new Option(id==='src'?'example-source':'example-target',id==='src'?'example-source':'example-target');$(id).append(option);$(id).value=option.value;}}
  $('namespaces').value = 'demo'; $('old').value = 'old.example.com'; $('new').value = 'new.example.com';
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
    const payload={source:live?[]:JSON.parse($('source').value),target:live?[]:JSON.parse($('target').value),source_context:$('src').value.trim(),target_context:$('dst').value.trim(),namespaces:$('namespaces').value.split(',').map(s=>s.trim()).filter(Boolean),old_registry:$('old').value.trim(),new_registry:$('new').value.trim()};
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

$('mode').onchange=()=>{const live=$('mode').value==='live';$('json-inputs').hidden=live;for(const id of ['source','target'])$(id).required=!live;$('results').hidden=true;};
async function clusterRequest(path,payload,timeout){const options=payload?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{};if(timeout)options.signal=AbortSignal.timeout(timeout);const response=await fetch(path,options);const data=await response.json();if(!response.ok)throw Error(data.error);return data;}
let loadedContexts=[];
$('connect').onclick=async()=>{ $('connect').disabled=true;try{const data=await clusterRequest('/api/contexts');loadedContexts=data.contexts;
for(const id of ['src','dst']){const previous=$(id).value;$(id).replaceChildren(new Option(id==='src'?'Select source context':'Select target context',''));for(const name of loadedContexts)$(id).append(new Option(name,name));if(loadedContexts.includes(previous))$(id).value=previous;else if(id==='src'&&loadedContexts.includes('aks-dev'))$(id).value='aks-dev';}
$('connection').textContent=loadedContexts.length+' contexts loaded. Select the exact source and target names. A cluster name may differ from its kubeconfig context name.';
}catch(error){$('connection').textContent=error.message;}finally{$('connect').disabled=false;}};
$('load-namespaces').onclick=async()=>{$('load-namespaces').disabled=true;try{const data=await clusterRequest('/api/namespaces',{context:$('src').value.trim()});$('namespace-options').textContent='Available source namespaces: '+data.namespaces.join(', ');$('connection').textContent='Source cluster reachable. Enter the namespaces to compare.';}catch(error){$('connection').textContent=error.message;}finally{$('load-namespaces').disabled=false;}};

function flat(value,path='',out={}){if(value!==null && typeof value==='object'){const entries=Object.entries(value);if(!entries.length)out[path]=JSON.stringify(value);for(const [key,v] of entries)flat(v,path+'/'+key.replaceAll('~','~0').replaceAll('/','~1'),out);}else out[path]=JSON.stringify(value);return out;}
function showVersus(row){selectedRow=row;$('versus').hidden=false;$('versus-title').textContent=row.kind+' / '+row.namespace+' / '+row.name;$('sync-status').textContent='';$('sync').hidden=!analyzedPayload||!row.manifest||row.kind==='Namespace';$('diff-paths').textContent='Changed fields: '+(row.diff_paths.join(', ')||'None');
const a=row.manifest?flat(row.manifest):{}, b=row.target_manifest?flat(row.target_manifest):{};const paths=[...new Set([...Object.keys(a),...Object.keys(b)])].sort();
for(const [id,values] of [['source-preview',a],['target-preview',b]]){$(id).replaceChildren();if(!paths.length)$(id).textContent='Resource absent';for(const path of paths){const line=document.createElement('div');line.textContent=path+' = '+(values[path]??'— absent');if(a[path]!==b[path]||row.diff_paths.some(p=>path===p||path.startsWith(p+'/')))line.className='diff-line';$(id).append(line);}}
}
async function prepareSync(){if(!analyzedPayload||!selectedRow)return;$('sync').disabled=true;$('sync-status').textContent='Reading current resource and validating server-side dry-run…';try{pendingPlan=await clusterRequest('/api/sync/plan',{...analyzedPayload,namespace:selectedRow.namespace,name:selectedRow.name,kind:selectedRow.kind,apiVersion:selectedRow.apiVersion});$('sync-description').textContent=pendingPlan.operation.toUpperCase()+' '+selectedRow.kind+'/'+selectedRow.name+' → '+pendingPlan.target_context+' / '+selectedRow.namespace+' (preview expires in 5 minutes)';$('sync-manifest').textContent=JSON.stringify(pendingPlan.manifest,null,2);$('sync-dialog').showModal();$('sync-status').textContent='Server-side validation passed. Review before confirming.';}catch(error){$('sync-status').textContent=error.message;}finally{$('sync').disabled=false;}}
$('sync').onclick=prepareSync;
$('sync-cancel').onclick=()=>{pendingPlan=null;$('sync-dialog').close();};
$('sync-confirm').onclick=async()=>{if(!pendingPlan)return;$('sync-confirm').disabled=true;try{await clusterRequest('/api/sync/execute',{token:pendingPlan.token,confirm:true});pendingPlan=null;$('sync-dialog').close();$('sync-status').textContent='Synced successfully. Run Analyze migration again to refresh both sides.';}catch(error){$('sync-dialog').close();pendingPlan=null;$('sync-status').textContent=error.message;}finally{$('sync-confirm').disabled=false;}};

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
  for(const row of rows){const tr=document.createElement('tr');for(const key of ['status','kind','namespace','name']){const td=document.createElement('td');if(key==='name'){const button=document.createElement('button');button.type='button';button.textContent=row.name;button.onclick=()=>showVersus(row);td.append(button);if(analyzedPayload&&row.manifest&&row.kind!=='Namespace'){const sync=document.createElement('button');sync.textContent='Sync to target';sync.onclick=()=>{showVersus(row);prepareSync();};td.append(sync);}}else td.textContent=row[key];tr.append(td);}fragment.append(tr);}
  if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=4;td.textContent='No resources match these filters.';tr.append(td);fragment.append(tr);}
  $('rows').append(fragment);
}
for(const id of ['filter-namespace','filter-kind','filter-status'])$(id).onchange=renderResources;
$('clear-filters').onclick=()=>{for(const id of ['filter-namespace','filter-kind','filter-status'])$(id).value='';renderResources();};
