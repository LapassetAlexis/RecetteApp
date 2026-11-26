# 🍽️ Menu Planner

Planificateur de repas de la semaine, branché sur une base de recettes Notion.
On compose la semaine à la main, l'app en tire une liste de courses fusionnée
et rangée par rayon, qu'on peut partager par lien. Un LLM sert seulement à
importer et enrichir les recettes (lecture d'une page ou d'un texte collé) ;
il ne choisit pas les repas.

**Fonctionnalités :**
- 📅 Constructeur manuel : grille midi/soir sur la période choisie, nombre de personnes par repas, midis groupés (même couleur = même plat cuisiné une fois)
- 📝 Brouillons : plusieurs semaines en préparation en parallèle, réouvrables et modifiables jusqu'à validation
- 🧱 Briques (steak, riz, œuf…) et ➕ repas libres, créés à la volée depuis le planning
- 🥗 Accompagnements multiples par repas
- 🛒 Liste de courses calculée depuis les ingrédients enregistrés : quantités mises à l'échelle, fusionnées, groupées par rayon, cases à cocher, articles ajoutés à la main, articles masqués et placard
- 🔗 Partage de la liste par lien public, sans connexion : on y coche les articles, synchronisés avec la page planning
- 👨‍🍳 Mode cuisine : une recette en plein écran, étape par étape, avec minuteurs
- 💾 Page « Données » (`/admin`) : export, import et réhydratation ; sauvegarde périodique en cron
- 📶 Hors ligne : la liste partagée reste consultable et cochable sans réseau
- 📊 Estimation nutritionnelle de la semaine et par recette
- 📚 Historique des semaines validées, aperçu au survol, « Refaire » une semaine
- 📖 Livre de recettes Notion : filtres, notation ⭐, fiche détail avec portions ajustables
- 🔗 Import de recette depuis une URL, un texte collé ou une saisie manuelle
- 🔒 Connexion optionnelle par session (formulaire de login)
- 📱 Installable (PWA)

---

## 🚀 Démarrage rapide (Docker)

### Prérequis

- Docker et Docker Compose
- Une intégration Notion ayant accès à la base de recettes
- Pour l'import de recettes : une clé Groq ou Gemini (gratuites), ou le conteneur Ollama fourni

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

## 💻 Lancer en local

Python 3.12 (version de la CI).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Base SQLite locale (le défaut /data/app_recettes.db est celui du conteneur)
DATABASE_PATH=./dev.db .venv/bin/uvicorn app.main:app --reload --port 8020
```

Sans `NOTION_TOKEN`, l'app démarre mais la liste de recettes est vide. Les
autres variables sont lues dans l'environnement (voir plus bas).

Vérifications (les mêmes qu'en CI) :

```bash
.venv/bin/ruff check app tests scripts
.venv/bin/python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=65
```

Les tests n'appellent ni Notion ni un LLM : ils tournent sur une base SQLite
temporaire avec des clients simulés.

### Tests de bout en bout (Playwright)

Les parcours navigateur sont dans `tests/e2e/` et exclus de la suite par
défaut (`addopts = "-m 'not e2e'"`). Ils démarrent l'app dans un sous-processus,
sur une base temporaire, avec un faux Notion (`tests/e2e/fake_notion.py`).

```bash
.venv/bin/playwright install chromium          # une fois
.venv/bin/python -m pytest -m e2e
.venv/bin/python -m pytest -m e2e --headed     # voir le navigateur
```

Le service worker est bloqué dans ces tests, sauf dans
`test_parcours_hors_ligne.py` qui ouvre son propre contexte pour le vérifier.
En CI, le job `e2e` tourne à part et n'est pas encore bloquant.

Pour essayer l'app à la main avec ce faux Notion (sans clé ni réseau) :

```bash
.venv/bin/python -m tests.e2e.fake_notion /tmp/menu.db 8799   # http://127.0.0.1:8799
```

---

## 📖 Guide d'utilisation

### 1. Construire la semaine (`/`)

1. Choisir la période (**Du** / **Au**).
2. Dans chaque case midi/soir, **+ choisir un repas** ouvre le sélecteur de
   recettes (recherche par nom, filtres par nature et base). **+ accompagnement** ajoute
   un ou plusieurs accompagnements.
3. Régler le nombre de personnes de chaque repas (0 = absent, la case est ignorée).
4. Les midis d'un même groupe (G1, G2…, même couleur) partagent le plat du
   premier midi rempli : on cuisine une fois pour plusieurs jours.
5. Finir avec :
   - **💾 Enregistrer le brouillon** : la semaine reste modifiable, on y revient
     depuis l'historique ;
   - **✅ Valider ma semaine** : ouvre la page du planning.

### 2. Ajuster le planning (`/planning/{id}`)

- **↻ Changer** un repas pour une autre recette.
- **➕ accompagnement** / **✕** pour composer les accompagnements.
- **➕ Repas libre** : un plat qui n'existe pas encore en base (nom + quelques
  ingrédients), créé dans Notion et placé dans la case.
- **➕ Nouvelle brique** : un composant simple (steak, riz…) réutilisable, placé
  en plat ou en accompagnement.
- **🍲 Enrichir ma semaine** : extrait les ingrédients manquants des recettes de
  la semaine.
- **📋 Copier le menu**, **🖨️** imprimer, **📊 Nutrition** pour l'estimation.
- **✅ Valider la semaine** : le brouillon rejoint l'historique.

### 3. Liste de courses

- **🛒 Générer la liste de courses** : calcul à partir des ingrédients déjà
  enregistrés pour chaque recette (pas d'appel LLM), mis à l'échelle du nombre
  de personnes, fusionnés par nom et unité, groupés par rayon. Les recettes
  sans ingrédients sont signalées : il faut d'abord les enrichir.
- Les cases cochées sont enregistrées côté serveur, article par article ; sans
  réseau, elles sont mises en file et envoyées au retour de la connexion.
- **Articles ajoutés à la main** (« 2 l jus d'orange », « Papier toilette ») :
  ils rejoignent la liste dans leur rayon ; **✕ Retirer de la liste** les enlève.
- **Je l'ai déjà** masque un article pour cette semaine ; **🏠 Toujours à la
  maison** le range dans le placard, un réglage du foyer qui le masque dans
  toutes les listes. Les articles masqués restent listés sous **Masqués**, d'où
  on les restaure (ou on les retire du placard).
- Chaque article indique les recettes qui l'utilisent (🍽).
- **📋 Copier** copie les articles non cochés.

### 4. Courses partagées (`/courses/<jeton>`)

**🔗 Partager la liste** crée un lien public, ouvert sans connexion. Le jeton
ne donne accès qu'à cette liste et ne permet que de cocher :

- articles restants groupés par rayon, articles cochés repliés sous
  **Déjà pris** (on peut les décocher) ;
- coches partagées avec la page planning, dans les deux sens : la page
  publique se met à jour toutes les 20 s quand l'onglet est visible ;
- **📋 Copier** la liste restante.

Sans réseau, la page reste utilisable (voir la section Hors ligne).

### 5. Mode cuisine (`/recette/{id}/cuisine`)

Depuis une fiche recette, **👨‍🍳 Mode cuisine** ouvre la recette en plein
écran, au nombre de portions choisi sur la fiche : ingrédients cochables,
étapes une à une (boutons, balayage ou flèches du clavier), écran maintenu
allumé, et minuteurs proposés pour les durées lues dans le texte (« 20 min »,
« 1 h 30 »).

### 6. Historique (`/historique`)

Brouillons en cours et semaines validées, avec aperçu au survol.
**🔁 Refaire** recopie une semaine en nouveau brouillon ; **🗑️ Supprimer**
efface un planning.

### 7. Recettes (`/recettes`, `/recette/{id}`)

- Liste des recettes Notion, recherche (nom ou ingrédient), filtres par nature, type, base, état et tags, notation ⭐
  (enregistrée dans Notion).
- **🔄 Enrichir toutes les recettes** : extraction des ingrédients manquants,
  avec la progression en direct.
- Fiche détail : ingrédients avec portions ajustables, instructions,
  nutrition, bloc d'export au format Cooklang, suppression (archive la page Notion).
- **Enrichir** une recette (`/recette/{id}/enrichir`) : relit la source,
  pré-remplit un formulaire éditable, puis enregistre dans Notion après validation.

### 8. Ajouter une recette (`/ajouter`)

Trois entrées :
- **🔗 Depuis une URL** (site, vidéo) → **🔍 Analyser** : lecture des données
  structurées de la page (JSON-LD) quand elles existent, sinon extraction par le LLM ;
- **📋 Depuis un texte** (note perso, réponse d'un assistant…) → **✨ Structurer** ;
- **✍️ Saisir manuellement**.

Le formulaire pré-rempli (nom, nature, type, base, tags, moment, portions,
ingrédients, instructions, image) reste éditable avant **💾 Ajouter la recette**,
qui crée la page dans Notion.

---

## 💾 Données (`/admin`)

Les recettes vivent dans Notion, mais les plannings, listes de courses,
ingrédients structurés et réglages du foyer n'existent que dans la base SQLite
du volume `/data`. La page **Données** (lien en pied de page, protégée par la
connexion quand elle est active) permet de :

- **Exporter** toute la base locale dans un fichier JSON ;
- **Importer** un tel fichier, en **fusion** ou en **remplacement** : le
  fichier est d'abord validé et un récapitulatif est affiché ; rien n'est écrit
  avant la confirmation, puis tout s'applique en une seule transaction ;
- **Réhydrater** le cache d'ingrédients depuis Notion : les ingrédients
  manquants en local sont reconstruits à partir de la copie texte que Notion
  garde de chaque recette.

### Sauvegarde périodique

`scripts/backup.py` écrit un export (même format que la page **Données**)
dans un dossier et ne garde que les N plus récents (8 par défaut) :

```bash
python -m scripts.backup /data/backups            # garde 8 fichiers
python -m scripts.backup /data/backups --keep 14
```

Exemple de crontab sur l'hôte, chaque nuit à 3 h :

```
0 3 * * * docker exec -u appuser menu-planner python -m scripts.backup /data/backups
```

---

## 📶 Hors ligne

L'app est installable (PWA) et enregistre un service worker, **seulement en
contexte sécurisé : HTTPS, ou `localhost` / `127.0.0.1`**. Servie en HTTP
simple sur une autre adresse (IP du réseau local, par exemple), elle marche
normalement mais sans mode hors ligne.

- La **liste partagée** `/courses/<jeton>` est mise en cache à chaque visite :
  sans réseau, elle se recharge depuis le cache avec le dernier état connu, un
  bandeau signale le mode hors ligne, et les coches sont mises en file puis
  envoyées au retour du réseau. Au plus 20 listes sont gardées (les plus
  anciennes sont retirées).
- Les **autres pages** ne sont jamais gardées en cache (elles peuvent être
  privées) : sans réseau, elles affichent une page « Hors ligne ».
- Les fichiers statiques (CSS, JS, icônes) sont servis depuis le cache puis
  rafraîchis en arrière-plan ; une seule version de chaque fichier est gardée.

---

## ⚙️ Configuration

### Providers LLM

Utilisés pour l'import et l'enrichissement des recettes uniquement.

| Provider | Clé | Gratuit ? | Configuration |
|---|---|---|---|
| **Groq** | `gsk_...` | ✅ avec quota | `LLM_PROVIDER=groq` + `GROQ_API_KEY` |
| **Gemini** | `AIza...` | ✅ avec quota | `LLM_PROVIDER=gemini` + `GEMINI_API_KEY` |
| **Ollama** (local) | aucune | ✅ | `LLM_PROVIDER=ollama` (défaut) |

Chaque appel est retenté sur les erreurs transitoires. Si le provider cloud
reste indisponible, l'app se rabat sur Ollama.

### Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `NOTION_TOKEN` | — | Token d'intégration Notion |
| `NOTION_DATABASE_ID` | _base du projet_ | ID de la base de recettes Notion |
| `LLM_PROVIDER` | `ollama` | `ollama`, `gemini` ou `groq` |
| `OLLAMA_URL` | `http://ollama:11434` | URL du serveur Ollama |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Modèle Ollama |
| `GEMINI_API_KEY` | — | Clé API Gemini |
| `GEMINI_MODEL` | `gemini-2.0-flash-001` | Modèle Gemini |
| `GROQ_API_KEY` | — | Clé API Groq |
| `GROQ_MODEL` | `llama-3.1-8b-instant` | Modèle Groq |
| `APP_TITLE` | `Menu Planner` | Titre affiché |
| `SECRET_KEY` | _aléatoire_ | Clé de signature des sessions. Si vide, une clé est générée à chaque démarrage et les sessions sont perdues au redémarrage |
| `AUTH_USER` | — | Identifiant de connexion (optionnel) |
| `AUTH_PASSWORD` | — | Mot de passe de connexion (optionnel) |
| `DATABASE_PATH` | `/data/app_recettes.db` | Fichier SQLite |

> 🔒 **Connexion optionnelle** : renseigner `AUTH_USER` **et** `AUTH_PASSWORD`
> active un formulaire de connexion (`/login`). La session dure 30 jours (cookie
> signé avec `SECRET_KEY` : la fixer pour ne pas être déconnecté à chaque
> redémarrage). Restent publics : `/health`, `/static`, `/login`, `/logout` et
> les listes partagées `/courses/<jeton>` (lecture et coches). Les deux vides = accès libre.

### Sécurité des appels sortants

- La clé Gemini passe dans l'en-tête `x-goog-api-key`, jamais dans l'URL.
- Les pages de recettes ne sont téléchargées que depuis des adresses publiques
  (http/https, IP non privées), avec une taille et une durée limitées. Les
  redirections (3 au plus) sont revalidées à chaque saut.

---

## 🏗️ Architecture

```
RecetteApp/
├── app/
│   ├── main.py              # Application FastAPI : middlewares (auth) et routeurs
│   ├── deps.py              # Instances partagées (base, Notion, LLM, templates)
│   ├── routers/             # Routes HTTP par domaine
│   │   ├── pages.py             # Constructeur, historique, /sw.js, /health
│   │   ├── planning.py          # Construction, page planning, changements de repas
│   │   ├── courses.py           # Liste de courses, coches, lien public, articles manuels, placard
│   │   ├── recettes.py          # Livre de recettes, fiche, enrichissement, ajout, import
│   │   ├── cuisine.py           # Mode cuisine
│   │   ├── catalogue.py         # Catalogue JSON du sélecteur
│   │   ├── admin.py             # Page « Données » : export, import, réhydratation
│   │   └── auth.py              # Connexion / déconnexion
│   ├── services/            # Règles métier, sans HTTP
│   │   ├── planning.py          # Construction et modification des plannings
│   │   ├── creations.py         # Briques et repas libres
│   │   ├── shopping.py          # Calcul de la liste, coches, masques, placard
│   │   ├── recettes.py          # Recettes : détail, enrichissement, import
│   │   ├── nutrition_week.py    # Nutrition de la semaine
│   │   └── backup.py            # Export, import, réhydratation
│   ├── config.py            # Variables d'env, taxonomie (Nature, Repas, Base, Tag)
│   ├── notion_client.py     # API Notion (lecture/écriture des recettes)
│   ├── llm_client.py        # Client LLM (Groq/Gemini/Ollama) + extraction de pages
│   ├── database.py          # SQLite : plannings, cache des recettes, préférences
│   ├── categories.py        # Rayon de magasin d'un ingrédient
│   ├── text_utils.py        # Parsing et fusion des ingrédients, nettoyage des titres
│   ├── nutrition.py         # Estimation nutritionnelle
│   ├── templates/           # Pages Jinja2
│   │   ├── base.html            # Layout commun
│   │   ├── index.html           # Constructeur de semaine
│   │   ├── planning.html        # Planning + liste de courses
│   │   ├── courses_public.html  # Liste partagée (coches)
│   │   ├── cuisine.html         # Mode cuisine
│   │   ├── historique.html      # Brouillons et semaines validées
│   │   ├── recettes.html        # Livre de recettes
│   │   ├── recette_detail.html  # Fiche recette
│   │   ├── enrichir.html        # Enrichissement d'une recette
│   │   ├── ajouter.html         # Ajout de recette
│   │   ├── admin.html           # Page « Données »
│   │   ├── login.html           # Connexion
│   │   └── partials/            # Morceaux partagés (grille, repas, liste de courses, modales)
│   └── static/
│       ├── style.css            # Styles communs
│       ├── css/                 # Styles par page
│       ├── js/                  # Scripts par page ; common.js = utilitaires partagés
│       ├── sw.js                # Service worker (hors ligne)
│       ├── offline.html         # Page de secours hors ligne
│       └── manifest.json        # PWA, avec les icônes
├── scripts/                 # Sauvegarde (backup.py) et scripts ponctuels (migration Notion, briques, portions)
├── tests/                   # pytest (Notion et LLM simulés)
│   └── e2e/                 # Parcours Playwright (faux Notion, pytest -m e2e)
├── docker-compose.yml       # App + Ollama
├── Dockerfile
├── pyproject.toml           # Config pytest et ruff
├── requirements.txt
└── requirements-dev.txt
```

### Cycle d'une semaine

```
1. Constructeur (/) : la grille est envoyée en JSON à POST /construire
2. Sauvegarde d'un BROUILLON en SQLite (planning_history, valide=0)
   + grille d'origine dans data_json["builder"] pour la réouverture
3. Page planning : changements de repas, accompagnements, briques, repas libres
4. Liste de courses : ingrédients du cache local (enriched_recipes),
   mis à l'échelle, fusionnés, rangés par rayon → data_json["liste_courses"]
5. Validation → la semaine rejoint l'historique
```

### Import d'une recette

```
1. URL → téléchargement sécurisé de la page
2. Données JSON-LD (schema.org/Recipe) si présentes, sinon texte visible → LLM
3. Classification (type, base, tags) : LLM + heuristique par mots-clés
4. Formulaire pré-rempli et éditable
5. Validation → page Notion + cache local des ingrédients
```

---

## 🔌 Base Notion attendue

| Propriété | Type | Description |
|---|---|---|
| `Nom` | Title | Nom de la recette |
| `URL` | URL | Lien vers la recette d'origine |
| `Nature` | Select | `Recette` (défaut) ou `Ingrédient` (brique) |
| `Repas` | Multi-select | Cours du repas : Plat, Entrée, Dessert… |
| `Base` | Multi-select | Ingrédient principal : Viande, Poisson, Œuf, Légume, Féculent, Végé |
| `Tag` | Multi-select | Attributs libres |
| `Moment` | Select | Midi, Soir, Les deux (créé automatiquement) |
| `État` | Status | À essayer, Réussie… |
| `Note` | Select | ⭐ à ⭐⭐⭐⭐⭐ |
| `Portions` | Number | Nombre de portions de la recette |
| `Ingrédients` | Rich text | Ingrédients (créé automatiquement) |

Les valeurs autorisées sont dans `app/config.py` et doivent correspondre aux
options de la base. `Ingrédients` et `Moment` sont créés au démarrage s'ils
manquent ; une propriété absente donne une valeur vide sans bloquer l'app.

---

## 📊 Stockage local (SQLite)

- **`planning_history`** : plannings (brouillons et semaines validées) ; le
  détail (repas, liste de courses, cases cochées, jeton de partage) est dans `data_json`
- **`planning_recipes`** : recettes utilisées par chaque planning
- **`enriched_recipes`** : cache des ingrédients, portions et nutrition par recette
- **`app_prefs`** : réglages du foyer, dont le placard

---

## 📝 Licence

Projet personnel — Tristabeau © 2024
