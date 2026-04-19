"""Apps Script Web App（Webhook）にスプレッドシート書き込みを POST する。

事前準備：
- 対象スプレッドシートに `apps_script/Code.gs` を Apps Script として配置・デプロイ
- `config/apps_script_webhook.json` に webhook_url と secret を記載
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from .config import CONFIG_DIR
from .schema import COLUMNS, SHEET_TAB_NAME, Submission


DEFAULT_CONFIG_FILENAME = "apps_script_webhook.json"
REQUEST_TIMEOUT_SEC = 30


class WebhookConfigError(RuntimeError):
    pass


def _load_webhook_config(path: Path) -> dict:
    if not path.exists():
        raise WebhookConfigError(
            f"Webhook 設定が見つかりません: {path}\n"
            f"apps_script_webhook.example.json をコピーして webhook_url と secret を記入してください。"
        )
    with path.open(encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("webhook_url", "secret"):
        if not cfg.get(key):
            raise WebhookConfigError(f"{path} に '{key}' が設定されていません")
    return cfg


def append_submission(
    submission: Submission,
    tab_name: str | None = None,
    config_path: Path | None = None,
) -> dict:
    """Submission を 3 行（main/parent1/parent2）に展開して Apps Script 経由で追記する。"""
    cfg_path = config_path or (CONFIG_DIR / DEFAULT_CONFIG_FILENAME)
    cfg = _load_webhook_config(cfg_path)
    tab = tab_name or cfg.get("tab") or SHEET_TAB_NAME

    payload = {
        "secret": cfg["secret"],
        "tab": tab,
        "columns": COLUMNS,
        "rows": submission.to_rows(),
    }
    resp = requests.post(
        cfg["webhook_url"],
        json=payload,
        timeout=REQUEST_TIMEOUT_SEC,
        allow_redirects=True,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Webhook からの応答が JSON ではありません: {resp.text[:200]}"
        ) from exc
    if not data.get("ok"):
        raise RuntimeError(f"Webhook 側でエラー: {data.get('error')}")
    return data
