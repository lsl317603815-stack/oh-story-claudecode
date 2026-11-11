#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const Module = require('module');
const { Writable } = require('stream');
const { Console } = require('console');

class ExitSignal extends Error {
  constructor(code) {
    super(`exit(${code})`);
    this.exitCode = Number.isInteger(code) ? code : 0;
  }
}

const compiledCache = new Map();

function loadWrapper(scriptPath) {
  let fn = compiledCache.get(scriptPath);
  if (fn) return fn;
  const raw = fs.readFileSync(scriptPath, 'utf8');
  const body = raw.startsWith('#!') ? raw.replace(/^#![^\r\n]*/, '') : raw;
  const wrapped = `(function (exports, require, module, __filename, __dirname, process, console) {${body}\n});`;
  fn = vm.runInThisContext(wrapped, { filename: scriptPath });
  compiledCache.set(scriptPath, fn);
  return fn;
}

function runSingleGate(spec) {
  const scriptPath = path.resolve(spec.script);
  let stdout = '';
  let stderr = '';
  const stdoutStream = new Writable({
    write(chunk, _encoding, callback) {
      stdout += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
      callback();
    },
  });
  stdoutStream.isTTY = false;
  const stderrStream = new Writable({
    write(chunk, _encoding, callback) {
      stderr += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
      callback();
    },
  });
  stderrStream.isTTY = false;
  const fakeProcess = Object.create(process);
  Object.defineProperties(fakeProcess, {
    argv: { value: ['node', scriptPath, ...(spec.args || [])], writable: true, configurable: true },
    exitCode: { value: 0, writable: true, configurable: true },
    stdout: { value: stdoutStream, configurable: true },
    stderr: { value: stderrStream, configurable: true },
    exit: {
      value(code = 0) {
        throw new ExitSignal(code === undefined ? (fakeProcess.exitCode || 0) : Number(code));
      },
      configurable: true,
    },
  });
  const localConsole = new Console({ stdout: stdoutStream, stderr: stderrStream, colorMode: false });
  const localRequire = Module.createRequire(scriptPath);
  const mod = { exports: {}, id: scriptPath, filename: scriptPath, loaded: false };

  let returncode = 0;
  try {
    const wrapper = loadWrapper(scriptPath);
    wrapper(mod.exports, localRequire, mod, scriptPath, path.dirname(scriptPath), fakeProcess, localConsole);
    mod.loaded = true;
    returncode = Number.isInteger(fakeProcess.exitCode) ? fakeProcess.exitCode : 0;
  } catch (error) {
    if (error instanceof ExitSignal) {
      returncode = error.exitCode;
    } else {
      stderr += `${error && error.stack ? error.stack : String(error)}\n`;
      returncode = 1;
    }
  }
  return { name: spec.name, returncode, stdout, stderr };
}

function main() {
  let raw = '';
  try {
    raw = fs.readFileSync(0, 'utf8');
  } catch (error) {
    process.stderr.write(`run_js_gates_batch: cannot read stdin: ${error.message}\n`);
    process.exitCode = 2;
    return;
  }
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (error) {
    process.stderr.write(`run_js_gates_batch: invalid JSON input: ${error.message}\n`);
    process.exitCode = 2;
    return;
  }
  const specs = Array.isArray(payload && payload.gates) ? payload.gates : [];
  const results = [];
  let stoppedAfter = null;
  for (const spec of specs) {
    const result = runSingleGate(spec);
    results.push(result);
    if (spec.fail_fast && result.returncode !== 0) {
      stoppedAfter = spec.name;
      break;
    }
  }
  process.stdout.write(`${JSON.stringify({ stopped_after: stoppedAfter, results })}\n`);
}

main();
