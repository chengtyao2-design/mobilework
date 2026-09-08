export function directAverageFormula(rawRuns, column, predicate, firstDataRow = 5) {
  const refs = rawRuns.flatMap((item, index) => predicate(item)
    ? [`'Raw Runs'!${column}${index + firstDataRow}`]
    : []);
  if (!refs.length) return '="n.a."';
  const args = refs.join(",");
  return `=IF(COUNT(${args})=0,"n.a.",AVERAGE(${args}))`;
}

export function directP95Formula(rawRuns, column, predicate, firstDataRow = 5) {
  const refs = rawRuns.flatMap((item, index) => predicate(item)
    ? [`'Raw Runs'!${column}${index + firstDataRow}`]
    : []);
  if (!refs.length) return '="n.a."';
  const args = refs.join(",");
  const positions = refs.map((_, index) => index + 1).join(",");
  return `=IF(COUNT(${args})=0,"n.a.",SMALL(CHOOSE({${positions}},${args}),ROUNDUP(COUNT(${args})*0.95,0)))`;
}
