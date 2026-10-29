FROM python:3.13-slim

# libsndfile is required by soundfile; espeak-ng is the phonemiser kokoro-onnx
# falls back to for non-English / unknown words.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the ONNX weights and voices bundle into the image so the container
# boots fully offline. ~200 MB total (fp16 model + voices).
COPY models/ /models/
# Download the fp16 ONNX model if it isn't included locally (e.g. when cloned from Git).
RUN test -f /models/kokoro-v1.0.fp16.onnx \
    || python -c "import urllib.request; print('Downloading kokoro-v1.0.fp16.onnx...'); urllib.request.urlretrieve('https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.fp16.onnx', '/models/kokoro-v1.0.fp16.onnx')"

COPY server.py .

ENV KOKORO_MODEL_DIR=/models \
    KOKORO_PORT=9100

EXPOSE 9100
CMD ["python", "-u", "server.py"]
