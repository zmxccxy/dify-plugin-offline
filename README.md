# dify-plugin-offline

<div align="center">

**将 Dify 插件（.difypkg）重打包为内网 / 离线安装包**

**Repackage Dify plugin packages for air-gapped / offline deployment**

[Python 3.8+](https://www.python.org/) · macOS / Windows / Linux · [MIT License](LICENSE)

[English](#english) | [中文说明](#中文说明)

</div>

---

# English

## What problem does it solve

Dify installs a plugin by having its plugin daemon (`plugin_daemon`) create a fresh Python
virtual environment and install the plugin's dependencies with `uv pip install` /
`uv sync` — which **requires network access to PyPI**. On intranet servers (air-gapped,
government, enterprise, or lab environments) plugin installation therefore fails.

**dify-plugin-offline** turns any existing plugin `.difypkg` into a **self-contained offline
package**: every dependency wheel is bundled inside the package, and the daemon installs
everything from local files — **no network needed at install time**.

Born from a real-world case: installing the official `openai_api_compatible` plugin on an
ARM64 intranet Dify 1.17.0 server.

## How it works

```
original .difypkg
      │
      ▼  1. unzip
plugin source + requirements.txt + pyproject.toml + uv.lock
      │
      ▼  2. pip download (per target arch: arm64 / amd64, manylinux2014 + manylinux_2_28,
      │     Python version read from manifest.yaml → meta.runner.version)
deps/*.whl
      │
      ▼  3. rewrite requirements.txt  →  ./deps/xxx.whl
      │     (--arch both adds platform_machine markers)
      ▼  4. remove pyproject.toml & uv.lock
      │     → forces the daemon onto the requirements.txt (local) path
      ▼  5. re-zip (deterministic, fixed timestamps)
<name>-<arch>-offline.difypkg
      │
      ▼  6. optional verification
uv pip install --dry-run --offline -r requirements.txt   ← same command the daemon uses
```

> Why remove `pyproject.toml`? When a plugin package contains `pyproject.toml`, the daemon
> prefers `uv sync --frozen`, which resolves from the network. Removing it (and `uv.lock`)
> forces the daemon to install from the bundled `requirements.txt` — fully offline.

## Features

- 🌍 **Cross-platform**: pure Python 3 stdlib + `pip`; runs identically on macOS, Windows and Linux
- 🏗️ **Multi-arch**: `arm64`, `amd64`, or `both` (one universal package, wheel selected at
  install time via `platform_machine` markers)
- 🪞 **Configurable pip sources**: official PyPI, Aliyun, Tsinghua, Tencent, USTC, or any
  custom index (e.g. an internal Nexus/Artifactory)
- 📋 **Robust failure handling**: whole-file download first, then automatic per-package
  retry; every failed dependency is logged individually, the run ends with exit code 1 and
  never produces a half-baked package
- 📝 **Full logging**: console + `build-offline-pkg.log`, timestamped, every retry recorded
- ✅ **Built-in verification**: replicates the daemon's exact install command with
  `uv pip install --dry-run --offline` when `uv` is available
- 🔏 **Reproducible**: fixed zip timestamps → identical SHA256 on re-runs
- 🚫 **No Dify CLI required**: the tool repackages an *existing* `.difypkg` (pure zip
  manipulation + `pip download`)

## Requirements

| Item | Requirement |
| ---- | ----------- |
| Python | 3.8+ with `pip` (the wheels are downloaded by that same pip) |
| Network | the build machine must reach the chosen pip index |
| Input | a valid Dify plugin `.difypkg` that **contains `requirements.txt`** (official marketplace / GitHub release packages all do) |
| Optional | `uv` for the offline-resolution verification step (`pip install uv`) |

## Compatibility

The tool uses only the Python standard library plus `pip`, with no shell commands or
OS-specific paths, and runs on all major desktop OSes:

| OS | Status |
| -- | ------ |
| macOS (arm64) | ✅ tested — full flow, arm64 & both-arch builds |
| Linux (amd64) | ✅ tested — full flow in a Debian container, incl. `uv --offline` verification |
| Linux (arm64) | ✅ same code path as the tested builds above |
| Windows | ✅ compatible by design (stdlib only, UTF-8 console handling, `\`/`/` path handling); machine-testing welcome — please open an issue if anything breaks |

Windows invocation: `python build-offline-pkg.py ...` (or `py -3 ...`).

## Quick start

```bash
# 1. put the original plugin package next to the script
cp ~/Downloads/openai_api_compatible-0.0.65.difypkg .

# 2. build — default: arm64, official PyPI
python3 build-offline-pkg.py

# 3. result
#    openai_api_compatible-0.0.65-arm64-offline.difypkg
#    openai_api_compatible-0.0.65-arm64-offline.difypkg.sha256
```

Windows (cmd / PowerShell):

```powershell
python build-offline-pkg.py --arch amd64 --pip-source tsinghua
```

## Usage

```
python3 build-offline-pkg.py [options]
```

| Option | Values | Description |
| ------ | ------ | ----------- |
| `--input` | path | Original `.difypkg` (default: auto-detect a non-`-offline` `.difypkg` in the script directory) |
| `--arch` | `arm64` / `amd64` / `both` | Target server architecture (default `arm64`). `both` = one universal package, roughly double the size |
| `--pip-source` | `official` / `aliyun` / `tsinghua` / `tencent` / `ustc` | PyPI index for downloading wheels (default `official`) |
| `--pip-index-url` | URL | Custom index (takes priority over `--pip-source`, e.g. internal Nexus/Artifactory) |
| `--python-version` | e.g. `3.12` | Python version of the wheels (default: `meta.runner.version` from `manifest.yaml`, fallback 3.12) |
| `--retries` | int | Retries per download action (default 3) |
| `--no-verify` | — | Skip the `uv` offline-resolution verification |
| `--log-file` | path | Log file (default `build-offline-pkg.log` next to the script) |
| `--verbose` | — | Debug-level console output (the log file always records everything) |

Output naming: `openai_api_compatible-0.0.65-arm64.difypkg` →
`openai_api_compatible-0.0.65-arm64-offline.difypkg` (`--arch both` → `-all-arch-offline`).

## Getting an original `.difypkg`

1. **Official marketplace / GitHub release** of the plugin you need — recommended, it always
   contains `requirements.txt`;
2. **Build from source** with the official CLI: `dify plugin package <dir> -o out.difypkg`
   — CLI install instructions: [Dify Plugin CLI](https://docs.dify.ai/en/develop-plugin/getting-started/cli)
   (binaries: [dify-plugin-daemon releases](https://github.com/langgenius/dify-plugin-daemon/releases));
3. Export from an existing Dify instance.

> This tool only repackages. If you must build from source, use the official CLI first.

## pip mirrors

| Name | URL |
| ---- | --- |
| `official` | `https://pypi.org/simple` |
| `aliyun` | `https://mirrors.aliyun.com/pypi/simple/` |
| `tsinghua` | `https://pypi.tuna.tsinghua.edu.cn/simple` |
| `tencent` | `https://mirrors.cloud.tencent.com/pypi/simple` |
| `ustc` | `https://pypi.mirrors.ustc.edu.cn/simple/` |

Mirrors occasionally lag behind PyPI for a few hours/days (e.g. a brand-new package version
may be missing) — if a dependency fails, simply switch sources:

```bash
python3 build-offline-pkg.py --pip-source aliyun
# or an internal mirror:
python3 build-offline-pkg.py --pip-index-url http://nexus.internal/pypi/simple
```

## Logging & failure handling

- Whole-file download first (fast); on failure it automatically switches to per-package
  downloads, reusing everything already downloaded;
- Each dependency gets its own retries and its own log line:

```
2026-09-06 18:14:51 [INFO] [3/43] gevent==26.5.0 —— 第 1/3 次下载尝试
2026-09-06 18:14:53 [WARNING] [3/43] gevent==26.5.0 —— 失败(exit=1): ERROR: No matching distribution ...
2026-09-06 18:15:02 [ERROR] 依赖下载失败: gevent==26.5.0
...
2026-09-06 18:15:02 [ERROR] 存在下载失败的依赖（43 条），详见上方 [ERROR] 日志: build-offline-pkg.log
```

- Failed runs exit with code **1** and produce no output package.

## Verification

When `uv` is installed, the script runs the **exact install command the Dify daemon uses**:

```
uv pip install --dry-run --offline --target <tmp> \
    --python-platform aarch64-unknown-linux-gnu --python-version 3.12 \
    -r requirements.txt
```

`离线解析验证通过` means the package can install on the server with zero network.

## Installing on the offline server

1. **Signature verification** — repackaging invalidates the official signature. If install
   fails with `plugin verification has been enabled ... bad signature`, set in the Dify
   deployment `.env` and restart the daemon:
   ```bash
   FORCE_VERIFYING_SIGNATURE=false
   docker compose up -d plugin_daemon
   ```
2. **Package size limit** — Dify 1.17.0's api container defaults to
   `PLUGIN_MAX_PACKAGE_SIZE=52428800` (50 MB). Fine for single-arch packages; raise it if a
   `both` package exceeds it.
3. **Front nginx** — `413 Request Entity Too Large` means the nginx in front of Dify needs
   `client_max_body_size` > package size, then reload.

## FAQ

**Why `manylinux2014` and `manylinux_2_28`?**
Some packages (e.g. recent gevent releases) only publish `manylinux_2_28` wheels. The
official plugin-daemon image is Ubuntu 24.04 (glibc 2.39), which runs both.

**Do I need the official Dify CLI?**
No — repackaging only needs Python's `zipfile` + `pip download`. The CLI is only required
if you must package a plugin from source code first.

**Why is the SHA256 identical on every re-run?**
Zip entries use fixed timestamps and sorted order, so identical content produces identical
bytes — handy for CI and integrity tracking.

**Will `--arch both` double the size?**
Only the binary wheels are duplicated (with `platform_machine` markers); pure-Python wheels
are stored once. A typical model-provider plugin goes from ~13 MB (single arch) to ~21 MB.

**Can I use an internal mirror (Nexus/Artifactory)?**
Yes: `--pip-index-url http://<mirror>/pypi/simple`.

## Project structure

```
.
├── build-offline-pkg.py          # the tool (Python 3 stdlib only)
├── 离线包打包文档.md               # full Chinese documentation
├── build-offline-pkg.log         # generated log (appended per run)
├── LICENSE
└── README.md
```

## Disclaimer

This is a community tool, **not affiliated with or endorsed by langgenius / Dify**.
"Dify" and related marks belong to their respective owners.

## License

[MIT](LICENSE) — replace the placeholder copyright line with your name.

---

# 中文说明

## 解决什么问题

Dify 安装插件时，插件守护进程（`plugin_daemon`）会为插件创建独立 Python 虚拟环境，
用 `uv pip install` / `uv sync` 从 PyPI **联网**安装依赖。内网服务器（离线/涉密/实验室环境）
装插件因此必然失败。

**dify-plugin-offline** 把任意现成的插件 `.difypkg` 重打包为**自包含离线包**：所有依赖
wheel 内置进包内，守护进程全部从本地文件安装——**安装时完全不需要联网**。

诞生于真实场景：把官方 `openai_api_compatible` 插件装到 ARM64 内网 Dify 1.17.0 服务器。

## 工作原理

```
原始 .difypkg
   │ 1. 解压
插件源码 + requirements.txt + pyproject.toml + uv.lock
   │ 2. pip download（按目标架构 arm64/amd64，manylinux2014+manylinux_2_28，
   │    Python 版本取自 manifest.yaml 的 meta.runner.version）
deps/*.whl
   │ 3. 重写 requirements.txt → ./deps/xxx.whl（--arch both 时按 platform_machine 标记）
   │ 4. 删除 pyproject.toml / uv.lock → 强制守护进程走 requirements.txt 本地安装路径
   │ 5. 重新打包（固定时间戳，产物字节级可复现）
<原名>-<arch>-offline.difypkg
   │ 6. 可选校验
uv pip install --dry-run --offline -r requirements.txt   ← 与守护进程安装命令完全一致
```

> 为什么删 `pyproject.toml`？包里有它时守护进程优先执行 `uv sync --frozen`（联网解析）。
> 删掉它（和 `uv.lock`）后守护进程只能走 `requirements.txt`，即本地 wheel 安装。

## 特性

- 🌍 跨平台：仅用 Python 3 标准库 + pip，macOS / Windows / Linux 一致运行
- 🏗️ 多架构：`arm64`、`amd64`，或 `both` 双架构合一（安装时按 `platform_machine` 自动选 wheel）
- 🪞 pip 源可配：官方 / 阿里 / 清华 / 腾讯 / 中科大，或任意自定义源（内部 Nexus 等）
- 📋 失败处理：先整包下载，失败自动转逐包重试；每个失败依赖单独记日志，退出码 1，
  绝不产出半成品包
- 📝 全流程日志：控制台 + `build-offline-pkg.log`，带时间戳，每次重试都记录
- ✅ 自动校验：装有 uv 时用守护进程同款命令 `uv pip install --dry-run --offline` 验证
- 🔏 可复现：固定 zip 时间戳，重复打包 SHA256 一致
- 🚫 不需要 Dify CLI：重打包只需 zip 操作 + pip download

## 快速开始

```bash
# 1. 把原始插件包放到脚本同目录
cp ~/Downloads/openai_api_compatible-0.0.65.difypkg .

# 2. 打包（默认 arm64 + 官方源）
python3 build-offline-pkg.py

# 3. 产物
#    openai_api_compatible-0.0.65-arm64-offline.difypkg
#    openai_api_compatible-0.0.65-arm64-offline.difypkg.sha256
```

Windows：

```powershell
python build-offline-pkg.py --arch amd64 --pip-source tsinghua
```

## 兼容性

脚本只依赖 Python 标准库 + pip（无 shell 命令、无平台相关路径），三大桌面系统均可运行：

| 系统 | 状态 |
| ---- | ---- |
| macOS (arm64) | ✅ 已实测——完整流程，arm64 与双架构构建 |
| Linux (amd64) | ✅ 已实测——Debian 容器内完整流程，含 uv 离线验证 |
| Linux (arm64) | ✅ 与上述实测构建同一代码路径 |
| Windows | ✅ 设计兼容（纯标准库、UTF-8 控制台处理、路径分隔符处理）；欢迎实机验证，有问题提 issue |

Windows 下调用：`python build-offline-pkg.py ...`（或 `py -3 ...`）。

## 参数

| 参数 | 取值 | 说明 |
| ---- | ---- | ---- |
| `--input` | 路径 | 原始 `.difypkg`（缺省自动识别同目录下非 `-offline` 的包） |
| `--arch` | `arm64` / `amd64` / `both` | 目标架构（默认 arm64；both 双架构一个包，体积约 1.6 倍） |
| `--pip-source` | `official` / `aliyun` / `tsinghua` / `tencent` / `ustc` | wheel 下载源（默认官方） |
| `--pip-index-url` | URL | 自定义源，优先于 `--pip-source` |
| `--python-version` | 如 `3.12` | 覆盖 wheel 的 Python 版本（默认读 manifest） |
| `--retries` | 整数 | 每个下载动作重试次数（默认 3） |
| `--no-verify` | — | 跳过 uv 离线校验 |
| `--log-file` | 路径 | 日志文件（默认脚本同目录 `build-offline-pkg.log`） |
| `--verbose` | — | 控制台调试级输出（日志文件始终全量记录） |

## 内网服务器安装注意

1. 重打包会破坏官方签名，报 `bad signature` 时在部署 `.env` 设
   `FORCE_VERIFYING_SIGNATURE=false` 并重启 `plugin_daemon`；
2. Dify 1.17.0 默认 `PLUGIN_MAX_PACKAGE_SIZE` 50MB，双架构包超限时调大；
3. 上传报 `413 Request Entity Too Large` 时调大前置 nginx 的 `client_max_body_size`。

## 常见问题（FAQ）

- **为什么用 manylinux2014 + manylinux_2_28 两个标签？** 部分新包（如 gevent 新版）只发
  `manylinux_2_28` wheel；官方守护进程镜像为 Ubuntu 24.04，两者都兼容。
- **需要装官方 Dify CLI 吗？** 不需要；只有“从源码打包原始包”才需要它。
- **重复打包 SHA256 一样吗？** 一样（固定时间戳 + 排序）。
- **both 包体积翻倍吗？** 仅二进制 wheel 双份，纯 Python wheel 只存一份（约 1.6 倍）。
- **能用公司内部镜像吗？** `--pip-index-url http://<镜像>/pypi/simple`。

## 许可证与声明

[MIT](LICENSE)（请把 LICENSE 里的版权占位符改成你的名字）。

本项目为社区工具，与 langgenius / Dify 官方无关；"Dify" 等商标归其所有者所有。
