"""green_star の Red 件を「name 正解 vs 空」で分類し、症状パターンを整理する。

分類:
  (P1) name 空 + star 0  → cropper で row=1 col=0 box が生成されていない疑い
  (P2) name 正解 + star 過少  → ★集計が緑スロット内で完結していない
  (P3) name 誤認 + star 過少  → そもそも別の box が緑スロットに入った可能性
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

EXPECTED = Path("tests") / "fixtures" / "expected_labels.csv"
REC = Path("tests") / "fixtures" / "colored_factors" / "recognition_results.json"


def main() -> int:
    with EXPECTED.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rec = json.loads(REC.read_text(encoding="utf-8"))

    p1, p2, p3 = [], [], []
    for r in rows:
        img = r["image_name"]
        role = r["role"]
        got_star = int(rec.get(img, {}).get(role, {}).get("green", {}).get("star") or 0)
        got_name = rec.get(img, {}).get(role, {}).get("green", {}).get("name") or ""
        exp_star = int(r["green_star"])
        exp_name = r["green_name"]
        if got_star == exp_star:
            continue
        # Red case
        bucket = (img, role, got_name, got_star, exp_name, exp_star)
        if not got_name and exp_name:
            p1.append(bucket)
        elif got_name == exp_name:
            p2.append(bucket)
        else:
            p3.append(bucket)

    print("=" * 70)
    print("green_star Red の内訳")
    print("=" * 70)
    print(f"\n(P1) name 空 + star 過少 : {len(p1)} 件 — cropper 段階で緑 box 生成失敗疑い")
    for img, role, gn, gs, en, es in p1:
        print(f"    {img[:42]:<42s} / {role:<7s}  ★{gs}→★{es}  name=(空)→{en!r}")

    print(f"\n(P2) name 正解 + star 過少 : {len(p2)} 件 — ★集計ロジックの問題")
    for img, role, gn, gs, en, es in p2:
        print(f"    {img[:42]:<42s} / {role:<7s}  ★{gs}→★{es}  name={gn!r}")

    print(f"\n(P3) name 誤認 + star 過少 : {len(p3)} 件 — box 選択 or ★集計複合")
    for img, role, gn, gs, en, es in p3:
        print(f"    {img[:42]:<42s} / {role:<7s}  ★{gs}→★{es}  name={gn!r}→{en!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
