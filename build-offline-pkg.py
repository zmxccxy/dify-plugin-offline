#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build-offline-pkg.py —— 把一个现成的 Dify 插件 .difypkg 重打成内网离线安装包

原理（适配 Dify 1.17.0 / dify-plugin-daemon 0.6.x）：
  1. 解压原始 .difypkg；
  2. 按 manifest.yaml 的 meta.runner.version 确定 Python 版本，
     用 pip 把 requirements.txt 的全部依赖下载成目标架构的 wheel；
  3. wheel 放入包内 deps/，重写 requirements.txt 指向 ./deps/*.whl；
  4. 移除 pyproject.toml / uv.lock，强制守护进程走 requirements.txt 的
     本地安装路径（不再联网解析）；
  5. 重新打包为 <原名>-<架构>-offline.difypkg 并校验。

跨平台：macOS / Linux / Windows 通用（仅需 Python 3.8+ 与 pip）。
日志：控制台 + 脚本同目录 build-offline-pkg.log；依赖下载失败逐条记录。
"""

import argparse
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("offline-pkg")

# --------------------------------------------------------------------------
# 常用 pip 源（-i 参数），可通过 --pip-source 选择，或 --pip-index-url 自定义
# --------------------------------------------------------------------------
PIP_SOURCES = {
    "official": "https://pypi.org/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple/",
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "tencent": "https://mirrors.cloud.tencent.com/pypi/simple",
    "ustc": "https://pypi.mirrors.ustc.edu.cn/simple/",
}

# 架构 -> (pip --platform 的 linux 平台名, uv 的 --python-platform 值)
ARCH_INFO = {
    "arm64": ("aarch64", "aarch64-unknown-linux-gnu"),
    "amd64": ("x86_64", "x86_64-unknown-linux-gnu"),
}

ARCH_OUT_LABEL = {"arm64": "arm64", "amd64": "amd64", "both": "all-arch"}

PIP_TIMEOUT = 900  # 单次 pip download 超时秒数


# --------------------------------------------------------------------------
# 日志：控制台 + 文件（UTF-8，兼容 Windows）
# --------------------------------------------------------------------------
def setup_logging(log_file: Path, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)

    file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    root.setLevel(logging.DEBUG)


def _tail(text: str, n: int = 8) -> str:
    lines = [x for x in text.splitlines() if x.strip()]
    return " | ".join(lines[-n:]) if lines else "(无输出)"


def _run(cmd, cwd=None, timeout=PIP_TIMEOUT, env=None):
    LOG.debug("执行命令: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        LOG.error("命令超时（%s 秒）: %s", timeout, " ".join(cmd))
        return None
    return proc


def download_with_retries(cmd, retries: int, label: str) -> bool:
    """带重试的下载；把每次尝试与失败原因写进日志。"""
    for attempt in range(1, retries + 1):
        LOG.info("%s —— 第 %d/%d 次下载尝试", label, attempt, retries)
        proc = _run(cmd)
        if proc is None:
            LOG.error("%s —— 命令超时", label)
        elif proc.returncode == 0:
            return True
        else:
            LOG.warning("%s —— 失败(exit=%s): %s", label, proc.returncode, _tail(proc.stderr))
        if attempt < retries:
            time.sleep(min(2 * attempt, 10))
    return False


# --------------------------------------------------------------------------
# 输入包与解析
# --------------------------------------------------------------------------
def find_input_pkg(explicit: str) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise SystemExit(f"找不到输入包: {p}")
        return p

    candidates = [
        p for p in SCRIPT_DIR.glob("*.difypkg") if "-offline" not in p.name.lower()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SystemExit(
            "脚本同目录下没有找到原始 .difypkg（已排除 *-offline.difypkg）。\n"
            "请把原始插件包放到脚本同目录，或用 --input 指定路径。"
        )
    raise SystemExit(
        "脚本同目录下找到多个原始 .difypkg，请用 --input 指定：\n  " + "\n  ".join(str(c) for c in candidates)
    )


def safe_extract(pkg: Path, dest: Path) -> None:
    with zipfile.ZipFile(pkg) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                LOG.error("检测到可疑的压缩包路径 %r，已中止", name)
                raise SystemExit(1)
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    LOG.info("已解压 %s -> %s", pkg.name, dest)


def parse_requirements(text: str):
    """解析 requirements.txt：去掉注释/空行/全局选项行/--hash 行。"""
    reqs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            LOG.info("跳过 requirements.txt 中的全局选项行: %s", line)
            continue
        if " #" in line:  # 去掉行内注释
            line = line.split(" #", 1)[0].strip()
        reqs.append(line)
    return reqs


def manifest_python_version(build_dir: Path):
    """从 manifest.yaml 的 meta.runner.version 取 Python 版本。"""
    p = build_dir / "manifest.yaml"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8", errors="replace")
    m = re.search(
        r"\bmeta:\s*(?:.|\n)*?\brunner:\s*(?:.|\n)*?\bversion:\s*[\"']?(\d+)\.(\d+)",
        text,
    )
    return f"{m.group(1)}.{m.group(2)}" if m else None


# --------------------------------------------------------------------------
# 依赖下载
# --------------------------------------------------------------------------
def _pip_cmd(req_file, dest, index, plat, pyver, req_arg=None):
    cmd = [
        sys.executable, "-m", "pip", "download",
        "-i", index,
        "-d", str(dest),
        "--only-binary=:all:",
        "--platform", f"manylinux2014_{plat}",
        "--platform", f"manylinux_2_28_{plat}",
        "--python-version", pyver,
        "--retries", "5",
        "--timeout", "60",
        "--disable-pip-version-check",
    ]
    if req_arg is not None:
        cmd.append(req_arg)
    else:
        cmd += ["-r", str(req_file)]
    return cmd


def download_wheels(reqs, tmp_dir: Path, dest: Path, index: str, plat: str, pyver: str, retries: int):
    """
    下载全部依赖到 dest。
    先整包下载（快）；失败后转入逐包下载并记录每个失败项。
    返回失败列表（空 = 全部成功）。
    """
    dest.mkdir(parents=True, exist_ok=True)
    req_file = tmp_dir / "requirements_download.txt"
    req_file.write_text("\n".join(reqs) + "\n", encoding="utf-8")

    LOG.info("== 开始下载依赖（平台 %s, Python %s, 源 %s）==", plat, pyver, index)
    full_cmd = _pip_cmd(req_file, dest, index, plat, pyver)
    if download_with_retries(full_cmd, retries, "整包下载"):
        LOG.info("整包下载成功")
        return []

    LOG.warning("整包下载失败，转入逐包下载模式（已下载的部分会复用，不会重复下载）")
    failed = []
    for i, req in enumerate(reqs, 1):
        label = f"[{i}/{len(reqs)}] {req}"
        single = _pip_cmd(None, dest, index, plat, pyver, req_arg=req)
        if not download_with_retries(single, retries, label):
            failed.append(req)
            LOG.error("依赖下载失败: %s", req)
    return failed


# --------------------------------------------------------------------------
# 打包目录整理与重打包
# --------------------------------------------------------------------------
def rewrite_requirements(build_dir: Path, wheel_sets, arch: str) -> int:
    """
    wheel_sets: [(arch_tag_or_None, wheel_dir), ...]
    把 wheel 合并进 build_dir/deps/，重写 requirements.txt。
    返回去重后的 wheel 总数。
    """
    deps_dir = build_dir / "deps"
    if deps_dir.exists():
        shutil.rmtree(deps_dir)
        LOG.warning("检测到输入包内已有 deps/ 目录，已清除（将重新生成）")
    deps_dir.mkdir(parents=True)

    lines, seen, total = [], set(), 0
    for tag, wdir in wheel_sets:
        for whl in sorted(Path(wdir).glob("*.whl")):
            name = whl.name
            if name in seen:
                continue
            seen.add(name)
            shutil.copy2(whl, deps_dir / name)
            total += 1
            if arch == "both" and tag and "-none-any" not in name:
                lines.append(f'./deps/{name} ; platform_machine == "{tag}"')
            else:
                lines.append(f"./deps/{name}")

    (build_dir / "requirements.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOG.info("已重写 requirements.txt：%d 个本地 wheel 引用", total)
    return total


def strip_for_offline(build_dir: Path) -> None:
    """移除会引导守护进程联网解析的文件。"""
    for fn in ("pyproject.toml", "uv.lock"):
        p = build_dir / fn
        if p.exists():
            p.unlink()
            LOG.info("已移除 %s —— 强制守护进程使用 requirements.txt 离线安装", fn)


def repack(build_dir: Path, out_path: Path) -> None:
    """重新打包。固定时间戳保证同内容产物字节一致（SHA256 可复现）。"""
    out_name = out_path.name
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(build_dir):
            dirs.sort()
            for f in sorted(files):
                fp = Path(root) / f
                rel = fp.relative_to(build_dir).as_posix()
                if rel == out_name:  # 防止把输出包打进自身
                    continue
                zi = zipfile.ZipInfo(rel, (1980, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(zi, fp.read_bytes())
    LOG.info("已重新打包: %s", out_path)


def verify_offline(build_dir: Path, uv_targets, pyver: str) -> None:
    """用 uv 模拟守护进程的离线安装命令做解析验证（有 uv 才执行）。"""
    uv = shutil.which("uv")
    if not uv:
        LOG.warning("未检测到 uv，跳过离线解析验证（可选：pip install uv 后重跑即可验证）")
        return
    for plat in uv_targets:
        cache_dir = tempfile.mkdtemp(prefix="uvcache-")
        target_dir = tempfile.mkdtemp(prefix="uvtarget-")
        try:
            env = os.environ.copy()
            env["UV_CACHE_DIR"] = cache_dir
            cmd = [
                uv, "pip", "install", "--dry-run", "--offline",
                "--target", target_dir,
                "--python-platform", plat,
                "--python-version", pyver,
                "-r", "requirements.txt",
            ]
            LOG.info("离线解析验证（%s）...", plat)
            proc = _run(cmd, cwd=build_dir, timeout=300, env=env)
            if proc and proc.returncode == 0:
                LOG.info("离线解析验证通过（%s）：全部依赖可离线安装", plat)
            else:
                LOG.error("离线解析验证失败（%s）: %s", plat, _tail(proc.stderr) if proc else "命令超时")
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)
            shutil.rmtree(target_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="把现成的 Dify 插件 .difypkg 重打成内网离线安装包（macOS/Windows/Linux 通用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python build-offline-pkg.py                      # 自动识别同目录 .difypkg，打 arm64 离线包\n"
            "  python build-offline-pkg.py --arch amd64 --pip-source tsinghua\n"
            "  python build-offline-pkg.py --arch both --pip-source aliyun\n"
            "  python build-offline-pkg.py --input /path/to/xx.difypkg --pip-index-url https://my.mirror/pypi/simple\n"
        ),
    )
    parser.add_argument("--input", help="原始 .difypkg 路径（缺省自动识别脚本同目录下非 offline 的 .difypkg）")
    parser.add_argument("--arch", choices=["arm64", "amd64", "both"], default="arm64",
                        help="目标服务器架构（默认 arm64；both = 双架构打进一个包）")
    parser.add_argument("--pip-source", choices=sorted(PIP_SOURCES), default="official",
                        help="pip 下载源（默认 official）")
    parser.add_argument("--pip-index-url", help="自定义 pip 源地址，优先级高于 --pip-source")
    parser.add_argument("--python-version", help="wheel 的 Python 版本（默认读 manifest 的 meta.runner.version，缺省 3.12）")
    parser.add_argument("--retries", type=int, default=3, help="每个下载动作的重试次数（默认 3）")
    parser.add_argument("--no-verify", action="store_true", help="跳过 uv 离线解析验证")
    parser.add_argument("--log-file", help="日志文件路径（默认脚本同目录 build-offline-pkg.log）")
    parser.add_argument("--verbose", action="store_true", help="控制台输出调试级日志（日志文件始终记录全部）")
    args = parser.parse_args()

    log_file = Path(args.log_file).expanduser() if args.log_file else SCRIPT_DIR / "build-offline-pkg.log"
    setup_logging(log_file, args.verbose)

    LOG.info("=" * 70)
    LOG.info("Dify 插件离线包打包脚本（适配 Dify 1.17.0 / plugin-daemon 0.6.x）")
    LOG.info("参数: arch=%s pip源=%s", args.arch, args.pip_index_url or args.pip_source)

    index_url = args.pip_index_url or PIP_SOURCES[args.pip_source]

    pkg = find_input_pkg(args.input)
    LOG.info("输入包: %s（%.1f MB）", pkg, pkg.stat().st_size / 1048576)

    archs = ["arm64", "amd64"] if args.arch == "both" else [args.arch]
    uv_targets = [ARCH_INFO[a][1] for a in archs]

    tmp_root = Path(tempfile.mkdtemp(prefix="dify-offline-pkg-"))
    build_dir = tmp_root / "build"
    try:
        safe_extract(pkg, build_dir)

        if not (build_dir / "manifest.yaml").exists():
            raise SystemExit("输入包缺少 manifest.yaml，不是有效的 Dify 插件包")
        if not (build_dir / "requirements.txt").exists():
            raise SystemExit(
                "输入包缺少 requirements.txt。\n"
                "请使用包含 requirements.txt 的官方 .difypkg（市场/GitHub Releases 的包都有；\n"
                "本地源码目录请先用官方 CLI 执行 dify plugin package 打包）。"
            )

        pyver = args.python_version or manifest_python_version(build_dir) or "3.12"
        LOG.info("目标 Python 版本: %s", pyver)

        reqs = parse_requirements((build_dir / "requirements.txt").read_text(encoding="utf-8", errors="replace"))
        if not reqs:
            raise SystemExit("requirements.txt 中没有可下载的依赖条目")
        LOG.info("依赖条目数: %d", len(reqs))

        # 逐架构下载
        all_failed, wheel_sets = [], []
        for a in archs:
            plat = ARCH_INFO[a][0]
            wdir = tmp_root / f"wheels_{a}"
            failed = download_wheels(reqs, tmp_root, wdir, index_url, plat, pyver, args.retries)
            wheel_sets.append((plat if args.arch == "both" else None, wdir))
            LOG.info("架构 %s：下载完成，wheel 数量 %d，失败 %d 个", a, len(list(wdir.glob("*.whl"))), len(failed))
            all_failed += failed

        if all_failed:
            LOG.error("存在下载失败的依赖（%d 条），详见上方 [ERROR] 日志: %s", len(all_failed), log_file)
            for f in all_failed:
                LOG.error("  失败: %s", f)
            LOG.info("建议: 更换 pip 源重试（--pip-source aliyun/tsinghua/tencent/ustc 或 --pip-index-url 自定义）")
            return 1

        total = rewrite_requirements(build_dir, wheel_sets, args.arch)
        strip_for_offline(build_dir)

        stem = pkg.stem
        for suf in ("-offline", "-all-arch", "-arm64", "-aarch64", "-amd64", "-x86_64"):
            if stem.endswith(suf):
                stem = stem[: -len(suf)]
        out_name = f"{stem}-{ARCH_OUT_LABEL[args.arch]}-offline.difypkg"
        out_path = SCRIPT_DIR / out_name
        tmp_out = tmp_root / out_name
        repack(build_dir, tmp_out)
        if out_path.exists():
            LOG.warning("输出文件已存在，将被覆盖: %s", out_path)
        shutil.move(str(tmp_out), str(out_path))

        if not args.no_verify:
            verify_offline(build_dir, uv_targets, pyver)

        sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
        (out_path.with_suffix(out_path.suffix + ".sha256")).write_text(
            f"{sha}  {out_path.name}\n", encoding="utf-8"
        )

        LOG.info("=" * 70)
        LOG.info("打包完成 ✅")
        LOG.info("输出: %s（%.1f MB, %d 个 wheel）", out_path, out_path.stat().st_size / 1048576, total)
        LOG.info("SHA256: %s", sha)
        LOG.info("校验文件: %s", out_path.with_suffix(out_path.suffix + ".sha256"))
        LOG.info("日志: %s", log_file)
        LOG.info("内网安装提示: 若守护进程开启 FORCE_VERIFYING_SIGNATURE，重打包会破坏原签名，")
        LOG.info("              需将其设为 false（见打包文档），否则安装报 bad signature。")
        return 0
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        LOG.error("用户中断")
        sys.exit(130)
    except SystemExit as e:
        sys.exit(e.code)
