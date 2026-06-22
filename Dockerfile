FROM python:3.11-slim
RUN apt-get update && apt-get install ffmpeg libsm6 libxext6  -y
COPY requirements/ ./requirements/
RUN pip install -r requirements/base.txt
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

CMD python ./speciesid.py
