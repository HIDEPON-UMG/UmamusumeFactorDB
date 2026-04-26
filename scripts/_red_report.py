"""pytest の Red 件を項目別・症状別に集計するレポート。

オプション:
    --scope {all, existing, new, unseen}
        all      : すべての画像（デフォルト）
        existing : new_ / unseen_ 以外（既存 28 画像）
        new      : new_ 始まり（E プラン用に追加した検証セット、テンプレ訓練に含まれる）
        unseen   : unseen_ 始まり（中期 Day 1 後の汎化検証用、テンプレ訓練に含まれない）
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

EXPECTED_CSV = Path("tests") / "fixtures" / "expected_labels.csv"
REC_PATH = Path("tests") / "fixtures" / "colored_factors" / "recognition_results.json"


def _scope_filter(image: str, scope: str) -> bool:
    if scope == "all":
        return True
    if scope == "new":
        return image.startswith("new_")
    if scope == "unseen":
        return image.startswith("unseen_")
    if scope == "existing":
        return not image.startswith(("new_", "unseen_"))
    raise ValueError(f"unknown scope: {scope}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=["all", "existing", "new", "unseen"], default="all")
    args = parser.parse_args()

    with EXPECTED_CSV.open(encoding="utf-8-sig", newline="") as f:
        expected = [r for r in csv.DictReader(f) if _scope_filter(r["image_name"], args.scope)]
    rec = json.loads(REC_PATH.read_text(encoding="utf-8"))

    # 項目別の Red を収集
    reds: dict[str, list[dict]] = defaultdict(list)

    fields = [
        ("character", "character"),
        ("blue_type", ("blue", "type")),
        ("blue_star", ("blue", "star")),
        ("red_type", ("red", "type")),
        ("red_star", ("red", "star")),
        ("green_name", ("green", "name")),
        ("green_star", ("green", "star")),
    ]

    for row in expected:
        img = row["image_name"]
        role = row["role"]
        uma = rec.get(img, {}).get(role, {})
        if "error" in rec.get(img, {}):
            continue
        for csv_field, rec_field in fields:
            if isinstance(rec_field, tuple):
                got = uma.get(rec_field[0], {}).get(rec_field[1], "")
            else:
                got = uma.get(rec_field, "")
            exp = row[csv_field]
            if csv_field.endswith("_star"):
                got = int(got or 0)
                exp = int(exp or 0)
            if got != exp:
                reds[csv_field].append({
                    "image": img, "role": role,
                    "got": got, "expected": exp,
                })

    # Summary
    print("=" * 70)
    print(f"  Red テスト分類レポート (scope={args.scope})")
    print("=" * 70)
    images_in_scope = {r["image_name"] for r in expected}
    total_cases = len(expected) * 7  # 7 項目
    total_reds = sum(len(v) for v in reds.values())
    print(f"\n対象画像数: {len(images_in_scope)}")
    print(f"対象ケース数: {total_cases} (= 行数 {len(expected)} × 7 項目)")
    print(f"Red 件数: {total_reds} ({100.0 * total_reds / max(total_cases, 1):.1f}%)")
    print("\n## 項目別 Red 件数")
    for f, _ in fields:
        print(f"  {f:<14s}: {len(reds.get(f, []))} 件")

    # 各項目の詳細
    for f, _ in fields:
        items = reds.get(f, [])
        if not items:
            continue
        print(f"\n## {f} の Red ({len(items)} 件)")
        for it in items:
            g = it["got"] if it["got"] != "" else "(空)"
            e = it["expected"] if it["expected"] != "" else "(空)"
            print(f"  [{it['image'][:42]:<42s} / {it['role']:<8s}] {g!r:<30s} → 期待 {e!r}")

    # 画像別の Red 件数（修正優先度）
    per_image: Counter = Counter()
    for items in reds.values():
        for it in items:
            per_image[it["image"]] += 1
    print("\n## 画像別 Red 件数（Top 15, 修正優先度）")
    for img, cnt in per_image.most_common(15):
        print(f"  {cnt:3d} 件: {img}")

    # role別
    print("\n## role 別 Red 件数")
    per_role: Counter = Counter()
    for items in reds.values():
        for it in items:
            per_role[it["role"]] += 1
    for role, cnt in per_role.most_common():
        print(f"  {role}: {cnt} 件")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
