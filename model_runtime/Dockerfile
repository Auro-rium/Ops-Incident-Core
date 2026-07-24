FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MODEL_ROLE=embedding \
    EMBEDDING_MODEL_ID=BAAI/bge-m3 \
    RERANKER_MODEL_ID=BAAI/bge-reranker-v2-m3 \
    MODEL_REVISION=

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY score.py /app/score.py

RUN useradd --create-home --uid 10001 modelserver
USER modelserver
