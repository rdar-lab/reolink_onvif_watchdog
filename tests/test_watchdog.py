"""
Unit tests for watchdog.py
"""

import os
import time
import types
import unittest
from unittest.mock import MagicMock, patch, call

import yaml

# ---------------------------------------------------------------------------
# Helpers — build minimal stubs so we can import watchdog without a real
# ONVIF library installed during CI (we mock it anyway).
# ---------------------------------------------------------------------------

import sys

# Stub out onvif module so watchdog.py can be imported in any environment
if "onvif" not in sys.modules:
    onvif_stub = types.ModuleType("onvif")
    onvif_stub.ONVIFCamera = MagicMock()
    sys.modules["onvif"] = onvif_stub

import watchdog  # noqa: E402 — must come after stub setup


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig(unittest.TestCase):
    def test_valid_config(self):
        import tempfile

        cfg_data = {
            "check_interval": 15,
            "retry_count": 2,
            "retry_delay": 3,
            "cycle_wait": 30,
            "cameras": [
                {"name": "cam1", "ip": "10.0.0.1", "onvif_port": 8000, "http_port": 80, "username": "admin"}
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
            yaml.dump(cfg_data, fh)
            path = fh.name

        try:
            config = watchdog.load_config(path)
            self.assertEqual(config["check_interval"], 15)
            self.assertEqual(len(config["cameras"]), 1)
            self.assertEqual(config["cameras"][0]["ip"], "10.0.0.1")
        finally:
            os.unlink(path)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            watchdog.load_config("/nonexistent/path/config.yaml")

    def test_defaults_applied(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
            yaml.dump({"cameras": []}, fh)
            path = fh.name

        try:
            config = watchdog.load_config(path)
            self.assertEqual(config["check_interval"], watchdog.DEFAULT_CONFIG["check_interval"])
            self.assertEqual(config["retry_count"], watchdog.DEFAULT_CONFIG["retry_count"])
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# get_password
# ---------------------------------------------------------------------------

class TestGetPassword(unittest.TestCase):
    def setUp(self):
        # Clear any relevant env vars before each test
        for key in ("CAMERA_PASSWORD", "CAMERA_PASSWORD_CAM1", "CAMERA_PASSWORD_FRONTDOOR"):
            os.environ.pop(key, None)

    def test_per_camera_variable(self):
        os.environ["CAMERA_PASSWORD_CAM1"] = "secret1"
        self.assertEqual(watchdog.get_password("cam1"), "secret1")

    def test_fallback_variable(self):
        os.environ["CAMERA_PASSWORD"] = "globalpass"
        self.assertEqual(watchdog.get_password("cam1"), "globalpass")

    def test_per_camera_takes_precedence(self):
        os.environ["CAMERA_PASSWORD"] = "globalpass"
        os.environ["CAMERA_PASSWORD_CAM1"] = "specific"
        self.assertEqual(watchdog.get_password("cam1"), "specific")

    def test_no_password_returns_empty(self):
        self.assertEqual(watchdog.get_password("cam1"), "")

    def tearDown(self):
        for key in ("CAMERA_PASSWORD", "CAMERA_PASSWORD_CAM1", "CAMERA_PASSWORD_FRONTDOOR"):
            os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# check_onvif
# ---------------------------------------------------------------------------

class TestCheckOnvif(unittest.TestCase):
    def _make_onvif_cam(self, snapshot_uri="http://cam/snap", status_code=200):
        mock_cam = MagicMock()
        media = MagicMock()
        mock_cam.create_media_service.return_value = media

        profile = MagicMock()
        profile.token = "token1"
        media.GetProfiles.return_value = [profile]

        snap_result = MagicMock()
        snap_result.Uri = snapshot_uri
        media.GetSnapshotUri.return_value = snap_result

        return mock_cam

    @patch("watchdog.requests.get")
    @patch("watchdog.ONVIFCamera")
    def test_success(self, mock_onvif_cls, mock_get):
        mock_onvif_cls.return_value = self._make_onvif_cam()
        mock_get.return_value = MagicMock(status_code=200)

        result = watchdog.check_onvif("10.0.0.1", 8000, "admin", "pass")
        self.assertTrue(result)

    @patch("watchdog.requests.get")
    @patch("watchdog.ONVIFCamera")
    def test_non_200_snapshot(self, mock_onvif_cls, mock_get):
        mock_onvif_cls.return_value = self._make_onvif_cam()
        mock_get.return_value = MagicMock(status_code=401)

        result = watchdog.check_onvif("10.0.0.1", 8000, "admin", "pass")
        self.assertFalse(result)

    @patch("watchdog.ONVIFCamera")
    def test_onvif_exception(self, mock_onvif_cls):
        mock_onvif_cls.side_effect = Exception("connection refused")

        result = watchdog.check_onvif("10.0.0.1", 8000, "admin", "pass")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# cycle_services
# ---------------------------------------------------------------------------

class TestCycleServices(unittest.TestCase):
    @patch("watchdog.time.sleep")
    @patch("watchdog.requests.post")
    def test_disables_then_enables(self, mock_post, mock_sleep):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        watchdog.cycle_services("10.0.0.1", 80, "admin", "pass", cycle_wait=10)

        self.assertEqual(mock_post.call_count, 2)

        # First call should have onvifEnable=0 and rtspEnable=0
        first_payload = mock_post.call_args_list[0][1]["json"]
        self.assertEqual(first_payload[0]["param"]["NetPort"]["onvifEnable"], 0)
        self.assertEqual(first_payload[0]["param"]["NetPort"]["rtspEnable"], 0)

        # Second call should have onvifEnable=1 and rtspEnable=1
        second_payload = mock_post.call_args_list[1][1]["json"]
        self.assertEqual(second_payload[0]["param"]["NetPort"]["onvifEnable"], 1)
        self.assertEqual(second_payload[0]["param"]["NetPort"]["rtspEnable"], 1)

        # Sleep called with the cycle_wait value
        mock_sleep.assert_called_once_with(10)

    @patch("watchdog.time.sleep")
    @patch("watchdog.requests.post")
    def test_api_failure_does_not_raise(self, mock_post, mock_sleep):
        mock_post.side_effect = Exception("network error")

        # Should not raise
        watchdog.cycle_services("10.0.0.1", 80, "admin", "pass", cycle_wait=10)
        mock_sleep.assert_not_called()

    @patch("watchdog.time.sleep")
    @patch("watchdog.requests.post")
    def test_custom_http_port_in_url(self, mock_post, mock_sleep):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        watchdog.cycle_services("10.0.0.1", 8080, "admin", "pass", cycle_wait=5)

        called_url = mock_post.call_args_list[0][0][0]
        self.assertIn(":8080", called_url)

    @patch("watchdog.time.sleep")
    @patch("watchdog.requests.post")
    def test_https_for_port_443(self, mock_post, mock_sleep):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        watchdog.cycle_services("10.0.0.1", 443, "admin", "pass", cycle_wait=5)

        called_url = mock_post.call_args_list[0][0][0]
        self.assertTrue(called_url.startswith("https://"))


# ---------------------------------------------------------------------------
# watch_camera
# ---------------------------------------------------------------------------

class TestWatchCamera(unittest.TestCase):
    def _global_cfg(self, retry_count=3, retry_delay=0, cycle_wait=0):
        return {
            "check_interval": 10,
            "retry_count": retry_count,
            "retry_delay": retry_delay,
            "cycle_wait": cycle_wait,
        }

    def _cam_cfg(self):
        return {
            "name": "testcam",
            "ip": "10.0.0.1",
            "onvif_port": 8000,
            "http_port": 80,
            "username": "admin",
        }

    @patch("watchdog.cycle_services")
    @patch("watchdog.check_onvif", return_value=True)
    def test_healthy_camera_no_cycle(self, mock_check, mock_cycle):
        watchdog.watch_camera(self._cam_cfg(), self._global_cfg())
        mock_cycle.assert_not_called()
        mock_check.assert_called_once()

    @patch("watchdog.time.sleep")
    @patch("watchdog.cycle_services")
    @patch("watchdog.check_onvif", return_value=False)
    def test_all_retries_fail_triggers_cycle(self, mock_check, mock_cycle, mock_sleep):
        watchdog.watch_camera(self._cam_cfg(), self._global_cfg(retry_count=3))
        self.assertEqual(mock_check.call_count, 3)
        mock_cycle.assert_called_once()

    @patch("watchdog.time.sleep")
    @patch("watchdog.cycle_services")
    @patch("watchdog.check_onvif", side_effect=[False, False, True])
    def test_succeeds_on_third_attempt_no_cycle(self, mock_check, mock_cycle, mock_sleep):
        watchdog.watch_camera(self._cam_cfg(), self._global_cfg(retry_count=3))
        self.assertEqual(mock_check.call_count, 3)
        mock_cycle.assert_not_called()


# ---------------------------------------------------------------------------
# main — basic smoke test (no infinite loop)
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):
    @patch("watchdog.time.sleep", side_effect=KeyboardInterrupt)
    @patch("watchdog.watch_camera")
    def test_main_runs_one_iteration(self, mock_watch, mock_sleep):
        import tempfile

        cfg_data = {
            "check_interval": 5,
            "retry_count": 1,
            "retry_delay": 0,
            "cycle_wait": 0,
            "cameras": [
                {"name": "cam1", "ip": "10.0.0.1", "onvif_port": 8000, "http_port": 80, "username": "admin"}
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
            yaml.dump(cfg_data, fh)
            path = fh.name

        try:
            with self.assertRaises(KeyboardInterrupt):
                watchdog.main(path)
            mock_watch.assert_called_once()
        finally:
            os.unlink(path)

    def test_main_exits_on_missing_config(self):
        with self.assertRaises(SystemExit):
            watchdog.main("/nonexistent/config.yaml")


if __name__ == "__main__":
    unittest.main()
