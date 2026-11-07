# Run dolibar

In the *dolibarr* folder run

```bash
sudo docker compose up -d
```

Open http://127.0.0.1/ in a browser if on host.

# Run opensandbox

Run

```bash
uv run opensandbox-server
```

in a separate terminal.

Run

```bash
uv run python opensandbox/main.py
```

Open
http://127.0.0.1:46796/proxy/6080/vnc.html?host=127.0.0.1&port=46796&path=proxy/6080
in a browser.

# Open dolibarr in opensandobx

The code below lunches dolibarr in chrome without some irrelevant functionalites.

```bash
google-chrome \
  --app=http://host.docker.internal/ \
  --window-size=1280,800 \
  --no-first-run \
  --no-default-browser-check \
  --disable-notifications \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --no-sandbox \
  --disable-dev-shm-usage
```
