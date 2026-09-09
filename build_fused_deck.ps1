$ErrorActionPreference = 'Stop'

$sourcePath = 'C:\Users\whitecity\Desktop\姚望辰-姚丞韬-实习答辩.pptx'
$outputDir = 'C:\Users\whitecity\Desktop\mobilework\output'
$outputPath = Join-Path $outputDir '姚望辰-姚丞韬-实习答辩-融合版.pptx'
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
Copy-Item -LiteralPath $sourcePath -Destination $outputPath -Force

function RGB([int]$r, [int]$g, [int]$b) {
    return $r + 256 * $g + 65536 * $b
}

$C = @{
    Navy = RGB 36 61 94
    Blue = RGB 46 117 182
    Blue2 = RGB 47 112 177
    LightBlue = RGB 233 242 251
    LightBlue2 = RGB 222 236 249
    Green = RGB 43 125 104
    LightGreen = RGB 232 246 241
    Purple = RGB 104 73 151
    LightPurple = RGB 242 237 249
    Gold = RGB 188 125 40
    LightGold = RGB 253 245 228
    Red = RGB 187 62 63
    LightRed = RGB 252 239 239
    Text = RGB 35 39 45
    Muted = RGB 91 104 117
    Border = RGB 205 214 224
    Pale = RGB 247 249 251
    White = RGB 255 255 255
}

function Set-TextStyle($shape, [string]$text, [double]$size, [int]$color, [bool]$bold = $false, [int]$align = 1, [int]$vertical = 3) {
    $shape.TextFrame.TextRange.Text = $text
    $shape.TextFrame.MarginLeft = 5
    $shape.TextFrame.MarginRight = 5
    $shape.TextFrame.MarginTop = 3
    $shape.TextFrame.MarginBottom = 3
    try { $shape.TextFrame.VerticalAnchor = $vertical } catch {}
    try { $shape.TextFrame.WordWrap = 1 } catch {}
    try { $shape.TextFrame.AutoSize = 0 } catch {}
    $shape.TextFrame.TextRange.ParagraphFormat.Alignment = $align
    $shape.TextFrame.TextRange.Font.Name = '微软雅黑'
    try { $shape.TextFrame.TextRange.Font.NameFarEast = '微软雅黑' } catch {}
    $shape.TextFrame.TextRange.Font.Size = $size
    $shape.TextFrame.TextRange.Font.Color.RGB = $color
    $shape.TextFrame.TextRange.Font.Bold = if ($bold) { -1 } else { 0 }
}

function Add-Text($slide, [double]$x, [double]$y, [double]$w, [double]$h, [string]$text, [double]$size, [int]$color, [bool]$bold = $false, [int]$align = 1, [int]$vertical = 3) {
    $shape = $slide.Shapes.AddTextbox(1, $x, $y, $w, $h)
    Set-TextStyle $shape $text $size $color $bold $align $vertical
    return $shape
}

function Add-Card($slide, [double]$x, [double]$y, [double]$w, [double]$h, [int]$fill, [int]$line, [double]$radiusType = 5) {
    $shape = $slide.Shapes.AddShape($radiusType, $x, $y, $w, $h)
    $shape.Fill.Visible = -1
    $shape.Fill.ForeColor.RGB = $fill
    $shape.Line.Visible = -1
    $shape.Line.ForeColor.RGB = $line
    $shape.Line.Weight = 1
    return $shape
}

function Add-StepBadge($slide, [double]$x, [double]$y, [string]$text, [int]$fill) {
    $shape = $slide.Shapes.AddShape(9, $x, $y, 28, 28)
    $shape.Fill.ForeColor.RGB = $fill
    $shape.Line.Visible = 0
    Set-TextStyle $shape $text 10 $C.White $true 2 3
    return $shape
}

function Add-Arrow($slide, [double]$x1, [double]$y1, [double]$x2, [double]$y2, [int]$color) {
    $shape = $slide.Shapes.AddConnector(1, $x1, $y1, $x2, $y2)
    $shape.Line.ForeColor.RGB = $color
    $shape.Line.Weight = 1.4
    $shape.Line.EndArrowheadStyle = 3
    return $shape
}

function Clear-TemplateBody($slide) {
    for ($i = $slide.Shapes.Count; $i -ge 4; $i--) {
        $slide.Shapes.Item($i).Delete()
    }
}

function Duplicate-TemplateSlide($presentation, [int]$targetIndex, [string]$title) {
    $range = $presentation.Slides.Item(8).Duplicate()
    $slide = $range.Item(1)
    $slide.MoveTo($targetIndex)
    Clear-TemplateBody $slide
    $slide.Shapes.Item(1).TextFrame.TextRange.Text = $title
    return $slide
}

function Set-SlideTitle($slide, [string]$title) {
    $slide.Shapes.Item(1).TextFrame.TextRange.Text = $title
}

function Add-FlowCard($slide, [double]$x, [double]$y, [double]$w, [double]$h, [string]$num, [string]$title, [string]$body, [int]$accent, [int]$fill) {
    Add-Card $slide $x $y $w $h $fill $C.Border | Out-Null
    Add-StepBadge $slide ($x + 12) ($y + 12) $num $accent | Out-Null
    Add-Text $slide ($x + 50) ($y + 10) ($w - 60) 38 $title 14 $accent $true 1 3 | Out-Null
    Add-Text $slide ($x + 14) ($y + 58) ($w - 28) ($h - 70) $body 11.5 $C.Muted $false 1 1 | Out-Null
}

$ppt = New-Object -ComObject PowerPoint.Application
try {
    $presentation = $ppt.Presentations.Open($outputPath, $false, $false, $false)

    # Slide 5: cover the flattened retrieval row and rebuild it with editable native shapes.
    $s5 = $presentation.Slides.Item(5)
    $row = Add-Card $s5 24 421 912 82 $C.LightGold (RGB 233 205 151)
    $row.Line.DashStyle = 4
    Add-StepBadge $s5 42 438 '3' (RGB 223 126 0) | Out-Null
    Add-Text $s5 78 430 76 28 '检索与问答' 14 $C.Gold $true 1 3 | Out-Null
    Add-Text $s5 40 461 108 30 '基于多路检索与\n证据融合生成回答' 8.5 $C.Muted $false 1 1 | Out-Null

    $flowX = @(158, 308, 458, 608, 758)
    $flowTitles = @('用户提出问题', '相关知识库路由', '库内三通道组合检索', '加权融合与重排', '证据交付与回答')
    $flowBodies = @(
        '输入自然语言问题',
        '目录语义评分\n阈值 0.5，最多 3 库',
        'Vector · Keyword · Graph',
        'RRF 融合\n时间意图触发重排',
        '带来源的 Top-5 证据\n由 LLM 生成回答'
    )
    for ($i = 0; $i -lt 5; $i++) {
        Add-Card $s5 $flowX[$i] 433 132 58 $C.White (RGB 229 205 160) | Out-Null
        Add-Text $s5 ($flowX[$i] + 6) 436 120 20 $flowTitles[$i] 9.2 $C.Navy $true 2 3 | Out-Null
        Add-Text $s5 ($flowX[$i] + 6) 456 120 31 $flowBodies[$i] 7.8 $C.Muted $false 2 3 | Out-Null
        if ($i -lt 4) { Add-Arrow $s5 ($flowX[$i] + 134) 462 ($flowX[$i + 1] - 5) 462 (RGB 223 126 0) | Out-Null }
    }

    # Slide 11 wording clarification.
    $s11 = $presentation.Slides.Item(11)
    $target11 = $null
    foreach ($shape in $s11.Shapes) {
        if ($shape.HasTextFrame -and $shape.TextFrame.HasText -and $shape.TextFrame.TextRange.Text -eq 'BM25 + 身份校验') { $target11 = $shape; break }
    }
    if ($null -eq $target11) { throw 'Slide 11 BM25 title was not found.' }
    Set-TextStyle $target11 "构建期页面匹配`nBM25 + 身份校验" 12.5 $C.Gold $true 1 3
    $target11.Height = 38
    $target11.Top = 127

    # Slide 14 title.
    $s14 = $presentation.Slides.Item(14)
    Set-SlideTitle $s14 '构建期页面去重：BM25 候选召回 + 两阶段语义决策'

    # Slide 15 summary.
    $s15 = $presentation.Slides.Item(15)
    $summary15 = $null
    foreach ($shape in $s15.Shapes) {
        if ($shape.HasTextFrame -and $shape.TextFrame.HasText -and $shape.TextFrame.TextRange.Text -like '最终目标*') { $summary15 = $shape; break }
    }
    if ($null -eq $summary15) { throw 'Slide 15 summary was not found.' }
    Set-TextStyle $summary15 "构建侧保证资料变化可检测、知识更新可定位、来源关系可追踪和发布失败可恢复，`n为后续多知识库检索提供稳定、结构化的 Wiki。" 11.5 $C.Navy $true 2 3
    $summary15.Top = 408
    $summary15.Height = 58

    # New slide 16: main retrieval chain.
    $s16 = Duplicate-TemplateSlide $presentation 16 '检索侧优化1：多知识库检索主链路'
    $fx = @(33, 265, 497, 729)
    $fills = @($C.LightBlue, $C.LightGreen, $C.LightPurple, $C.LightGold)
    $accents = @($C.Blue, $C.Green, $C.Purple, $C.Gold)
    $titles = @('查询解析与相关库路由', '多知识库并行执行', '库内三通道召回', '融合、重排与证据交付')
    $bodies = @(
        "解析 Query 与时间意图`n目录语义评分 ≥ 0.5`n最多选择 3 个知识库",
        "不同知识库并行执行`n最大并发数 8`n单库异常不阻断其他库",
        "Vector　权重 1.0`nKeyword　权重 0.35`nGraph　权重 0.20",
        "加权 RRF 统一排序`n时间意图触发新鲜度重排`n交付带来源的 Top-5 证据"
    )
    for ($i = 0; $i -lt 4; $i++) {
        Add-FlowCard $s16 $fx[$i] 122 198 242 ($i + 1).ToString('00') $titles[$i] $bodies[$i] $accents[$i] $fills[$i]
        if ($i -lt 3) { Add-Arrow $s16 ($fx[$i] + 202) 242 ($fx[$i + 1] - 6) 242 $C.Blue2 | Out-Null }
    }
    Add-Card $s16 78 394 804 76 $C.Pale $C.Border | Out-Null
    Add-Text $s16 98 404 764 25 '一次提问在库间与通道间完成受控检索' 15 $C.Navy $true 2 3 | Out-Null
    Add-Text $s16 98 431 764 26 '路由选库 · 多库并行 · 三通道召回 · 统一融合 · 可回溯交付' 11.5 $C.Muted $false 2 3 | Out-Null

    # New slide 17: atomic tools and fusion.
    $s17 = Duplicate-TemplateSlide $presentation 17 '检索侧优化2：原子化工具与多库融合'
    $cx = @(33, 345, 657)
    $ctitles = @('相关库路由', '三通道检索', '统一融合')
    $cbodies = @(
        "route_select`n`n先对目录做语义评分`n得分达标才进入候选`n若全部落空则回退全库",
        "vector_search`nkeyword_search`ngraph_search`n`n三个工具独立执行`n新增通道无需改动主流程",
        "rrf_merge`ntime_rerank`n`n多通道结果统一排名`n时间敏感查询增加新鲜度因子"
    )
    for ($i = 0; $i -lt 3; $i++) {
        Add-Card $s17 $cx[$i] 118 270 278 $fills[$i] $C.Border | Out-Null
        Add-StepBadge $s17 ($cx[$i] + 16) 135 ($i + 1).ToString('00') $accents[$i] | Out-Null
        Add-Text $s17 ($cx[$i] + 58) 130 190 35 $ctitles[$i] 16 $accents[$i] $true 1 3 | Out-Null
        Add-Text $s17 ($cx[$i] + 18) 174 234 190 $cbodies[$i] 12 $C.Text $false 1 1 | Out-Null
        if ($i -lt 2) { Add-Arrow $s17 ($cx[$i] + 274) 252 ($cx[$i + 1] - 5) 252 $C.Blue2 | Out-Null }
    }
    Add-Card $s17 45 414 870 70 $C.Pale $C.Border | Out-Null
    Add-Text $s17 64 422 832 20 '简化 RRF 示例' 11 $C.Muted $true 1 3 | Out-Null
    Add-Text $s17 64 442 832 28 'Vector #1  0.0164　+　Keyword #1  0.0057　+　Graph #1  0.0033　=　融合贡献 0.0254' 12.5 $C.Navy $true 2 3 | Out-Null
    Add-Text $s17 64 469 832 18 '同一证据被多个通道命中时，贡献值叠加并自然上浮。' 10.5 $C.Muted $false 2 3 | Out-Null

    # New slide 18: adaptation and resilience.
    $s18 = Duplicate-TemplateSlide $presentation 18 '检索侧优化3：查询适配与工程韧性'
    $mx = @(33, 345, 657)
    $mTitles = @('时间意图重排', '模糊 Query 改写', '路由与故障隔离')
    $mBig = @('Hit@5  87.5% → 100%', 'MRR  52.8% → 91.7%', '自动路由 p95  18.72 s')
    $mBodies = @(
        "识别「最新」等时间意图`n按更新时间调整排序`n`np95：10.51 s → 9.85 s",
        "「请假咋弄啊」`n改写为`n「请假申请流程和审批规则」`n`n三通道召回更完整",
        "路由 p95 降低 33.8%`n`n单通道降级时`n6 / 6 请求正常返回"
    )
    for ($i = 0; $i -lt 3; $i++) {
        Add-Card $s18 $mx[$i] 122 270 328 $fills[$i] $C.Border | Out-Null
        Add-StepBadge $s18 ($mx[$i] + 16) 140 ($i + 1).ToString('00') $accents[$i] | Out-Null
        Add-Text $s18 ($mx[$i] + 58) 134 190 36 $mTitles[$i] 16 $accents[$i] $true 1 3 | Out-Null
        Add-Text $s18 ($mx[$i] + 18) 187 234 42 $mBig[$i] 15 $C.Navy $true 2 3 | Out-Null
        Add-Text $s18 ($mx[$i] + 18) 238 234 184 $mBodies[$i] 12 $C.Text $false 2 1 | Out-Null
    }
    Add-Card $s18 72 468 816 34 $C.Pale $C.Border | Out-Null
    Add-Text $s18 88 472 784 25 '查询适配提升召回，故障隔离保证单个组件异常时仍能返回可用证据。' 11.5 $C.Navy $true 2 3 | Out-Null

    # New slide 19: end-to-end results.
    $s19 = Duplicate-TemplateSlide $presentation 19 '检索侧优化4：端到端实验结果'
    Add-Card $s19 35 62 890 34 $C.Pale $C.Border | Out-Null
    Add-Text $s19 45 64 870 28 '实验口径：相同 50 页 Wiki · 12 个独立问题 × 3 次 = 36 次 · Top-5 · 相同预计算向量 · 本地 p95 排除远程 Embedding' 10.5 $C.Muted $false 2 3 | Out-Null

    $kx = @(43, 337, 631)
    $kLabels = @('MRR 提升', '本地 p95', 'Hit@5')
    $kValues = @('+10.4 pt', '-37.2%', '75.0%')
    $kNotes = @('49.3% → 59.7%', '71.5 ms → 44.9 ms', '与 nashsu 持平')
    for ($i = 0; $i -lt 3; $i++) {
        Add-Card $s19 $kx[$i] 108 250 76 $fills[$i] $C.Border | Out-Null
        Add-Text $s19 ($kx[$i] + 12) 113 128 20 $kLabels[$i] 10.5 $C.Muted $true 1 3 | Out-Null
        Add-Text $s19 ($kx[$i] + 12) 132 128 30 $kValues[$i] 21 $accents[$i] $true 1 3 | Out-Null
        Add-Text $s19 ($kx[$i] + 140) 134 98 24 $kNotes[$i] 9 $C.Muted $false 2 3 | Out-Null
    }

    $tableShape = $s19.Shapes.AddTable(4, 5, 43, 198, 874, 142)
    $table = $tableShape.Table
    $widths = @(188, 128, 142, 142, 274)
    for ($col = 1; $col -le 5; $col++) { $table.Columns.Item($col).Width = $widths[$col - 1] }
    $headers = @('系统', '有效样本', 'Hit@5', 'MRR', 'p95')
    $rows = @(
        @('Karpathy', '13 / 36', '76.9%*', '71.2%*', '63,331.7 ms*'),
        @('nashsu', '36 / 36', '75.0%', '49.3%', '71.5 ms'),
        @('MobileworkWiki', '36 / 36', '75.0%', '59.7%', '44.9 ms')
    )
    for ($col = 1; $col -le 5; $col++) {
        $cell = $table.Cell(1, $col).Shape
        $cell.Fill.Solid()
        $cell.Fill.ForeColor.RGB = $C.Navy
        Set-TextStyle $cell $headers[$col - 1] 10.5 $C.White $true 2 3
    }
    for ($rowNo = 2; $rowNo -le 4; $rowNo++) {
        for ($col = 1; $col -le 5; $col++) {
            $cell = $table.Cell($rowNo, $col).Shape
            $cell.Fill.Solid()
            if ($rowNo -eq 4) { $cell.Fill.ForeColor.RGB = $C.LightBlue } else { $cell.Fill.ForeColor.RGB = $C.White }
            $textColor = if ($rowNo -eq 4 -and $col -ge 4) { $C.Blue } else { $C.Text }
            Set-TextStyle $cell $rows[$rowNo - 2][$col - 1] 10.5 $textColor ($rowNo -eq 4) 2 3
        }
    }

    Add-Text $s19 48 344 864 20 '* Karpathy 仅有 13/36 次成功返回，表中 Hit@5、MRR 与 p95 为成功子集口径。' 8.8 $C.Muted $false 1 3 | Out-Null
    Add-Card $s19 43 372 874 92 $C.LightBlue $C.Border | Out-Null
    Add-Text $s19 63 381 834 31 'MobileworkWiki 把 LLM Wiki 变成工程一等公民' 15.5 $C.Blue $true 2 3 | Out-Null
    Add-Text $s19 70 414 820 39 '相关库路由、三通道 RRF、查询适配和组件实验共同证明整套链路优于当前基线。' 12 $C.Navy $true 2 3 | Out-Null

    # Placeholder slide after the four original demo slides.
    $range25 = $presentation.Slides.Item(21).Duplicate()
    $s25 = $range25.Item(1)
    $s25.MoveTo(25)
    Clear-TemplateBody $s25
    Set-SlideTitle $s25 '演示结果'
    Add-Text $s25 40 76 500 32 '多知识库检索与证据溯源' 17 $C.Text $false 1 3 | Out-Null
    $placeholder = $s25.Shapes.AddShape(1, 55, 122, 850, 335)
    $placeholder.Fill.Visible = 0
    $placeholder.Line.Visible = -1
    $placeholder.Line.ForeColor.RGB = (RGB 178 187 198)
    $placeholder.Line.Weight = 1.5
    $placeholder.Line.DashStyle = 4
    Set-TextStyle $placeholder '此处插入检索演示截图' 18 (RGB 165 174 184) $false 2 3
    Add-Text $s25 55 467 850 22 '建议截图包含：选中知识库、Top-5 证据、通道贡献及来源归属' 10 $C.Muted $false 2 3 | Out-Null

    # Renumber slide-number placeholders only.
    for ($i = 1; $i -le $presentation.Slides.Count; $i++) {
        $slide = $presentation.Slides.Item($i)
        foreach ($shape in $slide.Shapes) {
            if ($shape.Type -eq 14 -and $shape.HasTextFrame -and $shape.TextFrame.HasText) {
                $raw = $shape.TextFrame.TextRange.Text.Trim()
                if ($raw -match '^\d+$') { $shape.TextFrame.TextRange.Text = [string]$i }
            }
        }
    }

    if ($presentation.Slides.Count -ne 26) { throw "Expected 26 slides, found $($presentation.Slides.Count)." }
    $presentation.Save()
    $presentation.Close()
}
finally {
    $ppt.Quit()
    [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($ppt) | Out-Null
}

Get-Item -LiteralPath $outputPath | Select-Object FullName, Length, LastWriteTime
