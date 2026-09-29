# Pinned by tag and digest like the images in docker-compose.yml, so every
# rebuild starts from the same Python and Debian patch level.
FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install the duryssoile package itself; its dependencies came from
# requirements.txt above. Built outside /srv so no build leftovers remain.
COPY pyproject.toml /tmp/pkg/
COPY src /tmp/pkg/src
RUN pip install --no-cache-dir --no-deps /tmp/pkg && rm -rf /tmp/pkg

# The migrations are run from /srv (alembic.ini's script_location).
COPY alembic.ini ./
COPY migrations ./migrations

# The seed data is bind-mounted here by compose rather than baked in, so the
# ~600 MB of clips never end up in the image. Only duryssoile.bootstrap reads it.
ENV SEED_DIR=/srv/seed

RUN useradd --create-home --uid 10001 duryssoile
USER duryssoile

EXPOSE 8000

# --forwarded-allow-ips: take the client address (and scheme) from the
# proxy's X-Forwarded-* headers. Only the proxy can reach this port, and it
# overwrites any X-Forwarded-For the client sent, so the headers are trusted;
# without this every visitor shares the proxy's address and the admin login
# throttle locks everyone out together.
CMD ["uvicorn", "duryssoile.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers", "--forwarded-allow-ips", "*"]
