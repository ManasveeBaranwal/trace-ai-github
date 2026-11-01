(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  let state = null, session = null, loading = false;
  const master = () => state?.role === 'master';
  const ownCompany = () => state?.companies?.[0];
  const count = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const plural = (n, singular, multiple = singular + 's') => `${n} ${n === 1 ? singular : multiple}`;
  const companyById = id => state.companies.find(c => c.id === id);
  const companyUrl = (cid, view) => '/company?company=' + encodeURIComponent(cid) + (view ? '&view=' + view : '');
  const brsrUrl = cid => '/brsr' + (cid ? '?company=' + encodeURIComponent(cid) : '');
  const billUrl = (cid, bid) => '/app?company=' + encodeURIComponent(cid || '') + (bid ? '&bill=' + encodeURIComponent(bid) : '');
  function reportProgress(report) {
    const progress = report.progress;
    if (!progress || progress.required == null || progress.required_reviewed == null) return 'Review progress not available';
    const required = Number(progress.required), reviewed = Number(progress.required_reviewed);
    if (!Number.isFinite(required) || !Number.isFinite(reviewed) || required <= 0) return 'Review progress not available';
    const percentage = progress.completion_percent == null ? null : Number(progress.completion_percent);
    return `${reviewed} / ${required} required entries reviewed` + (Number.isFinite(percentage) ? ` (${percentage}%)` : '');
  }
  function message(id, text = '') { $(id).textContent = text; $(id).hidden = !text; }
  function link(text, href, cls = 'button secondary') { const a = node('a', text, cls); a.href = href; return a; }
  function button(text, handler, cls = 'button secondary') { const b = node('button', text, cls); b.type = 'button'; b.addEventListener('click', handler); return b; }
  function badge(text, active = true) { return node('span', text, 'status-badge' + (active ? '' : ' off')); }
  function empty(target, title, description, action) { const box = node('div', undefined, 'empty-state'); box.append(node('strong', title), node('p', description)); if (action) box.append(action); target.append(box); }
  function managerName(company) { return company.manager_profile?.name || company.manager?.name || company.manager_name || 'Company Manager'; }
  function managerDisabled(company) { return company.manager_profile?.active === 0 || company.manager_profile?.active === false; }
  async function api(url, options = {}) {
    const response = await fetch(url, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
    let result; try { result = await response.json(); } catch (_) { throw new Error('The server returned an unreadable response. Refresh and try again.'); }
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Check the form values and try again.');
    return result;
  }
  const viewCopy = {
    master: {
      overview: ['Founder workspace', 'Your platform, with a clear view.', 'Manage companies, open their Company Manager access, and maintain the sources behind each calculation.'],
      companies: ['Company workspaces', 'The companies you support.', 'Create and manage company records. Existing evidence and reports stay connected to their company.'],
      access: ['Company Manager access', 'A workspace for each company.', 'Set a manager name and enter a company-scoped workspace for the current local phase.'],
      evidence: ['Evidence & records', 'Follow every source.', 'Inspect the documents and activity records available across your company workspaces.'],
      reports: ['BRSR preparation', 'Reporting work, in view.', 'Open company reporting years to collect answers, review evidence and prepare report snapshots.']
    },
    company: {
      overview: ['Company workspace', 'Your company, ready to report.', 'Set up your sources, collect supporting evidence and prepare your BRSR disclosures.'],
      company: ['Company & data sources', 'Start with the right context.', 'Your company details, facilities and reporting periods keep the source data organised.'],
      evidence: ['Evidence & records', 'Keep the source close.', 'Upload bills, review extracted details and maintain the activity records behind your figures.'],
      reports: ['BRSR preparation', 'Build your reporting record.', 'Collect answers and evidence for your company’s selected financial year and reporting boundary.']
    }
  };
  function allowedViews() { return master() ? ['overview', 'companies', 'access', 'evidence', 'reports'] : ['overview', 'company', 'evidence', 'reports']; }
  function show(requested, updateHistory = false) {
    if (!state) return;
    const view = allowedViews().includes(requested) ? requested : 'overview';
    document.querySelectorAll('.view').forEach(section => { section.hidden = section.id !== 'view-' + view; });
    $('workspace-nav').querySelectorAll('[data-view]').forEach(a => { if (a.dataset.view === view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
    const copy = viewCopy[master() ? 'master' : 'company'][view];
    $('page-eyebrow').textContent = copy[0]; $('page-title').textContent = copy[1]; $('page-description').textContent = copy[2];
    if (updateHistory && location.hash !== '#' + view) history.pushState({}, '', '#' + view);
  }
  function navItem(text, href, icon, view) { const a = link('', href, ''); a.append(node('span', icon, 'nav-icon'), node('span', text)); if (view) { a.dataset.view = view; a.addEventListener('click', event => { event.preventDefault(); show(view, true); }); } return a; }
  function renderNav() {
    const nav = $('workspace-nav'); nav.replaceChildren();
    nav.append(navItem('Overview', '#overview', '◈', 'overview'));
    if (master()) nav.append(navItem('Companies', '#companies', '▦', 'companies'), navItem('Company Manager access', '#access', '♙', 'access'));
    else nav.append(navItem('Company & sources', '#company', '▦', 'company'));
    nav.append(navItem('Evidence & records', '#evidence', '▤', 'evidence'), navItem('BRSR reports', '#reports', '≡', 'reports'),
      navItem('Coefficients & AI', '/coefficients', '◎'));
    if (master()) { nav.append(node('div', undefined, 'nav-separator'), navItem('System settings', '/admin?tab=settings', '◎'), navItem('Platform activity', '/admin?tab=audit', '◷')); }
  }
  function metric(label, value, caption) { const card = node('article', undefined, 'metric-card'); card.append(node('span', label), node('strong', String(value)), node('small', caption)); return card; }
  function renderMetrics() {
    const s = state.stats || {}, metrics = $('metrics'); metrics.replaceChildren();
    if (master()) metrics.append(metric('Company workspaces', state.companies.length, 'Your company portfolio'), metric('Evidence documents', state.documents.length, 'Uploaded electricity bills'), metric('BRSR reports', state.reports.length, 'Company, year and boundary'), metric('Bills awaiting review', count(s.needs_review), 'Extraction ready for a person'));
    else metrics.append(metric('Facilities', state.facilities.length, 'Sites and meters configured'), metric('Evidence documents', state.documents.length, 'Your company’s electricity bills'), metric('BRSR reports', state.reports.length, 'Saved reporting workspaces'), metric('Bills awaiting review', count(s.needs_review), 'Check against source evidence'));
  }
  function step(number, title, description, action) { const row = node('div', undefined, 'step-row'), content = node('div', undefined, 'step-content'); content.append(node('strong', title), node('p', description)); row.append(node('span', number, 'step-number'), content); if (action) row.append(action); return row; }
  function renderOverview() {
    const c = ownCompany(), hero = $('hero-actions'); hero.replaceChildren();
    $('hero-label').textContent = master() ? 'SustivioLabs · Super Access' : c?.name || 'Company Manager';
    $('hero-title').textContent = master() ? 'One platform. Two clear workspaces.' : 'Good evidence. Clearer reporting.';
    $('hero-description').textContent = master() ? 'Create a company, give its manager a focused workspace, and keep platform controls in your hands.' : 'Bring your company’s source records together, review the details and prepare answers with supporting evidence.';
    if (master()) hero.append(button('＋ Add company', () => openCompany(), 'button light'), link('Company Manager access →', '#access', 'button ghost'));
    else if (c) hero.append(link('Open BRSR workspace →', brsrUrl(c.id), 'button light'), link('Upload electricity bill', billUrl(c.id), 'button ghost'));
    const steps = $('next-steps'); steps.replaceChildren();
    if (master()) {
      steps.append(step('01', 'Set up a company', plural(state.companies.length, 'company', 'companies') + ' currently available.', button('Add company', () => openCompany(), 'text-link')));
      steps.append(step('02', 'Open Company Manager access', 'Enter a focused workspace with access to one company.', link('Manage access →', '#access', 'text-link')));
      steps.append(step('03', 'Follow reporting progress', plural(state.reports.length, 'BRSR report') + ' currently saved.', link('View reports →', '#reports', 'text-link')));
    } else if (c) {
      steps.append(step('01', 'Set up facilities & periods', plural(state.facilities.length, 'facility', 'facilities') + ' connected to your company.', link('Set up →', companyUrl(c.id, 'organisation'), 'text-link')));
      steps.append(step('02', 'Add and review source evidence', plural(state.documents.length, 'document') + ' in your company workspace.', link('Open evidence →', companyUrl(c.id, 'evidence'), 'text-link')));
      steps.append(step('03', 'Prepare the reporting year', state.reports.length ? 'Continue your saved BRSR answers and review.' : 'Choose a financial year and boundary to begin.', link('Open BRSR →', brsrUrl(c.id), 'text-link')));
    }
    $('responsibility-title').textContent = master() ? 'Platform control' : 'Your company’s records';
    $('responsibility-badge').textContent = master() ? 'Super Access' : 'Company Manager';
    $('responsibility-description').textContent = master() ? 'You can manage every company and the platform-wide calculation defaults.' : 'Your workspace contains only the company records available to this access.';
    const list = $('responsibility-list'); list.replaceChildren();
    const responsibilities = master() ? ['Create and manage company workspaces', 'Name and open Company Manager access', 'Maintain sourced calculation defaults', 'Inspect platform activity and evidence'] : ['Maintain company facilities and reporting periods', 'Upload bills and correct extracted details', 'Review activity records and calculations', 'Prepare BRSR answers and report snapshots'];
    responsibilities.forEach(text => list.append(node('div', text, 'responsibility-item')));
    $('recent-title').textContent = master() ? 'Company workspaces' : 'Your reporting work';
    $('recent-all').textContent = master() ? 'View companies →' : 'View reports →'; $('recent-all').href = master() ? '#companies' : '#reports';
    const recent = $('recent-work'); recent.replaceChildren();
    if (master()) {
      if (!state.companies.length) empty(recent, 'Your first company starts here.', 'Create a company to begin collecting evidence and preparing its reporting workspace.', button('＋ Add company', () => openCompany(), 'button primary'));
      for (const company of state.companies.slice(0, 4)) {
        const row = node('div', undefined, 'record-row'), detail = node('div'), actions = node('div', undefined, 'actions'); detail.append(node('strong', company.name), node('p', [company.industry, company.country].filter(Boolean).join(' · ') || 'Company details not set'));
        actions.append(link('BRSR workspace', brsrUrl(company.id), 'text-link'), enterButton(company)); row.append(detail, actions); recent.append(row);
      }
    } else if (state.reports.length) for (const report of state.reports.slice(0, 4)) { const row = node('div', undefined, 'record-row'), detail = node('div'); detail.append(node('strong', report.name || ('FY ' + report.financial_year)), node('p', `${report.financial_year} · ${report.boundary} · ${reportProgress(report)}`)); row.append(detail, link('Continue →', brsrUrl(c.id) + '&report=' + encodeURIComponent(report.id), 'text-link')); recent.append(row); }
    else if (c) empty(recent, 'Choose your first reporting year.', 'The BRSR workspace will guide you through company, year and reporting boundary before you start answering disclosures.', link('Open BRSR workspace →', brsrUrl(c.id), 'button primary'));
    else empty(recent, 'No active company is available.', 'This access needs an active company assignment.');
  }
  function enterButton(company) {
    const b = button('Open Company Manager →', async () => {
      b.disabled = true; message('page-error');
      try { const data = await api('/api/initial/companies/' + encodeURIComponent(company.id) + '/enter', { method: 'POST' }); location.assign(data.home || '/company-manager'); }
      catch (error) { message('page-error', error.message); b.disabled = false; }
    }, 'button primary');
    b.disabled = !company.active || managerDisabled(company) || !state.local_access; b.title = !company.active ? 'Activate this company to open its access.' : managerDisabled(company) ? 'Company Manager access is disabled.' : !state.local_access ? 'Passwordless company entry is available only in the local development mode.' : 'Switch this browser into this company’s Company Manager access.';
    return b;
  }
  function companyStats(company) { const box = node('div', undefined, 'company-stats'); for (const [value, label] of [[company.document_count, 'Documents'], [company.facility_count, 'Facilities'], [company.report_count, 'BRSR reports']]) { const d = node('div'); d.append(node('strong', String(count(value))), node('small', label)); box.append(d); } return box; }
  function renderCompanies() {
    const target = $('companies-grid'), q = $('company-search').value.toLowerCase(); target.replaceChildren();
    const companies = state.companies.filter(c => [c.name, c.industry, c.country].join(' ').toLowerCase().includes(q));
    if (!companies.length) empty(target, state.companies.length ? 'No matching companies.' : 'Create your first company.', state.companies.length ? 'Try another company name, industry or country.' : 'Its facilities, evidence and BRSR work will stay together in one workspace.', state.companies.length ? null : button('＋ Add company', () => openCompany(), 'button primary'));
    for (const company of companies) { const card = node('article', undefined, 'company-card'), actions = node('div', undefined, 'actions'); card.append(badge(company.active ? 'Active company' : 'Inactive company', company.active), node('h2', company.name), node('p', [company.industry, company.country].filter(Boolean).join(' · ') || 'Company details not set', 'muted'), companyStats(company)); actions.append(link('Open BRSR →', brsrUrl(company.id), 'button primary'), button('Manage company', () => openCompany(company))); card.append(actions); target.append(card); }
  }
  function renderAccess() {
    const target = $('access-grid'); target.replaceChildren();
    if (!state.companies.length) empty(target, 'Add a company before setting up its access.', 'Each Company Manager workspace belongs to a specific company.', button('＋ Add company', () => openCompany(), 'button primary'));
    for (const company of state.companies) { const card = node('article', undefined, 'company-card'), actions = node('div', undefined, 'actions'); card.append(badge(!company.active ? 'Company inactive' : managerDisabled(company) ? 'Manager access disabled' : 'Company-scoped access', company.active && !managerDisabled(company)), node('h2', company.name), node('div', managerName(company), 'manager-name'), node('p', 'Can work with this company’s sources, evidence, calculations and BRSR reporting records.', 'scope-text')); actions.append(enterButton(company), button('Set manager name', () => openManager(company))); card.append(actions); target.append(card); }
  }
  function renderCompanyProfile() {
    const c = ownCompany(), target = $('company-profile'), actions = $('company-profile-actions'); target.replaceChildren(); actions.replaceChildren(); $('company-tools').replaceChildren(); if (!c) { empty(target, 'No company is available.', 'An active company assignment is required for this workspace.'); return; }
    target.append(node('h2', c.name), badge(c.active ? 'Active company' : 'Inactive company', c.active)); const dl = node('dl', undefined, 'profile-grid');
    for (const [label, value] of [['Industry', c.industry || 'Not set'], ['Country / region', c.country || 'Not set'], ['Manager', session.user.name || managerName(c)]]) { const d = node('div'); d.append(node('dt', label), node('dd', value)); dl.append(d); } target.append(dl);
    actions.append(link('Manage facilities & periods →', companyUrl(c.id, 'organisation'), 'button primary'));
    const tools = [['01', 'Facilities & meters', 'Identify the office, plant or meter responsible for your activity records.', 'Manage sources →', companyUrl(c.id, 'organisation')], ['02', 'Source evidence', 'Bring electricity bills and supporting records into your company evidence inbox.', 'Open evidence →', companyUrl(c.id, 'evidence')], ['03', 'Activity inventory', 'Maintain quantities, sourced factors and reviewable calculations for your company.', 'Open inventory →', companyUrl(c.id, 'inventory')]];
    for (const [number, title, text, label, href] of tools) { const card = node('article', undefined, 'source-card'); card.append(node('span', number, 'tool-number'), node('h3', title), node('p', text), link(label, href, 'text-link')); $('company-tools').append(card); }
  }
  function table(target, headers, rows, title, description) { target.replaceChildren(); if (!rows.length) { empty(target, title, description); return; } const t = node('table'), head = node('thead'), tr = node('tr'), body = node('tbody'); headers.forEach(h => { const th = node('th', h); th.scope = 'col'; tr.append(th); }); head.append(tr); t.append(head); for (const cells of rows) { const row = node('tr'); for (const value of cells) { const td = node('td'); td.append(typeof value === 'string' ? document.createTextNode(value) : value); row.append(td); } body.append(row); } t.append(body); target.append(t); }
  function renderEvidence() {
    const actions = $('evidence-actions'); actions.replaceChildren(); if (!master() && ownCompany()) actions.append(link('Upload electricity bill →', billUrl(ownCompany().id), 'button primary'), link('Open inventory', companyUrl(ownCompany().id, 'inventory')));
    const documentRows = state.documents.map(d => { const detail = node('div'); detail.append(node('strong', d.filename || 'Untitled document')); if (master()) detail.append(node('small', companyById(d.company_id)?.name || 'Unassigned')); return [detail, badge((d.status || 'unknown').replaceAll('_', ' '), d.status === 'reviewed'), link('Open evidence →', billUrl(d.company_id, d.id), 'text-link')]; });
    table($('documents-table'), ['Document', 'Extraction / review', 'Action'], documentRows, 'No electricity bills yet.', 'Upload a bill to extract and review its consumption details.');
    const activityRows = state.activities.map(a => { const detail = node('div'); detail.append(node('strong', a.title || 'Activity')); if (master()) detail.append(node('small', companyById(a.company_id)?.name || 'Company record')); return [detail, `${a.quantity ?? '—'} ${a.unit || ''}`, badge(a.status || 'draft', a.status === 'reviewed'), link('Open record →', companyUrl(a.company_id, 'inventory'), 'text-link')]; });
    table($('activities-table'), ['Activity', 'Quantity', 'Status', 'Action'], activityRows, 'No activity records yet.', 'Configure a facility and reporting period in the company workspace, then add your first activity.');
  }
  function renderReports() {
    const actions = $('reports-actions'); actions.replaceChildren(); if (!master() && ownCompany()) actions.append(link('Open BRSR workspace →', brsrUrl(ownCompany().id), 'button primary')); else actions.append(link('Choose company & reporting year →', '/brsr', 'button primary'));
    const rows = state.reports.map(r => { const detail = node('div'); detail.append(node('strong', r.name || ('FY ' + r.financial_year))); if (master()) detail.append(node('small', companyById(r.company_id)?.name || 'Company record')); return [detail, r.financial_year || '—', r.boundary || '—', reportProgress(r), link('Continue →', brsrUrl(r.company_id) + '&report=' + encodeURIComponent(r.id), 'text-link')]; });
    table($('reports-table'), ['Reporting workspace', 'Financial year', 'Boundary', 'Required entries reviewed', 'Action'], rows, 'No BRSR reporting workspace yet.', 'Choose a company, financial year and boundary in the BRSR workspace. Your answers and evidence will be retained there.');
  }
  function render() {
    const label = master() ? 'Super Access' : 'Company Manager'; document.title = label + ' · Trace AI'; document.body.dataset.role = state.role;
    $('access-label').textContent = label; $('breadcrumb-role').textContent = label; $('scope-label').textContent = master() ? 'Platform control' : ownCompany()?.name || 'Company-scoped workspace'; $('scope-note').textContent = master() ? 'Platform-wide access' : 'Your company only';
    $('brand-home').href = session.home; renderNav(); renderMetrics(); renderOverview();
    if (master()) { renderCompanies(); renderAccess(); } else renderCompanyProfile();
    renderEvidence(); renderReports(); $('workspace-content').hidden = false; $('loading-state').hidden = true; $('refresh').disabled = false; show(location.hash.slice(1));
  }
  async function load() { if (loading) return; loading = true; $('refresh').disabled = true; message('page-error'); try { state = await api('/api/initial/overview'); state.companies ||= []; state.facilities ||= []; state.documents ||= []; state.activities ||= []; state.reports ||= []; render(); } catch (error) { message('page-error', error.message); $('loading-state').hidden = true; $('refresh').disabled = false; } finally { loading = false; } }
  function openCompany(company) { $('company-form').reset(); $('company-id').value = company?.id || ''; $('company-dialog-title').textContent = company ? 'Manage company' : 'Add company'; $('company-name').value = company?.name || ''; $('company-industry').value = company?.industry || ''; $('company-country').value = company?.country || ''; $('company-active').checked = company ? Boolean(company.active) : true; $('company-form').querySelector('.form-error').hidden = true; $('company-dialog').showModal(); }
  function openManager(company) { $('manager-form').reset(); $('manager-company-id').value = company.id; $('manager-company-name').textContent = company.name; $('manager-name').value = managerName(company); $('manager-form').querySelector('.form-error').hidden = true; $('manager-dialog').showModal(); }
  function form(id, handler) { $(id).addEventListener('submit', async event => { event.preventDefault(); if (!master()) return; const submit = event.submitter, error = event.target.querySelector('.form-error'); submit.disabled = true; error.hidden = true; try { await handler(); event.target.closest('dialog').close(); await load(); message('page-feedback', 'Saved. Your company records are up to date.'); } catch (err) { error.textContent = err.message; error.hidden = false; } finally { submit.disabled = false; } }); }
  form('company-form', async () => { const id = $('company-id').value, body = { name: $('company-name').value, industry: $('company-industry').value, country: $('company-country').value, active: $('company-active').checked }; if (!id) body.firm_id = null; await api('/api/access/companies' + (id ? '/' + encodeURIComponent(id) : ''), { method: id ? 'PUT' : 'POST', body: JSON.stringify(body) }); show('companies', true); });
  form('manager-form', async () => { const company = companyById($('manager-company-id').value); await api('/api/initial/companies/' + encodeURIComponent($('manager-company-id').value) + '/manager', { method: 'PUT', body: JSON.stringify({ name: $('manager-name').value, active: !managerDisabled(company) }) }); });
  $('add-company').addEventListener('click', () => openCompany()); $('refresh').addEventListener('click', load); $('company-search').addEventListener('input', () => { if (master()) renderCompanies(); }); document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => $(b.dataset.close).close()));
  window.addEventListener('hashchange', () => show(location.hash.slice(1))); window.addEventListener('popstate', () => show(location.hash.slice(1)));
  window.traceSession.then(async identity => { session = identity; await load(); }).catch(error => { message('page-error', error.message); $('loading-state').hidden = true; });
})();
