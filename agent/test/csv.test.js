import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function fixture(failSymbol = false) {
    const hooks = new Map();
    let timer = null;
    const code = ts.transpileModule(fs.readFileSync(new URL('../src/native/csv.ts', import.meta.url), 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    const context = {
        exports: {},
        require: () => ({ ensureResolved: () => ({
            module: { getExportByName: name => { if (failSymbol) throw Error('unsupported'); return name; } },
            profile: { symbols: { storageSetWaveDepth: 'source', saveRecordAsCsv: 'writer' } },
        }) }),
        ptr: n => ({ toInt32: () => n }),
        setTimeout: fn => { timer = fn; return 1; },
        clearTimeout: () => { timer = null; },
        Interceptor: {
            attach: (name, callbacks) => {
                hooks.set(name, callbacks);
                return { detach: () => hooks.delete(name) };
            },
            flush: () => {},
        },
    };
    vm.runInNewContext(code, context);
    return { api: context.exports, hooks, ptr: context.ptr, expire: () => timer() };
}

test('one source substitution; record-writer completion detaches both hooks', () => {
    const { api, hooks, ptr } = fixture();
    api.armRecordCsv(1000);
    assert.throws(() => api.armRecordCsv(1000), /already armed/);
    const args = [ptr(123), ptr(1)];
    hooks.get('source').onEnter(args);
    assert.equal(args[1].toInt32(), 2);
    assert.equal(hooks.has('source'), false);
    assert.equal(api.recordCsvStatus().finished, 0);
    const writer = hooks.get('writer');
    writer.onEnter(); writer.onLeave();
    assert.equal(api.recordCsvStatus().finished, 1);
    assert.equal(api.recordCsvStatus().armed, false);
    assert.equal(hooks.size, 0);
});

test('wrong source is not redirected and timeout removes idle hooks', () => {
    const { api, hooks, ptr, expire } = fixture();
    api.armRecordCsv(1000);
    const args = [ptr(123), ptr(0)];
    hooks.get('source').onEnter(args);
    assert.equal(args[1].toInt32(), 0);
    assert.equal(api.recordCsvStatus().redirected, 0);
    expire();
    assert.equal(api.recordCsvStatus().expired, true);
    assert.equal(hooks.size, 0);
});

test('explicit cleanup disarms before another export and missing symbols fail closed', () => {
    const { api, hooks } = fixture();
    api.armRecordCsv(1000); api.disarmRecordCsv();
    assert.equal(hooks.size, 0);
    assert.equal(api.armRecordCsv(1000).redirected, 0);
    api.disarmRecordCsv();
    const bad = fixture(true);
    assert.throws(() => bad.api.armRecordCsv(1000), /unsupported/);
    assert.equal(bad.hooks.size, 0);
    assert.equal(bad.api.recordCsvStatus().armed, false);
});
