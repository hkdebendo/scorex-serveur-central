# -*- coding: utf-8 -*-
"""Verification de mot de passe (pbkdf2_sha256) et jetons de session en memoire."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_SESSIONS: dict[str, int] = {}   # token -> user_id


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


def creer_session(user_id: int) -> str:
    token = secrets.token_urlsafe(24)
    _SESSIONS[token] = user_id
    return token


def user_id_pour(token: str) -> int | None:
    return _SESSIONS.get(token)
