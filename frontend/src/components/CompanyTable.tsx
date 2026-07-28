import {
  formatGrowth,
  formatSignedValue,
  formatValue,
  ROLE_LABELS,
} from "../format";
import type { CompanyResultView, CompanyRole } from "../types";

interface CompanyTableProps {
  readonly companies: readonly CompanyResultView[];
}

const ROLE_ORDER: Readonly<Record<CompanyRole, number>> = {
  farm: 0,
  processor: 1,
  retailer: 2,
};

export function CompanyTable({ companies }: CompanyTableProps) {
  const orderedCompanies = [...companies].sort(
    (left, right) =>
      ROLE_ORDER[left.role] - ROLE_ORDER[right.role] ||
      left.companyName.localeCompare(right.companyName, "zh-CN"),
  );

  return (
    <section className="panel">
      <div className="section-heading">
        <div>
          <span className="eyebrow">COMPANIES</span>
          <h2>六家公司经营结果</h2>
        </div>
        <p>所有金额与库存均来自后端结算结果</p>
      </div>
      <div className="table-scroll">
        <table>
          <caption className="sr-only">六家公司最终经营结果</caption>
          <thead>
            <tr>
              <th scope="col">产业层级 / 公司</th>
              <th scope="col">策略</th>
              <th className="numeric" scope="col">
                初始现金
              </th>
              <th className="numeric" scope="col">
                最终现金
              </th>
              <th className="numeric" scope="col">
                库存价值
              </th>
              <th className="numeric" scope="col">
                创造剩余
              </th>
              <th className="numeric" scope="col">
                资本倍数
              </th>
            </tr>
          </thead>
          <tbody>
            {orderedCompanies.map((company) => (
              <tr key={company.companyId}>
                <td>
                  <div className="company-cell">
                    <span className={`role-dot ${company.role}`} />
                    <span>
                      <small>{ROLE_LABELS[company.role]}</small>
                      <strong>{company.companyName}</strong>
                    </span>
                  </div>
                </td>
                <td>{company.policyName}</td>
                <td className="numeric">{formatValue(company.initialCash)}</td>
                <td className="numeric">{formatValue(company.finalCash)}</td>
                <td className="numeric">{formatValue(company.inventoryValue)}</td>
                <td
                  className={`numeric value-${company.surplus >= 0 ? "up" : "down"}`}
                >
                  {formatSignedValue(company.surplus)}
                </td>
                <td className="numeric">{formatGrowth(company.growth)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
