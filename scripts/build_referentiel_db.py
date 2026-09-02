# -*- coding: utf-8 -*-
"""
Construit la base SQLite canonique de Score X a partir de dataset_avec_noms.csv.

Sortie : scorex_server/data/referentiel.sqlite
  - contient TOUTES les tables (referentiel + operationnelles)
  - les tables operationnelles (analyses, activite...) sont vides
Ce meme fichier sert :
  - de base centrale au serveur de synchro (copie -> data/central.sqlite)
  - de base embarquee dans l'app Flutter (copie -> scorex_app/assets/db/referentiel.sqlite)

Regle registre : pour chaque client, le DERNIER pret du dataset est ignore
(il represente "la demande courante"). Les prets anterieurs forment l'historique
clos. Un client sans pret anterieur => primo (modele cold_start).

Usage :  python build_referentiel_db.py
"""
from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

RACINE = Path(__file__).resolve().parents[2]
CSV = RACINE / "dataset" / "dataset_avec_noms.csv"
OUT_DIR = RACINE / "scorex_server" / "data"
OUT = OUT_DIR / "referentiel.sqlite"
ASSET = RACINE / "scorex_app" / "assets" / "db" / "referentiel.sqlite"

RECOUVREMENT_DEFAUT = 0.40  # fraction remboursee sur un pret clos en defaut (reconstruite)
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

UTILISATEURS = [
    ("DOSSOU", "Aurel", "aurelscore@gmail.com", "aurelscore", "conseiller", "Cotonou"),
    ("EBA-DOVI", "Fidélia", "fideliascore@gmail.com", "fideliascore", "conseiller", "Abomey-Calavi"),
    ("ADOKONOU", "Lionel", "lionelscore@gmail.com", "lionelscore", "conseiller", "Porto-Novo"),
    ("AMOUSSOU", "Ricardo", "ricardoscore@gmail.com", "ricardoscore", "chef_agence", "Cotonou"),
]

SCHEMA = """
CREATE TABLE utilisateurs (
    id                 INTEGER PRIMARY KEY,
    nom                TEXT NOT NULL,
    prenom             TEXT NOT NULL,
    email              TEXT NOT NULL UNIQUE,
    role               TEXT NOT NULL,               -- 'conseiller' | 'chef_agence'
    mot_de_passe_hash  TEXT NOT NULL,
    agence             TEXT NOT NULL,
    actif              INTEGER NOT NULL DEFAULT 1,
    date_creation      TEXT NOT NULL
);

CREATE TABLE clients (
    id_client                INTEGER PRIMARY KEY,
    nom                      TEXT NOT NULL,
    prenoms                  TEXT NOT NULL,
    sexe                     TEXT NOT NULL,
    age                      INTEGER NOT NULL,
    situation_matrimoniale   TEXT NOT NULL,
    profession               TEXT NOT NULL,
    anciennete_activite      INTEGER NOT NULL,        -- mois
    localisation_activite    TEXT NOT NULL,
    tranche_revenu           TEXT NOT NULL,
    regularite_revenu        TEXT NOT NULL,
    nbr_mois_cotisation      INTEGER NOT NULL DEFAULT 0,
    montant_total_cotise     REAL NOT NULL DEFAULT 0,
    nbr_credits_anterieurs   INTEGER NOT NULL DEFAULT 0,
    nbr_credits_defaut       INTEGER NOT NULL DEFAULT 0,
    montant_total_emprunte   REAL NOT NULL DEFAULT 0,
    montant_total_rembourse  REAL NOT NULL DEFAULT 0,
    origine                  TEXT NOT NULL DEFAULT 'dataset',   -- 'dataset' | 'terrain'
    cree_par                 INTEGER,
    agence                   TEXT,
    date_creation            TEXT NOT NULL,
    maj_le                   TEXT NOT NULL
);
CREATE INDEX idx_clients_nom ON clients(nom, prenoms);

CREATE TABLE prets (
    id_pret            INTEGER PRIMARY KEY,
    id_client          INTEGER NOT NULL REFERENCES clients(id_client),
    date_pret          TEXT NOT NULL,
    date_fin           TEXT NOT NULL,
    montant_demande    REAL NOT NULL,
    duree_demande      INTEGER NOT NULL,
    nbr_echeances      INTEGER NOT NULL,
    taux_interet       REAL NOT NULL,
    mensualite         REAL NOT NULL,
    montant_rembourse  REAL NOT NULL,
    en_defaut          INTEGER NOT NULL DEFAULT 0,
    statut             TEXT NOT NULL DEFAULT 'clos'
);
CREATE INDEX idx_prets_client ON prets(id_client);

CREATE TABLE analyses (
    id                    TEXT PRIMARY KEY,
    id_client             INTEGER,
    client_nom            TEXT,
    client_prenoms        TEXT,
    conseiller_id         INTEGER,
    conseiller_nom        TEXT,
    agence                TEXT,
    cree_le               TEXT NOT NULL,
    mode                  TEXT NOT NULL DEFAULT 'hors-ligne',
    type_profil           TEXT,                       -- 'primo' | 'existant'
    modele                TEXT,
    montant_demande       REAL,
    duree_demande         INTEGER,
    objet_credit          TEXT,
    dossier_json          TEXT,
    score                 INTEGER,
    proba_defaut          REAL,
    bande                 TEXT,
    indice_confiance      REAL,
    revue_humaine         INTEGER DEFAULT 0,
    montant_recommande    REAL,
    duree_recommandee     INTEGER,
    mensualite_estimee    REAL,
    resultat_json         TEXT,
    decision              TEXT NOT NULL DEFAULT 'en_attente',
    decide_le             TEXT,
    est_brouillon         INTEGER NOT NULL DEFAULT 0,
    sync_etat             TEXT NOT NULL DEFAULT 'local',   -- 'local' | 'synchronise'
    sync_le               TEXT,
    maj_le                TEXT NOT NULL
);
CREATE INDEX idx_analyses_conseiller ON analyses(conseiller_id, cree_le);
CREATE INDEX idx_analyses_sync ON analyses(sync_etat);

CREATE TABLE activite (
    id             TEXT PRIMARY KEY,
    conseiller_id  INTEGER NOT NULL,
    conseiller_nom TEXT,
    agence         TEXT,
    action         TEXT NOT NULL,      -- 'connexion' | 'analyse' | 'decision' | 'creation_client' | 'sync'
    cible_type     TEXT,
    cible_id       TEXT,
    detail_json    TEXT,
    cree_le        TEXT NOT NULL,
    sync_etat      TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX idx_activite_conseiller ON activite(conseiller_id, cree_le);

CREATE TABLE meta (
    cle     TEXT PRIMARY KEY,
    valeur  TEXT
);
"""


def hash_mdp(mdp: str, iterations: int = 120_000) -> str:
    sel = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", mdp.encode(), sel, iterations)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"pbkdf2_sha256${iterations}${b64(sel)}${b64(dk)}"


def amortissement(montant: float, duree: int, taux: float) -> float:
    r = taux / 1200
    if r == 0:
        return montant / duree
    return montant * r / (1 - (1 + r) ** (-duree))


def main() -> None:
    if not CSV.exists():
        raise SystemExit(f"introuvable : {CSV}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    print(f"[1/5] Lecture {CSV.name}")
    df = pd.read_csv(CSV)
    df = df.sort_values(["id_client", "date_pret", "id_pret"]).reset_index(drop=True)
    print(f"      {len(df):,} prets, {df['id_client'].nunique():,} clients")

    con = sqlite3.connect(OUT)
    con.executescript(SCHEMA)

    print("[2/5] Utilisateurs")
    for i, (nom, prenom, email, mdp, role, agence) in enumerate(UTILISATEURS, start=1):
        con.execute(
            "INSERT INTO utilisateurs(id,nom,prenom,email,role,mot_de_passe_hash,agence,actif,date_creation)"
            " VALUES (?,?,?,?,?,?,?,1,?)",
            (i, nom, prenom, email, role, hash_mdp(mdp), agence, NOW),
        )

    print("[3/5] Clients (profil + agregats historique)")
    clients_rows, prets_rows = [], []
    agences = [u[5] for u in UTILISATEURS if u[4] == "conseiller"]
    for k, (cid, grp) in enumerate(df.groupby("id_client", sort=False)):
        grp = grp.reset_index(drop=True)
        courant = grp.iloc[-1]                 # demande "courante" -> ignoree
        histo = grp.iloc[:-1]                  # prets anterieurs clos
        nb = len(histo)
        nb_def = int(histo["en_defaut"].sum()) if nb else 0
        emp = float(histo["montant_demande"].sum()) if nb else 0.0
        remb = 0.0
        for _, pr in histo.iterrows():
            m = float(pr["montant_demande"])
            rb = m if int(pr["en_defaut"]) == 0 else round(m * RECOUVREMENT_DEFAUT)
            remb += rb
            prets_rows.append((
                int(pr["id_pret"]), int(cid), str(pr["date_pret"]), str(pr["date_fin"]),
                m, int(pr["duree_demande"]), int(pr["nbr_echeances"]),
                float(pr["taux_interet"]), round(amortissement(m, int(pr["duree_demande"]), float(pr["taux_interet"])), 2),
                rb, int(pr["en_defaut"]), "clos",
            ))
        clients_rows.append((
            int(cid), str(courant["nom"]), str(courant["prenoms"]), str(courant["sexe"]),
            int(courant["age"]), str(courant["situation_matrimoniale"]), str(courant["profession"]),
            int(courant["anciennete_activite"]), str(courant["localisation_activite"]),
            str(courant["tranche_revenu"]), str(courant["regularite_revenu"]),
            int(courant["nbr_mois_cotisation"]), float(courant["montant_total_cotise"]),
            nb, nb_def, emp, remb,
            "dataset", None, agences[k % len(agences)], NOW, NOW,
        ))

    con.executemany(
        "INSERT INTO clients VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", clients_rows)
    con.executemany(
        "INSERT INTO prets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", prets_rows)

    print(f"      {len(clients_rows):,} clients, {len(prets_rows):,} prets d'historique")
    primo = sum(1 for c in clients_rows if c[13] == 0)
    print(f"      primo (cold_start) : {primo:,}  |  existant : {len(clients_rows) - primo:,}")

    print("[4/5] Meta")
    for cle, val in {
        "schema_version": "1",
        "source": CSV.name,
        "genere_le": NOW,
        "recouvrement_defaut": str(RECOUVREMENT_DEFAUT),
        "date_evaluation": datetime.now().strftime("%Y-%m-%d"),
    }.items():
        con.execute("INSERT INTO meta VALUES (?,?)", (cle, val))

    con.commit()
    con.execute("VACUUM")
    con.close()

    import shutil
    shutil.copyfile(OUT, ASSET)
    print(f"[5/5] Ecrit : {OUT.relative_to(RACINE)}  ({OUT.stat().st_size/1024:.0f} Ko)")
    print(f"      Copie asset : {ASSET.relative_to(RACINE)}")


if __name__ == "__main__":
    main()
