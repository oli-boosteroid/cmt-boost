var SECRET = 'CHANGE_ME_to_a_long_random_string';

var WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

function test() {
  var data = _readAll();
  Logger.log('months: ' + data.order.join(', '));
  data.order.forEach(function (m) { Logger.log(m + ': ' + data.months[m].days.length + ' days, members: ' + data.months[m].members.join(', ')); });
}

function _pad(n) { return (n < 10 ? '0' : '') + n; }

function _fmtDate(v) {
  if (v instanceof Date) return _pad(v.getDate()) + '.' + _pad(v.getMonth() + 1) + '.' + v.getFullYear();
  return String(v || '').trim();
}

function _findHeader(values) {
  for (var r = 0; r < Math.min(values.length, 6); r++) {
    for (var c = 0; c < values[r].length; c++) {
      if (String(values[r][c] || '').trim().toLowerCase() === 'date') return { row: r, dateCol: c };
    }
  }
  return null;
}

function _memberInfo(values) {
  var h = _findHeader(values);
  if (!h) return null;
  var headers = values[h.row];
  var names = [], cols = [];
  for (var c = h.dateCol + 1; c < headers.length; c++) {
    var name = String(headers[c] || '').replace(/\s+/g, ' ').trim();
    if (name) { names.push(name); cols.push(c); }
  }
  return { row: h.row, dateCol: h.dateCol, names: names, cols: cols };
}

function _readTab(ws) {
  var values = ws.getDataRange().getValues();
  var info = _memberInfo(values);
  if (!info) return null;
  var wdCol = info.dateCol > 0 ? info.dateCol - 1 : -1;
  var days = [];
  for (var r = info.row + 1; r < values.length; r++) {
    var row = values[r];
    var date = _fmtDate(row[info.dateCol]);
    if (!date) continue;
    var weekday = wdCol >= 0 ? String(row[wdCol] || '').trim() : '';
    var shifts = {};
    for (var i = 0; i < info.names.length; i++) shifts[info.names[i]] = String(row[info.cols[i]] != null ? row[info.cols[i]] : '').trim();
    days.push({ weekday: weekday, date: date, shifts: shifts });
  }
  return { members: info.names, days: days };
}

function _readAll() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var months = {}, order = [];
  ss.getSheets().forEach(function (ws) {
    var parsed = _readTab(ws);
    if (parsed) { months[ws.getName()] = parsed; order.push(ws.getName()); }
  });
  return { months: months, order: order };
}

function _color(value) {
  var v = String(value || '').trim().toUpperCase();
  // VACATION, SICK LEAVE and OFFLINE all use the same "away" colour as VACATION.
  if (v === 'VACATION' || v === 'SICK LEAVE' || v === 'SICK' || v === 'OFFLINE') return '#f4cccc';
  if (!v) return null;
  return '#fff2cc';
}

function _setCell(month, date, memberIndex, memberName, value) {
  var ws = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(month);
  if (!ws) throw new Error('month not found: ' + month);
  var values = ws.getDataRange().getValues();
  var info = _memberInfo(values);
  if (!info) throw new Error('no header in ' + month);
  var want = String(memberName || '').replace(/\s+/g, ' ').trim();
  var idx = parseInt(memberIndex, 10);
  var col = -1;
  if (!isNaN(idx) && idx >= 0 && idx < info.cols.length && (!want || info.names[idx] === want)) {
    col = info.cols[idx];
  } else {
    for (var i = 0; i < info.names.length; i++) if (info.names[i] === want) { col = info.cols[i]; break; }
  }
  if (col < 0) throw new Error('member not found: ' + want);
  var wantDate = String(date).trim();
  for (var r = info.row + 1; r < values.length; r++) {
    if (_fmtDate(values[r][info.dateCol]) === wantDate) {
      var cell = ws.getRange(r + 1, col + 1);
      cell.setValue(value || '');
      cell.setBackground(_color(value) || '#ffffff');
      return true;
    }
  }
  throw new Error('date not found: ' + wantDate);
}

function _createMonth(month, year, members) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var mi = parseInt(month, 10);
  var y = parseInt(year, 10);
  var name = MONTHS[mi - 1] + ' ' + y;
  if (ss.getSheetByName(name)) throw new Error('already exists: ' + name);
  if (!members || !members.length) {
    var all = _readAll();
    if (all.order.length) members = all.months[all.order[all.order.length - 1]].members;
  }
  members = members || [];
  var ws = ss.insertSheet(name);
  var ncol = 2 + members.length;
  ws.getRange(1, 1, 1, ncol).merge();
  ws.getRange(1, 1).setValue(name).setFontWeight('bold').setFontSize(14).setHorizontalAlignment('center').setBackground('#b7b7e8');
  var header = ['Day', 'Date'].concat(members);
  ws.getRange(2, 1, 1, ncol).setValues([header]).setFontWeight('bold').setHorizontalAlignment('center').setBackground('#d9d2e9').setWrap(true);
  var days = new Date(y, mi, 0).getDate();
  var rows = [];
  for (var d = 1; d <= days; d++) {
    var dt = new Date(y, mi - 1, d);
    rows.push([WEEKDAYS[dt.getDay()], _pad(d) + '.' + _pad(mi) + '.' + y].concat(members.map(function () { return ''; })));
  }
  ws.getRange(3, 1, rows.length, ncol).setValues(rows);
  for (var r = 0; r < rows.length; r++) {
    var wd = new Date(y, mi - 1, r + 1).getDay();
    var bg = (wd === 0 || wd === 6) ? '#cfe2f3' : '#fdf6e3';
    ws.getRange(r + 3, 1, 1, 2).setBackground(bg);
  }
  ws.getRange(1, 1, rows.length + 2, ncol).setBorder(true, true, true, true, true, true);
  ws.setColumnWidth(1, 90); ws.setColumnWidth(2, 90);
  return name;
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  if (!e || !e.parameter || String(e.parameter.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  var data = _readAll();
  return _json({ ok: true, months: data.months, order: data.order });
}

function doPost(e) {
  var body = {};
  try { body = JSON.parse((e && e.postData && e.postData.contents) || '{}'); } catch (err) {}
  if (String(body.token || '') !== SECRET) return _json({ error: 'unauthorized' });
  try {
    if (body.action === 'set') { _setCell(body.month, body.date, body.member_index, body.member, body.value || ''); return _json({ ok: true }); }
    if (body.action === 'create_month') { var nm = _createMonth(body.month, body.year, body.members || []); return _json({ ok: true, name: nm }); }
    if (body.action === 'read') { var data = _readAll(); return _json({ ok: true, months: data.months, order: data.order }); }
  } catch (err) { return _json({ error: String(err) }); }
  return _json({ error: 'unknown action' });
}
