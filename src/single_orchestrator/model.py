from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import os
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Mapping, Protocol

from .prompts import BuiltPrompt
from .goal_api import GoalTransportError, GoalProtocolError

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
else:
    load_dotenv()


class ModelClient(Protocol):
    def complete(self, prompt: BuiltPrompt) -> str:
        """Return the model response for a whitelisted prompt."""


class ModelClientError(RuntimeError):
    pass


@dataclass
class PromptRoutingModelClient:
    default_client: ModelClient
    prompt_clients: Mapping[str, ModelClient]
    strict: bool = False

    def complete(self, prompt: BuiltPrompt) -> str:
        client = self.prompt_clients.get(prompt.name)
        if client is None:
            if self.strict:
                raise ModelClientError(
                    f"No model route configured for prompt {prompt.name!r}."
                )
            client = self.default_client
        from .local_audit import emit
        emit('model_route', prompt_name=prompt.name, model=getattr(client, 'model', None),
             client=type(client).__name__, timeout_seconds=getattr(client, 'timeout_seconds', None))
        return client.complete(prompt)


@dataclass
class CodexCliModelClient:
    cwd: Path
    model: str | None = None
    timeout_seconds: int = 300

    def complete(self, prompt: BuiltPrompt) -> str:
        with tempfile.NamedTemporaryFile("r", encoding="utf-8", delete=True) as output_file:
            return self._complete_with_output_file(prompt, Path(output_file.name))

    def _complete_with_output_file(self, prompt: BuiltPrompt, output_path: Path) -> str:
        command = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--output-last-message",
            str(output_path),
        ]
        if self.model:
            command.extend(["--model", self.model])
        command.append(prompt.text)

        try:
            completed = self._run_codex(command, prompt.name)
        except TimeoutError as exc:
            raise ModelClientError(
                f"codex exec timed out after {self.timeout_seconds} seconds "
                f"for {prompt.name}"
            ) from exc
        if completed.returncode != 0:
            detail = self._compact_codex_error(completed.stderr, completed.stdout)
            raise ModelClientError(f"codex exec failed for {prompt.name}: {detail}")

        final_message = output_path.read_text(encoding="utf-8").strip()
        if final_message:
            return final_message
        return completed.stdout.strip()

    def _run_codex(self, command: list[str], prompt_name: str) -> subprocess.CompletedProcess[str]:
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as stdout_file:
            with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=self.cwd,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
                deadline = (
                    time.monotonic() + self.timeout_seconds
                    if self.timeout_seconds > 0
                    else None
                )
                while process.poll() is None:
                    if deadline is not None and time.monotonic() >= deadline:
                        self._terminate_process_group(process)
                        raise TimeoutError(
                            f"codex exec exceeded {self.timeout_seconds} seconds "
                            f"for {prompt_name}"
                        )
                    time.sleep(0.5)
                stdout_file.seek(0)
                stderr_file.seek(0)
                return subprocess.CompletedProcess(
                    command,
                    process.returncode,
                    stdout=stdout_file.read(),
                    stderr=stderr_file.read(),
                )

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                process.kill()

    @staticmethod
    def _compact_codex_error(stderr: str, stdout: str, max_lines: int = 12) -> str:
        raw = "\n".join(part for part in (stderr.strip(), stdout.strip()) if part)
        if not raw:
            return "Codex CLI exited with a non-zero status and no output."

        kept: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("ERROR:"):
                kept.append(stripped)
                continue
            if "ran out of room in the model's context window" in stripped:
                kept.append(stripped)
                continue
            if "timed out" in stripped.lower():
                kept.append(stripped)
                continue
            if "usage limit" in stripped.lower():
                kept.append(stripped)
                continue
            if "error sending request" in stripped:
                kept.append(stripped)
                continue
            if "stream disconnected" in stripped:
                kept.append(stripped)
                continue
            if stripped.startswith("codex") and not kept:
                kept.append(stripped)

        if not kept:
            kept = [line.strip() for line in raw.splitlines() if line.strip()][:max_lines]

        deduped: list[str] = []
        for line in kept:
            if line not in deduped:
                deduped.append(line)
        return "\n".join(deduped[:max_lines])


@dataclass
class CodexSdkModelClient:
    model: str | None = None
    timeout_seconds: int = 300

    def complete(self, prompt: BuiltPrompt) -> str:
        try:
            from openai_codex import Codex, Sandbox
        except ImportError as exc:
            raise ModelClientError(
                "openai-codex is not installed. Install the codex-sdk optional dependency "
                "or use CodexCliModelClient."
            ) from exc

        try:
            with _wall_clock_timeout(self.timeout_seconds):
                with Codex() as codex:
                    kwargs = {"sandbox": Sandbox.read_only}
                    if self.model:
                        kwargs["model"] = self.model
                    thread = codex.thread_start(**kwargs)
                    result = thread.run(prompt.text)
        except TimeoutError as exc:
            raise ModelClientError(
                f"codex sdk timed out after {self.timeout_seconds} seconds "
                f"for {prompt.name}"
            ) from exc
        return result.final_response.strip()


@dataclass
class OpenAICompatibleModelClient:
    model: str
    timeout_seconds: int = 300
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"

    def complete(self, prompt: BuiltPrompt) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelClientError(
                "openai is not installed. Install the OpenAI Python package "
                "to use OpenRouter/OpenAI-compatible models."
            ) from exc

        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise ModelClientError(f"{self.api_key_env} is not set.")
        api_key = _ascii_header_value(api_key).strip()

        try:
            request_timeout = self.timeout_seconds if self.timeout_seconds > 0 else None
            completion_options: dict[str, int] = {}
            output_limit = _output_token_limit(prompt)
            if output_limit is not None:
                completion_options['max_tokens'] = output_limit
            if prompt.version in ('v20-bounded-repair', 'v21-guidance-only-repair') and 'retry_planner' in prompt.name:
                repair_timeout = int(os.getenv('DUAL_ARC_REPAIR_TIMEOUT_SECONDS', '180'))
                if repair_timeout <= 0:
                    raise ValueError('Repair request timeout must be positive')
                request_timeout = min(request_timeout, repair_timeout) if request_timeout else repair_timeout
            client = OpenAI(
                api_key=api_key,
                base_url=self.base_url or os.getenv(
                    "OPENAI_BASE_URL", "https://openrouter.ai/api/v1"
                ),
                timeout=request_timeout,
                max_retries=0 if prompt.version.startswith(('v14-', 'v15-', 'v16-', 'v17-', 'v18-', 'v19-', 'v20-', 'v21-')) else 2,
            )
            with _wall_clock_timeout(self.timeout_seconds):
                if prompt.version.startswith(('v14-', 'v15-', 'v16-', 'v17-', 'v18-', 'v19-', 'v20-', 'v21-')):
                    from .goal_api import complete_chat_with_retries, response_format
                    fmt = response_format(prompt)
                    options = {"response_format": fmt} if fmt else {}
                    provider = _openrouter_provider(self.model)
                    if provider and 'openrouter.ai' in str(client.base_url):
                        options['extra_body'] = {'provider': {'only': [provider],
                            'allow_fallbacks': False, 'require_parameters': True}}
                    effort = _model_reasoning_effort(self.model)
                    if effort:
                        if 'openrouter.ai' in str(client.base_url):
                            options.setdefault('extra_body', {})['reasoning'] = {'effort': effort}
                        else:
                            options['reasoning_effort'] = effort
                    from .local_audit import emit
                    import uuid
                    import time
                    def audited_create(**kwargs):
                        request_id = uuid.uuid4().hex
                        started = time.monotonic()
                        # Headers and credentials are deliberately excluded.
                        emit('api_request', request_id=request_id, model=self.model,
                             prompt_name=prompt.name, base_url=str(client.base_url),
                             request={k:v for k,v in kwargs.items() if k != 'extra_headers'})
                        try:
                            response = client.chat.completions.create(**kwargs)
                        except BaseException as exc:
                            emit('api_error', request_id=request_id,
                                 duration_seconds=time.monotonic()-started,
                                 error_type=type(exc).__name__, error=str(exc),
                                 status_code=getattr(exc, 'status_code', None))
                            raise
                        emit('api_response', request_id=request_id,
                             duration_seconds=time.monotonic()-started, response=response)
                        return response
                    raw, _ = complete_chat_with_retries(prompt, audited_create,
                        request_options=dict(model=self.model,
                            extra_headers=self._extra_headers(), **completion_options, **options),
                        max_attempts=int(os.getenv('DUAL_ARC_API_MAX_ATTEMPTS', '3')),
                        max_transport_attempts=int(os.getenv('DUAL_ARC_API_MAX_TRANSPORT_ATTEMPTS', '5')))
                    return raw
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt.text}],
                    extra_headers=self._extra_headers(),
                    **completion_options,
                )
        except (GoalTransportError, GoalProtocolError):
            raise
        except TimeoutError as exc:
            raise GoalTransportError(f"Model request timed out for {prompt.name}: {exc}") from exc
        except Exception as exc:
            raise ModelClientError(
                f"OpenAI-compatible model call failed for {prompt.name}: {exc}"
            ) from exc

        message = response.choices[0].message.content if response.choices else ""
        if isinstance(message, list):
            return "\n".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in message
            ).strip()
        return (message or "").strip()

    @staticmethod
    def _extra_headers() -> dict[str, str]:
        headers: dict[str, str] = {}
        referer = os.getenv("OPENROUTER_HTTP_REFERER")
        title = os.getenv("OPENROUTER_APP_TITLE")
        if referer:
            headers["HTTP-Referer"] = _ascii_header_value(referer)
        if title:
            headers["X-Title"] = _ascii_header_value(title)
        return headers


def _ascii_header_value(value: str) -> str:
    return value.encode("ascii", errors="ignore").decode("ascii")


@contextmanager
def _wall_clock_timeout(timeout_seconds: int):
    if timeout_seconds <= 0:
        yield
        return
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def raise_timeout(signum, frame):
        raise TimeoutError(f"model call exceeded {timeout_seconds} seconds")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def make_default_model_client(
    cwd: Path,
    model: str | None = None,
    timeout_seconds: int = 300,
) -> ModelClient:
    preferred_client = os.getenv("DUAL_ARC_MODEL_CLIENT", "").strip().lower()
    compatible_config = _openai_compatible_config(model)
    if compatible_config is not None:
        compatible_model, base_url, api_key_env = compatible_config
        return OpenAICompatibleModelClient(
            model=compatible_model,
            timeout_seconds=timeout_seconds,
            base_url=base_url,
            api_key_env=api_key_env,
        )
    if preferred_client == "cli":
        return CodexCliModelClient(
            cwd=cwd,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    if preferred_client and preferred_client != "sdk":
        raise ModelClientError(
            "DUAL_ARC_MODEL_CLIENT must be unset, 'sdk', or 'cli'."
        )
    try:
        import openai_codex  # noqa: F401
    except ImportError:
        return CodexCliModelClient(
            cwd=cwd,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    return CodexSdkModelClient(model=model, timeout_seconds=timeout_seconds)


def make_prompt_routing_model_client(
    cwd: Path,
    *,
    default_model: str | None = None,
    timeout_seconds: int = 300,
    prompt_models: Mapping[str, str | None] | None = None,
    strict: bool = False,
) -> ModelClient:
    default_client = make_default_model_client(
        cwd,
        model=default_model,
        timeout_seconds=timeout_seconds,
    )
    prompt_clients: dict[str, ModelClient] = {}
    for prompt_name, model in (prompt_models or {}).items():
        if not model:
            continue
        if model == default_model:
            prompt_clients[prompt_name] = default_client
            continue
        prompt_clients[prompt_name] = make_default_model_client(
            cwd,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    return PromptRoutingModelClient(
        default_client=default_client,
        prompt_clients=prompt_clients,
        strict=strict,
    )


def _uses_openai_compatible_api(model: str | None) -> bool:
    return _openai_compatible_config(model) is not None


def _openai_compatible_config(model: str | None) -> tuple[str, str, str] | None:
    if not model:
        return None
    if model.startswith("openai:"):
        return (
            model.removeprefix("openai:"),
            "https://api.openai.com/v1",
            "OPENAI_API_KEY",
        )
    if model.startswith("openrouter:"):
        return (
            model.removeprefix("openrouter:"),
            os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            "OPENROUTER_API_KEY",
        )
    if "/" in model:
        return (
            model,
            os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            "OPENROUTER_API_KEY",
        )
    return None


def _openrouter_provider(model: str) -> str:
    default = os.getenv('DUAL_ARC_OPENROUTER_PROVIDER', '').strip()
    overrides = json.loads(os.getenv('DUAL_ARC_OPENROUTER_MODEL_PROVIDERS', '{}'))
    if not isinstance(overrides, dict):
        raise ModelClientError('OpenRouter provider overrides must be a JSON object')
    provider = overrides.get(model, default)
    if not isinstance(provider, str):
        raise ModelClientError('OpenRouter provider name must be a string')
    return provider.strip()


def _model_reasoning_effort(model: str) -> str:
    overrides = json.loads(os.getenv('DUAL_ARC_MODEL_REASONING_EFFORTS', '{}'))
    if not isinstance(overrides, dict):
        raise ModelClientError('Model reasoning overrides must be a JSON object')
    effort = overrides.get(model, os.getenv('DUAL_ARC_REASONING_EFFORT', '').strip())
    if not isinstance(effort, str) or effort not in ('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise ModelClientError('Unsupported reasoning effort setting')
    return effort


def _output_token_limit(prompt: BuiltPrompt) -> int | None:
    """Bound current node output even when callers do not load an experiment env."""
    raw = os.getenv('DUAL_ARC_MAX_OUTPUT_TOKENS', '').strip()
    limit = int(raw) if raw else (16384 if prompt.version in ('v20-bounded-repair', 'v21-guidance-only-repair') else None)
    if limit is not None and limit <= 0:
        raise ValueError('Output-token limit must be positive')
    if prompt.version in ('v20-bounded-repair', 'v21-guidance-only-repair') and 'retry_planner' in prompt.name:
        repair_limit = int(os.getenv('DUAL_ARC_REPAIR_MAX_OUTPUT_TOKENS', '2048'))
        if repair_limit <= 0:
            raise ValueError('Repair output-token limit must be positive')
        limit = min(limit, repair_limit) if limit else repair_limit
    return limit
