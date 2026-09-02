# -*- coding: utf-8 -*-
"""
Serveur central de synchronisation Score X.

Role : recevoir les analyses / activites / nouveaux clients des postes conseillers
(quand ils ont une connexion) et alimenter le tableau de bord du chef d'agence.

Resolution de conflit : dernier ecrit gagne (last-write-wins sur `maj_le`).

Lancer :  uvicorn app.main:app --reload --port 8500
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .db import (
    DB,
    VERROU,
    arreter_persistance,
    commit,
    demarrer_persistance,
    etat_persistance,
    sauvegarder_maintenant,
)
from .security import creer_session, user_id_pour, verifier_mdp


@asynccontextmanager
async def cycle_de_vie(_: FastAPI):
    """Publie la base a intervalle regulier, et une derniere fois a l'extinction.

    Indispensable sur un hebergement au disque ephemere : sans cela, tout ce que
    la synchronisation a recu disparait au redemarrage du conteneur.
    """
    demarrer_persistance()
    yield
    arreter_persistance()


app = FastAPI(title="Score X — Serveur central", version="1.0.0", lifespan=cycle_de_vie)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
#  Auth                                                                        #
# --------------------------------------------------------------------------- #
class LoginIn(BaseModel):
    email: str
    password: str


def _user_dict(row) -> dict:
    return {"id": row["id"], "nom": row["nom"], "prenom": row["prenom"],
            "email": row["email"], "role": row["role"], "agence": row["agence"]}


@app.post("/auth/login")
def login(body: LoginIn):
    row = DB.execute("SELECT * FROM utilisateurs WHERE email = ? AND actif = 1",
                     (body.email.strip().lower(),)).fetchone()
    if not row or not verifier_mdp(body.password, row["mot_de_passe_hash"]):
        raise HTTPException(401, "Identifiants invalides")
    return {"token": creer_session(row["id"]), "user": _user_dict(row)}


def utilisateur_courant(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Jeton manquant")
    uid = user_id_pour(authorization.split(" ", 1)[1])
    if uid is None:
        raise HTTPException(401, "Session expiree")
    row = DB.execute("SELECT * FROM utilisateurs WHERE id = ?", (uid,)).fetchone()
    return _user_dict(row)


@app.get("/health")
def health():
    return {"ok": True, "service": "scorex-central", "time": now(),
            "persistance": etat_persistance()}


# --------------------------------------------------------------------------- #
#  Synchronisation  (poste conseiller -> central)                              #
# --------------------------------------------------------------------------- #
class SyncIn(BaseModel):
    analyses: list[dict[str, Any]] = []
    activites: list[dict[str, Any]] = []
    clients: list[dict[str, Any]] = []


ANALYSE_COLS = [
    "id", "id_client", "client_nom", "client_prenoms", "conseiller_id", "conseiller_nom",
    "agence", "cree_le", "mode", "type_profil", "modele", "montant_demande", "duree_demande",
    "objet_credit", "dossier_json", "score", "proba_defaut", "bande", "indice_confiance",
    "revue_humaine", "montant_recommande", "duree_recommandee", "mensualite_estimee",
    "resultat_json", "decision", "decide_le", "est_brouillon", "sync_etat", "sync_le", "maj_le",
]
CLIENT_COLS = [
    "id_client", "nom", "prenoms", "sexe", "age", "situation_matrimoniale", "profession",
    "anciennete_activite", "localisation_activite", "tranche_revenu", "regularite_revenu",
    "nbr_mois_cotisation", "montant_total_cotise", "nbr_credits_anterieurs", "nbr_credits_defaut",
    "montant_total_emprunte", "montant_total_rembourse", "origine", "cree_par", "agence",
    "date_creation", "maj_le",
]
ACTIVITE_COLS = ["id", "conseiller_id", "conseiller_nom", "agence", "action", "cible_type",
                 "cible_id", "detail_json", "cree_le", "sync_etat"]


def _upsert(table: str, cols: list[str], key: str, rows: list[dict], lww: bool) -> int:
    n = 0
    place = ",".join("?" * len(cols))
    for r in rows:
        vals = [r.get(c) for c in cols]
        vals[cols.index("sync_etat")] = "synchronise" if "sync_etat" in cols else None
        existing = DB.execute(f"SELECT * FROM {table} WHERE {key} = ?", (r.get(key),)).fetchone()
        if existing is None:
            DB.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({place})", vals)
            n += 1
        elif lww and str(r.get("maj_le") or "") >= str(existing["maj_le"] or ""):
            sets = ",".join(f"{c}=?" for c in cols if c != key)
            DB.execute(f"UPDATE {table} SET {sets} WHERE {key}=?",
                       [v for c, v in zip(cols, vals) if c != key] + [r.get(key)])
            n += 1
    return n


@app.post("/sync/push")
def sync_push(body: SyncIn, user: dict = Depends(utilisateur_courant)):
    # Le verrou serialise les poussees concurrentes entre elles et vis-a-vis de
    # l'instantane pris par la persistance distante.
    with VERROU:
        recu = {
            "clients": _upsert("clients", CLIENT_COLS, "id_client", body.clients, lww=True),
            "analyses": _upsert("analyses", ANALYSE_COLS, "id", body.analyses, lww=True),
            "activites": _upsert("activite", ACTIVITE_COLS, "id", body.activites, lww=False),
        }
        commit()
    return {"recus": recu, "server_time": now()}


@app.get("/sync/pull")
def sync_pull(since: str = "1970-01-01", user: dict = Depends(utilisateur_courant)):
    an = DB.execute("SELECT * FROM analyses WHERE maj_le > ?", (since,)).fetchall()
    cl = DB.execute("SELECT * FROM clients WHERE maj_le > ? AND origine='terrain'", (since,)).fetchall()
    return {"analyses": [dict(r) for r in an], "clients": [dict(r) for r in cl], "server_time": now()}


# --------------------------------------------------------------------------- #
#  Tableau de bord chef d'agence  (consolide)                                  #
# --------------------------------------------------------------------------- #
@app.get("/dashboard/chef")
def dashboard_chef(user: dict = Depends(utilisateur_courant)):
    if user["role"] != "chef_agence":
        raise HTTPException(403, "Reserve au chef d'agence")

    reels = "WHERE est_brouillon = 0"
    total = DB.execute(f"SELECT COUNT(*) n FROM analyses {reels}").fetchone()["n"]
    accept = DB.execute(f"SELECT COUNT(*) n FROM analyses {reels} AND decision='valide'").fetchone()["n"]
    refus = DB.execute(f"SELECT COUNT(*) n FROM analyses {reels} AND decision='refuse'").fetchone()["n"]
    revues = DB.execute(f"SELECT COUNT(*) n FROM analyses {reels} AND revue_humaine=1").fetchone()["n"]
    risque_eleve = DB.execute(f"SELECT COUNT(*) n FROM analyses {reels} AND bande='élevé'").fetchone()["n"]
    score_moy = DB.execute(f"SELECT AVG(score) m FROM analyses {reels}").fetchone()["m"]

    dist = {b: DB.execute(f"SELECT COUNT(*) n FROM analyses {reels} AND bande=?", (b,)).fetchone()["n"]
            for b in ("faible", "modéré", "élevé")}

    par_conseiller = DB.execute(f"""
        SELECT u.id, u.nom, u.prenom, u.agence,
               COUNT(a.id)                                   AS dossiers,
               AVG(a.score)                                  AS score_moyen,
               SUM(CASE WHEN a.decision='valide' THEN 1 ELSE 0 END)  AS acceptes,
               SUM(CASE WHEN a.revue_humaine=1 THEN 1 ELSE 0 END)    AS revues
        FROM utilisateurs u
        LEFT JOIN analyses a ON a.conseiller_id = u.id AND a.est_brouillon = 0
        WHERE u.role = 'conseiller'
        GROUP BY u.id ORDER BY dossiers DESC
    """).fetchall()

    perf = DB.execute(f"""
        SELECT substr(cree_le,1,10) AS jour,
               COUNT(*) AS analyses,
               SUM(CASE WHEN decision='valide' THEN 1 ELSE 0 END) AS acceptes,
               SUM(CASE WHEN bande='élevé' THEN 1 ELSE 0 END)     AS risque
        FROM analyses {reels}
        GROUP BY jour ORDER BY jour
    """).fetchall()

    agences = DB.execute("""
        SELECT agence,
               MAX(sync_le) AS derniere_sync,
               SUM(CASE WHEN sync_etat='local' THEN 1 ELSE 0 END) AS en_attente
        FROM analyses GROUP BY agence
    """).fetchall()

    return {
        "kpis": {
            "dossiers_analyses": total,
            "taux_acceptation": round(100 * accept / total, 1) if total else 0,
            "taux_refus": round(100 * refus / total, 1) if total else 0,
            "score_moyen": round(score_moy) if score_moy else 0,
            "risque_eleve": risque_eleve,
            "revues_humaines": revues,
        },
        "distribution_risque": dist,
        "activite_conseillers": [dict(r) for r in par_conseiller],
        "performance": [dict(r) for r in perf],
        "agences": [dict(r) for r in agences],
        "server_time": now(),
    }


# --------------------------------------------------------------------------- #
#  Exploitation                                                                #
# --------------------------------------------------------------------------- #
@app.post("/admin/sauvegarder")
def sauvegarder(user: dict = Depends(utilisateur_courant)):
    """Force la publication d'un instantane sans attendre l'intervalle."""
    if user["role"] != "chef_agence":
        raise HTTPException(403, "Reserve au chef d'agence")
    publie = sauvegarder_maintenant()
    return {"publie": publie, "persistance": etat_persistance(), "server_time": now()}
