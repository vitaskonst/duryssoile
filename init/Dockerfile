FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY init.py ./

# db/, data/ and audio/ are bind-mounted by compose, so the schema, the
# source JSON and the clips can all change without rebuilding the image.
ENV SCHEMA_PATH=/srv/db/schema.sql \
    DATA_DIR=/srv/data \
    AUDIO_DIR=/srv/audio

ENTRYPOINT ["python", "init.py"]
