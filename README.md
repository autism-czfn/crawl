# Autism Crawler（自闭症内容爬虫）

自动采集与自闭症相关的学术论文、社区讨论、新闻资讯，并存入 PostgreSQL 数据库，支持向量语义搜索。

---

## 系统架构概览

```
config/surfaces.json（68 个数据源，含信任等级元数据 + 多语言支持）
        │
        ▼
   Scheduler（调度器）
   每分钟检查一次，按各数据源的配置间隔触发采集
        │
        ├─► Collector（采集器）× 22 种平台
        │       Reddit / RSS / PubMed / Europe PMC /
        │       Semantic Scholar / CrossRef / bioRxiv /
        │       DOAJ / OpenAlex / ClinicalTrials /
        │       CORE / Wikipedia / Hacker News /
        │       YouTube / NewsAPI / HTML 爬虫 /
        │       Playwright 爬虫 / Sitemap / NHS API /
        │       CDC Data API
        │
        ▼
   Pipeline（入库管道）
   URL 去重 upsert → crawled_items 表
   自动注入来源信任元数据（authority_tier / source_type / audience_type）
   内容哈希比对（SHA-256），跳过未变更内容
   支持 force_recrawl 强制重新采集
        │
        ├─► Chunk Pipeline（分块管道）
        │   每 15 分钟将 content_body 分割为 500-1000 token 块
        │   存入 chunks 表，支持分块级语义搜索
        │
        ▼
   Embedding Loop（向量化循环）
   每 15 分钟批量生成 768 维向量（fastembed 本地模型）
   将标题 + 摘要转为向量，写入 pgvector
        │
        ▼
   PostgreSQL + pgvector
   表：crawled_items / chunks / surfaces / http_cache
```

---

## 核心组件说明

### 1. 数据源配置（`config/surfaces.json`）

每个数据源称为一个 **Surface（采集面）**，配置项包括：

| 字段 | 说明 |
|------|------|
| `key` | 唯一标识，如 `pubmed_autism` |
| `platform` | 采集器类型，如 `pubmed`、`reddit`、`sitemap`、`nhs_api` |
| `poll_interval_sec` | 采集间隔（秒） |
| `max_items` | 每次最多采集条数 |
| `authority_tier` | 来源信任等级：1（官方/政府）、2（学术/医院）、3（非营利/参考） |
| `source_type` | 来源类型：`official_health` / `academic` / `nonprofit` / `community` / `news` / `social` |
| `audience_type` | 受众类型：`parent_facing` / `clinician_facing` / `mixed` |
| `language` | 语言代码（`en`、`zh`、`fr`、`de`、`ja`、`es`） |
| `country` | 国家代码（`US`、`UK`、`CN` 等） |
| `organization_name` | 来源机构名称 |
| `config` | 平台专属参数（subreddit 名称、API 查询词、sitemap URL 等） |

首次启动时，调度器自动将 `surfaces.json` 写入数据库（含所有元数据字段），后续可通过 Django 管理后台直接启停或调整。

**来源信任等级分布（68 个数据源）：**

| 等级 | 数量 | 代表来源 |
|------|------|----------|
| Tier 1（官方/政府） | 17 | CDC、NIH/NIMH、NHS、NICE、AAP、FDA、各国卫生部、CDC Data API |
| Tier 2（学术/医院） | 18 | PubMed、Mayo Clinic、Europe PMC、Semantic Scholar、ClinicalTrials |
| Tier 3（非营利/参考） | 16 | Autism Society、Wikipedia、Spectrum News、ASAN |
| 未分级（社区） | 17 | Reddit、YouTube 个人频道、Hacker News |

---

### 2. 调度器（`src/scheduler.py`）

- 每 **60 秒** 检查所有已启用的 Surface
- 若距上次运行时间 ≥ `poll_interval_sec`，则触发对应采集器
- 所有 Surface 的采集任务**并发执行**（`asyncio.gather`）
- 每次运行后更新 `last_run_at`、`last_status`、`last_error`、`consecutive_fails`
- 支持 `force_recrawl` 标志，强制重新采集（忽略游标和间隔）
- 支持 22 种平台（通过 `_COLLECTOR_MAP` 映射采集器模块）

---

### 3. 采集器（`src/collectors/`）

每个采集器实现统一接口：

```python
async def collect(config, cursor, limit) -> tuple[list[CollectedItem], next_cursor]
```

- `cursor`：分页游标，支持断点续采
- 返回标准化的 `CollectedItem`（标题、URL、摘要、正文、作者、DOI、发布时间等）

**22 种平台对应的采集策略：**

| 类型 | 平台 |
|------|------|
| 学术 API | PubMed、Europe PMC、Semantic Scholar、CrossRef、bioRxiv/medRxiv、DOAJ、OpenAlex、CORE |
| 临床数据 | ClinicalTrials.gov |
| 官方健康 | NHS UK（内容 API）、CDC Data API（公共卫生数据集）|
| 社区 | Reddit（多个子版块）、Hacker News |
| 媒体 | RSS 订阅源、NewsAPI、YouTube |
| 百科 | Wikipedia |
| 网页爬取 | HTML 爬虫、Playwright 爬虫（Cloudflare 绕过） |
| Sitemap | 通用 Sitemap XML 采集器（Mayo Clinic 等） |

**HTML / Playwright 爬虫特性：**

- 内容提取优先级：JSON-LD → Open Graph → 每站 CSS 选择器
- **正文提取**（`_extract_body`）：自动去除导航/页脚/侧栏，提取主体文本
- **路径过滤**：`allowed_paths` / `excluded_paths` 配置项，使用 `fnmatch` 模式匹配
- **爬取深度控制**：`max_crawl_depth` 配置项（默认 2）
- 人类行为模拟：随机延迟、首页预访问、favicon 预取

**NHS API 采集器特性：**

- 通过 NHS Content API v2 获取结构化健康页面内容
- 需 NHS Developer 订阅密钥（`NHS_API_KEY` 环境变量）
- 自动提取 `mainEntityOfPage` 正文，支持按 slug 批量获取
- 按 NHS 服务条款，至少每 7 天刷新一次

**CDC Data API 采集器特性：**

- 通过 SODA REST API（data.cdc.gov）获取公共卫生统计数据集
- 主要用于自闭症流行病学与患病率数据
- 无需 API 密钥，支持 SoQL 查询过滤

**Sitemap 采集器特性：**

- 通用 Sitemap XML 采集器，可复用于任何提供 sitemap 的网站
- 解析 sitemap XML → 按 `filter_path` 过滤 URL → 抓取 + 解析页面
- 支持 sitemap index（嵌套 sitemap 文件）自动展开
- 当前用于 Mayo Clinic，可扩展至其他站点

---

### 4. 入库管道（`src/pipeline.py`）

- 以 **URL 为唯一键**，执行 PostgreSQL upsert（`ON CONFLICT DO UPDATE`）
- 重复 URL 只更新 `engagement`、`rank_position`、信任元数据等动态字段
- **来源信任元数据注入**：从 Surface 配置自动查询 `authority_tier`、`source_type`、`audience_type` 并写入每条记录（含 upsert 时更新）
- **内容变更检测**：对 `content_body` 计算 SHA-256 哈希（`content_hash`），跳过未变更的内容，更新 `content_updated_at` 时间戳
- **强制重采集**：Surface 的 `force_recrawl` 标志可跳过间隔检查和游标，完成后自动重置
- DOI 唯一索引冲突（同一论文不同 URL）自动跳过
- 通过 **Unpaywall API** 补充开放获取状态

---

### 5. 分块管道（`src/chunk_pipeline.py`） — 新增

- 每 **15 分钟**运行一次
- 查找有 `content_body` 但尚未分块的条目
- 使用 `src/chunker.py` 将正文分割为 **500-1000 token** 的块
- 分块策略：段落边界 → 句子边界 → 合并小段
- 分块存入 `chunks` 表，每块独立生成向量

---

### 6. 向量化循环（`src/embeddings.py`）

- 每 **15 分钟**运行一次，处理尚未向量化的条目（每批最多 500 条）
- 将 `title + description[:500]` 拼接后调用 **fastembed** 本地模型
- 模型：`nomic-ai/nomic-embed-text-v1.5`（768 维，无需 API Key）
- 生成的向量存入 `crawled_items.embedding`（pgvector 类型）

---

## 数据库表结构

### `crawled_items` — 采集内容

| 字段 | 说明 |
|------|------|
| `id` | 自增主键 |
| `url` | 唯一索引，去重依据 |
| `title` / `description` / `content_body` | 标题、摘要、正文 |
| `source` / `surface_key` | 来源平台和采集面标识 |
| `doi` / `journal` / `open_access` | 学术论文专属字段 |
| `authors_json` | 作者列表（JSONB） |
| `engagement` | 互动数据（点赞数等，JSONB） |
| `authority_tier` | 来源信任等级（1/2/3，从 Surface 配置注入） |
| `source_type` | 来源类型（official_health / academic / nonprofit 等） |
| `audience_type` | 受众类型（parent_facing / clinician_facing / mixed） |
| `content_hash` | 正文 SHA-256 哈希，用于变更检测 |
| `content_updated_at` | 正文最近更新时间 |
| `embedding` | 768 维语义向量（pgvector） |
| `collected_at` | 入库时间 |

### `chunks` — 内容分块 — 新增

| 字段 | 说明 |
|------|------|
| `id` | 自增主键 |
| `crawled_item_id` | 关联的 crawled_items.id |
| `chunk_index` | 块在文档中的位置序号 |
| `chunk_text` | 分块文本内容 |
| `embedding` | 768 维分块向量（pgvector） |
| `embedding_model` | 向量模型名称 |
| `embedded_at` | 向量生成时间 |

### `surfaces` — 采集面状态

记录每个数据源的运行状态和信任元数据，可通过 Django 后台管理。

信任元数据字段：`authority_tier`、`source_type`、`audience_type`、`language`、`country`、`organization_name`

控制字段：`force_recrawl`（布尔值，触发强制重新采集，完成后自动重置为 false）

### `http_cache` — HTTP 缓存

存储 ETag / Last-Modified，支持条件请求，减少重复流量。

---

## 快速启动

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，填写 DATABASE_URL、API 密钥等

# 2. 安装依赖（含 Django 管理后台）
pip install -r requirements.txt

# 3. 执行数据库迁移（包含新增的信任元数据和 chunks 表）
alembic -c src/storage/migrations/alembic.ini upgrade head

# 4. 运行管理菜单
bash setup.sh
```

**管理菜单选项：**

| 选项 | 功能 |
|------|------|
| 1 | 启动/重启 爬虫 + Django 管理后台 |
| 2 | 查看服务运行状态 |
| 3 | 执行数据库迁移（Alembic + Django） |
| 4 | 显示管理后台地址 |
| 5 | 创建 Django 超级用户 |

---

## Django 管理后台

后台运行于 **http://localhost:8001/admin/**，提供：

- **Surface 监控**：各数据源的运行状态、信任等级、最近错误、连续失败次数，可直接启停
- **内容浏览**：按来源/平台/信任等级筛选已采集内容，支持标题、DOI、作者搜索
- **HTTP 缓存**：查看缓存状态

首次使用需先通过菜单选项 5 创建超级用户。

---

## 环境变量说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `DATABASE_URL` | ✅ | asyncpg 连接串，供爬虫使用 |
| `DATABASE_URL_SYNC` | ✅ | psycopg2 连接串，供 Django 使用 |
| `REDDIT_CLIENT_ID/SECRET` | ☑️ 可选 | Reddit API 凭证 |
| `PUBMED_API_KEY` | ☑️ 可选 | 提升 PubMed 请求频率上限 |
| `NEWSAPI_KEY` | ☑️ 可选 | NewsAPI 访问密钥 |
| `YOUTUBE_API_KEY` | ☑️ 可选 | YouTube Data API 密钥 |
| `CORE_API_KEY` | ☑️ 可选 | CORE 全文搜索 API |
| `NHS_API_KEY` | ☑️ 可选 | NHS Content API v2 订阅密钥 |
| `CRAWLER_EMAIL` | ☑️ 可选 | 礼貌池标识（CrossRef、OpenAlex、Unpaywall） |

---

## 技术栈

- **Python 3.9+**，全程异步（`asyncio` + `asyncpg`）
- **SQLAlchemy 2.0**（异步 ORM）+ **Alembic**（数据库迁移）
- **PostgreSQL 16** + **pgvector**（向量相似度搜索）
- **fastembed**（本地语义向量化，nomic-embed-text-v1.5，无需 API Key）
- **httpx**（HTTP 客户端，支持速率限制与随机延迟）
- **BeautifulSoup4**（HTML 解析）+ **Playwright**（JS 渲染页面）
- **Django 4.2**（管理后台，`managed=False` 复用现有表）
