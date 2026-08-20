FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi==0.139.0 uvicorn==0.51.0 asyncpg==0.31.0
COPY live-eval/order_api.py /opt/live-eval/order_api.py

CMD ["uvicorn", "order_api:create_app", "--factory", "--app-dir", "/opt/live-eval", "--host", "0.0.0.0", "--port", "8082"]
