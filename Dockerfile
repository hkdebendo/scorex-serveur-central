# Image du serveur central Score X, pour tout hebergeur qui construit un Dockerfile
# (Render, Koyeb, Cloud Run, Space Hugging Face...).
#
# Le disque de ce conteneur est ephemere : la durabilite de central.sqlite est
# assuree par app/persistance.py, qui republie la base dans un depot Dataset prive.
FROM python:3.11-slim

# Utilisateur non root (uid 1000), comme l'exige un Space Hugging Face.
RUN useradd -m -u 1000 user
USER user

ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/home/user/.cache/huggingface

WORKDIR $HOME/app

COPY --chown=user requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY --chown=user app ./app
COPY --chown=user scripts ./scripts

# Dossier de travail de la base, cree explicitement pour qu'il appartienne a
# l'utilisateur : c'est la que central.sqlite est restaure puis ecrit.
RUN mkdir -p $HOME/app/data
# Base de depart si le depot Dataset est encore vide (premier demarrage).
COPY --chown=user data/referentiel.sqlite ./data/referentiel.sqlite

EXPOSE 7860

# Le port est impose par l'hebergeur via PORT (Render, Koyeb, Cloud Run) ; 7860
# est la valeur attendue par un Space Hugging Face. Forme shell pour que la
# variable soit bien substituee.
#
# Un seul worker, volontairement : la connexion SQLite, les sessions en memoire
# et le fil de publication sont propres au processus.
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1"]
