# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN groupadd --system polyglot && useradd --system --gid polyglot --create-home polyglot \
    && mkdir -p /data && chown polyglot:polyglot /data
# /data is where named volumes keep state: broker job journals and election state, the dashboard's history
# and speech clips. Created here, owned by polyglot, so an empty volume mounted on it is writable.

# Fast image for architecture testing. The socket-based core uses only stdlib.
FROM base AS mock
COPY --chown=polyglot:polyglot . /app
USER polyglot
CMD ["python", "broker.py", "--id", "1"]

# Translator workers only ever call the Gemini API - no audio tooling needed.
# Kept separate from `full` so a 15-language worker fleet builds/starts fast.
# Deps install before the app COPY so editing app code (or unrelated files
# like docs/.env) can't invalidate this layer and force a redownload.
FROM base AS translate
USER root
RUN python -m pip install "google-genai>=2.17,<3"
COPY --chown=polyglot:polyglot . /app
USER polyglot
CMD ["python", "broker.py", "--id", "1"]

# Full image for the STT producer: Whisper transcription plus ffmpeg/portaudio
# for file and microphone input. Also works for translator workers if you
# want the (optional, opt-in) Gemini Live speech-to-speech path.
# Deps install before the app COPY (see `translate` above) - this is the
# expensive layer (ffmpeg + Whisper/PyTorch), worth protecting most.
FROM base AS full
USER root
# Use a Debian mirror that remains reachable when the default Fastly-backed
# endpoint is blocked by an ISP, VPN, or corporate network. Keep both the
# main and security repositories on the same regional mirror.
RUN sed -i \
        -e 's|http://deb.debian.org/debian-security|https://mirror.sg.gs/debian-security|g' \
        -e 's|http://deb.debian.org/debian|https://mirror.sg.gs/debian|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o Acquire::Retries=5 \
    && apt-get install -y --no-install-recommends -o Acquire::Retries=5 ffmpeg libportaudio2 \
    && rm -rf /var/lib/apt/lists/*
# Only requirements.txt is copied here (not the full app) so this layer's
# cache survives app-code/doc edits - it only reinstalls when dependencies
# actually change.
COPY requirements.txt /app/requirements.txt
# openai-whisper pulls in plain "torch", and PyPI's default Linux torch
# wheel is the CUDA build - it drags in several GB of nvidia-* packages
# (cublas, cudnn, cusolver, nccl, ...) that a container with no GPU access
# will never use. Installing the CPU-only wheel first (~200MB total, no
# nvidia deps) satisfies whisper's requirement so the later install below
# doesn't pull the CUDA variant instead.
# --retries/--timeout give pip's own resumable-download logic more room
# against a slow/unstable link; the outer shell retry re-runs the whole
# install (pip resumes already-cached wheels) if one attempt still dies on
# a corrupted download.
RUN for i in 1 2 3; do \
        python -m pip install --retries 10 --timeout 120 \
            --index-url https://download.pytorch.org/whl/cpu torch && exit 0; \
        echo "torch install attempt $i failed, retrying..."; \
        sleep 5; \
    done; \
    exit 1
RUN for i in 1 2 3; do \
        python -m pip install --retries 10 --timeout 120 -r requirements.txt && exit 0; \
        echo "pip install attempt $i failed, retrying..."; \
        sleep 5; \
    done; \
    exit 1
COPY --chown=polyglot:polyglot . /app
# Pre-create the Whisper model cache dir with polyglot ownership: it's
# where compose.yaml mounts a named volume for it, and an empty named
# volume takes its initial ownership from whatever the image has at that
# path (root, if nothing's there yet) - without this, the polyglot user
# (which the container actually runs as) can't write the downloaded model.
RUN mkdir -p /home/polyglot/.cache/whisper /data && chown -R polyglot:polyglot /home/polyglot/.cache /data
USER polyglot
CMD ["python", "broker.py", "--id", "1"]

# Runs the unittest suite. Built on `mock` since the tests exercise the
# stdlib-only paths - stt_service/translator_worker import whisper/genai
# lazily, so the full suite (minus the full-image-only SDK contract check)
# passes without those heavy deps.
FROM mock AS test
USER polyglot
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]

# A plain `docker build .` intentionally produces the quick mock image.
FROM mock AS final
