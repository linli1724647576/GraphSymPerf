"""Rebuild editable method SVGs with only the Python standard library.

The figures explain constructed examples, not measured performance results.
SVG text remains text; no external fonts, raster images or web assets are used.
"""

from html import escape
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
INK = "#15324F"
BLUE = "#2B6CB0"
ORANGE = "#C76B24"
GREEN = "#2D7A64"
MUTED = "#52677C"
LINE = "#D8E2EB"
FONT = "'Noto Sans CJK SC','PingFang SC','Microsoft YaHei',Arial,sans-serif"


class Figure:
    def __init__(self, width, height, title, description):
        self.width, self.height = width, height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                      f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
                      f'<title id="title">{escape(title)}</title>',
                      f'<desc id="desc">{escape(description)}</desc>',
                      '<defs>',
                      '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
                      'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
                      f'<path d="M0 0L10 5L0 10Z" fill="{MUTED}"/></marker>',
                      '<marker id="work-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
                      'markerUnits="userSpaceOnUse" markerWidth="14" markerHeight="14" orient="auto">'
                      f'<path d="M0 0L10 5L0 10Z" fill="{ORANGE}"/></marker>',
                      '</defs>',
                      f'<rect width="{width}" height="{height}" fill="white"/>']

    def text(self, x, y, value, size=26, color=INK, weight=400, anchor="start"):
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
                          f'font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{escape(value)}</text>')

    def lines(self, x, y, values, size=26, gap=43, color=INK, weight=400):
        for index, value in enumerate(values):
            self.text(x, y + index * gap, value, size, color, weight)

    def box(self, x, y, width, height, fill="white", stroke=LINE, radius=18, dash=None):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" '
                          f'fill="{fill}" stroke="{stroke}" stroke-width="2"'
                          + (f' stroke-dasharray="{dash}"' if dash else '') + '/>')

    def path(self, value, color=MUTED, width=3, arrow=False, dash=None):
        self.parts.append(f'<path d="{value}" fill="none" stroke="{color}" stroke-width="{width}" '
                          'stroke-linecap="round" stroke-linejoin="round"'
                          + (f' marker-end="url(#{arrow if isinstance(arrow, str) else "arrow"})"' if arrow else '')
                          + (f' stroke-dasharray="{dash}"' if dash else '') + '/>')

    def circle(self, x, y, label, color=BLUE, fill="white", radius=25):
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{fill}" '
                          f'stroke="{color}" stroke-width="3"/>')
        self.text(x, y + 9, label, 25, color, 600, "middle")

    def write(self, name):
        value = '\n'.join([*self.parts, '</svg>']) + '\n'
        ElementTree.fromstring(value)
        path = ROOT / name
        path.write_text(value, encoding="utf-8")
        print(f'{path.name}: {self.width} × {self.height}; XML valid')


def stage(fig, x, y, index, title, lines, output, color):
    fig.box(x, y, 450, 265, fill="#FCFDFE")
    fig.box(x + 22, y + 20, 59, 40, fill=color, stroke=color, radius=10)
    fig.text(x + 51, y + 48, f'M{index}', 25, "white", 700, "middle")
    fig.text(x + 98, y + 48, title, 27, INK, 650)
    fig.lines(x + 28, y + 102, lines, size=26, gap=40, color=MUTED)
    fig.path(f'M{x + 25} {y + 205}H{x + 425}', color=LINE, width=2)
    fig.text(x + 28, y + 241, output, 28, color, 650)


def overview():
    fig = Figure(1600, 1040, "GraphSymPerf 六阶段方法总览",
                 "先独立生成抽象查询结构并嵌入局部测试模式，再联合构图、实例化和单点变异，最后分别检查结果关系与逻辑求值需求。")
    fig.text(65, 63, "从随机 AST 到结果保持的测试查询对", 42, INK, 700)
    fig.text(65, 105, "结构先固定，图与终端联合确定；同一图上只改变一个目标位点", 25, MUTED)
    stage(fig, 65, 150, 1, "基于 AST 的随机查询生成",
          ["选择查询骨架与匹配形状", "按期望值类型逐层展开", "保留一个带类型的插入口"], "A₀：抽象查询结构", BLUE)
    stage(fig, 575, 150, 2, "嵌入测试模式（Pattern）",
          ["核对局部规则的输入与输出", "固定源结构及变异备选", "标记唯一变异位点"], "A：包含目标的完整结构", BLUE)
    stage(fig, 1085, 150, 3, "构造模式感知图状态",
          ["合并局部前提与外层约束", "联合确定图、终端与见证", "保留可核查的构造事实"], "(G, β, I)：图、绑定、事实", GREEN)
    fig.path('M525 282H564', arrow=True)
    fig.path('M1035 282H1074', arrow=True)
    fig.path('M1310 427V460H290V516', arrow=True)
    fig.box(525, 438, 570, 44, fill="white", stroke="white", radius=0)
    fig.text(810, 469, "共同图状态与联合绑定传递至后续阶段", 24, GREEN, 600, "middle")
    stage(fig, 65, 535, 4, "查询实例化",
          ["按联合绑定填充整棵 AST", "保持结构与变量连接一致", "不再随机选择表达式"], "Q₁：完整原查询", BLUE)
    stage(fig, 575, 535, 5, "按 Pattern 变异",
          ["在同一图状态上局部变换", "对称：保持允许的观察", "短路：替换受保护的工作"], "(Q₁, Q₂)：结果保持的查询对", ORANGE)
    stage(fig, 1085, 535, 6, "依赖检查与传播分析",
          ["沿完整局部边界向外传播", "结果关系：逐层保持相等", "逻辑需求：检查屏蔽与重复"], "Δ：结果关系与需求报告", GREEN)
    fig.path('M525 667H564', arrow=True)
    fig.path('M1035 667H1074', arrow=True)
    fig.box(65, 855, 960, 101, fill="#F2F8F5", stroke="#BED8CD")
    fig.text(93, 892, "接受用例", 27, GREEN, 700)
    fig.text(93, 932, "共同图 G ＋ 原查询 Q₁ ＋ 变异查询 Q₂ ＋ 依赖报告 Δ", 25, INK)
    fig.box(1085, 855, 450, 101, fill="#FFF8F0", stroke="#E7C6A5", dash="8 5")
    fig.text(1113, 892, "任一阶段失败：记录原因", 25, ORANGE, 650)
    fig.text(1113, 932, "在预算内从 M1 重新采样", 25, INK)
    fig.text(65, 1007, "β：全查询终端的联合绑定；I：已核查图事实。实际性能及物理执行由数据库验证。", 23, MUTED)
    fig.write('ast-pattern-six-stage-overview.svg')


def headings(fig, y):
    for x, title, subtitle in [(65, "随机结构与局部规则", "M1–M2"),
                               (590, "模式感知图状态", "M3"),
                               (1135, "查询对与外层观察", "M4–M6")]:
        fig.text(x + 25, y, title, 27, INK, 650)
        fig.text(x + 25, y + 35, subtitle, 22, MUTED)


def examples():
    fig = Figure(1600, 1480, "P3 与 S7 两个贯穿示例及外层屏蔽对照",
                 "P3通过失败候选扩大路径搜索机会并反转无向路径，计数保持为一；S7通过非空前缀保护不同回退表达式，返回相同多重集；最后对照整个目标值被外层忽略的情况。")
    fig.text(65, 62, "两个例子贯穿六阶段：路径反转与短路替换", 40, INK, 700)
    fig.text(65, 103, "图规模与名称仅作方法示例；蓝色表示结构，橙色表示被排除或受保护的潜在工作", 23, MUTED)
    fig.box(65, 135, 1470, 475, fill="#FCFDFE")
    fig.text(92, 179, "例 A｜无向路径反转（P3）", 31, BLUE, 700)
    headings(fig, 227)
    fig.path('M560 215V574', color=LINE, width=2)
    fig.path('M1105 215V574', color=LINE, width=2)
    fig.box(105, 290, 350, 53, fill="#F0F5FB", stroke="#BFD1E7")
    fig.text(280, 325, "RETURN", 28, BLUE, 650, "middle")
    fig.path('M280 345V367', arrow=True)
    fig.box(105, 379, 350, 53, fill="#F0F5FB", stroke="#BFD1E7")
    fig.text(280, 414, "COUNT { □结果表 }", 28, BLUE, 650, "middle")
    fig.lines(100, 478, ["嵌入：无向二跳路径", "谓词：ALL keep = true"], 27, 44)
    # With N=2 there are exactly three two-hop candidates and one valid path.
    fig.path('M655 384L792 319L977 319', color=GREEN, width=4)
    fig.path('M655 384L792 449L977 419M792 449L977 499', color=ORANGE, width=4, dash="9 6")
    fig.circle(655, 384, "s", BLUE, "#F0F5FB")
    fig.circle(792, 319, "a", GREEN, "#F2F8F5")
    fig.circle(977, 319, "t", GREEN, "#F2F8F5")
    fig.circle(792, 449, "b", ORANGE, "#FFF4E8")
    fig.circle(977, 419, "z₁", GREEN, "#F2F8F5")
    fig.circle(977, 499, "z₂", GREEN, "#F2F8F5")
    fig.text(630, 434, "Start", 22, BLUE)
    fig.text(943, 285, "Target", 22, GREEN)
    fig.text(736, 506, "keep=false", 23, ORANGE, 600)
    fig.text(620, 534, "t、z₁、z₂ 均为 Target", 22, MUTED)
    fig.text(620, 564, "3 条候选 → 1 条有效路径", 27, GREEN, 650)
    fig.lines(1160, 312, ["Q₁：s — … — t", "Q₂：t — … — s"], 30, 63, BLUE, 600)
    fig.box(1158, 432, 345, 64, fill="#F2F8F5", stroke="#BED8CD")
    fig.text(1330, 474, "COUNT：1 → 1", 31, GREEN, 700, "middle")
    fig.lines(1160, 539, ["反转保持 ALL 的真假", "外层观察有效匹配数量"], 25, 37, MUTED)

    fig.box(65, 645, 1470, 475, fill="#FCFDFE")
    fig.text(92, 689, "例 B｜非空前缀保护回退参数（S7）", 31, ORANGE, 700)
    headings(fig, 737)
    fig.path('M560 725V1084', color=LINE, width=2)
    fig.path('M1105 725V1084', color=LINE, width=2)
    fig.lines(100, 828, ["MATCH Root", "RETURN □整数"], 29, 49, BLUE, 600)
    fig.path('M280 894V916', arrow=True)
    fig.box(100, 932, 395, 65, fill="#FFF8F0", stroke="#E7C6A5")
    fig.text(297, 973, "coalesce(NULL, κ, L)", 27, ORANGE, 650, "middle")
    fig.text(100, 1057, "κ 非空；只替换回退参数 L", 26, INK)
    fig.circle(642, 836, "u₁", BLUE, "#F0F5FB")
    fig.circle(725, 836, "u₂", BLUE, "#F0F5FB")
    fig.text(790, 845, "…  m 个 Root", 27, BLUE)
    # Directed HIGH relationships end outside the neighbour circles.
    for edge in ('M815 953L941 923', 'M816 959H941', 'M815 965L941 995'):
        fig.path(edge, color=ORANGE, width=4, arrow="work-arrow")
    fig.circle(789, 959, "r", ORANGE, "#FFF4E8")
    for y, name in [(916, "v₁"), (959, "v₂"), (1002, "vN")]:
        fig.circle(969, y, name, ORANGE, "#FFF4E8", radius=20)
    fig.text(634, 945, "WorkRoot", 23, ORANGE)
    fig.text(844, 895, "HIGH", 24, ORANGE, 600)
    fig.text(845, 1050, "N 个工作邻居", 25, ORANGE)
    fig.text(618, 1094, "前缀 κ=0；两类根节点不重叠", 24, GREEN, 600)
    fig.lines(1160, 820, ["Q₁：coalesce(NULL, 0, 0)", "Q₂：coalesce(NULL, 0,", "        COUNT { HIGH 区域 })"], 24, 45, ORANGE, 600)
    fig.box(1158, 956, 345, 65, fill="#F2F8F5", stroke="#BED8CD")
    fig.text(1330, 998, "两侧均返回 m 份 0", 27, GREEN, 700, "middle")
    fig.lines(1160, 1061, ["整体被使用，回退项应短路"], 25, 34, MUTED)

    fig.box(65, 1160, 1470, 211, fill="#F8FAFC", stroke="#BBCAD7")
    fig.text(94, 1203, "依赖对照｜整个 Pattern 的值被外层忽略", 30, INK, 700)
    fig.text(94, 1255, "COUNT { RETURN coalesce(…) AS x }", 29, BLUE, 600)
    fig.text(800, 1255, "只数返回行，不使用 x → 目标被屏蔽", 27, ORANGE, 600)
    fig.text(94, 1324, "当前流程重新生成该目标；它与“整体被观察、仅内部回退项应短路”的有效 S7 用例不同。", 26, MUTED)
    fig.text(65, 1428, "mN 仅表示逐输入枚举 H 时的潜在候选规模；物理调用、缓存、去相关和实际性能需要执行验证。", 24, MUTED)
    fig.write('ast-pattern-running-examples.svg')


if __name__ == '__main__':
    ROOT.mkdir(parents=True, exist_ok=True)
    overview()
    examples()
