"""Per-batch LLM cost and time metrics (print-only)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LlmCall:
    agent: str  # investigator | report
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int


@dataclass
class BatchLlmMetrics:
    calls: list[LlmCall] = field(default_factory=list)

    def record(
        self,
        *,
        agent: str,
        model: str,
        latency_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.calls.append(
            LlmCall(
                agent=agent,
                model=model,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )

    def print_summary(self) -> None:
        invest = [c for c in self.calls if c.agent == "investigator"]
        report = [c for c in self.calls if c.agent == "report"]
        n = len(self.calls)
        total_ms = sum(c.latency_ms for c in self.calls)
        total_in = sum(c.input_tokens for c in self.calls)
        total_out = sum(c.output_tokens for c in self.calls)
        models = sorted({c.model for c in self.calls}) or ["-"]
        avg_ms = int(total_ms / n) if n else 0

        print("\n--- LLM metrics ---")
        print(f"model: {', '.join(models)}")
        print(f"investigator calls: {len(invest)}")
        print(f"report calls: {len(report)}")
        print(f"total time: {total_ms / 1000:.1f}s")
        print(f"avg time per call: {avg_ms / 1000:.1f}s")
        print(f"input tokens: {total_in}")
        print(f"output tokens: {total_out}")
        print("-------------------\n")


# Reset at the start of each run_batch.
_batch: BatchLlmMetrics = BatchLlmMetrics()


def reset_batch_metrics() -> BatchLlmMetrics:
    global _batch
    _batch = BatchLlmMetrics()
    return _batch


def record_llm_call(
    *,
    agent: str,
    model: str,
    latency_ms: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    _batch.record(
        agent=agent,
        model=model,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def print_batch_metrics() -> None:
    _batch.print_summary()
