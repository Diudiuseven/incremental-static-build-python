# 增量静态生成与原子发布（Python）

源内容变更后只重建受影响的页面，并且对外始终暴露一份完整、自洽的站点版本。
单个页面构建失败、超时或进程中断都不会污染已发布版本；重复编辑、乱序到达、
批量合并与并发提交的最终结果，与一次全量重建完全一致。

## 目录模型

```
site/
  content/            页面与页面引用的数据
    index.md          -> /index.html
    guides/a.md       -> /guides/a/index.html
    data/site.json    被模板以 {{data:data/site.json:name}} 引用
  templates/          模板（.html 可省略后缀）
    page.html         默认模板
    partials/h.html   公共片段
  static/             原样发布的资源（保持目录结构）
```

模板语法（见 `isg/render.py`）：

- `{{template:NAME}}`：页面首行声明使用的模板（缺省为 `page`）
- `{{include:partials/h.html}}`：递归包含公共片段（带环检测）
- `{{content}}`：页面 Markdown 正文（支持标题、段落、无序列表）
- `{{data:data/x.json:path.to.field}}`：注入 `content/` 下的 JSON 数据（HTML 转义）

渲染时读取到的每个页面、模板、片段、数据文件都会登记为该页面的依赖。

## 一致性语义

### 1. 增量重建，结果等同全量

每次构建不依赖“变更通知”本身，而是把上一版清单与**当前源树**交叉校验：

1. 源文件哈希变化/删除 → 命中其直接来源或依赖图的页面标记为脏；
2. 清单中每个页面按其全部依赖的当前哈希重算指纹，与记录不符即重建；
3. 源树中新增、清单缺失的页面补建；源文件已删除的页面产物删除。

因此无论编辑是重复、乱序、被合并还是部分丢失，最终产物都只取决于源树最终状态，
与全量重建逐字节一致（有专门测试覆盖两条路径的产物等价性）。

### 2. 依赖追踪

清单 `manifest.json` 记录每个页面的产物指纹与有序去重依赖列表：

```json
{
  "schema": 1,
  "release_id": "20261006T...",
  "pages":   { "guides/a": "<fingerprint>", "": "<fingerprint>" },
  "deps":    { "guides/a": ["content/guides/a.md",
                            "templates/page.html",
                            "templates/partials/h.html"] },
  "assets":  { "static/app.css": "<hash>" }
}
```

模板、公共片段、数据文件、资源任意一个变化，都会使依赖它的页面（或资源本身）过期。

### 3. 原子发布，读取方永不看到半成品

```
输出根/
  releases/<release_id>/   不可变的完整版本（含 manifest.json）
  releases/.staging-*/     正在组装的私有暂存目录
  current -> releases/<id> 相对符号链接
```

- 构建只写自己的暂存目录，并先从上个完整版本硬链接复制出整站（未变文件零拷贝）；
- 全部页面渲染成功、`manifest.json` 落盘后，目录整体改名就位，再用
  `os.symlink + os.replace` **原子切换** `current`；
- 读取方跟随 `current`，只能看到旧版或新版，不会看到混合版本、半成品或短暂不可用；
  新版本构建期间旧版照常服务。

### 4. 失败与中断恢复

- 单页失败/超时：该页记录到构建结果，整版**不发布**，暂存目录整体删除，
  `current` 保持指向上一版，已发布目录不会出现部分写入。
- 重试是幂等的：渲染先写 `*.part` 再 `os.replace`；计划阶段按当前源树重算，
  不会重复应用产物，也不会跳过任何与清单不一致的页面。
- 进程崩溃后 `start()` 执行恢复：删除残留暂存目录、`current.tmp`、无清单的
  半改名目录以及未发布的“更新”版本；`current` 指向的版本原样保留。

### 5. 删除清理

源页面删除 → 对应 `.../index.html`、空叶子目录、清单 `pages/deps` 条目一并清除；
`static/` 资源删除同理。发布后默认立即回收所有不再被引用的旧版本
（`keep_releases` 可保留最近 N 个），历史资源不会无限残留。

### 6. 并发编辑与并发构建

- 引擎用一把锁串行化“应用编辑 + 扫描 + 计划 + 发布”整段过程，提交结果只取决于
  源文件最终内容（多编辑者压测有确定性测试）。
- 页面渲染在线程池中并发执行，受 `concurrency` 限制；每页独立超时，超时后**立即
  释放逻辑槽位**并继续派发无关页面，慢任务不会拖住其他页面（超时渲染线程被
  detach，迟到结果通过状态 CAS 丢弃，绝不记账两次）。

### 7. 批次、并发、超时与全量回退

| 配置项 | 默认 | 含义 |
| --- | --- | --- |
| `concurrency` | `4` | 单次构建内的最大并发渲染页面数 |
| `page_timeout` | `10.0` | 单页渲染超时秒数；超时页标记 `timed_out`，不阻塞其他页面 |
| `batch_size` | `100` | 增量构建允许合并的脏页/变更上限，**超过**即回退全量重建 |
| `full_rebuild_threshold` | `0.5` | 脏页占总页数比例严格**大于**该值时回退全量（等于不回退） |
| `keep_releases` | `0` | 发布后保留的旧版本数量，其余自动 GC |

回退路径与增量路径都从同一份源树构建同一份计划目标，产物完全一致。

## HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /...` | 从当前发布版本读取文件与目录索引（`a/` → `a/index.html`） |
| `POST /__admin/edits` | 提交一批 `{path, op, content}`（`op` 为 `upsert`/`delete`），同步构建并发布 |
| `POST /__admin/rebuild` | 强制全量重建 |
| `GET /__admin/status` | 当前 release id、上次构建结果、回退原因 |

构建存在失败/超时时编辑接口返回 `409`，响应体给出 `ok/failed/timed_out` 页面，
站点读取不受影响；非法路径（如 `../x`）返回 `400`。

## 使用方式

```bash
# 直接运行（读取 ./site，输出到 ./.build）
python3 main.py
# 或用 uv
uv run python main.py

# 自定义
ISG_SOURCE_DIR=site ISG_OUTPUT_DIR=.build \
ISG_CONCURRENCY=8 ISG_PAGE_TIMEOUT=2 ISG_BATCH_SIZE=200 \
ISG_FULL_REBUILD_THRESHOLD=0.6 ISG_KEEP_RELEASES=2 \
python3 main.py
```

代码内直接使用：

```python
from isg.engine import Engine
from isg.config import Config
from isg.model import Edit, Op

engine = Engine("site", ".build", Config(concurrency=8, page_timeout=2))
engine.start()  # 崩溃恢复 + 首次构建
outcome = engine.submit_edits([
    Edit("content/news.md", Op.UPSERT, b"# News"),
    Edit("static/app.css", Op.DELETE),
])
print(outcome.success, outcome.ok, outcome.failed, outcome.timed_out)
print(engine.site_path())  # 当前完整版本的绝对路径
```

## 测试

```bash
python3 -m pytest -q
```

覆盖：重复/乱序/批量编辑收敛、模板与数据依赖失效、全量阈值与批次回退的
产物等价性、单页失败隔离、超时不阻塞、崩溃恢复、删除产物与旧版本 GC、
多编辑者并发确定性，以及完整 HTTP 接口行为。
