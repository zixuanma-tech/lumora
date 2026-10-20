"""核心算子入口。实现统一输入处理、路线规划、讲解与探索手记流程。

op_process_input 处理问卷、游客信息调整和聊天；按钮事件仍由 Router 直达。
结构化输入直接校验，自然语言交给注入的 InputInterpreter 理解。
模型服务由调用方注入；输入处理不直接调用其他算子。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any, Protocol

from session_state import VISITOR_INFO_FIELDS, SessionState, VisitRecord, VisitorInfo
from route_planner import op_plan_route
from guide_generator import op_generate_guide
from guide_models import GuideContext, validate_guide_context as _validate_guide_context
from summary_generator import op_generate_summary


@dataclass(frozen=True, slots=True)
class InputAnalysis:
    """自然语言分析器的输出，允许一条输入同时包含需求和追问。

    visitor_info_changes 只含本次涉及的游客信息字段；涉及的列表须给出
    合并、删除或替换后的完整新值。未传字段保留，[] 清空，时间 None 清空。
    question 只提取需要针对当前文物回答的问题，不自动写入游客信息。
    两个 requested 标志表达明确的重规划/结束意图，不在这里执行对应操作。
    """

    visitor_info_changes: Mapping[str, Any] = field(default_factory=dict)
    question: str | None = None
    replan_requested: bool = False
    finish_requested: bool = False


class InputInterpreter(Protocol):
    """自然语言分析接口。

    项目内置的提示词、上下文序列化和模型 JSON 结果解析见
    ``input_interpreter_prompt.py``；模型 SDK 适配器仍由应用启动层注入。

    传入当前有效游客信息、目标展品的已完成记录和语音讲解上下文，
    以理解新增/替换需求及追问指代。正在讲的展品可以尚无 VisitRecord。
    返回 InputAnalysis，由算子校验。实现方不持有可修改的 SessionState。
    """

    async def __call__(
        self,
        text: str,
        *,
        visitor_info: VisitorInfo,
        current_visit: VisitRecord | None,
        guide_context: GuideContext | None,
    ) -> InputAnalysis: ...


@dataclass(frozen=True, slots=True)
class InputResult:
    """供 Router 调度的结果。此时游客信息已更新，路线和讲解尚未处理。

    changed_fields 是实际发生变化的字段；即使没有变化，也可能明确要求重规划。
    updated_fields 是本次显式提交的字段，包括值未变化的字段，供 Router
    在用户重新确认时间预算时重置真实计时基准。
    混合输入应先用更新后的游客信息回答 question，再安排所需的后续规划。
    若 needs_current_relic 为 True，应先明确当前展品，不把问题当作游客兴趣。
    """

    visitor_info: VisitorInfo
    changed_fields: tuple[str, ...] = ()
    question: str | None = None
    current_relic_id: str | None = None
    replan_requested: bool = False
    finish_requested: bool = False
    guide_context: GuideContext | None = None
    updated_fields: tuple[str, ...] = ()

    @property
    def needs_current_relic(self) -> bool:
        return self.question is not None and self.current_relic_id is None


def _validate_analysis(analysis: InputAnalysis) -> tuple[dict[str, Any], str | None]:
    """先检查整个分析结果，防止游客信息更新后才发现追问或标志无效。"""
    if not isinstance(analysis, InputAnalysis):
        raise ValueError("自然语言分析器必须返回 InputAnalysis")
    if not isinstance(analysis.visitor_info_changes, Mapping):
        raise ValueError("visitor_info_changes 必须是字段字典")
    changes = dict(analysis.visitor_info_changes)
    if set(changes) - VISITOR_INFO_FIELDS:
        raise ValueError("只允许更新五类游客信息字段")
    if type(analysis.replan_requested) is not bool or type(analysis.finish_requested) is not bool:
        raise ValueError("replan_requested 和 finish_requested 必须是布尔值")
    question = analysis.question
    if question is not None:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question 必须是非空字符串或 None")
        question = question.strip()
    return changes, question


async def op_process_input(
    state: SessionState,
    user_input: Mapping[str, Any] | str,
    *,
    interpreter: InputInterpreter | None = None,
    guide_context: GuideContext | None = None,
) -> InputResult:
    """理解输入，提交游客信息局部更新，返回供 Router 使用的调度信息。

    字典输入使用 VisitorInfo 的五个字段，表示已结构化的问卷或信息调整；
    字符串输入必须提供 interpreter，由其区分整体需求、当前追问或混合输入。
    所有输出校验通过后才更新游客信息；分析失败不提交任何状态变化。
    guide_context 由 Router 从当前讲解及实际语音播放进度组装；有上下文时
    追问优先指向其中的展品，不能只依据最近完成讲解的 current_relic_id。
    返回的上下文原样供 Router 交给讲解算子，不在输入算子内决定如何改稿。
    不修改参观轨迹、讲解记录、待看队列和会话状态；结束意图也只返回标志。
    主讲与问答在第三算子核对成功后统一保存，不由输入解析承担记录职责。
    同一会话的请求仍需由调用方串行处理。
    """
    if state.status != "active":
        raise RuntimeError("游览已结束，不能继续处理游览输入")
    _validate_guide_context(state, guide_context)
    target_relic_id = guide_context.relic_id if guide_context is not None else state.current_relic_id
    current_visit = (
        state.visited_relics[-1]
        if state.visited_relics and state.visited_relics[-1].relic_id == target_relic_id
        else None
    )

    if isinstance(user_input, Mapping):
        analysis = InputAnalysis(visitor_info_changes=user_input)
    elif isinstance(user_input, str):
        text = user_input.strip()
        if not text:
            raise ValueError("用户输入不能为空")
        if interpreter is None:
            raise NotImplementedError("自然语言分析器尚未接入，请提供 InputInterpreter")
        analysis = await interpreter(
            text,
            visitor_info=state.visitor_info,
            current_visit=current_visit,
            guide_context=guide_context,
        )
    else:
        raise ValueError("用户输入必须是游客信息字段字典或自然语言字符串")

    changes, question = _validate_analysis(analysis)
    previous_info = state.visitor_info
    # 复用 VisitorInfo 的校验和规范化；不在算子内维护另一份字段规则。
    updated_info = replace(previous_info, **changes)
    changed_fields = tuple(
        item.name for item in fields(VisitorInfo)
        if getattr(previous_info, item.name) != getattr(updated_info, item.name)
    )
    result = InputResult(
        visitor_info=updated_info,
        changed_fields=changed_fields,
        question=question,
        current_relic_id=target_relic_id if question is not None else None,
        replan_requested=analysis.replan_requested,
        finish_requested=analysis.finish_requested,
        guide_context=guide_context,
        updated_fields=tuple(item.name for item in fields(VisitorInfo) if item.name in changes),
    )
    if changes:
        state.update_visitor_info(**{
            name: getattr(updated_info, name) for name in changes
        })
    return result

