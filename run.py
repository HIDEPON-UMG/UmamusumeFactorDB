"""CLI エントリポイント。

使い方:
    python run.py <image_path> --submitter <ID>
    python run.py <image_path> --submitter <ID> --dry-run
    python run.py <image_path> --submitter <ID> --debug-crops ./crops
    python run.py <image_path> --submitter <ID> --tab factors_raw_test
    python run.py <image_path> --submitter <ID> --review    # 自信度の低い因子を人間レビュー
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from umafactor.pipeline import analyze_image, apply_review_results  # noqa: E402
from umafactor.schema import SHEET_TAB_NAME  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="ウマ娘 継承因子画像 → スプレ書き込み")
    parser.add_argument("image_path", help="解析対象の画像ファイル（PNG/JPEG）")
    parser.add_argument("--submitter", required=True, help="投稿者 ID")
    parser.add_argument("--dry-run", action="store_true", help="スプレに書かず JSON を標準出力")
    parser.add_argument("--debug-crops", default=None, help="切り出し画像を保存するディレクトリ")
    parser.add_argument("--tab", default=SHEET_TAB_NAME, help=f"書き込み先タブ名（既定: {SHEET_TAB_NAME}）")
    parser.add_argument(
        "--review",
        action="store_true",
        help="自信度の低い因子（赤<0.95 / 白<0.7 / 青<0.95）をポップアップでレビュー",
    )
    parser.add_argument("--review-all", action="store_true", help="全因子をレビューする（デバッグ用）")
    args = parser.parse_args()

    submission, review_queue = analyze_image(
        image_path=args.image_path,
        submitter_id=args.submitter,
        debug_crops_dir=args.debug_crops,
    )

    if args.review or args.review_all:
        from umafactor.review_ui import review_queue_interactive

        queue = review_queue if args.review_all else review_queue.filter_uncertain()
        print(f"レビュー対象: {len(queue.items)} 件（自信度が低いもの）")
        if queue.items:
            review_queue_interactive(queue)
            apply_review_results(submission, queue)

    if args.dry_run:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(submission.to_json_dict(), ensure_ascii=False, indent=2))
        return 0

    from umafactor.sheet_writer import append_submission

    result = append_submission(submission, tab_name=args.tab)
    print(f"書き込み完了: submission_id={submission.submission_id}, 応答={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
