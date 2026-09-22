#!/usr/bin/env node
/**
 * 番茄小说排行榜采集脚本
 *
 * 两条采集路径，输出同一份 scan-output-format.md 规范的 Markdown：
 *   fetch：纯 HTTPS，不需要 Chrome / agent-browser。
 *     1. 榜单页 SSR（/rank/{channel}_{type}_{catId}）的 __INITIAL_STATE__.rank 取 rankVersion、
 *        全部题材（rankCategoryTypeList）和前 10 本；
 *     2. 分页 API（/api/rank/category/list，每页 10 本）用开头取到的同一个 rankVersion 取全部排名，
 *        所以 --top 最多 100；
 *     3. 详情页 SSR（/page/{bookId}）的 state.page 取明文书名/作者/简介/题材，旧的多策略正则兜底。
 *   cdp：配合 browser-cdp skill，在 Chrome 里读榜单页 __INITIAL_STATE__，再用同步 XHR 逐本解码
 *     详情页。只拿得到页面已加载的列表（每题材约 20 本）。
 * 番茄列表级的书名/作者/简介是字体反爬的私有区字符，所以两条路径都要进详情页。
 * 输出 Markdown 格式匹配 scan-output-format.md 规范。
 *
 * 用法：
 *   node fanqie-rank-scraper.js --channel 1 --type 2              # 男频阅读榜
 *   node fanqie-rank-scraper.js --channel 0 --type 1              # 女频新书榜
 *   node fanqie-rank-scraper.js --channel 1 --type 2 --outdir ./  # 指定输出目录
 *   node fanqie-rank-scraper.js --channel all                     # 全部采集
 *   node fanqie-rank-scraper.js --channel 1 --top 50              # 每题材前 50 本（1-100）
 *   node fanqie-rank-scraper.js --channel 1 --mode fetch          # 只走纯 HTTPS
 *   node fanqie-rank-scraper.js --channel 1 --mode cdp            # 只走 Chrome CDP
 *
 * --mode auto（默认）先走 fetch；fetch 失败或标题解析率低于 50% 时回退 CDP，
 * 两份里取标题解析更多的一份写盘。
 *
 * 前置：
 *   fetch / auto 不需要 Chrome。
 *   cdp（以及 auto 的回退）需要：node {SKILL_DIR}/browser-cdp/scripts/setup-cdp-chrome.js 9222
 */

const fs = require("fs");
const path = require("path");
const { ab, sleep, evalJSONBase64, scrollLoad, getArg, captureRunClock, runCli } = require("./cdp-utils");
const RUN_CLOCK = captureRunClock();

const FANQIE_ORIGIN = "https://fanqienovel.com";
const MODES = ["auto", "fetch", "cdp"];
const DEFAULT_TOP = 20;
// 分页 API 的 total_num 固定为 100，再往后没有数据
const MAX_TOP = 100;
// 入口题材：男频西方奇幻 / 女频古风世情。用它们打开榜单页，拿题材表与 rankVersion
const ENTRY_CATEGORY = { 1: "1141", 0: "1139" };

// 一次详情请求的并发批大小。CDP 路径用同步 XHR 拉取，批太大会撞上 cdp-utils 里 ab() 的
// 20s 超时；fetch 路径同样按 5 本一批并发，批间停 300ms。
const DETAIL_CHUNK = 5;

// fetch 路径的节奏。2026-09 实测 5 题材 × 两榜 × 前 50（500 个详情页）零失败的配置。
const LIST_PAGE_SIZE = 10;
const LIST_PAGE_PAUSE_MS = 400;
const DETAIL_BATCH_PAUSE_MS = 300;
const HTTP_ATTEMPTS = 3;
const HTTP_BACKOFF_MS = 600;
const HTTP_TIMEOUT_MS = 15000;
// 连续这么多批详情页全部失败，就停止本次运行的详情请求。番茄对高频请求会整段返回
// 「HTTP 200 + 空 application/json」（同一 IP 连真浏览器也一样），继续请求只会加重拦截。
const DETAIL_FAILED_BATCHES_TO_STOP = 2;
// SSR 前 10 与同题材 API 首页相差几分钟：阅读榜实测同一集合但 #1/#2 互换，新书榜 8/10 重合。
// 所以 rankMold↔type 映射只按集合重合度校验，不比位置。
const MIN_MOLD_OVERLAP = 0.7;
const HTTP_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
  Accept: "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
  "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
};

// ---------------------------------------------------------------------------
// 解析：fetch 路径在 Node 里直接调用；CDP 路径由 buildDetailJS 把源码注入浏览器执行。
// 所以这三个函数必须自包含——只能引用彼此，不能引用模块里的其他变量。
// ---------------------------------------------------------------------------

/**
 * 从 HTML 里取出 window.__INITIAL_STATE__ 对象。
 * SSR 写的是 JS 对象字面量，不是纯 JSON：按花括号配对截出字面量（字符串里的括号不算），
 * 把字符串外的裸 undefined 换成 null，再 JSON.parse。取不到或解析失败返回 null。
 */
function extractInitialState(html) {
  var text = String(html || "");
  var hit = /__INITIAL_STATE__\s*=\s*\{/.exec(text);
  if (!hit) return null;
  var start = hit.index + hit[0].length - 1;
  var pieces = [];
  var from = start;
  var depth = 0;
  var quote = "";
  for (var i = start; i < text.length; i++) {
    var c = text.charAt(i);
    if (quote) {
      if (c === "\\") i++;
      else if (c === quote) quote = "";
      continue;
    }
    if (c === '"' || c === "'") {
      quote = c;
    } else if (c === "{") {
      depth++;
    } else if (c === "}") {
      depth--;
      if (depth === 0) {
        pieces.push(text.slice(from, i + 1));
        try {
          return JSON.parse(pieces.join(""));
        } catch (e) {
          return null;
        }
      }
    } else if (
      c === "u" &&
      text.slice(i, i + 9) === "undefined" &&
      !/[\w$]/.test(text.charAt(i - 1)) &&
      !/[\w$]/.test(text.charAt(i + 9))
    ) {
      pieces.push(text.slice(from, i), "null");
      from = i + 9;
      i += 8;
    }
  }
  return null;
}

/**
 * 从简介里挑题材标签，最多 6 个。简介常有好几组【…】/[…]，第一组往往是宣传或公告
 * （【新书评分刚出，后期会涨】【提示：…】），不能直接当标签。三条规则依次尝试：
 *   1. 第一个可用的括号组：按分隔符拆段、去掉含宣传词的段后仍有 ≥2 段，且每段 ≤10 字；
 *   2. 否则取第一串 ≥2 个紧邻（中间只有空白）的单词括号组，每组 ≤8 字且不含宣传词，
 *      如【日常】【修罗场】【多女】；
 *   3. 否则取起点在简介下标 ≤4、≤8 字且不含宣传词的单个括号，如【全民求生】。
 * 含句读或冒号（。！？!?“”"…：:）的括号组是句子或公告，三条规则都不采用。
 * @returns {string[]}
 */
function extractTags(text) {
  var GROUP_REJECT = /[。！？!?“”"…：:]/;
  var PART_PROMO = /新书|新作|评分|完本|完结|断更|出版|实体书|书友|群号|食用|捧场|放心|已更新|更新至|求月票|求收藏|打卡|签约|作品《|祝/;
  var SEPARATOR = /[+＋、,，\/｜|•·\s]+/;
  var src = String(text || "");
  var groupRe = /[【\[]([^【】\[\]]+)[】\]]/g;
  var groups = [];
  var m;
  while ((m = groupRe.exec(src))) {
    var body = m[1].trim();
    groups.push({
      start: m.index,
      end: m.index + m[0].length,
      body: body,
      parts: body.split(SEPARATOR).filter(Boolean),
      rejected: !body || GROUP_REJECT.test(body),
    });
  }
  function size(s) {
    return Array.from(s).length;
  }
  function finish(parts) {
    var out = [];
    for (var i = 0; i < parts.length && out.length < 6; i++) {
      if (out.indexOf(parts[i]) === -1) out.push(parts[i]);
    }
    return out;
  }
  function isSingleTag(g) {
    return !g.rejected && g.parts.length === 1 && size(g.parts[0]) <= 8 && !PART_PROMO.test(g.parts[0]);
  }

  for (var a = 0; a < groups.length; a++) {
    if (groups[a].rejected) continue;
    var kept = groups[a].parts.filter(function (p) {
      return !PART_PROMO.test(p);
    });
    if (kept.length >= 2 && kept.every(function (p) { return size(p) <= 10; })) {
      return finish(kept);
    }
  }

  var run = [];
  for (var b = 0; b < groups.length; b++) {
    var g = groups[b];
    if (!isSingleTag(g)) {
      if (run.length >= 2) break;
      run = [];
      continue;
    }
    if (run.length && /\S/.test(src.slice(run[run.length - 1].end, g.start))) {
      if (run.length >= 2) break;
      run = [];
    }
    run.push(g);
  }
  if (run.length >= 2) {
    return finish(run.map(function (item) { return item.parts[0]; }));
  }

  for (var c = 0; c < groups.length && groups[c].start <= 4; c++) {
    var h = groups[c];
    if (!h.rejected && size(h.body) <= 8 && !PART_PROMO.test(h.body)) return finish(h.parts);
  }
  return [];
}

/**
 * 解析详情页 HTML → {title, author, desc, category, tags, wordNumber, creationStatus,
 * lastChapterTitle, readCount, chapterTotal, source, err?}。
 * 优先读 SSR 的 state.page：bookName/author/abstract 是明文，categoryV2 是 JSON 字符串，
 * 首个 Name 就是题材；拿不到再回退旧的多策略正则（内嵌 JSON / <title> / og:meta）。
 * 带私有区字符（字体反爬）的字段按未解析处理。番茄 SSR 不含数字评分。
 */
function parseDetailHtml(html, id) {
  var OBFUSCATED = /[\uE000-\uF8FF]/;
  var text = String(html || "");
  var info = {
    title: "",
    author: "",
    desc: "",
    category: "",
    tags: "",
    wordNumber: "",
    creationStatus: "",
    lastChapterTitle: "",
    readCount: "",
    chapterTotal: "",
    source: "",
  };
  function plain(value) {
    var s = value === undefined || value === null ? "" : String(value).trim();
    return s && !OBFUSCATED.test(s) ? s : "";
  }
  function raw(value) {
    return value === undefined || value === null ? "" : String(value);
  }
  function firstName(value) {
    try {
      var list = typeof value === "string" ? JSON.parse(value) : value;
      if (Array.isArray(list)) {
        for (var i = 0; i < list.length; i++) {
          if (list[i] && list[i].Name) return String(list[i].Name);
        }
      }
    } catch (e) {}
    return "";
  }
  function unescapeJSON(value) {
    try {
      return JSON.parse('"' + value + '"');
    } catch (e) {
      return value;
    }
  }
  function pick(res) {
    for (var i = 0; i < res.length; i++) {
      var m = text.match(res[i]);
      if (m && m[1]) return m[1].trim();
    }
    return "";
  }

  if (!text.trim()) {
    info.err = "HTTP 200 但响应体为空（疑似被拦截）";
    return info;
  }
  var state = extractInitialState(text);
  var page = state && state.page && typeof state.page === "object" ? state.page : null;
  if (page) {
    // 有 state.page 就只信它：空壳页/验证页/别的书都按未解析处理，不再用正则在整页里乱抓书名
    if (page.bookId && id && String(page.bookId) !== String(id)) {
      info.err = "SSR state.page 属于另一本书（" + page.bookId + "）";
      return info;
    }
    info.title = plain(page.bookName);
    if (!info.title) {
      info.err = "SSR state.page 没有明文书名（验证页或页面结构变动）";
      return info;
    }
    info.author = plain(page.author);
    info.desc = plain(page.abstract) || plain(page.description);
    info.category = plain(firstName(page.categoryV2)) || plain(page.category);
    info.wordNumber = raw(page.wordNumber);
    info.creationStatus = raw(page.creationStatus);
    info.lastChapterTitle = plain(page.lastChapterTitle);
    info.readCount = raw(page.readCount);
    info.chapterTotal = raw(page.chapterTotal);
    info.source = "state";
  } else {
    info.title =
      plain(unescapeJSON(pick([/"bookName"\s*:\s*"((?:[^"\\]|\\.)+)"/]))) ||
      plain(pick([
        /<title>([^<]*?)(?:完整版|最新章节|在线阅读|_番茄小说|-番茄小说|_番茄|-番茄)/,
        /<meta[^>]+property="og:title"[^>]+content="([^"]+)"/,
        /<title>([^<|_]{1,40})/,
      ]));
    info.author =
      plain(unescapeJSON(pick([
        /"author"\s*:\s*"((?:[^"\\]|\\.)+)"/,
        /"authorName"\s*:\s*"((?:[^"\\]|\\.)+)"/,
      ]))) || plain(pick([/<meta[^>]+property="og:novel:author"[^>]+content="([^"]+)"/]));
    // abstract（真实简介）优先；meta description 是平台模板（「番茄小说提供...」），
    // 且常带 data-rh 属性，故用宽松属性匹配兜底，模板文本交给 cleanDesc 清掉。
    info.desc =
      plain(unescapeJSON(pick([/"abstract"\s*:\s*"((?:[^"\\]|\\.){6,})"/]))) ||
      plain(pick([
        /<meta[^>]+name="description"[^>]+content="([^"]+)"/,
        /<meta[^>]+property="og:description"[^>]+content="([^"]+)"/,
      ]));
    // 题材：category 常为空字符串，真实题材在 categoryV2（转义 JSON）首个 Name。
    info.category = plain(pick([
      /"categoryV2":"\[\{[\s\S]*?\\"Name\\":\\"([^"\\]+)/,
      /"category"\s*:\s*"([^"]{1,20})"/,
      /<meta[^>]+property="og:novel:category"[^>]+content="([^"]+)"/,
    ]));
    info.source = info.title ? "regex" : "";
  }
  info.tags = extractTags(info.desc).join("、");
  if (!info.title) info.err = "未找到 __INITIAL_STATE__.page 与书名（验证页或页面结构变动）";
  return info;
}

// ---------------------------------------------------------------------------
// CDP 路径：页面提取
// ---------------------------------------------------------------------------

/** 连通性 + 页面就绪自检 */
function probePage(port) {
  return evalJSONBase64(
    port,
    "JSON.stringify({host:location.host,hasState:!!window.__INITIAL_STATE__})"
  );
}

/** 构建：提取侧边菜单品类链接的浏览器 JS */
function buildCategoriesJS(prefix) {
  return `JSON.stringify((function(){
    var prefix=${JSON.stringify(prefix)};
    var out=[];var seen={};
    Array.from(document.querySelectorAll('a')).forEach(function(a){
      var href=a.getAttribute('href')||'';
      if(href.indexOf(prefix)===-1)return;
      var name=(a.innerText||a.textContent||'').trim();
      if(!name)return;
      if(seen[href])return;seen[href]=1;
      out.push({name:name,href:href});
    });
    return out;
  })())`;
}

/** 提取侧边菜单品类链接 */
function extractCategories(port, channel, type) {
  const prefix = `/rank/${channel}_${type}_`;
  return evalJSONBase64(port, buildCategoriesJS(prefix)) || [];
}

/**
 * 从 __INITIAL_STATE__ 提取当前品类页的作品列表。
 * 多路径尝试 + 深度兜底扫描，并把字段名归一，避免站点改 state 结构就全盘失败。
 */
function buildBookListJS() {
  return `JSON.stringify((function(){
    var s=window.__INITIAL_STATE__||{};
    var cands=[
      s.rank&&s.rank.book_list, s.rank&&s.rank.bookList, s.rank&&s.rank.rankList,
      s.rankData&&s.rankData.book_list, s.page&&s.page.book_list
    ];
    var list=null;
    for(var i=0;i<cands.length;i++){ if(Array.isArray(cands[i])&&cands[i].length){list=cands[i];break;} }
    if(!list){
      var found=null;
      (function walk(o,d){
        if(found||!o||d>6)return;
        if(Array.isArray(o)){
          if(o.length&&o[0]&&typeof o[0]==='object'&&(o[0].bookId||o[0].book_id)){found=o;return;}
          for(var j=0;j<o.length&&!found;j++)walk(o[j],d+1);return;
        }
        if(typeof o==='object'){ for(var k in o){ if(found)break; try{walk(o[k],d+1)}catch(e){} } }
      })(s,0);
      list=found||[];
    }
    return list.map(function(b){return {
      bookId:String(b.bookId||b.book_id||''),
      read_count:b.read_count||b.readCount||b.read||'',
      wordNumber:b.wordNumber||b.word_number||b.wordCount||'',
      creationStatus:(b.creationStatus!=null?b.creationStatus:(b.creation_status!=null?b.creation_status:b.status)),
      lastChapterTitle:b.lastChapterTitle||b.last_chapter_title||b.lastChapter||'',
      category:b.category||b.categoryName||b.category_name||''
    };}).filter(function(b){return b.bookId;});
  })())`;
}

function extractBookList(port) {
  const list = evalJSONBase64(port, buildBookListJS());
  return Array.isArray(list) ? list : [];
}

/**
 * 批量解码详情：逐本同步 XHR 请求 /page/{id}，交给与 fetch 路径同一份的 parseDetailHtml。
 * 返回 { id: {title, author, desc, category, tags, ...} }，单本失败带 err。
 */
function buildDetailJS(ids) {
  return `JSON.stringify((function(){
    ${extractInitialState.toString()}
    ${extractTags.toString()}
    ${parseDetailHtml.toString()}
    var ids=${JSON.stringify(ids)};
    var map={};
    for(var k=0;k<ids.length;k++){
      var id=ids[k];
      try{
        var x=new XMLHttpRequest();
        x.open('GET','/page/'+id,false);
        x.send();
        var info=parseDetailHtml(x.status===200?(x.responseText||''):'',id);
        if(x.status!==200)info.err='HTTP '+x.status;
        map[id]=info;
      }catch(e){
        map[id]={title:'',author:'',desc:'',category:'',tags:'',err:String(e&&e.message||e)};
      }
    }
    return map;
  })())`;
}

function fetchDetailsChunk(port, ids) {
  return evalJSONBase64(port, buildDetailJS(ids)) || {};
}

/** 分批解码，避免单次 eval 超时；返回合并后的 map */
function fetchDetails(port, bookIds) {
  const map = {};
  for (let i = 0; i < bookIds.length; i += DETAIL_CHUNK) {
    const chunk = bookIds.slice(i, i + DETAIL_CHUNK);
    const part = fetchDetailsChunk(port, chunk);
    Object.assign(map, part);
    sleep(300);
  }
  return map;
}

// ---------------------------------------------------------------------------
// fetch 路径：榜单页 SSR + 分页 API + 详情页 SSR
// ---------------------------------------------------------------------------

function defaultDelay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * GET 客户端：每个请求最多尝试 HTTP_ATTEMPTS 次，失败后指数退避。
 * parse(body) 抛错也算本次失败（空响应体、非 JSON、code≠0、页面无 state 等），会重试。
 */
function createHttpClient({ fetchImpl = globalThis.fetch, delay = defaultDelay } = {}) {
  async function get(url, { referer, parse = (body) => body } = {}) {
    if (typeof fetchImpl !== "function") {
      throw new Error("当前 Node 没有全局 fetch（需要 Node 18+），请改用 --mode cdp");
    }
    let lastError = null;
    for (let attempt = 1; attempt <= HTTP_ATTEMPTS; attempt++) {
      try {
        const headers = { ...HTTP_HEADERS };
        if (referer) headers.Referer = referer;
        const res = await fetchImpl(url, { headers, signal: AbortSignal.timeout(HTTP_TIMEOUT_MS) });
        const body = await res.text();
        if (res.status !== 200) throw new Error(`HTTP ${res.status}`);
        if (!body.trim()) throw new Error("HTTP 200 但响应体为空（疑似被拦截）");
        return parse(body);
      } catch (error) {
        lastError = error;
        if (attempt < HTTP_ATTEMPTS) await delay(HTTP_BACKOFF_MS * 2 ** (attempt - 1));
      }
    }
    throw lastError;
  }

  return { get };
}

function rankPageUrl(ch, type, catId) {
  return `${FANQIE_ORIGIN}/rank/${ch}_${type}_${catId}`;
}

/** 分页 API：gender=channel（1 男频 / 0 女频），rankMold 对应 type（2 阅读榜 / 1 新书榜） */
function rankApiUrl({ ch, rankMold, catId, offset, rankVersion }) {
  const query = new URLSearchParams({
    app_id: "2503",
    rank_list_type: "3",
    offset: String(offset),
    limit: String(LIST_PAGE_SIZE),
    category_id: String(catId),
    rank_version: rankVersion ? String(rankVersion) : "",
    gender: String(ch),
    rankMold: String(rankMold),
  });
  return `${FANQIE_ORIGIN}/api/rank/category/list?${query}`;
}

function present(value) {
  return value !== undefined && value !== null && value !== "";
}

/** 列表级文本若带私有区字符（字体反爬）就当作没有，避免乱码写进报告 */
function plainText(value) {
  const s = present(value) ? String(value).trim() : "";
  return s && !/[\uE000-\uF8FF]/.test(s) ? s : "";
}

function errorMessage(error) {
  return error && error.message ? error.message : String(error);
}

async function fetchRankState(http, ch, type, catId) {
  return http.get(rankPageUrl(ch, type, catId), {
    parse(html) {
      const state = extractInitialState(html);
      const rank = state && state.rank;
      if (!rank || !Array.isArray(rank.book_list)) {
        throw new Error("榜单页没有可解析的 __INITIAL_STATE__.rank（页面结构变动或被拦截）");
      }
      return rank;
    },
  });
}

async function fetchRankApiPage(http, params) {
  return http.get(rankApiUrl(params), {
    referer: rankPageUrl(params.ch, params.type, params.catId),
    parse(body) {
      const json = JSON.parse(body);
      const data = json && json.data;
      if (!json || Number(json.code) !== 0 || !data || !Array.isArray(data.book_list)) {
        throw new Error(`榜单 API 返回异常（code=${json && json.code}）`);
      }
      return data;
    },
  });
}

/** 取一页 API；同一 (题材, rankMold, offset) 只请求一次。记录 rankVersion 漂移。 */
async function apiPage(run, ctx, cat, offset, rankMold = ctx.rankMold) {
  const key = `${cat.id}|${rankMold}|${offset}`;
  if (ctx.pageCache.has(key)) return ctx.pageCache.get(key);
  await run.pause(LIST_PAGE_PAUSE_MS);
  const data = await fetchRankApiPage(run.http, {
    ch: ctx.ch,
    type: ctx.type,
    rankMold,
    catId: cat.id,
    offset,
    rankVersion: ctx.rankVersion,
  });
  const got = present(data.rankVersion) ? String(data.rankVersion) : "";
  if (!ctx.rankVersion && got) {
    ctx.rankVersion = got;
    ctx.versionSource = "API 首页";
  } else if (got && got !== ctx.rankVersion) {
    ctx.drift.push({ cat: cat.name, offset, got });
  }
  ctx.pageCache.set(key, data);
  return data;
}

function bookIdOf(item) {
  return String((item && (item.bookId || item.book_id)) || "");
}

/**
 * 校验 rankMold↔type：入口题材的 SSR 前 10 与 API 首页按集合重合度比对（≥70%）。
 * 不够就试另一个 rankMold（站点若对调了映射，照样取对榜单）；两个都不够就放弃 fetch。
 */
async function resolveRankMold(run, ctx, entryCat, ssrBooks) {
  const ssrIds = ssrBooks.map(bookIdOf).filter(Boolean);
  if (!ssrIds.length) throw new Error("榜单页 SSR 没有书目，无法校验 rankMold 映射");
  const tried = [];
  for (const mold of [ctx.type, ctx.type === "2" ? "1" : "2"]) {
    const page = await apiPage(run, ctx, entryCat, 0, mold);
    const apiIds = page.book_list.map(bookIdOf).filter(Boolean);
    const ssrSet = new Set(ssrIds);
    const overlap = apiIds.filter((id) => ssrSet.has(id)).length;
    const needed = Math.ceil(MIN_MOLD_OVERLAP * Math.min(ssrIds.length, apiIds.length));
    tried.push(`rankMold=${mold} 重合 ${overlap}/${ssrIds.length}`);
    if (apiIds.length && overlap >= needed) {
      return { mold, overlap: `${overlap}/${ssrIds.length}`, swapped: mold !== ctx.type };
    }
  }
  throw new Error(`rankMold↔type 映射校验失败：SSR 前 ${ssrIds.length} 本与 API 首页${tried.join("，")}`);
}

function normalizeApiBook(item, fallbackRank) {
  const pos = Number(item.currentPos);
  return {
    rank: Number.isInteger(pos) && pos > 0 ? pos : fallbackRank,
    bookId: bookIdOf(item),
    read_count: item.read_count || item.readCount || "",
    wordNumber: item.wordNumber || item.word_number || "",
    creationStatus: present(item.creationStatus) ? item.creationStatus : item.creation_status,
    // 列表级书名/作者/简介是私有区乱码，最新章节标题是明文
    lastChapterTitle: plainText(item.lastChapterTitle || item.last_chapter_title),
    category: "",
  };
}

async function collectCategoryFetch(run, ctx, cat) {
  const books = [];
  const seen = new Set();
  let total = MAX_TOP;
  let pages = 0;
  for (let offset = 0; offset < Math.min(run.top, total); offset += LIST_PAGE_SIZE) {
    const data = await apiPage(run, ctx, cat, offset);
    pages++;
    const reported = Number(data.total_num);
    if (Number.isFinite(reported) && reported >= 0) total = reported;
    data.book_list.forEach((item, index) => {
      const book = normalizeApiBook(item, offset + index + 1);
      if (!book.bookId || seen.has(book.bookId)) return;
      seen.add(book.bookId);
      books.push(book);
    });
    if (data.book_list.length < LIST_PAGE_SIZE) break;
  }
  return { books: books.slice(0, run.top), pages };
}

function emptyDetail(err) {
  return { title: "", author: "", desc: "", category: "", tags: "", err };
}

async function fetchDetailFetch(run, id, referer) {
  try {
    return await run.http.get(`${FANQIE_ORIGIN}/page/${id}`, {
      referer,
      parse(html) {
        const info = parseDetailHtml(html, id);
        if (!info.title) throw new Error(info.err || "详情页未解析到书名");
        return info;
      },
    });
  } catch (error) {
    return emptyDetail(errorMessage(error));
  }
}

/**
 * 详情页 5 本一批并发、批间停 300ms；解析成功的按 bookId 缓存（跨题材/跨榜单复用）。
 * 连续 DETAIL_FAILED_BATCHES_TO_STOP 批全部失败就熔断：本次运行不再请求详情页。
 */
async function fetchDetailsFetch(run, bookIds, referer) {
  const map = {};
  const pending = [];
  for (const id of bookIds) {
    if (run.detailCache.has(id)) map[id] = run.detailCache.get(id);
    else if (!pending.includes(id)) pending.push(id);
  }
  const gate = run.detailGate;
  for (let i = 0; i < pending.length; i += DETAIL_CHUNK) {
    if (gate.stopped) {
      for (const id of pending.slice(i)) map[id] = emptyDetail(`未请求：${gate.reason}`);
      break;
    }
    const chunk = pending.slice(i, i + DETAIL_CHUNK);
    const results = await Promise.all(chunk.map((id) => fetchDetailFetch(run, id, referer)));
    let failed = 0;
    chunk.forEach((id, k) => {
      map[id] = results[k];
      if (results[k].title) run.detailCache.set(id, results[k]);
      else failed++;
    });
    if (failed === chunk.length) {
      gate.failedBatches++;
      gate.failedBooks += chunk.length;
      if (gate.failedBatches >= DETAIL_FAILED_BATCHES_TO_STOP) {
        gate.stopped = true;
        gate.reason = `详情页连续 ${gate.failedBooks} 本请求失败（${results[0].err}），已停止请求详情页`;
        console.error(`  ✗ ${gate.reason}。疑似被番茄拦截，稍后重试或减小 --top / 采集范围。`);
      }
    } else {
      gate.failedBatches = 0;
      gate.failedBooks = 0;
    }
    await run.pause(DETAIL_BATCH_PAUSE_MS);
  }
  return map;
}

function rankCategories(rank, ch) {
  const lists = rank.rankCategoryTypeList || {};
  const list = lists[ch === "1" ? "male" : "female"];
  if (!Array.isArray(list)) return [];
  return list
    .filter((c) => c && present(c.id) && present(c.name))
    .map((c) => ({ id: String(c.id), name: String(c.name) }));
}

async function collectTargetFetch(ch, type, run) {
  const label = `${channelLabel(ch)}${typeLabel(type)}`;
  console.log(`\n→ 采集 ${label}（fetch：榜单 SSR + 分页 API + 详情页 SSR）...`);

  const entryCatId = ENTRY_CATEGORY[ch];
  const rank = await fetchRankState(run.http, ch, type, entryCatId);
  // 一次运行里每个榜单只在开头取一次 rankVersion，全部分页都钉住它，排名才来自同一份快照
  const ctx = {
    ch,
    type,
    rankVersion: present(rank.rankVersion) ? String(rank.rankVersion) : "",
    versionSource: "榜单页 SSR",
    rankMold: type,
    drift: [],
    pageCache: new Map(),
  };
  const notes = [];
  let categories = rankCategories(rank, ch);
  if (!categories.length) {
    console.log("  ⚠ 榜单页 SSR 缺少题材表，降级为单题材采集（入口页）");
    categories = [{ id: entryCatId, name: "全部（入口页）" }];
  }
  const entryCat = categories.find((c) => c.id === entryCatId) || { id: entryCatId, name: "入口题材" };
  const mapping = await resolveRankMold(run, ctx, entryCat, rank.book_list);
  ctx.rankMold = mapping.mold;
  if (mapping.swapped) {
    notes.push(`rankMold 与 type 对不上，已按集合重合度改用 rankMold=${mapping.mold}`);
    console.error(`  ⚠ rankMold=${type} 与 SSR 榜单不重合，已改用 rankMold=${mapping.mold}`);
  }
  console.log(
    `  发现 ${categories.length} 个题材 · rankVersion=${ctx.rankVersion || "（空）"} · ` +
      `rankMold=${mapping.mold}（入口题材 SSR 与 API 首页重合 ${mapping.overlap}）`
  );

  const out = [];
  for (let ci = 0; ci < categories.length; ci++) {
    const cat = categories[ci];
    try {
      const { books, pages } = await collectCategoryFetch(run, ctx, cat);
      const details = await fetchDetailsFetch(
        run,
        books.map((b) => b.bookId),
        rankPageUrl(ch, type, cat.id)
      );
      const resolved = books.filter((b) => details[b.bookId] && details[b.bookId].title).length;
      console.log(
        `  [${ci + 1}/${categories.length}] ${cat.name}：${books.length} 本（API ${pages} 页），详情 ${resolved}/${books.length}`
      );
      out.push({ name: cat.name, books: books.map((b) => ({ ...b, info: details[b.bookId] || {} })) });
    } catch (error) {
      const message = errorMessage(error);
      console.error(`  [fanqie] 题材 ${cat.name} 处理出错，跳过: ${message}`);
      out.push({ name: cat.name, books: [], error: message });
      if (out.length >= 3 && out.every((c) => c.error)) {
        throw new Error(`前 ${out.length} 个题材的榜单 API 全部失败（${message}）`);
      }
    }
  }

  if (run.detailGate.stopped) notes.push(`未解析的书显示为（标题待解析）：${run.detailGate.reason}`);
  if (ctx.drift.length) {
    console.error(`  ⚠ API 返回的 rankVersion 与固定版本不一致 ${ctx.drift.length} 次，已记入文件头`);
  }
  const drift = ctx.drift.length
    ? `${ctx.drift.length} 页（${ctx.drift
        .slice(0, 3)
        .map((d) => `${d.cat} offset=${d.offset} 返回 ${d.got}`)
        .join("，")}${ctx.drift.length > 3 ? "…" : ""}）`
    : "无";
  return {
    ch,
    type,
    top: run.top,
    mode: "fetch",
    method: "fetch（榜单 SSR + 分页 API + 详情页 SSR，无需 Chrome）",
    versionLine:
      `rankVersion=${ctx.rankVersion || "（空）"}（取自${ctx.versionSource}，全部分页固定此版本），` +
      `rankMold=${mapping.mold}（入口题材 SSR 与 API 首页重合 ${mapping.overlap}）；API 版本漂移：${drift}`,
    notes,
    categories: out,
  };
}

// ---------------------------------------------------------------------------
// CDP 路径：逐题材打开榜单页
// ---------------------------------------------------------------------------

function collectTargetCdp(ch, type, run) {
  const label = `${channelLabel(ch)}${typeLabel(type)}`;
  console.log(`\n→ 采集 ${label}（CDP）...`);

  // 用已知品类 ID 作为入口，确保菜单只显示当前频道/类型的品类
  const initCatId = ENTRY_CATEGORY[ch];
  ab(run.port, "open", rankPageUrl(ch, type, initCatId));
  sleep(3000);

  // 连通性自检：把"静默写出一堆 bookId"变成可操作的报错
  const probe = probePage(run.port);
  if (!probe) {
    console.error(
      `  ✗ CDP 无响应。请确认已用 browser-cdp 启动 Chrome（端口 ${run.port}），且 agent-browser 可用。`
    );
    return null;
  }
  if (probe.host && probe.host.indexOf("fanqie") === -1) {
    console.error(
      `  ✗ 当前页面非番茄（host=${probe.host}），可能被重定向到登录/验证页，已跳过。`
    );
    return null;
  }
  if (!probe.hasState) {
    console.error(`  ⚠ 页面未挂载 __INITIAL_STATE__，将尝试兜底扫描，结果可能不完整。`);
  }
  if (run.top > DEFAULT_TOP) {
    console.log(`  ⚠ CDP 路径只拿得到页面已加载的列表（每题材约 20 本），--top ${run.top} 可能取不满`);
  }

  let categories = extractCategories(run.port, ch, type);
  if (!categories.length) {
    // 菜单可能懒加载，滚动后重试一次
    scrollLoad(run.port, 2);
    sleep(1000);
    categories = extractCategories(run.port, ch, type);
  }
  if (!categories.length) {
    // 仍失败：降级为只采当前入口页，至少产出数据而不是空跑
    console.log(`  ⚠ 未提取到品类菜单，降级为单题材采集（入口页）`);
    categories = [{ name: "全部（入口页）", href: `/rank/${ch}_${type}_${initCatId}` }];
  } else {
    console.log(`  发现 ${categories.length} 个品类`);
  }

  const out = [];
  for (let ci = 0; ci < categories.length; ci++) {
    const cat = categories[ci];
    console.log(`  [${ci + 1}/${categories.length}] ${cat.name}`);
    try {
      ab(run.port, "open", `${FANQIE_ORIGIN}${cat.href}`);
      sleep(2500);
      scrollLoad(run.port, 2);

      const books = extractBookList(run.port).slice(0, run.top);
      if (!books.length) {
        out.push({ name: cat.name, books: [] });
        continue;
      }
      // 分批解码真实书名/作者/简介/题材/标签
      const details = fetchDetails(run.port, books.map((b) => String(b.bookId)));
      out.push({
        name: cat.name,
        books: books.map((b, i) => ({ ...b, rank: i + 1, info: details[String(b.bookId)] || {} })),
      });
    } catch (catErr) {
      const message = errorMessage(catErr);
      console.error(`  [fanqie] 品类 ${cat.name} 处理出错，跳过: ${message}`);
      out.push({ name: cat.name, books: [], error: message });
    }
  }

  return {
    ch,
    type,
    top: run.top,
    mode: "cdp",
    method: "cdp（Chrome CDP + agent-browser，每题材只取得到页面已加载的列表，约 20 本）",
    versionLine: "",
    notes: [],
    categories: out,
  };
}

// ---------------------------------------------------------------------------
// 格式化
// ---------------------------------------------------------------------------

function fmtReads(count) {
  if (!count || count === "0") return "未知";
  const n = parseInt(count, 10);
  if (isNaN(n)) return "未知";
  if (n >= 10000) return (n / 10000).toFixed(1) + "万";
  return String(n);
}

function fmtWords(count) {
  if (!count) return "未知";
  const n = parseInt(count, 10);
  if (isNaN(n)) return "未知";
  if (n >= 10000) return (n / 10000).toFixed(1) + "万";
  return String(n);
}

function fmtStatus(s) {
  const v = String(s);
  if (v === "1") return "连载中";
  if (v === "0" || v === "2") return "已完结";
  return s ? String(s) : "未知";
}

/** 清洗简介：去平台模板文本 → 折叠空白 → 句末截断 100 字 */
function cleanDesc(raw) {
  if (!raw) return "";
  let d = String(raw)
    // 简介可能取自 JSON 字符串原文，先还原常见转义（\n \uXXXX \" 等）
    .replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
    .replace(/\\[nrt]/g, " ")
    .replace(/\\"/g, '"')
    .replace(/番茄小说[^。！？]*?(?:免费阅读|完整版|在线阅读)[^。！？]*[。！？]/g, "")
    .replace(/番茄小说[^。！？]*?(?:免费阅读|完整版|在线阅读)[^。！？]*$/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (d.length <= 100) return d;
  const cut = d.slice(0, 100);
  const m = cut.match(/^[\s\S]*[。！？]/);
  return (m ? m[0] : cut) + "...";
}

function channelLabel(ch) {
  return ch === "1" ? "男频" : "女频";
}

function typeLabel(t) {
  return t === "2" ? "阅读榜" : "新书榜";
}

function outputFilename(ch, type, date) {
  return `番茄${channelLabel(ch)}${typeLabel(type)}_全题材_${date}.md`;
}

/** 标题解析比例是番茄采集成败的核心信号 */
function summarizeTarget(target) {
  let totalBooks = 0;
  let resolvedTitles = 0;
  let failedCategories = 0;
  for (const cat of target.categories) {
    if (cat.error) {
      failedCategories++;
      continue;
    }
    for (const b of cat.books) {
      totalBooks++;
      if (b.info && b.info.title) resolvedTitles++;
    }
  }
  const ratio = totalBooks ? resolvedTitles / totalBooks : 0;
  const quality = totalBooks === 0 ? "[无数据]" : ratio < 0.5 ? "[标题解析异常]" : "[OK]";
  return { totalBooks, resolvedTitles, failedCategories, quality };
}

function renderBook(b, index) {
  const info = b.info || {};
  const title = info.title || "（标题待解析）";
  const author = info.author || "未知";
  const category = info.category || b.category || "";
  const catSeg = category ? ` · ${category}` : "";
  const status = present(b.creationStatus) ? b.creationStatus : info.creationStatus;
  const lines = [
    `### #${b.rank || index + 1} ${title}`,
    `*${author}${catSeg} · ${fmtStatus(status)} · ${fmtReads(b.read_count || info.readCount)} 在读 · ${fmtWords(b.wordNumber || info.wordNumber)}字*`,
  ];
  if (info.tags) lines.push(`**标签：** ${info.tags}`);
  lines.push(`**最新更新：** ${plainText(b.lastChapterTitle) || info.lastChapterTitle || "未知"}`);
  lines.push(`**bookId：** ${b.bookId}`);
  lines.push(`[作品页](${FANQIE_ORIGIN}/page/${b.bookId})`);
  const desc = cleanDesc(info.desc);
  if (desc) lines.push("", "**简介**", "", desc);
  lines.push("");
  return lines;
}

/** 两条路径共用的渲染：文件头字段与正文结构只在这里定义一次 */
function renderTarget(target) {
  const summary = summarizeTarget(target);
  const lines = [
    `# 番茄 · ${channelLabel(target.ch)}${typeLabel(target.type)} · 全 ${target.categories.length} 题材`,
    "",
    `- 频道参数：channel=${target.ch}，type=${target.type}`,
    `- 抓取时间（UTC）：${RUN_CLOCK.utc}`,
    `- 报告日期（本地）：${RUN_CLOCK.localDate}`,
    `- 标题解析：成功 ${summary.resolvedTitles} / 共 ${summary.totalBooks}`,
    `- 数据质量：${summary.quality}`,
    `- 每题材上限 ≈ ${target.top}`,
    `- 抓取方式：${target.method}`,
  ];
  if (target.versionLine) lines.push(`- 榜单版本：${target.versionLine}`);
  if (target.notes && target.notes.length) lines.push(`- 说明：${target.notes.join("；")}`);
  lines.push("", "---", "");

  const body = [];
  for (const cat of target.categories) {
    if (cat.error) {
      body.push(`## ${cat.name} — 采集失败`, "", "---", "");
      continue;
    }
    if (!cat.books.length) {
      body.push(`## ${cat.name} — 0 本`, "", "---", "");
      continue;
    }
    body.push(`## ${cat.name} — ${cat.books.length} 本`, "");
    cat.books.forEach((b, i) => body.push(...renderBook(b, i)));
    body.push("---", "");
  }
  return { ...summary, mode: target.mode, content: lines.concat(body).join("\n") };
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------

function parseOptions(argv) {
  const channel = getArg(argv, "--channel") || "1";
  const type = getArg(argv, "--type") || "2";
  const mode = getArg(argv, "--mode") || "auto";
  const topArg = getArg(argv, "--top");
  if (!["0", "1", "all"].includes(channel)) {
    throw new Error(`未知 --channel: ${channel}`);
  }
  if (!["1", "2", "all"].includes(type)) {
    throw new Error(`未知 --type: ${type}`);
  }
  if (!MODES.includes(mode)) {
    throw new Error(`未知 --mode: ${mode}（可选 ${MODES.join("/")}）`);
  }
  const top = topArg === null ? DEFAULT_TOP : Number(topArg);
  if (topArg !== null && (!/^\d+$/.test(topArg) || top < 1 || top > MAX_TOP)) {
    throw new Error(`无效 --top: ${topArg}（可选 1-${MAX_TOP}）`);
  }
  return {
    port: parseInt(getArg(argv, "--port") || "9222", 10),
    outdir: getArg(argv, "--outdir") || ".",
    channel,
    type,
    mode,
    top,
  };
}

/** 一次运行的共享状态：详情缓存与熔断跨榜单生效。deps 供测试替换网络与等待。 */
function createRun(options, deps = {}) {
  return {
    ...options,
    http: createHttpClient({ fetchImpl: deps.fetchImpl, delay: deps.delay }),
    pause: deps.pause || ((ms) => sleep(ms)),
    collectCdp: deps.collectCdp || collectTargetCdp,
    detailCache: new Map(),
    detailGate: { failedBatches: 0, failedBooks: 0, stopped: false, reason: "" },
  };
}

/**
 * 按 --mode 采集一个榜单，返回待渲染的 target。
 * auto：fetch 健康（数据质量 [OK]）就用；否则回退 CDP，取标题解析更多的一份。
 * CDP 也不可用时保留 fetch 的降级结果（排名/bookId/在读数仍可用），由调用方标 partial。
 */
async function scrapeTarget(ch, type, run) {
  const label = `${channelLabel(ch)}${typeLabel(type)}`;
  if (run.mode === "cdp") return run.collectCdp(ch, type, run);

  let fetched = null;
  let fetchError = null;
  try {
    fetched = await collectTargetFetch(ch, type, run);
  } catch (error) {
    fetchError = error;
    console.error(`  ✗ ${label} fetch 采集失败：${errorMessage(error)}`);
  }
  if (run.mode === "fetch") {
    if (fetchError) throw fetchError;
    return fetched;
  }

  const fetchedSummary = fetched ? summarizeTarget(fetched) : null;
  if (fetchedSummary && fetchedSummary.quality === "[OK]") return fetched;
  const reason = fetchError
    ? `fetch 失败（${errorMessage(fetchError)}）`
    : `fetch 数据质量 ${fetchedSummary.quality}（标题解析 ${fetchedSummary.resolvedTitles}/${fetchedSummary.totalBooks}）`;
  console.log(`  → ${label}：${reason}，回退 CDP 采集`);

  let viaCdp = null;
  let cdpError = null;
  try {
    viaCdp = await run.collectCdp(ch, type, run);
  } catch (error) {
    cdpError = error;
    console.error(
      `  ✗ ${label} CDP 回退失败：${errorMessage(error)}。如需 CDP：先按 browser-cdp 启动 Chrome` +
        `（首次启动会关闭常规 Chrome，须先征得用户同意），再加 --mode cdp 重跑`
    );
  }
  const cdpSummary = viaCdp ? summarizeTarget(viaCdp) : null;
  if (viaCdp && (!fetched || cdpSummary.resolvedTitles > fetchedSummary.resolvedTitles)) {
    viaCdp.notes = [...(viaCdp.notes || []), `auto：${reason}，已回退 CDP`];
    return viaCdp;
  }
  if (fetched) {
    const cdpOutcome = cdpError ? `失败（${errorMessage(cdpError)}）` : "未取得更好的结果";
    fetched.notes.push(`auto：${reason}；CDP 回退${cdpOutcome}，保留 fetch 结果`);
    return fetched;
  }
  throw new Error(`${reason}；CDP 回退${cdpError ? `失败：${errorMessage(cdpError)}` : "无可用数据"}`);
}

async function main() {
  // 参数错误是配置问题：先于任何网络请求与 per-榜单容错快速失败
  const options = parseOptions(process.argv.slice(2));
  const run = createRun(options);
  const channels = options.channel === "all" ? ["1", "0"] : [options.channel];
  const types = options.type === "all" ? ["2", "1"] : [options.type];
  let written = 0;
  let failed = 0;
  const partialReasons = [];

  for (const ch of channels) {
    for (const ty of types) {
      const label = `${channelLabel(ch)}${typeLabel(ty)}`;
      try {
        const target = await scrapeTarget(ch, ty, run);
        if (!target) {
          failed++;
          partialReasons.push(`${label}: no usable data`);
          continue;
        }
        const report = renderTarget(target);
        fs.mkdirSync(options.outdir, { recursive: true });
        const filepath = path.join(options.outdir, outputFilename(ch, ty, RUN_CLOCK.localDate));
        fs.writeFileSync(filepath, report.content, "utf-8");
        written++;
        console.log(`  ✓ 已保存: ${filepath}`);

        if (report.totalBooks > 0 && report.resolvedTitles === 0) {
          console.error(
            `  ✗ ${label}：${report.totalBooks} 本全部标题解析失败。多为详情页被拦截（HTTP 200 空响应）、` +
              `结构变动或登录/验证拦截，请用浏览器打开任一 https://fanqienovel.com/page/{bookId} 确认页面正常。`
          );
        } else if (report.quality === "[标题解析异常]") {
          console.error(
            `  ⚠ ${label}：标题解析率偏低（${report.resolvedTitles}/${report.totalBooks}），结果质量已标注。`
          );
        }
        if (report.quality !== "[OK]") {
          partialReasons.push(
            `${label}: 数据质量 ${report.quality}（标题解析 ${report.resolvedTitles}/${report.totalBooks}）`
          );
        }
        if (report.failedCategories) {
          partialReasons.push(`${label}: ${report.failedCategories} 个题材采集失败`);
        }
      } catch (chErr) {
        failed++;
        const message = errorMessage(chErr);
        partialReasons.push(`${label}: ${message}`);
        console.error(`[fanqie] ${label} 采集失败，跳过: ${message}`);
      }
    }
  }
  return {
    planned: channels.length * types.length,
    written,
    failed,
    partial: failed > 0 || partialReasons.length > 0,
    partialReasons,
  };
}

if (require.main === module) {
  runCli(main, "番茄采集");
}

// 导出纯函数/JS 构建器与流程函数，供测试在 sandbox / 替身网络下验证解析与采集逻辑
module.exports = {
  buildCategoriesJS,
  buildBookListJS,
  buildDetailJS,
  extractInitialState,
  extractTags,
  parseDetailHtml,
  createRun,
  collectTargetFetch,
  scrapeTarget,
  summarizeTarget,
  renderTarget,
  fmtReads,
  fmtWords,
  fmtStatus,
  cleanDesc,
};
