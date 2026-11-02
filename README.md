# True Random: Umbrel-App für den Infinite Noise TRNG

Diese App liest den per USB angeschlossenen Infinite Noise TRNG aus und stellt echten Zufall als HTTP-API bereit. Das Repo ist gleichzeitig ein **Umbrel Community App Store**.

```
umbrel-app-store.yml          Store-Kennung „ppp“ (Public Ping Pong)
ppp-truerandom/               Umbrel-App (Manifest, docker-compose, Icon)
service/                      Quellcode des Docker-Images
  Dockerfile                  baut infnoise aus dem offiziellen Quellcode (fester Commit)
  app/main.py                 HTTP-Dienst (nur Python-Standardbibliothek)
  app/source.py               liest infnoise, Puffer, Health-Auswertung, Rohdaten-Test
  app/rng.py                  unverzerrte Zahlen, Fisher-Yates, Ziehen ohne Zurücklegen
  app/static/index.html       Verwaltungsseite
  tests/                      Tests der Zufallslogik + Test-Attrappe der Quelle
.github/workflows/build.yml   baut das Image und legt es bei GitHub (ghcr.io) ab
```

## Einrichtung (einmalig)

1. **GitHub-Repo anlegen**, z. B. `umbrel-truerandom`, und den Inhalt dieses Ordners hochladen.
2. Überall **`GITHUB_USER`** durch deinen GitHub-Namen in Kleinbuchstaben ersetzen. Das betrifft `ppp-truerandom/umbrel-app.yml` und `ppp-truerandom/docker-compose.yml`.
3. **Image bauen lassen:** einen Tag `v0.1.0` erstellen und pushen. Die GitHub Action testet die Zufallslogik, baut das Image und legt es unter `ghcr.io/<name>/truerandom:0.1.0` ab.
4. Auf GitHub unter *Packages → truerandom → Package settings* die Sichtbarkeit auf **Public** stellen. Sonst kann der Umbrel das Image nicht laden.
5. **Im Umbrel:** App Store → „…“ (oben rechts) → *Community App Stores* → die Repo-URL eintragen. Danach „True Random“ installieren.
6. Den TRNG per USB an den Umbrel Home anschließen. Auf der App-Seite sollte nach wenigen Sekunden **„Läuft – echter Zufall“** stehen.

**Updates:** In `ppp-truerandom/umbrel-app.yml` und im `image:`-Tag die Version hochzählen, dann den passenden Git-Tag pushen.

## API

Basis-URL: `http://umbrel.local:8517`, über Tailscale die Tailnet-Adresse des Umbrel.
Den API-Key findest du auf der Verwaltungsseite. Mitgeschickt wird er per Header `X-API-Key: …` oder `Authorization: Bearer …`.

| Endpunkt | Parameter | Ergebnis |
|---|---|---|
| `/v1/bytes` | `n` (1–65536), `format` = `hex` \| `base64` | Rohe Zufallsbytes |
| `/v1/int` | `min` (Std. 0), `max`, `count` (≤ 10000) | Ganze Zahlen in [min, max], Bereich bis 2⁶⁴ |
| `/v1/float` | `count` | Zahlen in [0, 1), 53 Bit Auflösung |
| `/v1/shuffle` | `n` (≤ 10000), `start` (Std. 0) | Permutation von start … start+n−1 |
| `/v1/choice` | `n`, `k`, `start` | k verschiedene Werte ohne Zurücklegen |
| `/v1/health` | – | Zustand der Quelle |

Jede Antwort enthält neben `data` auch `source`, `id`, `timestamp` und `bytes_used`.
Ist kein echter Zufall verfügbar, kommt **HTTP 503**. Es gibt nie einen Ersatz aus Pseudozufall.

```bash
curl -H "X-API-Key: $KEY" "http://umbrel.local:8517/v1/choice?n=40&k=6&start=1"
# {"data": [12, 14, 3, 19, 1, 8], "source": "infnoise", "id": "…", "timestamp": "…", "bytes_used": 64}
```

## Was der Dienst garantiert

- `infnoise` läuft **ohne `--multiplier`**. Es gibt also nur so viele Bits aus, wie es an Entropie gemessen hat, und nur, solange sein Health-Check besteht. Die Umgebungsvariable `INFNOISE_MULTIPLIER` wird zusätzlich entfernt.
- **Kein PRNG.** Jede Zahl entsteht aus frischen Bytes.
- **Jedes Byte wird genau einmal verwendet.** Reste eines Requests (höchstens 63 Bytes) werden verworfen und nie weitergegeben.
- **Kein Modulo-Bias:** Zahlenbereiche entstehen per Rejection Sampling, Mischen per Fisher-Yates.
- Rohdaten aus dem Rohdaten-Test gehen **nie** in die API.
- Die Verwaltungsseite (`/`, `/api/*`) liegt hinter dem Umbrel-Login. Nur `/v1/*` ist ohne Login erreichbar und wird über den API-Key geschützt.

## Prüfen

- **Verwaltungsseite:** Sie zeigt die laufende Entropie-Schätzung des Health-Checks. Der Designwert liegt bei ca. 0,864 Bit pro Rohbit.
- **Rohdaten-Test:** Der Test nimmt 1 MB ungewhitete Rohbits auf und lässt sie herunterladen. Für die belastbare Prüfung wertest du die Datei mit dem NIST-Werkzeug aus:
  ```bash
  git clone https://github.com/usnistgov/SP800-90B_EntropyAssessment
  # bauen laut README, dann:
  ./ea_non_iid -v raw-….bin 1      # 1 = jedes Byte als einzelne Bits auswerten
  ```
  Erwartung: deutlich über 0,5 Bit pro Bit, typischerweise in der Nähe des Designwerts.

## Lokal testen ohne TRNG

```bash
cd service
python3 -I tests/test_rng.py
DATA_DIR=/tmp/tr SOURCE_LABEL=TEST-urandom \
SOURCE_CMD="python3 tests/fake_infnoise.py" RAW_CMD="python3 tests/fake_infnoise.py --raw" \
python3 -I app/main.py      # → http://localhost:8080
```
Die Attrappe nutzt `os.urandom` und dient **nur** zum Testen. Die Antworten tragen dann `source: TEST-urandom`.

## Falls der TRNG nicht erkannt wird

- `lsusb` auf dem Umbrel (per SSH) sollte ein Gerät von *FTDI* zeigen.
- Falls Docker `device_cgroup_rules` nicht übernimmt: in `docker-compose.yml` diese Regel durch `privileged: true` ersetzen. Das ist weniger streng, funktioniert aber immer.
