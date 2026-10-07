# travel_agent

旅行素材的采集与整理。每个站点一个 skill，放在 `skills/<站点>/`。

站点 skill 不实现浏览器能力：它假定 `chrome-agent` 命令可用（CLI + daemon + 已加载的扩展），
只通过它的 `--json` 输出驱动浏览器。浏览器这一侧出问题，去 [browser_use](https://github.com/xunushan/browser_use)
那个仓库（chrome-agent skill 本身）查。

## skills/xiaohongshu

小红书笔记采集：搜索发现 → 打开核验 → 正文/作者/互动数 → 图片与视频下载 → 评论树。
入口是 [skills/xiaohongshu/SKILL.md](skills/xiaohongshu/SKILL.md)，操作细节在它旁边的 PLAYBOOK.md。

```bash
python -m pytest -q                       # 规则与产出合同的回归
pip install -e ".[dev]"                   # pytest + jsonschema
```

采集产出默认落在调用方指定的目录，本仓库不管产出。
