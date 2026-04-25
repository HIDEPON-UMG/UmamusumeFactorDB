"""pytest 共通 fixture。

- ROOT を CWD に固定（cv2.imread が日本語を含む絶対パスを扱えない Windows 対策）
- src/ を sys.path に追加
- session-scoped で recognition_results.json を読み込み、テストで使い回す
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

REC_PATH = ROOT / "tests" / "fixtures" / "colored_factors" / "recognition_results.json"


@pytest.fixture(scope="session")
def recognition_results() -> dict:
    """scripts/batch_recognize.py が生成した認識結果 JSON を読み込む。

    TDD ループ:
      1. コードを修正
      2. `.venv/Scripts/python.exe scripts/batch_recognize.py` で認識を走らせ直す
      3. `pytest tests/test_recognition.py -v` で Red/Green を確認

    コード修正のたびに pytest 側で analyze_image を呼ぶと 26 画像 × 15s ≈ 6分で
    とても重くなるため、認識は scripts 側で一括してキャッシュする運用にする。
    """
    if not REC_PATH.exists():
        pytest.fail(
            f"{REC_PATH} が存在しません。"
            "まず `.venv/Scripts/python.exe scripts/batch_recognize.py` を実行してください。"
        )
    return json.loads(REC_PATH.read_text(encoding="utf-8"))
