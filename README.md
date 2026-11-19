# MinecraftTranslationToolkit

MCTTK（矿译通）把 Minecraft 官方新闻全流程自动化：**爬取 → AI 翻译 → 格式转换 → 发布**。
它盯几个固定的新闻来源，把新文章翻成中文、转成论坛与专栏能直接用的排版，再发到 MCBBS
幻翼块讯和 B站专栏 —— 并且按版规自动设置标题高亮、按分类维护「最新版本加粗」。

- 定时运行：容器里每小时一轮，也可以交给 GitHub Actions
- 新闻来源：Minecraft 官方 API、Feedback 网站、Piston 版本清单
- 发布目标：MCBBS 论坛帖（BBCode + 图片/JSON 附件）、B站专栏（HTML / opus 正文）
- 失败可重试：处理失败的文章下次自动重试，超过上限才放弃
- 安全去重：基于 URL 的状态追踪，不会重复爬取或重复发布

## 特性

- **智能翻译**：OpenAI 兼容接口 + 命中注入式术语表，保证译名一致；长文章分批、截断自动
  拆分重试
- **按类型排版**：Java 快照 / 预发布 / RC / 正式版、基岩版更新 / 测试版、块讯 / 时评，
  各自套对应的头部与尾部模板（`config/modules_config.json`）
- **自动发帖**：登录 MCBBS 发帖（含验证码识别），失败重试；发完自动补标题高亮
- **高亮与加粗**：按版规「二、高亮设定」上色；按「Java/基岩 × 正式版/测试版」四条版本线
  各自只保留最新一篇加粗
- **类型过滤**：配置里指定只发布哪些类型的新闻
- **首次运行保护**：首次运行会把现有新闻全部标记为已处理，避免一次性刷屏

## 快速开始

```bash
# 1. 装依赖（可选扩展：--extra captcha 验证码识别 / --extra feedback Feedback 抓取）
uv sync --extra captcha --extra feedback

# 2. 配置：复制模板后填 API Key 与账号
cp config/config.sample.json config/config.json     # Windows: copy
cp .env.sample .env                                  # 放 OPENAI_API_KEY / MCBBS_USERNAME / MCBBS_PASSWORD

# 3. 先空跑看看能抓到什么（不翻译、不发帖）
uv run python main.py --dry-run

# 4. 真正跑一轮：抓取 → 翻译 → 转换 → 发布
uv run python main.py
```

容器化部署（含离线部署）见 [docs/DOCKER.md](docs/DOCKER.md)：

```bash
docker compose up -d --build     # 本地构建并启动
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/USAGE.md](docs/USAGE.md) | 完整用法：全部配置项、CLI 参数、日志、新闻来源、词汇表、注意事项 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南：环境与命令、代码规范、测试与验证、PR 标准、发版流程 |
| [docs/DOCKER.md](docs/DOCKER.md) | Docker / 服务器部署、离线部署、镜像与版本说明 |
| [docs/archive/](docs/archive/) | 历史设计与评审记录 |

## 注意事项

- **配置与凭据**：`config/config.json`、`.env` 都在 `.gitignore` 里，请从模板复制；
  API Key 与论坛密码建议只放在 `.env`
- **无授权不要真跑发布**：`--dry-run` 只探测不落盘；真要验证发布请用测试目录
- **状态文件**：`output/.state.json`（已处理的新闻）、`.posted.json`（已发布的）、
  `.posted_bili.json`（已投稿的）、`.mcbbs_threads.json`（高亮追踪）—— 删除语义见
  [docs/USAGE.md](docs/USAGE.md#注意事项)

## 许可证

本工具以 [GPL-3.0](https://www.gnu.org/licenses/gpl-3.0.zh-cn.html) 协议发布。

AI 翻译作品以 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.zh-hans) 协议发布。
