var SECRET = 'CHANGE_ME_to_a_long_random_string';

var COLS = ['title', 'description', 'platform', 'description_url', 'pegi', 'release_date', 'publisher', 'developer', 'app_link', 'youtube', 'related_apps'];
var HEADERS = ['Title', 'Description', 'Platform', 'Description URL (If Install)', 'PEGI Age Rating', 'Release Date', 'Publisher', 'Developer', 'App Link', 'Video Link on YouTube', 'Related Apps'];

function test() {
  var d = _readAll();
  Logger.log('weeks: ' + d.order.join(', '));
  d.order.forEach(function (w) { Logger.log(w + ': ' + d.weeks[w].rows.length + ' cards'); });
}

function _pad(n) { return (n < 10 ? '0' : '') + n; }
function _fmtVal(v) {
  if (v instanceof Date) return _pad(v.getDate()) + '.' + _pad(v.getMonth() + 1) + '.' + v.getFullYear();
  return String(v == null ? '' : v).trim();
}

function _colKey(h) {
  var s = String(h || '').toLowerCase();
  if (s.indexOf('title') >= 0) return 'title';
  if (s.indexOf('url') >= 0 || s.indexOf('install') >= 0) return 'description_url';
  if (s.indexOf('description') >= 0) return 'description';
  if (s.indexOf('platform') >= 0) return 'platform';
  if (s.indexOf('pegi') >= 0 || s.indexOf('age') >= 0) return 'pegi';
  if (s.indexOf('release') >= 0) return 'release_date';
  if (s.indexOf('publisher') >= 0) return 'publisher';
  if (s.indexOf('developer') >= 0) return 'developer';
  if (s.indexOf('youtube') >= 0 || s.indexOf('video') >= 0) return 'youtube';
  if ((s.indexOf('link') >= 0 && s.indexOf('app') >= 0)) return 'app_link';
  if (s.indexOf('related') >= 0) return 'related_apps';
  if (s.indexOf('link') >= 0) return 'app_link';
  return null;
}

function _colMap(headerRow) {
  var m = {};
  for (var c = 0; c < headerRow.length; c++) {
    var k = _colKey(headerRow[c]);
    if (k && m[k] == null) m[k] = c;
  }
  return m;
}

function _headerRowIndex(values) {
  for (var r = 0; r < Math.min(values.length, 4); r++) {
    for (var c = 0; c < values[r].length; c++) {
      if (String(values[r][c] || '').trim().toLowerCase().indexOf('title') >= 0) return r;
    }
  }
  return values.length ? 0 : -1;
}

function _readTab(ws) {
  var values = ws.getDataRange().getValues();
  var hr = _headerRowIndex(values);
  if (hr < 0) return null;
  var cm = _colMap(values[hr]);
  if (cm.title == null) return null;
  var rows = [];
  for (var r = hr + 1; r < values.length; r++) {
    var row = values[r];
    var card = { _row: r - hr - 1 };
    var any = false;
    COLS.forEach(function (k) { var c = cm[k]; var v = (c != null && row[c] != null) ? _fmtVal(row[c]) : ''; card[k] = v; if (v) any = true; });
    if (any) rows.push(card);
  }
  return { rows: rows };
}

function _readAll() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var weeks = {}, order = [];
  ss.getSheets().forEach(function (ws) {
    var parsed = _readTab(ws);
    if (parsed) { weeks[ws.getName()] = parsed; order.push(ws.getName()); }
  });
  return { weeks: weeks, order: order };
}

function _setCell(week, rowIndex, colKey, value) {
  var ws = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(week);
  if (!ws) throw new Error('week not found: ' + week);
  var values = ws.getDataRange().getValues();
  var hr = _headerRowIndex(values);
  if (hr < 0) throw new Error('no header');
  var cm = _colMap(values[hr]);
  var col = cm[colKey];
  if (col == null) throw new Error('column not found: ' + colKey);
  ws.getRange(hr + 1 + 1 + parseInt(rowIndex, 10), col + 1).setValue(value || '');
  return true;
}

function _setRow(week, rowIndex, card) {
  var ws = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(week);
  if (!ws) throw new Error('week not found: ' + week);
  var values = ws.getDataRange().getValues();
  var hr = _headerRowIndex(values);
  if (hr < 0) throw new Error('no header');
  var cm = _colMap(values[hr]);
  var sheetRow = hr + 1 + 1 + parseInt(rowIndex, 10);
  COLS.forEach(function (k) { if (card[k] != null && cm[k] != null) ws.getRange(sheetRow, cm[k] + 1).setValue(card[k] || ''); });
  return true;
}

function _deleteRow(week, rowIndex) {
  var ws = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(week);
  if (!ws) throw new Error('week not found: ' + week);
  var values = ws.getDataRange().getValues();
  var hr = _headerRowIndex(values);
  if (hr < 0) throw new Error('no header');
  var sheetRow = hr + 1 + 1 + parseInt(rowIndex, 10);   // same mapping as _setCell/_setRow (0-based data index)
  if (sheetRow <= hr + 1 || sheetRow > values.length) throw new Error('row out of range: ' + rowIndex);
  ws.deleteRow(sheetRow);
  return true;
}

function _rowArray(cm, ncol, card) {
  var arr = [];
  for (var i = 0; i < ncol; i++) arr.push('');
  COLS.forEach(function (k) { if (cm[k] != null && cm[k] < ncol) arr[cm[k]] = card[k] || ''; });
  return arr;
}

function _appendRows(week, cards) {
  var ws = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(week);
  if (!ws) throw new Error('week not found: ' + week);
  var values = ws.getDataRange().getValues();
  var hr = _headerRowIndex(values);
  if (hr < 0) throw new Error('no header');
  var headerRow = values[hr];
  var cm = _colMap(headerRow);
  var ncol = headerRow.length;
  var out = (cards || []).map(function (card) { return _rowArray(cm, ncol, card); });
  if (out.length) ws.getRange(values.length + 1, 1, out.length, ncol).setValues(out);
  return out.length;
}

function _createWeek(name) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  name = String(name || '').trim();
  if (!name) throw new Error('name required');
  if (ss.getSheetByName(name)) throw new Error('already exists: ' + name);
  var ws = ss.insertSheet(name, 0);
  ws.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold').setBackground('#4a6741').setFontColor('#ffffff').setWrap(true);
  ws.setFrozenRows(1);
  ws.setColumnWidth(2, 320);
  return name;
}

function _json(obj) { return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON); }

function doGet(e) {
  if (!e || !e.parameter || String(e.parameter.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  var d = _readAll();
  return _json({ ok: true, weeks: d.weeks, order: d.order });
}

function doPost(e) {
  var body = {};
  try { body = JSON.parse((e && e.postData && e.postData.contents) || '{}'); } catch (err) {}
  if (String(body.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  try {
    if (body.action === 'set') { _setCell(body.week, body.row_index, body.col, body.value || ''); return _json({ ok: true }); }
    if (body.action === 'set_row') { _setRow(body.week, body.row_index, body.card || {}); return _json({ ok: true }); }
    if (body.action === 'delete_row') { _deleteRow(body.week, body.row_index); return _json({ ok: true }); }
    if (body.action === 'append') { var n = _appendRows(body.week, body.cards || []); return _json({ ok: true, added: n }); }
    if (body.action === 'create_week') { var nm = _createWeek(body.week); return _json({ ok: true, name: nm }); }
    if (body.action === 'read') { var d = _readAll(); return _json({ ok: true, weeks: d.weeks, order: d.order }); }
  } catch (err) { return _json({ error: String(err) }); }
  return _json({ error: 'unknown action' });
}
