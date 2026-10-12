FROM debian:bookworm-slim
ARG DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="M50 Label Bench" \
      org.opencontainers.image.description="Label designer and print server for the Kodak Label Era M50" \
      org.opencontainers.image.licenses="MIT"

# Standard library only for Bluetooth: by default the host's bluetoothd opens the connection over D-Bus.
# Debian's Python also has Bluetooth sockets, for BT_MODE=socket on the host network.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pil python3-fonttools fonts-dejavu-core ca-certificates curl tzdata \
 && (apt-get install -y --no-install-recommends fonts-symbola || true) \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Archivo for text and Noto Emoji for line-drawn emoji, used when plain text is printed through the API.
# If GitHub can't be reached at build time, the bridge falls back to DejaVu and Symbola.
RUN mkdir -p fonts \
 && (curl -fsSL -o fonts/Archivo.ttf "https://github.com/google/fonts/raw/main/ofl/archivo/Archivo%5Bwdth,wght%5D.ttf" || rm -f fonts/Archivo.ttf) \
 && (curl -fsSL -o fonts/NotoEmoji.ttf "https://github.com/google/fonts/raw/main/ofl/notoemoji/NotoEmoji%5Bwght%5D.ttf" || rm -f fonts/NotoEmoji.ttf)

COPY bridge.py bluez.py render.py ./
COPY web ./web

ENV PYTHONUNBUFFERED=1 DATA_DIR=/data FONT_DIR=/app/fonts PORT=8091
EXPOSE 8091
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8091'), timeout=4)"
CMD ["python3", "bridge.py"]
