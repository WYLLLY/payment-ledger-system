# Milestone 2 — identité et comptes

Vérification locale du 8 octobre 2026 : **PASS**. Revue indépendante terminée,
aucun défaut significatif non résolu identifié. M2 reste soumis à l'approbation
de l'utilisateur. Aucun travail M3 n'a été entrepris.

## Fonctionnalités et architecture

- Inscription par e-mail ASCII validé et normalisé en minuscules ; unicité PostgreSQL.
- Mots de passe Argon2id : 64 MiB, trois itérations, quatre lanes, sel aléatoire.
- Connexion avec erreur générique et vérification Argon2 même pour un utilisateur absent.
- Sessions Bearer opaques : 32 octets aléatoires, seule l'empreinte SHA-256 persistée,
  expiration vérifiée selon l'horloge PostgreSQL et révocation à la déconnexion.
- Consultation de l'identité et création/liste/consultation des wallets du propriétaire.
  Une ressource tierce et une ressource inexistante donnent le même 404.
- Un wallet par utilisateur et devise EUR/USD/JPY, avec deux comptes `available` et
  `reserved`, initialement à zéro, et un audit dans une transaction unique.
- Rate limiting des routes inscription/connexion : compteur atomique PostgreSQL
  partagé entre processus, par IP directe ; échecs comptés, 429 avec `Retry-After`.
  La déconnexion d'une session valide reste possible quand ce quota est épuisé.
- Audits atomiques avec inscription, connexion, révocation et création de wallet.
  Les échecs de connexion ont leur propre transaction, sans secret.

Les routes HTTP restent minces. Les services possèdent les transactions explicites,
les modèles représentent la persistance, et le module de sécurité possède les
opérations cryptographiques. Le hachage Argon2 s'exécute hors transaction DB ouverte.
SQLAlchemy reste en READ COMMITTED ; aucune infrastructure ni interface spéculative
supplémentaire. Les comptes vides ne constituent pas un moteur ledger.

## Base de données et protections

`0002_identity_wallets` succède à `0001_foundation` et crée dans `payment` :
`users`, `sessions`, `wallets`, `ledger_accounts`, `audit_events`, `auth_rate_limits`.

L'unicité `(user_id, currency)` empêche les wallets dupliqués. L'unicité
`(wallet_id, account_kind)`, le CHECK des deux types autorisés et les constraint
triggers différés imposent exactement deux comptes au commit. Une FK composite
garantit la même devise que le wallet. Les soldes BIGINT sont non négatifs.

L'API reçoit des droits de lecture ciblés, des INSERT limités aux colonnes requises
des utilisateurs/sessions/audits et EXECUTE sur trois fonctions : création atomique
du wallet, révocation auditée et consommation du compteur auth. Les écritures
directes de wallets, comptes, soldes, révocations et compteurs sont interdites.
Les fonctions SECURITY DEFINER fixent `search_path = pg_catalog, pg_temp` et
qualifient les objets applicatifs. PUBLIC et le worker n'ont aucun droit d'exécution.
Le rôle de migration reste séparé de l'API. Le rôle applicatif est une frontière
de confiance explicite, conformément au plan approuvé.

Les erreurs HTTP ne reprennent ni paramètres SQL, ni entrées Pydantic, ni messages
ou clés arbitraires fournis par le client. Les réponses de session ne sont pas
cacheables. Compose désactive l'interprétation des en-têtes proxy par Uvicorn.
Le masquage des mots de passe CI et les régressions de confidentialité M1 sont conservés.
`/health/ready` exige le rôle applicatif, les objets M2 et leurs droits de lecture,
d'insertion et d'exécution. Il répond 503 si la base est restée à M1 ou si un droit
M2 requis est retiré. Les identifiants de migration restent dans le conteneur de
migration et dans la fixture de test ; l'API utilise uniquement `payment_app`.

## Fichiers

Créés :

- `migrations/versions/0002_identity_wallets.py`
- `src/payment_ledger/api.py`, `services.py`, `security.py`, `schemas.py`, `models.py`, `errors.py`
- `tests/integration/test_identity_wallets.py`, `tests/unit/test_security.py`
- `scripts/smoke_api.py`
- `docs/milestone-2-verification.md`

Modifiés :

- `src/payment_ledger/config.py`, `main.py`, `database.py` : paramètres auth,
  raccordement des routes et disponibilité liée aux objets et droits M2.
- `pyproject.toml`, `uv.lock` : Argon2 et validation e-mail ; marqueur de révision de test.
- `tests/integration/conftest.py`, `test_foundation.py` : choix explicite de la révision
  pour conserver le test du schéma vide M1, sans affaiblir ses assertions.
- `tests/unit/test_health.py` : contrat OpenAPI étendu ; routes santé conservées.
- `.env.example`, `compose.yaml`, `Dockerfile` : configuration auth et IP directe fiable.
- `.github/workflows/ci.yml` : parcours HTTP M2 ajouté aux sondes existantes.
- `README.md` : API, transactions, privilèges, limites et commandes.

## Tests et résultats

**90 tests PASS, aucun SKIPPED** : 36 intégration M1, 38 intégration M2,
11 tests unitaires M1 et 5 tests unitaires M2.

Les nouveaux tests couvrent inscription/validation/unicité, Argon2id et sels,
tokens aléatoires et stockage des empreintes, connexion invalide générique,
expiration sans temporisation, révocation, absence d'authentification, ownership,
devises, deux comptes à zéro, unicité, contraintes SQL et privilèges interdits.
Une régression suit readiness sur M1 → M2 → M1 et vérifie également qu'un droit
EXECUTE M2 retiré donne 503, puis 200 après sa restauration. Une seconde épuise
le quota anonyme depuis la même IP avant de vérifier logout 204, deux accès 401,
un compteur inchangé et un seul événement de révocation.

Des barrières synchronisent les inscriptions, créations de wallet et déconnexions
concurrentes, ainsi que les tentatives auth entre deux instances d'application.
Des triggers de panne testent le rollback lors du second compte et lors de l'audit
pour chacune des quatre transitions. Les fenêtres de rate limiting sont fixées en
base dans les tests afin d'éviter une dépendance à un franchissement de minute.

Chaque test d'intégration dispose d'une base PostgreSQL réelle isolée et supprimée
ensuite. Les migrations couvrent une base vierge, M1 → M2, M2 → M1 → M2 et
head → base → head. Le test du schéma M1 vide reste exécuté sur sa révision d'origine.

Commandes exécutées depuis la racine du dépôt :

| Commande | Résultat |
| --- | --- |
| `.venv/bin/ruff check .` | PASS |
| `.venv/bin/ruff format --check .` | PASS |
| `.venv/bin/mypy` | PASS — 28 fichiers source |
| `.venv/bin/pytest -q --tb=short` | PASS — 90 tests en 48,56 s |
| `UV_CACHE_DIR=.local/uv-cache .venv/bin/uv lock --check --offline` | PASS — 46 paquets résolus |
| `/opt/homebrew/bin/actionlint .github/workflows/ci.yml` | PASS |
| `/opt/homebrew/bin/shellcheck docker/init-db.sh` | PASS |
| `git diff --check` | PASS |
| `DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose config --quiet` | PASS |
| `DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose up --build --wait --wait-timeout 180` | PASS |
| `.venv/bin/python scripts/smoke_api.py` | PASS — santé, inscription, connexion, wallet, accès tiers et révocation via HTTP réel |
| `DOCKER_CONTEXT=colima-payment-ledger /opt/homebrew/bin/docker-compose ps -a` | PASS — DB/API healthy, migration sortie 0 |

Lecture SQL de `public.alembic_version` dans la base Compose :
`0002_identity_wallets`. Vérification locale supplémentaire : aucun des trois
mots de passe réels de `.env` dans les fichiers candidats Git ; `.env` reste ignoré.

La première passe Ruff a corrigé l'ordre d'import et le formatage. Le premier lot
de tests a révélé une assertion trop large (`input` recherché aussi dans le mot
« inputs » du message) ; l'assertion vérifie désormais l'absence du champ sensible.
La suite complète ci-dessus a été exécutée après ces corrections.

## Revue indépendante

Le sous-agent `milestone2_review`, distinct de l'implémenteur, a appliqué
`adversarial-review` et `financial-invariant-review` au code, aux migrations, aux
critères et aux tests. Il n'a modifié aucun fichier.

Il a examiné les frontières de confiance, tokens, mots de passe, expiration,
révocation concurrente, ownership, ACL et search_path, atomicité des comptes/audits,
contraintes de devises et soldes, rate limiting concurrent et erreurs sensibles.
Conclusion : **aucun défaut significatif non résolu**.

Exécutions indépendantes :

- `.venv/bin/python -m pytest tests/integration/test_identity_wallets.py tests/unit/test_security.py -q`
  → 36 PASS avant les six derniers ajouts.
- `.venv/bin/python -m pytest tests/integration/test_identity_wallets.py -q -k 'corrupt_hash or audit_failure or concurrent_logout'`
  → 6 PASS après les ajouts.
- `.venv/bin/python -m pytest tests/integration/test_identity_wallets.py -q -k 'concurrent or atomic or partial_failure or private or function_privileges'`
  → 6 PASS lors de sa première passe ciblée.
- `git diff --check` → PASS.

Les tentatives initiales sans autorisation réseau locale ont rencontré le sandbox,
puis les mêmes vérifications PostgreSQL ont passé avec l'accès local autorisé.
La revue interrompue par une limite d'utilisation a repris et s'est terminée.

Une revue indépendante complémentaire par `m2_correction_review` a porté sur
readiness et la déconnexion hors quota avec `adversarial-review`. Le reviewer a
consulté `financial-invariant-review` et conclu que ces corrections ne modifient
pas l'état financier. Ses deux tests PostgreSQL ciblés ont passé ; il n'a trouvé
aucun défaut significatif et n'a modifié aucun fichier.

## Limites et état Git

- Pas de vérification e-mail, récupération de mot de passe, refresh, gestion de
  sessions multiples par interface, ni nettoyage automatique des sessions/compteurs.
- L'inscription renvoie 409 pour une adresse existante. Le rate limit par IP partage
  le budget entre clients NAT et autorise les rafales en bordure de fenêtre ; il
  ne constitue pas une protection contre une attaque distribuée.
- La révocation n'annule pas les requêtes déjà authentifiées. Une future exposition
  publique requiert HTTPS et, si nécessaire, une politique explicite de proxy de confiance.
- Le smoke test local laisse deux utilisateurs fictifs et un wallet vide, avec
  sessions révoquées. Les autres tests nettoient leurs bases dédiées.
- L'environnement Mac n'a pas le plugin buildx ; Compose a utilisé avec succès le
  builder classique. Aucune vérification locale requise n'a été sautée.
- GitHub Actions distant : non exécuté, car aucun remote ni push n'a été demandé.
  Sa syntaxe et toutes les étapes applicables ont été vérifiées localement.
- M3, transferts, paiements, idempotence financière, outbox, worker, simulateur et
  réconciliation sont absents. Les garanties du futur ledger ne sont pas revendiquées.

Base Git : `main`, commit M1 `0edadff`. Le 10 octobre 2026, l'utilisateur a autorisé
la correction du dernier finding et le commit de M2. Aucun historique réécrit,
aucun remote créé, aucun push.

## Vérification finale après review indépendante — 10 octobre 2026

Le dernier finding Low concernait le test de logout à la frontière d'une fenêtre
de quota. Sa préparation fixe maintenant la fenêtre et le compteur épuisé ; deux
cas couvrent explicitement l'absence et la présence d'un rollover avant login.
Les assertions de révocation, refus d'accès, compteur et audit sont conservées.

La régression ciblée passe dans ses deux cas. La suite complète passe : **91 tests**,
dont 75 d'intégration sur PostgreSQL réel et 16 unitaires, sans skip. Ruff lint et
format, mypy, actionlint, shellcheck, migrations sur volume neuf, démarrage Compose,
healthchecks et smoke HTTP passent également. La relecture indépendante ciblée
approuve le correctif. Les commandes et résultats sont consignés dans
`reviews/M2-review.md`. Statut final local M2 : **PASS / APPROVE**.
