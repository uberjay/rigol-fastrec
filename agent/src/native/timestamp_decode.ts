// Pure header decoder, shared with offline tests. Decimal strings preserve
// integer precision through Frida RPC; arithmetic stays in BigInt.
const PERIOD = 1n << 48n;
const MASK = PERIOD - 1n;

export function decodeTimestampChunk(words: Uint16Array, frameWords: number,
        first: number, count: number, firstTag: string, lastTag: string): string[] | null {
    if (count < 1 || frameWords < 8 || words.length !== count * frameWords)
        throw new Error("incomplete timestamp header DMA");
    for (let i = 0; i < count; i++) {
        const h = i * frameWords;
        if (words[h] !== 0xfa05 || words[h + 4] !== ((first + i) & 0xffff))
            throw new Error(`timestamp header identity mismatch at frame ${first + i}`);
    }
    const start = BigInt(firstTag), end = BigInt(lastTag);
    if (start < 0n || end < start || end >= (1n << 64n))
        throw new Error("invalid timestamp register anchors");
    // A long chunk may contain whole 48-bit counter periods invisible in the
    // headers. Read those frames' full registers instead of guessing an epoch.
    if (end - start >= PERIOD) return null;
    const tags: bigint[] = [];
    for (let i = 0; i < count - 1; i++) {
        const h = (i + 1) * frameWords; // header i+1 carries frame i's tag
        const low = (BigInt(words[h + 5]) << 32n) |
                    (BigInt(words[h + 6]) << 16n) | BigInt(words[h + 7]);
        tags.push(end - (((end & MASK) - low) & MASK));
    }
    tags.push(end);
    if (tags[0] !== start || tags.some((v, i) => i > 0 && v <= tags[i - 1]))
        return null; // inconsistent header association: use full registers
    return tags.map(v => v.toString());
}
