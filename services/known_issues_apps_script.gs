var SECRET = 'CHANGE_ME_to_a_long_random_string';

var TABS = { games: 'Games', apps: 'Apps', session: 'Session', account: 'Account' };
var ENTITY_CATS = ['games', 'apps'];

function test() {
  var data = _readAll();
  Object.keys(data).forEach(function (cat) {
    Logger.log(cat + ': ' + data[cat].rows.length + ' rows (has_entity=' + data[cat].has_entity + ')');
  });
  Logger.log(JSON.stringify(data).slice(0, 1000));
}

function _colMap(headers) {
  var m = {};
  for (var i = 0; i < headers.length; i++) {
    var hl = String(headers[i] || '').trim().toLowerCase();
    if (!hl) continue;
    if ((hl === 'game' || hl === 'application' || hl === 'app') && m.entity == null) m.entity = i;
    else if (hl.indexOf('problem') === 0 && m.problem == null) m.problem = i;
    else if ((hl.indexOf('solution') >= 0 || hl.indexOf('explanation') >= 0) && m.solution == null) m.solution = i;
    else if (hl.indexOf('canned') >= 0 && m.canned == null) m.canned = i;
    else if (hl === 'link' && m.link == null) m.link = i;
  }
  if (m.canned == null) {
    for (var j = 0; j < headers.length; j++) {
      if (String(headers[j] || '').toLowerCase().indexOf('message') >= 0) { m.canned = j; break; }
    }
  }
  return m;
}

function _readTab(cat) {
  var hasEntity = ENTITY_CATS.indexOf(cat) >= 0;
  var out = { has_entity: hasEntity, rows: [] };
  var ws = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(TABS[cat]);
  if (!ws) return out;
  var values = ws.getDataRange().getValues();
  if (values.length < 2) return out;
  var cm = _colMap(values[0]);
  var last = '';
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var cell = function (k) { return (cm[k] != null && row[cm[k]] != null) ? String(row[cm[k]]).trim() : ''; };
    var entity = hasEntity ? cell('entity') : '';
    if (hasEntity) { if (entity) last = entity; else entity = last; }
    var problem = cell('problem'), solution = cell('solution'), link = cell('link'), canned = cell('canned');
    if (!problem && !solution && !canned && !(hasEntity && entity)) continue;
    out.rows.push({ entity: entity, problem: problem, solution: solution, link: link, canned: canned });
  }
  return out;
}

function _readAll() {
  var data = {};
  Object.keys(TABS).forEach(function (cat) { data[cat] = _readTab(cat); });
  return data;
}

function _insertIndex(values, hasEntity, newFirst) {
  var key = String(newFirst || '').trim().toLowerCase();
  var n = values.length;
  if (n <= 1) return 2;
  var eff = [], last = '';
  for (var i = 1; i < n; i++) {
    var v = values[i][0] != null ? String(values[i][0]).trim() : '';
    if (hasEntity) { if (v) last = v; eff.push(last); } else eff.push(v);
  }
  if (hasEntity) {
    var lastGroup = -1;
    for (var g = 0; g < eff.length; g++) if (eff[g].toLowerCase() === key) lastGroup = g;
    if (lastGroup >= 0) return lastGroup + 3;
  }
  for (var k = 0; k < eff.length; k++) if (eff[k].toLowerCase() > key) return k + 2;
  return n + 1;
}

function _addRow(cat, f) {
  var ws = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(TABS[cat]);
  if (!ws) throw new Error('unknown tab for category ' + cat);
  var values = ws.getDataRange().getValues();
  var headers = values.length ? values[0] : [];
  var cm = _colMap(headers);
  var hasEntity = ENTITY_CATS.indexOf(cat) >= 0;
  var width = Math.max(headers.length, 1);
  var row = [];
  for (var i = 0; i < width; i++) row.push('');
  var map = { entity: hasEntity ? (f.entity || '') : '', problem: f.problem || '', solution: f.solution || '',
              link: f.link || '', canned: f.canned || '' };
  Object.keys(cm).forEach(function (k) { if (cm[k] < row.length) row[cm[k]] = map[k] || ''; });
  var newFirst = hasEntity ? map.entity : map.problem;
  var idx = _insertIndex(values, hasEntity, newFirst);
  if (idx >= values.length + 1) {
    ws.appendRow(row);
  } else {
    ws.insertRowBefore(idx);
    ws.getRange(idx, 1, 1, row.length).setValues([row]);
  }
  return true;
}

// Fill the Canned Message cell of ONE existing row (an approved suggestion from the public page).
// The row is found by Problem (+ Game/Application on those tabs, forward-filled like _readTab),
// compared trimmed + lowercase. Never overwrites: a cell someone already filled is left as is.
function _norm(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim().toLowerCase(); }
function _setCanned(cat, f) {
  var ws = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(TABS[cat]);
  if (!ws) throw new Error('unknown tab for category ' + cat);
  var values = ws.getDataRange().getValues();
  if (values.length < 2) throw new Error('row not found');
  var cm = _colMap(values[0]);
  if (cm.canned == null || cm.problem == null) throw new Error('no Canned Message / Problem column');
  var hasEntity = ENTITY_CATS.indexOf(cat) >= 0;
  var wantE = _norm(f.entity), wantP = _norm(f.problem), last = '';
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var e = hasEntity && cm.entity != null ? String(row[cm.entity] || '').trim() : '';
    if (hasEntity) { if (e) last = e; else e = last; }
    if (_norm(row[cm.problem]) !== wantP) continue;
    if (hasEntity && _norm(e) !== wantE) continue;
    if (String(row[cm.canned] || '').trim()) return { ok: false, error: 'already has a canned message' };
    ws.getRange(r + 1, cm.canned + 1).setValue(String(f.canned || ''));
    return { ok: true };
  }
  return { ok: false, error: 'row not found' };
}

// Overwrite Problem / Solution / Canned Message of ONE existing row (an approved "Suggest edit").
// Found the same way as _setCanned (Problem + forward-filled Game/Application, trimmed + lowercase).
function _updateRow(cat, f) {
  var ws = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(TABS[cat]);
  if (!ws) throw new Error('unknown tab for category ' + cat);
  var values = ws.getDataRange().getValues();
  if (values.length < 2) throw new Error('row not found');
  var cm = _colMap(values[0]);
  if (cm.problem == null) throw new Error('no Problem column');
  var hasEntity = ENTITY_CATS.indexOf(cat) >= 0;
  var wantE = _norm(f.entity), wantP = _norm(f.problem), last = '';
  var set = f.set || {};
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var e = hasEntity && cm.entity != null ? String(row[cm.entity] || '').trim() : '';
    if (hasEntity) { if (e) last = e; else e = last; }
    if (_norm(row[cm.problem]) !== wantP) continue;
    if (hasEntity && _norm(e) !== wantE) continue;
    ['problem', 'solution', 'canned'].forEach(function (k) {
      if (set[k] != null && cm[k] != null) ws.getRange(r + 1, cm[k] + 1).setValue(String(set[k]));
    });
    return { ok: true };
  }
  return { ok: false, error: 'row not found' };
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  if (!e || !e.parameter || String(e.parameter.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  return _json({ ok: true, categories: _readAll() });
}

function doPost(e) {
  var body = {};
  try { body = JSON.parse((e && e.postData && e.postData.contents) || '{}'); } catch (err) {}
  if (String(body.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  if (body.action === 'add') {
    try { _addRow(String(body.category || '').toLowerCase(), body); return _json({ ok: true }); }
    catch (err) { return _json({ error: String(err) }); }
  }
  if (body.action === 'update') {
    try { return _json(_updateRow(String(body.category || '').toLowerCase(), body)); }
    catch (err) { return _json({ error: String(err) }); }
  }
  if (body.action === 'set_canned') {
    try { return _json(_setCanned(String(body.category || '').toLowerCase(), body)); }
    catch (err) { return _json({ error: String(err) }); }
  }
  if (body.action === 'read') return _json({ ok: true, categories: _readAll() });
  return _json({ error: 'unknown action' });
}
