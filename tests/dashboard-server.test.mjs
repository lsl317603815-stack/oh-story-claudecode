import assert from "node:assert/strict";
import { chmod, mkdtemp, mkdir, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { afterEach, describe, test } from "node:test";
import {
  DashboardError,
  browserLaunchCommand,
  countManuscriptCharacters,
  createDashboardServer,
  listWorkspaceDirectory,
  parseTargetWords,
  pathsReferToSameFile,
  readProjectStatus,
  resolveWorkspaceDirectory,
  resolveWorkspacePath,
  scanWorkspace,
  searchWorkspace,
} from "../skills/story/scripts/dashboard-server.mjs";
import { createDashboardFixture } from "./helpers/create-dashboard-fixture.mjs";

const temporaryDirectories = [];
const runningServers = [];

afterEach(async () => {
  await Promise.all(
    runningServers.splice(0).map(
      (server) => new Promise((accept) => server.close(accept)),
    ),
  );
  await Promise.all(
    temporaryDirectories.splice(0).map((directory) =>
      rm(directory, { recursive: true, force: true }),
    ),
  );
});

async function createWorkspace() {
  const root = await mkdtemp(resolve(tmpdir(), "oh-story-dashboard-test-"));
  temporaryDirectories.push(root);
  await mkdir(resolve(root, "拆文库", "对标样本甲", "章节"), { recursive: true });
  await mkdir(resolve(root, "长篇", "示例书", "大纲"), { recursive: true });
  await mkdir(resolve(root, "长篇", "示例书", "正文"), { recursive: true });
  // 基建目录必须落在被扫描的库/项目内部：放在工作区根下永远进不了树，
  // 断言就成了空转，测不出忽略规则有没有失效。
  await mkdir(resolve(root, "长篇", "示例书", ".git", "objects"), { recursive: true });
  await mkdir(resolve(root, "长篇", "示例书", "正文", "node_modules", "fake-package"), {
    recursive: true,
  });
  await mkdir(resolve(root, "拆文库", "对标样本甲", ".omc", "state"), { recursive: true });
  await writeFile(resolve(root, "拆文库", "对标样本甲", "拆文报告.md"), "# 对标样本甲\n", "utf8");
  await writeFile(resolve(root, "拆文库", "对标样本甲", "章节", "第1章.md"), "第一章", "utf8");
  await writeFile(resolve(root, "长篇", "示例书", "大纲", "总纲.md"), "# 总纲\n", "utf8");
  await writeFile(resolve(root, "长篇", "示例书", "正文", "第001章.md"), "初稿", "utf8");
  await writeFile(resolve(root, "长篇", "示例书", ".git", "config"), "secret", "utf8");
  await writeFile(
    resolve(root, "长篇", "示例书", "正文", "node_modules", "fake-package", "index.js"),
    "x",
    "utf8",
  );
  await writeFile(resolve(root, "拆文库", "对标样本甲", ".omc", "state", "secrets.json"), "{}", "utf8");
  await writeFile(resolve(root, "长篇", "示例书", "封面.png"), "not-an-image", "utf8");
  return root;
}

async function createProjectDiscoveryWorkspace() {
  const root = await mkdtemp(resolve(tmpdir(), "oh-story-dashboard-projects-"));
  temporaryDirectories.push(root);

  await mkdir(resolve(root, "长篇", "标准长篇", "正文"), { recursive: true });
  await mkdir(resolve(root, "短篇", "标准短篇"), { recursive: true });
  await writeFile(resolve(root, "短篇", "标准短篇", "正文.md"), "正文", "utf8");
  await writeFile(resolve(root, "短篇", "标准短篇", "小节大纲.md"), "大纲", "utf8");
  await writeFile(resolve(root, "短篇", "标准短篇", "设定.md"), "设定", "utf8");

  await mkdir(resolve(root, "普通资料"), { recursive: true });
  await writeFile(resolve(root, "普通资料", "正文.md"), "不是短篇工程", "utf8");

  await mkdir(resolve(root, "拆文库", "伪项目"), { recursive: true });
  await writeFile(resolve(root, "拆文库", "伪项目", "正文.md"), "拆文原文", "utf8");
  await writeFile(resolve(root, "拆文库", "伪项目", "设定.md"), "拆文资料", "utf8");

  return root;
}

// 一个目录超过单页 200 项即可验证分页；不用再造 5000 个文件测试全量树预算。
async function createOversizedWorkspace(fileCount = 205) {
  const root = await mkdtemp(resolve(tmpdir(), "oh-story-dashboard-oversized-"));
  temporaryDirectories.push(root);
  const body = resolve(root, "长篇", "巨书", "正文");
  const library = resolve(root, "拆文库", "对标样本甲");
  await mkdir(resolve(root, "长篇", "巨书", "大纲"), { recursive: true });
  await mkdir(body, { recursive: true });
  await mkdir(resolve(library, "章节"), { recursive: true });
  await writeFile(resolve(library, "拆文报告.md"), "# 对标样本甲\n", "utf8");
  for (let start = 0; start < fileCount; start += 200) {
    await Promise.all(
      Array.from({ length: Math.min(200, fileCount - start) }, (_, offset) =>
        writeFile(
          resolve(body, `第${String(start + offset + 1).padStart(5, "0")}章.md`),
          "初稿",
          "utf8",
        ),
      ),
    );
  }
  return root;
}

async function createDeepSearchWorkspace() {
  const root = await mkdtemp(resolve(tmpdir(), "oh-story-dashboard-deep-search-"));
  temporaryDirectories.push(root);
  const deepRoot = resolve(root, "A深项目", "正文");
  const targetRoot = resolve(root, "B目标项目", "正文");
  await mkdir(
    resolve(deepRoot, ...Array.from({ length: 25 }, (_, index) => `第${index + 1}层`)),
    { recursive: true },
  );
  await mkdir(targetRoot, { recursive: true });
  await writeFile(resolve(targetRoot, "第001章.md"), "目标正文", "utf8");
  return root;
}

async function createSearchBudgetWorkspace(fileCount = 5005) {
  const root = await mkdtemp(resolve(tmpdir(), "oh-story-dashboard-search-budget-"));
  temporaryDirectories.push(root);
  const body = resolve(root, "预算项目", "正文");
  await mkdir(body, { recursive: true });
  for (let start = 0; start < fileCount; start += 250) {
    await Promise.all(
      Array.from({ length: Math.min(250, fileCount - start) }, (_, offset) =>
        writeFile(
          resolve(body, `普通文件_${String(start + offset + 1).padStart(5, "0")}.md`),
          "正文",
          "utf8",
        ),
      ),
    );
  }
  return root;
}

async function startServer(root) {
  const server = createDashboardServer({ root });
  await new Promise((accept, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", accept);
  });
  runningServers.push(server);
  const { port } = server.address();
  return `http://127.0.0.1:${port}`;
}

describe("workspace scanning", () => {
  test("recognizes standard long and short projects without treating loose files or libraries as projects", async () => {
    const root = await createProjectDiscoveryWorkspace();
    const workspace = await scanWorkspace(root);

    assert.deepEqual(
      workspace.projects.map((entry) => entry.path),
      ["短篇/标准短篇", "长篇/标准长篇"],
    );
    assert.deepEqual(workspace.libraries.map((entry) => entry.path), ["拆文库/伪项目"]);
    assert.ok(!workspace.projects.some((entry) => entry.path === "普通资料"));
    assert.ok(!workspace.projects.some((entry) => entry.path.startsWith("拆文库/")));
  });

  test("does not use symlinked short-story marker files", async (context) => {
    const root = await createProjectDiscoveryWorkspace();
    const candidate = resolve(root, "短篇", "符号链接标记");
    await mkdir(candidate, { recursive: true });
    await writeFile(resolve(candidate, "设定.md"), "设定", "utf8");
    try {
      await symlink(resolve(root, "短篇", "标准短篇", "正文.md"), resolve(candidate, "正文.md"));
    } catch (error) {
      if (error?.code === "EPERM") {
        context.skip("当前平台不允许创建测试符号链接");
        return;
      }
      throw error;
    }

    const workspace = await scanWorkspace(root);
    assert.ok(!workspace.projects.some((entry) => entry.path === "短篇/符号链接标记"));
  });

  test("uses a stable dot path when the workspace itself is a short-story project", async () => {
    const root = await mkdtemp(resolve(tmpdir(), "oh-story-dashboard-root-project-"));
    temporaryDirectories.push(root);
    await writeFile(resolve(root, "正文.md"), "正文", "utf8");
    await writeFile(resolve(root, "小节大纲.md"), "大纲", "utf8");
    await writeFile(resolve(root, "设定.md"), "设定", "utf8");

    const workspace = await scanWorkspace(root);
    assert.deepEqual(workspace.projects.map((entry) => entry.path), ["."]);
    const page = await listWorkspaceDirectory(root, ".");
    assert.equal(page.path, ".");
    assert.deepEqual(
      page.entries.map((entry) => entry.name),
      ["设定.md", "小节大纲.md", "正文.md"],
    );
  });

  test("discovers roots without recursively serializing every manuscript", async () => {
    const root = await mkdtemp(resolve(tmpdir(), "oh-story-dashboard-fixture-"));
    temporaryDirectories.push(root);
    await createDashboardFixture(root);
    const workspace = await scanWorkspace(root);
    assert.deepEqual(
      workspace.libraries.map((entry) => entry.path),
      ["拆文库/对标样本甲", "拆文库/对标样本乙"],
    );
    assert.deepEqual(
      workspace.projects.map((entry) => entry.path),
      ["长篇/测试长篇项目"],
    );
    assert.equal(workspace.stats.libraries, 2);
    assert.equal(workspace.stats.projects, 1);
    assert.equal(workspace.stats.editableFiles, null);
    assert.equal(workspace.stats.onDemand, true);
    assert.ok(workspace.libraries.every((entry) => entry.loaded === false));
    assert.ok(workspace.projects.every((entry) => entry.children.length === 0));
    // 首屏只返回根节点：夹具里两层深的细纲文件名一旦出现，就说明又递归序列化了整棵树
    assert.doesNotMatch(JSON.stringify(workspace), /细纲_第001章/);
    // 前端要靠这几个字段判断「树是否被截断」，缺一个就会又变成静默漏文件
    assert.equal(workspace.limits.truncated, false);
    assert.equal(workspace.limits.directoryPageSize, 200);
  });

  test("loads only one directory level and keeps infrastructure folders hidden", async () => {
    const root = await createWorkspace();
    const page = await listWorkspaceDirectory(root, "长篇/示例书");
    assert.doesNotMatch(JSON.stringify(page), /\.git/);
    assert.doesNotMatch(JSON.stringify(page), /第001章\.md/);
    assert.deepEqual(
      page.entries.filter((entry) => entry.type === "directory").map((entry) => entry.name),
      ["大纲", "正文"],
    );
    const cover = page.entries.find((entry) => entry.name === "封面.png");
    assert.equal(cover.editable, false);
    assert.equal(page.nextCursor, null);

    const bodyPage = await listWorkspaceDirectory(root, "长篇/示例书/正文");
    assert.deepEqual(bodyPage.entries.map((entry) => entry.name), ["第001章.md"]);
    assert.doesNotMatch(JSON.stringify(bodyPage), /node_modules|fake-package/);

    const libraryPage = await listWorkspaceDirectory(root, "拆文库/对标样本甲");
    assert.deepEqual(
      libraryPage.entries.map((entry) => entry.name),
      ["章节", "拆文报告.md"],
    );
    assert.doesNotMatch(JSON.stringify(libraryPage), /\.omc|secrets\.json/);
  });

  test("paginates a wide directory without dropping or duplicating files", async () => {
    const root = await createOversizedWorkspace();
    const path = "长篇/巨书/正文";
    const first = await listWorkspaceDirectory(root, path);
    const second = await listWorkspaceDirectory(root, path, first.nextCursor);
    assert.equal(first.entries.length, 200);
    assert.equal(first.nextCursor, "200");
    assert.equal(second.entries.length, 5);
    assert.equal(second.nextCursor, null);
    assert.equal(new Set([...first.entries, ...second.entries].map((entry) => entry.path)).size, 205);
  });

  test("searches unloaded descendants on demand and respects the active collection", async () => {
    const root = await createWorkspace();
    const projects = await searchWorkspace(root, "第001章", "projects");
    assert.deepEqual(projects.results.map((entry) => entry.path), [
      "长篇/示例书/正文/第001章.md",
    ]);
    const libraries = await searchWorkspace(root, "第1章", "libraries");
    assert.deepEqual(libraries.results.map((entry) => entry.path), [
      "拆文库/对标样本甲/章节/第1章.md",
    ]);
    assert.equal(projects.truncated, false);
    const pathOnly = await searchWorkspace(root, "示例书", "projects");
    assert.deepEqual(pathOnly.results, []);
  });

  test("continues searching later projects after one subtree exceeds the depth limit", async () => {
    const root = await createDeepSearchWorkspace();
    const result = await searchWorkspace(root, "第001章", "projects");
    assert.deepEqual(result.results.map((entry) => entry.path), [
      "B目标项目/正文/第001章.md",
    ]);
    assert.equal(result.truncated, true);
    assert.deepEqual(result.truncation, {
      byResults: false,
      byNodes: false,
      byDepth: true,
      byReadError: false,
    });
  });

  test("reports result-limit and node-budget truncation independently", async () => {
    const resultRoot = await createOversizedWorkspace(205);
    const byResults = await searchWorkspace(resultRoot, "第", "projects");
    assert.equal(byResults.results.length, 100);
    assert.deepEqual(byResults.truncation, {
      byResults: true,
      byNodes: false,
      byDepth: false,
      byReadError: false,
    });

    const budgetRoot = await createSearchBudgetWorkspace();
    const byNodes = await searchWorkspace(budgetRoot, "不存在的文件名", "projects");
    assert.deepEqual(byNodes.results, []);
    assert.deepEqual(byNodes.truncation, {
      byResults: false,
      byNodes: true,
      byDepth: false,
      byReadError: false,
    });
  });

  test("marks search results incomplete when an unloaded descendant is unreadable", async (context) => {
    if (process.platform === "win32" || process.getuid?.() === 0) {
      context.skip("当前平台或用户无法制造不可读目录");
      return;
    }
    const root = await createWorkspace();
    const restricted = resolve(root, "长篇", "示例书", "正文", "受限卷");
    await mkdir(restricted, { recursive: true });
    await writeFile(resolve(restricted, "目标章.md"), "不可读取的正文", "utf8");
    await chmod(restricted, 0o000);
    try {
      const baseUrl = await startServer(root);
      const response = await fetch(
        `${baseUrl}/api/search?q=${encodeURIComponent("目标章")}&scope=projects`,
      );
      assert.equal(response.status, 200);
      const result = await response.json();
      assert.deepEqual(result.results, []);
      assert.equal(result.truncated, true);
      assert.deepEqual(result.truncation, {
        byResults: false,
        byNodes: false,
        byDepth: false,
        byReadError: true,
      });
      assert.deepEqual(
        result.scanErrors.map(({ path, code }) => ({ path, code })),
        [{ path: "长篇/示例书/正文/受限卷", code: "EACCES" }],
      );
    } finally {
      await chmod(restricted, 0o755);
    }
  });
});

describe("path boundary", () => {
  test("rejects traversal and absolute paths", async () => {
    const root = await createWorkspace();
    await assert.rejects(
      resolveWorkspacePath(root, "../outside.md"),
      (error) => error instanceof DashboardError && error.code === "path_outside_workspace",
    );
    await assert.rejects(
      resolveWorkspacePath(root, "/etc/hosts"),
      (error) => error instanceof DashboardError && error.code === "path_outside_workspace",
    );
    await assert.rejects(
      resolveWorkspaceDirectory(root, "../outside"),
      (error) => error instanceof DashboardError && error.code === "path_outside_workspace",
    );
  });

  test("does not follow file symlinks", async (context) => {
    const root = await createWorkspace();
    const outside = resolve(root, "..", `outside-${Date.now()}.md`);
    await writeFile(outside, "outside", "utf8");
    temporaryDirectories.push(outside);
    try {
      await symlink(outside, resolve(root, "逃逸.md"));
    } catch (error) {
      if (error?.code === "EPERM") {
        context.skip("当前平台不允许创建测试符号链接");
        return;
      }
      throw error;
    }
    await assert.rejects(
      resolveWorkspacePath(root, "逃逸.md", { editableOnly: true }),
      (error) => error instanceof DashboardError && error.code === "symlink_not_editable",
    );
  });
});

describe("CLI portability", () => {
  test("uses each operating system's default-browser command", () => {
    const url = "http://127.0.0.1:43110";
    assert.deepEqual(browserLaunchCommand(url, "darwin"), {
      command: "open",
      args: [url],
    });
    assert.deepEqual(browserLaunchCommand(url, "linux"), {
      command: "xdg-open",
      args: [url],
    });
    assert.deepEqual(browserLaunchCommand(url, "win32"), {
      command: "cmd",
      args: ["/c", "start", "", url],
    });
  });

  test("recognizes the CLI entrypoint through a symlinked install path", async (context) => {
    const root = await createWorkspace();
    const alias = `${root}-alias`;
    temporaryDirectories.push(alias);
    try {
      await symlink(root, alias, process.platform === "win32" ? "junction" : "dir");
    } catch (error) {
      if (error?.code === "EPERM") {
        context.skip("当前平台不允许创建测试目录链接");
        return;
      }
      throw error;
    }

    assert.equal(
      pathsReferToSameFile(
        resolve(root, "长篇", "示例书", "正文", "第001章.md"),
        resolve(alias, "长篇", "示例书", "正文", "第001章.md"),
      ),
      true,
    );
  });
});

describe("HTTP API", () => {
  test("serves lazy roots, directory pages, and on-demand search", async () => {
    const root = await createWorkspace();
    const baseUrl = await startServer(root);

    const workspace = await fetch(`${baseUrl}/api/workspace`).then((response) => response.json());
    assert.deepEqual(workspace.projects[0].children, []);
    assert.doesNotMatch(JSON.stringify(workspace), /第001章\.md/);

    const tree = await fetch(
      `${baseUrl}/api/tree?path=${encodeURIComponent("长篇/示例书")}`,
    ).then((response) => response.json());
    assert.deepEqual(
      tree.entries.filter((entry) => entry.type === "directory").map((entry) => entry.name),
      ["大纲", "正文"],
    );

    const search = await fetch(
      `${baseUrl}/api/search?q=${encodeURIComponent("第001章")}&scope=projects`,
    ).then((response) => response.json());
    assert.deepEqual(search.results.map((entry) => entry.path), [
      "长篇/示例书/正文/第001章.md",
    ]);

    const traversal = await fetch(
      `${baseUrl}/api/tree?path=${encodeURIComponent("../outside")}`,
    );
    assert.equal(traversal.status, 403);
    const invalidCursor = await fetch(
      `${baseUrl}/api/tree?path=${encodeURIComponent("长篇/示例书")}&cursor=next`,
    );
    assert.equal(invalidCursor.status, 400);
    const hiddenDirectory = await fetch(
      `${baseUrl}/api/tree?path=${encodeURIComponent("长篇/示例书/.git")}`,
    );
    assert.equal(hiddenDirectory.status, 403);
  });

  test("loads and atomically saves an editable file", async () => {
    const root = await createWorkspace();
    const baseUrl = await startServer(root);
    const filePath = "长篇/示例书/正文/第001章.md";

    const loadedResponse = await fetch(
      `${baseUrl}/api/file?path=${encodeURIComponent(filePath)}`,
    );
    assert.equal(loadedResponse.status, 200);
    assert.match(loadedResponse.headers.get("content-security-policy"), /default-src 'self'/);
    const loaded = await loadedResponse.json();
    assert.equal(loaded.content, "初稿");

    const savedResponse = await fetch(`${baseUrl}/api/file`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: filePath,
        content: "修改后的正文",
        expectedVersion: loaded.version,
      }),
    });
    assert.equal(savedResponse.status, 200);
    const saved = await savedResponse.json();
    assert.equal(saved.ok, true);
    assert.equal(await readFile(resolve(root, filePath), "utf8"), "修改后的正文");
  });

  test("returns 409 instead of overwriting an externally changed file", async () => {
    const root = await createWorkspace();
    const baseUrl = await startServer(root);
    const filePath = "长篇/示例书/正文/第001章.md";
    const loaded = await fetch(
      `${baseUrl}/api/file?path=${encodeURIComponent(filePath)}`,
    ).then((response) => response.json());

    await new Promise((accept) => setTimeout(accept, 20));
    await writeFile(resolve(root, filePath), "外部程序的新内容", "utf8");

    const response = await fetch(`${baseUrl}/api/file`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: filePath,
        content: "Dashboard 里的旧内容",
        expectedVersion: loaded.version,
      }),
    });
    assert.equal(response.status, 409);
    const payload = await response.json();
    assert.equal(payload.error.code, "file_changed");
    assert.equal(await readFile(resolve(root, filePath), "utf8"), "外部程序的新内容");
  });

  test("deletes an unchanged editable file but rejects cross-origin deletion", async () => {
    const root = await createWorkspace();
    const baseUrl = await startServer(root);
    const filePath = "长篇/示例书/正文/第001章.md";
    const loaded = await fetch(
      `${baseUrl}/api/file?path=${encodeURIComponent(filePath)}`,
    ).then((response) => response.json());

    const rejected = await fetch(`${baseUrl}/api/file`, {
      method: "DELETE",
      headers: {
        "Content-Type": "application/json",
        Origin: "https://example.com",
      },
      body: JSON.stringify({
        path: filePath,
        expectedVersion: loaded.version,
      }),
    });
    assert.equal(rejected.status, 403);
    assert.equal((await rejected.json()).error.code, "invalid_origin");
    assert.equal(await readFile(resolve(root, filePath), "utf8"), "初稿");

    const deletedResponse = await fetch(`${baseUrl}/api/file`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: filePath,
        expectedVersion: loaded.version,
      }),
    });
    assert.equal(deletedResponse.status, 200);
    const deleted = await deletedResponse.json();
    assert.deepEqual(deleted, { ok: true, path: filePath });
    await assert.rejects(
      readFile(resolve(root, filePath), "utf8"),
      (error) => error?.code === "ENOENT",
    );
  });

  test("does not delete a file changed after it was opened", async () => {
    const root = await createWorkspace();
    const baseUrl = await startServer(root);
    const filePath = "长篇/示例书/正文/第001章.md";
    const loaded = await fetch(
      `${baseUrl}/api/file?path=${encodeURIComponent(filePath)}`,
    ).then((response) => response.json());

    await new Promise((accept) => setTimeout(accept, 20));
    await writeFile(resolve(root, filePath), "外部程序的新内容", "utf8");

    const response = await fetch(`${baseUrl}/api/file`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: filePath,
        expectedVersion: loaded.version,
      }),
    });
    assert.equal(response.status, 409);
    assert.equal((await response.json()).error.code, "file_changed");
    assert.equal(await readFile(resolve(root, filePath), "utf8"), "外部程序的新内容");
  });

  test("accepts only one of several simultaneous saves based on the same version", async () => {
    const root = await createWorkspace();
    const baseUrl = await startServer(root);
    const filePath = "长篇/示例书/正文/第001章.md";
    const loaded = await fetch(
      `${baseUrl}/api/file?path=${encodeURIComponent(filePath)}`,
    ).then((response) => response.json());

    const responses = await Promise.all(
      Array.from({ length: 8 }, (_, index) =>
        fetch(`${baseUrl}/api/file`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            path: filePath,
            content: `并发写入-${index}`,
            expectedVersion: loaded.version,
          }),
        }),
      ),
    );
    const statuses = responses.map((response) => response.status);
    assert.equal(statuses.filter((status) => status === 200).length, 1, statuses);
    assert.equal(statuses.filter((status) => status === 409).length, 7, statuses);
    assert.match(await readFile(resolve(root, filePath), "utf8"), /^并发写入-[0-7]$/);
  });

  test("serializes simultaneous save and delete operations on the same version", async () => {
    const root = await createWorkspace();
    const baseUrl = await startServer(root);
    const filePath = "长篇/示例书/正文/第001章.md";
    const loaded = await fetch(
      `${baseUrl}/api/file?path=${encodeURIComponent(filePath)}`,
    ).then((response) => response.json());
    const versionedPath = { path: filePath, expectedVersion: loaded.version };

    const [saved, deleted] = await Promise.all([
      fetch(`${baseUrl}/api/file`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...versionedPath, content: "保存胜出时的正文" }),
      }),
      fetch(`${baseUrl}/api/file`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(versionedPath),
      }),
    ]);
    assert.deepEqual([saved.status, deleted.status].sort(), [200, 409]);
    if (saved.status === 200) {
      assert.equal(await readFile(resolve(root, filePath), "utf8"), "保存胜出时的正文");
    } else {
      await assert.rejects(
        readFile(resolve(root, filePath), "utf8"),
        (error) => error?.code === "ENOENT",
      );
    }
  });

  test("rejects unsupported files, traversal, and malformed JSON", async () => {
    const root = await createWorkspace();
    const baseUrl = await startServer(root);

    const unsupported = await fetch(
      `${baseUrl}/api/file?path=${encodeURIComponent("长篇/示例书/封面.png")}`,
    );
    assert.equal(unsupported.status, 415);

    const traversal = await fetch(
      `${baseUrl}/api/file?path=${encodeURIComponent("../outside.md")}`,
    );
    assert.equal(traversal.status, 403);

    const malformed = await fetch(`${baseUrl}/api/file`, {
      method: "PUT",
      body: "{bad",
    });
    assert.equal(malformed.status, 400);
    assert.equal((await malformed.json()).error.code, "invalid_json");

    const versionless = await fetch(`${baseUrl}/api/file`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: "长篇/示例书/正文/第001章.md",
        content: "不能无版本覆盖",
      }),
    });
    assert.equal(versionless.status, 400);
    assert.equal((await versionless.json()).error.code, "missing_file_version");

    // 删除同样必须带版本号：409 那道比较挡不住它（NaN > 0.5 恒为 false），
    // 少了这条断言，去掉守卫也能一路绿灯把章节删干净。
    const chapterPath = "长篇/示例书/正文/第001章.md";
    const versionlessDelete = await fetch(`${baseUrl}/api/file`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: chapterPath }),
    });
    assert.equal(versionlessDelete.status, 400);
    assert.equal((await versionlessDelete.json()).error.code, "missing_file_version");
    assert.equal(await readFile(resolve(root, chapterPath), "utf8"), "初稿");
  });

  test("keeps the saved file's permission bits instead of letting umask narrow them", async (context) => {
    if (process.platform === "win32") {
      context.skip("Windows 不使用 POSIX 权限位");
      return;
    }
    const root = await createWorkspace();
    const baseUrl = await startServer(root);
    const filePath = "长篇/示例书/正文/第001章.md";
    const absolutePath = resolve(root, filePath);
    await chmod(absolutePath, 0o664);

    const previousUmask = process.umask(0o022);
    try {
      const loaded = await fetch(
        `${baseUrl}/api/file?path=${encodeURIComponent(filePath)}`,
      ).then((response) => response.json());
      const saved = await fetch(`${baseUrl}/api/file`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: filePath,
          content: "改过的正文",
          expectedVersion: loaded.version,
        }),
      });
      assert.equal(saved.status, 200);
      assert.equal((await stat(absolutePath)).mode & 0o777, 0o664);
    } finally {
      process.umask(previousUmask);
    }
  });

  test("still serves the rest of the workspace when one library directory is unreadable", async (context) => {
    if (process.platform === "win32" || process.getuid?.() === 0) {
      context.skip("当前平台或用户无法制造不可读目录");
      return;
    }
    const root = await createWorkspace();
    const baseUrl = await startServer(root);
    const libraryRoot = resolve(root, "拆文库");
    await chmod(libraryRoot, 0o000);
    try {
      const response = await fetch(`${baseUrl}/api/workspace`);
      assert.equal(response.status, 200);
      const payload = await response.json();
      assert.deepEqual(payload.libraries, []);
      assert.equal(payload.limits.truncated, true);
      assert.equal(payload.limits.truncatedByReadError, true);
      assert.deepEqual(
        payload.scanErrors.map(({ path, code }) => ({ path, code })),
        [{ path: "拆文库", code: "EACCES" }],
      );
      assert.deepEqual(
        payload.projects.map((entry) => entry.path),
        ["长篇/示例书"],
      );
    } finally {
      await chmod(libraryRoot, 0o755);
    }
  });

  test("reports an actionable error when the workspace root itself is unreadable", async (context) => {
    if (process.platform === "win32" || process.getuid?.() === 0) {
      context.skip("当前平台或用户无法制造不可读目录");
      return;
    }
    const root = await createWorkspace();
    const baseUrl = await startServer(root);
    await chmod(root, 0o000);
    try {
      const response = await fetch(`${baseUrl}/api/workspace`);
      assert.equal(response.status, 403);
      const payload = await response.json();
      assert.equal(payload.error.code, "workspace_unreadable");
      assert.match(payload.error.message, /工作区目录无法读取/);
    } finally {
      await chmod(root, 0o755);
    }
  });
});

const STATUS_PROJECT = "长篇/状态书";

function statusTrackingState(overrides = {}) {
  const card = (openThreads = []) => ({
    identity: "测试",
    location: "某地",
    goal: "测试",
    state: "平稳",
    abilities_resources: [],
    relationships: [],
    knowledge: [],
    open_threads: openThreads,
  });
  const row = (id, planned, status = "已埋", importance = "中") => ({
    id,
    summary: `${id} 的伏笔`,
    planted_chapter: 1,
    planned_resolution_chapter: planned,
    status,
    importance,
    updated_chapter: 1,
  });
  return {
    schema_version: 5,
    book_title: "状态书",
    last_committed_chapter: 50,
    imported_through_chapter: 0,
    state_revision: 7,
    context: {
      position: { volume: "第一卷", volume_start_chapter: 1, story_time: "某日", scene: "某地" },
      long_term_constraints: [],
      active_character_names: ["甲", "乙"],
      continuity_risks: [],
      recent_chapters: [],
      next_chapter_commitments: [],
    },
    characters: { 甲: card(), 乙: card(["乙的线"]), 丙: card(["丙一", "丙二"]), 丁: card(["丁一"]), 戊: card(["戊一"]) },
    // 下一章 N = 51：临近窗口是 51..53，逾期按「最久未回收」排前
    foreshadow: {
      F011: row("F011", 50, "已埋", "中"),
      F010: row("F010", 45, "已埋", "高"),
      F012: row("F012", 51, "已埋", "低"),
      F013: row("F013", 53),
      F014: row("F014", 54),
      F015: row("F015", 40, "已回收"),
      F016: row("F016", null),
    },
    timeline: {},
    facts: {},
    appearances: {
      甲: { seen: [30, 35], snapshot_chapter: 35 },
      乙: { seen: [36], snapshot_chapter: 36 },
      丙: { seen: [12, 20], snapshot_chapter: 20 },
      丁: { seen: [21], snapshot_chapter: 21 },
    },
    ...overrides,
  };
}

async function createStatusWorkspace({ state = statusTrackingState(), files = {} } = {}) {
  const root = await mkdtemp(resolve(tmpdir(), "oh-story-dashboard-status-"));
  temporaryDirectories.push(root);
  const project = resolve(root, ...STATUS_PROJECT.split("/"));
  await mkdir(resolve(project, "追踪"), { recursive: true });
  await writeFile(
    resolve(project, "追踪", "_tracking-state.json"),
    typeof state === "string" ? state : JSON.stringify(state),
    "utf8",
  );
  for (const [relativePath, content] of Object.entries(files)) {
    const target = resolve(project, relativePath);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, typeof content === "string" ? content : JSON.stringify(content), "utf8");
  }
  return { root, project };
}

describe("project status", () => {
  test("reports every field for the modern long-form fixture over HTTP", async () => {
    const root = await mkdtemp(resolve(tmpdir(), "oh-story-dashboard-status-fixture-"));
    temporaryDirectories.push(root);
    await createDashboardFixture(root);
    const baseUrl = await startServer(root);

    const workspace = await fetch(`${baseUrl}/api/workspace`).then((response) => response.json());
    assert.deepEqual(
      workspace.projects.map(({ path, projectKind, hasTrackingState }) => ({ path, projectKind, hasTrackingState })),
      [{ path: "长篇/测试长篇项目", projectKind: "long", hasTrackingState: true }],
    );

    const response = await fetch(
      `${baseUrl}/api/project-status?path=${encodeURIComponent("长篇/测试长篇项目")}`,
    );
    assert.equal(response.status, 200);
    assert.match(response.headers.get("content-type"), /application\/json/);
    assert.deepEqual(await response.json(), {
      path: "长篇/测试长篇项目",
      schema_version: 5,
      last_committed_chapter: 40,
      state_revision: 42,
      // 第001章「顾临推开门。」6 字 + 第002章「　　沈砚没有回头。」去掉全角缩进 7 字；标题行不计
      words_written: 13,
      chapter_files: 2,
      words_truncated: false,
      target_words: 300000,
      overdue_foreshadows: [
        {
          id: "F001",
          summary: "旧信封里夹着一把铜钥匙",
          planned_resolution_chapter: 38,
          importance: "高",
          overdue_by: 3,
        },
      ],
      due_soon_foreshadows: [
        {
          id: "F002",
          summary: "档案馆地下室的第二道门",
          planned_resolution_chapter: 42,
          importance: "中",
        },
      ],
      absent_characters: [{ name: "沈砚", last_seen: 20, absent: 20 }],
      dormant_threads: [{ name: "周衡", open_threads: 2, last_seen: 8, absent: 32 }],
      open_candidate: {
        chapter: 41,
        status: "draft",
        run: "长篇/测试长篇项目/追踪/候选章/第041章/C20260926-090000",
        approval_mode: "review",
        gate_status: "pass",
        reviews: { deslop: "PASS", consistency: null },
      },
      quality: { gated: 1, legacy: 1 },
      errors: [],
      limits: { maxChapterFiles: 3000 },
    });
  });

  test("applies the tracking script's due-soon, overdue, absence and dormancy windows", async () => {
    const { root } = await createStatusWorkspace();
    const status = await readProjectStatus(root, STATUS_PROJECT);
    assert.equal(status.last_committed_chapter, 50);
    assert.equal(status.state_revision, 7);
    assert.deepEqual(
      status.overdue_foreshadows.map(({ id, overdue_by }) => ({ id, overdue_by })),
      [
        { id: "F010", overdue_by: 6 },
        { id: "F011", overdue_by: 1 },
      ],
    );
    assert.deepEqual(status.due_soon_foreshadows.map((item) => item.id), ["F012", "F013"]);
    assert.ok(status.due_soon_foreshadows.every((item) => !("overdue_by" in item)));
    // 甲 50-35=15 章正好到久别线；乙 14 章还不算
    assert.deepEqual(status.absent_characters, [{ name: "甲", last_seen: 35, absent: 15 }]);
    // 丙 30 章正好到搁置线；丁 29 章不算；戊没有出场记录无法判断；乙在活跃名单里不算搁置
    assert.deepEqual(status.dormant_threads, [{ name: "丙", open_threads: 2, last_seen: 20, absent: 30 }]);
    // 没有 正文/、大纲/、候选章/、章节提交/ 的新项目：数量是已知的 0，而不是未知
    assert.equal(status.words_written, 0);
    assert.equal(status.chapter_files, 0);
    assert.equal(status.target_words, null);
    assert.equal(status.open_candidate, null);
    assert.deepEqual(status.quality, { gated: 0, legacy: 0 });
    assert.deepEqual(status.errors, []);
  });

  test("treats a state without appearances as an older project, not an error", async () => {
    const state = statusTrackingState();
    delete state.appearances;
    const { root } = await createStatusWorkspace({ state });
    const status = await readProjectStatus(root, STATUS_PROJECT);
    assert.deepEqual(status.absent_characters, []);
    assert.deepEqual(status.dormant_threads, []);
    assert.equal(status.overdue_foreshadows.length, 2);
    assert.deepEqual(status.errors, []);
  });

  test("keeps the other blocks when the tracking state JSON is malformed", async () => {
    const { root } = await createStatusWorkspace({
      state: "{\"schema_version\": 5, \"last_committed_chapter\": ",
      files: {
        "正文/第001章.md": "# 第一章\n\n正文五个字",
        "大纲/大纲.md": "- 目标字数：10 万字\n",
        "追踪/章节提交/第001章.json": { protocol: "gated-v2" },
      },
    });
    const baseUrl = await startServer(root);
    const response = await fetch(
      `${baseUrl}/api/project-status?path=${encodeURIComponent(STATUS_PROJECT)}`,
    );
    assert.equal(response.status, 200);
    const status = await response.json();
    for (const field of [
      "schema_version",
      "last_committed_chapter",
      "state_revision",
      "overdue_foreshadows",
      "due_soon_foreshadows",
      "absent_characters",
      "dormant_threads",
    ]) {
      assert.equal(status[field], null, field);
    }
    assert.equal(status.words_written, 5);
    assert.equal(status.target_words, 100000);
    assert.deepEqual(status.quality, { gated: 1, legacy: 0 });
    assert.equal(status.errors.length, 1);
    assert.match(status.errors[0], /^追踪\/_tracking-state\.json：JSON 格式错误/);

    // 结构不对（顶层是数组、字段类型错）同样只让受影响的块置空，不 500
    const arrayState = await createStatusWorkspace({ state: "[]" });
    const arrayStatus = await readProjectStatus(arrayState.root, STATUS_PROJECT);
    assert.equal(arrayStatus.last_committed_chapter, null);
    assert.deepEqual(arrayStatus.errors, ["追踪/_tracking-state.json：顶层不是 JSON 对象"]);

    const badForeshadow = await createStatusWorkspace({
      state: statusTrackingState({ foreshadow: "坏掉的伏笔表" }),
    });
    const partial = await readProjectStatus(badForeshadow.root, STATUS_PROJECT);
    assert.equal(partial.last_committed_chapter, 50);
    assert.equal(partial.overdue_foreshadows, null);
    assert.equal(partial.due_soon_foreshadows, null);
    assert.deepEqual(partial.absent_characters, [{ name: "甲", last_seen: 35, absent: 15 }]);
    assert.deepEqual(partial.errors, ["追踪/_tracking-state.json：foreshadow 不是对象"]);
  });

  test("picks the first open candidate in script order and counts commit protocols", async () => {
    const { root } = await createStatusWorkspace({
      files: {
        "追踪/候选章/第009章/Z/manifest.json": { status: "committed", chapter: 9 },
        "追踪/候选章/第010章/A/candidate.md": "manifest 还没写好的运行目录要跳过",
        "追踪/候选章/第010章/B/manifest.json": {
          status: "approved",
          chapter: 10,
          approval_mode: "auto",
          gate_run: { status: "pass" },
          reviews: { deslop: { verdict: "PASS" }, consistency: { verdict: "CONCERNS" } },
        },
        "追踪/候选章/第011章/A/manifest.json": { status: "draft", chapter: 11 },
        "追踪/章节提交/第001章.json": { protocol: "gated-v2" },
        "追踪/章节提交/第002章.json": { protocol: "gated-v2" },
        "追踪/章节提交/第003章.json": { protocol: "legacy-v1" },
        "追踪/章节提交/第004章.json": { status: "committed" },
        "追踪/章节提交/说明.md": "不是提交凭证",
      },
    });
    const status = await readProjectStatus(root, STATUS_PROJECT);
    assert.deepEqual(status.open_candidate, {
      chapter: 10,
      status: "approved",
      run: `${STATUS_PROJECT}/追踪/候选章/第010章/B`,
      approval_mode: "auto",
      gate_status: "pass",
      reviews: { deslop: "PASS", consistency: "CONCERNS" },
    });
    assert.deepEqual(status.quality, { gated: 2, legacy: 2 });
    assert.deepEqual(status.errors, []);
  });

  test("nulls only the candidate or quality block whose JSON is malformed", async () => {
    const { root } = await createStatusWorkspace({
      files: {
        "追踪/候选章/第003章/A/manifest.json": "{坏",
        "追踪/候选章/第004章/A/manifest.json": { status: "draft", chapter: 4 },
        "追踪/章节提交/第001章.json": { protocol: "gated-v2" },
        "追踪/章节提交/第002章.json": "not json",
      },
    });
    const status = await readProjectStatus(root, STATUS_PROJECT);
    // 排在前面的 manifest 读不动，就无法断定第 4 章是不是「第一个」未闭环候选
    assert.equal(status.open_candidate, null);
    assert.equal(status.quality, null);
    assert.equal(status.last_committed_chapter, 50);
    assert.equal(status.errors.length, 2);
    assert.match(status.errors[0], /^追踪\/候选章\/第003章\/A\/manifest\.json：JSON 格式错误/);
    assert.match(status.errors[1], /^追踪\/章节提交\/第002章\.json：JSON 格式错误/);
  });

  test("counts Chinese manuscript characters without headings or whitespace", () => {
    assert.equal(
      countManuscriptCharacters("# 第一章\n\n　　他说：“走。”\r\nHello world\n## 小节\n#不是标题\n   ### 缩进标题\n"),
      // 他说：“走。” 7 + Helloworld 10 + #不是标题 5
      22,
    );
    assert.equal(countManuscriptCharacters("﻿# 标题\n正文😀"), 3);
    assert.equal(countManuscriptCharacters(""), 0);
  });

  test("parses the outline target in 万字 and prefers 目标字数 over 预计字数", () => {
    assert.equal(parseTargetWords("- 预计字数：80 万字\n\n- 目标字数：150.5 万字\n"), 1505000);
    assert.equal(parseTargetWords("- 预计字数: 80万字"), 800000);
    assert.equal(parseTargetWords("* **目标字数**：120 万字"), 1200000);
    assert.equal(parseTargetWords("- 目标字数：1.1 万字"), 11000);
    assert.equal(parseTargetWords("- 目标字数：{X} 万字\n- 预计字数：{X} 万字"), null);
    assert.equal(parseTargetWords("- 目标字数：0 万字"), null);
    assert.equal(parseTargetWords("正文里提到目标字数：100 万字"), null);
    assert.equal(parseTargetWords(""), null);
  });

  test("sums only top-level 正文/第*章*.md files and reads the outline target", async (context) => {
    const { root, project } = await createStatusWorkspace({
      files: {
        "正文/第001章.md": "# 第001章 开端\n\n一二三。\n",
        "正文/第002章_重逢.md": "## 第二章\n\n四五\n六\n",
        "正文/第003章.txt": "不是 Markdown 章节",
        "正文/笔记.md": "不是章节",
        "正文/第一卷/第004章.md": "只统计 正文/ 顶层章节",
        "大纲/大纲.md": "# 大纲\n\n- 预计字数：50 万字\n- 目标字数：80 万字\n",
      },
    });
    // 符号链接章节与文件树同规则：不跟随、不计字数
    try {
      await writeFile(resolve(root, "外部.md"), "链接目标不应计入", "utf8");
      await symlink(resolve(root, "外部.md"), resolve(project, "正文", "第005章.md"));
    } catch (error) {
      if (error?.code !== "EPERM") throw error;
      context.diagnostic("当前平台不允许创建测试符号链接，链接章节这一项未覆盖");
    }
    const status = await readProjectStatus(root, STATUS_PROJECT);
    // 第001章「一二三。」4 字 + 第002章_重逢「四五」「六」3 字；标题行、.txt、笔记、子卷、链接都不计
    assert.equal(status.words_written, 7);
    assert.equal(status.chapter_files, 2);
    assert.equal(status.words_truncated, false);
    assert.equal(status.target_words, 800000);
    assert.deepEqual(status.errors, []);
  });

  test("caps manuscript counting at 3000 chapter files", async () => {
    const { root, project } = await createStatusWorkspace();
    const body = resolve(project, "正文");
    await mkdir(body, { recursive: true });
    for (let start = 0; start < 3001; start += 250) {
      await Promise.all(
        Array.from({ length: Math.min(250, 3001 - start) }, (_, offset) =>
          writeFile(resolve(body, `第${String(start + offset + 1).padStart(4, "0")}章.md`), "字", "utf8"),
        ),
      );
    }
    const status = await readProjectStatus(root, STATUS_PROJECT);
    assert.equal(status.chapter_files, 3000);
    assert.equal(status.words_written, 3000);
    assert.equal(status.words_truncated, true);
  });

  test("rejects traversal, absolute and hidden paths like the other directory routes", async () => {
    const { root } = await createStatusWorkspace();
    const baseUrl = await startServer(root);
    const request = (path) =>
      fetch(`${baseUrl}/api/project-status?path=${encodeURIComponent(path)}`).then(async (response) => ({
        status: response.status,
        code: (await response.json()).error?.code,
      }));
    assert.deepEqual(await request("../outside"), { status: 403, code: "path_outside_workspace" });
    assert.deepEqual(await request(`${STATUS_PROJECT}/../../..`), { status: 403, code: "path_outside_workspace" });
    assert.deepEqual(await request("/etc"), { status: 403, code: "path_outside_workspace" });
    assert.deepEqual(await request(`${STATUS_PROJECT}/.git`), { status: 403, code: "directory_hidden" });
    assert.deepEqual(await request(""), { status: 400, code: "invalid_path" });
  });

  test("returns 404 JSON for directories that are not long-form projects", async () => {
    const root = await createWorkspace();
    await mkdir(resolve(root, "长篇", "半成品", "追踪"), { recursive: true });
    await writeFile(resolve(root, "长篇", "半成品", "追踪", "伏笔.md"), "# 旧版平铺追踪\n", "utf8");
    const baseUrl = await startServer(root);
    const request = (path) =>
      fetch(`${baseUrl}/api/project-status?path=${encodeURIComponent(path)}`).then(async (response) => ({
        status: response.status,
        type: response.headers.get("content-type"),
        body: await response.json(),
      }));

    const legacy = await request("长篇/示例书");
    assert.equal(legacy.status, 404);
    assert.match(legacy.type, /application\/json/);
    assert.equal(legacy.body.error.code, "project_not_found");
    assert.match(legacy.body.error.message, /追踪\/_tracking-state\.json/);
    assert.equal((await request("长篇/半成品")).body.error.code, "project_not_found");
    assert.equal((await request("长篇/不存在")).body.error.code, "directory_not_found");
    const file = await request("长篇/示例书/封面.png");
    assert.deepEqual([file.status, file.body.error.code], [400, "not_a_directory"]);

    const workspace = await scanWorkspace(root);
    assert.deepEqual(
      workspace.projects.map(({ path, hasTrackingState }) => ({ path, hasTrackingState })),
      [
        { path: "长篇/半成品", hasTrackingState: false },
        { path: "长篇/示例书", hasTrackingState: false },
      ],
    );
  });

  test("does not follow a symlinked tracking state", async (context) => {
    const { root, project } = await createStatusWorkspace();
    const stateFile = resolve(project, "追踪", "_tracking-state.json");
    const elsewhere = resolve(root, "别处状态.json");
    await writeFile(elsewhere, await readFile(stateFile, "utf8"), "utf8");
    await rm(stateFile);
    try {
      await symlink(elsewhere, stateFile);
    } catch (error) {
      if (error?.code === "EPERM") {
        context.skip("当前平台不允许创建测试符号链接");
        return;
      }
      throw error;
    }
    await assert.rejects(
      readProjectStatus(root, STATUS_PROJECT),
      (error) => error instanceof DashboardError && error.status === 403 && error.code === "symlink_not_readable",
    );
    const workspace = await scanWorkspace(root);
    assert.equal(workspace.projects.find((entry) => entry.path === STATUS_PROJECT).hasTrackingState, false);
  });
});
