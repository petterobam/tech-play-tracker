# tracker · 科技比赛/补贴专项监控器

一个 ~250 行的 Python 脚本，**抓取 RSS + 政府通知页 → 关键词匹配 → 去重 → 推送**，让你不再靠人工盯政府网站。

## 能力

- **抓取**：标准 RSS（feedparser）+ 任意通知页 HTML（requests + BeautifulSoup，提取 `<a>` 链接）
- **过滤**：关键词正则匹配标题（在 `sources.yaml` 配）
- **去重**：SQLite 存 `title|url` 的 md5，只推送新增
- **推送**：飞书 / 钉钉（含加签）/ 企业微信 / Server酱（微信）/ 邮件 SMTP，可多通道并发
- **调度**：本地 cron、云函数、或随附的 GitHub Actions（免服务器）

## 三步跑起来

### 1. 装依赖
```bash
cd tracker
pip install -r requirements.txt
```

### 2. 配置
编辑 [`sources.yaml`](sources.yaml)：
- `keywords`：想盯的关键词（已有常用模板，可加减）
- `rss` / `pages`：要盯的政府源 URL（已预填科技部、工信部、北上深科委等）
- `notify`：填一个你用的推送通道的 webhook（其余留空即可）

> 来源大全见上级目录 [`04-数据源清单.md`](../04-数据源清单.md)。

### 3. 运行
```bash
# 先 dry-run，看抓到什么、命中什么（不推送、不入库）
python monitor.py --config sources.yaml --dry-run

# 调试某个源抓得对不对
python monitor.py --config sources.yaml --source 上海市科委 --show-all

# 正式跑（命中即推送 + 入库去重）
python monitor.py --config sources.yaml
```

## 推送通道怎么接

| 通道 | 怎么拿 webhook |
|---|---|
| **飞书** | 群设置 → 群机器人 → 添加"自定义机器人" → 复制 webhook |
| **钉钉** | 群设置 → 智能群助手 → 添加"自定义"机器人 → 安全设置选"加签" → webhook + secret 都填 |
| **企业微信** | 群聊 → 添加群机器人 → 复制 webhook |
| **Server酱** | sct.ftqq.com 微信扫码登录 → 拿 SendKey，命中推送到微信 |
| **邮件** | 填 SMTP（QQ/163/Gmail 都行），用 SMTP 授权码而非登录密码 |

> webhook 建议用**环境变量**传入而非写进 yaml（CI 更安全）。脚本会优先读环境变量：`FEISHU_WEBHOOK`、`DINGTALK_WEBHOOK`、`DINGTALK_SECRET`、`WECOM_WEBHOOK`、`SERVERCHAN_KEY`、`SMTP_*`、`MAIL_FROM`、`MAIL_TO`。

## 免服务器方案：GitHub Actions（推荐）

把 `tracker/` 作为独立仓库（或拷到现有仓库根目录），仓库 Setting → Secrets 填上 webhook（如 `FEISHU_WEBHOOK`），随附的 [`.github/workflows/policy-monitor.yml`](.github/workflows/policy-monitor.yml) 会**每天自动跑 3 次**，命中即推送。

- 完全免费（GitHub Actions 公开仓库无限额度，私有仓库每月 2000 分钟，本项目每次 <1 分钟）
- 无需服务器、无需本机常开
- 想改频率：编辑 workflow 里的 `cron`

## 自建服务器方案

```bash
# crontab -e  ，每天 8:00、14:00 各跑一次
0 8,14 * * * cd /path/to/tracker && /usr/bin/python3 monitor.py --config sources.yaml >> monitor.log 2>&1
```

## 常见问题

- **某个源抓不到？** 政府站改版了。`--show-all` 看抓到啥，必要时在 `sources.yaml` 给该源配 `item_selector`（CSS 选择器）限定列表区块。
- **推送太吵？** 收紧 `keywords`（去掉宽泛词如"通知"），或在脚本里加白名单域名。
- **重复推送？** 正常不会——`seen.db` 做了去重。换域名/换机器要重置时删 `seen.db` 即可。
- **合规**：脚本默认每个源只取最近 30 条，频率低，仅做信息聚合。请遵守各站 robots.txt，勿做高频抓取或数据转售。
