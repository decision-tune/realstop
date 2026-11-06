from __future__ import annotations

"""Optional local tool-outcome advisory; no policy or continuation authority.

A caller may explicitly supply a reviewed Verdict-compatible instance. This module
never imports Verdict, downloads weights, or changes deterministic policy.
"""
from dataclasses import dataclass
import math
from typing import Protocol

LABELS = ("success", "failure", "blocked", "unknown")
MAX_COMMAND_CHARS = 1024
MAX_STREAM_CHARS = 12288


class Predictor(Protocol):
    def predict(self, command: str, exit_code: int, stdout: str = "", stderr: str = "") -> dict:
        ...


@dataclass(frozen=True)
class Advisory:
    label: str = "unknown"
    probabilities: tuple[tuple[str, float], ...] = ()
    confidence: float | None = None
    confidence_validated: bool = False
    truncated: bool = False
    status: str = "disabled"
    advisory: bool = True


def observe_tool(*, command, exit_code, stdout="", stderr="", enabled=False,
                 predictor: Predictor | None = None, already_truncated=False):
    """Return advisory evidence only; oversized/clipped observations stay unknown.

    Character bounds match the pinned model's output-stream limit. Command is
    separately bounded because upstream does not clip it. No omitted middle may
    silently become a success. Exceptions expose only a stable diagnostic code.
    """
    if type(enabled) is not bool or type(already_truncated) is not bool:
        return Advisory(status="invalid_input")
    if not enabled:
        return Advisory()
    if (not all(isinstance(v, str) for v in (command, stdout, stderr)) or
        type(exit_code) is not int):
        return Advisory(status="invalid_input")
    truncated = (already_truncated or len(command) > MAX_COMMAND_CHARS or
                 len(stdout) > MAX_STREAM_CHARS or len(stderr) > MAX_STREAM_CHARS)
    if truncated:
        return Advisory(truncated=True, status="truncated_input")
    if predictor is None:
        return Advisory(status="unavailable")
    try:
        result = predictor.predict(command=command, exit_code=exit_code,
                                   stdout=stdout, stderr=stderr)
        if not isinstance(result, dict) or result.get("label") not in LABELS:
            return Advisory(status="invalid_response")
        probabilities = result.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(LABELS):
            return Advisory(status="invalid_response")
        values = [probabilities[k] for k in LABELS]
        confidence = result.get("confidence")
        if (not all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1
                    for v in values + [confidence]) or
            abs(sum(values) - 1) > 0.0001 or
            abs(confidence - probabilities[result["label"]]) > 0.00001 or
            probabilities[result["label"]] != max(values)):
            return Advisory(status="invalid_response")
        return Advisory(label=result["label"],
                        probabilities=tuple((k, float(probabilities[k])) for k in LABELS),
                        confidence=float(confidence), status="ok")
    except Exception:
        return Advisory(status="backend_error")
