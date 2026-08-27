/**
 * "This view was narrowed to your data scope."
 *
 * The visible half of the row-level country gate (backend/app/core/data_scope.py).
 * Two CS colleagues may only see Global clients, so on these pages an IB's
 * totals are computed over a SUBSET of that IB's downline. The figures stay
 * correct for what they cover and quietly stop being comparable with anybody
 * else's — same IB, same date range, a smaller number — and a table of numbers
 * has no way of saying so on its own. That silence is the accepted-but-mitigated
 * cost of the chosen design, and this line is the mitigation: the reader has to
 * be told, or they will report the difference as a data bug (or, worse, not
 * notice it).
 *
 * Rendered ONLY when the backend says `data_scope_filtered === true`, i.e. when
 * the response really was narrowed for this caller. Not shown to the
 * unrestricted majority, and not shown to a restricted caller whose result
 * happened to need no narrowing — a notice that is always there is read by
 * nobody within a week.
 *
 * Deliberately understated: muted supporting text, not an Alert and not a
 * banner. Nothing has gone wrong and this is not the page's primary content, so
 * it earns the weight of a footnote (Refactoring UI: establish hierarchy by
 * de-emphasising secondary content rather than shouting the primary).
 */
import { Info } from "lucide-react";

import { useI18n } from "@/components/i18n-provider";

interface Props {
  /** The response's `data_scope_filtered` flag. Anything falsy renders nothing. */
  show: boolean | undefined | null;
  /** Layout only (spacing / separators). The type styling is fixed on purpose,
   *  so the same notice reads the same on all three pages. */
  className?: string;
}

export function DataScopeNotice({ show, className = "" }: Props) {
  const { t } = useI18n();
  if (!show) return null;
  return (
    <p
      className={`flex items-start gap-1.5 text-xs leading-relaxed text-muted-foreground ${className}`}
    >
      <Info className="mt-0.5 size-3.5 flex-shrink-0" aria-hidden="true" />
      <span>{t("common.dataScopeNotice")}</span>
    </p>
  );
}
