# -*- coding: utf-8 -*-
"""Acces SQLite du serveur central Score X."""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from pathlib import Path

from .persistance import PersistanceDistante

RACINE = Path(__file__).resolve().parents[1]
# Dossier de travail : surchargeable pour pointer un volume persistant.
DATA = Path(os.environ.get("SCOREX_DATA") or RACINE / "data")
# Le referentiel est livre avec le code (image Docker), il n'est jamais ecrit.
REFERENTIEL = Path(os.environ.get("SCOREX_REFERENTIEL") or RACINE / "data" / "referentiel.sqlite")
CENTRAL = DATA / "central.sqlite"

# Une seule connexion est partagee par les threads du serveur. Ce verrou serialise
# les ecritures et l'instantane pris par la persistance distante.
VERROU = threading.RLock()

PERSISTANCE = PersistanceDistante()


def _init() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if CENTRAL.exists():
        return
    # Disque ephemere : on repart du dernier instantane publie s'il existe.
    if PERSISTANCE.restaurer(CENTRAL):
        return
    # Tout premier demarrage : la base part du referentiel issu du dataset.
    if not REFERENTIEL.exists():
        raise RuntimeError(
            "data/referentiel.sqlite manquant — lancer d'abord "
            "scripts/build_referentiel_db.py"
        )
    shutil.copyfile(REFERENTIEL, CENTRAL)


def connect() -> sqlite3.Connection:
    _init()
    con = sqlite3.connect(CENTRAL, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


DB = connect()


def commit(modifie: bool = True) -> None:
    """Valide la transaction, et signale la base a republier si elle a change.

    `modifie=False` pour une poussee qui n'a rien ecrit : sans cela, chaque
    synchronisation a vide republierait la base entiere dans le depot Dataset.
    """
    DB.commit()
    if modifie:
        PERSISTANCE.marquer_modifie()


def demarrer_persistance() -> None:
    PERSISTANCE.demarrer(DB, VERROU)


def arreter_persistance() -> None:
    PERSISTANCE.arreter()


def etat_persistance() -> dict:
    return PERSISTANCE.etat()


def sauvegarder_maintenant() -> bool:
    """Publication immediate, quel que soit l'intervalle. Utile avant une demo."""
    return PERSISTANCE.sauvegarder(force=True)
