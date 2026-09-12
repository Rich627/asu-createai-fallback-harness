"""Anthropic Messages -> CreateAI Chat Completions adapter.

Python standard library only. Credentials and request bodies are never logged.
This is a compatibility subset, not an implementation of the whole Messages API.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets

from createai import BridgeError, dumps, sse_data
from model_map import accepts_forced_tool, accepts_tool_choice_none

NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Used when a requested model has no CreateAI counterpart.
DEFAULT_MODEL = "aws/claude5_opus"


def wire_name(name):
    # CreateAI follows the OpenAI 64-character function-name rule; Anthropic allows 128.
    return name if NAME_OK.match(name) else "t_" + hashlib.sha256(name.encode()).hexdigest()[:24]


class ToolMap:
    def __init__(self, tools):
        self.by_wire = {}
        self.by_name = {}
        self.tools = []
        for tool in tools or []:
            name = tool.get("name")
            schema = tool.get("input_schema")
            if not name or not isinstance(schema, dict):
                # Hosted/server-side tools cannot run on the fallback provider.
                continue
            wire = wire_name(name)
            if wire in self.by_wire:
                raise BridgeError("Duplicate tool name in this request.")
            self.by_wire[wire] = name
            self.by_name[name] = wire
            parameters = {key: value for key, value in schema.items() if key not in ("$schema", "title")}
            parameters.setdefault("type", "object")
            self.tools.append({"type": "function", "function": {
                "name": wire, "description": tool.get("description", "")[:4096], "parameters": parameters}})

    def original(self, wire):
        if wire not in self.by_wire:
            raise BridgeError("The fallback provider returned an unknown tool name.", 502)
        return self.by_wire[wire]

    def wire(self, name):
        return self.by_name.get(name, wire_name(name))


def system_text(system):
    if isinstance(system, str):
        return system
    parts = [block.get("text", "") for block in system or [] if block.get("type") == "text"]
    return "\n\n".join(part for part in parts if part)


def image_part(source):
    kind = (source or {}).get("type")
    if kind == "base64":
        return {"type": "image_url", "image_url": {
            "url": f"data:{source['media_type']};base64,{source['data']}"}}
    if kind == "url":
        return {"type": "image_url", "image_url": {"url": source["url"]}}
    raise BridgeError("Unsupported image source; uploaded file IDs are not available on the fallback provider.")


def result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        text = content
    else:
        parts = []
        for part in content or []:
            if part.get("type") == "text":
                parts.append(part.get("text", ""))
            elif part.get("type") == "image":
                parts.append("[image result omitted by the fallback provider]")
        text = "\n".join(parts)
    if block.get("is_error") and text:
        text = "Error: " + text
    return text or "(empty tool result)"


def flatten(parts):
    if all(part["type"] == "text" for part in parts):
        return "\n".join(part["text"] for part in parts)
    return parts


def translate(request, model):
    toolmap = ToolMap(request.get("tools"))
    messages = []
    system = system_text(request.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    history_tools = []
    for item in request.get("messages", []):
        role = item.get("role")
        # Claude Code's mid-conversation system reminders arrive as system messages.
        if role not in ("user", "assistant", "system"):
            raise BridgeError(f"Unsupported message role: {role}.")
        content = item.get("content")
        if isinstance(content, str):
            if content:
                messages.append({"role": role, "content": content})
            continue
        parts, calls, results = [], [], []
        for block in content or []:
            kind = block.get("type")
            if kind == "text":
                if block.get("text"):
                    parts.append({"type": "text", "text": block["text"]})
            elif kind == "image":
                parts.append(image_part(block.get("source")))
            elif kind == "tool_use":
                wire = toolmap.wire(block["name"])
                history_tools.append(wire)
                calls.append({"id": block["id"], "type": "function", "function": {
                    "name": wire, "arguments": dumps(block.get("input", {}))}})
            elif kind == "tool_result":
                results.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                                "content": result_text(block)})
            elif kind in ("thinking", "redacted_thinking", "server_tool_use", "web_search_tool_result",
                          "mcp_tool_use", "mcp_tool_result", "container_upload", "code_execution_tool_result"):
                # Opaque or provider-hosted blocks are not transferable; text and tool results remain.
                continue
            elif kind == "document":
                raise BridgeError("Document blocks are not supported by the fallback provider.")
            else:
                raise BridgeError(f"Unsupported content block: {kind}.")
        # Tool results answer the previous assistant turn and must precede new user text.
        messages.extend(results)
        if role == "assistant":
            if parts or calls:
                message = {"role": "assistant", "content": flatten(parts) if parts else None}
                if calls:
                    message["tool_calls"] = calls
                messages.append(message)
        elif parts:
            messages.append({"role": role, "content": flatten(parts)})
    if not messages or all(message["role"] == "system" for message in messages):
        raise BridgeError("The request contains no convertible messages.")
    body = {"model": model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True},
            "request_source": "override_params", "agentic": False}
    choice = (request.get("tool_choice") or {}).get("type")
    tools = list(toolmap.tools)
    declared = {tool["function"]["name"] for tool in tools}
    for wire in history_tools:
        # CreateAI rejects a conversation that replays tool calls without their definitions.
        if wire not in declared:
            declared.add(wire)
            tools.append({"type": "function", "function": {
                "name": wire, "description": "Tool used earlier in this conversation.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": True}}})
    if choice == "none" and not history_tools:
        tools = []
    if tools:
        body["tools"] = tools
        if choice == "tool" and request["tool_choice"].get("name") and accepts_forced_tool(model):
            body["tool_choice"] = {"type": "function", "function": {
                "name": toolmap.wire(request["tool_choice"]["name"])}}
        elif choice == "any":
            body["tool_choice"] = "required"
        elif choice == "none" and accepts_tool_choice_none(model):
            body["tool_choice"] = "none"
        else:
            # Neither model family accepts every form; auto is the one both take.
            body["tool_choice"] = "auto"
    if request.get("max_tokens"):
        body["max_tokens"] = request["max_tokens"]
    for key in ("temperature", "top_p"):
        if key in request:
            body[key] = request[key]
    if request.get("stop_sequences"):
        body["stop"] = request["stop_sequences"]
    return body, toolmap


def message_events(upstream, request, model, out=None, stats=None):
    """Yield Anthropic streaming events for one CreateAI chat completion."""
    body, toolmap = translate(request, model)
    message = {"id": "msg_" + secrets.token_hex(12), "type": "message", "role": "assistant",
               "model": request.get("model", model), "content": [], "stop_reason": None,
               "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
    with upstream.open("/chat/completions", body) as stream:
        yield {"type": "message_start", "message": dict(message, content=[])}
        text = ""
        open_text = False
        calls = {}
        finish = None
        usage = {}
        metric = {}
        done = False
        for raw in sse_data(stream):
            if raw == "[DONE]":
                done = True
                break
            chunk = json.loads(raw)
            if chunk.get("error"):
                raise BridgeError("The fallback provider returned a streaming error.", 502)
            usage = chunk.get("usage") or usage
            metric = (chunk.get("metadata") or {}).get("usage_metric") or metric
            for choice in chunk.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta", {}) or {}
                piece = delta.get("content") or delta.get("refusal") or ""
                if isinstance(piece, list):
                    piece = "".join(part.get("text", "") for part in piece if isinstance(part, dict))
                if piece:
                    if not open_text:
                        open_text = True
                        yield {"type": "content_block_start", "index": 0,
                               "content_block": {"type": "text", "text": ""}}
                    text += piece
                    yield {"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": piece}}
                for part in delta.get("tool_calls", []) or []:
                    call = calls.setdefault(part.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    identifier = part.get("id") or ""
                    if identifier and identifier != call["id"]:
                        # Some providers repeat the full id in every delta instead of chunking it.
                        call["id"] += identifier
                    function = part.get("function") or {}
                    call["name"] += function.get("name") or ""
                    call["arguments"] += function.get("arguments") or ""
        if not done:
            raise BridgeError("The fallback provider's stream ended early; no tool was dispatched.", 502)
        if finish in ("length", "content_filter") and calls:
            raise BridgeError("The fallback response was truncated or filtered; no tool was dispatched.", 502)
        if open_text:
            yield {"type": "content_block_stop", "index": 0}
            message["content"].append({"type": "text", "text": text})
        index = 1 if open_text else 0
        # Validate every tool call before any of them is handed to the client.
        blocks = []
        for key in sorted(calls):
            call = calls[key]
            try:
                arguments = json.loads(call["arguments"] or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                raise BridgeError("The fallback provider returned invalid tool arguments; "
                                  "no tool was dispatched.", 502) from None
            blocks.append({"type": "tool_use", "id": call["id"] or "toolu_" + secrets.token_hex(12),
                           "name": toolmap.original(call["name"]), "input": arguments})
        for block in blocks:
            yield {"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}
            yield {"type": "content_block_delta", "index": index,
                   "delta": {"type": "input_json_delta", "partial_json": dumps(block["input"])}}
            yield {"type": "content_block_stop", "index": index}
            message["content"].append(block)
            index += 1
        if not message["content"]:
            raise BridgeError("The fallback provider returned no text or tool calls; "
                              "its reasoning may have used the whole max_tokens budget.", 502)
        message["stop_reason"] = "tool_use" if blocks else ("max_tokens" if finish == "length" else "end_turn")
        message["usage"] = {"input_tokens": usage.get("prompt_tokens", 0),
                            "output_tokens": usage.get("completion_tokens", 0),
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)}
        yield {"type": "message_delta",
               "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
               "usage": message["usage"]}
        yield {"type": "message_stop"}
    if out is not None:
        out.append(message)
    if stats is not None:
        stats.update({"input_tokens": message["usage"]["input_tokens"],
                      "output_tokens": message["usage"]["output_tokens"],
                      "cost": metric.get("total_token_cost")})
