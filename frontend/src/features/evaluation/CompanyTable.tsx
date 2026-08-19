import {
  formatExactDecimal,
  formatGrowth,
  formatSignedExactDecimal,
} from "../../shared/format";
import type { CompanyResultView, CompanyRole } from "../../shared/api/types";
import { ROLE_LABELS } from "../../shared/labels";

interface CompanyTableProps {
  readonly companies: readonly CompanyResultView[];
  readonly completedWeeks: number;
  readonly provisional: boolean;
}

const ROLE_ORDER: Readonly<Record<CompanyRole, number>> = {
  farm: 0,
  processor: 1,
  retailer: 2,
};

export function CompanyTable({
  companies,
  completedWeeks,
  provisional,
}: CompanyTableProps) {
  const orderedCompanies = [...companies].sort(
    (left, right) =>
      ROLE_ORDER[left.role] - ROLE_ORDER[right.role] ||
      left.companyName.localeCompare(right.companyName, "en"),
  );

  return (
    <section className="panel">
      <div className="section-heading">
        <div>
          <span className="eyebrow">COMPANIES</span>
          <h2>Company results</h2>
        </div>
        <p>
          All financial and inventory values come from engine settlement
          {provisional ? ` through week ${completedWeeks}` : ""}.
        </p>
      </div>
      <div className="table-scroll">
        <table>
          <caption className="sr-only">
            {provisional ? `Week ${completedWeeks}` : "Final"} results for{" "}
            {companies.length} companies
          </caption>
          <thead>
            <tr>
              <th scope="col">Tier / company</th>
              <th scope="col">Policy</th>
              <th className="numeric" scope="col">
                Initial value
              </th>
              <th className="numeric" scope="col">
                {provisional ? "Current cash" : "Final cash"}
              </th>
              <th className="numeric" scope="col">
                Inventory value
              </th>
              <th className="numeric" scope="col">
                Surplus
              </th>
              <th className="numeric" scope="col">
                Capital multiple
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
                      <strong>
                        {company.companyName}
                        {company.status === "bankrupt" && (
                          <span className="company-status">BANKRUPT</span>
                        )}
                      </strong>
                    </span>
                  </div>
                </td>
                <td>{company.policyName}</td>
                <td className="numeric">{formatExactDecimal(company.initialValue)}</td>
                <td className="numeric">{formatExactDecimal(company.finalCash)}</td>
                <td className="numeric">{formatExactDecimal(company.inventoryValue)}</td>
                <td
                  className={`numeric value-${company.surplus.startsWith("-") ? "down" : "up"}`}
                >
                  {formatSignedExactDecimal(company.surplus)}
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
