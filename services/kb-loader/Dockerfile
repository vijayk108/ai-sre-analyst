FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/kb-loader/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/kb-loader/load.py /app/load.py
COPY knowledge-base /app/knowledge-base
ENTRYPOINT ["python", "/app/load.py", "--runbooks-dir", "/app/knowledge-base/runbooks"]
