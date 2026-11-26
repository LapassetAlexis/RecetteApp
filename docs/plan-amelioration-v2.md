# RecetteApp (Menu Planner) — deuxième audit et plan de développement v2

Date : 26/11/2026 — branche `main` à `c9fd426` (PR #69, lots L01 à L17 du premier plan), arbre propre.

## 0. Méthode et niveau de preuve

- **Lecture** de tout `app/` hors `llm_client.py` et `nutrition.py` (lus par extraits) : `main.py`, `deps.py`, `database.py`, les 8 routeurs, les 6 services, les 10 fichiers JS, `sw.js`, les templates et partiels. Lecture de `tests/e2e/` et survol des 42 fichiers de tests.
- **Commandes lancées** :
  - `.venv/bin/ruff check app tests scripts` → aucun défaut ;
  - `.venv/bin/python -m pytest -q` → 526 tests verts en 6 s, couverture Python 88 % ;
  - `.venv/bin/python -m pytest -q -m e2e` → 21 tests verts en 10 s.
- **App lancée** avec `tests/e2e/fake_notion.py` (Notion simulé, base SQLite temporaire) sur le port 8799, plus une seconde instance avec `AUTH_USER` / `AUTH_PASSWORD` sur le port 8798. Vérifications Playwright (Chromium) en 1280 px et 390 px : 15 pages capturées dans chaque largeur, mesure du débordement horizontal, erreurs console, taille des cibles, parcours au clic, hors-ligne avec un vrai service worker, 40 tours d'écritures concurrentes.
- Scripts, captures et sorties : `/private/tmp/claude-2007623921/-Users-alexis-lapasset-Projects-RecetteApp/5f3d40eb-92b6-4701-9532-1db54ce310a7/scratchpad/audit-v2/` (`a1.py` captures, `a2.py` parcours, `a3.py` hors-ligne, `race.py` concurrence, `shots/`).
- **Non vérifié** : le vrai Notion, les appels LLM réels, Safari / iOS, l'image Docker, le comportement en HTTP par IP locale (tout a tourné sur `127.0.0.1`, qui est un contexte sécurisé).
- **Incident de méthode** : un serveur du premier audit tourne encore sur le port 8765 (PID 40408, `serve.py` du scratchpad, base temporaire). Mon premier script l'a visé par erreur et y a créé deux plannings de test. Aucune donnée réelle n'est concernée ; je ne l'ai pas arrêté.

Marquage : **[V]** vérifié en exécutant, **[L]** lu dans le code, **[H]** hypothèse.

---

## 1. Livraison du premier plan

### 1.1 Bugs B1 à B27

**26 corrigés, 1 partiel, 0 non corrigé.**

| # | Statut | Preuve |
|---|---|---|
| B1 | fait | `week_start` et `nb_personnes` passés à `update_planning` (`app/routers/planning.py:149-155`, `app/database.py:193-199`) [L], e2e `test_brouillon_puis_changement_de_date_puis_validation` [V] |
| B2 | fait | `dupliquer_planning` copie ou reconstruit `builder` (`routers/planning.py:246-254`) [L], e2e `test_refaire_puis_reprendre` [V] |
| B3 | fait | `week_start = date.today()` (`routers/planning.py:243`) [L] |
| B4 | fait | en-tête « Mercredi 25/11 → samedi 28/11 — 2 pers. » (capture `planning_1280.png`) [V] |
| B5 | fait | planning de 4 jours : 4 colonnes, `--nb-jours:4` [V] |
| B6 | fait | `1 kg` + `250 g` de tomates → `1.25 kg` ; `0.5 l` + `25 cl` de lait → `75 cl` [V] |
| B7 | fait | clés `tomate\|` et `tomate\|masse` distinctes, coches indépendantes [V] |
| B8 | fait | « blanc de volaille » → Viande & poisson, « gâteau de riz » → Épicerie salée [V] |
| B9 | fait | `0.5` poulet → `1`, `137 g` → `140 g` [V] |
| B10 | fait | les 4 lignes du plan sont lues correctement [V]. Restes : voir N13 |
| B11 | fait | placeholder « Un ingrédient par ligne : 200 g de farine » (`ajouter.html:119`) [L] |
| B12 | fait | `/login?next=https://evil.example` et `?next=//evil.example` → 303 vers `/` [V] |
| B13 | fait | login avec « é » → 401 [V] |
| B14 | fait | aucun débordement horizontal en 390 px sur les 15 pages [V] |
| B15 | **partiel** | la grille est bien régénérée après une modification depuis la page planning [V], mais la régénération renumérote les groupes de midis : voir N3 |
| B16 | fait | upsert partiel `COALESCE` (`database.py:362-375`) [L] |
| B17 | fait | liste construite en `textContent` (`planning-week.js:94-118`) [L], e2e au nom piégé [V] |
| B18 | fait | `POST /api/planning/{id}/meals`, une écriture (`routers/planning.py:315-365`) [L][V]. Atomicité entre requêtes : voir N2 |
| B19 | fait | page non visitée hors ligne → `offline.html` servie [V] |
| B20 | fait | `_enrichir_context` n'extrait que sur `refetch`, `url` ou cache vide (`services/recettes.py:295`) [L] |
| B21 | fait | `_ajouter` renvoie `form` avec toute la saisie (`services/recettes.py:450-471`) [L] |
| B22 | fait | `if tags is not None` (`notion_client.py:573`) [L] |
| B23 | fait | `purge_orphans` (`services/shopping.py:294-302`) [L] |
| B24 | fait | icône posée seulement si absente, texte découpé en segments (`notion_client.py:19`, `:324-328`) [L] |
| B25 | fait | pagination des blocs et paragraphes acceptés (`notion_client.py:92-130`) [L] |
| B26 | fait | « Aucun ingrédient pour l'instant » (`_shopping_list.html:55-61`) [L] |
| B27 | fait | une seule règle `.group-picker` (`css/builder.css:60`) [L] |

### 1.2 Propositions

31 propositions : **25 faites, 4 partielles, 1 non faite, 1 écartée** (P7, export `.ics`, décision Q6).

| # | Statut | Remarque |
|---|---|---|
| A1 | fait | effet de bord sur les groupes (N3) |
| A2, A3, A4 | fait | |
| A5 | fait | le lot ne couvrait ni le CSRF ni les `str(e)` renvoyés au client (N7, N15) |
| A6, A7, A8 | fait | export, import en deux temps, réhydratation, script cron : en place [L], page `/admin` [V] |
| C1 | fait | coche depuis le lien, synchronisation, jeton invalide → 404 sur les trois routes [V] |
| C2 | **partiel** | le premier article s'ajoute, le second est impossible sans recharger la page (N1) [V] |
| C3 | fait | placard fonctionnel [L]. Pas d'écran pour le consulter ou le vider (§3.2) |
| C4 | fait | |
| C5 | fait | hors ligne : page rechargée, 2 coches mises en file, envoyées au retour du réseau, aucune perte [V] |
| C6 | **non fait** | ordre des rayons personnalisable : `RAYON_ORDER` reste une constante (`categories.py:10`). C'était une option |
| P1, P2, P4, P5, P6 | fait | |
| P3 | fait | réouverture et suppression en place. Libellés trompeurs dans le constructeur (N5) |
| P7 | écarté | Q6 |
| R1, R2, R4, R5, R6 | fait | |
| R3 | fait | fonctionne ; barre de progression bloquée après l'ajout (N1) et logique dupliquée (N8) |
| S1 | **partiel** | ruff, pytest, CI : faits. Le README, écrit en vague 1, est déjà faux sur les lots suivants (N10) |
| S2 | fait | `main.py` 76 lignes, `routers/planning.py` 500 lignes pile. La logique a glissé dans `services/` (§4) |
| S3 | **partiel** | `planning.html` (48 l.) et `index.html` (68 l.) découpés, 0 `onclick` inline. Il reste du JS inline dans 5 templates hors périmètre du lot (N8) |
| S4 | **partiel** | 21 tests e2e verts, mais job CI non bloquant (`ci.yml:36`) et parcours manquants (§4.3) |

---

## 2. Régressions et défauts nouveaux

### 2.1 Graves

| # | Défaut | Preuve | Emplacement |
|---|---|---|---|
| **N1** | **Le second article manuel ne peut pas être ajouté.** Après le premier ajout, le bouton « ＋ Ajouter » reste désactivé avec son spinner et la barre de progression du haut reste active. Entrée et clic sont sans effet jusqu'au rechargement. Même cause sur la fiche recette : après « Ajouter au planning », la barre reste active et le bouton garde la classe `loading`. | [V] `a2.py` : `{'disabled': True, 'cls': '… loading', 'nav': 'active'}` ; second ajout : `extras = 1`, clic en timeout. | L'écouteur `submit` global en capture de `base.html:64-68` désactive le bouton de tout formulaire, y compris ceux traités en JS avec `preventDefault` : `planning-shopping.js:86-96` (`#shop-add`), `recette-planifier.js:121`. Le constructeur contourne déjà le problème à la main (`builder.js:752-755`). Aucun test e2e n'ajoute deux articles. |
| **N2** | **Écritures concurrentes : un changement de repas peut être annulé par une coche.** Le verrou par planning ne protège que `mutate_planning_data`. Les autres écritures lisent puis réécrivent tout `data_json` sans verrou. Une coche qui a lu l'ancien état réécrit par-dessus le nouveau repas ; `planning_recipes` garde le nouveau, `data_json` l'ancien. | [V] `race.py`, 40 tours (1 changement de repas + 7 coches simultanées) : repas perdu 10 fois, `data_json` ≠ `planning_recipes` 10 fois, au moins une coche perdue 27 fois. La charge est forcée ; en usage réel (deux téléphones) la fenêtre est étroite mais existe. | Verrou : `services/shopping.py:453-468`. Sans verrou : `_refresh_shopping_and_save` (`:107-136`), `generate_shopping` (`routers/courses.py:56-99`), `partager_liste` (`:140-158`), `construire` (`routers/planning.py:145-155`), `_planifier` (`services/recettes.py:572-596`), `set_placard` (`services/shopping.py:379-391`). |
| **N3** | **Les groupes de midis sont renumérotés à chaque modification hors constructeur.** `_builder_from_plats` donne `group = jour` (1 à 7). Le constructeur n'a que 5 couleurs : G6 prend la couleur de G1, G7 celle de G2, alors que la légende dit « même couleur = même repas ». Le sélecteur ne propose que 5 groupes, on ne peut donc pas revenir en G6. Les groupes non consécutifs choisis par l'utilisateur sont perdus, et un jour sans plat dans la période repasse à 0 convive (« absent »). | [V] après un `set_main` : `builder` = groupes `1,1,3,4,5,6,7` ; capture `index_draft_1280.png` : G6 orange comme G1, G7 vert comme G2. | `services/planning.py:304-311` (appelé par `_refresh_shopping_and_save`, `dupliquer_planning`, `index`), `builder.js:14` et `:440` (modulo 5), `:615` (5 pastilles). |

### 2.2 Moyens

| # | Défaut | Preuve | Emplacement |
|---|---|---|---|
| N4 | **Un créneau vide de la période est invisible sur la page planning** : trou dans la grille en desktop, rien en mobile. On ne peut pas y ajouter un repas, alors que l'API le permet (`set_main` sur un créneau vide). Il faut repasser par le constructeur. | [V] planning de 3 jours avec 4 créneaux vides : 2 cases rendues, aucun bouton. | `partials/_week_grid.html:21-38`, `_week_list.html:8-19` |
| N5 | **« ✅ Valider ma semaine » ne valide pas.** Le bouton du constructeur crée un brouillon et renvoie sur la page planning, où il faut cliquer « ✅ Valider la semaine » une seconde fois. En réouverture d'une semaine validée (`/?planning=<id>`), les libellés restent « Enregistrer le brouillon » / « Valider ma semaine » et rien n'indique quelle semaine on modifie. | [V] capture `index_planning_390.png` ; [L] `save_planning` crée toujours en `valide = 0`. | `index.html:47-48`, `routers/planning.py:157-172` |
| N6 | **Session expirée = « Erreur réseau ».** Avec l'auth, les appels `/api/…` reçoivent un 401 en texte et les autres actions (`/generate-shopping`, `/planning/…/valider`, `/partager`, `/supprimer`) un 303 vers la page de login. `postJson` fait `r.json()` sans regarder le statut : exception, donc toast « Erreur réseau ». | [V] 401 texte et 303 constatés sur l'instance avec auth ; [L] pour le toast. | `common.js:33-41`, `main.py:51-56` |
| N7 | **Trois conventions d'erreur.** Routes récentes : statuts 400 / 404 / 502. Routes anciennes : 200 avec `{"error"}`. Routes dépréciées : 500 sur un corps illisible. 17 retours `str(e)` exposent le texte de l'exception. | [V] `POST /api/update-meal/3` avec un corps non JSON → 500 ; `/api/add-side` avec une liste → 500 ; `/api/free-meal` avec une liste → 200 `{"error":"'list' object has no attribute 'get'"}`. | `routers/planning.py:397`, `:451`, `:471` ; `grep "str(e)" app/routers app/services` |
| N8 | **Doublons et code mort laissés par les lots parallèles.** | [L] | voir la liste ci-dessous |
| N9 | **Service worker : cache sans purge et pages privées conservées.** `asset_version` change à chaque démarrage, donc chaque redémarrage ajoute une copie `?v=…` de chaque fichier statique, jamais retirée. Toute page naviguée est mise en cache, y compris les pages protégées, qui restent lisibles hors ligne après déconnexion. Les e2e bloquent le service worker : il n'est jamais exécuté en CI. | [V] cache après une visite : `style.css` et `style.css?v=1795707969` côte à côte ; [L] pour le reste. | `sw.js:80-93`, `:118-124`, `deps.py:40`, `tests/e2e/conftest.py` (`service_workers: block`) |
| N10 | **README déjà périmé** : « lien public en lecture seule » (`README.md:15`, `:214`), rien sur `/admin`, la sauvegarde en cron, le mode cuisine, le placard, les articles manuels, le hors-ligne, les tests e2e. | [V] `grep` | `README.md` |
| N11 | **Entrées non validées.** `week_start` accepte n'importe quel texte ; l'import ne vérifie pas la forme de `plats` (un fichier modifié à la main donne un 500 sur `/planning/<id>` via `p["jour"]`) ; une fusion peut créer deux plannings avec le même jeton de partage. | [V] `week_start=pas-une-date` → page « Semaine du pas-une-date » ; [L] pour l'import. | `routers/planning.py:59`, `services/backup.py:50-89`, `services/planning.py:56`, `database.py:514-518` |
| N12 | **« Cuisinée N fois » compte faux.** Un repas groupé sur 2 jours compte 2 fois, et les semaines validées à venir comptent comme cuisinées. | [V] « Cuisinée 2 fois · dernière fois le 26/11 » pour un seul poulet prévu sur 25-26/11. | `database.py:311-335` |
| N13 | **Restes du moteur d'ingrédients.** « 2 branches de thym » → article « Branches thym 2 » ; « sel, poivre » reste une ligne ; « lait de coco » en Boissons ; les parenthèses d'un article manuel sont supprimées (« Papier toilette (x6) » → « Papier toilette ») ; un article nommé « 2 » est accepté ; pas de choix du rayon pour un article manuel. | [V] | `text_utils.py` (`parse_ingredient_line`, `normalize_cached_ingredient`), `categories.py`, `services/shopping.py:310-323` |
| N14 | **Accessibilité des nouveaux écrans.** Pas de piège à focus dans la modale « Changer » ni dans le picker (après 40 Tab le focus est derrière la modale) ; seule la modale « Ajouter au planning » en a un. Contraste : orange `#e67e22` sur blanc 2,85:1, blanc sur vert `#27ae60` 2,87:1 (seuil AA : 4,5). Cibles en 390 px : cases de sélection 18 px, bouton 🍽 24 × 17, liens du menu 29 px de haut, lien « Données » 53 × 15. | [V] `a2.py`, calcul de contraste, mesures `a1.json` | `partials/_modal_change.html`, `builder-picker.js:150-168`, `style.css:2-10` |
| N15 | **Sécurité des nouvelles routes.** Les trois routes publiques ne laissent que cocher une clé existante (corps invalide → 400, autre méthode → 405, `/courses/<jeton>/extra` → 404) [V]. Restent : aucun moyen de révoquer ou régénérer un lien de partage ; aucune limite de débit sur `/login` et `/courses/*/check` ; aucune taille maximale de corps JSON ; `GET /logout` et `GET /api/enrich-all/stream` ont des effets de bord ; pas de CSRF (inchangé) ; sans `AUTH_USER`, `/admin/export` (qui contient les jetons) et `/admin/import` en mode « replace » sont ouverts à tout le réseau local. | [V] pour les contrôles d'accès (avec auth : `/admin*` → 303, `/api/*` → 401, `/courses/*` public) ; [L] pour le reste | `routers/courses.py:140-214`, `routers/auth.py:64`, `routers/admin.py`, `main.py:26` |

Détail de N8 :

- routes dépréciées sans appelant dans l'interface : `/api/update-meal`, `/api/add-side`, `/api/remove-side` (`routers/planning.py:387-479`), avec `_mutate_sides` et `_load_slot` ;
- `_planifier` (`services/recettes.py:545-600`) refait à la main ce que fait `apply_meals_batch` action `set_main` (`services/planning.py:738-756`) : créneau vide, `off_meals`, `nb_jours`. Le plan demandait d'appeler l'endpoint groupé ;
- contexte passé au template et jamais lu : `liste_courses`, `courses_par_rayon`, `rayon_order`, `creneaux_non_resolus`, `briques_manquantes` (`routers/planning.py:213-223`) ; `courses_checked` envoyé dans `page-data` sans lecteur (`planning.html:44`) ;
- `ingredients_force` encore lu (`routers/courses.py:79-84`) malgré la décision Q9 ;
- `construire` range les articles par rayon sans passer par `finalize_items` (`routers/planning.py:126-127`) : la liste créée là n'a pas de `key` stockée, celle de `generate_shopping` en a une. Sans effet aujourd'hui grâce au repli `_key_of`, mais deux formes de données coexistent ;
- deux files hors-ligne de formats différents : liste `[{item, checked}]` (`planning-shopping.js:18-49`) et objet `{clé: état}` (`courses-public.js:89-101`) ;
- `esc` redéfini dans `historique.html:100` et `cuisine.js:85` alors que `Common.esc` existe ;
- `<style>` inline dans `courses_public.html:15` avec le commentaire « courses.css est hors du lot L15 » ;
- JS inline restant : `historique.html` (137 lignes), `ajouter.html` (~125), `recettes.html` (~100), `recette_detail.html` (~100), `enrichir.html` (~20).

### 2.3 Mineurs

- La page planning ne se met pas à jour quand le lien public coche ; il faut recharger. L'inverse est rafraîchi toutes les 20 s. [L] `planning-shopping.js`
- La légende des groupes du constructeur liste aussi les jours hors période (« Dim », « Lun », « Mar » pour un planning de 4 jours). [V] capture `index_planning_390.png`
- `_enrichir_enregistrer` : si on vide les ingrédients, durée et portions ne sont pas écrites dans le cache, dont la base de portions reste prioritaire. Le message d'échec dit « Rien n'a été validé » alors que les écritures réussies sont faites. [L] `services/recettes.py:381-410`
- `/?draft=<id>` sur une semaine validée ouvre un constructeur vide sans message. [L] `routers/pages.py:51`
- Un nouveau brouillon créé depuis une fiche reste en base, vide, si l'écriture suivante échoue. [L] `services/recettes.py:570-571`
- `update_ingredients` fait une lecture Notion de plus à chaque écriture pour savoir si la page a une icône. [L] `notion_client.py:327`, `:341-352`
- `_locks` et `_PENDING` (jusqu'à 5 fichiers de 50 Mo) vivent en mémoire : correct avec un seul processus uvicorn (`entrypoint.sh:33`), faux au-delà. [L]

---

## 3. Audit UX / UI et fonctionnel

Rien à signaler sur : absence de débordement en 390 px, absence d'erreur console sur les 15 pages, hors-ligne de la liste publique, mode cuisine.

### 3.1 Parcours

- **Pas d'entrée « ma semaine en cours ».** L'accueil est un constructeur vide. Le planning en cours n'est atteignable que par le bloc « Dernier planning : Voir → » tout en bas, ou par l'historique. C'est pourtant l'écran le plus consulté en semaine. [V]
- **Trois façons de modifier un repas sur la page planning** : le bouton ↻ de la case, les cases à cocher avec la barre de sélection, « Modifier dans le constructeur ». La modale « Changer » contient en plus trois formulaires (liste, repas libre, brique). Aucune ne permet de remplir un créneau vide (N4). [V]
- **Constructeur** : deux boutons d'envoi dont aucun ne valide (N5) ; la date de début par défaut est le lundi de la semaine courante, donc dans le passé dès le mardi (`routers/pages.py:35-36`) [L].
- **Brouillons** : la carte d'un brouillon dans l'historique mène au constructeur ; la page planning du brouillon, avec sa liste de courses, n'a pas de lien. [V] capture `G_historique.png`

### 3.2 Courses

- **Placard sans écran de gestion** : on ne voit le contenu du placard qu'à travers les articles d'un planning qui le contiennent, et on ne peut en retirer un article que là. [L] `_shopping_list.html:44-46`
- **Lien de partage** : une fois créé, il est définitif (N15). Il n'apparaît que si la liste vient de recettes : une liste faite seulement d'articles manuels ne se partage pas (`_shopping.html:11`). [L]
- **« ↻ Régénérer »** efface toutes les coches sans confirmation (`services/shopping.py:305-307`). [L]
- Article manuel : pas de rayon au choix, pas de modification de la quantité après coup. [V]

### 3.3 Cohérence visuelle

- **Historique** : quatre boutons pleine largeur empilés par carte, cartes étroites qui laissent la moitié droite de l'écran vide en 1280 px, date technique « Créé le 2026-11-26 » alors que le reste de l'app écrit « 26/11 ». [V]
- **Trois styles de bouton d'action secondaire** : `btn-ghost btn-sm` (planning), `btn-secondary` vert plein (constructeur, « Voir → »), liens soulignés (pied de page). Le vert sert à la fois d'action secondaire et de succès. [V]
- **`/admin`** n'est atteignable que par un lien de 15 px de haut dans le pied de page. [V]
- 47 attributs `style="…"` dans les templates et 79 couleurs en dur dans `css/*.css` et les templates, malgré les jetons posés par L05. [V] `grep`
- La page publique et le mode cuisine n'ont pas de lien de retour vers l'app pour un utilisateur connecté ; c'est voulu pour la page publique, discutable pour le mode cuisine (bouton « Terminer » seulement à la dernière étape). [L]

---

## 4. Qualité et dette

### 4.1 Taille et répartition

| Fichier | Lignes | Constat |
|---|---|---|
| `app/llm_client.py` | 1 200 | inchangé, hors périmètre des lots |
| `app/static/js/builder.js` | 786 | fonctions pures (400 l.) et DOM (390 l.) dans le même fichier |
| `app/services/planning.py` | 771 | grille du constructeur, libellés de dates, période, édition groupée, briques : cinq sujets |
| `app/services/recettes.py` | 600 | liste, fiche, édition, ajout, ajout au planning, enrichissement par lot |
| `app/routers/planning.py` | 500 | dont 95 lignes de routes dépréciées et `construire` (120 l.) qui contient encore la logique métier |
| `app/services/shopping.py` | 468 | collecte, clés, coches, placard, vue, verrou |

- Les services importent encore des noms privés (`_builder_from_plats`, `_refresh_shopping_and_save`…) d'un module à l'autre, avec un import circulaire contourné par un import local (`services/planning.py:607`). [L]
- Deux styles d'accès aux instances : `from app.deps import db` (routeurs anciens) et `deps.db` (admin, cuisine). [L]

### 4.2 Tests

- 526 tests, 88 % de couverture Python. `notion_client` 67 %, `llm_client` 77 %.
- **Aucun test du JS hors e2e**, et le job e2e ne bloque pas la CI (`ci.yml:36`). N1 passe donc la CI.
- **15 fichiers redéfinissent la fixture `client`** ; pas de `tests/conftest.py`. `tests/test_routes.py` fait 1 484 lignes et 62 tests.
- Tests dépendants de l'horloge : `date.today()` dans `test_brouillons_integrite.py:108`, `test_recette_planifier.py:105`, `tests/e2e/outils.py:61` (risque à minuit, faible).

### 4.3 Chemins critiques non couverts

- ajout de **deux** articles manuels, masquer / restaurer, placard, depuis le navigateur ;
- page `/admin` : export, import en deux temps, réhydratation (testés par HTTP, pas au clic) ;
- historique : aperçu, suppression par modale, « Refaire » au clic ;
- service worker et hors-ligne (bloqué dans les e2e) ;
- app avec auth activée dans un navigateur (expiration de session) ;
- écritures concurrentes sur un même planning ;
- minuteurs du mode cuisine.

---

## 5. Plan de développement v2

### 5.1 Règles communes

Celles du premier plan (§3.1) restent valables : périmètre de fichiers strict, tests dans un nouveau fichier par lot, clés de `data_json` optionnelles avec repli, commits `type(portée): message`, sans mention d'IA, ni push ni PR sans accord. S'y ajoutent :

1. **Vérification avant de rendre un lot** : `.venv/bin/ruff check app tests scripts && .venv/bin/python -m pytest -q && .venv/bin/python -m pytest -q -m e2e`.
2. **Tout lot qui touche un écran** ajoute ou étend un test e2e du parcours modifié, dans un nouveau fichier `tests/e2e/test_parcours_<sujet>.py`.
3. **Un lot ne modifie pas les fichiers de test existants**, sauf M11. Si une assertion existante doit changer, l'agent le signale et s'arrête, sauf mention contraire dans le lot.
4. Lancement local pour vérifier au navigateur : `.venv/bin/python -m tests.e2e.fake_notion /tmp/x.db 8799`.

### 5.2 Vue d'ensemble

| Lot | Titre | Vague | Effort | Dépend de | Branche |
|---|---|---|---|---|---|
| M01 | Socle front : formulaires JS, session expirée, modale commune | 1 | S | — | `fix/socle-front-formulaires` |
| M02 | Écritures concurrentes et cohérence du planning | 1 | L | — | `fix/planning-concurrence-coherence` |
| M03 | Moteur d'ingrédients et compteur d'usage | 1 | S | — | `fix/ingredients-usage` |
| M04 | Service worker, README | 1 | S | — | `fix/sw-cache-readme` |
| M05 | Planning et constructeur : créneaux vides, libellés, groupes | 2 | M | M01, M02 | `feat/planning-creneaux-libelles` |
| M06 | Courses : placard, rayon, lien révocable, rafraîchissement | 2 | M | M01, M02 | `feat/courses-gestion` |
| M07 | Navigation, « Cette semaine », jetons visuels | 2 | M | M01 | `feat/navigation-semaine-jetons` |
| M08 | Recettes et historique : JS en fichiers, édition, modales | 2 | M | M01, M03 | `refactor/recettes-historique-js` |
| M09 | Erreurs HTTP cohérentes et durcissement | 3 | M | M05 à M08 | `fix/erreurs-http-durcissement` |
| M10 | Découpage des services | 3 | M | M05 à M08 | `refactor/services-decoupage` |
| M11 | Tests : fixture commune, parcours manquants, e2e bloquant | 4 | M | M09, M10 | `test/socle-et-parcours` |

---

### Vague 1 — quatre correctifs sans fichier commun

| Lot | Python | Templates | Statique | Tests (nouveaux) |
|---|---|---|---|---|
| M01 | — | `base.html` | `js/common.js` | `tests/e2e/test_parcours_articles.py`, `tests/test_socle_front.py` |
| M02 | `services/store.py` (nouveau), `services/shopping.py`, `services/planning.py`, `services/creations.py`, `services/recettes.py`, `routers/planning.py`, `routers/courses.py` | — | — | `tests/test_concurrence.py`, `tests/test_builder_groupes.py`, `tests/test_construire_validation.py` |
| M03 | `text_utils.py`, `categories.py`, `database.py` | — | — | `tests/test_ingredients_v2.py`, `tests/test_usage_recettes.py` |
| M04 | — | — | `sw.js` ; `README.md` | `tests/test_sw_cache.py`, `tests/e2e/test_parcours_hors_ligne.py` |

#### M01 — Socle front : formulaires JS, session expirée, modale commune

- **Objectif** : corriger N1 et N6, et fournir l'outil de modale que les lots de la vague 2 utiliseront (N14).
- **Fichiers** : `app/templates/base.html`, `app/static/js/common.js`, tests listés.
- **Étapes**
  1. `base.html:64-68` : l'écouteur `submit` global passe en phase de remontée et ne fait rien si `e.defaultPrevented` (un formulaire traité en JS ne navigue pas). Il ne désactive le bouton que pour une vraie navigation. Retirer ensuite le contournement devenu inutile n'est pas dans ce lot (`builder.js:752-755` appartient à M05).
  2. `Common.postJson` : lire le statut. 401, ou réponse dont l'URL finale est `/login` (`r.redirected`), → `toast('Session expirée, reconnecte-toi', 'error')` puis redirection vers `/login?next=<page courante>`. Réponse non JSON → objet `{error: 'Réponse inattendue du serveur'}` au lieu d'une exception. Ajouter `Common.requestJson(url, {method, body})` pour les `DELETE`.
  3. `Common.modal(el, {onClose, initialFocus})` : ouvre, piège le focus (Tab / Maj+Tab), ferme sur Échap, rend le focus à l'élément déclencheur. Reprendre la logique de `recette-planifier.js:108-119`. Renvoie `{close()}`. Aucun appelant n'est modifié ici.
- **Critères d'acceptation**
  - Sur `/planning/<id>`, ajouter « café » puis « 2 l lait » à la suite, au clavier puis au clic : deux articles, bouton actif, `#nav-progress` sans la classe `active`.
  - Sur une fiche, après « Ajouter au planning », `#nav-progress` n'est plus actif et `#plan-ok` n'a plus la classe `loading`.
  - Les formulaires qui naviguent (constructeur, ajout de recette, import) gardent leur spinner.
- **Tests** : e2e pour les trois critères ; `tests/test_socle_front.py` vérifie par lecture du fichier que `common.js` expose `modal` et `requestJson`.

#### M02 — Écritures concurrentes et cohérence du planning

- **Objectif** : plus aucune écriture de `data_json` hors verrou (N2), groupes de midis conservés (N3), une seule implémentation de chaque règle (N8 côté Python), entrées validées (N11).
- **Fichiers** : voir le tableau. `tests/test_routes.py` et `tests/test_routes_inventaire.py` peuvent être modifiés **uniquement** pour retirer les tests des trois routes supprimées (exception à la règle 3).
- **Étapes**
  1. `services/store.py` : `planning_lock(planning_id)` (le verrou actuel de `shopping.py:195`, `:459`) et `async def update_planning(planning_id, fn, recipes=False)` qui lit, applique `fn(planning, data)` (synchrone ou asynchrone), écrit `data_json` et, si demandé, `planning_recipes`, le tout sous verrou. `mutate_planning_data` devient un appel à cette fonction.
  2. Faire passer par `store.update_planning` : `_refresh_shopping_and_save`, `generate_shopping`, `partager_liste`, la branche « planning existant » de `construire`, `_planifier`, `_create_and_place`. Les appels à Notion (création de recette libre ou de brique) restent **hors** verrou ; seule la relecture, la mutation et l'écriture sont dedans. `set_placard` prend un verrou propre aux réglages.
  3. `_builder_from_plats(plats, off_meals, default_persons, previous=None)` : quand l'ancienne grille est fournie, garder le groupe et les convives de chaque midi dont le repas n'a pas changé ; pour les autres, prendre le plus petit numéro de groupe libre entre 1 et 5 avant d'aller au-delà. Sans ancienne grille, numéroter de façon compacte (1, 2, 3…) et non par jour. Un créneau vide d'un jour de la période garde les convives du jour, ou ceux du planning si le jour est vide. Passer `planning_data.get("builder")` depuis `_refresh_shopping_and_save`.
  4. `_planifier` appelle `apply_meals_batch(..., action="set_main")` au lieu de sa copie. Le brouillon neuf n'est créé qu'après la validation des paramètres, et supprimé si l'écriture échoue.
  5. `construire` : `week_start` doit être une date ISO valide, sinon ré-affichage du formulaire avec la saisie ; utiliser `finalize_items` ; sortir le calcul (plats, `per_day`, `off_meals`, liste) dans une fonction de `services/planning.py`.
  6. Supprimer `/api/update-meal`, `/api/add-side`, `/api/remove-side`, `_mutate_sides`, `_load_slot` s'il n'a plus d'appelant, le contexte mort de `voir_planning`, la lecture d'`ingredients_force`. (Décision D2.)
- **Critères d'acceptation**
  - `race.py` adapté en test : 40 tours, 0 repas perdu, 0 coche perdue, `data_json` et `planning_recipes` toujours cohérents.
  - Brouillon avec lundi et mercredi midi en G1, jeudi en G4 : après un `set_main` sur un soir, la grille garde G1 / G1 / G4. Aucun planning de 7 jours ne produit un groupe supérieur à 5 tant que 5 suffisent.
  - `POST /construire` avec `week_start=pas-une-date` ne crée rien.
  - `app/routers/planning.py` fait moins de 350 lignes.
- **Tests** : `tests/test_concurrence.py` (tâches `asyncio` concurrentes sur une base temporaire), `tests/test_builder_groupes.py`, `tests/test_construire_validation.py`.

#### M03 — Moteur d'ingrédients et compteur d'usage

- **Objectif** : N12, N13.
- **Fichiers** : `app/text_utils.py`, `app/categories.py`, `app/database.py`, tests listés.
- **Étapes**
  1. Unités de comptage reconnues : branche, brin, feuille, tige, tranche, botte, sachet, boîte, pot, bouquet (singulier et pluriel). `normalize_cached_ingredient` ne recolle plus l'unité dans le nom quand elle en fait partie.
  2. Une ligne « a, b » ou « a et b » sans quantité, faite de condiments connus (sel, poivre), donne deux ingrédients. Ne pas découper les autres lignes à virgule.
  3. `parse_ingredient_line(line, keep_parens=False)` : option pour garder le contenu des parenthèses dans le nom. M06 l'utilisera pour les articles manuels.
  4. `categorize` : « lait de coco », « crème de coco », « concentré de tomate », « bouillon » en Épicerie salée ; la règle du mot-clé le plus long doit suffire, sinon ajouter les expressions.
  5. `get_recipe_usage(today=None)` : ne compter que les repas dont la date est passée ou du jour ; compter une seule fois les midis consécutifs d'un même planning portant la même recette.
- **Critères d'acceptation** : « 2 branches de thym » → nom « thym », quantité 2, unité « branche » ; « sel, poivre » → 2 lignes ; poulet groupé sur 2 jours passés → `count = 1` ; semaine validée à venir → non comptée ; `tests/test_text_utils.py` et `tests/test_courses_unites.py` passent sans modification.

#### M04 — Service worker et README

- **Objectif** : N9, N10.
- **Fichiers** : `app/static/sw.js`, `README.md`, tests listés.
- **Étapes**
  1. `sw.js` : à chaque mise en cache d'un fichier `/static/…?v=X`, supprimer les autres entrées du même chemin. Ne mettre en cache une navigation que pour `/courses/<jeton>` ; les autres pages gardent le repli `offline.html`. Borner le nombre de pages en cache à 20 (retrait des plus anciennes). Passer `CACHE` à `menu-planner-v5`.
  2. `README.md` : corriger « lecture seule », ajouter les sections Courses partagées (coches, articles manuels, masqués, placard), Données (`/admin`, export, import, réhydratation, cron `python -m scripts.backup`), Mode cuisine, Hors-ligne (HTTPS ou localhost requis), Tests e2e. Mettre à jour l'arborescence.
  3. `tests/e2e/test_parcours_hors_ligne.py` : contexte Playwright **avec** service worker (nouveau contexte créé dans le test) ; reprendre le scénario de `a3.py` (page ouverte, hors ligne, rechargement, deux coches, retour du réseau, état du serveur).
- **Critères d'acceptation** : après deux visites avec deux valeurs de `?v=`, une seule entrée par fichier statique dans le cache ; `/planning/<id>` visitée puis hors ligne → `offline.html` ; le test hors-ligne passe.

---

### Vague 2 — quatre lots de fonctionnalités en parallèle

| Lot | Python | Templates | Statique |
|---|---|---|---|
| M05 | `routers/planning.py` | `planning.html`, `index.html`, `partials/_week_grid.html`, `_week_list.html`, `_meal.html`, `_modal_change.html`, `_picker.html` | `js/planning-week.js`, `js/builder.js`, `js/builder-picker.js`, `css/planning.css`, `css/builder.css` |
| M06 | `routers/courses.py`, `services/shopping.py` | `partials/_shopping.html`, `_shopping_list.html`, `courses_public.html` | `js/planning-shopping.js`, `js/courses-public.js`, `css/courses.css` |
| M07 | `routers/pages.py` | `base.html`, `login.html` | `style.css` |
| M08 | `routers/recettes.py`, `services/recettes.py` | `recettes.html`, `recette_detail.html`, `ajouter.html`, `enrichir.html`, `historique.html` | `js/recette-planifier.js`, nouveaux `js/recettes-liste.js`, `js/recette-fiche.js`, `js/ajouter.js`, `js/historique.js`, `css/recettes.css`, nouveau `css/historique.css` |

Aucun lot de cette vague ne touche `app/main.py`, `database.py`, `services/planning.py`, `js/common.js` ni `sw.js`.

#### M05 — Planning et constructeur : créneaux vides, libellés, groupes

- **Objectif** : N4, N5, la partie visible de N3, piège à focus des deux modales (N14).
- **Étapes**
  1. `_week_grid.html` et `_week_list.html` : pour chaque créneau de la période sans plat et hors `off_meals`, rendre une case « ＋ Ajouter un repas » qui ouvre la modale « Changer » sur ce créneau (`set_main` crée le plat). Calcul des jours couverts dans le template, à partir de `week_rows`.
  2. Constructeur, selon la décision D1. Option par défaut : un seul bouton principal « Enregistrer et voir le planning » plus « Enregistrer le brouillon » ; en réouverture d'une semaine validée, bandeau « Tu modifies la semaine validée du … » et boutons « Enregistrer les modifications » / « Annuler ». Le contexte `valide` est déjà fourni (`routers/pages.py:81`).
  3. `builder.js` : palette de 7 couleurs distinctes et 7 pastilles dans le sélecteur ; la légende ne liste que les jours de la période ; retirer le contournement de `:752-755` rendu inutile par M01.
  4. `planning-week.js` et `builder-picker.js` : ouverture et fermeture par `Common.modal`.
  5. Date de début par défaut d'un nouveau planning : aujourd'hui (attribut `value` posé par `builder.js` si le serveur envoie une date passée et qu'aucun brouillon n'est ouvert), sans toucher `routers/pages.py`.
- **Critères d'acceptation** : un planning de 3 jours avec 4 créneaux vides affiche 4 cases « Ajouter » en 1280 px et en 390 px ; choisir une recette dans l'une d'elles la remplit ; Tab ne sort d'aucune des deux modales ; aucune paire de groupes distincts ne partage une couleur.
- **Tests** : `tests/e2e/test_parcours_creneaux_vides.py`, `tests/test_planning_gabarits_v2.py`.

#### M06 — Courses : placard, rayon, lien révocable, rafraîchissement

- **Objectif** : §3.2, la partie « courses » de N8 et N15, le rafraîchissement de la page planning.
- **Étapes**
  1. Placard : `GET /api/placard` et `DELETE /api/placard/{nom}` ; bouton « 🏠 Placard (n) » dans l'en-tête de la liste, qui ouvre la liste complète avec retrait (modale via `Common.modal`).
  2. Article manuel : parenthèses gardées (`keep_parens` de M03), un nom purement numérique est refusé, sélecteur de rayon dans le menu « ⋯ » (`POST /api/courses/{id}/extra/{key}/rayon`).
  3. Partage : `POST /planning/{id}/partager` accepte `{"action": "revoquer" | "regenerer"}` ; bouton « Révoquer le lien » à côté de « Partager ». Le partage devient possible pour une liste faite seulement d'articles manuels. Jeton de 16 octets pour les nouveaux liens.
  4. `planning-shopping.js` : rafraîchissement des coches toutes les 20 s quand l'onglet est visible, par un nouveau `GET /api/courses/{id}/etat` (même réponse que la route publique). Même format de file hors-ligne que `courses-public.js` (objet `{clé: état}`), avec reprise de l'ancienne file au premier chargement.
  5. « ↻ Régénérer » : confirmation si des articles sont cochés.
  6. `/courses/{token}/check` : limite simple en mémoire, 120 requêtes par minute et par jeton, 429 au-delà. Déplacer le `<style>` inline de `courses_public.html:15` dans `css/courses.css`.
- **Critères d'acceptation** : un article mis au placard depuis un planning se retire depuis l'écran du placard sans ouvrir ce planning ; un lien révoqué répond 404 sur les trois routes publiques ; une coche faite sur le lien public apparaît sur la page planning ouverte en moins de 25 s sans rechargement.
- **Tests** : `tests/test_placard_gestion.py`, `tests/test_partage_revocation.py`, `tests/e2e/test_parcours_courses_gestion.py`.

#### M07 — Navigation, « Cette semaine », jetons visuels

- **Objectif** : §3.1 (entrée « ma semaine »), §3.3, contraste et cibles (N14).
- **Étapes**
  1. `GET /semaine` : redirige vers le planning validé dont la période contient aujourd'hui ; à défaut le plus récent ; à défaut `/`. Lien « Cette semaine » dans le menu, « Données » dans le menu (plus seulement en pied de page). (Décision D3.)
  2. `style.css` : jeton `--pr-text` pour le texte orange (contraste ≥ 4,5:1 sur blanc et sur `--bg`), utilisé par les titres de rayon, puces et liens ; texte des boutons verts et orange lisible (≥ 4,5:1) ; une seule règle de bouton secondaire. (Décision D5.)
  3. Cibles ≥ 44 px sous 768 px pour les liens du menu, `.ck`, `.shop-src`, `.side-add`, `.step-del`.
  4. `.hist-grid` et `.hist-actions` : actions en ligne avec retour à la ligne, grille qui occupe la largeur. Ce lot ne modifie que `style.css` ; le gabarit de l'historique appartient à M08.
- **Critères d'acceptation** : `/semaine` mène au planning en cours ; aucun couple texte / fond des jetons sous 4,5:1 (test qui calcule les contrastes depuis `:root`) ; en 390 px, aucun lien du menu sous 44 px de haut ; le menu tient sur deux lignes au plus.
- **Tests** : `tests/test_semaine_courante.py`, `tests/test_jetons_contraste.py`.

#### M08 — Recettes et historique : JS en fichiers, édition, modales

- **Objectif** : fin de S3 (N8), défauts d'édition (§2.3), dates lisibles.
- **Étapes**
  1. Sortir le JS inline de `historique.html`, `recettes.html`, `recette_detail.html`, `ajouter.html`, `enrichir.html` vers les fichiers listés, données serveur par `#page-data`, `Common.esc` partout. Styles de `historique.html:83-96` vers `css/historique.css`.
  2. Modales (suppression dans l'historique, suppression de recette, « Ajouter au planning ») par `Common.modal`.
  3. Historique : « Créé le 26/11/2026 » ; carte d'un brouillon avec deux liens, « Reprendre » (constructeur) et « Voir » (page planning).
  4. `_enrichir_enregistrer` : écrire portions et durée dans le cache même sans ingrédient ; ingrédients vidés → cache vidé ; message d'échec qui liste ce qui a été enregistré et ce qui ne l'a pas été.
  5. `/?draft=<id>` sur une semaine validée : hors périmètre (`routers/pages.py` appartient à M07) ; le signaler si le temps manque à M07.
- **Critères d'acceptation** : plus aucun `<script>` sans `src` dans ces 5 templates, hors `page-data` ; notation, tri, filtres, suppression, aperçu et « Refaire » fonctionnent comme avant ; `tests/test_static_js.py` couvre les nouveaux fichiers.
- **Tests** : `tests/e2e/test_parcours_historique.py`, `tests/e2e/test_parcours_recettes_liste.py`, `tests/test_recettes_edition_v2.py`.

---

### Vague 3 — deux lots transverses, disjoints

| Lot | Fichiers |
|---|---|
| M09 | `app/main.py`, `app/routers/*.py`, `app/templates/base.html` (bouton de déconnexion), `app/templates/recettes.html` (appel de l'enrichissement), `tests/test_erreurs_http.py`, `tests/test_durcissement.py` |
| M10 | `app/services/*.py` uniquement |

#### M09 — Erreurs HTTP cohérentes et durcissement

- **Objectif** : N7, reste de N15, N11 côté import.
- **Étapes**
  1. Une fonction `erreur(message, status)` dans `app/deps.py` ou un module `app/http.py` ; toutes les routes d'action renvoient 400 (corps invalide), 404 (introuvable), 502 (Notion ou LLM en échec), 500 avec un message générique. Plus aucun `str(e)` dans une réponse ; l'exception va dans les logs. Le front lit déjà `d.error` quel que soit le statut depuis M01. (Décision D4.)
  2. Lecture du corps par une fonction commune (celle de `routers/courses.py:37-43`), avec une taille maximale de 64 Ko.
  3. `POST /logout` (formulaire dans le menu) ; `POST /api/enrich-all/stream` avec lecture du flux par `fetch` côté `recettes` (remplace `EventSource`).
  4. `/login` : 5 échecs par minute et par adresse, puis 429.
  5. Import : `services/backup.py` appartient à M10 dans cette vague, donc la vérification de la forme des `plats` (liste d'objets avec `jour` 1-7, `moment`, `nom_recette`) est faite par M10 ; M09 ne fait que rendre `voir_planning` tolérant (planning illisible → page d'erreur 422 lisible, pas un 500).
  6. `/admin` sans auth configurée : bandeau « Accès non protégé : toute personne du réseau peut exporter ou remplacer les données ». (Décision D6.)
- **Exception à la règle 3** : ce lot met à jour les assertions de statut des tests existants qu'il fait changer, et rien d'autre dans ces fichiers.
- **Critères d'acceptation** : `grep -rn "str(e)" app/routers` ne renvoie rien ; chaque route d'action répond 400 à un corps non JSON (test paramétré sur l'inventaire des routes) ; `GET /logout` → 405.

#### M10 — Découpage des services

- **Objectif** : §4.1, sans changement de comportement.
- **Étapes**
  1. `services/planning.py` → `planning.py` (plats, accompagnements, libellés, période), `planning_builder.py` (grille du constructeur), `planning_batch.py` (édition groupée, briques).
  2. `services/recettes.py` → `recettes.py` (liste, fiche, usage), `recette_edition.py` (ajout, édition), `enrichissement.py` (lot, SSE), `recette_planifier.py`.
  3. `services/shopping.py` → `shopping.py` (collecte, génération) et `shopping_list.py` (clés, coches, articles manuels, placard, vue).
  4. Fonctions partagées entre modules : nom public (sans `_`). Supprimer l'import local circulaire de `services/planning.py:607`. Les anciens noms restent importables depuis leur module d'origine (ré-export) pour ne toucher ni les routeurs ni les tests ; M11 retire ces ré-exports.
  5. `services/backup.py` : validation de la forme des `plats` à l'import, jeton de partage en double retiré du planning importé en mode fusion.
- **Critères d'acceptation** : aucun fichier de `app/services/` au-dessus de 350 lignes ; suite complète verte sans modifier un seul test ; inventaire des routes inchangé.
- **Tests** : `tests/test_backup_validation.py`.

---

### Vague 4 — un lot

#### M11 — Tests : fixture commune, parcours manquants, e2e bloquant

- **Fichiers** : `tests/**`, `.github/workflows/ci.yml`, `pyproject.toml`, et les ré-exports de `app/services/*.py` laissés par M10.
- **Étapes**
  1. `tests/conftest.py` avec la fixture `client` unique (base temporaire, Notion simulé) ; retirer les 15 copies.
  2. Découper `tests/test_routes.py` par domaine (`test_routes_planning.py`, `_courses.py`, `_recettes.py`, `_pages.py`), sans réécrire les tests.
  3. Horloge : fixture `aujourdhui` qui fige `date.today()` dans les tests qui en dépendent.
  4. Imports des tests vers les nouveaux modules de services ; retrait des ré-exports.
  5. e2e manquants : `/admin` (export puis import « replace » au clic), instance avec auth (login, expiration de session pendant une coche → message et redirection), minuteur du mode cuisine avec `page.clock`, fonctions pures de `window.Cuisine`.
  6. CI : job e2e bloquant (retirer `continue-on-error`), si la décision D7 le confirme.
- **Critères d'acceptation** : une seule définition de `client` ; aucun fichier de test au-dessus de 500 lignes ; `pytest -m e2e` sous 2 minutes ; couverture Python ≥ 88 %.

---

### 5.3 Ordre d'exécution

| Vague | Lots en parallèle | Condition pour passer à la suivante |
|---|---|---|
| **1** | M01, M02, M03, M04 | Les quatre fusionnés, suites verte et e2e verte |
| **2** | M05, M06, M07, M08 | Les quatre fusionnés ; ordre de fusion conseillé : M07 → M08 → M06 → M05 |
| **3** | M09, M10 | Les deux fusionnés, M10 avant M09 |
| **4** | M11 | — |

Points de contact à surveiller à la fusion :

- **Vague 1** : aucun fichier partagé. M02 retire des tests dans `tests/test_routes.py` et `tests/test_routes_inventaire.py` ; aucun autre lot de la vague n'y touche.
- **Vague 2** : `base.html` (M07) et les templates de page (M05, M06, M08) sont disjoints. M07 change des classes CSS globales dans `style.css` : M05, M06 et M08 ne doivent pas y ajouter de règles (chacun a sa feuille). M06 utilise `keep_parens`, livré par M03 en vague 1.
- **Vague 3** : M09 modifie `recettes.html` (appel SSE) après que M08 en a sorti le JS : l'appel est alors dans `js/recettes-liste.js`, que M09 est autorisé à modifier pour ce seul changement. M09 corrige des assertions de statut dans les tests existants ; M10 n'en modifie aucun.
- **Vague 4** : M11 est seul.

---

## 6. Décisions à prendre

| # | Question | Pourquoi ça compte | Option par défaut |
|---|---|---|---|
| D1 | « Valider ma semaine » dans le constructeur : valider réellement, ou renommer ? | Aujourd'hui le bouton ne fait pas ce qu'il dit (N5). Valider directement supprime l'étape de relecture sur la page planning. | Renommer en « Enregistrer et voir le planning » ; la validation reste sur la page planning |
| D2 | Supprimer `/api/update-meal`, `/api/add-side`, `/api/remove-side` ? | Plus aucun appelant dans l'interface, 95 lignes et leurs tests. Un script externe qui les utiliserait casserait. | Oui |
| D3 | Ajouter « Cette semaine » au menu et garder le constructeur en accueil, ou faire de la semaine en cours la page d'accueil ? | Change le premier écran de l'app. | Lien dans le menu, accueil inchangé |
| D4 | Passer toutes les erreurs en statuts HTTP (400 / 404 / 502) ? | Cohérent et exploitable par un moniteur, mais modifie le contrat des routes anciennes. | Oui |
| D5 | Assombrir le texte orange et le texte des boutons pour atteindre 4,5:1 ? | Touche l'identité visuelle. | Oui pour le texte ; l'orange reste la couleur des fonds et des bordures |
| D6 | `/admin` sans auth configurée : laisser ouvert avec un bandeau, ou refuser l'import « replace » tant que l'auth n'est pas activée ? | Sans auth, tout le réseau local peut remplacer la base. | Bandeau d'avertissement |
| D7 | Rendre le job e2e bloquant en CI ? | N1 serait passé au rouge. Risque : un test instable bloque une PR. | Oui, à la fin de M11 |
| D8 | Lien de partage : révocable et régénérable (M06) ? | Aujourd'hui un lien diffusé reste valable tant que le planning existe. | Oui |
| D9 | « Cuisinée N fois » : compter une fois un repas groupé, ignorer les semaines à venir (M03) ? | Change les chiffres affichés et le tri « pas faite depuis longtemps ». | Oui |
| D10 | Le serveur du premier audit tourne encore sur le port 8765 (PID 40408). L'arrêter ? | Il occupe un port et j'y ai créé deux plannings de test par erreur. | Oui (`kill 40408`) |

## 7. Décisions retenues (26/11/2026)

Options par défaut retenues pour D1 à D9 :
- D1 : renommer le bouton.
- D2 : supprimer les routes dépréciées.
- D3 : lien de menu.
- D4 : statuts HTTP partout.
- D5 : assombrir le texte orange seulement.
- D6 : bandeau d'avertissement.
- D7 : e2e bloquants après M11.
- D8 : lien de partage révocable.
- D9 : compter une seule fois, sans les semaines à venir.

D10 est fait : le serveur a été arrêté. Chaque lot est fusionné en local dans `integration/plan-amelioration-v2`, puis une PR unique part vers `main`.
