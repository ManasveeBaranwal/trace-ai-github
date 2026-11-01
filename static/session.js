(() => {
  const originalFetch = window.fetch.bind(window);
  function returnToLogin() {
    const next=location.pathname+location.search+location.hash;
    location.replace('/login?next='+encodeURIComponent(next));
  }
  const readOnlyMessage='This public demo is read-only. Browse the fictional records, evidence and source lookup; changes and uploads are disabled.';
  function demoWriteControl(control) {
    if(control.closest('[role=tablist]')||['step1','step2','step3'].includes(control.id))return false;
    const label=(control.textContent||control.value||'').trim().replace(/^[＋+]\s*/,'').replace(/^\d+\s*/, '');
    return /^(?:add\b|new\b|create\b|edit\b|revise\b|save\b|publish\b|return for correction\b|review & approve\b|confirm review\b|prepare (?:reminder|drafts)\b|upload\b|start BRSR report\b|submit\b|calculate\b|delete\b|remove\b|archive\b|deactivate\b|activate\b|invite\b|approve\b|retire\b|import\b|mark reviewed\b|confirm & calculate\b|review & calculate\b|choose a bill\b|try a fictional sample\b|validate & preview\b)/i.test(label);
  }
  function preparePublicDemo() {
    document.documentElement.dataset.publicDemo='true';
    if(!document.querySelector('.sandbox-note')){
      const note=document.createElement('div');note.id='public-demo-notice';note.setAttribute('role','note');note.textContent='Public demo · Fictional records · Read only · Resets on restart. Source lookup is available; hosted AI and uploads are disabled.';
      note.style.cssText='background:#ecf1df;color:#2a5037;border-bottom:1px solid #d6e0c7;padding:12px 22px;font:12px/1.6 system-ui;position:relative;z-index:50;';
      const main=document.querySelector('main');(main||document.body).prepend(note);
    }
    const disableWrites=()=>document.querySelectorAll('button,input[type="submit"],input[type="button"]').forEach(control=>{
      if(demoWriteControl(control)){control.disabled=true;control.title=readOnlyMessage;control.dataset.demoReadOnly='true';}
    });
    disableWrites();
    new MutationObserver(disableWrites).observe(document.body,{childList:true,subtree:true});
    document.addEventListener('click',event=>{
      const control=event.target.closest?.('button,input[type="submit"],input[type="button"]');
      if(control&&demoWriteControl(control)){event.preventDefault();event.stopImmediatePropagation();control.disabled=true;control.title=readOnlyMessage;}
    },true);
  }
  window.traceSession = originalFetch('/auth/session').then(r=>r.json()).then(session=>{
    if(!session.user){returnToLogin();throw new Error('Sign in required');}
    const initialAccess=session.access_stage==='two';
    const roleLabels={master:initialAccess?'Super Access':'Super Master',consultant_manager:'Consultant Manager',consultant:'Consultant',company_admin:initialAccess?'Company Manager':'Company Administrator',company_user:initialAccess?'Company Manager':'Company Administrator',data_entry:'Data-entry User'};
    const homes={master:'/super-admin',consultant_manager:'/consultant-manager',consultant:'/consultant',company_admin:'/company-admin',company_user:'/company-admin',data_entry:'/data-entry'};
    if(initialAccess&&['company_admin','company_user'].includes(session.user.role))homes[session.user.role]='/company-manager';
    session.home=session.home||homes[session.user.role]||'/login';
    const banner=document.getElementById('account-banner');
    if(banner){banner.className='account-banner';const name=document.createElement('span');name.textContent=session.demo_mode?roleLabels.master+' · local access':session.user.name+' · '+roleLabels[session.user.role];const link=document.createElement('a');link.href=session.home;link.textContent='My dashboard ↗';const logout=document.createElement('button');logout.type='button';logout.textContent='Sign out';logout.addEventListener('click',async()=>{await originalFetch('/auth/logout',{method:'POST',headers:{'X-CSRF-Token':session.csrf}});location.replace('/login');});banner.append(name,link);if(session.demo_mode||session.local_company_mode)logout.textContent='Exit workspace';banner.append(logout);}
    if(session.public_demo_mode)preparePublicDemo();
    return session;
  });
  window.fetch = async (input,options={}) => {
    const url=new URL(typeof input==='string'?input:input.url,location.origin);
    if(url.origin!==location.origin)return originalFetch(input,options);
    const session=await window.traceSession;
    const headers=new Headers(options.headers || (input instanceof Request?input.headers:undefined));
    const method=(options.method || (input instanceof Request?input.method:'GET')).toUpperCase();
    if(session.public_demo_mode&&!['GET','HEAD','OPTIONS'].includes(method)&&!(method==='POST'&&['/api/boutique/ai/ask','/api/knowledge/ask','/auth/logout'].includes(url.pathname))){
      return new Response(JSON.stringify({detail:readOnlyMessage}),{status:403,headers:{'Content-Type':'application/json'}});
    }
    if(!['GET','HEAD','OPTIONS'].includes(method))headers.set('X-CSRF-Token',session.csrf);
    const response=await originalFetch(input,{...options,headers});
    if(response.status===401)returnToLogin();
    return response;
  };
})();
