#!/usr/bin/env node

import { spawn } from "node:child_process";
import { realpathSync } from "node:fs";
import { createServer } from "node:http";
import {
  chmod,
  copyFile,
  lstat,
  readFile,
  readdir,
  realpath,
  rename,
  stat,
  unlink,
  writeFile,
} from "node:fs/promises";
import { extname, basename, dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createHash, randomUUID } from "node:crypto";

const MODULE_PATH = fileURLToPath(import.meta.url);
const ASSET_DIR = fileURLToPath(new URL("../assets/", import.meta.url));
const EDITABLE_EXTENSIONS = new Set([".md", ".txt", ".json", ".yaml", ".yml", ".toml"]);
const LONG_PROJECT_DIRECTORY_MARKERS = new Set(["正文", "大纲", "设定", "追踪"]);
const SHORT_PROJECT_BODY_FILE = "正文.md";
const SHORT_PROJECT_COMPANION_FILES = new Set(["小节大纲.md", "设定.md"]);
const IGNORED_DIRECTORIES = new Set([
  ".git",
  ".omc",
  ".omx",
  ".claude",
  ".codex",
  ".opencode",
  ".zcode",
  ".agents",
  "node_modules",
  "test-results",
  "playwright-report",
  "__pycache__",
]);
const MAX_FILE_BYTES = 2 * 1024 * 1024;
const MAX_REQUEST_BYTES = MAX_FILE_BYTES + 64 * 1024;
const DIRECTORY_PAGE_SIZE = 200;
const MAX_SEARCH_RESULTS = 100;
const MAX_SEARCH_NODES = 5000;
const MAX_SEARCH_DEPTH = 20;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);
const FILE_MUTATION_TAILS = new Map();

// 长篇项目状态卡（只读）。口径与 story-long-write/scripts/tracking_commit.py、
// chapter_candidate.py 保持一致：下一章 N = last_committed_chapter + 1，
// 「临近」= N ≤ 计划回收章 ≤ N+2，久别 ≥15 章，搁置线程 ≥30 章。
const TRACKING_DIRECTORY = "追踪";
const TRACKING_STATE_FILE = "_tracking-state.json";
const TRACKING_STATE_LABEL = `${TRACKING_DIRECTORY}/${TRACKING_STATE_FILE}`;
const MAX_TRACKING_STATE_BYTES = 16 * 1024 * 1024;
const MAX_STATUS_CHAPTER_FILES = 3000;
const MAX_STATUS_RECORD_FILES = 10000;
const STATUS_READ_CONCURRENCY = 16;
const CHAPTER_FILE_PATTERN = /^第.*章.*\.md$/u;
const CANDIDATE_CHAPTER_DIRECTORY_PATTERN = /^第.*章$/u;
const COMMIT_RECEIPT_PATTERN = /^第.*章\.json$/u;
const OPEN_CANDIDATE_STATUSES = new Set(["draft", "approved", "promoted"]);
const GATED_PROTOCOL = "gated-v2";
const FORESHADOW_DUE_SOON_WINDOW = 2;
const LONG_ABSENCE_CHAPTERS = 15;
const DORMANT_THREAD_CHAPTERS = 30;
const TARGET_WORDS_LABELS = ["目标字数", "预计字数"];
const TARGET_WORDS_LINE =
  /^\s*[-*+]\s*(?:\*\*)?(目标字数|预计字数)(?:\*\*)?\s*[：:]\s*(?:约\s*)?(\d+(?:\.\d+)?)\s*万\s*字/u;
const MARKDOWN_HEADING_LINE = /^ {0,3}#{1,6}(?:[ \t]|$)/u;

const CONTENT_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml; charset=utf-8",
};

export class DashboardError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "DashboardError";
    this.status = status;
    this.code = code;
  }
}

function isPathInside(candidate, root) {
  const relation = relative(root, candidate);
  return relation === "" || (!relation.startsWith(`..${sep}`) && relation !== ".." && !isAbsolute(relation));
}

function toPosixPath(value) {
  return value.split(sep).join("/");
}

function isEditableFile(name) {
  return EDITABLE_EXTENSIONS.has(extname(name).toLowerCase());
}

function fileVersion(content) {
  return createHash("sha256").update(content, "utf8").digest("hex");
}

async function withSerializedFileMutation(absolutePath, operation) {
  const previous = FILE_MUTATION_TAILS.get(absolutePath) || Promise.resolve();
  let release;
  const gate = new Promise((accept) => {
    release = accept;
  });
  const tail = previous.catch(() => {}).then(() => gate);
  FILE_MUTATION_TAILS.set(absolutePath, tail);
  await previous.catch(() => {});
  try {
    return await operation();
  } finally {
    release();
    if (FILE_MUTATION_TAILS.get(absolutePath) === tail) {
      FILE_MUTATION_TAILS.delete(absolutePath);
    }
  }
}

function recordScanError(scanErrors, root, absolutePath, error) {
  const errorPath = toPosixPath(relative(root, absolutePath)) || ".";
  if (scanErrors.some((entry) => entry.path === errorPath)) {
    return;
  }
  scanErrors.push({
    path: errorPath,
    code: typeof error?.code === "string" ? error.code : "READ_ERROR",
    message: `目录无法读取，请检查访问权限或挂载状态：${errorPath}`,
  });
}

function shouldIgnoreDirectory(name) {
  return IGNORED_DIRECTORIES.has(name) || (name.startsWith(".") && name !== ".story");
}

function compareTreeEntries(left, right) {
  if (left.type !== right.type) {
    return left.type === "directory" ? -1 : 1;
  }
  return left.name.localeCompare(right.name, "zh-CN", { numeric: true, sensitivity: "base" });
}

async function existingRealRoot(root) {
  const absolute = resolve(root);
  const info = await stat(absolute).catch(() => null);
  if (!info?.isDirectory()) {
    throw new DashboardError(400, "invalid_workspace", `工作区不存在或不是目录：${absolute}`);
  }
  return realpath(absolute);
}

export async function resolveWorkspacePath(root, requestedPath, options = {}) {
  const { editableOnly = false } = options;
  if (typeof requestedPath !== "string" || requestedPath.length === 0) {
    throw new DashboardError(400, "invalid_path", "文件路径不能为空");
  }
  if (
    requestedPath.includes("\0") ||
    isAbsolute(requestedPath) ||
    /^[A-Za-z]:[\\/]/.test(requestedPath)
  ) {
    throw new DashboardError(403, "path_outside_workspace", "只允许访问工作区内的相对路径");
  }

  const realRoot = await existingRealRoot(root);
  const candidate = resolve(realRoot, requestedPath);
  if (!isPathInside(candidate, realRoot)) {
    throw new DashboardError(403, "path_outside_workspace", "路径超出工作区");
  }

  const info = await lstat(candidate).catch((error) => {
    if (error?.code === "ENOENT") {
      throw new DashboardError(404, "file_not_found", "文件不存在");
    }
    throw error;
  });
  if (info.isSymbolicLink()) {
    throw new DashboardError(403, "symlink_not_editable", "Dashboard 不读写符号链接文件");
  }
  if (!info.isFile()) {
    throw new DashboardError(400, "not_a_file", "目标不是普通文件");
  }

  const resolvedFile = await realpath(candidate);
  if (!isPathInside(resolvedFile, realRoot)) {
    throw new DashboardError(403, "path_outside_workspace", "符号链接指向工作区外部");
  }
  if (editableOnly && !isEditableFile(candidate)) {
    throw new DashboardError(415, "unsupported_file_type", "该文件类型不支持在线编辑");
  }

  return { absolutePath: candidate, realRoot, info };
}

function directoryNode(absolutePath, relativePath) {
  return {
    name: basename(absolutePath),
    path: relativePath ? toPosixPath(relativePath) : ".",
    type: "directory",
    children: [],
    loaded: false,
  };
}

function assertRelativeWorkspacePath(requestedPath, label = "路径") {
  if (typeof requestedPath !== "string" || requestedPath.length === 0) {
    throw new DashboardError(400, "invalid_path", `${label}不能为空`);
  }
  if (
    requestedPath.includes("\0") ||
    isAbsolute(requestedPath) ||
    /^[A-Za-z]:[\\/]/.test(requestedPath)
  ) {
    throw new DashboardError(403, "path_outside_workspace", "只允许访问工作区内的相对路径");
  }
}

export async function resolveWorkspaceDirectory(root, requestedPath) {
  assertRelativeWorkspacePath(requestedPath, "目录路径");
  const realRoot = await existingRealRoot(root);
  const candidate = resolve(realRoot, requestedPath);
  if (!isPathInside(candidate, realRoot)) {
    throw new DashboardError(403, "path_outside_workspace", "路径超出工作区");
  }
  if (
    requestedPath !== "." &&
    requestedPath.split(/[\\/]+/).some((segment) => shouldIgnoreDirectory(segment))
  ) {
    throw new DashboardError(403, "directory_hidden", "该目录不会显示在 Dashboard 中");
  }

  const info = await lstat(candidate).catch((error) => {
    if (error?.code === "ENOENT") {
      throw new DashboardError(404, "directory_not_found", "目录不存在");
    }
    throw error;
  });
  if (info.isSymbolicLink()) {
    throw new DashboardError(403, "symlink_not_readable", "Dashboard 不读取符号链接目录");
  }
  if (!info.isDirectory()) {
    throw new DashboardError(400, "not_a_directory", "目标不是目录");
  }

  const resolvedDirectory = await realpath(candidate);
  if (!isPathInside(resolvedDirectory, realRoot)) {
    throw new DashboardError(403, "path_outside_workspace", "符号链接指向工作区外部");
  }
  return { absolutePath: candidate, realRoot };
}

function parseDirectoryCursor(value) {
  if (value === null || value === "") return 0;
  if (!/^\d+$/.test(value)) {
    throw new DashboardError(400, "invalid_cursor", "目录游标无效");
  }
  const cursor = Number(value);
  if (!Number.isSafeInteger(cursor)) {
    throw new DashboardError(400, "invalid_cursor", "目录游标无效");
  }
  return cursor;
}

function visibleDirectoryEntries(entries) {
  return entries
    .filter(
      (entry) =>
        !entry.isSymbolicLink() &&
        (!entry.isDirectory() || !shouldIgnoreDirectory(entry.name)),
    )
    .sort((left, right) =>
      compareTreeEntries(
        { name: left.name, type: left.isDirectory() ? "directory" : "file" },
        { name: right.name, type: right.isDirectory() ? "directory" : "file" },
      ),
    );
}

export async function listWorkspaceDirectory(root, requestedPath, cursorValue = null) {
  const { absolutePath, realRoot } = await resolveWorkspaceDirectory(root, requestedPath);
  const cursor = parseDirectoryCursor(cursorValue);
  const entries = await readdir(absolutePath, { withFileTypes: true }).catch(() => {
    throw new DashboardError(
      403,
      "directory_unreadable",
      `目录无法读取，请检查访问权限或挂载状态：${toPosixPath(requestedPath)}`,
    );
  });
  const visibleEntries = visibleDirectoryEntries(entries);
  const page = visibleEntries.slice(cursor, cursor + DIRECTORY_PAGE_SIZE);
  const nodes = (
    await Promise.all(
      page.map(async (entry) => {
        const childAbsolute = resolve(absolutePath, entry.name);
        const childRelative = relative(realRoot, childAbsolute);
        if (entry.isDirectory()) {
          return directoryNode(childAbsolute, childRelative);
        }
        const info = await lstat(childAbsolute).catch(() => null);
        if (!info?.isFile() || info.isSymbolicLink()) return null;
        return {
          name: entry.name,
          path: toPosixPath(childRelative),
          type: "file",
          editable: isEditableFile(childAbsolute) && info.size <= MAX_FILE_BYTES,
          size: info.size,
        };
      }),
    )
  ).filter(Boolean);
  const nextOffset = cursor + page.length;
  return {
    path: toPosixPath(relative(realRoot, absolutePath)) || ".",
    entries: nodes,
    nextCursor: nextOffset < visibleEntries.length ? String(nextOffset) : null,
  };
}

async function listLibraryRoots(root, scanErrors) {
  const roots = [];
  const standardRoot = resolve(root, "拆文库");
  const standardInfo = await lstat(standardRoot).catch(() => null);
  if (standardInfo?.isDirectory() && !standardInfo.isSymbolicLink()) {
    // 单个拆文库读不动时保留其他项目，但把残缺扫描显式带回前端；空数组只能表达“确实为空”，
    // 不能再同时承担权限错误/外挂盘掉线，否则作者会把不可见文稿误当成不存在。
    const entries = await readdir(standardRoot, { withFileTypes: true }).catch((error) => {
      recordScanError(scanErrors, root, standardRoot, error);
      return [];
    });
    for (const entry of entries) {
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        roots.push({ absolutePath: resolve(standardRoot, entry.name), relativePath: `拆文库${sep}${entry.name}` });
      }
    }
  }

  // 工作区根目录读不动就没有任何可展示的树，直接给出可执行的报错，而不是静默返回空树。
  const rootEntries = await readdir(root, { withFileTypes: true }).catch(() => {
    throw new DashboardError(
      403,
      "workspace_unreadable",
      `工作区目录无法读取，请检查访问权限：${root}`,
    );
  });
  for (const entry of rootEntries) {
    if (
      entry.name.startsWith("拆文库-") &&
      entry.isDirectory() &&
      !entry.isSymbolicLink()
    ) {
      roots.push({ absolutePath: resolve(root, entry.name), relativePath: entry.name });
    }
  }

  return roots.sort((left, right) =>
    left.relativePath.localeCompare(right.relativePath, "zh-CN", { numeric: true }),
  );
}

function isUnderAnyPath(candidate, blockedPaths) {
  return blockedPaths.some((blocked) => isPathInside(candidate, blocked));
}

async function findProjectRoots(
  root,
  libraryPaths,
  scanErrors,
  currentPath = root,
  depth = 0,
  projects = [],
) {
  if (depth > 3 || isUnderAnyPath(currentPath, libraryPaths)) {
    return projects;
  }

  const entries = await readdir(currentPath, { withFileTypes: true }).catch((error) => {
    recordScanError(scanErrors, root, currentPath, error);
    return [];
  });
  const childDirectoryNames = new Set(
    entries.filter((entry) => entry.isDirectory() && !entry.isSymbolicLink()).map((entry) => entry.name),
  );
  const childFileNames = new Set(
    entries.filter((entry) => entry.isFile() && !entry.isSymbolicLink()).map((entry) => entry.name),
  );
  const isLongProject = [...LONG_PROJECT_DIRECTORY_MARKERS].some((marker) =>
    childDirectoryNames.has(marker),
  );
  const isShortProject =
    childFileNames.has(SHORT_PROJECT_BODY_FILE) &&
    [...SHORT_PROJECT_COMPANION_FILES].some((marker) => childFileNames.has(marker));
  if (isLongProject || isShortProject) {
    projects.push({
      absolutePath: currentPath,
      relativePath: relative(root, currentPath),
      kind: isLongProject ? "long" : "short",
      // 前端只给当前事务协议的长篇项目挂状态卡；先在这里判好，避免对每个项目盲打 404。
      hasTrackingState:
        isLongProject &&
        childDirectoryNames.has(TRACKING_DIRECTORY) &&
        (await hasRegularTrackingState(currentPath)),
    });
    return projects;
  }

  for (const entry of entries) {
    if (!entry.isDirectory() || entry.isSymbolicLink() || shouldIgnoreDirectory(entry.name)) {
      continue;
    }
    await findProjectRoots(
      root,
      libraryPaths,
      scanErrors,
      resolve(currentPath, entry.name),
      depth + 1,
      projects,
    );
  }
  return projects;
}

async function discoverWorkspaceRoots(realRoot) {
  const scanErrors = [];
  const libraryRoots = await listLibraryRoots(realRoot, scanErrors);
  const libraryPaths = libraryRoots.map((entry) => entry.absolutePath);
  const projectRoots = await findProjectRoots(realRoot, libraryPaths, scanErrors);
  return { libraryRoots, projectRoots, scanErrors };
}

export async function scanWorkspace(root) {
  const realRoot = await existingRealRoot(root);
  const { libraryRoots, projectRoots, scanErrors } = await discoverWorkspaceRoots(realRoot);
  const libraries = libraryRoots.map((entry) =>
    directoryNode(entry.absolutePath, entry.relativePath),
  );
  const projects = projectRoots.map((entry) => ({
    ...directoryNode(entry.absolutePath, entry.relativePath),
    projectKind: entry.kind,
    hasTrackingState: entry.hasTrackingState,
  }));
  libraries.sort(compareTreeEntries);
  projects.sort(compareTreeEntries);

  return {
    workspace: {
      name: basename(realRoot),
      path: realRoot,
    },
    libraries,
    projects,
    scanErrors,
    stats: {
      libraries: libraries.length,
      projects: projects.length,
      files: null,
      editableFiles: null,
      onDemand: true,
    },
    limits: {
      maxFileBytes: MAX_FILE_BYTES,
      editableExtensions: [...EDITABLE_EXTENSIONS],
      directoryPageSize: DIRECTORY_PAGE_SIZE,
      maxSearchResults: MAX_SEARCH_RESULTS,
      truncated: scanErrors.length > 0,
      truncatedByReadError: scanErrors.length > 0,
    },
  };
}

export async function searchWorkspace(root, queryValue, scopeValue) {
  const query = typeof queryValue === "string" ? queryValue.trim() : "";
  if (!query || query.length > 100) {
    throw new DashboardError(400, "invalid_query", "搜索词长度必须在 1–100 个字符之间");
  }
  if (!["libraries", "projects"].includes(scopeValue)) {
    throw new DashboardError(400, "invalid_scope", "搜索范围必须是拆文库或写作项目");
  }

  const realRoot = await existingRealRoot(root);
  const { libraryRoots, projectRoots, scanErrors } = await discoverWorkspaceRoots(realRoot);
  const roots = scopeValue === "libraries" ? libraryRoots : projectRoots;
  const normalizedQuery = query.toLocaleLowerCase("zh-CN");
  const state = {
    nodes: 0,
    truncatedByResults: false,
    truncatedByNodes: false,
    truncatedByDepth: false,
    results: [],
    scanErrors,
  };

  async function visit(absolutePath, relativePath, depth) {
    if (state.results.length >= MAX_SEARCH_RESULTS) {
      state.truncatedByResults = true;
      return;
    }
    if (state.nodes >= MAX_SEARCH_NODES) {
      state.truncatedByNodes = true;
      return;
    }
    if (depth > MAX_SEARCH_DEPTH) {
      state.truncatedByDepth = true;
      return;
    }
    state.nodes += 1;

    const info = await lstat(absolutePath).catch((error) => {
      recordScanError(state.scanErrors, realRoot, absolutePath, error);
      return null;
    });
    if (!info || info.isSymbolicLink()) return;
    if (info.isFile()) {
      const path = toPosixPath(relativePath);
      if (basename(absolutePath).toLocaleLowerCase("zh-CN").includes(normalizedQuery)) {
        state.results.push({
          name: basename(absolutePath),
          path,
          type: "file",
          editable: isEditableFile(absolutePath) && info.size <= MAX_FILE_BYTES,
          size: info.size,
        });
      }
      return;
    }
    if (!info.isDirectory()) return;

    const entries = await readdir(absolutePath, { withFileTypes: true }).catch((error) => {
      recordScanError(state.scanErrors, realRoot, absolutePath, error);
      return [];
    });
    for (const entry of visibleDirectoryEntries(entries)) {
      if (state.results.length >= MAX_SEARCH_RESULTS) {
        state.truncatedByResults = true;
        break;
      }
      if (state.nodes >= MAX_SEARCH_NODES) {
        state.truncatedByNodes = true;
        break;
      }
      await visit(
        resolve(absolutePath, entry.name),
        relativePath ? `${relativePath}${sep}${entry.name}` : entry.name,
        depth + 1,
      );
    }
  }

  for (const entry of roots) {
    await visit(entry.absolutePath, entry.relativePath, 0);
    if (state.truncatedByResults || state.truncatedByNodes) break;
  }
  state.results.sort(compareTreeEntries);
  const truncated =
    state.truncatedByResults ||
    state.truncatedByNodes ||
    state.truncatedByDepth ||
    state.scanErrors.length > 0;
  return {
    query,
    scope: scopeValue,
    results: state.results,
    truncated,
    truncation: {
      byResults: state.truncatedByResults,
      byNodes: state.truncatedByNodes,
      byDepth: state.truncatedByDepth,
      byReadError: state.scanErrors.length > 0,
    },
    scanErrors,
    limits: {
      maxResults: MAX_SEARCH_RESULTS,
      maxNodes: MAX_SEARCH_NODES,
      maxDepth: MAX_SEARCH_DEPTH,
    },
  };
}

// ── 长篇项目状态（只读） ─────────────────────────────────────────────────────
// 状态卡绝不能因为某份项目数据坏了就 500：每一块各自兜底，读不动的那块置 null，
// 原因写进 errors；只有「路径越界 / 不是长篇项目」这类请求本身的问题才返回 4xx。

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function nonNegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value >= 1 ? value : null;
}

function optionalString(value) {
  return typeof value === "string" ? value : null;
}

// 与 Python 的 sorted() 同序（按码位），保证「第一个未闭环候选」和写作脚本认的是同一个。
function compareCodePoints(left, right) {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

function compareChapterNames(left, right) {
  return left.localeCompare(right, "zh-CN", { numeric: true, sensitivity: "base" }) || compareCodePoints(left, right);
}

function errorCode(error) {
  return typeof error?.code === "string" ? error.code : "READ_ERROR";
}

async function mapWithConcurrency(items, limit, worker) {
  const results = new Array(items.length);
  let cursor = 0;
  async function drain() {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      results[index] = await worker(items[index], index);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, drain));
  return results;
}

async function hasRegularTrackingState(projectRoot) {
  const trackingInfo = await lstat(resolve(projectRoot, TRACKING_DIRECTORY)).catch(() => null);
  if (!trackingInfo?.isDirectory() || trackingInfo.isSymbolicLink()) return false;
  const stateInfo = await lstat(resolve(projectRoot, TRACKING_DIRECTORY, TRACKING_STATE_FILE)).catch(() => null);
  return Boolean(stateInfo?.isFile() && !stateInfo.isSymbolicLink());
}

async function assertLongProjectMarker(projectRoot) {
  const markers = [
    { path: resolve(projectRoot, TRACKING_DIRECTORY), directory: true },
    { path: resolve(projectRoot, TRACKING_DIRECTORY, TRACKING_STATE_FILE), directory: false },
  ];
  for (const marker of markers) {
    let info;
    try {
      info = await lstat(marker.path);
    } catch (error) {
      if (error?.code === "ENOENT" || error?.code === "ENOTDIR") {
        throw new DashboardError(
          404,
          "project_not_found",
          `该目录不是长篇项目：缺少 ${TRACKING_STATE_LABEL}`,
        );
      }
      throw new DashboardError(
        403,
        "project_unreadable",
        "项目追踪目录无法读取，请检查访问权限或挂载状态",
      );
    }
    if (info.isSymbolicLink()) {
      throw new DashboardError(403, "symlink_not_readable", "Dashboard 不读取符号链接形式的追踪状态");
    }
    if (marker.directory ? !info.isDirectory() : !info.isFile()) {
      throw new DashboardError(
        404,
        "project_not_found",
        `该目录不是长篇项目：缺少 ${TRACKING_STATE_LABEL}`,
      );
    }
  }
}

function projectLabel(projectRoot, absolutePath) {
  return toPosixPath(relative(projectRoot, absolutePath)) || ".";
}

// 只读普通目录：符号链接与文件树、/api/file 一样不跟随。缺失返回 missing，读不动记错误。
async function readStatusDirectory(absolutePath, projectRoot, errors) {
  const label = projectLabel(projectRoot, absolutePath);
  let info;
  try {
    info = await lstat(absolutePath);
  } catch (error) {
    if (error?.code === "ENOENT") return { missing: true, entries: null };
    errors.push(`${label}：目录无法读取（${errorCode(error)}）`);
    return { missing: false, entries: null };
  }
  if (info.isSymbolicLink() || !info.isDirectory()) {
    errors.push(`${label}：不是普通目录，Dashboard 不跟随符号链接`);
    return { missing: false, entries: null };
  }
  try {
    return { missing: false, entries: await readdir(absolutePath, { withFileTypes: true }) };
  } catch (error) {
    errors.push(`${label}：目录无法读取（${errorCode(error)}）`);
    return { missing: false, entries: null };
  }
}

async function readStatusText(absolutePath, projectRoot, errors, maxBytes = MAX_FILE_BYTES) {
  const label = projectLabel(projectRoot, absolutePath);
  let info;
  try {
    info = await lstat(absolutePath);
  } catch (error) {
    if (error?.code === "ENOENT") return { status: "missing" };
    errors.push(`${label}：无法读取（${errorCode(error)}）`);
    return { status: "error" };
  }
  if (info.isSymbolicLink() || !info.isFile()) {
    errors.push(`${label}：不是普通文件，Dashboard 不跟随符号链接`);
    return { status: "error" };
  }
  if (info.size > maxBytes) {
    errors.push(`${label}：文件超过 ${Math.round(maxBytes / (1024 * 1024))} MiB，未读取`);
    return { status: "error" };
  }
  try {
    return { status: "ok", text: (await readFile(absolutePath, "utf8")).replace(/^﻿/u, "") };
  } catch (error) {
    if (error?.code === "ENOENT") return { status: "missing" };
    errors.push(`${label}：无法读取（${errorCode(error)}）`);
    return { status: "error" };
  }
}

async function readStatusJson(absolutePath, projectRoot, errors, maxBytes = MAX_FILE_BYTES) {
  const result = await readStatusText(absolutePath, projectRoot, errors, maxBytes);
  if (result.status !== "ok") return result;
  try {
    return { status: "ok", value: JSON.parse(result.text) };
  } catch (error) {
    errors.push(`${projectLabel(projectRoot, absolutePath)}：JSON 格式错误（${error.message}）`);
    return { status: "error" };
  }
}

/** 中文网文口径的字数：非空白字符按码位计数，Markdown 标题行（章名）不计入。 */
export function countManuscriptCharacters(markdown) {
  let total = 0;
  for (const line of String(markdown).replace(/^﻿/u, "").split(/\r\n|\r|\n/u)) {
    if (MARKDOWN_HEADING_LINE.test(line)) continue;
    // \s 覆盖全角空格（U+3000）等 Unicode 空白：段首「　　」缩进不算字。
    total += [...line.replace(/\s+/gu, "")].length;
  }
  return total;
}

/** 从 大纲/大纲.md 读目标体量；「目标字数」优先于开书时的「预计字数」。 */
export function parseTargetWords(markdown) {
  const found = new Map();
  for (const line of String(markdown).split(/\r\n|\r|\n/u)) {
    const match = TARGET_WORDS_LINE.exec(line);
    if (match && !found.has(match[1])) found.set(match[1], Number(match[2]));
  }
  for (const label of TARGET_WORDS_LABELS) {
    const value = found.get(label);
    if (Number.isFinite(value) && value > 0) return Math.round(value * 10000);
  }
  return null;
}

function emptyTrackingSummary() {
  return {
    schema_version: null,
    last_committed_chapter: null,
    state_revision: null,
    overdue_foreshadows: null,
    due_soon_foreshadows: null,
    absent_characters: null,
    dormant_threads: null,
  };
}

function optionalMapping(value, field, errors) {
  if (value === undefined || value === null) return {};
  if (isPlainObject(value)) return value;
  errors.push(`${TRACKING_STATE_LABEL}：${field} 不是对象`);
  return null;
}

function summarizeTrackingState(document, errors) {
  const summary = emptyTrackingSummary();
  if (!isPlainObject(document)) {
    errors.push(`${TRACKING_STATE_LABEL}：顶层不是 JSON 对象`);
    return summary;
  }
  summary.schema_version = Number.isSafeInteger(document.schema_version) ? document.schema_version : null;
  summary.last_committed_chapter = nonNegativeInteger(document.last_committed_chapter);
  summary.state_revision = nonNegativeInteger(document.state_revision);
  if (summary.state_revision === null) {
    errors.push(`${TRACKING_STATE_LABEL}：state_revision 缺失或不是非负整数`);
  }
  const last = summary.last_committed_chapter;
  if (last === null) {
    errors.push(`${TRACKING_STATE_LABEL}：last_committed_chapter 缺失或不是非负整数`);
    return summary;
  }
  const nextChapter = last + 1;

  const foreshadow = optionalMapping(document.foreshadow, "foreshadow", errors);
  if (foreshadow) {
    const overdue = [];
    const dueSoon = [];
    for (const [key, row] of Object.entries(foreshadow)) {
      if (!isPlainObject(row) || row.status !== "已埋") continue;
      const planned = positiveInteger(row.planned_resolution_chapter);
      if (planned === null) continue;
      const item = {
        id: typeof row.id === "string" && row.id ? row.id : key,
        summary: optionalString(row.summary),
        planned_resolution_chapter: planned,
        importance: optionalString(row.importance),
      };
      if (planned < nextChapter) {
        overdue.push({ ...item, overdue_by: nextChapter - planned });
      } else if (planned <= nextChapter + FORESHADOW_DUE_SOON_WINDOW) {
        dueSoon.push(item);
      }
    }
    const byPlannedChapter = (left, right) =>
      left.planned_resolution_chapter - right.planned_resolution_chapter || compareCodePoints(left.id, right.id);
    summary.overdue_foreshadows = overdue.sort(byPlannedChapter);
    summary.due_soon_foreshadows = dueSoon.sort(byPlannedChapter);
  }

  // appearances 是 v5 才补上的出场记录，旧项目没有这一项时按空表处理，不算错误。
  const appearances = optionalMapping(document.appearances, "appearances", errors);
  const characters = optionalMapping(document.characters, "characters", errors);
  const context = optionalMapping(document.context, "context", errors);
  let activeNames = null;
  if (context) {
    const raw = context.active_character_names;
    if (raw === undefined || raw === null) {
      activeNames = [];
    } else if (Array.isArray(raw)) {
      activeNames = raw.filter((name) => typeof name === "string" && name.length > 0);
    } else {
      errors.push(`${TRACKING_STATE_LABEL}：context.active_character_names 不是数组`);
    }
  }
  const lastSeen = (name) => {
    const row = appearances && Object.hasOwn(appearances, name) ? appearances[name] : null;
    const seen = isPlainObject(row) && Array.isArray(row.seen) ? row.seen.filter((chapter) => positiveInteger(chapter) !== null) : [];
    return seen.length ? Math.max(...seen) : null;
  };
  if (appearances && activeNames) {
    summary.absent_characters = activeNames.flatMap((name) => {
      const seen = lastSeen(name);
      if (seen === null || last - seen < LONG_ABSENCE_CHAPTERS) return [];
      return [{ name, last_seen: seen, absent: last - seen }];
    });
  }
  if (appearances && activeNames && characters) {
    const active = new Set(activeNames);
    summary.dormant_threads = Object.keys(characters)
      .sort(compareCodePoints)
      .flatMap((name) => {
        if (active.has(name)) return [];
        const snapshot = characters[name];
        const threads = isPlainObject(snapshot) && Array.isArray(snapshot.open_threads) ? snapshot.open_threads.length : 0;
        const seen = lastSeen(name);
        if (threads === 0 || seen === null || last - seen < DORMANT_THREAD_CHAPTERS) return [];
        return [{ name, open_threads: threads, last_seen: seen, absent: last - seen }];
      });
  }
  return summary;
}

async function summarizeManuscript(projectRoot, errors) {
  const bodyRoot = resolve(projectRoot, "正文");
  const listing = await readStatusDirectory(bodyRoot, projectRoot, errors);
  if (listing.missing) return { words_written: 0, chapter_files: 0, words_truncated: false };
  if (!listing.entries) return { words_written: null, chapter_files: null, words_truncated: false };

  const names = listing.entries
    .filter((entry) => entry.isFile() && CHAPTER_FILE_PATTERN.test(entry.name))
    .map((entry) => entry.name)
    .sort(compareChapterNames);
  const counted = names.slice(0, MAX_STATUS_CHAPTER_FILES);
  const results = await mapWithConcurrency(counted, STATUS_READ_CONCURRENCY, async (name) => {
    const fileErrors = [];
    const file = await readStatusText(resolve(bodyRoot, name), projectRoot, fileErrors);
    return { file, fileErrors };
  });
  let words = 0;
  let files = 0;
  let skipped = false;
  for (const { file, fileErrors } of results) {
    errors.push(...fileErrors);
    if (file.status !== "ok") {
      skipped = skipped || file.status === "error";
      continue;
    }
    words += countManuscriptCharacters(file.text);
    files += 1;
  }
  return {
    words_written: words,
    chapter_files: files,
    words_truncated: names.length > counted.length || skipped,
  };
}

async function readTargetWords(projectRoot, errors) {
  const outline = await readStatusText(resolve(projectRoot, "大纲", "大纲.md"), projectRoot, errors);
  return outline.status === "ok" ? parseTargetWords(outline.text) : null;
}

function describeCandidate(manifest, chapterDirectory, runDirectory, realRoot) {
  const reviews = isPlainObject(manifest.reviews) ? manifest.reviews : {};
  const verdict = (kind) => {
    const receipt = Object.hasOwn(reviews, kind) ? reviews[kind] : null;
    return isPlainObject(receipt) ? optionalString(receipt.verdict) : null;
  };
  const numbered = /^第0*(\d+)章$/u.exec(chapterDirectory);
  return {
    chapter: positiveInteger(manifest.chapter) ?? (numbered ? Number(numbered[1]) : null),
    status: manifest.status,
    run: toPosixPath(relative(realRoot, runDirectory)),
    approval_mode: optionalString(manifest.approval_mode),
    gate_status: isPlainObject(manifest.gate_run) ? optionalString(manifest.gate_run.status) : null,
    reviews: { deslop: verdict("deslop"), consistency: verdict("consistency") },
  };
}

// 与 chapter_candidate.py 的 open_workspaces 同序扫描 追踪/候选章/第*章/*/manifest.json。
// 排在前面的 manifest 读坏时无法断定谁是「第一个未闭环候选」，整块置 null 并报错。
async function findOpenCandidate(projectRoot, realRoot, errors) {
  const candidatesRoot = resolve(projectRoot, TRACKING_DIRECTORY, "候选章");
  const chapterListing = await readStatusDirectory(candidatesRoot, projectRoot, errors);
  if (chapterListing.missing || !chapterListing.entries) return null;
  const chapterDirectories = chapterListing.entries
    .filter((entry) => entry.isDirectory() && CANDIDATE_CHAPTER_DIRECTORY_PATTERN.test(entry.name))
    .map((entry) => entry.name)
    .sort(compareCodePoints);
  let scanned = 0;
  for (const chapterDirectory of chapterDirectories) {
    const chapterRoot = resolve(candidatesRoot, chapterDirectory);
    const runListing = await readStatusDirectory(chapterRoot, projectRoot, errors);
    if (runListing.missing) continue;
    if (!runListing.entries) return null;
    const runs = runListing.entries
      .filter((entry) => entry.isDirectory() && !shouldIgnoreDirectory(entry.name))
      .map((entry) => entry.name)
      .sort(compareCodePoints);
    for (const run of runs) {
      scanned += 1;
      if (scanned > MAX_STATUS_RECORD_FILES) {
        errors.push(`${TRACKING_DIRECTORY}/候选章：候选运行目录超过 ${MAX_STATUS_RECORD_FILES} 个，未完成扫描`);
        return null;
      }
      const runDirectory = resolve(chapterRoot, run);
      const manifestPath = resolve(runDirectory, "manifest.json");
      const manifest = await readStatusJson(manifestPath, projectRoot, errors);
      if (manifest.status === "missing") continue;
      if (manifest.status === "error") return null;
      if (!isPlainObject(manifest.value)) {
        errors.push(`${projectLabel(projectRoot, manifestPath)}：顶层不是 JSON 对象`);
        return null;
      }
      if (OPEN_CANDIDATE_STATUSES.has(manifest.value.status)) {
        return describeCandidate(manifest.value, chapterDirectory, runDirectory, realRoot);
      }
    }
  }
  return null;
}

async function summarizeCommitReceipts(projectRoot, errors) {
  const receiptsRoot = resolve(projectRoot, TRACKING_DIRECTORY, "章节提交");
  const listing = await readStatusDirectory(receiptsRoot, projectRoot, errors);
  if (listing.missing) return { gated: 0, legacy: 0 };
  if (!listing.entries) return null;
  const names = listing.entries
    .filter((entry) => entry.isFile() && COMMIT_RECEIPT_PATTERN.test(entry.name))
    .map((entry) => entry.name)
    .sort(compareChapterNames);
  if (names.length > MAX_STATUS_RECORD_FILES) {
    errors.push(`${TRACKING_DIRECTORY}/章节提交：提交凭证超过 ${MAX_STATUS_RECORD_FILES} 份，未统计`);
    return null;
  }
  const results = await mapWithConcurrency(names, STATUS_READ_CONCURRENCY, async (name) => {
    const receiptErrors = [];
    const receipt = await readStatusJson(resolve(receiptsRoot, name), projectRoot, receiptErrors);
    return { name, receipt, receiptErrors };
  });
  let gated = 0;
  let legacy = 0;
  let failed = false;
  for (const { name, receipt, receiptErrors } of results) {
    errors.push(...receiptErrors);
    if (receipt.status === "missing") continue;
    if (receipt.status === "error") {
      failed = true;
      continue;
    }
    if (!isPlainObject(receipt.value)) {
      errors.push(`${TRACKING_DIRECTORY}/章节提交/${name}：顶层不是 JSON 对象`);
      failed = true;
      continue;
    }
    if (receipt.value.protocol === GATED_PROTOCOL) gated += 1;
    else legacy += 1;
  }
  return failed ? null : { gated, legacy };
}

// 意料之外的 I/O 异常（EIO、竞态删除等）也只让对应那一块置空，不把整张状态卡打成 500。
async function guardedStatusPart(label, errors, fallback, operation) {
  try {
    return await operation();
  } catch (error) {
    errors.push(`${label}：读取失败（${errorCode(error)}）`);
    return fallback;
  }
}

export async function readProjectStatus(root, requestedPath) {
  const { absolutePath: projectRoot, realRoot } = await resolveWorkspaceDirectory(root, requestedPath);
  await assertLongProjectMarker(projectRoot);

  const stateErrors = [];
  const manuscriptErrors = [];
  const outlineErrors = [];
  const candidateErrors = [];
  const receiptErrors = [];
  const [tracking, manuscript, targetWords, openCandidate, quality] = await Promise.all([
    guardedStatusPart(TRACKING_STATE_LABEL, stateErrors, emptyTrackingSummary(), async () => {
      const state = await readStatusJson(
        resolve(projectRoot, TRACKING_DIRECTORY, TRACKING_STATE_FILE),
        projectRoot,
        stateErrors,
        MAX_TRACKING_STATE_BYTES,
      );
      if (state.status === "missing") {
        stateErrors.push(`${TRACKING_STATE_LABEL}：文件在读取时消失`);
      }
      return state.status === "ok" ? summarizeTrackingState(state.value, stateErrors) : emptyTrackingSummary();
    }),
    guardedStatusPart(
      "正文",
      manuscriptErrors,
      { words_written: null, chapter_files: null, words_truncated: false },
      () => summarizeManuscript(projectRoot, manuscriptErrors),
    ),
    guardedStatusPart("大纲/大纲.md", outlineErrors, null, () => readTargetWords(projectRoot, outlineErrors)),
    guardedStatusPart(`${TRACKING_DIRECTORY}/候选章`, candidateErrors, null, () =>
      findOpenCandidate(projectRoot, realRoot, candidateErrors),
    ),
    guardedStatusPart(`${TRACKING_DIRECTORY}/章节提交`, receiptErrors, null, () =>
      summarizeCommitReceipts(projectRoot, receiptErrors),
    ),
  ]);

  return {
    path: toPosixPath(relative(realRoot, projectRoot)) || ".",
    schema_version: tracking.schema_version,
    last_committed_chapter: tracking.last_committed_chapter,
    state_revision: tracking.state_revision,
    words_written: manuscript.words_written,
    chapter_files: manuscript.chapter_files,
    words_truncated: manuscript.words_truncated,
    target_words: targetWords,
    overdue_foreshadows: tracking.overdue_foreshadows,
    due_soon_foreshadows: tracking.due_soon_foreshadows,
    absent_characters: tracking.absent_characters,
    dormant_threads: tracking.dormant_threads,
    open_candidate: openCandidate,
    quality,
    errors: [...stateErrors, ...manuscriptErrors, ...outlineErrors, ...candidateErrors, ...receiptErrors],
    limits: {
      maxChapterFiles: MAX_STATUS_CHAPTER_FILES,
    },
  };
}

async function readJsonBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_REQUEST_BYTES) {
      throw new DashboardError(413, "request_too_large", "保存内容超过 2 MiB 限制");
    }
    chunks.push(chunk);
  }

  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new DashboardError(400, "invalid_json", "请求正文不是有效 JSON");
  }
}

function responseHeaders(contentType) {
  return {
    "Cache-Control": "no-store",
    "Content-Security-Policy":
      "default-src 'self'; base-uri 'none'; connect-src 'self'; img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'",
    "Content-Type": contentType,
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
  };
}

function sendJson(response, status, payload) {
  response.writeHead(status, responseHeaders("application/json; charset=utf-8"));
  response.end(JSON.stringify(payload));
}

async function readWorkspaceFile(root, requestedPath) {
  const { absolutePath, info } = await resolveWorkspacePath(root, requestedPath, {
    editableOnly: true,
  });
  if (info.size > MAX_FILE_BYTES) {
    throw new DashboardError(413, "file_too_large", "文件超过 2 MiB，无法在 Dashboard 中打开");
  }
  const content = await readFile(absolutePath, "utf8");
  return {
    path: toPosixPath(relative(await existingRealRoot(root), absolutePath)),
    name: basename(absolutePath),
    content,
    size: Buffer.byteLength(content),
    mtimeMs: info.mtimeMs,
    version: fileVersion(content),
  };
}

async function replaceFileAtomically(target, content, mode) {
  const temporary = resolve(dirname(target), `.${basename(target)}.story-dashboard-${randomUUID()}.tmp`);
  await writeFile(temporary, content, { encoding: "utf8", mode });
  // open(2) 的 mode 会被进程 umask 削掉，光靠 writeFile 保不住原文件权限，
  // 所以改名前显式补一次；个别文件系统不支持权限位，失败就按原样落盘。
  await chmod(temporary, mode & 0o7777).catch(() => {});
  try {
    await rename(temporary, target);
  } catch (error) {
    if (process.platform !== "win32" || !["EACCES", "EPERM", "EEXIST"].includes(error?.code)) {
      await unlink(temporary).catch(() => {});
      throw error;
    }
    await copyFile(temporary, target);
    await unlink(temporary).catch(() => {});
  }
}

async function saveWorkspaceFile(root, payload) {
  if (!payload || typeof payload !== "object") {
    throw new DashboardError(400, "invalid_payload", "缺少保存参数");
  }
  if (typeof payload.content !== "string") {
    throw new DashboardError(400, "invalid_content", "文件内容必须是文本");
  }
  if (Buffer.byteLength(payload.content) > MAX_FILE_BYTES) {
    throw new DashboardError(413, "file_too_large", "文件超过 2 MiB，无法保存");
  }
  if (!/^[a-f0-9]{64}$/.test(payload.expectedVersion || "")) {
    throw new DashboardError(400, "missing_file_version", "保存请求缺少文件版本，请重新载入后再试");
  }

  const initial = await resolveWorkspacePath(root, payload.path, {
    editableOnly: true,
  });
  return withSerializedFileMutation(initial.absolutePath, async () => {
    let current;
    try {
      current = await resolveWorkspacePath(root, payload.path, { editableOnly: true });
    } catch (error) {
      if (error instanceof DashboardError && error.code === "file_not_found") {
        throw new DashboardError(409, "file_changed", "文件已被其他程序删除。请刷新目录后再保存。");
      }
      throw error;
    }
    const currentContent = await readFile(current.absolutePath, "utf8");
    if (fileVersion(currentContent) !== payload.expectedVersion) {
      throw new DashboardError(
        409,
        "file_changed",
        "文件已被其他程序修改。请重新载入后再保存，避免覆盖新内容。",
      );
    }

    await replaceFileAtomically(current.absolutePath, payload.content, current.info.mode);
    const updated = await stat(current.absolutePath);
    return {
      ok: true,
      path: toPosixPath(relative(current.realRoot, current.absolutePath)),
      size: updated.size,
      mtimeMs: updated.mtimeMs,
      version: fileVersion(payload.content),
    };
  });
}

async function deleteWorkspaceFile(root, payload) {
  if (!payload || typeof payload !== "object") {
    throw new DashboardError(400, "invalid_payload", "缺少删除参数");
  }
  if (!/^[a-f0-9]{64}$/.test(payload.expectedVersion || "")) {
    throw new DashboardError(400, "missing_file_version", "删除请求缺少文件版本，请重新载入后再试");
  }

  const initial = await resolveWorkspacePath(root, payload.path, {
    editableOnly: true,
  });
  return withSerializedFileMutation(initial.absolutePath, async () => {
    let current;
    try {
      current = await resolveWorkspacePath(root, payload.path, { editableOnly: true });
    } catch (error) {
      if (error instanceof DashboardError && error.code === "file_not_found") {
        throw new DashboardError(409, "file_changed", "文件已被其他程序删除。请刷新目录后再操作。");
      }
      throw error;
    }
    const currentContent = await readFile(current.absolutePath, "utf8");
    if (fileVersion(currentContent) !== payload.expectedVersion) {
      throw new DashboardError(
        409,
        "file_changed",
        "文件已被其他程序修改。请重新载入后再删除，避免误删新版本。",
      );
    }

    await unlink(current.absolutePath);
    return {
      ok: true,
      path: toPosixPath(relative(current.realRoot, current.absolutePath)),
    };
  });
}

async function serveStaticFile(requestPath, response) {
  const assetName = requestPath === "/" ? "index.html" : requestPath.slice(1);
  if (!["index.html", "styles.css", "app.js"].includes(assetName)) {
    sendJson(response, 404, { error: { code: "not_found", message: "页面不存在" } });
    return;
  }
  const assetPath = resolve(ASSET_DIR, assetName);
  const body = await readFile(assetPath);
  response.writeHead(200, responseHeaders(CONTENT_TYPES[extname(assetName)] || "application/octet-stream"));
  response.end(body);
}

function normalizedHostname(hostHeader) {
  if (!hostHeader) return "";
  try {
    return new URL(`http://${hostHeader}`).hostname.replace(/^\[|\]$/g, "").toLowerCase();
  } catch {
    return "";
  }
}

function normalizedOriginHostname(origin) {
  try {
    return new URL(origin).hostname.replace(/^\[|\]$/g, "").toLowerCase();
  } catch {
    return "";
  }
}

function assertLocalRequest(request, allowNetwork) {
  if (allowNetwork) return;
  const hostname = normalizedHostname(request.headers.host);
  if (!LOOPBACK_HOSTS.has(hostname)) {
    throw new DashboardError(403, "invalid_host", "Dashboard 只接受本机回环地址请求");
  }
  if (["PUT", "DELETE"].includes(request.method) && request.headers.origin) {
    const originHostname = normalizedOriginHostname(request.headers.origin);
    if (!LOOPBACK_HOSTS.has(originHostname)) {
      throw new DashboardError(403, "invalid_origin", "拒绝来自非本机页面的写入请求");
    }
  }
}

export function createDashboardServer({ root, allowNetwork = false }) {
  const workspaceRoot = resolve(root);
  return createServer(async (request, response) => {
    try {
      assertLocalRequest(request, allowNetwork);
      const url = new URL(request.url || "/", "http://127.0.0.1");
      if (request.method === "GET" && url.pathname === "/health") {
        sendJson(response, 200, { ok: true });
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/workspace") {
        sendJson(response, 200, await scanWorkspace(workspaceRoot));
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/tree") {
        sendJson(
          response,
          200,
          await listWorkspaceDirectory(
            workspaceRoot,
            url.searchParams.get("path") || "",
            url.searchParams.get("cursor"),
          ),
        );
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/search") {
        sendJson(
          response,
          200,
          await searchWorkspace(
            workspaceRoot,
            url.searchParams.get("q") || "",
            url.searchParams.get("scope") || "",
          ),
        );
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/project-status") {
        sendJson(response, 200, await readProjectStatus(workspaceRoot, url.searchParams.get("path") || ""));
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/file") {
        sendJson(response, 200, await readWorkspaceFile(workspaceRoot, url.searchParams.get("path") || ""));
        return;
      }
      if (request.method === "PUT" && url.pathname === "/api/file") {
        sendJson(response, 200, await saveWorkspaceFile(workspaceRoot, await readJsonBody(request)));
        return;
      }
      if (request.method === "DELETE" && url.pathname === "/api/file") {
        sendJson(response, 200, await deleteWorkspaceFile(workspaceRoot, await readJsonBody(request)));
        return;
      }
      if (request.method === "GET") {
        await serveStaticFile(url.pathname, response);
        return;
      }
      sendJson(response, 405, {
        error: { code: "method_not_allowed", message: "请求方法不支持" },
      });
    } catch (error) {
      const known = error instanceof DashboardError;
      if (!known) {
        console.error("[story-dashboard]", error);
      }
      sendJson(response, known ? error.status : 500, {
        error: {
          code: known ? error.code : "internal_error",
          message: known ? error.message : "Dashboard 处理请求时发生错误",
        },
      });
    }
  });
}

function parseCliArguments(argv) {
  const options = {
    root: process.cwd(),
    host: process.env.STORY_DASHBOARD_HOST || "127.0.0.1",
    port: Number(process.env.STORY_DASHBOARD_PORT || 43110),
    open: false,
    allowNetwork: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--root") {
      options.root = argv[++index];
    } else if (value === "--host") {
      options.host = argv[++index];
    } else if (value === "--port") {
      options.port = Number(argv[++index]);
    } else if (value === "--open") {
      options.open = true;
    } else if (value === "--allow-network") {
      options.allowNetwork = true;
    } else if (value === "--help" || value === "-h") {
      options.help = true;
    } else {
      throw new DashboardError(400, "unknown_argument", `未知参数：${value}`);
    }
  }

  if (!options.root) {
    throw new DashboardError(400, "missing_root", "--root 需要目录参数");
  }
  if (!Number.isInteger(options.port) || options.port < 0 || options.port > 65535) {
    throw new DashboardError(400, "invalid_port", "端口必须是 0–65535 的整数");
  }
  if (!LOOPBACK_HOSTS.has(options.host) && !options.allowNetwork) {
    throw new DashboardError(
      400,
      "network_binding_requires_opt_in",
      "非本机地址需要显式增加 --allow-network；通常不应把写作文件暴露到局域网",
    );
  }
  return options;
}

async function listen(server, host, preferredPort) {
  const attempts = preferredPort === 0 ? [0] : Array.from({ length: 11 }, (_, index) => preferredPort + index);
  for (const port of attempts) {
    try {
      await new Promise((accept, reject) => {
        const onError = (error) => {
          server.off("listening", onListening);
          reject(error);
        };
        const onListening = () => {
          server.off("error", onError);
          accept();
        };
        server.once("error", onError);
        server.once("listening", onListening);
        server.listen(port, host);
      });
      return server.address().port;
    } catch (error) {
      if (error?.code !== "EADDRINUSE" || port === attempts.at(-1)) {
        throw error;
      }
    }
  }
  throw new Error("No available port");
}

function openBrowser(url) {
  const { command, args } = browserLaunchCommand(url);
  const child = spawn(command, args, { detached: true, stdio: "ignore" });
  child.on("error", () => {});
  child.unref();
}

export function browserLaunchCommand(url, platform = process.platform) {
  if (platform === "darwin") {
    return { command: "open", args: [url] };
  }
  if (platform === "win32") {
    return { command: "cmd", args: ["/c", "start", "", url] };
  }
  return { command: "xdg-open", args: [url] };
}

export function pathsReferToSameFile(left, right) {
  if (!left || !right) return false;
  try {
    return realpathSync(left) === realpathSync(right);
  } catch {
    return false;
  }
}

function printHelp() {
  console.log(`Story Dashboard

Usage:
  node dashboard-server.mjs [--root <dir>] [--host 127.0.0.1] [--port 43110] [--open]

Options:
  --root <dir>       写作工作区，默认当前目录
  --host <host>      监听地址，默认 127.0.0.1
  --port <port>      首选端口，默认 43110；0 表示随机端口
  --open             启动后用系统默认浏览器打开
  --allow-network    显式允许绑定非回环地址（不推荐）
`);
}

async function main() {
  const options = parseCliArguments(process.argv.slice(2));
  if (options.help) {
    printHelp();
    return;
  }

  const workspace = await existingRealRoot(options.root);
  const server = createDashboardServer({ root: workspace, allowNetwork: options.allowNetwork });
  const port = await listen(server, options.host, options.port);
  const displayHost = options.host === "::1" ? "[::1]" : options.host;
  const url = `http://${displayHost}:${port}`;

  console.log("Story Dashboard 已启动");
  console.log(`工作区：${workspace}`);
  console.log(`本机地址：${url}`);
  if (options.open) {
    openBrowser(url);
  }

  const shutdown = () => {
    server.close(() => process.exit(0));
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

const isMain = pathsReferToSameFile(process.argv[1], MODULE_PATH);
if (isMain) {
  main().catch((error) => {
    const message = error instanceof DashboardError ? error.message : error?.stack || String(error);
    console.error(`Story Dashboard 启动失败：${message}`);
    process.exitCode = 1;
  });
}
