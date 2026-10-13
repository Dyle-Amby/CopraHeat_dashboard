import unittest
from pathlib import Path

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

    def test_cli_args(self):
        args = vision.parse_args(["--camera", "1", "--live"])
        self.assertEqual(args.camera, 1)
        self.assertTrue(args.live)


if __name__ == "__main__":
    unittest.main()
