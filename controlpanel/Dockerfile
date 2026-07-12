FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# app code + the repo's config.env / deploy/state.env are mounted or copied in.
COPY backend ./backend
COPY frontend ./frontend

# config.env and deploy/state.env are read from the repo root two levels above
# backend/. In the image we copy them in at build (or mount at runtime).
COPY config.env /config.env
COPY state.env /deploy/state.env

# config.py resolves REPO_ROOT as parents[2] of backend/config.py = /.
# Layout inside the image: /app/backend, /config.env, /deploy/state.env -> but
# parents[2] of /app/backend/config.py is / , so place files at /config.env and
# /deploy/state.env accordingly (done above).

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
