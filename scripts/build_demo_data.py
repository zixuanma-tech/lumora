"""把队友整理的国博数据 Excel 转换为 demo 数据文件。

用法（本机 Anaconda）：
    D:/tools/anaconda/python.exe scripts/build_demo_data.py [xlsx路径]

默认读取 data/国博AI文旅数据收集_官网来源层_20261021.xlsx，覆盖写入：
    data/relics.json     60 件 demo 文物（40 核心 + 20 候选，02_候选文物60 全量）
    data/edges.json      45 条文物知识关联（04_关联关系45）
    data/galleries.json  4 个展览/展厅底座（00_官方展览底座）
    data/routes.json     8 条推荐主题路线（05_主题路线8）

转换口径（与 backend 命名对齐，六标签按队友口径）：
    年代         -> era_broad（09_标签词表·时代 9 档）+ era_specific（去括号、去"代/时期"后缀）
    材质         -> category_main
    制作工艺     -> craft_types
    器类/用途    -> artifact_types
    纹样/图像    -> motifs
    文化主题标签 -> themes
    占位值（待专家复核/待视觉核验/待复核/待产品侧定义主题/无明显图像/
    器物/图像资料）视为缺失：标签字段输出空列表，避免污染后续语义向量；
    标量字段（"·"、空单元格）直接省略键。
    知名程度三档：镇馆之宝 -> importance_level 5（触发镇馆待遇），核心 -> 4，候选 -> 3。
    镇馆之宝为 2026-10-21 按网络通行说法（维基"中国国家博物馆"重要馆藏等）
    与团队判断标注，定位是测试用近似口径，不保证与馆方结论一致。

依赖 pandas + openpyxl（仅离线构建使用，运行时后端仍只需标准库）。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_XLSX = PROJECT_ROOT / "data" / "国博AI文旅数据收集_官网来源层_20261021.xlsx"

# 09_标签词表·时代 顺序；specific -> broad 映射覆盖 60 件候选全部年代取值。
ERA_ORDER = ("新石器", "商周", "春秋战国", "秦汉", "魏晋南北朝", "隋唐", "宋辽金元", "明", "清")
ERA_BROAD = {
    "新石器": "新石器", "红山文化": "新石器", "肖家屋脊文化": "新石器",
    "商": "商周", "西周": "商周",
    "春秋": "春秋战国", "战国": "春秋战国",
    "秦": "秦汉", "汉": "秦汉", "西汉": "秦汉", "东汉": "秦汉",
    "三国": "魏晋南北朝", "西晋": "魏晋南北朝", "东晋": "魏晋南北朝",
    "北朝": "魏晋南北朝", "南朝": "魏晋南北朝", "南北朝": "魏晋南北朝",
    "隋": "隋唐", "唐": "隋唐", "五代": "隋唐", "十国": "隋唐",
    "宋": "宋辽金元", "北宋": "宋辽金元", "南宋": "宋辽金元",
    "辽": "宋辽金元", "金": "宋辽金元", "西夏": "宋辽金元", "元": "宋辽金元",
    "明": "明", "永乐": "明", "成化": "明",
    "清": "清", "康熙": "清", "乾隆": "清",
}

# 队伍约定里的"尚未确定"占位写法，一律视为缺失。
PLACEHOLDER_MARKS = (
    "待专家复核", "待视觉核验", "待复核", "待产品侧定义", "无明显图像", "器物/图像资料",
)

MISSING_SCALARS = {"", "·", "无", "nan", "N/A", "NA"}

# 镇馆之宝标注（测试用近似口径，2026-10-21 依据维基"中国国家博物馆"重要馆藏
# 名单与网络通行说法 + 团队判断选取；四个展厅各至少一件以覆盖规划测试）。
TREASURE_NOTES = {
    "NMC-YQ-101": "红山文化玦形龙，与'中华第一龙'红山碧玉C形龙同属红山玉龙家族名品",
    "NMC-CQ-107": "汝窑传世品不足百件，汝窑天青釉盘为陶瓷史名品",
    "NMC-CQ-111": "元青花存世稀少，青花云龙纹罐为元青花代表作之一",
    "NMC-TJ-004": "'中国大宁'铭文鎏金铜镜，因镜铭出现'中国'二字而知名",
    "NMC-TJ-010": "唐代高士宴乐纹嵌螺钿铜镜（花鸟人物螺钿青铜镜），维基列为国博重要馆藏",
    "NMC-TJ-011": "唐代羽人花鸟金银平脱铜镜（羽人飞凤花鸟纹金银平脱漆背铜镜），维基标注一级甲等文物",
    "NMC-FS-024": "康熙御用石青实地纱片金边单朝衣，御用服饰孤品，服饰展代表",
}


def cell(row: pd.Series, column: str) -> str | None:
    """读取单元格；空值与占位符 '·' 返回 None。"""
    raw = row.get(column)
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    return None if text in MISSING_SCALARS else text


def split_tags(text: str | None) -> list[str]:
    """拆标签列表：；、/ 分隔；'X等Y' 拆成 X 与 Y；占位值返回空列表。"""
    if text is None:
        return []
    if any(mark in text for mark in PLACEHOLDER_MARKS):
        return []
    tags: list[str] = []
    for part in re.split(r"[；;、/]", text):
        part = part.strip()
        if not part:
            continue
        grouped = re.match(r"^(.+?)等(.{2,})$", part)
        pieces = (grouped.group(1), grouped.group(2)) if grouped else (part,)
        for piece in pieces:
            if piece not in tags:
                tags.append(piece)
    return tags


def parse_era(text: str | None) -> tuple[list[str], list[str]]:
    """官方年代原文 -> (era_specific, era_broad)。具体值按空格分词并去掉代/时期后缀。"""
    if text is None:
        return [], []
    core = re.sub(r"（[^）]*）", "", text).strip()
    specifics: list[str] = []
    for token in core.split():
        token = re.sub(r"(时代|时期|代)$", "", token)
        if token and token not in specifics:
            specifics.append(token)
    broads: list[str] = []
    for token in specifics:
        broad = ERA_BROAD.get(token)
        if broad and broad not in broads:
            broads.append(broad)
    return specifics, sorted(broads, key=ERA_ORDER.index)


def parse_minutes(text: str | None) -> int | None:
    """'120-180分钟' -> 120（取下界，避免高估可看时间）。"""
    if text is None:
        return None
    found = re.search(r"\d+", text)
    return int(found.group()) if found else None


def zone_of(hall: str | None) -> str | None:
    if hall and hall[:1] in ("北", "南"):
        return hall[:1]
    return None


def build_relic(row: pd.Series, related: dict[str, list[str]]) -> dict:
    relic_id = cell(row, "artifact_id")
    specifics, broads = parse_era(cell(row, "年代"))
    intro = cell(row, "官方介绍/说明摘录")
    source_refs = [ref for ref in (cell(row, "来源URL"), cell(row, "页面定位/抽取编号")) if ref]
    item: dict = {
        "relic_id": relic_id,
        "name": cell(row, "官方名称"),
        "era": cell(row, "年代"),
        "gallery_id": cell(row, "exhibition_id"),
        "gallery_name": cell(row, "展览名称"),
        "hall": cell(row, "展厅"),
        "status": cell(row, "入选状态"),
        "demo_use": cell(row, "Demo用途"),
        "importance_level": 5 if relic_id in TREASURE_NOTES else (4 if cell(row, "入选状态") == "核心" else 3),
        "tags": {
            "era_broad": broads,
            "era_specific": specifics,
            "category_main": split_tags(cell(row, "材质")),
            "craft_types": split_tags(cell(row, "制作工艺")),
            "artifact_types": split_tags(cell(row, "器类/用途")),
            "motifs": split_tags(cell(row, "纹样/图像")),
            "themes": split_tags(cell(row, "文化主题标签")),
        },
    }
    if intro:
        item["materials"] = [{
            "material_id": f"{relic_id}-m1",
            "text": intro,
            "source_refs": source_refs,
            "related_relic_ids": related.get(relic_id, []),
        }]
    if relic_id in TREASURE_NOTES:
        item["treasure_note"] = TREASURE_NOTES[relic_id]
    optional_scalars = (
        ("unit", "单元"), ("location_status", "展厅内位置状态"), ("excavation", "出土/收藏信息"),
        ("source_url", "来源URL"), ("page_ref", "页面定位/抽取编号"), ("image_url", "图片URL"),
        ("image_note", "图片/版权备注"), ("credibility", "数据可信度"), ("source_note", "来源说明"),
        ("review_note", "人工复核建议"), ("route_value", "路线一句话价值"),
    )
    for key, column in optional_scalars:
        value = cell(row, column)
        if value:
            item[key] = value
    return item


def main() -> None:
    xlsx = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_XLSX
    relics_df = pd.read_excel(xlsx, sheet_name="02_候选文物60")
    edges_df = pd.read_excel(xlsx, sheet_name="04_关联关系45")
    galleries_df = pd.read_excel(xlsx, sheet_name="00_官方展览底座")
    routes_df = pd.read_excel(xlsx, sheet_name="05_主题路线8")
    readme = pd.read_excel(xlsx, sheet_name="README_使用说明", header=None)
    package_version = str(readme.iloc[1, 1])

    meta = {
        "source_file": xlsx.name,
        "package_version": package_version,
        "generated": date.today().isoformat(),
    }

    # ---- 关联关系：端点必须落在 60 件 demo 文物内才可用于讲解关联 ----
    demo_ids = set(relics_df["artifact_id"].astype(str))
    pool_ids = set(pd.read_excel(xlsx, sheet_name="03_候选池160")["artifact_id"].astype(str))
    edges: list[dict] = []
    related: dict[str, list[str]] = {}
    unusable: list[str] = []
    for _, row in edges_df.iterrows():
        source, target = cell(row, "起点artifact_id"), cell(row, "终点artifact_id")
        missing = [rid for rid in (source, target) if rid not in demo_ids]
        edge = {
            "relation_id": cell(row, "relation_id"),
            "source_relic_id": source,
            "target_relic_id": target,
            "relation_type": cell(row, "关系类型"),
            "scenario": cell(row, "用户问题/路线场景"),
            "evidence_summary": cell(row, "证据摘要"),
            "source_refs": [part.strip() for part in str(cell(row, "证据来源") or "").split("；") if part.strip()],
            "confidence": cell(row, "置信度"),
            "demo_priority": cell(row, "入Demo优先级"),
            "available_in_demo": not missing,
            "review_status": cell(row, "核验状态"),
        }
        if cell(row, "备注"):
            edge["note"] = cell(row, "备注")
        if missing:
            unusable.append(f"{edge['relation_id']} 缺 {missing}"
                            f"（其中在候选池内：{[rid for rid in missing if rid in pool_ids]}）")
        else:
            related.setdefault(source, [])
            if target not in related[source]:
                related[source].append(target)
            related.setdefault(target, [])
            if source not in related[target]:
                related[target].append(source)
        edges.append(edge)

    relics = [build_relic(row, related) for _, row in relics_df.iterrows()]

    galleries = []
    for _, row in galleries_df.iterrows():
        hall = cell(row, "展厅")
        gallery = {
            "gallery_id": cell(row, "exhibition_id"),
            "name": cell(row, "展览名称"),
            "hall": hall,
            "zone": zone_of(hall),
            "exhibition_type": cell(row, "展览类型"),
            "status": cell(row, "展期/状态"),
            "scale": cell(row, "展出规模"),
            "units": [part.strip() for part in str(cell(row, "官方单元/部分") or "").split("；") if part.strip()],
            "intro": cell(row, "官方简介整理"),
            "detail_url": cell(row, "官网详情页"),
            "portal_url": cell(row, "专题页"),
            "source_level": cell(row, "来源等级"),
            "data_status": cell(row, "数据状态"),
        }
        if cell(row, "备注"):
            gallery["note"] = cell(row, "备注")
        galleries.append(gallery)

    routes = []
    for _, row in routes_df.iterrows():
        relic_ids = [part.strip().upper() for part in re.split(r"[;；]", str(cell(row, "主要artifact_id") or "")) if part.strip()]
        dropped = [rid for rid in relic_ids if rid not in demo_ids]
        route = {
            "id": cell(row, "route_id"),
            "title": cell(row, "路线名"),
            "description": cell(row, "叙事逻辑"),
            "relic_ids": [rid for rid in relic_ids if rid not in dropped],
            "estimated_minutes": parse_minutes(cell(row, "目标时长")),
            "user_question": cell(row, "用户原话/问题"),
            "duration_text": cell(row, "目标时长"),
            "audience": cell(row, "目标人群"),
            "halls": [part.strip() for part in str(cell(row, "涉及展厅") or "").split("；") if part.strip()],
            "constraints": cell(row, "硬约束/待核验"),
            "demo_value": cell(row, "Demo价值"),
            "priority": cell(row, "优先级"),
        }
        if dropped:
            route["excluded_relic_ids"] = dropped
        routes.append(route)

    outputs = {
        "relics.json": {"meta": {**meta, "note": "仅收录 02_候选文物60（40 核心 + 20 候选）；候选池160 其余条目无六标签，保留在 Excel 中作参考。"}, "relics": relics},
        "edges.json": {"meta": meta, "edges": edges},
        "galleries.json": {"meta": meta, "galleries": galleries},
        "routes.json": {"meta": meta, "recommended_routes": routes},
    }
    for name, payload in outputs.items():
        target = PROJECT_ROOT / "data" / name
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"写入 {target.relative_to(PROJECT_ROOT)}")

    # ---- 校验报告 ----
    print("\n== 校验报告 ==")
    treasures = {r["relic_id"] for r in relics if r["importance_level"] == 5}
    print(f"文物 {len(relics)} 件（镇馆之宝 {len(treasures)}，核心 {sum(r['importance_level'] == 4 for r in relics)}，"
          f"候选 {sum(r['importance_level'] == 3 for r in relics)}），"
          f"ID 唯一：{len({r['relic_id'] for r in relics}) == len(relics)}")
    by_gallery: dict[str, list[str]] = {}
    for r in relics:
        if r["relic_id"] in treasures:
            by_gallery.setdefault(r["gallery_name"], []).append(r["name"])
    for gallery, names in by_gallery.items():
        print(f"  镇馆之宝[{gallery}]: {'、'.join(names)}")
    print(f"展厅 {len(galleries)} 个；文物引用的展厅均在底座内："
          f"{all(r['gallery_id'] in {g['gallery_id'] for g in galleries} for r in relics)}")
    print(f"关联 {len(edges)} 条，其中 {len(unusable)} 条端点不在 60 件内（available_in_demo=false）：")
    for line in unusable:
        print(f"  - {line}")
    no_material = [r["relic_id"] for r in relics if not r.get("materials")]
    print(f"无官方介绍（无讲解材料）：{no_material or '无'}")
    no_broad = [f"{r['relic_id']}:{r['era']}" for r in relics if not r["tags"]["era_broad"]]
    print(f"年代未映射到词表九档（era_broad 为空）：{no_broad or '无'}")
    for field, column in (("craft_types", "制作工艺"), ("artifact_types", "器类/用途"),
                          ("motifs", "纹样/图像"), ("themes", "文化主题标签")):
        empty = sum(1 for r in relics if not r["tags"][field])
        print(f"六标签缺失统计 [{column}] -> {field} 空列表：{empty} 件")
    bad_routes = [(r["id"], r.get("excluded_relic_ids")) for r in routes if r.get("excluded_relic_ids")]
    print(f"路线 {len(routes)} 条；引用缺失文物的路线：{bad_routes or '无'}")


if __name__ == "__main__":
    main()
