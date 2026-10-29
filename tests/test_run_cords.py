import tempfile
import typing
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from hardware import db
from hardware.run_cords import (
    Actuators,
    capture_camera_frame,
    capture_camera_test_frame,
    conveyor_rate_hz,
    save_camera_preview,
)
from hardware.vision import USBWebcam


class FakeHX711:
    def __init__(self, *, offset=0, tare_value=1200, raw_value=3200):
        self.offset = offset
        self.scale = 21500.0
        self.tare_value = tare_value
        self.raw_value = raw_value

    def tare(self, samples):
        return self.tare_value

    def read_raw_median(self, samples):
        return self.raw_value


class ConveyorRateTests(unittest.TestCase):
    def test_driver_rate_matches_configured_conveyor_rate(self):
        args = SimpleNamespace(conveyor_hz=2000.0, sample_hz=200.0)
        self.assertEqual(conveyor_rate_hz(args), 2000.0)

    def test_driver_rate_falls_back_to_sampling_rate_then_default(self):
        self.assertEqual(conveyor_rate_hz(SimpleNamespace(conveyor_hz=None, sample_hz=250.0)), 250.0)
        self.assertEqual(conveyor_rate_hz(SimpleNamespace(conveyor_hz=None, sample_hz=None)), 400.0)


class ActuatorCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.temp.name) / "cords.db")
        db.init(self.conn)
        self.actuators = Actuators.__new__(Actuators)
        self.hx = FakeHX711()
        self.actuators.hx_bins = {"great": self.hx}

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_bin_tare_updates_current_offset_and_persists_it(self):
        result = self.actuators.calibrate_bin_scale(self.conn, "great", "tare")

        self.assertEqual(self.hx.offset, 1200)
        self.assertEqual(db.get_setting(self.conn, "hx711_offset_bin_great"), "1200")
        self.assertIn("tared", result)

    def test_bin_scale_uses_tare_offset_and_persists_scale(self):
        self.hx.offset = 1200

        result = self.actuators.calibrate_bin_scale(
            self.conn, "great", "scale", ref_kg=2.0
        )

        self.assertEqual(self.hx.scale, 1000.0)
        self.assertEqual(db.get_setting(self.conn, "hx711_scale_bin_great"), "1000.0")
        self.assertIn("1000.0 counts/kg", result)

    def test_bin_tare_without_readings_is_rejected_without_persisting(self):
        self.hx.tare_value = None

        with self.assertRaisesRegex(RuntimeError, "no HX711 readings"):
            self.actuators.calibrate_bin_scale(self.conn, "great", "tare")

        self.assertIsNone(db.get_setting(self.conn, "hx711_offset_bin_great"))

    def test_bin_scale_requires_positive_finite_reference_mass(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.actuators.calibrate_bin_scale(
                    self.conn, "great", "scale", ref_kg=value
                )


class CameraTestReadinessTests(unittest.TestCase):
    def test_preview_helper_annotations_resolve_at_import(self):
        self.assertIs(typing.get_type_hints(save_camera_preview)["preview_path"], Path)

    def test_preview_capture_does_not_require_classifier(self):
        frame = object()
        camera = SimpleNamespace(read_frame=lambda: (True, frame))
        self.assertIs(capture_camera_frame(camera), frame)

    def test_camera_test_rejects_missing_camera(self):
        classifier = SimpleNamespace(is_ready=True)
        with self.assertRaisesRegex(RuntimeError, "camera is not initialized"):
            capture_camera_test_frame(None, classifier)

    def test_camera_test_rejects_missing_model(self):
        camera = SimpleNamespace(read_frame=lambda: (True, object()))
        classifier = SimpleNamespace(is_ready=False)
        with self.assertRaisesRegex(RuntimeError, "vision model is not loaded"):
            capture_camera_test_frame(camera, classifier)

    def test_camera_test_rejects_missing_frame(self):
        camera = SimpleNamespace(read_frame=lambda: (False, None))
        classifier = SimpleNamespace(is_ready=True)
        with self.assertRaisesRegex(RuntimeError, "no frame"):
            capture_camera_test_frame(camera, classifier)

    def test_camera_test_returns_real_frame(self):
        frame = object()
        camera = SimpleNamespace(read_frame=lambda: (True, frame))
        classifier = SimpleNamespace(is_ready=True)

        self.assertIs(capture_camera_test_frame(camera, classifier), frame)

    def test_preview_write_uses_encoded_jpeg_and_atomic_replace(self):
        class EncodedFrame:
            @staticmethod
            def tobytes():
                return b"jpeg-data"

        cv2 = SimpleNamespace(imencode=lambda ext, frame: (ext == ".jpg", EncodedFrame()))
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "tmp" / "camera_preview.jpg"
            with patch.dict("sys.modules", {"cv2": cv2}):
                save_camera_preview(object(), target)

            self.assertEqual(target.read_bytes(), b"jpeg-data")
            self.assertFalse(target.with_name("camera_preview.jpg.tmp").exists())

    def test_preview_write_surfaces_encode_failure(self):
        cv2 = SimpleNamespace(imencode=lambda ext, frame: (False, None))
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "camera_preview.jpg"
            with patch.dict("sys.modules", {"cv2": cv2}):
                with self.assertRaisesRegex(RuntimeError, "JPEG encoding failed"):
                    save_camera_preview(object(), target)

    def test_usb_webcam_falls_back_to_other_video_indices(self):
        class FakeCapture:
            def __init__(self, idx):
                self.idx = idx
                self._opened = idx == 2

            def isOpened(self):
                return self._opened

            def set(self, *args, **kwargs):
                return True

        def fake_video_capture(idx, backend=None):
            if idx == 0:
                return FakeCapture(0)
            if idx == 2:
                return FakeCapture(2)
            return FakeCapture(-1)

        cv2 = SimpleNamespace(VideoCapture=fake_video_capture, CAP_V4L2=1, CAP_PROP_FRAME_WIDTH=1, CAP_PROP_FRAME_HEIGHT=2)
        cam = USBWebcam(camera_index=0)
        with patch.dict("sys.modules", {"cv2": cv2}):
            self.assertTrue(cam.open())
            self.assertEqual(cam.camera_index, 2)


if __name__ == "__main__":
    unittest.main()
