"""先のユーザー対話結果 (image0_test.png) を expected_labels.csv に反映。"""

from __future__ import annotations

import csv
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

CSV_PATH = Path("tests") / "fixtures" / "expected_labels.csv"

IMAGE0 = "image0_test.png"
# 先の Q&A で得た正解値
IMAGE0_TRUTH = {
    "main": {
        "character": "[Sunlit Outsider]ステイゴールド",
        "blue_type": "スタミナ", "blue_star": 2,
        "red_type": "差し", "red_star": 3,
        "green_name": "黄金を訪ねて", "green_star": 3,
    },
    "parent1": {
        "character": "[キセキの白星]オグリキャップ",
        "blue_type": "スピード", "blue_star": 3,
        "red_type": "ダート", "red_star": 2,
        "green_name": "勝利の鼓動", "green_star": 3,
    },
    "parent2": {
        "character": "[王者の喊声]ジャングルポケット",
        "blue_type": "スタミナ", "blue_star": 2,
        "red_type": "ダート", "red_star": 3,
        "green_name": "Faith in the Feral", "green_star": 3,
    },
}


def main() -> int:
    rows: list[dict] = []
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            if row["image_name"] == IMAGE0 and row["role"] in IMAGE0_TRUTH:
                t = IMAGE0_TRUTH[row["role"]]
                row["character"] = t["character"]
                row["blue_type"] = t["blue_type"]
                row["blue_star"] = str(t["blue_star"])
                row["red_type"] = t["red_type"]
                row["red_star"] = str(t["red_star"])
                row["green_name"] = t["green_name"]
                row["green_star"] = str(t["green_star"])
                row["source"] = "user"
            rows.append(row)

    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"image0_test.png の 3 role を user で上書きしました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
