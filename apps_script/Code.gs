/**
 * UmamusumeFactorDB 用 Webhook + Google Form トリガ + 検索 Web アプリ
 *
 * デプロイ方法：
 *   1. 対象スプレッドシートを開く
 *   2. 拡張機能 → Apps Script
 *   3. 本ファイル全文を貼り付け（※更新時は上書き）
 *   4. プロジェクト設定 → スクリプトプロパティに以下を登録：
 *        SHARED_SECRET         — factors 書き込み webhook 認証シークレット
 *        CLOUD_RUN_URL         — Cloud Run の /process エンドポイント URL
 *        CLOUD_RUN_SECRET      — Cloud Run 側 SHARED_SECRET（環境変数）
 *        FORM_RESPONSES_TAB    — フォーム応答タブ名（自動検出するので任意）
 *   5. デプロイ → ウェブアプリ再デプロイ → 新バージョン選択
 *   6. トリガ → onFormSubmit を「スプレッドシート起動時 → フォーム送信時」に設定
 */

// =============================================================================
// 1. factors 書き込み Webhook（Python CLI / Cloud Run から呼ばれる）
// =============================================================================

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return _json({ok: false, error: "no POST body"});
    }
    var payload = JSON.parse(e.postData.contents);

    var expected = PropertiesService.getScriptProperties().getProperty("SHARED_SECRET");
    if (!expected) {
      return _json({ok: false, error: "SHARED_SECRET not configured on server"});
    }
    if (payload.secret !== expected) {
      return _json({ok: false, error: "invalid secret"});
    }

    var tabName = payload.tab || "factors_raw";
    var columns = payload.columns;
    var rows = payload.rows;
    if (!rows && payload.row) {
      rows = [payload.row];
    }
    if (!Array.isArray(columns) || !Array.isArray(rows)) {
      return _json({ok: false, error: "columns and rows must be arrays"});
    }
    if (rows.length === 0) {
      return _json({ok: false, error: "rows must not be empty"});
    }
    for (var i = 0; i < rows.length; i++) {
      if (!Array.isArray(rows[i])) {
        return _json({ok: false, error: "each row must be an array"});
      }
      if (rows[i].length !== columns.length) {
        return _json({ok: false, error: "row " + i + " length " + rows[i].length + " != columns length " + columns.length});
      }
    }

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(tabName);
    if (!sheet) {
      sheet = ss.insertSheet(tabName);
      var needed = columns.length;
      var existing = sheet.getMaxColumns();
      if (existing < needed) {
        sheet.insertColumnsAfter(existing, needed - existing);
      }
      sheet.appendRow(columns);
      sheet.setFrozenRows(1);
    } else if (sheet.getLastRow() === 0) {
      var needed2 = columns.length;
      var existing2 = sheet.getMaxColumns();
      if (existing2 < needed2) {
        sheet.insertColumnsAfter(existing2, needed2 - existing2);
      }
      sheet.appendRow(columns);
      sheet.setFrozenRows(1);
    }

    // ── 列名マッピング方式で書き込む ──
    // 旧実装は「A 列から payload 順に setValues」していたため、
    // _ensureFactorNo が factor_no 列を挿入すると全体が 1 列ずれて着地するバグがあった。
    // 修正版は、シートの現在ヘッダーに対して payload の列名でマッピングし、
    // 未知の列は末尾に追加してから書き込む。
    var sheetLastCol = sheet.getLastColumn();
    var sheetHeader = sheet.getRange(1, 1, 1, sheetLastCol).getValues()[0].map(String);
    var sheetColIdx = {};
    for (var si = 0; si < sheetHeader.length; si++) sheetColIdx[sheetHeader[si]] = si;

    for (var ci = 0; ci < columns.length; ci++) {
      if (sheetColIdx[columns[ci]] === undefined) {
        sheetLastCol += 1;
        sheet.getRange(1, sheetLastCol).setValue(columns[ci]);
        sheetColIdx[columns[ci]] = sheetLastCol - 1;
      }
    }

    var startRow = sheet.getLastRow() + 1;
    var mappedRows = [];
    for (var ri = 0; ri < rows.length; ri++) {
      var newRow = new Array(sheetLastCol);
      for (var k = 0; k < sheetLastCol; k++) newRow[k] = "";
      for (var ci2 = 0; ci2 < columns.length; ci2++) {
        newRow[sheetColIdx[columns[ci2]]] = rows[ri][ci2];
      }
      mappedRows.push(newRow);
    }
    sheet.getRange(startRow, 1, mappedRows.length, sheetLastCol).setValues(mappedRows);
    var lastRow = startRow + mappedRows.length - 1;

    return _json({
      ok: true,
      sheet: tabName,
      rows_appended: rows.length,
      last_row_number: lastRow
    });
  } catch (err) {
    return _json({ok: false, error: String(err)});
  }
}

function doGet(e) {
  if (e && e.parameter && e.parameter.ui === "search") {
    // XFrameOptionsMode は指定せずデフォルト（SAMEORIGIN 相当）にして
    // 任意サイトへの iframe 埋め込みを防ぐ（clickjacking 対策）。
    // ※ HtmlService は <meta name="viewport"> を無視するため、
    //    モバイルレイアウトを機能させるには addMetaTag でサーバ側から注入する必要がある。
    return HtmlService.createHtmlOutputFromFile("search")
      .setTitle("UMG因子保管庫")
      .addMetaTag("viewport", "width=device-width, initial-scale=1, viewport-fit=cover");
  }
  return _json({ok: true, message: "UmamusumeFactorDB webhook alive"});
}

// =============================================================================
// 2. 検索 API（search.html から google.script.run 経由で呼ばれる）
// =============================================================================

var SEARCH_TAB_NAME = "factors_normalized";
var SEARCH_MAX_SLOTS = 60;

/**
 * factors_normalized に factor_no 列（A 列想定）を保証し、空の行には
 * submission_id の登場順に通番を付与する。同じ submission_id は同じ番号。
 * 既存の採番はそのまま残し、未採番の submission_id に対して次の連番を振る。
 */
function _ensureFactorNo(sheet) {
  var lastRow = sheet.getLastRow();
  var lastCol = sheet.getLastColumn();
  if (lastRow < 1 || lastCol < 1) return;
  var header = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String);
  var noIdx = header.indexOf("factor_no");
  if (noIdx < 0) {
    // 先頭列に factor_no を挿入
    sheet.insertColumnBefore(1);
    sheet.getRange(1, 1).setValue("factor_no");
    noIdx = 0;
    lastCol = sheet.getLastColumn();
  }
  if (lastRow < 2) return;
  var subIdx = header.indexOf("submission_id");
  if (subIdx < 0) {
    // ヘッダ再取得（挿入があった場合）
    header = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String);
    subIdx = header.indexOf("submission_id");
    if (subIdx < 0) return;
  } else if (noIdx === 0 && header[0] !== "factor_no") {
    // 挿入直後に submission_id の列位置が 1 ずれている
    header = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String);
    subIdx = header.indexOf("submission_id");
  }
  var noCol = noIdx + 1;
  var subCol = subIdx + 1;
  var rng = sheet.getRange(2, 1, lastRow - 1, lastCol);
  var values = rng.getValues();
  // 既存 no を収集して最大値を知る
  var sidToNo = {};
  var maxNo = 0;
  for (var r = 0; r < values.length; r++) {
    var n = Number(values[r][noIdx] || 0);
    var sid = String(values[r][subIdx] || "");
    if (n > 0 && sid) {
      sidToNo[sid] = n;
      if (n > maxNo) maxNo = n;
    }
  }
  // 未採番の submission_id に登場順で連番を振る
  var dirty = false;
  var noColValues = sheet.getRange(2, noCol, lastRow - 1, 1).getValues();
  for (var r = 0; r < values.length; r++) {
    var sid = String(values[r][subIdx] || "");
    if (!sid) continue;
    if (!sidToNo[sid]) {
      maxNo += 1;
      sidToNo[sid] = maxNo;
    }
    if (Number(noColValues[r][0] || 0) !== sidToNo[sid]) {
      noColValues[r][0] = sidToNo[sid];
      dirty = true;
    }
  }
  if (dirty) {
    sheet.getRange(2, noCol, lastRow - 1, 1).setValues(noColValues);
  }
}

/**
 * 検索 UI 用のプルダウン選択肢を factors_normalized シートから抽出。
 * 戻り値: { ok, characters, submitters, green_names, white_names }
 */
function getFilterOptions() {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(SEARCH_TAB_NAME);
    if (!sheet) return {ok: true, characters: [], submitters: [], green_names: [], white_names: []};
    _ensureFactorNo(sheet);
    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow < 2) return {ok: true, characters: [], submitters: [], green_names: [], white_names: []};
    var values = sheet.getRange(1, 1, lastRow, lastCol).getValues();
    var header = values[0].map(String);
    var colIdx = {};
    for (var i = 0; i < header.length; i++) colIdx[header[i]] = i;

    var charSet = {}, subSet = {}, greenSet = {}, whiteSet = {};
    for (var r = 1; r < values.length; r++) {
      var row = values[r];
      var role = String(row[colIdx["role"]] || "");
      // 親ウマ娘プルダウンは main の character のみ採用
      if (role === "main") {
        var ch = String(row[colIdx["character"]] || "").trim();
        if (ch) charSet[ch] = 1;
      }
      var sid = String(row[colIdx["submitter_id"]] || "").trim();
      if (sid) subSet[sid] = 1;
      var gn = String(row[colIdx["green_name"]] || "").trim();
      if (gn) greenSet[gn] = 1;
      for (var s = 1; s <= SEARCH_MAX_SLOTS; s++) {
        var key = "factor_" + (s < 10 ? "0" + s : s) + "_name";
        var idx = colIdx[key];
        if (idx === undefined) break;
        var wn = String(row[idx] || "").trim();
        if (wn) whiteSet[wn] = 1;
      }
    }
    function sortedKeys(obj) {
      return Object.keys(obj).sort(function(a, b) { return a.localeCompare(b, "ja"); });
    }
    // 目的・用途は Form 応答タブから収集（factors_normalized には無い列のため）
    var purposeSet = {};
    var purposeMap = _buildSubmissionPurposeMap();
    for (var psid in purposeMap) purposeSet[purposeMap[psid]] = 1;
    return {
      ok: true,
      characters: sortedKeys(charSet),
      submitters: sortedKeys(subSet),
      green_names: sortedKeys(greenSet),
      white_names: sortedKeys(whiteSet),
      purposes: sortedKeys(purposeSet)
    };
  } catch (err) {
    return {ok: false, error: String(err)};
  }
}

/**
 * Form 応答タブを取得（自動検出）。
 */
function _getFormResponsesSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var tabName = PropertiesService.getScriptProperties().getProperty("FORM_RESPONSES_TAB") || "";
  var sheet = tabName ? ss.getSheetByName(tabName) : null;
  if (!sheet) {
    var sheets = ss.getSheets();
    for (var i = 0; i < sheets.length; i++) {
      var n = sheets[i].getName();
      if (n === SEARCH_TAB_NAME || n === "factors_raw") continue;
      if (/回答|フォーム|form/i.test(n)) { sheet = sheets[i]; break; }
    }
  }
  return sheet;
}

/**
 * Form 応答タブから submission_id ごとに指定列を引くマップを作る。
 * headerRe: 列ヘッダーに対するマッチ用正規表現
 */
function _buildSubmissionMap(headerRe) {
  try {
    var sheet = _getFormResponsesSheet();
    if (!sheet) return {};
    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow < 2) return {};
    var values = sheet.getRange(1, 1, lastRow, lastCol).getValues();
    var header = values[0].map(String);
    var subIdx = -1, tgtIdx = -1;
    for (var j = 0; j < header.length; j++) {
      if (header[j] === "submission_id") subIdx = j;
      if (tgtIdx < 0 && headerRe.test(header[j])) tgtIdx = j;
    }
    if (subIdx < 0 || tgtIdx < 0) return {};
    var map = {};
    for (var r = 1; r < values.length; r++) {
      var sid = String(values[r][subIdx] || "");
      if (!sid) continue;
      var v = String(values[r][tgtIdx] || "").trim();
      if (v) map[sid] = v;
    }
    return map;
  } catch (err) {
    return {};
  }
}

function _buildSubmissionImageMap() {
  var map = _buildSubmissionMap(/画像|image|ファイル|スクリーンショット/i);
  // 画像は URL カンマ区切り対応（先頭のみ）
  var out = {};
  for (var k in map) out[k] = String(map[k]).split(",")[0].trim();
  return out;
}

function _buildSubmissionPurposeMap() {
  return _buildSubmissionMap(/目的|用途|purpose/i);
}

function _buildSubmissionTrainerIdMap() {
  return _buildSubmissionMap(/トレーナーID|trainer/i);
}

/**
 * 検索クエリを受けて submission_id 単位で集約した結果を返す。
 *
 * filters: {
 *   character: string (完全一致, 空=すべて),
 *   submitter_id: string (完全一致),
 *   purpose: string (完全一致),
 *   blue: [ {type, min_star, scope} ],   // 各条件に scope: "self" | "parents" | "all"
 *   red:  [ {type, min_star, scope} ],
 *   green: [ {name, scope} ],            // 完全一致
 *   white: [ {name, min_star, scope} ],  // 完全一致 + 最低星
 *   limit: number
 * }
 */
function searchFactors(filters) {
  try {
    filters = filters || {};
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(SEARCH_TAB_NAME);
    if (!sheet) return {ok: false, error: "sheet '" + SEARCH_TAB_NAME + "' not found"};
    _ensureFactorNo(sheet);
    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow < 2) return {ok: true, total: 0, submissions: []};

    var values = sheet.getRange(1, 1, lastRow, lastCol).getValues();
    var header = values[0].map(String);
    var colIdx = {};
    for (var i = 0; i < header.length; i++) colIdx[header[i]] = i;

    function buildUmaRow(row) {
      var whites = [];
      for (var s = 1; s <= SEARCH_MAX_SLOTS; s++) {
        var keyN = "factor_" + (s < 10 ? "0" + s : s) + "_name";
        var keyS = "factor_" + (s < 10 ? "0" + s : s) + "_star";
        if (colIdx[keyN] === undefined) break;
        var nm = String(row[colIdx[keyN]] || "").trim();
        if (!nm) continue;
        whites.push({name: nm, star: Number(row[colIdx[keyS]] || 0)});
      }
      return {
        character: String(row[colIdx["character"]] || ""),
        blue_type: String(row[colIdx["blue_type"]] || ""),
        blue_star: Number(row[colIdx["blue_star"]] || 0),
        red_type: String(row[colIdx["red_type"]] || ""),
        red_star: Number(row[colIdx["red_star"]] || 0),
        green_name: String(row[colIdx["green_name"]] || ""),
        green_star: Number(row[colIdx["green_star"]] || 0),
        whites: whites
      };
    }

    var subsMap = {};
    var subsOrder = [];
    for (var r = 1; r < values.length; r++) {
      var row = values[r];
      var sid = String(row[colIdx["submission_id"]] || "");
      if (!sid) continue;
      if (!subsMap[sid]) {
        subsMap[sid] = {
          submission_id: sid,
          factor_no: Number(row[colIdx["factor_no"]] || 0),
          submitted_at: String(row[colIdx["submitted_at"]] || ""),
          submitter_id: String(row[colIdx["submitter_id"]] || ""),
          image_filename: String(row[colIdx["image_filename"]] || ""),
          main: null, parent1: null, parent2: null
        };
        subsOrder.push(sid);
      }
      var role = String(row[colIdx["role"]] || "");
      if (role === "main" || role === "parent1" || role === "parent2") {
        subsMap[sid][role] = buildUmaRow(row);
      }
    }

    var charExact = String(filters.character || "").trim();
    var submitterExact = String(filters.submitter_id || "").trim();
    var purposeExact = String(filters.purpose || "").trim();
    var blue = Array.isArray(filters.blue) ? filters.blue : [];
    var red = Array.isArray(filters.red) ? filters.red : [];
    var green = Array.isArray(filters.green) ? filters.green : [];
    var white = Array.isArray(filters.white) ? filters.white : [];
    var limit = Number(filters.limit || 200);
    // アーカイブ（30 日超）を含めるか
    var includeArchive = Boolean(filters.include_archive);
    var cutoffMs = null;
    if (!includeArchive) {
      var _d = new Date();
      _d.setDate(_d.getDate() - 30);
      cutoffMs = _d.getTime();
    }

    // purpose フィルタを使う場合のみマップを事前取得
    var purposeMap = (purposeExact || true) ? _buildSubmissionPurposeMap() : {};

    function targetsFor(sub, scope) {
      if (scope === "self") return [sub.main].filter(Boolean);
      if (scope === "parents") return [sub.parent1, sub.parent2].filter(Boolean);
      return [sub.main, sub.parent1, sub.parent2].filter(Boolean);
    }
    function matchBlue(sub, cond) {
      var umas = targetsFor(sub, cond.scope || "all");
      for (var i = 0; i < umas.length; i++) {
        if (umas[i].blue_type === cond.type && umas[i].blue_star >= (cond.min_star || 0)) return true;
      }
      return false;
    }
    function matchRed(sub, cond) {
      var umas = targetsFor(sub, cond.scope || "all");
      for (var i = 0; i < umas.length; i++) {
        if (umas[i].red_type === cond.type && umas[i].red_star >= (cond.min_star || 0)) return true;
      }
      return false;
    }
    function matchGreen(sub, cond) {
      var v = String(cond.name || "");
      if (!v) return true;
      var umas = targetsFor(sub, cond.scope || "all");
      for (var i = 0; i < umas.length; i++) {
        if (String(umas[i].green_name || "") === v) return true;
      }
      return false;
    }
    function matchWhite(sub, cond) {
      var v = String(cond.name || "");
      var min = Number(cond.min_star || 0);
      if (!v) return true;
      var umas = targetsFor(sub, cond.scope || "all");
      for (var i = 0; i < umas.length; i++) {
        var whs = umas[i].whites || [];
        for (var k = 0; k < whs.length; k++) {
          if (String(whs[k].name) === v && whs[k].star >= min) return true;
        }
      }
      return false;
    }

    var out = [];
    for (var si = 0; si < subsOrder.length; si++) {
      var sub = subsMap[subsOrder[si]];

      // アーカイブフィルタ：OFF のときは 30 日以内のみ
      if (cutoffMs !== null) {
        var tsRaw = String(sub.submitted_at || "");
        if (!tsRaw) continue;  // 日付不明は古扱い
        // "yyyy-MM-dd HH:mm:ss" 形式は Date.parse が通りにくいので ISO 化を試みる
        var tsMs = Date.parse(tsRaw);
        if (isNaN(tsMs)) tsMs = Date.parse(tsRaw.replace(" ", "T"));
        if (isNaN(tsMs) || tsMs < cutoffMs) continue;
      }

      // 親ウマ娘名は main.character の完全一致
      if (charExact) {
        if (!sub.main || String(sub.main.character || "") !== charExact) continue;
      }
      if (submitterExact && String(sub.submitter_id || "") !== submitterExact) continue;
      if (purposeExact) {
        if (String(purposeMap[sub.submission_id] || "") !== purposeExact) continue;
      }

      var ok = true;
      for (var bi = 0; bi < blue.length && ok; bi++) if (!matchBlue(sub, blue[bi])) ok = false;
      for (var ri = 0; ri < red.length && ok; ri++) if (!matchRed(sub, red[ri])) ok = false;
      for (var gi = 0; gi < green.length && ok; gi++) if (!matchGreen(sub, green[gi])) ok = false;
      for (var wi = 0; wi < white.length && ok; wi++) if (!matchWhite(sub, white[wi])) ok = false;
      if (!ok) continue;

      // 結果に purpose を付与
      sub.purpose = purposeMap[sub.submission_id] || "";
      out.push(sub);
      if (out.length >= limit) break;
    }

    out.sort(function(a, b) {
      return String(b.submitted_at).localeCompare(String(a.submitted_at));
    });

    var imgMap = _buildSubmissionImageMap();
    var trainerIdMap = _buildSubmissionTrainerIdMap();
    for (var oi = 0; oi < out.length; oi++) {
      out[oi].image_url = imgMap[out[oi].submission_id] || "";
      out[oi].trainer_id = trainerIdMap[out[oi].submission_id] || "";
    }

    return {ok: true, total: out.length, submissions: out};
  } catch (err) {
    return {ok: false, error: String(err)};
  }
}

// =============================================================================
// バグ報告（search.html のモーダルから google.script.run 経由で呼ばれる）
// =============================================================================

var BUG_TAB_NAME = "bug_reports";
// 自動修正ワーカで使用する列：
//   factor_no        → 該当 submission の識別キー
//   target_role      → main / parent1 / parent2
//   wrong_field      → factors_normalized の列名
//   wrong_value      → 現在の値（整合性チェック用）
//   correct_value    → 修正後の値
//   status           → pending / applied / rejected / invalid（運用用）
//   applied_at       → 自動修正ワーカが書き込む
//   reviewer_note    → 運営側メモ
var BUG_COLUMNS = [
  "reported_at", "factor_no", "target_role", "wrong_field",
  "wrong_value", "correct_value",
  "status", "applied_at", "reviewer_note"
];

/**
 * バグ報告をスプレの bug_reports タブに追記する。
 * params: {factor_no, target_role, wrong_field, wrong_value, correct_value}
 */
function reportBug(params) {
  try {
    params = params || {};
    var factorNo = String(params.factor_no || "").trim();
    var wrong = String(params.wrong_value || "").trim();
    var correct = String(params.correct_value || "").trim();
    var wrongField = String(params.wrong_field || "").trim();
    var role = String(params.target_role || "").trim();
    if (!factorNo) {
      return {ok: false, error: "因子No は必須です"};
    }
    if (!/^\d+$/.test(factorNo)) {
      return {ok: false, error: "因子No は数字で入力してください"};
    }
    if (!wrongField && !wrong && !correct) {
      return {ok: false, error: "修正内容（項目・現在の値・正しい値）のいずれかは必要です"};
    }

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(BUG_TAB_NAME);
    if (!sheet) {
      sheet = ss.insertSheet(BUG_TAB_NAME);
      var existing = sheet.getMaxColumns();
      if (existing < BUG_COLUMNS.length) {
        sheet.insertColumnsAfter(existing, BUG_COLUMNS.length - existing);
      }
      sheet.appendRow(BUG_COLUMNS);
      sheet.setFrozenRows(1);
    }
    var now = Utilities.formatDate(new Date(), "Asia/Tokyo", "yyyy-MM-dd HH:mm:ss");
    sheet.appendRow([
      now,
      Number(factorNo),
      role,
      wrongField,
      wrong,
      correct,
      "pending",
      "",
      ""
    ]);
    return {ok: true};
  } catch (err) {
    return {ok: false, error: String(err)};
  }
}

// =============================================================================
// バグ報告の自動反映ワーカ
// =============================================================================
// 時限トリガ（setupBugReportTrigger）で定期実行するか、スプレッドシートの
// メニュー「UMG因子DB → 🛠 バグ報告を適用」から手動実行する。
//
// 処理内容：
//   - bug_reports の status="pending" を対象
//   - factor_no + target_role で factors_normalized の該当行を特定
//   - wrong_value が現在値と一致する場合のみ correct_value で上書き
//   - 星フィールドは数値化
//   - white_name は wrong_value に一致する factor_NN_name スロットを検索して置換
//   - 自動化できないケース（white_star / other / 曖昧）は needs_review
//
// 結果は status / applied_at / reviewer_note に反映される。

function applyBugReports(options) {
  options = options || {};
  var dryRun = Boolean(options.dry_run);
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var bugSheet = ss.getSheetByName(BUG_TAB_NAME);
    var factSheet = ss.getSheetByName(SEARCH_TAB_NAME);
    if (!bugSheet) return {ok: true, processed: 0, message: "bug_reports シートがまだ無いのでスキップ"};
    if (!factSheet) return {ok: false, error: "factors_normalized シートが見つかりません"};

    _ensureFactorNo(factSheet);

    var bugLast = bugSheet.getLastRow();
    var bugCols = bugSheet.getLastColumn();
    if (bugLast < 2) return {ok: true, processed: 0, applied: 0, invalid: 0, needs_review: 0};
    var bugValues = bugSheet.getRange(1, 1, bugLast, bugCols).getValues();
    var bugHeader = bugValues[0].map(String);
    var bIdx = {};
    for (var i = 0; i < bugHeader.length; i++) bIdx[bugHeader[i]] = i;

    // 必要な bug_reports 列が揃っているか確認（旧スキーマ対策）
    var need = ["factor_no", "target_role", "wrong_field", "wrong_value",
                "correct_value", "status", "applied_at", "reviewer_note"];
    for (var n = 0; n < need.length; n++) {
      if (bIdx[need[n]] === undefined) {
        return {ok: false, error: "bug_reports に列 '" + need[n] + "' が無い。新規報告を 1 件送ると再生成されます"};
      }
    }

    var factLast = factSheet.getLastRow();
    var factCols = factSheet.getLastColumn();
    var factValues = factSheet.getRange(1, 1, factLast, factCols).getValues();
    var factHeader = factValues[0].map(String);
    var fIdx = {};
    for (var i = 0; i < factHeader.length; i++) fIdx[factHeader[i]] = i;

    var AUTO_TEXT = {"character": true, "blue_type": true, "red_type": true, "green_name": true};
    var AUTO_NUM = {"blue_star": true, "red_star": true, "green_star": true};

    var results = {processed: 0, applied: 0, invalid: 0, needs_review: 0, skipped: 0};
    var factUpdates = [];  // {row, col, value}
    var bugUpdates = [];   // {row, status, note}

    for (var r = 1; r < bugValues.length; r++) {
      var br = bugValues[r];
      var status = String(br[bIdx["status"]] || "").trim();
      if (status !== "pending") { results.skipped += 1; continue; }
      results.processed += 1;

      var factorNo = Number(br[bIdx["factor_no"]] || 0);
      var targetRole = String(br[bIdx["target_role"]] || "").trim();
      var field = String(br[bIdx["wrong_field"]] || "").trim();
      var wrong = String(br[bIdx["wrong_value"]] || "").trim();
      var correct = String(br[bIdx["correct_value"]] || "").trim();

      if (!factorNo) {
        bugUpdates.push({row: r + 1, status: "invalid", note: "factor_no が空"});
        results.invalid += 1; continue;
      }
      if (!field) {
        bugUpdates.push({row: r + 1, status: "needs_review", note: "wrong_field が空"});
        results.needs_review += 1; continue;
      }

      // factor_no に一致する行群
      var matched = [];
      for (var fr = 1; fr < factValues.length; fr++) {
        if (Number(factValues[fr][fIdx["factor_no"]] || 0) === factorNo) matched.push(fr);
      }
      if (matched.length === 0) {
        bugUpdates.push({row: r + 1, status: "invalid", note: "該当 factor_no なし"});
        results.invalid += 1; continue;
      }

      // target_role で絞り込み
      var candidates = matched;
      if (targetRole) {
        candidates = matched.filter(function(fr) {
          return String(factValues[fr][fIdx["role"]] || "") === targetRole;
        });
        if (candidates.length === 0) {
          bugUpdates.push({row: r + 1, status: "invalid", note: "該当 role 行なし: " + targetRole});
          results.invalid += 1; continue;
        }
      }

      // --- テキスト系 ---
      if (AUTO_TEXT[field]) {
        if (fIdx[field] === undefined) {
          bugUpdates.push({row: r + 1, status: "invalid", note: "列 '" + field + "' が factors_normalized に無い"});
          results.invalid += 1; continue;
        }
        if (!targetRole) {
          if (wrong) {
            candidates = candidates.filter(function(fr) { return String(factValues[fr][fIdx[field]] || "") === wrong; });
          }
          if (candidates.length !== 1) {
            bugUpdates.push({row: r + 1, status: "needs_review", note: "target_role 未指定で一意に特定できず（候補 " + candidates.length + " 件）"});
            results.needs_review += 1; continue;
          }
        }
        if (candidates.length !== 1) {
          bugUpdates.push({row: r + 1, status: "needs_review", note: "候補が複数: " + candidates.length});
          results.needs_review += 1; continue;
        }
        var targetRow = candidates[0];
        var currentStr = String(factValues[targetRow][fIdx[field]] || "");
        if (wrong && currentStr !== wrong) {
          bugUpdates.push({row: r + 1, status: "invalid", note: "現在値 '" + currentStr + "' が wrong_value '" + wrong + "' と不一致"});
          results.invalid += 1; continue;
        }
        factUpdates.push({row: targetRow + 1, col: fIdx[field] + 1, value: correct});
        bugUpdates.push({row: r + 1, status: "applied", note: field + ": '" + currentStr + "' → '" + correct + "'"});
        results.applied += 1;
        continue;
      }

      // --- 数値（★）系 ---
      if (AUTO_NUM[field]) {
        if (fIdx[field] === undefined) {
          bugUpdates.push({row: r + 1, status: "invalid", note: "列 '" + field + "' が factors_normalized に無い"});
          results.invalid += 1; continue;
        }
        if (candidates.length !== 1 && !targetRole) {
          bugUpdates.push({row: r + 1, status: "needs_review", note: "target_role 未指定のため特定不能（★系）"});
          results.needs_review += 1; continue;
        }
        if (candidates.length !== 1) {
          bugUpdates.push({row: r + 1, status: "needs_review", note: "候補が複数: " + candidates.length});
          results.needs_review += 1; continue;
        }
        var newNum = Number(correct);
        if (isNaN(newNum)) {
          bugUpdates.push({row: r + 1, status: "invalid", note: "correct_value が数値でない: '" + correct + "'"});
          results.invalid += 1; continue;
        }
        var targetRow2 = candidates[0];
        var currentNum = Number(factValues[targetRow2][fIdx[field]] || 0);
        if (wrong && String(currentNum) !== wrong) {
          bugUpdates.push({row: r + 1, status: "invalid", note: "現在値 ★" + currentNum + " が wrong_value '" + wrong + "' と不一致"});
          results.invalid += 1; continue;
        }
        factUpdates.push({row: targetRow2 + 1, col: fIdx[field] + 1, value: newNum});
        bugUpdates.push({row: r + 1, status: "applied", note: field + ": " + currentNum + " → " + newNum});
        results.applied += 1;
        continue;
      }

      // --- 白因子名（スロット検索） ---
      if (field === "white_name") {
        if (!wrong) {
          bugUpdates.push({row: r + 1, status: "invalid", note: "wrong_value（置換対象のスキル名）が空"});
          results.invalid += 1; continue;
        }
        var hit = null;  // {row, col, slotKey}
        for (var ci = 0; ci < candidates.length; ci++) {
          var fr2 = candidates[ci];
          for (var s = 1; s <= SEARCH_MAX_SLOTS; s++) {
            var key = "factor_" + (s < 10 ? "0" + s : s) + "_name";
            if (fIdx[key] === undefined) break;
            if (String(factValues[fr2][fIdx[key]] || "") === wrong) {
              hit = {row: fr2, col: fIdx[key], slotKey: key};
              break;
            }
          }
          if (hit) break;
        }
        if (!hit) {
          bugUpdates.push({row: r + 1, status: "invalid", note: "該当スキル '" + wrong + "' が factor_no=" + factorNo + " の因子に見つからず"});
          results.invalid += 1; continue;
        }
        factUpdates.push({row: hit.row + 1, col: hit.col + 1, value: correct});
        bugUpdates.push({row: r + 1, status: "applied", note: "white (" + hit.slotKey + "): '" + wrong + "' → '" + correct + "'"});
        results.applied += 1;
        continue;
      }

      // --- 自動適用できない（white_star / other / 不明フィールド） ---
      bugUpdates.push({row: r + 1, status: "needs_review", note: "field='" + field + "' は手動対応が必要"});
      results.needs_review += 1;
    }

    if (!dryRun) {
      factUpdates.forEach(function(u) {
        factSheet.getRange(u.row, u.col).setValue(u.value);
      });
      var nowStr = Utilities.formatDate(new Date(), "Asia/Tokyo", "yyyy-MM-dd HH:mm:ss");
      bugUpdates.forEach(function(u) {
        bugSheet.getRange(u.row, bIdx["status"] + 1).setValue(u.status);
        bugSheet.getRange(u.row, bIdx["applied_at"] + 1).setValue(u.status === "applied" ? nowStr : "");
        bugSheet.getRange(u.row, bIdx["reviewer_note"] + 1).setValue(u.note || "");
      });
    }

    Logger.log("applyBugReports: " + JSON.stringify(results));
    return {ok: true, dry_run: dryRun, processed: results.processed,
            applied: results.applied, invalid: results.invalid,
            needs_review: results.needs_review, skipped: results.skipped};
  } catch (err) {
    Logger.log("applyBugReports error: " + err);
    return {ok: false, error: String(err)};
  }
}

/** 1 時間ごとの自動反映トリガを設置（同名の既存トリガは削除してから作り直す） */
function setupBugReportTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === "applyBugReports") {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }
  ScriptApp.newTrigger("applyBugReports").timeBased().everyHours(1).create();
  SpreadsheetApp.getActive().toast("1 時間ごとの自動反映トリガを設置しました", "UMG因子DB", 5);
}

/** 自動反映トリガを削除 */
function removeBugReportTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  var removed = 0;
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === "applyBugReports") {
      ScriptApp.deleteTrigger(existing[i]);
      removed += 1;
    }
  }
  SpreadsheetApp.getActive().toast("自動反映トリガを " + removed + " 件削除しました", "UMG因子DB", 5);
}

/** スプレッドシート起動時にメニューを追加 */
function onOpen(e) {
  try {
    SpreadsheetApp.getUi()
      .createMenu("UMG因子DB")
      .addItem("🛠 バグ報告を適用（今すぐ）", "_menuApplyBugReports")
      .addItem("🧪 バグ報告を適用（ドライラン）", "_menuApplyBugReportsDry")
      .addSeparator()
      .addItem("⏰ 1 時間ごとの自動反映トリガを設置", "setupBugReportTrigger")
      .addItem("⛔ 自動反映トリガを削除", "removeBugReportTrigger")
      .addSeparator()
      .addItem("🙈 投稿画像ファイル名を一括匿名化", "_menuRenameFormUploads")
      .addItem("🔒 フォーム設定を安全化（投稿者に回答非公開）", "_menuSecureFormSettings")
      .addSeparator()
      .addItem("📣 Discord に再通知（factor_no 指定）", "_menuResendDiscord")
      .addItem("🔧 列ズレ行を修復（factor_no 導入時の移行用）", "_menuRepairShiftedRows")
      .addToUi();
  } catch (err) {
    Logger.log("onOpen menu failed: " + err);
  }
}

function _menuApplyBugReports() {
  var res = applyBugReports({dry_run: false});
  var msg = res.ok
    ? "対象 " + res.processed + " 件 / 適用 " + res.applied + " / 要確認 " + res.needs_review + " / 不整合 " + res.invalid
    : "エラー: " + res.error;
  SpreadsheetApp.getUi().alert("バグ報告を適用", msg, SpreadsheetApp.getUi().ButtonSet.OK);
}

function _menuApplyBugReportsDry() {
  var res = applyBugReports({dry_run: true});
  var msg = res.ok
    ? "【ドライラン・実書込みなし】\n対象 " + res.processed + " 件 / 適用可 " + res.applied + " / 要確認 " + res.needs_review + " / 不整合 " + res.invalid
    : "エラー: " + res.error;
  SpreadsheetApp.getUi().alert("バグ報告 ドライラン", msg, SpreadsheetApp.getUi().ButtonSet.OK);
}

// =============================================================================
// 列ズレ行の修復
// =============================================================================
// _ensureFactorNo で factor_no 列を A 列に追加したのに対し、旧 doPost は
// payload を常に A 列から書き始めていたため、Cloud Run 経由で追記された行は
// 全体が 1 列左にズレ、さらに _ensureFactorNo が A 列を通番で上書きしたことで
// 元の submission_id (UUID) が失われている。
// この関数は B 列の値が UUID に見えない行を「ズレている」と判定し、
// B 列以降を 1 列右にシフトしたうえで、失われた submission_id を
// `recovered-<factor_no>-<short>` という識別子で埋め直す。
// （新 doPost はこの事象自体を起こさなくなる修正を同時に入れる）

function repairShiftedRows() {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(SEARCH_TAB_NAME);
    if (!sheet) return {ok: false, error: "factors_normalized シートが見つかりません"};
    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow < 2) return {ok: true, repaired: 0, skipped: 0};
    var values = sheet.getRange(1, 1, lastRow, lastCol).getValues();
    var header = values[0].map(String);
    var noIdx = header.indexOf("factor_no");
    var sidIdx = header.indexOf("submission_id");
    if (noIdx < 0 || sidIdx < 0) {
      return {ok: false, error: "factor_no / submission_id 列が見つかりません"};
    }

    var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    var RECOVERED_RE = /^recovered-\d+-/;

    var noToRecoveredSid = {};
    var repaired = 0, skipped = 0;

    for (var r = 1; r < values.length; r++) {
      var sidVal = String(values[r][sidIdx] || "").trim();
      // 既に UUID または復元済みなら問題なし
      if (UUID_RE.test(sidVal) || RECOVERED_RE.test(sidVal)) { skipped += 1; continue; }
      // 空の行はスキップ（シート末尾の余白）
      if (!sidVal && !String(values[r][noIdx] || "").trim()) { continue; }

      // ズレている。factor_no を保ちつつ B 列以降を右シフトする
      var factorNo = Number(values[r][noIdx] || 0);
      if (!factorNo) { skipped += 1; continue; }
      if (!noToRecoveredSid[factorNo]) {
        noToRecoveredSid[factorNo] = "recovered-" + factorNo + "-" + Utilities.getUuid().substring(0, 8);
      }
      var recoveredSid = noToRecoveredSid[factorNo];

      // 新しい値列を構築：
      //   index 0     = factor_no （変更なし）
      //   index 1     = 復元した submission_id
      //   index 2..N  = 旧値のそれぞれ 1 つ前 （B→C、C→D、...）
      var newRow = new Array(lastCol);
      for (var k = 0; k < lastCol; k++) newRow[k] = "";
      newRow[noIdx] = factorNo;
      newRow[sidIdx] = recoveredSid;
      for (var c = sidIdx + 1; c < lastCol; c++) {
        // 現在 c 列にある値は、本来 (c-1) 列にあるべき値（シフト前の位置）
        // 右シフト後は c 列に入っていた値を (c+1) 列へ移す
        if (c + 0 < lastCol) {
          // 元の (c-1) 列の値が目的の c 列へ
          newRow[c] = values[r][c - 1];
        }
      }
      // 最後の 1 列分（lastCol-1 の元値）は失われる（= 元 1 列シフト分の末尾）
      sheet.getRange(r + 1, 1, 1, lastCol).setValues([newRow]);
      repaired += 1;
    }
    Logger.log("repairShiftedRows: repaired=" + repaired + " skipped=" + skipped);
    return {ok: true, repaired: repaired, skipped: skipped};
  } catch (err) {
    Logger.log("repairShiftedRows error: " + err);
    return {ok: false, error: String(err)};
  }
}

function _menuRepairShiftedRows() {
  var ui = SpreadsheetApp.getUi();
  var confirm = ui.alert(
    "列ズレ行を修復",
    "submission_id 列が UUID でない行を検出して、B 列以降を 1 列右にシフト修復します。\n"
    + "元の submission_id は失われているため、『recovered-<factor_no>-<短縮 id>』で埋めます。\n\n"
    + "実行してよろしいですか？",
    ui.ButtonSet.OK_CANCEL
  );
  if (confirm !== ui.Button.OK) return;
  var res = repairShiftedRows();
  var msg = res.ok
    ? "修復: " + res.repaired + " 件 / 問題なし: " + res.skipped + " 件"
    : "エラー: " + res.error;
  ui.alert("列ズレ行を修復", msg, ui.ButtonSet.OK);
}

// =============================================================================
// 投稿画像ファイル名の一括匿名化
// =============================================================================
// Google Form は画像アップロード時に「<アップロード者の元ファイル名> - <投稿者アカウント名>.ext」
// という命名で Drive に保存する。onFormSubmit 側でリネームしているが、過去投稿や
// リネーム失敗のケースを救済するために、応答シートから辿って一括でリネームできるようにする。

function renameAllFormUploads() {
  try {
    var sheet = _getFormResponsesSheet();
    if (!sheet) return {ok: false, error: "Form 応答シートが見つかりません"};
    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow < 2) return {ok: true, renamed: 0, skipped: 0, errors: 0};
    var values = sheet.getRange(1, 1, lastRow, lastCol).getValues();
    var header = values[0].map(String);
    var imgIdx = -1;
    var tsIdx = 0;  // タイムスタンプは通常 1 列目
    for (var j = 0; j < header.length; j++) {
      if (imgIdx < 0 && /画像|image|ファイル|スクリーンショット/i.test(header[j])) imgIdx = j;
      if (/タイムスタンプ|timestamp/i.test(header[j])) tsIdx = j;
    }
    if (imgIdx < 0) return {ok: false, error: "画像列が見つかりません"};

    var renamed = 0, skipped = 0, errors = 0;
    var errorSamples = [];
    var ANON_RE = /^factor_\d{8}_\d{6}(?:_\d+)?\.[a-z]+$/i;
    var EXT_RE = /\.(png|jpe?g|gif|webp|heic|bmp)$/i;

    for (var r = 1; r < values.length; r++) {
      var cell = String(values[r][imgIdx] || "").trim();
      if (!cell) continue;
      var urls = cell.split(",");
      var tsVal = values[r][tsIdx];
      var d;
      if (tsVal instanceof Date) d = tsVal;
      else if (tsVal) d = new Date(tsVal);
      else d = new Date();
      var stamp = Utilities.formatDate(d, "Asia/Tokyo", "yyyyMMdd_HHmmss");

      for (var u = 0; u < urls.length; u++) {
        var url = urls[u].trim();
        if (!url) continue;
        var fileId = _extractDriveFileId(url);
        if (!fileId) continue;
        try {
          var file = DriveApp.getFileById(fileId);
          var origName = file.getName();
          if (ANON_RE.test(origName)) { skipped += 1; continue; }
          var extMatch = origName.match(EXT_RE);
          var ext = extMatch ? extMatch[0].toLowerCase() : ".png";
          var suffix = (u > 0) ? ("_" + (u + 1)) : "";
          var newName = "factor_" + stamp + suffix + ext;
          file.setName(newName);
          renamed += 1;
        } catch (fileErr) {
          errors += 1;
          if (errorSamples.length < 5) errorSamples.push(String(fileErr));
        }
      }
    }
    Logger.log("renameAllFormUploads: renamed=" + renamed + " skipped=" + skipped + " errors=" + errors);
    return {ok: true, renamed: renamed, skipped: skipped, errors: errors, error_samples: errorSamples};
  } catch (err) {
    Logger.log("renameAllFormUploads error: " + err);
    return {ok: false, error: String(err)};
  }
}

function _menuRenameFormUploads() {
  var ui = SpreadsheetApp.getUi();
  var confirm = ui.alert(
    "投稿画像の一括匿名化",
    "応答シートから辿れる全ての投稿画像の Drive ファイル名を\n『factor_yyyyMMdd_HHmmss.ext』形式に置き換えます。\n\n実行してよろしいですか？",
    ui.ButtonSet.OK_CANCEL
  );
  if (confirm !== ui.Button.OK) return;
  var res = renameAllFormUploads();
  var msg = res.ok
    ? "リネーム: " + res.renamed + " / スキップ: " + res.skipped + " / 失敗: " + res.errors +
      (res.error_samples && res.error_samples.length ? "\n\n失敗例:\n- " + res.error_samples.join("\n- ") : "")
    : "エラー: " + res.error;
  ui.alert("投稿画像の一括匿名化", msg, ui.ButtonSet.OK);
}

// =============================================================================
// フォーム設定の安全化（投稿者に回答状況を非公開）
// =============================================================================
// Google Form の既定では「結果の概要を見る」リンクが投稿後ページに出ることがあり、
// 他の投稿者のアップロード画像名（= 投稿者のアカウント名を含む場合がある）やトレーナーID
// 等が見えてしまう。setPublishingSummary(false) でこの挙動を止める。

function secureFormSettings() {
  try {
    var sheet = _getFormResponsesSheet();
    if (!sheet) return {ok: false, error: "Form 応答シートが見つかりません"};
    var formUrl = sheet.getFormUrl && sheet.getFormUrl();
    if (!formUrl) {
      // getFormUrl は「応答先」に設定されたシートでないと null。
      // SpreadsheetApp 側で取得を試みる。
      formUrl = SpreadsheetApp.getActiveSpreadsheet().getFormUrl && SpreadsheetApp.getActiveSpreadsheet().getFormUrl();
    }
    if (!formUrl) return {ok: false, error: "リンク済みフォームが見つかりません"};
    var form = FormApp.openByUrl(formUrl);

    // 結果の概要を投稿者に非公開（最重要）
    form.setPublishingSummary(false);
    // 投稿後に自分の回答を編集できるリンクも無効化
    form.setAllowResponseEdits(false);
    // 進捗バー表示（UX 向上、任意）
    form.setProgressBar(true);

    return {
      ok: true,
      publishing_summary: form.isPublishingSummary(),
      allow_response_edits: form.canEditResponse(),
      form_title: form.getTitle()
    };
  } catch (err) {
    Logger.log("secureFormSettings error: " + err);
    return {ok: false, error: String(err)};
  }
}

function _menuSecureFormSettings() {
  var res = secureFormSettings();
  var ui = SpreadsheetApp.getUi();
  if (!res.ok) {
    ui.alert("フォーム設定の安全化", "エラー: " + res.error, ui.ButtonSet.OK);
    return;
  }
  ui.alert(
    "フォーム設定の安全化",
    "フォーム『" + res.form_title + "』の設定を更新しました。\n\n"
    + "・結果の概要を投稿者に公開：" + (res.publishing_summary ? "ON" : "OFF") + "\n"
    + "・投稿者による回答編集：" + (res.allow_response_edits ? "許可" : "禁止"),
    ui.ButtonSet.OK
  );
}

// =============================================================================
// 3. Discord Webhook 通知
// =============================================================================

var DISCORD_WEBHOOKS = {
  "対人": "***REDACTED_DISCORD_WEBHOOK***",
  "査定": "***REDACTED_DISCORD_WEBHOOK***",
  "競技場": "***REDACTED_DISCORD_WEBHOOK***"
};

var SEARCH_UI_URL = "https://script.google.com/macros/s/***REDACTED_APPS_SCRIPT_ID***/exec?ui=search";
var FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSeubzORDqJSksgsDV6v-715_Py3_2FoHIuQ6V2CvORVoY22Hg/viewform";

function _pickWebhookForPurpose(purpose) {
  if (!purpose) return null;
  var keys = Object.keys(DISCORD_WEBHOOKS);
  for (var i = 0; i < keys.length; i++) {
    if (purpose.indexOf(keys[i]) >= 0) return {key: keys[i], url: DISCORD_WEBHOOKS[keys[i]]};
  }
  return null;
}

/** キタサンブラック全振り口調のメッセージ。バリエーションからランダム 1 つ。 */
var KITASAN_MESSAGES = {
  "対人": [
    "わっしょーい！皆さ〜ん！新しい**対人因子**が届いたみたいですよッ！これは見逃せませんねッ、さぁ一緒に因子保管庫を覗きに行きましょうッ！勝負の神様もきっと微笑んでくれますよ〜！🐎✨🔥",
    "おっ、皆さん！強そうな**対人因子**が投稿されたみたいですよッ！ふむふむ、こういう出会いこそ勝負の神様のお導きですねッ！わっしょい！一緒に見に行きましょうッ！💪🐎✨",
    "皆さーんッ！**対人因子**の新着情報ですッ！勝負の世界は日進月歩、遅れてはいけませんねッ！わっしょーい！さっそく因子保管庫でチェックしましょうッ！🔥🐎✨",
    "ふむふむ、また強そうな**対人因子**が届きましたねッ！皆さん、こういう時こそ因子保管庫でしっかり研究ですよッ！勝負の神様はいつも準備した者を見ているんですッ、わっしょい！🐎✨💫",
    "わっしょーい！皆さ〜ん！ぴかぴかの**対人因子**が投稿されましたよッ！良い因子は良い勝負を呼ぶんですッ、さっそく見に行きましょうッ！負けませんよぉ〜ッ！💪🔥🐎"
  ],
  "査定": [
    "わっしょーい！皆さ〜ん！新しい**査定因子**が届きましたよッ！育成の参考にぴったりですね〜、ふむふむ、これは楽しみですッ！一緒に見に行きましょうッ！📒✨🐎",
    "ふむ、これは良い**査定因子**の投稿ですよ皆さん！一手間かけた育成には、こういう出会いが大事なんですッ！わっしょい！さぁ因子保管庫を覗いてみましょうッ！💪📒✨",
    "おっと皆さん！**査定因子**の新着ですよッ！目指せランク上位、勝負の神様もきっと応援してくれますッ！わっしょーい、一緒に見に行きましょうねッ！🌟📒🐎",
    "ふむふむ、これは注目の**査定因子**ですよ皆さん！ランクアップのヒントが詰まってますねッ！わっしょい！因子保管庫でじっくり拝見しましょうッ！🌟📒💪",
    "皆さーんッ、わっしょーい！新着の**査定因子**が届きましたよッ！育成は地道な積み重ねですッ、良い因子との出会いは大事な一歩ですよッ！覗きに行きましょうねッ！🐎📒✨"
  ],
  "競技場": [
    "わっしょーい！皆さーんッ！新着の**競技場因子**が届きましたよッ！チームのポイントを1pでも多く積み上げて、上位クラスへ駆け上がりましょうッ！仲間と力を合わせる時ですッ！🏆🐎✨",
    "ふむふむ、**競技場因子**の投稿ですッ！皆さん、チーム戦は一人ひとりの積み重ねが勝負を決めるんですッ！わっしょい、良い因子でチームメイトを底上げしていきましょうねッ！🔥🏆✨",
    "おっ、これは皆さん必見の**競技場因子**ですよッ！仲間と稼いだポイントが順位に直結するんですからねッ、情報収集は怠れませんッ！わっしょーい、さぁ因子保管庫で作戦会議ですッ！🌟🏆🐎",
    "皆さーんッ、わっしょい！チームみんなの力を結集させる時ですッ！新着の**競技場因子**を参考に、ランキング上位を狙っていきましょうねッ！勝負の神様はチームワークにも微笑むんですよッ！💪🏆✨",
    "おっ、**競技場因子**の新着ですッ！皆さん、チーム競技場はワンチームの総力戦ですからねッ！仲間のために1pでも多く、わっしょーい！因子保管庫で研究していきましょうッ！🔥🏆🐎✨"
  ]
};

// AI 誤認識の注意喚起（embed のタイトル直下、因子サマリの前に挿入する短文）。
// OCR 精度が安定したら空文字に差し替えるだけで無効化可能。
var KITASAN_OCR_DISCLAIMER =
  "> ※ 因子情報はAIの自動読み取りなので、たまに誤認識があるんですッ。" +
  "気になる因子は原本スクショもご確認を。誤認識は 🐛 バグ報告で教えてくださいねッ！🐎";

function _kitasanMessage(purposeKey) {
  var arr = KITASAN_MESSAGES[purposeKey];
  if (arr && arr.length) {
    return arr[Math.floor(Math.random() * arr.length)];
  }
  return "皆さ〜ん、新しい因子が届きましたよッ！一緒に因子保管庫を覗きに行きましょうッ！🐎✨";
}

/** factors_normalized の 3 行から Discord 埋め込み用のサマリテキストを組み立てる。 */
function _buildFactorSummaryText(rows, colIdx) {
  var ROLE_LABEL = {main: "【親】", parent1: "【祖1】", parent2: "【祖2】"};
  var ordered = [];
  ["main", "parent1", "parent2"].forEach(function(role) {
    for (var i = 0; i < rows.length; i++) {
      if (String(rows[i][colIdx["role"]] || "") === role) { ordered.push(rows[i]); return; }
    }
  });
  var lines = [];
  ordered.forEach(function(row) {
    var role = String(row[colIdx["role"]] || "");
    var chara = String(row[colIdx["character"]] || "(?)");
    lines.push("**" + ROLE_LABEL[role] + " " + chara + "**");
    var bt = String(row[colIdx["blue_type"]] || "");
    var bs = Number(row[colIdx["blue_star"]] || 0);
    var rt = String(row[colIdx["red_type"]] || "");
    var rs = Number(row[colIdx["red_star"]] || 0);
    var gn = String(row[colIdx["green_name"]] || "");
    var gs = Number(row[colIdx["green_star"]] || 0);
    var chips = [];
    if (bt) chips.push("🔵 " + bt + " ★" + bs);
    if (rt) chips.push("🔴 " + rt + " ★" + rs);
    if (gn) chips.push("🟢 " + gn + " ★" + gs);
    if (chips.length) lines.push(chips.join(" ｜ "));
    var whites = [];
    for (var s = 1; s <= SEARCH_MAX_SLOTS; s++) {
      var keyN = "factor_" + (s < 10 ? "0" + s : s) + "_name";
      var keyS = "factor_" + (s < 10 ? "0" + s : s) + "_star";
      if (colIdx[keyN] === undefined) break;
      var nm = String(row[colIdx[keyN]] || "").trim();
      if (!nm) continue;
      var st = Number(row[colIdx[keyS]] || 0);
      whites.push(nm + "★" + st);
    }
    if (whites.length) {
      var shown = whites.slice(0, 8).join(" / ");
      if (whites.length > 8) shown += " …他" + (whites.length - 8) + "件";
      lines.push("⚪ " + shown);
    }
    lines.push("");
  });
  return lines.join("\n").slice(0, 3800);  // embed.description は 4096 文字まで
}

/** Discord Webhook に multipart で画像を直接添付して POST。 */
function _postDiscordWebhook(webhookUrl, content, imageBlob, summary, searchUrl, trainerId) {
  var fields = [];
  if (trainerId) {
    fields.push({
      name: "🆔 トレーナーID",
      value: "```" + trainerId + "```\n挨拶やフォローにそのままコピーしてお使いくださいねッ！🐎",
      inline: false
    });
  }
  fields.push({
    name: "📢 皆さんも因子を投稿してくださいねッ！",
    value: "あなたの一枚が誰かの勝負を支えるかもしれませんッ！わっしょーい！🐎✨\n▶ [投稿フォームはこちら](" + FORM_URL + ")",
    inline: false
  });
  // タイトル直下に注意書きを挟み、続く因子情報との間に空行を入れて可読性を確保する。
  var description = KITASAN_OCR_DISCLAIMER
    ? (KITASAN_OCR_DISCLAIMER + "\n\u200B\n" + summary)  // U+200B で強制段落分離
    : summary;
  // Discord の description 上限 4096 文字を超えないよう軽くガード。
  if (description.length > 4000) description = description.slice(0, 3997) + "...";

  var embed = {
    title: "🔍 UMG因子保管庫で詳細を見る",
    url: searchUrl,
    description: description,
    color: 0x000000,
    fields: fields,
    footer: {text: "勝負の神様は、準備した者に微笑むッ！🐎✨"}
  };
  var payloadJson = {content: content, embeds: [embed]};
  var options;
  if (imageBlob) {
    // multipart/form-data：payload_json + files[0]
    // embed.image を attachment 参照に差し替える
    // 念のため：投稿者名を含みうる元ファイル名を常に匿名化した名前で上書きする
    //（UrlFetchApp は blob.getName() をそのまま multipart filename として送信するため）
    var safeFilename = "factor_" + Utilities.formatDate(new Date(), "Asia/Tokyo", "yyyyMMdd_HHmmss") + ".png";
    try { imageBlob.setName(safeFilename); } catch (setNameErr) { /* 失敗しても続行 */ }
    var filename = imageBlob.getName() || safeFilename;
    embed.image = {url: "attachment://" + filename};
    options = {
      method: "post",
      payload: {
        "payload_json": JSON.stringify(payloadJson),
        "files[0]": imageBlob
      },
      muteHttpExceptions: true
    };
  } else {
    options = {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payloadJson),
      muteHttpExceptions: true
    };
  }
  var res = UrlFetchApp.fetch(webhookUrl, options);
  Logger.log("discord webhook status: " + res.getResponseCode());
}

// =============================================================================
// Discord 再通知（factor_no 指定）
// =============================================================================
// 列ズレ修復などで submission_id が失われたり、何らかの理由で onFormSubmit 時に
// 通知されなかった因子について、factor_no を指定して Discord へ投稿し直す。
// 対応する Form 応答は submitted_at / Timestamp の近さでマッチさせる（±10 分）。

function resendDiscordByFactorNo(factorNo) {
  try {
    factorNo = Number(factorNo);
    if (!factorNo) return {ok: false, error: "factor_no が不正"};

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var factSheet = ss.getSheetByName(SEARCH_TAB_NAME);
    if (!factSheet) return {ok: false, error: "factors_normalized が見つかりません"};
    _ensureFactorNo(factSheet);
    var lastRow = factSheet.getLastRow();
    var lastCol = factSheet.getLastColumn();
    var values = factSheet.getRange(1, 1, lastRow, lastCol).getValues();
    var header = values[0].map(String);
    var colIdx = {};
    for (var i = 0; i < header.length; i++) colIdx[header[i]] = i;

    var rows = [];
    var submittedAt = "";
    var imageFilename = "";
    var storedSid = "";
    for (var r = 1; r < values.length; r++) {
      if (Number(values[r][colIdx["factor_no"]] || 0) === factorNo) {
        rows.push(values[r]);
        if (!submittedAt) submittedAt = String(values[r][colIdx["submitted_at"]] || "");
        if (!imageFilename && colIdx["image_filename"] !== undefined) imageFilename = String(values[r][colIdx["image_filename"]] || "");
        if (!storedSid && colIdx["submission_id"] !== undefined) storedSid = String(values[r][colIdx["submission_id"]] || "");
      }
    }
    if (rows.length === 0) return {ok: false, error: "factor_no=" + factorNo + " の行が見つかりません"};

    // Form 応答シートから該当レコードを特定する
    var formSheet = _getFormResponsesSheet();
    if (!formSheet) return {ok: false, error: "Form 応答シートが見つかりません"};
    var formLastRow = formSheet.getLastRow();
    var formLastCol = formSheet.getLastColumn();
    var fvals = formSheet.getRange(1, 1, formLastRow, formLastCol).getValues();
    var fheader = fvals[0].map(String);
    var fIdx = {};
    for (var j = 0; j < fheader.length; j++) fIdx[fheader[j]] = j;

    var purposeIdx = -1, imageIdx = -1, trainerIdx = -1, tsIdx = 0, sidFormIdx = -1;
    for (var jj = 0; jj < fheader.length; jj++) {
      if (purposeIdx < 0 && /目的|用途|purpose/i.test(fheader[jj])) purposeIdx = jj;
      if (imageIdx < 0 && /画像|image|ファイル|スクリーンショット/i.test(fheader[jj])) imageIdx = jj;
      if (trainerIdx < 0 && /トレーナーID|trainer/i.test(fheader[jj])) trainerIdx = jj;
      if (/タイムスタンプ|timestamp/i.test(fheader[jj])) tsIdx = jj;
      if (fheader[jj] === "submission_id") sidFormIdx = jj;
    }

    var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

    // ===== 探索戦略 =====
    // 1) factors_normalized 側の submission_id が UUID → Form の同 UUID と完全一致
    // 2) submission_id が失われている（recovered-*）→ image_filename が
    //    `form-<sid 先頭 8 文字>.png` 形式なので、Form の submission_id との前方一致で復元
    // 3) それでもダメなら submitted_at と Form タイムスタンプの近似（±10 分）
    var bestRow = null;
    var matchStrategy = "";

    // Strategy 1: UUID 完全一致
    if (!bestRow && sidFormIdx >= 0 && UUID_RE.test(storedSid)) {
      for (var fr1 = 1; fr1 < fvals.length; fr1++) {
        if (String(fvals[fr1][sidFormIdx] || "").toLowerCase() === storedSid.toLowerCase()) {
          bestRow = fvals[fr1]; matchStrategy = "uuid-exact"; break;
        }
      }
    }

    // Strategy 2: image_filename の prefix で Form 側 submission_id に前方一致
    if (!bestRow && sidFormIdx >= 0) {
      var m = String(imageFilename).match(/form-([0-9a-f]{8})/i);
      if (m) {
        var prefix = m[1].toLowerCase();
        for (var fr2 = 1; fr2 < fvals.length; fr2++) {
          var sidCell = String(fvals[fr2][sidFormIdx] || "").toLowerCase();
          if (sidCell.indexOf(prefix) === 0) {
            bestRow = fvals[fr2]; matchStrategy = "filename-prefix:" + prefix; break;
          }
        }
      }
    }

    // Strategy 3: タイムスタンプ近似（最後の頼み）
    if (!bestRow) {
      var submittedMs = Date.parse(submittedAt);
      if (isNaN(submittedMs)) submittedMs = Date.parse(String(submittedAt).replace(" ", "T"));
      var bestDiff = 10 * 60 * 1000;  // ±10 分
      for (var fr3 = 1; fr3 < fvals.length; fr3++) {
        var ts = fvals[fr3][tsIdx];
        var ms = (ts instanceof Date) ? ts.getTime() : Date.parse(String(ts));
        if (!ms || isNaN(submittedMs)) continue;
        var diff = Math.abs(ms - submittedMs);
        if (diff <= bestDiff) {
          bestDiff = diff;
          bestRow = fvals[fr3];
          matchStrategy = "timestamp-near:" + diff + "ms";
        }
      }
    }

    if (!bestRow) return {ok: false, error: "factor_no=" + factorNo + " の Form 応答がマッチしません（image_filename=" + imageFilename + "）"};
    Logger.log("resendDiscord #" + factorNo + " matched via " + matchStrategy);

    var purpose = purposeIdx >= 0 ? String(bestRow[purposeIdx] || "").trim() : "";
    var imageUrl = imageIdx >= 0 ? String(bestRow[imageIdx] || "").split(",")[0].trim() : "";
    var trainerId = trainerIdx >= 0 ? String(bestRow[trainerIdx] || "").trim() : "";

    var matched = _pickWebhookForPurpose(purpose);
    if (!matched) return {ok: false, error: "purpose='" + purpose + "' に対応する Webhook がありません"};

    var fileId = _extractDriveFileId(imageUrl);
    var imageBlob = null;
    if (fileId) {
      try { imageBlob = DriveApp.getFileById(fileId).getBlob(); } catch (e) { Logger.log("blob fetch failed: " + e); }
    }

    var summary = _buildFactorSummaryText(rows, colIdx);
    var msg = _kitasanMessage(matched.key);
    _postDiscordWebhook(matched.url, msg, imageBlob, summary, SEARCH_UI_URL, trainerId);
    return {ok: true, factor_no: factorNo, purpose: purpose, trainer_id: trainerId, match: matchStrategy};
  } catch (err) {
    Logger.log("resendDiscordByFactorNo error: " + err);
    return {ok: false, error: String(err)};
  }
}

function _menuResendDiscord() {
  var ui = SpreadsheetApp.getUi();
  var input = ui.prompt(
    "Discord に再通知",
    "通知したい factor_no を入力してください（カンマ区切りで複数可。例: 3,4,5）",
    ui.ButtonSet.OK_CANCEL
  );
  if (input.getSelectedButton() !== ui.Button.OK) return;
  var text = String(input.getResponseText() || "").trim();
  if (!text) { ui.alert("入力が空です"); return; }
  var nos = text.split(",").map(function(s) { return Number(s.trim()); }).filter(function(n) { return n > 0; });
  if (nos.length === 0) { ui.alert("有効な factor_no がありません"); return; }
  var lines = [];
  for (var i = 0; i < nos.length; i++) {
    var res = resendDiscordByFactorNo(nos[i]);
    lines.push("#" + nos[i] + ": " + (res.ok ? "送信成功 [" + res.purpose + "] (" + res.match + ")" : "失敗 — " + res.error));
    Utilities.sleep(500);  // Webhook レート制限対策
  }
  ui.alert("Discord 再通知結果", lines.join("\n"), ui.ButtonSet.OK);
}

/** onFormSubmit の末尾から呼ばれる。目的・用途に応じた Webhook に通知。 */
function _notifyDiscordIfNeeded(submissionId, fileId, namedValues) {
  try {
    var purposeRaw = _pickFirst(namedValues, ["目的・用途", "目的", "用途", "purpose"]) || "";
    var matched = _pickWebhookForPurpose(purposeRaw);
    if (!matched) {
      Logger.log("notify skipped: purpose='" + purposeRaw + "'");
      return;
    }

    // 該当 submission_id の 3 行をスプレから取得
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(SEARCH_TAB_NAME);
    if (!sheet) { Logger.log("notify: factors_normalized not found"); return; }
    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow < 2) return;
    var values = sheet.getRange(1, 1, lastRow, lastCol).getValues();
    var header = values[0].map(String);
    var colIdx = {};
    for (var i = 0; i < header.length; i++) colIdx[header[i]] = i;
    var rows = [];
    for (var r = 1; r < values.length; r++) {
      if (String(values[r][colIdx["submission_id"]] || "") === submissionId) {
        rows.push(values[r]);
      }
    }
    if (rows.length === 0) { Logger.log("notify: rows not found for " + submissionId); return; }

    var summary = _buildFactorSummaryText(rows, colIdx);

    // Drive ファイルを Blob で取得し、multipart で Discord に直接添付する
    // （URL 経由埋め込みは Drive の公開範囲・ウイルススキャン等で失敗しやすいため）
    var imageBlob = null;
    if (fileId) {
      try {
        imageBlob = DriveApp.getFileById(fileId).getBlob();
      } catch (blobErr) {
        Logger.log("blob fetch failed: " + blobErr);
      }
    }

    // トレーナーID を Form 応答から拾う（見つからなければ通知側は省略）
    var trainerId = _pickFirst(namedValues, [
      "トレーナーID", "トレーナー ID", "トレーナＩＤ", "trainer id", "trainer"
    ]) || "";

    var msg = _kitasanMessage(matched.key);
    _postDiscordWebhook(matched.url, msg, imageBlob, summary, SEARCH_UI_URL, trainerId);
  } catch (err) {
    Logger.log("_notifyDiscordIfNeeded error: " + err);
  }
}

// =============================================================================
// 4. Google Form 送信時トリガ
// =============================================================================

function onFormSubmit(e) {
  var props = PropertiesService.getScriptProperties();
  var cloudRunUrl = props.getProperty("CLOUD_RUN_URL");
  var cloudRunSecret = props.getProperty("CLOUD_RUN_SECRET");

  if (!cloudRunUrl || !cloudRunSecret) {
    Logger.log("onFormSubmit: CLOUD_RUN_URL / CLOUD_RUN_SECRET not set. 処理をスキップ");
    return;
  }

  try {
    var namedValues = e && e.namedValues ? e.namedValues : {};
    // 連絡先（Discord 名など）が新フィールド。fallback で X ハンドル旧フィールドも拾う。
    var submitterId = _pickFirst(namedValues, [
      "連絡先", "Discord", "ディスコード",
      "投稿者Xハンドル", "X ハンドル", "Xハンドル", "Submitter",
    ]) || "(anonymous)";

    var imageUrlRaw = _pickFirst(namedValues, [
      "画像", "画像アップロード", "スクリーンショット", "Image",
    ]);
    if (!imageUrlRaw) {
      Logger.log("onFormSubmit: 画像フィールドが見つかりませんでした namedValues=" + JSON.stringify(namedValues));
      return;
    }

    var imageUrl = imageUrlRaw.split(",")[0].trim();
    var fileId = _extractDriveFileId(imageUrl);
    if (!fileId) {
      Logger.log("onFormSubmit: Drive file ID を抽出できません url=" + imageUrl);
      return;
    }

    // Form アップロードファイルはデフォルトで「回答者のアカウント名」を含むファイル名になる。
    // 個人情報漏洩を避けるため、投稿日時ベースの匿名名にリネームする。
    var file = DriveApp.getFileById(fileId);
    var origName = "";
    try {
      origName = file.getName();
      var extMatch = origName.match(/\.(png|jpe?g|gif|webp|heic|bmp)$/i);
      var ext = extMatch ? extMatch[0].toLowerCase() : ".png";
      var stamp = Utilities.formatDate(new Date(), "Asia/Tokyo", "yyyyMMdd_HHmmss");
      var safeName = "factor_" + stamp + ext;
      file.setName(safeName);
      // 念のため反映確認
      var afterName = file.getName();
      if (afterName !== safeName) {
        Logger.log("[RENAME] setName 後の名前が期待値と異なる origName='" + origName + "' after='" + afterName + "' want='" + safeName + "'");
      } else {
        Logger.log("[RENAME] OK '" + origName + "' → '" + safeName + "'");
      }
    } catch (renameErr) {
      // 無言で失敗させると投稿者名が Drive / Discord に残ってしまうため、明示的に目立たせる
      Logger.log("[RENAME-ERROR] 失敗: origName='" + origName + "' err=" + renameErr);
    }
    var blob = file.getBlob();
    var b64 = Utilities.base64Encode(blob.getBytes());

    var submissionId = Utilities.getUuid();

    var payload = {
      secret: cloudRunSecret,
      submitter_id: submitterId,
      image_base64: b64,
      submission_id: submissionId,
    };

    var response = UrlFetchApp.fetch(cloudRunUrl, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    });
    var code = response.getResponseCode();
    var body = response.getContentText();
    Logger.log("Cloud Run response: " + code + " " + body.substring(0, 500));

    _updateResponseRow(e, submissionId, code === 200 ? "processed" : "error:" + code);

    // Cloud Run 成功時のみ Discord Webhook 通知
    if (code === 200) {
      _notifyDiscordIfNeeded(submissionId, fileId, namedValues);
    }
  } catch (err) {
    Logger.log("onFormSubmit error: " + err);
  }
}

function _pickFirst(namedValues, keys) {
  for (var i = 0; i < keys.length; i++) {
    var v = namedValues[keys[i]];
    if (v && v.length > 0 && v[0]) return String(v[0]);
  }
  for (var k in namedValues) {
    for (var j = 0; j < keys.length; j++) {
      if (k.indexOf(keys[j]) >= 0 || keys[j].indexOf(k) >= 0) {
        var vals = namedValues[k];
        if (vals && vals.length > 0 && vals[0]) return String(vals[0]);
      }
    }
  }
  return null;
}

function _extractDriveFileId(url) {
  if (!url) return null;
  var m;
  m = url.match(/[?&]id=([a-zA-Z0-9_\-]+)/);
  if (m) return m[1];
  m = url.match(/\/file\/d\/([a-zA-Z0-9_\-]+)/);
  if (m) return m[1];
  return null;
}

function _findOrCreateColumn(sheet, header) {
  var lastCol = sheet.getLastColumn();
  if (lastCol === 0) {
    sheet.getRange(1, 1).setValue(header);
    return 1;
  }
  var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  for (var i = 0; i < headers.length; i++) {
    if (headers[i] === header) return i + 1;
  }
  var newCol = lastCol + 1;
  sheet.getRange(1, newCol).setValue(header);
  return newCol;
}

function _updateResponseRow(e, submissionId, status) {
  try {
    var sheet = e.range.getSheet();
    var row = e.range.getRow();
    var subCol = _findOrCreateColumn(sheet, "submission_id");
    var statusCol = _findOrCreateColumn(sheet, "status");
    sheet.getRange(row, subCol).setValue(submissionId);
    sheet.getRange(row, statusCol).setValue(status);
  } catch (err) {
    Logger.log("_updateResponseRow failed: " + err);
  }
}

// =============================================================================
// 4. 共通ユーティリティ
// =============================================================================

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
