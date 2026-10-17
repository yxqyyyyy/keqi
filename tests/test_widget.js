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
 setPadding() {}
 async presentSmall() { this.presented='small'; }
 async presentMedium() { this.presented='medium'; }
 async presentLarge() { this.presented='large'; }
 async presentExtraLarge() { this.presented='extraLarge'; }
}
class Color { constructor(value) { this.value=value; } static dynamic(a,b) { return a; } }
function strings(node) { return node.items.flatMap(x => x instanceof Stack ? strings(x) : x.text === undefined ? [] : [x.text]); }
function event(title, due, opts={}) {
 const course = opts.course || '概率论与数理统计A';
 return {identifier:title, title:course + ' · ' + title, startDate:new FixedDate(due), notes:opts.notes ||
   '截止时间：' + due.slice(0,16).replace('T',' ') + '（北京时间）\n' +
   (opts.start ? '考试开始：' + opts.start.slice(0,16).replace('T',' ') + '（北京时间）\n' : '') +
   '课程：' + course + '\n类型：' + (opts.kind || 'assignment') + '\n' + (opts.offline ? '线下提交\n' : '') +
   '本地记录更新时间：2026-10-16T02:00:00+00:00'};
}
async function run({family='medium', parameter=null, calendars=[{title:'作业与考试',identifier:'target'}], events=[], readError=false, permissionError=false, inApp=false, query={}}={}) {
 const state = {completed:false, alerts:0};
 class Alert { addAction(){} async presentAlert(){state.alerts++;} }
 await new AsyncFunction('Calendar','CalendarEvent','Color','Font','ListWidget','URLScheme','args','config','Script','Alert','Date', source)(
  {forEvents:async()=>{if(permissionError)throw Error('denied');return calendars;}},
  {between:async(start,end,selected)=>{assert.deepEqual(selected.map(c=>c.identifier),[calendars.find(c=>c.identifier==='target')?.identifier || 'target']); if(readError)throw Error('read'); state.bounds=[start,end]; return events;}},
  Color, {semiboldSystemFont:n=>n,systemFont:n=>n}, Stack, {forRunningScript:()=> 'scriptable:///run?scriptName=课期'},
  {widgetParameter:parameter,queryParameters:query}, {widgetFamily:inApp?null:family,runsInApp:inApp},
  {setWidget:w=>state.widget=w,complete:()=>state.completed=true}, Alert, FixedDate);
 assert(state.completed);
 return {...state,text:strings(state.widget).join('\n')};
}
(async()=>{
 let checks=0;
 const future=[event('较晚','2026-10-20T09:50:00+08:00'), event('下一项','2026-10-17T09:50:00+08:00'),event('再下一项','2026-10-18T09:50:00+08:00')];
 const past=[event('很旧','2026-10-01T09:50:00+08:00'),event('最近逾期','2026-10-15T09:50:00+08:00')];
 let r=await run({family:'small',events:[...past,...future]});assert(r.text.includes('下一项'));assert(!r.text.includes('很旧'));assert(r.text.includes('另有 2 项逾期'));assert(r.widget.url.includes('preview=large'));checks++;
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
 r=await run({family:'small',parameter:'large',events:future});assert(r.text.includes('另有')===false);assert(r.text.includes('课期'));assert(!r.text.includes('较晚'));checks++;
 console.log(checks+' Scriptable mock scenarios passed; device TZ='+process.env.TZ);
})().catch(error=>{console.error(error);process.exitCode=1;});
