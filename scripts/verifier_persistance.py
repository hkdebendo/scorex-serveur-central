# -*- coding: utf-8 -*-
"""Verifie hors ligne le cycle de persistance de la base centrale.

Ce que le script prouve, sans jeton ni reseau : une ecriture encore dans le
journal WAL se retrouve bien dans l'instantane publie, et un redemarrage sur
disque vide (le cas du conteneur Hugging Face) restitue exactement cette ecriture.

Le transport vers le depot Dataset est remplace par un dossier temporaire ; tout
le reste (instantane SQLite, drapeau de modification, restauration) est le code
de production.

    python scripts/verifier_persistance.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

ATELIER = Path(tempfile.mkdtemp(prefix="scorex-verif-"))
DEPOT = ATELIER / "depot_simule"
DEPOT.mkdir(parents=True)

# Doit etre pose avant l'import : app.db ouvre la connexion des le chargement.
os.environ["SCOREX_DATA"] = str(ATELIER / "data")

from app import db  # noqa: E402

DISTANT = DEPOT / "central.sqlite"
ok = True


def etape(libelle: str, condition: bool) -> None:
    global ok
    ok = ok and condition
    print(f"  [{'ok ' if condition else 'ECHEC'}] {libelle}")


def brancher_transport_simule() -> None:
    """Active la persistance et remplace ses seuls appels reseau par des copies."""
    p = db.PERSISTANCE
    p.depot, p.jeton, p.active = "simulation/scorex-central-db", "jeton-simule", True
    p.temporisation, p.intervalle = 1, 15  # raccourci pour que le script reste bref
    p._televerser = lambda chemin: shutil.copyfile(chemin, DISTANT)
    p._telecharger = lambda: DISTANT if DISTANT.exists() else None
    p._creer_depot = lambda: None


def inserer(identifiant: str) -> None:
    with db.VERROU:
        db.DB.execute(
            "INSERT INTO activite (id, conseiller_id, conseiller_nom, agence, action,"
            " cible_type, cible_id, detail_json, cree_le, sync_etat)"
            " VALUES (?, 1, 'Verification', 'Siege', 'test_persistance',"
            " 'systeme', ?, '{}', '2026-09-02T00:00:00', 'synchronise')",
            (identifiant, identifiant),
        )
        db.commit()


def present_dans(base: Path, identifiant: str) -> bool:
    con = sqlite3.connect(base)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM activite WHERE id=?", (identifiant,)
        ).fetchone()[0] == 1
    finally:
        con.close()


def main() -> int:
    print(f"Atelier : {ATELIER}\n")
    brancher_transport_simule()
    etape("persistance active", db.PERSISTANCE.active)

    db.demarrer_persistance()

    print("\n1. Ecriture puis publication")
    inserer("verif-1")
    etape("ecriture signalee comme a publier", db.PERSISTANCE.etat()["modifications_en_attente"])

    # Volontairement sans checkpoint : la transaction est encore dans le WAL,
    # exactement la situation ou une simple copie du .sqlite perdrait la donnee.
    wal = Path(str(db.CENTRAL) + "-wal")
    etape("transaction encore dans le journal WAL", wal.exists() and wal.stat().st_size > 0)

    etape("instantane publie", db.PERSISTANCE.sauvegarder())
    etape("depot alimente", DISTANT.exists())
    etape("plus rien en attente", not db.PERSISTANCE.etat()["modifications_en_attente"])
    etape("l'ecriture WAL est bien dans l'instantane", present_dans(DISTANT, "verif-1"))

    print("\n2. Publication automatique apres une ecriture")
    inserer("verif-2")
    limite = time.monotonic() + 10
    while time.monotonic() < limite and not present_dans(DISTANT, "verif-2"):
        time.sleep(0.2)
    etape("le fil de publication a reagi seul", present_dans(DISTANT, "verif-2"))

    print("\n3. Redemarrage sur disque vide")
    db.DB.close()
    for suffixe in ("", "-wal", "-shm"):
        Path(str(db.CENTRAL) + suffixe).unlink(missing_ok=True)
    etape("base locale effacee", not db.CENTRAL.exists())

    etape("restauration depuis le depot", db.PERSISTANCE.restaurer(db.CENTRAL))
    etape("la premiere ecriture a survecu", present_dans(db.CENTRAL, "verif-1"))
    etape("la seconde ecriture a survecu", present_dans(db.CENTRAL, "verif-2"))
    con = sqlite3.connect(db.CENTRAL)
    utilisateurs = con.execute("SELECT COUNT(*) FROM utilisateurs").fetchone()[0]
    con.close()
    etape("le referentiel est intact", utilisateurs > 0)

    print("\n4. Publication a l'arret")
    db.PERSISTANCE._con = sqlite3.connect(db.CENTRAL)
    db.PERSISTANCE.marquer_modifie()
    db.arreter_persistance()
    etape("instantane final ecrit", db.PERSISTANCE.etat()["derniere_sauvegarde"] is not None)
    etape("rien ne reste en attente", not db.PERSISTANCE.etat()["modifications_en_attente"])

    shutil.rmtree(ATELIER, ignore_errors=True)
    print("\n" + ("Cycle de persistance valide." if ok else "Des etapes ont echoue."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
