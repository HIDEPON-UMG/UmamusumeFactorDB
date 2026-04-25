"""analyze_image を debug_crops_dir 付きで呼び、overlay を出力するだけの簡易スクリプト。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))

from umafactor.pipeline import analyze_image


def main(image_rel: str, out_dir: str = "debug_overlay") -> int:
    analyze_image(image_path=image_rel, submitter_id="diag", debug_crops_dir=out_dir)
    print(f"OK → {out_dir}/_overlay.png etc.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(
        sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/umamusume_20260424_180452_warn.png",
        sys.argv[2] if len(sys.argv) > 2 else "debug_overlay",
    ))
