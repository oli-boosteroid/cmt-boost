var SECRET = 'CHANGE_ME';   // same value as the bot's env secret; set it in the Apps Script editor, never commit it
var TAB = 'Game List';

function test() {
  var rows = _readGames();
  Logger.log(rows.length + ' games');
  Logger.log(JSON.stringify(rows.slice(0, 10)));
}

function _colMap(headers) {
  var m = {};
  for (var i = 0; i < headers.length; i++) {
    var hl = String(headers[i] || '').trim().toLowerCase();
    if (!hl) continue;
    if (hl === 'id' && m.id == null) m.id = i;
    else if ((hl === 'title' || hl === 'name' || hl === 'game') && m.title == null) m.title = i;
    else if (hl === 'platform' && m.platform == null) m.platform = i;
  }
  if (m.id == null) m.id = 0;
  if (m.title == null) m.title = 1;
  return m;
}

function _sheet() {
  // Prefer the tab named TAB; if it does not exist, auto-find the tab whose header row has both an ID
  // and a Title/Name/Game column, else the first sheet. So a renamed tab never breaks the script.
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var ws = ss.getSheetByName(TAB);
  if (ws) return ws;
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var vals = sheets[i].getDataRange().getValues();
    if (!vals.length) continue;
    var hasId = false, hasTitle = false;
    for (var c = 0; c < vals[0].length; c++) {
      var h = String(vals[0][c] || '').trim().toLowerCase();
      if (h === 'id') hasId = true;
      if (h === 'title' || h === 'name' || h === 'game') hasTitle = true;
    }
    if (hasId && hasTitle) return sheets[i];
  }
  return sheets.length ? sheets[0] : null;
}

function _readGames() {
  var ws = _sheet();
  if (!ws) return [];
  var values = ws.getDataRange().getValues();
  if (values.length < 2) return [];
  var cm = _colMap(values[0]);
  var out = [];
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var id = row[cm.id] != null ? String(row[cm.id]).trim() : '';
    var title = row[cm.title] != null ? String(row[cm.title]).trim() : '';
    if (!id || !title) continue;
    var platform = (cm.platform != null && row[cm.platform] != null) ? String(row[cm.platform]).trim() : '';
    out.push({ id: id, title: title, platform: platform });
  }
  return out;
}

function _kiColumn(headers) {
  for (var i = 0; i < headers.length; i++) {
    if (String(headers[i] || '').trim().toLowerCase() === 'known issues') return i + 1;
  }
  return 0;
}

function _writeLinks(links, notes) {
  var ws = _sheet();
  if (!ws) throw new Error('Game List tab not found');
  var values = ws.getDataRange().getValues();
  if (values.length < 2) return { ok: true, wrote: 0 };
  var headers = values[0];
  var cm = _colMap(headers);
  var kiCol = _kiColumn(headers);
  if (!kiCol) throw new Error('no "Known Issues" column header');
  var idRow = {};
  for (var r = 1; r < values.length; r++) {
    var id = values[r][cm.id] != null ? String(values[r][cm.id]).trim() : '';
    if (id) idRow[id] = r + 1;
  }
  var n = values.length - 1;
  // Build RichText links (NOT a =HYPERLINK() formula): a formula's argument separator is locale-
  // dependent (comma vs semicolon), which produced #ERROR! in every cell. RichText is locale-proof and
  // renders a real clickable link. Preserve whatever a non-game row already had as plain text.
  var col = [];
  for (var i = 0; i < n; i++) {
    var ex = values[i + 1][kiCol - 1];
    col.push([SpreadsheetApp.newRichTextValue().setText(ex != null ? String(ex) : '').build()]);
  }
  var noUnderline = SpreadsheetApp.newTextStyle().setUnderline(false).build();
  var wrote = 0;
  (links || []).forEach(function (lk) {
    var row = idRow[String(lk.id)];
    if (!row) return;
    var text = String(lk.text || '') || ' ';
    // Set the link first, then override the style over the exact character range so the link's default
    // underline is removed (whole-value styling can be ignored on a linked run).
    col[row - 2] = [SpreadsheetApp.newRichTextValue().setText(text).setLinkUrl(String(lk.url || '')).setTextStyle(0, text.length, noUnderline).build()];
    wrote++;
  });
  ws.getRange(2, kiCol, n, 1).setRichTextValues(col);
  var noteCount = 0;
  if (notes) {
    Object.keys(notes).forEach(function (id) {
      var row = idRow[id];
      if (!row) return;
      ws.getRange(row, kiCol).setNote(String(notes[id] || ''));
      noteCount++;
    });
  }
  return { ok: true, wrote: wrote, notes: noteCount };
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  if (!e || !e.parameter || String(e.parameter.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  return _json({ ok: true, games: _readGames() });
}

function doPost(e) {
  var body = {};
  try { body = JSON.parse((e && e.postData && e.postData.contents) || '{}'); } catch (err) {}
  if (String(body.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  if (body.action === 'read') return _json({ ok: true, games: _readGames() });
  if (body.action === 'write_links') {
    try { return _json(_writeLinks(body.links, body.notes)); }
    catch (err) { return _json({ error: String(err) }); }
  }
  return _json({ error: 'unknown action' });
}
