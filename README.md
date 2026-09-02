# Score X — Serveur central de synchronisation

FastAPI + SQLite. Reçoit les analyses, la traçabilité d'activité et les nouveaux
clients des postes conseillers, et alimente le tableau de bord du chef d'agence.

> MVP : SQLite (et non PostgreSQL) pour un déploiement sans friction. La logique
> d'accès aux données est isolée dans `app/db.py` — le passage à PostgreSQL ne touche
> pas les endpoints.

## Installer & lancer en local

```bash
pip install -r requirements.txt

# une fois : construire le référentiel depuis le dataset
python scripts/build_referentiel_db.py

python -m uvicorn app.main:app --port 8500 --reload
```

`data/central.sqlite` est créé au premier démarrage (copie de `referentiel.sqlite`),
puis enrichi par les synchronisations. Doc interactive : http://127.0.0.1:8500/docs

En local, la persistance distante reste inactive : la base vit simplement sur le disque.

## Endpoints

| Méthode | Route | Rôle |
|---|---|---|
| POST | `/auth/login` | `{email, password}` → `{token, user}` |
| GET | `/health` | test de disponibilité + état de la persistance |
| POST | `/sync/push` | pousse `{analyses, activites, clients}` (bearer) — *dernier écrit gagne* sur `maj_le` |
| GET | `/sync/pull?since=` | changements depuis une date |
| GET | `/dashboard/chef` | KPIs consolidés (réservé au chef d'agence) |
| POST | `/admin/sauvegarder` | force la publication d'un instantané (réservé au chef d'agence) |

## Persistance de la base

Sur un hébergement gratuit, le disque du conteneur est éphémère : il repart à zéro
à chaque redémarrage, et aucune offre gratuite ne propose de disque persistant.
`app/persistance.py` rend donc `central.sqlite` durable sans disque, en la publiant
dans un **dépôt Dataset Hugging Face privé** :

- au démarrage, le dernier instantané publié est restauré ;
- une écriture réveille la publication, qui part après une courte temporisation
  (les rafales de synchronisation sont regroupées) ;
- un instantané final est publié à l'extinction.

L'instantané passe par l'API `backup` de SQLite, et contient donc aussi les
transactions encore dans le journal WAL. Le dépôt étant versionné par git, chaque
instantané reste consultable et restaurable.

Variables d'environnement :

| Variable | Rôle | Défaut |
|---|---|---|
| `SCOREX_HF_DATASET` | dépôt cible, ex. `moncompte/scorex-central-db` | — |
| `HF_TOKEN` | jeton Hugging Face avec droit d'écriture | — |
| `SCOREX_SAVE_DEBOUNCE` | délai entre une écriture et sa publication (s) | `20` |
| `SCOREX_SAVE_EVERY` | vérification périodique de sécurité (s) | `120` |
| `SCOREX_DATA` | dossier de travail de la base | `data/` |

Sans `SCOREX_HF_DATASET` ni `HF_TOKEN`, la persistance distante est inactive et le
serveur fonctionne sur son disque local.

Vérification hors ligne du cycle complet (écriture WAL → instantané → redémarrage
sur disque vide → restauration), sans jeton ni réseau :

```bash
python scripts/verifier_persistance.py
```

## Déploiement

Voir `DEPLOIEMENT.md` : mise en ligne gratuite (Render pour l'API, dépôt Dataset
Hugging Face pour la base) et branchement de l'application dessus. Le `Dockerfile`
respecte la variable `PORT`, il convient donc aussi à Koyeb, Cloud Run ou un Space
Hugging Face.

## Résolution de conflit

MVP : **last-write-wins** sur `maj_le`. Deux écritures concurrentes hors-ligne sur le
même dossier → la dernière enregistrée l'emporte. Pas de fusion avancée (limite assumée,
à mentionner au jury).

## Comptes de démonstration

Le mot de passe est la partie locale de l'adresse : `aurelscore@gmail.com` →
`aurelscore`. Trois conseillers (`aurelscore`, `fideliascore`, `lionelscore`) et un
chef d'agence (`ricardoscore`).
