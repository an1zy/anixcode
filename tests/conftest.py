import os
import tempfile

# Tests check Russian strings and must never touch the real settings, keys or sessions.
os.environ["ANIXCODE_LANG"] = "ru"
os.environ["ANIXCODE_HOME"] = tempfile.mkdtemp(prefix="anixcode-tests-")
