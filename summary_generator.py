"""第四算子：读取本次记录、组织探索手记、核对；不编写 prompt 或调用模型。"""

from collections.abc import Mapping, Sequence

from session_state import SessionState
from summary_data import JsonSummaryDataSource
from summary_models import (
    SummaryComposer, SummaryDataSource, SummaryDraft, SummaryFeedback, SummaryRelic,
    SummaryRequest, SummaryResult, SummaryReview, SummaryReviewer,
)


def _validate_draft(request: SummaryRequest, draft: SummaryDraft) -> None:
    if not isinstance(draft, SummaryDraft):
        raise ValueError("composer 必须返回 SummaryDraft")
    if tuple(item.relic_id for item in draft.trail) != request.visited_relic_ids:
        raise ValueError("参观足迹必须完整保留本次已参观展品及其顺序")
    relic_ids = {item.relic_id for item in request.relics}
    records = {item.guide_id: item for item in request.guide_records}
    for insight in (*draft.discoveries, *draft.connections):
        if not set(insight.relic_ids) <= relic_ids:
            raise ValueError("手记引用了本次记录之外的展品")
        if not set(insight.guide_ids) <= records.keys():
            raise ValueError("手记引用了本次记录之外的讲解稿")
        referenced_ids = {records[guide_id].relic_id for guide_id in insight.guide_ids}
        if referenced_ids != set(insight.relic_ids):
            raise ValueError("发现或知识联系中的展品必须与引用讲解稿对应")
    if any(len(item.relic_ids) < 2 for item in draft.connections):
        raise ValueError("跨展品知识联系至少需要两件展品")
    selected_ids = tuple(item.guide_id for item in draft.questions)
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError("精选问答不能重复引用同一回答")
    if any(guide_id not in records or records[guide_id].kind != "answer" for guide_id in selected_ids):
        raise ValueError("精选问答必须引用本次真实追问回答")


async def op_generate_summary(
    state: SessionState,
    *,
    feedback: SummaryFeedback | None = None,
    data_source: SummaryDataSource | None = None,
    composer: SummaryComposer | None = None,
    reviewer: SummaryReviewer | None = None,
) -> SummaryResult:
    """从完整主讲与问答生成结构化手记，支持游览中及结束后调用。

    不修改会话、不自动结束游览、不保存收藏或感想到游客信息。两个结束
    问题均可跳过；感想原样放入结果。按既定产品口径，已生成的完整内容
    都可用于手记，不依赖播放进度。空游览直接返回 empty，不调用服务。
    composer 与 reviewer 必须显式注入；只做内容提炼，新增史料留待后续。
    """
    if not isinstance(state, SessionState):
        raise ValueError("state 必须是 SessionState")
    if feedback is None:
        feedback = SummaryFeedback()
    if not isinstance(feedback, SummaryFeedback):
        raise ValueError("feedback 必须是 SummaryFeedback 或 None")
    if (feedback.favorite_relic_id is not None
            and feedback.favorite_relic_id not in state.visited_relic_ids):
        raise ValueError("印象最深的展品必须属于本次已参观展品")

    snapshot = state.to_dict()
    visitor_info, visits, records = state.visitor_info, state.visited_relics, state.guide_records
    if not visits and not records:
        return SummaryResult(SummaryRequest(
            state.session_id, visitor_info, visits, records, (), feedback,
        ))
    for name, service in (("composer", composer), ("reviewer", reviewer)):
        if service is None:
            raise NotImplementedError(f"手记服务尚未接入，请提供 {name}")
        if not callable(service):
            raise ValueError(f"{name} 必须是可调用的异步服务")
    if data_source is None:
        data_source = JsonSummaryDataSource()
    if not callable(getattr(data_source, "load_relics", None)):
        raise ValueError("data_source 必须提供异步 load_relics")

    def ensure_unchanged() -> None:
        if state.to_dict() != snapshot:
            raise RuntimeError("手记生成期间会话已变化，请串行调度后重新生成")

    # 全程问答都参与取材；未完成主讲的队首问答不会被写入参观足迹。
    required_ids = tuple(dict.fromkeys(
        [visit.relic_id for visit in visits] + [record.relic_id for record in records]
    ))
    relics = await data_source.load_relics(required_ids)
    ensure_unchanged()
    if (isinstance(relics, (str, bytes, Mapping)) or not isinstance(relics, Sequence)
            or any(not isinstance(item, SummaryRelic) for item in relics)):
        raise ValueError("load_relics 必须返回 SummaryRelic 序列")
    by_id = {item.relic_id: item for item in relics}
    if len(by_id) != len(relics) or set(by_id) != set(required_ids):
        raise ValueError("基础资料必须准确覆盖本次展品，不能缺失、重复或包含额外展品")
    request = SummaryRequest(
        state.session_id, visitor_info, visits, records,
        tuple(by_id[relic_id] for relic_id in required_ids), feedback,
    )

    draft = await composer(request=request)
    ensure_unchanged()
    _validate_draft(request, draft)
    review = await reviewer(request=request, draft=draft)
    ensure_unchanged()
    if not isinstance(review, SummaryReview):
        raise ValueError("reviewer 必须返回 SummaryReview")
    if not review.approved:
        details = "；".join(review.issues) or "未通过内容核对"
        raise ValueError(f"手记未通过核对：{details}")
    return SummaryResult(request, draft, review)
