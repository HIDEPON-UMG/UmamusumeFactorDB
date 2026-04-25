"""因子認識パイプラインの TDD 回帰テスト。

`tests/fixtures/expected_labels.csv` を真値として、
`tests/fixtures/colored_factors/recognition_results.json` の各項目を検証する。

スキーマ: image_name, role, character, blue_type, blue_star, red_type, red_star,
         green_name, green_star, source

パラメータ ID は `<image>__<role>` 形式。pytest -k でフィルタ可能。

使い方:
    # コード修正 → 再認識 → テスト
    .venv/Scripts/python.exe scripts/batch_recognize.py
    .venv/Scripts/python.exe -m pytest tests/test_recognition.py -v

    # 特定画像のみ
    .venv/Scripts/python.exe -m pytest tests/test_recognition.py -v -k "image0"

    # 青因子だけ
    .venv/Scripts/python.exe -m pytest tests/test_recognition.py::test_blue_type -v
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_CSV = ROOT / "tests" / "fixtures" / "expected_labels.csv"


def _load_expected() -> list[dict]:
    rows: list[dict] = []
    with EXPECTED_CSV.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


EXPECTED_ROWS = _load_expected()


def _rec_uma(recognition_results: dict, image: str, role: str) -> dict:
    """image / role に対する認識結果の uma 部分を取り出す。"""
    img_rec = recognition_results.get(image, {})
    if "error" in img_rec:
        pytest.fail(f"{image} の認識が失敗しています: {img_rec['error']}")
    return img_rec.get(role, {})


def _id(row: dict) -> str:
    return f"{row['image_name']}__{row['role']}"


# ---------- 各項目を別テスト関数として定義（カテゴリ別フィルタ用） ----------


@pytest.mark.parametrize("row", EXPECTED_ROWS, ids=_id)
def test_character(recognition_results, row):
    got = _rec_uma(recognition_results, row["image_name"], row["role"]).get("character", "")
    assert got == row["character"], (
        f"[character] {row['image_name']} / {row['role']}: "
        f"認識={got!r}, 期待={row['character']!r}"
    )


@pytest.mark.parametrize("row", EXPECTED_ROWS, ids=_id)
def test_blue_type(recognition_results, row):
    got = _rec_uma(recognition_results, row["image_name"], row["role"]).get("blue", {}).get("type", "")
    assert got == row["blue_type"], (
        f"[blue_type] {row['image_name']} / {row['role']}: "
        f"認識={got!r}, 期待={row['blue_type']!r}"
    )


@pytest.mark.parametrize("row", EXPECTED_ROWS, ids=_id)
def test_blue_star(recognition_results, row):
    got = int(_rec_uma(recognition_results, row["image_name"], row["role"]).get("blue", {}).get("star") or 0)
    assert got == int(row["blue_star"]), (
        f"[blue_star] {row['image_name']} / {row['role']}: "
        f"認識=★{got}, 期待=★{row['blue_star']}"
    )


@pytest.mark.parametrize("row", EXPECTED_ROWS, ids=_id)
def test_red_type(recognition_results, row):
    got = _rec_uma(recognition_results, row["image_name"], row["role"]).get("red", {}).get("type", "")
    assert got == row["red_type"], (
        f"[red_type] {row['image_name']} / {row['role']}: "
        f"認識={got!r}, 期待={row['red_type']!r}"
    )


@pytest.mark.parametrize("row", EXPECTED_ROWS, ids=_id)
def test_red_star(recognition_results, row):
    got = int(_rec_uma(recognition_results, row["image_name"], row["role"]).get("red", {}).get("star") or 0)
    assert got == int(row["red_star"]), (
        f"[red_star] {row['image_name']} / {row['role']}: "
        f"認識=★{got}, 期待=★{row['red_star']}"
    )


@pytest.mark.parametrize("row", EXPECTED_ROWS, ids=_id)
def test_green_name(recognition_results, row):
    got = _rec_uma(recognition_results, row["image_name"], row["role"]).get("green", {}).get("name", "")
    assert got == row["green_name"], (
        f"[green_name] {row['image_name']} / {row['role']}: "
        f"認識={got!r}, 期待={row['green_name']!r}"
    )


@pytest.mark.parametrize("row", EXPECTED_ROWS, ids=_id)
def test_green_star(recognition_results, row):
    got = int(_rec_uma(recognition_results, row["image_name"], row["role"]).get("green", {}).get("star") or 0)
    assert got == int(row["green_star"]), (
        f"[green_star] {row['image_name']} / {row['role']}: "
        f"認識=★{got}, 期待=★{row['green_star']}"
    )
