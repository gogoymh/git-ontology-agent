"""Exercise the public stdio command with MCP requests and notifications."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEETING = "What time is tomorrow's meeting?"


def request(request_id, method, **params):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def handshake(version="2025-11-25"):
    return [request("init", "initialize", protocolVersion=version, capabilities={},
                    clientInfo={"name": "goa-test", "version": "1"}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"}]


def tool(request_id, name, **arguments):
    return request(request_id, "tools/call", name=name, arguments=arguments)


class MCP(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="goa-mcp-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "conversation"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GOA_")}
        self.env.update(GOA_ENV_FILE=os.devnull, GOA_CONFIG=str(self.tmp / "config"), GOA_LOG="info",
                        PYTHONPATH=str(ROOT), PYTHONIOENCODING="ascii")

    def exchange(self, messages, argv=None):
        if argv is None:
            argv = [sys.executable, "-m", "goa", "mcp", "--repo", str(self.repo),
                    "--llm", "offline", "--decision", "local", "--lang", "en"]
        lines = [message if isinstance(message, bytes) else json.dumps(message).encode("utf-8")
                 for message in messages]
        proc = subprocess.run(argv, input=b"\n".join(lines) + b"\n", capture_output=True,
                              cwd=self.tmp, env=self.env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
        self.assertIn(b"goa.cli", proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertTrue(all(reply["jsonrpc"] == "2.0" for reply in replies))
        return replies

    def tool_result(self, reply):
        result = reply["result"]
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(result["content"][0]["type"], "text")
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data, result["structuredContent"])
        return data

    def test_initialize_and_list_tools(self):
        for requested, negotiated in [("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
                                      ("unknown-version", "2025-11-25")]:
            with self.subTest(requested=requested):
                init, listed, ping = self.exchange([*handshake(requested), request(2, "tools/list"),
                                                    request(3, "ping")])
                self.assertEqual([init["id"], listed["id"], ping["id"]], ["init", 2, 3])
                self.assertEqual(init["result"]["protocolVersion"], negotiated)
                self.assertEqual(init["result"]["serverInfo"]["name"], "git-ontology-agent")
                self.assertIn("tools", init["result"]["capabilities"])
                self.assertEqual(ping["result"], {})
                tools = {entry["name"]: entry for entry in listed["result"]["tools"]}
                self.assertEqual(set(tools), {"goa_ask", "goa_history"})
                self.assertEqual(tools["goa_ask"]["inputSchema"]["required"], ["messages"])
                self.assertTrue(tools["goa_history"]["annotations"]["readOnlyHint"])
                self.assertFalse(self.repo.exists())

    def test_ask_merges_request_and_follow_up(self):
        _, reply = self.exchange([*handshake(), tool(2, "goa_ask", messages=[
            "Plan my Busan business trip next week", "Oh, keep the budget under 500,000 won"])])
        data = self.tool_result(reply)
        answers = data["answers"]
        self.assertEqual([answer["kind"] for answer in answers], ["interim", "interim", "final"])
        self.assertEqual(answers[1]["relation"], "k1")
        self.assertIn("₩500,000", answers[-1]["text"])
        self.assertRegex(answers[-1]["source"], r"^main@[0-9a-f]{7}$")
        self.assertFalse(data["in_progress"])
        self.assertFalse(data["needs_clarification"])
        git = ["git", "-C", str(self.repo)]
        self.assertEqual(subprocess.check_output([*git, "rev-parse", "answers"], text=True).strip(),
                         answers[-1]["commit"])
        self.assertEqual(subprocess.check_output([*git, "rev-parse", "--short=7", "main"], text=True).strip(),
                         answers[-1]["source"].split("@")[1])

    def test_history_survives_restart(self):
        _, reply = self.exchange([*handshake(), tool(2, "goa_ask", messages=[MEETING])])
        started, final = self.tool_result(reply)["answers"]

        _, reply = self.exchange([*handshake(), tool(2, "goa_history", limit=1)])
        recent = self.tool_result(reply)
        self.assertEqual([item["commit"] for item in recent["items"]], [final["commit"]])
        self.assertEqual(recent["next_before"], final["commit"])

        _, reply = self.exchange([*handshake(), tool(2, "goa_history", limit=1, before=recent["next_before"])])
        older = self.tool_result(reply)
        self.assertEqual(older["items"][0], {"type": "user", "n": 1, "text": MEETING})
        self.assertEqual(older["items"][1]["commit"], started["commit"])
        self.assertIsNone(older["next_before"])

    def test_bad_requests_do_not_end_session(self):
        replies = self.exchange([
            request(0, "tools/list"), *handshake(), b"{malformed", b"\xff", b"{}",
            request(2, "goa/missing"), tool(3, "goa_missing"),
            request(4, "tools/call", name="goa_ask", arguments=[]),
            tool(5, "goa_ask", messages=[MEETING, "  "]),
            tool(6, "goa_history", limit=True), tool(7, "goa_history", before="--all"),
            tool(8, "goa_history", before="0" * 40), tool(9, "goa_history"), tool(10, "goa_ask", messages=[MEETING]),
        ])
        self.assertEqual([reply["id"] for reply in replies], [0, "init", None, None, None, *range(2, 11)])
        self.assertEqual([reply["error"]["code"] for reply in [replies[0], *replies[2:8]]],
                         [-32002, -32700, -32700, -32600, -32601, -32602, -32602])
        self.assertTrue(all(reply["result"]["isError"] for reply in replies[8:12]))
        self.assertEqual(self.tool_result(replies[12])["items"], [])
        self.assertEqual(self.tool_result(replies[13])["answers"][-1]["kind"], "final")

    def test_readme_configuration(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        example = re.search(r"^## MCP\n.*?^```json\n(.*?)^```", readme, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(example)
        config = json.loads(example.group(1))["mcpServers"]["goa"]
        argv = [config["command"], *config["args"]]
        argv[argv.index("--repo") + 1] = str(self.repo)
        _, listed = self.exchange([*handshake(), request(2, "tools/list")], argv=argv)
        self.assertEqual({entry["name"] for entry in listed["result"]["tools"]}, {"goa_ask", "goa_history"})
