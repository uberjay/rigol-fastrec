// CApiRecord fields/symbols: MHO98 00.01.00 libscope-auklet.so.
const m = Process.getModuleByName('libscope-auklet.so');
let sequence = 0, update = null, getter = null;
function fields(p) {
    return {frame:p.add(0x90).readU32(), state:p.add(0xa8).readU32(),
            elapsed_fs:p.add(0xd8).readU64().toString(),
            first_tag:p.add(0xe0).readU64().toString()};
}
Interceptor.attach(m.getExportByName('_ZN10CApiRecord25ApiRecord_UpdateTimeStampEv'), {
    onEnter(args) { this.object = args[0]; },
    onLeave() { update = {sequence:++sequence, ...fields(this.object)}; }
});
Interceptor.attach(m.getExportByName('_ZN10CApiRecord22ApiRecord_GetTimeStampER7RString'), {
    onEnter(args) { getter = fields(args[0]); }
});
rpc.exports = { snapshot() { return {sequence, update, getter}; } };
