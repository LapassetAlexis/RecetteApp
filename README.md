# 🍽️ Menu Planner

Générateur de planning de repas hebdomadaire avec IA, synchronisé avec Notion.

**Fonctionnalités :**
- 🤖 Génération IA de 7 jours de repas avec répétition des midis
- 🛒 Liste de courses intelligente (fusionnée, dédoublonnée)
- 📋 Planning modifiable (changer un repas, multi-sélection)
- 📓 Base de recettes Notion avec page détail en Cooklang
- 🏷️ Filtres par tags et état des recettes
- ⭐ Notation des recettes (synchronisée avec Notion)
- 🌙 Support de 4 providers LLM (Groq, Gemini, Ollama, OpenRouter)
- 🔄 Fallback automatique si un provider est indisponible
- 👨‍👩‍👧‍👧 Configuration par jour (nombre de personnes, groupes de midis)
- ✏️ Prompt personnalisable pour l'IA

---

## 🚀 Démarrage rapide

### Prérequis

- Docker & Docker Compose
- Un compte Notion avec une base de recettes
- Une clé API Groq (gratuite) ou Gemini (gratuite)

### Installation

```bash
git clone https://github.com/LapassetAlexis/RecetteApp.git
cd RecetteApp
cp .env.example .env
# Éditer .env avec vos clés
docker compose up -d
```

Accès : http://localhost:8020

---

## 📖 Guide d'utilisation

### 1. Générer un planning

Remplis le formulaire :

| Champ | Description |
|---|---|
| **Semaine du** | Date de début de la semaine |
| **🌤️ Saison** | Printemps, Été, Automne, Hiver |
| **🌡️ Température** | Impacte le choix des plats (ex: canicule → plats froids) |
| **🍳 Organisation des midis** | Config jour par jour (groupes + nombre de personnes) |
| **🥫 Restes à écouler** | Ingrédients à utiliser en priorité |
| **✏️ Instructions IA** | Consignes personnalisées (ex: "soirs légers, cuisiner pour 3 midis") |

Clique **"🔄 Générer 7 jours"** → le planning apparaît en ~2s (Groq).

### 2. Modifier le planning

Tu peux :
- **✅ Cocher plusieurs repas** → barre orange "Changer en masse"
- **↻ Cliquer sur un repas** → choisir une alternative
- **🛒 Générer la liste de courses** quand le planning est figé

### 3. Liste de courses

Une seule extraction LLM pour toutes les recettes (batch dédoublonné) :
- Ingrédients fusionnés (ex: "huile d'olive" n'apparaît qu'une fois)
- Quantités additionnées
- Cases à cocher pour faire les courses

### 4. Gestion des recettes

**Page Recettes** (`/recettes`) :
- Liste de toutes les recettes depuis Notion
- Filtres : tags, état (À essayer / Réussie)
- ⭐ Notation par étoiles (sauvegardée dans Notion)
- 📖 Détail de chaque recette en format Cooklang

**Ajout d'une recette** (`/ajouter`) :
- Colle une URL → 🔍 Analyser → tout se pré-remplit (nom, tags, ingrédients, instructions, image)
- Tu valides après vérification
- Les ingrédients et instructions sont sauvegardés dans la fiche Notion

### 5. Page détail recette

`/recette/{id}` — Affiche la recette avec :
- Métadonnées (type, état, moment, tags)
- Image de couverture
- Ingrédients en format Cooklang (mis en valeur)
- Instructions de cuisson
- Lien vers la fiche Notion originale

---

## ⚙️ Configuration

### Providers LLM

| Provider | Clé | Gratuit ? | Vitesse | Configuration |
|---|---|---|---|---|
| **Groq** | `gsk_...` | ✅ 30 req/min | ⚡ 1-2s | `LLM_PROVIDER=groq` + `GROQ_API_KEY` |
| **Gemini** | `AIza...` | ✅ 1500 req/jour | ⚡ 2-5s | `LLM_PROVIDER=gemini` + `GEMINI_API_KEY` |
| **Ollama** (local) | Aucune | ✅ Illimité | 🐢 2-3 min | `LLM_PROVIDER=ollama` (défaut) |

Le fallback automatique fonctionne ainsi :
```
LLM_PROVIDER=groq
  → Groq répond ? ✅ 1-2s
  → Erreur (quota, timeout) ? ⏳ Fallback sur Ollama (2-3 min)
```

### Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `NOTION_TOKEN` | — | Token d'intégration Notion |
| `LLM_PROVIDER` | `ollama` | `ollama`, `gemini` ou `groq` |
| `GROQ_API_KEY` | — | Clé API Groq |
| `GEMINI_API_KEY` | — | Clé API Gemini |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Modèle Ollama |
| `SECRET_KEY` | _aléatoire_ | Clé secrète sessions (générée au démarrage si vide) |
| `AUTH_USER` | — | Identifiant HTTP Basic (optionnel) |
| `AUTH_PASSWORD` | — | Mot de passe HTTP Basic (optionnel) |

> 🔒 **Auth optionnelle** : renseigner `AUTH_USER` **et** `AUTH_PASSWORD` protège
> toute l'app par HTTP Basic (`/health` et `/static` restent publics). Laisser
> vide = accès libre (défaut homelab).

---

## 🏗️ Architecture

```
app-recettes/
├── app/
│   ├── main.py              # Routes FastAPI + logique métier
│   ├── config.py             # Configuration (variables d'env)
│   ├── notion_client.py      # API Notion (lecture/écriture)
│   ├── llm_client.py         # Client LLM (Groq/Gemini/Ollama)
│   ├── cooklang.py           # Parseur Cooklang
│   ├── database.py           # SQLite (historique, cache)
│   ├── templates/            # Interface web (Jinja2)
│   │   ├── base.html         # Layout commun
│   │   ├── index.html        # Formulaire de génération
│   │   ├── planning.html     # Planning 7 jours + liste courses
│   │   ├── recettes.html     # Browse recettes Notion
│   │   ├── recette_detail.html # Détail recette en Cooklang
│   │   ├── ajouter.html      # Ajout recette en 2 étapes
│   │   └── historique.html   # Plannings précédents
│   └── static/style.css
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

### Workflow de génération

```
1. Formulaire → critères (saison, nb pers., groupes midi, prompt)
2. Récupération des recettes depuis Notion API
3. Filtrage : tags, état, exclues (historique 4 semaines)
4. Envoi au LLM (30 recettes max pour éviter le 413)
5. Parsing de la réponse (liste numérotée / markdown / JSON)
6. Association avec les infos Notion (ID, URLs)
7. Sauvegarde du planning en SQLite
8. (Optionnel) Extraction batch des ingrédients → liste de courses
```

### Workflow d'ajout de recette

```
1. Colle l'URL
2. Téléchargement du HTML + extraction og:image
3. Passage du texte brut au LLM → extraction : nom, tags, ingrédients, instructions
4. Pré-remplissage du formulaire (éditable)
5. Validation → création dans Notion + sauvegarde locale
```

---

## 🔌 API Notion

### Structure de la base attendue

| Propriété | Type | Description |
|---|---|---|
| `Nom` | Title | Nom de la recette |
| `URL` | URL | Lien vers la recette originale |
| `Repas` | Select | Plat, Dessert, Entrée, etc. |
| `Tag` | Multi-select | Viande, Poisson, Légumes, etc. |
| `État` | Status | À essayer, Réussie, Testée |
| `Note` | Select | ⭐ à ⭐⭐⭐⭐⭐ |
| `Moment` | Select | Midi, Soir, Les deux (créé auto) |
| `Ingrédients` | Rich text | Liste des ingrédients (créé auto) |

L'app crée automatiquement les champs manquants (`Ingrédients`, `Moment`) au premier démarrage.

---

## 📊 Stockage local (SQLite)

- **`planning_history`** : Historique des plannings générés (4 semaines anti-répétition)
- **`planning_recipes`** : Recettes utilisées dans chaque planning
- **`enriched_recipes`** : Cache des ingrédients extraits (évite de ré-appeler le LLM)

---

## 🛠️ Développement

```bash
# Cloner
git clone https://github.com/LapassetAlexis/RecetteApp.git
cd RecetteApp

# Installer les dépendances
pip install -r requirements.txt

# Lancer en dev
uvicorn app.main:app --reload --port 8020

# Tests
pip install -r requirements-dev.txt
pytest
```

---

## 📝 Licence

Projet personnel — Tristabeau © 2024
