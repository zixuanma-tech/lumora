"""第三算子：加载材料、组织表达、校验并提交；不绑定模型或 prompt。"""

from collections.abc import Callable
from uuid import uuid4

from guide_models import (
    GuideComposer, GuideContext, GuideDraft, GuideRequest, GuideResult, GuideReview,
    GuideReviewer, GuideSelection, GuideSelector, PreparedGuide, validate_guide_context,
)
from session_state import GuideRecord, SessionState


def build_guide_request(
    state: SessionState,
    *,
    relic_id: str | None = None,
    question: str | None = None,
    guide_context: GuideContext | None = None,
) -> GuideRequest:
    """从服务端状态构造不可变请求，不调用外部服务、不提交状态。"""
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

    current_text = None
    if question is not None and guide_context is None:
        current_text = state.visited_relics[-1].guide_text
    return GuideRequest(
        session_id=state.session_id, relic_id=target, visitor_info=state.visitor_info,
        visited_relic_ids=state.visited_relic_ids, question=question,
        guide_context=guide_context, current_guide_text=current_text,
    )


async def prepare_guide(
    request: GuideRequest,
    *,
    selector: GuideSelector | None,
    composer: GuideComposer | None,
    reviewer: GuideReviewer | None = None,
    review_mode: str = "independent",
    ensure_unchanged: Callable[[], None] | None = None,
) -> PreparedGuide:
    """生成并校验讲稿，结果只保存在返回值中，不接触可变会话。

    前台算子可注入每次 await 后的状态检查；后台预生成使用不可变请求，
    由 Router 在消费时核对版本，不因播放进度或自然计时使准备工作失效。
    """
    if not isinstance(request, GuideRequest):
        raise ValueError("request 必须是 GuideRequest")
    if review_mode not in ("independent", "integrated"):
        raise ValueError("review_mode 必须是 independent 或 integrated")
    if review_mode == "integrated" and reviewer is not None:
        raise ValueError("integrated 模式不能同时提供独立 reviewer")
    required_services = [("selector", selector), ("composer", composer)]
    if review_mode == "independent":
        required_services.append(("reviewer", reviewer))
    for name, service in required_services:
        if service is None:
            raise NotImplementedError(f"讲解服务尚未接入，请提供 {name}")
        if not callable(service):
            raise ValueError(f"{name} 必须是可调用的异步服务")
    if ensure_unchanged is not None and not callable(ensure_unchanged):
        raise ValueError("ensure_unchanged 必须是可调用对象或 None")

    def check_unchanged() -> None:
        if ensure_unchanged is not None:
            ensure_unchanged()

    selection = await selector(request=request)
    check_unchanged()
    if not isinstance(selection, GuideSelection) or selection.relic_id != request.relic_id:
        raise ValueError("selector 必须返回目标展品的 GuideSelection")

    draft = await composer(request=request, selection=selection)
    check_unchanged()
    if not isinstance(draft, GuideDraft) or draft.relic_id != request.relic_id:
        raise ValueError("composer 必须返回目标展品的 GuideDraft")
    material_ids = {item.material_id for item in selection.materials}
    if not set(draft.used_material_ids) <= material_ids:
        raise ValueError("草稿引用了选材结果中不存在的材料")

    review = None
    if review_mode == "independent":
        review = await reviewer(request=request, selection=selection, draft=draft)
        check_unchanged()
        if not isinstance(review, GuideReview):
            raise ValueError("reviewer 必须返回 GuideReview")
        if not review.approved:
            details = "；".join(review.issues) or "未通过依据核对"
            raise ValueError(f"讲解未通过核对：{details}")

    return PreparedGuide(request, selection, draft, review, review_mode)


def commit_prepared_guide(state: SessionState, prepared: PreparedGuide) -> GuideResult:
    """校验当前会话并一次提交准备稿；路由版本与显式时间调整由 Router 核对。"""
    if not isinstance(state, SessionState):
        raise ValueError("state 必须是 SessionState")
    if not isinstance(prepared, PreparedGuide):
        raise ValueError("prepared 必须是 PreparedGuide")
    request = prepared.request
    if state.session_id != request.session_id:
        raise ValueError("准备讲稿不属于当前会话")
    # 再次校验 active、当前目标与追问归属；原稿、历史和兴趣不能过期。
    latest = build_guide_request(
        state, relic_id=request.relic_id, question=request.question,
        guide_context=request.guide_context,
    )
    if latest.visited_relic_ids != request.visited_relic_ids:
        raise ValueError("准备讲稿的参观历史已变化，请重新生成")
    fields = ("content_interests", "guide_styles", "guide_focus", "other_needs")
    if any(getattr(latest.visitor_info, name) != getattr(request.visitor_info, name) for name in fields):
        raise ValueError("准备讲稿的游客信息已变化，请重新生成")
    if latest.current_guide_text != request.current_guide_text:
        raise ValueError("准备讲稿的当前原稿已变化，请重新生成")

    draft = prepared.draft
    new_context = GuideContext(state.session_id, request.relic_id, str(uuid4()), draft.segments)
    guide_record = GuideRecord(
        guide_id=new_context.guide_id, relic_id=request.relic_id,
        kind="guide" if request.question is None else "answer",
        segments=draft.segments, question=request.question,
    )
    # 主讲日志与参观一次提交；问答只追加日志，不消费队首或改写已有记录。
    visit_record = state.record_guide(guide_record)
    return GuideResult(
        request=request, selection=prepared.selection, draft=draft, review=prepared.review,
        guide_context=new_context, guide_record=guide_record,
        resume_context=request.guide_context, visit_record=visit_record,
        review_mode=prepared.review_mode,
    )


async def op_generate_guide(
    state: SessionState,
    *,
    relic_id: str | None = None,
    question: str | None = None,
    guide_context: GuideContext | None = None,
    selector: GuideSelector | None = None,
    composer: GuideComposer | None = None,
    reviewer: GuideReviewer | None = None,
    review_mode: str = "independent",
) -> GuideResult:
    """兼容前台生成入口；各异步阶段严格检查完整状态，成功后一次提交。"""
    request = build_guide_request(
        state, relic_id=relic_id, question=question, guide_context=guide_context,
    )
    snapshot = state.to_dict()

    def ensure_unchanged() -> None:
        if state.to_dict() != snapshot:
            raise RuntimeError("讲解期间会话已变化，请串行调度后重新生成")

    prepared = await prepare_guide(
        request, selector=selector, composer=composer, reviewer=reviewer,
        review_mode=review_mode, ensure_unchanged=ensure_unchanged,
    )
    return commit_prepared_guide(state, prepared)
