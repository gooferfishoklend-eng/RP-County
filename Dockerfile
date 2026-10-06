FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    DB_PATH=/data/geopolitics.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
# Build the ~4000 map provinces once at image build time so the bot starts instantly.
RUN python -c "from bot.geo import load_world; load_world()"

RUN useradd --uid 10001 --create-home app && mkdir -p /data && chown app:app /data
USER app
VOLUME ["/data"]

CMD ["python", "-m", "bot"]
