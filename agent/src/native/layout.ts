// Channel layout: which channels are enabled, the FPGA interleave stride, and
// each channel's offset within the interleave.

import { ensureResolved } from "./resolve.js";

export interface ChannelLayout {
    /** Total analog channels the scope reports. */
    apiChanCount: number;
    /** Bit i set ⇒ CHAN(i+1) enabled. */
    enabledMask: number;
    /** 1-indexed enabled channels, ascending. */
    enabledList: number[];
    /** FPGA interleave stride = CDrvParam::GetChanCount() (1/2/4). */
    stride: number;
}

/** Snapshot the live channel layout from the scope's CScopeChan list. */
export function channelLayout(): ChannelLayout {
    const r = ensureResolved();
    const off = r.profile.offsets;
    const total = (r.apiGetChanCount() as number) | 0;
    let mask = 0;
    const list: number[] = [];
    for (let i = 1; i <= total; i++) {
        const ch = r.scopeChanGetCH(i) as NativePointer;
        if (ch.isNull()) continue;
        // CScopeChan::getOnOff is (byte)*(this + chanOnOff) & 1.
        if ((ch.add(off.chanOnOff).readU8() & 1) !== 0) {
            mask |= 1 << (i - 1);
            list.push(i);
        }
    }
    // CDrvParam::GetChanCount() = *(uint32*)(Drv_GetScope() + drvParam + chanCount).
    const scope = r.drvGetScope() as NativePointer;
    const stride = scope.add(off.drvParam).add(off.chanCount).readU32();
    return { apiChanCount: total, enabledMask: mask, enabledList: list, stride };
}

/** Lane offset of `chanIdx` (1-indexed) within an interleaved engine frame, or
 *  -1 if that channel isn't enabled.
 *
 *  The FPGA runs in 1-, 2-, or 4-channel mode (= `stride`). In 4-channel mode
 *  (3 or 4 channels enabled) it streams ALL four physical channels, including
 *  the lanes of disabled ones — so a channel sits at its PHYSICAL lane
 *  `chanIdx-1` and gaps are kept (with {1,2,4} enabled, CH4 is at lane 3, not a
 *  dense lane 2). In 1-/2-channel mode the enabled channels are compacted into
 *  the available lanes, so the offset is the dense index (enabled channels
 *  below it). Verified on-scope via python -m tools.validate_scope. */
export function chanOffsetWithinEnabled(
        chanIdx: number, enabledMask: number, stride: number): number {
    if (chanIdx < 1) return -1;
    if ((enabledMask & (1 << (chanIdx - 1))) === 0) return -1;
    if (stride >= 4) return chanIdx - 1;     // 4-ch mode: physical lane, gaps kept
    let offset = 0;                          // 1-/2-ch mode: compacted (dense)
    for (let i = 0; i < chanIdx - 1; i++) {
        if (enabledMask & (1 << i)) offset++;
    }
    return offset;
}
