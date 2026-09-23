/* RailETA UI: one polling loop, no framework, dates rendered explicitly in IST. */
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const timeFormat = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Kolkata', hour:'2-digit', minute:'2-digit', hour12:false});
const clockFormat = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Kolkata', day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', hour12:false});
const dateIST = date => new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(date);
const trainSVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="5" y="3" width="14" height="15" rx="4"/><path d="M5 10h14M12 3v7M8 21l2-3m6 3-2-3M8 14h1m6 0h1"/></svg>';
let selected = '12301', data = null, fetchedAt = 0, previous = {}, changes = [], flashes = {};
let liveRetryAt = 0;
let mode = 'DEMO', livePollSeconds = 60, selectedDate = '', loading = false, failureCount = 0, retryAt = 0;
let pollTimer, controller, requestID = 0, map, routeLayer, dynamicLayer, trainMarker, accuracyCircle, mappedTrain;

function timeHTML(value) {
  if (!value) return '—';
  const date = new Date(value);
  const base = data?.journey_start_date || dateIST(new Date());
  const days = Math.round((Date.parse(value.slice(0,10)) - Date.parse(base)) / 86400000);
  return `${timeFormat.format(date)}${days > 0 ? `<sup class="day-offset">+${days}</sup>` : ''}`;
}
function signed(value) { if (!Number.isFinite(value)) return '—'; const rounded = Math.round(value); return `${rounded > 0 ? '+' : ''}${rounded}`; }
function delayPill(value) {
  if (!Number.isFinite(value)) return '<span class="delay-pill">Delay unavailable</span>';
  const n = Math.round(value);
  const type = n < 0 ? 'early' : n <= 5 ? 'ontime' : n <= 15 ? '' : n <= 60 ? 'moderate' : 'severe';
  const label = n < 0 ? `${Math.abs(n)} min early` : n <= 5 ? `On time${n > 0 ? ` · +${n}` : ''}` : `+${n} min`;
  return `<span class="delay-pill ${type}">${label}</span>`;
}
function sourceClass(position) { if (data?.data_mode === 'LIVE') return data.stale || position.source !== 'PROVIDER_REPORTED' ? 'estimated' : ''; return position.warnings.includes('SIGNAL_LOST') ? 'lost' : position.source === 'ESTIMATED' ? 'estimated' : ''; }
function sourceName(position) { if (data?.data_mode === 'LIVE') return data.stale ? 'STALE' : position.source.replaceAll('_',' '); return position.warnings.includes('SIGNAL_LOST') ? 'SIGNAL LOST' : position.source; }
function showError(message) { $('error').textContent = message; $('error').hidden = !message; }
async function api(path, options = {}) {
  if (!path.startsWith('/api/config')) path += (path.includes('?') ? '&' : '?') + 'mode=' + mode;
  const response = await fetch(path, {cache:'no-store', ...options, headers:{'Content-Type':'application/json',...options.headers}});
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.available ? `${body.error}. Available demo trains: ${body.available.join(', ')}.` : body.error || 'Request failed');
    const header = response.headers.get('Retry-After');
    error.retryAfter = Number(body.retry_after) || Number(header) || (header ? Math.max(0,(Date.parse(header)-Date.now())/1000) : 0);
    error.status = response.status;
    throw error;
  }
  return body;
}

function initMap() {
  if (!window.L) { $('map-fallback').hidden = false; $('map-fallback').textContent = 'Map library unavailable. Connect to the internet and reload; ETAs still work.'; return; }
  map = L.map('map', {zoomControl:true, scrollWheelZoom:false}).setView([25.5,82.8],6);
  const tiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:18, attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
  let failures = 0;
  tiles.on('tileerror', () => { if (++failures > 4) $('map-fallback').hidden = false; });
  tiles.on('tileload', () => { failures = 0; $('map-fallback').hidden = true; });
  routeLayer = L.layerGroup().addTo(map);
  dynamicLayer = L.layerGroup().addTo(map);
}
function drawMap() {
  if (!map || !data) return;
  const p = data.position, route = data.route.filter(s => Number.isFinite(s.lat) && Number.isFinite(s.lon));
  const hasPosition = Number.isFinite(p.latitude) && Number.isFinite(p.longitude);
  const mapKey = `${data.data_mode}:${data.train_number}:${data.journey_start_date}`;
  const first = mappedTrain !== mapKey;
  if (first) {
    routeLayer.clearLayers();
    L.polyline(route.map(s => [s.lat,s.lon]), {color:'#6e8fb7',weight:3,opacity:.8,dashArray:'5 7'}).addTo(routeLayer);
    mappedTrain = mapKey;
  }
  dynamicLayer.clearLayers();
  const traveled = route.filter(s => Number.isFinite(p.km_from_origin) && Number.isFinite(s.km) && s.km <= p.km_from_origin).map(s => [s.lat,s.lon]);
  if (hasPosition && Number.isFinite(p.km_from_origin)) traveled.push([p.latitude,p.longitude]);
  L.polyline(traveled, {color:'#25548d',weight:4,opacity:.95}).addTo(dynamicLayer);
  for (const condition of data.active_conditions) {
    const from = route.findIndex(s => s.code === condition.from), to = route.findIndex(s => s.code === condition.to);
    const color = {CONGESTION:'#e8a04f',SPEED_RESTRICTION:'#9d79c6',WEATHER_FOG:'#7d99a8'}[condition.type];
    if (from >= 0 && to > from) L.polyline(route.slice(from,to+1).map(s => [s.lat,s.lon]), {color,weight:8,opacity:.6}).bindTooltip(escapeHTML(condition.note)).addTo(dynamicLayer);
  }
  for (const point of route) {
    const stop = data.stops.find(s => s.code === point.code);
    if (!stop && !$('passing').checked) continue;
    const passed = stop && ['DEPARTED','ARRIVED'].includes(stop.status);
    const destination = point.code === data.stops.at(-1).code;
    const color = !stop ? '#8196ab' : passed ? '#9babb9' : destination ? '#b76c62' : '#285c96';
    const label = stop ? `${escapeHTML(point.name)} (${escapeHTML(point.code)})<br>${stop.predicted_eta ? (mode === 'LIVE' ? 'RailRadar ETA ' : 'Predicted ') + timeHTML(stop.predicted_eta) : 'Actual ' + timeHTML(stop.actual_arr || stop.actual_dep)}` : `${escapeHTML(point.name)} · Passing point`;
    L.circleMarker([point.lat,point.lon], {radius:stop ? 5 : 2.5,color,fillColor:passed ? '#b6c1cc' : '#fff',fillOpacity:1,weight:2}).bindTooltip(label).addTo(dynamicLayer);
  }
  const klass = sourceClass(p);
  const icon = L.divIcon({className:'train-marker',html:`<div class="train-dot ${klass}">${trainSVG}</div>`,iconSize:[35,35],iconAnchor:[17.5,17.5]});
  if (trainMarker) map.removeLayer(trainMarker);
  if (accuracyCircle) map.removeLayer(accuracyCircle);
  trainMarker = accuracyCircle = null;
  if (hasPosition) {
    if (Number.isFinite(p.accuracy_m)) accuracyCircle = L.circle([p.latitude,p.longitude], {radius:p.accuracy_m,color:klass ? '#b38c53' : '#427cb5',weight:1,fillOpacity:.12}).addTo(map);
    const tooltip = mode === 'LIVE' ? p.basis : p.source === 'MEASURED' ? 'Measured GPS position' : `Estimated position – last GPS ${Math.round(p.seconds_since_update/60)} min ago`;
    trainMarker = L.marker([p.latitude,p.longitude], {icon,zIndexOffset:1000}).bindTooltip(escapeHTML(tooltip)).addTo(map);
  }
  if (first && route.length) map.fitBounds(route.map(s => [s.lat,s.lon]), {padding:[35,50]});
  else if (hasPosition && $('follow').checked) map.panTo([p.latitude,p.longitude], {animate:true,duration:.6});
}
function renderHero() {
  if (mode === 'LIVE') return renderLiveHero();
  const destination = data.stops.at(-1), arrived = destination.status === 'ARRIVED';
  const eta = destination.predicted_eta || destination.actual_arr;
  $('destination-hero').innerHTML = `<div class="hero-top"><span class="eyebrow">${arrived ? 'JOURNEY COMPLETE' : 'DESTINATION ARRIVAL'}</span><span class="hero-kicker">${arrived ? 'ACTUAL' : 'PREDICTED ETA'}</span></div><div class="hero-name">${arrived ? 'Arrived at' : 'Arriving'} ${escapeHTML(destination.name)}</div><div class="hero-time-row"><strong class="hero-time">${timeHTML(eta)}</strong>${delayPill(destination.predicted_delay_min ?? destination.delay_min ?? 0)}</div><div class="hero-range">${arrived ? 'Arrival confirmed by recorded GPS' : `Expected range ${timeHTML(destination.eta_low)} – ${timeHTML(destination.eta_high)} <span> · ${escapeHTML(destination.confidence?.toLowerCase())} confidence</span>`}</div><div class="hero-bottom"><span>${arrived ? 'Scheduled' : 'Baseline'}<b>${timeHTML(destination.baseline_eta || destination.sched_arr)}</b></span><span class="updated-age">Updated just now</span></div>`;
}
function renderStatus() {
  if (mode === 'LIVE') return renderLiveStatus();
  const p = data.position;
  const current = data.stops.find(s => ['AT_STATION','ARRIVED'].includes(s.status));
  const location = current ? `At ${current.name}` : `Between ${p.between[0]} and ${p.between[1]}`;
  $('status-card').innerHTML = `<div class="section-heading"><span class="status-heading">${escapeHTML(location)}</span><span class="source-badge ${sourceClass(p)}">${sourceName(p)}</span></div><div class="status-metrics"><div class="metric"><div class="metric-value">${Math.round(p.speed_kmh)} <small>km/h</small></div><div class="metric-label">${p.source === 'ESTIMATED' ? 'Last measured speed' : 'Current speed'}</div></div><div class="metric"><div class="metric-value">${signed(data.current_delay_min)} <small>min</small></div><div class="metric-label">Delay at ${escapeHTML(data.delay_reference_station)}</div></div></div><div class="progress-label"><span>${Math.round(p.km_from_origin).toLocaleString()} of ${data.route.at(-1).km.toLocaleString()} km</span><span>${p.progress_pct.toFixed(1)}% complete</span></div><div class="progress-track"><span style="width:${Math.min(100,p.progress_pct)}%"></span></div><div class="status-bottom"><span>${p.unscheduled_halt ? '● Unscheduled signal halt' : `Last GPS ${timeHTML(p.last_update)}`}</span><span>${p.rejected_outliers} outliers rejected</span></div>`;
  $('map-location').textContent = location;
  $('map-position').textContent = `${Math.round(p.speed_kmh)} km/h · ${Math.round(p.km_from_origin).toLocaleString()} km covered · ${signed(data.current_delay_min)} min delay`;
  $('map-source').className = `source-badge ${sourceClass(p)}`;
  $('map-source').textContent = sourceName(p);
}
function renderTimeline() {
  if (mode === 'LIVE') return renderLiveTimeline();
  const now = Date.now();
  $('timeline').innerHTML = data.stops.map((stop,index) => {
    const passed = ['DEPARTED','ARRIVED'].includes(stop.status);
    const current = stop.status === 'AT_STATION';
    const destination = index === data.stops.length - 1;
    const flash = flashes[stop.code];
    const highlighted = flash && flash.until > now;
    const timing = stop.predicted_eta ? `<div class="time-grid"><div class="time-cell"><span>SCHEDULED</span><b>${timeHTML(stop.sched_arr)}</b></div><div class="time-cell"><span>BASELINE</span><b>${timeHTML(stop.baseline_eta)}</b></div><div class="time-cell predicted"><span>PREDICTED</span><b>${timeHTML(stop.predicted_eta)}</b></div></div><div class="prediction-meta">${delayPill(stop.predicted_delay_min)}<span>${timeHTML(stop.eta_low)}–${timeHTML(stop.eta_high)}</span><span class="confidence">${stop.confidence}</span></div><div class="reasons">${stop.reasons.map(reason => `<span class="reason ${reason.startsWith('Recovering') ? 'recovery' : ''}">${escapeHTML(reason)}</span>`).join('')}</div>${highlighted ? `<span class="eta-change ${flash.delta < 0 ? 'earlier' : ''}">${flash.delta > 0 ? '▲' : '▼'} ${signed(flash.delta)} min</span>` : ''}` : `<p class="actual-times">${current ? '● At station · ' : '✓ '}${stop.actual_arr ? `Arr ${timeHTML(stop.actual_arr)} · ` : ''}${stop.actual_dep ? `Dep ${timeHTML(stop.actual_dep)}` : stop.sched_dep ? `Scheduled dep ${timeHTML(stop.sched_dep)}` : 'Arrived'} ${stop.delay_min !== undefined ? ` · ${signed(stop.delay_min)} min` : ''}</p>`;
    const movingHere = !data.at_station && stop.code === data.position.between[0] && !destination;
    return `<article class="stop ${passed ? 'passed' : ''} ${destination ? 'destination' : ''} ${highlighted ? 'stop-changed' : ''}"><div class="stop-name"><h3>${escapeHTML(stop.name)}<span class="station-code">${stop.code}</span></h3><span class="stop-distance">${passed ? '' : `${Math.round(stop.km_to_go)} km to go`}</span></div>${timing}</article>${movingHere ? `<div class="current-marker"><span>▣</span><span>TRAIN NOW · ${Math.round(data.stops[index+1].km_to_go)} km to ${data.stops[index+1].code}${data.position.unscheduled_halt ? ' · Signal halt' : ''}</span></div>` : ''}`;
  }).join('');
  $('stop-count').textContent = `${data.stops.length} STOPS`;
}
function recordChanges(next) {
  for (const stop of next.stops) {
    if (!stop.predicted_eta) continue;
    const value = Date.parse(stop.predicted_eta);
    const delta = previous[stop.code] ? (value - previous[stop.code]) / 60000 : 0;
    if (Math.abs(delta) >= 2) {
      flashes[stop.code] = {delta, until:Date.now()+15000};
      if (stop.code === next.stops.at(-1).code) {
        const added = next.active_conditions.filter(c => !data?.active_conditions.some(old => old.id === c.id));
        const cleared = data?.active_conditions.filter(c => !next.active_conditions.some(fresh => fresh.id === c.id)) || [];
        const event = added.length ? `${added[0].type.replaceAll('_',' ').toLowerCase()} reported ${added[0].from}–${added[0].to}` : cleared.length ? `${cleared[0].type.replaceAll('_',' ').toLowerCase()} cleared ${cleared[0].from}–${cleared[0].to}` : stop.reasons.at(-1);
        changes.unshift({time:next.sim_time, code:stop.code, delta, reason:event});
        changes = changes.slice(0,6);
      }
    }
    previous[stop.code] = value;
  }
}
function renderChanges() {
  $('change-log').innerHTML = changes.length ? changes.map(c => `<li><strong>${timeHTML(c.time)} · ${c.code} ETA ${signed(c.delta)} min</strong><br>${escapeHTML(c.reason)}</li>`).join('') : '<li class="empty-note">Changes of 2 minutes or more appear here.</li>';
}
function tick() {
  if (mode === 'LIVE') { $('refresh-live').disabled = loading || Date.now() < retryAt; }
  if (!data) return;
  if (mode === 'LIVE') return tickLive();
  const elapsed = Math.max(0,(Date.now()-fetchedAt)/1000);
  const simulated = new Date(Date.parse(data.sim_time)+elapsed*data.sim_speed*1000);
  $('sim-clock').textContent = `Sim time ${clockFormat.format(simulated)} IST · ${data.sim_speed}×`;
  document.querySelectorAll('.updated-age').forEach(el => { el.textContent = `Updated ${Math.floor(elapsed)} s ago`; });
  if (Object.values(flashes).some(f => f.until <= Date.now())) {
    Object.keys(flashes).forEach(key => { if (flashes[key].until <= Date.now()) delete flashes[key]; });
    renderTimeline();
  }
}
// One self-scheduling poll loop: no overlap, no hidden-tab requests, bounded backoff.
function schedulePoll(seconds) {
  clearTimeout(pollTimer);
  if (document.hidden) return;
  pollTimer = setTimeout(() => load(), Math.max(seconds * 1000, retryAt - Date.now(), 0));
}
async function load(first = false) {
  if (loading || document.hidden) return;
  if (Date.now() < retryAt) { schedulePoll(0); return; }
  loading = true;
  controller = new AbortController();
  const activeController = controller;
  const id = ++requestID;
  const selectionKey = `${mode}:${selected}:${selectedDate}`;
  const timeout = setTimeout(() => activeController.abort(), 15000);
  let nextPoll = mode === 'LIVE' ? livePollSeconds : 5;
  try {
    const query = mode === 'LIVE' && selectedDate ? '?date=' + encodeURIComponent(selectedDate) : '';
    const next = await api(`/api/train/${encodeURIComponent(selected)}/live${query}`, {signal:activeController.signal});
    if (id !== requestID) return;
    const changedTrain = data?.train_number !== next.train_number || data?.journey_start_date !== next.journey_start_date || data?.data_mode !== next.data_mode;
    if (changedTrain) { previous = {}; changes = []; flashes = {}; mappedTrain = null; }
    recordChanges(next);
    data = next;
    data.selectionKey = selectionKey;
    fetchedAt = Date.now();
    failureCount = 0;
    retryAt = 0;
    if(mode === 'LIVE') liveRetryAt = 0;
    $('train-title').textContent = `${data.train_number} · ${data.train_name}`;
    $('route-title').innerHTML = `${escapeHTML(data.stops[0].name.replace(' Jn',''))} <span>→</span> ${escapeHTML(data.stops.at(-1).name.replace(' Jn',''))}`;
    const total = mode === 'LIVE' ? data.total_distance_km : data.route.at(-1).km;
    $('route-distance').textContent = Number.isFinite(total) ? `${total.toLocaleString()} km` : 'Distance unavailable';
    if (mode === 'DEMO') {
      $('speed').value = data.sim_speed;
      $('journey-date').value = data.journey_start_date;
      $('journey-date').min = data.journey_start_date;
      $('journey-date').max = data.journey_start_date;
      if (changedTrain) $('jump-stop').innerHTML = '<optgroup label="Stations">' + data.stops.slice(1).map(s => `<option value="${s.code}">${escapeHTML(s.name)} (${s.code})</option>`).join('') + '</optgroup><optgroup label="Demo events (when available)"><option value="event:congestion">Congestion report</option><option value="event:gps_gap">GPS gap</option></optgroup>';
    }
    renderHero(); renderStatus(); renderTimeline(); renderChanges(); drawMap(); tick();
    showError('');
  } catch (error) {
    if (id !== requestID) return;
    failureCount++;
    const retry = Number.isFinite(error.retryAfter) ? error.retryAfter : 0;
    nextPoll = Math.max(retry, Math.min(900, (mode === 'LIVE' ? livePollSeconds : 5) * 2 ** Math.min(failureCount-1,4)));
    retryAt = Date.now() + nextPoll * 1000;
    if(mode === 'LIVE') liveRetryAt = retryAt;
    if (mode === 'LIVE' && data) { data.stale = true; data.fetch_failed = true; renderHero(); renderStatus(); renderTimeline(); drawMap(); tick(); }
    showError((['TypeError','AbortError','SyntaxError'].includes(error.name) ? 'Could not retrieve train status.' : error.message) + ` Retrying in ${Math.ceil(nextPoll)} s.`);
    if (mode === 'LIVE' && !data) {
      $('live-info').textContent = 'LIVE DATA UNAVAILABLE · RailRadar · No simulated fallback';
      $('live-info').classList.add('stale');
    }
  } finally {
    clearTimeout(timeout);
    if (id === requestID) {
      loading = false;
      $('track-button').disabled = false;
      $('track-button').innerHTML = 'Track train <span aria-hidden="true">→</span>';
      tick();
      schedulePoll(nextPoll);
    }
  }
}
function clearView() {
  data = null; previous = {}; changes = []; flashes = {}; mappedTrain = null;
  if (mode === 'LIVE') { $('live-info').textContent = 'Connecting to RailRadar…'; $('live-info').classList.remove('stale'); }
  routeLayer?.clearLayers(); dynamicLayer?.clearLayers();
  if (trainMarker) map.removeLayer(trainMarker);
  if (accuracyCircle) map.removeLayer(accuracyCircle);
  trainMarker = accuracyCircle = null;
  $('route-title').textContent = 'Journey status'; $('train-title').textContent = 'Waiting for train data';
  $('route-distance').textContent = '—'; $('destination-hero').innerHTML = '<div class="skeleton">Arrival information pending</div>';
  $('status-card').innerHTML = ''; $('timeline').innerHTML = '<p class="empty-note">Track a train to load its stops.</p>';
  $('map-location').textContent = 'Location unavailable'; $('map-position').textContent = '';
  $('map-source').textContent = 'UNAVAILABLE'; $('stop-count').textContent = '';
  renderChanges();
}
function track(event) {
  event?.preventDefault();
  const number = $('train-input').value.trim();
  const day = mode === 'LIVE' ? $('journey-date').value : '';
  const changed = selected !== number || selectedDate !== day;
  clearTimeout(pollTimer);
  controller?.abort(); requestID++; loading = false;
  selected = number; selectedDate = day;
  if (changed) clearView();
  if (!selected) return;
  $('track-button').disabled = true;
  $('track-button').innerHTML = '<span class="spinner" aria-hidden="true"></span> Locating…';
  if (Date.now() < retryAt) {
    $('track-button').disabled = false;
    $('track-button').textContent = 'Track train';
    showError(`Provider retry pause: ${Math.ceil((retryAt-Date.now())/1000)} s remaining.`);
  }
  load(true);
}
async function control(path, body, method='POST') {
  const buttons = document.querySelectorAll('.controls button, .controls select');
  buttons.forEach(b => b.disabled = true);
  try {
    await api(path, {method, ...(body ? {body:JSON.stringify({...body,train_number:data?.train_number || selected})} : {})});
    if (body?.restart || body?.jump_before || body?.jump_event) { previous={}; changes=[]; flashes={}; }
    $('control-status').textContent = 'Updated';
    await load();
    setTimeout(() => { $('control-status').textContent = ''; },3000);
  } catch(error) { showError(error.message); }
  finally { buttons.forEach(b => b.disabled = false); }
}
async function loadEvaluation() {
  try {
    const result = await api('/api/evaluation');
    const rows = [...result.horizons, result.overall];
    const max = Math.max(...rows.flatMap(r => [r.baseline.mae,r.model.mae]));
    $('evaluation').innerHTML = `<table><thead><tr><th>Prediction horizon</th><th>Baseline MAE</th><th>RailETA MAE</th><th>Error comparison</th><th>Improvement</th><th>Within ±10 min (B / M)</th></tr></thead><tbody>${rows.map(r => `<tr><td>${escapeHTML(r.label)}<small style="display:block;color:#8998aa;font-size:8px;margin-top:4px">${r.count} predictions</small></td><td>${r.baseline.mae.toFixed(1)} min</td><td class="model-score">${r.model.mae.toFixed(1)} min</td><td><div class="bar-pair"><i style="width:${r.baseline.mae/max*100}%"></i><i style="width:${r.model.mae/max*100}%"></i></div></td><td>${signed(r.improvement_pct)}%</td><td>${r.baseline.within_10_pct.toFixed(0)}% / ${r.model.within_10_pct.toFixed(0)}%</td></tr>`).join('')}</tbody></table><p class="evaluation-note">MAE = mean absolute error; lower is better. Grey = baseline, blue = RailETA. First 45 dates train the model; last 15 dates per train are held out. Conditions are assumed reported before the affected section. Uncertainty ranges are heuristic, not calibrated guarantees. Simulated performance does not establish real-world accuracy.</p>`;
    $('accuracy-summary').textContent = `${Math.abs(result.overall.improvement_pct).toFixed(0)}% ${result.overall.improvement_pct >= 0 ? 'lower' : 'higher'} mean error`;
  } catch(error) { $('evaluation').innerHTML = '<p class="evaluation-note">Backtest unavailable. Run python evaluate.py, then reload this page.</p>'; $('accuracy-summary').textContent = 'Results pending'; }
}

$('track-form').addEventListener('submit', track);
$('follow').addEventListener('change', drawMap);
$('passing').addEventListener('change', drawMap);
$('fit-route').addEventListener('click', () => { if(map && data) { const points=data.route.filter(s=>Number.isFinite(s.lat)&&Number.isFinite(s.lon)).map(s=>[s.lat,s.lon]); if(points.length) map.fitBounds(points,{padding:[35,50]}); } });
$('speed').addEventListener('change', event => control('/api/sim',{speed:Number(event.target.value)}));
$('restart').addEventListener('click', () => control('/api/sim',{restart:true}));
$('jump').addEventListener('click', () => { const value=$('jump-stop').value; control('/api/sim',value.startsWith('event:') ? {jump_event:value.slice(6)} : {jump_before:value}); });
document.querySelectorAll('[data-condition]').forEach(button => button.addEventListener('click', () => control('/api/conditions',{type:button.dataset.condition})));
$('clear-reports').addEventListener('click', () => control('/api/conditions',null,'DELETE'));
$('how-open').addEventListener('click', () => $('how-modal').showModal());
$('how-close').addEventListener('click', () => $('how-modal').close());
$('how-modal').addEventListener('click', event => { if(event.target === $('how-modal')) { const box=event.target.getBoundingClientRect(); if(event.clientX<box.left || event.clientX>box.right || event.clientY<box.top || event.clientY>box.bottom) event.target.close(); } });
$('data-mode').addEventListener('change', () => switchMode($('data-mode').value));
$('refresh-live').addEventListener('click', () => { clearTimeout(pollTimer); load(); });
document.addEventListener('visibilitychange', () => {
  clearTimeout(pollTimer);
  if (!document.hidden) load();
});
initMap();
api('/api/config').then(config => {
  livePollSeconds = config.live_poll_seconds;
  switchMode(config.data_mode);
}).catch(() => { showError('Could not load app configuration. Reload to retry.'); clearView(); });
setInterval(tick,1000);

// Live presentation uses the same cards; source and freshness stay explicit.
function switchMode(nextMode) {
  controller?.abort(); requestID++; loading = false; clearTimeout(pollTimer);
  mode = nextMode;
  retryAt = mode === 'LIVE' ? liveRetryAt : 0;
  failureCount = 0;
  $('data-mode').value = mode;
  const live = mode === 'LIVE';
  $('data-badge').classList.toggle('live',live);
  $('data-badge').textContent = live ? 'LIVE DATA · RAILRADAR' : 'DEMO / SIMULATION DATA';
  document.querySelector('.controls').hidden = live;
  $('accuracy').hidden = live;
  $('refresh-live').hidden = !live;
  $('live-info').hidden = !live;
  $('live-info').textContent = 'Connecting to RailRadar…';
  $('live-info').classList.remove('stale');
  $('journey-label').textContent = live ? 'PROVIDER-REPORTED JOURNEY' : 'JOURNEY REPLAY';
  $('journey-date').value = live ? '' : dateIST(new Date());
  $('journey-date').min = live ? '' : $('journey-date').value;
  $('journey-date').max = live ? '' : $('journey-date').value;
  $('journey-date').required = !live;
  $('journey-date').title = live ? 'Origin departure date; leave blank for the provider’s current run' : 'Replay starts today in IST';
  $('date-label').textContent = live ? 'Journey date · optional' : 'Journey date · IST';
  $('data-disclaimer').textContent = live ? 'Third-party status from RailRadar. Positions and ETAs may be estimated or delayed. Not affiliated with Indian Railways.' : 'Simulated data for demonstration. Coordinates approximate, timetable illustrative. Not affiliated with Indian Railways.';
  document.querySelector('.map-credit').textContent = live ? 'Provider route' : 'Approximate route';
  document.querySelectorAll('.map-legend > span:nth-child(n+3):not(.map-credit)').forEach(el => el.hidden = live);
  $('live-explanation').hidden = !live;
  document.querySelectorAll('.how-cycle,.model-equation,.modal-note,.modal-intro:not(#live-explanation)').forEach(el => el.hidden = live);
  $('sim-clock').textContent = live ? 'Live status · RailRadar' : 'Sim time · Connecting…';
  $('train-list').innerHTML = '';
  clearView(); showError('');
  if (!live) {
    const requestedMode = mode;
    api('/api/trains').then(trains => { if(mode === requestedMode) $('train-list').innerHTML = trains.map(t=>`<option value="${escapeHTML(t.train_number)}">${escapeHTML(t.name)}</option>`).join(''); }).catch(()=>{});
    loadEvaluation();
  }
  track();
}
function renderLiveHero() {
  const stop = data.stops.at(-1);
  const complete = data.journey_status === 'COMPLETED';
  const cancelled = data.journey_status === 'CANCELLED';
  const arrival = complete ? stop.actual_arr : stop.predicted_eta;
  const heading = cancelled ? 'JOURNEY CANCELLED' : complete ? 'JOURNEY COMPLETE' : 'DESTINATION ARRIVAL';
  const label = complete ? 'REPORTED ARRIVAL' : data.stale ? 'LAST REPORTED ETA' : 'RAILRADAR ETA';
  $('destination-hero').innerHTML = `<div class="hero-top"><span class="eyebrow">${heading}</span><span class="hero-kicker">${label}</span></div><div class="hero-name">${escapeHTML(stop.name)}</div><div class="hero-time-row"><strong class="hero-time">${cancelled ? '—' : timeHTML(arrival)}</strong>${arrival ? delayPill(complete ? stop.delay_min : stop.predicted_delay_min) : ''}</div><div class="hero-range">${data.stale ? 'Stale or unverified observation · ' : ''}${arrival ? 'Reported by RailRadar · No calibrated uncertainty range' : 'Provider arrival estimate unavailable'}</div><div class="hero-bottom"><span>Baseline<b>${timeHTML(stop.baseline_eta)}</b></span><span class="updated-age">Checking observation age…</span></div>`;
}
function renderLiveStatus() {
  const p = data.position;
  const progress = Number.isFinite(p.progress_pct) ? `${p.progress_pct.toFixed(1)}% complete` : 'Progress unavailable';
  const km = Number.isFinite(p.km_from_origin) ? `${p.km_from_origin.toFixed(1)} km covered` : 'Distance unavailable';
  const speed = Number.isFinite(p.speed_kmh) ? Math.round(p.speed_kmh) : '—';
  $('status-card').innerHTML = `<div class="section-heading"><span class="status-heading">${escapeHTML(data.journey_status.replaceAll('_',' '))} · ${escapeHTML(p.station_name)}</span><span class="source-badge ${sourceClass(p)}">${sourceName(p)}</span></div><div class="status-metrics"><div class="metric"><div class="metric-value">${speed} <small>km/h</small></div><div class="metric-label">${Number.isFinite(p.speed_kmh) ? 'Provider-reported speed' : 'Speed unavailable'}</div></div><div class="metric"><div class="metric-value">${signed(data.current_delay_min)} <small>min</small></div><div class="metric-label">Provider-reported delay</div></div></div><div class="progress-label"><span>${km}</span><span>${progress}</span></div><div class="progress-track"><span style="width:${Number.isFinite(p.progress_pct) ? Math.max(0,Math.min(100,p.progress_pct)) : 0}%"></span></div><p class="evaluation-note">${escapeHTML(p.basis)}</p><div class="status-bottom"><span>Observed ${timeHTML(data.observed_at)}</span><span>Fetched ${timeHTML(data.fetched_at)}</span></div>`;
  $('map-location').textContent = p.source === 'UNAVAILABLE' ? 'Location unavailable' : `Reported near ${p.station_name}`;
  $('map-position').textContent = `${km} · ${signed(data.current_delay_min)} min reported delay`;
  $('map-source').className = `source-badge ${sourceClass(p)}`;
  $('map-source').textContent = sourceName(p);
}
function renderLiveTimeline() {
  $('stop-count').textContent = `${data.stops.length} STOPS`;
  $('timeline').innerHTML = data.stops.map((stop,index) => {
    const passed = ['DEPARTED','ARRIVED'].includes(stop.status);
    const flash = flashes[stop.code];
    const highlighted = flash && flash.until > Date.now();
    const actual = stop.actual_arr || stop.actual_dep;
    const times = actual ? `<p class="actual-times">${stop.actual_arr ? `Reported arrival ${timeHTML(stop.actual_arr)}` : 'Arrival not reported'}${stop.actual_dep ? ` · Departure ${timeHTML(stop.actual_dep)}` : ''}</p>` : '';
    const comparison = !passed ? `<div class="time-grid"><div class="time-cell"><span>SCHEDULED</span><b>${timeHTML(stop.sched_arr || stop.sched_dep)}</b></div><div class="time-cell"><span>BASELINE</span><b>${timeHTML(stop.baseline_eta)}</b></div><div class="time-cell predicted"><span>RAILRADAR ETA</span><b>${timeHTML(stop.predicted_eta)}</b></div></div><div class="prediction-meta">${stop.predicted_eta ? delayPill(stop.predicted_delay_min) : 'Provider ETA unavailable'}${data.stale && stop.predicted_eta ? ' · STALE' : ''}</div>` : !actual ? '<p class="actual-times">Reported as departed; actual time unavailable</p>' : '';
    return `<article class="stop ${passed ? 'passed' : ''} ${index===data.stops.length-1 ? 'destination' : ''} ${highlighted ? 'stop-changed' : ''}"><div class="stop-name"><h3>${escapeHTML(stop.name)}<span class="station-code">${escapeHTML(stop.code)}</span></h3><span class="stop-distance">${Number.isFinite(stop.km_to_go) && !passed ? Math.round(stop.km_to_go)+' km to go' : ''}</span></div>${times}${comparison}<div class="reasons"><span class="reason">${escapeHTML(stop.status.replaceAll('_',' '))} · RailRadar</span></div>${highlighted ? `<span class="eta-change ${flash.delta<0 ? 'earlier' : ''}">${signed(flash.delta)} min</span>` : ''}</article>`;
  }).join('');
}
function tickLive() {
  const age = data.observed_at ? Math.max(0,(Date.now()-Date.parse(data.observed_at))/1000) : null;
  if (age !== null && age > data.stale_after_seconds && !data.stale) {
    data.stale = true; renderHero(); renderStatus(); renderTimeline(); drawMap();
  }
  const ageLabel = age === null ? 'Observation age unknown' : `Observed ${Math.floor(age/60)}m ${Math.floor(age%60)}s ago`;
  $('sim-clock').textContent = `${clockFormat.format(new Date())} IST · RailRadar`;
  document.querySelectorAll('.updated-age').forEach(el=>el.textContent=ageLabel);
  $('live-info').classList.toggle('stale',data.stale);
  const details = data.warnings.length ? ` · ${data.warnings.join('; ')}` : '';
  const retry = data.fetch_failed ? ' · Fetch failed; showing last result' : '';
  $('live-info').textContent = `${data.stale ? 'STALE / UNVERIFIED' : 'LIVE'} · RailRadar · Journey ${data.journey_start_date} · ${ageLabel} · Last successful fetch ${clockFormat.format(new Date(data.fetched_at))} IST · Refresh ${livePollSeconds}s${details}${retry}`;
  if (Object.values(flashes).some(f=>f.until<=Date.now())) {
    Object.keys(flashes).forEach(key=>{if(flashes[key].until<=Date.now())delete flashes[key];});
    renderTimeline();
  }
}
