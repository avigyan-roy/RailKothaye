/* RailETA UI: one polling loop, no framework, dates rendered explicitly in IST. */
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const timeFormat = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Kolkata', hour:'2-digit', minute:'2-digit', hour12:false});
const clockFormat = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Kolkata', day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', hour12:false});
const dateIST = date => new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(date);
const trainSVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="5" y="3" width="14" height="15" rx="4"/><path d="M5 10h14M12 3v7M8 21l2-3m6 3-2-3M8 14h1m6 0h1"/></svg>';
let selected = '12301', data = null, fetchedAt = 0, previous = {}, changes = [], flashes = {};
let pollTimer, controller, requestID = 0, map, routeLayer, dynamicLayer, trainMarker, accuracyCircle, mappedTrain;

function timeHTML(value) {
  if (!value) return '—';
  const date = new Date(value);
  const base = data?.journey_start_date || dateIST(new Date());
  const days = Math.round((Date.parse(value.slice(0,10)) - Date.parse(base)) / 86400000);
  return `${timeFormat.format(date)}${days > 0 ? `<sup class="day-offset">+${days}</sup>` : ''}`;
}
function signed(value) { const rounded = Math.round(value); return `${rounded > 0 ? '+' : ''}${rounded}`; }
function delayPill(value) {
  const n = Math.round(value);
  const type = n < 0 ? 'early' : n <= 5 ? 'ontime' : n <= 15 ? '' : n <= 60 ? 'moderate' : 'severe';
  const label = n < 0 ? `${Math.abs(n)} min early` : n <= 5 ? `On time${n > 0 ? ` · +${n}` : ''}` : `+${n} min`;
  return `<span class="delay-pill ${type}">${label}</span>`;
}
function sourceClass(position) { return position.warnings.includes('SIGNAL_LOST') ? 'lost' : position.source === 'ESTIMATED' ? 'estimated' : ''; }
function sourceName(position) { return position.warnings.includes('SIGNAL_LOST') ? 'SIGNAL LOST' : position.source; }
function showError(message) { $('error').textContent = message; $('error').hidden = !message; }
async function api(path, options = {}) {
  const response = await fetch(path, {cache:'no-store', ...options, headers:{'Content-Type':'application/json',...options.headers}});
  const body = await response.json();
  if (!response.ok) throw new Error(body.available ? `${body.error}. Available demo trains: ${body.available.join(', ')}.` : body.error || 'Request failed');
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
  const p = data.position, route = data.route;
  const first = mappedTrain !== data.train_number;
  if (first) {
    routeLayer.clearLayers();
    L.polyline(route.map(s => [s.lat,s.lon]), {color:'#6e8fb7',weight:3,opacity:.8,dashArray:'5 7'}).addTo(routeLayer);
    mappedTrain = data.train_number;
  }
  dynamicLayer.clearLayers();
  const traveled = route.filter(s => s.km <= p.km_from_origin).map(s => [s.lat,s.lon]);
  traveled.push([p.latitude,p.longitude]);
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
    const label = stop ? `${escapeHTML(point.name)} (${escapeHTML(point.code)})<br>${stop.predicted_eta ? 'Predicted ' + timeHTML(stop.predicted_eta) : 'Actual ' + timeHTML(stop.actual_arr || stop.actual_dep)}` : `${escapeHTML(point.name)} · Passing point`;
    L.circleMarker([point.lat,point.lon], {radius:stop ? 5 : 2.5,color,fillColor:passed ? '#b6c1cc' : '#fff',fillOpacity:1,weight:2}).bindTooltip(label).addTo(dynamicLayer);
  }
  const klass = sourceClass(p);
  const icon = L.divIcon({className:'train-marker',html:`<div class="train-dot ${klass}">${trainSVG}</div>`,iconSize:[35,35],iconAnchor:[17.5,17.5]});
  if (trainMarker) map.removeLayer(trainMarker);
  if (accuracyCircle) map.removeLayer(accuracyCircle);
  accuracyCircle = L.circle([p.latitude,p.longitude], {radius:p.accuracy_m || 15,color:klass ? '#b38c53' : '#427cb5',weight:1,fillOpacity:.12}).addTo(map);
  trainMarker = L.marker([p.latitude,p.longitude], {icon,zIndexOffset:1000}).bindTooltip(p.source === 'MEASURED' ? 'Measured GPS position' : `Estimated position – last GPS ${Math.round(p.seconds_since_update/60)} min ago`).addTo(map);
  if (first) map.fitBounds(route.map(s => [s.lat,s.lon]), {padding:[35,50]});
  else if ($('follow').checked) map.panTo([p.latitude,p.longitude], {animate:true,duration:.6});
}
function renderHero() {
  const destination = data.stops.at(-1), arrived = destination.status === 'ARRIVED';
  const eta = destination.predicted_eta || destination.actual_arr;
  $('destination-hero').innerHTML = `<div class="hero-top"><span class="eyebrow">${arrived ? 'JOURNEY COMPLETE' : 'DESTINATION ARRIVAL'}</span><span class="hero-kicker">${arrived ? 'ACTUAL' : 'PREDICTED ETA'}</span></div><div class="hero-name">${arrived ? 'Arrived at' : 'Arriving'} ${escapeHTML(destination.name)}</div><div class="hero-time-row"><strong class="hero-time">${timeHTML(eta)}</strong>${delayPill(destination.predicted_delay_min ?? destination.delay_min ?? 0)}</div><div class="hero-range">${arrived ? 'Arrival confirmed by recorded GPS' : `Expected range ${timeHTML(destination.eta_low)} – ${timeHTML(destination.eta_high)} <span> · ${escapeHTML(destination.confidence?.toLowerCase())} confidence</span>`}</div><div class="hero-bottom"><span>${arrived ? 'Scheduled' : 'Baseline'}<b>${timeHTML(destination.baseline_eta || destination.sched_arr)}</b></span><span class="updated-age">Updated just now</span></div>`;
}
function renderStatus() {
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
  if (!data) return;
  const elapsed = Math.max(0,(Date.now()-fetchedAt)/1000);
  const simulated = new Date(Date.parse(data.sim_time)+elapsed*data.sim_speed*1000);
  $('sim-clock').textContent = `Sim time ${clockFormat.format(simulated)} IST · ${data.sim_speed}×`;
  document.querySelectorAll('.updated-age').forEach(el => { el.textContent = `Updated ${Math.floor(elapsed)} s ago`; });
  if (Object.values(flashes).some(f => f.until <= Date.now())) {
    Object.keys(flashes).forEach(key => { if (flashes[key].until <= Date.now()) delete flashes[key]; });
    renderTimeline();
  }
}
async function load(first = false) {
  controller?.abort();
  controller = new AbortController();
  const id = ++requestID;
  const timeout = setTimeout(() => controller?.abort(), 12000);
  try {
    const next = await api(`/api/train/${encodeURIComponent(selected)}/live`, {signal:controller.signal});
    if (id !== requestID) return;
    const changedTrain = data?.train_number !== next.train_number;
    if (changedTrain) { previous = {}; changes = []; flashes = {}; }
    recordChanges(next);
    data = next;
    fetchedAt = Date.now();
    $('train-title').textContent = `${data.train_number} · ${data.train_name}`;
    $('route-title').innerHTML = `${escapeHTML(data.stops[0].name.replace(' Jn',''))} <span>→</span> ${escapeHTML(data.stops.at(-1).name.replace(' Jn',''))}`;
    $('route-distance').textContent = `${data.route.at(-1).km.toLocaleString()} km`;
    $('speed').value = data.sim_speed;
    $('journey-date').value = data.journey_start_date;
    $('journey-date').min = data.journey_start_date;
    $('journey-date').max = data.journey_start_date;
    if (changedTrain) $('jump-stop').innerHTML = '<optgroup label="Stations">' + data.stops.slice(1).map(s => `<option value="${s.code}">${escapeHTML(s.name)} (${s.code})</option>`).join('') + '</optgroup><optgroup label="Demo events (when available)"><option value="event:congestion">Congestion report</option><option value="event:gps_gap">GPS gap</option></optgroup>';
    renderHero(); renderStatus(); renderTimeline(); renderChanges(); drawMap(); tick();
    showError('');
  } catch (error) {
    if (id !== requestID) return;
    showError(error.name === 'TypeError' || error.name === 'AbortError' ? 'Connection lost – retrying…' : error.message);
  } finally {
    clearTimeout(timeout);
    if (id === requestID) { $('track-button').disabled = false; $('track-button').innerHTML = 'Track train <span aria-hidden="true">→</span>'; }
  }
}
function track(event) {
  event?.preventDefault();
  selected = $('train-input').value.trim();
  clearInterval(pollTimer);
  if (!selected) return;
  $('track-button').disabled = true;
  $('track-button').innerHTML = '<span class="spinner" aria-hidden="true"></span> Locating…';
  load(true);
  pollTimer = setInterval(() => load(),5000);
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
$('fit-route').addEventListener('click', () => { if(map && data) map.fitBounds(data.route.map(s=>[s.lat,s.lon]),{padding:[35,50]}); });
$('speed').addEventListener('change', event => control('/api/sim',{speed:Number(event.target.value)}));
$('restart').addEventListener('click', () => control('/api/sim',{restart:true}));
$('jump').addEventListener('click', () => { const value=$('jump-stop').value; control('/api/sim',value.startsWith('event:') ? {jump_event:value.slice(6)} : {jump_before:value}); });
document.querySelectorAll('[data-condition]').forEach(button => button.addEventListener('click', () => control('/api/conditions',{type:button.dataset.condition})));
$('clear-reports').addEventListener('click', () => control('/api/conditions',null,'DELETE'));
$('how-open').addEventListener('click', () => $('how-modal').showModal());
$('how-close').addEventListener('click', () => $('how-modal').close());
$('how-modal').addEventListener('click', event => { if(event.target === $('how-modal')) { const box=event.target.getBoundingClientRect(); if(event.clientX<box.left || event.clientX>box.right || event.clientY<box.top || event.clientY>box.bottom) event.target.close(); } });
const today = dateIST(new Date());
$('journey-date').value = today;
$('journey-date').min = today;
$('journey-date').max = today;
initMap();
api('/api/trains').then(trains => { $('train-list').innerHTML = trains.map(t=>`<option value="${escapeHTML(t.train_number)}">${escapeHTML(t.name)}</option>`).join(''); }).catch(()=>{});
track();
loadEvaluation();
setInterval(tick,1000);
