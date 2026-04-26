"""recognition_results.json + labels_2026-04-20 から、TDD 用の全項目正解 CSV を組み立てる。

- 既存 labels_2026-04-20 に edited 行がある項目 → correct_value/correct_star を正解として採用
- edited 行がない項目 → recognition_results の値を正解と見なす（この時点では）
- image0 は先の対話結果を手動で上書き
- umamusume_* は後でユーザー判定で上書き

出力: tests/fixtures/expected_labels.csv
スキーマ: image_name, role, character, blue_type, blue_star, red_type, red_star, green_name, green_star, source
  source: 'auto'（既存ラベル+認識結果からの自動生成） / 'user'（ユーザー判定で確定）
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

FIX = Path("tests") / "fixtures"
REC_PATH = FIX / "colored_factors" / "recognition_results.json"
LABELS_OLD_PATH = FIX / "labels_2026-04-20T18-54-21.csv"
OUT_PATH = FIX / "expected_labels.csv"

ROLES = ("main", "parent1", "parent2")


def load_old_labels() -> dict[tuple[str, str, str], tuple[str, int]]:
    """旧 labels の edited 行を (image, role, color) -> (correct_value, correct_star) に。"""
    result: dict[tuple[str, str, str], tuple[str, int]] = {}
    with LABELS_OLD_PATH.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r["status"] != "edited":
                continue
            result[(r["image_name"], r["role"], r["color"])] = (
                r["correct_value"],
                int(r["correct_star"]),
            )
    return result


def main() -> int:
    rec = json.loads(REC_PATH.read_text(encoding="utf-8"))
    old = load_old_labels()

    with OUT_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "image_name", "role",
            "character",
            "blue_type", "blue_star",
            "red_type", "red_star",
            "green_name", "green_star",
            "source",
        ])
        for image in sorted(rec.keys()):
            img_rec = rec[image]
            if "error" in img_rec:
                continue
            for role in ROLES:
                d = img_rec.get(role, {})
                char = d.get("character", "")
                b = d.get("blue", {})
                r = d.get("red", {})
                g = d.get("green", {})
                blue_type = b.get("type", "")
                blue_star = int(b.get("star") or 0)
                red_type = r.get("type", "")
                red_star = int(r.get("star") or 0)
                green_name = g.get("name", "")
                green_star = int(g.get("star") or 0)

                # 旧 labels で誤認とマークされた項目を上書き
                if (image, role, "blue") in old:
                    blue_type, blue_star = old[(image, role, "blue")]
                if (image, role, "red") in old:
                    red_type, red_star = old[(image, role, "red")]
                if (image, role, "green") in old:
                    green_name, green_star = old[(image, role, "green")]

                source = "auto"
                if image.startswith(("umamusume_", "new_")):
                    source = "pending"  # ユーザー判定待ち（new_ は E プラン用の検証セット）
                w.writerow([
                    image, role,
                    char,
                    blue_type, blue_star,
                    red_type, red_star,
                    green_name, green_star,
                    source,
                ])

    print(f"出力: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
