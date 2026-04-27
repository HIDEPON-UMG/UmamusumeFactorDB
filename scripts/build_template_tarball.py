"""Cloud Run デプロイ前に datasets/all_templates.tar.gz を再生成する。

背景:
  Cloud Run の container import は image レイヤー内の Unicode 名
  （日本語ディレクトリ・☆等の全角記号）を拒否し ContainerImageImportFailed
  で失敗する。そのため Dockerfile では Unicode 名を直接 COPY せず、ASCII 名の
  tar.gz だけを焼き込み、起動時に CMD ラッパーで展開している。

  本スクリプトはローカルの datasets/{red_blue,star,green_name}_templates から
  単一の datasets/all_templates.tar.gz を再生成する。`gcloud run deploy
  --source .` を実行する直前に毎回呼び出すこと。

使い方:
  .venv/Scripts/python.exe scripts/build_template_tarball.py
"""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ROOT / "datasets"
OUTPUT = DATASETS / "all_templates.tar.gz"
DIRS = ("red_blue_templates", "star_templates", "green_name_templates")


def main() -> int:
    missing = [d for d in DIRS if not (DATASETS / d).exists()]
    if missing:
        print(f"ERROR: missing source dirs: {missing}", file=sys.stderr)
        return 1

    with tarfile.open(OUTPUT, "w:gz") as tar:
        for d in DIRS:
            src = DATASETS / d
            tar.add(src, arcname=d)

    size_kb = OUTPUT.stat().st_size / 1024
    print(f"rebuilt: {OUTPUT} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
