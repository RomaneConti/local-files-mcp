# Local Files MCP for Windows

让 ChatGPT 按需浏览、搜索和分段读取你允许的 Windows 本地目录。支持代码、日志、JSON/JSONL、gzip 文本和 NumPy NPY/NPZ；通过官方 Secure MCP Tunnel 连接，不需要上传整个目录。

服务严格只读：没有写入、删除、重命名、执行 shell 或运行项目代码的 MCP 工具。托盘启动器负责后台运行 MCP 和官方 tunnel-client。

## 从新电脑开始

需要 Windows 10/11、Git，以及下载构建依赖的网络连接。在 PowerShell 中运行：

```powershell
git clone <repository-url>
cd local-files-mcp
.\build.ps1
```

构建脚本自动准备本项目使用的 uv、Python、独立虚拟环境、锁定依赖和官方 tunnel-client，再用 PyInstaller 打包。无需预先安装 Python、创建 venv 或激活 Conda，不修改全局 Python 环境。首次构建需要下载依赖，后续构建复用本地缓存。重新构建前请先退出托盘程序。

若 PowerShell 执行策略阻止脚本，可仅对这次运行使用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

构建产物：`dist\LocalFilesMCP\LocalFilesMCP.exe`。这是目录包，复制或移动时请保留整个 `LocalFilesMCP` 文件夹及其 `_internal`，不能只复制 EXE。它可以脱离源码目录运行。构建不需要 Tunnel ID、runtime API key、允许目录或运行时代理，也不会启动服务或把本机配置打进 EXE。

## 第一次运行

1. 在 [OpenAI Platform Tunnels](https://platform.openai.com/settings/organization/tunnels) 创建 Tunnel，并关联实际使用的 ChatGPT 工作区。
2. 准备具有 `Tunnels Read + Use` 权限的 runtime API key。创建 Tunnel 另需 `Tunnels Read + Manage`。ChatGPT Developer Mode 的权限单独管理，具体见 [接入指南](docs/chatgpt-setup.md)。
3. 双击 EXE，在首次配置窗口添加允许读取的目录及简短别名，填写 Tunnel ID、可选 HTTP/HTTPS 代理和 runtime API key。代理为空表示直连；使用代理时先启动代理程序。
4. 保存后程序自动启动两个服务。右击托盘图标打开状态窗口，等待 MCP 和 Tunnel 都显示 `ready`。
5. 在 ChatGPT 创建 Developer Mode 连接，Connection 选择 **Tunnel**，选择相同 ID。新建对话选中此连接，让模型“列出允许的根目录”。

首次配置可取消；取消不会启动服务。以后可从托盘的 **设置 / 允许目录** 修改配置，保存后自动重启。添加目录、修改代理或端口不需要重新构建。

上述账户与连接流程依据 [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 和 [ChatGPT 接入文档](https://developers.openai.com/plugins/deploy/connect-chatgpt)。是否能创建 Tunnel 或开启 Developer Mode 取决于账户和工作区权限。

## 日常使用与配置位置

日常只需运行 EXE；如果配置了代理，请先启动代理。程序无 console 窗口，驻留系统托盘，图标可能位于“隐藏的图标”区域。重复双击会打开已有实例的状态窗口。

菜单提供查看状态、设置、Tunnel 管理页、日志目录、更新 runtime key、重启、停止和退出。关闭状态窗口继续驻留；停止保留托盘；退出会终止它创建的两个后台服务及其子进程。端口被其他程序占用时明确报错，不会结束其他程序。

默认配置目录为项目中的 `local-config`，不写入 C 盘用户目录。EXE 位于本项目的 `dist` 下时使用项目根目录；将完整程序文件夹单独搬走后，使用 EXE 旁的 `local-config`：

| 文件 | 用途 |
|---|---|
| `config.toml` | 允许目录、遍历跳过目录 |
| `launcher.toml` | Tunnel ID、运行时代理、MCP/Tunnel 端口 |
| `.runtime\secrets\runtime-key.dpapi` | 当前 Windows 用户的 DPAPI 加密凭据 |
| `.runtime\logs` | 分组件、脱敏、轮转日志 |

API key 不写入普通配置、命令行或 Git。读取已保存凭据时由 Windows DPAPI 解密；换电脑或用户需要重新输入。托盘使用显式保存的 DPAPI 凭据；手动命令行入口仍兼容 `CONTROL_PLANE_API_KEY`（其次 `OPENAI_API_KEY`）环境变量。不要将凭据粘贴到聊天或提交到仓库。

[config.example.toml](config.example.toml) 和 [launcher.example.toml](launcher.example.toml) 仅用于说明格式，不包含个人信息。手工编辑实际配置后点击托盘“重启”。高级测试可通过 `--data-dir <directory>` 使用隔离的配置目录。`local-config` 被 Git 忽略；不要将它放进要分享的程序包。换电脑时重新配置目录及密钥。

代理只应用于 OpenAI control plane；本地 MCP、健康检查始终直连，`NO_PROXY` 保留 `127.0.0.1,localhost`。状态检查包括 MCP 协议握手及 9 个工具定义，以及 Tunnel 的真实 control-plane 成功记录。仅有进程或 `/readyz` 返回 200 不会显示连接就绪。

菜单是固定的原生菜单，不随 health polling 重建。组件状态显示在图标提示和状态窗口，并仅在显示内容改变时更新。

## 工具和日常对话

| 工具 | 用途 |
|---|---|
| `list_roots` | 查看允许目录别名 |
| `list_directory` | 分页目录浏览 |
| `find_files` | 文件名/路径 glob 查找 |
| `search_text` | 字面文本搜索，返回行号及字节偏移 |
| `file_info` | 文件大小、修改时间和建议读取方式 |
| `read_text` | 文本/JSONL/gzip 的有界片段 |
| `read_json` | 小型 JSON 的结构化预览 |
| `inspect_array` | NPY/NPZ shape、dtype、成员元数据 |
| `read_array` | 有界 NumPy 样本 |

示例：

- “先浏览 project 的目录结构，搜索 Python 函数 `def step(`，再读取附近代码，给出路径和行号。”
- “先检查这个日志大小，只读取末尾 16 KiB。”
- “预览这个 JSONL/GZ 的前几条记录，不要一次读取整个文件。”
- “先查看 NPZ 成员和 shape/dtype，再读取指定成员的 20 个值。”

## 只读安全和读取限制

MCP 调用只能使用已配置根目录的别名和相对路径，不能新增根目录或修改配置。拒绝绝对路径、盘符相对路径、UNC/device 路径、路径穿越、ADS、保留设备名、reparse point/junction/symlink 和硬链接。Windows 原生只读句柄逐级验证目录与最终路径，防止检查后替换路径。并发写入日志可能因共享冲突被拒绝。

服务只监听回环地址并校验 Host/Origin；它信任本机进程，没有独立本机用户认证。不要用无认证反向代理公开端点。远端使用权限由 OpenAI 组织/工作区控制。允许目录内的文件均属于获准读取范围；被请求的片段会发给 ChatGPT。文件内容应作为数据处理，不能作为操作指令。

每次结果最多 64 KiB；文本窗口最多 32 KiB。搜索有文件数、字节数、深度及时间预算，未完成会明确返回，不能把局部“未找到”当成整个仓库无匹配。gzip 只支持前 8 MiB 解压内容内的窗口；普通日志支持随机字节偏移。JSON 结构化读取限 4 MiB，更大的文档用文本窗口。NPY 直接读取头部和样本，不加载全数组；NPZ 有压缩输入/成员数/解压上限。拒绝 object/pickle，ZIP 不解压到磁盘。数组偏移按文件存储顺序计算，不执行任意 Python 切片或统计代码。

## 开发与验证

完成构建后可运行正式自动化测试：

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q --basetemp .test-tmp
```

端口空闲且已经完成本机配置时，可验证实际 EXE 自动启动、真实连接、停止、重启、退出和子进程回收：

```powershell
.\.venv\Scripts\python.exe -B scripts\verify-tray.py
```

这些是本机验证，不代表已经完成 ChatGPT 对话中的云端调用。最终端到端验收是在 ChatGPT 调用 `list_roots` 并读取一个小片段。

核心模块按配置、安全句柄、浏览/搜索、文本和数据格式拆分；托盘、配置窗口、凭据、进程管理独立。增加格式解析器时接受已验证的文件句柄并限制读取量，不应绕过 `PathPolicy` 按用户字符串直接打开文件。构建缓存、本机配置、密钥、日志、测试产物和打包目录均不提交 Git。
