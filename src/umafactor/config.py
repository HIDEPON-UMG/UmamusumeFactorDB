"""recognizer.json と labels.json を読み込み、推論に必要な定数を提供する。

umacapture 本体は Flutter/C++ で複雑なアンカー解決（IntersectStart 等）を行うが、
本 MVP では「因子タブ」に限定し、以下の単純化を適用する：

- 画像は縦スクロール結合済みで、幅が基準解像度 540 px に比例（実測 1.0〜1.2 倍）
- 3 体分のウマ娘セクションは、左側のウマ娘ポートレート位置で決まるので
  empirically に検出する（cropper.py 側）
- 因子ボックスの x 範囲（左右 2 列）と vertical_delta（行送り）のみ recognizer.json から流用
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
MODELS_DIR = PROJECT_ROOT / "models" / "modules"


@dataclass(frozen=True)
class FactorLayout:
    """因子タブの正規化座標（recognizer.json factor_tab から抜粋）。

    座標は基準解像度 540×960 に対する比率。実画像では幅方向にスケールする。
    """

    # 因子ボックス左列 (x 比率)
    left_x0: float = 0.2426
    left_x1: float = 0.5519
    # 因子ボックス右列 (x 比率)
    right_x0: float = 0.6259
    right_x1: float = 0.9352
    # 1 ボックスの高さ（screen_height 比）
    box_height_rel: float = 0.0278
    # 因子行と行の間隔（screen_height 比）
    vertical_delta_rel: float = 0.0741
    # ウマ娘セクション間の間隔（screen_height 比）
    chara_gap_rel: float = 0.0593
    # 因子ボックス内での ★ランク領域（ボックス内の比率 W=168,H=16 基準）
    rank_x0_in_box_rel: float = 0.6786  # 114/168
    rank_x1_in_box_rel: float = 1.0

    @property
    def box_width_rel_left(self) -> float:
        return self.left_x1 - self.left_x0

    @property
    def box_width_rel_right(self) -> float:
        return self.right_x1 - self.right_x0


# ONNX モデルの入力サイズ（uint8 NHWC）: (H, W)
MODEL_INPUT_SIZES = {
    "factor": (16, 168),
    "factor_rank": (16, 52),
    "character": (32, 32),
    "aptitude": (16, 16),
}


def load_labels() -> dict[str, list[str]]:
    """modules/labels.json を読み込み、ラベルキーごとの配列を返す。"""
    path = MODELS_DIR / "labels.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_factor_info() -> list[dict]:
    """modules/factor_info.json を読み込む（sid/gid/names/tags 等を含む因子辞書）。"""
    path = MODELS_DIR / "factor_info.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_unique_skill_to_character() -> dict[str, str]:
    """config/unique_skill_to_character.json を読み込む。

    {固有スキル名: "[衣装名]キャラ名"} の辞書。存在しなければ空 dict。
    """
    path = CONFIG_DIR / "unique_skill_to_character.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def green_factor_names() -> list[str]:
    """factor_info.json から緑因子（固有スキル因子）のみの名前リストを返す。

    tags に 'factor_unique_skill' を含むものを採用。
    """
    info = load_factor_info()
    return [f["names"][0] for f in info if "factor_unique_skill" in f.get("tags", [])]


def model_path(name: str) -> Path:
    """指定モデル名の .onnx パス（例: 'factor' → models/modules/factor/prediction.onnx）。"""
    return MODELS_DIR / name / "prediction.onnx"


# 因子カラーチップの HSV しきい値（OpenCV は H=0..180, S=0..255, V=0..255）
# 実測で調整する想定。初期値は色相で大まかに判別。
FACTOR_COLOR_HSV_RANGES = {
    "blue": [(95, 80, 80), (130, 255, 255)],
    # 赤因子チップは magenta-pink で H≈167。
    # H=0-15 帯は白因子アイコンの淡いピンク縁にヒットして誤判定を招くので使わない。
    "red": [(160, 90, 90), (180, 255, 255)],
    "green": [(35, 80, 80), (85, 255, 255)],
    # 白因子は背景とほぼ同色（淡い灰色系）、最後のデフォルト扱い
}
