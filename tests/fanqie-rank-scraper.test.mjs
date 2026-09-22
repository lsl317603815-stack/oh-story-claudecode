import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { describe, test } from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const repositoryRoot = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const scriptsDir = path.join(repositoryRoot, "skills/story-long-scan/scripts");
const scraperPath = path.join(scriptsDir, "fanqie-rank-scraper.js");
const preloadPath = path.join(repositoryRoot, "tests/helpers/fanqie-fetch-preload.cjs");
const fanqie = require(scraperPath);
const { createFakeSite, stateHtml, detailHtml, obfuscate, bookIdFor } = require("./helpers/fanqie-fake-site.cjs");

// 与脚本同一份宣传词：断言输出标签里不残留宣传/公告
const PROMO_WORDS = /新书|新作|评分|完本|完结|断更|出版|实体书|书友|群号|食用|捧场|放心|已更新|更新至|求月票|求收藏|打卡|签约|作品《|祝/;

async function quietly(fn) {
  const { log, error } = console;
  console.log = () => {};
  console.error = () => {};
  try {
    return await fn();
  } finally {
    console.log = log;
    console.error = error;
  }
}

/** fetch 路径的 run：网络换成站点替身，节奏只记录不等待，CDP 默认禁止调用 */
function fetchRun(site, overrides = {}, deps = {}) {
  const pauses = [];
  const delays = [];
  const run = fanqie.createRun(
    { mode: "fetch", top: 20, port: 9222, outdir: ".", channel: "1", type: "2", ...overrides },
    {
      fetchImpl: site.fetchImpl,
      pause: (ms) => {
        pauses.push(ms);
        site.events.push(["pause", ms]);
      },
      delay: async (ms) => {
        delays.push(ms);
      },
      collectCdp: () => {
        throw new Error("CDP must not be used here");
      },
      ...deps,
    }
  );
  return { run, pauses, delays };
}

function apiParams(call) {
  return Object.fromEntries(new URL(call.url).searchParams);
}

describe("tag extraction", () => {
  test("skips promo and notice brackets that precede the genre tags", () => {
    const cases = [
      ["【新书：末日：我真不是土匪】【末世+囤货+无敌流】灾变第一天。", ["末世", "囤货", "无敌流"]],
      ["【新书评分刚出，后期会涨】【玄幻+系统+杀伐果断】少年拔剑。", ["玄幻", "系统", "杀伐果断"]],
      ["【提示：潜能点仅限对本校学生使用…】\n【高武+校园+群像】开学第一天。", ["高武", "校园", "群像"]],
      ["【已出版质量保证，架空高武+群像权谋…】【历史+权谋+争霸】乱世将至。", ["历史", "权谋", "争霸"]],
      ["【文爽梗多，已有超多书友品尝…】\n【都市+神豪+轻松】", ["都市", "神豪", "轻松"]],
    ];
    for (const [abstract, expected] of cases) {
      const tags = fanqie.extractTags(abstract);
      assert.deepEqual(tags, expected, abstract);
      for (const tag of tags) assert.doesNotMatch(tag, PROMO_WORDS, abstract);
    }
  });

  test("drops promo parts inside an otherwise valid tag group", () => {
    assert.deepEqual(fanqie.extractTags("【新书+种田+慢热】日出而作。"), ["种田", "慢热"]);
  });

  test("needs at least two short parts before a group counts as combined tags", () => {
    assert.deepEqual(fanqie.extractTags("【这是一段超过十个字的长标签内容+短标签】他醒了。"), []);
    assert.deepEqual(fanqie.extractTags("【新书+种田】日出而作。"), []);
  });

  test("takes the first run of adjacent single-token groups", () => {
    assert.deepEqual(fanqie.extractTags("【日常】【修罗场】【多女】主角是个普通人。"), ["日常", "修罗场", "多女"]);
    assert.deepEqual(fanqie.extractTags("开篇说明 【日常】 【修罗场】\n【多女】"), ["日常", "修罗场", "多女"]);
    // 中间隔着正文就不算一串，只剩规则 3 的开头短括号
    assert.deepEqual(fanqie.extractTags("【日常】主角【修罗场】"), ["日常"]);
    // 宣传括号打断连串
    assert.deepEqual(fanqie.extractTags("【日常】【新书】【多女】"), ["日常"]);
  });

  test("accepts one short leading bracket only near the start of the abstract", () => {
    assert.deepEqual(fanqie.extractTags("【全民求生】开局一艘木筏。"), ["全民求生"]);
    assert.deepEqual(fanqie.extractTags("  【全民求生】开局一艘木筏。"), ["全民求生"]);
    assert.deepEqual(fanqie.extractTags("穿越之后【全民求生】开局一艘木筏。"), ["全民求生"]);
    assert.deepEqual(fanqie.extractTags("穿越到异界后【全民求生】开局一艘木筏。"), []);
    assert.deepEqual(fanqie.extractTags("【新书上架】开局一艘木筏。"), []);
    assert.deepEqual(fanqie.extractTags("【这是超过八个字的单个标签】开局。"), []);
  });

  test("never uses sentence-like or colon brackets, even when short", () => {
    assert.deepEqual(fanqie.extractTags("【注意：慢热】【日常】"), []);
    assert.deepEqual(fanqie.extractTags("【爽！】正文"), []);
  });

  test("splits on every documented separator and keeps at most six tags", () => {
    assert.deepEqual(fanqie.extractTags("【种田＋慢热、西幻/群像｜爽文•日常·系统 无敌】"), [
      "种田",
      "慢热",
      "西幻",
      "群像",
      "爽文",
      "日常",
    ]);
    assert.deepEqual(fanqie.extractTags("[末世,囤货|无敌]"), ["末世", "囤货", "无敌"]);
  });

  test("returns no tags for missing or bracket-free abstracts", () => {
    for (const value of ["", undefined, null, "没有括号的简介。"]) {
      assert.deepEqual(fanqie.extractTags(value), []);
    }
  });
});

describe("SSR state parsing", () => {
  test("cuts the state literal by brace matching and nulls bare undefined", () => {
    const html = String.raw`<script>var x={"a":"}"};</script><script>window.__INITIAL_STATE__ = {"a":"}{\"x","b":undefined,"c":{"d":[1,{"e":"undefined"}]},"g":[undefined]};var after={"z":1};</script>`;
    assert.deepEqual(fanqie.extractInitialState(html), {
      a: '}{"x',
      b: null,
      c: { d: [1, { e: "undefined" }] },
      g: [null],
    });
  });

  test("returns null when the marker is missing or the literal is not JSON", () => {
    assert.equal(fanqie.extractInitialState("<html></html>"), null);
    assert.equal(fanqie.extractInitialState('window.__INITIAL_STATE__={"a":1'), null);
    assert.equal(fanqie.extractInitialState("window.__INITIAL_STATE__={a:1};"), null);
  });

  test("reads plaintext detail fields from state.page", () => {
    const html = detailHtml({
      bookId: "101",
      bookName: "末世囤货",
      author: "作者甲",
      abstract: "【新书：末日：我真不是土匪】【末世+囤货+无敌流】灾变第一天。",
      categoryV2: JSON.stringify([{ ObjectId: "8", Name: "科幻末世" }, { ObjectId: "1", Name: "囤货" }]),
      wordNumber: 1234567,
      creationStatus: 1,
      readCount: 88000,
      lastChapterTitle: "第88章 天亮了",
      chapterTotal: 88,
    });
    const info = fanqie.parseDetailHtml(html, "101");
    assert.equal(info.source, "state");
    assert.equal(info.title, "末世囤货");
    assert.equal(info.author, "作者甲");
    assert.equal(info.category, "科幻末世");
    assert.equal(info.tags, "末世、囤货、无敌流");
    assert.equal(info.wordNumber, "1234567");
    assert.equal(info.creationStatus, "1");
    assert.equal(info.readCount, "88000");
    assert.equal(info.lastChapterTitle, "第88章 天亮了");
    assert.equal(info.chapterTotal, "88");
    assert.equal(info.err, undefined);
  });

  test("treats empty, shell, obfuscated or foreign pages as unresolved", () => {
    assert.match(fanqie.parseDetailHtml("", "1").err, /响应体为空/);
    const shell = stateHtml({ rank: { book_list: [{ bookId: "5", bookName: "别的书" }] }, page: { bookId: "", bookName: "" } });
    const shellInfo = fanqie.parseDetailHtml(shell, "1");
    assert.equal(shellInfo.title, "", "空壳页不得从别处抓书名");
    assert.match(shellInfo.err, /没有明文书名/);
    assert.equal(fanqie.parseDetailHtml(detailHtml({ bookId: "1", bookName: obfuscate("乱码书名") }), "1").title, "");
    const foreign = fanqie.parseDetailHtml(detailHtml({ bookId: "999", bookName: "别的书" }), "1");
    assert.equal(foreign.title, "");
    assert.match(foreign.err, /另一本书/);
  });

  test("falls back to the legacy regex strategies when there is no state.page", () => {
    const blob = JSON.stringify({
      bookName: "回退书名",
      author: "回退作者",
      abstract: '【种田+慢热+西幻】这是"引号"简介。',
      categoryV2: JSON.stringify([{ ObjectId: "1141", Name: "西方奇幻" }]),
    });
    const html = `<html><head><title>回退书名完整版在线免费阅读_番茄小说官网</title></head><body><script>window.__DATA__=${blob}</script></body></html>`;
    const info = fanqie.parseDetailHtml(html, "1");
    assert.equal(info.source, "regex");
    assert.equal(info.title, "回退书名");
    assert.equal(info.author, "回退作者");
    assert.equal(info.desc, '【种田+慢热+西幻】这是"引号"简介。');
    assert.equal(info.category, "西方奇幻");
    assert.equal(info.tags, "种田、慢热、西幻");
  });
});

describe("description cleanup", () => {
  test("cleanDesc drops the platform template, unescapes, and cuts long text at a sentence end", () => {
    assert.equal(
      fanqie.cleanDesc("番茄小说提供某书完整版在线免费阅读。主角醒来，\n发现世界变了。"),
      "主角醒来， 发现世界变了。"
    );
    assert.equal(fanqie.cleanDesc(String.raw`转义\n换行\"引号\"中`), '转义 换行"引号"中');
    const long = `${"甲".repeat(60)}。${"乙".repeat(60)}。`;
    assert.equal(fanqie.cleanDesc(long), `${"甲".repeat(60)}。...`);
    assert.equal(fanqie.cleanDesc(""), "");
  });
});

describe("CDP page builders run in a browser sandbox", () => {
  test("buildDetailJS decodes detail pages with the shared parser", () => {
    const pages = {
      "/page/101": {
        status: 200,
        body: detailHtml({
          bookId: "101",
          bookName: "末世囤货",
          author: "作者甲",
          abstract: "【新书评分刚出，后期会涨】【末世+囤货+无敌流】灾变第一天。",
          categoryV2: JSON.stringify([{ ObjectId: "8", Name: "科幻末世" }]),
        }),
      },
      "/page/102": { status: 200, body: "" },
      "/page/103": { status: 403, body: "forbidden" },
    };
    class FakeXHR {
      open(method, url, async) {
        assert.equal(method, "GET");
        assert.equal(async, false, "CDP 路径用同步 XHR");
        this.url = url;
      }
      send() {
        if (this.url === "/page/104") throw new Error("network down");
        this.status = pages[this.url].status;
        this.responseText = pages[this.url].body;
      }
    }
    const context = vm.createContext({ XMLHttpRequest: FakeXHR });
    const map = JSON.parse(vm.runInContext(fanqie.buildDetailJS(["101", "102", "103", "104"]), context));
    assert.equal(map["101"].title, "末世囤货");
    assert.equal(map["101"].category, "科幻末世");
    assert.equal(map["101"].tags, "末世、囤货、无敌流", "CDP 路径同样跳过宣传括号");
    assert.match(map["102"].err, /响应体为空/);
    assert.equal(map["103"].err, "HTTP 403");
    assert.equal(map["104"].err, "network down");
  });

  test("buildBookListJS normalizes the SSR rank list", () => {
    const context = vm.createContext({
      window: {
        __INITIAL_STATE__: {
          rank: {
            book_list: [
              { bookId: 1, read_count: "123", wordNumber: "456", creationStatus: "1", lastChapterTitle: "第1章" },
              { bookName: "无 id 的条目被丢弃" },
            ],
          },
        },
      },
    });
    assert.deepEqual(JSON.parse(vm.runInContext(fanqie.buildBookListJS(), context)), [
      { bookId: "1", read_count: "123", wordNumber: "456", creationStatus: "1", lastChapterTitle: "第1章", category: "" },
    ]);
  });

  test("buildCategoriesJS keeps only this channel's rank links", () => {
    const anchor = (href, text) => ({ getAttribute: () => href, innerText: text });
    const context = vm.createContext({
      document: {
        querySelectorAll: () => [
          anchor("/rank/1_2_1141", "西方奇幻"),
          anchor("/rank/1_2_1141", "西方奇幻"),
          anchor("/rank/0_2_1139", "古风世情"),
          anchor("/rank/1_2_262", " 都市脑洞 "),
          anchor("/rank/1_2_8", ""),
        ],
      },
    });
    assert.deepEqual(JSON.parse(vm.runInContext(fanqie.buildCategoriesJS("/rank/1_2_"), context)), [
      { name: "西方奇幻", href: "/rank/1_2_1141" },
      { name: "都市脑洞", href: "/rank/1_2_262" },
    ]);
  });
});

describe("fetch mode", () => {
  test("pages the rank API under one pinned rankVersion and takes ranks from the API", async () => {
    const site = createFakeSite({
      channels: { 1: [{ id: "1141", name: "西方奇幻", count: 25 }, { id: "262", name: "都市脑洞", count: 12 }] },
    });
    const { run } = fetchRun(site, { top: 25 });
    const target = await quietly(() => fanqie.collectTargetFetch("1", "2", run));

    const [first, second] = target.categories;
    assert.deepEqual(first.books.map((b) => b.bookId), site.list("1", "2", "1141").slice(0, 25));
    assert.deepEqual(first.books.map((b) => b.rank), Array.from({ length: 25 }, (_, i) => i + 1));
    assert.equal(second.books.length, 12, "total_num 不足 --top 时按实际条数停");

    const apiCalls = site.calls.filter((c) => c.kind === "api");
    assert.equal(apiCalls.length, 5, "1141 三页（首页复用映射校验那一页）+ 262 两页");
    for (const call of apiCalls) {
      const q = apiParams(call);
      assert.equal(q.app_id, "2503");
      assert.equal(q.rank_list_type, "3");
      assert.equal(q.limit, "10");
      assert.equal(q.gender, "1");
      assert.equal(q.rankMold, "2");
      assert.equal(q.rank_version, "1790113200", "全部分页固定开头取到的 rankVersion");
      assert.equal(call.referer, `https://fanqienovel.com/rank/1_2_${q.category_id}`);
    }
    assert.deepEqual(
      apiCalls.map((c) => `${apiParams(c).category_id}@${apiParams(c).offset}`),
      ["1141@0", "1141@10", "1141@20", "262@0", "262@10"]
    );

    const rankCalls = site.calls.filter((c) => c.kind === "rank");
    assert.equal(rankCalls.length, 1, "每个榜单只取一次榜单页 SSR");
    assert.equal(rankCalls[0].referer, null);
    const detailCalls = site.calls.filter((c) => c.kind === "detail");
    assert.equal(detailCalls.length, 37);
    assert.ok(detailCalls.every((c) => /^https:\/\/fanqienovel\.com\/rank\/1_2_\d+$/.test(c.referer)));
  });

  test("renders the documented report header and book blocks", async () => {
    const site = createFakeSite({ channels: { 1: [{ id: "1141", name: "西方奇幻", count: 12 }] } });
    const { run } = fetchRun(site, { top: 12 });
    const target = await quietly(() => fanqie.collectTargetFetch("1", "2", run));
    const report = fanqie.renderTarget(target);
    const lines = report.content.split("\n");

    assert.equal(lines[0], "# 番茄 · 男频阅读榜 · 全 1 题材");
    assert.equal(lines[1], "");
    assert.equal(lines[2], "- 频道参数：channel=1，type=2");
    assert.match(lines[3], /^- 抓取时间（UTC）：\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    assert.match(lines[4], /^- 报告日期（本地）：\d{8}$/);
    assert.equal(lines[5], "- 标题解析：成功 12 / 共 12");
    assert.equal(lines[6], "- 数据质量：[OK]");
    assert.equal(lines[7], "- 每题材上限 ≈ 12");
    assert.match(lines[8], /^- 抓取方式：fetch（/);
    assert.match(lines[9], /^- 榜单版本：rankVersion=1790113200（取自榜单页 SSR，全部分页固定此版本），rankMold=2（入口题材 SSR 与 API 首页重合 10\/10）；API 版本漂移：无$/);
    assert.deepEqual(lines.slice(10, 13), ["", "---", ""]);
    assert.equal(report.quality, "[OK]");

    const id = site.list("1", "2", "1141")[0];
    const block = report.content.slice(report.content.indexOf("### #1 "), report.content.indexOf("### #2 "));
    assert.equal(
      block,
      [
        "### #1 西方奇幻样书1",
        "*作者1 · 西方奇幻 · 连载中 · 29.9万 在读 · 50.0万字*",
        "**标签：** 西方奇幻、系统、爽文",
        "**最新更新：** 第1章 明文",
        `**bookId：** ${id}`,
        `[作品页](https://fanqienovel.com/page/${id})`,
        "",
        "**简介**",
        "",
        "【新书评分刚出，后期会涨】【西方奇幻+系统+爽文】主角醒来，发现世界变了。",
        "",
        "",
      ].join("\n")
    );
    assert.ok(report.content.includes("## 西方奇幻 — 12 本"));
    assert.ok(!/[-]/.test(report.content), "列表级私有区乱码不得写进报告");
  });

  test("accepts a drifting SSR list by set overlap and records rankVersion drift", async () => {
    const site = createFakeSite({
      channels: { 1: [{ id: "1141", name: "西方奇幻", count: 10 }, { id: "262", name: "都市脑洞", count: 20 }] },
      ssrReplace: 2,
      drift: { catId: "262", offset: 10, rankVersion: "1790116800" },
    });
    const { run } = fetchRun(site, { top: 20 });
    const target = await quietly(() => fanqie.collectTargetFetch("1", "2", run));
    assert.match(target.versionLine, /重合 8\/10/);
    assert.match(target.versionLine, /API 版本漂移：1 页（都市脑洞 offset=10 返回 1790116800）$/);
    const versions = site.calls.filter((c) => c.kind === "api").map((c) => apiParams(c).rank_version);
    assert.ok(versions.every((v) => v === "1790113200"), "漂移后仍按固定版本请求");
  });

  test("switches rankMold when the set overlap shows the mapping flipped", async () => {
    const site = createFakeSite({ channels: { 1: [{ id: "1141", name: "西方奇幻", count: 15 }] }, swapMold: true });
    const { run } = fetchRun(site, { top: 15 });
    const target = await quietly(() => fanqie.collectTargetFetch("1", "2", run));
    assert.deepEqual(target.categories[0].books.map((b) => b.bookId), site.list("1", "2", "1141"));
    assert.match(target.notes.join("；"), /改用 rankMold=1/);
    const molds = site.calls.filter((c) => c.kind === "api").map((c) => apiParams(c).rankMold);
    assert.deepEqual(molds, ["2", "1", "1"], "先试 type 对应的 rankMold，不重合再换，之后全用校验通过的那个");
  });

  test("gives up on fetch when neither rankMold matches the SSR list", async () => {
    const site = createFakeSite({ channels: { 1: [{ id: "1141", name: "西方奇幻", count: 10 }] }, ssrForeign: true });
    const { run } = fetchRun(site);
    await assert.rejects(quietly(() => fanqie.collectTargetFetch("1", "2", run)), /rankMold↔type 映射校验失败/);
  });

  test("paces list pages and detail batches and retries transient failures with backoff", async () => {
    const flaky = bookIdFor("1", "2", "1141", 3);
    const flakySite = createFakeSite({
      channels: { 1: [{ id: "1141", name: "西方奇幻", count: 12 }] },
      failTimes: { [`/page/${flaky}`]: 2 },
    });
    const { run, delays } = fetchRun(flakySite, { top: 12 });
    const target = await quietly(() => fanqie.collectTargetFetch("1", "2", run));

    const events = flakySite.events;
    events.forEach((event, i) => {
      if (event[0] === "fetch" && event[1] === "api") {
        assert.deepEqual(events[i - 1], ["pause", 400], "每个分页请求前停 400ms");
      }
    });
    const detailPauses = events.filter((e) => e[0] === "pause" && e[1] === 300).length;
    assert.equal(detailPauses, 3, "12 本详情分 3 批，每批后停 300ms");
    let batch = new Set();
    for (const event of events) {
      if (event[0] === "pause" && event[1] === 300) {
        assert.ok(batch.size <= 5, `一批最多 5 本，实际 ${batch.size}`);
        batch = new Set();
      } else if (event[0] === "fetch" && event[1] === "detail") {
        batch.add(event[2]);
      }
    }
    assert.equal(flakySite.maxInFlight.detail, 5, "详情页 5 本并发");
    assert.equal(flakySite.calls.filter((c) => c.url.endsWith(`/page/${flaky}`)).length, 3, "失败的 GET 最多尝试 3 次");
    assert.deepEqual(delays, [600, 1200], "两次重试之间指数退避");
    assert.equal(target.categories[0].books[2].info.title, "西方奇幻样书3", "重试成功后照常解析");
  });

  test("stops requesting detail pages after two fully failed batches", async () => {
    const site = createFakeSite({
      channels: { 1: [{ id: "1141", name: "西方奇幻", count: 25 }], 0: [{ id: "1139", name: "古风世情", count: 5 }] },
      detail: "gate",
    });
    const { run } = fetchRun(site, { top: 25 });
    const male = await quietly(() => fanqie.collectTargetFetch("1", "2", run));
    const detailCalls = site.calls.filter((c) => c.kind === "detail");
    assert.equal(new Set(detailCalls.map((c) => c.url)).size, 10, "两批 10 本全失败后不再请求新的详情页");
    assert.equal(detailCalls.length, 30, "每本最多尝试 3 次");
    assert.ok(run.detailGate.stopped);
    assert.equal(fanqie.summarizeTarget(male).quality, "[标题解析异常]");
    assert.match(male.notes.join("；"), /详情页连续 10 本请求失败（HTTP 200 但响应体为空（疑似被拦截））/);
    assert.match(male.categories[0].books[24].info.err, /^未请求：/);

    await quietly(() => fanqie.collectTargetFetch("0", "2", run));
    assert.equal(site.calls.filter((c) => c.kind === "detail").length, 30, "熔断对同一次运行的后续榜单持续生效");
  });

  test("reuses decoded detail pages across categories", async () => {
    const site = createFakeSite({
      channels: {
        1: [
          { id: "1141", name: "西方奇幻", count: 10 },
          { id: "262", name: "都市脑洞", count: 6, borrow: { from: "1141", count: 3 } },
        ],
      },
    });
    const { run } = fetchRun(site, { top: 10 });
    const target = await quietly(() => fanqie.collectTargetFetch("1", "2", run));
    const detailUrls = site.calls.filter((c) => c.kind === "detail").map((c) => c.url);
    assert.equal(detailUrls.length, new Set(detailUrls).size, "同一本书只请求一次详情页");
    assert.equal(detailUrls.length, 13);
    assert.equal(target.categories[1].books[0].info.title, "西方奇幻样书1");
  });
});

describe("auto mode", () => {
  function cdpTarget(resolved) {
    return {
      ch: "1",
      type: "2",
      top: 20,
      mode: "cdp",
      method: "cdp（测试替身）",
      versionLine: "",
      notes: [],
      categories: [
        {
          name: "西方奇幻",
          books: Array.from({ length: 4 }, (_, i) => ({
            rank: i + 1,
            bookId: String(i + 1),
            info: i < resolved ? { title: `CDP 书${i + 1}` } : {},
          })),
        },
      ],
    };
  }

  test("keeps a healthy fetch result without touching CDP", async () => {
    const site = createFakeSite({ channels: { 1: [{ id: "1141", name: "西方奇幻", count: 10 }] } });
    const { run } = fetchRun(site, { mode: "auto", top: 10 });
    const target = await quietly(() => fanqie.scrapeTarget("1", "2", run));
    assert.equal(target.mode, "fetch");
    assert.equal(fanqie.summarizeTarget(target).quality, "[OK]");
  });

  test("falls back to CDP when fetch fails", async () => {
    const site = createFakeSite({ rankPageStatus: 500 });
    let cdpCalls = 0;
    const { run } = fetchRun(site, { mode: "auto" }, {
      collectCdp: () => {
        cdpCalls++;
        return cdpTarget(4);
      },
    });
    const target = await quietly(() => fanqie.scrapeTarget("1", "2", run));
    assert.equal(cdpCalls, 1);
    assert.equal(target.mode, "cdp");
    assert.match(target.notes.join("；"), /auto：fetch 失败（HTTP 500），已回退 CDP/);
    assert.equal(site.calls.filter((c) => c.kind === "rank").length, 3, "榜单页 SSR 失败也按 3 次重试");
  });

  test("keeps the degraded fetch report when the CDP fallback is unavailable or no better", async () => {
    const gated = { channels: { 1: [{ id: "1141", name: "西方奇幻", count: 10 }] }, detail: "gate" };

    const unavailable = fetchRun(createFakeSite(gated), { mode: "auto", top: 10 }, {
      collectCdp: () => {
        throw new Error("agent-browser failed: spawn agent-browser ENOENT");
      },
    });
    const kept = await quietly(() => fanqie.scrapeTarget("1", "2", unavailable.run));
    assert.equal(kept.mode, "fetch");
    const report = fanqie.renderTarget(kept);
    assert.match(report.content, /^- 数据质量：\[标题解析异常\]$/m);
    assert.match(
      report.content,
      /^- 说明：.*auto：fetch 数据质量 \[标题解析异常\]（标题解析 0\/10）；CDP 回退失败（agent-browser failed: spawn agent-browser ENOENT），保留 fetch 结果$/m
    );

    const noBetter = fetchRun(createFakeSite(gated), { mode: "auto", top: 10 }, { collectCdp: () => cdpTarget(0) });
    const stillFetch = await quietly(() => fanqie.scrapeTarget("1", "2", noBetter.run));
    assert.equal(stillFetch.mode, "fetch", "CDP 没解析出更多书名时保留更深的 fetch 列表");

    const better = fetchRun(createFakeSite(gated), { mode: "auto", top: 10 }, { collectCdp: () => cdpTarget(3) });
    assert.equal((await quietly(() => fanqie.scrapeTarget("1", "2", better.run))).mode, "cdp");
  });

  test("fetch mode never falls back and cdp mode never fetches", async () => {
    const failing = createFakeSite({ rankPageStatus: 500 });
    const { run } = fetchRun(failing, { mode: "fetch" });
    await assert.rejects(quietly(() => fanqie.scrapeTarget("1", "2", run)), /HTTP 500/);

    const untouched = createFakeSite();
    const cdpOnly = fetchRun(untouched, { mode: "cdp" }, { collectCdp: () => cdpTarget(4) });
    assert.equal((await quietly(() => fanqie.scrapeTarget("1", "2", cdpOnly.run))).mode, "cdp");
    assert.equal(untouched.calls.length, 0);
  });
});

describe("CLI", () => {
  const FAKE_AGENT_BROWSER = `#!/usr/bin/env node
const fs = require("fs");
if (process.env.AGENT_BROWSER_MARKER) {
  fs.appendFileSync(process.env.AGENT_BROWSER_MARKER, JSON.stringify(process.argv.slice(2)) + "\\n");
}
process.stderr.write("CDP connection refused\\n");
process.exit(1);
`;

  /** 放一个会失败并留痕的 agent-browser 替身在 PATH 最前面（Windows 走 npm 的 .cmd shim 形态） */
  function writeFakeAgentBrowser(dir) {
    if (process.platform === "win32") {
      fs.writeFileSync(path.join(dir, "fake-agent-browser.js"), FAKE_AGENT_BROWSER, "utf8");
      fs.writeFileSync(
        path.join(dir, "agent-browser.cmd"),
        `@echo off\r\n"${process.execPath}" "%~dp0fake-agent-browser.js" %*\r\n`,
        "utf8"
      );
      return;
    }
    const bin = path.join(dir, "agent-browser");
    fs.writeFileSync(bin, FAKE_AGENT_BROWSER, "utf8");
    fs.chmodSync(bin, 0o755);
  }

  function runScraperCli(args, spec) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fanqie-cli-"));
    try {
      writeFakeAgentBrowser(dir);
      const outdir = path.join(dir, "out");
      const marker = path.join(dir, "agent-browser-calls.log");
      const siteLog = path.join(dir, "site-calls.json");
      const result = spawnSync(
        process.execPath,
        ["--require", preloadPath, scraperPath, ...args, "--outdir", outdir],
        {
          cwd: repositoryRoot,
          encoding: "utf8",
          timeout: 60000,
          env: {
            ...process.env,
            PATH: `${dir}${path.delimiter}${process.env.PATH}`,
            FANQIE_FAKE_SITE: JSON.stringify(spec || {}),
            FANQIE_FAKE_SITE_LOG: siteLog,
            SCAN_TEST_UTILS: path.join(scriptsDir, "cdp-utils.js"),
            AGENT_BROWSER_MARKER: marker,
          },
        }
      );
      const files = fs.existsSync(outdir) ? fs.readdirSync(outdir).sort() : [];
      return {
        ...result,
        files,
        contents: files.map((name) => fs.readFileSync(path.join(outdir, name), "utf8")),
        agentBrowserCalls: fs.existsSync(marker) ? fs.readFileSync(marker, "utf8").trim().split("\n") : [],
        siteCalls: fs.existsSync(siteLog) ? JSON.parse(fs.readFileSync(siteLog, "utf8")) : [],
      };
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  test("rejects bad arguments before any request", () => {
    const cases = [
      [["--mode", "bogus"], /未知 --mode: bogus（可选 auto\/fetch\/cdp）/],
      [["--top", "101"], /无效 --top: 101（可选 1-100）/],
      [["--top", "0"], /无效 --top: 0/],
      [["--top=abc"], /无效 --top: abc/],
      [["--channel", "2"], /未知 --channel: 2/],
    ];
    for (const [args, message] of cases) {
      const run = runScraperCli(args);
      assert.equal(run.status, 1, `${args.join(" ")}: ${run.stderr}`);
      assert.match(run.stderr, message);
      assert.deepEqual(run.files, []);
      assert.deepEqual(run.siteCalls, [], "参数错误不得发出任何请求");
      assert.deepEqual(run.agentBrowserCalls, []);
    }
  });

  test("default auto mode writes the report over plain HTTPS without agent-browser", () => {
    const run = runScraperCli(["--channel", "1", "--type", "2", "--top", "15"], {
      channels: { 1: [{ id: "1141", name: "西方奇幻", count: 15 }, { id: "262", name: "都市脑洞", count: 5 }] },
    });
    assert.equal(run.status, 0, run.stderr);
    assert.deepEqual(run.agentBrowserCalls, [], "fetch 健康时不得调用 agent-browser");
    assert.equal(run.files.length, 1);
    const [content] = run.contents;
    const reportDate = content.match(/^- 报告日期（本地）：(\d{8})$/m)[1];
    assert.equal(run.files[0], `番茄男频阅读榜_全题材_${reportDate}.md`, "文件名与报告日期来自同一个 run clock");
    assert.match(content, /^- 标题解析：成功 20 \/ 共 20$/m);
    assert.match(content, /^- 数据质量：\[OK\]$/m);
    assert.match(content, /^- 抓取方式：fetch（/m);
    assert.match(content, /^## 西方奇幻 — 15 本$/m);
    assert.match(content, /^## 都市脑洞 — 5 本$/m);
  });

  test("auto mode keeps the fetch report and exits 2 when details are gated and CDP is unavailable", () => {
    const run = runScraperCli(["--channel", "1", "--type", "2", "--top", "10"], {
      channels: { 1: [{ id: "1141", name: "西方奇幻", count: 10 }, { id: "262", name: "都市脑洞", count: 10 }] },
      detail: "gate",
    });
    assert.equal(run.status, 2, `${run.stdout}\n${run.stderr}`);
    assert.match(run.stderr, /番茄采集 partial: wrote 1\/1; 男频阅读榜: 数据质量 \[标题解析异常\]（标题解析 0\/20）/);
    assert.ok(run.agentBrowserCalls.length > 0, "auto 应尝试 CDP 回退");
    assert.equal(run.files.length, 1, "CDP 不可用时保留 fetch 的降级报告");
    const [content] = run.contents;
    assert.match(content, /^- 数据质量：\[标题解析异常\]$/m);
    assert.match(content, /^- 说明：.*CDP 回退失败（agent-browser failed: CDP connection refused），保留 fetch 结果$/m);
    assert.match(content, /^### #10 （标题待解析）$/m);
    const detailIds = new Set(run.siteCalls.filter((c) => c.kind === "detail").map((c) => c.url));
    assert.equal(detailIds.size, 10, "熔断后不再请求新的详情页");
  });
});
