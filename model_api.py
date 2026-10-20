"""可注入的 Chat Completions / Embeddings HTTP 客户端。

使用 Python 标准库，不依赖模型 SDK；Prompt 模块保持厂商无关。
不重试、不自动降级、不跟随重定向，也不修改进程或系统代理配置。
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PLACEHOLDER_API_KEY = "REPLACE_WITH_YOUR_API_KEY"
JSONTransport = Callable[..., Awaitable[Mapping[str, Any]]]


class ModelServiceError(RuntimeError):
    """固定错误描述；不包含上游响应体、密钥或游客输入。"""

    def __init__(self, message: str, *, code: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _path(value: Any, name: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/")
            or value.startswith("//") or len(value.rstrip("/")) == 0
            or any(char.isspace() or ord(char) < 32 or char in "?#\\" for char in value)):
        raise ValueError(f"{name} 必须是以 / 开头、不含查询参数的 API 路径")
    return value.rstrip("/")


def _base_url(value: Any) -> str:
    value = _text(value, "base_url").rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port  # 立即验证端口，避免在发请求时泄露原始地址。
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment
                 and not any(char.isspace() or ord(char) < 32 or char == "\\" for char in value)
                 and (port is None or 0 < port < 65536))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("base_url 必须是无凭据、查询参数和片段的 HTTP(S) 地址")
    return value


def _endpoint(base_url: str, path: str) -> str:
    # 保留 base_url 中调用方明确提供的 /v1 等前缀；不猜测服务版本。
    return base_url if base_url.endswith(path) else base_url + path


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """模型服务配置；默认密钥为占位值，repr 不显示密钥。"""

    base_url: str = "https://llmapi.paratera.com"
    model: str = "GLM-5.3"
    api_key: str = field(default=PLACEHOLDER_API_KEY, repr=False)
    chat_path: str = "/chat/completions"
    timeout: float = 60.0
    response_mode: str = "json_schema"
    embedding_model: str | None = None
    embeddings_path: str = "/embeddings"

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _base_url(self.base_url))
        key = _text(self.api_key, "api_key")
        if any(not 33 <= ord(char) <= 126 for char in self.api_key):
            raise ValueError("api_key 必须是不含空白或控制字符的 ASCII 字符串")
        object.__setattr__(self, "api_key", key)
        object.__setattr__(self, "model", _text(self.model, "model"))
        if (type(self.timeout) not in (int, float)
                or not math.isfinite(self.timeout) or self.timeout <= 0):
            raise ValueError("timeout 必须是正有限秒数")
        object.__setattr__(self, "timeout", float(self.timeout))
        if self.response_mode not in ("json_schema", "json_object"):
            raise ValueError("response_mode 必须是 json_schema 或 json_object")
        object.__setattr__(self, "chat_path", _path(self.chat_path, "chat_path"))
        object.__setattr__(self, "embeddings_path", _path(self.embeddings_path, "embeddings_path"))
        if self.embedding_model is not None:
            object.__setattr__(self, "embedding_model", _text(self.embedding_model, "embedding_model"))

    @property
    def chat_url(self) -> str:
        return _endpoint(self.base_url, self.chat_path)

    @property
    def embeddings_url(self) -> str:
        return _endpoint(self.base_url, self.embeddings_path)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 自动重定向可能把 Authorization 发给其他主机，且可能改变 POST 方法。
        return None


def _json_response(raw: bytes) -> Mapping[str, Any]:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("nonfinite number")

    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_pairs,
                          parse_constant=invalid_constant)
    except (UnicodeDecodeError, ValueError):
        raise ModelServiceError("模型服务未返回合法 JSON", code="invalid_response") from None
    if not isinstance(data, Mapping):
        raise ModelServiceError("模型服务响应必须是 JSON 对象", code="invalid_response")
    return data


def _post_json_sync(*, url: str, headers: Mapping[str, str], payload: Mapping[str, Any],
                    timeout_seconds: float) -> Mapping[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        # build_opener 仍读取既有 urllib 代理设置；只禁止重定向，不覆盖代理。
        with build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
            return _json_response(response.read())
    except HTTPError as error:
        status = error.code
        error.close()
        if status in (401, 403):
            message, code = "模型服务认证失败，请替换 API 密钥或检查权限", "authentication_failed"
        elif 300 <= status < 400:
            message, code = "模型服务返回重定向，请检查 API 地址", "redirect_rejected"
        else:
            message, code = f"模型服务返回 HTTP {status}", "http_error"
        raise ModelServiceError(message, code=code, status_code=status) from None
    except TimeoutError:
        raise ModelServiceError("模型服务请求超时", code="timeout") from None
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise ModelServiceError("模型服务请求超时", code="timeout") from None
        raise ModelServiceError("无法连接模型服务，请检查网络和代理配置", code="connection_failed") from None
    except (OSError, HTTPException):
        raise ModelServiceError("模型服务连接或响应读取失败", code="connection_failed") from None


async def post_json(*, url: str, headers: Mapping[str, str], payload: Mapping[str, Any],
                    timeout_seconds: float) -> Mapping[str, Any]:
    return await asyncio.to_thread(
        _post_json_sync, url=url, headers=headers, payload=payload,
        timeout_seconds=timeout_seconds,
    )


def _chat_response_format(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("type") != "json_schema":
        raise ValueError("response_format 必须是 json_schema 对象")
    schema = value.get("schema")
    if not isinstance(schema, Mapping):
        raise ValueError("response_format.schema 必须是 JSON Schema 对象")
    strict = value.get("strict", True)
    if type(strict) is not bool:
        raise ValueError("response_format.strict 必须是布尔值")
    return {"type": "json_schema", "json_schema": {
        "name": _text(value.get("name"), "response_format.name"),
        "strict": strict, "schema": dict(schema),
    }}


def _response_content(data: Mapping[str, Any]) -> str:
    if not isinstance(data, Mapping):
        raise ModelServiceError("模型服务响应必须是 JSON 对象", code="invalid_response")
    if "error" in data:
        raise ModelServiceError("模型服务返回错误响应", code="invalid_response")
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        raise ModelServiceError("模型服务未返回唯一有效结果", code="invalid_response")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise ModelServiceError("模型输出未完整结束或被过滤", code="incomplete_response")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ModelServiceError("模型响应缺少 message", code="invalid_response")
    if message.get("refusal"):
        raise ModelServiceError("模型拒绝完成本次请求", code="model_refusal")
    if message.get("tool_calls") or message.get("function_call"):
        raise ModelServiceError("模型响应包含非预期工具调用", code="invalid_response")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ModelServiceError("模型响应缺少文本正文", code="invalid_response")
    return content


class ChatCompletionsClient:
    """满足全部 Prompt 的异步 model_call 及可选 VectorService 契约。"""

    def __init__(self, config: LLMConfig | None = None, *, transport: JSONTransport | None = None):
        self.config = config if config is not None else LLMConfig()
        if not isinstance(self.config, LLMConfig):
            raise TypeError("config 必须是 LLMConfig")
        if transport is not None and not callable(transport):
            raise TypeError("transport 必须是异步可调用对象")
        self._transport = post_json if transport is None else transport

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.config.api_key,
                "Content-Type": "application/json", "Accept": "application/json"}

    async def __call__(self, *, system_prompt: str, user_prompt: str,
                       response_format: Mapping[str, Any]) -> str:
        system_prompt = _text(system_prompt, "system_prompt")
        user_prompt = _text(user_prompt, "user_prompt")
        formatted = _chat_response_format(response_format)
        if self.config.response_mode == "json_object":
            schema_text = json.dumps(formatted["json_schema"]["schema"], ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False)
            system_prompt += "\n\n输出必须符合以下 JSON Schema，只返回 JSON 对象：\n" + schema_text
            formatted = {"type": "json_object"}
        payload = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_prompt}],
            "stream": False, "response_format": formatted,
        }
        data = await self._transport(
            url=self.config.chat_url, headers=self._headers, payload=payload,
            timeout_seconds=self.config.timeout,
        )
        return _response_content(data)

    async def embed_query(self, text: str) -> Sequence[float]:
        text = _text(text, "向量查询文本")
        if self.config.embedding_model is None:
            raise NotImplementedError("Embedding 模型尚未配置，请设置 embedding_model")
        data = await self._transport(
            url=self.config.embeddings_url, headers=self._headers,
            payload={"model": self.config.embedding_model, "input": text, "encoding_format": "float"},
            timeout_seconds=self.config.timeout,
        )
        rows = data.get("data") if isinstance(data, Mapping) and "error" not in data else None
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping)
                or type(rows[0].get("index")) is not int or rows[0]["index"] != 0):
            raise ModelServiceError("Embedding 服务未返回唯一有效向量", code="invalid_response")
        raw = rows[0].get("embedding")
        try:
            valid = (isinstance(raw, list) and bool(raw)
                     and all(type(value) in (int, float) and math.isfinite(value) for value in raw)
                     and any(raw))
        except OverflowError:
            valid = False
        if not valid:
            raise ModelServiceError("Embedding 服务返回了无效向量", code="invalid_response")
        return tuple(float(value) for value in raw)
