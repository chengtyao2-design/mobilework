import assert from "node:assert/strict";
import test from "node:test";

import { directAverageFormula, directP95Formula } from "../benchmarks/retrieval_eval/workbook_formulas.mjs";

test("direct average returns n.a. when no run matches", () => {
  assert.equal(directAverageFormula([], "AH", () => true), '="n.a."');
});

test("direct average guards a matched range whose metric cells are all blank", () => {
  assert.equal(
    directAverageFormula([{ value: null }, { value: null }], "AH", () => true),
    '=IF(COUNT(\'Raw Runs\'!AH5,\'Raw Runs\'!AH6)=0,"n.a.",AVERAGE(\'Raw Runs\'!AH5,\'Raw Runs\'!AH6))',
  );
});

test("direct average keeps numeric zero in a mixed null zero one population", () => {
  const formula = directAverageFormula(
    [{ value: null }, { value: 0 }, { value: 1 }],
    "AH",
    () => true,
  );
  assert.match(formula, /^=IF\(COUNT\(/);
  assert.match(formula, /AVERAGE\(/);
  assert.match(formula, /AH5/);
  assert.match(formula, /AH6/);
  assert.match(formula, /AH7/);
});

test("direct p95 uses legacy-compatible SMALL and CHOOSE over matching raw cells", () => {
  const formula = directP95Formula([{ ok: true }, { ok: false }, { ok: true }], "R", (row) => row.ok);
  assert.equal(
    formula,
    '=IF(COUNT(\'Raw Runs\'!R5,\'Raw Runs\'!R7)=0,"n.a.",SMALL(CHOOSE({1,2},\'Raw Runs\'!R5,\'Raw Runs\'!R7),ROUNDUP(COUNT(\'Raw Runs\'!R5,\'Raw Runs\'!R7)*0.95,0)))',
  );
});
