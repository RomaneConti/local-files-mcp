# 有界格式读取

统一流程是 `file_info` → `inspect_file` → 对应的结构化读取工具。路径始终是 root 别名下的相对路径；所有参数里的 selector/member 都是文件内部名称，不是新的磁盘路径。索引、offset/start 均从 0 开始；Excel 返回原始的 1-based 行号。JSON 响应超过 64 KiB 时请缩小范围。

## 格式和调用方式

| 格式 | 查看结构 | 读取与搜索 |
|---|---|---|
| `.xlsx/.xlsm` | `inspect_file` 列 sheets；selector 为 sheet 名 | `read_table(selector="Data", columns=["A","B"], offset=0, limit=20)`；`search_file` 搜索所选 sheet |
| `.parquet/.feather` | schema、列数、总行数、row group/batch 信息；offset/limit 分页列 | `read_table(columns=["time","value"], offset=100, limit=20)` |
| `.h5/.hdf5` | selector 为空列根 group；selector="group" 逐级列子项；dataset 显示 shape/dtype | `read_array(member="group/data", start=[10,0], shape=[2,4])`，或 flat offset/count |
| `.mat` | 列变量与 shape/dtype；支持传统 v4/v5 及 v7.3 HDF5 | `read_array(member="data", offset=10, count=20)` 或 start/shape |
| `.pdf` | 页数和分页 metadata | `read_document(start=5, limit=1)`；长页用 text_offset/max_chars；`search_file` 返回匹配页 |
| `.docx` | 正文块、标题和 table selector | `read_document(start=0, limit=5)`；`read_table(selector="table:0")`；`search_file` |
| `.pptx` | 有序 slide 列表 | `read_document(start=2, limit=1)`；表格 selector 例如 `slide:2/table:0`；`search_file` |
| `.sqlite/.sqlite3/.db` | 普通表列表；selector=表名获取 schema、列和有预算的 row count | `read_table(selector="events", columns=["id","message"], limit=20)`；可传 query="SELECT id FROM events ORDER BY id"，工具额外分页 |
| `.png/.jpg/.jpeg/.webp` | 尺寸/类型 | `read_image(max_dimension=1024)` 返回可直接视觉查看的 MCP image content |
| `.zip/.tar/.tar.gz` | 分页成员列表、大小和安全标记 | `read_archive_member(member="logs/run.txt", byte_offset=0, max_bytes=16384)`，仅 UTF-8 文本预览 |

普通 `.py/.cpp/.md/.json/.yaml/.toml/.csv` 等继续使用 `read_text/search_text`；JSON 结构化节点使用 `read_json`。不增加按扩展名排列的重复工具。NPY/NPZ 保留原有 `inspect_array/read_array` 接口。

## 限制与边界

- Office：直接以只读 ZIP/XML 流解析，不启动 Office，不求值公式，不保留/执行 VBA；公式返回文件已有缓存值，缺失缓存会标记。Excel offset 按实际存储的行计数，避免伪造 worksheet dimension 导致遍历海量空行；返回 row_number 对应工作表原始位置。数值日期可能保留 Excel serial，不模拟格式渲染。DOCX 只读正文，不合并页眉脚注；PPTX 只读文本和表格，不运行嵌入对象。超长文字会标记截断。
- OOXML/ZIP：文件最多 512 MiB，目录最多 4 MiB/4096 项，成员最多 32 MiB，声明解压总量最多 128 MiB，膨胀比上限 200（小于 1 MiB 的成员有宽限）；ZIP64、多卷、加密、非 store/deflate 压缩拒绝。XML 另有限定的节点、深度和读取预算；不是完整 Office 排版引擎。
- Parquet：仅请求列/行所在 row groups 分批解码，不默认加载整表。选中 row group 最多 32 MiB/100 万行，跨 group 每次最多 64 MiB；footer 最多 2 MiB。拒绝外部 column chunk、Arrow extension、嵌套/字典类型预览，避免隐式反序列化或不受控展开。
- Feather：支持 V2 IPC 的普通标量列，按 record batch 读取；batch 最多 32 MiB/100 万行，每次解码最多 64 MiB。压缩 buffer 在原生解码前校验声明展开长度；超大 batch 要在数据生成端拆小。V1、dictionary/extension/nested 类型拒绝。
- HDF5：每次最多 4096 元素/1 MiB 样本；压缩 chunk 最多 8 MiB，选区触及 chunk 的展开预算 32 MiB。逐级 group 浏览而非无限递归。拒绝 soft/external link、VDS、external raw storage、动态插件，以及 vlen/object/reference 内容；不加载任意 attributes。v7.3 MAT 显示 HDF5 存储的 shape/轴顺序，不猜测 MATLAB 转置规则。
- 传统 MAT：v4/v5 的数值、复数、逻辑及有界字符数组；flat 顺序为 Fortran。未压缩数组可直接 seek；压缩变量必须先有限解压，累计最多 16 MiB，过大压缩变量拒绝。cell/struct/sparse/object 等复杂变量仅提供可解析的元数据，不执行对象构造或 `scipy.loadmat` 整变量加载。
- PDF：文件最多 64 MiB、页面树最多 10000 项，每次最多 5 页；解压流最多 1 MiB，并限制页面操作数、表单展开和文字长度。只读文字层，不 OCR、不执行 JavaScript、附件、URI 或外部程序；加密文件拒绝。过大的单页内容也可能超限。长页通过 next_text_offset 继续，页面通过 next_offset 继续。
- SQLite：自定义 APSW VFS 只借用已验证句柄；连接使用 `SQLITE_OPEN_READONLY` 和 `mode=ro`，写操作及其他文件打开无实现。只支持独立、关闭/已 checkpoint 的 rollback-journal 数据库，WAL 及其 sidecar 不读取。SELECT 受 authorizer、函数白名单、3 秒/64 MiB 读取、列/单元格/SQL 大小限制；拒绝视图、虚拟/影子表、PRAGMA、ATTACH、DDL/DML、扩展加载及多语句。row count 过大也会超时拒绝；query 排序需用户显式 ORDER BY。
- 图片：源文件最多 16 MiB/1600 万像素，输出最长边 1536，编码后最多 512 KiB。转换为去除 metadata 的 JPEG 预览；PNG 只读首帧，动画 WebP 拒绝。WebP 在解码库分配画布前检查尺寸。返回 image block，而不是在文本里输出 base64；不进行 OCR。
- TAR：原始文件最多 512 MiB，gzip 展开最多 64 MiB，最多扫描 4096 成员；PAX/GNU 扩展头单个最多 64 KiB、累计 256 KiB、嵌套最多 32。链接、设备、稀疏成员不能读取，路径必须安全。读取成员时跳过的前缀最多 8 MiB，不落盘解压，不递归解析嵌套归档。
- `.pkl/.pickle/.joblib/.pt/.pth` 默认拒绝内容解析，连同同名 gzip/归档成员。模型权重后续若扩展，应单独设计 `.safetensors` 有界读取。

所有搜索均为字面匹配。`complete`、`scope`、截断标记和 `next_offset` 是结果的一部分：预算内没有命中不等于整个文件没有匹配。结构化工具遇到超限时返回明确错误，不退回全文件读取。

## 依赖和实现依据

- [MCP Python SDK Media](https://py.sdk.modelcontextprotocol.io/servers/media/)：从内存 bytes 创建 `Image`，输出协议 `ImageContent`，不使用 path 重开文件。
- [APSW VFS](https://rogerbinns.github.io/apsw/vfs.html) 和 [Connection.readonly](https://rogerbinns.github.io/apsw/connection.html)：自定义句柄 I/O 与只读连接检查。
- [h5py file-like objects](https://docs.h5py.org/en/stable/high/file.html#python-file-like-objects)：关闭 HDF5 对象后再归还外层句柄；数据切片有单独预算。
- [Arrow ParquetFile](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetFile.html) 和 [IPC 格式](https://arrow.apache.org/docs/format/Columnar.html#ipc-file-format)：row group/batch 和压缩 buffer 元数据预检。
- [pypdf Configuration](https://pypdf.readthedocs.io/en/stable/_modules/pypdf/_configuration.html)：按请求上下文限制解压、页面树和表单展开。
- [Pillow 文件处理](https://pillow.readthedocs.io/en/stable/reference/open_files.html)：只传借用的文件对象，尺寸/文件大小预检后生成有界预览。

正式测试通过内存或项目内临时目录生成真实文件，覆盖正常读取、分页、损坏、限额、危险引用和只读边界；`tests/test_format_transport.py` 同时支持源码服务和 PyInstaller EXE 的真实 HTTP 验证，并比较所有输入文件的 SHA-256。

2026-09-28 Windows x64 / Python 3.14.6 本机验收：完整测试 388 passed、2 skipped（平台不支持的 extended-precision dtype、账户无法创建的 symlink）。包含源码及实际 EXE 对所有新增格式的 HTTP 调用、原生图片 content 和输入哈希不变。真实托盘自动启动、Tunnel control-plane 就绪、重复实例、停止、重启、退出与端口释放均通过。这不替代 ChatGPT 对话中的云端端到端调用。
