"""LLM 비용 산출 — 게이트웨이 경계 (R1-6).

compute_cost는 원래 runtime.py에 있었고 admin 라우터가 이를 역임포트했다(§1.2-8).
LLM 사용량→비용 환산은 게이트웨이 관심사이므로 이 경계로 옮긴다. runtime.py는
하위 호환을 위해 재수출한다.
"""

from typing import Any


def compute_cost(usage: dict[str, int], cost_per_1k: dict[str, Any] | None) -> float:
    """usage(prompt/completion tokens)와 모델 cost_per_1k로 USD 비용 산출.

    cost_per_1k는 {"input"|"prompt": x, "output"|"completion": y} 형태를 허용한다.
    가격 미설정(빈 dict)이면 0.0.
    """
    if not cost_per_1k:
        return 0.0
    in_rate = float(cost_per_1k.get("input", cost_per_1k.get("prompt", 0)) or 0)
    out_rate = float(cost_per_1k.get("output", cost_per_1k.get("completion", 0)) or 0)
    prompt = usage.get("prompt_tokens", 0) or 0
    completion = usage.get("completion_tokens", 0) or 0
    return (prompt / 1000.0) * in_rate + (completion / 1000.0) * out_rate
