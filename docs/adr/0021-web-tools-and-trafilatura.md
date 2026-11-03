# ADR 0021 · 网页工具引入 trafilatura 抽取正文

- 状态：已采纳
- 日期：2026-11-03
- 关联：`src/xhs_agent/tools/webreader.py`、`src/xhs_agent/tools/websearch.py`、`docs/contracts/配置契约.md` §3.14 与 §4 第 8 步、`docs/技术栈.md`

## 背景

对话 agent 需要「搜索 + 抓取」两个网页工具。搜索（`tools/websearch.py`，走 SearXNG）只返回标题、摘要和 URL，真正的答案在网页正文里，所以还得有 `fetch_url`。

抓回来的原始 HTML 不能直接给模型，三个原因：

- **体积**：现代网页的内联脚本、样式表、导航与广告位上几百 KB 到几 MB 很常见，而契约里 `fetch_max_chars` 只给 20000 字符预算——预算会被噪声而不是正文吃光。
- **信噪比**：正文占比常常不到一成。模型要在菜单、cookie 横幅和相关推荐里找答案，钱花了还可能答错。
- **可复现性**：噪声随页面改版和广告投放变化，同一个 URL 两次抓取拿到的「正文」不一致。

所以 `fetch_url` 能不能用，取决于正文抽取质量。抽不干净，它就是个把混沌 HTML 换算成账单的机器。

### 不加它会怎样

AGENTS.md §6 要求每个依赖都回答这个问题，这里的答案很直接：

- 自己写 `html.parser` 只能剔掉**结构上显式标注**的噪声（`<script>` / `<style>` / `<nav>` / `<aside>` / `<footer>`）。评论区、相关推荐、「猜你喜欢」、cookie 横幅绝大多数是不带语义标签的 `<div>` / `<ul>`，没有可靠的剔除依据。
- 退化成「整页 HTML 直接喂模型」：token 成本按页面体积线性上涨，`fetch_max_chars` 截到的基本是噪声。
- 退化成「只取 `<title>` + `<meta description>`」：绝大多数站点这两样只有一两句话，等于没抓。

**结论**：不引入成熟抽取器，「网页正文抓取」这个能力直接不成立，`fetch_url` 白做。

## 备选方案

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| 自写 `html.parser` / 正则清洗 | 零依赖、完全可控 | 只剔得掉语义标签噪声；评论区、推荐位、cookie 横幅剔不掉；每个站点单独写规则，维护成本无上限。抽取质量决定 `fetch_url` 是否可用，这一环最不能省 |
| `readabilipy` 0.3.0 | MIT；Readability 启发式的 Python 侧封装，声明依赖只有 4 个 | 硬依赖 `beautifulsoup4` + `html5lib` + `lxml` + `regex`——`lxml` 这个 C 扩展照样躲不掉。**更要紧的是它自带的 `Readability.js` 路径要靠 Node.js 14+ 的外部安装**：CLI 默认走这条路，但 **Python API 默认走纯 Python 移植版**，要显式 `use_readability=True` 才切到 `Readability.js`——本项目正是 import 库用法。Node 不进 `uv.lock`、不受 `uv sync` 管，装了不会自动生效、没装也不会报错，两条路径的质量差异在部署侧完全静默。发行版停在 0.3.0，而 trafilatura 已迭代到 2.3.1；另外它只做正文抽取，没有多语言与精确模式这些现成开关 |
| **`trafilatura` 2.3.1** | 纯 Python wheel（`py3-none-any`）；正文抽取、元数据、样板剔除都是现成的；`favor_precision` 能应付 JS 空壳页；社区活跃；Apache-2.0 | 闭包 18 个包（见下）；引入 `lxml` C 扩展，但有预编译 wheel |

## 决策

**用 `trafilatura` 2.3.1 做正文抽取，且只在 `tools/webreader.py` 一处使用。**

配套约束（已写进模块 docstring，避免以后被「顺手加固」）：

- 固定 `with_metadata=False`；若要调 `extract_metadata`，参数名是 `default_url=` 而不是 `url=`。
- **禁用 `fetch_url` 与 `fetch_response`**：这两个 API 会自己发网络请求，绕过本模块的 SSRF 钩子、原始字节上限和 robots 检查。
- 「抽不到正文」判定用两层：`is None` 或全空白，再叠本项目的 200 字符策略阈值。

## 后果

**正面**

- `fetch_url` 交给模型的是人看得懂的正文，上下文花在内容上；
- 标题等元数据、分页切片、失败分类（`insufficient_text` / `too_large` / `content_type`）都有明确落点；
- 只在一个适配器里用，将来换库只改 `tools/webreader.py`。

**负面（代价，均为实测）**

- **闭包 18 个包**：`trafilatura` 本身 + **7 个声明硬依赖**（`certifi` / `charset-normalizer` / `courlan` / `htmldate` / `justext` / `lxml` / `urllib3`）+ **10 个传递依赖**（`babel` / `dateparser` / `lxml-html-clean` / `python-dateutil` / `pytz` / `regex` / `six` / `tld` / `tzdata` / `tzlocal`）。其中 `lxml-html-clean` 由 `justext` 要求的 `lxml[html_clean]` 带入、`six` 由 `python-dateutil` 带入，两条都要算。`uv.lock` 因此从 117 个包涨到 128 个：新增 11 条，另外 7 个本来就在。
- **冷缓存安装约 15.7s**，热缓存约 2s——瓶颈是下载，不是编译。
- 引入 `lxml` 6.1.3 这个 C 扩展。**但它有预编译 wheel**（实测 cp311 / cp312 的 win_amd64，以及 manylinux / macOS 各平台），**不需要 MSVC 或任何构建工具链**。这一点决定了它没有破坏「`uv sync` 一把过」的安装体验。
- 依赖面变大：以后每次 `uv lock` 升级都要顺带看一眼这 18 个包的安全公告。

**后续**

- 需要更强的 SSRF 防护时上出口代理（见下节），不在本模块里加复杂度；
- trafilatura 的大版本升级要重新实测 `favor_precision` / `with_metadata` 的默认语义，不能假定不变。

## 已知行为陷阱（全部实测，别按直觉改）

1. **markdown 输出默认丢弃所有链接**——正文里的 URL 不会出现在结果里；需要链接时要显式开参数并接受噪声上升。
2. **`with_metadata=True` 的 `date` 会凭空捏造**——页面上没有发布日期时它会猜一个。所以本项目固定 `with_metadata=False`。
3. **JS 空壳页在默认参数下返回 `<noscript>` 里的字符串而不是 `None`**——只有 `favor_precision=True` 才落 `None`。本项目两个都用：精确模式 + 长度校验双保险。
4. **库没有最小长度门槛**——30 字的页面也照返。「少于 200 字符判失败」是**本项目的策略阈值**（`MIN_TEXT_CHARS`），不是 trafilatura 的行为，别写成库的保证。
5. **`extract_metadata` 的参数名是 `default_url=`**，不是 `url=`。
6. **`fetch_url` 与 `fetch_response` 都会发真网络请求**——本模块禁用两者，统一走注入的 httpx client。

## 安全边界：只防直连内网，不防 DNS rebinding

这一条最不能含糊：**不要在任何文档或注释里把它说成「已经防住了 SSRF」**。

本模块的防护是「在 httpx 的 request 钩子里解析目标主机、拒绝非公网地址」，对应 `blocked_host` / `dns` 两个失败原因；`allow_private_ips` 只给测试用。

**它明确防不住 DNS rebinding。** 钩子里的解析与 httpx 实际建连时的解析是**两次独立解析**：攻击者可以让一个域名先解析到公网地址（通过检查），建连时再解析到内网地址（`127.0.0.1`、`169.254.169.254` 这类云元数据端点），从而绕过拒绝逻辑。这条路径已实测复现。

同理，钩子**只做拒绝判定、不改写 `request.url`**（不做「解析后钉 IP」）。一旦改写，TLS SNI 与 `Host` 头都会错，`test_guard_does_not_rewrite_request_url` 钉着这个设计决定。

**更强的防护需要出口代理**：把出网收敛到一个可控代理，由代理执行地址策略，钩子只负责构造请求。本期不做，理由是本工具默认关闭（`[web].enabled = false`）、当前只服务单机单用户，风险收益比不成立。真要对公网用户提供这个能力时，必须先补这一层再开放。
