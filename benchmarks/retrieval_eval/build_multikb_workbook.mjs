import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [reportPath, outputPath, previewDir] = process.argv.slice(2);
if (!reportPath || !outputPath || !previewDir) {
  throw new Error("usage: node build_multikb_workbook.mjs REPORT_JSON OUTPUT_XLSX PREVIEW_DIR");
}

const report = JSON.parse(await fs.readFile(reportPath, "utf8"));
if (report.run_summary.degraded_runs) {
  for (const group of [report.aggregate, report.pareto]) for (const row of group ?? []) {
    if (row.config_id !== "C02") row.name += " (vector disabled)";
  }
}
const workbook = Workbook.create();

const colors = {
  navy: "#17365D",
  blue: "#1F4E78",
  teal: "#0F766E",
  paleBlue: "#D9EAF7",
  paleGreen: "#DFF2E1",
  paleRed: "#FCE8E6",
  paleAmber: "#FFF2CC",
  white: "#FFFFFF",
  text: "#1F2937",
  muted: "#64748B",
  border: "#CBD5E1",
};

function matrix(rows, columns) {
  return rows.map((row) => columns.map((column) => row[column] ?? null));
}

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

function setupSheet(name, title, headers, rows, options = {}) {
  const sheet = workbook.worksheets.add(name);
  sheet.showGridLines = false;
  const lastColumn = columnName(Math.max(0, headers.length - 1));
  sheet.getRange("A1").values = [[title]];
  sheet.getRange(`A1:${lastColumn}1`).format = {
    font: { name: "Arial", bold: true, color: colors.text, size: 13 },
    verticalAlignment: "center",
  };
  sheet.getRange(`A2:${lastColumn}2`).values = [headers];
  sheet.getRange(`A2:${lastColumn}2`).format = {
    fill: colors.blue,
    font: { bold: true, color: colors.white },
    wrapText: true,
    verticalAlignment: "center",
    borders: { preset: "outside", style: "thin", color: colors.border },
  };
  if (rows.length) {
    sheet.getRange(`A3:${lastColumn}${rows.length + 2}`).values = rows;
    sheet.getRange(`A3:${lastColumn}${rows.length + 2}`).format = {
      font: { name: "Arial", color: colors.text, size: 11 },
      verticalAlignment: "top",
      wrapText: true,
      borders: {
        insideHorizontal: { style: "thin", color: colors.border },
        bottom: { style: "thin", color: colors.border },
      },
    };
  }
  if (rows.length > 15) sheet.freezePanes.freezeRows(2);
  if (options.freezeColumns) sheet.freezePanes.freezeColumns(options.freezeColumns);
  headers.forEach((_, index) => {
    const width = options.widths?.[index] ?? 16;
    sheet.getRange(`${columnName(index)}:${columnName(index)}`).format.columnWidth = width;
  });
  sheet.getRange("1:1").format.rowHeight = 28;
  sheet.getRange("2:2").format.rowHeight = 34;
  rows.forEach((row, index) => {
    const lines = Math.max(...row.map((value, col) => String(value ?? "").split("\n").reduce((n, s) => n + Math.max(1, Math.ceil(s.length / ((options.widths?.[col] ?? 16) * 0.65))), 0)));
    sheet.getRange(`${index + 3}:${index + 3}`).format.rowHeight = Math.min(409, Math.max(30, lines * 15));
  });
  return sheet;
}

const summaryRows = Object.entries(report.run_summary ?? {}).map(([field, value]) => [field, value]);
summaryRows.push(
  ["Weight - factual correctness", 0.35],
  ["Weight - citation recall", 0.25],
  ["Weight - grounded ratio", 0.20],
  ["Weight - conflict handling", 0.10],
  ["Weight - abstention", 0.10],
);
const summary = setupSheet(
  "Run Summary",
  "Multi-KB Agentic Retrieval Evaluation",
  ["Field", "Value"],
  summaryRows,
  { widths: [34, 86] },
);
summary.getRange(`B${summaryRows.length - 4 + 2}:B${summaryRows.length + 2}`).format.numberFormat = "0%";
for (let i = 0; i < summaryRows.length; i++) {
  if (["started_at", "finished_at"].includes(summaryRows[i][0])) summary.getRange(`B${i+3}`).format.numberFormat = "yyyy-mm-dd hh:mm:ss";
}

const sourceColumns = [
  "kb_id", "source_id", "title", "publisher", "published_at", "effective_at",
  "accessed_at", "license", "sha256", "source_url", "local_path", "status", "notes",
];
setupSheet(
  "Source Manifest",
  "Source Provenance",
  sourceColumns,
  matrix(report.source_manifest ?? [], sourceColumns),
  { freezeColumns: 2, widths: [16, 24, 34, 24, 16, 16, 20, 20, 40, 55, 48, 16, 50] },
);

const questionColumns = [
  "id", "question", "category", "target_kbs", "expected_sources", "required_facts",
  "forbidden_facts", "temporal_intent", "conflict_expected", "answerable",
];
const questionRows = (report.questions ?? []).map((row) => questionColumns.map((column) => {
  const value = row[column];
  return Array.isArray(value) ? value.join("\n") : value;
}));
setupSheet(
  "Questions",
  "Twenty-Question Evaluation Set",
  questionColumns,
  questionRows,
  { freezeColumns: 2, widths: [10, 46, 24, 24, 52, 58, 58, 16, 18, 14] },
);

const retrievalColumns = [
  "run_id", "question_id", "config_id", "repeat", "seed", "status", "kb_ids",
  "latency_ms", "tool_calls", "result_count", "top_sources", "route_reason", "failure_detail",
];
const retrievalRows = (report.retrieval_runs ?? []).map((row) => retrievalColumns.map((column) => {
  const value = row[column];
  return Array.isArray(value) ? value.join("\n") : value;
}));
const retrievalSheet = setupSheet(
  "Retrieval Runs",
  "Raw Retrieval Runs",
  retrievalColumns,
  retrievalRows,
  { freezeColumns: 3, widths: [38, 12, 12, 10, 14, 14, 24, 16, 12, 14, 62, 48, 55] },
);
if (retrievalRows.length) retrievalSheet.getRange(`H3:H${retrievalRows.length + 2}`).format.numberFormat = "0.0";

const answerColumns = [
  "run_id", "question_id", "config_id", "repeat", "factual_correctness", "citation_recall",
  "grounded_ratio", "conflict_handling", "abstention_score", "quality_score", "hit_at_5",
  "latency_ms", "tool_calls", "stale_fact_error", "answer", "citations", "notes",
];
const answerRows = (report.answer_scores ?? []).map((row) => answerColumns.map((column) => {
  const value = row[column];
  return Array.isArray(value) ? value.join("\n") : value;
}));
const answerSheet = setupSheet(
  "Answer Scores",
  "Extractive proxy scores (not validated answer quality)",
  answerColumns,
  answerRows,
  { freezeColumns: 4, widths: [38, 12, 12, 10, 15, 15, 15, 15, 15, 15, 12, 16, 12, 16, 72, 55, 45] },
);
if (answerRows.length) {
  const lastRow = answerRows.length + 2;
  const weightStart = summaryRows.length - 2;
  answerSheet.getRange("J3").formulas = [["=" + ["E", "F", "G", "H", "I"].map((col, i) => `${col}3*'Run Summary'!$B$${weightStart+i}`).join("+")]];
  answerSheet.getRange(`J3:J${lastRow}`).fillDown();
  answerSheet.getRange(`E3:K${lastRow}`).format.numberFormat = "0.0%";
  answerSheet.getRange(`L3:L${lastRow}`).format.numberFormat = "0.0";
  for (let i = 0; i < report.answer_scores.length; i++) {
    if (report.answer_scores[i].stale_fact_error_measured === false) answerSheet.getRange(`N${i+3}`).values = [[null]];
  }
}

const freshnessColumns = [
  "config_id", "sample_count", "quality_score", "hit_at_5", "stale_fact_error_rate",
  "p95_latency_ms", "ordinary_quality_drop", "stale_error_reduction", "promote", "decision_reason",
];
const freshnessSheet = setupSheet(
  "Freshness Ablation",
  "Claim Freshness Ablation (F0-F3)",
  freshnessColumns,
  matrix(report.freshness_ablation ?? [], freshnessColumns),
  { widths: [12, 14, 16, 12, 22, 18, 22, 22, 14, 58] },
);
if ((report.freshness_ablation ?? []).length) {
  const lastRow = report.freshness_ablation.length + 2;
  freshnessSheet.getRange(`C3:E${lastRow}`).format.numberFormat = "0.0%";
  freshnessSheet.getRange(`G3:H${lastRow}`).format.numberFormat = "0.0%";
  freshnessSheet.getRange(`F3:F${lastRow}`).format.numberFormat = "0.0";
}

const aggregateColumns = [
  "config_id", "name", "sample_count", "quality_score", "hit_at_5", "p95_latency_ms",
  "mean_tool_calls", "stale_fact_error_rate", "is_pareto", "promote", "decision_reason",
];
const aggregateSheet = setupSheet(
  "Aggregate",
  "Configuration Summary",
  aggregateColumns,
  matrix(report.aggregate ?? [], aggregateColumns),
  { widths: [12, 34, 14, 16, 12, 18, 18, 22, 14, 14, 58] },
);
if ((report.aggregate ?? []).length) {
  const lastRow = report.aggregate.length + 2;
  const end = answerRows.length + 2;
  for (let row = 3; row <= lastRow; row++) {
    const match = `'Answer Scores'!$C$3:$C$${end},A${row}`;
    aggregateSheet.getRange(`C${row}:E${row}`).formulas = [[`=COUNTIF(${match})`, `=AVERAGEIF(${match},'Answer Scores'!$J$3:$J$${end})`, `=AVERAGEIF(${match},'Answer Scores'!$K$3:$K$${end})`]];
    aggregateSheet.getRange(`G${row}`).formulas = [[`=AVERAGEIF(${match},'Answer Scores'!$M$3:$M$${end})`]];
  }
  aggregateSheet.getRange(`D3:E${lastRow}`).format.numberFormat = "0.0%";
  aggregateSheet.getRange(`H3:H${lastRow}`).format.numberFormat = "0.0%";
  aggregateSheet.getRange(`F3:G${lastRow}`).format.numberFormat = "0.0";
  aggregateSheet.getRange(`D3:D${lastRow}`).conditionalFormats.add("colorScale", {
    colors: ["#FCA5A5", "#FDE68A", "#86EFAC"], thresholds: ["min", "50%", "max"],
  });
}

const paretoColumns = ["config_id", "name", "quality_score", "hit_at_5", "p95_latency_ms", "mean_tool_calls"];
const paretoSheet = setupSheet(
  "Pareto",
  "Quality-Latency Pareto Frontier",
  paretoColumns,
  matrix(report.pareto ?? [], paretoColumns),
  { widths: [14, 36, 18, 14, 20, 18] },
);
if ((report.pareto ?? []).length) {
  const lastRow = report.pareto.length + 2;
  paretoSheet.getRange(`C3:D${lastRow}`).format.numberFormat = "0.0%";
  paretoSheet.getRange(`E3:F${lastRow}`).format.numberFormat = "0.0";
}

const failureColumns = ["run_id", "question_id", "config_id", "repeat", "status", "failure_detail"];
const failures = setupSheet(
  "Failures",
  "Failures and Degradations",
  failureColumns,
  matrix(report.failures ?? [], failureColumns),
  { widths: [40, 14, 14, 10, 18, 90] },
);
if ((report.failures ?? []).length) {
  failures.getRange(`A3:F${report.failures.length + 2}`).format.fill = colors.paleRed;
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(previewDir, { recursive: true });
for (const name of [
  "Run Summary", "Source Manifest", "Questions", "Retrieval Runs", "Answer Scores",
  "Freshness Ablation", "Aggregate", "Pareto", "Failures",
]) {
  const lastColumn = {"Run Summary":"B", "Source Manifest":"M", "Questions":"J", "Retrieval Runs":"M", "Answer Scores":"Q", "Freshness Ablation":"J", "Aggregate":"K", "Pareto":"F", "Failures":"F"}[name];
  const preview = await workbook.render({ sheetName: name, range: `A1:${lastColumn}${name === "Run Summary" ? summaryRows.length+2 : 8}`, scale: 1, format: "png" });
  await fs.writeFile(path.join(previewDir, `${name.replaceAll(" ", "-")}.png`), new Uint8Array(await preview.arrayBuffer()));
}

const errorScan = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
if (/"(?:value|text)":"#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A)/.test(errorScan.ndjson)) {
  throw new Error(`formula error detected: ${errorScan.ndjson}`);
}
console.log((await workbook.inspect({kind:"table", range:"Aggregate!A1:H11", include:"values,formulas", tableMaxRows:11, tableMaxCols:8})).ndjson);
console.log(errorScan.ndjson);

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({ outputPath, previewDir, sheets: 9 }));
