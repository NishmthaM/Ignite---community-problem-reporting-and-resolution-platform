/* CivicTrack — user interface. Talks to the server only through CT.api. */
(function () {
  'use strict';
  const api = window.CT.api;
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];

  const state = { user: null, afterLogin: null, staffFilter: '', staffItems: [], geo: null, photoUrl: null };
  const PAGES = ['home', 'report', 'track', 'issues', 'help', 'account', 'mine', 'staff'];

  /* ---------- helpers ---------- */
  const esc = (s = '') =>
    String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const day = (iso) => (iso ? String(iso).slice(0, 10) : '');
  const debounce = (fn, ms = 300) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  let toastTimer;
  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.style.display = 'block';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.style.display = 'none'; }, 4200);
  }
  const fail = (err) => toast(err && err.message ? err.message : 'Something went wrong.');

  async function withBusy(btn, fn) {
    if (btn) btn.disabled = true;
    try { return await fn(); } finally { if (btn) btn.disabled = false; }
  }

  const statusClass = (s) => (['Resolved', 'Closed'].includes(s) ? '' : s === 'In progress' ? 'blue' : ['Submitted', 'Under review'].includes(s) ? 'orange' : s === 'Rejected' ? 'red' : '');

  function card(r) {
    const desc = r.description || '';
    return `<article class="report-card" data-ref="${esc(r.reference)}" tabindex="0" role="link"><span class="tag ${statusClass(r.status)}">${esc(r.status)}</span><h3>${esc(r.title)}</h3><p>${esc(r.category)} · ${esc(r.location || 'Location not provided')}</p><p>${esc(desc.slice(0, 115))}${desc.length > 115 ? '…' : ''}</p><span class="muted">${esc(r.reference)} · Updated ${esc(day(r.updatedAt))}</span></article>`;
  }
  const timeline = (updates = []) =>
    `<div class="timeline">${updates.map((u) => `<div class="event"><strong>${esc(u.status)}</strong><span>${esc(day(u.date))} · ${esc(u.note)}</span></div>`).join('')}</div>`;
  const options = (list, selected) => list.map((o) => `<option ${o === selected ? 'selected' : ''}>${esc(o)}</option>`).join('');

  /* ---------- navigation ---------- */
  function go(name) {
    if (location.hash.slice(1) === name) render(name);
    else location.hash = name;
  }

  function render(name) {
    if (!PAGES.includes(name)) name = 'home';
    if (['report', 'mine', 'staff'].includes(name) && !state.user) {
      state.afterLogin = name;
      $('#accountNote').textContent = {
        report: 'Please sign in or create an account to submit a report.',
        mine: 'Please sign in to see your reports.',
        staff: 'Staff, please sign in to open the dashboard.',
      }[name];
      name = 'account';
    } else if (name === 'staff' && state.user.role !== 'staff') {
      toast('The staff dashboard is only available to municipal staff.');
      name = 'home';
    }
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
    $$('.navlinks button').forEach((b) => b.classList.toggle('active', b.dataset.page === name));
    window.scrollTo({ top: 0 });
    const load = { home: loadRecent, track: loadRecent, issues: loadIssues, mine: loadMine, staff: loadStaff }[name];
    if (load) load().catch(fail);
  }

  /* ---------- session / auth ---------- */
  function updateAuthUI() {
    const u = state.user;
    $('#authButton').textContent = u ? 'Sign out' : 'Sign in';
    $('#whoAmI').hidden = !u;
    $('#whoAmI').textContent = u ? u.name : '';
    $('#mineNav').hidden = !u;
    $('#staffNav').hidden = !(u && u.role === 'staff');
  }

  function signedIn({ token, user }) {
    api.setToken(token);
    state.user = user;
    updateAuthUI();
    const next = state.afterLogin || (user.role === 'staff' ? 'staff' : 'home');
    state.afterLogin = null;
    toast(`Welcome, ${user.name}.`);
    go(next);
  }

  function signOut(message) {
    api.setToken(null);
    state.user = null;
    updateAuthUI();
    if (message) toast(message);
    go('home');
  }

  function setupAuth() {
    api.onSessionExpired(() => { state.user = null; updateAuthUI(); state.afterLogin = null; toast('Your session expired. Please sign in again.'); go('account'); });
    $('#authButton').addEventListener('click', () => (state.user ? signOut('You have been signed out.') : go('account')));
    $$('.tab').forEach((t) => t.addEventListener('click', () => {
      $$('.tab').forEach((x) => x.classList.toggle('active', x === t));
      $('#loginForm').hidden = t.dataset.tab !== 'login';
      $('#registerForm').hidden = t.dataset.tab !== 'register';
    }));
    $('#loginForm').addEventListener('submit', (e) => {
      e.preventDefault();
      withBusy(e.submitter, async () => {
        try { signedIn(await api.login({ email: $('#loginEmail').value, password: $('#loginPassword').value })); e.target.reset(); } catch (err) { fail(err); }
      });
    });
    $('#registerForm').addEventListener('submit', (e) => {
      e.preventDefault();
      withBusy(e.submitter, async () => {
        try { signedIn(await api.register({ name: $('#regName').value, email: $('#regEmail').value, password: $('#regPassword').value })); e.target.reset(); } catch (err) { fail(err); }
      });
    });
  }

  /* ---------- home / track ---------- */
  async function loadRecent() {
    const [stats, recent] = await Promise.all([api.stats(), api.reports({ limit: 3 })]);
    $('#stat-open').textContent = stats.inProgress;
    $('#stat-resolved').textContent = stats.resolved;
    $('#stat-review').textContent = stats.awaitingReview;
    const html = recent.items.length ? recent.items.map(card).join('') : '<div class="empty" style="grid-column:1/-1">No public reports yet.</div>';
    $('#homeReports').innerHTML = html;
    $('#trackReports').innerHTML = html;
  }

  async function loadTrack(ref) {
    const el = $('#trackResult');
    el.innerHTML = '<div class="loading">Looking up report…</div>';
    try {
      const r = await api.track(ref);
      el.innerHTML = `<div class="panel"><div style="display:flex;justify-content:space-between;gap:15px;align-items:flex-start;flex-wrap:wrap"><div><span class="eyebrow">${esc(r.reference)}</span><h2 style="margin:7px 0">${esc(r.title)}</h2><p class="muted">${esc(r.category)} · ${esc(r.location || 'Location not provided')}</p></div><span class="tag ${statusClass(r.status)}">${esc(r.status)}</span></div><p>${esc(r.description)}</p>${timeline(r.updates)}</div>`;
    } catch (err) {
      el.innerHTML = `<div class="empty">${esc(err.status === 404 ? 'No report found with that reference. Check the number and try again.' : err.message)}</div>`;
    }
  }

  function setupTrack() {
    $('#trackForm').addEventListener('submit', (e) => { e.preventDefault(); const ref = $('#trackCode').value.trim(); if (ref) loadTrack(ref); });
    document.addEventListener('click', (e) => openCard(e.target.closest('.report-card[data-ref]')));
    document.addEventListener('keydown', (e) => { if (e.key === 'Enter') openCard(e.target.closest('.report-card[data-ref]')); });
  }
  function openCard(el) {
    if (!el) return;
    $('#trackCode').value = el.dataset.ref;
    go('track');
    loadTrack(el.dataset.ref);
  }

  /* ---------- report form ---------- */
  function setupReportForm() {
    $('#locate').addEventListener('click', () => {
      if (!navigator.geolocation) return toast('Location sharing is not available in this browser.');
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          state.geo = { latitude: pos.coords.latitude, longitude: pos.coords.longitude };
          if (!$('#location').value.trim()) $('#location').value = `Near ${pos.coords.latitude.toFixed(3)}, ${pos.coords.longitude.toFixed(3)}`;
          toast('Location added. You can edit or clear the text.');
        },
        () => toast('Location was not shared. You can enter a place manually.'),
        { timeout: 8000 }
      );
    });

    $('#reportForm').addEventListener('submit', (e) => {
      e.preventDefault();
      const file = $('#photo').files[0];
      if (file && file.size > 5 * 1024 * 1024) return toast('That photo is larger than 5 MB. Please choose a smaller one.');
      withBusy(e.submitter, async () => {
        try {
          const created = await api.createReport({
            category: $('#category').value,
            title: $('#title').value,
            description: $('#description').value,
            location: $('#location').value,
            contact: $('#contact').value,
            ...(state.geo || {}),
          });
          let note = '';
          if (file) {
            try { await api.uploadPhoto(created.id, file); } catch (err) { note = ` Your photo could not be attached: ${err.message}`; }
          }
          e.target.reset();
          state.geo = null;
          $('#trackCode').value = created.reference;
          go('track');
          loadTrack(created.reference);
          toast(`Report submitted. Your reference is ${created.reference}.${note}`);
        } catch (err) { fail(err); }
      });
    });
  }

  /* ---------- public issues ---------- */
  function renderMap(items) {
    const box = $('#issueMap');
    const pts = items.filter((i) => i.area);
    if (!pts.length) { box.innerHTML = '<div class="empty">No mapped reports yet.</div>'; return; }
    const lats = pts.map((p) => p.area.lat), lngs = pts.map((p) => p.area.lng);
    const [minLat, maxLat, minLng, maxLng] = [Math.min(...lats), Math.max(...lats), Math.min(...lngs), Math.max(...lngs)];
    const pos = (v, lo, hi) => (hi === lo ? 50 : 8 + (84 * (v - lo)) / (hi - lo));
    box.innerHTML = pts.map((p) => `<span class="pin" title="${esc(p.title)}" style="left:${pos(p.area.lng, minLng, maxLng)}%;top:${100 - pos(p.area.lat, minLat, maxLat)}%"></span>`).join('');
  }

  async function loadIssues() {
    const list = $('#issueList');
    list.innerHTML = '<div class="loading">Loading reports…</div>';
    const res = await api.reports({ limit: 60, category: $('#filterCategory').value, status: $('#filterStatus').value, q: $('#filterSearch').value.trim() });
    list.innerHTML = res.items.length ? res.items.map(card).join('') : '<div class="empty" style="grid-column:1/-1">No reports match those filters.</div>';
    renderMap(res.items);
  }

  async function loadMine() {
    const res = await api.mine();
    $('#mineList').innerHTML = res.items.length ? res.items.map(card).join('') : '<div class="empty" style="grid-column:1/-1">You haven’t submitted any reports yet.</div>';
  }

  /* ---------- staff dashboard ---------- */
  async function loadStaff() {
    const filter = state.staffFilter || $('#staffStatus').value;
    const [queue, summary] = await Promise.all([
      api.staffReports({ q: $('#staffSearch').value.trim(), status: filter, priority: $('#staffPriority').value, limit: 100 }),
      api.staffSummary(),
    ]);
    const s = summary.byStatus;
    $('#dashNew').textContent = (s.Submitted || 0) + (s['Under review'] || 0);
    $('#dashActive').textContent = (s.Accepted || 0) + (s.Assigned || 0) + (s['In progress'] || 0);
    $('#dashUrgent').textContent = summary.openHighPriority;
    $('#dashResolved').textContent = s.Resolved || 0;
    $('#queueCount').textContent = summary.total;
    state.staffItems = queue.items;
    $('#staffQueue').innerHTML = queue.items.length
      ? queue.items.map((r) => `<div class="queue-row"><div><strong>${esc(r.title)}</strong><small>${esc(r.reference)} · ${esc(r.category)}</small></div><div><span class="tag ${statusClass(r.status)}">${esc(r.status)}</span><small>${esc(r.location || 'No location')}</small></div><div><strong>${esc(r.priority)}</strong><small>${esc(r.department)}</small></div><button class="btn secondary small" data-open="${r.id}">Review</button></div>`).join('')
      : '<div class="empty">No reports match these filters.</div>';
  }

  async function openStaff(id) {
    const r = await api.report(id);
    const meta = state.meta;
    const related = r.related || [];
    $('#staffDetail').innerHTML = `<span class="eyebrow">Report review · ${esc(r.reference)}</span><h2 style="font-size:25px;margin:7px 0">${esc(r.title)}</h2><p class="muted">${esc(r.category)} · ${esc(r.location || 'No location supplied')} · Submitted ${esc(day(r.createdAt))}</p><p>${esc(r.description)}</p>${r.contact ? `<p class="muted">Contact: ${esc(r.contact)}</p>` : ''}<div id="photoSlot"></div><div style="display:flex;gap:10px;flex-wrap:wrap;margin:15px 0"><span class="tag ${statusClass(r.status)}">${esc(r.status)}</span><span class="tag">Priority: ${esc(r.priority)}</span>${r.duplicateOf ? `<span class="tag blue">Linked to ${esc(r.duplicateOf)}</span>` : ''}</div>${related.length ? `<div class="notice"><b>Possible related reports:</b> ${related.map((x) => esc(x.reference)).join(', ')}. Review before linking as duplicate.</div>` : ''}<div class="fields" style="margin-top:16px"><div class="field"><label for="editStatus">Update status</label><select id="editStatus">${options(meta.statuses, r.status)}</select></div><div class="field"><label for="editPriority">Priority</label><select id="editPriority">${options(meta.priorities, r.priority)}</select></div><div class="field"><label for="editDepartment">Department</label><select id="editDepartment">${options(meta.departments, r.department)}</select></div><div class="field"><label for="editAssignee">Assigned staff (optional)</label><input id="editAssignee" maxlength="80" value="${esc(r.assignee || '')}" placeholder="Staff member"></div><div class="field full"><label for="publicNote">Public update</label><textarea id="publicNote" maxlength="1000" placeholder="Write an update residents can see."></textarea><small>Required when changing the status, and to resolve a report.</small></div><div class="field full"><label for="internalNote">Internal note (staff only)</label><textarea id="internalNote" maxlength="2000" placeholder="Optional internal note"></textarea></div><div class="field full"><button class="btn" id="saveUpdate" type="button">Save update</button></div><div class="field full"><label for="dupRef">Link as duplicate of</label><div style="display:flex;gap:8px"><input id="dupRef" placeholder="CT-123456" value="${esc(related[0] ? related[0].reference : '')}"><button class="btn secondary" id="linkDuplicate" type="button">Link</button></div></div></div>${timeline(r.updates)}${(r.internalNotes || []).length ? `<h3 style="margin-top:20px">Internal notes</h3>${(r.internalNotes).map((n) => `<p class="muted"><b>${esc(day(n.date))} · ${esc(n.author || 'Staff')}:</b> ${esc(n.note)}</p>`).join('')}` : ''}`;
    $('#staffDetail').scrollIntoView({ behavior: 'smooth', block: 'start' });

    if (state.photoUrl) { URL.revokeObjectURL(state.photoUrl); state.photoUrl = null; }
    if (r.hasPhoto) api.photo(r.id).then((blob) => { state.photoUrl = URL.createObjectURL(blob); $('#photoSlot').innerHTML = `<img class="photo" alt="Photo submitted with report ${esc(r.reference)}" src="${state.photoUrl}">`; }).catch(() => {});

    $('#saveUpdate').onclick = (e) => withBusy(e.currentTarget, async () => {
      try {
        await api.staffUpdate(id, { status: $('#editStatus').value, priority: $('#editPriority').value, department: $('#editDepartment').value, assignee: $('#editAssignee').value, publicNote: $('#publicNote').value, internalNote: $('#internalNote').value });
        toast('Report update saved.');
        await Promise.all([loadStaff(), openStaff(id)]);
      } catch (err) { fail(err); }
    });
    $('#linkDuplicate').onclick = (e) => withBusy(e.currentTarget, async () => {
      try {
        await api.staffDuplicate(id, $('#dupRef').value);
        toast('Report linked as duplicate.');
        await Promise.all([loadStaff(), openStaff(id)]);
      } catch (err) { fail(err); }
    });
  }

  function exportCsv() {
    const cell = (v) => `"${String(v ?? '').replaceAll('"', '""')}"`;
    const rows = [['Reference', 'Title', 'Category', 'Status', 'Priority', 'Department', 'Assignee', 'Location', 'Created'],
      ...state.staffItems.map((r) => [r.reference, r.title, r.category, r.status, r.priority, r.department, r.assignee, r.location, day(r.createdAt)])];
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([rows.map((r) => r.map(cell).join(',')).join('\n')], { type: 'text/csv' }));
    a.download = 'civictrack-reports.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function setupStaff() {
    const reload = debounce(() => loadStaff().catch(fail));
    $('#staffSearch').addEventListener('input', reload);
    $('#staffPriority').addEventListener('change', reload);
    $('#staffStatus').addEventListener('change', () => { state.staffFilter = ''; $$('.sideitem').forEach((x, i) => x.classList.toggle('active', i === 0)); reload(); });
    $$('.sideitem').forEach((b) => b.addEventListener('click', () => {
      state.staffFilter = b.dataset.filter;
      $('#staffStatus').value = '';
      $$('.sideitem').forEach((x) => x.classList.toggle('active', x === b));
      loadStaff().catch(fail);
    }));
    $('#staffQueue').addEventListener('click', (e) => { const b = e.target.closest('[data-open]'); if (b) openStaff(b.dataset.open).catch(fail); });
    $('#staffExport').addEventListener('click', exportCsv);
  }

  /* ---------- help ---------- */
  function setupHelp() {
    $('#helpForm').addEventListener('submit', (e) => {
      e.preventDefault();
      withBusy(e.submitter, async () => {
        try {
          await api.support({ name: $('#helpName').value, contact: $('#helpContact').value, topic: $('#helpTopic').value, message: $('#helpMessage').value });
          e.target.reset();
          toast('Thank you. Your message has been sent to the municipal team.');
        } catch (err) { fail(err); }
      });
    });
    $('#botButton').addEventListener('click', () => { $('#botBox').hidden = !$('#botBox').hidden; });
    $('#botInput').addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase();
      const a = q.includes('track') ? 'Use the Track a report page and enter the reference number from your confirmation.'
        : q.includes('photo') ? 'Photos are optional. Avoid including faces or private information.'
        : q.includes('duplicate') ? 'Staff review similar reports and may link them while keeping each reference number.'
        : 'Try the Report an issue, Track a report, or Help & contact pages. For more help, contact the municipal helpline listed here.';
      $('#botAnswer').textContent = q ? a : '';
    });
  }

  /* ---------- start-up ---------- */
  async function init() {
    $('#year').textContent = new Date().getFullYear();
    $$('[data-page]').forEach((b) => b.addEventListener('click', () => go(b.dataset.page)));
    $$('[data-go]').forEach((b) => b.addEventListener('click', () => go(b.dataset.go)));
    window.addEventListener('hashchange', () => render(location.hash.slice(1)));
    $('#filterCategory').addEventListener('change', () => loadIssues().catch(fail));
    $('#filterStatus').addEventListener('change', () => loadIssues().catch(fail));
    $('#filterSearch').addEventListener('input', debounce(() => loadIssues().catch(fail)));
    setupAuth(); setupTrack(); setupReportForm(); setupStaff(); setupHelp();

    try {
      state.meta = await api.meta();
      $('#category').innerHTML += state.meta.categories.map((c) => `<option>${esc(c)}</option>`).join('');
      $('#filterCategory').innerHTML += state.meta.categories.map((c) => `<option>${esc(c)}</option>`).join('');
    } catch (err) {
      state.meta = { categories: [], statuses: [], priorities: [], departments: [] };
      fail(err);
    }
    if (api.hasToken()) {
      try { state.user = (await api.me()).user; } catch { api.setToken(null); }
    }
    updateAuthUI();
    render(location.hash.slice(1) || 'home');
  }

  init();
})();
