// Reporting regressions use the shipped implementation without a browser or DOM.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(new URL('../site/index.html', import.meta.url), 'utf8');
const source = html.split('// ---- review model: pure reporting functions, never release policy ----')[1]
  .split('// ---- end pure review model ----')[0];
const model = vm.createContext({});
vm.runInContext(source, model);
const base = { live: false, runEnded: true, activeHostile: 0, unresolved: 0, pending: 0, leakers: 0, defeated: 1 };

test('all shipped inline JavaScript parses', () => {
  for (const match of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
});
test('an open run cannot claim objectives met even after a defeat', () => {
  assert.match(model.reviewVerdict({ ...base, runEnded: false }), /^INTERIM REVIEW/);
});
for (const field of ['activeHostile', 'unresolved', 'pending']) {
  test(`an ended run remains incomplete when ${field} is present`, () => {
    assert.match(model.reviewVerdict({ ...base, [field]: 1 }), /REVIEW INCOMPLETE/);
  });
}
test('recorded arrivals prevent an objectives-met result', () => {
  assert.match(model.reviewVerdict({ ...base, leakers: 1 }), /OBJECTIVES NOT MET/);
});
test('a run with no outcome has no success result', () => {
  assert.match(model.reviewVerdict({ ...base, defeated: 0 }), /No defense outcome established/);
});
test('only a closed and fully accounted run can meet the illustrative objective', () => {
  assert.match(model.reviewVerdict(base), /^EXERCISE OBJECTIVES MET/);
  assert.match(model.reviewVerdict(base), /Simulated result only/);
});
test('live data never establishes mission completion', () => {
  assert.match(model.reviewVerdict({ ...base, live: true }), /^LIVE SNAPSHOT/);
});
test('empty denominators remain unavailable instead of showing zero effectiveness', () => {
  assert.equal(model.reviewPercent(0, 0), null);
  assert.equal(model.reviewPercent(0, 5), 0);
  assert.equal(model.reviewPercent(1, 4), 25);
});
test('source, receipt, and displayed-sample ages remain independent', () => {
  const f = model.reportFreshness({ lastUpdate: 10000, syncT: 9000, displayObservedAt: 7500 }, false, 12000);
  assert.equal(f.observed, 2);
  assert.equal(f.received, 3);
  assert.equal(f.displayed, 4.5);
});
test('a fresh live poll does not make an old source observation fresh', () => {
  const f = model.reportFreshness({ receivedAt: 9000, sourceTimeKnown: true, sourceAgeAtReceipt: 7 }, true, 10000);
  assert.equal(f.received, 1);
  assert.equal(f.observed, 8);
  assert.equal(f.displayed, 8);
});
test('missing source timestamps remain unknown, even with a fresh receipt', () => {
  const live = model.reportFreshness({ receivedAt: 10000, sourceTimeKnown: false }, true, 10000);
  assert.equal(live.observed, null);
  assert.equal(live.displayed, null);
  assert.equal(live.received, 0);
  const sim = model.reportFreshness({ lastUpdate: null }, false, 10000);
  assert.equal(sim.observed, null);
  assert.equal(sim.displayed, null);
});
test('replay sampling preserves the full time span and order', () => {
  const frames = Array.from({ length: 9001 }, (_, i) => ({ t: i * 500, snap: [[i, 0]] }));
  const sampled = model.sampleReplay(frames);
  assert.equal(sampled.sampled, true);
  assert.ok(sampled.frames.length <= 7200);
  assert.equal(sampled.frames[0], frames[0]);
  assert.equal(sampled.frames.at(-1), frames.at(-1));
  assert.ok(sampled.frames.every((f, i, a) => i === 0 || f.t > a[i - 1].t));
  assert.equal(frames.length, 9001);
});
test('dense replay sampling respects the point budget while keeping endpoints', () => {
  const frames = Array.from({ length: 601 }, (_, i) => ({ t: i * 500, snap: Array(1000).fill([1, 2]) }));
  const sampled = model.sampleReplay(frames);
  assert.ok(sampled.points <= 600000);
  assert.equal(sampled.frames[0].t, 0);
  assert.equal(sampled.frames.at(-1).t, 300000);
});
test('ordinary fifteen-minute replay is retained without sampling', () => {
  const frames = Array.from({ length: 1801 }, (_, i) => ({ t: i * 500, snap: [[1, 2]] }));
  const sampled = model.sampleReplay(frames);
  assert.equal(sampled.sampled, false);
  assert.equal(sampled.frames, frames);
});
