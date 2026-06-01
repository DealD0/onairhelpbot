/**
 * Google Apps Script — CRUD для фотографий.
 * Листы: "Инвестор", "Трейдер"
 * Каждый лист: ID | URL | Thumbnail | Файл | Дата | Кто загрузил
 *
 * SETUP:
 * 1. Google Sheets → Extensions → Apps Script → вставить этот код
 * 2. Заменить SPREADSHEET_ID
 * 3. Deploy → New deployment → Web app (Execute as: Me, Access: Anyone)
 */

const SPREADSHEET_ID = 'YOUR_SPREADSHEET_ID_HERE';

const SECTION_MAP = {
  'investor': 'Инвестор',
  'trader': 'Трейдер'
};

const HEADERS = ['ID', 'URL', 'Thumbnail', 'Файл', 'Дата', 'Кто загрузил'];

// ─── GET: list photos ─────────────────────────────────────────────────────────
function doGet(e) {
  try {
    const action = (e && e.parameter && e.parameter.action) || 'list';
    const sectionKey = (e && e.parameter && e.parameter.section) || '';
    const sheetName = SECTION_MAP[sectionKey];

    if (!sheetName) {
      return jsonResponse({ error: 'Invalid section' });
    }

    if (!SPREADSHEET_ID || SPREADSHEET_ID.indexOf('YOUR_') === 0) {
      return jsonResponse({ error: 'SPREADSHEET_ID не задан в code.gs — впиши ID своей Google-таблицы' });
    }

    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = getOrCreateSheet(ss, sheetName);
    const data = sheet.getDataRange().getValues();
    const photos = [];

    for (var i = 1; i < data.length; i++) {
      photos.push({
        rowId: i + 1,
        id: data[i][0],
        url: data[i][1],
        thumb: data[i][2],
        filename: data[i][3],
        date: data[i][4],
        uploadedBy: data[i][5]
      });
    }

    return jsonResponse({ status: 'ok', photos: photos });
  } catch (err) {
    return jsonResponse({ error: err.message });
  }
}

// ─── POST: add or delete ─────────────────────────────────────────────────────
function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    var action = data.action || '';
    var sectionKey = data.section || '';
    var sheetName = SECTION_MAP[sectionKey];

    if (!sheetName) {
      return jsonResponse({ error: 'Invalid section' });
    }

    if (!SPREADSHEET_ID || SPREADSHEET_ID.indexOf('YOUR_') === 0) {
      return jsonResponse({ error: 'SPREADSHEET_ID не задан в code.gs — впиши ID своей Google-таблицы' });
    }

    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var sheet = getOrCreateSheet(ss, sheetName);

    if (action === 'add' || action === 'upload') {
      return addPhotos(sheet, data);
    } else if (action === 'delete') {
      return deletePhotos(sheet, data);
    }

    return jsonResponse({ error: 'Unknown action' });
  } catch (err) {
    return jsonResponse({ error: err.message });
  }
}

// ─── Add photos ──────────────────────────────────────────────────────────────
function addPhotos(sheet, data) {
  var photos = data.photos || [];
  var userName = data.userName || data.user || 'unknown';
  var now = new Date();

  for (var i = 0; i < photos.length; i++) {
    var id = Utilities.getUuid();
    sheet.appendRow([
      id,
      photos[i].url || '',
      photos[i].thumb || '',
      photos[i].filename || '',
      now,
      userName
    ]);
  }

  return jsonResponse({ status: 'ok', added: photos.length });
}

// ─── Delete photos by row IDs ────────────────────────────────────────────────
function deletePhotos(sheet, data) {
  var rowIds = data.rowIds || [];
  // Sort descending so deleting doesn't shift subsequent rows
  rowIds.sort(function(a, b) { return b - a; });

  var deleted = 0;
  for (var i = 0; i < rowIds.length; i++) {
    var rowNum = rowIds[i];
    if (rowNum > 1 && rowNum <= sheet.getLastRow()) {
      sheet.deleteRow(rowNum);
      deleted++;
    }
  }

  return jsonResponse({ status: 'ok', deleted: deleted });
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
function getOrCreateSheet(ss, name) {
  var sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.appendRow(HEADERS);
    sheet.getRange(1, 1, 1, HEADERS.length)
      .setFontWeight('bold')
      .setBackground('#4a86e8')
      .setFontColor('#ffffff');
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
