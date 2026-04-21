"""★スロット分類器（金/空 の 2 クラス）を学習し、ONNX でエクスポートする。

入力形式は既存 OnnxPredictor と整合を取り、NHWC uint8 BGR バッチ (N, 28, 28, 3)。
内部で NHWC→NCHW、uint8→float32/255 変換してから CNN に渡す。
出力は index (int64) と confidence (float32) の 2 本で、既存 predictor と同じ名前。

使い方:
    python scripts/train_star_classifier.py
    python scripts/train_star_classifier.py --epochs 30 --batch-size 64
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SLOT_SIZE = 28
CLASS_NAMES = ["empty", "gold"]  # index 0 = empty, 1 = gold


def imread_unicode(path: Path):
    import cv2

    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def imwrite_unicode(path: Path, img) -> bool:
    import cv2

    ok, buf = cv2.imencode(path.suffix, img)
    if not ok:
        return False
    buf.tofile(str(path))
    return True


class StarSlotDataset(Dataset):
    """labels.csv を読み込んで (画像 NHWC uint8, ラベル int) を返す。"""

    def __init__(self, dataset_root: Path, augment: bool = False):
        self.dataset_root = dataset_root
        self.augment = augment
        self.items: list[tuple[Path, int]] = []
        with (dataset_root / "labels.csv").open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                label_name = row["label"]
                if label_name not in CLASS_NAMES:
                    continue
                label_idx = CLASS_NAMES.index(label_name)
                fname = row["filename"]
                path = dataset_root / label_name / fname
                if path.exists():
                    self.items.append((path, label_idx))

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, idx: int):
        path, label = self.items[idx]
        img = imread_unicode(path)
        if img is None:
            img = np.zeros((SLOT_SIZE, SLOT_SIZE, 3), dtype=np.uint8)
        if img.shape[:2] != (SLOT_SIZE, SLOT_SIZE):
            import cv2

            img = cv2.resize(img, (SLOT_SIZE, SLOT_SIZE), interpolation=cv2.INTER_AREA)

        if self.augment:
            img = self._augment(img)
        # NHWC uint8 のまま返し、モデル側の前処理で変換する
        return img, label

    def _augment(self, img: np.ndarray) -> np.ndarray:
        import cv2

        h, w = img.shape[:2]
        # 回転 ±5 度
        if random.random() < 0.5:
            angle = random.uniform(-5, 5)
            M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            img = cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
        # 明るさ ±10%
        if random.random() < 0.5:
            factor = random.uniform(0.9, 1.1)
            img = np.clip(img.astype(np.float32) * factor, 0, 255).astype(np.uint8)
        # ガウシアンノイズ
        if random.random() < 0.3:
            noise = np.random.normal(0, 4, img.shape).astype(np.float32)
            img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
        # 平行移動 ±2px
        if random.random() < 0.5:
            dx = random.randint(-2, 2)
            dy = random.randint(-2, 2)
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            img = cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
        return img


def collate(batch):
    imgs = np.stack([b[0] for b in batch])  # (N, H, W, 3) uint8
    labels = np.array([b[1] for b in batch], dtype=np.int64)
    return torch.from_numpy(imgs), torch.from_numpy(labels)


class StarClassifier(nn.Module):
    """28x28 BGR → 2 クラス。入力は NCHW float32（0-1 正規化済み）を想定。"""

    def __init__(self, num_classes: int = 2):
        super().__init__()
        self.conv1 = nn.Conv2d(3, 16, 3, padding=1)
        self.conv2 = nn.Conv2d(16, 32, 3, padding=1)
        self.pool = nn.MaxPool2d(2, 2)
        self.fc1 = nn.Linear(32 * 7 * 7, 64)
        self.fc2 = nn.Linear(64, num_classes)

    def forward(self, x):
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = torch.flatten(x, 1)
        x = F.relu(self.fc1(x))
        return self.fc2(x)


class InferenceWrapper(nn.Module):
    """既存 OnnxPredictor と同じ入出力形式に揃えるエクスポート用ラッパ。

    入力: NHWC uint8 (N, 28, 28, 3)
    出力: index (int64, shape=(N,)), confidence (float32, shape=(N,))
    """

    def __init__(self, core: StarClassifier):
        super().__init__()
        self.core = core

    def forward(self, x_nhwc_uint8):
        x = x_nhwc_uint8.to(torch.float32) / 255.0
        x = x.permute(0, 3, 1, 2)  # NHWC → NCHW
        logits = self.core(x)
        probs = F.softmax(logits, dim=1)
        conf, idx = probs.max(dim=1)
        return idx.to(torch.int64), conf


def train(args) -> None:
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    dataset_root = args.dataset
    full_ds = StarSlotDataset(dataset_root, augment=False)
    if len(full_ds) == 0:
        print(f"データが空です: {dataset_root}", file=sys.stderr)
        sys.exit(1)

    # ラベルごとに stratified split
    labels = np.array([lb for _, lb in full_ds.items])
    idx_empty = np.where(labels == 0)[0]
    idx_gold = np.where(labels == 1)[0]
    rng = np.random.default_rng(args.seed)
    rng.shuffle(idx_empty)
    rng.shuffle(idx_gold)

    def split(arr, ratio=0.8):
        n = int(len(arr) * ratio)
        return arr[:n], arr[n:]

    tr_e, va_e = split(idx_empty)
    tr_g, va_g = split(idx_gold)
    train_idx = np.concatenate([tr_e, tr_g])
    val_idx = np.concatenate([va_e, va_g])
    rng.shuffle(train_idx)

    class Subset(Dataset):
        def __init__(self, base, indices, augment):
            self.base = base
            self.indices = indices
            self.augment = augment

        def __len__(self):
            return len(self.indices)

        def __getitem__(self, i):
            real_i = int(self.indices[i])
            path, label = self.base.items[real_i]
            img = imread_unicode(path)
            if img is None:
                img = np.zeros((SLOT_SIZE, SLOT_SIZE, 3), dtype=np.uint8)
            if img.shape[:2] != (SLOT_SIZE, SLOT_SIZE):
                import cv2

                img = cv2.resize(img, (SLOT_SIZE, SLOT_SIZE), interpolation=cv2.INTER_AREA)
            if self.augment:
                img = self.base._augment(img)
            return img, label

    train_ds = Subset(full_ds, train_idx, augment=True)
    val_ds = Subset(full_ds, val_idx, augment=False)

    # クラス重み（クラス不均衡に対するサンプリング補正）
    train_labels = labels[train_idx]
    class_counts = np.bincount(train_labels, minlength=2)
    class_weight = 1.0 / np.maximum(class_counts, 1)
    sample_weights = class_weight[train_labels]
    sampler = WeightedRandomSampler(
        weights=torch.from_numpy(sample_weights).double(),
        num_samples=len(train_labels),
        replacement=True,
    )

    train_loader = DataLoader(
        train_ds, batch_size=args.batch_size, sampler=sampler, collate_fn=collate, num_workers=0
    )
    val_loader = DataLoader(
        val_ds, batch_size=args.batch_size, shuffle=False, collate_fn=collate, num_workers=0
    )

    print(f"[data] train={len(train_ds)}, val={len(val_ds)}, class_counts(train)={class_counts.tolist()}")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    core = StarClassifier(num_classes=2).to(device)
    optimizer = torch.optim.Adam(core.parameters(), lr=args.lr)

    best_val_acc = 0.0
    best_state = None
    history = []

    for epoch in range(args.epochs):
        core.train()
        running_loss = 0.0
        correct = 0
        total = 0
        for imgs, lbs in train_loader:
            imgs = imgs.to(device).to(torch.float32) / 255.0
            imgs = imgs.permute(0, 3, 1, 2)
            lbs = lbs.to(device)
            logits = core(imgs)
            loss = F.cross_entropy(logits, lbs)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            running_loss += float(loss.item()) * imgs.size(0)
            pred = logits.argmax(dim=1)
            correct += int((pred == lbs).sum().item())
            total += imgs.size(0)
        train_loss = running_loss / total
        train_acc = correct / total

        # 検証
        core.eval()
        vcorrect = 0
        vtotal = 0
        conf_mat = np.zeros((2, 2), dtype=np.int64)  # [true][pred]
        with torch.no_grad():
            for imgs, lbs in val_loader:
                imgs = imgs.to(device).to(torch.float32) / 255.0
                imgs = imgs.permute(0, 3, 1, 2)
                lbs = lbs.to(device)
                logits = core(imgs)
                pred = logits.argmax(dim=1)
                vcorrect += int((pred == lbs).sum().item())
                vtotal += imgs.size(0)
                for t, p in zip(lbs.cpu().numpy(), pred.cpu().numpy()):
                    conf_mat[int(t)][int(p)] += 1
        val_acc = vcorrect / vtotal
        history.append({
            "epoch": epoch + 1,
            "train_loss": train_loss,
            "train_acc": train_acc,
            "val_acc": val_acc,
            "confusion": conf_mat.tolist(),
        })
        print(
            f"[epoch {epoch+1:02d}] loss={train_loss:.4f} tr_acc={train_acc:.4f} "
            f"val_acc={val_acc:.4f} confusion={conf_mat.tolist()}"
        )
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.detach().cpu().clone() for k, v in core.state_dict().items()}

    assert best_state is not None
    core.load_state_dict(best_state)
    core.eval()

    # 成果物保存
    art_dir = PROJECT_ROOT / "artifacts" / "star_classifier"
    art_dir.mkdir(parents=True, exist_ok=True)
    torch.save(best_state, art_dir / "best.pt")
    (art_dir / "history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ONNX エクスポート
    wrapper = InferenceWrapper(core).to("cpu").eval()
    dummy = torch.zeros((1, SLOT_SIZE, SLOT_SIZE, 3), dtype=torch.uint8)
    onnx_dir = PROJECT_ROOT / "models" / "modules" / "star_classifier"
    onnx_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = onnx_dir / "prediction.onnx"
    torch.onnx.export(
        wrapper,
        dummy,
        str(onnx_path),
        input_names=["images"],
        output_names=["index", "confidence"],
        dynamic_axes={
            "images": {0: "batch"},
            "index": {0: "batch"},
            "confidence": {0: "batch"},
        },
        opset_version=17,
    )
    print(f"\n=== 学習完了 ===")
    print(f"best val_acc: {best_val_acc:.4f}")
    print(f"checkpoint: {art_dir / 'best.pt'}")
    print(f"ONNX: {onnx_path}")

    # ラベル定義も labels.json と同じ形式で出力
    labels_json = onnx_dir / "labels.json"
    labels_json.write_text(
        json.dumps({"star_classifier.class": CLASS_NAMES}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"labels.json: {labels_json}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "datasets" / "stars")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    train(args)


if __name__ == "__main__":
    main()
