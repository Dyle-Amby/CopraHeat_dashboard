import unittest
from pathlib import Path
from types import SimpleNamespace

from hardware import vision


class VisionTest(unittest.TestCase):
    def test_load_labels(self):
        labels = vision.load_labels()
        self.assertIn("great", labels)
        self.assertIn("good", labels)
        self.assertIn("bad", labels)

    def test_classifier_mock_fallback(self):
        # Even without tflite installed or model loaded, predict should return a valid tuple
        classifier = vision.CopraClassifier(model_path="nonexistent.tflite")
        grade, conf, scores = classifier.predict(None)
        self.assertIn(grade, ["great", "good", "bad"])
        self.assertGreater(conf, 0.0)
        self.assertIn(grade, scores)

    def test_capture_for_review_returns_frame_and_grade_details(self):
        frame = object()

        class FakeCapture:
            @staticmethod
            def isOpened():
                return True

            @staticmethod
            def read():
                return True, frame

        camera = vision.USBWebcam.__new__(vision.USBWebcam)
        camera._cap = FakeCapture()
        classifier = SimpleNamespace(
            predict=lambda captured: (
                "good", 0.82, {"bad": 0.08, "good": 0.82, "great": 0.10}
            )
        )

        captured, result = camera.capture_for_review(classifier)

        self.assertIs(captured, frame)
        self.assertEqual(result["grade"], "good")
        self.assertEqual(result["predicted_grade"], "good")
        self.assertAlmostEqual(result["confidence"], 0.82)
        self.assertTrue(result["quality_ok"])

    def test_cli_args(self):
        args = vision.parse_args(["--camera", "1", "--live"])
        self.assertEqual(args.camera, 1)
        self.assertTrue(args.live)


if __name__ == "__main__":
    unittest.main()
