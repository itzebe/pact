# Builds the frontend, then serves it from the FastAPI app so a single
# container is a complete, runnable demo — which is what the judges need.

FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/backend

# Install from the pinned requirements file, not an inline list: an inline list
# silently drifted from the real dependencies, and a missing PyJWT meant the image
# booted but every authenticated request raised ModuleNotFoundError.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=web /web/dist /app/static

# The app mounts the built UI at / when PACT_STATIC_DIR points at it.
ENV PACT_STATIC_DIR=/app/static PACT_DATA_DIR=/data
EXPOSE 8000
CMD ["uvicorn", "pact.main:app", "--host", "0.0.0.0", "--port", "8000"]
