FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
# CPU-only PyTorch first (the default Linux wheel pulls ~2 GB of CUDA libraries we do not need)
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements.txt
COPY alembic.ini .
COPY alembic alembic
COPY backend backend
RUN useradd --create-home app && mkdir -p /data /app/repo_data && chown -R app /data /app
USER app
EXPOSE 8000
# Apply migrations, then serve. Secrets come from environment variables, never from the image.
CMD ["sh", "-c", "alembic upgrade head && uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
