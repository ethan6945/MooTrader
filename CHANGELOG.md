# CHANGELOG

## 2026-08-08 — 新闻开关接进设置面板 + FinBERT 权重源修复（v2.4.0）

**FinBERT 的 ONNX 下载指向了一个没有 ONNX 的 repo。** `ProsusAI/finbert` 只发布 torch checkpoint —— 没有 `onnx/` 目录、没有 `tokenizer.json`。所以 `ensure_model()` 先 404 掉量化图、再 404 掉 fp32 fallback，对**每一个开了 FinBERT 的用户**返回「could not download an ONNX graph」。沙箱测不出来：推理逻辑是拿本地构造的真实 ONNX 图验的，那张图是对的，错的是它的来源。ONNX 路径改指 `Xenova/finbert`（同权重的 ONNX 导出，`id2label` 与架构均已比对一致），torch 路径留在 `ProsusAI` —— 反过来 Xenova 没有 `pytorch_model.bin`，两个 repo 各有一半。`FINBERT_MODEL` 仍可覆盖两者，但仅在它被指向 torch 默认值以外时才算数：把那个默认值当成用户的选择，正是 ONNX 路径撞 404 的原因。macOS 实测：111.7 MB / 7.8 秒，bullish 76 > neutral 56 > bearish 24。

**新闻功能在 DMG 安装上根本开不起来。** 上一版新增的开关全部只存在于 `.env`，而打包安装的 `.env` 在 `~/Library/Application Support/MooMooTrader/`，界面里没有任何入口能碰它 —— 装了 2.4.0 的人看到的和 2.3.0 完全一样。

- `/api/settings/toggles` + `/api/settings/toggle`：五个开关（`FINNHUB_ENABLED` / `SEC_EDGAR_ENABLED` / `FINBERT_ENABLED` / `NEWS_DRIVEN_ENABLED` / `NEWS_DRIVEN_SHADOW`）走独立布尔白名单，写入只可能是 `true`/`false`；原生面板与网页面板共用同一批端点。
- **开启新闻主导模式时，若用户从未对影子模式表过态，同时把影子模式打开并明说**。要避免的失败是：有人翻了一个开关，就在不知情的情况下拿真钱赌 LLM 对一条标题的读数。一旦用户明确关掉影子模式，这个偏好就被尊重，不会再被改回去。
- `SEC_EDGAR_USER_AGENT` 进 key 白名单，且**不打码**（它是联系方式不是密钥；`••••com` 会藏住那个让你 IP 被封 10 分钟的拼写错误）。原生面板对非密钥用普通输入框并预填。
- FinBERT 那一行显示模型实际状态 —— 「已开启但没下载」= 打分器永不被调用，这个陷阱现在写在开关旁边，而不是留给用户去日志里发现。

## 2026-08-07 — 新闻因子研究 + 影子模式（v2.4.0）

给 `NEWS_DRIVEN_ENABLED` 补上它一直欠着的那道闸门。期权放量因子当初必须先过 `scripts/options_factor_study.py`（5,980 组名日）才被允许碰仓位；新闻主导模式至今没有对应的东西，预检里那句「没有因子研究背书」写的就是这件事。

**`scripts/news_factor_study.py`** — 离线因子研究，现在才可能做，因为它需要两样东西同时存在：Finnhub 的历史新闻（Tavily 只答「现在」，过去某天的输入无法重建）＋ FinBERT（训练语料早于测试窗口；用前沿 LLM 去评 2026 年的新闻，测的是它的记忆）。

- **时点对齐刻意取保守的那一种**：用 **D-1 收盘前**发布的新闻打分 → **D 日开盘**进 → **D 日收盘**出。当天 15:00 发的新闻去「预测」当天走势不是预测、是复盘，而那是制造一条漂亮却毫无价值的回测曲线最省事的方法。
- `ret_session`（开盘→收盘）是主口径，因为收盘平仓的模式只能吃到这一段。next1 / next3 也报，用于和期权研究对齐、也因为 PEAD 漂移是按天算的 —— 但**当前模式吃不到**，不能当作它的预期收益读。
- PASS 判据：分桶在 `ret_session` 上单调 ＋ 顶桶在胜率和均值上都胜过基线 ＋ **两个半段都成立**。报告结尾明写三条局限：FinBERT 评的是情绪不是价格影响；单一市场环境、桶小到几只票就能带偏；**成本完全没建模**，日内往返每天付价差和手续费，边缘太薄的话实盘就是亏。
- 每次抓取落盘缓存（`data/news_study_cache/`），首跑慢（Finnhub 免费档 60 次/分），重跑免费、中断可续。

**影子模式 `NEWS_DRIVEN_SHADOW`（本次最推荐的东西）**

离线研究能测「FinBERT 情绪分能不能排序收益」，但**测不了真正会上场的那道闸门** —— LLM 的判断 ＋ 具名催化剂要求。想知道实盘链路有没有 edge，只能收集它自己的决策并附上结果，而这件事要么拿钱做、要么不拿钱做。

- 开启后完整跑完整条链路（新闻读数、催化剂判断、风控、仓位），在**下单前一行**停住，把「本来会买什么」写进 `data/news_shadow.jsonl`（JSON-lines 追加，崩溃不会丢历史行；时间戳用 ET，和这个机器人其他所有时间决策一致）。
- `python -m scripts.news_factor_study --shadow` 给这些决策附上结果：胜率、每笔均值、按 LLM 分数分桶，以及 **LLM 和 FinBERT 分歧时的表现** —— 如果分歧确实标记出了亏损单，那个交叉验证就值得从顾问升级成真闸门。
- 预检把影子模式报成 INFO 而非 DEGRADED（它不是降级，是刻意不交易），但措辞明确：**「这个模式当前不会下任何单」** —— 忘了自己开着影子的人，会困惑好几周为什么armed 的模式一单不下。

**打包**：`packaging/entry.py` 的 `--import-check` 补进 5 个新模块和 `onnxruntime` / `tokenizers`。这些是惰性 import，PyInstaller 的静态分析看不见，全靠 spec 里的 hiddenimports —— 而这条自检就是证明那个 hiddenimports 真的生效的东西，缺了会在构建时失败而不是在用户机器上变成一个永不触发的功能。版本 2.3.0 → 2.4.0。

## 2026-08-07 — Finnhub 新闻源 + FinBERT 落地（app / 源码共用一份模型）

**Finnhub**（`src/finnhub_news.py`，默认关，需 finnhub.io 免费 Key：60 次/分钟、约 1 年历史）

- 新闻由**供应商按股票代码标注**，而不是靠搜索字符串匹配 —— "AAPL" 不会再搜进无关的同名内容。
- 真正的意义是它能**查历史某一天**（`from` / `to`）。Tavily 只答"现在"，而**你没法回放一个输入都无法重建的决策** —— 这就是新闻策略至今无法回测的根本原因。`fetch_company_news(..., until=某个过去日期)` 就是为此留的接口；窗口边界在本地二次校验，不信任服务端（point-in-time 回放里漏进一条未来新闻就是前视 bug）。
- 与 Tavily 合并后按标题归一化去重 —— 同一条通讯社原稿从两个源进来会被算成两条，让单个事件看起来像互相印证，而这恰恰是新闻下注最不能有的错觉。Finnhub 优先保留。
- 复用 `NEWS_INCLUDE_DOMAINS` 做来源过滤（Finnhub 返回的是 source 名不是 URL，所以是宽松子串匹配，目的是滤掉明显的聚合噪音）。

**FinBERT 改造：从"源码版专属"变成"哪里都能用"**（`src/news_score_local.py`）

上一版把 FinBERT 做成了可选依赖，`.app` 用户实际上用不了。按要求改了三处：

- **运行时换成 ONNX Runtime**。`onnxruntime` + `tokenizers` 约 25 MB，进 `requirements.txt` 和打包 hiddenimports —— 这是让 `.app` 也能跑的唯一可行代价（torch + transformers 是 1–2 GB，而这个项目为省 53 MB 排除了 matplotlib）。源码版若已装 torch + transformers 则自动走那条路，不会存第二份权重。
- **模型放一份共享路径**，`news_score_local.model_home()`，**刻意不用 `config.ROOT`** —— ROOT 在源码版是仓库目录、frozen 是 app-support，用它会让两边各下一份。权重是机器级用户数据，不是每个安装各自的状态。macOS 走 `~/Library/Application Support/MooMooTrader/models/finbert`，Linux/Windows 各自的标准位置，`FINBERT_HOME` 可覆盖。
- **绝不隐式下载**。`FINBERT_ENABLED=true` 本身不拉一个字节。新增「设置 → FinBERT」面板：先 `confirm` 弹窗说明**约 120 MB、下载到哪个具体路径、随时可删回收**，用户点了才开始；后台线程下载 + 进度条轮询，另有「删除」按钮把空间还回去。headless 机器可以用 `FINBERT_AUTO_DOWNLOAD=true` 跳过弹窗，且该调用排在开机保护性止损**之后**，慢下载永远不会拖延止损。
- 下载走 `.part` 临时文件再改名，中断不会留下一个能加载但输出垃圾的截断图；加载前还有 1 MB 体积下限兜底。
- 新增 `/api/finbert`（GET 永远安全、不触发下载）、`/api/finbert/download`（POST 即为知情同意）、`/api/finbert/remove`。预检把「已开启但未下载」报成 INFO 并写明路径和体积，而不是报错。

**验证**：用真实 ONNX 图 + 真实 tokenizer 端到端跑通推理路径（padding、`token_type_ids` 输入名检测、softmax、`id2label` 映射），确认 bullish > neutral > bearish 单调且落在 0–100；`remove_model` 正确回收。真实权重下载和 Finnhub 实网调用在本沙箱被出网代理挡住，未端到端验证。

## 2026-08-07 — AI 故障必须发声 + SEC EDGAR 一手源 + FinBERT 交叉验证

**1. AI 挂掉不再静默**（`ai.py` / `health_check.py` / `web/`）

上一条修 `deepseek-chat` 时发现的根因不止是模型名过期，而是**看门狗本身有个分类漏洞**：`check_ai()` 把 404 归到 "unclassified" → `skip`，而 `skip` 从不翻转状态、从不告警。所以模型下线这种最该报警的情况，恰恰是唯一报不出来的。

- **404 / model not found 归类为 `bad`**（owner-actionable，改一个设置就好），补上那个洞。
- **新增运行时调用账本**（`ai.call_health()`）：`generate()` 每次调用记录成败，连续失败 3 次由看门狗边沿触发告警。这是和定时 ping **相互独立**的第二路信号 —— ping 能通不代表本机器人真正发出的请求能成。无 key 时不记为失败（「从没配过」和「配了但坏了」是两个问题）。
- **新闻主导模式下告警措辞升级**：平时 AI 挂了只是降级（技术面照跑、AI 层 fail-safe），但新闻主导模式下读不到新闻就不下单 —— 那是**完全停止交易**。同一个通道，两种后果，消息里说清楚是哪一种。
- **网页红色横幅**（`/api/status` 新增 `ai_health` + `index.html`）：4 秒轮询已有，不额外探测（否则每小时给供应商刷 900 次）。「知道了」按当次故障（provider+model+错误）记忆，不是按页面加载 —— 每 4 秒重弹的横幅只会训练你无视它，而那正是四天失明的成因。换一种新故障会重新弹。

**2. SEC EDGAR 一手催化剂源**（`src/sec_edgar.py`，默认关）

Tavily 搜的是开放网络，通讯社原稿、聚合站洗稿、涨跌复盘、SEO 列表长得都一样。EDGAR 反过来：**8-K 本身就是重大事件**，由发行人申报、带精确时间戳。对「有没有具体且新鲜的催化剂」这个问题，这是能拿到的最高信噪比答案，且免费。

- 8-K item code 映射成人话（2.02 业绩、1.01 重大协议、5.02 高管变动…），把「有份申报」变成「有个**什么类型**的催化剂」。10-K/10-Q 故意排除 —— 定期报告不是意外，当成催化剂就是在买已被消化的事件。
- **没配 User-Agent 就拒绝发请求**。SEC 要求 UA 写明身份 + 联系邮箱，否则 403 并可能封 IP 约 10 分钟 —— 而那是同一台连着券商的机器。宁可静默不发，也不赌这一下。预检把这条单列出来。
- Ticker→CIK 映射本地缓存一周，刷新失败时回退旧缓存（一周前的映射依然基本正确，为一次网络抖动丢掉整个数据源不划算）。
- **`assess_news` 的空源判断改成「两个都空才拒」**：原本 Tavily 没结果就直接不下单，但「已有 8-K 而媒体还没写」恰恰是这个模式能遇到的**最好情况**（一手源，早于转述），不该被沉默的 Tavily 否决掉。

**3. FinBERT 本地打分**（`src/news_score_local.py`，默认关，**不进打包**）

- 价值**不在于**让实盘进场更准 —— 它评的是句子情绪，不是价格影响；「公司公布创纪录利润」在它眼里就是正面，哪怕股票因不及预期而跌。价值在于它的训练语料**早于**任何你要测的窗口，所以没有后见之明。这是让新闻策略可回测的那一半拼图（另一半 point-in-time 新闻归档仍然缺，Tavily 只答「现在」）。
- 今天的定位：对**同一批**标题给一个确定性的第二意见，和 LLM 的分数一起记进成交记录，让「LLM 和 FinBERT 分歧」从直觉变成可量化的东西。ADVISORY，不参与闸门。
- **刻意不列进 requirements.txt**：torch + transformers 给冻结包加约 1–2 GB，而 `mmt-backend.spec` 为省 53 MB 排除了 matplotlib —— 为一个顾问性交叉验证背一整套深度学习栈不值。惰性 import，所有失败路径降级为 unavailable；源码安装 `pip install transformers torch` 即可开启，`.app` 永远不带。
- 打分为 mean(P(正) − P(负)) 重映射到 0–100，且**从模型 config 读 `id2label`** 而不是假定列序 —— 标签顺序不同的 fine-tune 会让整个信号悄悄反向，那种 bug 看起来像「alpha 不行」。取不到真实结果时返回 `None` 而非中性 50：「FinBERT 说中性」和「FinBERT 没跑」是两件事，只有前者是证据。

## 2026-08-07 — DeepSeek V4 迁移（`deepseek-chat` 已停止解析）+ 新闻检索质量

**这一条是修故障，不是加功能。** DeepSeek 于 2026-07-24 15:59 UTC 退役了 `deepseek-chat` / `deepseek-reasoner` 两个旧模型名，**没有软重定向** —— 请求直接失败。本仓库 `DEEPSEEK_MODEL` 的默认值正是 `deepseek-chat`，而所有 AI 层都是 fail-safe 的（AI 挂 → 返回中性 → 交易照跑），所以这是一次典型的静默降级：主循环看起来一切正常，AI 复核/情绪/智能退出实际全部空转。这正是 `preflight.py` 开头那段自述要防的事，但当时的预检只验 key、不验模型名，key 有效 → 一路绿灯。

- **调用时自动改名**（`ai.migrate_deepseek_model`）：`deepseek-chat` → `deepseek-v4-flash`（thinking 关）、`deepseek-reasoner` → `deepseek-v4-flash`（thinking 开），并 WARNING 一次。未编辑的 .env 和残留的 db-state 覆盖都能继续跑，不认识的名字原样放行 —— 绝不替用户猜他自己选的模型。
- **thinking 变成请求参数**：V4 把推理模式从模型名移到 body 的 `thinking`，且 pro 档默认开。`_deepseek()` 现在显式声明模式，不继承会变的默认值 —— 本仓库每个调用点要的都是「限时内吐一段短 JSON」，不是长思考。
- **预检验模型名**（`check_ai`）：配了退役名字时以 DEGRADED 明说「现在靠自动改名还能跑，但配置本身是坏的」，并指出 flash / pro 的取舍。默认值与 `_FALLBACK_MODELS` 同步更新为 V4。
- **修 `check_news_driven` 里我自己写的 Gemini 硬编码**：改成问 `ai.active_provider()` 要 key 名。`PROVIDERS` 自 2026-07-22 起就只有 deepseek，硬写 Gemini 会让这个装机去找一个用户早就删掉的 key。

**新闻检索质量**（`src/news_fetcher.py`）：

- **不再丢弃 `published_date`**（以及 Tavily 的 `score`）。此前每个消费方看到的 5 分钟前的头条和 3 天前的头条是同一段纯文本 —— 对新闻主导模式是致命的，它的核心问题就是「这个催化剂是新鲜的还是已经走完了」，光看标题答不了。`format_news` 现在前缀 `[时间戳]`，`NEWS_DRIVEN_PROMPT` 也把当前 ET 时间一起喂进去，那个 `stale` 判断这才真的有依据。
- **`NEWS_INCLUDE_DOMAINS` 域名白名单**（默认空 = 行为不变）。Tavily 搜的是开放网络，会把「3 只值得关注的 AI 股」这类 SEO 列表、聚合站洗稿、涨跌幅复盘和真报道混在一起返回，而这些在模型眼里都像催化剂。填 `recommended` 即启用主流财经通讯社 + `sec.gov` —— 8-K 本身**就是**重大事件，有时间戳，且早于媒体转述。
- 新增 `NEWS_SEARCH_DEPTH`、`NEWS_TICKER_DAYS`（默认 3 保持不变；同日内平仓的策略应该调到 1 —— 3 天窗口正是「读到的是旧闻」的直接原因）。

## 2026-08-07 — 新闻主导模式（`NEWS_DRIVEN_ENABLED`，默认关）

机主要求的一个显式开关：把新闻从「顾问」提升为「主信号」，据此下注，收盘平仓。默认关闭时全链路逐字节不变。

- **漏斗倒置**（`src/news_driven.py` 新增 + `src/main.py`）：开启后技术分降级为「这票能不能碰」的预筛（`threshold_floor` 按 `NEWS_DRIVEN_THRESHOLD_DELTA` 放松，硬地板 50 —— 再好的消息也不买烂走势），由 AI 新闻读数决定**选股**（`NEWS_DRIVEN_MIN_SCORE`，默认 65）和**仓位**（分数线性映射到 1.0–`NEWS_DRIVEN_MAX_MULT`，下游 `max_position_pct` 仍封顶）。加仓单同样过闸 —— 加仓是对同一条新闻的新下注。
- **要求具名催化剂**（`NEWS_DRIVEN_REQUIRE_CATALYST=true`）：提示词（`ai_validator.NEWS_DRIVEN_PROMPT`）明确区分「具体事件」（上调指引、签约、升评、获批）和「氛围」（泛泛看好、涨幅复盘、维持评级）。模型标记 `stale=true` 的催化剂降级为无催化剂 —— 市场已经走完的行情不是现在进场的理由。关掉这一项等于让 LLM 的情绪分替你选股。
- **fail-safe 方向反转**，这是本次最要紧的设计点：顾问模式下「AI 不可用 → 中性 50 → 照常下单」是对的（技术面才是论据）；新闻主导模式下新闻**就是**论据，所以无 key / 无新闻 / 配额错误 / 预算耗尽一律**不下单**，绝不回退成技术面选股 —— 那会在用户以为跑着 A 策略时偷偷跑 B。`assess_news()` 因此额外返回 `ok` 标志，`news_driven.gate()` 只认真读数。
- **收盘平仓**（`executor` auto-flush 0.7）：`NEWS_DRIVEN_FLATTEN_ET`（默认 15:45 ET，盘中正常挂单、避开 15:30 MOC 区）无视盈亏/TP/SL 平掉全部 bot 持仓。机主手工持仓照旧豁免；取不到价的停牌票宁可留过夜也不砸向虚空。
- **堵住两个空转**：① 平仓后加 `EOD_FLAT` 再入场冷却，否则 15:45 平掉、15:50 被扫回来、15:55 再平，在一天里价差最宽的时段来回付费；② 新增 `NEWS_DRIVEN_MIN_HOLD_MIN`（默认 30 分钟），15:15 后不再开新仓 —— 开一个五分钟后就被平掉的仓位只是白付一轮手续费和滑点。
- **回测失效必须说出口**（`src/preflight.py` 新增 `check_news_driven`，`src/sandbox.py` 注释）：本仓库其他 AI 层一律 advisory，正是为了让回测仍然描述实盘；这个开关改的是**选股**，而 sandbox 故意跳过 AI（防 LLM 后见之明），于是两边跑的是不同策略。开着它时每次启动预检都会以 DEGRADED 说明这一点，并指出它也没有因子研究背书（对比期权放量因子的 5,980 组样本）—— 实盘结果本身就是实验。缺 Tavily/AI key 时预检明说「一单都不会下」。
- 未加 `NEWS_DRIVEN_MODEL`：`ai.generate()` 没有 `model=` 参数，现有 `GAP_SENTINEL_MODEL` / `SMART_EXIT_MODEL` / `SENTIMENT_MODEL` 实际都没人读（`.env.example:32` 已注明）。不再增加第四个假旋钮。

## 2026-07-11 — Sandbox 配置对齐 + 真实 VIX + sandbox↔backtest 交易级差分

三件事把 sandbox 从"独立但配置漂移"修成可信的差分基准（快引擎 engine_compare 只互查 V3 与冻结 oracle，此前**没有任何东西**把快引擎和 sandbox 对过账）：

- **配置对齐**（`src/sandbox.py`）：启动资金改用 `risk_manager.budget_usd()`（db 预算覆盖 > 冻结 .env），仓位槽数从解析后的预算推导（对齐 `risk_manager.max_positions()`），动态 universe 的 top_n 改走 `runtime_config.universe_top_n()`。`SandboxConfig` 新增 `account_usd`（0=自动解析），供差分脚本注入同一资本。启动打印/结果 JSON 同步改为运行时生效值。
- **真实 VIX**（`SimFeed._load_vix`）：^VIX 日线经 yfinance 拉取 + parquet 缓存（`data/sandbox_cache/VIX_DAY.parquet`），与 `backtest.prefetch_data` 同一序列、同一 +1 天前移（读昨收，无前视）。旧 SPY-ATR×15 代理降级为网络故障 fallback（`_vix_proxy`）。有真 VIX 后 breadth 补上 live 同款 `VIX_PANIC≥30` 检查（fallback 时跳过）。VIX 减仓/regime 层不再吃代理噪音。
- **交易级差分**（`scripts/sandbox_vs_backtest.py`）：同窗口同运行时配置跑 sandbox + `_run_live_engine`(V3 live-fidelity)，按 (symbol, 入场日 ±1 交易日) 对齐每笔交易；单边交易给出 sandbox gate 归因（run_sandbox 新增有界 `skip_events` 日志，上限 1 万条）；配对交易报入/出场 bps 差。**只比较两引擎共同覆盖窗口**（V3 的 `days` 是 bar 数预算,实际跨度更宽——首跑 20 笔"only in v3"里 14 笔是窗口外假差异）。超容差退出码 2,可挂 cron/health check(符合"只推可操作事件")。原始引擎输出落盘 `data/sandbox_vs_backtest_raw_*.json` 供免重跑调参。
- **基线（2026-07-11,30 天窗）**：sandbox 27 vs v3 15 笔（窗口内）、9 笔配对；同日配对入场差中位 ~6bps（成交模型吻合）,±1 天配对含周末跳空属诚实非信号 → 容差只读同日配对。缺口主因：sandbox 的 regime 自适应阈值（BULL 60 vs v3 固定 65）+ v3 的 17 次 cash-limited。默认容差按基线+余量校准（match≥25%、同日中位≤25bps、净利差≤60%）。
- 环境修复：uv venv 缺 parquet 引擎（`pd.read_parquet` 全挂,sandbox 缓存实际不可用）→ 装 `pyarrow` 并补进 `requirements.txt`。
- **周检接线**（`main.py`）：新增 `_weekly_sandbox_diff_job`,周一 20:25 KL（周一链最后一个,量测的是本周实际要跑的配置）,subprocess 隔离跑差分脚本;**exit 0 只记 log+audit,exit 2（分歧超容差）才 Telegram**,脚本自身挂掉也通知。含启动 catch-up 注册（`sandbox_diff`）。已实跑验证全链路。
- **第 4 层脚手架**（`scripts/calibrate_fill_model.py`）：从券商 `history_order_list_query` 量测真实成交 vs 模型假设（5bps/边、触价即成）。**只测量不改常数**,改动被双闸门挡住：SIMULATE 环境（纸面撮合无微观结构信号,只有撤单率是真的）+ 样本 <30 笔。排除 SGOV（现金生息仓）。首测 90 天：BUY 177 单仅 45 成交（**成交率 25.4%**,132 单 5 分钟 TTL 撤单——对照引擎"一根 bar 内触价即成"的假设,这是最值得跟踪的真信号）;bot 账本记录的卖出价与实际 dealt 中位差仅 2.2bps（记账诚实）;SL 软止损中位越价 −12.6bps（n=6）。
- 发现：closed_trades 里 bot 的 `entry_price` 记的是**下单限价**而非 `dealt_avg_price`（代码只读过 `dealt_qty`）——真实成交价只能从券商订单历史拿,校准脚本因此直接查 API。

## 2026-07-08 — 网页面板：净值曲线移入历史 + 美国板块总览（Live）

仪表盘右下角的「净值曲线 · 累计已实现」面板移到**历史**标签页顶部（月度图上方，图表/悬浮提示行为不变），原位置换成**美国板块总览 · Live**。

- 新后端接口 `GET /api/sectors`（`web/server.py`）：yfinance 批量拉 11 只 SPDR 板块 ETF + SMH 半导体 + SPY/QQQ/IWM 三大指数，日线 last/prev close 算当日涨跌%（盘中即实时价）；服务端缓存 60s + 锁防并发打穿，拉取失败回退旧缓存（`stale:true`）。返回 `clock.market_session()` 的盘中/盘前/盘后/休市状态。
- 前端：板块面板与「持仓」互换位置——板块进左上大格子，渲染为 **4×3 热力图色块**（按涨跌排序、色深随幅度共享一个比例尺，块内显示板块名/ETF/涨跌%/现价）；指数为顶部 pill；标题栏显示 🟢盘中/🌅盘前 等状态 + 数据日期；每 60s 刷新（仅仪表盘标签激活时），中英文跟随语言切换。持仓表进右下窄格后隐藏 入场/止损/止盈 三列（区间条已编码：▏入场刻度 · ●现价，悬浮显示 SL/TP 具体价）；手机端持仓仍排最前、色块降为 2 列。
- 注意：web 服务需重启加载新接口（已重启；交易调度器不受影响）。

## 2026-06-26 — 更聪明的 regime 感知（VIX 感知 + 防抖滞回）

升级 `src/regime.py`：在不动原始 `label`/`block_new_entries`（回测一致性是硬约束，两个引擎都用 `assess()` 重算入场日 regime）的前提下，新增**始终计算**的咨询字段——`vix`（让 regime 知道波动环境）、`strength`（−1..+1 距 200MA 的强弱）、`sub_label`（STRONG_BULL/HIGH_VOL_BULL/DEEP_BEAR/HIGH_VOL_BEAR…）、`confirmed_label`（**滞回平滑**后的标签）、`risk_mult`（咨询用，**不自动施加**——VIX 已在 calc_position_size 减仓，避免重复扣）。

- **唯一行为改动**（`SMART_REGIME_ENABLED`，默认关）：入场闸 + cash_yield + inverse_sleeve 改用滞回后的 `confirmed` 标签（200MA 上下 0.4% 死区 + 上一标签），不再被单根 K 线擦边 200MA 来回甩进/甩出 BEAR。默认关时 `effective_label==raw label`，行为与回测逐字节一致。
- **接线**：`main.py` 给 `assess()` 传 `vix` + 上一 `confirmed`（存 db-state `regime_last_label`），算出 `effective_label` 喂给 kill_switch/两个 sweep；snapshot 增 `regime_sub`/`regime_confirmed`。`assess()` 签名向后兼容（vix/prev_label 关键字可选，3 处位置构造 Regime 仍可用）。
- **验证**：20/20 单测（回测一致性/位置构造/strength/sub_label/滞回各分支/risk_mult）；新 `scripts/smart_regime_diagnose.py` 实跑 OpenD **485 天：regime 翻转 35→4（−89%），BEAR 天数 47→57（基本不变）→ HELPS**。这是少数通过验证的"加复杂度"改动。

## 2026-06-26 — 自动复利预算（"钱生钱"）

新增 `src/auto_budget.py`：让"可投入预算"随机器人的已实现盈利自动滚动增长（创新高随高水位增投、回撤则缩减），把线性的日内 edge 变成几何复利。每日收盘后（16:45 ET）跑一次，全自动 + 每次变动 Telegram 通知 + 审计历史；可在网页面板 arm/disarm/查看（`/api/auto-budget`）。

- **不会破坏回撤熔断**（关键不变量）：复利会写大 `budget_usd`（部署上限），但 DD 用的 equity 锚定在 arm 时冻结的 **seed** 上（新增 `risk_manager.equity_baseline()`），所以 `equity = seed + realized` 永不把已实现盈亏重复计两次。未启用时 `equity_baseline()==budget_usd()`，行为与之前逐字节一致。
- 护栏：reinvest 比例（默认 1.0 全复投，对称"赢加码/亏减码"）、下限 seed×0.5、上限 seed×5、再被实际账户净值封顶、滞回步长 max($250, 5%) 防抖。全部 .env / 网页可调，**默认关**（`AUTO_BUDGET_ENABLED=false`）。
- 接线：`main.py` 新增 `_daily_auto_budget_job`（16:45 ET 周一至五）+ 启动 catch-up + `cron_state` 注册。`/api/status` 暴露 auto_budget 快照。
- 验证：20/20 单测通过（复投/缩减/上下限/净值封顶/滞回/DD 解耦/禁用即 no-op）+ 端到端 Flask test_client（arm→赚$1300→预算 $4500→$5800→disarm）。

## 2026-06-26 — 熊市现金生息（"坏行情也赚点小钱"）

新增 `src/cash_yield.py`：熊市（regime=BEAR，策略已暂停开多）时把闲置现金买入国债 ETF（默认 SGOV，~4-5% 年化、近零风险、极高流动性）吃无风险收益，行情转可交易时自动全部卖回现金给策略开多用。诚实版"坏行情赚钱"——不是玄学 alpha，只是别让现金躺着吃 0%。

- **保守作用域**：默认仅在 BEAR 扫到（`CASH_YIELD_ONLY_BEAR=true`），所以永不和核心策略抢现金；留流动性缓冲 + 忽略零碎额度。在 regime 早退之前调用（否则熊市里根本跑不到）。卖出在每个可交易扫描，先于开多逻辑。
- **隔离**：reconcile 把该 ETF 从 orphan/mismatch 检测里排除（它是现金等价物不是策略仓位）；不在 watchlist/评分里，executor 只管自己跟踪的策略仓 → 不会被当成策略仓管理。
- 接线：`main.py` 扫描中 `cash_yield.manage(c, regime.label, cash, positions)`；下单复用 `place_limit_order`（买 last×1.001 / 卖 last×0.999 marketable）。Web `GET/POST /api/cash-yield` 开关。**默认关**。
- 验证：6/6 单测（BEAR 买入/可交易清仓/零碎不动/无价不动/NEUTRAL 视为可交易解除）+ 全量 import clean。

## 2026-06-26 — 反向 ETF 对冲 sleeve（脚手架 + 回测闸门，未验证默认关）

新增 `src/inverse_sleeve.py` + `scripts/inverse_sleeve_backtest.py`：现金账户不能做空 → 在确认下跌时买入反向 ETF（默认 SH −1x，或 SQQQ −3x）赚下跌的钱。规则严格（反向 ETF 在 chop 里会损耗）：**BEAR 且反向 ETF 站上自己的 SMA20** 才进，趋势/regime 结束或止损/止盈/最长持有则出。仓位封顶 `INVERSE_SLEEVE_MAX_PCT`(默认 25%)。

- **⚠ 唯一会以新方式亏钱的功能 → 完全照 pattern 策略的规矩走：默认关、未验证、代码就位**。owner 必须先跑 `python -m scripts.inverse_sleeve_backtest` 过双窗闸门（用 DAILY bars，绕开 HOUR_1 ~150d 上限，给出真实多年窗口；判据 = 在部署天数里跑赢 SGOV 无风险 且回撤不离谱）才能开 `INVERSE_SLEEVE_ENABLED=true`。
- **隔离**：自己的 db-state 槽 `inverse_sleeve_position`（不进 open_trades，策略 executor 不碰它）；reconcile 把该 ETF 从 orphan 检测排除。`main.py` 扫描里在 cash_yield 之前调用（先占自己的 sleeve，剩下的现金再给 cash_yield 生息）。下单复用 `place_limit_order`。Web `GET/POST /api/inverse-sleeve`。
- 验证：14/14 纯函数单测（indicators / entry / exit 各分支 / size 封顶 / regime 序列）+ 全量 import clean。回测脚本需 OpenD 在线、由 owner 跑。

## 2026-06-23 — Phase 2：券商 App 式 AI 智能退出 / 加权选股 / 期权流骨架

复刻 券商个股 AI 分析卡的能力（新闻情绪 + 技术 + 期权异动），接进现有风控。全部默认关、FAIL-SAFE、Gemini 全用 ≥3.5-flash。

- **2A AI 智能退出** — `src/smart_exit.py`(新) + `ai_validator.assess_exit()`：持仓盘中遇
  「具体坏消息/分析师下调/板块转空」(AI) 或「技术明确破位且已盈利」(算法锁盈) 时强制提前退出，
  不等价格 TP。接在 `executor.manage_open_trades` 现有 gap-sentinel 块旁（5 分钟 tick），命中走
  **同一个 `_force_close`**（零新增执行代码）。配置 `SMART_EXIT_*`（默认关）。`SMART_EXIT_MIN_PROFIT_R`
  门控算法锁盈（浮盈≥N×R 才落袋，避免割没赚的单；AI 坏消息路径不受此限）。已验证：算法锁盈/盈利门控/
  禁用/无 key FAIL-SAFE 全正确（实测一次 Gemini 撞到 Google 地区限制 400→正确 HOLD 不崩）。
- **2B 券商 App 式加权选股** — `ai_validator.assess_sentiment()`：每个买入候选用 Gemini 融合
  新闻+分析师目标价方向+技术理由(sig.reasons)→ 0-100 看好度（50中性）。`main.py` 评分处接入，
  **默认 advisory**（写审计 extra，不改成交集→保回测平价）；可选 `SENTIMENT_SIZING` 折进 conviction
  仓位（仍不改选择）。配置 `SENTIMENT_*`（默认关）。
- **2C 宽 TP 验证** — 回测网格证明用户「降 TP/SL 增收益」直觉是**反的**：降 TP/SL 不增加交易数、
  反砍收益（把赢家提前砍掉）、收紧 SL 直接亏；**调宽** TP（2.0/2.5×ATR）才最优。受 券商小时数据
  ~150d 上限所限无法做真多窗口验证，故不自动改 `.env`，留 SIMULATE 观察后由 owner 定。
- **2D 期权异动（已激活并接入）** — `src/options_flow.py`(新)：volume≫OI、put/call 偏斜检测。
  owner 订阅美股期权 LV1 + 重启 OpenD 后**数据已通**（实测 MU/AAPL 返回真实 volume+未平仓）。
  `assess()` 只取**平值附近** ±15% 行权价（不抓全 850 腿）、**自带 20s 线程超时**（shutdown
  wait=False，绝不卡住 5 分钟 tick / 扫描循环）。已接入 **2A**（smart_exit：喂给 AI 退出 prompt +
  「期权流看空且盈利→锁盈」硬规则）和 **2B**（assess_sentiment 第 4 因子）。默认关
  （`OPTIONS_FLOW_ENABLED=false`），开启后才参与。修了两个 bug：option_type 列名冲突、
  ThreadPoolExecutor `with` 的 shutdown(wait=True) 抵消超时。
- **API/订阅健康看门狗** — `src/health_check.py`(新) + `HEALTH_CHECK_*` 配置（**默认开**，owner 要求）。
  每 30 分钟 + 启动时探测 ① 券商期权数据 ② Gemini API，**边沿触发**（仅状态变化才发 Telegram，
  带 2 次去抖防闪断刷屏）：订阅过期/无权限 → 告警续订；Gemini 配额/余额耗尽/key 失效/地区限制 → 告警
  充值（级联已是单模型 3.5-flash 无静默降级）。瞬时错误（OpenD 离线、503）归类 skip 不误报。挂在
  `main.py` 调度器（紧邻现有 17:30 `_watchdog_job`）。

## 2026-06-22 — AI 形态识别策略（第 4 套策略）

复刻 Autochartist/Trading Central 类「AI pattern signal」能力，作为与 trend /
momentum_break / mean_revert 并列的第 4 套策略，输出同构 `Signal` 走同一风控漏斗。

- **`src/pattern_detect.py`（新）** — 纯 numpy/pandas 几何 + 蜡烛检测（无新依赖）：
  区间突破、双底、上升/对称三角、头肩底、下降楔形、牛旗，及锤子/看涨吞没/启明星。
  每个形态返回 confidence + 关键价位（突破/颈线、目标、止损）。核心防误报闸：形态
  垂直幅度须 ≥ max(1.2×ATR, 2%)，否则平盘噪声会被误判成形态（实测噪声分从 77→64）。
- **`src/pattern_vision.py`（新）** — matplotlib 渲染 K 线图 → Gemini 视觉确认（复用
  `ai_validator` 的 client/key 轮转/级联/容错）。便宜固定模型 + 每扫描预算控成本；
  FAIL-SAFE：视觉不可用一律放行，绝不静默杀掉算法信号。
- **`src/strategy_pattern.py`（新）** — 0-100 加权评分（形态质量 30 / 触发 25 /
  量 20 / 趋势对齐 15；视觉占余下 10），与其他策略同 `evaluate(symbol, df)` 契约。
- **接线** — `main.py` 评分处 + 视觉确认处各加一段（视觉默认 advisory，
  `PATTERN_VISION_BLOCKING` 可让高置信 reject 否决进场）；`backtest_v3.py` 纳入回测
  （仅算法，视觉 live-only）；`executor`/web 看板展示形态名徽章。
- **默认全关**（`PATTERN_ENABLED=false`，同 MR/gap-sentinel 惯例）—— 上线真金前须先
  `PATTERN_ENABLED=true` 跑 `backtest_v3` 过双窗口验证。`Signal` 新增 `meta` 字段携带
  形态元数据（向后兼容，其余策略留空）。
- **准入过滤器（2026-06-23）** — `PATTERN_MIN_CONFIDENCE` / `PATTERN_REQUIRE_TRIGGERED` /
  `PATTERN_ALLOWED_TYPES`（config.py + strategy_pattern._admit，默认全惰性）。起因：
  未过滤的全集在 180d/10 半导体回测上净负（$16.4→$8.7/day，maxDD 5.8%→17%，被弱突破
  抓假顶拖累）。
- **回测验证结论（180d/10 半导体，algo-only，未含 live-only 视觉层）**：收紧后从「明显负」
  救回「大致中性」。最佳档 = 双底+上升三角、conf≥80、必须 triggered（$16.75 vs 基线
  $16.37/day，仅 +2 笔），但 **maxDD 翻倍 5.8%→11.8%、PF 3.63→3.12**——回报增量在噪声内
  且回撤变差，**未过「不恶化回撤」的 gate**。故 `PATTERN_ENABLED` 维持 false。
- **360d 复测无效（数据限制，2026-06-23）**：OpenD 的 HOUR_1 历史封顶 ≈679 根
  （约 150 日历天），360d 配置被截断成与 180d 同一批 bar → 交易集完全相同（同样 +2 笔、
  同样 maxDD 11.8%）。**小时线拿不到真正独立的第二窗口**，双窗口验证对 HOUR_1 不可用。
  结论不变：无可证 edge，`PATTERN_ENABLED` + `PATTERN_VISION_ENABLED` 均保持 false。若日后
  想真验证，需改用 DAILY bar（历史更长）或外部数据源（如 yfinance 730d 小时线）。

## v2.0 — 2026-06-10 ~ 06-11 「测量诚实化 + 动态股池 + 全自治」大版本

这次升级的起点是一个发现：**回测说 +$36/day，实盘（paper）却是 −$572（43 笔、胜率 27.9%）**。
逐层审计后证明两边跑的根本不是同一个策略，且回测引擎本身带系统性乐观偏差。
本版本分三阶段修复，并以「所有改动必须过诚实引擎双窗口验证」为铁律重建了全部数字。

---

### Phase 0 — 修测量仪器（回测↔实盘平价）

**诊断（PARITY 审计）**
- 83% 实盘买单分数 <70（已删除的 marginal band 放入，中位 62）——实盘亏损主力是回测从未背书的低门槛策略
- 回测股池是 in-sample 钉死的「10 赢家」，实盘 24 个 symbol 仅 5 个在内
- 出场不对等：实盘软止损均 −1.38R vs 回测精确 −1R（≈$212 差额）；盘中 TP 触价实盘全错过
- 进场不对等：回测 60 分钟成交窗 vs 实盘 5 分钟限价 TTL + 可能隔夜 stale 的挂单价
- 12/43 笔来自回测不存在的机制（8 笔孤儿恢复单 −$193、4 笔手动）

**复现实验**（`scripts/repro_live_window.py`，同一 23 天窗口）
| 配置 | 引擎 | 实盘现实 |
|---|---|---|
| 5 月实盘配置（24 股/thr60/5 月风控） | **+$414** | **−$572** → 引擎乐观偏差 ≈$986/23d |
| 当时新配置（10 股钉死/thr70） | −$56，盯市回撤 18.3% | （几乎无实盘样本） |

**修复（回测侧，5 个 realism 旗标，默认关、`_run_live_engine` 统一开，parity diff-test 字节不变）**
`scan_grid_exits`（软止损按 bar open/close 快照成交）· `entry_fill_open_only`（5min TTL 近似）·
`no_same_day_daily`（日线闸去当日收盘 lookahead）· `reclamp_position_cap`（乘数后重夹仓位上限）·
`apply_trade_windows`（09:45–15:30，周五 14:00 后禁新仓）

**修复（实盘侧）**
5 分钟持仓管理 tick（修止损滞后 + TP 错过）· `open_trades` 进程内锁（孤儿单根因之一）·
入场限价按实时快照重定价 + 0.2% 追价容差 · 扫描对齐 :01/:31 ·
孤儿领养改真 ATR(14) · 过期部分成交单保留 dealt 数量（修 `FILLED_PART` 漏网）·
统计剔除 orphan/MANUAL 至独立 `ops` 桶 · weekly 回测断网拒绝覆盖结果 · scale-out 死配置启动校验

**修正后真基线（当时 10 股钉死池）**：90d **$17.9/day**（旧口径 $36.47 被砍 51%）· 140d $11.5 · 最近 23d −$3.4

---

### Phase 1 — 规则化动态股池（杀幸存者偏差）

- `config/universe_pool.json`：72 个流动性大盘股（8 行业，纯流动性入选，**禁止按表现增删**）
- `src/universe.py`：6-1 动量（126 日收益跳过 21 日，Jegadeesh-Titman 标准，零参数拟合）
- 回测 walk-forward：每 ISO 周用该周之前的日线重算 top-N；实盘周日 22:00 自动刷新 + Telegram 通知

**验证**（诚实引擎，vs 钉死 10 股基线）
| 窗口 | 钉死 10 股 | 动态 top-15 |
|---|---|---|
| 140d | $11.5/day | **$19.25/day**（32 笔，WR 56.2%，PF 3.28，盯市 DD 11.0%） |
| 90d | $17.9 | $29.94 |
| 最近 23d | −$3.4 | +$27.95 |

top-N 平原：10=$12.2 / **15=$19.25** / 20=$18.57（取 15，处在平原非孤峰）
⚠ 23 天级别窗口符号不稳定（同配置隔日复测 −$8.49）——所有自动决策禁止基于 <90d/<30 笔。

---

### Phase 2 — 出场审计 + 全自治层（autopilot）

**出场消融（10 个引擎变体，140d 动态池）**
| 变体 | $/day | 结论 |
|---|---|---|
| 基线 | 18.81 | — |
| thr 65 / 75 | 13.33 / 12.90 | threshold 在平原峰值，资金瓶颈非信号瓶颈（80 次现金墙挡单 vs 32 成交） |
| TP 6 / 10 ×ATR | 8.68 / 11.38 | TP=8 在峰值 |
| max_hold 5 / 10 天 | 12.58 / 11.75 | 7 天在峰值；MAX_HOLD 桶净 +$833，别动 |
| Chandelier trail | 11.75（横盘 +$62） | regime 依赖件，留给未来条件化出场 |
| 可触及 scale-out | 14.25（横盘 +$25） | 同上 |
| **保本棘轮 +1R** | **18.03，PF 3.44，DD 9.77%** | **唯一双窗口全面占优的升级 → 已采纳** |

**保本棘轮 A/B（同缓存）**：OFF $19.09/PF 3.33/DD 11.57% → **ON $19.97/PF 3.81/DD 8.91%**（三优，默认开）

**出场平价修复**：实盘 TP 改触价全仓平（原 TP_HALF+EMA20 路径从未被验证，而 TP 桶≈全部净利）；
STALL_OUT 默认关（实盘 2 笔全亏）；引擎补建模「每扫描最多 2 新仓」。

**AI 顾问**：advisory 模式改下单后调用（拿掉下单前中位 42s/p90 99s 延迟）；`ai_score` 开始持久化
（33 笔旧样本显示 AI-pass 组反而更差——攒 50 笔干净样本后做去留判定）。

**全自治（autopilot）**
- 参数热加载：7 个可调参数全部免重启生效（`runtime_config`，新增 breakeven_trigger_r/max_hold_days/universe_top_n）
- `AUTO_APPLY_PARAMS`：周度提案过诚实引擎 180/360d 双窗闸 + 在预批边界内 → 自动应用 + Telegram 通知；越界 → 审批队列
- 自动回滚：应用后实盘 ≥15 笔且 avg R ≤ −0.30 → 撤回 + 28 天冷却 + 通知（owner 手批的参数永不自动回滚）
- 每日健康看门狗（17:30 ET）：扫描停摆 / 垃圾回测结果 / 逾期任务 / 账实偏差 / 运行时覆盖提醒——仅有问题时通知
- 优化器防自欺：weekly 健康检查 + Optuna 全部使用与实盘一致的配置（动态池 walk-forward + runtime 参数）；
  Optuna 移除 base_slip_bp 搜索维度（乐观滑点刷分漏洞）、TP 搜索上界 8→11

---

### 当前诚实口径（v2.0 完整配置：动态 top-15 + 保本 + 全部 realism）

- 90d：**35 笔，净 +$2,525 ≈ $28/day，盯市回撤 9.77%**（`data/backtest_results.json` 即此口径）
- 预期管理：引擎口径 ~$19–28/day 含顺风窗口成分；**live 合理预期 $10–15/day**（文献衰减先验）
- 校准参照：$50/day@$5k = 年化 +1,127%，是 Medallion（66%/yr）的 17 倍、巴西 19,646 名日内交易者研究中
  0.04% 分位——**$50/day 是复利里程碑而非日费率**；40% CAGR ≈5.5 年，若 $23/day 成立 ≈12 个月

### 已证伪清单（别再走）
ML 分类器（AUC≈0.5）· RS-vs-SPY 闸 · 板块 ETF 闸 · 静态扩股池 · 加并发仓位 · 均值回归叠加 ·
浮盈加仓 · 单票 100% 集中 · 强制日内平仓 · 逆风强制做多 · 反向 ETF 熊市仓（200dma 下方做空胜率仅 29%）·
threshold/TP/SL/max_hold 调参（全部已在平原峰值）

### 升级后首次启动
1. 复制 `.env.example` → `.env` 填入密钥（已有 `.env` 的跳过）
2. 启动 OpenD 并解锁交易
3. `bash start-scheduler.command` — catchup 会自动补跑股池刷新等任务，Telegram 推送新 top-15 名单
