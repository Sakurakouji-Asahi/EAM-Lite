const test = require("node:test");
const assert = require("node:assert/strict");
const {parseUsageMicroUnits: parse, formatUsageMicroUnits: format} = require("../../static/js/monthly-usage.js");

test("input totals keep decimal precision and do not use binary floating point", () => {
  assert.equal(format(parse("0.1") + parse("0.2")), "0.3");
  assert.equal(format(parse("999999999999999999.999999") + parse("0.000001")), "1000000000000000000");
  assert.equal(format(parse("0.123456") + parse("0.000001")), "0.123457");
});

test("zero, leading zeros and accepted decimal notations remain exact", () => {
  for (const value of ["0", "-0", "0.000000", "0e50"]) assert.equal(parse(value), 0n);
  assert.equal(parse(" 0001.200000 "), 1200000n);
  assert.equal(parse(".5"), 500000n);
  assert.equal(parse("1e-6"), 1n);
  assert.equal(parse("1.2345670e1"), 12345670n);
});

test("missing values, negatives and unsupported precision are not silently counted", () => {
  for (const value of ["", " ", "abc", "NaN", "Infinity", "-0.1", "1e-7", "0.1234567", "0.0000000", "1000000000000000000", "1e100000"])
    assert.equal(parse(value), null, value);
});
