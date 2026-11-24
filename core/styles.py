# -*- coding: utf-8 -*-
"""风格库：内置 30 种风格 + 用户自定义风格 + 提示词关键词自动识别。

风格只负责“提示词模板 + 默认比例 + 负面词”，不绑定服务商，
因此同一风格可以在 OpenAI / Gemini / DashScope 之间任意切换。

用户可在插件数据目录放置 ``styles.json`` 扩展或覆盖风格（见 README）：
    {"styles": {"my": {"name": "我的风格", "prefix": "...", "ratio": "1:1"}},
     "override": {"guofeng": {"prefix": "..."}},
     "disable": ["ukiyoe"]}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from astrbot.api import logger


@dataclass(frozen=True)
class Style:
    id: str
    name: str
    category: str
    summary: str
    prefix: str = ""
    suffix: str = ""
    negative: str = ""
    ratio: str = ""
    aliases: tuple[str, ...] = ()


# 负面词通用后缀：几乎所有风格都怕这些
_BASE_NEG = "低清晰度，模糊，畸形，多余肢体，水印，签名，乱码文字"

BUILTIN_STYLES: tuple[Style, ...] = (
    Style(
        id="general",
        name="通用插画",
        category="通用",
        summary="万金油：构图完整、光影自然的通用插画",
        prefix="高质量插画，构图完整，主体明确，光影自然，细节丰富",
        suffix="整体风格统一，画面干净",
        negative=_BASE_NEG,
    ),
    Style(
        id="anime",
        name="二次元",
        category="二次元",
        summary="日系赛璐璐动画质感",
        prefix="日系二次元插画，精致线稿，赛璐璐上色，干净通透的色彩",
        suffix="流行动画质感，人物比例准确",
        negative="写实照片，3D渲染，崩坏的脸，多余手指，" + _BASE_NEG,
        aliases=("动漫", "日系", "二次元插画"),
    ),
    Style(
        id="chibi",
        name="Q版萌系",
        category="二次元",
        summary="大头小身、圆润可爱的 Q 版",
        prefix="Q版可爱卡通风格，大头小身比例，圆润造型，明亮柔和的配色",
        suffix="萌系表情，简洁背景",
        negative="写实，恐怖，尖锐线条，血腥",
        ratio="1:1",
        aliases=("q版", "萌系", "可爱卡通风"),
    ),
    Style(
        id="manga",
        name="黑白漫画",
        category="二次元",
        summary="网点阴影与钢笔线条的漫画分镜",
        prefix="黑白漫画风格，网点阴影，强烈的明暗对比，干净的钢笔线条",
        suffix="分镜感构图，纸质纹理",
        negative="彩色，写实照片，模糊线条",
        ratio="3:4",
        aliases=("漫画", "黑白漫画风"),
    ),
    Style(
        id="pixel",
        name="像素风",
        category="风格化",
        summary="16-bit 复古游戏像素画面",
        prefix="像素艺术风格，16-bit 复古游戏画面，清晰的像素颗粒，有限色板",
        suffix="复古游戏质感",
        negative="写实照片，高分辨率平滑渐变，模糊",
        ratio="1:1",
        aliases=("像素", "像素画", "像素风"),
    ),
    Style(
        id="guofeng",
        name="国风水墨",
        category="国风",
        summary="宣纸留白、墨色浓淡的写意水墨",
        prefix="中国水墨画风格，宣纸质感，留白意境，墨色浓淡变化自然",
        suffix="淡雅设色，传统题跋布局，气韵生动",
        negative="照片，3D渲染，夸张透视，油腻高光",
        ratio="3:4",
        aliases=("水墨", "国风", "水墨画", "写意"),
    ),
    Style(
        id="guochao",
        name="国潮插画",
        category="国风",
        summary="传统纹样 + 现代平面设计",
        prefix="国潮插画风格，传统纹样与现代平面设计结合，红金配色，对称构图",
        suffix="潮流感强，画面有张力",
        negative="陈旧感，模糊，" + _BASE_NEG,
        ratio="3:4",
        aliases=("国潮", "新中式"),
    ),
    Style(
        id="ukiyoe",
        name="浮世绘",
        category="国风",
        summary="木版画平涂与日式古典构图",
        prefix="浮世绘风格，木版画质感，平涂色块，清晰的轮廓线，传统日式构图",
        suffix="古典雅致，色调温润",
        negative="照片，3D，现代元素，" + _BASE_NEG,
        ratio="3:4",
        aliases=("浮世绘风格",),
    ),
    Style(
        id="photoreal",
        name="写实摄影",
        category="摄影",
        summary="超写实、真实光影的照片感",
        prefix="超写实摄影，真实自然的光影，高细节质感，35mm 镜头，自然景深",
        suffix="照片级真实感",
        negative="插画，卡通，塑料感，过度磨皮，变形",
        ratio="3:2",
        aliases=("写实", "摄影", "真实照片", "电影感"),
    ),
    Style(
        id="portrait",
        name="人像写真",
        category="摄影",
        summary="柔和布光的写实人像",
        prefix="写实人像摄影，柔和布光，浅景深，真实皮肤质感，自然表情",
        suffix="影棚级质感",
        negative="畸形五官，多余手指，塑料脸，油画感",
        ratio="2:3",
        aliases=("人像", "写真", "半身像"),
    ),
    Style(
        id="food",
        name="美食摄影",
        category="摄影",
        summary="暖光浅景深的诱人美食照",
        prefix="美食摄影，暖色光线，浅景深，诱人的食物质感，精致摆盘",
        suffix="杂志级美食大片",
        negative="生肉感，脏乱背景，塑料感",
        ratio="4:3",
        aliases=("美食", "菜品摄影", "食物"),
    ),
    Style(
        id="product",
        name="产品图",
        category="摄影",
        summary="干净背景的电商产品棚拍",
        prefix="产品商业摄影，干净简洁的背景，柔和反光，高级材质质感，棚拍布光",
        suffix="电商主图风格，主体突出",
        negative="杂乱背景，过曝，变形，" + _BASE_NEG,
        ratio="1:1",
        aliases=("产品", "商品图", "电商图"),
    ),
    Style(
        id="oil",
        name="油画",
        category="绘画",
        summary="厚涂笔触与画布肌理",
        prefix="古典油画风格，厚涂笔触，丰富的色彩层次，可见的画布纹理",
        suffix="光影层次厚重，质感饱满",
        negative="扁平色块，像素感，照片",
        ratio="4:3",
        aliases=("油画风", "厚涂"),
    ),
    Style(
        id="watercolor",
        name="水彩",
        category="绘画",
        summary="透明叠色、湿画法晕染",
        prefix="水彩画风格，透明叠色，湿画法晕染，自然的水痕，清晰的纸张纹理",
        suffix="轻盈通透",
        negative="油画厚重感，3D渲染，塑料高光",
        aliases=("水彩画", "水彩风"),
    ),
    Style(
        id="sketch",
        name="素描线稿",
        category="绘画",
        summary="铅笔排线与黑白线稿",
        prefix="铅笔素描风格，黑白线稿，细腻的明暗排线，纸张颗粒质感",
        suffix="手绘草稿感",
        negative="彩色，3D渲染，照片，粗劣线条",
        aliases=("素描", "线稿", "速写", "铅笔稿"),
    ),
    Style(
        id="children",
        name="儿童画",
        category="绘画",
        summary="稚拙可爱的儿童蜡笔/水彩",
        prefix="儿童绘本风格，稚拙温暖的手绘笔触，明亮柔和的色彩，简单可爱的造型",
        suffix="童趣自然",
        negative="恐怖，写实，复杂阴暗",
        aliases=("儿童插画", "童趣"),
    ),
    Style(
        id="cyberpunk",
        name="赛博朋克",
        category="风格化",
        summary="霓虹雨夜与未来科技感",
        prefix="赛博朋克风格，霓虹灯光，雨夜城市，高对比色彩，未来科技感",
        suffix="电影级光影，体积雾",
        negative="田园风，明亮平淡，" + _BASE_NEG,
        ratio="16:9",
        aliases=("赛博", "赛博朋克风"),
    ),
    Style(
        id="vaporwave",
        name="蒸汽波",
        category="风格化",
        summary="粉紫渐变与复古电脑元素",
        prefix="蒸汽波风格，粉紫渐变，复古电脑界面元素，网格地平线，梦幻光晕",
        suffix="80 年代复古未来感",
        negative="写实照片，自然风景",
        ratio="16:9",
        aliases=("蒸汽波风格",),
    ),
    Style(
        id="neon",
        name="霓虹灯管",
        category="风格化",
        summary="深色背景上的发光线条",
        prefix="霓虹灯管艺术风格，发光线条，深色背景，强烈的色彩对比，柔和光晕",
        suffix="夜景氛围",
        negative="日常白天光线，模糊，" + _BASE_NEG,
        ratio="16:9",
        aliases=("霓虹",),
    ),
    Style(
        id="steampunk",
        name="蒸汽朋克",
        category="风格化",
        summary="黄铜齿轮与复古机械",
        prefix="蒸汽朋克风格，黄铜齿轮，复古机械结构，暖黄色调，做旧金属质感",
        suffix="维多利亚时代科幻感",
        negative="现代电子产品，极简，" + _BASE_NEG,
        aliases=("蒸汽朋克风",),
    ),
    Style(
        id="dark",
        name="暗黑奇幻",
        category="风格化",
        summary="强烈明暗与神秘史诗氛围",
        prefix="暗黑奇幻风格，强烈的明暗对比，神秘氛围，史诗级构图，冷暖对撞的光色",
        suffix="电影海报质感",
        negative="明亮卡通，平淡日常，" + _BASE_NEG,
        ratio="3:4",
        aliases=("暗黑", "暗黑风"),
    ),
    Style(
        id="storybook",
        name="绘本童话",
        category="插画",
        summary="温暖手绘的童话绘本",
        prefix="儿童绘本插画，温暖的手绘质感，柔和梦幻的配色，童话氛围",
        suffix="治愈系画面",
        negative="恐怖，写实照片，尖锐线条",
        aliases=("绘本", "童话"),
    ),
    Style(
        id="flat",
        name="扁平插画",
        category="设计",
        summary="几何色块的现代扁平风",
        prefix="扁平矢量插画，简洁的几何形状，纯色块，现代 UI 插画风格",
        suffix="配色克制，层次清晰",
        negative="写实照片，复杂纹理，" + _BASE_NEG,
        aliases=("扁平风", "矢量插画"),
    ),
    Style(
        id="minimal",
        name="极简",
        category="设计",
        summary="大留白、单一主体",
        prefix="极简主义设计，大量留白，单一主体，柔和的渐变背景，克制的配色",
        suffix="高级感",
        negative="杂乱，过多元素，复杂纹理",
        ratio="1:1",
        aliases=("极简风", "简约"),
    ),
    Style(
        id="poster",
        name="海报设计",
        category="设计",
        summary="强主体 + 版式留白的平面海报",
        prefix="平面海报设计，强烈的视觉主体，大胆留白，现代版式，主标题排版",
        suffix="印刷级配色",
        negative="杂乱排版，错别字，模糊，" + _BASE_NEG,
        ratio="3:4",
        aliases=("海报", "宣传海报"),
    ),
    Style(
        id="infographic",
        name="信息图",
        category="设计",
        summary="分区清晰的知识图/流程图",
        prefix="信息图设计，清晰的结构分区，图标与数据可视化，整洁的中文标注排版，阅读动线明确",
        suffix="教育科普风格",
        negative="文字错乱，排版拥挤，乱码",
        ratio="3:4",
        aliases=("信息图设计", "知识图", "图解"),
    ),
    Style(
        id="logo",
        name="标志图标",
        category="设计",
        summary="极简图形符号、无文字",
        prefix="标志设计，极简图形符号，矢量风格，单一主体，纯色背景，不含文字",
        suffix="专业品牌感",
        negative="文字，复杂背景，照片，多主体",
        ratio="1:1",
        aliases=("标志", "图标", "logo设计"),
    ),
    Style(
        id="emoji",
        name="表情包",
        category="设计",
        summary="白底夸张表情，适合群聊",
        prefix="表情包风格，夸张生动的表情，简洁描边，白色背景，可爱搞笑",
        suffix="适合聊天传播",
        negative="复杂背景，写实照片，模糊",
        ratio="1:1",
        aliases=("表情", "贴纸"),
    ),
    Style(
        id="render3d",
        name="3D渲染",
        category="3D/潮玩",
        summary="柔和环境光的 C4D 质感",
        prefix="3D 渲染风格，C4D 质感，柔和环境光，次表面散射，精致材质与倒角",
        suffix="干净的高级感",
        negative="手绘线条，扁平色块，" + _BASE_NEG,
        ratio="1:1",
        aliases=("3d", "c4d", "三维渲染"),
    ),
    Style(
        id="clay",
        name="黏土手办",
        category="3D/潮玩",
        summary="软陶黏土质感的潮玩",
        prefix="黏土手办风格，软陶黏土材质，微距摄影质感，可爱立体造型，柔和棚拍光",
        suffix="潮玩盲盒质感",
        negative="扁平插画，写实照片，尖锐边缘",
        ratio="1:1",
        aliases=("黏土", "黏土风", "手办"),
    ),
    Style(
        id="wallpaper",
        name="风景壁纸",
        category="场景",
        summary="电影级宽幅风景",
        prefix="宏大风景壁纸，电影级构图，丰富的氛围光，超广角视野，高细节远景",
        suffix="适合做桌面壁纸",
        negative="杂乱前景，模糊，" + _BASE_NEG,
        ratio="16:9",
        aliases=("壁纸", "风景"),
    ),
    Style(
        id="concept",
        name="概念设定",
        category="场景",
        summary="角色/场景设定集风格",
        prefix="概念设定图，角色与场景设计，精细的结构刻画，氛围光照，设定集排版感",
        suffix="专业原画水准",
        negative="低细节，比例失调，" + _BASE_NEG,
        ratio="16:9",
        aliases=("概念图", "设定图", "原画"),
    ),
    Style(
        id="game",
        name="游戏场景",
        category="场景",
        summary="史诗感游戏场景原画",
        prefix="游戏场景原画，史诗感构图，体积光，丰富的环境细节，电影级氛围",
        suffix="3A 游戏美术风格",
        negative="低多边形感，手机游戏卡通感，模糊",
        ratio="16:9",
        aliases=("游戏原画", "场景原画"),
    ),
)

# 关键词 → 风格。顺序即优先级：更具体的风格排在前面。
AUTO_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("guochao", ("国潮", "新中式")),
    ("guofeng", ("水墨", "国风", "山水画", "工笔", "写意", "宣纸", "青绿山水")),
    ("ukiyoe", ("浮世绘",)),
    ("chibi", ("q版", "萌系", "大头小身")),
    ("anime", ("二次元", "动漫", "日系", "番剧", "赛璐璐", "立绘")),
    ("manga", ("黑白漫画", "漫画分镜", "漫画风")),
    ("pixel", ("像素", "8bit", "8-bit", "像素风")),
    ("oil", ("油画", "厚涂")),
    ("watercolor", ("水彩", "水粉")),
    ("sketch", ("素描", "线稿", "速写", "铅笔")),
    ("photoreal", ("写实", "摄影", "照片感", "真实照片", "电影感", "镜头")),
    ("portrait", ("人像", "写真", "半身像", "证件照")),
    ("food", ("美食", "菜品", "食物", "甜品")),
    ("product", ("产品图", "商品图", "电商主图")),
    ("poster", ("海报",)),
    ("infographic", ("信息图", "知识图", "图解", "流程图")),
    ("emoji", ("表情包", "贴纸")),
    ("logo", ("标志", "图标", "logo")),
    ("wallpaper", ("壁纸", "风景照")),
    ("concept", ("概念图", "设定图", "原画", "概念设计")),
    ("game", ("游戏场景", "游戏原画")),
    ("cyberpunk", ("赛博朋克", "赛博", "义体", "霓虹都市")),
    ("vaporwave", ("蒸汽波",)),
    ("neon", ("霓虹",)),
    ("steampunk", ("蒸汽朋克",)),
    ("dark", ("暗黑", "哥特", "克苏鲁")),
    ("storybook", ("绘本", "童话")),
    ("children", ("儿童画", "蜡笔", "童趣")),
    ("clay", ("黏土", "手办", "盲盒")),
    ("render3d", ("3d", "3d渲染", "c4d", "立体渲染")),
    ("flat", ("扁平", "矢量插画", "ui插画")),
    ("minimal", ("极简", "简约")),
)

_NAME_SUFFIXES = ("风格", "画风", "风")
_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(
    r"^[\s,，、:：;；.。!！?？\"'“”‘’]+|[\s,，、:：;；.。!！?？\"'“”‘’]+$"
)


def normalize(text: str) -> str:
    """把风格名归一化：去空白、转小写、去尾部“风格/画风/风”。"""
    if not text:
        return ""
    out = _SPACE_RE.sub("", str(text)).strip().lower()
    out = out.replace("（", "(").replace("）", ")")
    for suffix in _NAME_SUFFIXES:
        if len(out) > len(suffix) and out.endswith(suffix):
            out = out[: -len(suffix)]
            break
    return out


class StyleBook:
    """风格索引：精确匹配 → 子串匹配 → 关键词自动识别。"""

    def __init__(self, user_file: Path | None = None):
        self.styles: dict[str, Style] = {s.id: s for s in BUILTIN_STYLES}
        self._alias_index: dict[str, str] = {}
        self.user_file = Path(user_file) if user_file else None
        self.load_notes: list[str] = []
        if self.user_file:
            self.load_user_file(self.user_file)
        self._rebuild_index()

    # ---------- 索引 ----------

    def _rebuild_index(self) -> None:
        index: dict[str, str] = {}
        for style in self.styles.values():
            keys = [style.id, style.name, *style.aliases]
            for key in keys:
                norm = normalize(key)
                if norm and norm not in index:
                    index[norm] = style.id
        self._alias_index = index

    def ids(self) -> list[str]:
        return list(self.styles.keys())

    def get(self, style_id: str, default: str = "general") -> Style:
        return (
            self.styles.get(style_id) or self.styles.get(default) or BUILTIN_STYLES[0]
        )

    def resolve(self, name: str) -> Style | None:
        """把用户输入解析成风格：先精确，再最长子串。"""
        norm = normalize(name)
        if not norm:
            return None
        if norm in self._alias_index:
            return self.styles[self._alias_index[norm]]
        # “用国风水墨”“来个赛博朋克风格”这类输入：取最长的已注册子串
        best_key = ""
        for key in self._alias_index:
            if len(key) >= 2 and key in norm and len(key) > len(best_key):
                best_key = key
        if best_key:
            return self.styles[self._alias_index[best_key]]
        return None

    def resolve_token(self, token: str) -> Style | None:
        """只做整词匹配：避免把“水墨画里的山”整句误吞成风格。"""
        if not token:
            return None
        norm = normalize(token)
        if not norm:
            return None
        return self.styles.get(self._alias_index.get(norm, ""))

    def detect(self, text: str) -> Style | None:
        """根据提示词里的关键词推断风格。"""
        lowered = (text or "").lower()
        if not lowered:
            return None
        for style_id, keywords in AUTO_RULES:
            style = self.styles.get(style_id)
            if not style:
                continue
            for keyword in keywords:
                if keyword.lower() in lowered:
                    return style
        return None

    def grouped(self) -> dict[str, list[Style]]:
        groups: dict[str, list[Style]] = {}
        for style in self.styles.values():
            groups.setdefault(style.category, []).append(style)
        return groups

    # ---------- 用户自定义 ----------

    def load_user_file(self, path: Path) -> None:
        """读取用户 styles.json；任何格式问题只记录、不影响内置风格。"""
        self.load_notes = []
        path = Path(path)
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.load_notes.append(f"styles.json 解析失败：{exc}")
            logger.warning(f"[imggen] 用户风格文件解析失败，使用内置风格：{exc!r}")
            return
        if not isinstance(raw, dict):
            self.load_notes.append("styles.json 顶层必须是对象")
            return

        payload = raw.get("styles")
        if not isinstance(payload, dict):
            # 兼容“直接写 id → 定义”的简写
            payload = {
                k: v
                for k, v in raw.items()
                if k not in ("styles", "override", "disable") and isinstance(v, dict)
            }

        for style_id, item in payload.items():
            try:
                style = self._build_style(str(style_id), item)
            except Exception as exc:  # noqa: BLE001
                self.load_notes.append(f"风格 {style_id} 无效：{exc}")
                continue
            if style:
                self.styles[style.id] = style
                self.load_notes.append(f"已加载自定义风格：{style.name}")

        disable = raw.get("disable")
        if isinstance(disable, list):
            for style_id in disable:
                if isinstance(style_id, str) and self.styles.pop(style_id, None):
                    self.load_notes.append(f"已停用风格：{style_id}")

        override = raw.get("override")
        if isinstance(override, dict):
            for style_id, patch in override.items():
                base = self.styles.get(style_id)
                if not base or not isinstance(patch, dict):
                    continue
                allowed = {
                    k: v for k, v in patch.items() if k in Style.__dataclass_fields__
                }
                try:
                    self.styles[style_id] = replace(base, **allowed)
                    self.load_notes.append(f"已覆盖风格：{style_id}")
                except Exception as exc:  # noqa: BLE001
                    self.load_notes.append(f"覆盖风格 {style_id} 失败：{exc}")

        self._rebuild_index()

    @staticmethod
    def _build_style(style_id: str, item) -> Style | None:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or style_id).strip() or style_id
        aliases = item.get("aliases") or ()
        if isinstance(aliases, str):
            aliases = [a.strip() for a in re.split(r"[,，]", aliases) if a.strip()]
        else:
            aliases = tuple(str(a).strip() for a in aliases if str(a).strip())
        return Style(
            id=style_id.strip(),
            name=name,
            category=str(item.get("category") or "自定义").strip() or "自定义",
            summary=str(item.get("summary") or "用户自定义风格").strip(),
            prefix=str(item.get("prefix") or "").strip(),
            suffix=str(item.get("suffix") or "").strip(),
            negative=str(item.get("negative") or "").strip(),
            ratio=str(item.get("ratio") or "").strip(),
            aliases=tuple(aliases),
        )
