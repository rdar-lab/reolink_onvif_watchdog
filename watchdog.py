"""
Reolink ONVIF Watchdog
======================
Periodically checks each configured camera via ONVIF (snapshot pull).
When a camera's ONVIF check fails repeatedly, the script:
  1. Disables ONVIF *and* RTSP via the Reolink HTTP API.
  2. Waits a configurable number of seconds (default 60 s).
  3. Re-enables both services.

Configuration is read from a YAML file (default: config.yaml).
Passwords are supplied through environment variables so that secrets are
never stored in the configuration file:
  - Per-camera:  CAMERA_<N>   where N is the 1-based position of the camera
                 in the configuration file (e.g. CAMERA_1 for the first camera)
  - Fallback:    CAMERA_PASSWORD
"""

import logging
import os
import sys
import time
import urllib.parse
from typing import Optional

import requests
import urllib3
import yaml
from onvif import ONVIFCamera

# Reolink cameras use self-signed TLS certificates. Suppress the
# InsecureRequestWarning that urllib3 emits for every verify=False request.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("onvif_watchdog")


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "check_interval": 30,
    "retry_count": 3,
    "retry_delay": 5,
    "cycle_wait": 60,
    "cameras": [],
}


def load_config(path: str) -> dict:
    """Load and validate configuration from a YAML file."""
    with open(path, "r") as fh:
        data = yaml.safe_load(fh)

    config = {**DEFAULT_CONFIG, **(data or {})}

    # Validate cameras list structure and required per-camera fields.
    cameras = config.get("cameras")
    if cameras is None:
        cameras = []
        config["cameras"] = cameras

    if not isinstance(cameras, list):
        raise ValueError(
            f"Configuration error: 'cameras' must be a list, got {type(cameras).__name__}."
        )

    for idx, cam in enumerate(cameras):
        if not isinstance(cam, dict):
            raise ValueError(
                f"Configuration error: camera entry at index {idx} must be a mapping, "
                f"got {type(cam).__name__}."
            )

        cam_name = cam.get("name") or f"index {idx}"

        ip = cam.get("ip")
        if ip is None or not str(ip).strip():
            raise ValueError(
                f"Configuration error for camera '{cam_name}': missing or empty 'ip' field."
            )

        for key, value in cam.items():
            if key == "port" or key.endswith("_port"):
                if not isinstance(value, int):
                    raise ValueError(
                        f"Configuration error for camera '{cam_name}': "
                        f"field '{key}' must be an integer, got {type(value).__name__}."
                    )

    if not cameras:
        logger.warning("No cameras defined in configuration file.")

    return config


def get_password(camera_index: int, camera_name: str) -> str:
    """
    Resolve the camera password from environment variables.

    Lookup order:
    1. CAMERA_<N>       (per-camera, where N is the 1-based position in config)
    2. CAMERA_PASSWORD  (global fallback)
    """
    per_camera_var = f"CAMERA_{camera_index}"
    password = os.environ.get(per_camera_var)
    if password is None:
        password = os.environ.get("CAMERA_PASSWORD", "")
    if not password:
        logger.warning(
            "No password found for camera '%s' (index %d). "
            "Set %s or CAMERA_PASSWORD environment variable.",
            camera_name,
            camera_index,
            per_camera_var,
        )
    return password


# ---------------------------------------------------------------------------
# HTTP reachability check
# ---------------------------------------------------------------------------

def is_http_reachable(ip: str, http_port: int, timeout: int = 5) -> bool:
    """
    Return True if the camera responds to a plain HTTP/HTTPS request.

    This is used as a pre-flight check before attempting ONVIF.  If the
    camera is entirely offline (power loss, network outage) there is no
    point cycling ONVIF/RTSP — the API call would fail anyway.
    """
    scheme = "https" if http_port == 443 else "http"
    port_suffix = f":{http_port}" if http_port not in (80, 443) else ""
    url = f"{scheme}://{ip}{port_suffix}/"
    try:
        requests.get(url, timeout=timeout, verify=False)  # noqa: S501 — self-signed certs are common on cameras
        return True
    except requests.exceptions.ConnectionError:
        return False
    except requests.exceptions.Timeout:
        return False
    except Exception as exc:
        logger.debug("Unexpected error during HTTP reachability check for %s: %s", ip, exc)
        return False


# ---------------------------------------------------------------------------
# ONVIF health-check
# ---------------------------------------------------------------------------

def check_onvif(ip: str, onvif_port: int, username: str, password: str) -> bool:
    """
    Connect to the camera via ONVIF and attempt to download a snapshot.

    Returns True on success, False on any failure.
    """
    try:
        cam = ONVIFCamera(ip, onvif_port, username, password)
        media = cam.create_media_service()
        profiles = media.GetProfiles()

        token = profiles[0].token
        req = media.create_type("GetSnapshotUri")
        req.ProfileToken = token
        result = media.GetSnapshotUri(req)

        response = requests.get(
            result.Uri,
            auth=requests.auth.HTTPDigestAuth(username, password),
            timeout=10,
            verify=False,  # noqa: S501 — self-signed certs are common on cameras
        )
        if response.status_code == 200:
            logger.info("ONVIF check passed for %s.", ip)
            return True

        logger.warning(
            "Snapshot download failed for %s: HTTP %s.", ip, response.status_code
        )
        return False

    except Exception as exc:
        logger.warning("ONVIF check failed for %s: %s", ip, exc)
        return False


# ---------------------------------------------------------------------------
# Reolink API — service cycling
# ---------------------------------------------------------------------------

def _reolink_post(base_url: str, payload: list, timeout: int = 10) -> Optional[requests.Response]:
    """Send a JSON command to the Reolink HTTP API."""
    try:
        resp = requests.post(base_url, json=payload, timeout=timeout, verify=False)  # noqa: S501
        resp.raise_for_status()
        return resp
    except Exception as exc:
        logger.error("Reolink API call failed: %s", exc)
        return None


def cycle_services(
    ip: str,
    http_port: int,
    username: str,
    password: str,
    cycle_wait: int,
) -> None:
    """
    Disable ONVIF and RTSP, wait, then re-enable both.

    The Reolink SetNetPort command accepts a single NetPort object so both
    flags are toggled together in one request.
    """
    logger.warning("ONVIF failure detected for %s. Cycling ONVIF and RTSP…", ip)

    port_suffix = f":{http_port}" if http_port not in (80, 443) else ""
    scheme = "https" if http_port == 443 else "http"
    # Reolink's CGI API requires credentials in the query string.
    # Use urllib.parse.urlencode to safely handle special characters in credentials.
    # Use http_port 443 (HTTPS) to encrypt the query string in transit.
    qs = urllib.parse.urlencode({"user": username, "password": password})
    base_url = f"{scheme}://{ip}{port_suffix}/cgi-bin/api.cgi?{qs}"

    off_payload = [
        {
            "cmd": "SetNetPort",
            "param": {"NetPort": {"onvifEnable": 0, "rtspEnable": 0}},
        }
    ]
    on_payload = [
        {
            "cmd": "SetNetPort",
            "param": {"NetPort": {"onvifEnable": 1, "rtspEnable": 1}},
        }
    ]

    if _reolink_post(base_url, off_payload) is not None:
        logger.info("ONVIF and RTSP disabled for %s. Waiting %d s…", ip, cycle_wait)
        time.sleep(cycle_wait)
        if _reolink_post(base_url, on_payload) is not None:
            logger.info("ONVIF and RTSP re-enabled for %s.", ip)
        else:
            logger.error("Failed to re-enable services for %s!", ip)
    else:
        logger.error(
            "Could not reach Reolink API for %s. Services may still be running.", ip
        )


# ---------------------------------------------------------------------------
# Per-camera watchdog loop
# ---------------------------------------------------------------------------

def watch_camera(camera_cfg: dict, global_cfg: dict, camera_index: int) -> None:
    """Run one full check cycle for a single camera."""
    name = camera_cfg.get("name", camera_cfg.get("ip", "unknown"))
    ip = camera_cfg["ip"]
    onvif_port = camera_cfg.get("onvif_port", 8000)
    http_port = camera_cfg.get("http_port", 80)
    username = camera_cfg.get("username", "admin")
    password = get_password(camera_index, name)

    retry_count = global_cfg.get("retry_count", DEFAULT_CONFIG["retry_count"])
    retry_delay = global_cfg.get("retry_delay", DEFAULT_CONFIG["retry_delay"])
    cycle_wait = global_cfg.get("cycle_wait", DEFAULT_CONFIG["cycle_wait"])

    log = logging.getLogger(f"onvif_watchdog.{name}")

    # Pre-flight: skip cameras that are completely unreachable via HTTP/HTTPS.
    # If the camera is offline there is nothing we can do via the API.
    if not is_http_reachable(ip, http_port):
        log.warning(
            "Camera '%s' (%s) is not reachable via HTTP/HTTPS. "
            "Skipping ONVIF check (camera may be offline).",
            name,
            ip,
        )
        return

    for attempt in range(1, retry_count + 1):
        log.info("Attempt %d/%d for camera '%s' (%s)…", attempt, retry_count, name, ip)
        if check_onvif(ip, onvif_port, username, password):
            return  # healthy — nothing to do

        if attempt < retry_count:
            log.info("Retrying in %d s…", retry_delay)
            time.sleep(retry_delay)

    # All retries exhausted — cycle the services
    cycle_services(ip, http_port, username, password, cycle_wait)


# ---------------------------------------------------------------------------
# Main entry-point
# ---------------------------------------------------------------------------

def main(config_path: str = "config.yaml") -> None:
    logger.info("Starting Reolink ONVIF Watchdog (config: %s).", config_path)

    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logger.error("Configuration file not found: %s", config_path)
        sys.exit(1)
    except yaml.YAMLError as exc:
        logger.error("Invalid YAML configuration: %s", exc)
        sys.exit(1)
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    cameras = config.get("cameras", [])
    check_interval = config.get("check_interval", DEFAULT_CONFIG["check_interval"])

    if not cameras:
        logger.error("No cameras configured. Exiting.")
        sys.exit(1)

    logger.info(
        "Monitoring %d camera(s) every %d s.", len(cameras), check_interval
    )

    while True:
        for idx, camera in enumerate(cameras, start=1):
            try:
                watch_camera(camera, config, idx)
            except Exception as exc:
                logger.error(
                    "Unexpected error while checking camera '%s': %s",
                    camera.get("name", camera.get("ip", "?")),
                    exc,
                )
        time.sleep(check_interval)


if __name__ == "__main__":
    config_file = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    main(config_file)
