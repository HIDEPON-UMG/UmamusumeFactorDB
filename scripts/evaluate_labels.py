"""labels.csv × recognition_results.json で色付き因子の精度を評価する。

使い方:
    # 現行の認識結果の誤認件数を確認
    python scripts/evaluate_labels.py \
        --labels tests/fixtures/labels_2026-04-20T18-54-21.csv \
        --after tests/fixtures/colored_factors/recognition_results.json

    # 改善前後を比較
    python scripts/evaluate_labels.py \
        --labels tests/fixtures/labels_2026-04-20T18-54-21.csv \
        --before tests/fixtures/colored_factors/recognition_results_before_T3a.json \
        --after tests/fixtures/colored_factors/recognition_results.json

labels.csv のスキーマ:
    image_name, status, role, color, wrong_value, correct_value, wrong_star, correct_star
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)


def load_recognition(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def get_slot(rec: dict, image: str, role: str, color: str) -> tuple[str, int]:
    """recognition_results から (value, star) を取り出す。color は blue/red/green。"""
    img_rec = rec.get(image, {})
    if "error" in img_rec:
        return ("", 0)
    role_data = img_rec.get(role, {}) or {}
    field = role_data.get(color, {}) or {}
    value = field.get("type") or field.get("name") or ""
    star = int(field.get("star") or 0)
    return (value, star)


def load_labels(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def evaluate(labels: list[dict], rec: dict) -> dict:
    """labels の各 edited 行について、rec が正解と一致するかを判定。

    Returns:
        集計結果 dict
    """
    stars_correct = 0
    stars_wrong = 0
    names_correct = 0
    names_wrong = 0
    by_color_correct: Counter = Counter()
    by_color_wrong: Counter = Counter()
    star_confusion: Counter = Counter()  # (wrong_star -> recognized_star) のペア
    details: list[dict] = []

    for r in labels:
        if r["status"] != "edited":
            continue
        image = r["image_name"]
        role = r["role"]
        color = r["color"]
        correct_value = r["correct_value"]
        correct_star = int(r["correct_star"])

        rec_value, rec_star = get_slot(rec, image, role, color)

        name_hit = (rec_value == correct_value)
        star_hit = (rec_star == correct_star)

        if name_hit:
            names_correct += 1
        else:
            names_wrong += 1
        if star_hit:
            stars_correct += 1
            by_color_correct[color] += 1
        else:
            stars_wrong += 1
            by_color_wrong[color] += 1
            star_confusion[(correct_star, rec_star)] += 1

        details.append({
            "image": image,
            "role": role,
            "color": color,
            "correct_value": correct_value,
            "rec_value": rec_value,
            "correct_star": correct_star,
            "rec_star": rec_star,
            "name_hit": name_hit,
            "star_hit": star_hit,
        })

    total = stars_correct + stars_wrong
    return {
        "total_edited_rows": total,
        "stars_correct": stars_correct,
        "stars_wrong": stars_wrong,
        "star_accuracy": stars_correct / total if total else 0.0,
        "names_correct": names_correct,
        "names_wrong": names_wrong,
        "by_color_correct": dict(by_color_correct),
        "by_color_wrong": dict(by_color_wrong),
        "star_confusion": {f"{k[0]}->{k[1]}": v for k, v in star_confusion.items()},
        "details": details,
    }


def print_single(title: str, summary: dict) -> None:
    t = summary["total_edited_rows"]
    print(f"\n===== {title} =====")
    print(f"  対象 edited 行: {t}")
    print(f"  ★数一致:        {summary['stars_correct']} / {t}  ({summary['star_accuracy']:.1%})")
    print(f"  ★数誤認:        {summary['stars_wrong']}")
    if summary["by_color_wrong"]:
        print(f"  色別★誤認:      {summary['by_color_wrong']}")
    if summary["star_confusion"]:
        print(f"  ★混同ペア:      {summary['star_confusion']}")
    if summary["names_wrong"]:
        print(f"  因子名誤認:      {summary['names_wrong']} 件（本来は正解だったはず）")


def print_diff(before: dict, after: dict, details_before: list[dict], details_after: list[dict]) -> None:
    """before と after の差分（どの行が改善/悪化/据え置きか）を表示。"""
    # details は同じ順序と仮定（同一 labels から生成）
    improved = []  # before 誤 → after 正
    regressed = []  # before 正 → after 誤
    stayed_wrong = []  # both wrong
    stayed_right = []  # both right

    for db, da in zip(details_before, details_after):
        assert db["image"] == da["image"] and db["role"] == da["role"] and db["color"] == da["color"]
        if db["star_hit"] and da["star_hit"]:
            stayed_right.append(da)
        elif not db["star_hit"] and da["star_hit"]:
            improved.append(da)
        elif db["star_hit"] and not da["star_hit"]:
            regressed.append(da)
        else:
            stayed_wrong.append(da)

    print("\n===== 前後比較 =====")
    print(f"  改善（誤→正）: {len(improved)} 件")
    for d in improved:
        print(f"    [+] {d['image']} / {d['role']} / {d['color']}: ★{d['rec_star']} (正解)")
    print(f"  悪化（正→誤）: {len(regressed)} 件")
    for d in regressed:
        print(f"    [-] {d['image']} / {d['role']} / {d['color']}: ★{d['rec_star']}（正解★{d['correct_star']}）")
    print(f"  据え置き誤認:   {len(stayed_wrong)} 件")
    for d in stayed_wrong:
        print(f"    [=] {d['image']} / {d['role']} / {d['color']}: ★{d['rec_star']}（正解★{d['correct_star']}）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path, help="新しい（または現行の）recognition_results.json")
    parser.add_argument("--before", type=Path, help="比較用：旧バージョンの recognition_results.json")
    args = parser.parse_args()

    labels = load_labels(args.labels)
    rec_after = load_recognition(args.after)
    eval_after = evaluate(labels, rec_after)

    if args.before:
        rec_before = load_recognition(args.before)
        eval_before = evaluate(labels, rec_before)
        print_single("改善前", eval_before)
        print_single("改善後", eval_after)
        print_diff(eval_before, eval_after, eval_before["details"], eval_after["details"])
    else:
        print_single("現在の認識結果", eval_after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
