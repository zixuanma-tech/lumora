"""把七个模型职责统一接到可配置的 Chat Completions 客户端。

这是部署适配层；Prompt、数据契约、算子及 Router 调用方式不变。
馆藏、候选史料、空间快照仍由调用方注入，不能从聊天模型编造。
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Any

from guide_models import GuideMaterial, GuideRequest
from guide_prompts import make_guide_composer, make_guide_reviewer, make_guide_selector
from input_interpreter_prompt import make_input_interpreter
from interest_prompts import make_interest_parser
from model_api import ChatCompletionsClient, LLMConfig
from route_models import MuseumDataSource, MuseumSnapshot, PlannerConfig, VectorService
from router import RouterServices
from summary_models import SummaryDataSource
from summary_prompts import make_summary_composer, make_summary_reviewer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "llm_config.json"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "llm_config.example.json"
ModelCall = Callable[..., Awaitable[str | Mapping[str, Any]]]
CandidateLoader = Callable[..., Awaitable[Sequence[GuideMaterial]]]


def load_llm_config(
    path: str | Path | None = None, *, environ: Mapping[str, str] | None = None,
) -> LLMConfig:
    """读取配置文件，再以 ``LUMORA_LLM_<大写字段名>`` 环境变量覆盖。

    优先读取未跟踪的本地配置；不存在时读取仓库中的示例配置。
    路径相对于模块定位，和启动工作目录无关。真实密钥只通过
    LUMORA_LLM_API_KEY 提供；错误消息不回显配置值。
    """
    config_path = Path(path) if path is not None else (
        DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.is_file() else EXAMPLE_CONFIG_PATH
    )

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("模型配置包含重复字段")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("模型配置不能包含非有限数值")

    try:
        with config_path.open(encoding="utf-8-sig") as stream:
            data = json.load(
                stream, object_pairs_hook=object_pairs, parse_constant=invalid_constant,
            )
    except json.JSONDecodeError:
        raise ValueError("模型配置必须是合法 JSON") from None
    if not isinstance(data, dict):
        raise ValueError("模型配置必须是 JSON 对象")
    allowed = {item.name for item in fields(LLMConfig)}
    if set(data) - allowed:
        raise ValueError("模型配置包含不支持的字段")
    values = dict(data)
    env = os.environ if environ is None else environ
    for name in allowed:
        key = "LUMORA_LLM_" + name.upper()
        if key in env:
            value = env[key]
            if name == "timeout":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise ValueError("LUMORA_LLM_TIMEOUT 必须是正数秒数") from None
            elif name == "embedding_model" and value == "":
                value = None
            values[name] = value
    return LLMConfig(**values)


async def _missing_candidates(*, request: GuideRequest) -> Sequence[GuideMaterial]:
    raise NotImplementedError("讲解候选材料尚未接入，请注入 candidates 或异步材料加载函数")


def build_model_services(
    *, config: LLMConfig | None = None, config_path: str | Path | None = None,
    model_call: ModelCall | None = None,
    candidates: Sequence[GuideMaterial] | CandidateLoader | None = None,
    snapshot: MuseumSnapshot | None = None, data_source: MuseumDataSource | None = None,
    vector_service: VectorService | None = None,
    summary_data_source: SummaryDataSource | None = None,
    taxonomy: Mapping[str, Sequence[str]] | None = None,
    planner_config: PlannerConfig | None = None, transport=None,
) -> RouterServices:
    """一次装配全部模型服务；创建时不发送网络请求。

    七个职责使用同一配置，Reviewer 始终为单独模型调用。需要真实请求时
    替换占位密钥；测试可注入 model_call 或底层异步 transport。embedding
    只在配置了明确的 embedding_model 后接入，或显式注入 VectorService。
    """
    if config is not None and config_path is not None:
        raise ValueError("config 和 config_path 只能提供一个")
    if config is not None and not isinstance(config, LLMConfig):
        raise ValueError("config 必须是 LLMConfig")
    if snapshot is not None and data_source is not None:
        raise ValueError("snapshot 和 data_source 只能提供一个")
    if model_call is not None and transport is not None:
        raise ValueError("model_call 和 transport 只能提供一个")
    if model_call is not None and (config is not None or config_path is not None):
        raise ValueError("model_call 与 config/config_path 不能同时提供")
    if model_call is not None and not callable(model_call):
        raise TypeError("model_call 必须是异步可调用对象")
    if model_call is None:
        settings = config if config is not None else load_llm_config(config_path)
        client = ChatCompletionsClient(settings, transport=transport)
        model_call = client
        if vector_service is None and settings.embedding_model is not None:
            vector_service = client

    return RouterServices(
        interpreter=make_input_interpreter(model_call),
        interest_parser=make_interest_parser(model_call, taxonomy=taxonomy),
        selector=make_guide_selector(
            model_call, _missing_candidates if candidates is None else candidates,
        ),
        composer=make_guide_composer(model_call), reviewer=make_guide_reviewer(model_call),
        summary_composer=make_summary_composer(model_call),
        summary_reviewer=make_summary_reviewer(model_call),
        snapshot=snapshot, data_source=data_source, vector_service=vector_service,
        planner_config=planner_config, summary_data_source=summary_data_source,
    )
