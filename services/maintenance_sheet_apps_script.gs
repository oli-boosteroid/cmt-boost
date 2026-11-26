var SECRET = 'CHANGE_ME';   // same value as the bot's env secret; set it in the Apps Script editor, never commit it
var TAB = '';
var LASTCOL = 14;
var MERGES = [[1, 3], [4, 5], [6, 7], [8, 9], [10, 11]];

function _sheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  return TAB ? ss.getSheetByName(TAB) : ss.getSheets()[0];
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function _targetRow(sh) {
  var maxRows = sh.getMaxRows();
  var colA = sh.getRange(1, 1, maxRows, 1).getValues();
  for (var r = 1; r < colA.length; r++) {
    var v = colA[r][0];
    if (v === null || v === '' || String(v).trim() === '') return r + 1;
  }
  sh.insertRowAfter(maxRows);
  return maxRows + 1;
}

function _addRow(body) {
  var sh = _sheet();
  if (!sh) return { error: 'sheet not found' };
  var newRow = _targetRow(sh);
  var tmpl = newRow - 1;
  if (tmpl >= 2) {
    var src = sh.getRange(tmpl, 1, 1, LASTCOL);
    var dst = sh.getRange(newRow, 1, 1, LASTCOL);
    src.copyTo(dst, SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
    src.copyTo(dst, SpreadsheetApp.CopyPasteType.PASTE_DATA_VALIDATION, false);
  }
  MERGES.forEach(function (m) {
    var rng = sh.getRange(newRow, m[0], 1, m[1] - m[0] + 1);
    if (!rng.getMergedRanges().length) rng.mergeAcross();
  });
  sh.getRange(newRow, 1).setValue(String(body.game || ''));
  sh.getRange(newRow, 6).setValue(String(body.status || 'Ready to work'));
  sh.getRange(newRow, 13).setValue(String(body.maintenance_in || ''));
  sh.getRange(newRow, 14).setValue(String(body.started_at || ''));
  return { ok: true, row: newRow };
}

function doPost(e) {
  var body = {};
  try { body = JSON.parse((e && e.postData && e.postData.contents) || '{}'); } catch (err) {}
  if (String(body.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  if (body.action === 'add') {
    try { return _json(_addRow(body)); } catch (err) { return _json({ error: String(err) }); }
  }
  return _json({ error: 'unknown action' });
}

function doGet(e) {
  if (!e || !e.parameter || String(e.parameter.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  return _json({ ok: true });
}
