"""推論結果の自信度が低い因子を人間にレビューさせるための仕組み。

ReviewItem: レビュー対象 1 件（因子 1 つ）。切り抜き画像と候補リストを持つ。
ReviewQueue: Submission に紐付く ReviewItem のリスト。

tkinter ベースの GUI は review_ui.py 側に分離。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np


SlotKind = Literal["blue", "red", "green", "white"]


@dataclass
class ReviewItem:
    uma_index: int  # 0=main, 1=parent1, 2=parent2
    uma_role: str  # "main" / "parent1" / "parent2"
    slot: SlotKind  # "blue" / "red" / "green" / "white"
    white_index: int  # 白因子の並び順（0..）、slot != "white" なら 0 で無視
    image: np.ndarray  # 因子ボックスの BGR 画像（元解像度、拡大なし）
    candidates: list[tuple[str, float]]  # (名前, 確信度) の上位 K 件（マージ後）
    current_name: str
    current_star: int
    # 候補ごとの出所フラグ: "onnx" / "ocr" / "both"
    candidate_sources: dict[str, str] | None = None
    # EasyOCR の生テキスト（前処理・正規化後、辞書マッチ前）
    ocr_raw: str = ""
    # レビュー結果（ユーザ操作後にセット）
    reviewed_name: str | None = None
    reviewed_star: int | None = None


@dataclass
class ReviewQueue:
    items: list[ReviewItem] = field(default_factory=list)

    def add(self, item: ReviewItem) -> None:
        self.items.append(item)

    def filter_uncertain(
        self,
        red_gap_threshold: float = 0.1,
        white_threshold: float = 0.85,
        blue_threshold: float = 0.95,
    ) -> "ReviewQueue":
        """指定の信頼度を下回るものだけ残した新しいキューを返す。

        赤因子は max aggregation のため top-1 も 1.0 近くになるが、短/中/長距離は
        全部 1.0 近くになりがち。そこで **top-1 と top-2 の差** でレビュー要否を判断する。

        - red: top1 - top2 < red_gap_threshold ならレビュー対象（候補が拮抗）
        - white: 0.7 未満（モデルが揺れる領域）
        - blue: 0.95 未満（基本は高確信だが念のため）
        - green: レビュー対象外
        """
        out = ReviewQueue()
        for it in self.items:
            cand = it.candidates
            if not cand:
                continue
            top1 = cand[0][1]
            top2 = cand[1][1] if len(cand) >= 2 else 0.0
            if it.slot == "red":
                if top1 - top2 < red_gap_threshold:
                    out.add(it)
            elif it.slot == "white":
                if top1 < white_threshold:
                    out.add(it)
            elif it.slot == "blue":
                if top1 < blue_threshold:
                    out.add(it)
            # green はレビュー対象外
        return out
