# ADR-005：PDF 解析保持 pypdf，高级解析延后

- **状态**：已采纳（当前阶段）
- **日期**：2026-11-26
- **决策者**：项目负责人（待确认）
- **相关阶段**：阶段04 Data Center / 4.8 PDF 摄取

---

## 背景

4.8 节用 `pypdf` 实现 PDF 纯文本提取，能力边界：

- ✅ 文本型 PDF 逐页提取
- ✅ 物理页码映射
- ✅ 加密/损坏/无文本页拒绝
- ❌ 表格结构（单元格被展平）
- ❌ 扫描件 OCR
- ❌ 多栏阅读顺序推断
- ❌ 公式识别

复审时评估了两个主流替代方案：Docling 和 MinerU。

---

## 备选方案

### 方案 A：保持 pypdf（当前）

**优点**
- 已验收，23 项开发包测试 + 41 项补充测试全部通过
- 依赖极轻（纯 Python）
- 与 4.8 的审核指纹机制深度集成
- 无模型下载、无 GPU 需求

**缺点**
- 表格、OCR、版面分析全部缺失

### 方案 B：Docling

IBM Research 出品，GitHub 37k+ stars，已捐赠 Linux Foundation。

**能力**
- TableFormer 表格结构识别
- Layout Model 版面分析
- 阅读顺序重建
- 公式识别

**代价**
- 需下载 AI 模型（数百 MB）
- 依赖较重

### 方案 C：MinerU

上海 AI Lab 出品。

**能力**
- 109 语言 OCR
- 公式识别（可开关）
- 多引擎选择（vlm/hybrid-auto-engine）

**代价**
- 依赖 torch
- CUDA 配置复杂
- 打包体积大

### 方案 D：Docling + pypdf 混合

简单文档用 pypdf（快、轻），复杂文档用 Docling（慢、准）。

**优点**
- 按需付费

**缺点**
- 两套解析路径，维护成本翻倍
- 判定"复杂"的规则难设计

---

## 决策

**当前保持方案 A（pypdf），不引入 Docling/MinerU。**

理由：

1. **无真实 PDF 样本**。项目内只有一份中文双页**合成** PDF（`tests/fixtures/synthetic_policy_zh.pdf`），首行标注"测试材料 非真实政策"。在没有任何真实政府政策 PDF 的情况下，无法验证 Docling/MinerU 的实际收益。

2. **当前业务未受阻塞**。2 份真实政策都是 HTML，PDF 路径尚未在业务中实际使用。

3. **4.8 的审核指纹机制依赖确定性的解析结果**。引入 Docling/MinerU 会改变提取文本，导致所有已有审核指纹失效。这是一次需要谨慎处理的迁移。

4. **依赖体积与项目原则冲突**。项目从开始坚持"新增第三方依赖 0"（除必需的 bs4、pypdf、fastapi）。Docling 需模型文件，MinerU 需 torch，都是数量级更大的依赖。

---

## 触发引入的条件

引入高级文档解析需要**同时满足**：

1. **有真实政策 PDF 样本**（至少 5 份，覆盖表格、多栏、扫描件类型）
2. **业务上确实需要表格结构**（例如电价表、分时时段表）
3. **完成 A/B 对比**：同一份 PDF 用 pypdf 和 Docling 分别提取，人工评估信息保留度
4. **确认不影响 4.8 审核指纹机制**：需设计版本化 parser_version，允许新旧解析结果共存

### 优先选择 Docling 的理由（若触发）

- API 更简洁（`DocumentConverter().convert(path)`）
- 有 CPU 模式，不强依赖 GPU
- 表格识别专用模型（TableFormer），比通用 OCR 更精准
- 开源治理更规范（Linux Foundation）

MinerU 更适合"大量扫描件 + 需要多语言 OCR"的场景，当前项目不符合。

---

## 后果

### 正面

- 保持依赖轻量
- 4.8 审核指纹机制稳定
- 不引入未验证的能力

### 负面

- 4.5 发现的 15 条 PDF 候选无法高质量摄取
- 表格型政策（电价表）信息会丢失结构

### 缓解

在 4.5 的候选清单里，明确标注 PDF 候选需等待解析能力升级。当前候选状态为 `pending_review`，不会自动入库，风险可控。

---

## 重新评估触发条件

1. 获得 ≥5 份经授权的真实政策 PDF
2. 业务方明确提出"需要表格结构化"
3. pypdf 在真实 PDF 上出现明显提取错误
4. 项目允许增加 500MB+ 依赖体积

---

## 参考

- [Docling 项目介绍](https://www.ibm.com/think/news/doclings-rise-llm-ready-data)（访问 2026-11-26）
- [Docling IBM Models](https://pypi.org/project/docling-ibm-models/)（访问 2026-11-26）
- [MinerU 2.7.0 更新日志](https://pypi.org/project/mineru/2.7.0/)（访问 2026-11-26）
- 项目内实现：`powercopy/data/pdf_ingest.py`
