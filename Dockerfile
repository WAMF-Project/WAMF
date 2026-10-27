# Retain Debian 13 and CPython 3.11; refresh this reviewed digest for security updates.
FROM python:3.11.17-slim-trixie@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377

LABEL org.opencontainers.image.version="0.4.2"

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install --no-install-recommends -y libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/ ./requirements/
# Use patched build tools, then remove them and their bootstrap wheels.
RUN python -m pip install --no-cache-dir --upgrade \
    pip==26.2.2 setuptools==84.1.0 wheel==0.48.0 packaging==26.3 \
    && python -m pip install --no-cache-dir -c requirements/constraints.txt -r requirements/base.txt \
    && python -m pip check \
    && python -m pip uninstall --yes pip setuptools wheel packaging \
    && python -c "import pathlib, shutil, sysconfig; shutil.rmtree(pathlib.Path(sysconfig.get_path('stdlib')) / 'ensurepip')"

COPY model.tflite .
COPY birdnames.db .
COPY speciesid.py .
COPY webui.py .
COPY wamf_paths.py .
COPY version.py .
COPY retention.py .
COPY integrations/ ./integrations/
COPY app/ ./app/
COPY routes/ ./routes/
COPY templates/ ./templates/
COPY static/ ./static/

# Keep the bootstrap example outside the bind-mounted config directory.
COPY config/config.yml.example /usr/local/share/wamf/config.yml.example
COPY scripts/docker-entrypoint.sh /usr/local/bin/wamf-entrypoint

EXPOSE 7767

ENTRYPOINT ["sh", "/usr/local/bin/wamf-entrypoint"]

CMD ["python", "./speciesid.py"]
