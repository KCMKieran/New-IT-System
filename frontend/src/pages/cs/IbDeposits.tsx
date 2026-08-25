/**
 * IB Deposits & Withdrawals (CS · IB 出入金查询) — `/cs/ib-deposits`.
 *
 * A copy of the IB half of `/warehouse/ib-data` (Data Query) for CS, who need
 * the same per-IB deposit/withdrawal figures without being granted the whole
 * `data` module. "Copy" only in the sense of a second entry point: the card is
 * the SAME component, so the two pages cannot drift apart in definition or in
 * the numbers they report.
 *
 * The Company (region) card deliberately did NOT come along — it is a firm-wide
 * CN/Global roll-up, not something CS looks up per IB. That is also why the
 * backend carve-out covers `/ib-data/query` and `/ib-data/last-run` only, and
 * `/ib-data/region-query` stays `data`-module.
 */

import IbFundFlowCard from "@/components/ib-data/IbFundFlowCard";

export default function CsIbDepositsPage() {
  return (
    <div className="space-y-3 p-2 sm:space-y-6 sm:p-6">
      <IbFundFlowCard />
    </div>
  );
}
