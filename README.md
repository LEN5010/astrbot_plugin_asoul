# 枝江自动推送

AstrBot 插件，提供 A-SOUL 直播日历查询，以及 B 站动态、视频、直播和评论监控。通知统一使用「爱驼推送」图片卡片。

## 功能

- 查询今日、明日和本周直播日历
- 日历卡片随机展示成员表情包，支持全局特别关注标记
- 按 UID 错峰监控 B 站动态、视频和直播状态
- 内容监控与评论抓取独立调度
- 评论区头部优先检查、楼中楼按 rcount/缺口驱动补扫、SQLite 去重与逐群投递确认
- 动态、视频、直播、评论统一卡片；渲染失败自动回退文本格式
- 转发动态分区展示原内容，开播 `@全体` 不可用时自动降级发送

## 指令

### 日历

| 指令 | 说明 |
| --- | --- |
| `今日直播` | 查询当天日程 |
| `明日直播` | 查询次日日程 |
| `本周直播` | 查询本周日程 |


### 日程特别关注

以下指令仅限管理员，标记结果全局生效。

| 指令 | 说明 |
| --- | --- |
| `/日程高亮 YYYY-MM-DD` | 列出当天日程 |
| `/日程高亮 YYYY-MM-DD 序号 [粉色\|红色\|白金色]` | 标记特别关注 |
| `/取消日程高亮 YYYY-MM-DD 序号` | 取消标记 |
| `/日程高亮列表` | 查看已保存标记 |
| `/取消日程高亮记录 序号` | 移除已失效日程的记录 |

### B 站管理

| 指令 | 说明 |
| --- | --- |
| `/bili_status` | 查看内容与评论任务状态 |
| `/bili_login` | 私聊二维码登录 |
| `/bili_logout` | 清除运行期登录态 |
| `/bili_test_dynamic UID` | 测试动态抓取与卡片 |
| `/bili_test_video UID` | 测试视频抓取与卡片 |
| `/bili_test_live UID` | 测试直播抓取与卡片 |
| `/bili_test_comment UID` | 测试评论抓取与卡片 |
| `/bili_test_all UID` | 执行全部测试抓取 |
| `/bili_test_atall` | 测试当前群 `@全体` 能力 |
| `/bili_dump_dynamic UID` | 导出动态原始响应 |
| `/bili_dump_live UID` | 导出直播原始响应 |

## 配置

配置项以 `_conf_schema.json` 为准，常用项如下。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 启用 B 站自动播报 |
| `group_whitelist` | `[]` | 主动推送群白名单 |
| `target_uids` | 内置成员 UID | 动态、视频、直播监控目标 |
| `comment_target_uids` | `[]` | 评论目标；留空时沿用 `target_uids` |
| `poll_interval_seconds` | `300` | 内容轮询周期，最小 60 秒 |
| `task_gap_seconds` | `20` | UID 任务错峰间隔 |
| `push_dynamic` | `true` | 推送新动态 |
| `push_video` | `true` | 推送新视频 |
| `push_live` | `true` | 推送开播 |
| `push_comment` | `false` | 启用评论抓取 |
| `comment_request_interval_seconds` | `2` | 评论接口全局最小间隔 |
| `render_bilibili_cards` | `true` | 使用统一图片卡片 |
| `request_client` | `aiohttp` | B 站请求客户端 |
| `calendar_cache_minutes` | `30` | 日历缓存时间 |

白名单群需要先接收过一条消息，插件才能记录 `unified_msg_origin` 并主动推送。B 站凭据可通过配置填写，也可使用 `/bili_login` 保存运行期登录态。

## 运行行为

- 首次内容轮询只建立基线，不回放历史动态、视频或开播事件。
- 评论覆盖每个资源所有者最近 3 条非视频动态和最近 3 个视频。
- 评论基线按资源发布时间划分（缺省时回退短宽限）；事件与逐群投递状态持久化在 SQLite。
- 已删除楼中楼标记终态不再重试；不再做 24 小时全量 safety 轮询。
- 内容或评论只有在对应群发送成功后才确认游标，失败任务会保留并重试。
- 开播消息优先尝试 `@全体`；平台不支持或实际发送失败时去掉 `@全体` 重发。
- `render_bilibili_cards=false` 时直接使用文本、图片和链接格式。
- 升级不会主动清理现有 KV、评论数据库、游标、缓存、群配置或特别关注记录。

## 素材

成员表情包目录：

```text
贝拉/
嘉然/
乃琳/
心宜/
思诺/
```

支持 PNG、JPG、JPEG、WebP、GIF 和子目录。每条日程独立抽取，同一张卡内同一成员优先不重复。字体使用插件目录下的 `font.ttf` 或 `font.otf`。

## 依赖与数据

- Python 3.10+
- Pillow
- bilibili-api-python
- aiohttp、httpx 或 curl_cffi
- qrcode >= 8.2

直播日历来自 [枝江站](https://asoul.love/calendar.ics)，B 站能力基于 [bilibili-api](https://github.com/Nemo2011/bilibili-api)。

## License

本项目使用 GNU Affero General Public License v3.0，详见 [LICENSE](LICENSE)。
