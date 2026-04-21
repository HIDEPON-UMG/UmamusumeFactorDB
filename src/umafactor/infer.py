"""ONNX モデルによる因子名・★ランク・ウマ娘名の推論ラッパ。

factor モデルは softmax 確率（onnx::ReduceMax_639）も取り出せるよう、
onnx.load で内部ノードを追加出力として登録した派生モデルを使う。これにより
「青因子/赤因子/緑因子のカテゴリに制限した argmax」が可能になる。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort

from .config import MODEL_INPUT_SIZES, load_labels, model_path


@dataclass
class Prediction:
    index: int
    label: str
    confidence: float


# factor モデルの softmax 出力テンソル名（onnx.load でグラフを調べて特定）
FACTOR_SOFTMAX_NAME = "onnx::ReduceMax_639"
FACTOR_WITH_PROBS_FILENAME = "prediction_with_probs.onnx"


def _ensure_factor_with_probs(src_path: Path) -> Path:
    """factor モデルに softmax 確率出力を追加した派生モデルを作成（未作成時のみ）。"""
    derived = src_path.parent / FACTOR_WITH_PROBS_FILENAME
    if derived.exists():
        return derived
    m = onnx.load(str(src_path))
    # 既存の出力に softmax ノードを追加
    probs_vi = onnx.helper.make_tensor_value_info(
        FACTOR_SOFTMAX_NAME, onnx.TensorProto.FLOAT, ["batch", 820]
    )
    m.graph.output.extend([probs_vi])
    onnx.save(m, str(derived))
    return derived


class OnnxPredictor:
    def __init__(
        self,
        model_name: str,
        label_key: str,
        index_output: str = "index",
        confidence_output: str = "confidence",
        extra_outputs: tuple[str, ...] = (),
    ) -> None:
        self.model_name = model_name
        path = model_path(model_name)
        if model_name == "factor":
            path = _ensure_factor_with_probs(path)
        self.session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.index_output = index_output
        self.confidence_output = confidence_output
        self.extra_outputs = extra_outputs
        self.labels = load_labels()[label_key]
        self.expected_hw = MODEL_INPUT_SIZES[model_name]

    def _preprocess(self, img_hwc_bgr: np.ndarray) -> np.ndarray:
        import cv2

        eh, ew = self.expected_hw
        if img_hwc_bgr.shape[:2] != (eh, ew):
            img_hwc_bgr = cv2.resize(img_hwc_bgr, (ew, eh), interpolation=cv2.INTER_LINEAR)
        return img_hwc_bgr.astype(np.uint8)[None, ...]

    def predict(self, img_hwc_bgr: np.ndarray) -> Prediction:
        batch = self._preprocess(img_hwc_bgr)
        outputs = self.session.run(
            [self.index_output, self.confidence_output], {self.input_name: batch}
        )
        idx = int(outputs[0][0])
        conf = float(outputs[1][0])
        label = self.labels[idx] if 0 <= idx < len(self.labels) else f"<out_of_range:{idx}>"
        return Prediction(index=idx, label=label, confidence=conf)

    def predict_with_perturbation(
        self,
        img_hwc_bgr: np.ndarray,
        perturbations: list[tuple[int, int]],
    ) -> Prediction:
        """(dy, dx) シフト画像群で推論し、confidence 加算投票でラベル決定する。

        softmax 確率出力を持たないモデル（factor_rank など）向けの軽量アンサンブル。
        入力を expected_hw にリサイズしてから warpAffine (BORDER_REPLICATE) でシフト。
        同じラベルを出した予測の confidence を加算し、合計が最大のラベルを採用する。
        """
        import cv2

        if not perturbations:
            return self.predict(img_hwc_bgr)

        eh, ew = self.expected_hw
        h, w = img_hwc_bgr.shape[:2]
        if (h, w) != (eh, ew):
            base = cv2.resize(img_hwc_bgr, (ew, eh), interpolation=cv2.INTER_LINEAR)
        else:
            base = img_hwc_bgr

        label_score: dict[int, float] = {}
        label_count: dict[int, int] = {}
        for dy, dx in perturbations:
            if dy == 0 and dx == 0:
                shifted = base
            else:
                affine = np.float32([[1, 0, dx], [0, 1, dy]])
                shifted = cv2.warpAffine(
                    base,
                    affine,
                    (ew, eh),
                    flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE,
                )
            batch = shifted.astype(np.uint8)[None, ...]
            outs = self.session.run(
                [self.index_output, self.confidence_output],
                {self.input_name: batch},
            )
            idx = int(outs[0][0])
            conf = float(outs[1][0])
            label_score[idx] = label_score.get(idx, 0.0) + conf
            label_count[idx] = label_count.get(idx, 0) + 1

        best_idx = max(label_score.keys(), key=lambda k: label_score[k])
        avg_conf = label_score[best_idx] / label_count[best_idx]
        label = (
            self.labels[best_idx] if 0 <= best_idx < len(self.labels) else f"<oor:{best_idx}>"
        )
        return Prediction(index=best_idx, label=label, confidence=avg_conf)

    def predict_probs(self, img_hwc_bgr: np.ndarray) -> np.ndarray:
        """softmax 確率（全クラス）を返す。extra_outputs を使える predictor のみ。"""
        if not self.extra_outputs:
            raise RuntimeError(f"{self.model_name} は probs 出力を持ちません")
        batch = self._preprocess(img_hwc_bgr)
        outputs = self.session.run(list(self.extra_outputs), {self.input_name: batch})
        return outputs[0][0]  # shape (num_classes,)

    def predict_in_category(
        self, img_hwc_bgr: np.ndarray, allowed_labels: list[str]
    ) -> Prediction:
        """softmax 確率を使い、指定ラベルのうち最も確率が高いものを返す。"""
        probs = self.predict_probs(img_hwc_bgr)
        allowed_idxs = [self.labels.index(lb) for lb in allowed_labels]
        sub_probs = probs[allowed_idxs]
        best_in = int(np.argmax(sub_probs))
        global_idx = allowed_idxs[best_in]
        return Prediction(
            index=global_idx,
            label=allowed_labels[best_in],
            confidence=float(sub_probs[best_in]),
        )

    def predict_in_category_best_of(
        self, img_list: list[np.ndarray], allowed_labels: list[str]
    ) -> Prediction:
        """複数クロップを試し、カテゴリ内の最高 confidence を返す。

        用途：同じ因子を 540 正規化版と元解像度版でクロップし、片方でしか
        まともな信号が出ない場合でも良いほうを採用する（平均では片方の誤認識に
        引きずられるため、max-picking を採用）。"""
        if not img_list:
            raise ValueError("img_list is empty")
        allowed_idxs = [self.labels.index(lb) for lb in allowed_labels]
        best: Prediction | None = None
        for img in img_list:
            probs = self.predict_probs(img)
            sub = probs[allowed_idxs]
            bi = int(np.argmax(sub))
            conf = float(sub[bi])
            if best is None or conf > best.confidence:
                best = Prediction(
                    index=allowed_idxs[bi],
                    label=allowed_labels[bi],
                    confidence=conf,
                )
        assert best is not None
        return best

    def predict_in_category_multi_interp(
        self,
        img_list: list[np.ndarray],
        allowed_labels: list[str],
        interps: tuple[int, ...] = (1, 3),  # INTER_LINEAR=1, INTER_AREA=3
    ) -> Prediction:
        """複数クロップ × 複数補間方法の全組合せで推論し、カテゴリ内の最高 conf。

        短/中/長距離など crop 位置と補間方法の両方に敏感なケース向け。
        内部で self.expected_hw にリサイズしてから推論するため、リサイズ前の
        画像を渡す前提。
        """
        import cv2

        if not img_list:
            raise ValueError("img_list is empty")
        allowed_idxs = [self.labels.index(lb) for lb in allowed_labels]
        eh, ew = self.expected_hw
        best: Prediction | None = None
        for img in img_list:
            for interp in interps:
                resized = cv2.resize(img, (ew, eh), interpolation=interp)
                batch = resized.astype(np.uint8)[None, ...]
                outs = self.session.run(list(self.extra_outputs), {self.input_name: batch})
                probs = outs[0][0]
                sub = probs[allowed_idxs]
                bi = int(np.argmax(sub))
                conf = float(sub[bi])
                if best is None or conf > best.confidence:
                    best = Prediction(
                        index=allowed_idxs[bi],
                        label=allowed_labels[bi],
                        confidence=conf,
                    )
        assert best is not None
        return best

    def predict_ensemble(self, img_list: list[np.ndarray]) -> Prediction:
        """カテゴリ制約なしの softmax 平均アンサンブル（白因子向け）。"""
        if not img_list:
            raise ValueError("img_list is empty")
        probs_sum = np.zeros_like(self.predict_probs(img_list[0]))
        for img in img_list:
            probs_sum += self.predict_probs(img)
        probs_avg = probs_sum / len(img_list)
        best = int(np.argmax(probs_avg))
        return Prediction(
            index=best,
            label=self.labels[best] if 0 <= best < len(self.labels) else f"<oor:{best}>",
            confidence=float(probs_avg[best]),
        )

    def topk_ensemble(
        self, img_list: list[np.ndarray], k: int = 8
    ) -> list[tuple[str, float]]:
        """平均 softmax で上位 k 件の (label, conf) を返す（レビュー候補用）。"""
        if not img_list:
            raise ValueError("img_list is empty")
        probs_sum = np.zeros_like(self.predict_probs(img_list[0]))
        for img in img_list:
            probs_sum += self.predict_probs(img)
        probs_avg = probs_sum / len(img_list)
        top_idx = np.argsort(-probs_avg)[:k]
        return [(self.labels[i], float(probs_avg[i])) for i in top_idx if 0 <= i < len(self.labels)]

    def topk_in_category(
        self,
        img_list: list[np.ndarray],
        allowed_labels: list[str],
        k: int = 8,
        use_multi_interp: bool = False,
        interps: tuple[int, ...] = (1, 3),
    ) -> list[tuple[str, float]]:
        """カテゴリ内の上位 k 件を返す。赤/青因子のレビュー候補用。

        use_multi_interp=True の場合は各画像 × 補間方法の組合せで確率を集計（max 採用）。
        """
        import cv2

        if not img_list:
            raise ValueError("img_list is empty")
        allowed_idxs = [self.labels.index(lb) for lb in allowed_labels]
        if not use_multi_interp:
            probs_sum = np.zeros_like(self.predict_probs(img_list[0]))
            for img in img_list:
                probs_sum += self.predict_probs(img)
            probs_avg = probs_sum / len(img_list)
            sub = probs_avg[allowed_idxs]
        else:
            eh, ew = self.expected_hw
            # 各クラスについて全組合せ中の最大 prob を集計
            sub = np.zeros(len(allowed_labels))
            for img in img_list:
                for interp in interps:
                    resized = cv2.resize(img, (ew, eh), interpolation=interp)
                    batch = resized.astype(np.uint8)[None, ...]
                    outs = self.session.run(list(self.extra_outputs), {self.input_name: batch})
                    probs = outs[0][0]
                    sub_i = probs[allowed_idxs]
                    sub = np.maximum(sub, sub_i)
        top_idx = np.argsort(-sub)[:k]
        return [(allowed_labels[i], float(sub[i])) for i in top_idx]


@lru_cache(maxsize=None)
def get_predictor(model_name: str) -> OnnxPredictor:
    if model_name == "factor":
        return OnnxPredictor(
            "factor",
            "factor.name",
            extra_outputs=(FACTOR_SOFTMAX_NAME,),
        )
    if model_name == "factor_rank":
        return OnnxPredictor("factor_rank", "factor_rank.name")
    if model_name == "aptitude":
        return OnnxPredictor("aptitude", "aptitude.name")
    if model_name == "character":
        return OnnxPredictor(
            "character",
            "character.card",
            index_output="card_index",
            confidence_output="card_confidence",
        )
    raise KeyError(f"Unknown model: {model_name}")


# ---------------------------------------------------------------------------
# ★スロット分類器（CNN 2 クラス: empty / gold）
# ---------------------------------------------------------------------------
# 既存 OnnxPredictor は labels.json を参照するため、labels.json を編集しない
# 軽量モデルは独立経路で扱う。入力は NHWC uint8 BGR (N, 28, 28, 3)、
# 出力は index (int64) と confidence (float) で共通化してある。

STAR_CLASS_NAMES = ["empty", "gold"]
STAR_SLOT_SIZE = 28


@lru_cache(maxsize=1)
def _get_star_session() -> ort.InferenceSession:
    path = model_path("star_classifier")
    return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])


def _prep_star_slot(img_bgr: np.ndarray) -> np.ndarray:
    import cv2

    h, w = img_bgr.shape[:2]
    if (h, w) != (STAR_SLOT_SIZE, STAR_SLOT_SIZE):
        img_bgr = cv2.resize(
            img_bgr, (STAR_SLOT_SIZE, STAR_SLOT_SIZE), interpolation=cv2.INTER_AREA
        )
    return img_bgr.astype(np.uint8)


def predict_star(slot_img_bgr: np.ndarray) -> tuple[str, float]:
    """★スロット画像 (BGR) を『gold』『empty』に分類する。

    任意サイズの入力を受け取り、内部で 28x28 にリサイズして CNN 推論する。
    """
    session = _get_star_session()
    batch = _prep_star_slot(slot_img_bgr)[None, ...]
    outs = session.run(["index", "confidence"], {"images": batch})
    idx = int(outs[0][0])
    conf = float(outs[1][0])
    return STAR_CLASS_NAMES[idx], conf


def predict_stars_batch(slot_imgs: list[np.ndarray]) -> list[tuple[str, float]]:
    """複数の★スロット画像をまとめて推論する。"""
    if not slot_imgs:
        return []
    session = _get_star_session()
    batch = np.stack([_prep_star_slot(img) for img in slot_imgs], axis=0)
    outs = session.run(["index", "confidence"], {"images": batch})
    idxs = outs[0].tolist()
    confs = outs[1].tolist()
    return [(STAR_CLASS_NAMES[int(i)], float(c)) for i, c in zip(idxs, confs)]
