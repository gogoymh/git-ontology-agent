"""MCP tools for a single local GOA conversation over stdio."""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from functools import cached_property
from typing import Any, BinaryIO, Callable, Dict, Optional

from . import __version__, log
from .agent import Agent
from .gitstore import ANSWERS
from .serve import HISTORY_LIMIT, HISTORY_MAX

_log = log.get("mcp")
VERSIONS = ("2025-11-25", "2025-06-18")
COMMIT_ID = r"^([0-9a-f]{40}|[0-9a-f]{64})$"

ASK = {
    "name": "goa_ask",
    "description": "Submit messages to GOA and return its answers and their Git provenance. "
                   "Pass a request and its follow-ups in order to apply them before running the work.",
    "inputSchema": {
        "type": "object",
        "properties": {"messages": {"type": "array", "minItems": 1,
                                    "items": {"type": "string", "pattern": r"\S"}}},
        "required": ["messages"],
        "additionalProperties": False,
    },
}
HISTORY = {
    "name": "goa_history",
    "description": "Read recent GOA conversation turns from Git in chronological order. "
                   "Pass the returned next_before value as before to read an older page.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": HISTORY_MAX, "default": HISTORY_LIMIT,
                      "description": "Maximum number of answers, including their associated user turns."},
            "before": {"type": "string", "pattern": COMMIT_ID,
                       "description": "An answer commit returned as next_before by a previous page."},
        },
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class ToolInputError(ValueError):
    pass


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


class Server:
    def __init__(self, make_agent: Callable[[], Agent]):
        self.make_agent = make_agent
        self.version = ""
        self.ready = False

    @cached_property
    def agent(self) -> Agent:
        return self.make_agent()

    def handle(self, line: bytes) -> Optional[Dict[str, Any]]:
        try:
            message = json.loads(line.decode("utf-8"), parse_constant=_invalid_constant)
        except (ValueError, RecursionError):
            return _error(None, -32700, "Parse error")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        request_id = message.get("id")
        is_request = "id" in message
        if is_request and type(request_id) not in (int, str):
            return _error(None, -32600, "Request ID must be a string or an integer")
        method = message.get("method")
        if not isinstance(method, str):
            return _error(request_id, -32600, "Invalid Request")
        params = message.get("params", {})
        if not is_request:
            if method == "notifications/initialized" and self.version and isinstance(params, dict):
                self.ready = True
            return None
        if not isinstance(params, dict):
            return _error(request_id, -32602, "params must be an object")
        try:
            result = self.dispatch(method, params)
        except RPCError as exc:
            return _error(request_id, exc.code, str(exc))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def dispatch(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            return self.initialize(params)
        if method == "ping":
            return {}
        if not self.ready:
            raise RPCError(-32002, "Server is not initialized")
        if method == "tools/list":
            if params.get("cursor") is not None:
                raise RPCError(-32602, "Unknown tools cursor")
            return {"tools": [ASK, HISTORY]}
        if method == "tools/call":
            return self.call_tool(params)
        raise RPCError(-32601, "Method not found")

    def initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.version:
            raise RPCError(-32600, "Server already initialized")
        version = params.get("protocolVersion")
        client = params.get("clientInfo")
        if (not isinstance(version, str) or not isinstance(params.get("capabilities"), dict)
                or not isinstance(client, dict) or not isinstance(client.get("name"), str)
                or not isinstance(client.get("version"), str)):
            raise RPCError(-32602, "initialize requires protocolVersion, capabilities and clientInfo")
        self.version = version if version in VERSIONS else VERSIONS[0]
        return {"protocolVersion": self.version, "capabilities": {"tools": {}},
                "serverInfo": {"name": "git-ontology-agent", "version": __version__}}

    def call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise RPCError(-32602, "tools/call requires a tool name and an arguments object")
        tools = {ASK["name"]: self.ask, HISTORY["name"]: self.history}
        if name not in tools:
            raise RPCError(-32602, "Unknown tool")
        try:
            data = tools[name](arguments)
        except ToolInputError as exc:
            return _tool_error(str(exc))
        except Exception as exc:
            _log.error("%s failed: %s", name, exc)
            return _tool_error("GOA could not complete this call. See the server's stderr for details.")
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data}

    def ask(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        messages = arguments.get("messages")
        if (arguments.keys() != {"messages"} or not isinstance(messages, list) or not messages
                or any(not isinstance(message, str) or not message.strip() for message in messages)):
            raise ToolInputError("Provide messages as a nonempty array of nonblank strings.")
        agent = self.agent
        answers = []
        for message in messages:
            answers.extend(agent.submit(message))
        # Leave the target open so the next call can answer the clarification first.
        if not agent.pending:
            answers.extend(agent.run())
        return {"answers": [asdict(answer) for answer in answers], "in_progress": agent.busy(),
                "needs_clarification": bool(agent.pending)}

    def history(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        limit = arguments.get("limit", HISTORY_LIMIT)
        before = arguments.get("before")
        if arguments.keys() - {"limit", "before"} or type(limit) is not int or not 1 <= limit <= HISTORY_MAX:
            raise ToolInputError(f"Pass a limit between 1 and {HISTORY_MAX} and an optional before cursor.")
        if "before" in arguments and (not isinstance(before, str) or not re.fullmatch(COMMIT_ID, before)):
            raise ToolInputError("Use an answer commit from next_before as the before cursor.")
        agent = self.agent
        if before:
            head = agent.repo.rev(ANSWERS)
            try:
                valid_cursor = head and agent.repo.is_ancestor(before, head)
            except RuntimeError:
                valid_cursor = False
            if not valid_cursor:
                raise ToolInputError("The before cursor is not in this conversation's answer history.")
        items = agent.history(limit=limit, before=before or "")
        oldest = next((item["commit"] for item in items if item["type"] == "answer"), "")
        return {"items": items, "next_before": oldest if oldest and agent.has_older(oldest) else None}


def serve_mcp(make_agent: Callable[[], Agent], stdin: BinaryIO, stdout: BinaryIO) -> int:
    server = Server(make_agent)
    for line in stdin:
        reply = server.handle(line)
        if reply is not None:
            try:
                stdout.write(json.dumps(reply).encode("utf-8") + b"\n")
                stdout.flush()
            except BrokenPipeError:
                break
    return 0
