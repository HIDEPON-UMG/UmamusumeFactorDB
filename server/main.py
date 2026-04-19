"""Cloud Run で動作する因子 OCR サーバ (FastAPI)。

フロー:
  1. Apps Script の onFormSubmit トリガからリクエストを受信
     {secret, submitter_id, image_base64, submission_id}
  2. 画像を pipeline.analyze_image で解析
  3. Apps Script Webhook (factors_normalized タブ) に 3 行書き込み
  4. 結果を呼び出し元に返す

環境変数:
  SHARED_SECRET          — Apps Script → Cloud Run 認証用
  APPS_SCRIPT_WEBHOOK_URL — factors 書き込み用 webhook URL
  APPS_SCRIPT_SECRET     — factors webhook のシークレット
  TARGET_TAB             — factors 書き込み先タブ名（既定: factors_normalized）
"""

from __future__ import annotations

import base64
import io
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

# src/ を import パスに
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from umafactor.pipeline import analyze_image  # noqa: E402
from umafactor.schema import COLUMNS, Submission  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("factor-processor")


# 環境変数取得
def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


SHARED_SECRET = _env("SHARED_SECRET")
APPS_SCRIPT_WEBHOOK_URL = _env("APPS_SCRIPT_WEBHOOK_URL")
APPS_SCRIPT_SECRET = _env("APPS_SCRIPT_SECRET")
TARGET_TAB = _env("TARGET_TAB", "factors_normalized")

app = FastAPI(title="UmamusumeFactorDB Processor")


class ProcessRequest(BaseModel):
    secret: str
    submitter_id: str
    image_base64: str
    submission_id: str | None = None


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "target_tab": TARGET_TAB,
        "has_webhook": bool(APPS_SCRIPT_WEBHOOK_URL),
    }


@app.post("/process")
def process(req: ProcessRequest, request: Request) -> dict[str, Any]:
    # 認証
    if not SHARED_SECRET:
        logger.error("SHARED_SECRET env var not configured")
        raise HTTPException(status_code=500, detail="server not configured")
    if req.secret != SHARED_SECRET:
        logger.warning("Invalid secret from %s", request.client.host if request.client else "?")
        raise HTTPException(status_code=401, detail="invalid secret")

    submission_id = req.submission_id or str(uuid.uuid4())

    # 画像デコード
    try:
        img_bytes = base64.b64decode(req.image_base64)
    except Exception as e:
        logger.exception("base64 decode failed")
        raise HTTPException(status_code=400, detail=f"invalid image_base64: {e}")

    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=400, detail="cv2.imdecode returned None")

    # analyze_image は image_path を受けるので一時ファイル経由
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        cv2.imwrite(tmp.name, img)
        tmp_path = tmp.name

    try:
        submission, _queue = analyze_image(
            image_path=tmp_path,
            submitter_id=req.submitter_id,
            debug_crops_dir=None,
        )
        # 明示的に投稿側が指定した submission_id があればそちらで上書き
        submission.submission_id = submission_id
        submission.image_filename = f"form-{submission_id[:8]}.png"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Apps Script webhook に書き込み
    if not APPS_SCRIPT_WEBHOOK_URL or not APPS_SCRIPT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="APPS_SCRIPT_WEBHOOK_URL / APPS_SCRIPT_SECRET not configured",
        )

    import requests as _req

    payload = {
        "secret": APPS_SCRIPT_SECRET,
        "tab": TARGET_TAB,
        "columns": COLUMNS,
        "rows": submission.to_rows(),
    }
    try:
        resp = _req.post(APPS_SCRIPT_WEBHOOK_URL, json=payload, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        sheet_result = resp.json()
    except Exception as e:
        logger.exception("webhook write failed")
        raise HTTPException(status_code=502, detail=f"webhook error: {e}") from e

    if not sheet_result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"webhook returned error: {sheet_result.get('error')}",
        )

    logger.info(
        "processed submission_id=%s submitter=%s main=%s parent1=%s parent2=%s",
        submission_id,
        req.submitter_id,
        submission.main.character,
        submission.parent1.character,
        submission.parent2.character,
    )

    return {
        "ok": True,
        "submission_id": submission_id,
        "rows_written": sheet_result.get("rows_appended", 3),
        "summary": {
            "main": {
                "character": submission.main.character,
                "blue": f"{submission.main.blue_type}★{submission.main.blue_star}",
                "red": f"{submission.main.red_type}★{submission.main.red_star}",
                "green": f"{submission.main.green_name}★{submission.main.green_star}",
                "skills_count": len(submission.main.skills),
            },
            "parent1": {
                "character": submission.parent1.character,
                "blue": f"{submission.parent1.blue_type}★{submission.parent1.blue_star}",
                "red": f"{submission.parent1.red_type}★{submission.parent1.red_star}",
                "green": f"{submission.parent1.green_name}★{submission.parent1.green_star}",
                "skills_count": len(submission.parent1.skills),
            },
            "parent2": {
                "character": submission.parent2.character,
                "blue": f"{submission.parent2.blue_type}★{submission.parent2.blue_star}",
                "red": f"{submission.parent2.red_type}★{submission.parent2.red_star}",
                "green": f"{submission.parent2.green_name}★{submission.parent2.green_star}",
                "skills_count": len(submission.parent2.skills),
            },
        },
    }
