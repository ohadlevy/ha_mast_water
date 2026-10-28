"""Offline checks for the credential-handling behavior of the research probe."""

import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "probe", Path(__file__).parents[1] / "probe_session_resume.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def jwt(expiry):
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode())
        .decode()
        .rstrip("=")
    )
    return "header." + payload + ".dummy-signature"


class Response(io.BytesIO):
    def __init__(self, body, headers=None, status=200):
        super().__init__(json.dumps(body).encode())
        self.code = status
        self.headers = headers or {}


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return next(self.responses)


class ProbeTests(unittest.TestCase):
    def test_independent_resume_rotation_and_water_access(self):
        old, new = jwt(1700000000), jwt(1800000000)
        opener = Opener(
            [
                Response(
                    {
                        "isSuccess": True,
                        "user": {"token": "rotated-secret", "phone": "never-print"},
                    },
                    {"new-token": new},
                ),
                Response({"isValid": True}),
                Response([{"waterCardNumber": "never-print"}]),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "session.json"
            result = probe.probe(
                {"user_token": "original-secret", "jwt_token": old},
                "110",
                output,
                opener,
            )
            self.assertTrue(result["jwt_expiry_extended"])
            self.assertEqual(result["water_card_count"], 1)
            self.assertNotIn(
                "jwt", {name.lower() for name, _ in opener.requests[0].header_items()}
            )
            self.assertFalse(opener.requests[0].has_header("Cookie"))
            self.assertEqual(opener.requests[1].get_header("Jwt"), "Bearer " + new)
            self.assertEqual(
                json.loads(output.read_text()),
                {"user_token": "rotated-secret", "jwt_token": new},
            )
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            for secret in (
                "original-secret",
                "rotated-secret",
                "never-print",
                new,
                old,
            ):
                self.assertNotIn(secret, json.dumps(result))

    def test_rejected_resume_does_not_disclose_echoed_secret(self):
        opener = Opener(
            [
                Response(
                    {
                        "isSuccess": False,
                        "message": "echoed-original-secret",
                        "user": None,
                    }
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "session.json"
            result = probe.probe(
                {"user_token": "original-secret"}, "110", output, opener
            )
            self.assertFalse(result["resume_success"])
            self.assertNotIn("original-secret", json.dumps(result))
            self.assertFalse(output.exists())
            self.assertEqual(len(opener.requests), 1)

    def test_exception_text_is_not_printed(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "input.json"
            credentials.write_text(json.dumps({"user_token": "secret"}))
            args = [
                "probe",
                "--credentials-file",
                str(credentials),
                "--output-file",
                str(Path(directory) / "output.json"),
            ]
            stdout = io.StringIO()
            with (
                patch.object(sys, "argv", args),
                patch.object(
                    probe, "probe", side_effect=RuntimeError("URL?token=secret")
                ),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(probe.main(), 2)
            self.assertNotIn("secret", stdout.getvalue())

    def test_existing_output_is_preserved_before_any_request(self):
        opener = Opener([])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.json"
            output.write_text("existing-session")
            with self.assertRaises(FileExistsError):
                probe.probe({"user_token": "secret"}, "110", output, opener)
            self.assertEqual(output.read_text(), "existing-session")
            self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
