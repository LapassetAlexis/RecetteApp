# RecetteApp (Menu Planner) — audit, propositions et plan de développement

Date : 26/11/2026 — branche `main` à `0715e80`, arbre propre.

## 0. Méthode et niveau de preuve

- **Lecture complète** de `app/` (10 modules Python, 10 templates, CSS, service worker), du README, de la CI, du Dockerfile, et de l'historique git (249 commits).
- **Tests lancés** : `.venv/bin/python -m pytest` → 199 tests verts, couverture 76 % (seuil CI : 65 %).
- **App lancée en local** (uvicorn sur 127.0.0.1, base SQLite temporaire, client Notion remplacé par un catalogue factice de 8 recettes, aucun appel réseau, aucun fichier du repo modifié). Vérifications par `curl` et captures Chromium headless en 1280 px et 390 px.
- Ce qui n'a **pas** été vérifié : le comportement contre le vrai Notion, les appels LLM réels, les interactions au clic dans le navigateur (modales, sélecteur de groupe), le rendu iOS/Safari, le déploiement Docker.

Chaque constat est marqué :

- **[V]** vérifié en exécutant l'app ou un bout de code ;
- **[L]** constaté à la lecture du code (fichier:ligne), non exécuté ;
- **[H]** hypothèse à confirmer.

---

## 1. Audit

### 1.1 Ce que fait l'app aujourd'hui

| Domaine | Fonctionnalités | Où |
|---|---|---|
| Constructeur de semaine | Grille 1 à 7 jours × midi/soir, convives par repas (0 = absent), groupes de midis (même couleur = même repas), accompagnements multiples, repas et accompagnements « libres », brouillons multiples | `app/templates/index.html`, `POST /construire` (`app/main.py:1252`) |
| Page planning | Grille fusionnée (jours consécutifs identiques), liste empilée sous 640 px, changement de repas à l'unité ou en masse, ajout d'une « brique », nutrition estimée, validation du brouillon, copie du menu | `app/templates/planning.html`, `app/main.py:352` |
| Liste de courses | Agrégation depuis le cache SQLite des ingrédients, mise à l'échelle par repas et par base de portions, fusion par (nom, unité), rayons, coches persistées côté serveur, copie texte, impression, lien public en lecture seule | `app/main.py:545-651`, `app/text_utils.py:326`, `app/categories.py`, `app/templates/courses_public.html` |
| Recettes | Liste Notion avec recherche (nom + ingrédients) et filtres, fiche détail avec portions ajustables et nutrition, notation, ajout par URL / texte collé / saisie, page « enrichir », suppression (archive Notion) | `recettes.html`, `recette_detail.html`, `ajouter.html`, `enrichir.html` |
| Historique | Brouillons et semaines validées, aperçu au survol, « Refaire » | `historique.html`, `app/main.py:1019` |
| Transverse | Auth par session optionnelle, PWA (manifest + service worker), 3 providers LLM avec retry et repli | `app/main.py:59-89`, `app/static/sw.js`, `app/llm_client.py` |

Point important : **la génération IA du planning a été retirée** (commit `1efc6b7`). Le LLM ne sert plus qu'à l'extraction de recettes (URL, texte) et à la classification.

### 1.2 Architecture et stack

- **Backend** : FastAPI 0.115, Jinja2, `aiosqlite`, `httpx`. Python 3.12 en CI et en Docker, 3.11 dans le `.venv` local.
- **Front** : HTML rendu serveur, JavaScript inline dans chaque template, une feuille `app/static/style.css` (436 lignes). Aucun build, aucune dépendance front.
- **Données** : Notion est la source des recettes (cache mémoire 60 s, `app/notion_client.py:13`). SQLite porte les plannings (`planning_history.data_json`, un gros JSON par planning) et le cache des ingrédients structurés (`enriched_recipes`).
- **Déploiement** : Docker Compose (app + Ollama), volume `/data`.
- **Qualité** : pytest + couverture en CI (`.github/workflows/ci.yml`). Pas de linter, pas de formateur, pas de typage vérifié, pas de test front.

Répartition du code : `app/main.py` fait 2 285 lignes (routes + logique métier + helpers), `app/llm_client.py` 1 177, `planning.html` 761 dont 430 de JS, `index.html` 678 dont 520 de JS.

### 1.3 Bugs

#### Confirmés en exécutant l'app [V]

| # | Bug | Preuve | Emplacement |
|---|---|---|---|
| B1 | **La date d'un brouillon n'est jamais mise à jour.** Rouvrir un brouillon, changer « Du », enregistrer : `week_start` reste l'ancienne date, donc tous les libellés de jours sont faux. | Brouillon créé au 25/11, re-soumis au 02/12 avec `draft_id=1` → la base contient toujours `2026-11-25`. | `Database.update_planning` ne touche que `data_json` (`app/database.py:130-160`) ; appel `app/main.py:1375-1381` |
| B2 | **« Refaire » une semaine produit un brouillon impossible à reprendre.** La copie n'embarque pas la grille `builder` : « Reprendre » ouvre un constructeur vide, et l'enregistrement crée un *troisième* planning. | `GET /?draft=2` → `INITIAL_BUILDER = []` et `draft_id=""`. | `app/main.py:432` (copie sans `builder` ni `off_meals`), `app/main.py:136-144` |
| B3 | **« Refaire » décale les jours.** La copie est datée du lundi courant alors que `week_start` est désormais « la date du jour 1 » (commit `2bf3541`). Une semaine commencée un mercredi se retrouve calée sur lundi. | Original `2026-11-25` (mercredi) → copie `2026-11-23` (lundi), mêmes indices de jour. | `app/main.py:428-429` |
| B4 | **En-tête du planning cassé** : « Mercredi 25/11 → samedi 28/11 —  — 2 pers. ». La saison n'existe plus. Même reliquat dans l'historique (« · 4 pers. »). | Capture + HTML. | `planning.html:8`, `historique.html:33` |
| B5 | **Les jours hors période s'affichent « 🚫 Absent ».** Un planning de 4 jours montre 3 colonnes hachurées (6 cartes « Absent » en mobile). | 12 occurrences pour un planning de 4 jours. | `serialize()` émet `persons: 0` pour les jours non sélectionnés (`index.html:627-628`) → `off_meals` (`app/main.py:1333-1335`) |
| B6 | **Mêmes denrées sur deux lignes** quand les unités diffèrent : « Tomates 1 kg » + « Tomates 250 g », « Lait 0,5 l » + « Lait 25 cl ». | Liste générée. | `merge_ingredients` regroupe par (nom, unité) sans conversion (`app/text_utils.py:346`) |
| B7 | **Coche partagée entre deux lignes.** La clé de coche est le nom seul : les deux lignes « Lait » ont le même `data-item`. Cocher l'une coche l'autre au rechargement, et les deux disparaissent de la page publique. | `data-item="lait"` ×2, `data-item="tomates"` ×2. | `planning.html:301`, `app/main.py:1469`, `app/main.py:1522` |
| B8 | **Mauvais rayon par sous-chaîne.** « blanc de volaille » part en *Fruits & légumes* (contient « ail »). Même mécanisme possible avec « eau » (gâteau, morceau), « sel », « the », « vin », « riz ». | `categorize('blanc de volaille')`. | `app/categories.py:85-89` (`if m in n`) |
| B9 | **Quantités inutilisables** pour les denrées comptées : « Poulet 0.5 », « Citron 0.5 », « Gâteau de riz 0.5 ». | Liste générée pour 2 personnes. | `scale_ingredients` (`app/text_utils.py:301`) sans arrondi |
| B10 | **Lignes d'ingrédients mal lues** : `1 1/2 l de lait` → quantité 1, nom « 1/2 l de lait » ; `½ citron` → pas de quantité ; `2-3 carottes` → nom « -3 carottes » ; `2 c. à soupe d'huile` → unité non reconnue. | `parse_ingredient_line` appelé sur ces 4 lignes. | `app/text_utils.py:160-193`, `:75` |
| B11 | **Le placeholder induit en erreur** : « 4 pommes, 200g farine... » alors que le parseur attend une ligne par ingrédient. La ligne donne un seul ingrédient « pommes, 200g farine ». | Idem. | `ajouter.html:115` |
| B12 | **Redirection ouverte** sur `/login?next=https://evil.example` → 303 vers le site externe, même sans auth activée. | `curl`. | `app/main.py:2250-2251`, `:2274` |
| B13 | **Erreur 500 au login** si l'identifiant ou le mot de passe saisi contient un accent. | `secrets.compare_digest('é', 'admin')` lève `TypeError`. | `app/main.py:2265-2266` |
| B14 | **Barre d'outils de la liste de courses qui déborde en 390 px** : le bouton ↻ sort de l'écran, le titre se replie sur 3 lignes. | Capture mobile. | `planning.html:270-283`, `.shop-h` (`style.css:155`) |

#### Constatés à la lecture [L]

| # | Bug probable | Emplacement |
|---|---|---|
| B15 | **Modifier un brouillon depuis la page planning puis le « Reprendre » annule les modifications.** Les endpoints `/api/update-meal`, `/api/add-side`, `/api/free-meal`, `/api/brique` réécrivent `plats` mais jamais `data["builder"]` ; le constructeur recharge l'ancienne grille et l'enregistrement l'écrase. | `app/main.py:619-651` (`_refresh_shopping_and_save`), `:136-142` |
| B16 | **`/api/enrich/{id}` et le repas libre écrasent la base de portions.** `save_enriched` fait un `INSERT OR REPLACE` complet : sans `base_servings`, la valeur retombe à 4 et la nutrition/durée sont effacées. | `app/database.py:316-333` ; appels sans ces champs `app/main.py:1652`, `:1696`, `:2022`, `:2197` |
| B17 | **XSS / sélection cassée dans la modale « Changer ».** Le nom de recette est injecté sans échappement dans `innerHTML` et dans `data-n="…"`. Un nom avec guillemets (`Salade "César"`) tronque l'attribut : le bouton « Choisir » envoie un nom incomplet, et le repas devient « libre ». | `planning.html:520` ; `usl()` (`:621-640`) a le même défaut mais n'est plus appelée |
| B18 | **Remplacement non atomique d'une case fusionnée.** Un appel par jour, chacun régénère toute la liste de courses. Un échec au 2ᵉ jour laisse la case à moitié changée. | `planning.html:525-534`, `:488-498` |
| B19 | **Repli hors-ligne du service worker inopérant.** `hit \|\| caches.match('/') \|\| new Response(...)` : `caches.match` renvoie une promesse, toujours « vraie », donc la page de secours n'est jamais servie. | `app/static/sw.js:67-70` |
| B20 | **« Modifier » une recette relance l'extraction depuis l'URL source** et pré-remplit avec les données d'origine, pas avec les ingrédients déjà corrigés à la main. Lent, et les corrections sont à refaire. | `app/main.py:749-769` (source avant cache) |
| B21 | **Formulaire d'ajout vidé en cas d'erreur** : le template est re-rendu sans les valeurs saisies. | `app/main.py:1875-1886`, `ajouter.html` |
| B22 | **Impossible de retirer tous les tags** d'une recette (`tags or None` → champ ignoré). | `app/main.py:880-881`, `app/notion_client.py:492` |
| B23 | **Liste de courses non mise à jour après un changement de repas : les coches restent** sur d'anciennes clés (pas de purge de `courses_checked`). | `app/main.py:635` vs `:1443` |
| B24 | **L'icône Notion de la page est remplacée par 🥗** à chaque écriture des ingrédients, et le texte est tronqué à 2 000 caractères sans avertissement. | `app/notion_client.py:257`, `:264` |
| B25 | **Instructions tronquées** à 100 blocs et limitées aux listes numérotées. | `app/notion_client.py:63-73` |
| B26 | **Texte d'état trompeur** : « 📋 Planning validé ! Génère la liste de courses » s'affiche aussi sur un brouillon dont la liste est vide. | `planning.html:315-320` |
| B27 | **Sélecteur de couleur de groupe décalé** [H] : `.group-picker` est défini deux fois ; la règle globale garde `transform: translateX(-50%)` alors que le JS positionne `left` en absolu. Près du bord gauche en mobile, le popup peut sortir de l'écran. | `style.css:59`, `index.html:43-45`, `:494-496` |

### 1.4 Sécurité

Le contexte est un homelab, à pondérer en conséquence.

- **B12, B13** ci-dessus.
- **Clé Gemini dans l'URL** (`app/llm_client.py:643-646`). Les exceptions `httpx` contiennent l'URL ; plusieurs routes renvoient `str(e)` au navigateur (`app/main.py:1595`, `:1721`). [H] : fuite possible de la clé dans les logs ou dans un message d'erreur.
- **Garde anti-SSRF contournable** : `extract_ingredients` télécharge l'URL sans passer par `_safe_fetch_html` (`app/llm_client.py:870-871`), et `_safe_fetch_html` suit les redirections sans revalider la cible (`:161`). [L]
- **Pas de protection CSRF** sur les POST ; `GET /logout` ; cookie `https_only=False` (`app/main.py:83-89`). Acceptable derrière un réseau privé, à durcir si l'app est exposée.
- **Les routes d'action renvoient toujours 200** avec `{"error": …}` : pas de code HTTP exploitable par un client ou un moniteur.

### 1.5 Dette technique

- **`app/main.py` monolithique** : routes, règles métier (courses, nutrition, regroupement), helpers de dates. Tout changement y passe, donc tout travail en parallèle s'y heurte.
- **JavaScript inline non testable**, avec des noms d'une ou deux lettres dans `planning.html` (`c`, `u`, `la`, `rpl`, `usl`, `bul`). Le code de notation par étoiles est dupliqué mot pour mot (`recettes.html:174-220`, `recette_detail.html:187-224`), l'éditeur d'étapes aussi (`ajouter.html:146-162`, `enrichir.html:127-143`), la copie presse-papier aussi (`planning.html:667-683`, `courses_public.html:111-125`).
- **Reliquats de la génération IA** :
  - README entièrement décrit l'ancien produit (formulaire saison/température, « Générer 7 jours ») ;
  - lien de navigation « Générer » (`base.html:22`), docstring de `main.py:1` ;
  - colonnes `saison`, `ingredients_force` et contexte `creneaux_non_resolus`, `briques_manquantes` (`app/main.py:410-411`) ;
  - méthodes mortes `get_recent_recipe_names`, `delete_draft_plannings`, `save_prefs`, `get_prefs` (`app/database.py:171`, `:193`, `:238`, `:248`), encore testées ;
  - module `app/cooklang.py` utilisé seulement par ses tests ;
  - routes sans appelant dans l'UI : `/api/enrich/{id}`, `/api/enrich-all` (non-SSE), `/api/update-side` ;
  - CSS mort : `.day-group`, `.midi-grid`, `.nb-input`, `.mt`, `.meal-toggles`, `.preview-*`, `.cook-*`, `.hero-force` (`style.css:57-63`, `:218-223`, `:309-326`).
- **Variables CSS inexistantes** : `--bd`, `--bg2`, `--bg3`, `--t1`, `--pri`, `--card` sont utilisées dans `index.html`, `historique.html`, `courses_public.html` et `style.css:115`, mais `:root` ne définit que `--bg --surface --text --t2 --pr --prd --sc --ac --bo` (`style.css:2-7`). Les replis codés en dur s'appliquent ; là où il n'y en a pas (`style.css:115`, `courses_public.html:19`), la règle est ignorée.
- **Modèle de données fragile** :
  - le planning est un JSON opaque ; retrouver un planning par jeton de partage parcourt toute la table (`app/database.py:279-299`) ;
  - `planning_recipes` est écrite partout mais lue seulement par une méthode morte et par `/api/alternatives` ;
  - **les ingrédients structurés n'existent que dans SQLite**. Notion ne reçoit qu'une copie texte tronquée et l'app ne la relit jamais (`_parse_page` ignore « Ingrédients », `app/notion_client.py:107-187`). Perdre le volume `/data`, c'est perdre tous les ingrédients, portions et durées.
- **Deux implémentations de la même règle** : `per_day` (`app/main.py:361-368` et `:532-542`), parsing de quantité côté Python et côté JS (`text_utils.py:254`, `recette_detail.html:117`), découpage d'instructions (`text_utils.py:238`, `ajouter.html:163`).
- **Dépréciation Starlette** : `TemplateResponse(name, {"request": …})` génère 39 avertissements aux tests.
- **Démarrage local non documenté** : `DATABASE_PATH` vaut `/data/app_recettes.db` par défaut (`app/config.py:58`), chemin inexistant hors Docker ; le README n'en parle pas.

### 1.6 UX / UI

#### Mobile (390 px)

- **Le constructeur fait plus de 2 600 px de haut** (14 cartes empilées) et les boutons « Enregistrer / Valider » sont tout en bas, sans barre d'action fixe. [V]
- **Cibles tactiles trop petites** : croix `✕` des repas (`.b-x`, `index.html:31`), boutons `↻`/`✕` des accompagnements à 0,65 rem (`style.css:114`), étoiles de notation. [L]
- **Barre d'outils des courses qui déborde** (B14). [V]
- **Aperçu de l'historique au survol uniquement** (`historique.html:92`) : rien au doigt. [L]
- **Page publique des courses en lecture seule** (`courses_public.html:21`) : la personne qui fait les courses ne peut pas cocher. C'est pourtant l'usage principal en magasin. [L]
- **Hors-ligne probablement inactif** [H] : le service worker ne s'enregistre qu'en contexte sécurisé (`base.html:90`), et le commit `2a152dc` indique que l'app est consultée en HTTP par IP locale.
- **Pas d'icône d'écran d'accueil sur iOS** [H] : `apple-touch-icon` et le manifest pointent vers un SVG (`base.html:12`, `manifest.json:13`).

#### Desktop

- **Colonnes « Absent » inutiles** pour les plannings courts (B5). [V]
- **Navigation trompeuse** : « Générer » mène au constructeur manuel ; l'onglet « Historique » est actif sur la page planning. [V]
- **Le picker se vide sans explication** si le catalogue n'est pas encore chargé ou si Notion est en erreur (`index.html:666-669`, `app/main.py:1235-1237`). [L]
- **Aucune alerte en quittant le constructeur** avec une grille non enregistrée. [L]
- **Impossible de déplacer ou copier un repas** d'une case à l'autre ; il faut le retirer et le rechoisir. [L]
- **Impossible de supprimer une semaine validée** depuis l'UI, ni de la rouvrir dans le constructeur. [L]
- **Liste de courses non éditable** : pas d'ajout d'article hors recette, pas de retrait, pas de « j'en ai déjà ». [L]
- **`alert()` natif** dans le constructeur alors qu'un système de toasts existe (`index.html:652`). [L]

### 1.7 Accessibilité

- **Cartes et étoiles non atteignables au clavier** : `div` et `span` cliquables sans rôle, sans `tabindex` (`recettes.html:79`, `:109`, picker `index.html:560-571`). [L]
- **Modales sans sémantique** : pas de `role="dialog"`, pas de piège à focus, pas de fermeture par Échap (`planning.html:162`, `index.html:140`, `recette_detail.html:93`). [L]
- **Focus peu visible** : `outline: none` sur les champs (`style.css:35`), aucun style `:focus-visible` sur les boutons et liens. [L]
- **Boutons à pictogramme sans nom accessible** : `↻`, `✕`, `🖨️`, `G1` n'ont qu'un `title`. [L]
- **Couleur seule** pour distinguer les groupes de midis (le libellé `G1` aide, la légende aussi). [V]
- **Contraste** [H] : orange `#e67e22` sur blanc pour les chips et titres de rayon, texte 0,6 rem dans les cases. À mesurer.
- Points positifs : `lang="fr"`, `aria-live` sur les toasts, labels de formulaires liés, `aria-label` sur les boutons de portions.

### 1.8 Performance

- **Une connexion SQLite par requête** et des lectures en boucle : `_collect_shopping` et `_week_nutrition` appellent `get_enriched` recette par recette (`app/main.py:588`, `:204`). Négligeable à cette échelle.
- **Un client HTTP par appel Notion** (`httpx.AsyncClient()` partout dans `notion_client.py`) : pas de réutilisation de connexion ; l'enregistrement d'une recette enchaîne 5 à 7 requêtes séquentielles (`app/main.py:859-881`).
- **Page Recettes** : toutes les cartes sont rendues côté serveur, le filtrage parcourt tout le DOM à chaque frappe (`recettes.html:135-155`). Correct jusqu'à quelques centaines de recettes.
- **Picker** : jusqu'à 300 cartes reconstruites à chaque frappe, sans temporisation (`index.html:527-575`).
- **Images Notion hébergées** [H] : les URL de couverture de type `file` expirent au bout d'une heure ; une page gardée en cache hors-ligne affichera des images cassées.
- **Cache CSS** : `asset_version` change à chaque redémarrage (`app/main.py:99`), bon réflexe.

### 1.9 Tests

- 199 tests, 2,8 s. Bonne couverture de la logique : `text_utils` 95 %, `categories` 100 %, `nutrition` 99 %, `main` 82 %.
- `notion_client` à 30 % : seule l'analyse des pages est testée, aucune écriture.
- **Aucun test du JavaScript**, alors que la moitié de la logique du constructeur y vit (groupes de midis, sérialisation, période).
- **Aucun test de bout en bout**. Les 14 bugs confirmés plus haut passent tous la CI.
- `tests/test_routes.py` fait 1 524 lignes dans un seul fichier.

---

## 2. Propositions d'amélioration

Effort : S (< ½ journée d'agent), M (½ à 1 journée), L (plus). Priorité : P0 = corrige une perte de données ou un parcours cassé, P1 = gain d'usage net, P2 = confort.

### 2.1 Corrections et fiabilité

| # | Proposition | Valeur pour l'utilisateur | Effort | Priorité | Lot |
|---|---|---|---|---|---|
| A1 | Corriger les brouillons : date mise à jour, « Refaire » qui garde la grille et les bons jours, grille synchronisée après modification depuis la page planning (B1, B2, B3, B15) | Un brouillon repris affiche ce qu'on y a mis, aux bons jours | M | P0 | L01 |
| A2 | Ne plus écraser portions, nutrition et durée quand on ré-enregistre des ingrédients (B16) | Les quantités de la liste de courses restent justes | S | P0 | L01 |
| A3 | Fusionner les unités compatibles (g/kg, ml/cl/l), arrondir les denrées comptées, lire `1 1/2`, `½`, `2-3`, « c. à soupe » (B6, B9, B10) | Une ligne par denrée, des quantités achetables | M | P0 | L02 |
| A4 | Rayons par mot entier et non par sous-chaîne (B8) | Les articles sont dans le bon rayon | S | P1 | L02 |
| A5 | Fermer la redirection ouverte et l'erreur 500 du login, sortir la clé Gemini de l'URL, appliquer la garde SSRF partout (B12, B13, §1.4) | Pas de surprise si l'app est un jour exposée | S | P1 | L01, L04 |
| A6 | Échapper les noms dans la modale, corriger l'en-tête, le débordement mobile, les variables CSS, le texte d'état (B4, B14, B17, B26, B27) | Plus de sélection cassée par un guillemet, écran mobile propre | M | P0 | L05 |
| A7 | Service worker : repli hors-ligne qui fonctionne, icônes PNG (B19) | Installation correcte sur iOS, page de secours réelle | S | P1 | L03 |
| A8 | Export / import de la base locale, et relecture des ingrédients depuis Notion | Les ingrédients saisis ne dépendent plus d'un seul volume Docker | M | P1 | L13 |

### 2.2 Courses

| # | Proposition | Valeur | Effort | Priorité | Lot |
|---|---|---|---|---|---|
| C1 | **Cocher depuis le lien public** (le jeton donne le droit de cocher), avec rafraîchissement périodique | Celui qui fait les courses coche sur son téléphone, l'autre voit l'avancement | M | P0 | L09 |
| C2 | **Articles ajoutés à la main** (« papier toilette », « café ») et retrait d'une ligne | Une seule liste pour toute la semaine | M | P1 | L09 |
| C3 | **Placard** : liste d'ingrédients toujours disponibles (sel, poivre, huile) masqués par défaut, repliés sous « Déjà à la maison » | Liste plus courte, sans recocher les mêmes lignes chaque semaine | M | P1 | L09 |
| C4 | Clé de coche = nom + unité, purge des coches obsolètes quand le planning change (B7, B23) | Une coche ne déborde plus sur une autre ligne | S | P1 | L09 |
| C5 | Liste utilisable hors-ligne en magasin (cache de la page publique + file d'attente des coches) | La liste reste disponible sans réseau | M | P2 | L15 |
| C6 | Ordre des rayons personnalisable (parcours de son magasin) | Moins d'allers-retours | S | P2 | L09 (option) |

### 2.3 Planning

| # | Proposition | Valeur | Effort | Priorité | Lot |
|---|---|---|---|---|---|
| P1 | Masquer les jours hors période au lieu de « Absent » (B5) | Planning court lisible | S | P1 | L10 |
| P2 | Remplacement groupé en une seule requête (B18) | Changement de repas instantané, jamais à moitié fait | M | P1 | L10 |
| P3 | **Rouvrir une semaine validée dans le constructeur** et supprimer une semaine validée | Corriger une semaine sans tout refaire | M | P1 | L10 |
| P4 | **Copier / déplacer un repas** d'une case à l'autre, et « restes » : le soir alimente le midi suivant | Construction de la semaine deux fois plus rapide | M | P1 | L12 |
| P5 | Dans le picker : badge « planifié il y a 2 semaines », tri par note, filtre par tag (Rapide, saison), les recettes « À essayer » mises en avant | Varier les menus sans se souvenir de tout | M | P1 | L12 |
| P6 | Barre d'action fixe en mobile, alerte avant de quitter une grille non enregistrée, état de chargement du catalogue | Plus de saisie perdue | S | P1 | L12 |
| P7 | Export du menu en `.ics` (agenda partagé) | Le menu apparaît dans l'agenda familial | S | P2 | L10 (option) |

### 2.4 Recettes

| # | Proposition | Valeur | Effort | Priorité | Lot |
|---|---|---|---|---|---|
| R1 | « Modifier » ouvre les données enregistrées ; « Ré-extraire depuis la source » devient un bouton explicite (B20) | Édition immédiate, corrections conservées | S | P0 | L11 |
| R2 | Formulaire d'ajout qui garde la saisie en cas d'erreur ; Moment et État modifiables ; retrait de tous les tags (B21, B22) | Plus de re-saisie | S | P1 | L11 |
| R3 | **Ajouter au planning** depuis une fiche recette (choix du brouillon, du jour, du moment) | Planifier au fil de la navigation | M | P1 | L11 |
| R4 | « Cuisinée N fois, dernière fois le … » sur la fiche et tri « pas faite depuis longtemps » dans la liste | Redécouvrir ses recettes | S | P2 | L11 |
| R5 | **Mode cuisine** : une étape à la fois en gros caractères, ingrédients cochables, écran qui reste allumé, minuteurs détectés dans le texte (« 20 min ») | Cuisiner avec le téléphone posé sur le plan de travail | M | P1 | L14 |
| R6 | Cartes et étoiles accessibles au clavier, code de notation mutualisé | Navigation clavier, moins de code dupliqué | S | P1 | L06 |

### 2.5 Socle

| # | Proposition | Valeur | Effort | Priorité | Lot |
|---|---|---|---|---|---|
| S1 | README à jour, lint `ruff`, configuration pytest, démarrage local documenté | Base saine pour les agents | S | P1 | L04 |
| S2 | Découper `main.py` en routeurs et services | Permet de développer plusieurs fonctionnalités en parallèle | L | P1 | L07 |
| S3 | Découper `planning.html` et `index.html` en partiels, sortir le JS en fichiers statiques | Idem côté front, et JS testable | L | P1 | L08 |
| S4 | Tests de bout en bout Playwright sur les 4 parcours clés | Les régressions du type B1 à B5 sont attrapées en CI | M | P1 | L16 |

---

## 3. Plan de développement

### 3.1 Règles communes à tous les lots

À recopier dans le brief de chaque agent.

1. **Périmètre de fichiers strict.** Un lot ne modifie que les fichiers listés. S'il faut toucher un autre fichier, l'agent s'arrête et le signale.
2. **Tests dans un nouveau fichier par lot** (`tests/test_<sujet>.py`), jamais ajoutés à la fin de `tests/test_routes.py`, pour éviter les conflits. Reprendre la fixture `client` de `tests/test_routes.py:47-58` (base SQLite temporaire, Notion simulé par `monkeypatch`).
3. **Commandes de vérification** (aucun build, aucun lint avant L04) :
   - installation : `python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt` (Python 3.12 comme en CI) ;
   - tests : `.venv/bin/python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=65` ;
   - lint, à partir de L04 : `.venv/bin/ruff check app tests scripts` ;
   - lancement local : `DATABASE_PATH=./dev.db .venv/bin/uvicorn app.main:app --reload --port 8020` (sans `NOTION_TOKEN`, la liste de recettes est vide ; les parcours se testent avec Notion simulé) ;
   - image : `docker compose build app`.
4. **Compatibilité des données.** Les plannings existants en base doivent rester lisibles : toute nouvelle clé de `data_json` est optionnelle, avec un repli.
5. **Commits** : format existant `type(portée): message en français` (ex. `fix(courses): …`), sans mention d'IA ni co-auteur. **Ni push ni PR sans accord explicite.**
6. **Définition de « terminé »** : critères d'acceptation remplis, tests du lot verts, suite complète verte, couverture ≥ 65 %.

### 3.2 Vue d'ensemble

| Lot | Titre | Vague | Effort | Dépend de | Branche |
|---|---|---|---|---|---|
| L01 | Intégrité des brouillons et du cache | 1 | M | — | `fix/brouillons-integrite` |
| L02 | Moteur de courses : unités, quantités, rayons | 1 | M | — | `fix/courses-unites-rayons` |
| L03 | PWA : service worker et icônes | 1 | S | — | `fix/pwa-hors-ligne-icones` |
| L04 | Outillage, README, durcissement LLM | 1 | S | — | `chore/outillage-readme` |
| L05 | Correctifs d'interface planning et constructeur | 1 | M | — | `fix/ui-planning-mobile-a11y` |
| L06 | Recettes : accessibilité et code mutualisé | 1 | S | — | `fix/recettes-a11y-notation` |
| L07 | Découpage de `main.py` | 2 | L | L01, L04 | `refactor/routeurs-services` |
| L08 | Découpage des templates et du JS | 2 | L | L05, L06 | `refactor/templates-partiels-js` |
| L09 | Courses : coche publique, articles manuels, placard | 3 | L | L02, L07, L08 | `feat/courses-partage-interactif` |
| L10 | Planning : période, édition groupée, réouverture | 3 | M | L07, L08 | `feat/planning-edition` |
| L11 | Recettes : édition directe, ajout au planning | 3 | M | L07, L08 | `feat/recettes-edition-planning` |
| L12 | Constructeur : copier, restes, picker enrichi | 3 | M | L01, L07, L08 | `feat/constructeur-ux` |
| L13 | Sauvegarde et restauration des données | 3 | M | L07 | `feat/sauvegarde-donnees` |
| L14 | Mode cuisine | 4 | M | L11 | `feat/mode-cuisine` |
| L15 | Courses hors-ligne | 4 | M | L03, L09 | `feat/courses-hors-ligne` |
| L16 | Tests de bout en bout | 4 | M | L09 à L12 | `test/e2e-parcours` |

---

### Vague 1 — correctifs, six lots sans fichier commun

#### L01 — Intégrité des brouillons et du cache

- **Branche** : `fix/brouillons-integrite`
- **Objectif** : un brouillon repris, dupliqué ou modifié depuis la page planning reste cohérent ; le cache des ingrédients n'est plus écrasé ; le login est durci.
- **Fichiers** : `app/main.py`, `app/database.py`, `tests/test_brouillons_integrite.py` (nouveau), `tests/test_database.py` (suppression des tests de méthodes retirées uniquement).
- **Étapes**
  1. `Database.update_planning` : ajouter les paramètres optionnels `week_start` et `nb_personnes`, mis à jour s'ils sont fournis. Les passer depuis `construire` (`app/main.py:1375-1381`). (B1)
  2. `dupliquer_planning` (`app/main.py:419-444`) : copier aussi `builder` et `off_meals`. Si `builder` est absent (anciens plannings), le reconstruire depuis `plats` avec une fonction `_builder_from_plats(plats, off_meals)` : une case par (jour, moment), `persons` du plat, `group` = jour pour les midis, sauf midis consécutifs identiques qui partagent un groupe. (B2)
  3. Même fonction : `week_start` = aujourd'hui (jour 1 = date du jour), plus « lundi de la semaine ». (B3)
  4. `_refresh_shopping_and_save` (`app/main.py:619-651`) : après mutation de `plats`, régénérer `planning_data["builder"]` avec `_builder_from_plats`, et retirer de `courses_checked` les clés qui ne correspondent plus à un article de la nouvelle liste. (B15, B23)
  5. `Database.save_enriched` : passer en `INSERT … ON CONFLICT(notion_id) DO UPDATE` qui ne remplace `cuisson_minutes`, `nutrition`, `base_servings` que si l'appelant les fournit (paramètres `None` par défaut). Adapter les 6 appels de `main.py`. (B16)
  6. Login : fonction `_safe_next(url)` qui n'accepte qu'un chemin relatif commençant par un seul `/` (rejette `//`, `\`, un schéma) ; l'utiliser dans `login_page` et `login_submit`. Comparer les identifiants encodés en UTF-8 (`secrets.compare_digest(a.encode(), b.encode())`). (B12, B13)
  7. Supprimer `get_recent_recipe_names`, `delete_draft_plannings`, `save_prefs`, `get_prefs` et leurs tests. Ajouter `Database.get_recipe_usage() -> dict[str, dict]` (`{notion_id: {"count": n, "last": "AAAA-MM-JJ"}}`, plannings validés uniquement, date = `week_start` + jour − 1) : elle servira à L11 et L12.
- **Critères d'acceptation**
  - Re-soumettre un brouillon avec une autre date change `week_start` en base et le titre de la page planning.
  - « Refaire » puis `GET /?draft=<id>` rend un `INITIAL_BUILDER` non vide et `draft_id` renseigné ; le premier repas de la copie tombe sur la date du jour.
  - Changer un repas via `/api/update-meal` sur un brouillon, puis `GET /?draft=<id>` : la grille contient le nouveau repas.
  - Après `/api/enrich/{id}` sur une recette dont `base_servings` vaut 2, la valeur reste 2.
  - `GET /login?next=https://evil.example` et `?next=//evil.example` redirigent vers `/`. Un login avec « é » renvoie 401, pas 500.
- **Tests à écrire** : un test par critère ci-dessus, plus `_builder_from_plats` (midis groupés, repas libre, accompagnements) et `get_recipe_usage`.
- **Vérification** : `.venv/bin/python -m pytest --cov=app --cov-fail-under=65`.

#### L02 — Moteur de courses : unités, quantités, rayons

- **Branche** : `fix/courses-unites-rayons`
- **Objectif** : une ligne par denrée, des quantités lisibles, le bon rayon.
- **Fichiers** : `app/text_utils.py`, `app/categories.py`, `tests/test_courses_unites.py` (nouveau), `tests/test_categories.py` (nouveau).
- **Étapes**
  1. `parse_ingredient_line` : accepter les nombres mixtes (`1 1/2`), les fractions Unicode (`½ ¼ ¾ ⅓ ⅔`), les intervalles (`2-3`, `2 à 3` → on garde la borne haute). Étendre les motifs d'unités à « c. à soupe », « c. à café », « cuil. à s. » (`app/text_utils.py:75-76`). (B10)
  2. `merge_ingredients` : avant regroupement, ramener masses en grammes et volumes en millilitres ; regrouper par (nom, famille d'unité) ; à l'affichage, repasser en kg au-delà de 1 000 g et en l au-delà de 1 000 ml, cl sinon conservés tels quels si l'entrée était en cl. Les unités non convertibles (pièce, sachet, c. à s.) restent regroupées par unité exacte. (B6)
  3. Nouvelle fonction `round_for_shopping(item)` appliquée en sortie de `merge_ingredients` : denrée sans unité ou en unité de comptage → arrondi à l'entier supérieur ; grammes et millilitres → arrondi à 5 près au-dessus de 50. Ne pas l'appliquer dans `scale_ingredients` (la fiche recette garde les valeurs exactes). (B9)
  4. `categorize` : remplacer `if m in n` par une recherche sur limite de mot (`re.search(rf"\b{re.escape(m)}s?\b", n)`), en gardant la règle « le mot-clé le plus long gagne ». Compiler les motifs une fois au chargement. (B8)
  5. `group_by_rayon` : un rayon inconnu retombe dans « Autre » au lieu d'être perdu (`app/categories.py:98-99`).
- **Critères d'acceptation**
  - `1 kg tomates` + `250 g tomates` → une ligne « 1,25 kg » (valeur `1.25`, unité `kg`). `0.5 l lait` + `25 cl lait` → une ligne de 750 ml ou 75 cl.
  - `0.5` poulet → `1`. `137 g` → `140 g`.
  - `categorize("blanc de volaille")` = « Viande & poisson » (ajouter « volaille » aux mots-clés) ; « gâteau de riz », « morceau de sucre » ne partent pas en Boissons ; « pomme de terre » reste en Fruits & légumes.
  - Les 4 lignes de B10 donnent quantité, unité et nom corrects.
  - Les tests existants de `tests/test_text_utils.py` passent sans modification, sauf ceux qui figeaient explicitement « deux lignes pour deux unités », à mettre à jour en le justifiant dans le commit.
- **Vérification** : `.venv/bin/python -m pytest tests/test_text_utils.py tests/test_courses_unites.py tests/test_categories.py`, puis la suite complète.

#### L03 — PWA : service worker et icônes

- **Branche** : `fix/pwa-hors-ligne-icones`
- **Objectif** : page de secours hors-ligne réelle, icônes installables.
- **Fichiers** : `app/static/sw.js`, `app/static/manifest.json`, `app/static/icon-192.png`, `app/static/icon-512.png`, `app/static/icon-180.png`, `app/static/offline.html` (nouveaux), `tests/test_pwa.py` (nouveau).
- **Étapes**
  1. `sw.js:66-72` : chaîner correctement (`caches.match(req).then(hit => hit || caches.match('/static/offline.html'))`). Pré-cacher `offline.html` ; retirer `/` de `PRECACHE` (avec l'auth, `/` répond par une redirection, inutilisable pour une navigation).
  2. Ne pas mettre en cache les réponses non-200 ni les redirections. Passer `CACHE` à `menu-planner-v3`.
  3. Générer les trois PNG depuis `icon.svg` (fond `#e67e22`). Manifest : entrées 192 et 512 en `purpose: "any"`, une entrée `maskable` distincte.
  4. Ne pas toucher `base.html` : le lien `apple-touch-icon` vers `/static/icon-180.png` est fait par L05.
- **Critères d'acceptation** : `GET /static/offline.html` et les trois icônes répondent 200 ; `manifest.json` est un JSON valide avec trois icônes PNG ; `sw.js` ne contient plus l'expression `caches.match('/') ||`.
- **Tests** : `tests/test_pwa.py` (statuts, JSON du manifest, présence de la page de secours dans `PRECACHE`).
- **Vérification manuelle** (à consigner dans le message de PR) : Chrome DevTools → Application → Service Workers, mode « Offline », navigation vers une page non visitée.

#### L04 — Outillage, README, durcissement LLM

- **Branche** : `chore/outillage-readme`
- **Objectif** : README fidèle au produit, lint en CI, deux failles LLM fermées.
- **Fichiers** : `README.md`, `pyproject.toml` (nouveau), `requirements-dev.txt`, `.github/workflows/ci.yml`, `.env.example`, `app/llm_client.py`, `tests/test_llm_securite.py` (nouveau).
- **Étapes**
  1. `pyproject.toml` : section `[tool.pytest.ini_options]` (`testpaths = ["tests"]`, filtre de l'avertissement Starlette en attendant L07) et `[tool.ruff]` (cible py312, règles `E`, `F`, `I`, `B`, `UP`, longueur de ligne 120, **sans** reformatage de masse). Si des règles échouent sur le code existant hors `llm_client.py`, les lister en `per-file-ignores` : le nettoyage est fait par L07.
  2. `requirements-dev.txt` : ajouter `ruff`. CI : étape `ruff check app tests scripts` avant les tests.
  3. README : réécrire « Fonctionnalités », « Guide d'utilisation » et « Architecture » pour décrire le constructeur manuel, les brouillons, les briques, les repas libres, le partage de liste, l'auth par session. Ajouter une section « Lancer en local » avec `DATABASE_PATH=./dev.db`. Corriger le nom du dossier (`app-recettes/` → `RecetteApp/`) et la mention « HTTP Basic ».
  4. `llm_client.py` : envoyer la clé Gemini par l'en-tête `x-goog-api-key` au lieu du paramètre d'URL (`:643-646`, `:845`). Faire passer `extract_ingredients` par `_safe_fetch_html` (`:870-871`). Dans `_safe_fetch_html`, désactiver `follow_redirects` et suivre à la main 3 redirections au plus en revalidant chaque cible avec `_is_public_http_url`.
- **Critères d'acceptation** : `ruff check` passe ; la CI exécute lint puis tests ; aucune URL construite par `LLMClient` ne contient la clé ; une URL publique qui redirige vers `http://127.0.0.1` renvoie `""`.
- **Tests** : `tests/test_llm_securite.py` avec un transport `httpx.MockTransport`.
- **Vérification** : `.venv/bin/ruff check app tests scripts && .venv/bin/python -m pytest`.

#### L05 — Correctifs d'interface planning et constructeur

- **Branche** : `fix/ui-planning-mobile-a11y`
- **Objectif** : corriger les défauts visibles et l'échappement, poser des jetons CSS cohérents.
- **Fichiers** : `app/static/style.css`, `app/templates/base.html`, `app/templates/planning.html`, `app/templates/index.html`, `app/templates/historique.html`, `app/templates/courses_public.html`, `tests/test_ui_gabarits.py` (nouveau).
- **Étapes**
  1. `style.css:2-7` : définir `--bd`, `--bg2`, `--bg3`, `--t1`, `--pri`, `--card` comme alias des jetons existants. Supprimer le CSS mort listé en §1.5 après un `grep` de chaque classe dans `app/templates/`. Retirer la règle `.group-picker` de `style.css:59` (celle d'`index.html` suffit). (B27)
  2. Ajouter un style `:focus-visible` commun (contour 2 px `var(--pr)`) pour boutons, liens, cases ; garder la bordure colorée des champs.
  3. Cibles tactiles : `min-width`/`min-height` de 40 px pour `.b-x`, `.side-ch`, `.side-rm`, `.meal-ch`, `.star` sous 768 px.
  4. `planning.html:8` et `historique.html:33` : retirer la saison ; afficher « N pers. » seul. Retirer les blocs `ingredients_force`, `creneaux_non_resolus`, `briques_manquantes` (`planning.html:9-27`). (B4)
  5. `planning.html:270-283` : barre d'outils des courses en `flex-wrap`, titre sur sa propre ligne sous 640 px. (B14)
  6. `planning.html:507-524` : construire la liste des alternatives avec `document.createElement` et `textContent` (plus de `innerHTML` avec des noms). Supprimer la fonction morte `usl`. (B17)
  7. `planning.html:315-320` : texte « Aucun ingrédient pour l'instant » quand la liste est vide, avec le bouton de génération. (B26)
  8. Modales (`planning.html:162`, `index.html:140`) : `role="dialog"`, `aria-modal="true"`, `aria-labelledby`, fermeture par Échap, focus rendu à l'élément déclencheur. Ajouter un `aria-label` à chaque bouton à pictogramme.
  9. `base.html` : libellé « Planifier » au lieu de « Générer » ; onglet actif « Planifier » sur `/` uniquement, « Historique » sur `/historique` et `/planning/…` ; `apple-touch-icon` vers `/static/icon-180.png`. `historique.html:44-45` : « Aucun planning pour le moment » / « Construire une semaine ».
  10. `index.html:652` : remplacer `alert()` par `toast(…, 'error')`. Barre `.build-actions` en `position: sticky; bottom: 0` sous 700 px.
  11. `courses_public.html:19` : utiliser `var(--text)`.
- **Critères d'acceptation**
  - Le HTML de `/planning/<id>` ne contient plus « —  — ».
  - Une recette nommée `Salade "César" <b>x</b>` apparaît intacte dans la modale et se sélectionne correctement (vérification manuelle ou par L16).
  - En 390 px, aucun débordement horizontal sur `/planning/<id>` et `/`.
  - Échap ferme les deux modales.
- **Tests** : `tests/test_ui_gabarits.py` vérifie par requêtes HTTP l'absence de « —  — », la présence de `role="dialog"`, du libellé « Planifier », et que chaque `var(--x)` utilisé dans `app/templates/` et `style.css` est défini dans `:root` (test par expression régulière sur les fichiers).
- **Vérification** : suite complète, puis captures en 1280 px et 390 px des pages `/`, `/planning/<id>`, `/historique` jointes à la PR.

#### L06 — Recettes : accessibilité et code mutualisé

- **Branche** : `fix/recettes-a11y-notation`
- **Objectif** : liste et fiche utilisables au clavier, notation et éditeur d'étapes en un seul exemplaire.
- **Fichiers** : `app/templates/recettes.html`, `app/templates/recette_detail.html`, `app/templates/ajouter.html`, `app/templates/enrichir.html`, `app/static/js/rating.js`, `app/static/js/steps-editor.js` (nouveaux), `tests/test_recettes_gabarits.py` (nouveau).
- **Étapes**
  1. `rating.js` : la logique de `recettes.html:174-220`, avec des `<button type="button" aria-label="Noter 3 sur 5" aria-pressed>` à la place des `<span>`. Inclure le script dans les deux pages et supprimer les deux copies.
  2. `steps-editor.js` : `renumberSteps`, `bindDel`, `addStepRow` ; l'inclure dans `ajouter.html` et `enrichir.html`.
  3. `recettes.html:79-81` : le titre devient un lien `<a href="/recette/{id}">` étiré sur la carte (pseudo-élément `::after`), suppression du gestionnaire de clic sur le `div`. Chips de filtre en `<button>` avec `aria-pressed`.
  4. `recettes.html:135-155` : temporisation de 120 ms sur la recherche.
  5. `ajouter.html:115` : placeholder « Un ingrédient par ligne : 200 g de farine ». (B11)
  6. `recette_detail.html:93` : `role="dialog"`, Échap, comme en L05.
- **Critères d'acceptation** : Tab atteint chaque recette puis ses étoiles ; Entrée ouvre la fiche ; la notation fonctionne sur les deux pages ; plus aucun bloc de notation inline dans les templates.
- **Tests** : `tests/test_recettes_gabarits.py` (présence des `<script src="/static/js/rating.js">`, des liens de carte, absence de `data-href`) ; `GET /static/js/rating.js` → 200.
- **Vérification** : suite complète ; contrôle manuel au clavier.

---

### Vague 2 — deux refontes de structure, en parallèle entre elles

Ces deux lots ne changent aucun comportement. Ils existent pour que la vague 3 puisse tourner en parallèle. Aucun autre lot ne doit être ouvert pendant cette vague.

#### L07 — Découpage de `main.py`

- **Branche** : `refactor/routeurs-services`
- **Dépend de** : L01, L04 (et L02 fusionné, pour partir du code à jour).
- **Fichiers** : `app/main.py`, nouveaux `app/deps.py`, `app/services/planning.py`, `app/services/shopping.py`, `app/services/nutrition_week.py`, `app/routers/pages.py`, `app/routers/planning.py`, `app/routers/courses.py`, `app/routers/recettes.py`, `app/routers/catalogue.py`, `app/routers/auth.py`, `app/database.py`, `app/notion_client.py` ; tous les fichiers de `tests/` (imports uniquement).
- **Étapes**
  1. `app/deps.py` : instances uniques `db`, `notion`, `llm`, `templates`, constantes `BASE_SERVINGS`, `RATED_STATUS`. `app/main.py` les ré-exporte (`from app.deps import db, notion, llm`) : les tests font `main.notion.<méthode> = …` et `main.db.path = …` sur les instances, ce qui continue de fonctionner tant que les instances sont partagées. Vérifié : aucun test ne fait `monkeypatch.setattr(main, "<nom>", …)`.
  2. Déplacer sans réécrire :
     - `services/planning.py` : `plat_accompagnements`, `_apply_recipe_info`, `_group_week`, `_day_labels`, `_week_label`, `_parse_builder_meals`, `_build_plat`, `_build_side`, `_repas_libres`, `_builder_from_plats`, `_per_day_list` ;
     - `services/shopping.py` : `_collect_shopping`, `_refresh_shopping_and_save`, `_ingredients_to_text`, `_ing_lines` ;
     - `services/nutrition_week.py` : `_week_nutrition`.
  3. Un `APIRouter` par domaine : `pages` (`/`, `/historique`, `/health`, `/sw.js`), `planning` (`/construire`, `/planning/*`, `/api/update-meal`, `/api/free-meal`, `/api/*-side`, `/api/brique`, `/api/alternatives`, `/api/planning/*/apercu`), `courses` (`/generate-shopping`, `/api/shopping-check`, `/planning/*/partager`, `/courses/*`), `recettes` (`/recettes`, `/recette/*`, `/ajouter*`, `/api/rate`, `/api/analyze-*`, `/api/enrich*`), `catalogue` (`/api/catalogue`), `auth` (`/login`, `/logout`). `main.py` ne garde que l'application, les middlewares et les `include_router`.
  4. Supprimer les routes sans appelant : `/api/enrich/{id}`, `/api/enrich-all` (version non-SSE), `/api/update-side`, et leurs tests. Supprimer `app/cooklang.py` et `tests/test_cooklang.py` **seulement si** la question Q5 (§4) est tranchée dans ce sens.
  5. Passer tous les `TemplateResponse` à la signature `TemplateResponse(request, name, context)` ; retirer le filtre d'avertissement posé en L04.
  6. `notion_client.py` : un `httpx.AsyncClient` partagé, créé à la demande et fermé dans le `lifespan`. `database.py` : factoriser l'insertion de `planning_recipes` dupliquée (`:113-126` et `:146-159`).
  7. Retirer les `per-file-ignores` de ruff posés en L04 et corriger les avertissements.
  8. `database.py` : ajouter `get_app_prefs() -> dict` et `save_app_prefs(data: dict)` sur la table `app_prefs` existante (réglages uniques du foyer : placard, ordre des rayons), avec leurs tests. L09 en a besoin.
- **Critères d'acceptation** : la liste des routes (`app.routes`) est identique avant et après, hors routes supprimées à l'étape 4 ; `app/main.py` fait moins de 150 lignes ; aucun fichier de `app/routers/` ne dépasse 500 lignes ; 0 avertissement de dépréciation aux tests.
- **Tests** : `tests/test_routes_inventaire.py` (nouveau) fige l'ensemble des couples (méthode, chemin). Aucun test métier réécrit ; seuls les imports changent.
- **Vérification** : `.venv/bin/ruff check app tests scripts && .venv/bin/python -m pytest --cov=app --cov-fail-under=65 -W error::DeprecationWarning`.

#### L08 — Découpage des templates et du JS

- **Branche** : `refactor/templates-partiels-js`
- **Dépend de** : L05, L06.
- **Parallèle avec L07** : oui (L07 ne touche que du Python, L08 que `app/templates/` et `app/static/`). Seul point de contact : les noms de templates et les variables de contexte, qui ne changent pas.
- **Fichiers** : `app/templates/planning.html`, `app/templates/index.html`, `app/templates/courses_public.html`, nouveaux `app/templates/partials/_week_grid.html`, `_week_list.html`, `_meal.html`, `_nutrition.html`, `_shopping.html`, `_modal_change.html`, `_picker.html` ; nouveaux `app/static/js/common.js`, `planning-week.js`, `planning-shopping.js`, `builder.js`, `builder-picker.js` ; `app/static/css/builder.css` (nouveau).
- **Étapes**
  1. `planning.html` devient un assemblage d'`{% include %}`. La macro `meal_inner` va dans `_meal.html`.
  2. JS de `planning.html` réparti en `planning-week.js` (sélection, modale, accompagnements, repas libre, brique) et `planning-shopping.js` (coches, file d'attente, copie, partage, génération). Renommer `c`, `u`, `la`, `rpl`, `bul` en `closeModal`, `updateBulkBar`, `loadAlternatives`, `replaceMeals`, `selection`.
  3. Les données serveur passent par un seul bloc `<script type="application/json" id="page-data">` lu au chargement, au lieu d'interpolations Jinja dispersées dans le JS.
  4. `common.js` : `esc`, `copyText`, `postJson`. Utilisé par les pages planning et courses publiques (suppression des copies).
  5. `index.html` : styles vers `css/builder.css`, JS vers `builder.js` (état, grille, groupes, période, sérialisation) et `builder-picker.js`. Les fonctions pures (`consolidateGroups`, `midiEditorDay`, `serialize`, calcul de période) sont écrites sans accès au DOM et exportées sur `window.Builder` pour être testables par L16.
  6. Ajouter les nouveaux fichiers statiques au pré-cache de `sw.js` **n'est pas** dans ce lot (fait par L15).
- **Critères d'acceptation** : `planning.html` et `index.html` font moins de 120 lignes chacun ; plus aucun gestionnaire `onclick="…"` inline ; rendu identique en 1280 px et 390 px (captures avant/après jointes à la PR) ; parcours manuels identiques : construire, enregistrer un brouillon, valider, changer un repas, ajouter un accompagnement, cocher un article, copier la liste.
- **Tests** : la suite existante suffit pour le HTML ; ajouter `tests/test_static_js.py` (chaque script référencé par un template répond 200).
- **Vérification** : suite complète ; `node --check app/static/js/*.js` si Node est disponible.

---

### Vague 3 — cinq lots de fonctionnalités en parallèle

Répartition des fichiers, sans recouvrement :

| Lot | Python | Templates | Statique |
|---|---|---|---|
| L09 | `routers/courses.py`, `services/shopping.py` | `partials/_shopping.html`, `courses_public.html` | `js/planning-shopping.js`, `js/courses-public.js` (nouveau) |
| L10 | `routers/planning.py`, `services/planning.py`, `routers/pages.py` | `partials/_week_grid.html`, `_week_list.html`, `_meal.html`, `_modal_change.html`, `historique.html` | `js/planning-week.js` |
| L11 | `routers/recettes.py`, `notion_client.py` | `recettes.html`, `recette_detail.html`, `enrichir.html`, `ajouter.html` | `js/recette-planifier.js` (nouveau) |
| L12 | `routers/catalogue.py` | `index.html`, `partials/_picker.html` | `js/builder.js`, `js/builder-picker.js`, `css/builder.css` |
| L13 | `routers/admin.py` (nouveau), `services/backup.py` (nouveau), `database.py` | `admin.html` (nouveau), `base.html` | — |

`app/main.py` : chaque lot qui crée un routeur ajoute une ligne `include_router`. Seul L13 en crée un ; les autres n'y touchent pas. `style.css` : aucun lot de la vague 3 ne le modifie ; les styles nouveaux vont dans un fichier CSS propre au lot (`css/courses.css`, `css/planning.css`, `css/recettes.css`, `css/admin.css`).

#### L09 — Courses : coche publique, articles manuels, placard

- **Branche** : `feat/courses-partage-interactif`
- **Dépend de** : L02, L07, L08.
- **Étapes**
  1. Clé d'article = `nom normalisé|famille d'unité`, calculée côté serveur par une fonction `item_key(item)` dans `services/shopping.py` et écrite dans chaque article (`key`). Lecture tolérante : une ancienne coche stockée par nom seul coche toutes les lignes de ce nom. (B7)
  2. `POST /courses/{token}/check` : même corps que `/api/shopping-check`, autorisé par le jeton, limité à cocher/décocher. `GET /courses/{token}/etat` : `{checked: [...], version: n}` pour le rafraîchissement.
  3. `courses_public.html` : cases à cocher actives, articles cochés repliés en bas sous « Déjà pris (n) », rafraîchissement toutes les 20 s quand l'onglet est visible, grandes cibles tactiles (48 px).
  4. Articles manuels : `planning_data["courses_extra"] = [{nom, quantite, unite, rayon, key}]`. `POST /api/courses/{planning_id}/extra` (ajout), `DELETE /api/courses/{planning_id}/extra/{key}`. Champ de saisie rapide en tête de liste (« 2 l lait » passe par `parse_ingredient_line`). Les extras survivent à la régénération de la liste.
  5. Retrait d'une ligne issue d'une recette : `planning_data["courses_masques"] = [key]`, bouton « Je l'ai déjà », section repliée « Masqués (n) » avec restauration.
  6. Placard : liste de clés d'articles stockée dans la ligne unique de la table `app_prefs` (`app/database.py:63-66`), lue et écrite par `Database.get_app_prefs` / `save_app_prefs` (livrées par L07). L09 ne modifie pas `database.py`, qui appartient à L13 dans cette vague.
  7. Bouton « Toujours à la maison » sur une ligne → ajout au placard ; les articles du placard sont rangés d'office dans « Masqués ».
- **Critères d'acceptation**
  - Deux lignes de même nom et d'unités incompatibles se cochent indépendamment.
  - Cocher depuis `/courses/<token>` se reflète sur `/planning/<id>` après rechargement, et inversement en moins de 20 s.
  - Un jeton invalide renvoie 404 sur les trois routes publiques. Aucune route publique ne permet de modifier autre chose que les coches.
  - Un article manuel reste présent après « Régénérer la liste ».
  - « Sel » mis au placard n'apparaît plus dans la liste d'un nouveau planning, mais reste accessible sous « Masqués ».
- **Tests** : `tests/test_courses_partage.py`, `tests/test_courses_extra.py`, `tests/test_placard.py` (routes, compatibilité des anciennes coches, auth activée : les routes `/courses/…` restent publiques, `/api/courses/…` non).
- **Vérification** : suite complète ; essai manuel à deux navigateurs.

#### L10 — Planning : période, édition groupée, réouverture

- **Branche** : `feat/planning-edition`
- **Dépend de** : L07, L08.
- **Étapes**
  1. `construire` enregistre `data["nb_jours"]` (dernier jour de la période). `voir_planning` le passe au template ; repli pour les anciens plannings : dernier jour porteur d'un plat. La grille et la liste n'affichent que les jours `1..nb_jours` ; `grid-template-columns` suit. « Absent » ne reste que pour les créneaux à 0 convive dans la période. (B5)
  2. `POST /api/planning/{id}/meals` : corps `{slots: [{jour, moment}], action: "set_main"|"add_side"|"remove_side", nom}`. Applique toutes les mutations, régénère la liste une seule fois, écrit une seule fois. `planning-week.js` l'utilise à la place des boucles d'appels. Les anciens endpoints unitaires restent, marqués dépréciés dans leur docstring. (B18)
  3. `GET /?planning=<id>` : ouvre n'importe quel planning (validé ou non) dans le constructeur. À l'enregistrement d'un planning validé, il reste validé. Bouton « ✏️ Modifier dans le constructeur » sur la page planning.
  4. Historique : bouton « Supprimer » sur les semaines validées (confirmation par modale, plus de `confirm()`), aperçu accessible au toucher (bouton « 👁 » qui déplie l'aperçu dans la carte), pagination « Voir plus » au-delà de 20.
  5. Option (si Q6 = oui) : `GET /planning/{id}/menu.ics`, un événement par repas (12 h 30 et 19 h 30, durée 1 h), nom du plat et accompagnements.
- **Critères d'acceptation** : un planning de 4 jours affiche 4 colonnes ; remplacer le plat d'une case fusionnée de 3 jours émet une seule requête et la base ne contient jamais un état intermédiaire (test : erreur simulée → aucun jour modifié) ; une semaine validée modifiée dans le constructeur reste dans « Semaines validées ».
- **Tests** : `tests/test_planning_periode.py`, `tests/test_planning_batch.py`, `tests/test_planning_reouverture.py`.
- **Vérification** : suite complète ; captures 1280/390 px d'un planning de 3 jours.

#### L11 — Recettes : édition directe, ajout au planning

- **Branche** : `feat/recettes-edition-planning`
- **Dépend de** : L07, L08 (et L01 pour `get_recipe_usage`).
- **Étapes**
  1. `GET /recette/{id}/enrichir` : pré-remplit depuis le cache local et Notion, **sans** appel réseau à la source. `?refetch=1` (bouton « 🔄 Ré-extraire depuis la source », avec avertissement « remplace les champs ci-dessous ») déclenche l'extraction actuelle. Si la recette n'a aucun ingrédient en cache et une URL, l'extraction reste automatique. (B20)
  2. Champs Moment et État ajoutés au formulaire d'édition (`notion_client.update_recipe_meta` étendu). `tags=[]` vide réellement les tags. (B22)
  3. `POST /ajouter-recette` : en cas d'erreur, re-rendre le formulaire à l'étape 2 avec toutes les valeurs saisies. (B21)
  4. `notion_client.update_ingredients` : ne plus forcer l'icône 🥗 si la page en a déjà une ; découper le texte en plusieurs segments `rich_text` de 2 000 caractères au lieu de tronquer. `get_recipe_instructions` : paginer au-delà de 100 blocs et accepter les paragraphes. (B24, B25)
  5. Fiche recette : bouton « 📅 Ajouter au planning » → modale listant les brouillons (`db.list_drafts`) + « Nouveau brouillon », choix du jour et du moment, puis `POST /api/planning/{id}/meals` (L10). **Dépendance croisée avec L10** : si L10 n'est pas encore fusionné, utiliser `/api/update-meal`, qui existe déjà ; le créneau doit exister, donc pour un créneau vide passer par un nouveau `POST /api/recette/{id}/planifier` dans `routers/recettes.py` qui appelle les fonctions de `services/planning.py` en lecture seule (sans modifier ce fichier).
  6. Fiche et liste : « Cuisinée 3 fois · dernière fois le 12/10 » à partir de `db.get_recipe_usage()` ; tri « Pas faite depuis longtemps » et « Mieux notées » dans la liste.
- **Critères d'acceptation** : ouvrir « Modifier » ne déclenche aucun appel à `llm.extract_recipe_from_url` (test avec un simulacre qui lève) ; une erreur Notion à l'ajout conserve nom, ingrédients, étapes et tags dans le formulaire ; une recette ajoutée depuis sa fiche apparaît dans le brouillon choisi.
- **Tests** : `tests/test_recettes_edition.py`, `tests/test_recette_planifier.py`, `tests/test_notion_ecritures.py` (corps des requêtes via `httpx.MockTransport` : relève la couverture de `notion_client`).
- **Vérification** : suite complète.

#### L12 — Constructeur : copier, restes, picker enrichi

- **Branche** : `feat/constructeur-ux`
- **Dépend de** : L01 (`get_recipe_usage`), L07, L08.
- **Étapes**
  1. `/api/catalogue` : ajouter `tags`, `note`, `etat`, `duree` (cache), `usage` (`count`, `last`). Renvoyer `{"error": …}` avec un statut 502 quand Notion échoue, au lieu d'une liste vide.
  2. Picker : état « Chargement du catalogue… », message d'erreur avec « Réessayer », temporisation de 120 ms sur la recherche, rendu par lots de 60 cartes avec « Afficher plus ». Cartes en `<button>`. Filtres supplémentaires : tags Rapide / saison en cours, « À essayer », « Pas faite depuis 4 semaines ». Badge « il y a 2 sem. » sur les recettes récentes ; tri par défaut : jamais planifiées et anciennes d'abord, puis par note.
  3. Menu « ⋯ » sur chaque case remplie : « Copier vers… » (liste des cases de la période), « Déplacer vers… », « Restes → midi suivant » (copie le repas sur le midi du lendemain et rattache ce midi à un groupe propre).
  4. Alerte `beforeunload` quand la grille a changé depuis le dernier enregistrement ; désactivée à la soumission.
  5. Ajustement des convives par boutons − / + (48 px) en mobile au lieu du champ numérique seul.
- **Critères d'acceptation** : ouvrir le picker avant la fin du chargement affiche l'état d'attente puis la liste ; « Restes » sur mardi soir remplit mercredi midi avec le même plat et les mêmes accompagnements ; fermer l'onglet avec une grille modifiée déclenche l'alerte du navigateur.
- **Tests** : `tests/test_catalogue_api.py` (champs, statut d'erreur). Les fonctions pures de `builder.js` sont couvertes par L16.
- **Vérification** : suite complète ; parcours manuel en 390 px.

#### L13 — Sauvegarde et restauration des données

- **Branche** : `feat/sauvegarde-donnees`
- **Dépend de** : L07.
- **Étapes**
  1. `services/backup.py` : `export_all()` → JSON versionné (`{"version": 1, "exported_at", "plannings": [...], "enriched": [...], "prefs": {...}}`) ; `import_all(data, mode="merge"|"replace")` dans une transaction.
  2. `routers/admin.py` : `GET /admin` (page), `GET /admin/export` (téléchargement `menu-planner-AAAA-MM-JJ.json`), `POST /admin/import` (fichier + mode, avec récapitulatif avant validation). Routes protégées par l'auth quand elle est activée ; lien « Données » dans le pied de page de `base.html`.
  3. Réhydratation depuis Notion : `notion_client._parse_page` lit la propriété « Ingrédients » ; `POST /admin/rehydrater` remplit `enriched_recipes` pour les recettes absentes du cache en passant chaque ligne par `parse_ingredient_line`. N'écrase jamais une entrée existante.
  4. `Database` : colonne `share_token` indexée sur `planning_history` (migration idempotente comme `app/database.py:72-89`, remplie depuis `data_json` au premier démarrage) ; `get_planning_by_share_token` devient une requête directe.
  5. Script `scripts/backup.py` utilisable en cron dans le conteneur (`python -m scripts.backup /data/backups`), rotation sur 8 fichiers.
- **Critères d'acceptation** : export puis import en mode « replace » sur une base vide restitue des pages planning et des listes de courses identiques ; l'import d'un fichier invalide ne modifie rien ; après suppression de `enriched_recipes`, la réhydratation restaure les ingrédients des recettes dont Notion a la copie texte.
- **Tests** : `tests/test_backup.py`, `tests/test_rehydratation.py`, `tests/test_share_token_migration.py`.
- **Vérification** : suite complète ; `docker compose build app`.

---

### Vague 4 — trois lots en parallèle

#### L14 — Mode cuisine

- **Branche** : `feat/mode-cuisine`
- **Dépend de** : L11.
- **Fichiers** : `app/routers/cuisine.py` (nouveau), `app/main.py` (une ligne), `app/templates/cuisine.html` (nouveau), `app/static/js/cuisine.js`, `app/static/css/cuisine.css` (nouveaux), `app/templates/recette_detail.html` (un bouton), `tests/test_mode_cuisine.py`.
- **Étapes**
  1. `GET /recette/{id}/cuisine?portions=N` : page plein écran sans navigation, fond clair, texte ≥ 1,25 rem.
  2. Deux volets : ingrédients cochables (état en `sessionStorage`), étapes une par une avec « Précédent / Suivant » en bas (zones de 64 px), balayage horizontal, barre de progression.
  3. `navigator.wakeLock.request('screen')` à l'ouverture, relancé au retour de visibilité ; repli silencieux si l'API est absente (contexte non sécurisé).
  4. Détection des durées dans le texte d'une étape (`(\d+)\s*(min|minutes|h|heures)`) → bouton « ⏱ 20 min » qui lance un minuteur affiché en haut, avec vibration et son à la fin ; plusieurs minuteurs simultanés.
  5. Les quantités d'ingrédients citées dans la liste suivent `portions`.
- **Critères d'acceptation** : la page s'ouvre depuis la fiche avec le nombre de portions courant ; une étape « Cuire 20 min » propose un minuteur de 20:00 ; la page fonctionne sans erreur console en HTTP simple.
- **Tests** : rendu de la route (étapes, ingrédients mis à l'échelle), 404 sur recette inconnue ; extraction des durées testée par L16 ou par un test Node si disponible.

#### L15 — Courses hors-ligne

- **Branche** : `feat/courses-hors-ligne`
- **Dépend de** : L03, L09. **Prérequis d'exploitation** : l'app doit être servie en HTTPS (voir Q2), sinon le service worker ne s'enregistre pas et ce lot n'apporte rien.
- **Fichiers** : `app/static/sw.js`, `app/static/js/courses-public.js`, `app/templates/courses_public.html`, `tests/test_pwa_courses.py`.
- **Étapes** : enregistrement du service worker sur la page publique ; pré-cache des CSS et JS de la page ; stratégie « réseau puis cache » pour `/courses/<token>` et `/courses/<token>/etat` ; file d'attente des coches en `localStorage`, rejouée au retour du réseau (événement `online` + au chargement) ; bandeau « Hors ligne — tes coches seront envoyées au retour du réseau ».
- **Critères d'acceptation** : page publique ouverte une fois en ligne, puis réseau coupé : la page se recharge, les coches sont possibles, et elles arrivent au serveur au retour du réseau, sans doublon ni perte.
- **Vérification** : essai manuel DevTools « Offline », consigné dans la PR ; test automatisé dans L16 si celui-ci est fusionné.

#### L16 — Tests de bout en bout

- **Branche** : `test/e2e-parcours`
- **Dépend de** : L09 à L12.
- **Fichiers** : `tests/e2e/` (nouveau : `conftest.py`, `fake_notion.py`, `test_parcours_*.py`), `requirements-dev.txt`, `.github/workflows/ci.yml`, `pyproject.toml` (marqueur `e2e`).
- **Étapes**
  1. `pytest-playwright`. `conftest.py` démarre uvicorn sur un port libre avec une base temporaire et un `NotionClient` simulé en mémoire (catalogue de 10 recettes dont une au nom piégé `Salade "César" <b>x</b>`).
  2. Parcours :
     - construire 4 jours avec un groupe de midis, enregistrer en brouillon, rouvrir, changer la date, valider → titres et jours corrects ;
     - changer le plat d'une case fusionnée, ajouter un accompagnement libre, choisir la recette au nom piégé ;
     - générer la liste, cocher, ouvrir le lien public dans un second contexte, cocher depuis le lien, vérifier la synchronisation ;
     - « Refaire » une semaine puis « Reprendre » → grille pré-remplie ;
     - viewport 390 × 844 : pas de défilement horizontal sur `/`, `/planning/<id>`, `/courses/<token>`.
  3. Fonctions pures de `window.Builder` testées par `page.evaluate` (groupes, sérialisation, période).
  4. CI : job séparé `e2e` (`playwright install --with-deps chromium`), non bloquant pendant deux semaines puis bloquant.
- **Critères d'acceptation** : `pytest -m e2e` passe en local et en CI en moins de 2 minutes ; chaque bug B1, B2, B3, B5, B7, B17 a un test qui échouerait sur le commit `0715e80`.
- **Vérification** : `.venv/bin/python -m pytest -m e2e`.

---

### 3.3 Ordre d'exécution

| Vague | Lots (en parallèle) | Condition pour passer à la suivante |
|---|---|---|
| **1** | L01, L02, L03, L04, L05, L06 | Les six fusionnés dans `main`, suite verte |
| **2** | L07, L08 | Les deux fusionnés ; aucun autre lot ouvert pendant cette vague |
| **3** | L09, L10, L11, L12, L13 | Fusion dans l'ordre conseillé L13 → L10 → L09 → L11 → L12 (L11 profite de l'endpoint groupé de L10) |
| **4** | L14, L15, L16 | — |

Conflits résiduels connus, à surveiller à la fusion :

- **Vague 1** : aucun fichier partagé. L05 référence `/static/icon-180.png` créé par L03 : fusionner L03 avant L05, ou accepter une icône manquante entre les deux.
- **Vague 2** : L07 et L08 sont disjoints. L07 doit partir d'un `main` contenant L01, L02 et L04.
- **Vague 3** : `app/main.py` n'est modifié que par L13 (une ligne). L11 étend `notion_client.py`, L13 modifie `_parse_page` dans le même fichier : zones différentes, fusion simple attendue.
- **Vague 4** : `sw.js` (L15) et `recette_detail.html` (L14) ne sont touchés par aucun autre lot de la vague.

Si la vague 2 est refusée (voir Q1), les lots L09 à L12 doivent s'exécuter **l'un après l'autre** : ils modifient tous `app/main.py` et `planning.html` ou `index.html`.

---

## 4. Décisions attendues avant de lancer les agents

| # | Question | Pourquoi ça compte | Option par défaut proposée |
|---|---|---|---|
| Q1 | Accepter la vague 2 (découpage de `main.py` et des templates) ? | Sans elle, pas de parallélisme en vague 3. Avec elle, deux grosses PR sans gain visible, à relire avec soin. | Oui |
| Q2 | L'app est-elle servie en HTTPS, ou en HTTP par IP locale ? | En HTTP, le service worker ne s'enregistre pas : L03 est cosmétique et L15 inutile. Le verrou d'écran du mode cuisine (L14) ne marche pas non plus. | Si HTTP : ajouter un lot d'exploitation (reverse proxy avec certificat) avant L15, ou retirer L15 |
| Q3 | La personne qui reçoit le lien public peut-elle cocher (C1) ? Peut-elle aussi ajouter des articles ? | Le jeton devient un droit d'écriture limité. | Cocher : oui. Ajouter : non |
| Q4 | Arrondi des quantités (L02) : arrondir au supérieur les denrées comptées et à 5 g près ? | Change les chiffres de toutes les listes. | Oui, dans la liste de courses uniquement, pas sur la fiche recette |
| Q5 | Supprimer `app/cooklang.py` (utilisé par ses seuls tests) et le bloc « Format Cooklang » de la fiche ? | Le README met Cooklang en avant ; le code ne s'en sert plus. | Supprimer le module, garder le bloc d'export de la fiche |
| Q6 | Export `.ics` du menu (P7) : utile ? | Petit ajout à L10. | Non, sauf demande |
| Q7 | Notion reste-t-il la source des recettes ? | Tout le plan le suppose. L13 réduit la dépendance sans la lever. Une bascule vers SQLite seul serait un chantier à part. | Oui, Notion reste la source |
| Q8 | Où stocker le placard et l'ordre des rayons : réglages uniques pour le foyer ? | L'app n'a pas de notion d'utilisateur. | Oui, réglages uniques |
| Q9 | Les colonnes `saison` et `ingredients_force` peuvent-elles être ignorées définitivement ? | Elles restent en base (SQLite ne les supprime pas simplement) mais disparaissent de l'affichage. | Oui |
| Q10 | Les agents peuvent-ils pousser leurs branches et ouvrir des PR, ou s'arrêtent-ils au commit local ? | La consigne actuelle du projet interdit push et PR sans accord. | Commit local, puis accord lot par lot |

## 5. Décisions retenues (26/11/2026)

| # | Décision |
|---|---|
| Q1 | Vague 2 acceptée. |
| Q2 | Mode de service non garanti : L03 et L15 sont faits, mais tout doit se dégrader proprement en HTTP (aucune erreur console, fonctions désactivées sans bruit). |
| Q3 | Lien public : cocher oui, ajouter non. |
| Q4 | Arrondi appliqué dans la liste de courses uniquement. |
| Q5 | Supprimer `app/cooklang.py` et ses tests ; garder le bloc d'export de la fiche. |
| Q6 | Pas d'export `.ics`. |
| Q7 | Notion reste la source des recettes. |
| Q8 | Réglages uniques pour le foyer (`app_prefs`). |
| Q9 | `saison` et `ingredients_force` ignorées définitivement (restent en base). |
| Q10 | Commits locaux uniquement. Chaque lot sur sa branche, fusionné en local dans `integration/plan-amelioration`. Ni push ni PR. |
