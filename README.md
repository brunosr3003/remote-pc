# remote-pc

Control a Linux X11 desktop from any browser, and use your phone as a trackpad and keyboard for it.

- **`/`** gives you the full desktop in the browser (noVNC → x11vnc).
- **`/pad`** turns your phone into a trackpad and keyboard. Input is injected straight into X through XTEST, without VNC, so it stays responsive even on bad connections.

It's a single Python process that uses only the **standard library**: no pip, no root, no build step.

## Features

**Desktop in the browser**
- Serves noVNC and bridges its WebSocket to a local x11vnc.
- Opening the link connects automatically, with scaling and auto-reconnect already set.

**Phone trackpad (`/pad`)**
- Relative cursor movement with acceleration: slow for precision, fast to cross the screen.
- Tap = left click, double tap = right click, two-finger tap = middle click.
- Two-finger scroll.
- Drag lock: holds the left button so you can select long text or move windows.
- Physical-style Left/Middle/Right buttons with real press and release, so you can hold one and drag.
- Keyboard panel that uses the phone's native keyboard, including autocorrect and accented characters, plus sticky Ctrl/Alt/Shift/Cmd, Esc, Tab, arrows, Backspace and Enter.
- Characters that aren't on the X keyboard layout (emoji, symbols, accents) are typed by temporarily remapping a spare keycode.
- Buttons and modifiers still held down are released automatically if the tab closes or the screen turns off.
- Live latency display.

**Safety**
- Listens on `127.0.0.1` by default.
- Every route, including the WebSockets, requires a token.
- x11vnc runs with `-localhost` and a VNC password.
- Blocks `../` path traversal on static files.

## Requirements

- Linux with an X11 session (not Wayland)
- Python 3.8+
- `x11vnc`
- `libX11` and `libXtst`, which are present on basically every X11 desktop
- `git`, to fetch noVNC

## Setup

```sh
git clone https://github.com/brunosr3003/remote-pc.git ~/remote-pc
cd ~/remote-pc

# 1. Fetch noVNC (not vendored here)
./scripts/get-novnc.sh

# 2. Create the access token and the VNC password
python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > etc/token.txt
python3 -c 'import secrets; print(secrets.token_urlsafe(12))' > etc/vncpass.txt
x11vnc -storepasswd "$(cat etc/vncpass.txt)" etc/vncpasswd
chmod 600 etc/*

# 3. Install and start the systemd user services
mkdir -p ~/.config/systemd/user
cp systemd/remote-vnc.service systemd/remote-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now remote-vnc remote-web
```

Then open:

- Desktop: `http://127.0.0.1:8786/?t=YOUR_TOKEN`
- Trackpad: `http://127.0.0.1:8786/pad?t=YOUR_TOKEN`

After the first visit with `?t=`, a cookie remembers the token, but only over HTTPS.

The service files assume the repo lives at `~/remote-pc`. If it doesn't, edit the paths.

## Reaching it from your phone

The server only listens on localhost, so you choose how to expose it.

### Option A: Tailscale or LAN (lowest latency, best for the trackpad)

Add your Tailscale (or LAN) IP to `remote-web.service`:

```ini
Environment=REMOTE_PC_EXTRA_BINDS=100.x.y.z
```

Then open `http://100.x.y.z:8786/pad?t=YOUR_TOKEN` on the phone. If the interface comes up after the service at boot, the server keeps retrying that bind in the background.

### Option B: public HTTPS through a VPS

`systemd/remote-tunnel.service` keeps an SSH reverse tunnel open that publishes the server on the VPS at `127.0.0.1:8787`. Put a TLS reverse proxy in front of it there, and forward the WebSocket upgrade on `/ws`. Both the desktop and the trackpad share that one path.

nginx example:

```nginx
location / {
    proxy_pass http://127.0.0.1:8787;
}
location /ws {
    proxy_pass http://127.0.0.1:8787;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 1h;
}
```

Every touch makes a round trip through the VPS, so use Option A for the trackpad whenever you can.

## Configuration

These environment variables are all optional:

| Variable | Default | Meaning |
|---|---|---|
| `REMOTE_PC_BIND` | `127.0.0.1` | Main listen address |
| `REMOTE_PC_PORT` | `8786` | Listen port |
| `REMOTE_PC_EXTRA_BINDS` | *(empty)* | Extra addresses to also listen on, comma-separated |
| `REMOTE_PC_VNC_HOST` | `127.0.0.1` | x11vnc host |
| `REMOTE_PC_VNC_PORT` | `5900` | x11vnc port |
| `REMOTE_PC_DISPLAY` | `:0` | X display that the trackpad drives |
| `REMOTE_PC_TOKEN` | contents of `etc/token.txt` | Access token. If empty, **no auth**. |

## How it works

```
phone / browser ──HTTP/WS──▶ server.py ──┬──TCP──▶ x11vnc ──▶ X :0   (desktop view)
                                         └─XTEST─────────────▶ X :0   (trackpad input)
```

- **`server.py`** is a small HTTP server with a hand-written WebSocket layer. It serves noVNC's static files and relays `/ws` to x11vnc as raw TCP. `/ws?mode=pad` goes to the trackpad handler instead.
- **`xtest.py`** calls libX11 and libXtst through `ctypes`. It doesn't use xdotool, because forking a process for each of ~60 events per second would make the cursor stutter.
- **`pad/index.html`** is the trackpad UI: one self-contained file with no dependencies. It sends movement once per animation frame so a bad connection doesn't queue up stale events.

### Trackpad protocol

JSON text frames over `/ws?mode=pad`:

| Message | Action |
|---|---|
| `{"t":"m","x":dx,"y":dy}` | Relative move |
| `{"t":"abs","x":x,"y":y}` | Absolute move |
| `{"t":"c","b":1,"n":1}` | Click button `b`, `n` times |
| `{"t":"b","b":1,"d":1}` | Press (`d:1`) or release (`d:0`) button |
| `{"t":"s","d":"down","n":1}` | Scroll `up`/`down`/`left`/`right` |
| `{"t":"k","k":"enter","mods":["ctrl"]}` | Named key or single char, with modifiers |
| `{"t":"mod","m":"shift","d":1}` | Hold or release a sticky modifier |
| `{"t":"type","s":"text"}` | Type a string |
| `{"t":"ping"}` | Server answers `{"t":"pong"}` |

## Security notes

- **Anyone with the token controls your desktop.** Treat the `/pad?t=...` link like a password.
- Plain HTTP over Tailscale is encrypted by WireGuard. Over the open internet, always put TLS in front.
- `etc/` holds your secrets and is git-ignored. Never commit it.

## Credits

[noVNC](https://github.com/novnc/noVNC), which has its own license. It is downloaded at setup time and not included in this repository.
