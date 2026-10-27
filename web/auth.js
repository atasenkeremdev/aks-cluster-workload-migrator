(()=>{
 fetch('/auth/status',{cache:'no-store'}).then(response=>response.ok?response.json():null).then(data=>{
  if(data?.state!=='authenticated')return;
  document.getElementById('user-session').hidden=false;
  document.getElementById('user-name').textContent='Signed in as '+data.user.name+(data.user.username?' · '+data.user.username:'');
 }).catch(()=>{});
 document.getElementById('sign-out').onclick=async()=>{const response=await fetch('/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(response.ok)location.replace('/signin');};
})();
