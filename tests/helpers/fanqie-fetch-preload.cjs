"use strict";
// 番茄采集脚本 CLI 测试的预加载（node --require）：
//   - 全局 fetch 换成站点替身，规格来自 FANQIE_FAKE_SITE（JSON）；
//   - cdp-utils 的 sleep 掉空、定时器立即触发，节奏与重试退避不必在测试里真等；
//   - 退出时把请求记录写到 FANQIE_FAKE_SITE_LOG，供测试断言请求量。
const fs = require("fs");
const { createFakeSite } = require("./fanqie-fake-site.cjs");

const site = createFakeSite(JSON.parse(process.env.FANQIE_FAKE_SITE || "{}"));
globalThis.fetch = site.fetchImpl;

const utils = require(process.env.SCAN_TEST_UTILS);
utils.sleep = () => {};
const realSetTimeout = global.setTimeout;
global.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, 0, ...args);

process.on("exit", () => {
  if (process.env.FANQIE_FAKE_SITE_LOG) {
    fs.writeFileSync(process.env.FANQIE_FAKE_SITE_LOG, JSON.stringify(site.calls), "utf8");
  }
});
