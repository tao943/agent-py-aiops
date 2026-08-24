import { defineConfig } from "vitepress";

export default defineConfig({
  lang: "zh-CN",
  title: "Agent Py AIOps",
  description: "可审计、可评测、恢复受治理的 AIOps Agent 项目文档",
  cleanUrls: true,
  themeConfig: {
    nav: [
      { text: "首页", link: "/" },
      { text: "架构", link: "/architecture" },
      { text: "评测", link: "/aiops/agentpy-domainbench" },
      { text: "运行手册", link: "/operations-and-monitoring" }
    ],
    sidebar: [
      {
        text: "项目",
        items: [
          { text: "系统架构", link: "/architecture" },
          { text: "评测体系", link: "/aiops/agentpy-domainbench" },
          { text: "RAG 知识卡", link: "/knowledge-catalog" }
        ]
      },
      {
        text: "安装与运行",
        items: [
          { text: "Windows", link: "/setup/windows" },
          { text: "Linux", link: "/setup/linux" },
          { text: "macOS", link: "/setup/macos" },
          { text: "配置与监控", link: "/operations-and-monitoring" },
          { text: "Live Eval", link: "/runbooks/live-eval" },
          { text: "真实日志与告警", link: "/tutorials/real-log-and-alert" }
        ]
      },
      {
        text: "示例",
        items: [{ text: "多步骤 Skills", link: "/examples/skills/" }]
      }
    ],
    search: { provider: "local" },
    outline: { level: [2, 3], label: "本页目录" },
    docFooter: { prev: "上一页", next: "下一页" },
    sidebarMenuLabel: "目录",
    returnToTopLabel: "返回顶部"
  }
});
