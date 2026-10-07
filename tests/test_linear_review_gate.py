"""Tests for hooks/linear-review-gate.py — In Review requires a linked PR.

The gate (read from the script):
  * tool_name != mcp__linear__save_issue, or `state` isn't a targeted In Review
    value                                              -> silent allow (no-op).
  * state targets In Review AND the same call carries a `links` PR URL
                                                        -> silent allow.
  * state targets In Review, no `links` PR, and the Linear API is
    indeterminate (no key file / unreachable endpoint) -> fail-open allow.
  * state targets In Review, no `links` PR, and the API affirmatively reports
    no PR-shaped attachment                            -> DENY.

Config is env-only and optional: LINEAR_API_KEY or LINEAR_KEY_FILE for the
key, LINEAR_IN_REVIEW_STATE_ID for a state UUID matched alongside the names,
LINEAR_API_URL for the endpoint. Unconfigured, the hook never denies.

Because this hook calls the Linear API via `urllib.request` (not a shim-able
`curl` subprocess like the bash hooks), the only way to exercise its
positive/negative PR-attachment determination deterministically — without ever
touching the live Linear API — is to point LINEAR_API_URL at a throwaway local
HTTP server, which is what the override-specific tests below do.
"""
import http.server
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import HookSandbox, load_json, run_python_hook


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Base local Linear-API stub; subclasses set ATTACHMENTS."""
    ATTACHMENTS = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        resp = {"data": {"issue": {"attachments": {"nodes": self.ATTACHMENTS}}}}
        self.wfile.write(json.dumps(resp).encode())

    def log_message(self, *a):  # silence
        pass


class _NoPRHandler(_StubHandler):
    ATTACHMENTS = []


class _WithPRHandler(_StubHandler):
    ATTACHMENTS = [{"url": "https://github.com/o/r/pull/42"}]


def _start_stub(handler_cls):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


class LinearReviewGateTest(unittest.TestCase):
    def tearDown(self):
        self.sbx.close()

    def _run(self, payload, extra_env=None):
        return run_python_hook(
            self.sbx, "linear-review-gate.py",
            stdin_text=json.dumps(payload), extra_env=extra_env,
        )

    # -- deterministic paths (no network) ----------------------------------

    def test_non_review_state_is_silent_allow(self):
        self.sbx = HookSandbox()
        rc, out, _ = self._run({"tool_name": "mcp__linear__save_issue",
                                 "tool_input": {"id": "ENG-1", "state": "In Progress"}})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_wrong_tool_name_is_silent_allow(self):
        self.sbx = HookSandbox()
        rc, out, _ = self._run({"tool_name": "mcp__linear__create_issue",
                                 "tool_input": {"id": "ENG-1", "state": "In Review"}})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_review_with_pr_link_in_same_call_allows(self):
        self.sbx = HookSandbox()
        rc, out, _ = self._run({
            "tool_name": "mcp__linear__save_issue",
            "tool_input": {"id": "ENG-1", "state": "In Review",
                            "links": [{"url": "https://github.com/o/r/pull/9"}]},
        })
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_review_no_link_no_key_file_fails_open(self):
        self.sbx = HookSandbox()  # no key configured anywhere
        rc, out, _ = self._run({"tool_name": "mcp__linear__save_issue",
                                 "tool_input": {"id": "ENG-1", "state": "In Review"}})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_review_no_link_api_unreachable_fails_open(self):
        # Key present, but the endpoint refuses the connection -> caught, None -> allow.
        self.sbx = HookSandbox(linear_key="lin_fake")
        rc, out, _ = self._run(
            {"tool_name": "mcp__linear__save_issue", "tool_input": {"id": "ENG-1", "state": "In Review"}},
            extra_env={"LINEAR_API_URL": "http://127.0.0.1:1/graphql"},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    # -- env-override behavior ----------------------------------------------

    def test_default_in_review_string_still_denies_via_api_url_override(self):
        # No LINEAR_IN_REVIEW_STATE_ID -> the state NAME "In Review" is still
        # recognised; LINEAR_API_URL is redirected to a local
        # stub reporting no PR, so the deny path is reachable deterministically.
        server, port = _start_stub(_NoPRHandler)
        try:
            self.sbx = HookSandbox(linear_key="lin_fake")
            rc, out, _ = self._run(
                {"tool_name": "mcp__linear__save_issue", "tool_input": {"id": "ENG-1", "state": "In Review"}},
                extra_env={"LINEAR_API_URL": f"http://127.0.0.1:{port}/graphql"},
            )
        finally:
            server.shutdown()
        self.assertEqual(rc, 0)
        self.assertEqual(load_json(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_api_url_override_allows_when_pr_found(self):
        server, port = _start_stub(_WithPRHandler)
        try:
            self.sbx = HookSandbox(linear_key="lin_fake")
            rc, out, _ = self._run(
                {"tool_name": "mcp__linear__save_issue", "tool_input": {"id": "ENG-1", "state": "In Review"}},
                extra_env={"LINEAR_API_URL": f"http://127.0.0.1:{port}/graphql"},
            )
        finally:
            server.shutdown()
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_key_file_override_is_the_key_actually_used(self):
        # No key is configured in this sandbox at all, so without
        # LINEAR_KEY_FILE, has_pr_attachment has no key and must fail open
        # (allow) regardless of what the stub says.
        # Point LINEAR_KEY_FILE at a DIFFERENT path holding a DIFFERENT key
        # and use a no-PR stub: a DENY here is only possible if the override
        # path was actually opened, the key actually sent, and a real
        # request/response round-trip completed -- proving the override is
        # read, not merely tolerated.
        server, port = _start_stub(_NoPRHandler)
        try:
            self.sbx = HookSandbox()  # no key configured at all
            custom_key_path = os.path.join(self.sbx.dir, "custom-creds.json")
            with open(custom_key_path, "w") as fh:
                json.dump({"env": {"LINEAR_API_KEY": "lin_custom"}}, fh)
            rc, out, _ = self._run(
                {"tool_name": "mcp__linear__save_issue", "tool_input": {"id": "ENG-1", "state": "In Review"}},
                extra_env={"LINEAR_API_URL": f"http://127.0.0.1:{port}/graphql",
                           "LINEAR_KEY_FILE": custom_key_path},
            )
        finally:
            server.shutdown()
        self.assertEqual(rc, 0)
        self.assertEqual(load_json(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_in_review_state_id_override_recognizes_a_custom_id(self):
        # A state value that is NOT "in review"/"review" is recognized ONLY
        # once LINEAR_IN_REVIEW_STATE_ID names it.
        custom_id = "custom-review-id-777"
        server, port = _start_stub(_NoPRHandler)
        try:
            self.sbx = HookSandbox(linear_key="lin_fake")
            rc, out, _ = self._run(
                {"tool_name": "mcp__linear__save_issue", "tool_input": {"id": "ENG-1", "state": custom_id}},
                extra_env={"LINEAR_API_URL": f"http://127.0.0.1:{port}/graphql",
                           "LINEAR_IN_REVIEW_STATE_ID": custom_id},
            )
        finally:
            server.shutdown()
        self.assertEqual(rc, 0)
        self.assertEqual(load_json(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_custom_state_id_without_override_is_not_recognized(self):
        # Same custom state value as above, but with NO LINEAR_IN_REVIEW_STATE_ID
        # set -> must NOT be recognized as In Review -> silent allow, and the
        # (stubbed) API must never even be queried.
        custom_id = "custom-review-id-777"
        server, port = _start_stub(_NoPRHandler)
        try:
            self.sbx = HookSandbox(linear_key="lin_fake")
            rc, out, _ = self._run(
                {"tool_name": "mcp__linear__save_issue", "tool_input": {"id": "ENG-1", "state": custom_id}},
                extra_env={"LINEAR_API_URL": f"http://127.0.0.1:{port}/graphql"},
            )
        finally:
            server.shutdown()
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


    def test_api_key_env_is_used_without_a_key_file(self):
        # LINEAR_API_KEY alone (no key file) is enough to reach the API.
        server, port = _start_stub(_NoPRHandler)
        try:
            self.sbx = HookSandbox()
            rc, out, _ = self._run(
                {"tool_name": "mcp__linear__save_issue", "tool_input": {"id": "ENG-1", "state": "In Review"}},
                extra_env={"LINEAR_API_URL": f"http://127.0.0.1:{port}/graphql",
                           "LINEAR_API_KEY": "lin_env"},
            )
        finally:
            server.shutdown()
        self.assertEqual(rc, 0)
        self.assertEqual(load_json(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unconfigured_never_denies_even_when_api_would(self):
        # No key at all: even with an endpoint that would report "no PR", the
        # hook cannot authenticate, so it must stay a silent no-op.
        server, port = _start_stub(_NoPRHandler)
        try:
            self.sbx = HookSandbox()
            rc, out, _ = self._run(
                {"tool_name": "mcp__linear__save_issue", "tool_input": {"id": "ENG-1", "state": "In Review"}},
                extra_env={"LINEAR_API_URL": f"http://127.0.0.1:{port}/graphql"},
            )
        finally:
            server.shutdown()
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

if __name__ == "__main__":
    unittest.main()
