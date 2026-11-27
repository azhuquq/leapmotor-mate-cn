"""The setup wizard posts only the cars its last detection found.

A detection fills hidden fields (VIN, model, the list of cars) that the form submits. A second attempt
used to fill them only when it succeeded, so a retry that failed, or one run with another account's
email, still submitted the cars found before: finishing setup by hand configured those, with their
packs and the PIN typed for the new attempt. Each attempt now starts from empty fields, and editing
the email empties them too, so setup waits for a detection with that email. The email is locked while
a detection runs, so an answer that arrives late is for the email shown.
"""
import pathlib
import shutil
import subprocess

import pytest
from test_the_wizard_lets_a_two_car_account_finish import _rendered_script

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_wizard_posts_only_the_cars_its_last_detection_found(tmp_path):
    """Run in node against the real script: see setup_wizard_detection_fails.cjs."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required to run the wizard's own script")
    subprocess.run([node, str(ROOT / "tests/setup_wizard_detection_fails.cjs"),
                    str(_rendered_script(tmp_path)), "posts"], check=True)
