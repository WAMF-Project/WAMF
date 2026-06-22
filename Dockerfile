FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/ ./requirements/
RUN pip install --no-cache-dir -r requirements/base.txt

COPY model.tflite .
COPY birdnames.db .
COPY speciesid.py .
COPY webui.py .
COPY wamf_paths.py .
COPY version.py .
COPY app/ ./app/
COPY routes/ ./routes/
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 7766

CMD ["python", "./speciesid.py"]
