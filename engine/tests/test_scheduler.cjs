const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../ops/apps_script_scheduler.gs'), 'utf8');

function scheduler(instant, scriptApp) {
  const calls = [];
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [instant])); }
  }
  let released = false;
  const context = vm.createContext({
    Date: Clock, console: { log() {} }, ScriptApp: scriptApp,
    LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() { released = true; } }) },
    triggerGitHubWorkflow: (file, payload) => calls.push(JSON.parse(JSON.stringify({ file, payload })))
  });
  vm.runInContext(source, context);
  return { context, calls, released: () => released };
}

for (let day = 1; day <= 7; day++) {
  // November 1, 2026 is Sunday. +09:00 exercises the UTC date boundary too.
  const date = `2026-11-0${day}`;
  test(`morning ALL dispatch uses Korean weekday: ${date}`, () => {
    const { context, calls } = scheduler(`${date}T07:00:00+09:00`);
    context.runMorningWatchlist();
    const expected = day >= 3 && day <= 7; // Tuesday through Saturday
    assert.equal(calls.length, expected ? 1 : 0);
    if (expected) assert.deepEqual(calls[0], {
      file: 'watchlist.yml', payload: { ref: 'main', inputs: { market: 'ALL', refresh_cache: 'false' } }
    });
  });
  test(`afternoon ALL dispatch uses Korean weekday: ${date}`, () => {
    const { context, calls } = scheduler(`${date}T19:00:00+09:00`);
    context.runAfternoonWatchlist();
    const expected = day >= 2 && day <= 6; // Monday through Friday
    assert.equal(calls.length, expected ? 1 : 0);
    if (expected) assert.deepEqual(calls[0], {
      file: 'watchlist.yml', payload: { ref: 'main', inputs: { market: 'ALL', refresh_cache: 'false' } }
    });
  });
}

for (const [instant, expected] of [
  ['2026-11-01T20:00:00Z', false], ['2026-11-01T21:00:00Z', true],
  ['2026-11-01T22:00:00Z', false], ['2026-11-02T00:00:00Z', true],
  ['2026-11-06T23:00:00Z', true], ['2026-11-07T00:00:00Z', true],
  ['2026-11-07T01:00:00Z', true], ['2026-11-07T02:00:00Z', false]
]) {
  test(`hourly Collector preserves original window without Watchlist: ${instant}`, () => {
    const { context, calls } = scheduler(instant);
    context.runHourlyTasks();
    assert.equal(calls.length, expected ? 1 : 0);
    if (expected) assert.deepEqual(calls[0], {
      file: 'trading_journal.yml', payload: { ref: 'main', inputs: { mode: 'auto' } }
    });
  });
}

function triggersMock(failOnCreate = Infinity) {
  const original = ['runHourlyTasks', 'unrelatedTask'].map(handler => ({
    handler, getHandlerFunction() { return this.handler; }
  }));
  let triggers = [...original], attempts = 0;
  const api = {
    getProjectTriggers: () => [...triggers],
    deleteTrigger(t) { triggers = triggers.filter(item => item !== t); },
    newTrigger(handler) {
      const settings = { handler, getHandlerFunction() { return this.handler; } };
      const builder = { create() {
        if (++attempts === failOnCreate) throw new Error('synthetic trigger creation failure');
        triggers.push(settings);
        return settings;
      } };
      for (const name of ['timeBased', 'everyHours', 'inTimezone', 'atHour', 'nearMinute', 'everyDays']) {
        builder[name] = value => { settings[name] = value; return builder; };
      }
      return builder;
    }
  };
  return { api, original, current: () => triggers };
}

test('setup installs three schedules, preserves unrelated triggers and is repeatable', () => {
  const mock = triggersMock();
  const { context, calls, released } = scheduler('2026-11-03T00:00:00Z', mock.api);
  context.configureSchedulerTriggers();
  context.configureSchedulerTriggers();
  const triggers = mock.current();
  assert.equal(triggers.length, 4);
  assert.ok(triggers.includes(mock.original[1]));
  const hourly = triggers.find(t => t.handler === 'runHourlyTasks');
  assert.equal(hourly.everyHours, 1);
  for (const [handler, hour, minute] of [
    ['runMorningWatchlist', 7, 0], ['runAfternoonWatchlist', 19, 0]
  ]) {
    const trigger = triggers.find(t => t.handler === handler);
    assert.equal(trigger.atHour, hour);
    assert.equal(trigger.nearMinute, minute);
    assert.equal(trigger.inTimezone, 'Asia/Seoul');
    assert.equal(trigger.everyDays, 1);
  }
  assert.deepEqual(calls, []); // Setup must never dispatch production jobs.
  assert.equal(released(), true);
});

test('creation failure removes new triggers and leaves the original schedules', () => {
  const mock = triggersMock(2);
  const { context, released } = scheduler('2026-11-03T00:00:00Z', mock.api);
  assert.throws(() => context.configureSchedulerTriggers(), /synthetic trigger creation failure/);
  assert.deepEqual(mock.current(), mock.original);
  assert.equal(released(), true);
});

