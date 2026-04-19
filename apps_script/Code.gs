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

    var startRow = sheet.getLastRow() + 1;
    sheet.getRange(startRow, 1, rows.length, columns.length).setValues(rows);
    var lastRow = startRow + rows.length - 1;

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
    return HtmlService.createHtmlOutputFromFile("search")
      .setTitle("UMG因子保管庫");
  }
  return _json({ok: true, message: "UmamusumeFactorDB webhook alive"});
}

// =============================================================================
// 2. 検索 API（search.html から google.script.run 経由で呼ばれる）
// =============================================================================

var SEARCH_TAB_NAME = "factors_normalized";
var SEARCH_MAX_SLOTS = 60;

/**
 * 検索 UI 用のプルダウン選択肢を factors_normalized シートから抽出。
 * 戻り値: { ok, characters, submitters, green_names, white_names }
 */
function getFilterOptions() {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(SEARCH_TAB_NAME);
    if (!sheet) return {ok: true, characters: [], submitters: [], green_names: [], white_names: []};
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
    for (var oi = 0; oi < out.length; oi++) {
      out[oi].image_url = imgMap[out[oi].submission_id] || "";
    }

    return {ok: true, total: out.length, submissions: out};
  } catch (err) {
    return {ok: false, error: String(err)};
  }
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
function _postDiscordWebhook(webhookUrl, content, imageBlob, summary, searchUrl) {
  var embed = {
    title: "🔍 UMG因子保管庫で詳細を見る",
    url: searchUrl,
    description: summary,
    color: 0x000000,
    footer: {text: "勝負の神様は、準備した者に微笑むッ！🐎✨"}
  };
  var payloadJson = {content: content, embeds: [embed]};
  var options;
  if (imageBlob) {
    // multipart/form-data：payload_json + files[0]
    // embed.image を attachment 参照に差し替える
    var filename = imageBlob.getName() || "factor.png";
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

    var msg = _kitasanMessage(matched.key);
    _postDiscordWebhook(matched.url, msg, imageBlob, summary, SEARCH_UI_URL);
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
    try {
      var origName = file.getName();
      var extMatch = origName.match(/\.(png|jpe?g|gif|webp|heic|bmp)$/i);
      var ext = extMatch ? extMatch[0].toLowerCase() : ".png";
      var stamp = Utilities.formatDate(new Date(), "Asia/Tokyo", "yyyyMMdd_HHmmss");
      file.setName("factor_" + stamp + ext);
    } catch (renameErr) {
      Logger.log("rename skipped: " + renameErr);
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
