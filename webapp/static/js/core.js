/* Shared state, helpers and the pages the app shows. */
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const S = {path: null, ships: [], empty: [], models: [], slot: null, palettes: {},
           target: 'primary', paint: null, job: null, edit: null, optLabel: {}};
const randomSeed = () => '0x' + [...crypto.getRandomValues(new Uint8Array(8))].map(b => b.toString(16).padStart(2, '0')).join('').toUpperCase();
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const hex = c => '#' + (c || [0, 0, 0]).map(v => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, '0')).join('');
const modelUrl = (ship, q) => `/api/model?ship=${encodeURIComponent(ship)}&${q}`;
const VIEWERS = {};
function show3d(id, url, fallback) {
  const el = $('#' + id); if (!el) return;
  VIEWERS[id]?.destroy(); el.classList.add('v3d');
  const v = VIEWERS[id] = window.ShipViewer && ShipViewer.mount(el);
  const fail = msg => { v?.destroy(); el.classList.remove('v3d'); el.innerHTML = fallback || `<span class="muted">${esc(msg)}</span>`; };
  if (!v) return fail('3D view needs WebGL2');
  v.load(url).catch(e => fail(e.message));
}
const viewBar = id => `<div class="row viewbar">${[['three', '3/4'], ['front', 'Front'], ['side', 'Side'], ['top', 'Top'], ['back', 'Back']]
  .map(([k, l]) => `<button class="btn" onclick="VIEWERS['${id}']?.view('${k}')">${l}</button>`).join('')}<button class="btn" onclick="VIEWERS['${id}']?.spin()">Spin</button>
  <span class="muted" style="font-size:12px">Drag to turn, scroll to zoom, double-click to reset</span></div>`;

// `timeout` (ms) makes a call give up; only the background polls use it (POLL_TIMEOUT). Everything else waits
// as long as it takes: the first read after a game update can spend minutes reading game files.
const POLL_TIMEOUT = 60000;
async function api(url, opts = {}) {
  const {timeout, ...init} = opts, wait = timeout || 0;
  const ctl = new AbortController(), t = wait ? setTimeout(() => ctl.abort(), wait) : null;
  let r, data;
  try {
    r = await fetch(url, {...init, signal: ctl.signal});
    data = (r.headers.get('content-type') || '').includes('json') ? await r.json() : null;
  } catch (e) {
    if (e.name !== 'AbortError') throw e;
    const err = new Error('Shipwright took too long to answer. Try again in a moment.'); err.status = 0; err.timeout = true;
    throw err;
  } finally { clearTimeout(t); }
  if (!r.ok || (data && data.ok === false)) {
    const err = new Error((data && data.error) || `${r.status} ${r.statusText}`);
    err.status = r.status;
    if (data && data.hint) { err.hint = data.hint; err.gameData = true; }
    if (data && data.backup) err.backup = data.backup;
    if (data && data.locked) err.locked = true;
    if (data && data.stale) err.stale = true;
    throw err;
  }
  return data;
}
const post = (url, body) => api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
/* Edits change a working copy ("draft") of the save, kept by the server in a file of its own. Nothing reaches
   the save until the player presses Review & save and confirms the summary of what changes. */
const UNSAVED = 'Not saved yet — press Review & save.';
S.draft = {dirty: false, changes: 0, stale: false};
let editQueue = Promise.resolve();
// One edit at a time, in click order: two at once would race on the same working copy.
function edit(url, body) {
  const run = editQueue.then(() => post(url, body));
  editQueue = run.catch(() => {}).then(() => refreshDraft().catch(() => {}));
  return run;
}
// The one in-page dialog. Opening another ends the previous one (as cancelled) so no caller is left waiting.
// Each opening gets a new generation: anything the old one still does later (a write finishing) leaves the
// new one alone.
function dialog(html) {
  const d = $('#saveDlg'), prev = d.abandon;
  d.abandon = d.onclose = d.oncancel = null; d.busy = false; d.gen = (d.gen || 0) + 1;
  if (d.open) d.close();
  if (prev) prev();
  d.innerHTML = html; d.showModal(); return d;
}
function settle(d, resolve, empty) {
  const gen = d.gen, mine = () => d.gen === gen;
  let over = false;
  const finish = v => { if (!over) { over = true; resolve(v); } };
  const done = v => { if (mine()) { d.abandon = d.onclose = d.oncancel = null; d.busy = false; if (d.open) d.close(); } finish(v); };
  d.abandon = () => finish(empty);
  d.onclose = () => {
    if (!mine() || d.open) return;  // a close event queued by an older dialog, arriving after this one opened
    if (d.busy) d.showModal(); else done(empty);  // mid-write it stays up until it's settled (or hidden)
  };
  return done;
}
// An in-page yes/no (window.confirm blocks the page and can't be styled). `danger`: Cancel gets the focus.
function ask(text, yes = 'OK', danger = false) {
  const d = dialog(`<p class="intro">${esc(text).replace(/\n/g, '<br>')}</p>
    <div class="row" style="justify-content:flex-end"><button class="btn" id="dlgNo">Cancel</button><button class="btn primary" id="dlgYes">${esc(yes)}</button></div>`);
  return new Promise(resolve => {
    const done = settle(d, resolve, false);
    $('#dlgNo').onclick = () => done(false); $('#dlgYes').onclick = () => done(true);
    $(danger ? '#dlgNo' : '#dlgYes').focus();
  });
}
// An in-page prompt: a text box, or a list to pick from when `options` ([{value, label}]) is given.
function askValue(text, {value = '', options = null, yes = 'OK'} = {}) {
  const field = options
    ? `<select id="dlgVal" style="width:100%">${options.map(o => `<option value="${esc(o.value)}" ${o.disabled ? 'disabled' : ''}>${esc(o.label)}</option>`).join('')}</select>`
    : `<input id="dlgVal" style="width:100%" value="${esc(value)}">`;
  const d = dialog(`<p class="intro">${esc(text).replace(/\n/g, '<br>')}</p>${field}
    <div class="row" style="justify-content:flex-end"><button class="btn" id="dlgNo">Cancel</button><button class="btn primary" id="dlgYes">${esc(yes)}</button></div>`);
  if (options && value !== '') $('#dlgVal').value = value;
  return new Promise(resolve => {
    const done = settle(d, resolve, null), take = () => { const v = $('#dlgVal').value.trim(); if (v) done(v); };
    $('#dlgNo').onclick = () => done(null); $('#dlgYes').onclick = take;
    $('#dlgVal').onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); take(); } };
    $('#dlgVal').focus();
  });
}
function changesHtml(p) {
  const areas = new Map();
  for (const c of p.changes) { if (!areas.has(c.area)) areas.set(c.area, []); areas.get(c.area).push(c); }
  const row = c => {
    const what = c.what ? `<span class="what">${esc(c.what)}</span>` : '';
    if (c.count) return `<li class="${c.kind}">${c.kind === 'added' ? '+' : '−'} ${what} ${c.count.toLocaleString()} items`
      + `${c.sample && c.sample.length ? ` <span class="muted">(e.g. ${esc(c.sample.join(', '))})</span>` : ''}</li>`;
    if (c.kind === 'added') return `<li class="added">+ ${what} ${esc(c.after)}</li>`;
    if (c.kind === 'removed') return `<li class="removed">− ${what} ${esc(c.before)}</li>`;
    return `<li>${what}${what ? ': ' : ''}<span class="old">${esc(c.before)}</span> → <b>${esc(c.after)}</b></li>`;
  };
  const more = p.total - p.changes.length;
  return `<p class="muted">${p.total.toLocaleString()} change${p.total === 1 ? '' : 's'} to <code>${esc(p.file)}</code>:</p>
    <div class="changes">${[...areas].map(([a, cs]) => `<div class="area"><b>${esc(a)}</b><ul>${cs.map(row).join('')}</ul></div>`).join('')}
      ${more > 0 ? `<p class="muted">…and ${more.toLocaleString()} more</p>` : ''}</div>
    <p class="muted">Your save is backed up first; undo a write from Save &gt; Backups.</p>`;
}
// What confirmChanges gives back when the player hid the dialog while the write was still running.
const IN_BACKGROUND = Symbol('in background');
// The summary dialog; `write()` does the write. While it runs, Esc and Cancel do nothing: the write can't be
// called back, and the player must see how it ended. A 409 (game running), 423 (another window), 500 that
// still kept the edits, or 400 (the file moved on) shows why inside the dialog; `onError(e)` can add buttons.
// A write that takes long gets a Hide button: the dialog goes, the write carries on, and `later(r)` (or a
// toast, for a failure) reports how it ended.
function confirmChanges(p, intro, write, {extra = '', onError, later} = {}) {
  const d = dialog(`<h2>Write this to your save?</h2>${intro ? `<p class="intro">${esc(intro).replace(/\n/g, '<br>')}</p>` : ''}
    ${changesHtml(p)}<p class="err" id="saveDlgErr" hidden></p><p class="muted" id="saveDlgSlow" hidden>Still writing...</p>
    <div class="row" style="justify-content:flex-end">${extra}<button class="btn" id="saveDlgHide" hidden>Hide (keeps saving in the background)</button>
      <button class="btn" id="saveDlgNo">Cancel</button><button class="btn primary" id="saveDlgYes">Write to save</button></div>`);
  const gen = d.gen;  // a newer dialog may take this element over while the write runs
  return new Promise((resolve, reject) => {
    const done = settle(d, resolve, null);
    let away = false, slow = null;
    const busy = on => {
      d.busy = on; $('#saveDlgYes').disabled = $('#saveDlgNo').disabled = on;
      clearTimeout(slow); $('#saveDlgHide').hidden = $('#saveDlgSlow').hidden = true;
      if (on) slow = setTimeout(() => { if (d.busy && d.gen === gen) $('#saveDlgHide').hidden = $('#saveDlgSlow').hidden = false; }, 15000);
    };
    d.oncancel = e => { if (d.busy) e.preventDefault(); };
    d.finish = v => done(v);
    $('#saveDlgNo').onclick = () => done(null);
    $('#saveDlgHide').onclick = () => { away = true; clearTimeout(slow); d.busy = false; done(IN_BACKGROUND); };
    $('#saveDlgYes').onclick = async () => {
      busy(true);
      try {
        const r = await write(); clearTimeout(slow);
        if (away || d.gen !== gen) { if (later) later(r); return; }  // hidden, or another dialog took over
        d.busy = false; done(r);
      } catch (e) {
        if (away || d.gen !== gen) { toast(e.message, true); refreshDraft().catch(() => {}); if (!away) reject(e); return; }
        busy(false);
        if (![400, 409, 423].includes(e.status) && !(e.status === 500 && e.backup)) {
          d.abandon = d.onclose = null; if (d.open) d.close(); return reject(e);
        }
        if (!d.open) d.showModal();  // a second Esc can close it anyway: bring it back to say what happened
        $('#saveDlgErr').textContent = e.message + (e.status === 409 ? ' Then press "Write to save" again.' : '');
        $('#saveDlgErr').hidden = false;
        $('#saveDlgYes').disabled = e.status === 400 || e.status === 423;
        if (onError) onError(e);
      }
    };
  });
}
const editCount = (n = S.draft.changes || 0) => `${n} unsaved edit${n === 1 ? '' : 's'}`;
const LOCKED = 'This save is open in another Shipwright window — read only here.';
async function refreshDraft() {
  if (!S.path) return;
  const path = S.path, d = await api('/api/draft?path=' + encodeURIComponent(path), {timeout: POLL_TIMEOUT});
  if (path !== S.path) return;
  S.draft = d;
  const bar = $('#saveBar');
  bar.hidden = !d.dirty && !d.locked;
  bar.classList.toggle('stale', !!(d.stale || d.locked));
  bar.classList.toggle('warn', !!(d.dirty && d.game_running && !d.stale && !d.locked));
  $('#sbReview').hidden = $('#sbDiscard').hidden = !!d.locked;
  $('#sbText').textContent = d.locked ? LOCKED + ' Close Shipwright in the other window to edit here.' : editCount() + (d.resumed ? ' from last time' : '') + (d.stale
    ? ' — but the save file changed on disk since you started (did the game save?). Discard them and redo.'
    : d.game_running ? " — No Man's Sky is running: it may save over these edits before you can write them. Quit the game, then Review & save."
    : ' — not in your save until you press Review & save.');
  if (!bar.hidden) document.body.style.setProperty('--barh', bar.offsetHeight + 'px');  // toasts and the page clear it
}
async function reviewDraft() {
  const path = S.path, p = await post('/api/draft/review', {path});
  if (p.missing) { toast('No unsaved edits.'); await refreshDraft(); return; }
  if (!p.total && !p.stale) {
    // Only drop the edits that were reviewed: if another one came in meanwhile, look again instead.
    try { await post('/api/draft/discard', {path, rev: p.rev}); } catch (e) {
      if (e.status !== 400) throw e;
      await refreshDraft(); return reviewDraft();
    }
    toast('Nothing to save: your edits put everything back as it was.');
    await refreshDraft(); await reloadAll(); return;
  }
  const saved = async r => { toast(`Saved to ${r.file}. Backup made.`); await refreshDraft(); await reloadAll(); };
  const run = confirmChanges(p, p.stale ? "The save file changed on disk since you started editing (did the game save?). "
    + "These edits can't be written over it: discard them and redo." : '',
    () => post('/api/draft/save', {path, rev: p.rev}), {
      extra: '<button class="btn" id="saveDlgDrop" hidden>Discard edits</button><button class="btn" id="saveDlgAgain" hidden>Review again</button>',
      onError: async e => {
        if (e.status !== 400) return;
        await refreshDraft().catch(() => {});
        if (S.draft.stale) $('#saveDlgDrop').hidden = false; else $('#saveDlgAgain').hidden = false;
      },
      later: r => saved(r).catch(() => {})});
  $('#saveDlgDrop').onclick = () => guard(async () => { $('#saveDlg').finish(null); await discardDraft(true); });
  $('#saveDlgAgain').onclick = () => { $('#saveDlg').finish(null); guard(reviewDraft); };
  if (p.stale) { $('#saveDlgYes').disabled = true; $('#saveDlgDrop').hidden = false; }
  const r = await run;
  if (!r || r === IN_BACKGROUND) return;
  await saved(r);
}
// Throws away the edits the player was shown (`rev`); a 400 means more came in meanwhile, so nothing went.
async function discardDraft(sure) {
  const rev = S.draft.rev;
  if (!sure && !await ask(`Throw away ${editCount()}? Your save stays as it is on disk.`, 'Throw away', true)) return false;
  try { await post('/api/draft/discard', {path: S.path, rev}); } catch (e) {
    if (e.status !== 400) throw e;
    await refreshDraft(); toast('More edits came in just now, so nothing was thrown away. Look again and retry.', true);
    return false;
  }
  await refreshDraft(); await reloadAll();
  return true;
}
// Before switching to another save: unsaved edits are kept in their own file, so they can wait for later
// (the save list marks them) or be thrown away. A design still being put in would land out of sight: wait.
async function leaveDraft() {
  if (S.installJob) throw new Error('A design is still being put into this save. Wait for it to finish, or press Stop.');
  await editQueue; await refreshDraft().catch(() => {});
  if (!S.draft.dirty || S.draft.locked) return true;
  const d = dialog(`<p class="intro">This save has ${esc(editCount())} that are not written yet. Keep them for later
      (the save list marks it), or throw them away?</p>
    <div class="row" style="justify-content:flex-end"><button class="btn" id="dlgNo">Cancel</button><button class="btn" id="dlgDrop">Throw away</button>
      <button class="btn primary" id="dlgKeep">Keep for later</button></div>`);
  const pick = await new Promise(resolve => {
    const done = settle(d, resolve, null);
    $('#dlgNo').onclick = () => done(null); $('#dlgDrop').onclick = () => done('drop'); $('#dlgKeep').onclick = () => done('keep');
    $('#dlgNo').focus();
  });
  if (!pick) return false;
  if (pick === 'drop') await post('/api/draft/discard', {path: S.path, rev: S.draft.rev});
  S.draft = {dirty: false, changes: 0, stale: false}; $('#saveBar').hidden = true;
  return true;
}
// Restoring a backup writes the file straight away, so it shows its own summary first (the server only ever
// stages a restore; /api/save/commit writes it). Gives back the result, or null when it didn't finish here.
async function restoreBackup(file) {
  const p = (await post('/api/backups/restore', {path: S.path, file, preview: true})).preview;
  const out = await confirmChanges(p, 'Restore this backup over the current save? The current file is backed up first.',
    () => post('/api/save/commit', {token: p.token}),
    {later: () => { toast('Backup restored.'); refreshDraft().then(reloadAll).catch(() => {}); }});
  if (out === IN_BACKGROUND) return null;  // still writing: `later` reports it
  if (!out) post('/api/save/discard', {token: p.token}).catch(() => {});
  return out;
}
window.onbeforeunload = e => { if (S.draft.dirty) { e.preventDefault(); e.returnValue = ''; } };
$('#sbReview').onclick = () => guard(reviewDraft);
$('#sbDiscard').onclick = () => guard(() => discardDraft(false));
function toast(msg, err) {
  const d = document.createElement('div'); if (err) d.className = 'err'; d.textContent = msg;
  $('#toast').append(d); setTimeout(() => d.remove(), err ? 9000 : 5000);
}
async function guard(fn) {
  try { return await fn(); } catch (e) {
    toast(e.hint ? `${e.message}\n${e.hint}` : e.message, true);
    if (e.gameData || e.status === 409) showBanner(e.message + (e.status === 409 ? ' Nothing was saved.' : ''), true);
    if (e.locked || e.stale || e.status === 400) refreshDraft().catch(() => {});
  }
}

function showBanner(text, bad, action) {
  const b = $('#banner');
  b.hidden = false; b.className = 'banner' + (bad ? '' : ' info');
  b.innerHTML = esc(text) + (action ? ` <button class="btn" id="bFix">${esc(action)}</button>` : '')
    + ' <button class="btn" id="bHide">Dismiss</button>';
  $('#bHide').onclick = () => { b.hidden = true; };
  if (action) $('#bFix').onclick = () => guard(refreshGameData);
}

async function loadHealth(deep) {
  const h = await api('/api/health' + (deep ? '?deep=1' : ''));
  if ($('#hList')) {
    $('#hBuild').textContent = h.nms_dir ? `game build ${h.build.exe[0]} bytes · ${h.build.paks} archives` : '';
    if ($('#hDir') !== document.activeElement) $('#hDir').value = h.nms_dir || '';
    $('#hList').innerHTML = h.checks.map(c => `<div class="hrow ${c.ok ? '' : 'bad'}"><b>${c.ok ? '✓' : '✕'} ${esc(c.name)}</b>
      <span class="d">${esc(String(c.detail))}</span><span style="flex:1"></span><span class="muted">${esc(c.needed_for)}</span></div>`).join('');
  }
  if (!h.nms_dir) {
    showBanner('No Man\'s Sky was not found on this PC. Open Save > Game data and choose the folder it is installed in '
               + '(the one containing Binaries\\NMS.exe).', true);
  } else if (!h.ok) {
    const broken = h.checks.filter(c => !c.ok).map(c => c.name).join(', ');
    showBanner(`Some game data could not be read (${broken}). Features that need it will not work.`, true, 'Re-read game files');
  } else if (h.updated) {
    showBanner('No Man\'s Sky was updated. The tool re-read the new game files.', false);
  } else {
    $('#banner').hidden = true;
  }
  return h;
}
async function refreshGameData() {
  toast('Reading the game files again, this takes a moment...');
  const h = await api('/api/health/refresh', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  toast(h.ok ? 'Game files re-read. Everything checks out.' : 'Game files re-read, but some checks still fail.', !h.ok);
  await loadHealth(false);
}
const shortPath = p => p.split(/[\\/]/).slice(-2).join('/');

$$('nav button').forEach(b => b.onclick = () => {
  $$('nav button').forEach(x => x.classList.toggle('active', x === b));
  $$('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + b.dataset.page));
  const load = {player: async () => { await loadPlayer(); await loadBackups(); setupExport(); await loadHealth(false); }, quests: loadQuests,
                inventory: loadInventories, unlocks: loadUnlocks, designer: () => post('/api/find/warm', {}).catch(() => {})}[b.dataset.page];
  if (load) guard(load);
});

async function loadStatus() {
  const st = await api('/api/status', {timeout: POLL_TIMEOUT}), pill = $('#gamePill');
  pill.className = 'pill ' + (st.game_running ? 'on' : 'off');
  pill.textContent = st.game_running ? 'Game running: saving blocked' : 'Game closed: safe to save';
  await refreshDraft();
}
async function loadSaves(boot) {
  const [saves, drafts] = await Promise.all([api('/api/saves'), api('/api/drafts', {timeout: POLL_TIMEOUT}).catch(() => [])]);
  const pending = new Map(drafts.map(d => [d.path, d]));
  const hm = t => `${Math.floor(t / 3600)}h ${String(Math.floor(t / 60) % 60).padStart(2, '0')}m`;
  const slots = {};
  saves.forEach(s => (slots[s.slot] = slots[s.slot] || []).push(s));
  $('#saveSel').innerHTML = Object.keys(slots).sort((a, b) => a - b).map(n => {
    const list = slots[n].sort((a, b) => b.mtime - a.mtime), top = list[0];
    const head = `Slot ${n}${top.name ? ` "${top.name}"` : ''}${top.difficulty ? ` · ${top.difficulty}` : ''}${top.playtime ? ` · ${hm(top.playtime)} played` : ''}`;
    return `<optgroup label="${esc(head)}">` + list.map(s => { const d = pending.get(s.path);
      return `<option value="${esc(s.path)}">${s === saves[0] ? '★ ' : ''}${d ? '✎ ' : ''}${esc(s.kind)}${s.summary ? ' - ' + esc(s.summary) : ''} (${new Date(s.mtime * 1000).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'})})${d ? ` — ${esc(editCount(d.changes))}` : ''}</option>`; }).join('') + '</optgroup>';
  }).join('');
  const has = p => saves.some(s => s.path === p);
  const back = boot && drafts.find(d => !d.locked && has(d.path));
  S.path = back ? back.path : (!boot && has(S.path)) ? S.path : saves.length ? saves[0].path : null;
  $('#saveSel').value = S.path || '';
  if (back) toast(`Back from last time: ${editCount(back.changes)} to ${back.file}. Review & save, or Discard.`);
}
// Ships plus whatever page is open, after the save (or its working copy) changed under them.
async function reloadAll() {
  await loadShips(); if (typeof fillTargets === 'function') fillTargets();
  const act = $$('.page').find(p => p.classList.contains('active'))?.id;
  const reload = {'page-quests': loadQuests, 'page-inventory': loadInventories, 'page-unlocks': loadUnlocks,
                  'page-player': async () => { await loadPlayer(); await loadBackups(); if ($('#rawBox').open) await rawOpen(''); }}[act];
  if (reload) await reload();
}
$('#saveSel').onchange = e => guard(async () => {
  const to = e.target.value;
  let ok = false;
  try { ok = await leaveDraft(); } finally { if (!ok) e.target.value = S.path; }
  if (!ok) return;
  S.path = to; await refreshDraft(); await reloadAll();
});
// Refresh keeps the save that is open (and its unsaved edits) when it is still there.
$('#refresh').onclick = () => guard(async () => {
  await loadStatus(); await loadSaves(false); await refreshDraft(); await reloadAll();
});
