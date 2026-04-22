"""EasyOCR による日本語 OCR ラッパ。

ONNX 因子モデル（umacapture）が系統的に誤認識する因子に対して、
EasyOCR で生テキストを抽出 → rapidfuzz で因子辞書 813 件に最近傍マッチ、
という 2 段構えで候補を補完する。

初期化コスト（モデル DL/ロード）が大きいため、FactorOCR はシングルトンで使う。
"""

from __future__ import annotations

import re
from functools import lru_cache

import cv2
import numpy as np
from rapidfuzz import fuzz, process as fuzz_process

from .config import green_factor_names, load_labels


# Reader 初期化が重いのでグローバルキャッシュ
_READER = None


def _get_reader():
    global _READER
    if _READER is None:
        import os

        import easyocr

        # Cloud Run 等で事前 DL したモデル置き場を尊重
        model_dir = os.environ.get("EASYOCR_MODULE_PATH")
        kwargs = {"gpu": False, "verbose": False}
        if model_dir:
            kwargs["model_storage_directory"] = model_dir
            kwargs["download_enabled"] = False
        # 緑因子（固有スキル）には英字混じりが多いため en も併用
        _READER = easyocr.Reader(["ja", "en"], **kwargs)
    return _READER


def _preprocess_for_ocr(bgr: np.ndarray, upscale: int = 3) -> np.ndarray:
    """OCR 前処理：拡大してコントラスト強化（Uma の小さいテキスト向け）。"""
    if bgr.size == 0:
        return bgr
    # cv2.INTER_CUBIC で拡大
    big = cv2.resize(
        bgr, (bgr.shape[1] * upscale, bgr.shape[0] * upscale), interpolation=cv2.INTER_CUBIC
    )
    return big


# EasyOCR が誤認識しやすい文字のゆるい置換（因子名辞書の表記に近づける）
_OCR_NORMALIZE = [
    ("0", "○"),  # 因子名の「○」は頻出。EasyOCR は 0 と読むことが多い
    ("◯", "○"),  # 別字形
    ("Ｏ", "○"),
]


def _normalize_ocr_text(raw: str) -> str:
    for src, dst in _OCR_NORMALIZE:
        raw = raw.replace(src, dst)
    # 連結スペースやタブを削除
    raw = re.sub(r"\s+", "", raw)
    return raw


class FactorOCR:
    """因子画像 → 因子辞書候補への変換器。"""

    def __init__(self) -> None:
        # 辞書をキャッシュ
        self._factor_names: list[str] = list(load_labels()["factor.name"])
        # 緑因子（固有スキル）専用の辞書（249 件）— ファジーマッチの範囲を絞って精度を上げる
        self._green_factor_names: list[str] = green_factor_names()

    def recognize(self, img_bgr: np.ndarray) -> str:
        """画像から生テキストを抽出。複数領域の結合＋記号正規化を行う。"""
        if img_bgr is None or img_bgr.size == 0:
            return ""
        reader = _get_reader()
        big = _preprocess_for_ocr(img_bgr, upscale=3)
        parts = reader.readtext(big, detail=0)
        if not parts:
            return ""
        raw = "".join(parts)
        return _normalize_ocr_text(raw)

    def recognize_with_parts(self, img_bgr: np.ndarray) -> tuple[str, list[str]]:
        """画像から生テキストを抽出し、(連結テキスト, 断片リスト) を返す。

        緑因子（固有スキル）用：1 つの長いスキル名が OCR で複数の断片に分かれる
        ことが多く、連結しか使わないと「長い辞書エントリ」にアンカー寄せされる。
        断片ごとに独立したクエリを作り、辞書マッチの多様性を高める。
        """
        if img_bgr is None or img_bgr.size == 0:
            return "", []
        reader = _get_reader()
        big = _preprocess_for_ocr(img_bgr, upscale=3)
        parts = reader.readtext(big, detail=0)
        if not parts:
            return "", []
        combined = _normalize_ocr_text("".join(parts))
        fragments = [_normalize_ocr_text(p) for p in parts]
        fragments = [f for f in fragments if f]
        return combined, fragments

    def recognize_red(self, img_bgr: np.ndarray) -> str:
        """赤因子（距離/脚質/バ場）専用の OCR。候補文字を allowlist で絞って
        「2」「]」等のゴミ出力を排除する。候補文字集合は RED_FACTOR_TYPES に
        含まれる「短中長距離芝ダート逃先差追込マイル」の総計 12 文字。
        """
        if img_bgr is None or img_bgr.size == 0:
            return ""
        reader = _get_reader()
        big = _preprocess_for_ocr(img_bgr, upscale=3)
        # RED_FACTOR_TYPES = ["逃げ","先行","差し","追込","短距離","マイル","中距離","長距離","芝","ダート"]
        # 構成文字をユニーク化
        allowlist = "短中長距離芝ダートマイル逃げ先行差し追込"
        parts = reader.readtext(big, detail=0, allowlist=allowlist)
        if not parts:
            return ""
        raw = "".join(parts)
        return _normalize_ocr_text(raw)

    def recognize_blue(self, img_bgr: np.ndarray) -> str:
        """青因子（ステータス5種）専用の OCR。allowlist で絞ってゴミ文字を抑制。
        BLUE_FACTOR_TYPES = ["スピード","スタミナ","パワー","根性","賢さ"] の
        構成文字をユニーク化して候補にする。
        """
        if img_bgr is None or img_bgr.size == 0:
            return ""
        reader = _get_reader()
        big = _preprocess_for_ocr(img_bgr, upscale=3)
        # ユニーク化: スピードタミナパワー根性賢さ
        allowlist = "スピードタミナパワー根性賢さ"
        parts = reader.readtext(big, detail=0, allowlist=allowlist)
        if not parts:
            return ""
        raw = "".join(parts)
        return _normalize_ocr_text(raw)

    def match_to_factor(
        self, raw_text: str, top_k: int = 5, min_score: float = 50.0
    ) -> list[tuple[str, float]]:
        """OCR 生テキストを 813 件因子辞書にファジーマッチ。

        戻り値: [(因子名, スコア 0.0-1.0)] を確信度降順で上位 top_k 件。
        min_score 未満のマッチは除外。
        """
        if not raw_text:
            return []
        # rapidfuzz の partial_ratio を使うと「地固」から「地固め」を確実に拾える
        hits = fuzz_process.extract(
            raw_text,
            self._factor_names,
            scorer=fuzz.partial_ratio,
            limit=top_k * 3,  # 後段で ratio を使って絞るので多めに取る
        )
        # partial_ratio で絞った後、ratio で精度を算出し並び替え
        scored: list[tuple[str, float]] = []
        for name, pscore, _i in hits:
            if pscore < min_score:
                continue
            # 部分一致＋全体類似度のブレンドを最終スコアに
            rscore = fuzz.ratio(raw_text, name)
            final = (pscore * 0.6 + rscore * 0.4) / 100.0  # 0..1 に正規化
            scored.append((name, final))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def match_to_green_factor(
        self, raw_text: str, top_k: int = 5, min_score: float = 40.0
    ) -> list[tuple[str, float]]:
        """OCR 生テキストを 249 件の緑因子（固有スキル）辞書にファジーマッチ。

        緑因子は英字混じりが多く全 813 辞書では誤マッチしやすいため、
        緑因子専用の辞書で別途引き直す。
        重みは ratio 寄り（0.4/0.6）にして、部分一致だけで特定の長いスキル名に
        アンカー寄せされる傾向を抑制する。
        """
        if not raw_text:
            return []
        hits = fuzz_process.extract(
            raw_text,
            self._green_factor_names,
            scorer=fuzz.partial_ratio,
            limit=top_k * 3,
        )
        scored: list[tuple[str, float]] = []
        for name, pscore, _i in hits:
            if pscore < min_score:
                continue
            rscore = fuzz.ratio(raw_text, name)
            # ratio 寄りの重み（0.4/0.6）: 全体の一致度を優先
            final = (pscore * 0.4 + rscore * 0.6) / 100.0
            scored.append((name, final))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def match_to_green_factor_multi(
        self,
        combined: str,
        fragments: list[str],
        top_k: int = 5,
        min_score_combined: float = 40.0,
        min_score_fragment: float = 60.0,
        fragment_weight: float = 0.9,
    ) -> list[tuple[str, float]]:
        """連結テキスト＋断片テキスト複数で緑因子辞書に並列マッチし、統合する。

        - combined: 全テキスト連結。従来の match_to_green_factor 相当のクエリ
        - fragments: readtext の断片（1 スキル名が複数断片に分かれることが多い）
        - min_score_fragment を高めに設定して、断片経路の誤マッチを抑える
        - fragment_weight (<1.0) で断片由来スコアを軽く割り引き、連結一致を優先
        - 断片経路は **文字数補正** を適用: 短い断片から長い辞書エントリへの
          partial_ratio 高スコアを len 比で減点し、"Joy" が "Joyful Voyage!" に
          短文マッチして正解 "Joy to the World" を逆転するような事故を防ぐ
        - 同一候補は max スコア採用（断片と連結で別クエリになるため）
        """
        candidates: dict[str, float] = {}
        # 連結クエリ（連結テキストは元々長さを持つので文字数補正なし）
        for name, score in self.match_to_green_factor(
            combined, top_k=top_k * 2, min_score=min_score_combined
        ):
            prev = candidates.get(name, 0.0)
            if score > prev:
                candidates[name] = score
        # 断片クエリ（短すぎる断片はスキップ、min_score を高めに）
        for frag in fragments:
            if len(frag) < 3:
                continue
            for name, score in self.match_to_green_factor(
                frag, top_k=top_k, min_score=min_score_fragment
            ):
                # 文字数補正: 断片長 / 候補名長 の比でスコアを減点
                # frag="Joy"(3), name="Joyful Voyage!"(13) → 0.23 倍
                # frag="Joy"(3), name="Joy to the World"(16) → 0.19 倍
                # 連結経路で「Joy to the World」が top1 なら断片経路の減点後スコアでは
                # 逆転されにくくなる
                len_ratio = min(1.0, len(frag) / max(1, len(name)))
                weighted = score * fragment_weight * len_ratio
                prev = candidates.get(name, 0.0)
                if weighted > prev:
                    candidates[name] = weighted
        merged = sorted(candidates.items(), key=lambda x: -x[1])
        return merged[:top_k]


@lru_cache(maxsize=1)
def get_ocr() -> FactorOCR:
    return FactorOCR()
