"""Computer vision grading module for CORDS.

Captures images from a generic 1080p USB webcam via OpenCV and classifies
copra quality (Great / Good / Bad) using the MobileNetV3 TFLite model.

Usage:
    # Test a single capture & prediction from USB webcam (default index 0):
    python -m hardware.vision

    # Specify camera index or custom model path:
    python -m hardware.vision --camera 0 --model cords_model.tflite

    # Continuous live preview / detection loop:
    python -m hardware.vision --live
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Paths to default model and labels in CopraHeat_dashboard root
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = BASE_DIR / "cords_model.tflite"
DEFAULT_LABELS_PATH = BASE_DIR / "labels.txt"


def load_labels(path: Path | str | None = None) -> list[str]:
    """Load class labels from file, or return default ['bad', 'good', 'great']."""
    p = Path(path if path is not None else DEFAULT_LABELS_PATH)
    if p.is_file():
        with open(p, "r", encoding="utf-8") as f:
            labels = [line.strip().lower() for line in f if line.strip()]
        if labels:
            return labels
    return ["bad", "good", "great"]


class CopraClassifier:
    """TFLite interpreter wrapper for MobileNetV3 copra grading."""

    def __init__(self, model_path: Path | str | None = None, labels_path: Path | str | None = None):
        self.model_path = Path(model_path if model_path is not None else DEFAULT_MODEL_PATH)
        self.labels = load_labels(labels_path)
        self.interpreter = None
        self.input_details = None
        self.output_details = None
        self.input_shape = (224, 224)

        if not self.model_path.is_file():
            print(f"Warning: model file not found at {self.model_path}. Vision classifier will be mock-only.")
            return

        self._load_interpreter()

    def _load_interpreter(self) -> None:
        try:
            import tflite_runtime.interpreter as tflite

            self.interpreter = tflite.Interpreter(model_path=str(self.model_path))
        except ImportError:
            try:
                import tensorflow.lite as tflite

                self.interpreter = tflite.Interpreter(model_path=str(self.model_path))
            except ImportError:
                print(
                    "Notice: neither 'tflite-runtime' nor 'tensorflow' is installed. "
                    "Inference will fall back to mock mode."
                )
                return

        self.interpreter.allocate_tensors()
        self.input_details = self.interpreter.get_input_details()
        self.output_details = self.interpreter.get_output_details()

        # (batch, height, width, channels)
        shape = self.input_details[0]["shape"]
        self.input_shape = (int(shape[1]), int(shape[2]))

    @property
    def is_ready(self) -> bool:
        return self.interpreter is not None

    def predict(self, frame) -> tuple[str, float, dict[str, float]]:
        """Classify a BGR/RGB frame (numpy array).

        Returns:
            (top_grade, top_confidence, all_class_probabilities)
            e.g. ('great', 0.942, {'bad': 0.01, 'good': 0.048, 'great': 0.942})
        """
        import numpy as np

        if not self.is_ready:
            # Fallback mock prediction
            return "good", 0.85, {label: (0.85 if label == "good" else 0.075) for label in self.labels}

        import cv2

        # Resize to model input shape (224, 224)
        resized = cv2.resize(frame, self.input_shape)

        # Convert BGR (OpenCV default) to RGB
        if len(resized.shape) == 3 and resized.shape[2] == 3:
            resized = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        # Float32 in range [0, 255] as expected by MobileNetV3
        input_data = np.expand_dims(resized.astype(np.float32), axis=0)

        self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
        self.interpreter.invoke()

        output = self.interpreter.get_tensor(self.output_details[0]["index"])[0]

        # Map softmax output to class names
        scores = {}
        for idx, score in enumerate(output):
            label = self.labels[idx] if idx < len(self.labels) else f"class_{idx}"
            scores[label] = float(score)

        top_idx = int(np.argmax(output))
        top_grade = self.labels[top_idx] if top_idx < len(self.labels) else f"class_{top_idx}"
        top_confidence = float(output[top_idx])

        return top_grade, top_confidence, scores


class USBWebcam:
    """Manages video capture from a generic 1080p USB camera via OpenCV."""

    def __init__(self, camera_index: int = 0, width: int = 1920, height: int = 1080):
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self._cap = None

    def open(self) -> bool:
        try:
            import cv2

            self._cap = cv2.VideoCapture(self.camera_index)
            if not self._cap.isOpened():
                return False
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            return True
        except Exception:
            return False

    def read_frame(self):
        """Returns (success, frame_numpy_array)."""
        if self._cap is None or not self._cap.isOpened():
            if not self.open():
                return False, None
        return self._cap.read()

    def snapshot_grade(self, classifier: CopraClassifier) -> tuple[str, float, dict[str, float]]:
        """Grab a single frame, classify it, and return the result."""
        ok, frame = self.read_frame()
        if not ok or frame is None:
            raise RuntimeError(f"Could not read frame from USB camera #{self.camera_index}")
        return classifier.predict(frame)

    def capture_softmax(
        self,
        classifier: CopraClassifier,
        blur_threshold: float = 100.0,
        min_confidence: float = 0.55,
    ) -> list[float] | None:
        """Capture a frame, filter for blur & confidence, and return softmax vector.

        Returns:
            [p_bad, p_good, p_great] or None if rejected by blur or low confidence.
        """
        ok, frame = self.read_frame()
        if not ok or frame is None:
            return None

        # Check blur using Laplacian variance
        try:
            import cv2
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
            laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            if laplacian_var < blur_threshold:
                return None
        except Exception:
            pass  # Fall through if cv2 not available

        top_grade, top_conf, scores = classifier.predict(frame)
        if top_conf < min_confidence:
            return None

        # Return vector ordered by ("bad", "good", "great")
        return [scores.get(label, 0.0) for label in ("bad", "good", "great")]

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--camera", type=int, default=0, help="USB webcam device index (default: 0)")
    p.add_argument("--model", default=str(DEFAULT_MODEL_PATH), help="path to .tflite model")
    p.add_argument("--labels", default=str(DEFAULT_LABELS_PATH), help="path to labels.txt")
    p.add_argument("--live", action="store_true", help="run continuous live detection loop")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    print("=" * 60)
    print("CORDS USB WEBCAM VISION CLASSIFIER")
    print(f"Model  : {args.model}")
    print(f"Labels : {args.labels}")
    print(f"Camera : USB device index #{args.camera}")
    print("=" * 60)

    classifier = CopraClassifier(model_path=args.model, labels_path=args.labels)
    labels = classifier.labels
    print(f"Loaded class labels: {labels}")

    cam = USBWebcam(camera_index=args.camera)
    if not cam.open():
        print(f"\nError: Could not open USB webcam at index {args.camera}.")
        print("Tip: Check USB connection or try --camera 1.")
        return

    try:
        if args.live:
            import cv2

            print("\nStarting live classification loop (Press 'q' in window or Ctrl+C to stop)...")
            fps_t0 = time.monotonic()
            frames = 0
            while True:
                ok, frame = cam.read_frame()
                if not ok:
                    print("Camera frame drop, retrying...")
                    time.sleep(0.1)
                    continue

                grade, conf, scores = classifier.predict(frame)
                frames += 1

                # Display stats on image
                text = f"{grade.upper()} ({conf*100:.1f}%)"
                color = (0, 255, 0) if grade == "great" else ((0, 165, 255) if grade == "good" else (0, 0, 255))
                cv2.putText(frame, text, (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.5, color, 3)

                cv2.imshow("CORDS USB Camera Copra Classifier", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
        else:
            print("\nCapturing snapshot for classification...")
            # Allow camera auto-exposure to settle for 2 frames
            for _ in range(3):
                cam.read_frame()

            grade, conf, scores = cam.snapshot_grade(classifier)
            print("\n" + "-" * 40)
            print(f"PREDICTED GRADE: {grade.upper()} ({conf*100:.2f}%)")
            print("Confidence breakdown:")
            for cls_name, score in sorted(scores.items()):
                bar = "█" * int(score * 20)
                print(f"  {cls_name.upper():<6} : {score*100:>5.1f}% | {bar}")
            print("-" * 40)

    finally:
        cam.release()
        try:
            import cv2

            cv2.destroyAllWindows()
        except Exception:
            pass


if __name__ == "__main__":
    main()
