"""享做笔记（TouchNotes）盘上格式读写工具。

- container  容器层：定位笔记、.ncc / .p 分段读写、pathinfo
- records    记录层：v1 手写 / v2 形状记录的解析与编码、类型映射
- model      中间表示：统一成规范化 JSON（导入导出的公共接口）
- geometry   几何与压感：采样密度、压感→笔宽、变宽带
- render     渲染：栅格（PNG/PDF）与矢量（SVG/HTML/PDF）
- writer     写回：规范化 JSON → App 能认的笔记文件（可推送）
- cli        命令行入口
"""

__version__ = "0.1.0"
