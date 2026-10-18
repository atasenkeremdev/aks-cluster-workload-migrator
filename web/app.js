const $ = id => document.getElementById(id);
let report, analyzedPayload, selectedRow, pendingPlan, pendingRollback=false, activeReview=null, reviewGeneration=0;
let resultNeedsRefresh=false, previewExpiryTimer=null;
let reportScope=null, scopeStale=false, connectionState='Connection pending', writeInFlight=false, previewInFlight=false;
const statusLabels={MATCH:'Match',DIFF:'Different',SOURCE_ONLY:'Source only',TARGET_ONLY:'Target only'};
$('demo').onclick = () => {
  for(const id of ['src','dst']){if(!$(id).value){const option=new Option(id==='src'?'example-source':'example-target',id==='src'?'example-source':'example-target');$(id).append(option);$(id).value=option.value;}}
  setNamespaces(['demo'],['demo']); $('old').value = 'old.example.com'; $('new').value = 'new.example.com';
  const resource = {apiVersion:'apps/v1',kind:'Deployment',metadata:{name:'checkout',namespace:'demo'},spec:{replicas:2,template:{metadata:{labels:{app:'checkout'}},spec:{containers:[{name:'app',image:'old.example.com/checkout:v1'}]}}}};
  $('source').value = JSON.stringify([resource],null,2);
  const target = JSON.parse(JSON.stringify(resource)); target.spec.template.spec.containers[0].image='new.example.com/checkout:v1'; target.spec.replicas=1;
  $('target').value = JSON.stringify([target],null,2);updateOverview();
};
$('planner').onsubmit = async event => {
  event.preventDefault(); $('error').textContent=''; $('results').hidden=true; report=null; analyzedPayload=null; reportScope=null; resultNeedsRefresh=false; $('submit').disabled=true; $('scope-stale').hidden=true;
  try {
    if(!$('src').value||!$('dst').value)throw Error('Select source and target contexts.');
    if($('src').value.trim()===$('dst').value.trim()) throw Error('Source and target must be different.');
    if(Boolean($('old').value.trim())!==Boolean($('new').value.trim())) throw Error('Provide both old and new registry addresses.');
    const live=$('mode').value==='live';
    if(live && (!loadedContexts.includes($('src').value)||!loadedContexts.includes($('dst').value))) throw Error('Load contexts and select the actual source and target names.');
    const payload={source:live?[]:JSON.parse($('source').value),target:live?[]:JSON.parse($('target').value),source_context:$('src').value.trim(),target_context:$('dst').value.trim(),namespaces:[...selectedNamespaces].sort(),old_registry:$('old').value.trim(),new_registry:$('new').value.trim()};
    if(!payload.namespaces.length)throw Error('Select at least one namespace.');
    if(payload.namespaces.length>200)throw Error('Select no more than 200 namespaces per analysis.');
    const submittedScope=scopeFingerprint();
    const data=live?await analyzeWithProgress(payload):await clusterRequest('/api/analyze',payload);
    analyzedPayload=live?structuredClone(payload):null;
    report={...data,source_context:payload.source_context,target_context:payload.target_context};reportScope=submittedScope;
    $('source-column-context').textContent=payload.source_context;$('target-column-context').textContent=payload.target_context;
    $('report-notice').textContent=data.notice+(data.unavailable_kinds?' Unavailable API kinds: source: '+data.unavailable_kinds.source.join(', ')+ '; target: '+data.unavailable_kinds.target.join(', '):'');
    $('summary').replaceChildren(); $('rows').replaceChildren(); $('skipped').replaceChildren(); $('versus').hidden=true;
    for(const [status,count] of Object.entries(data.summary)){const box=document.createElement('div');box.className='metric';box.dataset.status=status;const n=document.createElement('strong');n.textContent=count;box.append(n,document.createTextNode(statusLabels[status]||status));$('summary').append(box);}
    $('differences-only').checked=true;$('resource-search').value='';populateFilters(); renderResources();
    $('skip-count').textContent=`${data.skipped.length} excluded resources`;
    for(const entry of data.skipped){const li=document.createElement('li');li.textContent=`${entry.side}: ${entry.kind}/${entry.name} — ${entry.reason}`;$('skipped').append(li);}
    $('overview-activity').textContent='Analysis complete';$('results').hidden=false;$('step-compare').disabled=false;$('scope').open=false;connectionState=live?'Both clusters read':'Offline analysis';updateOverview();setStep('compare');$('results').scrollIntoView({block:'start'});
  }catch(error){$('overview-activity').textContent='Analysis incomplete';$('scope').open=true;$('error').textContent=error.message;connectionState='Analysis not verified';updateOverview();}finally{$('submit').disabled=false;}
};
$('download').onclick=()=>{if(!report)return;const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='migration-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};

$('mode').onchange=()=>{const live=$('mode').value==='live';$('json-inputs').hidden=live;for(const id of ['source','target'])$(id).required=!live;$('results').hidden=true;report=null;analyzedPayload=null;reportScope=null;$('step-compare').disabled=true;setNamespaces([]);$('namespace-from-json').hidden=live;$('load-namespaces').hidden=!live;connectionState=live?'Connection pending':'Offline manifest';if(!live)loadOfflineNamespaces();updateOverview();};
async function clusterRequest(path,payload,timeout){const options=payload?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{};if(timeout)options.signal=AbortSignal.timeout(timeout);const response=await fetch(path,options);const data=await response.json();if(!response.ok){const error=Error(data.error);error.category=data.category;throw error;}return data;}
let loadedContexts=[];
$('connect').onclick=async()=>{ $('connect').disabled=true;const previousSource=$('src').value;try{const data=await clusterRequest('/api/contexts');loadedContexts=data.contexts;
for(const id of ['src','dst']){const previous=$(id).value;$(id).replaceChildren(new Option(id==='src'?'Select source context':'Select target context',''));for(const name of loadedContexts)$(id).append(new Option(name,name));if(loadedContexts.includes(previous))$(id).value=previous;else if(id==='src'&&loadedContexts.includes('aks-dev'))$(id).value='aks-dev';}
if($('src').value!==previousSource)setNamespaces([]);updateOverview();
$('connection').textContent=loadedContexts.length+' contexts loaded. Select the exact source and target names.';
}catch(error){$('connection').textContent=error.message;connectionState='Unable to load contexts';}finally{$('connect').disabled=false;updateOverview();}};
$('load-namespaces').onclick=async()=>{const context=$('src').value.trim();$('load-namespaces').disabled=true;try{const data=await clusterRequest('/api/namespaces',{context});if($('src').value!==context||$('mode').value!=='live')return;setNamespaces(data.namespaces);$('namespace-options').textContent=data.namespaces.length+' namespaces available.';$('connection').textContent='Source connection verified. Choose your namespace scope.';connectionState='Source reachable';updateOverview();}catch(error){$('connection').textContent=error.message;}finally{$('load-namespaces').disabled=false;}};


function flat(value,path='',out={}){if(value!==null && typeof value==='object'){const entries=Object.entries(value);if(!entries.length)out[path]=JSON.stringify(value);for(const [key,v] of entries)flat(v,path+'/'+key.replaceAll('~','~0').replaceAll('/','~1'),out);}else out[path]=JSON.stringify(value);return out;}
function showVersus(row){selectedRow=row;$('versus').hidden=false;$('versus-title').textContent=row.kind+' / '+(row.namespace||'cluster')+' / '+row.name;$('sync-status').textContent='';$('selected-actions').replaceChildren();if(canSync(row))$('selected-actions').append(actionButtons(row));$('action-explanation').textContent=isWorkload(row)?'Prepare without starting replicas, or activate using the source configuration.':'Sync this configuration resource to the selected target.';highlightSelection();$('diff-paths').textContent='Changed fields: '+(row.diff_paths.join(', ')||'None');
const a=row.manifest?flat(row.manifest):{}, b=row.target_manifest?flat(row.target_manifest):{};const paths=[...new Set([...Object.keys(a),...Object.keys(b)])].sort();
for(const [id,values] of [['source-preview',a],['target-preview',b]]){$(id).replaceChildren();if(!paths.length)$(id).textContent='Resource absent on this side';for(const path of paths){const line=document.createElement('div');line.textContent=path+' = '+(values[path]??'— absent');if(a[path]!==b[path]||row.diff_paths.some(p=>path===p||path.startsWith(p+'/')))line.className='diff-line';$(id).append(line);}}
}
function isWorkload(row){return ['Deployment','StatefulSet','DaemonSet','CronJob','ActiveMQArtemis'].includes(row.kind);}
function actionButtons(row){
 const group=document.createDocumentFragment();
 const actions=isWorkload(row)?(['Deployment','StatefulSet','CronJob'].includes(row.kind)?['prepare','activate']:['activate']):['sync'];
 for(const action of actions){const block=document.createElement('div');block.className='action-choice';const button=document.createElement('button');button.type='button';button.dataset.sync='true';button.dataset.action=action;button.textContent=actionText(action);button.classList.toggle('activate-action',action==='activate');button.onclick=()=>{showVersus(row);openReview(row,action);};block.append(button);const hint=document.createElement('small');hint.textContent=action==='prepare'?(row.kind==='CronJob'?'Suspend scheduled jobs':'Set replicas to zero'):action==='activate'?'May start target workloads':'Copy configuration';block.append(hint);group.append(block);}
 return group;
}
function actionText(action){return action==='prepare'?'Prepare target':action==='activate'?'Activate target':'Sync to target';}
function openReview(row,action){
 if(writeInFlight||previewInFlight||!canSync(row))return;
 $('bulk-review').hidden=true;$('sync-cancel').textContent='Cancel';
 pendingRollback=false;pendingPlan=null;activeReview={row,action,base:structuredClone(analyzedPayload),scope:scopeFingerprint()};
 $('update-strategy').value='apply';$('strategy-control').hidden=false;$('review-advanced').open=false;$('review-manifest').open=false;
 $('confirm-title').textContent=action==='sync'?'Review configuration sync':action==='prepare'?'Review passive preparation':'Review activation';
 $('sync-confirm').textContent=actionText(action);$('sync-confirm').classList.toggle('activate-action',action==='activate');
 $('sync-description').textContent=row.kind+'/'+row.name+' → '+activeReview.base.target_context+' / '+row.namespace;
 $('confirm-impact').textContent=action==='sync'?syncImpact(row):action==='prepare'?prepareImpact(row):'Apply the source workload configuration and allow it to run on the target. The source stays running; coordinate traffic and duplicate processing.';
 $('sync-dialog').showModal();validateReview();
}
async function discardPreview(plan){
 if(!plan)return;const tokens=plan.bulk?plan.plans.map(item=>item.plan.token):[plan.token];
 if(!tokens.length)return;
 try{await clusterRequest('/api/sync/discard',{tokens});}catch(error){/* Tokens expire server-side; no resource writes occur here. */}
}
function setBulkState(index,state){const cell=$('bulk-review-rows').children[index]?.lastElementChild;if(cell)cell.textContent=state;}
async function validateReview(){
 if(!activeReview||writeInFlight)return;
 clearTimeout(previewExpiryTimer);
 const generation=++reviewGeneration,review=activeReview,previous=pendingPlan;
 pendingPlan=null;previewInFlight=true;$('sync-confirm').disabled=true;$('review-conflict').hidden=true;$('preview-guarded').hidden=true;$('retry-preview').hidden=true;$('review-error').textContent='';$('sync-manifest').textContent='';$('sync-warnings').textContent='';$('review-status').textContent='Checking the target and validating this change…';$('overview-activity').textContent='Validating preview…';
 const plans=[],rows=review.rows||[review.row];let index=0;if(review.bulk)rows.forEach((row,i)=>setBulkState(i,'Pending validation'));
 try{
  await discardPreview(previous);
  if(generation!==reviewGeneration)return;
  if(scopeFingerprint()!==review.scope)throw Error('Scope changed. Close this review and run analysis again.');
  for(index=0;index<rows.length;index++){
   if(generation!==reviewGeneration||!$('sync-dialog').open)break;
   const row=rows[index];if(review.bulk){setBulkState(index,'Validating…');$('review-status').textContent=`Validating ${index+1} of ${rows.length}: ${row.kind}/${row.name}`;}
   const plan=await clusterRequest('/api/sync/plan',{...review.base,namespace:row.namespace,name:row.name,kind:row.kind,apiVersion:row.apiVersion,mode:review.action,strategy:$('update-strategy').value});
   plan.expiresAt=Date.now()+(plan.expires_in||300)*1000;plans.push({row,plan});if(generation!==reviewGeneration||!$('sync-dialog').open)break;if(review.bulk)setBulkState(index,'Validated');
  }
  if(generation!==reviewGeneration||!$('sync-dialog').open){await discardPreview({bulk:true,plans});return;}
  const expiresAt=Math.min(...plans.map(item=>item.plan.expiresAt));
  if(expiresAt<=Date.now())throw Error('Validation took longer than the preview lifetime. Narrow the result filters and try again.');
  pendingPlan=review.bulk?{bulk:true,plans,expiresAt}:plans[0].plan;armPreviewExpiry();
  $('sync-manifest').textContent=JSON.stringify(review.bulk?plans.map(item=>item.plan.manifest):plans[0].plan.manifest,null,2);
  $('sync-warnings').textContent=[...new Set(plans.flatMap(item=>[item.plan.update_effect,...(item.plan.warnings||[])].filter(Boolean)))].join(' ');
  $('review-status').textContent=review.bulk?`All ${rows.length} resources passed validation. Confirm to apply sequentially; previews expire in 5 minutes.`:'Validation passed. Review the effect above, then confirm. Preview expires in 5 minutes.';$('sync-confirm').disabled=false;$('overview-activity').textContent='Awaiting confirmation';
 }catch(error){
  await discardPreview({bulk:true,plans});
  if(generation!==reviewGeneration||!$('sync-dialog').open)return;
  if(review.bulk){setBulkState(index,'Validation failed');const row=rows[Math.min(index,rows.length-1)];error.message=row.kind+'/'+row.name+' ('+row.namespace+'): '+error.message;}
  displayReviewError(error);
 }finally{if(generation===reviewGeneration)previewInFlight=false;}
}
function cancelReview(){clearTimeout(previewExpiryTimer);discardPreview(pendingPlan);pendingPlan=null;activeReview=null;reviewGeneration++;previewInFlight=false;$('overview-activity').textContent='Preview cancelled';$('sync-dialog').close();}
$('sync-cancel').onclick=cancelReview;
$('update-strategy').onchange=()=>{if(activeReview)validateReview();};
function displayReviewError(error,afterWrite=false){
 $('review-status').textContent='This change needs review before it can be applied.';$('review-conflict').hidden=false;$('review-error').textContent=error.message;
 const managers=error.message.match(/Field manager\(s\): (.*?)\. Nothing/);
 $('conflict-summary').textContent=error.category==='field_ownership'?(managers?'Target fields are managed by '+managers[1]+'. Review a guarded update?':'Another manager owns target fields. Review a guarded update?'):error.message;
 const rejected=['field_ownership','resource_version'].includes(error.category);
 $('conflict-note').textContent=afterWrite&&!rejected?'Inspect the target and operation history before retrying; the write outcome may be uncertain.':'No changes have been applied.';
 $('preview-guarded').hidden=error.category!=='field_ownership'||!activeReview;$('preview-guarded').textContent=activeReview?.bulk?'Review guarded updates for all '+activeReview.rows.length+' resources':'Review guarded update';
 $('retry-preview').hidden=error.category!=='resource_version'||!activeReview;
 $('overview-activity').textContent='Review needed';$('sync-confirm').disabled=true;
}
$('retry-preview').onclick=validateReview;
$('sync-confirm').onclick=async()=>{
 if(!pendingPlan||writeInFlight)return;
 clearTimeout(previewExpiryTimer);
 if(pendingPlan.bulk){await executeBulk();return;}
 writeInFlight=true;$('update-strategy').disabled=true;$('overview-activity').textContent=pendingRollback?'Rolling back…':'Applying to target…';$('sync-confirm').disabled=true;$('sync-cancel').disabled=true;
 try{
  const result=await clusterRequest(pendingRollback?'/api/rollback/execute':'/api/sync/execute',{token:pendingPlan.token,confirm:true});
  pendingPlan=null;activeReview=null;resultNeedsRefresh=true;updateOverview();$('sync-dialog').close();$('overview-activity').textContent=result.status;$('sync-status').textContent=result.status+' · Run analysis again to refresh the comparison.';
  await loadHistory();if(result.status==='Applied')trackReadiness(result.operation_id);
 }catch(error){
  pendingPlan=null;if(!['field_ownership','resource_version'].includes(error.category))activeReview=null;
  displayReviewError(error,true);$('sync-status').textContent=error.message;await loadHistory();
 }finally{writeInFlight=false;$('update-strategy').disabled=false;$('sync-confirm').disabled=!pendingPlan;$('sync-cancel').disabled=false;updateActionLabels();}
};



async function analyzeWithProgress(payload){
  $('analysis-progress').hidden=false;$('analysis-progress').classList.remove('finished');$('analysis-progress').scrollIntoView({block:'start'});connectionState='Analyzing';updateOverview();
  $('progress-title').textContent='Analyzing clusters…';
  $('progress-current').textContent='Starting analysis';
  $('progress-stats').textContent='';$('progress-wait').textContent='';$('progress-log').textContent='';
  let lastStatus=null,receivedAt=0;
  const paint=()=>{if(!lastStatus)return;const extra=lastStatus.state==='running'?(Date.now()-receivedAt)/1000:0;const elapsed=lastStatus.elapsed_seconds+extra,step=lastStatus.step_seconds+extra;
    $('progress-current').textContent=lastStatus.current;$('overview-activity').textContent=lastStatus.state==='running'?`Analyzing · ${Math.floor(elapsed)}s`:(lastStatus.state==='completed'?'Analysis complete':'Analysis failed');
    $('progress-stats').textContent=`Elapsed ${Math.floor(elapsed)}s · Current step ${Math.floor(step)}s · ${lastStatus.completed_commands} requests completed`;
    $('progress-wait').textContent=lastStatus.state==='running'&&lastStatus.phase==='waiting'?'Waiting for an API or authentication response. API timeout: 15s; batch process limit: 25s per kind.':lastStatus.phase==='comparing'?'Cluster reads complete; comparing manifests.':'';
    if(lastStatus.state==='running'&&extra>5)$('progress-wait').textContent+=' Progress updates delayed; checking the local connection.';
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
  }catch(error){$('progress-title').textContent=lastStatus?.state==='failed'?'Analysis failed':'Application connection interrupted';$('progress-wait').textContent=lastStatus?.state==='failed'?'Analysis stopped at the step shown above.':'The server analysis may still be running; check the local connection.';throw error;}finally{clearInterval(timer);$('analysis-progress').classList.add('finished');}
}

function populateFilters(){
  for(const [id,key,label] of [['filter-namespace','namespace',"All namespaces"],['filter-kind','kind',"All kinds"],['filter-status','status','All statuses']]){
    $(id).replaceChildren(new Option(label,''));for(const value of [...new Set(report.resources.map(row=>row[key]))].sort())$(id).append(new Option(key==='status'?(statusLabels[value]||value):value||'(cluster)',value));
  }
}
function filteredResources(){const query=$('resource-search').value.trim().toLowerCase();return report.resources.filter(row=>(!$('differences-only').checked||row.status!=='MATCH')&&(!query||[row.name,row.namespace,row.kind].some(value=>(value||'').toLowerCase().includes(query)))&&[['filter-namespace','namespace'],['filter-kind','kind'],['filter-status','status']].every(([id,key])=>!$(id).value||row[key]===$(id).value));}
function renderResources(){
  if(!report)return;
  const rows=filteredResources();$('rows').replaceChildren();
  if(!rows.includes(selectedRow)){$('versus').hidden=true;selectedRow=null;}
  $('filtered-count').textContent=`${rows.length} of ${report.resources.length} resources shown · summary and report cover all results`;
  const fragment=document.createDocumentFragment();
  for(const row of rows){
    const tr=document.createElement('tr');tr.className='resource-row';tr.dataset.key=resourceKey(row);
    const status=document.createElement('td'),chip=document.createElement('span');chip.className='status-chip '+row.status.toLowerCase();chip.textContent=statusLabels[row.status]||row.status;chip.title=row.status;status.append(chip);tr.append(status);
    for(const side of ['source','target']){
      const td=document.createElement('td'),manifest=side==='source'?row.manifest:row.target_manifest;
      if(manifest){const name=document.createElement('button');name.type='button';name.className='resource-name';name.textContent=row.name;name.onclick=()=>{showVersus(row);$('versus').scrollIntoView({block:'start'});};td.append(name);const meta=document.createElement('div');meta.className='resource-meta';meta.textContent=row.kind+' · '+(row.namespace||'cluster');td.append(meta);
        if(side==='source'&&canSync(row)){const actions=document.createElement('div');actions.className='row-actions';actions.append(actionButtons(row));td.append(actions);}
      }else{const absent=document.createElement('span');absent.className='resource-absent';absent.textContent='Absent on this side';td.append(absent);const meta=document.createElement('div');meta.className='resource-meta';meta.textContent=row.kind+' · '+(row.namespace||'cluster');td.append(meta);}
      tr.append(td);
    }
    fragment.append(tr);
  }
  if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=3;td.textContent=report.resources.length?'No resources match these filters. Turn off “Differences only” to include matching resources.':'No resources found in the selected scope.';tr.append(td);fragment.append(tr);}
  $('rows').append(fragment);highlightSelection();updateBulkActions();
}
for(const id of ['filter-namespace','filter-kind','filter-status'])$(id).onchange=renderResources;
$('resource-search').oninput=renderResources;$('differences-only').onchange=renderResources;
$('filter-status').addEventListener('change',()=>{if($('filter-status').value==='MATCH'){$('differences-only').checked=false;renderResources();}});
$('clear-filters').onclick=()=>{for(const id of ['filter-namespace','filter-kind','filter-status'])$(id).value='';$('resource-search').value='';$('differences-only').checked=true;renderResources();};

async function loadHistory(){
 try{const data=await clusterRequest('/api/history');$('history-rows').replaceChildren();for(const op of data.operations){const tr=document.createElement('tr');for(const text of [new Date(op.updated_at).toLocaleString()+' / '+op.mode,op.target+' / '+op.namespace+' / '+op.kind+'/'+op.name,op.status+(op.health?' · '+op.health.state+' — '+op.health.detail:'')]){const td=document.createElement('td');td.textContent=text;tr.append(td);}const actions=document.createElement('td');if(op.applied_uid&&op.status!=='RolledBack'){const health=document.createElement('button');health.textContent='Check readiness';health.onclick=async()=>{health.disabled=true;try{const result=await clusterRequest('/api/operations/health',{operation_id:op.id});$('history-status').textContent=result.state+' — '+result.detail;await loadHistory();}catch(error){$('history-status').textContent=error.message;}finally{health.disabled=false;}};actions.append(health);}if(op.rollback_available){const rollback=document.createElement('button');rollback.textContent='Preview rollback';rollback.onclick=()=>prepareRollback(op.id);actions.append(rollback);}tr.append(actions);$('history-rows').append(tr);}renderOutcomes(data.operations);$('history-status').textContent=data.operations.length+' recent operations loaded. These are recorded checks; refresh readiness for current state.';}catch(error){$('history-status').textContent=error.message;}
}
$('load-history').onclick=loadHistory;
async function prepareRollback(id){if(writeInFlight||previewInFlight)return;previewInFlight=true;$('bulk-review').hidden=true;$('sync-cancel').textContent='Cancel';activeReview=null;reviewGeneration++;$('strategy-control').hidden=true;$('review-advanced').open=false;$('review-manifest').open=false;$('review-conflict').hidden=true;$('review-error').textContent='';$('review-status').textContent='Review this rollback before confirming.';$('overview-activity').textContent='Preparing rollback preview…';setStep('execute');try{pendingPlan=await clusterRequest('/api/rollback/plan',{operation_id:id});pendingRollback=true;$('confirm-title').textContent='Confirm rollback';$('confirm-impact').textContent=pendingPlan.operation==='restore'?'The pre-sync target configuration will be restored. This may change a running workload.':'The target resource created by this sync will be deleted. Data and external effects are not restored.';$('sync-confirm').classList.add('activate-action');$('sync-confirm').textContent='Confirm rollback';$('sync-description').textContent=pendingPlan.operation+' '+pendingPlan.kind+'/'+pendingPlan.name+' → '+pendingPlan.target_context+' / '+pendingPlan.namespace;$('sync-manifest').textContent=pendingPlan.manifest?JSON.stringify(pendingPlan.manifest,null,2):'Delete the resource created by this sync (UID and resourceVersion preconditions).';$('sync-warnings').textContent=pendingPlan.warning;$('sync-confirm').disabled=false;$('sync-dialog').showModal();}catch(error){$('history-status').textContent=error.message;}finally{previewInFlight=false;}}
async function trackReadiness(id){for(let attempt=0;attempt<12;attempt++){try{const result=await clusterRequest('/api/operations/health',{operation_id:id});$('overview-activity').textContent=result.state;$('sync-status').textContent=result.state+' — '+result.detail;await loadHistory();if(result.state!=='RollingOut')return;}catch(error){$('sync-status').textContent='Applied; readiness check failed: '+error.message;return;}await new Promise(resolve=>setTimeout(resolve,5000));}$('sync-status').textContent='Still rolling out. Use Check readiness in operation history to continue monitoring.';}

function canSync(row){return Boolean(analyzedPayload && !scopeStale && row.manifest && row.kind!=='Namespace' && ['DIFF','SOURCE_ONLY'].includes(row.status));}

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
  $('namespace-empty').hidden=visible.length>0;$('namespace-empty').textContent=availableNamespaces.length?'No namespaces match the search.':$('mode').value==='live'?'Load source namespaces first.':'Load an example or extract namespaces from your JSON manifests.';
  updateNamespaceCount();
}
function updateNamespaceCount(){
  const count=selectedNamespaces.size;
  $('namespace-summary-text').textContent=count?`${count} namespaces selected`:'Select namespaces';
  $('namespace-count').textContent=`${availableNamespaces.length} namespaces, ${count} selected · maximum 200`;
  $('namespace-all').checked=availableNamespaces.length>0&&count===availableNamespaces.length;
  $('namespace-all').indeterminate=count>0&&count<availableNamespaces.length;
  $('namespace-all').disabled=!availableNamespaces.length;updateOverview();
}
$('namespace-search').oninput=renderNamespaces;
$('namespace-all').onchange=()=>{selectedNamespaces=new Set($('namespace-all').checked?availableNamespaces:[]);renderNamespaces();};
$('namespace-clear').onclick=()=>{selectedNamespaces.clear();renderNamespaces();};
function loadOfflineNamespaces(){
 try{const names=[];for(const id of ['source','target']){if(!$(id).value.trim())continue;const data=JSON.parse($(id).value);const objects=Array.isArray(data)?data:data.kind==='List'?data.items:[data];for(const obj of objects){const name=obj.metadata?.namespace||(obj.kind==='Namespace'?obj.metadata?.name:null);if(name&&!['elastic-agent','kube-system','kube-public','kube-node-lease'].includes(name))names.push(name);}}setNamespaces(names);$('error').textContent='';}catch(error){$('error').textContent='Provide valid source and target JSON before loading namespaces.';}
}
$('namespace-from-json').onclick=loadOfflineNamespaces;
for(const id of ['source','target'])$(id).addEventListener('change',()=>{if($('mode').value==='offline')loadOfflineNamespaces();});
$('src').addEventListener('change',()=>{if($('mode').value==='live'){setNamespaces([]);$('namespace-options').textContent='Source changed. Load namespaces again.';}});
document.addEventListener('click',event=>{if(!$('namespace-dropdown').contains(event.target))$('namespace-dropdown').open=false;});
$('namespace-dropdown').addEventListener('keydown',event=>{if(event.key==='Escape'){$('namespace-dropdown').open=false;$('namespace-summary').focus();}});
renderNamespaces();

function scopeFingerprint(){return JSON.stringify([$('mode').value,$('src').value,$('dst').value,[...selectedNamespaces].sort(),$('old').value.trim(),$('new').value.trim(),$('mode').value==='offline'?[$('source').value,$('target').value]:null]);}
function updateOverview(){
  $('overview-source').textContent=$('src').value||'Select a source';$('overview-target').textContent=$('dst').value||'Select a target';$('overview-namespaces').textContent=selectedNamespaces.size+' namespace';$('overview-connection').textContent=connectionState;$('overview-connection').classList.toggle('connected',connectionState==='Both clusters read'||connectionState==='Source reachable');
  const stale=Boolean(report&&reportScope&&(resultNeedsRefresh||scopeFingerprint()!==reportScope));if(stale!==scopeStale){scopeStale=stale;$('scope-stale').hidden=!stale;renderResources();if(selectedRow){$('selected-actions').replaceChildren();if(canSync(selectedRow))$('selected-actions').append(actionButtons(selectedRow));}}
}
function updateActionLabels(){for(const button of document.querySelectorAll('[data-sync]'))button.disabled=writeInFlight;}
function resourceKey(row){return JSON.stringify([row.apiVersion,row.kind,row.namespace,row.name]);}
function highlightSelection(){for(const tr of document.querySelectorAll('.resource-row'))tr.classList.toggle('selected',Boolean(selectedRow)&&tr.dataset.key===resourceKey(selectedRow));}
function setStep(name){for(const key of ['scope','compare','execute']){const active=key===name;$('step-'+key).classList.toggle('active',active);if(active)$('step-'+key).setAttribute('aria-current','step');else $('step-'+key).removeAttribute('aria-current');}}
$('step-scope').onclick=()=>{$('scope').open=true;setStep('scope');$('scope').scrollIntoView({block:'start'});};
$('step-compare').onclick=()=>{setStep('compare');$('results').scrollIntoView({block:'start'});};
$('step-execute').onclick=()=>{setStep('execute');$('operations').scrollIntoView({block:'start'});};
$('close-versus').onclick=()=>{$('versus').hidden=true;selectedRow=null;highlightSelection();};
for(const id of ['src','dst','old','new','source','target'])$(id).addEventListener('change',()=>{if(id==='src'||id==='dst')connectionState='Connection not verified';updateOverview();});
function renderOutcomes(operations){
 const counts={prepared:0,ready:0,failed:0,pending:0},seen=new Set();
 for(const op of operations||[]){const key=JSON.stringify([op.target,op.namespace,op.kind,op.name]);if(seen.has(key))continue;seen.add(key);const state=op.health?.state;if(op.status==='RolledBack'||state==='Deleted')continue;if(['Unknown','RollbackUnknown','Conflict'].includes(op.status)||state==='Failed'||state==='Drifted')counts.failed++;else if(state==='Prepared')counts.prepared++;else if(state==='Ready')counts.ready++;else counts.pending++;}
 $('outcome-summary').replaceChildren();for(const [key,label] of [['prepared','Prepared'],['ready','Controller ready'],['failed','Failed / review'],['pending','Pending checks']]){const box=document.createElement('div');box.className='outcome-item';const n=document.createElement('strong');n.textContent=operations?counts[key]:'—';box.append(n,document.createTextNode(label));$('outcome-summary').append(box);}
}
$('sync-dialog').addEventListener('cancel',event=>{if(writeInFlight)event.preventDefault();else cancelReview();});
$('mode').onchange();updateActionLabels();renderOutcomes(null);

function prepareImpact(row){if(['Deployment','StatefulSet'].includes(row.kind))return 'Target '+row.kind+' replicas will become zero. Existing target workloads may stop; the source is unchanged.';if(row.kind==='CronJob')return 'The target CronJob will be suspended. Running Jobs are not stopped; the source is unchanged.';return 'Selected '+row.kind+' will be applied to the target. Preparation does not deactivate configuration; running workloads may be affected.';}
new ResizeObserver(entries=>document.documentElement.style.setProperty('--bar-height',entries[0].target.getBoundingClientRect().height+'px')).observe(document.querySelector('.migration-bar'));

$('preview-guarded').onclick=()=>{$('update-strategy').value='merge';validateReview();};

function syncImpact(row){if(row.kind==='Secret')return 'Sync this Secret to the target. Running applications may be affected. Secret rollback backups are not stored.';if(row.kind==='HorizontalPodAutoscaler')return 'Sync this autoscaling configuration to the target. It may immediately change workload replica counts.';return 'Copy this configuration to the target. Running applications that consume it may be affected.';}

function bulkCandidates(action){return report?filteredResources().filter(row=>canSync(row)&&(action==='activate'?isWorkload(row):!isWorkload(row))):[];}
function updateBulkActions(){
 let visible=false;for(const action of ['sync','activate']){const rows=bulkCandidates(action),button=$('bulk-'+action);button.hidden=rows.length<2;button.disabled=writeInFlight||previewInFlight||rows.length>100;button.textContent=(action==='sync'?'Sync all':'Activate all')+' ('+rows.length+')';button.title=rows.length>100?'Narrow the filters to at most 100 resources per batch.':'Applies only to the eligible filtered resources shown below.';visible=visible||rows.length>=2;}$('bulk-actions').hidden=!visible;
}
function openBulkReview(action){
 if(writeInFlight||previewInFlight)return;const rows=bulkCandidates(action);if(rows.length<2||rows.length>100)return;
 pendingRollback=false;pendingPlan=null;activeReview={bulk:true,rows:[...rows],action,base:structuredClone(analyzedPayload),scope:scopeFingerprint()};
 $('sync-cancel').textContent='Cancel';$('bulk-review').hidden=false;$('bulk-review-rows').replaceChildren();
 for(const row of rows){const tr=document.createElement('tr'),name=document.createElement('td'),state=document.createElement('td');name.textContent=row.kind+'/'+row.name+' · '+row.namespace;state.textContent='Pending validation';tr.append(name,state);$('bulk-review-rows').append(tr);}
 $('update-strategy').value='apply';$('strategy-control').hidden=false;$('review-advanced').open=false;$('review-manifest').open=false;
 $('confirm-title').textContent=action==='sync'?'Review sync all':'Review activate all';$('sync-confirm').textContent=(action==='sync'?'Sync all':'Activate all')+' '+rows.length+' resources';$('sync-confirm').classList.toggle('activate-action',action==='activate');
 $('sync-description').textContent=rows.length+' filtered resources → '+activeReview.base.target_context+' / '+new Set(rows.map(row=>row.namespace)).size+' namespace(s)';
 $('confirm-impact').textContent=action==='sync'?'Sync the listed configuration resources. Running workloads may be affected; Secret rollback backups are not stored.':'Activate the listed workloads using their source configuration. The source remains running; coordinate duplicate processing and traffic cutover.';
 $('sync-dialog').showModal();validateReview();
}
$('bulk-sync').onclick=()=>openBulkReview('sync');$('bulk-activate').onclick=()=>openBulkReview('activate');
async function executeBulk(){
 const batch=pendingPlan;pendingPlan=null;writeInFlight=true;$('sync-confirm').disabled=true;$('sync-cancel').disabled=true;$('update-strategy').disabled=true;let completed=0,currentIndex=-1;
 try{
  if(!activeReview||scopeFingerprint()!==activeReview.scope)throw Error('Scope changed. Run analysis again.');
  for(let i=0;i<batch.plans.length;i++){
   currentIndex=i;const {row,plan}=batch.plans[i];setBulkState(i,'Applying…');$('review-status').textContent=`Applying ${i+1} of ${batch.plans.length}: ${row.kind}/${row.name}`;$('overview-activity').textContent=`Applying ${i+1}/${batch.plans.length}`;
   try{await clusterRequest('/api/sync/execute',{token:plan.token,confirm:true});completed++;setBulkState(i,'Applied · check readiness');}
   catch(error){setBulkState(i,'Stopped: '+error.message);throw error;}
  }
  $('review-status').textContent=`Applied ${completed} resources. API acceptance is not readiness; check operation history and re-analyze.`;$('overview-activity').textContent=`Applied ${completed} resources`;
 }catch(error){for(let i=currentIndex+1;i<batch.plans.length;i++)setBulkState(i,'Not processed');$('review-status').textContent=`Stopped after ${completed} of ${batch.plans.length} applied. ${error.message} Remaining resources were not processed. Inspect history before retrying.`;$('overview-activity').textContent='Batch stopped; inspect history';}
 finally{
  await discardPreview(batch);activeReview=null;resultNeedsRefresh=true;updateOverview();await loadHistory();writeInFlight=false;$('sync-cancel').disabled=false;$('sync-cancel').textContent='Close';$('update-strategy').disabled=false;updateActionLabels();
 }
}

function armPreviewExpiry(){
 clearTimeout(previewExpiryTimer);const plan=pendingPlan;
 previewExpiryTimer=setTimeout(()=>{if(pendingPlan!==plan||writeInFlight)return;discardPreview(plan);pendingPlan=null;const error=Error('Preview expired. Refresh validation before confirming.');error.category='resource_version';displayReviewError(error);},Math.max(0,plan.expiresAt-Date.now()));
}
