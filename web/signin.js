(()=>{
 const button=document.getElementById('microsoft-signin'),status=document.getElementById('signin-status');
 let pending=false;
 async function poll(){
  try{
   const response=await fetch('/auth/status',{cache:'no-store'});const data=await response.json();
   if(!response.ok)throw Error(data.error||'Sign-in is unavailable.');
   if(data.state==='authenticated'){location.replace('/');return;}
   if(data.state==='pending'){
    document.getElementById('device-challenge').hidden=false;
    document.getElementById('device-code').textContent=data.user_code;
    document.getElementById('device-link').href=data.verification_uri;
    status.textContent='Waiting for Microsoft sign-in. This code expires in '+Math.max(0,Math.ceil((data.expires-Date.now()/1000)/60))+' minutes.';
   }else if(data.state==='failed'){pending=false;button.disabled=false;status.textContent=data.error;document.getElementById('device-challenge').hidden=true;return;}
   else if(data.state==='anonymous'){pending=false;button.disabled=false;status.textContent='Select Sign in with Microsoft to start.';return;}
   else status.textContent='Requesting a Microsoft sign-in code…';
   if(pending||['starting','pending'].includes(data.state)){pending=true;button.disabled=true;setTimeout(poll,1500);}
  }catch(error){pending=false;button.disabled=false;status.textContent=error.message;}
 }
 button.onclick=async()=>{button.disabled=true;pending=true;status.textContent='Starting Microsoft sign-in…';try{const response=await fetch('/auth/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const data=await response.json();if(!response.ok)throw Error(data.error||'Sign-in failed.');poll();}catch(error){pending=false;button.disabled=false;status.textContent=error.message;}};
 poll();
})();
