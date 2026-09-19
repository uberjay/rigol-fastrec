import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function module(name, globals = {}) {
    const code = ts.transpileModule(fs.readFileSync(new URL(`../src/native/${name}.ts`, import.meta.url), 'utf8'), {
        compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020},
    }).outputText;
    const context = {exports: {}, ...globals};
    vm.runInNewContext(code, context);
    return context.exports;
}
const decoder = module('timestamp_decode');
const period = 1n << 48n;
function headers(tags, first = 0, frameWords = 12) {
    const words = new Uint16Array(tags.length * frameWords);
    tags.forEach((v, i) => {
        const h = i * frameWords, prev = (i ? tags[i - 1] : 123n) & (period - 1n);
        words[h] = 0xfa05; words[h + 4] = (first + i) & 0xffff;
        words[h + 5] = Number(prev >> 32n); words[h + 6] = Number((prev >> 16n) & 65535n);
        words[h + 7] = Number(prev & 65535n);
    });
    return words;
}
function decode(tags, first = 0, words = headers(tags, first)) {
    return decoder.decodeTimestampChunk(words, 12, first, tags.length, tags[0].toString(), tags.at(-1).toString());
}

test('exact uint64 tags above 2^53, 48-bit epoch and frame-index wrap', () => {
    const base = (1n << 60n) + period - 100n;
    const tags = [base, base + 5n, base + 100n, base + 101n];
    assert.deepEqual(Array.from(decode(tags, 65534)), tags.map(String));
    assert.deepEqual(Array.from(decode([base])), [base.toString()]);
});
test('ambiguous epochs and stale associations request full-register fallback', () => {
    assert.equal(decode([5n, period + 5n]), null);
    const tags = [100n, 200n, 300n], words = headers(tags);
    words[12 + 7] = 200; // own-frame tag rather than predecessor
    assert.equal(decode(tags, 0, words), null);
    assert.equal(decode([100n, 100n]), null);
});
test('corrupt headers and short DMA are rejected, not silently repaired', () => {
    const tags = [100n, 200n], badMarker = headers(tags), badIndex = headers(tags);
    badMarker[0] = 0; badIndex[16] = 3;
    assert.throws(() => decode(tags, 0, badMarker), /identity mismatch/);
    assert.throws(() => decode(tags, 0, badIndex), /identity mismatch/);
    assert.throws(() => decode(tags, 0, headers(tags).slice(1)), /incomplete/);
    assert.throws(() => decode([200n, 100n]), /anchors/);
});

function fixture(tags, {badIndex = false, shortReads = 0} = {}) {
    let buffer, tagOut, pending, current, dmaCalls = 0, exportWave, exportTx;
    const args = {wave: [0,0,0,3048,7,0], tx: [0,1000], mask:7, recordLen:1000, interval:0, p9:0};
    const ptr = size => ({bytes: new ArrayBuffer(size), readByteArray(n) {return this.bytes.slice(0,n);},
        readU64() {return BigInt(this.tag);}});
    const r = {
        module: {getExportByName: () => 'getTag'}, profile: {symbols: {getRecordTag:'getTag'}},
        setPlyCurr() {}, setPlyBase() {}, setProcChEn() {}, setProcLaEn() {}, txFrmHead() {},
        stopScope() {}, setIntxOut() {},
        setWaveRange(...a) {exportWave = a;}, setTxInfo(...a) {exportTx = a;},
        setRun(mask,mode,len,n,base) {pending = {n,base};},
        traceReadEx(b, bytes) {
            dmaCalls++;
            if (shortReads-- > 0) return -3;
            current = pending;
            const h = headers(tags.slice(current.base, current.base + current.n), current.base, bytes/current.n/2);
            if (badIndex) h[4] = 999;
            new Uint16Array(b.bytes).set(h);
            return bytes;
        },
    };
    const api = module('timestamps', {
        require: () => decoder, uint64: x => BigInt(x), Thread: {sleep() {}},
        Memory: {alloc(n) {if (!buffer) return buffer=ptr(n); return tagOut=ptr(n);}},
        NativeFunction: function () {return () => {tagOut.tag = tags[current.base+current.n-1]; return 0;};},
    });
    return {run: (count=tags.length) => api.readFrameTimestamps(r, {}, args, 0, count, 4, 3),
            state: () => ({dmaCalls, exportWave, exportTx}), args};
}
test('prefix pass anchors each chunk and restores original geometry', () => {
    const f = fixture([100n,200n,300n,400n]);
    const result = f.run();
    assert.deepEqual(Array.from(result.frameTimestampTicks), ['100','200','300','400']);
    assert.equal(result.timestampFallbackFrames, 0);
    assert.equal(f.state().dmaCalls, 3); // two reads for chunk 3, one for chunk 1
    assert.deepEqual(f.state().exportWave, f.args.wave);
    assert.deepEqual(f.state().exportTx, f.args.tx);
});
test('long-span chunks fall back to full registers for intermediate frames', () => {
    const f = fixture([10n,period+20n,period*2n+30n]);
    const result = f.run();
    assert.equal(result.timestampFallbackFrames, 3);
    assert.deepEqual(Array.from(result.frameTimestampTicks), ['10',String(period+20n),String(period*2n+30n)]);
    assert.equal(f.state().dmaCalls, 3);
});
test('DMA retries are bounded; errors also restore original geometry', () => {
    const retry = fixture([10n,20n], {shortReads:1});
    assert.equal(retry.run().timestampRetries, 1);
    for (const options of [{shortReads:4}, {badIndex:true}]) {
        const f = fixture([10n,20n], options);
        assert.throws(() => f.run(), /timestamp (DMA failed|header identity mismatch)/);
        assert.deepEqual(f.state().exportWave, f.args.wave);
        assert.deepEqual(f.state().exportTx, f.args.tx);
    }
});
test('agent rejects averaging or ambiguous flags before resolving any native function', () => {
    let resolves = 0;
    const api = module('readback', {require: () => ({ensureResolved() {resolves++; throw Error('resolved');}})});
    for (const opts of [{timestamps:true,average:2},{timestampRegisters:true,average:2},
                        {timestamps:'yes',average:1},{timestamps:true,timestampRegisters:true,average:1}])
        assert.throws(() => api.readFrames(opts), /mutually exclusive|must be boolean|only one/);
    assert.equal(resolves, 0);
});
