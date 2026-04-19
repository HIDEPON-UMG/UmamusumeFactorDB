"""tkinter ベースの因子レビュー UI（単一ウィンドウでナビゲーション）。

モデルの top 候補に正解が含まれないケース（地固めなど）でも、因子名辞書（813 件）
から自由入力欄でインクリメンタル検索できるようにする。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np
from PIL import Image, ImageTk
from rapidfuzz import process as fuzz_process

from .config import load_labels
from .review import ReviewItem, ReviewQueue


SLOT_LABELS = {
    "blue": "青因子（ステータス）",
    "red": "赤因子（適性）",
    "green": "緑因子（固有スキル）",
    "white": "白因子（スキル）",
}

_ALL_FACTOR_NAMES: list[str] | None = None


def _all_factor_names() -> list[str]:
    """labels.json の factor.name（813 件）をキャッシュして返す。"""
    global _ALL_FACTOR_NAMES
    if _ALL_FACTOR_NAMES is None:
        _ALL_FACTOR_NAMES = list(load_labels()["factor.name"])
    return _ALL_FACTOR_NAMES


def _bgr_to_tk(bgr: np.ndarray, target_h: int = 160) -> ImageTk.PhotoImage:
    h, w = bgr.shape[:2]
    scale = max(1, target_h // max(1, h))
    resized = cv2.resize(bgr, (w * scale, h * scale), interpolation=cv2.INTER_NEAREST)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    return ImageTk.PhotoImage(Image.fromarray(rgb))


class ReviewWindow:
    def __init__(self, queue: ReviewQueue) -> None:
        self.queue = queue
        self.idx = 0
        self.root = tk.Tk()
        self.root.title("ウマ娘 因子レビュー")
        self.root.geometry("720x540")

        # 進捗ラベル
        self.progress_var = tk.StringVar()
        ttk.Label(self.root, textvariable=self.progress_var, font=("Yu Gothic UI", 10)).pack(
            anchor="w", padx=12, pady=(8, 0)
        )

        # コンテキスト（どのウマ娘 × どのスロット）
        self.context_var = tk.StringVar()
        ttk.Label(self.root, textvariable=self.context_var, font=("Yu Gothic UI", 14, "bold")).pack(
            anchor="w", padx=12, pady=(2, 4)
        )

        # 画像表示
        self.img_label = tk.Label(self.root, borderwidth=1, relief="solid", background="#eee")
        self.img_label.pack(padx=12, pady=4)

        # モデル予測情報
        self.model_info_var = tk.StringVar()
        ttk.Label(self.root, textvariable=self.model_info_var).pack(anchor="w", padx=12)

        # OCR 生テキスト
        self.ocr_info_var = tk.StringVar()
        ttk.Label(
            self.root,
            textvariable=self.ocr_info_var,
            foreground="#006",
            font=("Yu Gothic UI", 9),
        ).pack(anchor="w", padx=12)

        # 候補ラジオ
        cand_frame = ttk.LabelFrame(self.root, text="候補（キーボード 1-8 で選択可）")
        cand_frame.pack(fill=tk.X, padx=12, pady=6)
        self.selected_name = tk.StringVar()
        self.cand_buttons: list[ttk.Radiobutton] = []
        self.cand_frame = cand_frame

        # 自由入力 + 因子名辞書からのインクリメンタル検索
        free_frame = ttk.LabelFrame(
            self.root, text="自由入力（上書き）— 入力すると辞書 813 件からサジェストを表示"
        )
        free_frame.pack(fill=tk.X, padx=12, pady=4)
        self.free_name = tk.StringVar()
        self.free_entry = ttk.Entry(free_frame, textvariable=self.free_name)
        self.free_entry.pack(fill=tk.X, padx=6, pady=(4, 0))
        self.free_name.trace_add("write", lambda *_: self._update_suggestions())
        # サジェストリスト（rapidfuzz で部分一致上位 8 件）
        self.suggest_listbox = tk.Listbox(free_frame, height=5, exportselection=False)
        self.suggest_listbox.pack(fill=tk.X, padx=6, pady=(2, 6))
        self.suggest_listbox.bind("<<ListboxSelect>>", self._on_suggest_click)
        self.suggest_listbox.bind("<Double-Button-1>", self._on_suggest_click)

        # ★数
        star_frame = ttk.LabelFrame(self.root, text="★数（キーボード F1/F2/F3）")
        star_frame.pack(fill=tk.X, padx=12, pady=4)
        self.selected_star = tk.IntVar(value=1)
        for s in (1, 2, 3):
            ttk.Radiobutton(
                star_frame, text=f"★{s}", variable=self.selected_star, value=s
            ).pack(side=tk.LEFT, padx=10, pady=4)

        # ナビゲーションボタン
        nav = tk.Frame(self.root)
        nav.pack(fill=tk.X, padx=12, pady=8)
        ttk.Button(nav, text="← 戻る", command=self._prev).pack(side=tk.LEFT)
        ttk.Button(nav, text="スキップ", command=self._skip_next).pack(side=tk.LEFT, padx=6)
        ttk.Button(nav, text="確定して次へ →  (Enter)", command=self._save_next).pack(side=tk.RIGHT)
        ttk.Button(nav, text="全て完了", command=self._finish).pack(side=tk.RIGHT, padx=6)

        # キーボードショートカット
        self.root.bind("<Return>", lambda _e: self._save_next())
        self.root.bind("<Escape>", lambda _e: self._skip_next())
        self.root.bind("<Left>", lambda _e: self._prev())
        self.root.bind("<Right>", lambda _e: self._save_next())
        self.root.bind("<F1>", lambda _e: self.selected_star.set(1))
        self.root.bind("<F2>", lambda _e: self.selected_star.set(2))
        self.root.bind("<F3>", lambda _e: self.selected_star.set(3))
        for i in range(1, 9):
            self.root.bind(str(i), lambda _e, n=i: self._pick_candidate(n - 1))

        self._render()

    def _current_item(self) -> ReviewItem | None:
        if 0 <= self.idx < len(self.queue.items):
            return self.queue.items[self.idx]
        return None

    def _render(self) -> None:
        item = self._current_item()
        if item is None:
            self.root.destroy()
            return

        total = len(self.queue.items)
        self.progress_var.set(f"{self.idx + 1} / {total}")
        self.context_var.set(
            f"{item.uma_role}  —  {SLOT_LABELS.get(item.slot, item.slot)}"
        )

        self._tk_img = _bgr_to_tk(item.image, target_h=160)
        self.img_label.configure(image=self._tk_img)

        top_name, top_conf = item.candidates[0] if item.candidates else (item.current_name, 0.0)
        self.model_info_var.set(
            f"top候補: {top_name}  スコア {top_conf:.2f}    "
            f"現在値: {item.current_name} ★{item.current_star}"
        )
        self.ocr_info_var.set(f"OCR 生テキスト: {item.ocr_raw or '(なし)'}")

        # 候補ラジオを動的再構築
        for rb in self.cand_buttons:
            rb.destroy()
        self.cand_buttons = []
        init = item.reviewed_name if item.reviewed_name else item.current_name
        self.selected_name.set(init)
        sources = item.candidate_sources or {}
        for i, (name, conf) in enumerate(item.candidates[:8], start=1):
            src = sources.get(name, "onnx")
            prefix = {"ocr": "[OCR] ", "both": "[ONNX+OCR] ", "onnx": ""}.get(src, "")
            rb = ttk.Radiobutton(
                self.cand_frame,
                text=f"[{i}] {prefix}{name}    (スコア {conf:.2f})",
                variable=self.selected_name,
                value=name,
            )
            rb.pack(anchor="w", padx=6, pady=1)
            self.cand_buttons.append(rb)

        self.free_name.set("")
        self.selected_star.set(item.reviewed_star or item.current_star or 1)

    def _pick_candidate(self, idx: int) -> None:
        item = self._current_item()
        if item is None:
            return
        if 0 <= idx < len(item.candidates):
            self.selected_name.set(item.candidates[idx][0])

    def _update_suggestions(self) -> None:
        """自由入力欄の内容に応じて因子辞書から候補を検索する。"""
        query = self.free_name.get().strip()
        self.suggest_listbox.delete(0, tk.END)
        if not query:
            return
        # まず部分一致を最優先し、次に rapidfuzz の類似度で補う
        names = _all_factor_names()
        prefix_or_sub = [n for n in names if query in n][:8]
        shown = list(prefix_or_sub)
        if len(shown) < 8:
            fuzz_hits = fuzz_process.extract(query, names, limit=8 + len(shown))
            for name, score, _i in fuzz_hits:
                if name not in shown:
                    shown.append(name)
                if len(shown) >= 8:
                    break
        for n in shown[:8]:
            self.suggest_listbox.insert(tk.END, n)

    def _on_suggest_click(self, _event=None) -> None:
        sel = self.suggest_listbox.curselection()
        if not sel:
            return
        name = self.suggest_listbox.get(sel[0])
        self.free_name.set(name)

    def _save_current(self) -> None:
        item = self._current_item()
        if item is None:
            return
        name = self.free_name.get().strip() or self.selected_name.get().strip()
        if not name:
            name = item.current_name
        item.reviewed_name = name
        item.reviewed_star = int(self.selected_star.get())

    def _save_next(self) -> None:
        self._save_current()
        self.idx += 1
        if self.idx >= len(self.queue.items):
            self.root.destroy()
            return
        self._render()

    def _skip_next(self) -> None:
        # reviewed_name を None のままで進む（現状値を維持）
        self.idx += 1
        if self.idx >= len(self.queue.items):
            self.root.destroy()
            return
        self._render()

    def _prev(self) -> None:
        if self.idx > 0:
            self.idx -= 1
            self._render()

    def _finish(self) -> None:
        # 現在のアイテムを保存して閉じる
        self._save_current()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def review_queue_interactive(queue: ReviewQueue) -> None:
    if not queue.items:
        return
    ReviewWindow(queue).run()
