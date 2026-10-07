# travel_agent

旅行素材的采集与整理。每个站点一个 skill，放在 `skills/<站点>/`。

站点 skill 不实现浏览器能力：它假定 `chrome-agent` 命令可用（CLI + daemon + 已加载的扩展），
只通过它的 `--json` 输出驱动浏览器。浏览器这一侧出问题，去 [chrome-agent](https://github.com/xunushan/chrome-agent)
那个仓库查。

站点 skill 由哪些文件组成、每个文件里必须有什么，见 [docs/site-skill-spec.md](docs/site-skill-spec.md)。

## skills/xhs-downloader

小红书笔记采集：**按意图搜索筛选** → 打开核验 → 正文/作者/互动数 → 图片与视频下载 → 评论树。
分两阶段：阶段一定关键词与筛选、批量抓筛选所需内容、选出一份清单；阶段二下载清单里笔记的正文/媒体/评论。
已采集的内容记在 sqlite 里（`noteId` + 更新时间 + 内容指纹判重），不重复下载。

入口是 [skills/xhs-downloader/SKILL.md](skills/xhs-downloader/SKILL.md)，重内容按需加载在它旁边的
`references/` 下。

```bash
python -m pytest -q                       # 规则与产出合同的回归
pip install -e ".[dev]"                   # pytest + jsonschema
```

采集产出与数据库默认落在 `~/Documents/travel_agent`（`--data-root` 可指定），本仓库不管产出。
