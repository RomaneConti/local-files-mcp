# ChatGPT 与 Secure MCP Tunnel 接入

本项目使用官方 Secure MCP Tunnel。连接顺序是 ChatGPT → OpenAI 隧道 → 本机 tunnel-client → 回环 MCP 服务；不需要公网监听或入站端口映射。电脑和服务需要在线；配置了代理时，代理也需要在线。

## 1. 创建 Tunnel 和 runtime key

打开 [Platform Tunnels](https://platform.openai.com/settings/organization/tunnels)，选择目标组织，创建 Tunnel 并记录 ID。关联实际使用的 ChatGPT 工作区；只关联 Platform 组织不保证它出现在目标工作区。

创建或管理 Tunnel 需要 `Tunnels Read + Manage`；运行客户端、在 ChatGPT 选择 Tunnel 需要 `Tunnels Read + Use`。缺少权限时，请组织所有者或 RBAC 管理员授予。

通过 [Platform API keys](https://platform.openai.com/settings/organization/api-keys) 准备 runtime API key，其主体需要运行权限。不要使用 Admin API key 作为常驻客户端凭据。只在本机首次配置的隐藏输入框中输入密钥。

这些权限独立于 ChatGPT Developer Mode；请分别核对 Platform 组织和目标 ChatGPT 工作区。依据：[官方 Tunnel 文档](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。

## 2. 构建和首次配置

按照 [README](../README.md) 运行 `build.ps1`，双击 `dist\LocalFilesMCP\LocalFilesMCP.exe`。选择允许读取的目录、填写 Tunnel ID、可选 HTTP/HTTPS 代理和 runtime key，保存后等待两个服务就绪。

默认配置存放在项目的 `local-config`；独立移动程序目录后则存放在 EXE 旁的 `local-config`。不写入 C 盘用户目录，且不进入 Git 或构建包。runtime key 使用 Windows DPAPI 加密，后续自动读取。构建时无需填写这些信息。托盘“设置 / 允许目录”用于修改配置；“设置 / 更新 runtime key”用于更新凭据。

若使用 Clash，可填自己的 mixed/HTTP 监听地址；例如 `http://127.0.0.1:7897`。无需代理时留空。只将代理传给官方 `--control-plane.http-proxy`，本地 MCP 和健康检查直连回环地址。[官方代理配置](https://github.com/openai/tunnel-client/blob/v0.0.15/docs/configuration.md#outbound-http-proxy)

## 3. 在 ChatGPT 创建连接

按照 [官方连接步骤](https://developers.openai.com/plugins/deploy/connect-chatgpt)：

1. 在 ChatGPT 的 Settings → Security and login 启用 Developer mode；入口取决于账户与工作区策略。
2. 打开 [ChatGPT Plugins](https://chatgpt.com/plugins)，点击加号创建连接。
3. Connection 选择 **Tunnel**，选择已有 Tunnel 或填入对应 ID。不要把本机 `127.0.0.1` 当作公开远程服务地址。
4. 创建连接并检查发现的工具。新建普通对话，选中该连接，然后请求“列出允许的根目录”，再读取一个小片段。

更新 MCP 工具定义后，需要更新 ChatGPT 保存的连接元数据，重启 EXE 不会自动完成这一步：

1. 启动最新 EXE，等托盘 MCP 和 Tunnel 就绪。
2. 打开 [ChatGPT Plugins](https://chatgpt.com/plugins)，进入现有 Local Files 连接，选择 **Refresh**。
3. 确认连接发现了 15 个工具，包括 `inspect_file`、`read_table`、`read_document`、`search_file`、`read_image`、`read_archive_member`。
4. 新建对话并选中该连接，再测试读取。旧对话可能仍使用旧的工具定义。

这是 [OpenAI 官方 Refresh metadata 流程](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)。仅修改允许目录不需要重新打包或刷新工具定义。

如果 `file_info` 已推荐新工具，而 ChatGPT 声称这些工具不存在，先区分服务端注册与连接元数据。托盘状态和 `launcher.log` 会显示 MCP 协议握手返回的服务版本及实际工具数量，`list_roots` 也返回 `server_version`。可在完成构建的源码目录检查真实 HTTP 服务：

```powershell
.\.venv\Scripts\python.exe -B scripts\smoke-test.py --url http://127.0.0.1:8765/mcp --list-tools
```

此命令直接调用 MCP `initialize` 和 `tools/list`，不读取文件内容。若本地为 15 个，而 ChatGPT 只有旧的 9 个，按上面的 Refresh 流程操作；若本地也缺工具，则检查是否启动了旧 EXE 或其他环境的入口。版本号用于诊断，不能替代 ChatGPT 元数据刷新。

`file_info.reader` 表示建议首先调用的 MCP 工具；`readers` 表示检查文件结构后可继续调用的读取/搜索工具。例如 Excel 先 `inspect_file` 获取 sheet，再 `read_table` / `search_file`；PDF 先 `inspect_file` 获取页数，再 `read_document` / `search_file`。这些名称均是公开的 MCP tools，不是内部 dispatcher 名称。

## 排查

- **代理不可达：**启动代理或在托盘设置中更正地址，然后重启。
- **control-plane network_error：**检查网络和代理规则。代理端口可连接不代表 OpenAI 可连接。
- **401/403：**核对 runtime key、组织、Tunnel ID 和运行权限。
- **ChatGPT 看不到 Tunnel：**核对工作区关联、平台权限与 Developer Mode 权限。
- **端口占用：**关闭自己之前运行的服务，或修改本机配置中的端口；程序不会杀死其他进程。
- **MCP 失败：**检查允许目录是否存在、是否为普通本地目录，查看状态窗口与脱敏日志。

日志可通过托盘打开，分为 launcher、mcp、tunnel，单文件 1 MiB，最多 3 个轮转备份。管理页默认 `http://127.0.0.1:8766/ui`。`/healthz` 和 `/readyz` 成功并不保证 control plane 连通；本项目还检查成功轮询记录。[官方健康接口](https://github.com/openai/tunnel-client/blob/v0.0.15/docs/health.md)

点击“停止”保留托盘，点击“退出”结束它启动的全部服务。不要为解决连接问题放宽 MCP 目录安全边界或公开回环端点。
