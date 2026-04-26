"""tests/fixtures/ 配下の画像を現行パイプラインで一括推論し、結果を JSON に集約する。

ラベラー UI（tests/fixtures/colored_factors/labeler.html）に食わせるための基礎データ生成スクリプト。
EasyOCR の初回ロード後は同一プロセス内でモデルを使い回すので、run.py を画像ごとに起動するより高速。

使い方（必ずプロジェクトルートから実行）:
    .venv/Scripts/python.exe scripts/batch_recognize.py

出力先:
    tests/fixtures/colored_factors/recognition_results.json

備考:
- OpenCV `cv2.imread()` は Windows で日本語を含む **絶対パス** を扱えないため、
  本スクリプトでは CWD をプロジェクトルートに固定したうえで **相対パス** を渡す。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Windows + 日本語パス対策: 必ず ROOT を CWD にして相対パスで imread させる
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))

from umafactor.pipeline import analyze_image  # noqa: E402

FIXTURES_DIR = Path("tests") / "fixtures"
OUTPUT_PATH = FIXTURES_DIR / "colored_factors" / "recognition_results.json"


def _collect_images() -> list[Path]:
    """対象画像を相対パスのまま返す。

    - receipt_* / combine_* / sample_* / umamusume_* / image0_*: 既存学習用
    - new_*: E プラン（過学習評価）用に追加した新規画像（テンプレ訓練に含む）
    - unseen_*: 中期 Day 1 後の汎化検証用「初見」画像（テンプレ訓練に含めない）
    """
    patterns = [
        "receipt_*.png",
        "combine_*.png",
        "sample_*.png",
        "umamusume_*.png",
        "image0_*.png",
        "new_*.png",
        "unseen_*.png",
    ]
    paths: list[Path] = []
    for pat in patterns:
        paths.extend(FIXTURES_DIR.glob(pat))
    return sorted(paths, key=lambda p: p.name)


def main() -> int:
    images = _collect_images()
    print(f"対象画像 {len(images)} 枚を解析します（CWD={ROOT}）")
    if not images:
        print("fixtures 配下に対象画像がありません", file=sys.stderr)
        return 1

    results: dict[str, dict] = {}
    t0 = time.time()
    for i, img_path in enumerate(images, 1):
        tt0 = time.time()
        try:
            submission, _review = analyze_image(
                image_path=str(img_path),  # 相対パスを渡す
                submitter_id="batch_recognize",
            )
            results[img_path.name] = submission.to_json_dict()
            elapsed = time.time() - tt0
            print(f"  [{i}/{len(images)}] {img_path.name}  ok ({elapsed:.1f}s)")
        except Exception as e:  # noqa: BLE001
            results[img_path.name] = {"error": str(e)}
            print(f"  [{i}/{len(images)}] {img_path.name}  FAILED: {e}", file=sys.stderr)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n合計 {time.time() - t0:.1f}s")
    print(f"出力: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
