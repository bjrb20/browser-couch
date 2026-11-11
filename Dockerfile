# Sofa Link game host. The image has the program and the PS1 and PS2 cores,
# and no BIOS or game data: mount those. See "Run it as a container" in README.md.
FROM python:3.12-slim

# Play! (the PS2 core without a BIOS) draws with OpenGL: Mesa gives it a
# software context on a host with no GPU. The other cores need only libc.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libegl1 libgl1 libgl1-mesa-dri \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY fetch_core.py systems.py ./
# The official cores from the libretro buildbot. fetch_core.py checks each
# one's CPU type and records its SHA-256 in runtime/cores/download.json.
RUN python fetch_core.py --console ps1 \
 && python fetch_core.py --console ps2 \
 && python fetch_core.py --console ps2-play

COPY *.py ./
COPY web web
COPY docker/start.py docker/
RUN python -m compileall -q /app \
 # The mount points. The volumes start with these modes; start.py makes
 # each file it writes private to the container user.
 && mkdir -p /games /bios /data /turn \
 && chmod 1777 /data /turn

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/tmp/cache
# Any user can run it: the compose file picks the one that owns the game files.
USER 1000:1000
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/', timeout=4)"]
ENTRYPOINT ["python", "/app/docker/start.py"]
