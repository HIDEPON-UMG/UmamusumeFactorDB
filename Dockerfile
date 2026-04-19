# UmamusumeFactorDB Cloud Run 用 Dockerfile
# - Python 3.12 slim ベース（Cloud Run で実績多）
# - torch CPU 版を先入れ
# - ONNX モデル (models/modules) と EasyOCR モデルをイメージに焼き込んでコールドスタート短縮

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # EasyOCR モデル保存先を固定化
    EASYOCR_MODULE_PATH=/models/easyocr

WORKDIR /app

# システム依存（opencv が求める libGL など）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Python 依存
COPY server/requirements.txt /tmp/server_requirements.txt
RUN pip install --upgrade pip \
    && pip install torch==2.5.1+cpu torchvision==0.20.1+cpu \
       --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r /tmp/server_requirements.txt

# EasyOCR 日本語モデルを事前 DL（コールドスタート時の DL を回避）
RUN mkdir -p $EASYOCR_MODULE_PATH \
    && python -c "import easyocr; r = easyocr.Reader(['ja', 'en'], gpu=False, model_storage_directory='$EASYOCR_MODULE_PATH', verbose=False); print('easyocr preloaded')"

# アプリコードをコピー
COPY src /app/src
COPY config/recognizer.json /app/config/recognizer.json
COPY config/unique_skill_to_character.json /app/config/unique_skill_to_character.json
COPY models/modules /app/models/modules
COPY server/main.py /app/server/main.py

# ビルド時に factor ONNX の softmax 出力版を事前生成
#（Cloud Run の FS は /tmp 以外は read-only なので実行時生成不可）
RUN cd /app && python -c "import sys; sys.path.insert(0, 'src'); from umafactor.infer import _ensure_factor_with_probs; from umafactor.config import model_path; _ensure_factor_with_probs(model_path('factor')); print('factor probs model prebuilt')"

# Cloud Run はポート $PORT (通常 8080) を listen する
ENV PORT=8080
EXPOSE 8080

# uvicorn で起動
CMD ["sh", "-c", "uvicorn server.main:app --host 0.0.0.0 --port ${PORT}"]
