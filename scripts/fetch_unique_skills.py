"""UmaTools リポジトリから固有スキル → カード（[衣装名]キャラ名）の対応表を生成する。

データソース: https://github.com/daftuyda/UmaTools
  - assets/skills_green.json — 緑スキル定義（char にカード ID 配列が付く）
  - assets/uma_data.json     — 全カード情報（UmaId, UmaNameJP, UmaNicknameJP）

使い方:
    python scripts/fetch_unique_skills.py

出力:
    config/unique_skill_to_character.json
        { "<固有スキル名>": "[衣装名]キャラ名", ... }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests


SKILLS_URL = "https://raw.githubusercontent.com/daftuyda/UmaTools/main/assets/skills_all.json"
UMA_DATA_URL = "https://raw.githubusercontent.com/daftuyda/UmaTools/main/assets/uma_data.json"


def _get(url: str) -> list[dict]:
    print(f"GET {url}", file=sys.stderr)
    r = requests.get(url, timeout=30, headers={"User-Agent": "UmamusumeFactorDB/1.0"})
    r.raise_for_status()
    return r.json()


def _build_card_id_to_name(uma_data: list[dict]) -> dict[int, str]:
    """UmaId (int) → "[衣装名]キャラ名" 形式の辞書を作る。"""
    out: dict[int, str] = {}
    for entry in uma_data:
        uid_str = entry.get("UmaId")
        name_jp = entry.get("UmaNameJP") or entry.get("UmaName") or ""
        nick_jp = entry.get("UmaNicknameJP") or entry.get("UmaNickname") or ""
        if not uid_str or not name_jp:
            continue
        try:
            uid = int(uid_str)
        except (TypeError, ValueError):
            continue
        # 衣装名がある場合のみ括弧付き、ない場合は素名
        if nick_jp:
            out[uid] = f"[{nick_jp}]{name_jp}"
        else:
            out[uid] = name_jp
    return out


def build_mapping() -> dict[str, str]:
    skills = _get(SKILLS_URL)
    umas = _get(UMA_DATA_URL)
    print(f"skills_green: {len(skills)}, uma_data: {len(umas)}", file=sys.stderr)

    card_names = _build_card_id_to_name(umas)
    print(f"card id map: {len(card_names)}", file=sys.stderr)

    mapping: dict[str, str] = {}
    missing_char_ids: set[int] = set()
    skipped = 0
    for sk in skills:
        # 固有スキルは rarity=4（基本）または rarity=5（進化）で char にカード ID が入る
        # 継承因子画面には両方が出現しうるため両方採用
        if sk.get("rarity") not in (4, 5):
            continue
        jpname = sk.get("jpname") or sk.get("name") or ""
        chars = sk.get("char") or []
        if not jpname or not chars:
            skipped += 1
            continue
        # char[0] = このスキルを所有する主要カード ID
        try:
            cid = int(chars[0])
        except (TypeError, ValueError):
            skipped += 1
            continue
        name = card_names.get(cid)
        if name is None:
            missing_char_ids.add(cid)
            continue
        # 同名スキルが複数カードに紐づく場合、最初の 1 件を採用
        if jpname not in mapping:
            mapping[jpname] = name

    if missing_char_ids:
        print(f"WARN: uma_data に存在しない char ID {len(missing_char_ids)} 件: {list(missing_char_ids)[:5]}...", file=sys.stderr)
    if skipped:
        print(f"WARN: skipped (jpname/char 欠損): {skipped}", file=sys.stderr)
    return mapping


def main() -> int:
    out_path = Path(__file__).resolve().parents[1] / "config" / "unique_skill_to_character.json"
    mapping = build_mapping()
    if len(mapping) < 100:
        print(f"WARN: mapping 件数が少ない ({len(mapping)})", file=sys.stderr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out_path} ({len(mapping)} entries)")
    for i, (k, v) in enumerate(mapping.items()):
        if i >= 5:
            break
        print(f"  {k} -> {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
