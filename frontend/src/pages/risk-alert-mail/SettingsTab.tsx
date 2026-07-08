/**
 * Tab 3 — global settings (deliberately light for v2). The frozen contract
 * exposes no SMTP-status endpoint, so this renders a read-only explanation
 * card plus the live source registry from GET /sources. SMTP connectivity is
 * verified through Tab-1's test-send (same SMTP path as real alerts).
 * v2 scope: no global rate-limit fuse / recipient groups (tracked follow-up).
 */
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

import type { MailSource } from "./types";

interface SettingsTabProps {
  sources: MailSource[];
}

export function SettingsTab({ sources }: SettingsTabProps) {
  return (
    <Card className="gap-3">
      <CardHeader>
        <CardTitle className="text-base">全局设置</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6 text-sm">
        <div className="space-y-1.5">
          <h3 className="font-semibold">SMTP 发信通道</h3>
          <p className="text-muted-foreground">
            发件账号 / SMTP 服务器由后端环境变量统一管理，页面不可修改。要验证连通性，
            在「订阅规则」里对任一订阅点「试发」——它走的就是真实告警的同一条 SMTP
            链路，成功即代表通道正常；失败会在弹窗提示具体 SMTP 错误。
          </p>
        </div>

        <div className="space-y-1.5">
          <h3 className="font-semibold">口径铁律</h3>
          <p className="text-muted-foreground">
            检测规则决定「页面上有什么」，订阅条件决定「邮箱里有什么」——后者永远是前者的子集。
            给新检测模块加邮件能力 = 在后端源注册表登记一条，检测代码零改动。
          </p>
        </div>

        <div className="space-y-2">
          <h3 className="font-semibold">已注册告警源（源注册表）</h3>
          {sources.length === 0 ? (
            <p className="text-muted-foreground">加载中或暂无注册源。</p>
          ) : (
            <ul className="space-y-2">
              {sources.map((s) => (
                <li
                  key={s.module}
                  className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2"
                >
                  <span className="font-medium">{s.label}</span>
                  <Badge variant="outline" className="font-mono text-xs">
                    {s.module}
                  </Badge>
                  <span className="text-xs text-muted-foreground">
                    rule_id {s.rule_id_range[0]}–{s.rule_id_range[1]} ·{" "}
                    {s.rules.length} 条检测规则 · {s.filterable_fields.length} 个可过滤字段
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <p className="text-xs text-muted-foreground">
          v2 暂不提供全局限流保险丝与收件人组（已记 follow-up）。发送失败自动重试与
          Webhook 渠道（Slack/Telegram）同样在后续版本规划中。
        </p>
      </CardContent>
    </Card>
  );
}
