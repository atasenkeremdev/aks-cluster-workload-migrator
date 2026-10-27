/* Independent read-only IAM workspace; no workload action state is shared. */
(()=>{
  const element=id=>document.getElementById(id);
  let report=null,reportScope=null,page=0,contexts=[],running=false;
  const pageSize=50;
  function activate(name){
    element('workspace-title').textContent=name==='iam'?'IAM INVENTORY':'WORKLOAD MIGRATION';
    for(const link of document.querySelectorAll('aside a[href^="#"]'))link.classList.toggle('secondary',link.getAttribute('href')==='#operations'||(link.dataset.workspace==='iam')!==(name==='iam'));
    for(const key of ['workloads','iam']){
      const tab=element('tab-'+key),active=key===name;
      tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;
      element(key==='workloads'?'workload-panel':'iam-panel').hidden=!active;
    }
  }
  for(const name of ['workloads','iam']){
    const tab=element('tab-'+name);
    tab.onclick=()=>activate(name);
    tab.onkeydown=event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();const next=event.key==='Home'?'workloads':event.key==='End'?'iam':name==='iam'?'workloads':'iam';
      activate(next);element('tab-'+next).focus();
    };
  }
  for(const link of document.querySelectorAll('aside a[href^="#"]'))link.addEventListener('click',()=>activate(link.dataset.workspace==='iam'?'iam':'workloads'));
  function scope(){return {source_context:element('iam-source').value,target_context:element('iam-target').value,
    namespaces:[...new Set(element('iam-namespaces').value.split(/[\s,]+/).filter(Boolean))].sort(),
    include_azure:element('iam-azure').checked,azure_source_id:element('iam-azure-source').value.trim(),azure_target_id:element('iam-azure-target').value.trim()};}
  function markStale(){element('iam-stale').hidden=!report||JSON.stringify(scope())===reportScope;}
  for(const id of ['iam-source','iam-target','iam-namespaces','iam-azure','iam-azure-source','iam-azure-target'])element(id).addEventListener('input',markStale);
  const resolutionGeneration={source:0,target:0};
  const resolvedContext={source:'',target:''};
  async function resolveContext(side){
    const context=element('iam-'+side).value,input=element('iam-azure-'+side),message=element('iam-resolve-'+side);
    const generation=++resolutionGeneration[side];
    if(resolvedContext[side]!==context){input.value='';resolvedContext[side]=context;markStale();}
    if(!context||!element('iam-azure').checked){message.textContent='';return;}
    const previous=input.value;
    message.textContent=(side==='source'?'Source':'Target')+': finding AKS resource ID…';
    try{
      const data=await clusterRequest('/api/iam/resolve-context',{context},180000);
      if(generation!==resolutionGeneration[side]||element('iam-'+side).value!==context||!element('iam-azure').checked)return;
      // A manual edit made while lookup was running takes precedence.
      if(input.value!==previous){message.textContent='Manual AKS resource ID retained.';return;}
      if(data.resource_id)input.value=data.resource_id;
      message.textContent=(side==='source'?'Source: ':'Target: ')+data.message;markStale();
    }catch(error){
      if(generation===resolutionGeneration[side]&&element('iam-'+side).value===context&&element('iam-azure').checked)message.textContent=(side==='source'?'Source: ':'Target: ')+error.message+' Enter the AKS resource ID manually.';
    }
  }
  for(const side of ['source','target'])element('iam-'+side).addEventListener('change',()=>resolveContext(side));
  element('iam-azure').onchange=()=>{
    const enabled=element('iam-azure').checked;element('iam-azure-fields').hidden=!enabled;
    for(const id of ['iam-azure-source','iam-azure-target'])element(id).required=enabled;
    for(const side of ['source','target'])resolveContext(side);
    markStale();
  };
  element('iam-connect').onclick=async()=>{
    element('iam-connect').disabled=true;
    try{
      const data=await clusterRequest('/api/contexts');contexts=data.contexts;
      for(const id of ['iam-source','iam-target']){const select=element(id),previous=select.value;
        select.replaceChildren(new Option(id==='iam-source'?'Select source context':'Select target context',''));
        for(const context of contexts)select.append(new Option(context,context));
        if(contexts.includes(previous))select.value=previous;
      }
      for(const side of ['source','target'])resolveContext(side);
      element('iam-connection').textContent=contexts.length+' contexts loaded for IAM. Cluster connectivity is checked during the scan.';markStale();
    }catch(error){element('iam-connection').textContent=error.message;}finally{element('iam-connect').disabled=false;}
  };
  element('iam-form').onsubmit=async event=>{
    event.preventDefault();if(running)return;
    element('iam-error').textContent='';
    const payload=scope(),submitted=JSON.stringify(payload);
    if(!contexts.includes(payload.source_context)||!contexts.includes(payload.target_context)){element('iam-error').textContent='Load IAM contexts and select source and target.';return;}
    if(payload.source_context===payload.target_context){element('iam-error').textContent='Source and target contexts must be distinct.';return;}
    running=true;element('iam-start').disabled=true;element('iam-progress').hidden=false;
    element('iam-progress-status').textContent='Starting IAM scan…';element('iam-progress-log').textContent='';
    try{
      const job=await clusterRequest('/api/iam/jobs',payload,10000);
      while(true){
        const state=await clusterRequest('/api/iam/jobs/'+encodeURIComponent(job.job_id),null,10000);
        element('iam-progress-status').textContent=state.current+' · '+Math.floor(state.elapsed_seconds)+'s';
        element('iam-progress-log').textContent=(state.events||[]).map(item=>item.message).join('\n');
        if(state.state==='failed')throw Error(state.error);
        if(state.state==='completed'){report=state.result;reportScope=submitted;page=0;renderReport();markStale();break;}
        await new Promise(resolve=>setTimeout(resolve,750));
      }
    }catch(error){element('iam-error').textContent=error.message;element('iam-progress-status').textContent='IAM scan interrupted. A submitted scan may still be running; no resources were modified.';}
    finally{running=false;element('iam-start').disabled=false;}
  };
  function renderReport(){
    element('iam-results').hidden=false;element('iam-detail').hidden=true;
    element('iam-result-scope').textContent=report.source_context+' → '+report.target_context+' · '+(report.namespaces.length?report.namespaces.join(', '):'All non-excluded namespaces')+(report.include_azure?' · Kubernetes + Azure':' · Kubernetes only');
    element('iam-completeness').textContent=report.complete?'Requested inventory reads completed. Differences still require identity and permission review before any sync.':'Partial inventory: some reads were unavailable. UNKNOWN is not evidence of missing configuration; inspect coverage before deciding.';
    element('iam-notice').textContent=report.notice;
    element('iam-summary').replaceChildren();
    for(const [status,count] of Object.entries(report.summary)){const box=document.createElement('div');box.className='metric';const strong=document.createElement('strong');strong.textContent=count;box.append(strong,document.createTextNode(status));element('iam-summary').append(box);}
    element('iam-kind').replaceChildren(new Option('All kinds',''));for(const kind of [...new Set(report.resources.map(row=>row.kind))].sort())element('iam-kind').append(new Option(kind,kind));
    element('iam-coverage-list').replaceChildren();
    for(const [side,checks] of Object.entries(report.coverage))for(const check of checks){const li=document.createElement('li');li.textContent=side+' · '+check.kind+' · '+(check.scope||'cluster')+' · '+check.state+' · '+check.count+' resources'+(check.error?' — '+check.error:'');element('iam-coverage-list').append(li);}
    renderRows();
  }
  function renderRows(){
    if(!report)return;
    element('iam-detail').hidden=true;
    const query=element('iam-search').value.trim().toLowerCase();
    const rows=report.resources.filter(row=>(!query||[row.name,row.namespace,row.kind].some(value=>value.toLowerCase().includes(query)))&&[['iam-kind','kind'],['iam-provider','provider'],['iam-status','status']].every(([id,key])=>!element(id).value||element(id).value===row[key]));
    const total=Math.max(1,Math.ceil(rows.length/pageSize));page=Math.min(page,total-1);element('iam-rows').replaceChildren();
    for(const row of rows.slice(page*pageSize,(page+1)*pageSize)){
      const tr=document.createElement('tr');for(const text of [row.provider+' / '+row.kind,row.name+' / '+(row.namespace||'cluster'),row.status]){const td=document.createElement('td');td.textContent=text;tr.append(td);}
      const td=document.createElement('td'),button=document.createElement('button');button.type='button';button.textContent='View comparison';button.onclick=()=>{
        element('iam-detail').hidden=false;element('iam-detail-title').textContent=row.kind+' / '+row.name;
        element('iam-detail-recommendation').textContent=row.recommendation;
        for(const side of ['source','target'])element('iam-detail-'+side).textContent=row[side]?JSON.stringify(row[side].configuration,null,2):row.status==='UNKNOWN'?'Unavailable or not observed; coverage is incomplete':'Not present in the completed inventory';
        element('iam-detail-paths').textContent='Changed fields: '+(row.diff_paths.join(', ')||'None or not comparable');element('iam-detail').scrollIntoView({block:'start'});
      };td.append(button);tr.append(td);element('iam-rows').append(tr);
    }
    if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=4;td.textContent='No IAM resources match these filters. Check coverage for unavailable reads.';tr.append(td);element('iam-rows').append(tr);}
    element('iam-count').textContent=rows.length+' of '+report.resources.length+' IAM entries · report includes all results';
    element('iam-page').textContent='Page '+(page+1)+' of '+total;element('iam-prev').disabled=page===0;element('iam-next').disabled=page>=total-1;
  }
  for(const id of ['iam-search','iam-kind','iam-provider','iam-status'])element(id).addEventListener('input',()=>{page=0;renderRows();});
  element('iam-prev').onclick=()=>{page--;renderRows();};element('iam-next').onclick=()=>{page++;renderRows();};
  element('iam-download').onclick=()=>{
    if(!report)return;const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download='iam-inventory-report.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
})();
