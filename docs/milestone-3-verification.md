# Milestone 3 — Ledger et fixtures

Date : 10 octobre 2026. **Verification status: PASS (locale)**.
Revue indépendante : aucun finding significatif restant, correction de concurrence
vérifiée séparément. La CI distante est consultable sur la PR de `feature/m3-ledger`.

## Périmètre et plan

Source de vérité : `docs/approved-plan.md`, plan révisé. Le dernier document fourni
par l'utilisateur a été comparé octet pour octet au fichier initial : identique.
Il n'existe donc aucun changement du plan dans cette branche.

M3 uniquement : migration `0003_ledger`, journaux et lignes immuables, comptes
système funding/clearing par devise, fonctions de comptabilisation d'ouverture,
fixtures fixes, soldes projetés, historique privé et rapprochement interne.
Aucune implémentation des transferts M4, idempotence API, paiements, outbox ou worker.

Fichiers :

- `migrations/versions/0003_ledger.py` : schéma, contraintes, triggers, fonctions et ACL.
- `scripts/seed_demo.py`, `scripts/__init__.py` : CLI explicite dev/test et imports testables.
- `src/payment_ledger/ledger/{history,reconciliation}.py` : lecture et contrôle d'intégrité.
- `src/payment_ledger/{api,database,models}.py` : route, readiness M3, comptes système.
- `tests/integration/test_ledger.py` : 51 cas M3 sur PostgreSQL réel.
- Tests M2 : comptages limités aux comptes utilisateur pour conserver leurs assertions
  après ajout des six comptes système ; vérification des cinq fonctions M2 nommées.
- Test OpenAPI, `Makefile`, `README.md`, ce rapport et `reviews/M3-review.md`.

## Garanties et frontières

Chaque journal d'ouverture référence un wallet unique. Les FK composites imposent
la devise du wallet, du journal et des comptes. Deux constraint triggers différés
imposent exactement deux lignes opposées au commit, y compris pour un journal vide.
Les montants sont non nuls et bornés à ±10^12 unités mineures ; les soldes BIGINT
utilisent la plage entière, dont tout dépassement provoque un rollback.

La fonction privée de paire contrôle propriétaire, devise, types de comptes et
montant. Elle verrouille chaque compte dans l'ordre UUID, insère le journal et les
deux lignes, puis l'audit. Un trigger applique les deltas aux soldes dans la même
transaction, avec débit conditionnel ; les CHECK interdisent les soldes utilisateur
négatifs. Le chargement de plusieurs fixtures préverrouille l'ensemble des comptes
dans l'ordre global avant les ouvertures. Aucun appel HTTP dans ces transactions.

API et worker ne peuvent pas écrire directement journaux/lignes/soldes ni appeler
les ouvertures. Le rôle dev peut seulement appeler la fonction de fixture à montants
fixes, pas la primitive privée. Les fonctions privilégiées ont un search_path fixé.
UPDATE, DELETE et TRUNCATE du ledger sont refusés, même pour le propriétaire sans
modification préalable du schéma. L'administrateur/migrator reste hors garantie.

Les migrations normales et le conteneur de démonstration ne provisionnent pas le
rôle dev. Seul le script explicitement activé l'établit sur le cluster dev/test.
Les fixtures créent six ouvertures fixes ; un second lancement n'en ajoute aucune.

Le rapprochement lit un snapshot unique : projection/somme des lignes, paire de
chaque journal, conservation par devise et soldes utilisateur. Il signale sans
réparer. L'historique HTTP filtre le propriétaire, masque les comptes système et
borne la pagination ; un wallet tiers donne le même 404 qu'un wallet absent.

## Vérification exécutée

Depuis la racine du dépôt, sur PostgreSQL 17 réel :

| Commande | Résultat |
| --- | --- |
| `.venv/bin/ruff check .` | PASS |
| `.venv/bin/ruff format --check .` | PASS |
| `.venv/bin/mypy` | PASS, 35 fichiers source |
| `.venv/bin/pytest -q --tb=short` | **142 passed in 83.75s**, aucun skip |
| `UV_CACHE_DIR=.local/uv-cache .venv/bin/uv lock --check --offline` | PASS, 46 paquets |
| `/opt/homebrew/bin/actionlint .github/workflows/ci.yml` | PASS |
| `/opt/homebrew/bin/shellcheck docker/init-db.sh` | PASS |
| `git diff --check` | PASS |
| `DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose up --build --wait --wait-timeout 180` | PASS, DB/API healthy, migration exit 0 |
| `.venv/bin/python scripts/smoke_api.py` | PASS, parcours HTTP réel M1/M2 |
| `.venv/bin/python -m payment_ledger.ledger.reconciliation` | PASS, aucune divergence |

Suite complète : 16 tests unitaires + 126 tests d'intégration (75 M1/M2, 51 M3).
Chaque test DB crée sa base isolée puis la supprime. Couverture :

- migrations sur base vierge, M1→head, M2→M3, M3 vide→M2→M3, head vide→base→head ;
- refus explicite de downgrade d'un ledger alimenté, sans suppression de données ;
- rôles autorisés/interdits, absence de grants PUBLIC et de financement runtime ;
- paires vides/incomplètes/déséquilibrées/surnuméraires, devises, montants et ownership ;
- rollback lors de la seconde ligne, de l'audit et avant commit ; overflow BIGINT ;
- ouvertures concurrentes identiques/distinctes, débit concurrent insuffisant au
  niveau du trigger, ordre des verrous observé avec `pg_blocking_pids` et `NOWAIT` ;
- dérive de projection et journal corrompu injectés par le propriétaire du schéma,
  détectés sans réparation ; historique privé, pagination et readiness M3.

Vérification supplémentaire du CLI réel dans une base temporaire neuve :
`scripts/seed_demo.py` exécuté deux fois avec `PAYMENT_LEDGER_DEV_FIXTURES=1` et un
mot de passe local aléatoire fourni via environnement, puis module reconciliation.
Trois codes de sortie 0 ; exactement six journaux et douze lignes ; résultat PASS.
La base temporaire a ensuite été supprimée. Aucun secret imprimé ou commité ;
contrôle des trois secrets DB configurés contre le diff indexé : PASS.

## Échecs rencontrés et corrections

- Premier test impossible : PostgreSQL local arrêté. Redémarrage du profil Colima
  existant, puis reprise des tests. Aucun test DB remplacé par un mock ou sauté.
- Première passe ledger : 45 PASS, 3 FAIL. Deux échecs de downgrade dus aux triggers
  différés en attente après suppression des comptes système ; correction par
  `SET CONSTRAINTS ALL IMMEDIATE` avant DDL. L'autre test TRUNCATE rencontrait la FK
  avant le trigger ; la tentative vise désormais journals+entries pour tester celui-ci.
- Première suite M1–M3 : 137 PASS, 1 FAIL. Le test M2 comptait toutes les fonctions du
  schéma ; il cible désormais explicitement ses cinq fonctions, sans retirer ses
  assertions de sécurité. Les nouvelles fonctions ont leurs tests M3.
- Revue indépendante : risque de deadlock du batch fixtures, reproduit puis corrigé
  par ordre global des comptes ; régression déterministe ajoutée. Détails dans
  `reviews/M3-review.md`. Le reviewer a exécuté indépendamment trois régressions :
  **3 passed, 48 deselected in 2.10s**.
- Ruff a corrigé imports/formatage initiaux. Mypy nécessitait `scripts/__init__.py`
  pour un seul nom de module. Tous les contrôles finaux passent après corrections.

Aucun contrôle local requis sauté. Le builder Docker classique a remplacé buildx
absent sur ce Mac ; construction et healthchecks ont réussi.

## Limites et Git

- Seules les ouvertures sont activées en M3 ; les autres types/phases arriveront
  avec leurs objets métier dans les milestones correspondants.
- Rôles PostgreSQL globaux au cluster : séparer cluster dev/test et démonstration.
- Le rapprochement est une commande à la demande, sans réparation automatique.
- Downgrade d'un ledger alimenté volontairement refusé pour préserver l'historique.
- Branche `feature/m3-ledger`. Aucun merge, aucune réécriture d'historique.
- Modifications préexistantes de `AGENTS.md` et `.agents/skills/git-workflow/`
  conservées hors des commits M3.
