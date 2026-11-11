"""Keep every test run away from the real relay state and credentials."""
import os
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="codex-ask-tests-")
os.environ["JARVIS_STATE_DIR"] = os.path.join(_SANDBOX, "state")
os.environ["JARVIS_RELAY_CONFIG_DIR"] = os.path.join(_SANDBOX, "config")
for _name in ("JARVIS_RELAY_TOKEN", "JARVIS_RELAY_TOKENS_JSON"):
    os.environ.pop(_name, None)
