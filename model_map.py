"""Map a client's model id onto the closest CreateAI model.

The client keeps choosing models its own way (Claude Code's /model, Codex's
/model); this resolves each choice to the matching CreateAI id when one exists,
so a fallback turn stays on the tier the user picked.
"""

from __future__ import annotations

import re
import threading
import time

AUTO = "auto"
CLAUDE_FAMILIES = ("opus", "sonnet", "haiku")
# Used when CreateAI's model list cannot be read; refreshed from /models when it can.
KNOWN_MODELS = (
    "aws/claude4_sonnet", "aws/claude4_1_opus", "aws/claude4_5_haiku", "aws/claude4_5_opus",
    "aws/claude4_5_sonnet", "aws/claude4_6_opus", "aws/claude4_7_opus", "aws/claude4_8_opus",
    "aws/claude5_opus", "aws/claude5_sonnet",
    "openai/gpt4_1", "openai/gpt4o", "openai/gpt5", "openai/gpt5_mini", "openai/gpt5_nano",
    "openai/gpt5_1", "openai/gpt5_1_instant", "openai/gpt5_1_thinking", "openai/gpt5_2",
    "openai/gpt5_2_pro", "openai/gpt5_4_mini", "openai/gpt5_4_nano", "openai/gpt5_4_pro",
    "openai/gpt5_4_thinking", "openai/gpt5_5", "openai/gpt5_5_pro", "openai/gpt5_5_thinking",
    "openai/gpt5_6_luna", "openai/gpt5_6_sol", "openai/gpt5_6_terra", "openai/gpt6_astra",
)


def accepts_forced_tool(model):
    """Measured 3/3 each way: Bedrock-hosted Claude models accept a forced single tool and
    reject tool_choice "none"; CreateAI's OpenAI-hosted models do the opposite."""
    return not str(model or "").startswith("openai/")


def accepts_tool_choice_none(model):
    return str(model or "").startswith("openai/")


def version_key(model):
    numbers = re.findall(r"\d+", model)
    return tuple(int(number) for number in numbers) or (0,)


def claude_candidates(requested):
    """claude-opus-5 -> claude5_opus; claude-haiku-4-5-20251001 -> claude4_5_haiku."""
    text = (requested or "").lower()
    family = next((name for name in CLAUDE_FAMILIES if name in text), None)
    if not family:
        return None, []
    after = text.split(family, 1)[1]
    before = text.split(family, 1)[0]
    groups = [part for part in re.findall(r"\d+", after) if len(part) < 5]
    if not groups:
        groups = [part for part in re.findall(r"\d+", before) if len(part) < 5]
    versions = []
    if groups:
        versions.append("_".join(groups[:2]))
        versions.append(groups[0])
    return family, [f"aws/claude{version}_{family}" for version in dict.fromkeys(versions)]


def openai_candidates(requested):
    """gpt-5.6-sol -> gpt5_6_sol, then gpt5_6; gpt-6-astra -> gpt6_astra, then gpt6."""
    text = (requested or "").lower()
    match = re.match(r"(?:openai/)?gpt-?([\d.]+)(.*)$", text)
    if not match:
        return None, []
    version = match.group(1).strip(".").replace(".", "_")
    suffixes = [part for part in re.split(r"[-_]", match.group(2)) if part and not part.isdigit()]
    names = []
    if suffixes:
        names.append(f"openai/gpt{version}_{'_'.join(suffixes)}")
        names.append(f"openai/gpt{version}_{suffixes[0]}")
    names.append(f"openai/gpt{version}")
    return "gpt", list(dict.fromkeys(names))


def resolve(requested, available, default):
    """Exact family+version match first, then the newest model of the same family."""
    available = set(available or KNOWN_MODELS)
    for finder in (claude_candidates, openai_candidates):
        family, candidates = finder(requested)
        if not family:
            continue
        for candidate in candidates:
            if candidate in available:
                return candidate
        prefix = "aws/claude" if family in CLAUDE_FAMILIES else "openai/gpt"
        same_family = [name for name in available
                       if name.startswith(prefix) and (family == "gpt" or name.endswith("_" + family))]
        if same_family:
            return max(same_family, key=version_key)
        break
    return default


class Resolver:
    """Caches CreateAI's model list so mapping follows new models without a code change."""

    def __init__(self, source, default, ttl=3600):
        self.source = source
        self.default = default
        self.ttl = ttl
        self.lock = threading.Lock()
        self.available = ()
        self.checked = 0.0

    def models(self):
        with self.lock:
            if self.available and time.time() - self.checked < self.ttl:
                return self.available
            try:
                upstream = self.source() if callable(self.source) else self.source
                if upstream is None:
                    return self.available or KNOWN_MODELS
                listing = upstream.models()
                names = tuple(item["id"] for item in listing.get("data", []) if item.get("id"))
                self.available = names or KNOWN_MODELS
            except Exception:
                self.available = self.available or KNOWN_MODELS
            self.checked = time.time()
            return self.available

    def target(self, requested, override=AUTO):
        if override and override != AUTO:
            return override
        return resolve(requested, self.models(), self.default)
