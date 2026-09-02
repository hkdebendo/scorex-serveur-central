# -*- coding: utf-8 -*-
"""Persistance de la base centrale dans un depot Dataset Hugging Face.

Pourquoi : sur un hebergement gratuit (Hugging Face Space, entre autres) le disque
du conteneur est ephemere. Il est remis a zero a chaque redemarrage, chaque
reconstruction et chaque sortie de veille. `central.sqlite` y disparaitrait avec
toutes les synchronisations recues.

Principe : la base ne vit pas dans le conteneur mais dans un depot Dataset prive.
Au demarrage on restaure le dernier instantane publie ; pendant la vie du service
on republie la base des qu'elle a change, puis une derniere fois a l'arret.
Le depot etant versionne par git, chaque instantane reste consultable.

Instantane coherent : la base tourne en mode WAL, copier le seul fichier .sqlite
perdrait les transactions encore dans le journal. On passe donc par l'API de
sauvegarde de SQLite (`Connection.backup`), qui produit un fichier complet et
coherent sans interrompre le service.

Configuration, par variables d'environnement :

  SCOREX_HF_DATASET     depot cible, ex. "moncompte/scorex-central-db"
  HF_TOKEN              jeton Hugging Face disposant du droit d'ecriture
  SCOREX_SAVE_EVERY     filet de securite periodique, en secondes (defaut 120)
  SCOREX_SAVE_DEBOUNCE  delai entre une ecriture et sa publication (defaut 20)

Fenetre de perte : une ecriture reveille immediatement le fil de publication,
qui patiente le temps de la temporisation pour regrouper une rafale de
synchronisations en une seule publication. Une extinction brutale ne peut donc
perdre que les quelques secondes de cette temporisation, et une extinction
normale ne perd rien du tout.

Sans SCOREX_HF_DATASET ni HF_TOKEN la persistance distante reste simplement
inactive : le serveur fonctionne alors sur son disque local, ce qui est le
comportement attendu en developpement.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

FICHIER_DISTANT = "central.sqlite"


def _secondes(nom: str, defaut: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(nom, "")))
    except ValueError:
        return defaut


def _horodatage() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class PersistanceDistante:
    """Rend `central.sqlite` durable malgre un disque de conteneur ephemere."""

    def __init__(self) -> None:
        self.depot = (os.environ.get("SCOREX_HF_DATASET") or "").strip()
        self.jeton = (
            os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or ""
        ).strip()
        self.intervalle = _secondes("SCOREX_SAVE_EVERY", 120, minimum=15)
        self.temporisation = _secondes("SCOREX_SAVE_DEBOUNCE", 20, minimum=1)
        self.active = bool(self.depot and self.jeton)

        self.derniere_sauvegarde: str | None = None
        self.derniere_erreur: str | None = None

        self._modifie = False
        self._con: sqlite3.Connection | None = None
        self._verrou: threading.RLock | None = None
        self._arret = threading.Event()
        self._reveil = threading.Event()
        # Empeche le fil periodique et la publication d'arret de se chevaucher.
        self._publication = threading.Lock()
        self._fil: threading.Thread | None = None
        self._api = None

    # ----------------------------------------------------------------- outils
    def _client(self):
        if self._api is None:
            from huggingface_hub import HfApi

            self._api = HfApi(token=self.jeton)
        return self._api

    def _journal(self, message: str) -> None:
        print(f"[persistance] {message}", flush=True)

    # ------------------------------------------------------------ restauration
    def restaurer(self, cible: Path) -> bool:
        """Ecrit le dernier instantane publie dans `cible`.

        Renvoie False s'il n'y a rien a restaurer (persistance inactive, depot
        vide ou inaccessible) : l'appelant repart alors du referentiel.
        """
        if not self.active:
            return False
        try:
            source = self._telecharger()
        except Exception as err:  # noqa: BLE001 - on ne bloque jamais le demarrage
            self.derniere_erreur = f"restauration : {err}"
            self._journal(f"aucun instantane restaure ({err})")
            return False
        if source is None:
            self._journal("depot encore vide, demarrage depuis le referentiel")
            return False

        cible.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, cible)
        # Un journal WAL laisse par une execution precedente decrirait un etat
        # different de l'instantane : on repart d'une base propre.
        for suffixe in ("-wal", "-shm"):
            reste = cible.with_name(cible.name + suffixe)
            if reste.exists():
                reste.unlink()
        self._journal(f"base restauree depuis {self.depot}")
        return True

    def _telecharger(self) -> Path | None:
        from huggingface_hub import hf_hub_download
        from huggingface_hub.utils import EntryNotFoundError, RepositoryNotFoundError

        try:
            return Path(
                hf_hub_download(
                    repo_id=self.depot,
                    repo_type="dataset",
                    filename=FICHIER_DISTANT,
                    token=self.jeton,
                )
            )
        except (EntryNotFoundError, RepositoryNotFoundError):
            self._creer_depot()
            return None

    def _creer_depot(self) -> None:
        try:
            self._client().create_repo(
                self.depot, repo_type="dataset", private=True, exist_ok=True
            )
        except Exception as err:  # noqa: BLE001
            self.derniere_erreur = f"creation du depot : {err}"
            self._journal(f"depot indisponible ({err})")

    # -------------------------------------------------------------- publication
    def marquer_modifie(self) -> None:
        """Signale une ecriture et reveille aussitot le fil de publication."""
        self._modifie = True
        self._reveil.set()

    def sauvegarder(self, force: bool = False) -> bool:
        """Publie un instantane si la base a change depuis la derniere passe."""
        if not self.active or self._con is None:
            return False
        with self._publication:
            return self._publier(force)

    def _publier(self, force: bool) -> bool:
        if not self._modifie and not force:
            return False

        # Remis a False avant l'instantane : une ecriture qui survient pendant la
        # publication remarque la base et sera reprise au tour suivant, plutot
        # que d'etre silencieusement consideree comme deja publiee.
        self._modifie = False
        try:
            instantane = self._instantane()
        except Exception as err:  # noqa: BLE001
            self._modifie = True
            self.derniere_erreur = f"instantane : {err}"
            self._journal(f"instantane impossible ({err})")
            return False

        try:
            self._televerser(instantane)
            self.derniere_sauvegarde = _horodatage()
            self.derniere_erreur = None
            self._journal(f"instantane publie ({instantane.stat().st_size} octets)")
            return True
        except Exception as err:  # noqa: BLE001
            self._modifie = True
            self.derniere_erreur = f"publication : {err}"
            self._journal(f"echec de publication, nouvel essai au prochain tour ({err})")
            return False
        finally:
            instantane.unlink(missing_ok=True)

    def _instantane(self) -> Path:
        """Copie coherente de la base, transactions du journal WAL comprises."""
        fd, brut = tempfile.mkstemp(prefix="scorex-instantane-", suffix=".sqlite")
        os.close(fd)
        chemin = Path(brut)
        chemin.unlink()  # sqlite3.connect recree le fichier proprement

        cible = sqlite3.connect(chemin)
        try:
            with self._verrou:
                self._con.backup(cible)
        finally:
            cible.close()
        return chemin

    def _televerser(self, chemin: Path) -> None:
        self._client().upload_file(
            path_or_fileobj=str(chemin),
            path_in_repo=FICHIER_DISTANT,
            repo_id=self.depot,
            repo_type="dataset",
            commit_message=f"Instantane {_horodatage()}",
        )

    # ------------------------------------------------------------- cycle de vie
    def demarrer(self, connexion: sqlite3.Connection, verrou: threading.RLock) -> None:
        self._con = connexion
        self._verrou = verrou
        atexit.register(self.arreter)

        if not self.active:
            self._journal(
                "inactive (SCOREX_HF_DATASET ou HF_TOKEN absent) : base locale uniquement"
            )
            return

        self._creer_depot()
        self._fil = threading.Thread(
            target=self._boucle, name="scorex-persistance", daemon=True
        )
        self._fil.start()
        self._journal(
            f"publication vers {self.depot} : {self.temporisation} s apres une "
            f"ecriture, verification toutes les {self.intervalle} s"
        )

    def _boucle(self) -> None:
        while not self._arret.is_set():
            # Reveil immediat sur ecriture ; l'intervalle sert de filet de securite
            # (une publication ratee est ainsi retentee sans nouvelle ecriture).
            self._reveil.wait(self.intervalle)
            self._reveil.clear()
            # Temporisation : une rafale de synchronisations tient en une publication.
            if self._arret.wait(self.temporisation):
                return
            self.sauvegarder()

    def arreter(self) -> None:
        """Derniere publication avant extinction (redemarrage ou mise en veille)."""
        if self._arret.is_set():
            return
        self._arret.set()
        self._reveil.set()  # debloque le fil, qui sort sans publier
        self.sauvegarder()

    # ---------------------------------------------------------------- diagnostic
    def etat(self) -> dict:
        """Resume expose par /health. Ne contient jamais le jeton."""
        return {
            "active": self.active,
            "depot": self.depot or None,
            "intervalle_s": self.intervalle,
            "temporisation_s": self.temporisation,
            "modifications_en_attente": self._modifie,
            "derniere_sauvegarde": self.derniere_sauvegarde,
            "derniere_erreur": self.derniere_erreur,
        }
