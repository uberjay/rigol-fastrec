// Single mutable state container.
//
// ES-module imported bindings are read-only, so shared mutable state lives in
// one exported object per domain and is mutated via property writes
// (`native.module = …`), which keeps the shared surface explicit.

import type { FirmwareProfile } from "./firmware.js";

/** Resolved module + active firmware profile. Populated by resolve(); null
 *  until then. (The typed NativeFunction wrappers live in resolve.ts's
 *  ResolvedFns, not here.) */
export const native: {
    module: Module | null;
    profile: FirmwareProfile | null;
} = {
    module: null,
    profile: null,
};

/** Reset everything to the pre-init state (dispose()). */
export function resetState(): void {
    native.module = null;
    native.profile = null;
}
