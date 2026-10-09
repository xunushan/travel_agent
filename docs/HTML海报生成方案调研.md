# HTML 海报生成方案调研

> 调研目的：为自驾游智能体的海报/图文卡片产出（阶段 0 游览总览、阶段 7 路书等）选型「大模型基于代码生成海报」的技术方案。要求：灵活、质量高、不依赖额外生图模型。

## 结论（TL;DR）

**主路线：LLM 生成单文件 HTML（内联 CSS + 图片本地化/base64）→ Playwright 无头浏览器高清截图导出 PNG。**

- 这是当前 Agent 生态里事实上的标准做法，灵活度最高（完整 CSS/字体/布局能力），质量上限就是网页设计的上限；
- HTML 同时是网页产物和图片母版，一份代码两种消费；
- 已有多个开源 Agent Skill 直接可抄（见第 4 节），其中 **guizang-social-card-skill** 的版式系统与旅行内容最匹配。

不推荐：html2canvas/dom-to-image（浏览器端，样式兼容性差）、Pillow 纯代码绘制（美观度低、调版成本高）、satori+resvg（仅适合固定模板小卡片，不支持 grid 等复杂布局）。

## 一、技术路线对比

| 路线 | 原理 | 质量 | 灵活度 | 适用 |
|------|------|------|--------|------|
| **Playwright/Puppeteer 截图** | 无头 Chrome 真实渲染页面后截图 | ★★★★★ 完全还原 | ★★★★★ 全部 CSS/JS/字体 | 海报、长图、卡片，**首选** |
| satori + resvg（@vercel/og） | JSX → SVG → PNG | ★★★★ 像素级精确 | ★★ 仅 flexbox 子集，不支持 grid | 固定模板的 OG 小卡片；速度极快（70-160ms/张），适合批量 |
| html2canvas / dom-to-image | 浏览器内 DOM → Canvas | ★★ 样式兼容问题多 | ★★★ 无需服务端 | 纯前端轻量场景，不适合质量要求高的海报 |
| Pillow/Canvas 代码绘制 | 逐元素画图 | ★★ 取决于投入 | ★★ 改版成本高 | 模板极固定、批量大的场景 |
| 在线设计器二开（迅排设计等） | 人工拖拽 + 服务端出图 | ★★★★ | ★★ 需人工介入 | 不适合 Agent 自动化，可作人工兜底工具 |

## 二、大模型生成海报的工作流模式（业界实践）

1. **模板生成模式**：给 LLM 参考（设计稿/对标截图/风格描述）→ 生成 HTML → 渲染 → 人看后反馈 → LLM 迭代修改 HTML。淘宝技术的实践表明，初版即可较好复刻参考的配色与版式，关键在"渲染-反馈-迭代"闭环。
2. **模板填充模式**：预置若干 HTML 模板（固定版式），LLM 只做内容解析与坑位填充（标题/正文/图片）。稳定性最高，适合批量、固定栏目化产出（如每日推送卡）。
3. **混合模式（建议）**：先按模板填充保证下限，允许 LLM 在模板基础上微调样式拉高上限。

实践要点：提示词中显式指定版式约束（尺寸、字号层级、配色、分页规则）；竖版海报常用 750px 宽（手机端）或 1242×1656（小红书 3:4）。

## 三、Playwright 高清截图实施要点

```javascript
const { chromium } = require('playwright');
const browser = await chromium.launch();
const page = await browser.newPage({ deviceScaleFactor: 2 }); // 2x~3x 高清
await page.setViewportSize({ width: 750, height: 1334 });     // 宽度即海报宽度
await page.goto('file:///path/to/poster.html');
await page.waitForTimeout(1000);                              // 等字体/图片渲染
// 长图：先量出内容实际高度再截全页
const h = await page.evaluate(() => document.body.scrollHeight);
await page.setViewportSize({ width: 750, height: h + 100 });
await page.screenshot({ path: 'poster.png', fullPage: true, type: 'png' });
await browser.close();
```

- **deviceScaleFactor: 2~3** 是清晰度关键（2x 即 Retina 级，文件约 2-3MB）；
- **图片必须本地化**：小红书等平台图片有防盗链，需先下载到本地再用相对路径或 base64 引用；
- **字体**：中文海报需确保环境有中文字体（或内嵌 webfont），否则回退成默认字体很丑；
- **分页 vs 长图**：多图组图场景（如小红书图文）按卡片逐个元素截图导出多张，比截一张超长图更实用；
- **降级链**：Playwright 不可用时依次退到 puppeteer → wkhtmltoimage → 直接给用户 HTML 文件。

## 四、可直接复用的开源 Skill / 项目

| 项目 | 说明 | 复用价值 | 地址 |
|------|------|----------|------|
| **guizang-social-card-skill** | Claude Code/Codex 图文卡片 Skill：小红书组图+公众号封面；28 种布局、10 种主题，杂志风/瑞士风两套设计系统，单文件 HTML 导出 PNG（5.6k+ star，AGPL-3.0） | ★ 最推荐：杂志风适合旅行内容；版式库（assets/references）可直接当模板库；`npx skills add op7418/guizang-social-card-skill` | https://github.com/op7418/guizang-social-card-skill |
| **industry-chain-chart** | SKILL.md 内含完整的"HTML → Playwright 3x 高清截图"脚本与降级链 | ★ 截图环节可直接抄 | https://github.com/LeoBoy1st/industry-chain-chart |
| **link-poster** | Claude Code Skill：任意链接 → 同风格海报（HTML + html2canvas 一键下载 2x PNG） | 风格提取思路可参考（抓配色/字体/氛围） | https://github.com/OrangeViolin/link-poster |
| **canvas-design** | Anthropic 官方 example-skills 之一，海报/设计类，Python 代码出图 | 官方 Skill 写法参考 | https://github.com/anthropics/skills |
| **XHS-TextCard** | 纯前端 Markdown → 小红书卡片，本地 Canvas 渲染 1242×1656，12 款模板（MIT） | 无需 Agent 时的轻量备选；模板样式可参考 | https://github.com/geekfoxcharlie/XHS-TextCard |
| **baoyu-skills** | 宝玉（JimLiu）的视觉工具箱（16k+ star）：xhs-images 小红书卡片（11 风格 × 8 排版）、infographic（21 布局）、cover-image、markdown-to-html 等 | 阶段 6/7 的备选全家桶 | https://github.com/JimLiu/baoyu-skills |
| **mp-cover-generator** | 公众号封面 Skill：生图底图 + HTML 文字层 + Playwright 转 PNG | "底图+HTML 叠加层"的合成思路 | 火山引擎开发者社区有教程 |
| **poster-design（迅排设计）** | 开源在线海报设计器（Vue3 + Puppeteer 服务端出图，MIT，4.7k star） | 人工兜底编辑工具；服务端出图架构可参考 | https://github.com/palxiao/poster-design |
| **FastPoster** | 上传背景图+摆组件生成海报，多语言 SDK，docker 部署（MIT） | 模板固定、批量调用场景 | https://github.com/psoho/fast-poster |

## 五、落地建议（对应自驾游助手）

- **阶段 0（游览总览）**：LLM 生成单文件 HTML（结论→路线图→景点卡片→来源），先以 HTML 交付；需海报时用 Playwright 截图导出，脚本照抄 industry-chain-chart 的写法。
- **阶段 6/7（笔记/路书）**：图文组图可复用 guizang-social-card-skill 的版式库（杂志风契合旅行叙事）；动态路书视频用 HyperFrames（HeyGen 开源，HTML→MP4，专为 Agent 设计，Apache 2.0）。
- **演进路径**：先"模板填充"保证稳定出图 → 积累几套自有模板后 → 开放"模板生成"让 LLM 自由发挥 + 人工反馈迭代。

## 六、质量自检清单

- [ ] deviceScaleFactor ≥ 2
- [ ] 图片全部本地化/base64（无外链防盗链问题）
- [ ] 中文字体可用且已显式指定
- [ ] 截图前等待渲染完成（waitForTimeout / networkidle）
- [ ] 输出尺寸符合目标平台（小红书 3:4=1242×1656，手机竖版海报宽 750）
- [ ] 生成后抽查：无文字溢出、无图片占位框、无字体回退

---

## 七、guizang-social-card-skill 深入调研与借鉴方案

> 仓库：[github.com/op7418/guizang-social-card-skill](https://github.com/op7418/guizang-social-card-skill)（作者郭浩 @op7418，5.6k+ star，**AGPL-3.0** 协议）

### 7.1 它到底是什么

不是 AI 绘图提示词，而是**一套本地渲染流水线**：版式骨架和主题预设全部固定，Agent 只负责填内容和调参数。输出单文件 HTML → `node render.mjs`（Playwright）渲染成 PNG，附带 `validate-social-deck.mjs` 校验器（9 条规则，用 Playwright 跑真实 DOM 测量，不是凭感觉猜）。

- **两套视觉系统**：Editorial（电子杂志风，Monocle/Kinfolk 式克制版面，**适合旅行/叙事**）、Swiss（瑞士国际主义网格，适合数据/教程）；
- **28 个版式骨架**（Editorial 16 + Swiss 12）+ **10 套主题预设**（不允许自定义 hex——约束即质量保障）；
- **画板**：小红书 3:4（1080×1440）、公众号 21:9 + 1:1 封面对；
- **7 步工作流**：Intake（抓平台/风格/素材/用户图，无图时一次性给 ABC 三选不二次劝导）→ 选风格主题 → 选版式 → 取图（Unsplash/Pexels 等，落本地 + 写 SOURCES.md）→ 渲染 → 交付 review → 迭代；
- **11 个小红书品类适配**，其中**旅行是"端到端强势"品类**（文/结构/图都在能力圈内）。

### 7.2 能不能直接拿来用

| 场景 | 结论 |
|---|---|
| **阶段 6/7：小红书图文笔记、路书卡片** | ✅ **可以直接用**——旅行是其端到端强势品类，`npx skills add op7418/guizang-social-card-skill` 安装即可 |
| **阶段 0：游览总览（长页面）** | ⚠️ **不能整套直接套**——它的模板是卡片尺寸（3:4/21:9），不是长页面；但设计语言和渲染管线可完整借鉴 |
| 商用/闭源分发 | ⚠️ AGPL-3.0，自用没问题；产品化分发需评估开源义务（衍生作品需开源） |

### 7.3 借鉴清单（阶段 0 总览模板自建时）

| 借鉴什么 | 怎么用 |
|---|---|
| **"版式骨架 + 主题预设"机制** | 我们的"游览总览"也做预置模板：固定骨架（结论区/当季情况区/路线区/景点卡片区/来源区）+ 少量主题色预设，LLM 只填坑位——这就是 §五说的"模板填充模式"的现成范本 |
| **`render.mjs` + validator 管线** | 渲染脚本和"真实 DOM 测量校验"的思路直接抄：生成后用 Playwright 量文字溢出、图片缺失、字号对比 |
| **Editorial 视觉语言** | 克制色板、字号+字体对比撑层级、网格留白——契合旅行内容；主题预设可直接挑一套（如大地色系） |
| **Intake 交互设计** | "无图时一次性给 A/B/C 三选，不二次劝导"这类交互细节，可参考进我们自己的 skill 设计 |
| **quiet zone 避让原则** | 满铺图上压文字要避开主体——景点美照配文字时用得上 |

### 7.4 需要改造的点

1. **取图源**：它默认从 Unsplash/Pexels/Wallhaven 取图，我们要改为从 `notes/` 素材库取图（防盗链问题也不存在）；
2. **画板尺寸**：阶段 0 总览是长页面/长图，需自建模板骨架，不套用 3:4 卡片；
3. **SOURCES.md 机制保留**：它给每张图记录来源——和我们"注明 noteId 来源"的需求一致，直接沿用。

### 7.5 顺带的发现（阶段 6/7 备选）

- **guizang-ppt-skill**（同作者姊妹项目）：文章 → PPT/演讲图/封面，阶段 7 路书可参考 —— https://github.com/op7418/guizang-ppt-skill
- **guizang-material-illustration**：生成带中文标签的中心解释图（依赖生图模型，本项目的"路线图自制"场景如开放生图可用）—— https://github.com/op7418/guizang-material-illustration
- **baoyu-skills**（@JimLiu，16k+ star）：封面/信息图/结构图工具箱，12 种视觉风格 —— https://github.com/JimLiu/baoyu-skills
- **html-anything**：Markdown → HTML 页面/海报/卡片 PNG —— https://github.com/clockless-org/html-anything

---

## 八、参考链接汇总

- Playwright：https://playwright.dev ｜ Puppeteer：https://github.com/puppeteer/puppeteer
- satori：https://github.com/vercel/satori ｜ @vercel/og：https://github.com/vercel/og ｜ resvg-js：https://github.com/yisibl/resvg-js
- html2canvas：https://github.com/niklasvh/html2canvas
- 其余项目链接见 §四、§七 表格内链
