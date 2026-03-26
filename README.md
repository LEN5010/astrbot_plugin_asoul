# astrbot_plugin_asoul

AstrBot 插件，用于查询 A-SOUL 今日 / 明日直播安排，并提供一个简短的 Bot 使用帮助入口。

## 功能

- 读取 `https://asoul.love/calendar.ics`
- 清洗指定日期的直播数据，合并同时间同内容的多人直播
- 使用 `Pillow` 本地绘制直播卡片图片
- 支持在卡片右侧展示当前场次对应成员头像
- 提供 `/bot帮助` 文本帮助

## 指令

### 直播查询

发送以下任一消息：

- `今日直播`
- `明日直播`

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
asoul推送请使用【今日直播】或【明日直播】
```

## 依赖

本插件当前以本地图片渲染为主，运行环境需要：

- Python 可用
- `Pillow` 已安装

如果 `Pillow` 不可用，直播图片无法正常生成。

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

- `font.otf`

## 数据来源

直播数据来源：

- `https://asoul.love/calendar.ics`

插件会在内存中做短时间缓存，避免每次请求都重新拉取日历。

## 说明

- 插件按 `Asia/Shanghai` 时区处理今天 / 明天的直播
- 会过滤取消事件和全天事件
- 团播会按识别到的成员名称合并展示
- 如果本地图片绘制失败，会退回纯文本结果

## 开发

仓库地址：

- [astrbot_plugin_asoul](https://github.com/LEN5010/astrbot_plugin_asoul)

AstrBot 相关文档：

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)

鸣谢:

- [枝江站](https://asoul.love/)
