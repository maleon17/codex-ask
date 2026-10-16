FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCKER=true \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install --no-install-recommends -y \
    build-essential ca-certificates curl ffmpeg git libavcodec-dev \
    libavdevice-dev libavformat-dev libavutil-dev libcairo2 libmagic1 \
    libswscale-dev nodejs openssh-server python3 python3-pip python3-venv \
    xfonts-75dpi xfonts-base \
    && rm -rf /var/lib/apt/lists/*

ARG HEROKU_REF=master
RUN git clone --depth 1 --branch "$HEROKU_REF" \
    https://github.com/coddrago/Heroku.git /opt/Heroku \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir -r /opt/Heroku/requirements.txt

COPY entrypoint.sh /usr/local/bin/telegram-ai-userbot-entrypoint
RUN chmod 0755 /usr/local/bin/telegram-ai-userbot-entrypoint

WORKDIR /opt/Heroku
ENTRYPOINT ["/usr/local/bin/telegram-ai-userbot-entrypoint"]
CMD ["python", "-m", "heroku", "--root"]
