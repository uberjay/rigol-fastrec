// Bulk timestamp header investigation. Loaded by tools.diagnostics.replay_headers.
const m=Process.getModuleByName('libscope-auklet.so');
const ex={scheduling:'exclusive'};
function nf(n,r,a,opt){return new NativeFunction(m.getExportByName(n),r,a,opt);}
const f={
 getScope:nf('_Z12Drv_GetScopev','pointer',[]),
 setPlay:nf('_Z24DrvAcquire_SetPlayEnableb','void',['uint8']),
 init:nf('_Z22DrvWaveform_ExportInitj','int',['uint32']),
 back:nf('_Z22DrvWaveform_ExportBackv','int',[]),
 curr:nf('_Z20DrvRecord_SetPlyCurrj','void',['uint32']),
 base:nf('_Z20DrvRecord_SetPlyBasej','void',['uint32']),
 stop:nf('_ZN9CDrvScope9StopScopeEb','int',['pointer','uint8']),
 run:nf('DevSystemScu_SetRun','int',['uint32','int32','uint64','int32','uint32','int32','int32','uint64','uint64']),
 wave:nf('DevAcquireSPU_SetWaveRange','void',['uint32','uint32','uint32','uint32','uint32','uint32']),
 tx:nf('DevAcquireSpu_SetTxInfo','void',['uint32','uint32']),
 head:nf('DevAcquireSPU_TxFrmHead','void',['uint32']),
 ch:nf('DevSystemSCU_SetProcChEn','void',['uint32']),
 la:nf('DevSystemSCU_SetProcLaEn','void',['uint32']),
 intx:nf('DevLaDisplayWpu_SetIntxOut','void',['uint32','uint32','uint32']),
 read:nf('DevAnalyzeTrace_Read','int',['pointer','uint32'],ex),
 tag:nf('_Z16DrvRecord_GetTagRy','int',['pointer']),
};
// Native callbacks log without acquiring Frida's JavaScript lock during DMA.
// Instrumentation is opt-in; the untraced path has no diagnostic callbacks.
let tracer = null;
function makeTracer() {
 const libc=Process.getModuleByName('libc.so');
 const state=Memory.alloc(12), events=Memory.alloc(4096*112), lock=Memory.alloc(64);
 const cm=new CModule(`
#include <gum/guminterceptor.h>
#include <stdint.h>
typedef struct { long sec; long nsec; } TS;
typedef struct {
 uint64_t ns, caller, args[9], ret, out;
 uint32_t tid; uint16_t id, phase;
} Event;
extern uint32_t state[];
extern Event events[];
extern char trace_lock[];
extern int clock_gettime(int, TS *);
extern int gettid(void);
extern int pthread_mutex_lock(void *);
extern int pthread_mutex_unlock(void *);
typedef struct { void *out; int active; } Call;
void record(GumInvocationContext *ic, int phase) {
 unsigned int id=(unsigned int)(uintptr_t)gum_invocation_context_get_listener_function_data(ic);
 Call *call=gum_invocation_context_get_listener_invocation_data(ic,sizeof(Call));
 if (!phase) {
  call->active=state[0]!=0;
  if (!call->active) return;
  uintptr_t arg=(uintptr_t)gum_invocation_context_get_nth_argument(ic,0);
  if ((id==11 || id==12) && (arg<0x4000 || arg>0x40ff)) { call->active=0; return; }
  if (id==13 && (arg<0x1000 || arg>0x10ff)) { call->active=0; return; }
  call->out=id==9 ? (void *)arg : id==12 ? gum_invocation_context_get_nth_argument(ic,1) : 0;
 } else if (!call->active) return;
 TS ts; clock_gettime(1,&ts);
 pthread_mutex_lock(trace_lock);
 unsigned int n=state[1];
 if(n>=4096) {state[2]++;pthread_mutex_unlock(trace_lock);return;}
 Event *e=&events[n]; state[1]=n+1;
 e->ns=(uint64_t)ts.sec*1000000000+ts.nsec;
 e->caller=(uintptr_t)gum_invocation_context_get_return_address(ic);
 e->tid=gettid();e->id=id;e->phase=phase;e->out=0;e->ret=0;
 for(int i=0;i<9;i++)e->args[i]=phase?0:(uintptr_t)gum_invocation_context_get_nth_argument(ic,i);
 if(phase) {
  e->ret=(uintptr_t)gum_invocation_context_get_return_value(ic);
  if(call->out)e->out=id==9?*(uint64_t*)call->out:*(uint32_t*)call->out;
 }
 pthread_mutex_unlock(trace_lock);
}
void on_enter(GumInvocationContext *ic){record(ic,0);}
void on_leave(GumInvocationContext *ic){record(ic,1);}
`,{state,events,trace_lock:lock,
 clock_gettime:libc.getExportByName('clock_gettime'),gettid:libc.getExportByName('gettid'),
 pthread_mutex_lock:libc.getExportByName('pthread_mutex_lock'),pthread_mutex_unlock:libc.getExportByName('pthread_mutex_unlock')});
 const specs=[
 [1,'DevSystemScu_SetRun'],[2,'_ZN9CDrvScope9StopScopeEb'],
 [3,'DevAcquireSPU_TxFrmHead'],[4,'DevAcquireSpu_SetTxInfo'],[5,'DevAcquireSPU_SetWaveRange'],
 [6,'_Z24DrvAcquire_SetPlayEnableb'],[7,'_Z22DrvWaveform_ExportInitj'],[8,'_Z22DrvWaveform_ExportBackv'],
 [9,'DevSystemSCU_getTimeStamp'],[10,'DevAnalyzeTrace_Read'],
 [11,'DevSystemScu_WriteRegister'],[12,'DevSystemScu_ReadRegister'],[13,'DevAcquireSpu_WriteRegister'],
 [14,'_ZN9CDrvScope16RequestNormTraceEj']];
 const hooks=specs.map(([id,name])=>Interceptor.attach(m.getExportByName(name),{onEnter:cm.on_enter,onLeave:cm.on_leave},ptr(id)));
 Interceptor.flush();
 return {cm,hooks,state,events,specs,
 begin(){state.add(4).writeU32(0);state.add(8).writeU32(0);state.writeU32(1);},
 end(){state.writeU32(0);const n=state.add(4).readU32();const rows=[];
  for(let i=0;i<n;i++){const p=events.add(i*112);rows.push({ns:p.readU64().toString(),caller:p.add(8).readPointer().sub(m.base).toString(),
   args:Array.from({length:9},(_,j)=>p.add(16+j*8).readU64().toString()),ret:p.add(88).readU64().toString(),out:p.add(96).readU64().toString(),
   tid:p.add(104).readU32(),id:p.add(108).readU16(),phase:p.add(110).readU16()});}
  return {events:rows,dropped:state.add(8).readU32(),specs};},
 dispose(){state.writeU32(0);for(const h of hooks)h.detach();Interceptor.flush();cm.dispose();}
 };
}
let cap={};let watching=false;
for(const [name,sym,len] of [['wave','DevAcquireSPU_SetWaveRange',6],['tx','DevAcquireSpu_SetTxInfo',2]])
 Interceptor.attach(m.getExportByName(sym),{onEnter(a){if(watching)cap[name]=Array.from({length:len},(_,i)=>a[i].toUInt32());}});
Interceptor.attach(m.getExportByName('DevSystemScu_SetRun'),{onEnter(a){if(watching&&a[1].toInt32()===4)cap.run=Array.from({length:9},(_,i)=>a[i].toString());}});
const tagbuf=Memory.alloc(8);
function tag(){tagbuf.writeU64(0);const ret=f.tag(tagbuf);return {ret,ticks:tagbuf.readU64().toString()};}
const clockGetTime=new NativeFunction(Process.getModuleByName('libc.so').getExportByName('clock_gettime'),'int',['int','pointer']);
const clockBuf=Memory.alloc(16);
function timeNs(){clockGetTime(1,clockBuf);return uint64(clockBuf.readU64().toString()+'000000000').add(clockBuf.add(8).readU64()).toString();}
rpc.exports={
 watch(on){watching=on;return cap;},
 trace(on){if(on && tracer===null)tracer=makeTracer();if(!on && tracer!==null){tracer.dispose();tracer=null;}return {enabled:tracer!==null};},
 read(base,n,bytesPerFrame,head,extra,interval,options={}){
  if(!cap.run||!cap.wave||!cap.tx)throw Error('no captured replay args');
  const scope=f.getScope();const want=n*(bytesPerFrame+extra);
  const buf=Memory.alloc(want+64);const start=Date.now();
  if(tracer)tracer.begin();
  try{
   f.setPlay(0);f.init(0);
   if(options.settleMs)Thread.sleep(options.settleMs/1000);
   const wave=cap.wave.slice(), tx=cap.tx.slice();
   if(options.txSamples){wave[3]=cap.wave[3]-cap.tx[1]+options.txSamples;tx[1]=options.txSamples;}
   f.curr(base);f.base(base);f.ch(1);f.la(0);f.wave(...wave);f.tx(...tx);f.head(head);f.stop(scope,1);
   const before=options.pollStages===false?null:tag();
   const run=f.run(parseInt(cap.run[0]),4,uint64(cap.run[2]),n,base,base,base+n-1,uint64(interval===undefined ? cap.run[7] : interval),uint64(cap.run[8]));
   f.intx(0,0,0);
   const afterRun=options.pollStages===false?null:tag();
   if(options.armDelayMs)Thread.sleep(options.armDelayMs/1000);
   let ret=0;const reads=[];
   const chunk=options.chunkFrames||n;
   for(let off=0;off<n;off+=chunk){
    if(off && options.chunkDelayMs)Thread.sleep(options.chunkDelayMs/1000);
    const bytes=Math.min(chunk,n-off)*(bytesPerFrame+extra);
    // The exclusive NativeFunction call does not appear in this script's
    // interceptor log; bracket it explicitly using the same native clock.
    const beginNs=tracer?timeNs():null;
    const got=f.read(buf.add(ret),bytes);
    reads.push({want:bytes,got,beginNs,endNs:tracer?timeNs():null});
    if(got!==bytes){ret=got>0?-100:got;break;}ret+=got;
   }
   const afterDma=tag();
   const result={base,n,bytesPerFrame,head,extra,want,ret,run,before,afterRun,afterDma,elapsed_ms:Date.now()-start,cap,reads,options};
   if(ret>0){send({type:'raw',...result},buf.readByteArray(ret));}
   return result;
  }finally{
   // A shortened diagnostic export must not leave the live replay loop using
   // a short transfer length with its full-depth DMA buffer expectation.
   if(options.txSamples){f.wave(...cap.wave);f.tx(...cap.tx);}
   f.back();f.setPlay(1);if(tracer)send({type:'trace',...tracer.end()});
  }
 }
};
