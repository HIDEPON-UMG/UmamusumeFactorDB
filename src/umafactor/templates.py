"""赤/青因子の「参照テンプレート画像」で類似度マッチする簡易分類器。

背景：
  低解像度 crop に対して OCR が 1〜2 文字しか拾えず、ONNX も top-5 圏外に
  なるケースが多い。しかし赤/青因子のカテゴリは 10 種/5 種と少なく、
  既知の正解 crop と「形で」比較すれば高精度に判定できる。

方式：
  datasets/red_blue_templates/{red,blue}/<label>/*.png に保存された
  正解 crop を 128×16 に正規化してテンプレートとして保持。
  クエリ画像も同サイズにリサイズし、各サンプルとのピアソン相関を取り、
  カテゴリごとの max 相関を (label, score) で返す。

テンプレートは scripts/_build_red_blue_templates.py で生成。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_DIR = _PROJECT_ROOT / "datasets" / "red_blue_templates"
_TEMPLATE_SIZE = (128, 16)  # (W, H)

# 各色の★分類用テンプレート（タイル右半分を★領域として抽出したもの）
_STAR_TEMPLATE_ROOT = _PROJECT_ROOT / "datasets" / "star_templates"
_STAR_TEMPLATE_SIZE = (64, 16)  # (W, H)

# 緑因子名テンプレート（タイル左 85%）
_GREEN_NAME_TEMPLATE_DIR = _PROJECT_ROOT / "datasets" / "green_name_templates"
_GREEN_NAME_TEMPLATE_SIZE = (128, 16)


def _imread_ja(path: Path) -> np.ndarray | None:
    """日本語パス対応の cv2.imread。"""
    try:
        return cv2.imdecode(
            np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR,
        )
    except Exception:
        return None


@lru_cache(maxsize=2)
def _load_templates(category: str) -> dict[str, list[np.ndarray]]:
    """category='red' or 'blue'。{label: [template, ...]} を返す。"""
    root = _TEMPLATE_DIR / category
    if not root.exists():
        return {}
    result: dict[str, list[np.ndarray]] = {}
    for label_dir in root.iterdir():
        if not label_dir.is_dir():
            continue
        tmpls: list[np.ndarray] = []
        for png in label_dir.glob("*.png"):
            img = _imread_ja(png)
            if img is None:
                continue
            # ピアソン相関用に float32 に変換しておく
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
            flat = gray.flatten()
            flat = flat - flat.mean()
            norm = float(np.linalg.norm(flat))
            if norm == 0:
                continue
            flat = flat / norm
            tmpls.append(flat)
        if tmpls:
            result[label_dir.name] = tmpls
    return result


def _prepare_query(img_bgr: np.ndarray) -> np.ndarray | None:
    """クエリ画像をテンプレート比較用に正規化。"""
    if img_bgr is None or img_bgr.size == 0:
        return None
    resized = cv2.resize(img_bgr, _TEMPLATE_SIZE, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY).astype(np.float32)
    flat = gray.flatten()
    flat = flat - flat.mean()
    norm = float(np.linalg.norm(flat))
    if norm == 0:
        return None
    return flat / norm


def match_templates(img_bgr: np.ndarray, category: str) -> list[tuple[str, float]]:
    """クエリ画像を category のテンプレ集合と比較し (label, score) の降順リストを返す。

    score は各ラベルのサンプル中の max ピアソン相関（-1..1）を 0..1 に圧縮した値。
    """
    q = _prepare_query(img_bgr)
    if q is None:
        return []
    tmpls = _load_templates(category)
    if not tmpls:
        return []
    scored: list[tuple[str, float]] = []
    for label, samples in tmpls.items():
        max_sim = max(float(q @ s) for s in samples)
        # -1..1 → 0..1 に圧縮
        score = (max_sim + 1.0) / 2.0
        scored.append((label, score))
    scored.sort(key=lambda x: -x[1])
    return scored


@lru_cache(maxsize=3)
def _load_star_templates(color: str) -> dict[int, list[np.ndarray]]:
    """指定色（green/blue/red）の★数テンプレ（1/2/3）を {star: [flat, ...]} で返す。"""
    result: dict[int, list[np.ndarray]] = {}
    base = _STAR_TEMPLATE_ROOT / color
    if not base.exists():
        return result
    for star_dir in base.iterdir():
        if not star_dir.is_dir():
            continue
        try:
            star = int(star_dir.name)
        except ValueError:
            continue
        tmpls: list[np.ndarray] = []
        for png in star_dir.glob("*.png"):
            img = _imread_ja(png)
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
            flat = gray.flatten()
            flat = flat - flat.mean()
            norm = float(np.linalg.norm(flat))
            if norm == 0:
                continue
            tmpls.append(flat / norm)
        if tmpls:
            result[star] = tmpls
    return result


def _prepare_star_query(img_bgr: np.ndarray) -> np.ndarray | None:
    if img_bgr is None or img_bgr.size == 0:
        return None
    resized = cv2.resize(img_bgr, _STAR_TEMPLATE_SIZE, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY).astype(np.float32)
    flat = gray.flatten()
    flat = flat - flat.mean()
    norm = float(np.linalg.norm(flat))
    if norm == 0:
        return None
    return flat / norm


def match_star(img_bgr: np.ndarray, color: str) -> list[tuple[int, float]]:
    """指定色のタイル★領域画像から (star, score) 降順リストを返す。

    color: 'green' / 'blue' / 'red'
    """
    q = _prepare_star_query(img_bgr)
    if q is None:
        return []
    tmpls = _load_star_templates(color)
    if not tmpls:
        return []
    scored: list[tuple[int, float]] = []
    for star, samples in tmpls.items():
        max_sim = max(float(q @ s) for s in samples)
        score = (max_sim + 1.0) / 2.0
        scored.append((star, score))
    scored.sort(key=lambda x: -x[1])
    return scored


# 後方互換: 旧 match_green_star() 呼び出しを match_star(..., "green") に委譲
def match_green_star(img_bgr: np.ndarray) -> list[tuple[int, float]]:
    return match_star(img_bgr, "green")


@lru_cache(maxsize=1)
def _load_green_name_map() -> dict[str, str]:
    """safe_dirname → original_name のマップを読む。"""
    import csv as _csv
    mp: dict[str, str] = {}
    path = _GREEN_NAME_TEMPLATE_DIR / "_label_map.csv"
    if not path.exists():
        return mp
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in _csv.DictReader(f):
            mp[r["safe_name"]] = r["original_name"]
    return mp


@lru_cache(maxsize=1)
def _load_green_name_templates() -> dict[str, list[np.ndarray]]:
    """緑因子名テンプレを {original_name: [flat, ...]} で返す。"""
    result: dict[str, list[np.ndarray]] = {}
    if not _GREEN_NAME_TEMPLATE_DIR.exists():
        return result
    name_map = _load_green_name_map()
    for d in _GREEN_NAME_TEMPLATE_DIR.iterdir():
        if not d.is_dir():
            continue
        original = name_map.get(d.name, d.name)
        tmpls: list[np.ndarray] = []
        for png in d.glob("*.png"):
            img = _imread_ja(png)
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
            flat = gray.flatten()
            flat = flat - flat.mean()
            norm = float(np.linalg.norm(flat))
            if norm == 0:
                continue
            tmpls.append(flat / norm)
        if tmpls:
            result[original] = tmpls
    return result


def _prepare_green_name_query(img_bgr: np.ndarray) -> np.ndarray | None:
    if img_bgr is None or img_bgr.size == 0:
        return None
    resized = cv2.resize(img_bgr, _GREEN_NAME_TEMPLATE_SIZE, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY).astype(np.float32)
    flat = gray.flatten()
    flat = flat - flat.mean()
    norm = float(np.linalg.norm(flat))
    if norm == 0:
        return None
    return flat / norm


def match_green_name(img_bgr: np.ndarray) -> list[tuple[str, float]]:
    """緑因子タイルの名前領域から (label, score) 降順リストを返す。"""
    q = _prepare_green_name_query(img_bgr)
    if q is None:
        return []
    tmpls = _load_green_name_templates()
    if not tmpls:
        return []
    scored: list[tuple[str, float]] = []
    for label, samples in tmpls.items():
        max_sim = max(float(q @ s) for s in samples)
        score = (max_sim + 1.0) / 2.0
        scored.append((label, score))
    scored.sort(key=lambda x: -x[1])
    return scored
