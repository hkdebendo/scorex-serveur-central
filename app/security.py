# -*- coding: utf-8 -*-
"""Verification de mot de passe (pbkdf2_sha256) et jetons de session signes.

Les jetons sont sans etat : ils portent l'identifiant et l'echeance, scelles par
une signature HMAC. Rien n'est conserve en memoire.

C'est indispensable des lors que l'hebergeur recycle le processus, ce que fait
toute offre gratuite a chaque mise en veille et a chaque deploiement. Avec le
dictionnaire en memoire d'origine, toutes les sessions mouraient au premier
redemarrage et l'application affichait « Session expiree » en permanence.

Le secret de signature est range dans la table `meta` de la base, donc publie
avec elle dans le depot Dataset : il survit aux redemarrages, et les jetons
restent valides d'un deploiement au suivant. SCOREX_SECRET le remplace si on
prefere le fournir par l'environnement.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

CLE_META = "jeton_secret"
DUREE_JETON_S = 30 * 24 * 3600

_secret_cache: bytes | None = None


def verifier_mdp(mdp: str, encode: str) -> bool:
    try:
        algo, iters, sel_b64, dk_b64 = encode.split("$")
        if algo != "pbkdf2_sha256":
            return False
        sel = base64.b64decode(sel_b64)
        attendu = base64.b64decode(dk_b64)
        calcule = hashlib.pbkdf2_hmac("sha256", mdp.encode(), sel, int(iters))
        return hmac.compare_digest(calcule, attendu)
    except Exception:  # noqa: BLE001
        return False


def _secret() -> bytes:
    """Secret de signature, stable d'un redemarrage a l'autre."""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    fourni = (os.environ.get("SCOREX_SECRET") or "").strip()
    if fourni:
        _secret_cache = fourni.encode()
        return _secret_cache

    # Import differe : db importe persistance, et security est importe par main
    # apres db. L'import au niveau module creerait une dependance circulaire.
    from .db import DB, VERROU, commit

    with VERROU:
        ligne = DB.execute("SELECT valeur FROM meta WHERE cle = ?", (CLE_META,)).fetchone()
        if ligne is None:
            valeur = secrets.token_urlsafe(32)
            DB.execute("INSERT INTO meta (cle, valeur) VALUES (?, ?)", (CLE_META, valeur))
            # Marque la base a republier : sans cela le secret serait regenere au
            # prochain demarrage et tous les jetons deviendraient invalides.
            commit()
        else:
            valeur = ligne["valeur"]
    _secret_cache = valeur.encode()
    return _secret_cache


def _signature(charge: str) -> str:
    return hmac.new(_secret(), charge.encode(), hashlib.sha256).hexdigest()


def creer_session(user_id: int) -> str:
    charge = f"{user_id}.{int(time.time()) + DUREE_JETON_S}"
    brut = f"{charge}.{_signature(charge)}".encode()
    return base64.urlsafe_b64encode(brut).decode().rstrip("=")


def user_id_pour(token: str) -> int | None:
    try:
        brut = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        uid, echeance, sig = brut.split(".")
        if not hmac.compare_digest(sig, _signature(f"{uid}.{echeance}")):
            return None
        if time.time() > int(echeance):
            return None
        return int(uid)
    except Exception:  # noqa: BLE001
        return None
