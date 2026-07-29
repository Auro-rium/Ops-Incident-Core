FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MODEL_ROLE=reranker \
    RERANKER_MODEL_ID=BAAI/bge-reranker-v2-m3 \
    MODEL_REVISION= \
    MODEL_DIR=/opt/incidentops/model

ARG MODEL_REVISION
ARG RERANKER_MODEL_ID=BAAI/bge-reranker-v2-m3

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY score.py /app/score.py
COPY server.py /app/server.py

RUN test -n "${MODEL_REVISION}" \
    && MODEL_REVISION="${MODEL_REVISION}" RERANKER_MODEL_ID="${RERANKER_MODEL_ID}" \
       python -c "import os; from huggingface_hub import snapshot_download; snapshot_download(repo_id=os.environ['RERANKER_MODEL_ID'], revision=os.environ['MODEL_REVISION'], local_dir='/opt/incidentops/model')"

ENV MODEL_REVISION=${MODEL_REVISION} \
    RERANKER_MODEL_ID=/opt/incidentops/model

RUN useradd --create-home --uid 10001 modelserver
USER modelserver

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
