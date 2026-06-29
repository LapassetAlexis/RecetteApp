#!/bin/bash
set -e

# On n'attend Ollama (et on ne pull le modèle) que si on l'utilise réellement.
# En mode gemini/groq, inutile de bloquer le démarrage sur le conteneur Ollama.
PROVIDER=${LLM_PROVIDER:-ollama}
if [ "$PROVIDER" = "ollama" ]; then
    echo "⏳ Attente d'Ollama..."
    until curl -s http://ollama:11434/api/tags > /dev/null 2>&1; do
        sleep 2
    done
    echo "✅ Ollama prêt."

    MODEL=${OLLAMA_MODEL:-qwen2.5:3b}
    echo "📦 Vérification du modèle $MODEL..."
    if ! curl -s "http://ollama:11434/api/show" -d "{\"name\": \"$MODEL\"}" | grep -q "license"; then
        echo "📥 Téléchargement de $MODEL (premier lancement, peut prendre 2-5 min)..."
        curl -s "http://ollama:11434/api/pull" -d "{\"name\": \"$MODEL\"}" > /dev/null
        echo "✅ Modèle $MODEL prêt."
    else
        echo "✅ Modèle $MODEL déjà présent."
    fi
else
    echo "ℹ️ Provider LLM = $PROVIDER (cloud), on saute l'attente d'Ollama."
fi

# Corriger les droits du volume /data (souvent root, hérité d'anciens conteneurs)
# puis lancer l'app en tant qu'utilisateur non-root.
mkdir -p /data
chown -R appuser:appuser /data 2>/dev/null || true

echo "🚀 Démarrage de l'application..."
exec gosu appuser uvicorn app.main:app --host 0.0.0.0 --port 8000
