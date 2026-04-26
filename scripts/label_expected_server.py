"""expected_labels.csv を編集するためのローカル Web UI（FastAPI）。

起動:
    .venv/Scripts/python.exe scripts/label_expected_server.py
    → http://127.0.0.1:8766

機能:
  - 全画像の認識結果（tests/fixtures/colored_factors/recognition_results.json）と
    現在の正解ラベル（tests/fixtures/expected_labels.csv）を並べて表示
  - 画像 × role（main/parent1/parent2）ごとに character / blue_type / blue_star /
    red_type / red_star / green_name / green_star を編集可能
  - 「認識結果をコピー」ボタンで現行認識結果を正解ラベルに反映
  - 「この画像を確定」ボタンで source を user に遷移
  - フォーム変更時に自動保存（POST /api/labels）
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response, JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

FIX = Path("tests") / "fixtures"
REC_PATH = FIX / "colored_factors" / "recognition_results.json"
LABELS_PATH = FIX / "expected_labels.csv"
IMAGES_DIR = FIX

ROLES = ("main", "parent1", "parent2")
BLUE_TYPES = ["スピード", "スタミナ", "パワー", "根性", "賢さ"]
RED_TYPES = [
    "芝", "ダート",
    "短距離", "マイル", "中距離", "長距離",
    "逃げ", "先行", "差し", "追込",
]

app = FastAPI()


class LabelRow(BaseModel):
    image_name: str
    role: str
    character: str
    blue_type: str
    blue_star: int
    red_type: str
    red_star: int
    green_name: str
    green_star: int
    source: str


def _load_rows() -> list[dict]:
    rows: list[dict] = []
    with LABELS_PATH.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def _save_rows(rows: list[dict]) -> None:
    fieldnames = [
        "image_name", "role",
        "character",
        "blue_type", "blue_star",
        "red_type", "red_star",
        "green_name", "green_star",
        "source",
    ]
    with LABELS_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


@app.get("/api/labels")
def api_labels() -> JSONResponse:
    rows = _load_rows()
    # 画像単位にグループ化
    groups: dict[str, dict] = {}
    for r in rows:
        img = r["image_name"]
        if img not in groups:
            groups[img] = {"image_name": img, "rows": {}, "source": r["source"]}
        groups[img]["rows"][r["role"]] = {
            "character": r["character"],
            "blue_type": r["blue_type"], "blue_star": int(r["blue_star"]),
            "red_type": r["red_type"], "red_star": int(r["red_star"]),
            "green_name": r["green_name"], "green_star": int(r["green_star"]),
        }
        # 1 画像の source はどれか 1 つでも pending/user なら全体で代表値を入れる
        if r["source"] == "pending":
            groups[img]["source"] = "pending"
        elif r["source"] == "user" and groups[img]["source"] != "pending":
            groups[img]["source"] = "user"
    return JSONResponse({"images": list(groups.values())})


@app.post("/api/labels")
def api_labels_save(rows: list[LabelRow]) -> JSONResponse:
    out = [r.model_dump() for r in rows]
    # int を str に戻す
    for r in out:
        r["blue_star"] = str(r["blue_star"])
        r["red_star"] = str(r["red_star"])
        r["green_star"] = str(r["green_star"])
    _save_rows(out)
    return JSONResponse({"ok": True, "count": len(out)})


@app.get("/image/{filename}")
def get_image(filename: str) -> Response:
    path = IMAGES_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    img = cv2.imdecode(
        np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR,
    )
    if img is None:
        raise HTTPException(status_code=500, detail="imread failed")
    # 横幅 540 に縮小（元画像は 540 基準で正規化されるので）
    h, w = img.shape[:2]
    if w > 540:
        scale = 540 / w
        img = cv2.resize(img, (540, int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise HTTPException(status_code=500, detail="imencode failed")
    return Response(content=buf.tobytes(), media_type="image/png")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(HTML)


HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>expected_labels ラベラー</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: "Hiragino Sans", "Yu Gothic", "Meiryo", sans-serif;
    margin: 0; padding: 0;
    background: #1e1e24; color: #e8e8f0; font-size: 14px;
  }
  header {
    background: #2d2d3a; padding: 12px 20px;
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 2px solid #3e3e4e; position: sticky; top: 0; z-index: 10;
  }
  header h1 { margin: 0; font-size: 18px; color: #fff; }
  .stats { color: #aaa; font-size: 13px; }
  .stats b { color: #fff; }
  main { display: flex; height: calc(100vh - 60px); }
  .sidebar {
    width: 260px; background: #252530; border-right: 1px solid #3e3e4e;
    overflow-y: auto; padding: 8px 0;
  }
  .sidebar .item {
    padding: 6px 12px; cursor: pointer; font-size: 12px;
    border-left: 4px solid transparent;
  }
  .sidebar .item:hover { background: #2d2d3a; }
  .sidebar .item.active { background: #3e3e4e; border-left-color: #5a7fd6; }
  .sidebar .item .fname { color: #e8e8f0; word-break: break-all; }
  .sidebar .item .src { font-size: 10px; color: #888; margin-top: 2px; }
  .sidebar .item .src.pending { color: #f5a623; }
  .sidebar .item .src.user { color: #7ed321; }
  .sidebar .item .src.auto { color: #888; }
  .content {
    flex: 1; overflow-y: auto; padding: 20px;
    display: grid; grid-template-columns: minmax(400px, 1fr) 1fr; gap: 20px;
  }
  .image-pane img {
    max-width: 100%; max-height: calc(100vh - 100px);
    border: 1px solid #3e3e4e;
    display: block; margin: 0 auto;
  }
  .form-pane { display: flex; flex-direction: column; gap: 12px; }
  .role-card {
    background: #2d2d3a; padding: 12px; border-radius: 6px;
    border: 1px solid #3e3e4e;
  }
  .role-card h3 { margin: 0 0 8px; font-size: 14px; color: #5a7fd6; }
  .field { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .field label { min-width: 80px; font-size: 12px; color: #aaa; }
  .field input, .field select {
    flex: 1; background: #1e1e24; color: #e8e8f0;
    border: 1px solid #3e3e4e; padding: 4px 8px; border-radius: 4px;
    font-size: 13px;
  }
  .field input.star { flex: 0 0 60px; }
  .actions { display: flex; gap: 8px; margin-top: 8px; }
  button {
    background: #5a7fd6; color: #fff; border: 0;
    padding: 6px 14px; border-radius: 4px; cursor: pointer; font-size: 12px;
  }
  button:hover { background: #3f63b9; }
  button.ghost { background: #4a4a58; }
  button.success { background: #3c8a3c; }
  .footer-bar {
    position: sticky; bottom: 0;
    background: #2d2d3a; padding: 8px 20px;
    border-top: 1px solid #3e3e4e;
    display: flex; gap: 12px; align-items: center;
  }
  .toast {
    position: fixed; top: 80px; right: 20px;
    background: #3c8a3c; color: #fff; padding: 10px 16px;
    border-radius: 4px; opacity: 0; transition: opacity .3s;
    z-index: 100;
  }
  .toast.show { opacity: 1; }
  .recognized { font-size: 11px; color: #888; margin-left: 8px; }
  .diff { color: #f5a623; }
</style>
</head>
<body>
<header>
  <h1>expected_labels ラベラー</h1>
  <div class="stats" id="stats"></div>
  <button class="ghost" onclick="load()">↻ リスト再読込</button>
</header>
<main>
  <div class="sidebar" id="sidebar"></div>
  <div class="content">
    <div class="image-pane"><img id="preview" src="" alt="preview"></div>
    <div class="form-pane" id="form"></div>
  </div>
</main>
<div class="toast" id="toast">保存しました</div>

<script>
const BLUE_TYPES = ["スピード", "スタミナ", "パワー", "根性", "賢さ"];
const RED_TYPES = [
  "芝", "ダート",
  "短距離", "マイル", "中距離", "長距離",
  "逃げ", "先行", "差し", "追込",
];
const STARS = [0, 1, 2, 3];
const ROLES = ["main", "parent1", "parent2"];

let IMAGES = [];  // list of image groups
let RECOGNIZED = {};  // image -> role -> recognized values（比較用）
let currentIdx = 0;

async function load() {
  // 直前に開いていた画像名を覚え、再読込後も同じ位置に戻す
  const prevName = IMAGES[currentIdx]?.image_name;
  const res = await fetch('/api/labels');
  const data = await res.json();
  IMAGES = data.images;
  const newIdx = prevName ? IMAGES.findIndex(i => i.image_name === prevName) : -1;
  currentIdx = newIdx >= 0 ? newIdx : 0;
  renderSidebar();
  renderForm();
  updateStats();
}

function updateStats() {
  const total = IMAGES.length;
  const pending = IMAGES.filter(i => i.source === "pending").length;
  const user = IMAGES.filter(i => i.source === "user").length;
  const auto = IMAGES.filter(i => i.source === "auto").length;
  document.getElementById('stats').innerHTML =
    `全 <b>${total}</b> 枚 / <span style="color:#f5a623">pending <b>${pending}</b></span> / <span style="color:#7ed321">user <b>${user}</b></span> / <span style="color:#888">auto <b>${auto}</b></span>`;
}

function renderSidebar() {
  const sb = document.getElementById('sidebar');
  sb.innerHTML = '';
  IMAGES.forEach((img, i) => {
    const d = document.createElement('div');
    d.className = 'item' + (i === currentIdx ? ' active' : '');
    d.innerHTML = `
      <div class="fname">${img.image_name}</div>
      <div class="src ${img.source}">${img.source}</div>
    `;
    d.onclick = () => { currentIdx = i; renderSidebar(); renderForm(); };
    sb.appendChild(d);
  });
}

function renderForm() {
  const img = IMAGES[currentIdx];
  document.getElementById('preview').src = `/image/${img.image_name}`;
  const form = document.getElementById('form');
  form.innerHTML = '';

  ROLES.forEach(role => {
    const r = img.rows[role];
    if (!r) return;
    const card = document.createElement('div');
    card.className = 'role-card';
    card.innerHTML = `
      <h3>${role}</h3>
      <div class="field">
        <label>character</label>
        <input data-role="${role}" data-field="character" value="${escapeHtml(r.character)}">
      </div>
      <div class="field">
        <label>blue</label>
        <select data-role="${role}" data-field="blue_type">${makeOptions(BLUE_TYPES, r.blue_type)}</select>
        <input class="star" type="number" min="0" max="3" data-role="${role}" data-field="blue_star" value="${r.blue_star}">
      </div>
      <div class="field">
        <label>red</label>
        <select data-role="${role}" data-field="red_type">${makeOptions(RED_TYPES, r.red_type)}</select>
        <input class="star" type="number" min="0" max="3" data-role="${role}" data-field="red_star" value="${r.red_star}">
      </div>
      <div class="field">
        <label>green name</label>
        <input data-role="${role}" data-field="green_name" value="${escapeHtml(r.green_name)}">
      </div>
      <div class="field">
        <label>green star</label>
        <input class="star" type="number" min="0" max="3" data-role="${role}" data-field="green_star" value="${r.green_star}">
      </div>
    `;
    form.appendChild(card);
  });

  // 操作ボタン
  const actions = document.createElement('div');
  actions.className = 'actions';
  actions.innerHTML = `
    <button class="success" onclick="markUser()">この画像を user で確定</button>
    <button class="ghost" onclick="prev()">← 前へ</button>
    <button class="ghost" onclick="next()">次へ →</button>
  `;
  form.appendChild(actions);

  // イベント登録
  form.querySelectorAll('input, select').forEach(el => {
    el.oninput = onFieldChange;
    el.onchange = onFieldChange;
  });
}

function makeOptions(values, current) {
  return values.map(v => `<option value="${v}"${v===current?' selected':''}>${v}</option>`).join('');
}
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
}

function onFieldChange(e) {
  const el = e.target;
  const role = el.dataset.role;
  const field = el.dataset.field;
  const val = field.endsWith('_star') ? parseInt(el.value) || 0 : el.value;
  IMAGES[currentIdx].rows[role][field] = val;
  save();
}

async function save() {
  // 全 rows を flatten
  const rows = [];
  IMAGES.forEach(img => {
    ROLES.forEach(role => {
      const r = img.rows[role];
      if (!r) return;
      rows.push({
        image_name: img.image_name,
        role,
        character: r.character,
        blue_type: r.blue_type,
        blue_star: r.blue_star,
        red_type: r.red_type,
        red_star: r.red_star,
        green_name: r.green_name,
        green_star: r.green_star,
        source: img.source,
      });
    });
  });
  const res = await fetch('/api/labels', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(rows),
  });
  if (res.ok) showToast('保存しました');
  updateStats();
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1500);
}

function markUser() {
  IMAGES[currentIdx].source = 'user';
  save();
  renderSidebar();
}
function prev() { if (currentIdx > 0) { currentIdx--; renderSidebar(); renderForm(); } }
function next() { if (currentIdx < IMAGES.length - 1) { currentIdx++; renderSidebar(); renderForm(); } }

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.key === 'ArrowLeft') prev();
  if (e.key === 'ArrowRight') next();
  if (e.key === 'u') markUser();
});

load();
</script>
</body>
</html>
"""


def main() -> int:
    import uvicorn
    print("http://127.0.0.1:8766 でラベラー起動")
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
