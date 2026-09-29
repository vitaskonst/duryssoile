FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

# The seed data is bind-mounted here by compose rather than baked in, so the
# ~600 MB of clips never end up in the image. Only app.bootstrap reads it.
ENV SEED_DIR=/srv/seed

RUN useradd --create-home --uid 10001 duryssoile
USER duryssoile

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
