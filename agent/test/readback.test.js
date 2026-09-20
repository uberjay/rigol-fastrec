import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function fixture() {
    // Expose the private helper only in this VM; all memory reads are faked.
    const source = fs.readFileSync(new URL('../src/native/readback.ts', import.meta.url), 'utf8');
    const code = ts.transpileModule(source + '\nexport { clampChunk };', {
        compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020},
    }).outputText;
    const context = {exports: {}, require: () => ({})};
    vm.runInNewContext(code, context);
    return (raw, {total = 253000, geometry = '1000:2:3'} = {}) => {
        const scope = {add() {return this;}, readU32() {
            if (raw instanceof Error) throw raw;
            return raw;
        }};
        return context.exports.clampChunk(scope, {drvParam:0x5a00, dwMaxFrameCount:0x60}, total, geometry);
    };
}

test('valid replay caps bound chunks, including short tails and cap=1', () => {
    for (const cap of [1, 16, 500, 8192]) {
        const clamp = fixture();
        assert.equal(clamp(cap).chunk, cap);
        assert.equal(clamp(cap, {total:1}).chunk, 1);
    }
});

test('invalid or unreadable caps never become full-record DMA requests', () => {
    for (const raw of [0, 8193, 253952, 0xffffffff, new Error('unreadable')]) {
        const clamp = fixture();
        assert.equal(clamp(raw).chunk, 1);
        // The fallback must not be cached: a later valid read restores throughput.
        assert.equal(clamp(500).chunk, 500);
    }
});

test('validated cap survives racy reads and cannot grow within one geometry', () => {
    const clamp = fixture();
    assert.equal(clamp(500).chunk, 500);
    for (const raw of [0, 253952, 8192, new Error('unreadable')]) {
        const result = clamp(raw);
        assert.equal(result.chunk, 500);
        assert.equal(result.hwRaw, raw instanceof Error ? null : raw);
    }
    assert.equal(clamp(250).chunk, 250);
    assert.equal(clamp(500).chunk, 250);
});

test('changing depth or channel layout discards the previous cap', () => {
    const clamp = fixture();
    assert.equal(clamp(500).chunk, 500);
    assert.equal(clamp(0, {geometry:'10000:2:3'}).chunk, 1);
    assert.equal(clamp(50, {geometry:'10000:2:3'}).chunk, 50);
    assert.equal(clamp(0, {geometry:'10000:4:15'}).chunk, 1);
    assert.equal(clamp(25, {geometry:'10000:4:15'}).chunk, 25);
    assert.equal(clamp(500).chunk, 500);
});
