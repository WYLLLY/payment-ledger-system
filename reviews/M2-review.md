# M2 — nouvelle review indépendante après corrections

Date : 10 octobre 2026. Verdict après correction M2-003 : **APPROVE**.

La section « Suivi de correction » en fin de document contient les résultats actuels. Les constats ci-dessous sont conservés comme historique de la review avant correction.

Les deux problèmes signalés lors de la précédente review sont corrigés dans le code. Aucun nouveau défaut fonctionnel, de sécurité ou de cohérence financière n'a été confirmé dans le périmètre M1 + M2 examiné. Un nouveau finding **Low** concerne la fiabilité du test de régression de déconnexion : son résultat dépend encore du passage d'une fenêtre horaire à la suivante.

**Verification status : FAIL pour la propriété de déterminisme du nouveau test ; PASS pour les commandes de vérification configurées et les scénarios fonctionnels exécutés.** La validation finale sans réserve reste conditionnée à la correction de ce test.

## Périmètre et méthode

- Lecture indépendante d'`AGENTS.md`, de `docs/approved-plan.md`, des skills locaux `adversarial-review`, `verification` et des garanties financières pertinentes, de l'implémentation, des tests, des migrations et de la CI.
- Analyse des modifications locales et des corrections par rapport aux constats de la précédente review ; exécution des vérifications par le reviewer, sans reprendre les conclusions de Chat A comme preuve.
- Relecture indépendante complémentaire confiée à un sous-agent conformément à `AGENTS.md` ; reproduction du nouveau finding par le reviewer principal sur PostgreSQL réel.
- Le fichier `reviews/M2-review.md` était absent au début de cette reprise. Ce rapport est donc créé au chemin demandé à partir des constats antérieurs de cette conversation et de nouvelles vérifications.
- Git contient le commit M1 `0edadff` ; les fichiers M2 et leurs corrections sont encore dans le working tree. Aucun historique de commits M2 ne permet d'attribuer séparément chaque correction. Aucun remote Git n'est configuré.
- Aucun code, test, migration ou fichier de configuration n'a été modifié par cette review. M3 reste hors périmètre.

## Findings précédents

### M2-001 — Medium — Readiness positive avec seulement le schéma M1 : CORRIGÉ

**Localisation :** `src/payment_ledger/database.py`, `database_is_ready` ; `tests/integration/test_foundation.py:91`.

La sonde vérifie désormais le rôle `payment_app`, l'accès au schéma, la présence des six tables M2, les droits SELECT requis, les droits INSERT par colonne et les droits EXECUTE sur les trois fonctions M2. L'absence d'un objet ou d'un droit requis produit une indisponibilité ; les exceptions SQL ne sont pas exposées dans les logs.

Le nouveau test `test_readiness_tracks_m2_upgrade_and_downgrade` vérifie avec le même client : M1 → 503, M2 → 200, retrait du droit EXECUTE sur la création de wallet → 503, restauration → 200, downgrade M1 → 503. Il passe sur PostgreSQL réel.

Des probes supplémentaires ont vérifié l'absence de la fonction de révocation, le retrait d'INSERT sur `users.password_hash` et le retrait de SELECT sur `wallets` : readiness 503, liveness 200, puis readiness 200 après restauration. Les migrations et la récupération de la sonde restent correctes dans ces scénarios.

### M2-002 — Low — Quota anonyme empêchant la révocation d'une session : CORRIGÉ

**Localisation :** `src/payment_ledger/api.py:88` ; `tests/integration/test_identity_wallets.py:307`.

`/auth/logout` ne dépend plus du quota partagé de login/register. L'extraction et la validation du bearer token, son hachage, la révocation transactionnelle et l'audit restent en place. Login et inscription conservent leur limitation.

Le nouveau test couvre quota épuisé, logout 204, refus ultérieur de `/users/me` et `/wallets`, deuxième logout 401, compteur inchangé par le logout et un seul événement de révocation. Il passe dans l'exécution normale, mais présente le défaut de préparation décrit ci-dessous.

Un scénario indépendant avec les paramètres par défaut et deux clients partageant la même adresse a également passé : après épuisement effectif du quota par le second client, logout sans token 401, logout valide 204, accès protégés 401 et un unique audit de révocation. La correction fonctionnelle reste valide même dans la reproduction du défaut de test.

## Nouveau finding

### M2-003 — Low — Le test de logout reste instable à la frontière horaire

**Localisation :** `tests/integration/test_identity_wallets.py:314–320`, assertion aux lignes 322–326.

**Scénario :** l'inscription consomme une tentative juste avant la frontière de fenêtre. Le login suivant a lieu dans la nouvelle fenêtre : PostgreSQL remet le compteur à 1. Le test fixe ensuite uniquement `window_start` dans le futur. Avec une limite de 2, le login suivant est encore autorisé et répond 200, alors que le test exige 429.

**Impact :** échec intermittent et injustifié de la CI ; le scénario prétendant vérifier la révocation après épuisement du quota peut s'arrêter avant d'avoir atteint cette propriété. Aucun contournement du quota ni défaut de révocation en production n'est démontré par ce finding.

**Cause :** le gel de la fenêtre est effectué après les deux requêtes préparatoires et ne normalise pas `attempts`. Il ne peut donc pas garantir que les deux requêtes ont consommé le même budget.

**Couverture existante :** la nouvelle régression réussit lorsque register et login sont dans la même fenêtre. Elle ne force pas leur séparation entre deux fenêtres. Les 90 tests passent dans l'exécution normale.

**Reproduction réelle :** dans une base PostgreSQL isolée, inscription 201, vieillissement de `window_start` à 0 avant le login pour simuler la fenêtre précédente, login 200 avec compteur égal à 1, puis application exacte du SQL de préparation du test. Le login suivant répond **200 au lieu du 429 attendu**. Après consommation effective du budget restant, le quota répond 429 et logout réussit toujours avec 204, puis token refusé avec 401.

**Recommandation :** après obtention de la session, préparer explicitement une fenêtre stable **et** un compteur épuisé (`attempts = settings.auth_rate_limit`), ou consommer explicitement le quota restant avant les assertions. Ne pas retirer les assertions sur le logout, les accès protégés, le compteur et l'audit.

**Test requis :** forcer de façon déterministe un changement de fenêtre entre register et login, sans attendre l'horloge réelle, puis vérifier que la préparation du scénario garantit bien le quota épuisé et que les assertions de révocation sont atteintes.

## Vérifications réellement exécutées

Les commandes ci-dessous ont été exécutées depuis la racine du dépôt. PostgreSQL utilisait le port isolé `27432`, l'API le port `18002`, dans le projet Compose temporaire `m2-rereview-20261010`. Les options désactivant les caches évitent des écritures inutiles dans le dépôt.

| Commande | Résultat |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/ruff check --no-cache .` | PASS |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/ruff format --check --no-cache .` | PASS — 37 fichiers |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/mypy --cache-dir /tmp/m2-rereview-mypy-cache` | PASS — 28 fichiers |
| `PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' .venv/bin/pytest tests/unit` | PASS — 16 tests |
| `APP_DB_PORT=27432 PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' .venv/bin/pytest tests/integration/test_foundation.py::test_readiness_tracks_m2_upgrade_and_downgrade tests/integration/test_identity_wallets.py::test_logout_revokes_session_when_anonymous_auth_quota_is_exhausted -v` | PASS — 2 tests ciblés |
| `APP_DB_PORT=27432 PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' .venv/bin/pytest` | PASS — **90 tests**, dont 74 tests d'intégration et 16 unitaires ; 48,77 s ; aucun test skipped |
| `actionlint .github/workflows/ci.yml` | PASS |
| `shellcheck docker/init-db.sh` | PASS |
| `git diff --check` | PASS |
| `DOCKER_CONTEXT=colima-payment-ledger docker info --format '{{.ServerVersion}}'` | FAIL initial — moteur arrêté/socket absent ; problème d'environnement |
| `colima start --profile payment-ledger --activate=false` | PASS — moteur existant redémarré |
| `DOCKER_CONTEXT=colima-payment-ledger APP_DB_PORT=27432 API_PORT=18002 docker-compose -p m2-rereview-20261010 config --quiet` | PASS |
| `DOCKER_CONTEXT=colima-payment-ledger APP_DB_PORT=27432 API_PORT=18002 docker-compose -p m2-rereview-20261010 up --build --wait --wait-timeout 180` | PASS — volume neuf, migration réussie, PostgreSQL et API healthy |
| `curl --fail --silent http://127.0.0.1:18002/health/live` | PASS — `{"status":"ok"}` |
| `curl --fail --silent http://127.0.0.1:18002/health/ready` | PASS — `{"status":"ok"}` |
| `DOCKER_CONTEXT=colima-payment-ledger APP_DB_PORT=27432 API_PORT=18002 docker-compose -p m2-rereview-20261010 exec -T api python - < scripts/smoke_api.py` | PASS — health, inscription, login, wallet, ownership, logout |
| `APP_DB_PORT=27432 PYTHONDONTWRITEBYTECODE=1 .venv/bin/alembic current` | PASS — `0002_identity_wallets (head)` |
| `APP_DB_PORT=27432 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -` avec probes temporaires fournies sur stdin | PASS — objets/droits manquants et récupération de readiness ; quota par défaut partagé entre deux clients ; révocation et audit |
| Même invocation Python, probe de frontière de fenêtre décrite dans M2-003 | **FAIL de la propriété attendue** — réponse 200 reproduite au lieu de 429 ; script de diagnostic terminé normalement |
| `DOCKER_CONTEXT=colima-payment-ledger APP_DB_PORT=27432 API_PORT=18002 docker-compose -p m2-rereview-20261010 down --volumes --remove-orphans` | PASS — ressources temporaires de review supprimées |

Les probes supplémentaires ont utilisé de vraies bases PostgreSQL isolées, supprimées ensuite ; aucun fichier de test n'a été ajouté ni modifié. Aucun contrôle ou assertion existante n'a été désactivé ou affaibli.

## Sécurité, PostgreSQL, migrations et non-régression

- Les tests de privilèges positifs et négatifs, l'interdiction d'escalade des rôles et les frontières d'écriture passent. L'API utilise le rôle restreint, avec isolation READ COMMITTED ; les credentials migrateur restent séparés.
- Les parcours d'identité vérifient Argon2id, stockage du hash du token, expiration/révocation, erreurs génériques et absence de secrets dans les réponses concernées. Les wallets d'autres utilisateurs restent inaccessibles.
- Les contraintes PostgreSQL protègent la paire available/reserved, la devise et les soldes entiers initiaux non négatifs. Création du wallet, de ses comptes et audit sont atomiques ; les tests de rollback sur erreur partielle passent.
- Les tests de concurrence sur inscription, création de wallet, quota partagé entre instances et révocation simultanée passent ; une révocation ne produit qu'un audit.
- Les migrations sur base neuve, upgrade depuis M1, réexécution et chemins de downgrade prévus sont couverts par les tests PostgreSQL. Le démarrage Compose a également appliqué les migrations à un volume neuf et servi les parcours API.
- La CI a été relue, validée avec actionlint, et ses contrôles applicables ont été exécutés localement. **SKIPPED : exécution GitHub Actions distante**, aucun remote n'étant configuré ; aucun résultat de runner GitHub n'est revendiqué. L'environnement local ne reproduit pas à l'identique le runner Ubuntu.
- Les transferts, paiements, écritures du ledger et workers des milestones suivants sont hors périmètre M2 ; aucune validation anticipée de ces fonctionnalités n'est revendiquée.

## Décision

Les corrections runtime de readiness et de logout sont acceptées. Il reste à fiabiliser la préparation du nouveau test de logout et à prouver le scénario de changement de fenêtre. Ce finding Low n'impose aucune modification fonctionnelle de l'API d'après les éléments reproduits.

**APPROVE WITH CHANGES**

## Suivi de correction — 10 octobre 2026

À la demande de l'utilisateur, M2-003 a été corrigé dans le test : la préparation fixe désormais le compteur à `settings.auth_rate_limit` en même temps que la fenêtre. Le test est paramétré avec et sans expiration forcée de la fenêtre entre inscription et login. Le scénario de rollover vérifie explicitement le compteur remis à 1 avant préparation du quota épuisé. Toutes les assertions fonctionnelles antérieures sont conservées.

La relecture indépendante `review_rollover_fix`, menée avec `adversarial-review`, approuve cette correction ciblée sans nouveau finding. Aucun code applicatif ni schéma de base n'a changé pour corriger M2-003.

Vérifications exécutées après correction :

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/ruff check --no-cache .` : PASS.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/ruff format --check --no-cache .` : PASS.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/mypy --cache-dir /tmp/m2-fix-mypy-cache` : PASS, 28 fichiers.
- `APP_DB_PORT=27432 PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' .venv/bin/pytest tests/integration/test_identity_wallets.py::test_logout_revokes_session_when_anonymous_auth_quota_is_exhausted -v` : PASS, les deux paramètres.
- `APP_DB_PORT=27432 PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' .venv/bin/pytest` : PASS, **91 tests en 49,95 s**, dont 75 tests d'intégration PostgreSQL et 16 unitaires ; aucun skip.
- `actionlint .github/workflows/ci.yml`, `shellcheck docker/init-db.sh`, `git diff --check` : PASS.
- `DOCKER_CONTEXT=colima-payment-ledger APP_DB_PORT=27432 API_PORT=18002 docker-compose -p m2-fix-20261010 up --build --wait --wait-timeout 180` : PASS, base neuve, migrations et services healthy.
- `curl --fail --silent http://127.0.0.1:18002/health/live` et `/health/ready` : PASS.
- `DOCKER_CONTEXT=colima-payment-ledger APP_DB_PORT=27432 API_PORT=18002 docker-compose -p m2-fix-20261010 exec -T api python - < scripts/smoke_api.py` : PASS.

**Verification status : PASS.** Les trois findings sont résolus. L'exécution GitHub Actions distante reste non effectuée, sans remote configuré. Aucun travail M3 n'a été entrepris.

**APPROVE**
