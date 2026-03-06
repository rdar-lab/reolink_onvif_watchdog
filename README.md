# reolink_onvif_watchdog

A Python watchdog that monitors Reolink cameras via ONVIF and automatically
cycles the ONVIF and RTSP services when a camera becomes unresponsive.

## How it works

1. For each configured camera the watchdog first performs a quick HTTP/HTTPS
   reachability check.
   - If the camera is **completely unreachable** (power loss, network outage)
     the check is skipped — no action is taken until the camera comes back.
2. When the camera responds to HTTP but the **ONVIF snapshot pull fails**
   (after the configured number of retries), the watchdog:
   1. Disables both **ONVIF and RTSP** via the Reolink HTTP CGI API.
   2. Waits `cycle_wait` seconds (default 60 s).
   3. Re-enables both services.
3. The loop repeats every `check_interval` seconds for every camera.

## Requirements

- Python 3.9+
- pip packages: `requests`, `onvif-zeep`, `PyYAML`

## Installation

```bash
git clone https://github.com/rdar-lab/reolink_onvif_watchdog.git
cd reolink_onvif_watchdog
pip install -r requirements.txt
```

## Configuration

Copy and edit `config.yaml`:

```yaml
# Seconds between health-check rounds
check_interval: 30

# How many consecutive ONVIF failures trigger a service cycle
retry_count: 3

# Seconds to wait between retries
retry_delay: 5

# Seconds to keep ONVIF/RTSP disabled before re-enabling
cycle_wait: 60

cameras:
  - name: frontdoor
    ip: 192.168.1.100
    onvif_port: 8000   # ONVIF port (Reolink default: 8000)
    http_port: 80      # HTTP port for the Reolink CGI API (use 443 for HTTPS)
    username: admin

  - name: backyard
    ip: 192.168.1.101
    onvif_port: 8000
    http_port: 80
    username: admin
```

> **Tip:** Use `http_port: 443` to enable HTTPS for the CGI API calls,
> which encrypts the credentials on the wire.

## Passwords

Camera passwords are **never** stored in the configuration file.
They are injected via environment variables:

| Variable | Scope |
|---|---|
| `CAMERA_PASSWORD_<NAME_UPPERCASE>` | Per-camera (takes precedence) |
| `CAMERA_PASSWORD` | Global fallback for all cameras |

Examples:

```bash
# Per-camera (camera named "frontdoor")
export CAMERA_PASSWORD_FRONTDOOR=my_secret

# Global fallback (applies to all cameras that have no per-camera variable)
export CAMERA_PASSWORD=my_secret
```

## Running

```bash
# Use the default config.yaml in the current directory
python watchdog.py

# Use a custom config file
python watchdog.py /path/to/my_config.yaml
```

## Docker

### Build

```bash
docker build -t reolink-watchdog .
```

### Run

```bash
docker run -d \
  -e CAMERA_PASSWORD_FRONTDOOR=my_secret \
  -e CAMERA_PASSWORD_BACKYARD=other_secret \
  reolink-watchdog
```

Mount a custom configuration file:

```bash
docker run -d \
  -v /path/to/my_config.yaml:/app/config.yaml \
  -e CAMERA_PASSWORD=my_secret \
  reolink-watchdog
```

## Running tests

```bash
pip install pytest
python -m pytest tests/ -v
```
