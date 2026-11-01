(() => {
  const originalFetch = window.fetch.bind(window);
  function returnToLogin() {
    const next=location.pathname+location.search+location.hash;
    location.replace('/login?next='+encodeURIComponent(next));
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
    return session;
  });
  window.fetch = async (input,options={}) => {
    const url=new URL(typeof input==='string'?input:input.url,location.origin);
    if(url.origin!==location.origin)return originalFetch(input,options);
    const session=await window.traceSession;
    const headers=new Headers(options.headers || (input instanceof Request?input.headers:undefined));
    const method=(options.method || (input instanceof Request?input.method:'GET')).toUpperCase();
    if(!['GET','HEAD','OPTIONS'].includes(method))headers.set('X-CSRF-Token',session.csrf);
    const response=await originalFetch(input,{...options,headers});
    if(response.status===401)returnToLogin();
    return response;
  };
})();
