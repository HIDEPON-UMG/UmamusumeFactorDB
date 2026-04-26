"""Pictures フォルダから新規因子画像を fixtures に <prefix>NNN.png 形式でインポート。

E プラン（テンプレマッチ過学習評価）用。
- 既存 fixtures と完全分離した連番命名にして、_red_report.py の --scope で集計しやすくする
- jpg は OpenCV 経由で png に再エンコード
- マッピング (<prefix>NNN -> 元ファイル名) を tests/fixtures/<prefix>image_map.csv に出力
- ファイルサイズ / ピクセルサイズで因子画像でないものを除外
- 既存マップ CSV の src_name に既出のファイルはスキップ（重複取り込み防止）

使い方（必ずプロジェクトルートから実行）:
    .venv/Scripts/python.exe scripts/_import_new_images.py            # 既定 prefix=new_
    .venv/Scripts/python.exe scripts/_import_new_images.py --prefix unseen_
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

SRC_DIR = Path(r"C:\Users\hidek\Pictures\UmamusumeReceiptMaker")
DST_DIR = Path("tests") / "fixtures"

# 因子画像と判定するための最小しきい値
MIN_BYTES = 50_000
MIN_WIDTH = 400
MIN_HEIGHT = 400


def _read_image_jp(path: Path):
    """Windows 日本語パス対応の imread。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _write_image_jp(path: Path, img) -> None:
    """Windows 日本語パス対応の imwrite。PNG で書き出す。"""
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError(f"imencode failed: {path}")
    path.write_bytes(buf.tobytes())


def _load_imported_src_names() -> set[str]:
    """既存マップ CSV (new_image_map.csv / unseen_image_map.csv 等) から取込済みの src_name を集める。"""
    imported: set[str] = set()
    for csv_path in DST_DIR.glob("*image_map.csv"):
        try:
            with csv_path.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if r.get("src_name"):
                        imported.add(r["src_name"])
        except Exception:
            continue
    return imported


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prefix",
        default="new_",
        help="出力ファイル名の prefix (例: new_, unseen_)。末尾アンダースコア込みで指定。",
    )
    parser.add_argument(
        "--src-dir",
        default=str(SRC_DIR),
        help="取り込み元ディレクトリ (既定: Pictures/UmamusumeReceiptMaker)",
    )
    args = parser.parse_args()

    src_dir = Path(args.src_dir)
    if not src_dir.exists():
        print(f"取り込み元が存在しません: {src_dir}", file=sys.stderr)
        return 1

    prefix = args.prefix
    map_path = DST_DIR / f"{prefix}image_map.csv"

    # 既存 fixtures に同名で取り込み済みのファイル + 過去マップ CSV の src_name はスキップ
    existing_names = {p.name for p in DST_DIR.glob("*.png")}
    imported_src_names = _load_imported_src_names()

    selected: list[tuple[Path, str]] = []
    for src in sorted(src_dir.iterdir(), key=lambda p: p.name.lower()):
        if not src.is_file():
            continue
        if src.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        if src.name in existing_names:
            print(f"  skip (same filename in fixtures): {src.name}")
            continue
        if src.name in imported_src_names:
            print(f"  skip (already imported under another prefix): {src.name}")
            continue
        if src.stat().st_size < MIN_BYTES:
            print(f"  skip (small bytes): {src.name} ({src.stat().st_size} B)")
            continue
        img = _read_image_jp(src)
        if img is None:
            print(f"  skip (decode failed): {src.name}")
            continue
        h, w = img.shape[:2]
        if w < MIN_WIDTH or h < MIN_HEIGHT:
            print(f"  skip (small dim): {src.name} ({w}x{h})")
            continue
        new_name = f"{prefix}{len(selected) + 1:03d}.png"
        selected.append((src, new_name))

    if not selected:
        print("インポート対象が 0 件でした", file=sys.stderr)
        return 1

    print(f"\n{len(selected)} 枚を fixtures にインポートします (prefix={prefix})")
    rows: list[dict] = []
    for src, dst_name in selected:
        dst = DST_DIR / dst_name
        img = _read_image_jp(src)
        _write_image_jp(dst, img)
        h, w = img.shape[:2]
        print(f"  {src.name} -> {dst_name}  ({w}x{h})")
        rows.append({
            "new_name": dst_name,
            "src_name": src.name,
            "src_path": str(src),
            "width": w,
            "height": h,
            "src_bytes": src.stat().st_size,
        })

    with map_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["new_name", "src_name", "src_path", "width", "height", "src_bytes"],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nマッピング出力: {map_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
