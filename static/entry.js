(() => {
  const get = id => document.getElementById(id);
  const recovery = get('session-recovery');
  const button = get('clear-session');
  const error = get('recovery-error');
  const resume = get('resume-workspace');
  const details = get('workspace-details');
  const status = get('entry-status');
  const routes = new Set(['/','/super-admin','/company-manager','/company-admin','/company','/app','/brsr','/coefficients','/admin','/consultant-manager','/consultant','/data-entry']);
  function destination(value) {
    if (!value || value.length > 2048) return null;
    try {
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || url.username || url.password || !routes.has(url.pathname)) return null;
      return url.pathname + url.search + url.hash;
    } catch (_) { return null; }
  }
  const requested = destination(new URLSearchParams(location.search).get('next'));
  // Browsers retain the original fragment across a server-side login redirect;
  // it is never sent to the server, so keep it when resuming the workspace.
  const next = requested && !requested.includes('#') ? requested + location.hash : requested;
  function entryUrl(value) {
    const url = destination(value);
    if (!url) return null;
    if (next && url === '/super-admin') return url + '?return_to=' + encodeURIComponent(next);
    return url;
  }
  function showError(message) { error.textContent = message; error.hidden = false; }
  async function checkSession() {
    get('retry-session').hidden = true;
    try {
      const response = await fetch('/auth/session', {cache:'no-store'});
      if (!response.ok) throw new Error('Could not check your workspace. Check that Trace AI is running, then try again.');
      const session = await response.json();
      if (session.user) {
        location.replace(destination(session.home) || '/');
        return;
      }
      const available = session.local_entry_available && entryUrl(session.local_entry_url);
      resume.hidden = !available;
      recovery.hidden = !session.recovery_available || Boolean(available);
      details.open = !available;
      if (available) {
        resume.href = available;
        get('entry-description').textContent = 'Your previous local workspace is ready to reopen. Continue with your existing access.';
        get('workspace-summary').textContent = 'Use another workspace link';
        status.textContent = 'No active session. Reopen your workspace to continue.';
      } else {
        status.textContent = session.recovery_available ? 'Clear the ended session below before continuing.' : 'Enter your workspace link below to continue.';
      }
    } catch (failure) {
      status.textContent = 'Workspace connection unavailable.';
      showError(failure.message);
      get('retry-session').hidden = false;
    }
  }
  button.addEventListener('click', async () => {
    button.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch('/auth/logout', {method:'POST'});
      if (!response.ok) throw new Error('Could not clear this session. Refresh and try again.');
      const result = await response.json();
      if (result.next === '/super-admin') {
        location.replace(entryUrl(result.next));
        return;
      }
      await checkSession();
      status.textContent = 'Previous session cleared. Open your workspace link, or ask your workspace administrator to reopen access.';
    } catch (failure) { showError(failure.message); }
    finally { button.disabled = false; }
  });
  get('workspace-form').addEventListener('submit', event => {
    event.preventDefault();
    error.hidden = true;
    const target = destination(get('workspace-link').value.trim());
    if (!target) {
      showError('Enter a workspace link for this Trace AI app. Links to other websites, sign-in actions and API addresses cannot be opened here.');
      return;
    }
    location.assign(target);
  });
  get('retry-session').addEventListener('click', () => {error.hidden = true; checkSession();});
  checkSession();
})();
