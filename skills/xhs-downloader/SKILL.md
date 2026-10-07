---
name: xiaohongshu
description: 采集小红书（xiaohongshu.com）笔记：图文或视频的正文、作者与互动数据、图片与视频下载、完整评论树（含多级回复）。用在本站点的搜索发现、笔记详情、评论抓取与批量采集任务上。
---

# 小红书采集

本站点专属的一切都在这里：容器结构、参数、踩的坑、产出结构。驱动浏览器的通用
能力来自 `chrome-agent` skill，那边不含本站内容。

## 依赖

- `chrome-agent` 命令必须可用（CLI + daemon + 已加载的扩展）。没有就先按 chrome-agent
  skill 把它装好——本站不 import 它的代码，只调它的 CLI，也只依赖它的 `--json` 输出。
- 采集走用户已登录的 Chrome，不碰登录态、验证码与风控。

## 怎么干

**先完整读 [PLAYBOOK.md](PLAYBOOK.md)，再动手。** 第 1 节照顺序跑完即一次采集，第 2 节是
每条规则的证据（出问题回来查），第 3 节说明各文件职责与改动规矩。

```text
PLAYBOOK.md    流程 / 踩坑与证据 / 目录职责
locators.yaml  站点选择器、作用域、滚动与阈值——改选择器只改这里
scripts/       发现 → 采集 → 批量；改产出结构要连 schemas/ 一起改
schemas/       note / comments / downloads 三份产出合同
tests/         test_playbook.py（规则单测）、test_schemas.py（产出与合同一致）、e2e_search.py（手动冒烟）
```
