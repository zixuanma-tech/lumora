"""第二个算子（兴趣解析）的提示词与结构化模型适配层。

该模块把 VisitorInfo 转换为路线评分器使用的 InterestProfile。
它不读取数据库，也不把数据库表结构暴露给模型；应用启动时可以把当前馆藏
词汇表作为可选的 taxonomy 注入提示词，使模型使用馆藏的规范标签。
具体模型 SDK 仍由调用方提供。
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from route_models import InterestProfile
from session_state import VisitorInfo


INTEREST_PARSER_SYSTEM_PROMPT = r"""你是博物馆导览系统的“兴趣结构化解析器”。
你的唯一任务是把服务端给出的当前游客信息转换成路线规划使用的六维 InterestProfile。
你不规划路线，不选择具体展品，不计算分数，不生成讲解，不修改游客信息，也不回答游客问题。

必须只输出一个 JSON 对象，不能输出 Markdown、解释文字、代码块或额外字段：
{
  "target_eras": [],
  "target_regions": [],
  "target_categories": [],
  "target_craft_types": [],
  "target_themes": [],
  "target_aesthetics": [],
  "target_cultural_ip": [],
  "target_audience": [],
  "query_text": ""
}

## 六个评分维度
- target_eras：时代或朝代，例如“唐”“宋”“明清”。
- target_regions：地域、文化圈或来源地，例如“中原”“江南”。
- target_categories：器物或一级品类，例如“陶瓷”“青铜器”。
- target_craft_types：工艺、材质、器型或具体制作方式，例如“青花”“失蜡法”。
- target_themes：题材、用途或生活场景，例如“动物”“礼制”“宫廷生活”。
- target_aesthetics：视觉或审美特征，例如“纹饰繁复”“极简”。
- target_cultural_ip：文化 IP、人物、故事或知名度目标，例如“龙”“名家作品”。
- target_audience：明确要求适合的受众或体验，例如“儿童”“亲子”。

前四个字段是字符串数组，数组内部表示 OR：例如 ["唐", "宋"] 表示唐或宋。
后四个字段是“目标组”的数组，每个目标组本身是非空字符串数组；同一组表示 OR，
不同组表示独立目标。例如 [["动物", "神话瑞兽"], ["礼制"]] 表示命中动物或
神话瑞兽，同时另一个独立目标是礼制。不要把两个独立目标错误合并成一个 OR 组。

## 信息来源与边界
1. 只使用 current_visitor_info 中明确表达的内容。content_interests 是主要来源，
   guide_focus 可以补充用户明确要求关注的历史、工艺、材料、题材或审美角度。
2. guide_styles（生动、通俗、简洁、深入等）是讲解表达偏好，不要把它当作展品标签。
3. remaining_minutes 只用于时间预算，不进入任何兴趣维度，也不要写进 query_text。
4. other_needs 只有在明确描述受众或体验适配时才进入 target_audience，例如儿童、
   亲子、学生、无障碍；年龄本身不要臆测成标签，休息需求也不要变成展品兴趣。
5. 不要凭常识从“喜欢历史”推断具体朝代，不要从“想看好看的”编造审美标签，
   不要从“带孩子”推断具体题材。表达不够具体时保留在 query_text，不要硬填标签。

## 规范词汇与保守原则
用户信息下方可能附有 catalog_taxonomy。若提供该词汇表：
- target 字段只能使用对应维度中出现的规范词；可把用户的同义说法映射到明显对应的规范词。
- 不确定的映射不要猜；将原始语义保留在 query_text，等待后续人工或数据层处理。
- 不要输出词汇表中不存在的新标签、数据库 ID、展品名称或展厅名称。
若没有提供 catalog_taxonomy：
- 只输出用户明确说出的、可作为标签的短语；不要自造上位类或词表。
- 仍然保留完整语义到 query_text，供后续向量服务使用。

## query_text
query_text 是供语义检索或审计使用的简洁查询文本，应概括 content_interests 和明确的
guide_focus，保留时代、地域、品类、工艺、题材等原意；不要加入 guide_styles、剩余时间、
模型推测或不存在的事实。没有内容兴趣时可以为空字符串，但只要有明确兴趣就应非空。

先理解语义，再按 Schema 输出。不能因为字段不确定而编造标签；不能输出说明文字。"""


_TARGET_FIELDS = (
    "target_eras", "target_regions", "target_categories", "target_craft_types",
    "target_themes", "target_aesthetics", "target_cultural_ip", "target_audience",
)
_FLAT_FIELDS = frozenset(_TARGET_FIELDS[:4])
_GROUP_FIELDS = frozenset(_TARGET_FIELDS[4:])


def _string_array_schema() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string", "minLength": 1}}


def _group_array_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
    }


INTEREST_PROFILE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [*_TARGET_FIELDS, "query_text"],
    "properties": {
        **{name: _string_array_schema() for name in _FLAT_FIELDS},
        **{name: _group_array_schema() for name in _GROUP_FIELDS},
        "query_text": {"type": "string"},
    },
}


def _taxonomy_payload(taxonomy: Mapping[str, Sequence[str]] | None) -> dict[str, list[str]] | None:
    if taxonomy is None:
        return None
    if not isinstance(taxonomy, Mapping):
        raise TypeError("taxonomy 必须是字段到字符串序列的映射或 None")
    result: dict[str, list[str]] = {}
    allowed = {
        "eras": "target_eras", "regions": "target_regions", "categories": "target_categories",
        "craft_types": "target_craft_types", "themes": "target_themes",
        "aesthetics": "target_aesthetics", "cultural_ip": "target_cultural_ip",
        "audience": "target_audience",
    }
    for key, values in taxonomy.items():
        if key not in allowed:
            raise ValueError(f"taxonomy 包含未知维度：{key}")
        if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Sequence):
            raise ValueError(f"taxonomy.{key} 必须是字符串序列")
        cleaned = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"taxonomy.{key} 中的标签必须是非空字符串")
            if value.strip() not in cleaned:
                cleaned.append(value.strip())
        result[allowed[key]] = cleaned
    return result


def build_interest_parser_messages(
    *,
    visitor_info: VisitorInfo,
    taxonomy: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, str]]:
    """构造 InterestParser 的 system + user 消息。"""
    if not isinstance(visitor_info, VisitorInfo):
        raise ValueError("visitor_info 必须是 VisitorInfo")
    payload = {
        "task": "把当前游客信息转换为 InterestProfile，只返回规定 JSON。",
        "current_visitor_info": {
            "content_interests": list(visitor_info.content_interests),
            "guide_styles": list(visitor_info.guide_styles),
            "guide_focus": list(visitor_info.guide_focus),
            "remaining_minutes": visitor_info.remaining_minutes,
            "other_needs": list(visitor_info.other_needs),
        },
        "catalog_taxonomy": _taxonomy_payload(taxonomy),
    }
    return [
        {"role": "system", "content": INTEREST_PARSER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def _decode(value: Mapping[str, Any] | str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("兴趣解析模型必须返回 JSON 对象")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("兴趣解析模型返回的内容不是合法 JSON") from error
    if not isinstance(decoded, Mapping):
        raise ValueError("兴趣解析模型返回值必须是 JSON 对象")
    return decoded


def _dedupe_strings(values: Any, name: str) -> list[str]:
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Sequence):
        raise ValueError(f"{name} 必须是字符串数组")
    result = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 中的每项必须是非空字符串")
        value = value.strip()
        if value not in result:
            result.append(value)
    return result


def _dedupe_groups(values: Any, name: str) -> list[list[str]]:
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Sequence):
        raise ValueError(f"{name} 必须是字符串数组的数组")
    result: list[list[str]] = []
    for group in values:
        cleaned = _dedupe_strings(group, name)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def parse_interest_profile(value: Mapping[str, Any] | str) -> InterestProfile:
    """将模型 JSON 解析为现有 InterestProfile，拒绝未知键和非法形状。"""
    data = _decode(value)
    allowed = set(_TARGET_FIELDS) | {"query_text"}
    unknown = set(data) - allowed
    missing = allowed - set(data)
    if unknown:
        raise ValueError(f"兴趣解析模型返回了未知字段：{', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"兴趣解析模型缺少字段：{', '.join(sorted(missing))}")
    normalized: dict[str, Any] = {}
    for name in _FLAT_FIELDS:
        normalized[name] = _dedupe_strings(data[name], name)
    for name in _GROUP_FIELDS:
        normalized[name] = _dedupe_groups(data[name], name)
    query_text = data["query_text"]
    if not isinstance(query_text, str):
        raise ValueError("query_text 必须是字符串")
    normalized["query_text"] = query_text.strip()
    profile = InterestProfile(**normalized)
    if not profile.has_targets:
        raise ValueError("兴趣解析结果至少需要一个目标维度")
    return profile


def make_interest_parser(
    model_call: Callable[..., Awaitable[str | Mapping[str, Any]]],
    *,
    taxonomy: Mapping[str, Sequence[str]] | None = None,
) -> Callable[..., Awaitable[InterestProfile]]:
    """创建可注入 Router/op_plan_route 的 InterestParser。"""
    if not callable(model_call):
        raise TypeError("model_call 必须是可调用对象")
    # 提前校验一次，但保留调用方的原始维度名称，避免把内部字段名
    # 再次当作外部 taxonomy 参数解析。
    _taxonomy_payload(taxonomy)

    async def parse(*, visitor_info: VisitorInfo) -> InterestProfile:
        messages = build_interest_parser_messages(
            visitor_info=visitor_info, taxonomy=taxonomy,
        )
        raw = await model_call(
            system_prompt=messages[0]["content"],
            user_prompt=messages[1]["content"],
            response_format={
                "type": "json_schema", "name": "interest_profile", "strict": True,
                "schema": INTEREST_PROFILE_JSON_SCHEMA,
            },
        )
        return parse_interest_profile(raw)

    return parse

