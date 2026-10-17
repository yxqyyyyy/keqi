// Offline Scriptable API double: no device, network, or personal calendar access.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const source = fs.readFileSync(process.argv[2] || path.join(__dirname, '../widgets/课期.js'), 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
class FixedDate extends Date {
  constructor(...values) { super(...(values.length ? values : ['2026-10-16T02:00:00Z'])); }
}
class Stack {
 constructor() { this.items = []; }
 addText(value) { const item = {text:value}; this.items.push(item); return item; }
 addStack() { const item = new Stack(); this.items.push(item); return item; }
 addSpacer(value) { this.items.push({spacer:value}); }
 layoutVertically() {}
 centerAlignContent() {}
 setPadding(...padding) { this.padding=padding; }
 async presentSmall() { this.presented='small'; }
 async presentMedium() { this.presented='medium'; }
 async presentLarge() { this.presented='large'; }
 async presentExtraLarge() { this.presented='extraLarge'; }
}
class Color { constructor(value) { this.value=value; } static dynamic(a,b) { return a; } }
function nodes(node) { return [node, ...node.items.flatMap(x => x instanceof Stack ? nodes(x) : [x])]; }
function strings(node) { return nodes(node).filter(x => x.text !== undefined).map(x => x.text); }
function event(title, due, opts={}) {
 const course = opts.course || '概率论与数理统计A';
 return {identifier:title, title:course + ' · ' + title, startDate:new FixedDate(due), notes:opts.notes ||
   '截止时间：' + due.slice(0,16).replace('T',' ') + '（北京时间）\n' +
   (opts.start ? '考试开始：' + opts.start.slice(0,16).replace('T',' ') + '（北京时间）\n' : '') +
   '课程：' + course + '\n类型：' + (opts.kind || 'assignment') + '\n' + (opts.offline ? '线下提交\n' : '') +
   '本地记录更新时间：2026-10-16T02:00:00+00:00'};
}
async function run({family='medium', parameter=null, calendars=[{title:'作业与考试',identifier:'target'}], events=[], readError=false, permissionError=false, inApp=false, query={}, dark=false}={}) {
 const state = {completed:false, alerts:0};
 class AppearanceColor extends Color { static dynamic(light,night){ return dark ? night : light; } }
 class Alert { addAction(){} async presentAlert(){state.alerts++;} }
 await new AsyncFunction('Calendar','CalendarEvent','Color','Font','ListWidget','URLScheme','args','config','Script','Alert','Date', source)(
  {forEvents:async()=>{if(permissionError)throw Error('denied');return calendars;}},
  {between:async(start,end,selected)=>{assert.deepEqual(selected.map(c=>c.identifier),[calendars.find(c=>c.identifier==='target')?.identifier || 'target']); if(readError)throw Error('read'); state.bounds=[start,end]; return events;}},
  AppearanceColor, {semiboldSystemFont:n=>n,systemFont:n=>n}, Stack, {forRunningScript:()=> 'scriptable:///run?scriptName=课期'},
  {widgetParameter:parameter,queryParameters:query}, {widgetFamily:inApp?null:family,runsInApp:inApp},
  {setWidget:w=>state.widget=w,complete:()=>state.completed=true}, Alert, FixedDate);
 assert(state.completed);
 return {...state,text:strings(state.widget).join('\n')};
}
(async()=>{
 let checks=0;
 const future=[event('较晚','2026-10-20T09:50:00+08:00'), event('下一项','2026-10-17T09:50:00+08:00'),event('再下一项','2026-10-18T09:50:00+08:00')];
 const past=[event('很旧','2026-10-01T09:50:00+08:00'),event('最近逾期','2026-10-15T09:50:00+08:00')];
 let r=await run({family:'small',events:[...past,...future]});assert(r.text.includes('下一项'));assert(!r.text.includes('很旧'));assert(r.text.includes('逾期 2'));assert(r.widget.url.includes('preview=large'));checks++;
 r=await run({events:[...past,...future]}); assert(r.text.indexOf('下一项')<r.text.indexOf('再下一项'));assert(!r.text.includes('最近逾期'));checks++;
 r=await run({family:'large',events:[...past,...future.slice(0,2)]});assert(r.text.indexOf('最近逾期')<r.text.indexOf('很旧'));assert(r.text.includes('逾期'));checks++;
 r=await run({family:'extraLarge',events:Array.from({length:12},(_,i)=>event('任务'+i,'2026-10-'+String(17+i).padStart(2,'0')+'T09:50:00+08:00'))});assert(r.text.includes('任务7'));assert(!r.text.includes('任务8'));checks++;
 r=await run({events:past});assert(r.text.includes('未来暂无 · 逾期'));assert(!r.text.includes('近期没有'));checks++;
 r=await run({events:[{identifier:'private',title:'私人安排',startDate:new FixedDate(),notes:'私人内容'}]});assert(r.text.includes('近期没有'));assert(!r.text.includes('私人安排'));checks++;
 r=await run({calendars:[]});assert(r.text.includes('找不到'));assert(!r.text.includes('近期没有'));checks++;
 r=await run({permissionError:true});assert(r.text.includes('完整访问'));assert(!r.text.includes('近期没有'));checks++;
 r=await run({readError:true});assert(r.text.includes('读取失败'));checks++;
 r=await run({calendars:[{title:'作业与考试',identifier:'target'},{title:'作业与考试',identifier:'duplicate'}]});assert(r.text.includes('多个'));checks++;
 r=await run({inApp:true,calendars:[{title:'作业与考试',identifier:'target'},{title:'作业与考试',identifier:'duplicate'}]});assert.equal(r.alerts,1);checks++;
 r=await run({parameter:'{"calendarId":"target"}',calendars:[{title:'作业与考试',identifier:'target'},{title:'作业与考试',identifier:'duplicate'}],events:future});assert(r.text.includes('下一项'));assert(r.widget.url.includes('calendarId=target'));checks++;
 r=await run({parameter:'{"bad'});assert(r.text.includes('不是有效 JSON'));checks++;
 const broken=event('格式有错','2026-10-18T09:50:00+08:00');broken.startDate=new FixedDate('2026-10-19T09:50:00+08:00');r=await run({events:[broken]});assert(r.text.includes('格式异常'));assert(!r.text.includes('近期没有'));checks++;
 r=await run({family:'small',events:[event('固定线下作业','2026-10-18T00:00:00+08:00',{course:'大学物理',offline:true})]});assert(r.text.includes('10/18 00:00'));assert(r.text.includes('周六 24:00'));checks++;
 r=await run({events:[event('磁学单元测验','2026-10-18T16:20:00+08:00',{course:'大学物理',kind:'exam',start:'2026-10-18T14:00:00+08:00'})]});assert(r.text.includes('14:00 开考'));assert(r.text.includes('10/18 16:20'));checks++;
 r=await run({events:[event('作业3-1','2026-10-21T09:50:00+08:00',{kind:'assignment',start:'2026-10-14T12:15:00+08:00'})]});assert(!r.text.includes('开考'));assert(!r.text.includes('12:15'));assert(r.text.includes('10/21 09:50'));checks++;
 r=await run({inApp:true,query:{preview:'large'},events:future});assert.equal(r.widget.presented,'large');checks++;
 r=await run({family:'small',parameter:'large',events:future});assert(r.text.includes('逾期')===false);assert(r.text.includes('课期'));assert(!r.text.includes('较晚'));checks++;
 // Exact threshold and next-minute checks: only future, <= 24h; red at <= 2h.
 const pending24=event('24小时边界','2026-10-17T10:00:00+08:00');
 const pending24Outside=event('24小时外','2026-10-17T10:01:00+08:00');
 const pending2=event('2小时边界','2026-10-16T12:00:00+08:00');
 const pending2Outside=event('2小时外','2026-10-16T12:01:00+08:00');
 const atDeadline=event('正好截止','2026-10-16T10:00:00+08:00');
 r=await run({family:'small',events:[atDeadline]});assert(r.text.includes('现在截止'));assert(!r.text.includes('剩 1 分钟'));
 const overdueMinute=event('已过一分钟','2026-10-16T09:59:00+08:00');
 for(const [task,bg,urgent,critical] of [[pending24,'FFE2BA',true,false],[pending24Outside,'F7F7F0',false,false],
    [pending2,'AB242C',true,true],[pending2Outside,'FFE2BA',true,false],[atDeadline,'AB242C',true,true],
    [overdueMinute,'F7F7F0',false,false]]) {
   r=await run({family:'small',events:[task]});
   assert.equal(r.widget.backgroundColor.value,bg);
   assert.equal(r.text.includes('24h 待交 1'),urgent);
   assert.equal(r.text.includes('尽快提交'),critical);
   assert(r.text.includes('以已同步日历为准'));
   assert(!/未提交|实时|刚刚同步/.test(r.text));
   checks++;
 }
 // All families and both appearances retain capacity, clear colour cues and a source qualifier.
 for(const dark of [false,true]) for(const family of ['small','medium','large','extraLarge']) {
   const count={small:1,medium:3,large:5,extraLarge:8}[family];
   const entries=Array.from({length:count+1},(_,i)=>event('紧急事项'+i,'2026-10-16T12:00:00+08:00',
     {course:'课程名称很长的概率论与数理统计课程'}));
   r=await run({family,dark,events:entries});
   assert(r.text.includes('紧急事项'+(count-1)));
   assert(!r.text.includes('紧急事项'+count));
   assert(r.text.includes('24h 待交 '+(count+1)));
   assert(r.text.includes('尽快提交'));
   assert(r.text.includes('以已同步日历为准'));
   const coloured=nodes(r.widget).filter(node=>node.backgroundColor?.value===(dark?'92252D':'AB242C'));
   assert(coloured.length>0,'critical state needs a visible red surface, not only a text colour');
   assert(nodes(r.widget).filter(node=>node.text?.startsWith('剩 ')).every(node=>node.font>=12));
   checks++;
 }
 // Warm orange rows are distinct from the unchanged green state; expired items do not inflate the 24h badge.
 r=await run({events:[pending24, pending24Outside, overdueMinute]});
 assert(r.text.includes('24h 待交 1'));
 assert(nodes(r.widget).some(node=>node.backgroundColor?.value==='FFE2BA'));
 assert(r.text.indexOf('24小时边界')<r.text.indexOf('24小时外'));
 assert(!r.text.includes('已过一分钟'));
 checks++;
 r=await run({dark:true,events:[pending24]});
 assert(nodes(r.widget).some(node=>node.backgroundColor?.value==='583218'));
 checks++;
 // Exams and offline work must not claim an online homework submission status.
 r=await run({events:[event('快截止的考试','2026-10-16T12:00:00+08:00',{kind:'exam',course:'大学物理'}), pending24]});
 assert(r.text.includes('24h 待交 1 · 待考 1'));
 assert(r.text.includes('大学物理')===false); // existing short course display uses 大物
 assert(r.text.includes('大物 · 待考 · 快截止的考试'));
 assert(!r.text.includes('尽快提交')); // assignment is 24h away; red exam never says submit homework
 checks++;
 r=await run({family:'small',events:[event('纸质题单','2026-10-17T00:00:00+08:00',{course:'大学物理',offline:true})]});
 assert(r.text.includes('线下待交'));assert(!/平台|未提交|实时/.test(r.text));
 checks++;
 // Once Mac removes a completed task from the synced calendar, the badge and red/orange surface disappear.
 const before=await run({events:[pending2]});
 const after=await run({events:[]});
 assert(before.text.includes('24h 待交 1'));
 assert(!after.text.includes('24h 待交'));assert(!after.text.includes('2小时边界'));
 assert.equal(after.widget.backgroundColor.value,'F7F7F0');
 checks++;
 // A read error must not render a reassuring empty state or an urgency status from partial data.
 for(const options of [{readError:true},{permissionError:true},{calendars:[]}]) {
   r=await run({...options,events:[pending2]});
   assert(r.text.includes('需要检查日历'));
   assert(!r.text.includes('24h 待交'));assert(!r.text.includes('近期没有'));
   assert.equal(r.widget.backgroundColor.value,'F7F7F0');
   checks++;
 }
 assert(!/new\s+Request\b|new\s+Notification\b|CalendarEvent\.(?:save|remove)/.test(source));
 assert.equal(source,fs.readFileSync(path.join(__dirname,'../web/widgets/keqi-widget.js'),'utf8'));
 checks++;
 console.log(checks+' Scriptable mock scenarios passed; device TZ='+process.env.TZ);
})().catch(error=>{console.error(error);process.exitCode=1;});
