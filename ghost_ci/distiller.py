"""4070 Ti Ollama distiller. Compresses pytest traceback tails into a
JSON summary via llama3.1:8b-instruct-q8_0. Falls back gracefully on
VRAM eviction, timeout, or garbage output.
"""
from __future__ import annotations

import asyncio
import json

import aiohttp

OLLAMA_BASE = "http://localhost:11535"
OLLAMA_GENERATE_URL = f"{OLLAMA_BASE}/api/generate"
OLLAMA_PS_URL = f"{OLLAMA_BASE}/api/ps"
OLLAMA_MODEL = "llama3.1:8b-instruct-q8_0"
DISTILLATION_TIMEOUT = 3.0
VRAM_CHECK_TIMEOUT = 1.0


def generate_distill_payload(traceback_tail: str) -> str:
    """Build JSON payload safely. f-string + json.dumps — immune to %-chars."""
    raw_prompt = (
        "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
        "You are an error distillation pipeline for an autonomous AI.\n"
        "Your sole job is to extract the root cause of the test failure.\n"
        "DO NOT suggest fixes. DO NOT write code. Output ONLY valid JSON:\n"
        "{\n"
        '  "summary": "Concise 2-sentence explanation of what broke",\n'
        '  "file": "Main file causing the error",\n'
        '  "line_number": <integer or null>,\n'
        '  "exception_type": "Name of the exception (e.g. ValueError)"\n'
        "}\n"
        "<|eot_id|><|start_header_id|>user<|end_header_id|>\n"
        "Extract the root cause from this traceback tail:\n\n"
        f"{traceback_tail}\n"
        "<|eot_id|><|start_header_id|>assistant<|end_header_id|>"
    )
    return json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": raw_prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": 150},
    })


async def is_model_resident() -> bool:
    """Pre-flight VRAM check before distillation."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                OLLAMA_PS_URL,
                timeout=aiohttp.ClientTimeout(total=VRAM_CHECK_TIMEOUT),
            ) as resp:
                data = await resp.json()
                resident = [m.get("name", "") for m in data.get("models", [])]
                return any(name.startswith("llama3.1:8b") for name in resident)
    except Exception:
        return False


async def distill_error(traceback_tail: str) -> dict | None:
    """Returns parsed dict or None on any failure."""
    try:
        payload = generate_distill_payload(traceback_tail)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OLLAMA_GENERATE_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=DISTILLATION_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return json.loads(data["response"])
    except (asyncio.TimeoutError, json.JSONDecodeError, aiohttp.ClientError, KeyError):
        return None


def filter_relevant_tracebacks(pytest_output: str, modified_file: str) -> str | None:
    """Surface error only if the modified file appears in the traceback."""
    if not modified_file:
        return pytest_output
    norm_a = modified_file.replace("\\", "/")
    norm_b = modified_file.replace("/", "\\")
    if (modified_file in pytest_output
            or norm_a in pytest_output
            or norm_b in pytest_output):
        return pytest_output
    return None


def is_syntax_error_output(pytest_output: str, exit_code: int) -> bool:
    """Trap 1: cc mid-edit produces SyntaxError. Swallow silently."""
    if exit_code != 2:
        return False
    return ("SyntaxError" in pytest_output) or ("IndentationError" in pytest_output)
