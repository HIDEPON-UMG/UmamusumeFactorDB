"""★ラベル手動修正のローカル Web UI（FastAPI + uvicorn）。

起動:
    python scripts/label_review_server.py
    # → http://127.0.0.1:8765 にアクセス

機能:
  - review.csv の suspect を一覧表示（サムネイル付き）
  - カードクリックで詳細: 元画像の該当箇所（context）と拡大スロットを並べて表示
  - ボタンで「gold に変更」「empty に変更」「削除」「現状維持」を選択
  - 操作時にファイル移動と labels.csv 更新が即時反映される
  - キーボード: g=gold / e=empty / d=delete / k=keep / ← → で前後移動
"""
from __future__ import annotations

import csv
import io
import shutil
import sys
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response, JSONResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET = PROJECT_ROOT / "datasets" / "stars"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
REVIEW_CSV = DATASET / "review" / "review.csv"
LABELS_CSV = DATASET / "labels.csv"

SLOT_SIZE = 28
SLOT_SCALE = 6  # 拡大倍率（28 → 168）
CONTEXT_SCALE = 3  # 元画像コンテキストの拡大倍率


def imread_unicode(path: Path):
    if not path.exists():
        return None
    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def encode_png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("PNG エンコード失敗")
    return buf.tobytes()


# --- データ読み込み -----------------------------------------------------------

def load_labels() -> dict[str, dict]:
    """labels.csv をファイル名キーで辞書化。"""
    if not LABELS_CSV.exists():
        return {}
    result: dict[str, dict] = {}
    with LABELS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            result[row["filename"]] = row
    return result


def write_labels(rows: list[dict]) -> None:
    fields = ["filename", "label", "source_image", "x", "y", "w", "h"]
    with LABELS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def load_review_items() -> list[dict]:
    """review.csv を読んで labels.csv の現状と突き合わせる。

    既に労働後（ラベル変更 or 削除済み）のアイテムは除外する。
    """
    if not REVIEW_CSV.exists():
        return []
    labels = load_labels()
    items: list[dict] = []
    with REVIEW_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            fname = row["filename"]
            lb = labels.get(fname)
            if not lb:
                continue  # 既に削除済み
            current_label = lb["label"]
            # auto_label と現ラベルが違うなら、既に修正済みとして除外
            if current_label != row["auto_label"]:
                continue
            items.append({
                "filename": fname,
                "current_label": current_label,
                "cnn_pred": row["cnn_pred"],
                "confidence": float(row["confidence"]),
                "bin": row["bin"],
                "source_image": lb.get("source_image", ""),
                "x": int(lb["x"]) if lb.get("x") else 0,
                "y": int(lb["y"]) if lb.get("y") else 0,
                "w": int(lb["w"]) if lb.get("w") else 0,
                "h": int(lb["h"]) if lb.get("h") else 0,
            })
    # 優先順に並べ替え（should_be_gold → should_be_empty → uncertain_*）
    bin_priority = {
        "should_be_gold": 0,
        "should_be_empty": 1,
        "uncertain_gold": 2,
        "uncertain_empty": 3,
    }
    items.sort(key=lambda x: (bin_priority.get(x["bin"], 9), x["confidence"]))
    return items


# --- FastAPI -----------------------------------------------------------------

app = FastAPI(title="Star Label Reviewer")


class RelabelRequest(BaseModel):
    filename: str
    action: Literal["keep", "gold", "empty", "delete"]


@app.get("/api/items")
def api_items():
    return load_review_items()


@app.get("/api/summary")
def api_summary():
    items = load_review_items()
    counts: dict[str, int] = {}
    for it in items:
        counts[it["bin"]] = counts.get(it["bin"], 0) + 1
    return {"remaining": len(items), "by_bin": counts}


@app.post("/api/relabel")
def api_relabel(req: RelabelRequest):
    labels = load_labels()
    if req.filename not in labels:
        raise HTTPException(404, "該当ファイルが labels.csv に存在しません")
    current = labels[req.filename]
    current_label = current["label"]
    src = DATASET / current_label / req.filename

    if req.action == "keep":
        return {"ok": True, "note": "現状維持"}

    if req.action == "delete":
        if src.exists():
            src.unlink()
        del labels[req.filename]
        write_labels(list(labels.values()))
        return {"ok": True, "note": "削除"}

    # action in {"gold", "empty"}
    new_label = req.action
    if new_label == current_label:
        return {"ok": True, "note": "同じラベル（無変更）"}
    dst = DATASET / new_label / req.filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.move(str(src), str(dst))
    current["label"] = new_label
    labels[req.filename] = current
    write_labels(list(labels.values()))
    return {"ok": True, "note": f"{current_label} → {new_label}"}


@app.get("/img/slot/{filename}")
def img_slot(filename: str):
    labels = load_labels()
    if filename not in labels:
        raise HTTPException(404)
    label = labels[filename]["label"]
    path = DATASET / label / filename
    img = imread_unicode(path)
    if img is None:
        raise HTTPException(404)
    h, w = img.shape[:2]
    enlarged = cv2.resize(
        img, (w * SLOT_SCALE, h * SLOT_SCALE), interpolation=cv2.INTER_NEAREST
    )
    return Response(content=encode_png(enlarged), media_type="image/png")


@app.get("/img/context/{filename}")
def img_context(filename: str):
    """元画像の該当スロット周辺を切り出して拡大表示する。"""
    labels = load_labels()
    if filename not in labels:
        raise HTTPException(404)
    lb = labels[filename]
    source_image = lb.get("source_image")
    if not source_image:
        raise HTTPException(404, "source_image メタ情報なし")
    # fixtures 配下から .png を探す
    src_path = FIXTURES / f"{source_image}.png"
    if not src_path.exists():
        # サブフォルダも探索
        candidates = list(FIXTURES.rglob(f"{source_image}.png"))
        if not candidates:
            raise HTTPException(404, f"元画像 {source_image}.png 未発見")
        src_path = candidates[0]
    img_orig = imread_unicode(src_path)
    if img_orig is None:
        raise HTTPException(500, "元画像読み込み失敗")

    # labels.csv の x,y,w,h は normalize_width(BASE_WIDTH=540) 後の座標
    # 元画像と同じスケールに戻すため、幅比で換算する
    BASE_WIDTH = 540
    oh, ow = img_orig.shape[:2]
    scale = ow / BASE_WIDTH
    x = int(lb["x"]) if lb.get("x") else 0
    y = int(lb["y"]) if lb.get("y") else 0
    w = int(lb["w"]) if lb.get("w") else 0
    h = int(lb["h"]) if lb.get("h") else 0
    cx = int((x + w / 2) * scale)
    cy = int((y + h / 2) * scale)

    # コンテキスト範囲: 横に広め（同じ行の★3スロットが見えるように ±150px）、縦は ±30px
    CTX_HALF_X = int(150 * scale)
    CTX_HALF_Y = int(30 * scale)
    x0 = max(0, cx - CTX_HALF_X)
    x1 = min(ow, cx + CTX_HALF_X)
    y0 = max(0, cy - CTX_HALF_Y)
    y1 = min(oh, cy + CTX_HALF_Y)
    crop = img_orig[y0:y1, x0:x1].copy()

    # 対象★の位置を赤枠で強調
    sx = int(x * scale) - x0
    sy = int(y * scale) - y0
    sw = int(w * scale)
    sh = int(h * scale)
    cv2.rectangle(crop, (sx, sy), (sx + sw, sy + sh), (0, 0, 255), 2)

    # 拡大表示
    ch, cw = crop.shape[:2]
    enlarged = cv2.resize(
        crop, (cw * CONTEXT_SCALE // 2, ch * CONTEXT_SCALE // 2),
        interpolation=cv2.INTER_LINEAR,
    )
    return Response(content=encode_png(enlarged), media_type="image/png")


HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>★ラベル修正 UI</title>
<style>
  body { font-family: "Yu Gothic UI", sans-serif; margin: 0; padding: 0; background: #1e1e1e; color: #eaeaea; }
  #top { padding: 10px 18px; background: #2a2a2a; border-bottom: 1px solid #444; display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }
  #top h1 { margin: 0; font-size: 18px; }
  #summary { font-size: 14px; color: #bbb; }
  #main { display: flex; gap: 16px; padding: 14px; }
  #list { width: 280px; max-height: calc(100vh - 80px); overflow-y: auto; border: 1px solid #444; background: #252525; }
  #list .row { padding: 6px 10px; border-bottom: 1px solid #333; cursor: pointer; display: flex; gap: 8px; align-items: center; font-size: 12px; }
  #list .row:hover { background: #333; }
  #list .row.active { background: #375080; }
  #list .bin { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: bold; }
  #list .bin.should_be_gold { background: #c27c00; }
  #list .bin.should_be_empty { background: #7a3b3b; }
  #list .bin.uncertain_gold { background: #5a5a2a; }
  #list .bin.uncertain_empty { background: #3a3a5a; }
  #detail { flex: 1; background: #252525; border: 1px solid #444; padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  #detail .meta { font-size: 13px; color: #ddd; line-height: 1.7; }
  #detail .meta strong { color: #ffe07a; }
  #detail img { background: #111; display: block; image-rendering: pixelated; max-width: 100%; }
  #slot-img { border: 1px solid #666; }
  #context-img { border: 1px solid #666; max-height: 380px; object-fit: contain; }
  #actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 6px; }
  #actions button { padding: 10px 20px; font-size: 14px; border: 0; border-radius: 4px; cursor: pointer; color: #fff; }
  .b-gold { background: #d69b00; }
  .b-empty { background: #4a6d8a; }
  .b-keep { background: #5a5a5a; }
  .b-delete { background: #8a2a2a; }
  .b-prev, .b-next { background: #3b5c3b; }
  #toast { position: fixed; bottom: 24px; right: 24px; background: #375080; color: #fff; padding: 10px 20px; border-radius: 4px; display: none; }
</style>
</head>
<body>
<div id="top">
  <h1>★ラベル修正</h1>
  <span id="summary"></span>
  <span style="font-size:12px;color:#999">キー: g=gold, e=empty, d=delete, k=keep, ← →=前後</span>
</div>
<div id="main">
  <div id="list"></div>
  <div id="detail">
    <div id="empty-state" style="color:#aaa">全件処理済みです。お疲れさまでした。</div>
    <div id="detail-content" style="display:none">
      <div class="meta">
        <div><strong>ファイル:</strong> <span id="m-fname"></span></div>
        <div><strong>元画像:</strong> <span id="m-src"></span></div>
        <div><strong>現ラベル:</strong> <span id="m-label"></span> / <strong>CNN予測:</strong> <span id="m-pred"></span> (<span id="m-conf"></span>) / <strong>分類:</strong> <span id="m-bin"></span></div>
      </div>
      <div>
        <div style="margin-bottom:4px;color:#bbb;font-size:12px">元画像の該当スロット（赤枠が対象★）</div>
        <img id="context-img" alt="context">
      </div>
      <div>
        <div style="margin-bottom:4px;color:#bbb;font-size:12px">学習データに入っている 28x28 画像（拡大）</div>
        <img id="slot-img" alt="slot">
      </div>
      <div id="actions">
        <button class="b-gold" onclick="relabel('gold')">★金 (g)</button>
        <button class="b-empty" onclick="relabel('empty')">空 (e)</button>
        <button class="b-keep" onclick="relabel('keep')">現状維持 (k)</button>
        <button class="b-delete" onclick="relabel('delete')">削除 (d)</button>
        <button class="b-prev" onclick="move(-1)">← 前</button>
        <button class="b-next" onclick="move(1)">次 →</button>
      </div>
    </div>
  </div>
</div>
<div id="toast"></div>
<script>
let items = [];
let currentIdx = 0;

async function loadItems() {
  const res = await fetch('/api/items');
  items = await res.json();
  renderList();
  if (items.length === 0) {
    document.getElementById('empty-state').style.display = 'block';
    document.getElementById('detail-content').style.display = 'none';
  } else {
    if (currentIdx >= items.length) currentIdx = items.length - 1;
    renderDetail();
  }
  const s = await (await fetch('/api/summary')).json();
  let text = `残り ${s.remaining} 件`;
  for (const [k, v] of Object.entries(s.by_bin)) text += ` / ${k}: ${v}`;
  document.getElementById('summary').textContent = text;
}

function renderList() {
  const list = document.getElementById('list');
  list.innerHTML = '';
  items.forEach((it, i) => {
    const row = document.createElement('div');
    row.className = 'row' + (i === currentIdx ? ' active' : '');
    row.onclick = () => { currentIdx = i; renderList(); renderDetail(); };
    row.innerHTML = `
      <span class="bin ${it.bin}">${it.bin.replace('uncertain_','?').replace('should_be_','→')}</span>
      <span>${it.filename.slice(0, 30)}...</span>
    `;
    list.appendChild(row);
  });
}

function renderDetail() {
  if (items.length === 0) return;
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('detail-content').style.display = 'flex';
  const it = items[currentIdx];
  document.getElementById('m-fname').textContent = it.filename;
  document.getElementById('m-src').textContent = it.source_image;
  document.getElementById('m-label').textContent = it.current_label;
  document.getElementById('m-pred').textContent = it.cnn_pred;
  document.getElementById('m-conf').textContent = it.confidence.toFixed(3);
  document.getElementById('m-bin').textContent = it.bin;
  // キャッシュ回避のため ts クエリ
  const ts = Date.now();
  document.getElementById('slot-img').src = `/img/slot/${encodeURIComponent(it.filename)}?ts=${ts}`;
  document.getElementById('context-img').src = `/img/context/${encodeURIComponent(it.filename)}?ts=${ts}`;
}

async function relabel(action) {
  if (items.length === 0) return;
  const it = items[currentIdx];
  const res = await fetch('/api/relabel', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({filename: it.filename, action})
  });
  const j = await res.json();
  showToast(`${action}: ${j.note || 'OK'}`);
  if (action !== 'keep') {
    // リストから消えたので再取得
    const prevIdx = currentIdx;
    await loadItems();
    if (items.length > 0) {
      currentIdx = Math.min(prevIdx, items.length - 1);
      renderList();
      renderDetail();
    }
  } else {
    // keep は次へ進む
    move(1);
  }
}

function move(delta) {
  if (items.length === 0) return;
  currentIdx = (currentIdx + delta + items.length) % items.length;
  renderList();
  renderDetail();
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 1500);
}

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  switch (e.key) {
    case 'g': relabel('gold'); break;
    case 'e': relabel('empty'); break;
    case 'd': relabel('delete'); break;
    case 'k': relabel('keep'); break;
    case 'ArrowLeft': move(-1); break;
    case 'ArrowRight': move(1); break;
  }
});

loadItems();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return HTMLResponse(HTML)


def main() -> None:
    import uvicorn

    if not REVIEW_CSV.exists():
        print(
            f"{REVIEW_CSV} が存在しません。先に scripts/review_star_labels.py を実行してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"起動中: http://127.0.0.1:8765")
    print("Ctrl+C で停止")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
