const $ = id => document.getElementById(id);
let report, analyzedPayload, selectedRow, pendingPlan, pendingRollback=false;
let reportScope=null, scopeStale=false, connectionState='Bağlantı bekleniyor', writeInFlight=false, previewInFlight=false;
const statusLabels={MATCH:'Eşleşiyor',DIFF:'Farklı',SOURCE_ONLY:'Yalnızca source',TARGET_ONLY:'Yalnızca target'};
$('demo').onclick = () => {
  for(const id of ['src','dst']){if(!$(id).value){const option=new Option(id==='src'?'example-source':'example-target',id==='src'?'example-source':'example-target');$(id).append(option);$(id).value=option.value;}}
  setNamespaces(['demo'],['demo']); $('old').value = 'old.example.com'; $('new').value = 'new.example.com';
  const resource = {apiVersion:'apps/v1',kind:'Deployment',metadata:{name:'checkout',namespace:'demo'},spec:{replicas:2,template:{metadata:{labels:{app:'checkout'}},spec:{containers:[{name:'app',image:'old.example.com/checkout:v1'}]}}}};
  $('source').value = JSON.stringify([resource],null,2);
  const target = JSON.parse(JSON.stringify(resource)); target.spec.template.spec.containers[0].image='new.example.com/checkout:v1'; target.spec.replicas=1;
  $('target').value = JSON.stringify([target],null,2);updateOverview();
};
$('planner').onsubmit = async event => {
  event.preventDefault(); $('error').textContent=''; $('results').hidden=true; report=null; analyzedPayload=null; reportScope=null; $('submit').disabled=true; $('scope-stale').hidden=true;
  try {
    if(!$('src').value||!$('dst').value)throw Error('Kaynak ve hedef context seç.');
    if($('src').value.trim()===$('dst').value.trim()) throw Error('Kaynak ve hedef farklı olmalı.');
    if(Boolean($('old').value.trim())!==Boolean($('new').value.trim())) throw Error('Eski ve yeni registry adreslerini birlikte gir.');
    const live=$('mode').value==='live';
    if(live && (!loadedContexts.includes($('src').value)||!loadedContexts.includes($('dst').value))) throw Error('Context listesini yükleyip gerçek kaynak ve hedefi seç.');
    const payload={source:live?[]:JSON.parse($('source').value),target:live?[]:JSON.parse($('target').value),source_context:$('src').value.trim(),target_context:$('dst').value.trim(),namespaces:[...selectedNamespaces].sort(),old_registry:$('old').value.trim(),new_registry:$('new').value.trim()};
    if(!payload.namespaces.length)throw Error('En az bir namespace seç.');
    if(payload.namespaces.length>200)throw Error('Bir analizde en fazla 200 namespace seçebilirsin.');
    const submittedScope=scopeFingerprint();
    const data=live?await analyzeWithProgress(payload):await clusterRequest('/api/analyze',payload);
    analyzedPayload=live?structuredClone(payload):null;
    report={...data,source_context:payload.source_context,target_context:payload.target_context};reportScope=submittedScope;
    $('source-column-context').textContent=payload.source_context;$('target-column-context').textContent=payload.target_context;
    $('report-notice').textContent=data.notice+(data.unavailable_kinds?' Unavailable API kinds: source: '+data.unavailable_kinds.source.join(', ')+ '; target: '+data.unavailable_kinds.target.join(', '):'');
    $('summary').replaceChildren(); $('rows').replaceChildren(); $('skipped').replaceChildren(); $('versus').hidden=true;
    for(const [status,count] of Object.entries(data.summary)){const box=document.createElement('div');box.className='metric';box.dataset.status=status;const n=document.createElement('strong');n.textContent=count;box.append(n,document.createTextNode(statusLabels[status]||status));$('summary').append(box);}
    $('differences-only').checked=true;$('resource-search').value='';populateFilters(); renderResources();
    $('skip-count').textContent=`${data.skipped.length} kapsam dışı kaynak`;
    for(const entry of data.skipped){const li=document.createElement('li');li.textContent=`${entry.side}: ${entry.kind}/${entry.name} — ${entry.reason}`;$('skipped').append(li);}
    $('overview-activity').textContent='Analiz tamamlandı';$('results').hidden=false;$('step-compare').disabled=false;$('scope').open=false;connectionState=live?'İki cluster okundu':'Offline analiz';updateOverview();setStep('compare');$('results').scrollIntoView({block:'start'});
  }catch(error){$('overview-activity').textContent='Analiz tamamlanamadı';$('scope').open=true;$('error').textContent=error.message;connectionState='Analiz doğrulanamadı';updateOverview();}finally{$('submit').disabled=false;}
};
$('download').onclick=()=>{if(!report)return;const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='migration-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};

$('mode').onchange=()=>{const live=$('mode').value==='live';$('json-inputs').hidden=live;for(const id of ['source','target'])$(id).required=!live;$('results').hidden=true;report=null;analyzedPayload=null;reportScope=null;$('step-compare').disabled=true;setNamespaces([]);$('namespace-from-json').hidden=live;$('load-namespaces').hidden=!live;connectionState=live?'Bağlantı bekleniyor':'Offline manifest';if(!live)loadOfflineNamespaces();updateOverview();};
async function clusterRequest(path,payload,timeout){const options=payload?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{};if(timeout)options.signal=AbortSignal.timeout(timeout);const response=await fetch(path,options);const data=await response.json();if(!response.ok)throw Error(data.error);return data;}
let loadedContexts=[];
$('connect').onclick=async()=>{ $('connect').disabled=true;const previousSource=$('src').value;try{const data=await clusterRequest('/api/contexts');loadedContexts=data.contexts;
for(const id of ['src','dst']){const previous=$(id).value;$(id).replaceChildren(new Option(id==='src'?'Select source context':'Select target context',''));for(const name of loadedContexts)$(id).append(new Option(name,name));if(loadedContexts.includes(previous))$(id).value=previous;else if(id==='src'&&loadedContexts.includes('aks-dev'))$(id).value='aks-dev';}
if($('src').value!==previousSource)setNamespaces([]);updateOverview();
$('connection').textContent=loadedContexts.length+' context yüklendi. Kaynak ve hedefin gerçek context adlarını seç.';
}catch(error){$('connection').textContent=error.message;connectionState='Context okunamadı';}finally{$('connect').disabled=false;updateOverview();}};
$('load-namespaces').onclick=async()=>{const context=$('src').value.trim();$('load-namespaces').disabled=true;try{const data=await clusterRequest('/api/namespaces',{context});if($('src').value!==context||$('mode').value!=='live')return;setNamespaces(data.namespaces);$('namespace-options').textContent=data.namespaces.length+' namespace seçilebilir.';$('connection').textContent='Kaynak bağlantısı başarılı. Namespace listesinden kapsamını seç.';connectionState='Source erişilebilir';updateOverview();}catch(error){$('connection').textContent=error.message;}finally{$('load-namespaces').disabled=false;}};


function flat(value,path='',out={}){if(value!==null && typeof value==='object'){const entries=Object.entries(value);if(!entries.length)out[path]=JSON.stringify(value);for(const [key,v] of entries)flat(v,path+'/'+key.replaceAll('~','~0').replaceAll('/','~1'),out);}else out[path]=JSON.stringify(value);return out;}
function showVersus(row){selectedRow=row;$('versus').hidden=false;$('versus-title').textContent=row.kind+' / '+(row.namespace||'cluster')+' / '+row.name;$('sync-status').textContent='';$('sync').hidden=!canSync(row);updateActionLabels();highlightSelection();$('diff-paths').textContent='Değişen alanlar: '+(row.diff_paths.join(', ')||'Yok');
const a=row.manifest?flat(row.manifest):{}, b=row.target_manifest?flat(row.target_manifest):{};const paths=[...new Set([...Object.keys(a),...Object.keys(b)])].sort();
for(const [id,values] of [['source-preview',a],['target-preview',b]]){$(id).replaceChildren();if(!paths.length)$(id).textContent='Bu tarafta kaynak yok';for(const path of paths){const line=document.createElement('div');line.textContent=path+' = '+(values[path]??'— mevcut değil');if(a[path]!==b[path]||row.diff_paths.some(p=>path===p||path.startsWith(p+'/')))line.className='diff-line';$(id).append(line);}}
}
async function prepareSync(){if(writeInFlight||previewInFlight||!selectedRow||!canSync(selectedRow))return;previewInFlight=true;$('overview-activity').textContent='Önizleme doğrulanıyor…';const row=selectedRow,requestedMode=$('sync-mode').value;setStep('execute');$('sync').disabled=true;$('sync-status').textContent='Güncel kaynak okunuyor ve hedefte dry-run doğrulanıyor…';try{pendingRollback=false;pendingPlan=await clusterRequest('/api/sync/plan',{...analyzedPayload,namespace:row.namespace,name:row.name,kind:row.kind,apiVersion:row.apiVersion,mode:requestedMode});$('confirm-title').textContent=pendingPlan.mode==='prepare'?'Hedefi pasif hazırlamayı onayla':'Hedefi aktifleştirmeyi onayla';$('sync-confirm').textContent=pendingPlan.mode==='prepare'?'Pasif hazırlamayı onayla':'Aktifleştirmeyi onayla';$('sync-confirm').classList.toggle('activate-action',pendingPlan.mode==='activate');$('confirm-impact').textContent=pendingPlan.mode==='prepare'?prepareImpact(row):'Kaynak konfigürasyonu hedefe uygulanır ve workload çalışmaya başlayabilir. Source otomatik durdurulmaz; çift işlem ve trafik geçişini kontrol et.'; $('sync-warnings').textContent=(pendingPlan.warnings||[]).join(' ');$('sync-description').textContent=(pendingPlan.operation==='create'?'OLUŞTUR':'GÜNCELLE')+' '+row.kind+'/'+row.name+' → '+pendingPlan.target_context+' / '+row.namespace+' · Önizleme 5 dakika geçerli';$('sync-manifest').textContent=JSON.stringify(pendingPlan.manifest,null,2);$('sync-dialog').showModal();$('overview-activity').textContent='Onay bekleniyor';$('sync-status').textContent='Server-side doğrulama geçti. Onaylamadan önce önizlemeyi incele.';}catch(error){$('overview-activity').textContent='Önizleme başarısız';$('sync-status').textContent=error.message;$('versus').scrollIntoView({block:'start'});}finally{previewInFlight=false;$('sync').disabled=false;}}
$('sync').onclick=prepareSync;
$('sync-cancel').onclick=()=>{pendingPlan=null;$('overview-activity').textContent='Önizleme iptal edildi';$('sync-dialog').close();};
$('sync-confirm').onclick=async()=>{
 if(!pendingPlan||writeInFlight)return;writeInFlight=true;$('overview-activity').textContent=pendingRollback?'Geri alınıyor…':'Hedefe uygulanıyor…';$('sync-confirm').disabled=true;$('sync-cancel').disabled=true;
 try{const result=await clusterRequest(pendingRollback?'/api/rollback/execute':'/api/sync/execute',{token:pendingPlan.token,confirm:true});pendingPlan=null;$('sync-dialog').close();$('overview-activity').textContent=result.status;$('sync-status').textContent=result.status+' · Karşılaştırmayı güncellemek için yeniden analiz et.';await loadHistory();if(result.status==='Applied')trackReadiness(result.operation_id);}
 catch(error){$('sync-dialog').close();pendingPlan=null;$('overview-activity').textContent='İşlem sonucu kontrol edilmeli';$('sync-status').textContent=error.message;await loadHistory();}
 finally{writeInFlight=false;$('sync-confirm').disabled=false;$('sync-cancel').disabled=false;updateActionLabels();}
};



async function analyzeWithProgress(payload){
  $('analysis-progress').hidden=false;$('analysis-progress').classList.remove('finished');$('analysis-progress').scrollIntoView({block:'start'});connectionState='Analiz ediliyor';updateOverview();
  $('progress-title').textContent='Cluster’lar analiz ediliyor…';
  $('progress-current').textContent='Analiz başlatılıyor';
  $('progress-stats').textContent='';$('progress-wait').textContent='';$('progress-log').textContent='';
  let lastStatus=null,receivedAt=0;
  const paint=()=>{if(!lastStatus)return;const extra=lastStatus.state==='running'?(Date.now()-receivedAt)/1000:0;const elapsed=lastStatus.elapsed_seconds+extra,step=lastStatus.step_seconds+extra;
    $('progress-current').textContent=lastStatus.current;$('overview-activity').textContent=lastStatus.state==='running'?`Analiz · ${Math.floor(elapsed)} sn`:(lastStatus.state==='completed'?'Analiz tamamlandı':'Analiz başarısız');
    $('progress-stats').textContent=`Toplam ${Math.floor(elapsed)} sn · Bu adım ${Math.floor(step)} sn · ${lastStatus.completed_commands} istek tamamlandı`;
    $('progress-wait').textContent=lastStatus.state==='running'&&lastStatus.phase==='waiting'?'API / oturum yanıtı bekleniyor. API timeout: 15 sn; toplu isteğin süreç sınırı kind başına 25 sn.':lastStatus.phase==='comparing'?'Cluster okumaları tamamlandı; manifestler karşılaştırılıyor.':'';
    if(lastStatus.state==='running'&&extra>5)$('progress-wait').textContent+=' İlerleme güncellemesi gecikti; yerel bağlantı kontrol ediliyor.';
  };
  const timer=setInterval(paint,500);
  try{
    const job=await clusterRequest('/api/live/jobs',payload,10000);
    while(true){lastStatus=await clusterRequest('/api/jobs/'+encodeURIComponent(job.job_id),null,5000);receivedAt=Date.now();paint();
      $('progress-log').textContent=lastStatus.events.map(e=>`[${e.elapsed.toFixed(1)}s] ${e.message}`).join('\n');$('progress-log').scrollTop=$('progress-log').scrollHeight;
      if(lastStatus.state==='completed'){$('progress-title').textContent='Analiz tamamlandı';$('progress-wait').textContent='';return lastStatus.result;}
      if(lastStatus.state==='failed')throw Error(lastStatus.error);
      await new Promise(resolve=>setTimeout(resolve,750));
    }
  }catch(error){$('progress-title').textContent=lastStatus?.state==='failed'?'Analiz başarısız':'Uygulama bağlantısı kesildi';$('progress-wait').textContent=lastStatus?.state==='failed'?'Analiz yukarıdaki adımda durdu.':'Sunucudaki analiz devam ediyor olabilir; yerel bağlantıyı kontrol et.';throw error;}finally{clearInterval(timer);$('analysis-progress').classList.add('finished');}
}

function populateFilters(){
  for(const [id,key,label] of [['filter-namespace','namespace',"Tüm namespace'ler"],['filter-kind','kind',"Tüm kind'lar"],['filter-status','status','Tüm durumlar']]){
    $(id).replaceChildren(new Option(label,''));for(const value of [...new Set(report.resources.map(row=>row[key]))].sort())$(id).append(new Option(key==='status'?(statusLabels[value]||value):value||'(cluster)',value));
  }
}
function filteredResources(){const query=$('resource-search').value.trim().toLowerCase();return report.resources.filter(row=>(!$('differences-only').checked||row.status!=='MATCH')&&(!query||[row.name,row.namespace,row.kind].some(value=>(value||'').toLowerCase().includes(query)))&&[['filter-namespace','namespace'],['filter-kind','kind'],['filter-status','status']].every(([id,key])=>!$(id).value||row[key]===$(id).value));}
function renderResources(){
  if(!report)return;
  const rows=filteredResources();$('rows').replaceChildren();
  if(!rows.includes(selectedRow)){$('versus').hidden=true;selectedRow=null;}
  $('filtered-count').textContent=`${report.resources.length} kaynaktan ${rows.length} gösteriliyor · özet ve rapor tüm analizi kapsar`;
  const fragment=document.createDocumentFragment();
  for(const row of rows){
    const tr=document.createElement('tr');tr.className='resource-row';tr.dataset.key=resourceKey(row);
    const status=document.createElement('td'),chip=document.createElement('span');chip.className='status-chip '+row.status.toLowerCase();chip.textContent=statusLabels[row.status]||row.status;chip.title=row.status;status.append(chip);tr.append(status);
    for(const side of ['source','target']){
      const td=document.createElement('td'),manifest=side==='source'?row.manifest:row.target_manifest;
      if(manifest){const name=document.createElement('button');name.type='button';name.className='resource-name';name.textContent=row.name;name.onclick=()=>{showVersus(row);$('versus').scrollIntoView({block:'start'});};td.append(name);const meta=document.createElement('div');meta.className='resource-meta';meta.textContent=row.kind+' · '+(row.namespace||'cluster');td.append(meta);
        if(side==='source'&&canSync(row)){const actions=document.createElement('div');actions.className='row-actions';const action=document.createElement('button');action.type='button';action.dataset.sync='true';action.textContent=actionLabel();action.classList.toggle('activate-action',$('sync-mode').value==='activate');action.onclick=()=>{showVersus(row);prepareSync();};actions.append(action);td.append(actions);}
      }else{const absent=document.createElement('span');absent.className='resource-absent';absent.textContent='Bu tarafta mevcut değil';td.append(absent);const meta=document.createElement('div');meta.className='resource-meta';meta.textContent=row.kind+' · '+(row.namespace||'cluster');td.append(meta);}
      tr.append(td);
    }
    fragment.append(tr);
  }
  if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=3;td.textContent=report.resources.length?'Bu filtrelerle gösterilecek kaynak yok. Eşleşenleri görmek için “Yalnızca farklar” seçimini kaldır.':'Seçilen kapsamda kaynak bulunamadı.';tr.append(td);fragment.append(tr);}
  $('rows').append(fragment);highlightSelection();
}
for(const id of ['filter-namespace','filter-kind','filter-status'])$(id).onchange=renderResources;
$('resource-search').oninput=renderResources;$('differences-only').onchange=renderResources;
$('filter-status').addEventListener('change',()=>{if($('filter-status').value==='MATCH'){$('differences-only').checked=false;renderResources();}});
$('clear-filters').onclick=()=>{for(const id of ['filter-namespace','filter-kind','filter-status'])$(id).value='';$('resource-search').value='';$('differences-only').checked=true;renderResources();};

async function loadHistory(){
 try{const data=await clusterRequest('/api/history');$('history-rows').replaceChildren();for(const op of data.operations){const tr=document.createElement('tr');for(const text of [new Date(op.updated_at).toLocaleString()+' / '+op.mode,op.target+' / '+op.namespace+' / '+op.kind+'/'+op.name,op.status+(op.health?' · '+op.health.state+' — '+op.health.detail:'')]){const td=document.createElement('td');td.textContent=text;tr.append(td);}const actions=document.createElement('td');if(op.applied_uid&&op.status!=='RolledBack'){const health=document.createElement('button');health.textContent='Durumu kontrol et';health.onclick=async()=>{health.disabled=true;try{const result=await clusterRequest('/api/operations/health',{operation_id:op.id});$('history-status').textContent=result.state+' — '+result.detail;await loadHistory();}catch(error){$('history-status').textContent=error.message;}finally{health.disabled=false;}};actions.append(health);}if(op.rollback_available){const rollback=document.createElement('button');rollback.textContent='Geri almayı önizle';rollback.onclick=()=>prepareRollback(op.id);actions.append(rollback);}tr.append(actions);$('history-rows').append(tr);}renderOutcomes(data.operations);$('history-status').textContent=data.operations.length+' son işlem yüklendi. Görünüm geçmiş kayıtlara dayanır; güncel durum için kontrol et.';}catch(error){$('history-status').textContent=error.message;}
}
$('load-history').onclick=loadHistory;
async function prepareRollback(id){if(writeInFlight||previewInFlight)return;previewInFlight=true;$('overview-activity').textContent='Geri alma önizleniyor…';setStep('execute');try{pendingPlan=await clusterRequest('/api/rollback/plan',{operation_id:id});pendingRollback=true;$('confirm-title').textContent='Geri almayı onayla';$('confirm-impact').textContent=pendingPlan.operation==='restore'?'Hedefin sync öncesindeki konfigürasyonu geri yüklenecek. Bu işlem çalışan workload’u değiştirebilir.':'Bu sync işleminin oluşturduğu hedef kaynak silinecek. Veri ve harici etkiler geri alınmaz.';$('sync-confirm').classList.add('activate-action');$('sync-confirm').textContent='Geri almayı onayla';$('sync-description').textContent=pendingPlan.operation+' '+pendingPlan.kind+'/'+pendingPlan.name+' → '+pendingPlan.target_context+' / '+pendingPlan.namespace;$('sync-manifest').textContent=pendingPlan.manifest?JSON.stringify(pendingPlan.manifest,null,2):'Delete the resource created by this sync (UID and resourceVersion preconditions).';$('sync-warnings').textContent=pendingPlan.warning;$('sync-dialog').showModal();}catch(error){$('history-status').textContent=error.message;}finally{previewInFlight=false;}}
async function trackReadiness(id){for(let attempt=0;attempt<12;attempt++){try{const result=await clusterRequest('/api/operations/health',{operation_id:id});$('overview-activity').textContent=result.state;$('sync-status').textContent=result.state+' — '+result.detail;await loadHistory();if(result.state!=='RollingOut')return;}catch(error){$('sync-status').textContent='Applied; readiness sorgusu başarısız: '+error.message;return;}await new Promise(resolve=>setTimeout(resolve,5000));}$('sync-status').textContent='Rollout sürüyor. İşlem geçmişinden Durumu kontrol et ile takibe devam edebilirsin.';}

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
  $('namespace-empty').hidden=visible.length>0;$('namespace-empty').textContent=availableNamespaces.length?'Aramayla eşleşen namespace yok.':$('mode').value==='live'?'Önce kaynak namespace’lerini yükle.':'Örnek yükle veya JSON manifestlerden namespace listesini al.';
  updateNamespaceCount();
}
function updateNamespaceCount(){
  const count=selectedNamespaces.size;
  $('namespace-summary-text').textContent=count?`${count} namespace seçili`:'Namespace seç';
  $('namespace-count').textContent=`${availableNamespaces.length} namespace içinden ${count} seçili · en fazla 200`;
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
$('src').addEventListener('change',()=>{if($('mode').value==='live'){setNamespaces([]);$('namespace-options').textContent='Kaynak değişti. Namespace listesini yeniden yükle.';}});
document.addEventListener('click',event=>{if(!$('namespace-dropdown').contains(event.target))$('namespace-dropdown').open=false;});
$('namespace-dropdown').addEventListener('keydown',event=>{if(event.key==='Escape'){$('namespace-dropdown').open=false;$('namespace-summary').focus();}});
renderNamespaces();

function scopeFingerprint(){return JSON.stringify([$('mode').value,$('src').value,$('dst').value,[...selectedNamespaces].sort(),$('old').value.trim(),$('new').value.trim(),$('mode').value==='offline'?[$('source').value,$('target').value]:null]);}
function updateOverview(){
  $('overview-source').textContent=$('src').value||'Kaynak seçilmedi';$('overview-target').textContent=$('dst').value||'Hedef seçilmedi';$('overview-namespaces').textContent=selectedNamespaces.size+' namespace';$('overview-mode').textContent=actionLabel();$('overview-connection').textContent=connectionState;$('overview-connection').classList.toggle('connected',connectionState==='İki cluster okundu'||connectionState==='Source erişilebilir');
  const stale=Boolean(report&&reportScope&&scopeFingerprint()!==reportScope);if(stale!==scopeStale){scopeStale=stale;$('scope-stale').hidden=!stale;renderResources();if(selectedRow)$('sync').hidden=!canSync(selectedRow);}
}
function actionLabel(){return $('sync-mode').value==='activate'?'Aktifleştir':'Pasif hazırla';}
function updateActionLabels(){for(const button of document.querySelectorAll('[data-sync],#sync')){button.textContent=actionLabel();button.classList.toggle('activate-action',$('sync-mode').value==='activate');button.disabled=writeInFlight;}const active=$('sync-mode').value==='activate';$('mode-explanation').textContent=active?'Kaynak konfigürasyonu hedefe uygulanır. Source otomatik durmaz; çift işlem ve trafik geçişini ayrıca planla.':'Deployment/StatefulSet sıfır replica, CronJob suspend edilir. Mevcut hedef workload durabilir.';$('action-explanation').textContent=active?'Aktifleştirme için önce server-side dry-run ve onayın alınır.':'Pasif hazırlama hedefte mevcut workload’u durdurabilir. Önizlemede değişiklikleri kontrol et.';}
$('sync-mode').onchange=()=>{updateActionLabels();updateOverview();};
function resourceKey(row){return JSON.stringify([row.apiVersion,row.kind,row.namespace,row.name]);}
function highlightSelection(){for(const tr of document.querySelectorAll('.resource-row'))tr.classList.toggle('selected',Boolean(selectedRow)&&tr.dataset.key===resourceKey(selectedRow));}
function setStep(name){for(const key of ['scope','compare','execute']){const active=key===name;$('step-'+key).classList.toggle('active',active);if(active)$('step-'+key).setAttribute('aria-current','step');else $('step-'+key).removeAttribute('aria-current');}}
$('step-scope').onclick=()=>{$('scope').open=true;setStep('scope');$('scope').scrollIntoView({block:'start'});};
$('step-compare').onclick=()=>{setStep('compare');$('results').scrollIntoView({block:'start'});};
$('overview-mode').onclick=()=>$('step-execute').click();
$('step-execute').onclick=()=>{setStep('execute');$('operations').scrollIntoView({block:'start'});};
$('close-versus').onclick=()=>{$('versus').hidden=true;selectedRow=null;highlightSelection();};
for(const id of ['src','dst','old','new','source','target'])$(id).addEventListener('change',()=>{if(id==='src'||id==='dst')connectionState='Bağlantı doğrulanmadı';updateOverview();});
function renderOutcomes(operations){
 const counts={prepared:0,ready:0,failed:0,pending:0},seen=new Set();
 for(const op of operations||[]){const key=JSON.stringify([op.target,op.namespace,op.kind,op.name]);if(seen.has(key))continue;seen.add(key);const state=op.health?.state;if(op.status==='RolledBack'||state==='Deleted')continue;if(['Unknown','RollbackUnknown'].includes(op.status)||state==='Failed'||state==='Drifted')counts.failed++;else if(state==='Prepared')counts.prepared++;else if(state==='Ready')counts.ready++;else counts.pending++;}
 $('outcome-summary').replaceChildren();for(const [key,label] of [['prepared','Pasif hazır'],['ready','Controller ready'],['failed','Hata / inceleme'],['pending','Kontrol bekliyor']]){const box=document.createElement('div');box.className='outcome-item';const n=document.createElement('strong');n.textContent=operations?counts[key]:'—';box.append(n,document.createTextNode(label));$('outcome-summary').append(box);}
}
$('sync-dialog').addEventListener('cancel',event=>{if(writeInFlight)event.preventDefault();else{pendingPlan=null;$('overview-activity').textContent='Önizleme iptal edildi';}});
$('mode').onchange();updateActionLabels();renderOutcomes(null);

function prepareImpact(row){if(['Deployment','StatefulSet'].includes(row.kind))return 'Hedef '+row.kind+' replica sayısı 0 olacak. Mevcut hedef workload durabilir; kaynak cluster değişmez.';if(row.kind==='CronJob')return 'Hedef CronJob suspend edilecek. Zaten çalışan Job’lar durdurulmaz; kaynak cluster değişmez.';return 'Seçilen '+row.kind+' hedefe uygulanacak. Pasif mod bu konfigürasyonu etkisizleştirmez; çalışan workload’ları etkileyebilir.';}
new ResizeObserver(entries=>document.documentElement.style.setProperty('--bar-height',entries[0].target.getBoundingClientRect().height+'px')).observe(document.querySelector('.migration-bar'));
