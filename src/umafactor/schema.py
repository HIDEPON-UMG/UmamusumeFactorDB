"""スプレッドシート書き込み用の行スキーマ定義。

1 つの投稿（Submission）は **3 行** に分解してスプレッドシートに記録する：
- main   : 本人の 1 行
- parent1: 継承元1 の 1 行
- parent2: 継承元2 の 1 行

3 行は共通の `submission_id` で紐付く。

各行は以下の列を持つ（1 セル 1 値）：
- 共通メタデータ（submission_id / submitted_at / submitter_id / image_filename / role / character）
- 青因子（blue_type / blue_star）
- 赤因子（red_type / red_star）
- 緑因子（green_name / green_star）
- 白因子スロット factor_01..factor_60（name / star の 2 セル × 60 = 120 セル）

白因子が 60 を超えた場合は先頭 60 件で切り捨て、空スロットは空文字。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


SHEET_TAB_NAME = "factors_raw"

MAX_FACTOR_SLOTS = 60


def _build_columns() -> list[str]:
    cols = [
        "submission_id",
        "submitted_at",
        "submitter_id",
        "image_filename",
        "role",
        "character",
        "blue_type",
        "blue_star",
        "red_type",
        "red_star",
        "green_name",
        "green_star",
    ]
    for i in range(1, MAX_FACTOR_SLOTS + 1):
        cols.append(f"factor_{i:02d}_name")
        cols.append(f"factor_{i:02d}_star")
    return cols


COLUMNS: list[str] = _build_columns()


@dataclass
class FactorEntry:
    """白因子（スキル因子）の 1 エントリ。"""

    color: str  # "white"（将来 blue/red/green 混在時のために保持）
    name: str
    star: int


@dataclass
class UmaFactors:
    """1 体分の因子群。"""

    character: str = ""
    blue_type: str = ""
    blue_star: int = 0
    red_type: str = ""
    red_star: int = 0
    green_name: str = ""
    green_star: int = 0
    skills: list[FactorEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "character": self.character,
            "blue": {"type": self.blue_type, "star": self.blue_star},
            "red": {"type": self.red_type, "star": self.red_star},
            "green": {"name": self.green_name, "star": self.green_star},
            "skills": [{"name": s.name, "star": s.star} for s in self.skills],
        }


@dataclass
class Submission:
    """1 枚の画像解析結果（3 体分をまとめて持つ）。"""

    submitter_id: str
    image_filename: str
    main: UmaFactors = field(default_factory=UmaFactors)
    parent1: UmaFactors = field(default_factory=UmaFactors)
    parent2: UmaFactors = field(default_factory=UmaFactors)
    submission_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_rows(self) -> list[list[str]]:
        """3 行分のデータを COLUMNS 順で返す（role: main, parent1, parent2 の順）。"""
        timestamp = self.submitted_at.isoformat()
        return [
            self._build_row("main", self.main, timestamp),
            self._build_row("parent1", self.parent1, timestamp),
            self._build_row("parent2", self.parent2, timestamp),
        ]

    def _build_row(self, role: str, uma: UmaFactors, timestamp: str) -> list[str]:
        row = [
            self.submission_id,
            timestamp,
            self.submitter_id,
            self.image_filename,
            role,
            uma.character,
            uma.blue_type,
            str(uma.blue_star),
            uma.red_type,
            str(uma.red_star),
            uma.green_name,
            str(uma.green_star),
        ]
        for i in range(MAX_FACTOR_SLOTS):
            if i < len(uma.skills):
                s = uma.skills[i]
                row.append(s.name)
                row.append(str(s.star))
            else:
                row.append("")
                row.append("")
        return row

    def to_json_dict(self) -> dict[str, Any]:
        """--dry-run 用の構造化出力。"""
        return {
            "submission_id": self.submission_id,
            "submitted_at": self.submitted_at.isoformat(),
            "submitter_id": self.submitter_id,
            "image_filename": self.image_filename,
            "main": self.main.to_dict(),
            "parent1": self.parent1.to_dict(),
            "parent2": self.parent2.to_dict(),
        }
