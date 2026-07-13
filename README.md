# astrbot_plugin_asoul

AstrBot 插件，用于查询 A-SOUL 今日 / 明日直播安排，并提供一个简短的 Bot 使用帮助入口。

同时支持轮询指定 B 站 UID 的：

- 新动态
- 新视频
- 开播信息
- 最近 3 条动态和最近 3 个视频评论区中，目标 UID 发表的新评论

并自动推送到配置白名单内、且已经被插件登记过会话来源的群聊。

## 功能

- 读取 `https://asoul.love/calendar.ics`
- 清洗指定日期的直播数据，合并同时间同内容的多人直播
- 使用 `Pillow` 本地绘制直播卡片图片
- 支持在卡片右侧展示当前场次对应成员头像
- 提供 `/bot帮助` 文本帮助
- 支持轮询指定 B 站 UID 的动态 / 视频 / 直播并主动推送到白名单群
- 支持独立的 180 秒评论轮询、600 秒资源发现、楼中楼增量抓取和按群投递确认

## 指令

### 直播查询

发送以下任一消息：

- `今日直播`
- `明日直播`
- `本周直播`

插件会返回一张直播安排图片，包含：

- 开播时间
- 主播 / 团播成员
- 直播内容
- 当前场次对应头像

说明：

- `今日直播` 查询当天直播
- `明日直播` 查询下一天直播
- 如果当天是周日，发送 `明日直播` 会返回 `还没有下周的直播排表哦`

### Bot 帮助

发送以下任一消息：

- `/bot帮助`
- `bot帮助`

返回内容为：

```text
鸣潮bot请使用【ww帮助】获取图文
自动签到请使用【ww登陆】，然后输入【ww开启自动签到】
asoul推送请使用【今日直播】、【明日直播】或【本周直播】
```

### B站评论推送状态

管理员使用：

- `/bili_status`

该命令显示评论推送开关、登录状态、请求客户端、任务状态、目标群数量，以及每个 UID 最近一次轮询结果、错误分类和下次最早轮询时间。

## 依赖

本插件当前以本地图片渲染为主，运行环境需要：

- Python 可用
- `Pillow` 已安装
- 若启用 B 站自动播报，还需要：
- `bilibili-api-python`
- 异步请求库，推荐 `aiohttp`

如果 `Pillow` 不可用，直播图片无法正常生成。

其中，B 站相关能力明显依赖：

- [Nemo2011/bilibili-api](https://github.com/Nemo2011/bilibili-api)

当前以下功能直接建立在该库之上：

- B 站二维码登录
- UID 动态抓取
- UID 视频抓取
- 直播状态查询
- 最近资源评论抓取

如果没有正确安装 `bilibili-api-python`，上述 B 站功能将无法正常工作。

## 素材文件

插件目录下可放置以下素材：

- `贝拉.png`
- `嘉然.png`
- `乃琳.png`
- `心宜.png`
- `思诺.png`
- `font.ttf` 或 `font.otf`

说明：

- 头像建议使用透明底 PNG
- 字体文件建议使用完整支持简体中文的字体
- 如果存在 `font.ttf` 或 `font.otf`，插件会优先使用它

当前仓库里已经包含一份示例字体文件：

- `font.ttf`

## 数据来源

直播数据来源：

- `https://asoul.love/calendar.ics`

插件会做内存和磁盘缓存，避免每次查询都重新拉取日历。

默认缓存 30 分钟，并会把最近一次成功拉取的 ICS 保存到本地 `temp/calendar_cache.json`。缓存过期后会优先使用 `ETag` / `Last-Modified` 做条件请求；源站返回未修改或短暂不可用时，会继续复用本地缓存以降低请求量。日历请求的 `User-Agent` 为 `asasfans`。

可在插件配置中调整：

- `calendar_cache_minutes`：直播日历缓存时间，默认 `30`，小于 `10` 会按 `10` 处理。

## 说明

- 插件按 `Asia/Shanghai` 时区处理今天 / 明天的直播
- 会过滤取消事件和全天事件
- 团播会按识别到的成员名称合并展示
- 如果本地图片绘制失败，会退回纯文本结果
- B 站主动播报只会对白名单群生效
- B 站轮询默认按 UID 错峰调度：`poll_interval_seconds` 默认 300 秒，`task_gap_seconds` 默认 20 秒
- 新视频播报从动态流中识别，不再额外轮询独立视频列表
- 开播检测走直播状态批量接口，不再依赖更容易触发 412 的 `get_live_info`
- 白名单群必须先出现过一条消息，插件才能拿到 `unified_msg_origin` 并主动推送
- `push_comment` 单独控制“最近资源评论抓取”，该能力更容易触发风控，默认关闭
- 评论子系统请求按 2 秒固定间隔串行发送；插件重启后会继续遵守上次 UID 轮询的 180 秒冷却
- 每个资源只维护最新评论页的 20 个一级楼；资源目录每 600 秒刷新一次
- 评论接口异常不会推进评论游标，发送成功后才推进对应群的状态
- 评论接口仍按资源查询；插件只在维护窗口内筛选并推送 `target_uids` 账号发表的评论

## 开发

仓库地址：

- [astrbot_plugin_asoul](https://github.com/LEN5010/astrbot_plugin_asoul)

AstrBot 相关文档：

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)

B 站 API 依赖项目：

- [Nemo2011/bilibili-api](https://github.com/Nemo2011/bilibili-api)

鸣谢:

- [枝江站](https://asoul.love/)
