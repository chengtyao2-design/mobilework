import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [reportPath, outputPath, previewDir] = process.argv.slice(2);
if (!reportPath || !outputPath || !previewDir) {
  throw new Error("usage: node build_comparison_workbook.mjs REPORT_JSON OUTPUT_XLSX PREVIEW_DIR");
}

const report = JSON.parse(await fs.readFile(reportPath, "utf8"));
const workbook = Workbook.create();
for (const name of [
  "Summary", "System Comparison", "Pain Point Tests", "Skill Regression",
  "Raw Runs", "Queries & Rubric", "Sources & Setup", "Failures",
]) workbook.worksheets.add(name);
const FONT = "Arial";
const COLORS = {
  navy: "#17365D", blue: "#1F4E78", teal: "#0F766E", paleBlue: "#D9EAF7",
  paleGreen: "#DFF2E1", paleRed: "#FCE8E6", paleAmber: "#FFF2CC", white: "#FFFFFF",
  text: "#1F2937", muted: "#64748B", border: "#CBD5E1",
};

const asArray = (value) => (Array.isArray(value) ? value : []);
const asText = (value) => {
  if (value === null || value === undefined) return null;
  if (Array.isArray(value)) return value.map(asText).filter(Boolean).join("\n");
  if (typeof value === "object") return JSON.stringify(value);
  return value;
};
const metric = (row, name) => {
  const value = row?.metrics?.[name] ?? row?.[name];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
};
const qualityScore = (row) => {
  const weighted = [["factual_correctness", .35], ["citation_recall", .25], ["grounded_ratio", .20],
    ["conflict_handling", .10], ["abstention_score", .10]]
    .map(([name, weight]) => [metric(row, name), weight])
    .filter(([value]) => value !== null);
  const denominator = weighted.reduce((sum, [, weight]) => sum + weight, 0);
  return denominator ? weighted.reduce((sum, [value, weight]) => sum + value * weight, 0) / denominator : null;
};
const excelText = (value) => String(value ?? "").replaceAll('"', '""');

function columnName(index) {
  let value = index + 1;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}

function styleTitle(sheet, title, lastColumn) {
  sheet.showGridLines = false;
  sheet.getRange("A2").values = [[title]];
  sheet.getRange(`A2:${lastColumn}2`).format = {
    font: { name: FONT, size: 15, bold: true, color: COLORS.text }, verticalAlignment: "center",
  };
  sheet.getRange(`A3:${lastColumn}3`).format.borders = {
    bottom: { style: "thin", color: COLORS.border },
  };
  sheet.getRange("2:2").format.rowHeight = 28;
}

function styleHeader(range) {
  range.format = {
    fill: COLORS.blue,
    font: { name: FONT, size: 10, bold: true, color: COLORS.white },
    horizontalAlignment: "center", verticalAlignment: "center", wrapText: true,
    borders: { preset: "outside", style: "thin", color: COLORS.border },
  };
}

function addDataSheet(name, title, headers, rows, widths = [], options = {}) {
  const sheet = workbook.worksheets.getItem(name);
  const lastColumn = columnName(headers.length - 1);
  styleTitle(sheet, title, lastColumn);
  sheet.getRange(`A4:${lastColumn}4`).values = [headers];
  styleHeader(sheet.getRange(`A4:${lastColumn}4`));
  sheet.getRange("4:4").format.rowHeight = 34;
  if (rows.length) {
    const end = rows.length + 4;
    sheet.getRange(`A5:${lastColumn}${end}`).values = rows;
    sheet.getRange(`A5:${lastColumn}${end}`).format = {
      font: { name: FONT, size: 10, color: COLORS.text }, verticalAlignment: "top", wrapText: true,
      borders: { insideHorizontal: { style: "thin", color: COLORS.border } },
    };
  } else {
    sheet.getRange("A5").values = [["暂无可用记录"]];
    sheet.getRange("A5").format.font = { name: FONT, size: 10, italic: true, color: COLORS.muted };
  }
  headers.forEach((_, index) => {
    sheet.getRange(`${columnName(index)}:${columnName(index)}`).format.columnWidth = widths[index] ?? 16;
  });
  if (rows.length > 14) sheet.freezePanes.freezeRows(4);
  if (options.freezeColumns) sheet.freezePanes.freezeColumns(options.freezeColumns);
  return sheet;
}

function writeFormulaRow(sheet, row, formulas) {
  for (const [column, formula] of Object.entries(formulas)) sheet.getRange(`${column}${row}`).formulas = [[formula]];
}

function countIfs(criteria) {
  return `COUNTIFS(${criteria.flatMap(([range, value]) => [range, `"${excelText(value)}"`]).join(",")})`;
}

function averageIfs(valueRange, criteria) {
  const metricCriteria = [[valueRange, ">=0"], ...criteria];
  return `=IF(${countIfs(metricCriteria)}=0,\"n.a.\",AVERAGEIFS(${valueRange},${criteria.flatMap(([range, value]) => [range, `"${excelText(value)}"`]).join(",")}))`;
}

function p95Ifs(latencyRange, criteria) {
  const filter = criteria.map(([range, value]) => `(${range}=\"${excelText(value)}\")`).join("*");
  return `=IF(${countIfs(criteria)}=0,\"n.a.\",SMALL(FILTER(${latencyRange},${filter}),ROUNDUP(${countIfs(criteria)}*0.95,0)))`;
}

const systems = asArray(report.systems).length ? asArray(report.systems) : [
  { id: "karpathy", name: "Astro-Han/karpathy-llm-wiki", repository: "https://github.com/Astro-Han/karpathy-llm-wiki" },
  { id: "nashsu", name: "nashsu/llm_wiki", repository: "https://github.com/nashsu/llm_wiki" },
  { id: "mobilework", name: "Mobilework", repository: "local" },
];
const rawRuns = asArray(report.raw_runs ?? report.runs);
const questions = asArray(report.questions);
const failureInput = asArray(report.failures);
const failures = failureInput.length ? failureInput : rawRuns.filter((row) => !["ok", "degraded"].includes(row.status));

const rawHeaders = [
  "run_id", "experiment_id", "system_id", "system_name", "system_commit", "provenance_label",
  "provenance_origin", "question_id", "query", "query_variant", "condition_id", "repeat", "seed",
  "mode", "status", "started_at", "finished_at", "latency_ms", "tool_calls", "result_count",
  "top_sources", "queried_kb_ids", "channels", "route_detail", "degradation_detail", "failure_detail",
  "hit_at_5", "mrr", "citation_recall", "irrelevant_evidence_rate", "factual_correctness",
  "grounded_ratio", "conflict_handling", "abstention_score", "stale_fact_error", "answer", "citations",
  "quality_score", "condition", "adapter_metadata",
];
const rawRows = rawRuns.map((row) => {
  const results = asArray(row.results);
  const topSources = row.top_sources ?? results.map((result) => result.source ?? result.path ?? result.title).filter(Boolean);
  return [
    row.run_id, row.experiment_id ?? row.experiment_group, row.system_id, row.system_name,
    row.system_commit ?? row.commit, row.provenance_label, row.provenance_origin, row.question_id,
    row.query, row.query_variant ?? "standard", row.condition_id, row.repeat, row.seed, row.mode,
    row.status, row.started_at, row.finished_at, row.latency_ms, row.tool_calls,
    row.result_count ?? results.length, asText(topSources), asText(row.queried_kb_ids), asText(row.channels),
    asText(row.route_detail), asText(row.degradation_detail ?? row.degradations), row.failure_detail,
    metric(row, "hit_at_5"), metric(row, "mrr"), metric(row, "citation_recall"),
    metric(row, "irrelevant_evidence_rate"), metric(row, "factual_correctness"), metric(row, "grounded_ratio"),
    metric(row, "conflict_handling"), metric(row, "abstention_score"), metric(row, "stale_fact_error"),
    row.answer, asText(row.citations), qualityScore(row), asText(row.condition), asText(row.adapter_metadata),
  ].map(asText);
});
const rawSheet = addDataSheet(
  "Raw Runs", "真实运行明细", rawHeaders, rawRows,
  [30,18,14,28,18,18,24,10,46,14,22,9,10,12,14,21,21,14,12,12,54,25,24,46,42,55,12,12,15,20,18,16,18,17,18,70,45,16],
  { freezeColumns: 3 },
);
const rawEnd = Math.max(5, rawRows.length + 4);
if (rawRows.length) {
  rawSheet.getRange(`P5:Q${rawEnd}`).format.numberFormat = "yyyy-mm-dd hh:mm:ss";
  rawSheet.getRange(`R5:R${rawEnd}`).format.numberFormat = "0.0";
  rawSheet.getRange(`AA5:AI${rawEnd}`).format.numberFormat = "0.0%";
  rawSheet.getRange(`AL5:AL${rawEnd}`).format.numberFormat = "0.0%";
  rawSheet.getRange(`O5:O${rawEnd}`).conditionalFormats.add("containsText", {
    text: "failed", format: { fill: COLORS.paleRed, font: { color: "#B91C1C", bold: true } },
  });
  rawSheet.getRange(`O5:O${rawEnd}`).conditionalFormats.add("containsText", {
    text: "unavailable", format: { fill: COLORS.paleAmber, font: { color: "#92400E", bold: true } },
  });
}

const R = {
  experiment: `'Raw Runs'!$B$5:$B$${rawEnd}`, system: `'Raw Runs'!$C$5:$C$${rawEnd}`,
  variant: `'Raw Runs'!$J$5:$J$${rawEnd}`, condition: `'Raw Runs'!$K$5:$K$${rawEnd}`,
  mode: `'Raw Runs'!$N$5:$N$${rawEnd}`, status: `'Raw Runs'!$O$5:$O$${rawEnd}`,
  latency: `'Raw Runs'!$R$5:$R$${rawEnd}`, calls: `'Raw Runs'!$S$5:$S$${rawEnd}`,
  hit: `'Raw Runs'!$AA$5:$AA$${rawEnd}`, mrr: `'Raw Runs'!$AB$5:$AB$${rawEnd}`,
  citation: `'Raw Runs'!$AC$5:$AC$${rawEnd}`, irrelevant: `'Raw Runs'!$AD$5:$AD$${rawEnd}`,
  abstention: `'Raw Runs'!$AH$5:$AH$${rawEnd}`, stale: `'Raw Runs'!$AI$5:$AI$${rawEnd}`,
  quality: `'Raw Runs'!$AL$5:$AL$${rawEnd}`,
};
const okCriteria = (extra = []) => [[R.status, "ok"], ...extra];
const directAverage = (column, predicate) => {
  const refs = rawRuns.flatMap((item, index) => predicate(item) ? [`'Raw Runs'!${column}${index + 5}`] : []);
  return refs.length ? `=AVERAGE(${refs.join(",")})` : '="n.a."';
};

const systemRows = systems.map((system) => [system.id, system.name ?? system.id, system.commit, system.status ?? "not_run", null, null, null, null, null, null, null].map(asText));
const systemSheet = addDataSheet(
  "System Comparison", "三方同语料对比",
  ["system_id","system_name","commit","system_status","retrieval_runs","hit_at_5","mrr","retrieval_p95_ms","answer_runs","answer_quality","citation_recall"],
  systemRows, [14,30,20,16,15,14,12,18,14,16,16],
);
systems.forEach((system, index) => {
  const row = index + 5;
  const retrievalCount = `COUNTIFS(${R.status},\"ok\",${R.experiment},\"system_retrieval\",${R.system},A${row},${R.mode},\"retrieval\")`;
  const answerCount = `COUNTIFS(${R.status},\"ok\",${R.experiment},\"system_answer\",${R.system},A${row},${R.mode},\"answer\")`;
  const retrievalArgs = `${R.status},\"ok\",${R.experiment},\"system_retrieval\",${R.system},A${row},${R.mode},\"retrieval\"`;
  const answerArgs = `${R.status},\"ok\",${R.experiment},\"system_answer\",${R.system},A${row},${R.mode},\"answer\"`;
  const metricCount = (range, args) => `COUNTIFS(${range},\">=0\",${args})`;
  writeFormulaRow(systemSheet, row, {
    E: `=${retrievalCount}`,
    F: `=IF(${metricCount(R.hit, retrievalArgs)}=0,\"n.a.\",AVERAGEIFS(${R.hit},${retrievalArgs}))`,
    G: `=IF(${metricCount(R.mrr, retrievalArgs)}=0,\"n.a.\",AVERAGEIFS(${R.mrr},${retrievalArgs}))`,
    H: `=IF(${retrievalCount}=0,\"n.a.\",SMALL(FILTER(${R.latency},(${R.status}=\"ok\")*(${R.experiment}=\"system_retrieval\")*(${R.system}=A${row})*(${R.mode}=\"retrieval\")),ROUNDUP(${retrievalCount}*0.95,0)))`,
    I: `=${answerCount}`,
    J: directAverage("AL", (item) => item.status === "ok" && item.experiment_id === "system_answer" && item.system_id === system.id && item.mode === "answer"),
    K: directAverage("AC", (item) => item.status === "ok" && item.experiment_id === "system_answer" && item.system_id === system.id && item.mode === "answer"),
  });
});
if (systems.length) {
  const end = systems.length + 4;
  systemSheet.getRange(`F5:G${end}`).format.numberFormat = "0.0%";
  systemSheet.getRange(`J5:K${end}`).format.numberFormat = "0.0%";
  systemSheet.getRange(`H5:H${end}`).format.numberFormat = "0.0";
}

const painDefinitions = [
  ...["keyword", "vector", "vector_keyword", "three_channel"].flatMap((condition) =>
    ["standard", "fuzzy"].map((variant) => ({ experiment: "fuzzy_query", condition, variant, label: `模糊 Query：${condition} / ${variant}` }))),
  ...["auto_route", "all_kbs", "gold_kbs"].map((condition) => ({ experiment: "routing", condition, variant: "standard", label: `多库路由：${condition}` })),
  ...["vector_normal", "vector_disabled", "embedding_error"].map((condition) => ({ experiment: "failure_degradation", condition, variant: "standard", label: `向量降级：${condition}` })),
  ...["F0", "F1", "F2", "F3"].map((condition) => ({ experiment: "historical_multikb", condition, variant: "standard", label: `证据新鲜度：${condition}` })),
];
const painRows = painDefinitions.map((item) => [item.experiment, item.condition, item.variant, item.label, null, null, null, null, null, null, null]);
const painSheet = addDataSheet(
  "Pain Point Tests", "检索痛点量化实验",
  ["experiment_id","condition_id","query_variant","comparison","successful_runs","hit_at_5","mrr","irrelevant_evidence_rate","p95_latency_ms","abstention_score","stale_fact_error_rate"],
  painRows, [20,22,14,35,16,14,12,24,18,18,22],
);
painDefinitions.forEach((item, index) => {
  const row = index + 5;
  const criteria = okCriteria([[R.experiment, item.experiment], [R.condition, item.condition], [R.variant, item.variant]]);
  const matches = (run) => run.status === "ok"
    && run.experiment_id === item.experiment
    && run.condition_id === item.condition
    && run.query_variant === item.variant;
  writeFormulaRow(painSheet, row, {
    E: `=${countIfs(criteria)}`,
    F: averageIfs(R.hit, criteria), G: averageIfs(R.mrr, criteria), H: averageIfs(R.irrelevant, criteria),
    I: p95Ifs(R.latency, criteria),
    J: directAverage("AH", matches),
    K: directAverage("AI", matches),
  });
});
painSheet.getRange(`F5:H${painRows.length + 4}`).format.numberFormat = "0.0%";
painSheet.getRange(`J5:K${painRows.length + 4}`).format.numberFormat = "0.0%";
painSheet.getRange(`I5:I${painRows.length + 4}`).format.numberFormat = "0.0";

const skillDefinitions = [
  ["fast_q01", "fast", "Q01", "vector", 1, "单次检索"],
  ["balanced_q06", "balanced", "Q06", "vector + keyword", 1, "单次检索，可带原始证据"],
  ["reasoning_q08", "reasoning", "Q08", "vector + keyword + graph", 4, "允许分解、图扩展和补检"],
  ["research_q19", "research", "Q19", "vector + keyword + graph", 8, "充分性检查与原文核验"],
  ["research_q20", "research", "Q20", "vector + keyword + graph", 8, "知识缺口时拒绝编造"],
];
const skillRows = skillDefinitions.map((row) => [...row, null, null, null, null]);
const skillSheet = addDataSheet(
  "Skill Regression", "统一 Skill 分级回归",
  ["condition_id","profile","question_id","expected_channels","max_tool_calls","expected_behavior","runs","mean_tool_calls","abstention_score","acceptance"],
  skillRows, [22,14,12,30,16,40,10,18,18,20],
);
skillDefinitions.forEach((item, index) => {
  const row = index + 5;
  const criteria = okCriteria([[R.experiment, "skill_regression"], [R.condition, item[0]]]);
  const allCriteria = [[R.experiment, "skill_regression"], [R.condition, item[0]]];
  const matches = (run) => run.status === "ok" && run.experiment_id === "skill_regression" && run.condition_id === item[0];
  writeFormulaRow(skillSheet, row, {
    G: `=${countIfs(criteria)}`, H: directAverage("S", matches), I: directAverage("AH", matches),
    J: `=IF(${countIfs(allCriteria)}=0,\"n.a.\",IF(AND(${countIfs(criteria)}>0,_xlfn.MAXIFS(${R.calls},${R.experiment},\"skill_regression\",${R.condition},A${row})<=E${row}),\"通过\",\"需检查\"))`,
  });
});
skillSheet.getRange(`I5:I${skillRows.length + 4}`).format.numberFormat = "0.0%";

const questionHeaders = ["id","question","fuzzy_question","category","target_kbs","expected_sources","required_facts","forbidden_facts","temporal_intent","conflict_expected","answerable"];
const questionRows = questions.map((question) => questionHeaders.map((header) => asText(question[header])));
addDataSheet("Queries & Rubric", "问题集与评分标准", questionHeaders, questionRows, [10,48,48,24,24,58,58,58,16,18,14], { freezeColumns: 2 });

const sourceSheet = workbook.worksheets.getItem("Sources & Setup");
styleTitle(sourceSheet, "来源与实验配置", "H");
sourceSheet.getRange("A5:H5").values = [["system_id","system_name","repository","commit","adapter","status","corpus_wiki_pages","notes"]];
styleHeader(sourceSheet.getRange("A5:H5"));
const sourceRows = systems.map((system) => [system.id, system.name, system.repository, system.commit, system.adapter, system.status, system.corpus_wiki_pages, system.notes].map(asText));
if (sourceRows.length) sourceSheet.getRange(`A6:H${sourceRows.length + 5}`).values = sourceRows;
sourceSheet.getRange("A11:B11").values = [["run_summary_field","value"]];
styleHeader(sourceSheet.getRange("A11:B11"));
const summaryEntries = Object.entries(report.run_summary ?? {});
if (summaryEntries.length) sourceSheet.getRange(`A12:B${summaryEntries.length + 11}`).values = summaryEntries.map(([key, value]) => {
  const displayed = (key === "started_at" || key === "finished_at") ? `${String(value).replace("T", " ")} HKT` : asText(value);
  return [key, displayed];
});
const historyStart = Math.max(15, summaryEntries.length + 14);
sourceSheet.getRange(`A${historyStart}:F${historyStart}`).values = [["historical_id","use","provenance_label","origin","status","notes"]];
styleHeader(sourceSheet.getRange(`A${historyStart}:F${historyStart}`));
const historyRows = asArray(report.historical_imports).map((item) => {
  const origin = item.origin ?? item.path ?? "";
  const inferredId = String(origin).split(/[\\/]/).filter(Boolean).at(-1) ?? "historical-import";
  return [
    item.id ?? inferredId,
    item.use ?? "historical reference",
    item.provenance_label ?? "historical_result",
    origin,
    item.status ?? "imported",
    item.notes ?? `${item.record_count ?? 0} records; kept separate from current runs`,
  ].map(asText);
});
if (historyRows.length) sourceSheet.getRange(`A${historyStart + 1}:F${historyStart + historyRows.length}`).values = historyRows;
else sourceSheet.getRange(`A${historyStart + 1}`).values = [["暂无已导入历史结果"]];
sourceSheet.showGridLines = false;
[28,32,52,58,20,24,20,60].forEach((width, index) => sourceSheet.getRange(`${columnName(index)}:${columnName(index)}`).format.columnWidth = width);
sourceSheet.getRange("A5:H200").format.font = { name: FONT, size: 10, color: COLORS.text };

const failureHeaders = ["run_id","experiment_id","system_id","question_id","condition_id","status","degradation_detail","failure_detail","provenance_origin"];
const failureRows = failures.map((row) => [row.run_id, row.experiment_id ?? row.experiment_group, row.system_id, row.question_id, row.condition_id, row.status, asText(row.degradation_detail ?? row.degradations), row.failure_detail, row.provenance_origin].map(asText));
const failureSheet = addDataSheet("Failures", "失败与降级记录", failureHeaders, failureRows, [30,20,14,12,22,16,45,65,35], { freezeColumns: 3 });
if (failureRows.length) failureSheet.getRange(`A5:I${failureRows.length + 4}`).format.fill = COLORS.paleRed;

const summarySheet = workbook.worksheets.getItem("Summary");
summarySheet.showGridLines = false;
summarySheet.tabColor = COLORS.navy;
styleTitle(summarySheet, "多知识库检索实验汇总", "Q");
summarySheet.getRange("A5:B9").values = [
  ["实验状态", rawRows.length ? "已载入真实运行" : "等待实验数据"], ["真实运行记录", rawRows.length],
  ["失败/不可用记录", failures.length], ["系统数量", systems.length], ["问题数量", questions.length],
];
summarySheet.getRange("A5:A9").format = { fill: COLORS.paleBlue, font: { name: FONT, bold: true, color: COLORS.text } };
summarySheet.getRange("B5:B9").format.font = { name: FONT, size: 11, color: COLORS.text };
summarySheet.getRange("A12:F12").values = [["系统","Hit@5","MRR","检索 p95 (ms)","回答质量","Citation Recall"]];
styleHeader(summarySheet.getRange("A12:F12"));
systems.forEach((system, index) => {
  const row = index + 13;
  const sourceRow = index + 5;
  summarySheet.getRange(`A${row}`).values = [[system.name ?? system.id]];
  summarySheet.getRange(`B${row}:F${row}`).formulas = [[`='System Comparison'!F${sourceRow}`, `='System Comparison'!G${sourceRow}`, `='System Comparison'!H${sourceRow}`, `='System Comparison'!J${sourceRow}`, `='System Comparison'!K${sourceRow}`]];
});
summarySheet.getRange(`B13:C${systems.length + 12}`).format.numberFormat = "0.0%";
summarySheet.getRange(`E13:F${systems.length + 12}`).format.numberFormat = "0.0%";
summarySheet.getRange(`D13:D${systems.length + 12}`).format.numberFormat = "0.0";
summarySheet.getRange("A20:E20").values = [["模糊 Query 配置","标准 Hit@5","口语 Hit@5","标准 p95 (ms)","口语 p95 (ms)"]];
styleHeader(summarySheet.getRange("A20:E20"));
["keyword", "vector", "vector_keyword", "three_channel"].forEach((condition, index) => {
  const row = index + 21;
  const standardRow = 5 + index * 2;
  summarySheet.getRange(`A${row}`).values = [[condition]];
  summarySheet.getRange(`B${row}:E${row}`).formulas = [[`='Pain Point Tests'!F${standardRow}`, `='Pain Point Tests'!F${standardRow + 1}`, `='Pain Point Tests'!I${standardRow}`, `='Pain Point Tests'!I${standardRow + 1}`]];
});
summarySheet.getRange("B21:C24").format.numberFormat = "0.0%";
summarySheet.getRange("D21:E24").format.numberFormat = "0.0";
summarySheet.getRange("H12:I12").values = [["检索 p95 (ms)", "回答质量"]];
styleHeader(summarySheet.getRange("H12:I12"));
systems.forEach((_, index) => {
  const row = index + 13;
  const sourceRow = index + 5;
  summarySheet.getRange(`H${row}:I${row}`).formulas = [[`='System Comparison'!H${sourceRow}`, `='System Comparison'!J${sourceRow}`]];
});
summarySheet.getRange("H13:H20").format.numberFormat = "0.0";
summarySheet.getRange("I13:I20").format.numberFormat = "0.0%";
const scatter = summarySheet.charts.add("scatter", summarySheet.getRange(`H12:I${systems.length + 12}`));
scatter.title = "回答质量与检索延迟";
scatter.titleTextStyle.typeface = FONT;
scatter.hasLegend = false;
scatter.xAxis = { numberFormatCode: "0", numberFormatSourceLinked: false, textStyle: { typeface: FONT } };
scatter.yAxis = { numberFormatCode: "0%", numberFormatSourceLinked: false, textStyle: { typeface: FONT } };
scatter.setPosition("K5", "Q17");
const fuzzyChart = summarySheet.charts.add("column", summarySheet.getRange("A20:C24"));
fuzzyChart.title = "标准与口语 Query 的 Hit@5";
fuzzyChart.titleTextStyle.typeface = FONT;
fuzzyChart.hasLegend = true;
fuzzyChart.legend = { position: "top", textStyle: { typeface: FONT } };
fuzzyChart.yAxis = { numberFormatCode: "0%", numberFormatSourceLinked: false, textStyle: { typeface: FONT } };
fuzzyChart.setPosition("K19", "Q32");
summarySheet.getRange("A28:G28").values = [["结论状态","三方对比","模糊 Query","多库路由","故障降级","新鲜度","Skill 回归"]];
styleHeader(summarySheet.getRange("A28:G28"));
summarySheet.getRange("A29:G29").formulas = [[
  '=IF(B6=0,"等待实验数据","按真实结果更新")', '=IF(SUM(\'System Comparison\'!E5:E7)=0,"n.a.","已计算")',
  '=IF(SUM(\'Pain Point Tests\'!E5:E12)=0,"n.a.","已计算")', '=IF(SUM(\'Pain Point Tests\'!E13:E15)=0,"n.a.","已计算")',
  '=IF(SUM(\'Pain Point Tests\'!E16:E18)=0,"n.a.","已记录")', '=IF(SUM(\'Pain Point Tests\'!E19:E22)=0,"n.a.","已计算")',
  '=IF(SUM(\'Skill Regression\'!G5:G9)=0,"n.a.","已检查")',
]];
summarySheet.getRange("A29:G29").format = { fill: COLORS.paleGreen, font: { name: FONT, bold: true, color: COLORS.text }, horizontalAlignment: "center" };
summarySheet.getRange("A32:B32").values = [["说明", "无真实结果时显示 n.a.；失败不计为 0；历史与当前运行分开标记。"]];
summarySheet.getRange("A32").format.font = { name: FONT, bold: true, color: COLORS.text };
summarySheet.getRange("B32").format = { font: { name: FONT, italic: true, color: COLORS.muted }, wrapText: true, verticalAlignment: "top" };
summarySheet.getRange("32:32").format.rowHeight = 36;
[24,32,16,18,18,18,18,16,16,3,16,16,16,16,16,16,16].forEach((width, index) => summarySheet.getRange(`${columnName(index)}:${columnName(index)}`).format.columnWidth = width);

for (const name of ["System Comparison", "Pain Point Tests", "Skill Regression"]) workbook.worksheets.getItem(name).tabColor = name === "System Comparison" ? COLORS.blue : COLORS.teal;
workbook.recalculate();
await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(previewDir, { recursive: true });
const previewRanges = {
  Summary: "A1:Q34", "System Comparison": `A1:K${Math.max(8, systems.length + 4)}`,
  "Pain Point Tests": `A1:K${painRows.length + 4}`, "Skill Regression": "A1:J9",
  "Raw Runs": `A1:AN${Math.min(Math.max(8, rawEnd), 25)}`,
  "Queries & Rubric": `A1:K${Math.min(Math.max(8, questionRows.length + 4), 20)}`,
  "Sources & Setup": `A1:H${Math.max(historyStart + historyRows.length + 2, 20)}`,
  Failures: `A1:I${Math.min(Math.max(8, failureRows.length + 4), 25)}`,
};
for (const [sheetName, range] of Object.entries(previewRanges)) {
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  await fs.writeFile(path.join(previewDir, `${sheetName.replaceAll(" ", "-")}.png`), new Uint8Array(await preview.arrayBuffer()));
}
const summaryCheck = await workbook.inspect({ kind: "table", range: "Summary!A1:I29", include: "values,formulas", tableMaxRows: 29, tableMaxCols: 9 });
const errorScan = await workbook.inspect({
  kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!|e\\.reduce is not a function",
  options: { useRegex: true, maxResults: 300 }, summary: "final formula error scan",
});
if (/"(?:value|text)":"(?:#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!|SPILL!|CALC!)|e\.reduce is not a function)/.test(errorScan.ndjson)) throw new Error(`formula error detected: ${errorScan.ndjson}`);
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(summaryCheck.ndjson);
console.log(errorScan.ndjson);
console.log(JSON.stringify({ outputPath, previewDir, sheets: Object.keys(previewRanges) }));
