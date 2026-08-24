# Stage 1: build the React dashboard into static files.
# This has to be a real build stage, not an assumption that frontend/dist
# already exists on disk -- it's gitignored (it's a build artifact), so a
# fresh `git clone` + `docker compose up --build` would otherwise ship an
# image with no dashboard at all, only the bare API.
FROM node:20-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: the API, serving the dashboard built above as static files --
# one deployable image, no separate frontend host or build step needed.
FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend-build /frontend/dist ./frontend/dist

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
