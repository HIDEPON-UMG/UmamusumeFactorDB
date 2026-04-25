"""pytest の Red 件を項目別・症状別に集計するレポート。"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

EXPECTED_CSV = Path("tests") / "fixtures" / "expected_labels.csv"
REC_PATH = Path("tests") / "fixtures" / "colored_factors" / "recognition_results.json"


def main() -> int:
    with EXPECTED_CSV.open(encoding="utf-8-sig", newline="") as f:
        expected = list(csv.DictReader(f))
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
    print("  Red テスト分類レポート")
    print("=" * 70)
    print(f"\n全 Red 件数: {sum(len(v) for v in reds.values())}")
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
