# travel_agent

旅行素材的采集与整理。每个站点一个 skill，放在 `skills/<站点>/`。

站点 skill 不实现浏览器能力：它假定 `chrome-agent` 命令可用（CLI + daemon + 已加载的扩展），
只通过它的 `--json` 输出驱动浏览器。浏览器这一侧出问题，去 [chrome-agent](https://github.com/xunushan/chrome-agent)
那个仓库查。

## skills/xhs-downloader

小红书笔记的搜索与下载工具，两个入口：`discover.py` 搜一个关键词、施加筛选、输出候选清单
（**不打开任何笔记**）；`download.py` 按清单把指定笔记的正文/封面/图片/视频/评论下到本地。

工具本身不做判断——搜什么、留哪几篇、什么时候停都是调用方的事，清单就是 `discover.py` 的输出。
下载状态就维护在笔记目录的文件里（`note.json` / `cover.*` / `images/` / `videos/` /
`comments.json` / `downloads.json`），**盘上已有的不再下，变了就整篇刷新**，所以原样重跑一遍
不会重复下载；sqlite 只存一份索引（`noteId` + 链接 + 更新时间 + 内容指纹），不存正文，
也没有任何"已下载"标志位。

入口是 [skills/xhs-downloader/SKILL.md](skills/xhs-downloader/SKILL.md)，重内容按需加载在它旁边的
`references/` 下。

```bash
python -m pytest -q                       # 规则与产出合同的回归
pip install -e ".[dev]"                   # pytest + jsonschema
```

采集产出与数据库默认落在 `~/Documents/travel_agent`（`--data-root` 可指定），本仓库不管产出。
