'use strict';
const $ = id => document.getElementById(id);
const make = (tag, text, cls) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
};
const principles = {
  '1': 'Ethics & responsible governance', '2': 'Sustainable products & services',
  '3': 'Employee & worker wellbeing', '4': 'Stakeholder engagement',
  '5': 'Human rights', '6': 'Environment', '7': 'Responsible policy advocacy',
  '8': 'Inclusive growth', '9': 'Consumer responsibility'
};
const sectionNames = {A: 'General disclosures', B: 'Management & processes', C: 'Principle-wise performance'};
const viewTitles = {
  overview: ['A clearer path to your BRSR.', 'Collect source data, review disclosures and prepare a traceable reporting draft.'],
  collection: ['Your disclosure workspace.', 'Enter the data, explain the method and connect each response to its source.'],
  reports: ['Review first. Report with context.', 'Resolve gaps, preserve a snapshot and export a reporting draft with its supporting records.'],
  assignments: ['Every disclosure needs an owner.', 'Assign company data-entry people to the records they are responsible for preparing.']
};
let identity, companies = [], reports = [], detail = null, access = null, bills = [];
let companyId = '', reportId = '', currentView = 'overview', requestVersion = 0;
let editingItem = null, editingAnswer = null, dirty = false, saving = false;
let fieldReaders = new Map();
const isEntry = () => identity?.user.role === 'data_entry';
const portfolioRole = () => ['master', 'consultant_manager', 'consultant'].includes(identity?.user.role);
const mayCreate = () => ['master', 'consultant_manager', 'consultant', 'company_admin', 'company_user'].includes(identity?.user.role);
const items = () => detail?.catalog?.items || [];
const answerFor = id => detail?.answers.find(answer => answer.question_id === id);
const sectionOf = item => String(item.section || '').replace(/^Section\s*/i, '').toUpperCase().slice(0, 1);
const principleOf = item => String(item.principle || '').replace(/^P/i, '');
const itemTitle = item => item.title || item.question || item.id;
const statusOf = answer => answer?.status || 'missing';
const statusLabel = answer => answer?.applicability === 'not_applicable' ? 'Not applicable · ' + statusOf(answer) : statusOf(answer);
const canEdit = id => Boolean(detail?.permissions.editable_question_ids?.includes(id));
const safeHttp = value => { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch { return null; } };
function message(id, text) { $(id).textContent = text; $(id).hidden = !text; }
async function api(url, options = {}) {
  const response = await fetch(url, {...options, headers: {'Content-Type': 'application/json', ...options.headers}});
  let data;
  try { data = await response.json(); } catch { throw new Error('The server returned an unreadable response. Refresh and try again.'); }
  if (!response.ok) {
    const details = typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map(issue => issue.msg || 'Invalid value').join('; ') : 'Check the values and try again.';
    throw new Error(details);
  }
  return data;
}
function button(text, action, cls = 'secondary') { const node = make('button', text, cls); node.type = 'button'; node.addEventListener('click', action); return node; }
function statusBadge(answer) { return make('span', statusLabel(answer), 'status-tag ' + statusOf(answer)); }
function link(text, href, cls = 'text-button') { const node = make('a', text, cls); node.href = href; return node; }
function setOptions(select, list, selected, placeholder) {
  select.replaceChildren(new Option(placeholder, ''));
  list.forEach(item => select.add(new Option(item.name, item.id)));
  if (list.some(item => item.id === selected)) select.value = selected;
}
function show(view, updateUrl = true) {
  if (!viewTitles[view]) view = 'overview';
  if (view === 'assignments' && !detail?.permissions.can_assign) view = 'overview';
  currentView = view;
  document.querySelectorAll('.brsr-view').forEach(node => node.hidden = node.id !== 'view-' + view);
  document.querySelectorAll('[data-view]').forEach(node => {
    const selected = node.dataset.view === view;
    node.classList.toggle('selected', selected);
    if (selected) node.setAttribute('aria-current', 'page'); else node.removeAttribute('aria-current');
  });
  $('page-title').textContent = viewTitles[view][0];
  $('page-description').textContent = viewTitles[view][1];
  if (updateUrl) syncUrl();
}
function syncUrl() {
  const params = new URLSearchParams();
  if (companyId) params.set('company', companyId);
  if (reportId) params.set('report', reportId);
  const destination = '/brsr' + (params.size ? '?' + params : '') + (currentView === 'overview' ? '' : '#' + currentView);
  if (location.pathname + location.search + location.hash !== destination) history.replaceState({}, '', destination);
}
function emptyWorkspace(title, copy, create = false) {
  $('workspace').hidden = true;
  $('empty-workspace').hidden = false;
  $('empty-title').textContent = title;
  $('empty-copy').textContent = copy;
  $('empty-create').hidden = !create;
}
function setCompanyHeading() {
  $('sidebar-company').textContent = companies.find(company => company.id === companyId)?.name || 'Choose a company';
}
async function chooseCompany(id, preferredReport = '') {
  const version = ++requestVersion;
  companyId = id;
  reportId = '';
  detail = null;
  emptyWorkspace('Loading reporting records…', 'Connecting this company to its reporting years.');
  message('page-error', '');
  setCompanyHeading();
  $('new-report').hidden = !companyId || !mayCreate();
  $('assignments-nav').hidden = true;
  setOptions($('report-select'), [], '', 'Choose reporting year');
  if (!companyId) { emptyWorkspace('Choose a company workspace.', 'Available companies are limited to your account’s access.'); syncUrl(); return; }
  try {
    const loaded = await api('/api/brsr/reports?company_id=' + encodeURIComponent(companyId));
    if (version !== requestVersion) return;
    reports = Array.isArray(loaded) ? loaded : loaded.reports || [];
    setOptions($('report-select'), reports.map(report => ({id: report.id, name: (report.financial_year || '') + ' · ' + report.name})), '', 'Choose reporting year');
    const selected = reports.some(report => report.id === preferredReport) ? preferredReport : reports[0]?.id;
    if (selected) { $('report-select').value = selected; await chooseReport(selected); }
    else { emptyWorkspace('Start your first BRSR reporting year.', mayCreate() ? 'Define the financial year and reporting boundary, then collect the data section by section.' : 'A reporting year will appear here when it is available to your account.', mayCreate()); syncUrl(); }
    if (mayCreate()) {
      api('/api/access/overview').then(data => { if (companyId === id) { access = data; if (detail) renderAssignments(); } }).catch(() => { access = null; });
    }
    api('/api/bills').then(data => { if (companyId === id) bills = data.filter(bill => bill.company_id === id); }).catch(() => { bills = []; });
  } catch (error) { if (version === requestVersion) { emptyWorkspace('This reporting workspace could not be loaded.', 'Your existing records have not been changed.'); message('page-error', error.message); } }
}
async function chooseReport(id) {
  const version = ++requestVersion;
  reportId = id;
  detail = null;
  emptyWorkspace('Loading reporting year…', 'Retrieving this year’s disclosure records and review state.');
  message('page-error', '');
  if (!id) { detail = null; emptyWorkspace('Choose a reporting year.', 'Each year keeps its own reporting boundary, responses, reviews and snapshots.'); syncUrl(); return; }
  try {
    const loaded = await api('/api/brsr/reports/' + encodeURIComponent(id));
    if (version !== requestVersion) return;
    detail = loaded;
    if (detail.report.company_id !== companyId) throw new Error('The report does not belong to the selected company.');
    detail.answers ||= [];
    detail.assignments ||= [];
    detail.snapshots ||= [];
    $('workspace').hidden = false;
    $('empty-workspace').hidden = true;
    render();
    syncUrl();
  } catch (error) { if (version === requestVersion) { emptyWorkspace('This reporting year could not be loaded.', 'Choose another year or refresh the workspace.'); message('page-error', error.message); } }
}
function applyRoleCopy() {
  const contributor = isEntry();
  $('report-view-nav').textContent = contributor ? '◷  Submission status' : '▤  Review & reports';
  $('report-view-action').textContent = contributor ? 'View submissions' : 'Review & export';
  if (contributor) viewTitles.reports = ['Your submission status.', 'Follow your assigned disclosure records from draft to submitted and reviewed.'];
  $('review-queue-eyebrow').textContent = contributor ? 'YOUR ASSIGNED RECORDS' : 'REVIEW BEFORE EXPORT';
  $('review-queue-title').textContent = contributor ? 'Your disclosure status' : 'What still needs attention?';
  $('review-queue-note').textContent = contributor ? 'Open an assigned disclosure to see its saved data and current status. Keep source references connected to every submission.' : 'A saved value remains a draft until it is reviewed. Not-applicable responses need a reason and review.';
}
function progressFor(records) {
  return {total: records.length, reviewed: records.filter(item => statusOf(answerFor(item.id)) === 'reviewed' && answerFor(item.id).applicability !== 'missing').length};
}
function render() {
  applyRoleCopy();
  const profile = detail.report;
  $('report-summary').textContent = [companies.find(company => company.id === companyId)?.name, 'FY ' + profile.financial_year, profile.boundary + ' boundary', 'Previous FY ' + profile.previous_financial_year].join(' · ');
  const progress = detail.progress || {};
  const total = progress.total ?? items().length;
  const requiredItems = items().filter(item => item.required);
  const optionalItems = items().filter(item => !item.required);
  const required = progress.required ?? requiredItems.length;
  const reviewedRequired = progress.required_reviewed ?? requiredItems.filter(item => statusOf(answerFor(item.id)) === 'reviewed' && answerFor(item.id).applicability !== 'missing').length;
  const reviewed = progress.reviewed ?? items().filter(item => statusOf(answerFor(item.id)) === 'reviewed').length;
  const percentage = Number(progress.completion_percent ?? (required ? reviewedRequired / required * 100 : 0));
  $('completion-percent').textContent = Number.isFinite(percentage) ? Math.round(percentage) + '%' : '0%';
  $('completion-count').textContent = reviewedRequired + ' of ' + required + ' essential/general entries reviewed';
  $('optional-count').textContent = optionalItems.length + ' optional leadership entries tracked separately';
  $('metric-total').textContent = total;
  $('metric-reviewed').textContent = reviewed;
  $('metric-submitted').textContent = progress.submitted ?? detail.answers.filter(answer => answer.status === 'submitted').length;
  $('metric-missing').textContent = requiredItems.filter(item => statusOf(answerFor(item.id)) !== 'reviewed' || answerFor(item.id)?.applicability === 'missing').length;
  $('metric-missing-note').textContent = optionalItems.length + ' optional leadership entries are not in this count';
  $('catalog-version').textContent = required + ' essential/general · ' + optionalItems.length + ' optional leadership · ' + (detail.catalog.version || 'Versioned catalogue');
  $('scope-note').textContent = detail.scope_note || detail.catalog.coverage_note || 'This workspace prepares disclosure records and reporting drafts. Source-format completeness remains part of review.';
  $('assignments-nav').hidden = !detail.permissions.can_assign;
  $('new-report').hidden = !mayCreate();
  $('snapshot-create-card').hidden = !detail.permissions.can_export;
  $('snapshot-list-card').hidden = !detail.permissions.can_export;
  $('review-report-grid').classList.toggle('submissions-only', !detail.permissions.can_export);
  $('create-snapshot').hidden = !detail.permissions.can_export;
  $('snapshot-confirm').closest('label').hidden = !detail.permissions.can_export;
  $('snapshot-message').textContent = detail.permissions.can_export ? 'Incomplete snapshots remain clearly labelled as preparation drafts.' : 'Snapshots are prepared through the reporting review workflow.';
  renderOverview();
  renderDisclosures();
  renderReview();
  renderSnapshots();
  renderAssignments();
  show(currentView, false);
}
function renderOverview() {
  $('section-progress').replaceChildren();
  for (const section of ['A', 'B', 'C']) {
    const sectionItems = items().filter(item => sectionOf(item) === section);
    const coverage = progressFor(sectionItems.filter(item => item.required));
    const optionalCount = sectionItems.filter(item => !item.required).length;
    const row = make('div', undefined, 'section-item');
    const top = make('div', undefined, 'section-item-top');
    const title = make('div', section + ' · ' + sectionNames[section]);
    title.append(make('small', coverage.total + ' essential/general · ' + optionalCount + ' optional leadership'));
    top.append(title, button(coverage.reviewed + '/' + coverage.total + ' reviewed ↗', () => { $('section-filter').value = section; $('principle-filter').value = ''; show('collection'); renderDisclosures(); }, 'text-button'));
    const track = make('div', undefined, 'coverage-track'), fill = make('i');
    fill.style.width = (coverage.total ? coverage.reviewed / coverage.total * 100 : 0) + '%';
    track.append(fill); row.append(top, track); $('section-progress').append(row);
  }
  $('next-actions').replaceChildren();
  const requiredIds = new Set(items().filter(item => item.required).map(item => item.id));
  const drafts = detail.answers.filter(answer => answer.status === 'submitted' && requiredIds.has(answer.question_id));
  const missing = items().filter(item => item.required && !answerFor(item.id));
  const tasks = isEntry() ? [
    [missing.length, 'Prepare assigned data', missing.length + ' essential/general records without saved responses', () => { $('status-filter').value = 'missing'; show('collection'); renderDisclosures(); }],
    [detail.answers.filter(answer => answer.status === 'submitted').length, 'Follow submissions', 'See draft, submitted and reviewed statuses', () => show('reports')],
    [detail.answers.filter(answer => answer.status === 'reviewed').length, 'View reviewed records', 'Your assigned records with completed review', () => { $('status-filter').value = 'reviewed'; show('collection'); renderDisclosures(); }]
  ] : [
    [drafts.length, 'Review submitted disclosures', drafts.length + ' records awaiting review', () => { $('status-filter').value = 'submitted'; show('collection'); renderDisclosures(); }],
    [missing.length, 'Fill remaining data gaps', missing.length + ' records without saved responses', () => { $('status-filter').value = 'missing'; show('collection'); renderDisclosures(); }],
    [detail.snapshots.length, 'Prepare a report version', detail.snapshots.length + ' snapshots saved for this year', () => show('reports')]
  ];
  for (const [count, title, caption, action] of tasks) {
    const row = make('div', undefined, 'task-item'), content = make('div', title);
    content.append(make('small', caption)); row.append(make('span', String(count)), content, button('Open', action, 'text-button')); $('next-actions').append(row);
  }
  $('principle-grid').replaceChildren();
  for (const [number, name] of Object.entries(principles)) {
    const principleItems = items().filter(item => sectionOf(item) === 'C' && principleOf(item) === number);
    const coverage = progressFor(principleItems.filter(item => item.required));
    const optionalCount = principleItems.filter(item => !item.required).length;
    if (!principleItems.length && isEntry()) continue;
    const card = button('', () => { $('section-filter').value = 'C'; $('principle-filter').value = number; $('status-filter').value = ''; show('collection'); renderDisclosures(); }, 'principle-card');
    card.append(make('span', 'PRINCIPLE ' + number), make('strong', name), make('small', coverage.reviewed + '/' + coverage.total + ' essential/general reviewed · ' + optionalCount + ' optional'));
    $('principle-grid').append(card);
  }
}
function matchesFilters(item) {
  if (!item.required && !$('leadership-filter').checked) return false;
  const query = $('disclosure-search').value.trim().toLowerCase();
  const answer = answerFor(item.id), status = statusOf(answer), requestedStatus = $('status-filter').value;
  const search = [item.id, itemTitle(item), item.description || '', ...(item.fields || []).map(field => field.label)].join(' ').toLowerCase();
  return (!query || search.includes(query)) && (!$('section-filter').value || sectionOf(item) === $('section-filter').value) && (!$('principle-filter').value || principleOf(item) === $('principle-filter').value) && (!requestedStatus || (requestedStatus === 'not_applicable' ? answer?.applicability === 'not_applicable' : status === requestedStatus)) && (!$('core-filter').checked || Boolean(item.core));
}
function renderDisclosures() {
  if (!detail) return;
  const matching = items().filter(matchesFilters);
  const includeOptional = $('leadership-filter').checked;
  const eligible = items().filter(item => includeOptional || item.required).length;
  const optionalCount = items().filter(item => !item.required).length;
  $('result-count').textContent = matching.length + ' of ' + eligible + (includeOptional ? ' disclosure records' : ' essential/general records') + (isEntry() ? ' assigned to you' : '') + (!includeOptional && optionalCount ? ' · ' + optionalCount + ' optional leadership entries hidden' : '');
  $('disclosure-list').replaceChildren();
  if (!matching.length) {
    const empty = make('section', undefined, 'card');
    empty.append(make('h2', isEntry() && !items().length ? 'No disclosures assigned yet.' : 'No matching disclosures.'), make('p', isEntry() && !items().length ? 'Your assigned reporting records will appear here.' : 'Change the filters or search to find a disclosure.', 'subtle'));
    $('disclosure-list').append(empty); return;
  }
  for (const item of matching) {
    const answer = answerFor(item.id), row = make('article', undefined, 'disclosure-row'), content = make('div');
    content.append(make('span', item.id, 'disclosure-code'), make('h2', itemTitle(item), 'disclosure-title'));
    const tags = make('div', undefined, 'disclosure-tags');
    tags.append(statusBadge(answer), make('small', item.required === false ? 'Leadership / optional' : 'Essential / tracked'));
    if (item.core) tags.append(make('span', 'Core-tagged', 'core-tag'));
    if (item.principle) tags.append(make('small', 'P' + principleOf(item)));
    content.append(tags);
    row.append(make('span', sectionOf(item), 'section-letter'), content, button(canEdit(item.id) ? 'Open disclosure ↗' : 'View disclosure ↗', () => openAnswer(item), 'secondary'));
    $('disclosure-list').append(row);
  }
}
function renderReview() {
  $('review-queue').replaceChildren();
  const pending = isEntry() ? items() : items().filter(item => item.required && statusOf(answerFor(item.id)) !== 'reviewed');
  if (!pending.length) $('review-queue').append(make('p', isEntry() ? 'No disclosure records are assigned to your account for this reporting year yet.' : 'All essential/general entries in this workspace are reviewed. Optional leadership entries remain available through Data collection. Check the reporting scope and source guidance before using an export.', 'subtle'));
  for (const item of pending.slice(0, 8)) {
    const row = make('div', undefined, 'review-queue-item'), content = make('div');
    content.append(make('strong', itemTitle(item)), make('small', item.id + ' · ' + statusLabel(answerFor(item.id))));
    row.append(content, button('Open', () => openAnswer(item), 'text-button')); $('review-queue').append(row);
  }
  if (pending.length > 8) $('review-queue').append(button('View all ' + pending.length + (isEntry() ? ' assigned records ↗' : ' unfinished records ↗'), () => { $('status-filter').value = ''; show('collection'); }, 'text-button'));
}
function renderSnapshots() {
  $('snapshot-list').replaceChildren();
  if (!detail.snapshots.length) { $('snapshot-list').append(make('p', 'No snapshots yet. Save a version when you are ready to review or share your preparation draft.', 'subtle')); return; }
  for (const snapshot of [...detail.snapshots].sort((a, b) => b.created_at.localeCompare(a.created_at))) {
    const row = make('div', undefined, 'snapshot-item'), content = make('div');
    content.append(make('strong', new Date(snapshot.created_at).toLocaleString()), make('p', (snapshot.complete ? 'Tracked required records reviewed' : 'Preparation draft · missing or unreviewed records') + ' · ' + (snapshot.catalog_version || detail.catalog.version)));
    row.append(content);
    if (detail.permissions.can_export) {
      const links = make('div', undefined, 'snapshot-links');
      for (const format of ['html', 'json', 'csv']) {
        const anchor = link(format === 'html' ? 'Open report ↗' : format.toUpperCase() + ' ↓', '/api/brsr/snapshots/' + encodeURIComponent(snapshot.id) + '/export?format=' + format, 'secondary');
        if (format === 'html') { anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; }
        links.append(anchor);
      }
      row.append(links);
    }
    $('snapshot-list').append(row);
  }
}
function renderAssignments() {
  if (!detail?.permissions.can_assign) return;
  const people = detail.contributors?.length ? detail.contributors.filter(user => user.active) : (access?.users || []).filter(user => user.role === 'data_entry' && user.company_id === companyId && user.active);
  const selected = $('assignment-user').value;
  setOptions($('assignment-user'), people, selected, 'Choose a data-entry person');
  if (!selected && people.length === 1) $('assignment-user').value = people[0].id;
  renderAssignmentChoices();
}
function renderAssignmentChoices() {
  $('assignment-options').replaceChildren();
  const uid = $('assignment-user').value;
  const record = detail?.assignments.find(assignment => assignment.user_id === uid);
  const assigned = new Set(record?.question_ids || []);
  if (!uid) { $('assignment-options').append(make('p', 'Choose a company data-entry person. People must already have an account in this company.', 'subtle')); updateAssignmentCount(); return; }
  for (const item of items()) {
    const label = make('label', undefined, 'assignment-option'), input = make('input');
    input.type = 'checkbox'; input.value = item.id; input.checked = assigned.has(item.id); input.addEventListener('change', updateAssignmentCount);
    const text = make('span', itemTitle(item)); text.append(make('small', item.id + ' · Section ' + sectionOf(item)));
    label.append(input, text); $('assignment-options').append(label);
  }
  updateAssignmentCount();
}
function updateAssignmentCount() { $('assignment-count').textContent = $('assignment-options').querySelectorAll('input:checked').length + ' disclosures selected'; }
function createReportDialog() {
  $('report-form').reset();
  const now = new Date(), startYear = now.getMonth() >= 3 ? now.getFullYear() : now.getFullYear() - 1;
  const financialYear = startYear + '-' + String(startYear + 1).slice(-2);
  $('report-year').value = financialYear;
  $('report-previous').value = startYear - 1 + '-' + String(startYear).slice(-2);
  $('report-start').value = startYear + '-04-01';
  $('report-end').value = startYear + 1 + '-03-31';
  $('report-name').value = 'BRSR · FY ' + financialYear;
  $('report-form').querySelector('.form-error').hidden = true;
  $('report-dialog').showModal();
}
function dirtyEditor() { dirty = true; $('answer-dirty').hidden = false; $('review-answer').disabled = true; }
function createInput(field, value, disabled = false) {
  let input;
  if (field.type === 'boolean') {
    input = make('select'); input.add(new Option('Not yet provided', '')); input.add(new Option('Yes', 'true')); input.add(new Option('No', 'false'));
    input.value = value === true ? 'true' : value === false ? 'false' : '';
  } else if (field.type === 'select') {
    input = make('select'); input.add(new Option('Choose a value', ''));
    for (const option of field.options || []) input.add(new Option(typeof option === 'string' ? option : option.label, typeof option === 'string' ? option : option.value));
    input.value = value ?? '';
  } else if (field.type === 'textarea') {
    input = make('textarea'); input.rows = field.rows || 4; input.value = value ?? ''; input.maxLength = field.max_length || 20000;
  } else {
    input = make('input');
    input.type = ['number', 'integer'].includes(field.type) ? 'number' : ['date', 'url', 'email'].includes(field.type) ? field.type : 'text';
    if (input.type === 'number') { input.step = field.type === 'integer' ? '1' : 'any'; if (field.min !== undefined) input.min = field.min; if (field.max !== undefined) input.max = field.max; }
    else input.maxLength = field.max_length || 4000;
    input.value = value ?? '';
  }
  input.disabled = disabled;
  input.addEventListener('input', dirtyEditor);
  input.addEventListener('change', dirtyEditor);
  const read = () => field.type === 'boolean' ? input.value === '' ? null : input.value === 'true' : input.value === '' ? null : input.value;
  return {input, read};
}
function renderTableField(field, value, disabled) {
  const wrap = make('div', undefined, 'field-table'), heading = make('h3', field.label || field.id);
  if (field.unit) heading.append(make('span', field.unit, 'field-unit'));
  wrap.append(heading);
  const columns = field.columns || [];
  if (!columns.length) {
    const input = make('textarea'); input.rows = 6; input.value = value === null || value === undefined ? '' : JSON.stringify(value, null, 2); input.disabled = disabled;
    input.setAttribute('aria-label', field.label || field.id);
    input.placeholder = 'Enter table rows as a JSON array. Example structure is defined by the source guidance.';
    input.addEventListener('input', dirtyEditor); wrap.append(input, make('p', 'Structured table data is saved as rows. Leave unknown data blank.', 'field-description'));
    fieldReaders.set(field.id, () => { if (!input.value.trim()) return null; const parsed = JSON.parse(input.value); if (!Array.isArray(parsed)) throw new Error((field.label || field.id) + ' must contain an array of table rows.'); return parsed; });
    return wrap;
  }
  const tableWrap = make('div', undefined, 'table-wrap'), table = make('table'), head = make('tr'), body = make('tbody');
  columns.forEach(column => head.append(make('th', (column.label || column.id) + (column.unit ? ' · ' + column.unit : ''))));
  if (!disabled) head.append(make('th', ''));
  const thead = make('thead'); thead.append(head); table.append(thead, body); tableWrap.append(table); wrap.append(tableWrap);
  const readers = [];
  function addRow(initial = {}) {
    const row = make('tr'), rowReaders = new Map();
    for (const column of columns) {
      const cell = make('td'), {input, read} = createInput(column, initial[column.id], disabled);
      input.setAttribute('aria-label', (field.label || field.id) + ' · ' + (column.label || column.id));
      rowReaders.set(column.id, read); cell.append(input); row.append(cell);
    }
    const entry = {row, readers: rowReaders}; readers.push(entry);
    if (!disabled) { const cell = make('td'); cell.append(button('Remove', () => { row.remove(); readers.splice(readers.indexOf(entry), 1); dirtyEditor(); }, 'text-button')); row.append(cell); }
    body.append(row);
  }
  if (Array.isArray(value)) value.forEach(addRow);
  if (!disabled) wrap.append(button('＋ Add row', () => { addRow(); dirtyEditor(); }, 'text-button'));
  if (field.description) wrap.append(make('p', field.description, 'field-description'));
  fieldReaders.set(field.id, () => readers.map(entry => Object.fromEntries([...entry.readers].map(([key, read]) => [key, read()]))).filter(row => Object.values(row).some(cell => cell !== null)));
  return wrap;
}
function renderFields(item, values, disabled) {
  fieldReaders = new Map(); $('answer-fields').replaceChildren();
  for (const field of item.fields || []) {
    if (field.type === 'table') { $('answer-fields').append(renderTableField(field, values[field.id], disabled)); continue; }
    const label = make('label', field.label || field.id, field.type === 'textarea' ? 'wide' : '');
    if (field.unit) label.append(make('span', ' · ' + field.unit, 'field-unit'));
    const {input, read} = createInput(field, values[field.id], disabled);
    input.id = 'brsr-field-' + field.id;
    label.htmlFor = input.id;
    if (field.placeholder) input.placeholder = field.placeholder;
    label.append(input);
    if (field.description) label.append(make('p', field.description, 'field-description'));
    $('answer-fields').append(label); fieldReaders.set(field.id, read);
  }
  if (!(item.fields || []).length) $('answer-fields').append(make('p', 'Use the narrative and evidence references to prepare this disclosure.', 'subtle'));
}
function addEvidenceRow(evidence = {}, disabled = false) {
  const row = make('div', undefined, 'evidence-row'), type = make('select');
  type.setAttribute('aria-label', 'Evidence type'); type.add(new Option('Source reference', 'reference')); type.add(new Option('Saved bill', 'bill')); type.value = evidence.type || 'reference'; type.disabled = disabled;
  const holder = make('div');
  function referenceInput() {
    const previous = holder.firstChild?.value ?? evidence.reference ?? '';
    holder.replaceChildren();
    let input;
    if (type.value === 'bill') {
      input = make('select'); input.add(new Option('Choose a saved company bill', ''));
      bills.forEach(bill => input.add(new Option(bill.filename + ' · ' + bill.status, bill.id)));
      if (previous && !bills.some(bill => bill.id === previous)) input.add(new Option('Saved bill · ' + previous, previous));
      input.value = previous;
    } else { input = make('input'); input.type = 'text'; input.value = previous; input.maxLength = 1500; input.placeholder = 'Document / register / source record reference'; }
    input.setAttribute('aria-label', 'Evidence reference'); input.disabled = disabled; input.addEventListener('input', dirtyEditor); input.addEventListener('change', dirtyEditor); holder.append(input);
  }
  referenceInput();
  const page = make('input'); page.type = 'number'; page.min = '1'; page.step = '1'; page.placeholder = 'Page'; page.value = evidence.page ?? ''; page.setAttribute('aria-label', 'Evidence page'); page.disabled = disabled; page.addEventListener('input', dirtyEditor);
  const remove = button('×', () => { row.remove(); dirtyEditor(); }, 'remove-evidence'); remove.setAttribute('aria-label', 'Remove evidence reference'); remove.hidden = disabled;
  type.addEventListener('change', () => { referenceInput(); dirtyEditor(); });
  row.append(type, holder, page, remove); row.readEvidence = () => { const reference = holder.firstChild.value.trim(); return reference ? {type: type.value, reference, ...(page.value ? {page: Number(page.value)} : {})} : null; };
  $('answer-evidence').append(row);
}
function applyApplicability() {
  const value = $('answer-applicability').value;
  $('applicability-reason-label').hidden = value === 'applicable';
  $('answer-fields').hidden = value !== 'applicable';
}
function renderCalculations(answer) {
  const calculations = answer?.calculations || answer?.calculation;
  $('calculation-preview').replaceChildren(); $('calculation-preview').hidden = !calculations;
  if (!calculations) return;
  $('calculation-preview').append(make('h3', 'Saved calculation workings'));
  const lines = Array.isArray(calculations) ? calculations : Object.entries(calculations).map(([name, value]) => ({name, value}));
  for (const line of lines) {
    let text;
    if (typeof line === 'string') text = line;
    else if (line.value === null || line.value === undefined) text = (line.label || line.name || line.id || 'Calculation') + ': Not calculated · ' + (line.reason || 'Inputs are missing or not suitable for this calculation.');
    else text = (line.label || line.name || line.id || 'Calculation') + ': ' + line.value + (line.unit ? ' ' + line.unit : '') + (line.formula ? ' · ' + line.formula : '');
    $('calculation-preview').append(make('div', text, 'calculation-line'));
  }
}
function openAnswer(item) {
  editingItem = item; editingAnswer = answerFor(item.id) || null; dirty = false; saving = false;
  const editable = canEdit(item.id), answer = editingAnswer;
  $('answer-form').reset(); $('answer-form').querySelector('.form-error').hidden = true;
  $('answer-code').textContent = 'SECTION ' + sectionOf(item) + (item.principle ? ' · PRINCIPLE ' + principleOf(item) : '') + ' · ' + item.id;
  $('answer-title').textContent = itemTitle(item);
  $('answer-meta').replaceChildren(statusBadge(answer), make('span', 'FY ' + detail.report.financial_year, 'pill'), make('span', 'Previous FY ' + detail.report.previous_financial_year, 'pill'));
  if (item.core) $('answer-meta').append(make('span', 'Core-tagged', 'core-tag'));
  $('answer-guidance').textContent = item.description || item.guidance || 'Prepare the response using the reporting boundary and applicable source guidance. Keep unknown values blank.';
  const source = safeHttp(item.source_url || ''); $('answer-source').hidden = !source; if (source) $('answer-source').href = source;
  $('answer-applicability').value = answer?.applicability || 'applicable'; $('answer-applicability').disabled = !editable;
  $('answer-reason').value = answer?.applicability_reason || ''; $('answer-reason').disabled = !editable;
  $('answer-narrative').value = answer?.narrative || ''; $('answer-narrative').disabled = !editable;
  renderFields(item, answer?.values || {}, !editable);
  $('answer-evidence').replaceChildren(); (answer?.evidence || []).forEach(evidence => addEvidenceRow(evidence, !editable));
  $('add-evidence').hidden = !editable; $('save-actions').hidden = !editable; $('answer-readonly').hidden = editable;
  $('answer-dirty').hidden = true; $('review-actions').hidden = !detail.permissions.can_review || !answer || answer.status === 'reviewed';
  $('review-answer').disabled = false;
  applyApplicability(); renderCalculations(answer);
  $('answer-dialog').showModal();
}
function readAnswer(status) {
  const applicability = $('answer-applicability').value;
  const values = applicability === 'applicable' ? Object.fromEntries([...fieldReaders].map(([key, read]) => [key, read()]).filter(([, value]) => value !== null)) : {};
  return {values, narrative: $('answer-narrative').value.trim(), evidence: Array.from($('answer-evidence').children).map(row => row.readEvidence()).filter(Boolean), applicability, applicability_reason: $('answer-reason').value.trim(), status, expected_revision: editingAnswer?.revision || 0};
}
async function saveAnswer(status, submit) {
  if (saving) return;
  saving = true; submit.disabled = true;
  const error = $('answer-form').querySelector('.form-error'); error.hidden = true;
  try {
    const body = readAnswer(status);
    await api('/api/brsr/reports/' + encodeURIComponent(reportId) + '/answers/' + encodeURIComponent(editingItem.id), {method: 'PUT', body: JSON.stringify(body)});
    dirty = false; $('answer-dialog').close(); await chooseReport(reportId); message('page-feedback', status === 'submitted' ? 'Disclosure submitted for review.' : 'Draft saved. It remains separate from reviewed disclosures.');
  } catch (failure) { error.textContent = failure.message; error.hidden = false; }
  finally { saving = false; submit.disabled = false; }
}
$('answer-form').addEventListener('submit', event => { event.preventDefault(); saveAnswer(event.submitter.value || 'draft', event.submitter); });
$('review-answer').addEventListener('click', async () => {
  const error = $('answer-form').querySelector('.form-error'); error.hidden = true;
  if (dirty) { error.textContent = 'Save your changes before reviewing this disclosure.'; error.hidden = false; return; }
  if (!$('answer-review-confirm').checked) { error.textContent = 'Confirm that you checked the values and evidence.'; error.hidden = false; return; }
  const submit = $('review-answer'); submit.disabled = true; saving = true;
  try {
    await api('/api/brsr/reports/' + encodeURIComponent(reportId) + '/answers/' + encodeURIComponent(editingItem.id) + '/review', {method: 'POST', body: JSON.stringify({confirmed: true, expected_revision: editingAnswer.revision})});
    dirty = false; $('answer-dialog').close(); await chooseReport(reportId); message('page-feedback', 'Disclosure reviewed. Its saved evidence and revision stay connected.');
  } catch (failure) { error.textContent = failure.message; error.hidden = false; }
  finally { saving = false; submit.disabled = false; }
});
$('add-evidence').addEventListener('click', () => { addEvidenceRow(); dirtyEditor(); });
$('answer-applicability').addEventListener('change', () => { applyApplicability(); dirtyEditor(); });
for (const id of ['answer-reason', 'answer-narrative']) $(id).addEventListener('input', dirtyEditor);
$('answer-dialog').addEventListener('cancel', event => { if (saving || dirty && !confirm('Close this disclosure and discard unsaved changes?')) event.preventDefault(); });
$('report-form').addEventListener('submit', async event => {
  event.preventDefault(); const submit = event.submitter, error = event.target.querySelector('.form-error'); submit.disabled = true; error.hidden = true;
  try {
    const body = {name: $('report-name').value.trim(), financial_year: $('report-year').value.trim(), previous_financial_year: $('report-previous').value.trim(), reporting_start: $('report-start').value, reporting_end: $('report-end').value, boundary: $('report-boundary').value, listed: $('report-listed').checked, notes: $('report-notes').value.trim()};
    const report = await api('/api/brsr/reports?company_id=' + encodeURIComponent(companyId), {method: 'POST', body: JSON.stringify(body)});
    $('report-dialog').close(); currentView = 'overview'; await chooseCompany(companyId, report.id); message('page-feedback', 'Reporting year created. Begin with company details and your reporting boundary.');
  } catch (failure) { error.textContent = failure.message; error.hidden = false; }
  finally { submit.disabled = false; }
});
$('assignment-user').addEventListener('change', renderAssignmentChoices);
$('assign-all').addEventListener('click', () => { $('assignment-options').querySelectorAll('input').forEach(input => input.checked = true); updateAssignmentCount(); });
$('assign-none').addEventListener('click', () => { $('assignment-options').querySelectorAll('input').forEach(input => input.checked = false); updateAssignmentCount(); });
$('assignment-form').addEventListener('submit', async event => {
  event.preventDefault(); const submit = event.submitter, error = event.target.querySelector('.form-error'); submit.disabled = true; error.hidden = true;
  try {
    const body = {question_ids: Array.from($('assignment-options').querySelectorAll('input:checked')).map(input => input.value)};
    await api('/api/brsr/reports/' + encodeURIComponent(reportId) + '/assignments/' + encodeURIComponent($('assignment-user').value), {method: 'PUT', body: JSON.stringify(body)});
    await chooseReport(reportId); message('page-feedback', 'Disclosure assignments saved for this reporting year.');
  } catch (failure) { error.textContent = failure.message; error.hidden = false; }
  finally { submit.disabled = false; }
});
$('create-snapshot').addEventListener('click', async () => {
  message('page-error', '');
  if (!$('snapshot-confirm').checked) { message('page-error', 'Confirm that incomplete records will remain marked in the preparation draft.'); return; }
  const submit = $('create-snapshot'); submit.disabled = true;
  try {
    await api('/api/brsr/reports/' + encodeURIComponent(reportId) + '/snapshots', {method: 'POST', body: JSON.stringify({confirmed: true})});
    await chooseReport(reportId); $('snapshot-confirm').checked = false; message('page-feedback', 'Report snapshot saved. Open its HTML report or download the supporting JSON / CSV.');
  } catch (failure) { message('page-error', failure.message); }
  finally { submit.disabled = false; }
});
for (const node of document.querySelectorAll('[data-view]')) node.addEventListener('click', () => show(node.dataset.view));
for (const node of document.querySelectorAll('[data-go]')) node.addEventListener('click', () => show(node.dataset.go));
for (const node of document.querySelectorAll('[data-close]')) node.addEventListener('click', () => { const id = node.dataset.close; if (id === 'answer-dialog' && (saving || dirty && !confirm('Close this disclosure and discard unsaved changes?'))) return; $(id).close(); });
for (const id of ['disclosure-search', 'section-filter', 'principle-filter', 'status-filter', 'core-filter', 'leadership-filter']) $(id).addEventListener(id === 'disclosure-search' ? 'input' : 'change', renderDisclosures);
for (const [number, title] of Object.entries(principles)) $('principle-filter').add(new Option('P' + number + ' · ' + title, number));
$('new-report').addEventListener('click', createReportDialog); $('empty-create').addEventListener('click', createReportDialog);
$('company-select').addEventListener('change', () => { message('page-feedback', ''); chooseCompany($('company-select').value); });
$('report-select').addEventListener('change', () => { message('page-feedback', ''); chooseReport($('report-select').value); });
$('refresh').addEventListener('click', () => reportId ? chooseReport(reportId) : chooseCompany(companyId));
window.addEventListener('hashchange', () => show(location.hash.slice(1) || 'overview', false));
window.addEventListener('beforeunload', event => { if (dirty && $('answer-dialog').open) { event.preventDefault(); event.returnValue = ''; } });
window.traceSession.then(async session => {
  identity = session; applyRoleCopy(); $('my-dashboard').href = session.home;
  companies = (await api('/api/companies')).filter(company => company.active);
  const params = new URLSearchParams(location.search), requestedCompany = params.get('company');
  companyId = session.user.company_id || (companies.some(company => company.id === requestedCompany) ? requestedCompany : companies.length === 1 ? companies[0].id : '');
  setOptions($('company-select'), companies, companyId, 'Choose company'); $('company-select').disabled = !portfolioRole();
  currentView = viewTitles[location.hash.slice(1)] ? location.hash.slice(1) : 'overview';
  if (!companies.length) emptyWorkspace('No company workspaces are available.', 'Company records appear here when they are available to your account.');
  else await chooseCompany(companyId, params.get('report'));
}).catch(error => { message('page-error', error.message); emptyWorkspace('Your workspace could not be loaded.', 'Refresh the page to reconnect to the reporting workspace.'); });
