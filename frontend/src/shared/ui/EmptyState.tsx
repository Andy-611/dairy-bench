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
        Run a 52-week simulation across production, procurement, processing,
        retail, and Sunday settlement of perishable inventory.
      </p>
      <ul>
        <li>3 farms</li>
        <li>3 processors</li>
        <li>3 retailers</li>
      </ul>
    </section>
  );
}
