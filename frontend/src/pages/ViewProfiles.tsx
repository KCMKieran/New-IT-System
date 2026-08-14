/**
 * Configuration → View Profiles (OPT-0035).
 *
 * Lives at /cfg/view-profiles. It used to be the /settings page; /settings is
 * now a placeholder while a real settings page is built.
 *
 * Two zones:
 *  1. 我的身份 — claim a named profile exclusively to this device (auto-save then
 *     pushes your view there); release it; create a new profile.
 *  2. 观摩他人 — read-only preview of someone else's view (exit restores yours).
 *
 * Plus an admin force-release escape hatch for a lock stuck on a lost device-id.
 */
import { useCallback, useEffect, useState } from "react";
import { Copy, Eye, LockKeyhole, Plus, ShieldAlert, UserCheck } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  ProfileConflictError,
  ProfileForbiddenError,
  claimProfile,
  createProfile,
  forceReleaseProfile,
  getProfile,
  listProfiles,
  releaseProfile,
  saveProfileState,
  type ProfileDTO,
} from "@/lib/view-profiles/api";
import { getDeviceId } from "@/lib/view-profiles/device-id";
import { clearClaimedName, getClaimedName, setClaimedName } from "@/lib/view-profiles/identity";
import { enterObserve, getObservingName } from "@/lib/view-profiles/observe";
import { captureSnapshot } from "@/lib/view-profiles/snapshot";

export default function ViewProfilesPage() {
  const [profiles, setProfiles] = useState<ProfileDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const deviceId = getDeviceId();
  const claimedName = getClaimedName();
  const observingName = getObservingName();

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setProfiles(await listProfiles(signal));
    } catch (e) {
      if ((e as Error)?.name !== "AbortError") toast.error("加载档案失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const handleClaim = async (name: string) => {
    setBusy(name);
    try {
      await claimProfile(name, deviceId ? `device ${deviceId.slice(0, 8)}` : undefined);
      setClaimedName(name);
      // Seed the profile with the view I'm currently using.
      await saveProfileState(name, captureSnapshot());
      toast.success(`已认领 ${name}，之后的视图变更会自动保存到这里`);
      await load();
    } catch (e) {
      if (e instanceof ProfileConflictError) toast.error(`${name} 已被另一台设备认领`);
      else toast.error(`认领 ${name} 失败`);
    } finally {
      setBusy(null);
    }
  };

  const handleRelease = async (name: string) => {
    setBusy(name);
    try {
      await releaseProfile(name);
      clearClaimedName();
      toast.success(`已解绑 ${name}`);
      await load();
    } catch {
      toast.error(`解绑 ${name} 失败`);
    } finally {
      setBusy(null);
    }
  };

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) return;
    setBusy("__create__");
    try {
      await createProfile(name);
      setNewName("");
      toast.success(`已创建档案 ${name}`);
      await load();
    } catch {
      toast.error(`创建失败（名称可能已存在）`);
    } finally {
      setBusy(null);
    }
  };

  const handleObserve = async (name: string) => {
    setBusy(name);
    try {
      const profile = await getProfile(name);
      if (!profile) {
        toast.error(`${name} 不存在`);
        return;
      }
      enterObserve(name, profile.state);
      window.location.reload(); // remount so AG-Grid re-hydrates from the observed view
    } catch {
      toast.error(`观摩 ${name} 失败`);
      setBusy(null);
    }
  };

  const handleForceRelease = async (name: string) => {
    if (!window.confirm(`强制解绑 ${name}？这会清除其当前设备绑定，使其可被重新认领。`)) return;
    setBusy(name);
    try {
      await forceReleaseProfile(name);
      toast.success(`已强制解绑 ${name}`);
      await load();
    } catch (e) {
      if (e instanceof ProfileForbiddenError) toast.error("本设备没有强制解绑权限");
      else toast.error(`强制解绑 ${name} 失败`);
    } finally {
      setBusy(null);
    }
  };

  const mine = (p: ProfileDTO) => claimedName === p.name && p.owner_device === deviceId;

  return (
    <div className="mx-auto max-w-3xl space-y-6 pb-10">
      <div>
        <h1 className="text-2xl font-semibold">视图档案 / View Profiles</h1>
        <p className="text-sm text-muted-foreground">
          把「怎么看数据」（列、过滤器、视图模式）绑定到一个命名档案，跨机保存、可供他人观摩。
        </p>
        {deviceId && (
          <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
            <span>本机 device-id：</span>
            <code className="rounded bg-muted px-1.5 py-0.5">{deviceId}</code>
            <Button
              size="icon"
              variant="ghost"
              className="size-6"
              title="复制完整 device-id（管理员把它加进 VIEW_PROFILES_ADMIN_DEVICES 即可获得强制解绑权限）"
              onClick={() => {
                void navigator.clipboard?.writeText(deviceId);
                toast.success("已复制 device-id");
              }}
            >
              <Copy className="size-3.5" />
            </Button>
          </div>
        )}
      </div>

      {/* Zone 1 — identity / claim */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <UserCheck className="size-5" /> 我的身份
          </CardTitle>
          <CardDescription>
            {claimedName ? (
              <>当前认领：<strong>{claimedName}</strong> —— 视图变更会自动保存到这里。</>
            ) : (
              <>尚未认领。选一个档案认领后，你的视图变更会默认静默保存到它名下（排他：别人认不了同一个）。</>
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {loading ? (
            <p className="text-sm text-muted-foreground">加载中…</p>
          ) : (
            <ul className="divide-y rounded-md border">
              {profiles.map((p) => {
                const claimedBySomeone = p.owner_device !== null;
                const claimedByMe = mine(p);
                return (
                  <li key={p.name} className="flex items-center justify-between gap-3 px-3 py-2">
                    <span className="flex items-center gap-2">
                      <span className="font-medium">{p.name}</span>
                      {claimedByMe ? (
                        <Badge variant="default" className="gap-1">
                          <UserCheck className="size-3" /> 我（本机）
                        </Badge>
                      ) : claimedBySomeone ? (
                        <Badge variant="secondary" className="gap-1">
                          <LockKeyhole className="size-3" /> 已被其他设备认领
                          {p.owner_label ? ` · ${p.owner_label}` : ""}
                        </Badge>
                      ) : (
                        <Badge variant="outline">空闲</Badge>
                      )}
                    </span>
                    <span className="flex items-center gap-2">
                      {claimedByMe ? (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy === p.name}
                          onClick={() => handleRelease(p.name)}
                        >
                          解绑
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          disabled={busy === p.name || claimedBySomeone}
                          onClick={() => handleClaim(p.name)}
                        >
                          认领
                        </Button>
                      )}
                      {claimedBySomeone && !claimedByMe && (
                        <Button
                          size="sm"
                          variant="ghost"
                          className="text-destructive hover:text-destructive"
                          disabled={busy === p.name}
                          title="管理员强制解绑（用于丢失 device-id 的死锁）"
                          onClick={() => handleForceRelease(p.name)}
                        >
                          <ShieldAlert className="size-4" />
                        </Button>
                      )}
                    </span>
                  </li>
                );
              })}
              {profiles.length === 0 && (
                <li className="px-3 py-4 text-sm text-muted-foreground">还没有任何档案，先在下面创建一个。</li>
              )}
            </ul>
          )}

          <div className="flex items-center gap-2">
            <Input
              placeholder="新档案名（如 Kieran）"
              value={newName}
              maxLength={64}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleCreate()}
            />
            <Button variant="outline" disabled={busy === "__create__" || !newName.trim()} onClick={handleCreate}>
              <Plus className="size-4" /> 新建
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Zone 2 — observe */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Eye className="size-5" /> 观摩他人
          </CardTitle>
          <CardDescription>
            只读预览别人的视图布局，退出自动还原你自己的设置（不会写入对方档案）。
            {observingName && <span className="ml-1 font-medium text-amber-600">正在观摩 {observingName}。</span>}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-sm text-muted-foreground">加载中…</p>
          ) : (
            <ul className="divide-y rounded-md border">
              {profiles.map((p) => (
                <li key={p.name} className="flex items-center justify-between gap-3 px-3 py-2">
                  <span className="font-medium">{p.name}</span>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy === p.name || observingName === p.name}
                    onClick={() => handleObserve(p.name)}
                  >
                    <Eye className="size-4" /> {observingName === p.name ? "观摩中" : "观摩"}
                  </Button>
                </li>
              ))}
              {profiles.length === 0 && (
                <li className="px-3 py-4 text-sm text-muted-foreground">暂无可观摩的档案。</li>
              )}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
