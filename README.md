# touchnotes-tool —— 享做笔记（TouchNotes）导入导出工具

把 Android 笔记应用「享做笔记」的盘上文件（`.ncc` / `.p`）读懂、导出成通用格式，
也能反向造出 App 能打开的笔记。**无需数据库、无需 App、纯本地文件操作。**

```
tn list                                    列出所有笔记
tn info minecraft                          一篇笔记的概要与每页统计
tn export minecraft -f pdf -o ~/out        导出（栅格 PDF，含源 PDF 底图）
tn export minecraft -f svg                 导出矢量 SVG
tn dump  minecraft -o note.json --indent 1 导出规范化 JSON（交换格式）
tn build note.json --out /tmp/stage        从 JSON 造一篇笔记（--push 才进网盘）
tn verify --all                            字节级回合自检
tn doctor                                  环境自检
```

## 能做什么 / 不能做什么（实测结论，不吹）

| 方向 | 状态 |
|---|---|
| **导出** | 完善。栅格 PDF（源 PDF 打底 + 笔迹叠加）、逐页 PNG、矢量 SVG / HTML / 矢量 PDF。矢量导出走「描边」不走轮廓填充，结构上不会出现自交空洞 |
| **读** | 全部读得出来：v1 手写、v2 形状、类型、压感、页属性、索引、擦除痕迹 |
| **写** | v1 手写可写（带 `identity` 时字节级可重建）；v2 形状可写，但**必须借 App 自己写过的同类型记录前缀**，字段语义有 3 个未解 |
| **笔记级** | `.ncc` + `page_usn.json` + `pathinfo` 三处一致才算数；写完自动回读自检 |
| **服务端删除** | 做不到 —— App 本地有副本，下次同步会把文件重新传回来（实测：删掉后又被上传） |
| **未解** | `fillColor`（填充）、`square`（方头线端）、`alpha<255`（半透明）三个字段的语义；`Pen` 对象能否自己造（schema 与其它类型不同） |

## 快速开始

不需要安装：仓库自带入口脚本，只依赖 `Pillow`（渲染）和 `poppler-utils`（`pdftoppm`，
仅底图/栅格 PDF 需要）。

```bash
cd ~/tools/touchnotes
./tn doctor                    # 环境 + 依赖 + 前缀模板 + 全量无损自检
ln -sf ~/tools/touchnotes/tn ~/bin/tn   # 装到 PATH（已装好）
```

数据根默认 `/data/netdisk/files/touchnotes/享做笔记5`（可用 `--root` 换）。
**写网盘目录需要 root**：推送/移除用 `sudo tn build ... --push`。

## 子命令

| 命令 | 说明 |
|---|---|
| `tn list [--json]` | 列出所有笔记（名称/页数/大小/时间） |
| `tn info <note> [--json]` | 每页的条数、点数、画布、记录类型、usn |
| `tn dump <note> [-p N] [-o FILE] [--indent N]` | 导出规范化 JSON（`-p` 只导一页） |
| `tn export <note> -f png\|pdf\|svg\|html\|vpdf\|json [-o DIR] [-p N] [--gain G] [--canvas auto\|ink\|declared]` | 导出成品 |
| `tn build <spec.json> [--name N] [--out DIR] [--push] [--backup DIR]` | 造笔记；`--push` 推进网盘 |
| `tn verify [<note>\|--all]` | 字节级回合自检（读页→重组，必须与原文件全等） |
| `tn remove <note> [--yes]` | 移除笔记并清 `pathinfo`（服务端删除会被 App 同步回来） |
| `tn doctor` | 环境自检 + 全量无损自检 |

`<note>` 选择器可以是 `file_id`、笔记名或唯一子串（`minecraft`、`I-20s`、`1791647...`）。

## 中间格式（规范化 JSON）

导入导出都走这一层，`format` 字段锁版本：

```json
{
  "format": "touchnotes/1",
  "note":  {"file_id": "note_...", "name": "...", "page_count": 2},
  "pages": [
    {"id": "note_page...", "index": 0, "viewport": {"angle":0,"offsetX":0,"scaleX":1,...},
     "objects": [
       {"kind": "stroke", "color": "#E53935", "alpha": 255, "width": 8.0,
        "points": [[x, y, pressure], ...], "identity": "<24B hex>"},
       {"kind": "polygon|circle|wave|bezier|pen", "tool": "ShapeTool",
        "color": "#8E24AA", "alpha": 255, "width": 6.6176467, "closed": true,
        "arrow": 0, "dotted": false, "square": false, "fill": null,
        "points": [[x, y], ...], "attrs": {"bottom": false, "cardUuid": "", ...}}
     ]}
  ]
}
```

* `points` 的点数就是 App 索引里的 `pointSize`（重建时自动写回）。
* `attrs` 原样保留未解读字段，保证「读出来 → 写回去」不丢东西。
* 坐标保留 3 位小数（≈0.00013 mm，视觉不可辨）。**因此 JSON 是语义级交换格式**：
  字节级无损的保证在「读页 → 重组页面字节」这一层（`tn verify` 全量 20/20）。

## 磁盘格式规格

### 目录结构

```
享做笔记5/
  pathinfo/pathinfo.json     笔记本索引：infos[]（每篇一条）+ dirs[]（folder_root.children）
  <fileid>.ncc               笔记本体
  <fileid>__res__/
    <pageid>.p               一页的记录
    page_usn.json            每页 usn（必须等于页属性里的 usn）
    <pageid>/                占位空目录
    .note_<fileid>.pdf       导入的源 PDF
    doc / pageids.json / catalog.json   App 自己维护的索引
```

### `.ncc`

```
[21B 头: 20 19 04 29 | 6 | 1 | 06 "note"\0 | 0]
[u32 长度][JSON 元数据 A：name/id/pageCount/pageList...]
[u32 1][u32 长度][JSON 字典：{"md5":"nc","pageList":[...]}]
[u32 长度][JSON 元数据 B：与 A 相同但 synced=true]
[u32][u32][u32 常量 92 (0x5c)]
```

### `.p`

```
[u32 头区长度][头 JSON = 视口 shapeMatrix]
[记录区]
[u32 长度][索引块 {"materials":[], "ms":[{c,i,n,s}...]}]
[u32 长度][属性块（页属性）]
[u32 头区长度(陈旧值)][u32 记录区结束][u32 属性块起始][u32 0x1234]
```

* **记录边界必须用索引块的 `s`/`c`**（绝对偏移 / 字节数）：两者之和正好等于记录区全长。
  只按魔数扫描会漏掉记录尾部的非记录字节（实测每页少 4~40 字节）。
* 尾部第一个字段是**页面创建时的头长，App 改视口后不更新**（陈旧值），重组时必须原样保留。

### 两种记录

| | v1 手写 | v2 形状 |
|---|---|---|
| 布局 | `u32 255 \| f32 width \| u32 argb \| u32 n \| n×(f32 x,y,pressure) \| 24B 身份` | `[前缀 ~136/140B][u32 JSON长度][13 字段 JSON][u32 n][n×(f32 x,y)]` |
| 魔数 | `00 00 00 ff` | `01 33 f0 e9`（Pen 是 `01 34 64 7c`） |
| 每点 | 12 B（含第 3 浮点 = **压感**） | 8 B |
| 语义 | 录像带：一串带压感的采样点 | 图纸：两个端点 + 属性 |

一页里两种记录**按绘制先后交替存放**，必须统一扫描。

**v1 身份尾部 24B** = `[递增 id][常量 19][起始时间][结束时间][8×00]`（时间像设备开机毫秒）；
原样保留即可字节级重建。

**压感**：第 3 个浮点，与序号/弧长/间距都无稳定相关，同一笔最宽/最窄中位 3.4x。
渲染规则 = 以该笔压感第 90 百分位为满宽（最粗处 = 设定笔宽），下限 0.18。

### 类型表（**类型不在记录字节里，在索引 `ms[].n`**）

| `ms[].n` | 形状 | 点数 | 备注 |
|---|---|---|---|
| `FunPen` | 自由手写 | N | 带压感 |
| `ShapeTool` | 闭合多边形 | 直线 2 / 矩形 4 / 五角星 10 | 顶点数决定形状 |
| `CircleTool` | 圆 | 2 | 圆心 + 圆上一点 |
| `WaveTool` | 波浪 | 2 | App 生成波形（导出时按正弦近似） |
| `BezierTool` | 二次贝塞尔 | 3 | 起点/控制点/终点 |
| `Pen` | 平滑路径对象 | N | **JSON schema 不同**（10 字段，含 `id`/`smoothingMode`） |

### v2 的 13 个字段

`alpha, arrow, bottom, cardUuid, closePath, color, dotted, fillColor, itemUuid, mapUuid, pointSize, square, width`

* `arrow`：0/1/2 = 无 / 单向 / 双向；**箭头画在最后一个点上**（已用坐标验证：两条坐标轴画出的
  箭头方向与「两个向上、两个向右」完全吻合）。
* `closePath`：闭合形状（多边形/圆）为 true；**手写识别成的对象为 false**。
* `pointSize`：顶点个数。
* `bottom`：只在「手写识别成的对象」里出现 true（工具画的都是 false），语义未解。
* `color` 是**有符号**整数（`-16777216` = `0xFF000000`）。
* `fillColor / square / alpha<255` 语义未解 —— 写的值 App 不一定照做。

### 擦除

**不是删记录、也不是擦像素** —— 把闭合路径**剪开**：`closePath` 改 false、剪口两端
**插入新顶点**；剪断成不相连的两块就**拆成多条记录**（实测 3 条 → 4 条），整页重写
（usn +2）。所以生成「带缺口的开放折线」技术上完全可行。

### 画布

* 有源 PDF 的页：用页属性的 `width/height`（典型 1626×2105），原点不动，与底图 1:1 对齐。
* 纯笔迹页：无限画布、坐标可为负 → 取墨迹包围盒 + 边距（含最粗笔半宽）并平移。
* `tn export --canvas auto|ink|declared` 可覆盖这个策略。

## 写回的前置条件

1. **前缀模板**：v2 形状必须借 App 自己写过的同类型记录前缀（`tn doctor` 会列出可写类型）。
   App 里画过一条该类型的记录，之后就能批量生成。
2. **写权限**：笔记本目录属 keeper 容器用户 → 用 `sudo`。
3. **三处一致**：`.ncc` 的 `pageList/pageCount`、`page_usn.json` 的 usn、页属性里的 usn。
4. 生成后 `build` 会自动回读自检（条数/类型/点数/坐标偏差/字节级回合）。

## 已知问题

* 生成的笔记在 App 里**能打开但内容不可见**（ZZ2 实测）。疑点在页属性/视口层，未查。
* 服务端删除无效（App 会同步回来），要删就在 App 里删。
* `Pen` 对象只读不写（schema 未吃透）。

## 目录

```
touchnotes/
  __init__.py
  container.py   容器层：定位笔记、.ncc/.p 分段读写、pathinfo
  records.py     记录层：v1/v2 记录解析与编码、类型映射
  model.py       中间表示：规范化 JSON
  geometry.py    几何与压感：重采样、压感→笔宽、变宽带
  render.py      渲染：栅格（PNG/PDF）与矢量（SVG/HTML/PDF）
  writer.py      写回：JSON → App 能认的笔记（自检 + 推送）
  cli.py         命令行
tn               入口脚本
```

## 从这台机器推 GitHub

`github.com` 的 git 端口直连会 SSL 超时（`api.github.com` 正常），所以用 Git Data API 推：

```bash
export GITHUB_TOKEN=ghp_...        # 或写进 ~/.hermes/.env（脚本会自己找）
python3 tools/push_via_api.py      # blob → tree → commit → 更新 ref，只推 git 已跟踪的文件
python3 tools/push_via_api.py --dry-run   # 先看会推什么
```

## 旧脚本对照（已被本工具取代）

| 旧脚本（`~/bin/`） | 现在 |
|---|---|
| `tn-export.py`（栅格 PDF/PNG 全量） | `tn export <note> -f pdf\|png` |
| `tn-vector.py`（SVG/HTML/矢量 PDF 全量） | `tn export <note> -f svg\|html\|vpdf` |
| `tn-inspect.py`（逐条列记录） | `tn info <note>` / `tn dump <note>` |
| `tn-write.py`（记录编解码往返） | `tn verify <note>` |
| `tn-newnote.py`（造测试笔记并推送） | `tn build <spec.json> --push` |
| `tn-makenote.py`（克隆模板） | `writer.build()` 内部已含 |

旧脚本保留未删，随时可对比输出。
