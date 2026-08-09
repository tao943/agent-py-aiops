## 1. 规格和入口配置

- [x] 1.1 定义 Nginx 网关、分级限流和宿主机应用边界
- [ ] 1.2 增加官方 Nginx Compose 服务和只读配置
- [ ] 1.3 使用 Compose config 与 `nginx -t` 验证配置

## 2. 本地运行入口

- [ ] 2.1 将前端与演示脚本默认 API 地址切换到 8080
- [ ] 2.2 让 macOS/Linux 和 Windows 启动器启动并报告 Nginx
- [ ] 2.3 保持 FastAPI 8000 仅作为本机调试直连入口

## 3. CI 与文档

- [ ] 3.1 使用 TDD 扩展路径检测与 gateway CI job
- [ ] 3.2 更新根 README 和基础设施运行指南
- [ ] 3.3 同步 OpenSpec WIKI 并验证 VitePress 构建

## 4. 运行验收

- [ ] 4.1 验证网关健康和 FastAPI 健康代理
- [ ] 4.2 验证普通、认证和 SSE 建连限流返回 429
- [ ] 4.3 验证 SSE 无缓冲、12m 网关请求体与后端限流回归
- [ ] 4.4 运行全量确定性检查并归档变更
