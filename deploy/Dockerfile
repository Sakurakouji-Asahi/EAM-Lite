FROM python:3.14.7-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18=18.6-1.pgdg13+2 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/eam-lite/app
COPY requirements/production.lock /tmp/production.lock
RUN python -m pip install --upgrade pip==26.2.1 \
    && python -m pip install -r /tmp/production.lock

ARG APP_VERSION=unknown
ARG BUILD_COMMIT=unknown
ARG BUILD_TIME=unknown

ENV APP_VERSION=${APP_VERSION} \
    BUILD_COMMIT=${BUILD_COMMIT} \
    BUILD_TIME=${BUILD_TIME}

LABEL org.opencontainers.image.title="EAM-Lite" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}"

COPY . /opt/eam-lite/app
RUN useradd --system --uid 10001 --home /nonexistent --shell /usr/sbin/nologin eam-lite \
    && mkdir -p /srv/static /var/lib/eam-lite/media /var/lib/eam-lite/tmp \
       /var/backups/eam-lite/stage /var/backups/eam-lite/tmp /var/backups/eam-lite/mirror \
    && chown -R 10001:10001 /srv/static /var/lib/eam-lite /var/backups/eam-lite

USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "2", "--timeout", "120", "--error-logfile", "-"]
