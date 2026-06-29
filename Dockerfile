# ---- Build stage ----
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Runtime stage ----
FROM python:3.12-slim

WORKDIR /app

# Installer les dépendances système minimales (pour aiosqlite)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY app/ /app/app/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Utilisateur non-root + dossier de données lui appartenant
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

# Volume pour les données SQLite
VOLUME ["/data"]

EXPOSE 8000

# L'entrypoint démarre en root (pour corriger les droits du volume /data hérité
# d'anciens conteneurs root), puis bascule sur appuser via gosu.
ENTRYPOINT ["/app/entrypoint.sh"]
