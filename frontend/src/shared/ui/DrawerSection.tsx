import type { ReactNode } from "react";

interface DrawerSectionProps {
  readonly children: ReactNode;
  readonly label: string;
  readonly title: string;
}

export function DrawerSection({ children, label, title }: DrawerSectionProps) {
  return (
    <section className="drawer-section">
      <header>
        <span>{label}</span>
        <h3>{title}</h3>
      </header>
      <div>{children}</div>
    </section>
  );
}
