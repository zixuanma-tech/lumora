"""第三算子：选材、组织表达、核对、提交；不接入模型或 prompt。"""

from uuid import uuid4

from guide_models import (
    GuideComposer, GuideContext, GuideDraft, GuideRequest, GuideResult, GuideReview,
    GuideReviewer, GuideSelection, GuideSelector, validate_guide_context,
)
from session_state import GuideRecord, SessionState


async def op_generate_guide(
    state: SessionState,
    *,
    relic_id: str | None = None,
    question: str | None = None,
    guide_context: GuideContext | None = None,
    selector: GuideSelector | None = None,
    composer: GuideComposer | None = None,
    reviewer: GuideReviewer | None = None,
) -> GuideResult:
    """生成队首讲解或回答当前展品问题，向所有阶段传入参观历史 ID。

    主讲与问答均在核对成功后保存到会话，首次讲解才推进队首。追问优先
    使用播放上下文的展品，其次最近完成生成的展品，不从待看队首猜目标。
    追问只追加讲解记录、不覆盖原稿，返回原 resume_context 供 Router 续播。
    初代等待完整草稿与核对后返回分句稿，不输出未核对 token 或实际音频。
    同会话仍须串行调度；每次 await 后校验状态，拒绝提交过期结果。
    """
    if not isinstance(state, SessionState):
        raise ValueError("state 必须是 SessionState")
    if state.status != "active":
        raise RuntimeError("游览已结束，不能生成讲解")
    if relic_id is not None:
        if not isinstance(relic_id, str) or not relic_id.strip():
            raise ValueError("relic_id 必须是非空字符串")
        relic_id = relic_id.strip()
    if question is not None:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question 必须是非空字符串或 None")
        question = question.strip()
    validate_guide_context(state, guide_context)

    if question is None:
        if guide_context is not None:
            raise ValueError("首次讲解不接受已有稿件上下文；续播由 Router 处理")
        if not state.pending_queue:
            raise ValueError("待参观队列为空")
        target = state.pending_queue[0].relic_id
    else:
        target = guide_context.relic_id if guide_context else state.current_relic_id
        if target is None:
            raise ValueError("请先明确正在讲解的展品，不能从待看队首猜测追问目标")
    if relic_id is not None and relic_id != target:
        raise ValueError("展品与当前讲解目标不匹配，请读取最新路线或播放上下文")
    for name, service in (("selector", selector), ("composer", composer), ("reviewer", reviewer)):
        if service is None:
            raise NotImplementedError(f"讲解服务尚未接入，请提供 {name}")
        if not callable(service):
            raise ValueError(f"{name} 必须是可调用的异步服务")

    snapshot = state.to_dict()
    current_text = None
    if question is not None and guide_context is None:
        current_text = state.visited_relics[-1].guide_text
    request = GuideRequest(
        session_id=state.session_id, relic_id=target, visitor_info=state.visitor_info,
        visited_relic_ids=state.visited_relic_ids, question=question,
        guide_context=guide_context, current_guide_text=current_text,
    )

    def ensure_unchanged() -> None:
        if state.to_dict() != snapshot:
            raise RuntimeError("讲解期间会话已变化，请串行调度后重新生成")

    selection = await selector(request=request)
    ensure_unchanged()
    if not isinstance(selection, GuideSelection) or selection.relic_id != target:
        raise ValueError("selector 必须返回目标展品的 GuideSelection")

    draft = await composer(request=request, selection=selection)
    ensure_unchanged()
    if not isinstance(draft, GuideDraft) or draft.relic_id != target:
        raise ValueError("composer 必须返回目标展品的 GuideDraft")
    material_ids = {item.material_id for item in selection.materials}
    if not set(draft.used_material_ids) <= material_ids:
        raise ValueError("草稿引用了选材结果中不存在的材料")

    review = await reviewer(request=request, selection=selection, draft=draft)
    ensure_unchanged()
    if not isinstance(review, GuideReview):
        raise ValueError("reviewer 必须返回 GuideReview")
    if not review.approved:
        details = "；".join(review.issues) or "未通过依据核对"
        raise ValueError(f"讲解未通过核对：{details}")

    new_context = GuideContext(state.session_id, target, str(uuid4()), draft.segments)
    guide_record = GuideRecord(
        guide_id=new_context.guide_id, relic_id=target,
        kind="guide" if question is None else "answer",
        segments=draft.segments, question=question,
    )
    # 主讲日志与参观一次提交；问答只追加日志，不消费队首或改写已有记录。
    visit_record = state.record_guide(guide_record)
    return GuideResult(
        request=request, selection=selection, draft=draft, review=review,
        guide_context=new_context, guide_record=guide_record,
        resume_context=guide_context, visit_record=visit_record,
    )
