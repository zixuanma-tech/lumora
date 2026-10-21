"""寻迹的独立演示场馆；全部展品、场馆及资料均为虚构教学设定。

不覆盖 data/ 中的真实数据占位文件。字符向量仅供离线演示推荐，生产场馆应
替换为同模型编码的真实馆藏向量、空间数据及经过核实的来源材料。
"""
from __future__ import annotations

from hashlib import blake2b
from math import sqrt

from guide_models import GuideMaterial
from model_services import build_model_services
from route_models import Exhibit, ExhibitTags, Gallery, MuseumSnapshot, PlannerConfig, SpatialEdge
from summary_models import SummaryRelic

ENTRANCE = "demo-entrance"
EXIT = "demo-exit"
NOTICE = "演示场馆 · 展品、展厅与路线均为虚构示例，用于体验寻迹导览。"
GALLERIES = (
    Gallery("demo-daily", "日常之器", base_pass_min=0.5),
    Gallery("demo-craft", "匠心之作", base_pass_min=0.5),
    Gallery("demo-travel", "远行之物", base_pass_min=0.5),
)
# 资料中的尺寸、纹样和结构只是演示设定，不对应真实文物。
ITEMS = (
    ("demo-pot", "回纹陶罐", "demo-daily", "陶瓷", "手工成型", ("日常生活", "储藏"),
     "回纹陶罐是本演示场馆虚构的教学展品，不是真实出土文物。设定为红褐色陶质容器，高约二十厘米，口部略向外张，腹部较宽。罐肩有一圈连续转折的回纹。演示中把它用作储藏干燥谷物的容器。罐身保留手工压接的痕迹，可以观察从材料、成型到表面装饰的不同环节。没有真实年代、出土地或使用者资料。"),
    ("demo-bowl", "青釉小碗", "demo-daily", "陶瓷", "施釉", ("日常生活", "饮食"),
     "青釉小碗是虚构的教学展品，不对应真实馆藏。设定为口径约十二厘米的陶瓷碗，内外施浅青色釉，底部留有一圈未上釉的圈足。演示用途是盛放食物。观察重点是有釉表面与底足裸露胎体在颜色和光泽上的差别，以及碗口、碗腹、圈足如何组成稳定的形状。无具体窑口、年代、价格和出土地的真实资料。"),
    ("demo-mirror", "叶纹铜镜", "demo-craft", "青铜器", "铸造", ("日常生活", "纹饰", "制作工艺"),
     "叶纹铜镜是虚构的教学展品，不是真实古代铜镜。设定为直径约十五厘米的圆形金属镜模型，背面布置四片对称叶形纹，中心有穿绳的钮。正面作为照面，背面作为装饰面。展示旁有一个分开的模具模型，用来说明通过模具得到外形的演示步骤。它与回纹陶罐都可用于观察几何纹样如何重复排列，但两者的材料和成型设定不同。没有具体历史人物或出土信息。"),
    ("demo-joint", "榫卯拼接模型", "demo-craft", "木器", "榫卯", ("制作工艺", "结构", "日常生活"),
     "榫卯拼接模型是本演示场馆虚构的现代教学模型，由两个木质部件组成。一个部件有凸出的榫头，另一个部件有对应的卯眼，两者拼合后形成直角连接。展台同时摆有拆开和拼合两种状态，便于观察结构。模型没有使用金属钉；讲解只介绍这个模型的连接关系，不推断所有传统木构都没有钉子。无历史年代、建筑原址或工匠姓名的真实资料。"),
    ("demo-cloth", "交织纹布样", "demo-travel", "织物", "编织", ("日常生活", "制作工艺", "旅行"),
     "交织纹布样是虚构的现代教学样本，由两组方向互相垂直的线交错构成。演示设定使用米色与褐色两种线，放大模型用较粗线条呈现一上一下的交织方式。它在演示故事中被选作旅行包裹的外层布料。观察重点是整体平面如何由连续线条构成。没有纤维检测结果、真实年代、出土地或贸易路线资料，不能据此推断具体历史交流。"),
    ("demo-flask", "双耳行旅水壶", "demo-travel", "陶瓷", "手工成型", ("日常生活", "旅行", "携带"),
     "双耳行旅水壶是虚构的教学展品。设定为约十八厘米高的扁圆陶质容器，两侧有可穿绳的小耳，窄口配一个独立的木塞模型。演示用途是携带饮用水，参观者可比较它与宽口储藏罐在开口、体形和携带方式上的差别。这种比较只针对演示模型，不说明任何真实时代的普遍使用习惯。没有实际出土地、使用者或商路记录。"),
)


class DemoVectors:
    """同一确定性字符编码器供演示查询和馆藏使用；不是生产语义模型。"""
    @staticmethod
    def encode(text):
        vector = [0.0] * 128
        compact = "".join(text.split()) or "演示"
        for size in (1, 2):
            for i in range(max(0, len(compact) - size + 1)):
                token = compact[i:i + size].encode("utf-8")
                index = int.from_bytes(blake2b(token, digest_size=2).digest(), "big") % len(vector)
                vector[index] += 1
        norm = sqrt(sum(x * x for x in vector))
        return tuple(x / norm for x in vector)

    async def embed_query(self, text):
        return self.encode(text)


def demo_snapshot():
    exhibits = []
    for i, (relic_id, name, gallery, category, craft, themes, material) in enumerate(ITEMS):
        exhibits.append(Exhibit(
            relic_id, name, gallery,
            tags=ExhibitTags(category_main=(category,), craft_types=(craft,), themes=themes,
                             audience=("亲子", "儿童", "入门"), cultural_ip=("代表性展品",)),
            vector=DemoVectors.encode(name + "；" + category + "；" + craft + "；" + "；".join(themes)),
            importance_level=5 if i in (0, 2, 3) else 4,
            coords=(float(i % 2 + 1), 0),
            dwell_time_map={"quick": 2, "standard": 4, "deep": 6},
        ))
    nodes = [ENTRANCE, *(gallery.node_id for gallery in GALLERIES), EXIT]
    edges = [SpatialEdge(a, b, 1) for a, b in zip(nodes, nodes[1:])]
    return MuseumSnapshot(GALLERIES, exhibits, edges, {node: 1 for node in nodes})


async def demo_candidates(*, request):
    item = next((item for item in ITEMS if item[0] == request.relic_id), None)
    if item is None:
        raise ValueError("演示场馆中没有该展品")
    return (GuideMaterial(item[0] + "-material", item[6], ("demo-fictional-catalog:" + item[0],)),)


class DemoSummarySource:
    async def load_relics(self, relic_ids):
        by_id = {item[0]: item for item in ITEMS}
        galleries = {gallery.gallery_id: gallery.name for gallery in GALLERIES}
        return tuple(SummaryRelic(key, by_id[key][1], "虚构教学展品", galleries[by_id[key][2]]) for key in relic_ids)


def build_demo_services(*, model_call=None):
    # 演示馆仅六件展品，采用较宽松的示例筛选线；真实部署保留默认参数并校准。
    return build_model_services(
        model_call=model_call, snapshot=demo_snapshot(), candidates=demo_candidates,
        vector_service=DemoVectors(), summary_data_source=DemoSummarySource(),
        planner_config=PlannerConfig(score_threshold=25),
        taxonomy={"categories": ("陶瓷", "青铜器", "木器", "织物"),
                  "craft_types": ("手工成型", "施釉", "铸造", "榫卯", "编织"),
                  "themes": ("日常生活", "制作工艺", "旅行", "纹饰", "储藏", "饮食", "结构", "携带"),
                  "audience": ("亲子", "儿童", "入门"),
                  "cultural_ip": ("代表性展品",)},
    )


def museum_metadata():
    return {
        "mode": "demo", "name": "寻迹演示馆", "notice": NOTICE,
        "entrance_node_id": ENTRANCE, "exit_node_id": EXIT,
        "nodes": [{"node_id": ENTRANCE, "name": "入口大厅"},
                  *[{"node_id": g.node_id, "name": g.name} for g in GALLERIES],
                  {"node_id": EXIT, "name": "出口"}],
        "relics": [{"relic_id": item[0], "name": item[1], "node_id": item[2]} for item in ITEMS],
        "recommended_routes": [
            {"id": "daily", "title": "器物里的日常", "description": "从陶罐、小碗到水壶，看看日常生活如何影响器物的形状。", "relic_ids": ["demo-pot", "demo-bowl", "demo-flask"], "estimated_minutes": 20},
            {"id": "craft", "title": "材料变成器物", "description": "从陶瓷、金属到木头，观察成型、铸造和榫卯的制作工艺。", "relic_ids": ["demo-pot", "demo-mirror", "demo-joint"], "estimated_minutes": 25},
            {"id": "family", "title": "一起找找小巧思", "description": "适合亲子入门，一起观察器物的纹饰、结构和便于携带的设计。", "relic_ids": ["demo-mirror", "demo-joint", "demo-flask"], "estimated_minutes": 25},
        ],
    }
