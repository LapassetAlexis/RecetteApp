#!/bin/bash
set -e

echo "⏳ Attente d'Ollama..."
until curl -s http://ollama:11434/api/tags > /dev/null 2>&1; do
    sleep 2
done
echo "✅ Ollama prêt."

MODEL=${OLLAMA_MODEL:-mistral}
echo "📦 Vérification du modèle $MODEL..."
if ! curl -s "http://ollama:11434/api/show" -d "{\"name\": \"$MODEL\"}" | grep -q "license"; then
    echo "📥 Téléchargement de $MODEL (premier lancement, peut prendre 2-5 min)..."
    curl -s "http://ollama:11434/api/pull" -d "{\"name\": \"$MODEL\"}" > /dev/null
    echo "✅ Modèle $MODEL prêt."
else
    echo "✅ Modèle $MODEL déjà présent."
fi

echo "🚀 Démarrage de l'application..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
