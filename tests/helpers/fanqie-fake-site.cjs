"use strict";
// 番茄站点替身：按 URL 路由返回榜单页 SSR、分页 API 与详情页 SSR，供 fetch 路径测试。
// 书目按规格确定性生成：列表级书名/作者/简介用私有区字符模拟字体反爬，最新章节与详情页是明文。
//
// spec = {
//   rankVersion: "1790113200",
//   channels: { "1": [{ id, name, count, borrow?: { from, count } }], "0": [...] },
//   ssrSwapTop2: true,          // SSR 前 10 与 API 首页 #1/#2 互换（真实漂移）
//   ssrReplace: 2,              // SSR 前 10 里换掉几本（集合重合 8/10）
//   ssrForeign: false,          // SSR 前 10 与两个榜单都不重合
//   swapMold: false,            // API 的 rankMold 与 type 对调
//   drift: { catId, offset, rankVersion },  // 某一页返回别的 rankVersion
//   detail: "ok" | "gate",      // gate：详情页一律 HTTP 200 + 空 application/json
//   rankPageStatus: 200,
//   failTimes: { "<url 片段>": n },          // 前 n 次命中返回 HTTP 502
// }

const TYPES = ["2", "1"];

function obfuscate(text) {
  return Array.from(String(text))
    .map((ch, i) => (i % 2 === 0 ? String.fromCharCode(0xe000 + (ch.codePointAt(0) % 0x1000)) : ch))
    .join("");
}

/** SSR 输出的是 JS 对象字面量，真实页面里有裸 undefined；前后还有别的脚本和花括号 */
function stateHtml(state, title = "番茄小说网") {
  const literal = JSON.stringify(state).replace(/"__undefined__"/g, "undefined");
  return [
    "<!DOCTYPE html><html><head>",
    `<title>${title}</title>`,
    '<script>var cfg={"brace":"}{"};</script>',
    '</head><body><div id="app"></div>',
    `<script nonce="t">(function(){window.__INITIAL_STATE__=${literal};})()</script>`,
    '<script>window.later={"x":1};</script>',
    "</body></html>",
  ].join("");
}

function detailHtml(page) {
  return stateHtml(
    {
      common: { user: "__undefined__" },
      rank: { book_list: [], rankVersion: "", total_num: 0 },
      page: { serverRendered: true, hasFetch: true, ...page },
    },
    `${page.bookName || ""}完整版在线免费阅读_番茄小说官网`
  );
}

function bookIdFor(ch, type, catId, pos) {
  // 阅读榜与新书榜书目不相交，rankMold 的集合校验才有意义
  return `7${ch}${type}${String(catId).padStart(5, "0")}${String(pos).padStart(3, "0")}`;
}

function createFakeSite(spec = {}, options = {}) {
  const rankVersion = String(spec.rankVersion || "1790113200");
  const channels = spec.channels || { 1: [{ id: "1141", name: "西方奇幻", count: 12 }] };
  const events = options.events || [];
  const calls = [];
  const failures = {};
  const inFlight = { rank: 0, api: 0, detail: 0 };
  const maxInFlight = { rank: 0, api: 0, detail: 0 };
  const books = new Map();

  function categoryOf(ch, catId) {
    return (channels[ch] || []).find((c) => c.id === String(catId));
  }

  /** 某频道某榜单某题材的完整排名（API 口径） */
  function list(ch, type, catId) {
    const cat = categoryOf(ch, catId);
    if (!cat) return [];
    const ids = [];
    if (cat.borrow) ids.push(...list(ch, type, cat.borrow.from).slice(0, cat.borrow.count));
    for (let pos = 1; ids.length < Math.min(cat.count, 100); pos++) {
      const id = bookIdFor(ch, type, cat.id, pos);
      if (!books.has(id)) {
        books.set(id, { id, pos, catId: cat.id, catName: cat.name, typeName: type === "2" ? "阅读榜" : "新书榜" });
      }
      ids.push(id);
    }
    return ids;
  }

  function apiItem(id, rank) {
    const book = books.get(id);
    const title = `${book.catName}样书${book.pos}`;
    return {
      abstract: obfuscate(`简介${book.pos}`),
      author: obfuscate(`作者${book.pos}`),
      bookId: id,
      bookName: obfuscate(title),
      category: "",
      categoryV2: "",
      creationStatus: "1",
      currentPos: rank,
      curent_category_id: Number(book.catId),
      lastChapterTitle: `第${book.pos}章 明文`,
      rankPosDiff: 0,
      read_count: String(300000 - book.pos * 1000),
      readCount: "0",
      wordNumber: String(500000 + book.pos),
    };
  }

  function detailPage(id) {
    const book = books.get(id);
    return {
      bookId: id,
      bookName: `${book.catName}样书${book.pos}`,
      author: `作者${book.pos}`,
      abstract: `【新书评分刚出，后期会涨】【${book.catName}+系统+爽文】主角醒来，发现世界变了。`,
      category: "",
      categoryV2: JSON.stringify([{ ObjectId: book.catId, Name: book.catName }]),
      wordNumber: 500000 + book.pos,
      creationStatus: 1,
      readCount: 9000 + book.pos,
      lastChapterTitle: `第${book.pos}章 明文`,
      chapterTotal: 100 + book.pos,
    };
  }

  function categoryTable() {
    const table = {};
    for (const [ch, key] of [["1", "male"], ["0", "female"]]) {
      table[key] = (channels[ch] || []).map((c) => ({ id: c.id, name: c.name }));
    }
    return table;
  }

  function ssrList(ch, type, catId) {
    let ids = list(ch, type, catId).slice(0, 10);
    if (spec.ssrSwapTop2 !== false && ids.length >= 2) ids = [ids[1], ids[0], ...ids.slice(2)];
    const replace = Number(spec.ssrReplace || 0);
    for (let i = 0; i < replace && i < ids.length; i++) ids[ids.length - 1 - i] = `9${String(i).padStart(18, "0")}`;
    if (spec.ssrForeign) ids = ids.map((_, i) => `8${String(i).padStart(18, "0")}`);
    return ids.map((id, i) => (books.has(id) ? apiItem(id, i + 1) : { bookId: id, bookName: obfuscate("外部书"), currentPos: i + 1 }));
  }

  function response(body, status = 200, contentType = "text/html; charset=utf-8") {
    return new Response(body, { status, headers: { "content-type": contentType } });
  }

  function route(url) {
    const u = new URL(url);
    const rankPage = u.pathname.match(/^\/rank\/([01])_([12])_(\d+)$/);
    if (rankPage) {
      const [, ch, type, catId] = rankPage;
      if (spec.rankPageStatus && spec.rankPageStatus !== 200) return response("error", spec.rankPageStatus);
      return response(
        stateHtml({
          common: { hasAuthentication: false, user: "__undefined__" },
          rank: {
            serverRendered: true,
            book_list: ssrList(ch, type, catId),
            readRankList: null,
            rankCategoryTypeList: categoryTable(),
            defaultPage: 1,
            total_num: 100,
            rankVersion,
            rankTypeText: "",
          },
          page: { bookId: "", bookName: "", categoryV2: "", creationStatus: -1 },
        })
      );
    }
    if (u.pathname === "/api/rank/category/list") {
      const q = u.searchParams;
      if (q.get("app_id") !== "2503" || q.get("rank_list_type") !== "3") {
        return response(JSON.stringify({ code: 1, message: "bad params" }), 200, "application/json");
      }
      const mold = q.get("rankMold");
      const type = spec.swapMold ? (mold === "2" ? "1" : "2") : mold;
      const ids = list(q.get("gender"), type, q.get("category_id"));
      const offset = Number(q.get("offset"));
      const limit = Number(q.get("limit"));
      const drift = spec.drift;
      const version =
        drift && drift.catId === q.get("category_id") && Number(drift.offset) === offset
          ? String(drift.rankVersion)
          : q.get("rank_version") || rankVersion;
      return response(
        JSON.stringify({
          code: 0,
          data: {
            book_list: ids.slice(offset, offset + limit).map((id, i) => apiItem(id, offset + i + 1)),
            rankTypeText: "",
            rankVersion: version,
            total_num: ids.length,
          },
        }),
        200,
        "application/json; charset=utf-8"
      );
    }
    const detail = u.pathname.match(/^\/page\/(\d+)$/);
    if (detail) {
      if (spec.detail === "gate") return response("", 200, "application/json");
      if (!books.has(detail[1])) return response("not found", 404);
      return response(detailHtml(detailPage(detail[1])));
    }
    return response("not found", 404);
  }

  function kindOf(url) {
    const pathname = new URL(url).pathname;
    if (pathname.startsWith("/rank/")) return "rank";
    if (pathname.startsWith("/api/")) return "api";
    return "detail";
  }

  async function fetchImpl(url, init = {}) {
    const href = String(url);
    const kind = kindOf(href);
    const referer = (init.headers && init.headers.Referer) || null;
    calls.push({ kind, url: href, referer });
    events.push(["fetch", kind, href]);
    inFlight[kind]++;
    maxInFlight[kind] = Math.max(maxInFlight[kind], inFlight[kind]);
    try {
      // 让出一轮事件循环，并发请求才会真的重叠
      await new Promise((resolve) => setImmediate(resolve));
      for (const [needle, times] of Object.entries(spec.failTimes || {})) {
        if (!href.includes(needle)) continue;
        failures[needle] = (failures[needle] || 0) + 1;
        if (failures[needle] <= times) return response("bad gateway", 502);
      }
      return route(href);
    } finally {
      inFlight[kind]--;
    }
  }

  // 预先生成全部书目，测试可直接拿到期望排名
  for (const ch of Object.keys(channels)) {
    for (const cat of channels[ch]) for (const type of TYPES) list(ch, type, cat.id);
  }

  return { fetchImpl, calls, events, maxInFlight, list, bookIdFor, detailPage };
}

module.exports = { createFakeSite, stateHtml, detailHtml, obfuscate, bookIdFor };
