export function EmptyState() {
  return (
    <section className="empty-state">
      <div aria-hidden="true" className="empty-illustration">
        <span className="milk-drop" />
        <span className="supply-line" />
        <span className="company-node node-one">牧</span>
        <span className="company-node node-two">加</span>
        <span className="company-node node-three">零</span>
      </div>
      <span className="eyebrow">READY TO SIMULATE</span>
      <h2>从一颗随机种子开始</h2>
      <p>
        点击“运行 30 天”，六家公司会依次完成生产、采购、加工、零售与库存过期结算。
      </p>
      <ul>
        <li>2 家牧场</li>
        <li>2 家加工厂</li>
        <li>2 家零售商</li>
      </ul>
    </section>
  );
}
