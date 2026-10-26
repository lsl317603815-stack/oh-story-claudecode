#!/usr/bin/env python3
"""Classify a planned copy before any recursive replace is attempted."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def path_key(path: Path) -> str:
    # Windows 路径大小写不敏感：C:\Proj 与 c:\proj 是同一处，比较前统一 normcase。
    return os.path.normcase(str(path))


def is_relative_to(path: Path, parent: Path) -> bool:
    child_key = path_key(path)
    parent_key = path_key(parent)
    if child_key == parent_key:
        return True
    try:
        return os.path.commonpath((child_key, parent_key)) == parent_key
    except ValueError:
        # Different Windows drives cannot contain one another.
        return False


def stat_path(path: Path) -> tuple[bool | None, OSError | None]:
    """存在返回 True，不存在返回 False；权限不足等其他错误原样交回，调用方按 error 停止。"""
    try:
        path.stat()
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return None, exc
    return True, None


def classify(source_raw: str, target_raw: str) -> dict[str, Any]:
    source_input = Path(source_raw).expanduser()
    target_input = Path(target_raw).expanduser()
    source = source_input.resolve(strict=False)
    target = target_input.resolve(strict=False)
    payload: dict[str, Any] = {
        "source_input": str(source_input),
        "target_input": str(target_input),
        "source": str(source),
        "target": str(target),
        "status": "error",
        "copy_allowed": False,
        "reason": "unclassified",
    }

    source_exists, source_error = stat_path(source)
    if source_error is not None:
        payload.update(status="error", reason="filesystem_identity_error", detail=str(source_error))
        return payload
    if not source_exists:
        payload.update(status="error", reason="source_missing")
        return payload

    if path_key(source) == path_key(target):
        payload.update(status="same", reason="same_realpath")
        return payload

    target_exists, target_error = stat_path(target)
    if target_error is not None:
        payload.update(status="error", reason="filesystem_identity_error", detail=str(target_error))
        return payload
    if target_exists:
        # 无法判定是否同一文件系统对象时不放行：继续按「不同对象」走可能让递归复制覆盖源目录。
        try:
            same_object = os.path.samefile(source, target)
        except OSError as exc:
            payload.update(status="error", reason="filesystem_identity_error", detail=str(exc))
            return payload
        if same_object:
            payload.update(status="same", reason="same_filesystem_object")
            return payload

    if is_relative_to(target, source):
        payload.update(status="unsafe", reason="target_inside_source")
        return payload

    if is_relative_to(source, target):
        payload.update(status="unsafe", reason="source_inside_target")
        return payload

    payload.update(status="safe", copy_allowed=True, reason="disjoint_paths")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="copy source path")
    parser.add_argument("target", help="copy target path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = classify(args.source, args.target)
    except (OSError, RuntimeError) as exc:
        payload = {
            "source_input": args.source,
            "target_input": args.target,
            "status": "error",
            "copy_allowed": False,
            "reason": "path_resolution_failed",
            "detail": str(exc),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload["status"] in {"safe", "same"}:
        return 0
    return 1 if payload["status"] == "unsafe" else 2


if __name__ == "__main__":
    sys.exit(main())
