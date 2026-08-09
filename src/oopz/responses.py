"""
Oopz API 响应处理工具。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def http_error(status_code: int, body_text: str = "", limit: int = 200) -> str:
    """构造统一的 HTTP 错误描述。

    无响应体时返回 ``"HTTP 500"``；有响应体时返回 ``"HTTP 500 | <截断的响应体>"``。
    截断长度由 ``limit`` 控制（默认 200）。
    """
    suffix = f" | {body_text[:limit]}" if body_text else ""
    return f"HTTP {status_code}{suffix}"


@dataclass(frozen=True)
class ApiResult:
    """Oopz API 响应的归一化结果。

    各调用方按需把它映射到自身返回形态（``{"error": ...}`` / ``[]`` / ``data`` 子集），
    从而把「状态码判定 + JSON 解析 + 业务 status 字段」这段样板收口到唯一来源，
    同时保留各方法差异化的返回类型与日志文案。
    """

    ok: bool
    data: Any = None
    error: str | None = None
    status_code: int | None = None
    raw: dict | None = None


def _response_status_code(resp: Any) -> int | None:
    value = getattr(resp, "status_code", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_api_response(
    resp: Any,
    *,
    data_key: str = "data",
    data_default: Any = None,
    error_with_body: bool = False,
    body_limit: int = 200,
) -> ApiResult:
    """把一次 Oopz API 响应归一化为 :class:`ApiResult`。

    判定顺序：

    1. ``resp is None`` → 失败「未获得响应」。
    2. ``status_code != 200`` → 失败，error 为 :func:`http_error`。
    3. 响应体为空 → 失败（见 ``error_with_body``）。
    4. JSON 解析失败 → 失败（见 ``error_with_body``）。
    5. ``result["status"]`` 为假 → 失败，error 取 ``message``/``error``/「未知错误」。
    6. 成功 → ``data`` 为 ``result[data_key]``（缺省回退 ``data_default``）。

    ``error_with_body``：传输/解析类错误是否携带响应体片段。
    - ``False``（默认，查询类接口）：错误为简洁的 ``"HTTP 500"`` / ``"empty response"`` / ``"invalid JSON"``。
    - ``True``（频道创建等需要排障细节的接口）：错误为 ``"HTTP 500 | <body>"`` /
      ``"响应非 JSON: <body>"``，与变更类接口的口径一致。
    """
    if resp is None:
        return ApiResult(False, error="未获得响应")

    status_code = _response_status_code(resp)
    raw = (getattr(resp, "text", "") or "") if error_with_body else ""
    if status_code is None:
        return ApiResult(False, error="响应缺少有效 HTTP 状态码")
    if status_code != 200:
        err = http_error(status_code, raw, body_limit) if error_with_body else http_error(status_code)
        return ApiResult(False, error=err, status_code=status_code)

    if not error_with_body and not getattr(resp, "content", None):
        return ApiResult(False, error="empty response", status_code=status_code)

    try:
        result = resp.json()
    except ValueError:
        err = f"响应非 JSON: {raw[:body_limit]}" if error_with_body else "invalid JSON"
        return ApiResult(False, error=err, status_code=status_code)

    if not result.get("status"):
        msg = result.get("message") or result.get("error") or "未知错误"
        return ApiResult(False, error=msg, status_code=status_code, raw=result)

    return ApiResult(
        True,
        data=result.get(data_key, data_default),
        status_code=status_code,
        raw=result,
    )


@dataclass(frozen=True)
class MutationOutcome:
    """「变更类」API（增删改）响应的归一化结果。

    只归一化成败判定与错误提取；成功消息文案与成功/失败日志策略各异，留给调用方处理，
    从而零行为变更地复用同一段「非 200 / 非 JSON / status·code 判定 / 错误提取」样板。
    """

    ok: bool
    server_message: str | None = None
    error: str | None = None


def parse_mutation_response(
    resp: Any,
    *,
    accept_code: bool = False,
    body_limit: int = 200,
) -> MutationOutcome:
    """把一次变更类 API 响应归一化为 :class:`MutationOutcome`。

    判定顺序与各变更方法内联样板逐字等价：

    1. ``status_code != 200`` → 失败，error 为 ``http_error(code, raw, body_limit)``。
    2. JSON 解析失败 → 失败「响应非 JSON: <raw 截断>」。
    3. ``result["status"] is True``（``accept_code`` 时附加 ``code in (0,"0","success",200)``）→ 成功，
       ``server_message`` 为 ``result["message"]``（可能为 None，由调用方决定默认文案）。
    4. 否则 → 失败，error 取 ``message``/``error``/``str(result)``。
    """
    raw = getattr(resp, "text", "") or ""
    status_code = _response_status_code(resp)
    if status_code is None:
        return MutationOutcome(False, error="响应缺少有效 HTTP 状态码")
    if status_code != 200:
        return MutationOutcome(False, error=http_error(status_code, raw, body_limit))
    try:
        result = resp.json()
    except Exception:
        return MutationOutcome(False, error=f"响应非 JSON: {raw[:body_limit]}")
    ok = result.get("status") is True or (
        accept_code and result.get("code") in (0, "0", "success", 200)
    )
    if ok:
        return MutationOutcome(True, server_message=result.get("message"))
    return MutationOutcome(False, error=result.get("message") or result.get("error") or str(result))
