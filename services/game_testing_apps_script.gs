
var SECRET = 'CHANGE_ME';   // same value as the bot's env secret; set it in the Apps Script editor, never commit it
var WEEK_RE = /(\d{1,2})\/(\d{1,2})\s*-\s*(\d{1,2})\/(\d{1,2})/;
var PLAT_RE = /\b(xbox|steam)\b/i;
function _plat(t) { var m = PLAT_RE.exec(String(t || '')); return m ? (m[1].toLowerCase() === 'xbox' ? 'Xbox' : 'Steam') : ''; }

function test() {
  var d = _list();
  Logger.log('weeks: ' + d.join(', '));
  if (d.length) Logger.log(JSON.stringify(_readWeek(d[0])).slice(0, 2000));
}

function _json(obj) { return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON); }

function _list() {
  var sheets = SpreadsheetApp.getActive().getSheets(), out = [];
  for (var i = 0; i < sheets.length; i++) {
    var n = sheets[i].getName();
    if (WEEK_RE.test(n)) out.push({ name: n, idx: i });
  }
  out.sort(function (a, b) { return b.idx - a.idx; });
  return out.map(function (x) { return x.name; });
}

function _headerRow(values) {
  for (var r = 0; r < values.length; r++) {
    if (String(values[r][0] || '').trim().toLowerCase() === 'game name') return r;
  }
  return -1;
}

function _options(ws, row1, col1) {
  // One call per column (getDataValidations on the whole block) instead of one call per cell:
  // the per-cell version made a read take 40-60+ seconds on a 3-week sheet.
  var last = ws.getLastRow();
  var n = Math.min(last, row1 + 60) - row1 + 1;
  if (n < 1) return [];
  var rules = ws.getRange(row1, col1, n, 1).getDataValidations();
  for (var i = 0; i < rules.length; i++) {
    var rule = rules[i][0];
    if (!rule) continue;
    var t = rule.getCriteriaType(), v = rule.getCriteriaValues();
    if (t === SpreadsheetApp.DataValidationCriteria.VALUE_IN_LIST) return (v[0] || []).map(String);
    if (t === SpreadsheetApp.DataValidationCriteria.VALUE_IN_RANGE) {
      return v[0].getValues().map(function (x) { return String(x[0]); }).filter(function (x) { return x; });
    }
  }
  return [];
}

function _readWeek(name) {
  var ws = SpreadsheetApp.getActive().getSheetByName(name);
  if (!ws) return null;
  var values = ws.getDataRange().getValues();
  var hr = _headerRow(values);
  if (hr < 0) return { week: name, columns: [], rows: [] };
  var head = values[hr], columns = [];
  for (var c = 1; c < head.length; c++) {
    var h = String(head[c] || '').trim();
    if (!h) continue;
    columns.push({ name: h, col: c + 1, options: _options(ws, hr + 2, c + 1) });
  }
  var rows = [], plat = 'Steam', blank = 0;
  for (var r = hr + 1; r < values.length; r++) {
    var game = String(values[r][0] || '').trim();
    if (!game) {
      if (!values[r].some(function (x) { return String(x || '').trim(); })) blank++;
      continue;
    }
    if (blank >= 3 && rows.length && plat === 'Steam') plat = 'Xbox';
    blank = 0;
    var rest = values[r].slice(1).some(function (x) { return String(x || '').trim(); });
    if (!rest && _plat(game) && game.length <= 30) { plat = _plat(game); continue; }
    var vals = {};
    columns.forEach(function (col) { vals[col.name] = String(values[r][col.col - 1] == null ? '' : values[r][col.col - 1]).trim(); });
    var any = columns.some(function (col) { return vals[col.name]; });
    var vr = r + 1;
    if (!any && r + 1 < values.length && !String(values[r + 1][0] || '').trim()) {
      var below = {};
      columns.forEach(function (col) { below[col.name] = String(values[r + 1][col.col - 1] == null ? '' : values[r + 1][col.col - 1]).trim(); });
      if (columns.some(function (col) { return below[col.name]; })) { vals = below; vr = r + 2; }
    }
    rows.push({ game: game, row: vr, platform: plat, values: vals });
  }
  return { week: name, columns: columns.map(function (c) { return { name: c.name, options: c.options }; }), rows: rows };
}

function doGet(e) {
  if (!e || !e.parameter || String(e.parameter.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  var weeks = _list();
  var want = String(e.parameter.weeks || '3');
  var n = Math.max(1, Math.min(8, parseInt(want, 10) || 3));
  var out = {};
  weeks.slice(0, n).forEach(function (w) { out[w] = _readWeek(w); });
  return _json({ ok: true, order: weeks, weeks: out });
}

function doPost(e) {
  var body = {};
  try { body = JSON.parse(e.postData.contents || '{}'); } catch (x) { return _json({ error: 'bad json' }); }
  if (String(body.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  if (body.action !== 'set') return _json({ error: 'unknown action' });
  var ws = SpreadsheetApp.getActive().getSheetByName(String(body.week || ''));
  if (!ws) return _json({ error: 'week not found' });
  var values = ws.getDataRange().getValues();
  var hr = _headerRow(values);
  if (hr < 0) return _json({ error: 'no Game Name header' });
  var col = -1;
  for (var c = 1; c < values[hr].length; c++) { if (String(values[hr][c] || '').trim() === String(body.col || '')) { col = c + 1; break; } }
  if (col < 0) return _json({ error: 'column not found' });
  var row = parseInt(body.row, 10);
  if (!(row > hr + 1)) return _json({ error: 'bad row' });
  var g = String(body.game || '').trim();
  if (g) {
    var here = String(ws.getRange(row, 1).getValue() || '').trim(), above = String(ws.getRange(row - 1, 1).getValue() || '').trim();
    if (here !== g && !(here === '' && above === g)) return _json({ error: 'row moved, sync and try again' });
  }
  var value = String(body.value == null ? '' : body.value);
  var opts = _options(ws, hr + 2, col);
  if (value && opts.length && opts.indexOf(value) < 0) return _json({ error: 'value not allowed: ' + value });
  ws.getRange(row, col).setValue(value);
  return _json({ ok: true });
}
