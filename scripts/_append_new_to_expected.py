"""recognition_results.json の new_*.png / unseen_*.png 行を expected_labels.csv に append-only で追加する。

E プラン + 中期汎化検証用。`_build_expected_labels.py` は CSV を完全に再生成するので、
user 確定済み行が pending に巻き戻ってしまう。本スクリプトは既存行を一切変更せず、
指定 prefix 画像分の pending 行のみ末尾に追加する。

使い方:
    .venv/Scripts/python.exe scripts/_append_new_to_expected.py            # 既定: new_ + unseen_ 両方
    .venv/Scripts/python.exe scripts/_append_new_to_expected.py --prefix unseen_
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

FIX = Path("tests") / "fixtures"
REC_PATH = FIX / "colored_factors" / "recognition_results.json"
EXPECTED_CSV = FIX / "expected_labels.csv"

ROLES = ("main", "parent1", "parent2")
HEADERS = [
    "image_name", "role",
    "character",
    "blue_type", "blue_star",
    "red_type", "red_star",
    "green_name", "green_star",
    "source",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prefix",
        action="append",
        default=None,
        help="対象画像の prefix（複数指定可、例: --prefix new_ --prefix unseen_）。指定なしなら new_ + unseen_ 両方。",
    )
    args = parser.parse_args()
    prefixes = tuple(args.prefix) if args.prefix else ("new_", "unseen_")

    if not REC_PATH.exists():
        print(f"認識結果が見つかりません: {REC_PATH}", file=sys.stderr)
        return 1
    if not EXPECTED_CSV.exists():
        print(f"既存 expected_labels.csv が見つかりません: {EXPECTED_CSV}", file=sys.stderr)
        return 1

    rec = json.loads(REC_PATH.read_text(encoding="utf-8"))

    # 既存行をそのまま保持
    with EXPECTED_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        existing_rows = list(reader)
        existing_keys = {(r["image_name"], r["role"]) for r in existing_rows}

    # 指定 prefix 画像で、既存 CSV にまだ無い (image, role) を pending で追加
    new_rows: list[dict] = []
    for image in sorted(rec.keys()):
        if not image.startswith(prefixes):
            continue
        img_rec = rec[image]
        if "error" in img_rec:
            print(f"  skip (error): {image}", file=sys.stderr)
            continue
        for role in ROLES:
            if (image, role) in existing_keys:
                continue
            d = img_rec.get(role, {})
            b = d.get("blue", {})
            r = d.get("red", {})
            g = d.get("green", {})
            new_rows.append({
                "image_name": image,
                "role": role,
                "character": d.get("character", ""),
                "blue_type": b.get("type", ""),
                "blue_star": int(b.get("star") or 0),
                "red_type": r.get("type", ""),
                "red_star": int(r.get("star") or 0),
                "green_name": g.get("name", ""),
                "green_star": int(g.get("star") or 0),
                "source": "pending",
            })

    if not new_rows:
        print(f"追加対象 (prefix={prefixes}) はありません（全行既出または該当画像が認識結果に無い）")
        return 0

    # 既存 + 新規を 1 つにまとめて書き出し（既存行は変更なし）
    out = existing_rows + new_rows
    with EXPECTED_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        w.writerows(out)

    images_added = len({r["image_name"] for r in new_rows})
    print(f"既存 {len(existing_rows)} 行を保持、新規 {len(new_rows)} 行 ({images_added} 画像分) を追加")
    print(f"出力: {EXPECTED_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
