# syntax=docker/dockerfile:1

# Calibre-Web fork image.
# Intentionally runs as root (no USER directive): the container must be able to
# read/write the whole library volume (scan import keeps books in place,
# covers are stored in <library>/.covers) and update metadata.db inside it.

# ---- builder: compile any dependency that has no wheel for the target arch ----
FROM python:3.12-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- runtime ----
FROM python:3.12-slim

# libmagic: python-magic; ImageMagick + ghostscript: cover extraction (pdf)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 imagemagick ghostscript curl \
    && rm -rf /var/lib/apt/lists/* \
    && sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/g' /etc/ImageMagick-6/policy.xml || true

COPY --from=builder /install /usr/local

WORKDIR /app
COPY cps.py .
COPY cps ./cps

RUN mkdir -p /config /books /app/cps/cache
VOLUME ["/config", "/books"]

ENV PORT=8083
EXPOSE 8083

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s \
    CMD curl -fsL http://localhost:8083/ || exit 1

CMD ["python", "-u", "cps.py", "-p", "/config/app.db"]
