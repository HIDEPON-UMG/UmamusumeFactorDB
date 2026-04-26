"""ONNX ファインチューン用データセット構築スクリプト（中期 Day 2）。

入力:
    tests/fixtures/expected_labels.csv の user / auto 確定行
出力:
    datasets/finetune/factor/{blue,red,green}/<label_safe>/<image>__<role>__<aug>.png
    datasets/finetune/character/<label_safe>/<image>__<role>__<aug>.png
    datasets/finetune/manifest.csv  (path, head, label, role, image, aug_idx, split)
    datasets/finetune/stats.json    (label 別件数、入力 hash 重複検知、split 分布)

データ拡張 (各 base crop を 8 倍に拡張):
    0: original
    1: CLAHE(clipLimit=2.0, tileGridSize=(4,4))
    2: ガウシアンノイズ σ=3
    3: 平行移動 +5px (右)
    4: 平行移動 -5px (左)
    5: 回転 +3 度
    6: 回転 -3 度
    7: 明度 0.85 倍

train/val 分割:
    既存 28 画像 (receipt_/combine_/sample_/image0_/umamusume_) は val 強制
    new_*, unseen_* は train 強制
    → 既存 28 画像で val_acc ≥ 99% を強制ガードできる

使い方:
    .venv/Scripts/python.exe scripts/build_finetune_dataset.py
    .venv/Scripts/python.exe scripts/build_finetune_dataset.py --dry-run
    .venv/Scripts/python.exe scripts/build_finetune_dataset.py --augment-count 8
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))

from umafactor.cropper import (
    BASE_WIDTH, detect_chara_sections, extract_factor_boxes, normalize_width,
)
from umafactor.pipeline import _display_crop_from_original, _extract_character_icon_bgr

EXPECTED = Path("tests") / "fixtures" / "expected_labels.csv"
FIX_DIR = Path("tests") / "fixtures"
OUT_ROOT = Path("datasets") / "finetune"
DEBUG_DIR = OUT_ROOT / "_debug_dryrun"

ROLES = ["main", "parent1", "parent2"]

# 出力サイズ (元 ONNX 互換)
FACTOR_SIZE = (168, 16)   # (W, H)
CHARACTER_SIZE = (32, 32)


def _safe_dirname(label: str) -> str:
    """Windows 禁則文字を _ に置換。"""
    bad = '\\/:*?"<>|'
    return "".join("_" if c in bad else c for c in label)


def _imread_ja(path: Path):
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def _imwrite_ja(path: Path, img) -> None:
    ok, buf = cv2.imencode(".png", img)
    if ok:
        path.write_bytes(buf.tobytes())


def _split_for_image(image_name: str) -> str:
    """train / val を画像名 prefix で固定。"""
    if image_name.startswith(("new_", "unseen_")):
        return "train"
    return "val"


def _augment(img: np.ndarray, aug_idx: int, rng: np.random.Generator) -> np.ndarray:
    """8 通りのデータ拡張。aug_idx=0 は original。"""
    if aug_idx == 0:
        return img.copy()
    if aug_idx == 1:
        # CLAHE: LAB の L チャンネルにのみ適用
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
        l2 = clahe.apply(l)
        return cv2.cvtColor(cv2.merge([l2, a, b]), cv2.COLOR_LAB2BGR)
    if aug_idx == 2:
        # ガウシアンノイズ σ=3
        noise = rng.normal(0, 3, img.shape).astype(np.float32)
        out = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
        return out
    if aug_idx in (3, 4):
        # 平行移動 ±5px
        dx = 5 if aug_idx == 3 else -5
        h, w = img.shape[:2]
        M = np.float32([[1, 0, dx], [0, 1, 0]])
        return cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
    if aug_idx in (5, 6):
        # 回転 ±3 度
        deg = 3 if aug_idx == 5 else -3
        h, w = img.shape[:2]
        center = (w / 2.0, h / 2.0)
        M = cv2.getRotationMatrix2D(center, deg, 1.0)
        return cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
    if aug_idx == 7:
        # 明度 0.85 倍
        out = np.clip(img.astype(np.float32) * 0.85, 0, 255).astype(np.uint8)
        return out
    raise ValueError(f"unknown aug_idx: {aug_idx}")


def _crop_factor_box(img_orig, norm, scale, boxes, uma_idx: int, color: str):
    """
    指定 uma の指定色因子 box の display_crop を返す。
    color: 'blue' (row=0,col=0), 'red' (row=0,col=1), 'green' (row=1,col=0 or color=green)
    """
    if color == "blue":
        cands = [b for b in boxes if b.uma_index == uma_idx and b.row_index == 0 and b.col_index == 0]
        if not cands:
            return None
        return _display_crop_from_original(img_orig, cands[0].bbox, scale, pad_y_norm=8)
    if color == "red":
        cands = [b for b in boxes if b.uma_index == uma_idx and b.row_index == 0 and b.col_index == 1]
        if not cands:
            return None
        x0, y0, x1, y1 = cands[0].bbox
        bbox = (x0, y0, x1, min(norm.shape[0], y1 + 14))
        return _display_crop_from_original(img_orig, bbox, scale, pad_y_norm=2)
    if color == "green":
        # row=1 col=0 または color=green の col=0 を優先
        cands = [b for b in boxes if b.uma_index == uma_idx and b.col_index == 0 and b.color == "green"]
        if not cands:
            cands = [b for b in boxes if b.uma_index == uma_idx and b.row_index == 1 and b.col_index == 0]
        if not cands:
            return None
        return _display_crop_from_original(img_orig, cands[0].bbox, scale, pad_y_norm=2)
    raise ValueError(f"unknown color: {color}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--targets", nargs="+", default=["factor"],
        choices=["factor", "character"],
        help="生成対象 head (factor / character)。複数指定可。",
    )
    parser.add_argument("--augment-count", type=int, default=8, help="1 サンプルあたりの拡張枚数")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="1 画像のみ全 8 拡張を datasets/finetune/_debug_dryrun/ に書き出して終了",
    )
    args = parser.parse_args()

    if args.augment_count > 8:
        print(f"--augment-count > 8 はサポート外です。8 に制限します。", file=sys.stderr)
        args.augment_count = 8

    if not EXPECTED.exists():
        print(f"expected_labels.csv が見つかりません: {EXPECTED}", file=sys.stderr)
        return 1

    rng = np.random.default_rng(args.seed)

    with EXPECTED.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("source") in ("user", "auto")]
    print(f"対象行数: {len(rows)} (user/auto only)")

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    # 画像ごとに 1 回だけ box 検出
    per_image_cache: dict = {}

    manifest_rows: list[dict] = []
    label_counter: Counter = Counter()
    hash_seen: set[str] = set()
    hash_collisions = 0

    dry_run_done = False

    for r in rows:
        image = r["image_name"]
        role = r["role"]
        uma_idx = ROLES.index(role)
        split = _split_for_image(image)

        # 画像読込 + box 検出 (キャッシュ)
        if image not in per_image_cache:
            try:
                img_orig = _imread_ja(FIX_DIR / image)
                if img_orig is None:
                    raise RuntimeError("imread failed")
                norm, scale = normalize_width(img_orig, BASE_WIDTH)
                sections = detect_chara_sections(norm)
                boxes = extract_factor_boxes(norm, sections)
                per_image_cache[image] = (img_orig, norm, scale, sections, boxes)
            except Exception as e:
                print(f"  skip {image}: {e}", file=sys.stderr)
                per_image_cache[image] = None
        if per_image_cache[image] is None:
            continue
        img_orig, norm, scale, sections, boxes = per_image_cache[image]

        # ---- factor head ----
        if "factor" in args.targets:
            for color, label_field in [
                ("blue", "blue_type"),
                ("red", "red_type"),
                ("green", "green_name"),
            ]:
                label = r.get(label_field, "").strip()
                if not label:
                    continue
                base_crop = _crop_factor_box(img_orig, norm, scale, boxes, uma_idx, color)
                if base_crop is None or base_crop.size == 0:
                    continue
                resized = cv2.resize(base_crop, FACTOR_SIZE, interpolation=cv2.INTER_AREA)
                label_safe = _safe_dirname(label)
                out_dir = OUT_ROOT / "factor" / color / label_safe
                out_dir.mkdir(parents=True, exist_ok=True)
                stem = image.replace(".png", "")
                for aug_idx in range(args.augment_count):
                    aug_img = _augment(resized, aug_idx, rng)
                    out_path = out_dir / f"{stem}__{role}__a{aug_idx}.png"
                    if args.dry_run:
                        # 1 枚だけ出して終了
                        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                        _imwrite_ja(DEBUG_DIR / f"factor_{color}_a{aug_idx}.png", aug_img)
                        if aug_idx == args.augment_count - 1:
                            dry_run_done = True
                        continue
                    _imwrite_ja(out_path, aug_img)
                    h = hashlib.md5(aug_img.tobytes()).hexdigest()
                    if h in hash_seen:
                        hash_collisions += 1
                    hash_seen.add(h)
                    manifest_rows.append({
                        "path": str(out_path).replace("\\", "/"),
                        "head": f"factor_{color}",
                        "label": label,
                        "role": role,
                        "image": image,
                        "aug_idx": aug_idx,
                        "split": split,
                    })
                    label_counter[(f"factor_{color}", label, split)] += 1

        # ---- character head ----
        if "character" in args.targets:
            label = r.get("character", "").strip()
            if label:
                # section から portrait icon
                sec = next((s for s in sections if s.uma_index == uma_idx), None)
                if sec is not None:
                    base_crop = _extract_character_icon_bgr(norm, sec)
                    if base_crop.size > 0:
                        # 既に 32x32 にリサイズ済みだが明示的に
                        resized = cv2.resize(base_crop, CHARACTER_SIZE, interpolation=cv2.INTER_LINEAR)
                        label_safe = _safe_dirname(label)
                        out_dir = OUT_ROOT / "character" / label_safe
                        out_dir.mkdir(parents=True, exist_ok=True)
                        stem = image.replace(".png", "")
                        for aug_idx in range(args.augment_count):
                            aug_img = _augment(resized, aug_idx, rng)
                            out_path = out_dir / f"{stem}__{role}__a{aug_idx}.png"
                            if args.dry_run:
                                DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                                _imwrite_ja(DEBUG_DIR / f"character_a{aug_idx}.png", aug_img)
                                if aug_idx == args.augment_count - 1:
                                    dry_run_done = True
                                continue
                            _imwrite_ja(out_path, aug_img)
                            h = hashlib.md5(aug_img.tobytes()).hexdigest()
                            if h in hash_seen:
                                hash_collisions += 1
                            hash_seen.add(h)
                            manifest_rows.append({
                                "path": str(out_path).replace("\\", "/"),
                                "head": "character",
                                "label": label,
                                "role": role,
                                "image": image,
                                "aug_idx": aug_idx,
                                "split": split,
                            })
                            label_counter[("character", label, split)] += 1

        if args.dry_run and dry_run_done:
            print(f"\ndry-run 完了: {DEBUG_DIR} を確認してください")
            return 0

    if args.dry_run:
        print("dry-run でした（書き出し画像なし）", file=sys.stderr)
        return 1

    # manifest CSV 書き出し
    manifest_path = OUT_ROOT / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["path", "head", "label", "role", "image", "aug_idx", "split"],
        )
        w.writeheader()
        w.writerows(manifest_rows)

    # stats JSON
    head_split_counts: defaultdict = defaultdict(int)
    head_label_counts: defaultdict = defaultdict(lambda: defaultdict(lambda: {"train": 0, "val": 0}))
    for (head, label, split), n in label_counter.items():
        head_split_counts[(head, split)] += n
        head_label_counts[head][label][split] += n
    stats = {
        "total_samples": len(manifest_rows),
        "unique_hashes": len(hash_seen),
        "hash_collisions": hash_collisions,
        "head_split": {f"{h}/{s}": v for (h, s), v in sorted(head_split_counts.items())},
        "head_label_split": {
            head: dict(sorted(labels.items()))
            for head, labels in head_label_counts.items()
        },
    }
    stats_path = OUT_ROOT / "stats.json"
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n総サンプル数: {len(manifest_rows)}")
    print(f"ユニーク hash: {len(hash_seen)} (重複: {hash_collisions})")
    print(f"head/split 分布:")
    for k, v in sorted(head_split_counts.items()):
        print(f"  {k[0]}/{k[1]}: {v}")
    print(f"\nmanifest: {manifest_path}")
    print(f"stats: {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
