# Milestone 4 — Transferts atomiques

Date : 11 octobre 2026. **Verification status: PASS (locale)**.
Revue indépendante : **APPROVE**, aucun finding bloquant restant.
La CI distante est consultable sur la PR de `feature/m4-transfers`.

## Périmètre

Source de vérité : `docs/approved-plan.md`, plan révisé inchangé. M3 a été fusionné
avec l'autorisation explicite de l'utilisateur : PR #1, commit
`fbc62d16b02e275af835f05270b17ccb17ee08ee`.

M4 ajoute uniquement les transferts internes et leur idempotence. Aucun paiement,
outbox, prestataire, worker ou élément M5 n'est implémenté.

- Migration `0004_transfers` : transferts, clés conservées, liens aux journaux,
  contraintes différées, triggers d'immutabilité, fonctions et privilèges.
- Module `transfers` : contrats et service à transaction explicite.
- API : création et consultation privée des transferts, historique enrichi,
  readiness exigeant les objets et permissions M4.
- 54 tests M4 PostgreSQL/API, cible Makefile, smoke HTTP, README et rapport de revue.

## Décisions et garanties

La clé unique `(user_id, operation_kind, key)` est acquise avec INSERT ON CONFLICT,
puis verrouillée. Le hash SHA-256 est calculé en PostgreSQL à partir d'un JSONB
canonique versionné et de paramètres typés. Un doublon concurrent attend le résultat
du détenteur ; payload identique : réponse initiale ; payload différent : 409.

Une transaction rassemble claim, transfert, journal, deux lignes, projections,
audit et réponse. Le commit est le seul point de réussite. Les rejets métier
404/409/422 sont conservés, y compris un refus pour solde insuffisant ; une nouvelle
tentative métier nécessite une nouvelle clé. Une erreur technique annule tout.
Les validations de forme et d'authentification précèdent le claim.

Après l'idempotence, les comptes sont verrouillés individuellement par UUID
croissant, puis leurs soldes relus. Ouvertures et transferts utilisent la même
primitive privée de paire. Les contraintes M3 de devise, équilibre, montants et
non-négativité restent actives. Des triggers différés imposent la correspondance
transfert/journal/paire/réponse. Les fonctions vérifient le propriétaire source et
les devises ; les rôles runtime/dev ne peuvent pas écrire directement les tables
financières ni appeler la primitive privée.

Le service retente toute la transaction locale au plus trois fois uniquement sur
40001/40P01. Une perte de connexion ou un commit ambigu n'est jamais rejoué
automatiquement. Le client peut reprendre avec sa même clé. Aucun HTTP externe
n'est effectué dans la transaction.

## Vérification exécutée

PostgreSQL 17 réel, profil Colima `payment-ledger`, Python 3.14 :

| Commande | Résultat |
| --- | --- |
| `.venv/bin/ruff check .` | PASS |
| `.venv/bin/ruff format --check .` | PASS, 53 fichiers |
| `.venv/bin/mypy` | PASS, 40 fichiers source |
| `.venv/bin/pytest -q --tb=short` | **196 passed in 122.38s**, aucun skip |
| `UV_CACHE_DIR=.local/uv-cache .venv/bin/uv lock --check --offline` | PASS, 46 paquets |
| `/opt/homebrew/bin/actionlint .github/workflows/ci.yml` | PASS |
| `/opt/homebrew/bin/shellcheck docker/init-db.sh` | PASS |
| `git diff --check` et `git diff --cached --check` | PASS |
| `DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose up --build --wait --wait-timeout 180` | PASS, DB/API healthy, migration exit 0 |
| `.venv/bin/python scripts/smoke_api.py` | PASS, HTTP réel identité/comptes/ownership/rejet/replay/conflit/logout |
| `.venv/bin/python -m payment_ledger.ledger.reconciliation` | PASS, aucune divergence |

La suite complète contient 16 tests unitaires et 180 tests d'intégration : les 142
tests M1–M3 précédents et 54 nouveaux cas M4. Chaque test DB utilise une base isolée.
Le smoke Compose utilise des wallets vides et teste les rejets ; les transferts
réussis avec fonds sont vérifiés via l'API TestClient et PostgreSQL réel isolé.

Couverture M4 :

- réponse originale, canonicalisation UUID, conflit sur chacun des paramètres,
  séparation des utilisateurs, clés obligatoires et bornées, montants stricts ;
- ownership SQL/HTTP, lectures privées, devises EUR/USD/JPY, wallets incompatibles ;
- opérations interdites pour payment_app, payment_worker et payment_dev,
  immutabilité même pour le propriétaire du schéma sans désactivation des triggers ;
- rollback sur crédit, audit, réponse et commit différé, puis reprise de la même clé ;
- concurrence : doublon, clés différentes sur solde insuffisant, payload
  contradictoire et transferts croisés ; barrières et observation des attentes SQL ;
- attente de clé suivie de commit ou rollback ; verrou inférieur bloqué et compte
  supérieur libre vérifié avec NOWAIT ; aucun délai aléatoire ;
- retries locaux et épuisement bornés par séquence non transactionnelle ; absence
  de retry sur SQLSTATE 08006 injecté (ne simule pas une coupure réseau au commit) ;
- migration vierge et chemins antérieurs via les suites M1–M3 ; M3 alimenté vers
  M4 puis retour sans transferts, conservation des ouvertures, readiness et ACL ;
- refus de downgrade M4 alimenté et refus de commit incomplet.

Le remplacement du bloc d'ouverture M3 par le helper partagé a aussi été vérifié
comme correspondant exactement à une occurrence. Les trois secrets DB configurés
localement sont absents du diff indexé, sans affichage de leur contenu.

## Revue et incidents

Le reviewer indépendant a appliqué `adversarial-review` et
`financial-invariant-review` : **15 passed, 39 deselected in 10.84s** pour sa sélection
de régressions. Verdict APPROVE et détails dans `reviews/M4-review.md`.
Son point sur la preuve de concurrence a conduit à renforcer les tests avec
observation de blocages PostgreSQL et couverture commit/rollback du détenteur.

Une première suite complète à la reprise a donné 16 PASS et 180 erreurs de setup,
car PostgreSQL était arrêté. Le profil Colima existant a été redémarré, Compose
reconstruit et la suite complète relancée avec le résultat vert ci-dessus. Les
erreurs initiales de lint/import/typage des nouveaux tests et du smoke ont été
corrigées. Aucun test ni contrôle requis désactivé ou sauté.

Docker a utilisé le builder classique, buildx absent ; le build et les healthchecks
ont réussi. Aucune déviation architecturale connue ni finding important non résolu.

## Limites et Git

La frontière de confiance du rôle applicatif reste celle du plan : le service
transmet l'utilisateur authentifié à la fonction SQL. Un administrateur/migrator
compromis reste hors garantie. Le rapprochement détecte sans réparation automatique.
Le downgrade refuse toute clé M4 conservée, même un rejet, pour préserver l'historique.

Branche `feature/m4-transfers`, fondée sur M3 fusionné. Les modifications préexistantes
de `AGENTS.md`, `.agents/skills/git-workflow/` et `reviews/M3-review.md` sont conservées
hors des commits M4. Aucune réécriture d'historique. La fusion M4 nécessite une
nouvelle autorisation utilisateur ; M5 n'a pas été commencé.
