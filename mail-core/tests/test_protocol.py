"""Framing and envelope tests for the stdio protocol (docs/IPC_PROTOCOL.md v1)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest

from mail_core import protocol
from mail_core.__main__ import serve
from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.support import workspace_temporary_directory


class FakeService:
    """Stand-in for Service that records calls and never touches disk."""

    shutdown_requested = False

    def __init__(self) -> None:
        self.calls = []
        self._stdout = None

    def attach_stdout(self, stream) -> None:
        self._stdout = stream

    def emit_response(self, response) -> bool:
        self._stdout.write(protocol.dumps(response))
        self._stdout.write("\n")
        self._stdout.flush()
        return True

    def dispatch(self, method, params):
        self.calls.append((method, params))
        if method == "explode":
            raise MailCoreError(protocol.INTERNAL, "boom")
        return {"echo": method}


class FramingTests(unittest.TestCase):
    def test_response_is_one_line_of_json(self):
        line = protocol.dumps({"id": "1", "ok": True, "result": {"name": "收件箱"}})
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertEqual(json.loads(line)["result"]["name"], "收件箱")

    def test_consecutive_lines_each_get_one_response(self):
        payload = (
            '{"id":"1","method":"account.list","params":{}}\n'
            '{"id":"2","method":"sync.status"}\n'
            "\n"
            '{"id":"3","method":"account.list"}\n'
        )
        stdout = io.StringIO()
        service = FakeService()
        serve(service, io.StringIO(payload), stdout)

        lines = [line for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 3)
        responses = [json.loads(line) for line in lines]
        self.assertEqual([r["id"] for r in responses], ["1", "2", "3"])
        self.assertTrue(all(r["ok"] for r in responses))
        self.assertEqual(responses[0]["result"], {"echo": "account.list"})
        # params may be omitted entirely; it defaults to {}
        self.assertEqual(service.calls[1], ("sync.status", {}))

    def test_shutdown_stops_the_loop(self):
        payload = '{"id":"1","method":"shutdown"}\n{"id":"2","method":"account.list"}\n'
        stdout = io.StringIO()
        service = FakeService()
        service.dispatch = _shutdown_dispatch(service)
        serve(service, io.StringIO(payload), stdout)

        responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["result"], {"ok": True})


def _shutdown_dispatch(service):
    def dispatch(method, params):
        service.calls.append((method, params))
        if method == "shutdown":
            service.shutdown_requested = True
            return {"ok": True}
        return {"echo": method}

    return dispatch


class RequestValidationTests(unittest.TestCase):
    def test_malformed_json_returns_bad_request_with_null_id(self):
        response = protocol.handle_line(FakeService(), '{"id": "1", "method": }')
        self.assertIsNone(response["id"])
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "BAD_REQUEST")
        self.assertTrue(response["error"]["message"])

    def test_non_object_json_is_rejected(self):
        response = protocol.handle_line(FakeService(), '"just a string"')
        self.assertIsNone(response["id"])
        self.assertEqual(response["error"]["code"], "BAD_REQUEST")

    def test_missing_method_is_bad_request_and_keeps_id(self):
        response = protocol.handle_line(FakeService(), '{"id":"9"}')
        self.assertEqual(response["id"], "9")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "BAD_REQUEST")

    def test_params_must_be_an_object(self):
        response = protocol.handle_line(
            FakeService(), '{"id":"4","method":"account.list","params":[]}'
        )
        self.assertEqual(response["id"], "4")
        self.assertEqual(response["error"]["code"], "BAD_REQUEST")

    def test_unknown_method(self):
        with workspace_temporary_directory() as tmp:
            service = Service(tmp)
            try:
                response = protocol.handle_line(
                    service, '{"id":"7","method":"nope","params":{}}'
                )
            finally:
                service.close()
        self.assertEqual(response["id"], "7")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "UNKNOWN_METHOD")

    def test_unexpected_exception_becomes_internal(self):
        response = protocol.handle_line(FakeService(), '{"id":"5","method":"explode"}')
        self.assertEqual(response["id"], "5")
        self.assertEqual(response["error"]["code"], "INTERNAL")
        self.assertNotIn("Traceback", response["error"]["message"])


if __name__ == "__main__":
    unittest.main()
