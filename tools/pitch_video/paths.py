"""Where things live. CODE is this folder; WORK holds everything generated (frames, library copy,
narration audio, output). Override WORK with the PITCH_WORK environment variable."""
import os

CODE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(CODE))
WORK = os.environ.get("PITCH_WORK") or os.path.join(CODE, "work")
os.makedirs(WORK, exist_ok=True)
