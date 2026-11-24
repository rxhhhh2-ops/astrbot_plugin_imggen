# 绘影生图（astrbot_plugin_imggen）

[![Tests](https://github.com/rxhhhh2-ops/astrbot_plugin_imggen/actions/workflows/tests.yml/badge.svg)](https://github.com/rxhhhh2-ops/astrbot_plugin_imggen/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

在 QQ（以及 AstrBot 支持的任意平台）上用**一句话生成图片**的 AstrBot 插件。

- 30 种内置风格：二次元、国风水墨、像素、赛博朋克、写实摄影、海报、信息图、3D、绘本、浮世绘……
- 一句话触发：`/画 一只戴墨镜的橘猫`，或直接发「画一张……」「帮我画……」
- 图生图：QQ 直接附图 + 工作台上传参考图，走 OpenAI 兼容 `/images/edits`（gpt-image-1 / dall-e-2 / 支持 edits 的中转站）
- 三种服务商：OpenAI 兼容接口（OpenAI / 硅基流动 / 智谱 / 中转站）、Google Gemini、阿里云百炼 DashScope；另带离线 mock 占位图
- 面向“稳定”设计：并发闸门、冷却、每日额度、超时重试、备用服务商自动降级、图片字节校验、磁盘自动清理
- 可选 AI 工具：让大模型在用户要求作图时自动调用生图并直接把图发给用户

---

## 一、安装

把整个 `astrbot_plugin_imggen` 目录放进 AstrBot 的插件目录，然后在 WebUI 的插件页重载插件。

```bash
cd AstrBot/data/plugins
git clone https://github.com/rxhhhh2-ops/astrbot_plugin_imggen.git
```

也可以从 [Releases](https://github.com/rxhhhh2-ops/astrbot_plugin_imggen/releases) 下载 zip，解压后把 `astrbot_plugin_imggen` 目录放进 `AstrBot/data/plugins/`。

```
AstrBot/
└── data/plugins/
    └── astrbot_plugin_imggen/
        ├── main.py
        ├── metadata.yaml
        ├── _conf_schema.json
        ├── core/
        └── tests/
```

本插件**不引入额外第三方依赖**：网络请求用 AstrBot 自带的 `aiohttp`，占位图用自带的 `Pillow`。

## 二、配置

WebUI → 插件 → 绘影生图 → 配置。

### 2.1 先用 mock 试玩（不联网、不花钱）

| 配置项 | 值 |
| --- | --- |
| 服务商 `provider` | `mock` |

发 `/画 一只猫`，会得到一张带尺寸文字的渐变占位图。确认命令、权限、出图链路都通后，再换成真实服务商。

### 2.2 接入真实服务商

**OpenAI 兼容接口（推荐，覆盖面最广）**

| 配置项 | 示例 |
| --- | --- |
| `provider` | `openai` |
| `api_key` | `sk-...` |
| `base_url` | 官方留空；中转站填如 `https://api.siliconflow.cn/v1` |
| `model` | `gpt-image-1` / `dall-e-3` / `cogview-4` / `flux.1-schnell` 等 |

**Google Gemini**

| 配置项 | 示例 |
| --- | --- |
| `provider` | `gemini` |
| `api_key` | Google AI Studio 的 Key |
| `base_url` | 留空（默认 `https://generativelanguage.googleapis.com/v1beta`） |
| `model` | `gemini-2.5-flash-image` |

**阿里云百炼 DashScope**

| 配置项 | 示例 |
| --- | --- |
| `provider` | `dashscope` |
| `api_key` | 百炼 API Key |
| `base_url` | 留空（默认 `https://dashscope.aliyuncs.com/api/v1`） |
| `model` | `qwen-image-plus` / `wanx2.1-t2i-turbo` 等 |

> 密钥也支持环境变量兜底：`IMGGEN_API_KEY`、`IMGGEN_BASE_URL`、`IMGGEN_MODEL`；备用服务商对应 `IMGGEN_BACKUP_*`。

以上连接参数也可以不开配置文件：在 WebUI 工作台右上角点「设置」即可更换服务商、模型、接口地址与密钥，保存后立即生效并写回插件配置。

### 2.3 建议开启的稳定性开关

| 配置项 | 建议 | 说明 |
| --- | --- | --- |
| `backup_enabled` | 开 | 主服务超时/5xx/限流时自动切换备用（备用可设 `mock` 兜底） |
| `cooldown_seconds` | 15 | 同一用户冷却，防连点 |
| `daily_limit` | 30 | 每人每日次数（0=不限） |
| `max_concurrency` | 2 | 全局并发，防止触发服务商限流；同时也是 AI 工具后台 worker 数量 |
| `request_timeout` | 180 | 慢模型可调大 |
| `max_retries` | 2 | 只对网络错误/5xx/429 重试，4xx 不重试 |
| `openai_response_format` | auto | 图片返回格式：auto 优先 `url`（JSON 只有几百字节，避免大 base64 被截断），失败自动退回 `b64`；gpt-image 官方固定 b64，显式选 `url` 才会额外试探 |
| `keep_days` / `max_files` | 7 / 2000 | 本地图片自动清理兜底 |

## 三、WebUI 工作台

插件自带可视化页面 `pages/studio/`（AstrBot 同时兼容 `views/` 与 `pages/` 布局）。在 WebUI 打开插件详情页，选择「绘影生图工作台」即可：

- 输入画面描述，选择风格 / 比例 / 张数，点「开始绘制」直接出图；
- 上传**参考图**（PNG / JPEG / WebP，≤10MB）后按图生图处理，结果区会标注「图生图」；
- 右上角「设置」可更换**绘图服务商、模型、接口地址、API Key**，配置备用服务商，以及默认风格 / 比例 / 张数，保存后立即生效；
- API Key 只写不读：页面只显示 `sk-****1234` 形式的掩码，留空表示不修改，也可勾选清空；
- 结果区展示缩略图、尺寸、服务商与耗时，可一键下载原图；
- 「最近生成」展示历史图片，点击即可下载；
- 顶部状态条显示服务商 / 备用 / 风格数 / 存储占用 / 当前队列；
- 「重载配置」等价于 `/画重载`，「清理旧图」等价于 `/画清理`（二次点击确认）。

工作台走 Dashboard 登录态鉴权，只有登录用户能访问；生图只受全局并发闸门约束，不占用 QQ 用户的冷却与每日额度。页面后端接口（均以 `astrbot_plugin_imggen/` 为前缀注册）：

| 接口 | 方法 | 作用 |
| --- | --- | --- |
| `state` | GET | 服务商、风格、配额、存储与最近图片 |
| `settings` | GET | 读取可编辑设置（密钥只返回掩码） |
| `save_settings` | POST | 保存服务商 / 模型 / 密钥 / 默认值并重载 |
| `generate` | POST | 生成并落盘，返回缩略图与元信息 |
| `history` | GET | 最近生成列表 |
| `thumb` | GET | 单张缩略图（data URI） |
| `image` | GET | 下载原图（仅允许访问存储目录内文件名） |
| `cleanup` | POST | 按保留策略清理 |
| `reload` | POST | 重读配置与自定义风格 |

> 若 AstrBot 版本不支持插件页面，工作台不会显示，但 QQ 内的 `/画` 等命令不受影响。

## 四、使用

### 3.1 命令

```
/画 <描述>                     自动匹配风格
/画 <风格> <描述>              指定风格
/画 赛博朋克 雨夜街景 --比例 16:9 --n 2
/画 <描述> + 附图              图生图（OpenAI 兼容）
/画 帮助
/画风                          查看全部风格
/画风 详情 国风水墨             查看该风格的提示词模板
/画状态                        查看服务商/并发/额度/本地存储
/画任务                        查看自己的 AI 生图后台任务
/画取消 <编号|全部>            取消排队中/生成中的任务
/画清理                        管理员：立即清理旧图
/画重载                        管理员：重读配置与自定义风格
```

- **图生图**：把图片和 `/画 改图要求` 一起发送即可（QQ 适配器需要把图片放在同一条消息里）；只发图不写描述时，会用默认的“保持主体与构图、提升细节质感”提示词。
- 图生图目前只支持 **OpenAI 兼容**服务商（`gpt-image-1`、`dall-e-2`、以及支持 `/images/edits` 的中转站）和 mock；Gemini / DashScope 当前仅文生图，会提示“不支持图生图”。
- 参考图支持 PNG / JPEG / WebP，最大 10MB；用 `mock` 时可离线验证整条图生图链路。
- 风格可以写在描述前面（`/画 二次元 猫耳少女`），也可以写 `--风格 anime`。
- 比例支持 `1:1`、`4:3`、`3:4`、`16:9`、`9:16`、`3:2`、`2:3`、`21:9`，也可写 `横图/竖图/方图`。
- 不写风格时先看描述里的关键词（“水墨”“像素”“海报”等），再回落到默认风格。
- `--n` 受 `max_images` 限制（默认最多 4 张）。

别名：`/绘画`、`/画图`、`/生图`、`/draw`、`/imagine`、`/绘制`。

### 3.2 关键词自动触发

默认开启，消息以这些词开头即出图（可在配置里改）：

```
画一张 画一幅 画个 画一个 画张 画只 帮我画 给我画
帮我生成 给我生成 生成一张 生成一幅 生成图片 生成图
生图 绘制一张 绘制一个
```

触发后会阻止消息继续传给大模型，避免“又出图又聊天”。

> 群聊里被 @ 之后的前缀可能影响匹配，此时建议直接发 `/画 ...` 命令。

### 3.3 AI 自动调用（llm_tool）

开启 `tool_enabled` 后，大模型可以调用：

- `generate_image(prompt, style, aspect_ratio, num_images)`：生成图片并通过会话主动发送；工具会**立即返回**“已开始生成”，图片在后台完成后再发到会话（避开 AstrBot 工具调用的 120 秒硬超时）；
- `list_image_styles()`：列出风格与 id。

模型偶尔会自造参数名（如 `ratio`、`size`、`quality`、`seed`），插件会尽量映射或安全忽略，不会因为多一个参数就抛 `TypeError`；缺少 `prompt` 时也会返回提示而不是报错。

工具不会等待出图完成：参数校验后立即返回“已开始生成”，任务进入常驻后台 worker，完成后主动发图。因此即使模型或中转站很慢，也不会出现 `tool generate_image execution timeout after 120 seconds`；等待超过 60 秒会收到“仍在生成中”的进度提示，后台失败或超时会直接把可读原因发回会话。

适合“帮我画个公众号封面”这类没有用命令的对话。

## 五、内置风格

| 分类 | 风格 |
| --- | --- |
| 通用 | 通用插画 |
| 二次元 | 二次元、Q版萌系、黑白漫画 |
| 国风 | 国风水墨、国潮插画、浮世绘 |
| 摄影 | 写实摄影、人像写真、美食摄影、产品图 |
| 绘画 | 油画、水彩、素描线稿、儿童画 |
| 设计 | 扁平插画、极简、海报设计、信息图、标志图标、表情包 |
| 3D/潮玩 | 3D渲染、黏土手办 |
| 风格化 | 赛博朋克、蒸汽波、霓虹灯管、蒸汽朋克、暗黑奇幻、像素风 |
| 场景 | 风景壁纸、概念设定、游戏场景 |

每个风格都带提示词前缀/补充词/负面词和默认比例，可用 `/画风 详情 <名字>` 查看。

### 自定义风格

在插件数据目录放一个 `styles.json`（路径见 `/画状态` 里的“存储目录”的上一级）：

```json
{
  "styles": {
    "my_style": {
      "name": "我的风格",
      "category": "自定义",
      "summary": "一句话介绍",
      "prefix": "风格化的提示词前缀",
      "suffix": "补充描述",
      "negative": "不要出现的东西",
      "ratio": "16:9",
      "aliases": ["我的", "mys"]
    }
  },
  "override": {
    "guofeng": {"prefix": "覆盖内置水墨风格的前缀"}
  },
  "disable": ["ukiyoe"]
}
```

保存后执行 `/画重载`（管理员）或重启插件。格式出错只会记录日志，不影响内置风格。

插件根目录附带了 `styles.example.json`，可直接复制到数据目录并改名为 `styles.json` 作为起点。

## 六、稳定性设计

| 风险 | 处理 |
| --- | --- |
| 用户连点/刷图 | 同用户并发拒绝 + 冷却 + 每日额度 |
| 人多排队 | 全局并发闸门 + 排队上限 + 排队超时 |
| 服务商抖动 | 5xx/429 退避重试、备用服务商降级 |
| 生成请求超时 | **不重试**（避免同一张图重复扣费），并计入服务商熔断 |
| 服务商连续失败 | 连续 3 次可重试失败后熔断约 120 秒，期间直接走备用或给出提示 |
| AI 工具长任务 | 常驻 worker 池（大小 = `max_concurrency`）+ 队列；`/画任务` 查看、`/画取消` 取消；超过 60 秒有进度心跳 |
| 4xx 参数/审核错误 | 不重试，避免重复计费，直接给出可读原因 |
| 异常返回 | 魔数校验，非 PNG/JPEG/WebP/GIF 一律丢弃 |
| 图生图参考图 | 仅接受 PNG/JPEG/WebP、≤10MB；链接/本地文件/`base64://` 都做格式与体积校验 |
| 超大图片/响应 | 下载与响应体均有 MB 上限 |
| 磁盘写满 | 按保留天数 + 最大文件数自动清理 |
| 密钥泄漏 | 日志与错误信息中自动打码，配置项标记为 secret |
| 阻塞事件循环 | 文件读写/清理走线程池 |
| 配置写错 | 全部带默认值与区间裁剪，缺失时给出配置指引而非报错 |

每日额度与冷却计数持久化在插件数据目录的 `usage.json`：AstrBot 重启后仍保留当天额度与冷却；失败请求会退回额度。

## 七、常见问题

**Q：提示“还没有可用的生图服务”？**
配置不完整：`provider`、`api_key`、`model` 至少要齐（`base_url` 可留空用默认）。也可以先设 `provider=mock` 试玩。

**Q：401 / 403？**
Key 不对、未开通该模型，或中转站要求不同的鉴权方式。核对 `base_url` 是否包含 `/v1`。

**Q：404？**
模型名或接口路径不对。OpenAI 兼容模式会把请求发到 `{base_url}/images/generations`；`base_url` 已写到该路径时插件不会重复拼接。

**Q：图片一直不出来 / 超时？**
调大 `request_timeout`；确认服务器能访问服务商；中文提示词过长时会被截断（`max_prompt_chars`）。

**Q：一直提示“服务商返回的数据不完整 / 不是合法 JSON”？**
v1.0.4 已修复最主要的根因：此前读取响应体只取了**第一个网络数据块**（常见 7KB 左右），2MB+ 的 base64 图片 JSON 会被误判为“截断”。现在会循环读满整个响应体（上限 32MB），JSON 解析与图片下载两条路径都已修正。

在此之上仍保留这些兜底：

1. **优先用 `url` 返回**（`openai_response_format=auto` 默认）：JSON 只有几百字节，基本不会被中转站/代理截断；站点不支持时会自动退回 `b64`；
2. 去 BOM/NUL 杂字节、按最后一个完整对象解析；
3. **图片数据已完整、只缺 JSON 结尾引号/括号**时，直接按 PNG/JPEG 结构校验后恢复图片；
4. 真正截断时自动重试一次，并切换到另一种返回格式。

错误信息会给出**收到字节数 / 声明字节数**：

- “连接提前结束（收到 < 声明）”：链路（中转站、本地代理、CDN）在传输中关闭了连接，建议重试、换 `base_url` 或直连；
- 收到 = 声明但仍非法：服务商本身返回了坏 JSON；
- 还可以把 `openai_response_format` 显式设为 `url`（gpt-image 接中转站时也可试），或调小 `custom_size` / 降低模型画质以减少响应体积。

**Q：AI 生图一直显示生成中怎么办？**
发送 `/画任务` 查看排队/生成中的任务和编号，`/画取消 <编号>` 可立即取消；`/画取消 全部` 取消自己的全部任务。连续失败的服务商会自动熔断约 120 秒，`/画状态` 能看到熔断剩余时间，稍后重试或切换到备用即可。

**Q：提示 `tool generate_image execution timeout after 120 seconds`？**
v1.2.2 起改为「立即返回 + 常驻 worker 后台生成」：工具参数校验后马上返回“已开始生成”，不再等待出图，因此不会被 AstrBot 的 120 秒工具超时掐断；任务由插件启动时创建的常驻 worker 处理，不会随单次工具调用被取消。等待超过 60 秒会收到进度提示，生成完成后图片主动发到会话；失败或超过总预算（约 3~7 分钟）会收到明确原因。

**Q：图生图怎么用？为什么提示不支持？**
在 QQ 里把图片和 `/画 改图要求` 放在同一条消息里发送，或在工作台「参考图」处上传。当前仅 **OpenAI 兼容**服务商接入 `/images/edits`（gpt-image-1 / dall-e-2 / 支持 edits 的中转站），Gemini 与 DashScope 仍是纯文生图。参考图支持 PNG / JPEG / WebP、≤10MB；若服务商返回 404/400，说明该模型不支持图片编辑，换模型或换服务商即可。

**Q：WebUI 里看不到「绘影生图工作台」？**
确认插件已重载（新增页面目录后需要重载插件）；再确认 AstrBot 版本支持插件页面（`views/` 或兼容的 `pages/` 布局）。若页面打开后提示未注入 bridge SDK，说明版本过旧，升级 AstrBot 即可；QQ 内的 `/画` 命令不受影响。

**Q：Gemini 一次要很久？**
Gemini 原生接口没有 `n` 参数，多张会按张数**串行**请求，属正常现象。

**Q：群里图片发不出去？**
插件先落盘再发送，失败会退回单图发送；若平台限制条数/体积，可把 `default_images` 设为 1。

**Q：额度什么时候重置？**
每日额度按自然日计算，进程重启会清零。

## 八、测试

全部离线（mock 服务商 + 本地 aiohttp 假服务器），不需要真实 Key：

```bash
python tests/run_all.py
```

覆盖：

- `test_core.py`：风格解析/自定义风格、提示词清洗、比例尺寸换算、落盘校验与清理、冷却/额度/并发、额度持久化、熔断器、配置裁剪
- `test_providers.py`：OpenAI b64/URL/重试/401 不重试/密钥打码、图生图 multipart 与格式回退、Gemini imageConfig 降级、DashScope 轮询与失败、空响应防御
- `test_harness.py`：假 AstrBot 驱动 `/画`、风格指定、参数、帮助、`/画风`、`/画状态`、关键词触发、防重复触发、llm_tool、任务列表/取消、服务商熔断、黑名单、冷却、额度、群聊开关、缺配置指引、备用降级、图生图（QQ 附图 / 工作台上传 / 非法参考图 / 不支持服务商）、生命周期；以及 9 个界面接口（state / settings / save_settings / generate / history / thumb / image / cleanup / reload）与路径穿越防护

## 九、目录结构

```
astrbot_plugin_imggen/
├── main.py                  # 命令 / 关键词触发 / llm_tool / 生命周期
├── metadata.yaml
├── _conf_schema.json        # WebUI 配置
├── pages/studio/            # WebUI 工作台（AstrBot 亦兼容 views/ 布局）
│   ├── index.html
│   ├── app.js
│   └── style.css
├── .astrbot-plugin/i18n/    # 页面标题与描述（zh-CN / en-US）
├── core/
│   ├── config.py            # 配置校验与运行时设置
│   ├── styles.py            # 30 种内置风格 + 自定义风格 + 关键词识别
│   ├── prompt.py            # 提示词清洗/拼装/命令剥离
│   ├── sizes.py             # 比例 → 各服务商尺寸
│   ├── store.py             # 落盘、魔数校验、自动清理
│   ├── limits.py            # 冷却/额度/并发/排队
│   └── providers/
│       ├── base.py          # 错误类型、HTTP 重试、下载限流
│       ├── openai_compat.py # OpenAI 兼容
│       ├── gemini.py        # Gemini 原生
│       ├── dashscope.py     # 百炼异步任务
│       └── mock.py          # 离线占位图
└── tests/
    ├── fake_astrbot/        # 假 AstrBot API
    ├── test_core.py
    ├── test_providers.py
    ├── test_harness.py
    └── run_all.py
```

## 十、隐私

- 提示词与你上传的描述只会发送给**你配置的服务商**；
- 生成的图片保存在插件数据目录，按保留策略自动删除；
- API Key 不会出现在回复、日志或错误信息中。

## 十一、参考与致谢

- 早期设计参考了 [shanliuling/dsh-image-gen](https://github.com/shanliuling/dsh-image-gen)（Apache-2.0）的多风格、多服务商思路；本项目为 AstrBot 插件形态的独立实现，未复制其代码。

## 十二、许可

[MIT](LICENSE)

## 更新记录

### v1.3.4

- `metadata.yaml` 补充 `astrbot_version: ">=4.16,<5"` 与 `support_platforms` 声明，符合插件市场元数据规范；
- README 增加参考项目致谢；全仓库代码已用 `ruff format` 统一格式；
- 离线测试维持 257 项（core 76 / providers 66 / harness 115），全部通过。

### v1.3.3

- **日志合规修复（上架审核）**：`core/limits.py`、`core/providers/base.py`、`core/providers/openai_compat.py`、`core/styles.py` 统一改为 `from astrbot.api import logger`，移除 `import logging`、`logging.getLogger(...)` 及测试回退 logger；全仓库不再使用 Python 内置 `logging` 模块；
- 离线测试维持 257 项（core 76 / providers 66 / harness 115），全部通过。

### v1.3.2

- **修复 Linux 容器/新机器上首次请求被误判冷却**：`UsageLimiter` 原先用 `0.0` 作为“没有上次请求”的哨兵，而 `time.monotonic()` 在刚启动的容器里可能只有几十秒，导致 `60 - 30` 被算成“还在冷却”。现在改用 `None` 判断“从未请求”，Docker 部署下第一次 `/画` 不会再被拦；
- 新增低 uptime 回归测试（模拟 `monotonic=5.0`）；离线测试增至 257 项（core 76 / providers 66 / harness 115）。

### v1.3.1

- **修复关键词触发“没有任何回复、服务商零请求”**：群聊/私聊发送“画一张…”“帮我画…”“生图…”时，旧实现在生成开始前调用 `event.stop_event()`；AstrBot 调度器一旦看到事件已停止，会在第一个进度 `yield` 处丢弃事件链，生成器被挂起，后续 `_create_images()` 根本不会执行。现在改用 `event.should_call_llm(True)` 禁止默认 LLM 请求，不再阻断插件自己的结果发送；
- 增加模拟 AstrBot 调度语义的回归测试（`show_progress=True` 时进度消息先于服务商请求），防止同类“先 stop 再 yield”导致请求被吞；
- `/画状态` 增加版本号显示；离线测试增至 256 项（core 75 / providers 66 / harness 115）。

### v1.3.0

- **AI 工具 worker 池**：后台任务由 `max_concurrency` 个常驻 worker 并行消费，单个慢任务不再堵住所有用户；新增 `/画任务` 查看排队/生成中/最近任务，`/画取消 <编号|全部>` 取消（运行中任务会立即中止并退回额度）；
- **生成请求超时不重试**：图片生成/图生图的 POST 超时不再自动重试，避免服务商已出图计费后重复扣费；超时仍计入熔断；
- **服务商熔断**：连续 3 次可重试失败后熔断约 120 秒，期间自动跳过该线路并尝试备用，`/画状态` 可见熔断剩余时间；
- **额度持久化**：每日额度、冷却与今日总量写入数据目录 `usage.json`，重启后保留；失败请求退回额度；
- 离线测试增至 251 项（core 75 / providers 66 / harness 110）。

### v1.2.2

- 修复 v1.2.1 中“工具已返回已开始生成、但后续没有任何结果”的静默丢失：生成任务改由插件启动时/首次使用时创建的**常驻 worker** 串行处理，不再依赖单次工具调用内创建的临时后台任务；worker 意外退出会在下次调用时自动重启；
- 新增 60 秒心跳（“图片仍在生成中”）、生成总预算（约 180~420 秒）、主动发送 20/30 秒超时与全链路日志（入队 / 开始 / 结束 / worker 退出），避免任务无限悬空；
- 保留工具立即返回与用户冷却 / 额度 / 并发限制；插件重载或卸载时取消 worker 并清空未处理队列；
- 离线测试维持 225 项（core 65 / providers 63 / harness 97）。

### v1.2.1

- 修复 AI 工具 `generate_image` 必然触发 `tool generate_image execution timeout after 120 seconds` 的问题：AstrBot 对工具调用有约 120 秒硬超时，而插件允许单次请求 180 秒并带重试/降级；现在工具只做参数校验并**立即返回**“已开始生成”，生成放入后台任务，完成后主动把说明 + 图片发到会话；
- 后台任务仍受全局并发、用户冷却与每日额度约束；失败原因会直接发回会话；插件重载/卸载时自动取消未完成的后台任务；
- 新增“工具不等待生成完成”的回归测试，离线测试增至 225 项（core 65 / providers 63 / harness 97）。

### v1.2.0

- 新增**图生图**：QQ 同条消息附图 + 工作台上传参考图，走 OpenAI 兼容 `/images/edits`（multipart）；支持 gpt-image-1 / dall-e-2 / 支持 edits 的中转站，mock 可离线验证整条链路；
- 参考图读取兼容 QQ 图片组件的本地路径、http URL 与 `base64://`，统一做 PNG/JPEG/WebP 魔数与 10MB 上限校验；只发图不写描述时使用默认改图提示词；
- 图生图复用完整响应体读取与截断检测，并具备 url→b64 格式回退、不认 `response_format` 时自动去掉重试；
- 工作台新增参考图上传 / 预览 / 移除，状态条与结果区标注图生图；Gemini / DashScope 会明确提示暂不支持；
- 离线测试增至 221 项（core 65 / providers 63 / harness 93），并完成「上传参考图 → 改图」浏览器烟测。

### v1.1.0

- 工作台新增「设置」弹层：可直接更换绘图服务商（OpenAI 兼容 / Gemini / DashScope / mock）、模型、接口地址、API Key、画质，配置备用服务商，以及默认风格 / 比例 / 张数；
- 设置保存后写回插件配置并立即重载服务商链；密钥只写不读，页面仅显示掩码，支持保留或清空；
- 后端新增 `settings`（GET）与 `save_settings`（POST）两个接口，含服务商、地址、风格、比例等校验；
- 工作台设置弹层做了 light / dark / 移动端适配与浏览器烟测；
- 离线测试增至 203 项（core 65 / providers 53 / harness 85）。

### v1.0.4

- **修复大图响应被误判“JSON 截断”的根因**：`aiohttp` 的 `content.read(n)` 语义是“一有数据就返回、最多 n 字节”，此前单次调用只拿到响应第一个数据块（常见 7KB 左右），2MB+ 的 base64 图片 JSON 因此被截断解析。现在循环读满整个响应体（32MB 上限），JSON 解析与 URL 图片下载两条路径均已修复；
- 新增响应完整性校验：实收字节 < `Content-Length` 时明确报“连接提前结束（收到 / 声明）”，并保留重试；错误响应体不完整时仍优先按 HTTP 状态码处理，4xx 不重试；
- 分块传输（chunked）按规范忽略残留的 `Content-Length`，避免误判；
- 新增 12 项回归测试（大 b64 响应、chunked 大响应、提前断开、大图下载、4xx 截断），离线测试增至 180 项。

### v1.0.3

- OpenAI 兼容接口默认改用 `response_format=url`（JSON 小，规避大 base64 被截断），失败自动退回 `b64`；新增配置 `openai_response_format`（auto/url/b64）；
- 截断响应增强：图片数据完整但 JSON 尾部缺失时，按 PNG/JPEG/GIF/WEBP 结构校验后直接恢复图片（PNG 允许缺 IEND，但 chunk 不完整仍拒绝）；
- 截断错误信息补充“收到字节数 / 声明字节数”，便于区分链路截断与服务商坏响应；
- 截断的网络层重试收敛为 1 次，剩余交由“换返回格式”重试，减少无谓请求；
- 离线测试增至 168 项。

### v1.0.2

- 新增 WebUI 工作台（`pages/studio/`）：表单选风格/比例/张数，直接生成、预览、下载；最近生成、状态条、重载配置、清理旧图；
- 工作台后端新增 7 个接口（state / generate / history / thumb / image / cleanup / reload），走 Dashboard 登录态鉴权，只受全局并发约束，不占 QQ 用户额度；
- 新增 `.astrbot-plugin/i18n/` 页面标题与描述（zh-CN / en-US）；
- 离线测试增至 162 项。

### v1.0.1

- 修复 `llm_tool` 因模型多传参数（如 `aspect_ratio`）抛 `TypeError`：工具签名支持 `**kwargs`，并新增 `aspect_ratio` / `num_images` 参数，常见别名自动映射；
- 修复部分中转站导致“服务商返回的不是合法 JSON”：支持 BOM/NUL 杂字节、截断 JSON 修复、完整图片条目抢救，疑似截断自动重试；
- 错误提示补充 JSON 解析的具体原因，并对该类失败给出可操作建议；
- 离线测试增至 144 项。
