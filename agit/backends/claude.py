from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agit.backends.base import AgentResult, TokenUsage


class ClaudeBackend:
    name = "claude"

    def __init__(self, repo: Path, *, verbose: bool = False) -> None:
        self.repo = repo
        self.verbose = verbose

    def run(self, prompt: str, *, model: str | None, session_id: str | None) -> AgentResult:
        command = ["claude", "-p", prompt, "--output-format", "json"]
        if model:
            command.extend(["--model", model])
        if session_id:
            command.extend(["--resume", session_id])

        process = subprocess.run(
            command,
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if self.verbose and process.stderr.strip():
            print(process.stderr.rstrip())

        final_response, parsed_session_id, parsed_model, tokens = self._parse_output(process.stdout)
        return AgentResult(
            backend=self.name,
            session_id=parsed_session_id or session_id,
            model=parsed_model or model,
            final_response=final_response.strip(),
            exit_code=process.returncode,
            tokens=tokens,
        )

    def _parse_output(self, output: str) -> tuple[str, str | None, str | None, TokenUsage]:
        data = self._load_result(output)
        if data is None:
            return output.strip(), None, None, TokenUsage()
        result = data.get("result")
        final_response = result if isinstance(result, str) else ""
        session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else None
        return final_response, session_id, self._model(data), self._tokens(data.get("usage"))

    def _load_result(self, output: str) -> dict | None:
        # --output-format json prints a single JSON object; tolerate leading logs
        # by scanning lines and, as a fallback, the outermost object.
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("type") == "result":
                return data
        start = output.find("{")
        end = output.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(output[start : end + 1])
            except json.JSONDecodeError:
                return None
            if isinstance(data, dict):
                return data
        return None

    def _model(self, data: dict) -> str | None:
        # An explicit top-level model field, when present, is authoritative.
        if isinstance(data.get("model"), str) and data["model"]:
            return data["model"]
        # `modelUsage` can list several models for one invocation (a Haiku
        # sub-agent or background model alongside the main one) in arbitrary
        # order; the metadata (and the --model passed on later runs) must name
        # the MAIN conversation model — the one that produced the most output,
        # with overall token volume as the tie-breaker.
        model_usage = data.get("modelUsage")
        if not isinstance(model_usage, dict) or not model_usage:
            return None

        def weight(item: tuple[str, object]) -> tuple[int, int]:
            usage = item[1]
            if not isinstance(usage, dict):
                return (0, 0)
            output = self._int(usage.get("outputTokens"))
            total = sum(self._int(value) for value in usage.values())
            return (output, total)

        return max(model_usage.items(), key=weight)[0]

    def _tokens(self, usage: object) -> TokenUsage:
        if not isinstance(usage, dict):
            return TokenUsage()
        input_tokens = self._int(usage.get("input_tokens"))
        output_tokens = self._int(usage.get("output_tokens"))
        cache_read = self._int(usage.get("cache_read_input_tokens"))
        cache_write = self._int(usage.get("cache_creation_input_tokens"))
        return TokenUsage(
            context=(input_tokens + cache_read + cache_write) or None,
            total=output_tokens,
            input=input_tokens,
            output=output_tokens,
            reasoning=0,
            cache_read=cache_read,
            cache_write=cache_write,
        )

    def _int(self, value: object) -> int:
        return value if isinstance(value, int) else 0
