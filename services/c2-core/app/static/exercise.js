/* Shared fictional exercise. No operational sensor, weapon, or release interfaces. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = '/api/exercise';
  const SESSION_KEY = 'cuas.exercise.session.v1';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const dateMs = value => value == null || value === '' ? NaN : typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : Date.parse(value);
  const iso = ms => new Date(ms).toISOString();
  const utc = value => Number.isFinite(dateMs(value)) ? new Date(dateMs(value)).toISOString().replace('T',' ').replace(/\.\d{3}Z$/, ' UTC') : 'Unavailable';
  const shortTime = value => Number.isFinite(dateMs(value)) ? new Date(dateMs(value)).toISOString().slice(11,19) + 'Z' : 'Time unavailable';
  const ageText = (value, now = referenceTime()) => {
    const at = dateMs(value); if (!Number.isFinite(at)) return 'Unavailable';
    const age = Math.max(0, Math.floor((now - at) / 1000));
    return age < 60 ? age + 's' : age < 3600 ? Math.floor(age / 60) + 'm ' + age % 60 + 's' : Math.floor(age / 3600) + 'h ' + Math.floor(age % 3600 / 60) + 'm';
  };
  const duration = seconds => {
    const n = Math.max(0, Math.floor(Number(seconds) || 0));
    return (n >= 3600 ? String(Math.floor(n / 3600)).padStart(2,'0') + ':' : '') + String(Math.floor(n / 60) % 60).padStart(2,'0') + ':' + String(n % 60).padStart(2,'0');
  };
  const trainingTitles = ['Initial observation','Conflicting source reports','Source delivery interrupted','Source correction received','Coordination still pending','Review and handover'];
  const trainingNotes = ['A visual report needs a named human review. Its classification is unresolved.','Two sources disagree. Preserve both observations and request clarification.','A quiet source does not prove the situation has cleared. Check the age of each report.','A correction changes the evidence record. The earlier report remains in its revision history.','Assign the outstanding review and record its acknowledgment and outcome.','Review the record, unresolved requests, and handover before ending the exercise.'];
  let state = null, currentState = null, session = null, health = null, healthChecked = false, selectedId = null;
  let online = false, historical = false, fetching = false, refreshDone = Promise.resolve(), lastSync = 0, stateReceived = 0, tak = null;
  let dialogHandler = null, dialogReturnFocus = null, busy = false, toastTimer = null, pendingInvite = '';
  try { session = JSON.parse(sessionStorage.getItem(SESSION_KEY) || 'null'); } catch (_) { /* An unavailable local session is not a connected session. */ }
  const controller = () => state?.participant?.role === 'controller';
  const canWrite = () => Boolean(session?.token && online && !historical && !busy && state?.room?.status !== 'ended');
  const canReview = () => canWrite() && ['controller','reviewer'].includes(state?.participant?.role);
  const referenceTime = () => historical ? dateMs(state?.snapshotAt || state?.events?.at(-1)?.at || state?.events?.at(-1)?.createdAt || state?.serverTime) : Date.now();
  const actorName = id => state?.participants?.find(p => p.id === id)?.name || (typeof id === 'object' ? id?.name : id) || 'System';
  const reports = () => Array.isArray(state?.reports) ? state.reports : [];
  const requests = () => Array.isArray(state?.requests) ? state.requests : [];
  const report = () => reports().find(r => r.id === selectedId) || null;
  const show = (id, value) => { $(id).hidden = !value; };
  const setText = (id, value) => { const next = String(value ?? ''); if ($(id).textContent !== next) { $(id).textContent = next; delete $(id).dataset.rendered; } };
  function markup(id, html) {
    const target = $(id);
    if (target.dataset.rendered === html) return;
    const focused = target.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = focused && {id:focused.id, reportId:focused.dataset.reportId, action:focused.dataset.requestAction, requestId:focused.dataset.requestId};
    target.innerHTML = html; target.dataset.rendered = html;
    if (focusKey) {
      const replacement = focusKey.id ? $(focusKey.id) : [...target.querySelectorAll('button')].find(b => focusKey.reportId ? b.dataset.reportId === focusKey.reportId : b.dataset.requestAction === focusKey.action && b.dataset.requestId === focusKey.requestId);
      replacement?.focus({preventScroll:true});
    }
  }
  function toast(message, error = false) {
    clearTimeout(toastTimer); setText('toast', message); $('toast').className = 'toast' + (error ? ' error' : ''); show('toast', true);
    toastTimer = setTimeout(() => show('toast', false), error ? 10000 : 6500);
  }
  function saveSession(value) {
    session = value;
    try { if (value) sessionStorage.setItem(SESSION_KEY, JSON.stringify(value)); else sessionStorage.removeItem(SESSION_KEY); }
    catch (_) { toast('This browser cannot retain the session in this tab. Keep the page open.', true); }
  }
  async function api(path, {method='GET', body, setupKey, raw=false, authenticated=true, timeout=10000} = {}) {
    const abort = new AbortController(); const timer = setTimeout(() => abort.abort(), timeout);
    const headers = {'Accept':raw ? '*/*' : 'application/json'};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (authenticated && session?.token) headers.Authorization = 'Bearer ' + session.token;
    if (setupKey) headers['X-Setup-Key'] = setupKey;
    try {
      const response = await fetch(API + path, {method, headers, body:body === undefined ? undefined : JSON.stringify(body), signal:abort.signal, cache:'no-store', credentials:'omit', referrerPolicy:'no-referrer'});
      if (!response.ok) {
        let data; try { data = await response.json(); } catch (_) { data = null; }
        const detail = data?.detail ?? data?.error ?? data?.message;
        const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(x => x.msg || x.message || 'Invalid field').join('; ') : response.status === 401 ? 'This session is unavailable or has expired. Join with a new invitation.' : response.status === 409 ? 'The record changed while you were editing. Your draft is preserved. Review the current version before submitting again.' : 'Service returned HTTP ' + response.status + '.';
        const error = new Error(message); error.status = response.status; error.detail = data; throw error;
      }
      if (raw) return response;
      if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('The shared exercise service is not available at this address.');
      return await response.json();
    } finally { clearTimeout(timer); }
  }
  async function refresh(force = false) {
    if (fetching) { if (force) { await refreshDone; return refresh(true); } return; }
    if (!session?.token || (!health && !force)) return;
    let finishRefresh;
    refreshDone = new Promise(resolve => { finishRefresh = resolve; });
    fetching = true;
    try {
      const fresh = await api('/state');
      currentState = fresh; online = true; lastSync = Date.now();
      if (!historical) { state = fresh; stateReceived = Date.now(); render(); }
      else renderConnection();
      if (!tak || force || Date.now() - (tak._checkedAt || 0) > 15000) {
        try { tak = {...await api('/tak/status'), _checkedAt:Date.now()}; renderTak(); }
        catch (_) { tak = {configured:false, unavailable:true, _checkedAt:Date.now()}; renderTak(); }
      }
    } catch (error) {
      online = false; renderConnection(); renderControls();
      if (error.status === 401) setText('connectionBanner', error.message + ' Your last received snapshot remains visible.');
    } finally { fetching = false; finishRefresh(); }
  }
  function previewState() {
    const now = Date.now();
    return {
      room:{id:'preview',name:'Washington-area evidence exercise',status:'preview',createdAt:iso(now - 150000),elapsedSeconds:120,scenarioStep:1},
      participant:{id:'visitor',name:'Visitor',role:'observer'},
      participants:[{id:'preview-control',name:'Exercise Control',role:'controller'},{id:'preview-review',name:'Evidence Reviewer',role:'reviewer'}],
      reports:[
        {id:'TRAIN-A',title:'Source A: object classification unresolved',text:'Fictional observer reports a small airborne object near the exercise reference point. No independent confirmation is available. Request a human review; do not infer intent from location.',source:'Fictional observer A',observedAt:iso(now-95000),receivedAt:iso(now-82000),lat:38.903,lon:-77.055,version:1,revisions:[],createdBy:'preview-control'},
        {id:'TRAIN-B',title:'Source B: possible biological observation',text:'A second fictional observer describes the same area as possible bird activity. The observations conflict and require clarification. This preview has no live sensor connection.',source:'Fictional observer B',observedAt:iso(now-65000),receivedAt:iso(now-57000),lat:38.879,lon:-77.016,version:1,revisions:[],createdBy:'preview-control'}
      ],
      requests:[{id:'preview-request',reportId:'TRAIN-A',assigneeId:'preview-review',summary:'Compare source accounts and record the unresolved disagreement.',status:'open',createdAt:iso(now-40000),createdBy:'preview-control'}],
      handovers:[],events:[{seq:3,type:'request.created',actorId:'preview-control',createdAt:iso(now-40000),summary:'Human review assigned to Evidence Reviewer'},{seq:2,type:'report.created',actorId:'preview-control',createdAt:iso(now-57000),summary:'Source B reported possible biological activity'},{seq:1,type:'report.created',actorId:'preview-control',createdAt:iso(now-82000),summary:'Source A report received; classification unresolved'}],latestSeq:3,serverTime:iso(now)
    };
  }
  function renderConnection() {
    const connected = Boolean(session && online);
    const status = historical ? 'HISTORICAL' : connected ? 'CONNECTED' : session ? 'CONNECTION LOST' : health ? 'SERVICE READY' : 'READ-ONLY PREVIEW';
    setText('connectionStatus', status);
    $('connectionStatus').className = 'pill ' + (historical ? '' : connected ? 'good' : 'warn');
    $('connectionBanner').className = 'connection-banner' + (connected && !historical ? ' connected' : '');
    if (historical) setText('connectionBanner', 'Historical evidence snapshot. All changes are disabled. Return to the current incident to continue coordination.' + (!online ? ' The connection to the current incident is unavailable.' : ''));
    else if (connected) setText('connectionBanner', 'Shared training room. Reports and decisions are saved by the service. Source age describes the evidence, not the connection.');
    else if (session) setText('connectionBanner', 'Connection unavailable. Showing the last received snapshot. Changes are disabled until the service reconnects; open drafts are preserved.');
    else if (health) setText('connectionBanner', 'Shared exercise service is available. Create a room or use a named invitation to participate. The example below is a read-only preview.');
    else markup('connectionBanner', 'Read-only preview. Shared participants, saved decisions, and TAK exchange require the exercise service.<a id="localServiceLink" href="http://127.0.0.1:8787/exercise.html">Open local exercise service ↗</a>');
    setText('participantName', session ? state?.participant?.name || session?.participant?.name || 'Participant' : 'Visitor');
    setText('participantRole', session ? state?.participant?.role || session?.participant?.role || 'participant' : 'Read-only preview');
    setText('sessionButton', session ? 'Session' : 'Join / create');
    $('sessionButton').disabled = !healthChecked;
    setText('footerState', historical ? 'Historical sequence ' + (state?.throughSeq ?? 'unknown') : session ? (online ? 'Shared service · Record ' : 'Offline snapshot · Record ') + (state?.latestSeq ?? 0) : 'Public preview · No shared room joined');
    updateAges();
  }
  function renderControls() {
    const write = canWrite(), control = write && controller(), editable = canReview(), ended = state?.room?.status === 'ended';
    for (const id of ['inviteButton','handoverButton']) $(id).disabled = !control;
    $('clockButton').disabled = !control || ended;
    $('clockButton').textContent = state?.room?.status === 'running' ? 'Pause' : state?.room?.status === 'paused' ? 'Resume' : 'Start exercise';
    $('advanceButton').disabled = !control || ended || Number(state?.room?.scenarioStep) >= 5;
    $('endButton').disabled = !control || ended;
    $('newReportButton').disabled = !editable;
    $('reviseReportButton').disabled = !editable || !report();
    $('requestReviewButton').disabled = !editable || !report();
    for (const id of ['exportButton','replayButton','takExportButton']) $(id).disabled = !session || !online || busy || historical;
    $('takSendButton').disabled = !control || !tak?.configured || Boolean(tak?.pendingAttemptId) || historical;
    document.querySelectorAll('[data-request-action]').forEach(button => {
      const request = requests().find(r => r.id === button.dataset.requestId);
      const action = button.dataset.requestAction;
      button.disabled = !editable || !request || request.assigneeId !== state?.participant?.id || (action === 'ack' ? request.status !== 'open' : request.status !== 'acknowledged');
    });
    document.querySelectorAll('[data-handover-accept]').forEach(button => button.disabled = !write);
  }
  function render() {
    if (!state) return;
    if (!reports().some(r => r.id === selectedId)) selectedId = reports()[0]?.id || null;
    setText('roomName', state.room?.name || 'Shared exercise');
    setText('roomStatus', historical ? 'HISTORICAL' : state.room?.status || 'unknown');
    $('roomStatus').className = 'pill ' + (state.room?.status === 'running' && !historical ? 'good' : '');
    const step = Number(state.room?.scenarioStep);
    setText('checkpointLabel', step >= 0 ? 'Case checkpoint ' + (step + 1) + ' / 6' : 'Case not started');
    setText('caseTitle', trainingTitles[step] || 'Shared evidence review');
    setText('caseSummary', trainingNotes[step] || 'Start the fictional case or submit a training report. Each checkpoint is advanced by the controller so discussion can take the time it needs.');
    setText('activeParticipantCount', state.participants?.length || 0);
    setText('openRequestCount', requests().filter(r => r.status !== 'resolved').length);
    renderConnection(); renderReports(); renderEvidence(); renderRequests(); renderEvents(); renderTak(); renderControls(); updateAges(); drawMap();
  }
  function renderReports() {
    const filter = $('reportSearch').value.trim().toLowerCase();
    const visible = reports().filter(r => [r.title,r.source,r.id,r.text].some(x => String(x || '').toLowerCase().includes(filter)));
    setText('reportCount', reports().length);
    markup('reportList', visible.length ? visible.map(r => `<div role="listitem"><button class="report-item" type="button" data-report-id="${esc(r.id)}" aria-current="${r.id === selectedId}"><span class="report-item-top"><span>${esc(r.id)}</span><span>REV ${esc(r.version || 1)}</span></span><span class="report-item-title">${esc(r.title)}</span><span class="report-item-source">${esc(r.source || 'Source unavailable')}</span><span class="report-item-age">Observation age <span data-age-at="${esc(r.observedAt || '')}">Unavailable</span></span></button></div>`).join('') : `<p class="empty-state">${filter ? 'No reports match this filter.' : 'No reports received. Add a fictional report or advance the evidence case.'}</p>`);
    updateAges();
  }
  function renderEvidence() {
    const r = report();
    setText('selectedReportVersion', r ? 'REV ' + (r.version || 1) : 'NO SELECTION');
    setText('mapSelection', r ? r.title : 'Fictional Washington-area exercise');
    if (!r) { markup('evidenceDetail','<p class="empty-state">Select a report to inspect its source, observation time, receipt time, and correction history.</p>'); return; }
    const location = Number.isFinite(Number(r.lat)) && Number.isFinite(Number(r.lon)) && r.lat != null && r.lon != null ? Number(r.lat).toFixed(5) + ', ' + Number(r.lon).toFixed(5) : 'Not supplied';
    const revisionHTML = (r.revisions || []).map((rev,i) => `<div class="revision-item"><strong>Revision ${esc(rev.version || i + 1)} · ${esc(actorName(rev.actor || rev.createdBy || rev.actorId || rev.authorId))}</strong><span>${esc(utc(rev.createdAt || rev.revisedAt || rev.receivedAt || rev.at))}</span>${rev.reason ? `<p>Reason: ${esc(rev.reason)}</p>` : ''}<details><summary>Evidence at this revision</summary><p>Source: ${esc(rev.source || 'Unavailable')}<br>Observed: ${esc(utc(rev.observedAt))}<br>Received: ${esc(utc(rev.receivedAt))}</p><p>${esc(rev.text || rev.previousText || 'No wording available')}</p></details></div>`).join('');
    markup('evidenceDetail', `<h3 class="report-heading">${esc(r.title)}</h3><div class="report-body">${esc(r.text)}</div><dl class="detail-grid"><div><dt>Source</dt><dd>${esc(r.source || 'Unavailable')}</dd></div><div><dt>Recorded by</dt><dd>${esc(actorName(r.createdBy))}</dd></div><div><dt>Observed at</dt><dd>${esc(utc(r.observedAt))}</dd></div><div><dt>Received at</dt><dd>${esc(utc(r.receivedAt))}</dd></div><div><dt>Observation age</dt><dd class="age" data-age-at="${esc(r.observedAt || '')}">Unavailable</dd></div><div><dt>Receipt age</dt><dd data-age-at="${esc(r.receivedAt || '')}">Unavailable</dd></div><div><dt>Reported location</dt><dd>${esc(location)}</dd></div><div><dt>Evidence status</dt><dd>Human review required</dd></div></dl>${r.attachment ? `<section class="evidence-section"><h3>Source attachment</h3><button id="viewAttachmentButton" type="button" class="small">Open ${esc(r.attachment.name || 'text attachment')}</button></section>` : ''}<section class="evidence-section"><h3>Correction history</h3>${revisionHTML || '<p class="muted fine-print">No corrections recorded. A correction preserves the original report and its author.</p>'}</section><section class="evidence-section"><p class="muted fine-print">This is fictional training evidence. A report marker does not establish identity, intent, or a validated track.</p></section>`);
  }
  function renderRequests() {
    const items = [...requests()].sort((a,b) => Number(a.status === 'resolved') - Number(b.status === 'resolved') || dateMs(b.createdAt) - dateMs(a.createdAt));
    setText('requestCount', items.filter(r => r.status !== 'resolved').length);
    markup('requestList', items.length ? items.map(r => `<div class="request-card ${esc(r.status)}"><div class="request-meta"><span>${esc(r.reportId)}</span><span>${esc(r.status?.toUpperCase())}</span></div><p>${esc(r.summary)}</p><div class="request-assignee">Assigned: ${esc(actorName(r.assigneeId))}</div>${r.dueAt ? `<p class="muted fine-print">Due ${esc(utc(r.dueAt))}</p>` : ''}${r.resolution ? `<p class="muted">Outcome: ${esc(r.resolution)}</p>` : ''}${r.status !== 'resolved' ? `<div class="button-row">${r.status === 'open' ? `<button type="button" class="small" data-request-action="ack" data-request-id="${esc(r.id)}">Acknowledge</button>` : ''}<button type="button" class="small" data-request-action="resolve" data-request-id="${esc(r.id)}">Record outcome</button></div>` : ''}</div>`).join('') : '<p class="empty-state">No review requests. Assign a report to a named participant to begin coordination.</p>');
  }
  function eventSummary(event) {
    if (event.summary || event.message || event.description) return event.summary || event.message || event.description;
    const kind = String(event.type || event.kind || 'record').replace(/[._]/g,' ');
    const data = event.data || event.payload || {};
    const detail = data.reason || data.summary || data.note || data.title || data.reportId || data.name;
    return kind + (detail ? ': ' + (typeof detail === 'object' ? JSON.stringify(detail) : detail) : '');
  }
  function renderEvents() {
    const events = [...(state.events || [])].sort((a,b) => Number(b.seq) - Number(a.seq));
    setText('eventCount', events.length);
    markup('eventList', events.length ? events.slice(0,120).map(e => `<div class="event-row"><div class="event-meta"><span>${esc(shortTime(e.createdAt || e.at || e.timestamp))}</span><span>#${esc(e.seq)} · ${esc(actorName(e.actor || e.actorId || e.participantId))}</span></div><p>${esc(eventSummary(e))}</p></div>`).join('') : '<p class="empty-state">Saved participant actions will appear here.</p>');
    const pending = (state.handovers || []).filter(h => h.status !== 'accepted' && !h.acceptedAt && h.toParticipantId === state.participant?.id);
    markup('handoverNotice', pending.map(h => `<div class="handover-pending"><strong>Handover awaits your acceptance</strong><p>${esc(h.note)}</p><button type="button" class="small" data-handover-accept="${esc(h.id)}">Accept handover</button></div>`).join(''));
  }
  function renderTak() {
    setText('takStatus', tak?.configured ? 'CONFIGURED' : 'MANUAL');
    $('takStatus').className = 'pill ' + (tak?.configured ? 'good' : '');
    setText('takSummary', !session ? 'Training report markers only. Connect the exercise service to export or send.' : tak?.unavailable ? 'TAK status unavailable. No delivery or client receipt is confirmed.' : tak?.lastError ? 'Last transfer failed. Open Details for the transport status.' : tak?.lastSentAt ? 'Last transport write ' + shortTime(tak.lastSentAt) + '. WinTAK receipt unverified.' : tak?.configured ? 'Manual training report transfer available. WinTAK receipt remains unverified.' : 'Export a training XML review bundle. Client import compatibility is unverified.');
    renderControls();
  }
  function updateAges() {
    if (!state) return;
    document.querySelectorAll('[data-age-at]').forEach(e => e.textContent = ageText(e.dataset.ageAt));
    const runningAddition = online && !historical && state.room?.status === 'running' ? Math.max(0,Date.now()-stateReceived)/1000 : 0;
    setText('exerciseClock', duration((state.room?.elapsedSeconds || 0) + runningAddition));
    setText('connectionAge', lastSync ? 'Last state received ' + ageText(lastSync,Date.now()) + ' ago' : health ? 'Local service available' : 'No shared state received');
  }
  function openDialog(title, html, {submit='Save', handler=null, cancel='Cancel'} = {}) {
    if (!$('exerciseDialog').open) dialogReturnFocus = document.activeElement;
    dialogHandler = handler;
    setText('dialogTitle', title); $('dialogBody').innerHTML = html;
    $('dialogError').hidden = true; setText('dialogError','');
    $('dialogSubmitButton').textContent = submit || ''; $('dialogSubmitButton').hidden = !submit;
    $('dialogCancelButton').textContent = cancel; $('dialogSubmitButton').disabled = false;
    if (!$('exerciseDialog').open) $('exerciseDialog').showModal();
    const first = $('dialogBody').querySelector('input:not([readonly]),textarea,select,button');
    (first || $('dialogCloseButton')).focus();
  }
  function closeDialog() { $('exerciseDialog').close(); dialogHandler = null; dialogReturnFocus?.focus({preventScroll:true}); }
  function dialogError(message) { setText('dialogError',message); show('dialogError',true); }
  function field(id,label,{value='',type='text',required=false,placeholder='',hint='',maxLength=200}={}) {
    return `<label class="field" for="${id}">${esc(label)}<input id="${id}" name="${id}" type="${type}" value="${esc(value)}" ${required?'required':''} maxlength="${maxLength}" placeholder="${esc(placeholder)}">${hint?`<small>${esc(hint)}</small>`:''}</label>`;
  }
  const textField = (id,label,value='',required=true,maxLength=10000) => `<label class="field" for="${id}">${esc(label)}<textarea id="${id}" name="${id}" maxlength="${maxLength}" ${required?'required':''}>${esc(value)}</textarea></label>`;
  const selectField = (id,label,options) => `<label class="field" for="${id}">${esc(label)}<select id="${id}" name="${id}" required>${options.map(o=>`<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('')}</select></label>`;
  function value(id) { return $(id).value.trim(); }
  async function mutate(path, body, message) {
    if (!canWrite()) throw new Error('A current service connection is required. Your draft is preserved.');
    busy = true; renderControls();
    try { const result = await api(path,{method:'POST',body}); await refresh(true); if (message) toast(message); return result; }
    finally { busy = false; renderControls(); }
  }
  function sessionDialog() {
    if (!health) {
      openDialog('Connect the shared exercise service','<p>This public page is a read-only preview. Shared rooms run through the exercise service on your computer or an approved host.</p><div class="form-note">Start the local exercise service, then open its address. The service keeps participant tokens out of the public website and is the place to configure the optional TAK server connection.</div><p><a href="http://127.0.0.1:8787/exercise.html">Open http://127.0.0.1:8787/exercise.html ↗</a></p><p>A TAK account or downloaded connection package is configured on the service host. No certificate or private key is uploaded to this public page.</p>',{submit:null,cancel:'Close'}); return;
    }
    if (session) {
      openDialog('Your exercise session',`<p>You are signed into this training room as <strong>${esc(state.participant?.name)}</strong>, with the <strong>${esc(state.participant?.role)}</strong> role.</p><div class="form-note">This tab keeps its participant token in session storage. A different participant should open their named invitation in a separate tab or browser. The service enforces role permissions.</div><p>Room: ${esc(state.room?.name)}<br>Room ID: ${esc(state.room?.id)}</p><button id="leaveSessionButton" type="button" class="danger">Leave this tab's session</button>`,{submit:null,cancel:'Close'});
      $('leaveSessionButton').onclick = () => { saveSession(null); online = false; historical = false; currentState = null; state = previewState(); tak = null; show('replayBanner',false); closeDialog(); render(); }; return;
    }
    openDialog('Create a shared training room',`<p>Give the exercise a name and identify its controller. Invite each additional participant by name after creating the room.</p>${field('createRoomName','Exercise name',{value:'Washington-area evidence exercise',required:true,maxLength:100})}${field('createParticipantName','Your name',{required:true,placeholder:'Named exercise controller',maxLength:80})}${health.setupRequired ? field('setupKey','Service setup key',{type:'password',required:true,hint:'Required by this service. The value is sent once and is not stored.'}) : ''}<div class="form-note">All reports are fictional training data. The server saves the room and action history. No operational commands are exchanged.</div><details><summary>Already have an invitation?</summary><label class="field" for="joinInviteToken">Paste the invitation token or this service's invitation link<input id="joinInviteToken" autocomplete="off"></label><button id="joinExistingButton" type="button">Join invitation</button></details>`,{submit:'Create room',handler:async()=>{
      const data = await api('/rooms',{method:'POST',authenticated:false,body:{name:value('createRoomName'),participantName:value('createParticipantName')},setupKey:$('setupKey')?.value});
      saveSession(data); closeDialog(); await refresh(true); toast('Shared training room created. Invite a named participant to begin coordination.');
    }});
    $('dialogSubmitButton').disabled = !health.canCreate;
    if (!health.canCreate) dialogError('Room creation is not available from this connection. Join with an invitation or create the room on the service host.');
    $('joinExistingButton').onclick = async () => {
      try {
        let token = value('joinInviteToken');
        if (token.includes('://')) { const url = new URL(token); if (url.origin !== location.origin) throw new Error('Use an invitation issued by this service.'); token = new URLSearchParams(url.hash.slice(1)).get('invite') || ''; }
        if (!token) throw new Error('Paste a valid invitation token.');
        await join(token); closeDialog();
      } catch(error) { dialogError(error.message); }
    };
  }
  async function join(inviteToken) {
    const data = await api('/join',{method:'POST',body:{inviteToken},authenticated:false});
    saveSession(data); historical = false; show('replayBanner',false); await refresh(true); toast('Joined the shared training room as ' + (data.participant?.name || 'the invited participant') + '.');
  }
  function inviteDialog() {
    openDialog('Invite a named participant',`${field('inviteName','Participant name',{required:true,maxLength:80})}${selectField('inviteRole','Role',[{value:'reviewer',label:'Reviewer · Reports and assigned review requests'},{value:'observer',label:'Observer · Read-only access'}])}<div class="form-note">The invitation grants the named role to whoever redeems it. Share it directly with that participant. The token is carried in the URL fragment and removed after joining.</div>`,{submit:'Create invitation',handler:async()=>{
      const data = await mutate('/invitations',{name:value('inviteName'),role:value('inviteRole')});
      const link = location.origin + location.pathname + '#invite=' + encodeURIComponent(data.inviteToken);
      openDialog('Invitation ready',`<p>Send this link to the intended participant. It opens the same shared room with their assigned identity.</p><label class="field" for="joinLink">Named invitation link<div class="link-copy"><input id="joinLink" readonly value="${esc(link)}"><button id="copyInviteButton" type="button">Copy link</button></div></label><p>Expires: ${esc(utc(data.expiresAt))}</p><div class="form-note warn">Anyone with this link can redeem this invitation. Keep it out of public screenshots and shared reports.</div>`,{submit:null,cancel:'Close'});
      $('copyInviteButton').onclick = () => copyText(link,'Invitation link copied.');
    }});
  }
  function participantsDialog() {
    const items = state.participants || [];
    openDialog('Participants',`<p>${session ? 'Named identities and responsibilities in this incident. A participant list does not confirm that everyone is currently connected.' : 'These identities are examples in the read-only preview.'}</p>${items.map(p=>`<div class="participant-row"><div><strong>${esc(p.name)}</strong><small>${esc(p.id)}${p.lastSeenAt ? '<br>Last contact ' + esc(utc(p.lastSeenAt)) : '<br>Connection presence unavailable'}</small></div><span class="pill">${esc(p.role)}</span></div>`).join('')}`,{submit:null,cancel:'Close'});
  }
  function reportDialog() {
    const observed = new Date().toISOString().slice(0,19);
    openDialog('Record a fictional report',`<div class="form-note">Report the source's wording and when it was observed. Receipt time is assigned by the service. A location is optional and represents a reported point.</div>${field('reportTitle','Report title',{required:true,maxLength:160})}${field('reportSource','Source name',{required:true,maxLength:120})}${field('reportObservedAt','Observed at (UTC)',{type:'datetime-local',value:observed,hint:'Leave empty if the observation time is unknown.'})}<div class="field-row">${field('reportLat','Latitude',{type:'number',placeholder:'38.8900'})}${field('reportLon','Longitude',{type:'number',placeholder:'-77.0350'})}</div>${textField('reportText','Original report wording') }<label class="field" for="reportAttachment">Optional source text file<input id="reportAttachment" type="file" accept=".txt,.md,.csv,text/plain,text/markdown,text/csv"><small>Plain text only, maximum 50 KB. Attach fictional material. Files are stored as text, never executed.</small></label>`,{submit:'Save report',handler:async()=>{
      const lat = value('reportLat'), lon = value('reportLon');
      if (Boolean(lat) !== Boolean(lon)) throw new Error('Provide both latitude and longitude, or leave both empty.');
      if (lat && (!Number.isFinite(Number(lat)) || Math.abs(Number(lat))>90 || !Number.isFinite(Number(lon)) || Math.abs(Number(lon))>180)) throw new Error('Latitude must be between -90 and 90; longitude between -180 and 180.');
      const file = $('reportAttachment').files[0]; let attachment;
      if (file) {
        if (file.size > 50000) throw new Error('Choose a text attachment of 50 KB or less.');
        if (!/\.(txt|md|csv)$/i.test(file.name)) throw new Error('Choose a .txt, .md, or .csv text attachment.');
        const content = await file.text(); if (content.includes('\u0000')) throw new Error('This attachment does not appear to be plain text.');
        attachment = {name:file.name,content};
      }
      const data = await mutate('/reports',{title:value('reportTitle'),source:value('reportSource'),text:value('reportText'),observedAt:value('reportObservedAt') ? value('reportObservedAt') + 'Z' : null,lat:lat ? Number(lat) : null,lon:lon ? Number(lon) : null,...(attachment?{attachment}:{})},'Fictional report saved with its source and receipt time.');
      selectedId = data.result?.id || data.report?.id || data.id || selectedId; closeDialog(); render();
    }});
    $('reportLat').step = 'any'; $('reportLon').step = 'any';
    $('reportLat').min = '-90'; $('reportLat').max = '90'; $('reportLon').min = '-180'; $('reportLon').max = '180';
    $('reportObservedAt').step = '1';
  }
  function revisionDialog() {
    const original = report(); if (!original) return;
    let expectedVersion = original.version;
    openDialog('Record an evidence correction',`<p>Editing report <strong>${esc(original.id)}</strong>, revision <strong id="revisionExpectedVersion">${esc(expectedVersion)}</strong>. The existing wording stays in the saved history.</p>${textField('revisionText','Corrected report wording',original.text)}${textField('revisionReason','Reason for the correction','',true,2000)}<div id="revisionConflict" hidden class="form-note warn"><strong>This report changed while you were editing.</strong><p id="revisionCurrentText"></p><button id="revisionRefreshButton" type="button">Use current version as baseline; keep my draft</button></div>`,{submit:'Save correction',handler:async()=>{
      try { await mutate('/reports/'+encodeURIComponent(original.id)+'/revisions',{expectedVersion,text:value('revisionText'),reason:value('revisionReason')},'Correction saved. Earlier wording remains in the record.'); closeDialog(); }
      catch(error) {
        if(error.status===409) { await refresh(true); const current = reports().find(r=>r.id===original.id); show('revisionConflict',true); setText('revisionCurrentText','Current revision ' + current?.version + ': ' + (current?.text || 'Refresh required.')); $('revisionRefreshButton').onclick = () => { expectedVersion=current?.version; setText('revisionExpectedVersion',expectedVersion); show('revisionConflict',false); show('dialogError',false); }; }
        throw error;
      }
    }});
  }
  function requestDialog() {
    const r = report(); if (!r) return;
    const people = (state.participants || []).filter(p=>['reviewer','controller'].includes(p.role));
    openDialog('Request a human review',`<p>Evidence: <strong>${esc(r.title)}</strong><br>Report ${esc(r.id)}, revision ${esc(r.version || 1)}</p>${selectField('requestAssignee','Assign to',people.map(p=>({value:p.id,label:p.name+' · '+p.role})))}${textField('requestSummary','What must the participant review?','',true,2000)}${field('requestDueAt','Optional due time (UTC)',{type:'datetime-local'})}<div class="form-note">The recipient acknowledges receipt and records an outcome. A request remains unresolved until an outcome is saved.</div>`,{submit:'Send review request',handler:async()=>{
      await mutate('/requests',{reportId:r.id,assigneeId:value('requestAssignee'),summary:value('requestSummary'),...(value('requestDueAt')?{dueAt:value('requestDueAt')+'Z'}:{})},'Review request saved and assigned.'); closeDialog();
    }});
  }
  function resolveDialog(requestId) {
    const request = requests().find(r=>r.id===requestId); if(!request) return;
    openDialog('Record the review outcome',`<p>${esc(request.summary)}</p>${textField('resolutionReason','Outcome and supporting reason','',true,2000)}<div class="form-note">Describe what was established, what remains uncertain, and any further action required. This will become part of the incident record.</div>`,{submit:'Resolve request',handler:async()=>{ await mutate('/requests/'+encodeURIComponent(requestId)+'/resolve',{reason:value('resolutionReason')},'Review outcome recorded.'); closeDialog(); }});
  }
  function handoverDialog() {
    const people = (state.participants || []).filter(p=>p.id!==state.participant?.id && p.role!=='observer');
    openDialog('Transfer exercise responsibility',`${people.length ? '<p>The current controller remains responsible until the named recipient accepts the handover.</p>' : '<p>Invite a reviewer before offering a handover.</p>'}${selectField('handoverTo','Incoming controller',people.map(p=>({value:p.id,label:p.name+' · '+p.role})))}${textField('handoverNote','Current situation, unresolved items, and handover notes','',true,2000)}`,{submit:people.length?'Offer handover':null,handler:async()=>{ await mutate('/handover',{toParticipantId:value('handoverTo'),note:value('handoverNote')},'Handover offered. Responsibility transfers when the recipient accepts.'); closeDialog(); }});
  }
  function replayDialog() {
    const events = [...(currentState?.events || state.events || [])].sort((a,b)=>Number(b.seq)-Number(a.seq));
    openDialog('Review the saved incident history','<p>Choose a saved event to inspect what the room contained at that point. Historical mode disables all changes.</p>' + (events.length ? events.map((e,i)=>`<label class="replay-event-choice"><input type="radio" name="replaySequence" value="${esc(e.seq)}" ${i===0?'checked':''}>#${esc(e.seq)} <span>${esc(shortTime(e.createdAt || e.at || e.timestamp))}</span><br>${esc(eventSummary(e))}</label>`).join('') : '<p>No saved events are available.</p>'),{submit:events.length?'Open historical snapshot':null,handler:async()=>{
      const seq = $('dialogBody').querySelector('input[name="replaySequence"]:checked')?.value;
      if(!seq) return; const snapshot=await api('/replay?through='+encodeURIComponent(seq));
      state=snapshot; historical=true; show('replayBanner',true); setText('replayLabel','Incident record through event #'+(snapshot.throughSeq ?? seq)); closeDialog(); render();
    }});
  }
  function takDialog() {
    openDialog('TAK training report exchange',`<div class="form-note">The browser never receives a TAK client private key or server credential. Server connection settings belong on the exercise service host.</div><dl class="detail-grid"><div><dt>Transport</dt><dd>${tak?.configured?'Configured on service':'Not configured'}</dd></div><div><dt>Send mode</dt><dd>Manual, controller initiated</dd></div><div><dt>Last attempt</dt><dd>${esc(utc(tak?.lastAttemptAt))}</dd></div><div><dt>Last transport write</dt><dd>${esc(utc(tak?.lastSentAt))}</dd></div><div><dt>Client receipt</dt><dd>Unverified</dd></div><div><dt>Content</dt><dd>Fictional report point markers</dd></div></dl>${tak?.lastError?`<p class="form-note warn spaced">${esc(tak.lastError)}</p>`:''}<p class="spaced">Export XML creates a training report review file. A single located report is a CoT event; multiple reports use an XML review bundle. WinTAK import compatibility for the bundle is unverified. Send to server writes selected room reports through the configured transport. A successful transport write does not prove that WinTAK displayed or acknowledged the data.</p><p>Open WinTAK separately and verify the marker labels and locations during your integration test. This room has no operational tracking, targeting, or command interface.</p>`,{submit:null,cancel:'Close'});
  }
  async function copyText(text,message) {
    try { await navigator.clipboard.writeText(text); toast(message); }
    catch(_) { $('joinLink')?.select(); toast('Clipboard access is unavailable. Select the link and copy it manually.',true); }
  }
  function download(content,name,type) {
    const blob = content instanceof Blob ? content : new Blob([content],{type});
    const url=URL.createObjectURL(blob), a=document.createElement('a'); a.href=url; a.download=name; document.body.append(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),30000);
  }
  function safeAction(action) { return async()=>{ try { await action(); } catch(error) { toast(error.message || 'The action could not be completed.',true); } }; }

  $('sessionButton').onclick=sessionDialog;
  $('participantsButton').onclick=participantsDialog;
  $('inviteButton').onclick=inviteDialog;
  $('newReportButton').onclick=reportDialog;
  $('reviseReportButton').onclick=revisionDialog;
  $('requestReviewButton').onclick=requestDialog;
  $('handoverButton').onclick=handoverDialog;
  $('replayButton').onclick=replayDialog;
  $('takDetailsButton').onclick=takDialog;
  $('reportSearch').oninput=renderReports;
  $('reportList').onclick=event=>{ const button=event.target.closest('[data-report-id]'); if(button){selectedId=button.dataset.reportId;renderReports();renderEvidence();renderControls();updateAges();drawMap();} };
  $('requestList').onclick=event=>{const button=event.target.closest('[data-request-action]');if(!button||button.disabled)return;if(button.dataset.requestAction==='ack')safeAction(()=>mutate('/requests/'+encodeURIComponent(button.dataset.requestId)+'/ack',{},'Review request acknowledged.'))();else resolveDialog(button.dataset.requestId);};
  $('handoverNotice').onclick=event=>{const button=event.target.closest('[data-handover-accept]');if(button&&!button.disabled)safeAction(()=>mutate('/handover/'+encodeURIComponent(button.dataset.handoverAccept)+'/accept',{},'Handover accepted. The shared room now reflects the new controller.'))();};
  $('evidenceDetail').onclick=event=>{if(event.target.closest('#viewAttachmentButton')){const attachment=report()?.attachment;if(attachment)openDialog(attachment.name||'Source attachment',`<p>Stored source text from this fictional report.</p><pre>${esc(attachment.content)}</pre>`,{submit:null,cancel:'Close'});}};
  $('clockButton').onclick=safeAction(()=>mutate('/clock',{action:state.room.status==='running'?'pause':state.room.status==='paused'?'resume':'start'},'Exercise clock updated.'));
  $('advanceButton').onclick=safeAction(()=>mutate('/scenario/advance',{},'Next fictional case checkpoint recorded.'));
  $('endButton').onclick=()=>openDialog('End this exercise',`<p>Ending freezes the exercise clock. The incident record remains available for review and export.</p><div class="form-note warn">${requests().filter(r=>r.status!=='resolved').length} review request(s) remain unresolved. Ending the exercise does not mark them complete or establish a successful outcome.</div>`,{submit:'End exercise',handler:async()=>{await mutate('/clock',{action:'end'},'Exercise ended. Review unresolved items in the saved record.');closeDialog();}});
  $('exitReplayButton').onclick=safeAction(async()=>{historical=false;show('replayBanner',false);state=currentState||state;render();await refresh(true);});
  $('exportButton').onclick=safeAction(async()=>{const data=await api('/export');download(JSON.stringify(data,null,2),'training-incident-'+state.room.id+'.json','application/json');toast('Saved incident record exported. Participant tokens are not part of the export.');});
  $('takExportButton').onclick=safeAction(async()=>{const response=await api('/tak/export',{raw:true});download(await response.blob(),'training-report-review-'+state.room.id+'.xml','application/xml');toast('Training XML review file exported. WinTAK import compatibility is unverified.');});
  $('takSendButton').onclick=()=>openDialog('Send training reports to TAK',`<p>This manually sends ${reports().filter(r=>r.lat!=null&&r.lon!=null).length} located report(s) from this exercise through the configured server transport.</p><div class="form-note">Markers are labeled fictional training data. A successful write confirms transport activity only. Check WinTAK separately to verify client receipt and display.</div>`,{submit:'Send training markers',handler:async()=>{const result=await mutate('/tak/send',{});closeDialog();tak=null;await refresh(true);toast(result.message || 'Training markers written to configured transport. WinTAK client receipt is unverified.');}});
  $('dialogCancelButton').onclick=closeDialog;
  $('dialogCloseButton').onclick=closeDialog;
  $('exerciseDialog').addEventListener('cancel',event=>{event.preventDefault();if(!busy)closeDialog();});
  $('dialogForm').onsubmit=async event=>{event.preventDefault();if(!dialogHandler)return;$('dialogSubmitButton').disabled=true;show('dialogError',false);try{await dialogHandler();}catch(error){dialogError(error.message || 'Unable to save this action. Your draft is preserved.');}finally{$('dialogSubmitButton').disabled=false;}};

  /* Geographic context only. Imagery is the default; tactical grid is an explicit choice. */
  const canvas=$('exerciseMap'), ctx=canvas.getContext('2d');
  let map={lat:38.891,lon:-77.034,zoom:12,mode:'imagery',width:0,height:0}, tiles=new Map(), markerHits=[], drag=null, moved=false;
  const worldPoint=(lat,lon,z)=>{const size=256*Math.pow(2,z),bounded=Math.max(-85.0511,Math.min(85.0511,lat)),sin=Math.sin(bounded*Math.PI/180);return{x:(lon+180)/360*size,y:(.5-Math.log((1+sin)/(1-sin))/(4*Math.PI))*size};};
  const coordinate=(x,y,z)=>{const size=256*Math.pow(2,z);return{lon:((x/size*360-180+540)%360)-180,lat:Math.atan(Math.sinh(Math.PI*(1-2*y/size)))*180/Math.PI};};
  function requestTile(z,x,y){
    const max=Math.pow(2,z);if(y<0||y>=max)return null;const wrapped=(x%max+max)%max,key=z+'/'+wrapped+'/'+y;
    if(tiles.has(key))return tiles.get(key);
    const item={image:new Image(),loaded:false,error:false};tiles.set(key,item);item.image.referrerPolicy='no-referrer';
    item.image.onload=()=>{item.loaded=true;drawMap();};item.image.onerror=()=>{item.error=true;drawMap();};
    item.image.src='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/'+z+'/'+y+'/'+wrapped;
    if(tiles.size>240){const first=tiles.keys().next().value;tiles.delete(first);}return item;
  }
  function drawMap(){
    if(!ctx||!state)return;const rect=canvas.getBoundingClientRect();const dpr=Math.min(window.devicePixelRatio||1,2);
    if(canvas.width!==Math.round(rect.width*dpr)||canvas.height!==Math.round(rect.height*dpr)){canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);}
    map.width=rect.width;map.height=rect.height;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,map.width,map.height);ctx.fillStyle='#13212b';ctx.fillRect(0,0,map.width,map.height);
    const center=worldPoint(map.lat,map.lon,map.zoom),left=center.x-map.width/2,top=center.y-map.height/2;
    let failures=0,loaded=0;
    if(map.mode==='imagery'){
      for(let x=Math.floor(left/256);x<=Math.floor((left+map.width)/256);x++)for(let y=Math.floor(top/256);y<=Math.floor((top+map.height)/256);y++){const tile=requestTile(map.zoom,x,y);if(tile?.loaded){ctx.drawImage(tile.image,x*256-left,y*256-top,256,256);loaded++;}if(tile?.error)failures++;}
      ctx.fillStyle='rgba(5,16,22,.18)';ctx.fillRect(0,0,map.width,map.height);
    }else{
      ctx.strokeStyle='#2a4352';ctx.lineWidth=1;for(let x=-(left%80);x<map.width;x+=80){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,map.height);ctx.stroke();}for(let y=-(top%80);y<map.height;y+=80){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(map.width,y);ctx.stroke();}
      ctx.font='10px ui-monospace, monospace';ctx.fillStyle='#668d9e';ctx.fillText('TACTICAL GRID · GEOGRAPHIC CONTEXT ONLY',16,map.height-47);
    }
    show('mapTileStatus',map.mode==='imagery'&&failures>0&&loaded===0);
    markerHits=[];reports().forEach((r,i)=>{
      if(r.lat==null||r.lon==null||!Number.isFinite(Number(r.lat))||!Number.isFinite(Number(r.lon)))return;
      const p=worldPoint(Number(r.lat),Number(r.lon),map.zoom),x=p.x-left,y=p.y-top;
      if(x<-30||x>map.width+30||y<-30||y>map.height+30)return;
      const selected=r.id===selectedId;ctx.beginPath();ctx.arc(x,y,selected?11:8,0,Math.PI*2);ctx.fillStyle=selected?'#68d3f0':'#c9a96d';ctx.fill();ctx.lineWidth=2;ctx.strokeStyle='#06121c';ctx.stroke();ctx.fillStyle='#06121c';ctx.font='bold 10px ui-monospace, monospace';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(String(i+1),x,y);ctx.textAlign='left';ctx.textBaseline='alphabetic';
      const text=String(r.id).slice(0,24),width=ctx.measureText(text).width+12;const labelX=Math.min(Math.max(5,x+16),Math.max(5,map.width-width-5));const labelY=Math.max(18,y-10);ctx.fillStyle='#081522ed';ctx.fillRect(labelX,labelY-14,width,21);ctx.strokeStyle=selected?'#68c5e5':'#4f6b7c';ctx.lineWidth=1;ctx.strokeRect(labelX,labelY-14,width,21);ctx.fillStyle=selected?'#d1f5ff':'#bfd2dd';ctx.fillText(text,labelX+6,labelY);markerHits.push({x,y,id:r.id});
    });
    const metersPerPixel=156543.03392*Math.cos(map.lat*Math.PI/180)/Math.pow(2,map.zoom);const raw=metersPerPixel*80;const unit=raw>=1000?1000:100;const nice=Math.max(unit,Math.round(raw/unit)*unit);setText('mapScale',nice>=1000?(nice/1000).toFixed(nice%1000?1:0)+' km':nice+' m');$('mapScale').style.width=Math.min(130,Math.max(35,nice/metersPerPixel))+'px';
    setText('mapAttribution',map.mode==='imagery'?'Imagery © Esri and its data providers. Imagery is not an elevation model.':'Tactical reference grid. No geographic basemap or elevation data.');
  }
  function mapZoom(delta){map.zoom=Math.max(4,Math.min(18,map.zoom+delta));drawMap();}
  $('basemapSelect').onchange=()=>{map.mode=$('basemapSelect').value;drawMap();};
  $('zoomInButton').onclick=()=>mapZoom(1);$('zoomOutButton').onclick=()=>mapZoom(-1);
  $('fitMapButton').onclick=()=>{const points=reports().filter(r=>r.lat!=null&&r.lon!=null&&Number.isFinite(Number(r.lat))&&Number.isFinite(Number(r.lon)));if(points.length){map.lat=points.reduce((sum,r)=>sum+Number(r.lat),0)/points.length;map.lon=points.reduce((sum,r)=>sum+Number(r.lon),0)/points.length;map.zoom=12;}else{map.lat=38.891;map.lon=-77.034;map.zoom=12;}drawMap();};
  canvas.addEventListener('pointerdown',event=>{const p=worldPoint(map.lat,map.lon,map.zoom);drag={x:event.clientX,y:event.clientY,worldX:p.x,worldY:p.y};moved=false;canvas.setPointerCapture(event.pointerId);});
  canvas.addEventListener('pointermove',event=>{const rect=canvas.getBoundingClientRect();if(drag){const dx=event.clientX-drag.x,dy=event.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>4)moved=true;const next=coordinate(drag.worldX-dx,drag.worldY-dy,map.zoom);map.lat=Math.max(-80,Math.min(80,next.lat));map.lon=next.lon;drawMap();}const center=worldPoint(map.lat,map.lon,map.zoom),point=coordinate(center.x+event.clientX-rect.left-map.width/2,center.y+event.clientY-rect.top-map.height/2,map.zoom);setText('mapCoordinate',point.lat.toFixed(5)+'°, '+point.lon.toFixed(5)+'°');});
  canvas.addEventListener('pointerup',event=>{if(drag&&!moved){const rect=canvas.getBoundingClientRect(),hit=markerHits.find(m=>Math.hypot(m.x-event.clientX+rect.left,m.y-event.clientY+rect.top)<17);if(hit){selectedId=hit.id;renderReports();renderEvidence();renderControls();updateAges();drawMap();}}drag=null;});
  canvas.addEventListener('pointercancel',()=>{drag=null;});
  canvas.addEventListener('wheel',event=>{event.preventDefault();mapZoom(event.deltaY<0?1:-1);},{passive:false});
  canvas.addEventListener('keydown',event=>{if(['+','=','-','ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();if(['+','=','-'].includes(event.key))mapZoom(event.key==='-'?-1:1);else{const p=worldPoint(map.lat,map.lon,map.zoom),dx=event.key==='ArrowLeft'?-80:event.key==='ArrowRight'?80:0,dy=event.key==='ArrowUp'?-80:event.key==='ArrowDown'?80:0;const next=coordinate(p.x+dx,p.y+dy,map.zoom);map.lat=next.lat;map.lon=next.lon;drawMap();}}});
  new ResizeObserver(drawMap).observe(canvas);
  async function initialize(){
    const fragment=new URLSearchParams(location.hash.slice(1));pendingInvite=fragment.get('invite')||'';
    if(pendingInvite)history.replaceState(null,'',location.pathname+location.search);
    state=previewState();render();
    try{health=await api('/health',{authenticated:false,timeout:4000});if(health.service!=='exercise')throw new Error('Unexpected service');}catch(_){health=null;online=false;}
    healthChecked=true;
    if(health&&pendingInvite){try{await join(pendingInvite);}catch(error){toast('Invitation could not be redeemed: '+error.message,true);}}
    else if(health&&session)await refresh(true);
    else if(pendingInvite)toast('This address has no shared exercise service. Open the invitation on the service host.',true);
    render();
    setInterval(()=>refresh(),2000);setInterval(updateAges,1000);
    window.addEventListener('online',()=>refresh(true));
    window.addEventListener('offline',()=>{online=false;renderConnection();renderControls();});
  }
  initialize();
})();
