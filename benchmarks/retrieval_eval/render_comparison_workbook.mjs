import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [inputPath, outputDir] = process.argv.slice(2);
if (!inputPath || !outputDir) throw new Error("usage: node render_comparison_workbook.mjs INPUT_XLSX OUTPUT_DIR");

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
await fs.mkdir(outputDir, { recursive: true });
const ranges = {
  Summary: "A1:Q34", "System Comparison": "A1:K8", "Pain Point Tests": "A1:K23",
  "Skill Regression": "A1:J9", "Raw Runs": "A1:AR25", "Queries & Rubric": "A1:K20",
  "Sources & Setup": "A1:H30", Failures: "A1:M25",
};
for (const [sheetName, range] of Object.entries(ranges)) {
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  await fs.writeFile(path.join(outputDir, `${sheetName.replaceAll(" ", "-")}.png`), new Uint8Array(await preview.arrayBuffer()));
}
const errorScan = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 300 },
  summary: "recalculated workbook error scan",
});
console.log(errorScan.ndjson);
console.log(JSON.stringify({ inputPath, outputDir, sheets: Object.keys(ranges) }));
