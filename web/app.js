const $ = id => document.getElementById(id);
let report;
$('demo').onclick = () => {
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
    const payload={source:live?[]:JSON.parse($('source').value),target:live?[]:JSON.parse($('target').value),source_context:$('src').value.trim(),target_context:$('dst').value.trim(),namespaces:$('namespaces').value.split(',').map(s=>s.trim()).filter(Boolean),old_registry:$('old').value.trim(),new_registry:$('new').value.trim()};
    const response=await fetch(live?'/api/live/analyze':'/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await response.json(); if(!response.ok) throw Error(data.error);
    report={...data,source_context:$('src').value,target_context:$('dst').value};
    $('report-notice').textContent=data.notice+(data.unavailable_kinds?' Unavailable API kinds: source: '+data.unavailable_kinds.source.join(', ')+ '; target: '+data.unavailable_kinds.target.join(', '):'');
    $('summary').replaceChildren(); $('rows').replaceChildren(); $('skipped').replaceChildren(); $('manifest').textContent='Select a resource.';
    for(const [status,count] of Object.entries(data.summary)){const box=document.createElement('div');box.className='metric';const n=document.createElement('strong');n.textContent=count;box.append(n,document.createTextNode(status.replaceAll('_',' ')));$('summary').append(box);}
    for(const row of data.resources){const tr=document.createElement('tr');for(const key of ['status','kind','namespace','name']){const td=document.createElement('td'); if(key==='name'){const button=document.createElement('button');button.type='button';button.textContent=row.name;button.onclick=()=>{$('preview').open=true;$('manifest').textContent=row.manifest?JSON.stringify(row.manifest,null,2):'Target-only resource; no source manifest.';};td.append(button);}else td.textContent=row[key];tr.append(td);} $('rows').append(tr);}
    if(!data.resources.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=4;td.textContent='No resources in the selected migration scope.';tr.append(td);$('rows').append(tr);}
    $('skip-count').textContent=`${data.skipped.length} excluded resources`;
    for(const entry of data.skipped){const li=document.createElement('li');li.textContent=`${entry.side}: ${entry.kind}/${entry.name} — ${entry.reason}`;$('skipped').append(li);}
    $('results').hidden=false;
  }catch(error){$('error').textContent=error.message;}finally{$('submit').disabled=false;}
};
$('download').onclick=()=>{if(!report)return;const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='migration-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};

$('mode').onchange=()=>{const live=$('mode').value==='live';$('json-inputs').hidden=live;for(const id of ['source','target'])$(id).required=!live;$('results').hidden=true;};
async function clusterRequest(path,payload){const response=await fetch(path,payload?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{});const data=await response.json();if(!response.ok)throw Error(data.error);return data;}
$('connect').onclick=async()=>{ $('connect').disabled=true;try{const data=await clusterRequest('/api/contexts');$('contexts').replaceChildren();for(const context of data.contexts){const option=document.createElement('option');option.value=context;$('contexts').append(option);}$('connection').textContent=data.contexts.length+' contexts loaded. Select source and target, then load namespaces.';}catch(error){$('connection').textContent=error.message;}finally{$('connect').disabled=false;}};
$('load-namespaces').onclick=async()=>{$('load-namespaces').disabled=true;try{const data=await clusterRequest('/api/namespaces',{context:$('src').value.trim()});$('namespace-options').textContent='Available source namespaces: '+data.namespaces.join(', ');$('connection').textContent='Source cluster reachable. Enter the namespaces to compare.';}catch(error){$('connection').textContent=error.message;}finally{$('load-namespaces').disabled=false;}};
