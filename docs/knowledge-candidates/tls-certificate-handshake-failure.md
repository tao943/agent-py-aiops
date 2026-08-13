# TLS 证书与握手失败差分排查

## 适用现象
HTTPS 或数据库 TLS 连接在握手阶段失败，表现为证书过期、unknown authority、hostname mismatch、protocol alert 或间歇性失败。

## 候选原因
- 叶证书过期、尚未生效或系统时钟错误。
- 中间证书链缺失，客户端信任库不包含颁发者。
- SNI/主机名与证书 SAN 或虚拟主机不匹配。
- 客户端与服务端协议、cipher 或 mTLS 要求不兼容。

## 建议证据
从真实客户端路径保存不含私钥的证书链、有效期、SAN、issuer、SNI、协议/cipher、验证错误和两端时间；核对负载均衡各实例证书指纹与最近轮换。

## 如何区分
当前时间超出有效期偏向过期或时钟；浏览器部分成功而精简客户端失败常见于链缺失；指定正确 SNI 后成功偏向虚拟主机；无共同协议/cipher 或缺客户端证书属于握手能力。TCP 不通不是 TLS 根因。

## 安全恢复边界
证书公有信息和握手探测可自动执行。部署证书、改协议、信任库或时钟需审批；禁止输出私钥、关闭证书校验或长期启用弱协议。

## 恢复后验证
从不同客户端和入口验证完整链、主机名、有效期、SNI 与协议，确认所有实例指纹一致、错误率归零并验证下一次轮换告警。

## 来源
- RFC 8446 TLS 1.3：https://www.rfc-editor.org/rfc/rfc8446
- OpenSSL Verification Options：https://docs.openssl.org/3.0/man1/openssl-verification-options/
RFC 可公开引用，OpenSSL 文档按 Apache-2.0 项目许可；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
