# 运行日志模板

`question_id` 为问题去空白、标点并统一大小写后，SHA-1 的前 8 位：

```bash
printf '%s' "$Q" | python3 -c 'import sys,hashlib,unicodedata; q="".join(c for c in sys.stdin.read().lower() if not c.isspace() and not unicodedata.category(c).startswith("P")); print(hashlib.sha1(q.encode()).hexdigest()[:8])'
```

## 运行日志：runs/<question_id>.json

以下为字段模板。开始时创建，搜索、粗筛及每篇理解后更新。

```json
{
  "question": "问题",
  "requirements": "回答要求",
  "status": "running",
  "rounds": [
    {
      "keyword": "搜索词",
      "filters": {"排序依据": "最多收藏"},
      "top_k": 6,
      "purpose": "本轮需要回答的内容",
      "candidates": ["noteId"],
      "light_reads": [
        {"id": "noteId", "note": "downloaded", "cover": "reused"}
      ],
      "screen": [
        {"id": "noteId", "keep": 1, "pri": 1, "why": "提供节点顺序"},
        {"id": "noteId", "keep": 0, "why": "无所需信息"},
        {"id": "noteId", "keep": null, "why": "正文读取失败"}
      ]
    }
  ],
  "reads": [
    {
      "id": "noteId",
      "round": 1,
      "digest": "reused",
      "verdict": "used",
      "increment": ["本篇新增的所需信息"],
      "coverage": [
        {"item": "要求中的一项", "status": "covered", "evidence": "结论及来源"}
      ],
      "remaining": ["仍未回答的内容"]
    }
  ],
  "failures": [
    {"id": "noteId", "stage": "download", "why": "失败原因"}
  ],
  "unread": [
    {"id": "noteId", "why": "要求已满足，未继续阅读"}
  ],
  "counts": {"light_downloaded": 0, "light_reused": 0, "reads": 0, "new_digests": 0},
  "stop": {"reason": null, "rounds": 0, "reads": 0},
  "sources": ["实际引用的 noteId"],
  "gaps": [
    {"item": "未回答项", "tried": ["搜索词"], "why": "未解决原因", "evidence": []}
  ]
}
```

### 填写说明

- **初始记录**：状态为 `running`，列表为空、计数为 0、`stop.reason` 为 null。每次运行单独维护当前记录。
- **rounds**：每轮一项，派搜索任务前记录搜索配置；结果返回后补候选、轻量下载与粗筛结果。`light_reads` 的 note、cover 使用 `downloaded`、`reused` 或 `failed`。
- **reads：本次已读清单**：每篇报告返回后立即追加；主 agent 用 `reads[].id` 排除重复阅读并随每轮粗筛传入。没有完成报告的笔记不写入，已有 digest 也不自动算本次已读。
- **理解结果**：`digest` 为 `new` 或 `reused`；`verdict` 为 `used`、`duplicate` 或 `no-increment`，分别表示提供所需信息、内容重复或无新增信息。
- **覆盖记录**：coverage 按要求列项，状态为 `covered`、`partial` 或 `missing`；有结论时标依据，每篇保留当时的覆盖情况与 remaining。
- **失败与未读**：failures 记录运行故障；unread 在停止时列出仍未读取的保留候选及原因，不与粗筛排除项混淆。
- **计数**：light_downloaded、light_reused 分别统计发生轻量下载、复用的笔记次数，同篇部分下载部分复用可计入两项；reads 为完成报告的篇数，new_digests 为本次新解析篇数。
- **停止记录**：reason 使用 `closed`、`rounds-cap`、`reads-cap` 或 `interrupted`；停止时更新 rounds、reads，status 改为 `completed` 或 `interrupted`。
- **sources 与 gaps**：sources 仅列实际引用来源；gaps 保存未解决项。待确认的名称关系的 tried 可为空，evidence 须写判断依据与引用路径。
