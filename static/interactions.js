(() => {
  const el = id => document.getElementById(id);
  let activeRecord = null, zoom = 100, toastTimer;
  const checked = new Set();
  const format = number => number.toLocaleString(undefined, {maximumFractionDigits: 2});
  function notify(message) {
    el('toast').textContent = message;
    el('toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el('toast').hidden = true; }, 3200);
  }
  function numeric(value) {
    const text = String(value).trim();
    if (!/^(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?$/.test(text)) return NaN;
    return Number(text.replaceAll(',', ''));
  }
  function updateLab() {
    const consumption = el('field-consumption');
    const fromBill = activeRecord && consumption && !el('review-form').hidden;
    let kwh = fromBill ? numeric(consumption.value) : Number(el('lab-kwh').value.trim() || NaN);
    const unit = fromBill ? el('field-unit').value.trim().toLowerCase() : 'kwh';
    if (unit === 'mwh') kwh *= 1000;
    el('lab-kwh').disabled = Boolean(fromBill);
    if (fromBill && Number.isFinite(kwh)) el('lab-kwh').value = kwh;
    const factor = el('factor-value').value === '' ? (activeRecord ? NaN : 0.727) : Number(el('factor-value').value);
    const gas = el('gas').value === 'CO2e' ? 'CO₂e' : 'CO₂';
    const reduction = Number(el('reduction').value);
    el('reduction-label').textContent = reduction + '%';
    el('scenario-bar').style.width = (100 - reduction) + '%';
    const valid = Number.isFinite(kwh) && kwh >= 0 && kwh <= 1e15 && ['kwh', 'mwh'].includes(unit) && Number.isFinite(factor) && factor >= 0 && factor <= 10;
    el('lab-context').textContent = fromBill ? 'Exploring the current bill values. This scenario does not change your reviewed bill or exports.' : 'An illustrative scenario. Upload a bill to explore your own consumption.';
    el('lab-warning').hidden = valid;
    el('lab-warning').textContent = 'Enter valid consumption and a factor between 0 and 10. Bill units must be kWh or MWh.';
    const baseline = kwh * factor, after = baseline * (1 - reduction / 100);
    el('lab-baseline').textContent = valid ? format(baseline) + ' kg ' + gas : '—';
    el('lab-after').textContent = valid ? format(after) + ' kg ' + gas : '—';
    el('lab-saved').textContent = valid ? format(baseline - after) + ' kg ' + gas : '—';
    el('lab-baseline-kwh').textContent = valid ? format(kwh) + ' kWh' : 'Check bill values';
    el('lab-after-kwh').textContent = valid ? format(kwh * (1 - reduction / 100)) + ' kWh' : 'Check bill values';
    el('lab-factor').textContent = `${Number.isFinite(factor) ? factor : 'Unspecified'} kg ${gas}/kWh · ${el('factor-region').value || 'India'} · ${el('factor-year').value || '2023–24'} · Hypothetical scenario, not an approved calculation.`;
  }
  function updateChecks() {
    el('review-count').textContent = `${checked.size} of 8 fields checked`;
    el('review-meter-fill').style.width = (checked.size / 8 * 100) + '%';
  }
  function inspectEvidence(key) {
    const field = activeRecord?.fields[key];
    const quote = field?.evidence;
    const text = activeRecord?.source_text || 'This is an image or scan. Inspect the bill preview visually.';
    const pre = el('source-text');
    pre.replaceChildren();
    const index = quote ? text.toLowerCase().indexOf(quote.toLowerCase()) : -1;
    if (index >= 0) {
      const mark = document.createElement('mark');
      mark.textContent = text.slice(index, index + quote.length);
      pre.append(document.createTextNode(text.slice(0, index)), mark, document.createTextNode(text.slice(index + quote.length)));
      pre.parentElement.open = true;
      mark.scrollIntoView({block:'nearest', behavior:'smooth'});
    } else pre.textContent = text;
    el('evidence-focus').hidden = false;
    el('evidence-focus').textContent = quote ? `Evidence for ${labels[key]}: “${quote}”${index < 0 ? ' · Verify visually in the source.' : ' · Highlighted in PDF text below.'}` : `No explicit evidence for ${labels[key]}. Inspect the source before entering a value.`;
    document.querySelectorAll('.field').forEach(node => node.classList.toggle('inspecting', node.dataset.field === key));
    el('source-view').scrollIntoView({behavior:'smooth',block:'start'});
  }
  function attachFieldTools(record) {
    activeRecord = record;
    checked.clear();
    updateChecks();
    for (const [key, label] of Object.entries(labels)) {
      const field = el('field-' + key).closest('.field');
      field.dataset.field = key;
      const tools = document.createElement('div');tools.className = 'field-actions';
      const inspect = document.createElement('button');inspect.type = 'button';inspect.className = 'quiet-button';inspect.textContent = 'Inspect evidence ↗';inspect.setAttribute('aria-label', 'Inspect evidence for ' + label);inspect.addEventListener('click', () => inspectEvidence(key));
      const checkLabel = document.createElement('label');checkLabel.className = 'field-check';
      const check = document.createElement('input');check.type = 'checkbox';check.setAttribute('aria-label', 'Mark ' + label + ' as checked');
      check.addEventListener('change', () => {check.checked ? checked.add(key) : checked.delete(key);field.classList.toggle('checked', check.checked);updateChecks();});
      checkLabel.append(check, document.createTextNode('Checked'));
      tools.append(inspect, checkLabel);field.append(tools);
      el('field-' + key).addEventListener('input', () => {checked.delete(key);check.checked = false;field.classList.remove('checked');updateChecks();});
    }
    el('step2').disabled = false;
    el('step3').disabled = false;
    updateLab();
  }
  el('choose-file').addEventListener('click', () => el('file').click());
  el('step1').addEventListener('click', () => {el('dropzone').scrollIntoView({behavior:'smooth',block:'center'});el('choose-file').focus({preventScroll:true});});
  el('step2').addEventListener('click', () => {el('review-form').scrollIntoView({behavior:'smooth',block:'start'});el('field-consumption').focus({preventScroll:true});});
  el('step3').addEventListener('click', () => (el('result').hidden ? el('method') : el('result')).scrollIntoView({behavior:'smooth',block:'center'}));
  el('reduction').addEventListener('input', updateLab);
  el('lab-kwh').addEventListener('input', updateLab);
  el('reset-lab').addEventListener('click', () => {el('reduction').value = 20;if (!activeRecord) el('lab-kwh').value = 1250;updateLab();notify('Scenario reset. Bill details are unchanged.');});
  el('reset-checks').addEventListener('click', () => {checked.clear();document.querySelectorAll('.field-check input').forEach(input => input.checked = false);document.querySelectorAll('.field.checked').forEach(node => node.classList.remove('checked'));updateChecks();});
  document.addEventListener('trace:loaded', e => attachFieldTools(e.detail));
  document.addEventListener('trace:edited', () => queueMicrotask(updateLab));
  el('review-form').addEventListener('input', updateLab);
  document.addEventListener('trace:selection', () => {activeRecord = null;el('evidence-focus').hidden = true;el('review-form').hidden = true;el('step2').disabled = true;el('step3').disabled = true;updateLab();});
  document.addEventListener('trace:calculated', () => notify('Review saved. Your evidence exports are ready.'));
  document.addEventListener('trace:busy', e => {
    const busy = e.detail;
    el('source-view').setAttribute('aria-busy', String(busy));
    el('progress').classList.toggle('working', busy);
    el('sample').disabled = busy;
    el('sample').textContent = busy ? 'Reading with local AI…' : 'Try a fictional sample ↗';
    if (busy) {activeRecord = null;el('step2').disabled = true;el('step3').disabled = true;el('evidence-focus').hidden = true;el('step1').classList.add('current');el('step2').classList.remove('current');updateLab();}
  });
  document.querySelector('nav a[href="#method-note"]').addEventListener('click', () => {el('method-note').open = true;});
  function setZoom(next) {zoom = Math.min(250, Math.max(50, next));el('viewer-image').style.width = zoom + '%';el('zoom-level').textContent = zoom + '%';el('zoom-in').disabled = zoom === 250;el('zoom-out').disabled = zoom === 50;}
  el('open-viewer').addEventListener('click', () => {el('viewer-image').src = el('preview').src;setZoom(100);el('source-dialog').showModal();});
  el('preview').addEventListener('click', () => el('open-viewer').click());
  el('close-viewer').addEventListener('click', () => el('source-dialog').close());
  el('zoom-in').addEventListener('click', () => setZoom(zoom + 25));
  el('zoom-out').addEventListener('click', () => setZoom(zoom - 25));
  el('zoom-fit').addEventListener('click', () => setZoom(100));
  el('copy-formula').addEventListener('click', async () => {try {await navigator.clipboard.writeText(el('formula').textContent);notify('Calculation copied.');}catch {notify('Copy unavailable. Select the calculation text to copy it.');}});
  const factorObserver = new MutationObserver(updateLab);
  factorObserver.observe(el('model-status'), {childList:true});
  updateLab();
})();
