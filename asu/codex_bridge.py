"""Small, stateless Responses -> CreateAI Chat Completions adapter.

Python standard library only. Credentials and request bodies are never logged.
This is a compatibility subset, not an implementation of all Responses features.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from asu.createai import BridgeError, dumps, sse_data
from asu.model_map import accepts_forced_tool, accepts_tool_choice_none


class ToolMap:
    def __init__(self, tools):
        self.by_wire = {}
        self.by_original = {}
        self.tools = []
        self._add(tools)

    def _add(self, tools, namespace=None):
        for tool in tools:
            kind = tool.get("type")
            if kind == "namespace":
                self._add(tool.get("tools", []), tool["name"])
                continue
            if kind not in ("function", "custom"):
                raise BridgeError(f"Unsupported tool type: {kind}. Disable hosted tools/web search.")
            name = tool["name"]
            original = (namespace, name)
            wire = "t_" + hashlib.sha256(dumps(original).encode()).hexdigest()[:32]
            if original in self.by_original:
                raise BridgeError("Duplicate tool name in namespace.")
            self.by_wire[wire] = (namespace, name, kind)
            self.by_original[original] = wire
            description = tool.get("description", "")
            if kind == "custom":
                description += "\nReturn the tool's raw input string in the input field."
                if tool.get("format", {}).get("type") == "grammar":
                    description += "\nInput grammar:\n" + tool["format"].get("definition", "")
                parameters = {"type": "object", "properties": {"input": {"type": "string"}},
                              "required": ["input"], "additionalProperties": False}
            else:
                parameters = tool.get("parameters", {"type": "object", "properties": {}})
            self.tools.append({"type": "function", "function": {
                "name": wire, "description": description, "parameters": parameters}})

    def wire_name(self, item):
        key = (item.get("namespace"), item["name"])
        if key in self.by_original:
            return self.by_original[key]
        # Old calls can belong to tools no longer offered on this turn.
        return "t_" + hashlib.sha256(dumps(key).encode()).hexdigest()[:32]

    def output(self, call):
        fn = call["function"]
        if fn["name"] not in self.by_wire:
            raise BridgeError("ASU returned an unrecognized tool name.", 502)
        namespace, name, kind = self.by_wire[fn["name"]]
        result = {"type": "custom_tool_call" if kind == "custom" else "function_call", "id": "fc_" + secrets.token_hex(12),
                  "call_id": call["id"], "name": name, "status": "completed"}
        if namespace:
            result["namespace"] = namespace
        try:
            args = json.loads(fn["arguments"])
            if not isinstance(args, dict):
                raise ValueError()
            if kind == "custom":
                if not isinstance(args.get("input"), str):
                    raise ValueError()
                result["input"] = args["input"]
            else:
                result["arguments"] = fn["arguments"]
        except (ValueError, TypeError):
            raise BridgeError("ASU returned invalid tool arguments; no tool was dispatched.", 502) from None
        return result


def content_to_chat(content, role):
    if isinstance(content, str):
        return content
    result = []
    for part in content or []:
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            result.append({"type": "text", "text": part["text"]})
        elif kind == "input_image" and role == "user" and part.get("image_url"):
            result.append({"type": "image_url", "image_url": {
                "url": part["image_url"], "detail": part.get("detail", "auto")}})
        else:
            raise BridgeError(f"Unsupported content: {kind}. Use text or user image URLs/data URLs.")
    if all(p["type"] == "text" for p in result):
        return "\n".join(p["text"] for p in result)
    return result


def collect_tools(request):
    """Codex also declares tools through additional_tools items, not only the tools array."""
    tools = list(request.get("tools", []) or [])
    items = request.get("input", [])
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            tools.extend(item.get("tools", []) or [])
    return tools


def translate(request):
    if request.get("previous_response_id") or request.get("conversation"):
        raise BridgeError("This bridge requires full input history; server-side conversation IDs are unsupported.")
    toolmap = ToolMap(collect_tools(request))
    messages = []
    if request.get("instructions"):
        messages.append({"role": "system", "content": request["instructions"]})
    items = request.get("input", [])
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    for item in items:
        kind = item.get("type", "message")
        if kind == "message":
            role = item["role"]
            if role == "developer":
                role = "system"
            if role not in ("system", "user", "assistant"):
                raise BridgeError("Unsupported message role.")
            messages.append({"role": role, "content": content_to_chat(item.get("content"), role)})
        elif kind in ("function_call", "custom_tool_call"):
            arguments = item.get("arguments", "{}") if kind == "function_call" else dumps({"input": item["input"]})
            call = {"id": item["call_id"], "type": "function", "function": {
                "name": toolmap.wire_name(item), "arguments": arguments}}
            if not messages or messages[-1].get("role") != "assistant" or "tool_calls" not in messages[-1]:
                messages.append({"role": "assistant", "content": None, "tool_calls": []})
            messages[-1]["tool_calls"].append(call)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            output = item.get("output", "")
            if not isinstance(output, str):
                output = dumps(output)
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": output})
        elif kind == "additional_tools":
            continue
        elif kind == "reasoning":
            # Opaque vendor reasoning is not transferable; visible messages and tool results remain.
            continue
        else:
            raise BridgeError(f"Unsupported input item: {kind}. Start a fresh ASU session with a task summary.")
    body = {"model": request["model"], "messages": messages, "stream": True,
            "stream_options": {"include_usage": True},
            "request_source": "override_params", "agentic": False}
    choice = request.get("tool_choice", "auto")
    tools = list(toolmap.tools)
    if tools and choice == "none" and not accepts_tool_choice_none(body["model"]):
        # Bedrock-hosted models reject "none"; dropping the tools says the same thing.
        tools = [] if not any(message.get("tool_calls") for message in messages) else tools
        choice = "auto"
    if tools:
        body["tools"] = tools
        if isinstance(choice, dict):
            if choice.get("type") not in ("function", "custom"):
                raise BridgeError("Unsupported tool_choice.")
            choice = ({"type": "function", "function": {"name": toolmap.wire_name(choice)}}
                      if accepts_forced_tool(body["model"]) else "auto")
        body["tool_choice"] = choice
    for key in ("temperature", "top_p", "parallel_tool_calls"):
        if key in request:
            body[key] = request[key]
    if request.get("max_output_tokens"):
        body["max_tokens"] = request["max_output_tokens"]
    # Reasoning effort is opt-in at the launcher; some ASU models reject it.
    if request.get("reasoning", {}).get("effort"):
        body["reasoning_effort"] = request["reasoning"]["effort"]
    return body, toolmap


def response_events(upstream, request, stats=None):
    body, toolmap = translate(request)
    result = {"id": "resp_" + secrets.token_hex(12), "object": "response",
              "created_at": int(time.time()), "status": "in_progress", "model": body["model"],
              "output": [], "error": None, "incomplete_details": None}
    sequence = 0

    def event(kind, **fields):
        nonlocal sequence
        value = {"type": kind, "sequence_number": sequence, **fields}
        sequence += 1
        return value

    with upstream.open("/chat/completions", body) as stream:
        yield event("response.created", response=dict(result))
        text_item = None
        text = ""
        calls = {}
        finish = None
        done = False
        usage = {}
        metric = {}
        for raw in sse_data(stream):
            if raw == "[DONE]":
                done = True
                break
            chunk = json.loads(raw)
            if chunk.get("error"):
                raise BridgeError("ASU returned a streaming error.", 502)
            usage = chunk.get("usage") or usage
            metric = (chunk.get("metadata") or {}).get("usage_metric") or metric
            for choice in chunk.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta", {})
                piece = delta.get("content") or delta.get("refusal") or ""
                if piece:
                    if text_item is None:
                        text_item = {"id": "msg_" + secrets.token_hex(12), "type": "message",
                                     "role": "assistant", "status": "in_progress", "content": []}
                        yield event("response.output_item.added", output_index=0, item=dict(text_item))
                        yield event("response.content_part.added", item_id=text_item["id"], output_index=0,
                                    content_index=0, part={"type": "output_text", "text": "", "annotations": []})
                    text += piece
                    yield event("response.output_text.delta", item_id=text_item["id"], output_index=0,
                                content_index=0, delta=piece)
                for part in delta.get("tool_calls", []):
                    call = calls.setdefault(part["index"], {"id": "", "function": {"name": "", "arguments": ""}})
                    if part.get("id"):
                        call["id"] += part["id"]
                    for key in ("name", "arguments"):
                        call["function"][key] += part.get("function", {}).get(key) or ""
        if not done or finish not in ("stop", "tool_calls", "length", "content_filter"):
            raise BridgeError("ASU stream ended without a valid completion; no tool was dispatched.", 502)
        if finish in ("length", "content_filter"):
            raise BridgeError("ASU response was truncated or filtered; no tool was dispatched. Shorten the task.", 502)
        # Validate every call before dispatching any of them.
        outputs = [toolmap.output(calls[i]) for i in sorted(calls)]
        if any(not c["call_id"] for c in outputs):
            raise BridgeError("ASU returned a tool call without an ID.", 502)
        if text_item:
            part = {"type": "output_text", "text": text, "annotations": []}
            text_item.update(status="completed", content=[part])
            result["output"].append(text_item)
            yield event("response.output_text.done", item_id=text_item["id"], output_index=0,
                        content_index=0, text=text)
            yield event("response.content_part.done", item_id=text_item["id"], output_index=0,
                        content_index=0, part=part)
            yield event("response.output_item.done", output_index=0, item=text_item)
        for item in outputs:
            index = len(result["output"])
            result["output"].append(item)
            yield event("response.output_item.added", output_index=index, item={**item, "status": "in_progress"})
            yield event("response.output_item.done", output_index=index, item=item)
        if not result["output"]:
            raise BridgeError("ASU returned no text or tool calls.", 502)
        result["status"] = "completed"
        result["usage"] = {"input_tokens": usage.get("prompt_tokens", 0),
                           "output_tokens": usage.get("completion_tokens", 0),
                           "total_tokens": usage.get("total_tokens", 0),
                           "input_tokens_details": {"cached_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)},
                           "output_tokens_details": {"reasoning_tokens": usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)}}
        if stats is not None:
            stats.update({"input_tokens": result["usage"]["input_tokens"],
                          "output_tokens": result["usage"]["output_tokens"],
                          "cost": metric.get("total_token_cost")})
        yield event("response.completed", response=result)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream, token, port=0):
        self.upstream = upstream
        self.token = token
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server_port}/v1"

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def events(self, request, headers):
        return response_events(self.upstream, request)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def authorized(self):
        if getattr(self.server, "accept_any_bearer", False):
            return (not self.headers.get("Origin") and
                    self.headers.get("Authorization", "").startswith("Bearer "))
        if getattr(self.server, "uses_primary_auth", False):
            return (not self.headers.get("Origin") and
                    self.headers.get("Authorization", "").startswith("Bearer ") and
                    hmac.compare_digest(self.headers.get("ASU-Bridge-Key", ""), self.server.token))
        return (not self.headers.get("Origin") and
                hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.token))

    def json_response(self, status, value):
        body = dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if not self.authorized():
                return self.json_response(401, {"error": {"message": "Local bridge authentication required."}})
            if self.path == "/health":
                return self.json_response(200, {"status": "ok"})
            proxy = getattr(self.server, "proxy_get", None)
            if proxy:
                try:
                    status, body = proxy(self.path, self.headers)
                except BridgeError as exc:
                    return self.json_response(exc.status, {"error": {"message": str(exc)}})
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            self.json_response(404, {"error": {"message": "Unsupported endpoint."}})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        streaming = False
        try:
            if not self.authorized():
                raise BridgeError("Local bridge authentication required.", 401)
            if self.path != "/v1/responses":
                raise BridgeError("Unsupported endpoint; remote compaction/hosted tools are not available.", 404)
            if self.headers.get("Content-Encoding", "identity") != "identity":
                raise BridgeError("Disable Codex request compression for this provider.")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 20 * 1024 * 1024:
                raise BridgeError("Invalid or oversized request.", 413)
            request = json.loads(self.rfile.read(length))
            events = self.server.events(request, self.headers)
            first = next(events)  # Validate/open upstream before returning HTTP 200.
            if request.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                streaming = True
                self.send_event(first)
                for event in events:
                    self.send_event(event)
            else:
                last = first
                for last in events:
                    pass
                self.json_response(200, last["response"])
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            message = str(exc) if isinstance(exc, BridgeError) else "Invalid request or upstream response."
            if streaming:
                try:
                    self.send_event({"type": "error", "code": "bridge_error", "message": message})
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.json_response(getattr(exc, "status", 502), {"error": {"message": message, "type": "bridge_error"}})

    def send_event(self, event):
        self.wfile.write(("event: " + event["type"] + "\ndata: " + dumps(event) + "\n\n").encode())
        self.wfile.flush()
