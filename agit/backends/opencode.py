from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import IO

from agit.backends.base import AgentResult, TokenUsage


class OpenCodeBackend:
    name = "opencode"

    def __init__(self, repo: Path, *, verbose: bool = False, backend_args: list[str] | None = None) -> None:
        self.repo = repo
        self.verbose = verbose
        self.backend_args = list(backend_args or [])  # forwarded verbatim to the backend CLI (#32)

    def run(self, prompt: str, *, model: str | None, session_id: str | None) -> AgentResult:
        command = ["opencode", "run", "--format", "json", "--dir", str(self.repo)]
        if model:
            command.extend(["--model", model])
        if session_id:
            command.extend(["--session", session_id])
        # Passthrough options go before the prompt positional (#32).
        command.extend(self.backend_args)
        if prompt.startswith("/"):
            slash_command, args = self._split_slash_command(prompt)
            if slash_command:
                command.extend(["--command", slash_command])
                command.extend(args)
            else:
                command.append(prompt)
        else:
            command.append(prompt)

        process = subprocess.Popen(
            command,
            cwd=self.repo,
            text=True,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
        )
        final_response, parsed_session_id, parsed_model, tokens = self._read_events(process.stdout)
        exit_code = process.wait()

        return AgentResult(
            backend=self.name,
            session_id=parsed_session_id or session_id,
            model=parsed_model or model,
            final_response=final_response.strip(),
            exit_code=exit_code,
            tokens=tokens,
        )

    def _read_events(self, output: IO[str] | None) -> tuple[str, str | None, str | None, TokenUsage]:
        if output is None:
            return "", None, None, TokenUsage()

        final_parts: list[str] = []
        session_id = None
        model = None
        tokens = TokenUsage()
        for line in output:
            parsed = self._parse_event_line(line)
            if parsed is None:
                if self.verbose and line.strip():
                    print(line.rstrip())
                continue

            display_text, final_text, parsed_session_id, parsed_model, parsed_tokens = parsed
            session_id = session_id or parsed_session_id
            model = model or parsed_model
            self._add_tokens(tokens, parsed_tokens)
            if display_text:
                print(display_text, end="" if display_text.endswith("\n") else "\n")
            if final_text:
                final_parts.append(final_text)

        return "".join(final_parts).strip(), session_id, model, tokens

    def _split_slash_command(self, prompt: str) -> tuple[str | None, list[str]]:
        parts = prompt[1:].strip().split()
        if not parts:
            return None, []
        return parts[0], parts[1:]

    def _parse_event_line(self, line: str) -> tuple[str | None, str | None, str | None, str | None, TokenUsage] | None:
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None

        # Debug: print event structure
        if self.verbose:
            event_type = event.get("type", "")
            part = event.get("part", {})
            part_type = part.get("type", "") if isinstance(part, dict) else ""
            text = self._event_text(event)
            if text or event_type:
                print(f"  [DEBUG] event_type={event_type}, part_type={part_type}, has_text={bool(text)}")

        session_id = self._find_value(event, {"sessionID", "sessionId", "session_id"})
        model = self._event_model(event)
        tokens = self._event_tokens(event)
        event_type = str(event.get("type", "")).lower()
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        part_type = str(part.get("type", "")).lower()

        if "thinking" in event_type or "thinking" in part_type:
            return None

        text = self._event_text(event)
        if text:
            is_final = self._is_final_text(event, part)
            if self.verbose and text:
                print(f"  [DEBUG] is_final={is_final}, text_preview={text[:50]}...")
            return text, text if is_final else None, session_id, model, tokens

        status = self._event_status(event, part)
        return status, None, session_id, model, tokens

    def _event_text(self, event: dict) -> str | None:
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text
            for key in ("content", "message", "response"):
                value = part.get(key)
                if isinstance(value, str) and value.strip():
                    return value

        for key in ("text", "content", "message", "response"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value
        data = event.get("data")
        if isinstance(data, dict):
            for key in ("text", "content", "message", "response"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return None

    def _is_final_text(self, event: dict, part: dict) -> bool:
        metadata = part.get("metadata")
        if isinstance(metadata, dict) and self._find_value(metadata, {"phase"}) == "final_answer":
            return True
        event_type = str(event.get("type", "")).lower()
        part_type = str(part.get("type", "")).lower()
        # opencode sends final responses as "text" type events
        return event_type in {"final", "complete", "done", "text"} or part_type in {"final", "complete", "done", "text"}

    def _event_status(self, event: dict, part: dict) -> str | None:
        event_type = str(event.get("type", "")).lower()
        part_type = str(part.get("type", "")).lower()
        if event_type == "tool" or "tool" in part_type:
            tool = part.get("tool") or part.get("name") or event.get("tool") or event.get("name")
            if isinstance(tool, str) and tool:
                return f"[{tool}]"
        return None

    def _event_tokens(self, event: dict) -> TokenUsage:
        part = event.get("part")
        tokens = part.get("tokens") if isinstance(part, dict) else event.get("tokens")
        if not isinstance(tokens, dict):
            return TokenUsage()

        cache = tokens.get("cache")
        if not isinstance(cache, dict):
            cache = {}
        input_tokens = self._int_value(tokens.get("input"))
        output_tokens = self._int_value(tokens.get("output"))
        reasoning_tokens = self._int_value(tokens.get("reasoning"))
        return TokenUsage(
            context=input_tokens or None,
            total=output_tokens + reasoning_tokens,
            input=input_tokens,
            output=output_tokens,
            reasoning=reasoning_tokens,
            cache_read=self._int_value(cache.get("read")),
            cache_write=self._int_value(cache.get("write")),
        )

    def _add_tokens(self, current: TokenUsage, addition: TokenUsage) -> None:
        current.add(addition)

    def _int_value(self, value: object) -> int:
        return value if isinstance(value, int) else 0

    def _event_model(self, event: dict) -> str | None:
        model = event.get("model")
        if isinstance(model, dict):
            provider = model.get("providerID") or model.get("provider")
            model_id = model.get("modelID") or model.get("id")
            if provider and model_id:
                return f"{provider}/{model_id}"
            return str(model_id) if model_id else None
        if isinstance(model, str) and model.strip():
            return model.strip()
        provider = self._find_value(event, {"providerID", "provider"})
        model_id = self._find_value(event, {"modelID"})
        if provider and model_id:
            return f"{provider}/{model_id}"
        return model_id

    def _find_value(self, value: object, keys: set[str]) -> str | None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, str) and item.strip():
                    return item.strip()
                found = self._find_value(item, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_value(item, keys)
                if found:
                    return found
        return None
