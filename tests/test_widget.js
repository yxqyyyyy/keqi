// Offline Scriptable API double: no device, network, or personal calendar access.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const source = fs.readFileSync(process.argv[2] || path.join(__dirname, '../widgets/课期.js'), 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const DEFAULT_NOW = '2026-10-16T02:00:00Z';
let mockNow = DEFAULT_NOW;
class FixedDate extends Date {
  constructor(...values) { super(...(values.length ? values : [mockNow])); }
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
   (opts.kind === 'lecture' ? '上课时间：' : '截止时间：') + due.slice(0,16).replace('T',' ') + '（北京时间）\n' +
   (opts.start ? '考试开始：' + opts.start.slice(0,16).replace('T',' ') + '（北京时间）\n' : '') +
   '课程：' + course + '\n类型：' + (opts.kind || 'assignment') + '\n' + (opts.offline ? '线下提交\n' : '') +
   '本地记录更新时间：2026-10-16T02:00:00+00:00' +
   (Object.prototype.hasOwnProperty.call(opts, 'platformSync') ? '\n平台最近成功同步：' + opts.platformSync : '') +
   (Object.prototype.hasOwnProperty.call(opts, 'platformAttempt') ? '\n平台最近尝试同步：' + opts.platformAttempt : '') +
   (Object.prototype.hasOwnProperty.call(opts, 'platformResult') ? '\n平台同步结果：' + opts.platformResult : '')};
}
async function run({family='medium', parameter=null, calendars=[{title:'作业与考试',identifier:'target'}], events=[], readError=false, permissionError=false, inApp=false, query={}, dark=false, now=DEFAULT_NOW}={}) {
 mockNow = now;
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
 mockNow = DEFAULT_NOW;
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
 const broken=event('格式有错','2026-10-18T09:50:00+08:00');broken.startDate=new FixedDate('2026-10-19T09:50:00+08:00');r=await run({events:[broken]});assert(r.text.includes('时间待核对'));assert(r.text.includes('备注截止 10/18 09:50'));assert(!r.text.includes('需要检查日历'));checks++;
 r=await run({family:'small',events:[event('固定线下作业','2026-10-18T00:00:00+08:00',{course:'大学物理',offline:true})]});assert(r.text.includes('10/18 00:00'));assert(r.text.includes('周六 24:00'));checks++;
 r=await run({events:[event('磁学单元测验','2026-10-18T16:20:00+08:00',{course:'大学物理',kind:'exam',start:'2026-10-18T14:00:00+08:00'})]});assert(r.text.includes('14:00 开考'));assert(r.text.includes('16:20 结束'));checks++;
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
   assert(r.text.includes('平台同步时间未知'));assert(r.text.includes('日历读取 10:00'));
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
   assert(r.text.includes('平台同步时间未知'));assert(r.text.includes('日历读取 10:00'));
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
 r=await run({events:[event('快截止的考试','2026-10-16T12:00:00+08:00',{kind:'exam',course:'大学物理',start:'2026-10-16T12:00:00+08:00'}), pending24]});
 assert(r.text.includes('24h 待交 1 · 待考 1'));
 assert(r.text.includes('大学物理')===false); // existing short course display uses 大物
 assert(r.text.includes('大物 · 待考 · 快截止的考试'));
 assert(!r.text.includes('尽快提交')); // assignment is 24h away; red exam never says submit homework
 checks++;
 r=await run({family:'small',events:[event('纸质题单','2026-10-17T00:00:00+08:00',{course:'大学物理',offline:true})]});
 assert(r.text.includes('线下待交'));assert(!/未提交|实时/.test(r.text));
 checks++;
 // The two timestamps have separate sources in every supported size, including the small widget.
 for(const family of ['small','medium','large','extraLarge']) {
   r=await run({family,events:[event('已核对待办','2026-10-17T12:00:00+08:00',
     {platformSync:'2026-10-15T23:21:00.123456+00:00'})]});
   assert(r.text.includes('平台同步 10/16 07:21'));
   assert(r.text.includes('日历读取 10:00'));
   const footerTexts=strings(r.widget);
   assert(footerTexts.at(-1).includes('日历读取 10:00'));
   assert(footerTexts.at(family==='medium'?-1:-2).includes('平台同步 10/16 07:21'));
   assert(!r.text.includes('平台同步 10/16 10:00'));
   checks++;
   r=await run({family,events:[pending2]});
   assert(r.text.includes('平台同步时间未知'));
   assert(r.text.includes('日历读取 10:00'));
   checks++;
   r=await run({family,events:[]});
   assert(r.text.includes('近期没有'));
   assert(r.text.includes('平台同步时间未知'));
   assert(r.text.includes('日历读取 10:00'));
   checks++;
 }
 // A local task updated_at timestamp never proves the platform was checked. Reject malformed, future,
 // impossible and timezone-free input rather than allowing JavaScript's permissive Date normalization.
 const invalidSyncs=['', '2026-10-16T10:00:01+08:00', '2026-10-16T09:00:00',
   '2026-10-16 09:00:00+08:00', '2026-02-30T09:00:00+08:00', '2026-02-29T09:00:00+08:00',
   '2026-10-15T24:00:00+08:00', '2026-10-15T09:60:00+08:00', '2026-10-15T09:00:60+08:00',
   '2026-10-15T09:00:00+14:01', '2026-10-15T09:00:00+99:99', 'not a date'];
 for(const platformSync of invalidSyncs) {
   r=await run({events:[event('无效同步时间','2026-10-17T12:00:00+08:00',{platformSync})]});
   assert(r.text.includes('平台同步时间未知'),platformSync);
   assert(r.text.includes('日历读取 10:00'));
   assert(!r.text.includes('需要检查日历'));
   checks++;
 }
 // Mixed copies can be caused by iCloud propagation; even a non-visible task limits the freshness claim.
 r=await run({family:'small',events:[
   event('可见的新记录','2026-10-16T12:00:00+08:00',{platformSync:'2026-10-16T09:45:00+08:00'}),
   event('未显示的旧记录','2026-10-20T12:00:00+08:00',{platformSync:'2026-10-15T09:30:00+08:00'})]});
 assert(r.text.includes('平台同步 10/15 09:30'));
 assert(!r.text.includes('平台同步 10/16 09:45'));
 assert(!r.text.includes('未显示的旧记录'));
 checks++;
 for(const oldTask of [pending24, event('坏字段','2026-10-17T12:00:00+08:00',{platformSync:'bad'})]) {
   r=await run({events:[event('新记录','2026-10-16T12:00:00+08:00',
     {platformSync:'2026-10-16T09:45:00+08:00'}),oldTask]});
   assert(r.text.includes('平台同步时间未知'));
   assert(!r.text.includes('平台同步 10/16 09:45'));
   checks++;
 }
 const duplicateSync=event('重复字段','2026-10-17T12:00:00+08:00',{platformSync:'2026-10-16T09:00:00+08:00'});
 duplicateSync.notes+='\n平台最近成功同步：2026-10-16T09:45:00+08:00';
 r=await run({events:[duplicateSync]});assert(r.text.includes('平台同步时间未知'));checks++;
 r=await run({events:[event('已核对','2026-10-17T12:00:00+08:00',{platformSync:'2026-10-16T01:00:00Z'}),
   {identifier:'private',title:'私人日程',startDate:new FixedDate(),notes:'平台最近成功同步：2020-01-01T00:00:00Z'}]});
 assert(r.text.includes('平台同步 10/16 09:00'));assert(!r.text.includes('私人日程'));checks++;
 // Attempts are visibly distinct from successful platform sync, including partial and failed runs.
 for(const family of ['small','medium','large','extraLarge']) for(const [platformResult,label] of
   [['partial','部分'],['failed','未完成'],['running','读取中']]) {
   r=await run({family,events:[event('尝试读取的待办','2026-10-17T12:00:00+08:00',
     {platformAttempt:'2026-10-16T09:15:00+08:00',platformResult})]});
   assert(r.text.includes((family==='small'?'尝试 ':'平台尝试 ')+'10/16 09:15'));
   assert(r.text.includes(label));assert(r.text.includes('日历读取 10:00'));
   assert(!r.text.includes('平台同步 10/16 09:15'));
   checks++;
 }
 // Known full success stays labeled as success time, while a later partial attempt adds a qualification.
 r=await run({events:[event('旧完整成功、新部分尝试','2026-10-17T12:00:00+08:00',
   {platformSync:'2026-10-15T09:30:00+08:00',platformAttempt:'2026-10-16T09:15:00+08:00',platformResult:'partial'})]});
 assert(r.text.includes('平台同步 10/15 09:30 · 部分'));assert(!r.text.includes('平台同步 10/16'));
 checks++;
 r=await run({events:[event('新尝试','2026-10-16T12:00:00+08:00',
   {platformAttempt:'2026-10-16T09:45:00+08:00',platformResult:'partial'}),
   event('旧尝试','2026-10-17T12:00:00+08:00',
   {platformAttempt:'2026-10-15T09:30:00+08:00',platformResult:'partial'})]});
 assert(r.text.includes('平台尝试 10/15 09:30 · 部分'));checks++;
 for(const badOpts of [{platformResult:'partial'},
   {platformAttempt:'2026-10-16T09:15:00+08:00'},
   {platformAttempt:'2026-10-16T09:15:00',platformResult:'partial'},
   {platformAttempt:'2026-10-16T10:00:01+08:00',platformResult:'partial'},
   {platformAttempt:'2026-02-30T09:15:00+08:00',platformResult:'partial'},
   {platformAttempt:'2026-10-16T09:15:00+08:00',platformResult:'claimed-success'}]) {
   r=await run({events:[event('缺失或无效尝试','2026-10-17T12:00:00+08:00',badOpts)]});
   assert(r.text.includes('平台同步时间未知'));assert(!r.text.includes('平台尝试 '));checks++;
 }
 r=await run({events:[event('有尝试','2026-10-16T12:00:00+08:00',
   {platformAttempt:'2026-10-16T09:15:00+08:00',platformResult:'partial'}),pending24]});
 assert(r.text.includes('平台同步时间未知'));checks++;
 // Duplicate generated fields never use the first matching line, whether the other value is valid or malicious.
 for(const [name,value,opts] of [
   ['平台最近成功同步','malicious',{platformSync:'2026-10-16T09:00:00+08:00'}],
   ['平台最近尝试同步','2026-10-16T09:30:00+08:00',{platformAttempt:'2026-10-16T09:00:00+08:00',platformResult:'partial'}],
   ['平台最近尝试同步','malicious',{platformAttempt:'2026-10-16T09:00:00+08:00',platformResult:'partial'}],
   ['平台同步结果','success',{platformAttempt:'2026-10-16T09:00:00+08:00',platformResult:'partial'}]]) {
   const duplicate=event('伪字段','2026-10-17T12:00:00+08:00',opts);
   duplicate.notes+='\n'+name+'：'+value;
   r=await run({events:[duplicate]});assert(r.text.includes('平台同步时间未知'));checks++;
 }
 // A class is timed from its start, never described as an assignment submission or an exam.
 const lecture=event('Lecture 3 上课','2026-10-16T12:00:00+08:00',{kind:'lecture',course:'线上课程'});
 for(const family of ['small','medium','large','extraLarge']) {
   r=await run({family,events:[lecture]});
   assert(r.text.includes('Lecture 3 上课'));assert(r.text.includes('待上课'));
   assert(r.text.includes('24h 上课 1'));
   assert(!/待交|待考|尽快提交|截止|开考/.test(r.text));
   assert(r.text.includes(family==='small'?'10/16 12:00 上课':'上课 10/16 12:00'));
   checks++;
 }
 r=await run({events:[pending24,lecture,event('测验','2026-10-16T12:00:00+08:00',{kind:'exam',start:'2026-10-16T12:00:00+08:00'})]});
 assert(r.text.includes('24h 待交 1 · 待考 1 · 上课 1'));checks++;
 r=await run({family:'small',events:[event('刚开始的课','2026-10-16T10:00:00+08:00',{kind:'lecture'})]});
 assert(r.text.includes('现在上课'));assert(!r.text.includes('现在截止'));checks++;
 r=await run({family:'small',events:[event('已开始的课','2026-10-16T09:00:00+08:00',{kind:'lecture'})]});
 assert(r.text.includes('近期没有课期待办'));assert(!/已开始的课|逾期|剩 1 小时/.test(r.text));checks++;
 const badLecture=event('时间有错的课','2026-10-16T12:00:00+08:00',{kind:'lecture'});
 badLecture.startDate=new FixedDate('2026-10-16T13:00:00+08:00');
 r=await run({events:[badLecture]});assert(r.text.includes('备注上课 10/16 12:00'));assert(r.text.includes('时间待核对'));checks++;
 // Both old placeholders and real event intervals use the recorded exam start for reminders.
 const competition=event('大学生数学竞赛','2026-11-14T11:30:00+08:00',
   {kind:'exam',course:'高数',start:'2026-11-14T09:00:00+08:00'});
 competition.startDate=new FixedDate('2026-11-14T09:00:00+08:00');
 competition.endDate=new FixedDate('2026-11-14T11:30:00+08:00');
 for(const family of ['small','medium','large','extraLarge']) {
   r=await run({family,events:[competition]});
   assert(r.text.includes('大学生数学竞赛'));assert(r.text.includes('11:30 结束'));
   assert(r.text.includes('09:00 开考'));assert(!/需要检查日历|时间待核对|备注截止/.test(r.text));
   checks++;
 }
 // A range inconsistent with the notes remains visible with a conflict warning, never a guessed deadline.
 const changedCompetition={...competition,endDate:new FixedDate('2026-11-14T12:30:00+08:00')};
 r=await run({events:[changedCompetition,...future]});
 assert(r.text.includes('下一项'));assert(r.text.includes('1 项时间待核对'));assert(!r.text.includes('需要检查日历'));checks++;
 r=await run({family:'small',events:[changedCompetition]});
 assert(r.text.includes('备注 11/14 09:00 开考'));assert(!r.text.includes('12:30'));checks++;
 // The countdown, exact 24h/2h boundaries and card colours all use 09:00, independent of 11:30 end.
 for(const family of ['small','medium','large','extraLarge']) {
   for(const [now,label,background,urgent] of [
     ['2026-11-14T08:00:00+08:00','剩 1 小时','AB242C',true],
     ['2026-11-14T07:00:00+08:00','剩 2 小时','AB242C',true],
     ['2026-11-14T06:59:00+08:00','剩 3 小时','FFE2BA',true],
     ['2026-11-13T09:00:00+08:00','剩 24 小时','FFE2BA',true],
     ['2026-11-13T08:59:00+08:00','剩 25 小时','F7F7F0',false],
     ['2026-11-14T09:00:00+08:00','现在开考','AB242C',true]]) {
     r=await run({family,events:[competition],now});
     assert(r.text.includes(label),`${family} at ${now}: ${r.text}`);
     assert(r.text.includes('09:00 开考'));assert(r.text.includes('11:30 结束'));
     assert.equal(r.text.includes('24h 待考 1'),urgent);
     if(family==='small') assert.equal(r.widget.backgroundColor.value,background);
     else if(urgent) assert(nodes(r.widget).some(node=>node.backgroundColor?.value===background));
     assert(!/尽快提交|逾期|截止/.test(r.text));checks++;
   }
   r=await run({family,events:[competition],now:'2026-11-14T09:01:00+08:00'});
   assert(r.text.includes('考试中'));assert(r.text.includes('大学生数学竞赛'));
   assert(!/24h 待考|待考 ·|逾期|剩 3 小时/.test(r.text));
   assert.equal(r.widget.backgroundColor.value,'F7F7F0');checks++;
   for(const now of ['2026-11-14T11:30:00+08:00','2026-11-14T11:31:00+08:00']) {
     r=await run({family,events:[competition],now});
     assert(r.text.includes('近期没有课期待办'));assert(!r.text.includes('大学生数学竞赛'));
     assert(!/24h 待考|待考 ·|逾期|剩 3 小时/.test(r.text));
     assert.equal(r.widget.backgroundColor.value,'F7F7F0');checks++;
   }
 }
 // A missing start stays visible without using a near end time as an alarm anchor.
 const missingStart=event('缺少开考时间的考试','2026-11-14T11:30:00+08:00',{kind:'exam',course:'高数'});
 for(const family of ['small','medium','large','extraLarge']) {
   r=await run({family,events:[missingStart],now:'2026-11-14T10:00:00+08:00'});
   assert(r.text.includes('开考时间待补充'));assert(r.text.includes('11:30 结束'));
   assert(!/24h 待考|剩 |逾期|截止/.test(r.text));
   assert.equal(r.widget.backgroundColor.value,'F7F7F0');checks++;
 }
 // Past classes and ended exams never inflate overdue counts or the hidden-item count.
 const pastLecture=event('历史上课记录','2026-10-16T09:00:00+08:00',{kind:'lecture'});
 const endedExam=event('历史考试记录','2026-10-16T09:00:00+08:00',
   {kind:'exam',start:'2026-10-16T08:00:00+08:00'});
 const oldMissingExam=event('历史缺开考考试','2026-10-16T09:00:00+08:00',{kind:'exam'});
 const overdueAssignment=event('应提醒的逾期作业','2026-10-16T09:00:00+08:00');
 for(const family of ['small','medium','large','extraLarge']) {
   r=await run({family,events:[pastLecture,endedExam,oldMissingExam]});
   assert(r.text.includes('近期没有课期待办'));
   assert(!/历史上课记录|历史考试记录|历史缺开考考试|逾期|待补/.test(r.text));
   assert.equal(r.widget.backgroundColor.value,'F7F7F0');checks++;
   r=await run({family,events:[pastLecture,endedExam,oldMissingExam,overdueAssignment]});
   assert(r.text.includes('应提醒的逾期作业'));
   assert(r.text.includes('逾期 1 小时'));
   assert(!/历史上课记录|历史考试记录|历史缺开考考试|另 \d+ 项|待补/.test(r.text));
   if(family!=='small') assert(r.text.includes('未来暂无 · 逾期 1'));
   checks++;
   r=await run({family,events:[pastLecture,endedExam,oldMissingExam,overdueAssignment,lecture]});
   assert(r.text.includes('24h 上课 1'));
   assert(r.text.includes('Lecture 3 上课'));
   assert(!/历史上课记录|历史考试记录|历史缺开考考试|逾期 [23]|另 [23] 项/.test(r.text));
   if(family==='small') assert(r.text.includes('逾期 1'));
   checks++;
 }
 // Earlier start takes precedence over an assignment ending before this exam ends.
 const laterAssignment=event('十点作业','2026-11-14T10:00:00+08:00');
 for(const family of ['small','medium','large','extraLarge']) {
   r=await run({family,events:[laterAssignment,competition],now:'2026-11-14T08:00:00+08:00'});
   assert(r.text.includes('大学生数学竞赛'));
   if(family==='small') assert(!r.text.includes('十点作业'));
   else assert(r.text.indexOf('大学生数学竞赛')<r.text.indexOf('十点作业'));
   r=await run({family,events:[laterAssignment,competition],now:'2026-11-14T09:30:00+08:00'});
   assert(r.text.includes('考试中'));assert(!/24h 待考/.test(r.text));checks++;
 }
 // The previous deadline placeholder and the new instant-exam placeholder are both supported.
 const oldCompetition={...competition,startDate:new FixedDate('2026-11-14T11:30:00+08:00'),
   endDate:new FixedDate('2026-11-14T11:45:00+08:00')};
 const instantExam=event('同刻考试','2026-11-14T09:00:00+08:00',
   {kind:'exam',start:'2026-11-14T09:00:00+08:00'});
 instantExam.endDate=new FixedDate('2026-11-14T09:15:00+08:00');
 for(const value of [oldCompetition,instantExam]) {
   r=await run({family:'small',events:[value],now:'2026-11-14T08:00:00+08:00'});
   assert(r.text.includes('剩 1 小时'));assert(!r.text.includes('时间待核对'));checks++;
 }
 // Invalid order is isolated and never becomes a countdown to the end time.
 const reverseExam=event('开考晚于结束','2026-11-14T09:00:00+08:00',
   {kind:'exam',start:'2026-11-14T11:30:00+08:00'});
 r=await run({events:[reverseExam,laterAssignment],now:'2026-11-14T08:00:00+08:00'});
 assert(r.text.includes('十点作业'));assert(r.text.includes('1 项异常'));
 assert(!r.text.includes('开考晚于结束'));checks++;
 // Bad metadata must not blank other classes, nor report successful full coverage or an empty task list.
 const invalid=event('损坏的记录','2026-10-18T09:50:00+08:00',{platformSync:'2026-10-16T09:00:00+08:00'});
 invalid.notes=invalid.notes.replace('2026-10-18 09:50','2026-02-30 09:50');
 for(const family of ['small','medium','large','extraLarge']) {
   r=await run({family,events:[invalid,...future]});
   assert(r.text.includes('下一项'));assert(r.text.includes('1 项异常'));
   assert(r.text.includes('平台同步时间未知'));assert(r.text.includes('日历读取 10:00'));
   assert(!/需要检查日历|近期没有|损坏的记录/.test(r.text));checks++;
 }
 r=await run({events:[invalid]});assert(r.text.includes('有记录需要核对'));
 assert(r.text.includes('1 项课期记录格式异常'));assert(!r.text.includes('近期没有'));checks++;
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
   assert(r.text.includes('日历读取失败'));assert(!r.text.includes('日历读取 10:00'));
   assert(r.text.includes('平台同步时间未知'));
   assert(!r.text.includes('24h 待交'));assert(!r.text.includes('近期没有'));
   assert.equal(r.widget.backgroundColor.value,'F7F7F0');
   checks++;
 }
 assert(!/new\s+Request\b|new\s+Notification\b|CalendarEvent\.(?:save|remove)/.test(source));
 assert.equal(source,fs.readFileSync(path.join(__dirname,'../web/widgets/keqi-widget.js'),'utf8'));
 checks++;
 console.log(checks+' Scriptable mock scenarios passed; device TZ='+process.env.TZ);
})().catch(error=>{console.error(error);process.exitCode=1;});
