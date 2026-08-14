FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/mock-inference-svc/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/mock-inference-svc/app /app/app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
