# 多模态交付契约 v1.0（评审稿）

解析层输出 `ParsedDocument 2.2`，质量层输出 `QualityPackage 1.0`。Wiki 只接收 `pass` 或 `pass_with_warnings`，不依赖解析器私有格式。

## 图片

图片 Asset 必须提供安全相对路径、MIME、SHA-256 和 `referenced_by_block_ids`。文件必须存在且哈希一致；禁止 data URI。page/bbox/宽高缺失时使用 null，并通过 capability 说明原因，不得推测。

```json
{
  "path": "images/figure-0001.png",
  "kind": "image",
  "file_type": "image/png",
  "sha256": "64位小写SHA-256",
  "referenced_by_block_ids": ["block UUID"],
  "anchor": {
    "page_number": 3,
    "bbox": [72.0, 120.0, 540.0, 420.0],
    "page_width": 612.0,
    "page_height": 792.0,
    "coordinate_system": "pdf_points_top_left"
  },
  "metadata": {"caption": "图注", "caption_source": "source", "evidence_status": "verified"}
}
```

## OCR、表格与能力

OCR 原文进入 `ocr_spans`；视觉 caption 不得覆盖 OCR，必须标为 `vision_model/inferred`。表格通过 `block_id` 指向 table block；没有原生 cells 时不得从渲染结果反推。

能力键建议为 `text/assets/page/bbox/ocr/tables/reading_order`，状态为 `available/partial/unavailable/failed`；除 available 外必须有 reason。

空正文、rejected/reparse_required、路径逃逸、data URI、资源缺失、哈希错误或无效 table block 均阻断发布。

## 与杨欣川产物对齐

- 不需要缩略图，必须交付原图。
- `natural_image/chart/page_screenshot` 必须 Caption；`table_screenshot/decorative_image` 跳过。
- `chart/page_screenshot/table_screenshot` 必须 OCR；普通图片由 contains_text 决定；装饰图跳过。
- 每张图片固定保留 image_caption 与 image_ocr 两个槽位；非 ready 不可检索并必须说明 reason/error。
- package/item/asset/chunk/visual evidence 的稳定 ID 必须闭合；无法可靠关联正文时 context_item_ids 留空。
- 原始 PDF 图注、OCR 和 VLM Caption 不互相覆盖；claims 明确区分 extracted/inferred/ambiguous。

仍待确认：normalized_1000 bbox 与 pt 页面尺寸的换算语义、table.rows 的稳定 Schema、叶侧 Gate 阈值，以及 Wiki 实际索引字段和资源大小限制。
