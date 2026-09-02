# Mettre le serveur central en ligne, gratuitement

Objectif : une API joignable depuis n'importe où, et une base centrale qui survit
aux redémarrages, sans rien payer et sans carte bancaire.

Le principe tient en une phrase : **le calcul et le stockage sont séparés**. Un
hébergeur gratuit exécute l'API dans un conteneur au disque jetable ; un dépôt
Dataset Hugging Face privé détient la base et, lui, ne s'efface jamais.

```
   App Flutter  ──HTTPS──>  Render (API FastAPI, conteneur Docker)
                                  │
                    instantané ~20 s après chaque écriture,
                    et systématiquement avant extinction
                                  v
                       Dataset privé  scorex-central-db
                            (central.sqlite, versionné)
```

C'est cette séparation qui rend la mise en veille inoffensive : le conteneur peut
disparaître à tout moment, la base est ailleurs.

> **Pourquoi pas un Space Hugging Face.** Depuis juillet 2026, créer un Space
> Docker ou Gradio exige un abonnement PRO ; seuls les Spaces statiques restent
> gratuits. Le dépôt **Dataset**, lui, reste gratuit et accessible par API sans
> PRO : on ne garde donc de Hugging Face que le stockage. Si vous disposez déjà
> d'un compte PRO, l'annexe en fin de document donne la variante Space.

---

## 1. Le dépôt qui détient la base (Hugging Face)

1. Créer un compte sur https://huggingface.co (gratuit, sans carte).
2. https://huggingface.co/settings/tokens → **Create new token**, type **Write**.
   Le nommer `scorex-serveur` et copier la valeur : elle ne sera plus affichée.
3. https://huggingface.co/new-dataset → nom `scorex-central-db`, visibilité
   **Private**.

Rien d'autre à y faire : le serveur y écrira `central.sqlite` tout seul. Retenir
l'identifiant complet, de la forme `<votre-compte>/scorex-central-db`.

## 2. Le dépôt de code (GitHub)

Render déploie depuis un dépôt git. Créer un dépôt GitHub, par exemple
`scorex-serveur-central`, et y placer **le contenu du dossier `scorex_server/`**
(et non le dossier lui-même : le `Dockerfile` doit être à la racine).

```bash
cd "scorex_server"
git init
git add .
git commit -m "Serveur central Score X"
git remote add origin https://github.com/<votre-compte>/scorex-serveur-central.git
git push -u origin main
```

Le `.gitignore` fourni exclut déjà `data/central.sqlite` : c'est la base vivante,
elle ne doit jamais être versionnée ici. En revanche `data/referentiel.sqlite`
(1,8 Mo) doit bien être envoyé, il sert de base de départ au tout premier
démarrage. Le pousser en fichier git ordinaire, **sans Git LFS** : rien ne
garantit que l'hébergeur récupère les objets LFS, et la construction de l'image
échouerait sur un fichier de pointeur au lieu de la base.

Le dépôt peut être privé, Render sait s'y connecter.

## 3. Le service qui exécute l'API (Render)

1. Créer un compte sur https://render.com (gratuit, sans carte bancaire).
2. **New** → **Web Service** → connecter le dépôt GitHub de l'étape 2.
3. Réglages :
   - Language / Runtime : **Docker**
   - Instance type : **Free**
   - Health check path : `/health`
4. **Environment** → ajouter :

| Nom | Valeur |
|---|---|
| `HF_TOKEN` | le jeton de l'étape 1 |
| `SCOREX_HF_DATASET` | `<votre-compte>/scorex-central-db` |
| `SCOREX_SAVE_DEBOUNCE` | `20` |

5. **Create Web Service**.

Le fichier `render.yaml` fourni décrit déjà ce service : en passant par
**New → Blueprint**, Render le lit et ne demande plus que les deux secrets.

Suivre les logs de démarrage. La ligne attendue est :

```
[persistance] publication vers <compte>/scorex-central-db : 20 s apres une ecriture, ...
```

Si elle affiche `inactive`, c'est que `HF_TOKEN` ou `SCOREX_HF_DATASET` manque.

## 4. Vérifier

Ouvrir `https://<votre-service>.onrender.com/health` :

```json
{"ok": true, "persistance": {"active": true, "depot": "<compte>/scorex-central-db", ...}}
```

`active: true` est le point à contrôler.

**Test de durabilité, à faire une fois avant le jury** — c'est lui qui prouve que
la promesse tient :

1. Se connecter depuis l'application et synchroniser un dossier.
2. Attendre ~30 s, puis vérifier que le dépôt Dataset porte un nouveau commit
   `Instantane ...` sur `central.sqlite`.
3. Dans Render : **Manual Deploy** → **Restart service** (le conteneur repart de zéro).
4. Rouvrir le tableau de bord chef : le dossier est toujours là.

## 5. Pointer l'application sur le serveur

Au lancement ou à la compilation :

```bash
flutter run -d windows --dart-define=SCOREX_SERVEUR_URL=https://<votre-service>.onrender.com
flutter build windows --release --dart-define=SCOREX_SERVEUR_URL=https://<votre-service>.onrender.com
```

Pour figer l'URL une fois pour toutes, remplacer la `defaultValue` de
`urlServeurDefaut` dans `scorex_app/lib/data/sync.dart`.

L'application reste évidemment fonctionnelle hors ligne : le serveur central ne
sert qu'à la synchronisation et au tableau de bord du chef d'agence.

---

## Ce qu'il faut savoir

**Mise en veille.** L'offre gratuite de Render endort le service après 15 minutes
sans trafic, et le réveil demande environ une minute. Les délais réseau de
l'application sont réglés pour l'absorber, mais une minute d'attente devant un
jury se remarque : **ouvrir l'URL `/health` dans un navigateur quelques minutes
avant la démonstration** suffit à le réveiller.

Pour l'éviter complètement le jour J, un service de ping gratuit (cron-job.org,
par exemple) qui appelle `/health` toutes les 10 minutes maintient le service
éveillé. L'offre gratuite couvre 750 h par mois, soit un peu plus qu'un mois
complet, donc un service unique tient sans dépasser le quota.

**Fenêtre de perte.** Une extinction normale (mise en veille, redémarrage,
nouveau déploiement) ne perd rien : Render envoie un SIGTERM, et le serveur
publie un instantané final avant de s'arrêter. Seule une coupure brutale pourrait
perdre les écritures des toutes dernières secondes, d'où la temporisation courte.
En cas de doute avant une manipulation, le chef d'agence peut forcer une
publication :

```bash
curl -X POST https://<votre-service>.onrender.com/admin/sauvegarder \
     -H "Authorization: Bearer <jeton de session>"
```

**Un seul worker, un seul service.** La connexion SQLite, les sessions en mémoire
et le fil de publication appartiennent au processus : ne pas augmenter
`--workers`, et surtout ne jamais faire tourner deux instances sur le même dépôt
Dataset, elles s'écraseraient mutuellement.

**Sécurité.** L'API est publique. Les routes sensibles (`/sync/*`,
`/dashboard/chef`, `/admin/*`) sont protégées par l'authentification Score X,
mais `/docs` et `/health` sont ouverts et les comptes de démonstration ont des
mots de passe devinables. Le dataset embarqué étant synthétique, c'est sans
conséquence pour le hackathon ; il ne faut pas y verser de données réelles.

**Reconstruire le référentiel.** Après un `build_referentiel_db.py`, renvoyer
`data/referentiel.sqlite` dans le dépôt GitHub. Attention : cela ne remplace pas
la base vivante, qui continue d'être restaurée depuis le Dataset. Pour repartir
de zéro, supprimer `central.sqlite` du dépôt Dataset, puis redémarrer le service.

---

## Autres hébergeurs

Le `Dockerfile` lit la variable `PORT` et n'a aucune dépendance à Render. Le
service se déplace donc sans modification de code :

| Hébergeur | Intérêt | Réserve |
|---|---|---|
| **Koyeb** (free) | ne s'endort qu'après 1 h d'inactivité | carte parfois demandée pour la vérification |
| **Google Cloud Run** | palier gratuit permanent, réveil en quelques secondes | compte de facturation avec carte obligatoire |
| **Space Hugging Face** | tout au même endroit que le Dataset | exige un abonnement PRO depuis juillet 2026 |

### Annexe : variante Space Hugging Face (compte PRO)

Le Space attend le port 7860, ce que le `Dockerfile` fait déjà par défaut.
Créer le Space en SDK **Docker**, y envoyer le contenu de `scorex_server/`, puis
placer `HF_TOKEN` en *secret* et `SCOREX_HF_DATASET` en *variable*. Le `README.md`
doit alors porter en tête l'en-tête attendu par la plateforme :

```yaml
---
title: Score X Serveur Central
emoji: 📊
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---
```
