"""labeler_template.html の緑因子選択肢を埋め込み、スタンドアロンな labeler.html を生成する。

緑因子（固有スキル）は 249 件あり手書きで template に含めると視認性が悪いので、
config/unique_skill_to_character.json をビルド時に JSON 埋め込みする方式を採用。

使い方:
    .venv/Scripts/python.exe scripts/build_labeler.py

出力:
    tests/fixtures/colored_factors/labeler.html
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "scripts" / "labeler_template.html"
SKILL_JSON = ROOT / "config" / "unique_skill_to_character.json"
OUTPUT_PATH = ROOT / "tests" / "fixtures" / "colored_factors" / "labeler.html"


def main() -> int:
    if not TEMPLATE_PATH.exists():
        print(f"テンプレートがない: {TEMPLATE_PATH}", file=sys.stderr)
        return 1
    if not SKILL_JSON.exists():
        print(f"緑因子辞書がない: {SKILL_JSON}", file=sys.stderr)
        return 1

    # 緑因子辞書 { "スキル名": "キャラ名" }
    skill_map = json.loads(SKILL_JSON.read_text(encoding="utf-8"))
    green_names = sorted(skill_map.keys())

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace(
        "__GREEN_NAMES_JSON__",
        json.dumps(green_names, ensure_ascii=False),
    ).replace(
        "__CHARACTER_OF_SKILL_JSON__",
        json.dumps(skill_map, ensure_ascii=False),
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"生成: {OUTPUT_PATH.relative_to(ROOT)}  (緑因子 {len(green_names)} 件を埋め込み)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
