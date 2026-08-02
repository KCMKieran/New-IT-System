/**
 * Native SVG signed bar chart (SSOT §2.2 / prototype).
 * Click bar → expand that slot and scroll (callback).
 */

import { useMemo, useState } from "react";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { ChartMetric, Granularity, SlotChartPoint } from "./types";
import { barColor, fmtAxisK, fmtInt, fmtLots, fmtSigned } from "./format";
import { slotLabel, winRatePct } from "./tree";

interface Props {
  points: SlotChartPoint[];
  gran: Granularity;
  metric: ChartMetric;
  onMetricChange: (m: ChartMetric) => void;
  onBarClick: (slot: number) => void;
}

interface TipState {
  x: number;
  y: number;
  html: string;
}

const W = 1060;
const H = 210;
const PAD_L = 54;
const PAD_R = 8;
const PAD_T = 14;
const PAD_B = 26;

export function HoldBucketChart({
  points,
  gran,
  metric,
  onMetricChange,
  onBarClick,
}: Props) {
  const [tip, setTip] = useState<TipState | null>(null);

  const { bars, grid, zeroY, xLabels, title } = useMemo(() => {
    const n = points.length || 1;
    const vals = points.map((d) =>
      metric === "total" ? d.total_profit_sum : d.per_lot,
    );
    const maxV = Math.max(...vals, 0);
    const minV = Math.min(...vals, 0);
    const span = maxV - minV || 1;
    const y = (v: number) => PAD_T + ((maxV - v) / span) * (H - PAD_T - PAD_B);
    const bw = (W - PAD_L - PAD_R) / n;

    const gridLines = [];
    for (let s = 0; s <= 4; s++) {
      const v = minV + (span * s) / 4;
      const yy = y(v);
      gridLines.push({ y: yy, label: fmtAxisK(v) });
    }

    const maxAbs = Math.max(...vals.map(Math.abs), 0);
    const minAbs = Math.min(...vals);
    // Label only extreme bars (prototype).
    const barsOut = points.map((d, i) => {
      const v = vals[i] ?? 0;
      const x = PAD_L + i * bw + 1;
      const w = Math.max(bw - 2, 2);
      const yTop = v >= 0 ? y(v) : y(0);
      const h = Math.max(2, Math.abs(y(v) - y(0)));
      const showLabel = v === maxV || v === minAbs;
      return { d, i, v, x, w, yTop, h, showLabel, maxAbs };
    });

    const step = gran === 60 ? 2 : 4;
    const labels = [];
    for (let i = 0; i < n; i += step) {
      labels.push({
        x: PAD_L + i * bw + bw / 2,
        text: slotLabel(i, gran).slice(0, 5),
      });
    }

    return {
      bars: barsOut,
      grid: gridLines,
      zeroY: y(0),
      xLabels: labels,
      title:
        metric === "total"
          ? "各时段 Profit (commission, swaps) 总额（全范围合计）"
          : "各时段每手 Profit (commission, swaps)（全范围）",
    };
  }, [points, gran, metric]);

  return (
    <div className="rounded-xl border bg-card px-3.5 pb-1.5 pt-3.5">
      <div className="mb-1.5 flex flex-wrap items-center gap-2.5">
        <span className="text-[13px] font-semibold">{title}</span>
        <ToggleGroup
          type="single"
          value={metric}
          onValueChange={(v) => v && onMetricChange(v as ChartMetric)}
          className="inline-flex items-center rounded-lg bg-muted p-0.5"
        >
          <ToggleGroupItem
            value="total"
            className="rounded-md px-2.5 py-1 text-xs text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
          >
            总额
          </ToggleGroupItem>
          <ToggleGroupItem
            value="perlot"
            className="rounded-md px-2.5 py-1 text-xs text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
          >
            每手
          </ToggleGroupItem>
        </ToggleGroup>
        <span className="ml-auto text-[11.5px] text-muted-foreground">
          正=客户赚
        </span>
      </div>

      <svg
        width="100%"
        height={210}
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="block"
      >
        {grid.map((g, idx) => (
          <g key={idx}>
            <line
              x1={PAD_L}
              x2={W - PAD_R}
              y1={g.y}
              y2={g.y}
              stroke="#eef1f5"
              strokeWidth={1}
            />
            <text
              x={PAD_L - 6}
              y={g.y + 4}
              textAnchor="end"
              fontSize={10}
              fill="#94a3b8"
            >
              {g.label}
            </text>
          </g>
        ))}
        <line
          x1={PAD_L}
          x2={W - PAD_R}
          y1={zeroY}
          y2={zeroY}
          stroke="#cbd5e1"
          strokeWidth={1.4}
        />
        {bars.map(({ d, i, v, x, w, yTop, h, showLabel }) => (
          <g key={i}>
            <rect
              x={x}
              y={yTop}
              width={w}
              height={h}
              rx={3}
              fill={barColor(v)}
              opacity={0.92}
              style={{ cursor: "pointer" }}
              onMouseMove={(e) => {
                setTip({
                  x: e.clientX + 14,
                  y: e.clientY - 10,
                  html:
                    `<b>${slotLabel(d.slot, gran)}</b><br/>` +
                    `Profit(c,s) ${fmtSigned(Math.round(d.total_profit_sum))} · 每手 ${fmtSigned(d.per_lot, 1)}<br/>` +
                    `${fmtInt(d.orders_count)} 单 / ${fmtInt(d.clients)} 客户 / ${fmtLots(d.lots_sum)} 手 · 胜率 ${winRatePct(d.win_rate).toFixed(1)}%`,
                });
              }}
              onMouseLeave={() => setTip(null)}
              onClick={() => onBarClick(d.slot)}
            />
            {showLabel && (
              <text
                x={x + w / 2}
                y={v >= 0 ? yTop - 4 : yTop + h + 11}
                textAnchor="middle"
                fontSize={10}
                fill="#475569"
                fontWeight={600}
              >
                {fmtSigned(Math.round(v))}
              </text>
            )}
          </g>
        ))}
        {xLabels.map((lb, idx) => (
          <text
            key={idx}
            x={lb.x}
            y={H - 8}
            textAnchor="middle"
            fontSize={10}
            fill="#94a3b8"
          >
            {lb.text}
          </text>
        ))}
      </svg>

      {tip && (
        <div
          className="pointer-events-none fixed z-[200] whitespace-nowrap rounded-md bg-slate-800 px-2.5 py-1.5 text-xs leading-normal text-white"
          style={{ left: tip.x, top: tip.y }}
          dangerouslySetInnerHTML={{ __html: tip.html }}
        />
      )}
    </div>
  );
}
