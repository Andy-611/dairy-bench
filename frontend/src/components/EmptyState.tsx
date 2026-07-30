export function EmptyState() {
  return (
    <section className="empty-state">
      <div aria-hidden="true" className="empty-illustration">
        <span className="milk-drop" />
        <span className="supply-line" />
        <span className="company-node node-one">F</span>
        <span className="company-node node-two">P</span>
        <span className="company-node node-three">R</span>
      </div>
      <span className="eyebrow">READY TO SIMULATE</span>
      <h2>Start with a random seed</h2>
      <p>
        Run a 30-day simulation across production, procurement, processing,
        retail, and perishable inventory settlement.
      </p>
      <ul>
        <li>2 farms</li>
        <li>2 processors</li>
        <li>2 retailers</li>
      </ul>
    </section>
  );
}
