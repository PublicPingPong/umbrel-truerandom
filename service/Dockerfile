# ---------- Stufe 1: infnoise aus dem offiziellen Quellcode bauen ----------
FROM debian:trixie-slim AS build
ARG INFNOISE_COMMIT=95c5e54dd722d23b60ba0a2289f99b58c07e9df3
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git ca-certificates libftdi1-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/waywardgeek/infnoise.git /src \
 && cd /src && git checkout "$INFNOISE_COMMIT"
WORKDIR /src/software
RUN make -f Makefile.linux infnoise && ./infnoise --version

# ---------- Stufe 2: schlankes Laufzeit-Image ----------
FROM python:3.13-slim-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends libftdi1-2 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/software/infnoise /usr/local/bin/infnoise
COPY app /app
ENV DATA_DIR=/data PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
# Läuft als root im Container, weil libusb Schreibzugriff auf /dev/bus/usb braucht.
# Der Container ist NICHT privilegiert; Zugriff nur auf USB-Geräte (siehe docker-compose).
CMD ["python3", "-I", "/app/main.py"]
