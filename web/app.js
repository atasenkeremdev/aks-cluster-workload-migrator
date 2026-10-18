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
    const payload={source:JSON.parse($('source').value),target:JSON.parse($('target').value),namespaces:$('namespaces').value.split(',').map(s=>s.trim()).filter(Boolean),old_registry:$('old').value.trim(),new_registry:$('new').value.trim()};
    const response=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await response.json(); if(!response.ok) throw Error(data.error);
    report={...data,source_context:$('src').value,target_context:$('dst').value};
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
