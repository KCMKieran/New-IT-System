/**
 * 风控告警邮件中心 (OPT-0043) — /risk-alert-mail
 *
 * Risk team self-serve mail subscriptions on top of the OPT-0042 dispatcher:
 *   Tab 1 订阅规则 — subscription CRUD (shadcn Table + Sheet drawer)
 *   Tab 2 发送记录 — mail_outbox ledger (AG-Grid, persisted columns/filters)
 *   Tab 3 全局设置 — read-only SMTP / registry explanation card
 *
 * The source registry (GET /alert-mail/sources) is fetched once here and
 * shared by all three tabs (module labels, rules, filterable fields).
 */
import { useEffect, useState } from "react";
import { IconMail, IconSend, IconSettings } from "@tabler/icons-react";
import { toast } from "sonner";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { apiFetch } from "@/lib/fetch";

import { OutboxTab } from "./risk-alert-mail/OutboxTab";
import { SettingsTab } from "./risk-alert-mail/SettingsTab";
import { SubscriptionsTab } from "./risk-alert-mail/SubscriptionsTab";
import { readErrorDetail } from "./risk-alert-mail/helpers";
import type { ListEnvelope, MailSource } from "./risk-alert-mail/types";

export default function RiskAlertMailCenter() {
  const [sources, setSources] = useState<MailSource[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await apiFetch("/api/v1/alert-mail/sources", {
          signal: controller.signal,
        });
        if (!res.ok) {
          toast.error(`加载告警源注册表失败: ${await readErrorDetail(res)}`);
          return;
        }
        const body = (await res.json()) as ListEnvelope<MailSource>;
        setSources(body.data);
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        toast.error(
          `加载告警源注册表失败: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    })();
    return () => controller.abort();
  }, []);

  return (
    <div className="flex-1 space-y-4 p-4 md:p-6">
      <p className="text-sm text-muted-foreground">
        风控告警邮件中心——哪个检测模块、什么条件、发给谁、实时还是每日汇总，风控自助配置，
        无需 IT 参与。
      </p>

      <Tabs defaultValue="subscriptions" className="w-full">
        <TabsList className="grid w-full max-w-2xl grid-cols-3">
          <TabsTrigger value="subscriptions" className="gap-1.5">
            <IconMail className="h-4 w-4" />
            订阅规则
          </TabsTrigger>
          <TabsTrigger value="outbox" className="gap-1.5">
            <IconSend className="h-4 w-4" />
            发送记录
          </TabsTrigger>
          <TabsTrigger value="settings" className="gap-1.5">
            <IconSettings className="h-4 w-4" />
            全局设置
          </TabsTrigger>
        </TabsList>

        <TabsContent value="subscriptions" className="mt-4">
          <SubscriptionsTab sources={sources} />
        </TabsContent>
        <TabsContent value="outbox" className="mt-4">
          <OutboxTab sources={sources} />
        </TabsContent>
        <TabsContent value="settings" className="mt-4">
          <SettingsTab sources={sources} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
