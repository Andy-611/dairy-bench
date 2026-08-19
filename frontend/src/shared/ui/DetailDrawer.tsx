import { useEffect, useRef } from "react";
import type { ReactNode } from "react";

interface DetailDrawerProps {
  readonly ariaLabel: string;
  readonly children: ReactNode;
  readonly eyebrow: string;
  readonly onClose: () => void;
  readonly title: ReactNode;
}

export function DetailDrawer({
  ariaLabel,
  children,
  eyebrow,
  onClose,
  title,
}: DetailDrawerProps) {
  const closeButton = useRef<HTMLButtonElement | null>(null);
  const dialog = useRef<HTMLElement | null>(null);
  const closeAction = useRef(onClose);

  useEffect(() => {
    closeAction.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButton.current?.focus();

    function containFocus(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        event.preventDefault();
        closeAction.current();
        return;
      }
      if (event.key !== "Tab" || dialog.current === null) {
        return;
      }
      const focusable = focusableElements(dialog.current);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (first === undefined || last === undefined) {
        event.preventDefault();
        return;
      }
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !dialog.current.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !dialog.current.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", containFocus);
    return () => {
      document.removeEventListener("keydown", containFocus);
      if (previousFocus?.isConnected) {
        previousFocus.focus();
      }
    };
  }, []);

  return (
    <div className="drawer-layer" role="presentation">
      <button
        aria-label={`Close ${ariaLabel}`}
        className="drawer-backdrop"
        onClick={onClose}
        type="button"
      />
      <aside
        aria-label={ariaLabel}
        aria-modal="true"
        className="detail-drawer"
        ref={dialog}
        role="dialog"
      >
        <header className="drawer-header">
          <div>
            <span className="eyebrow">{eyebrow}</span>
            <h2>{title}</h2>
          </div>
          <button aria-label="Close" onClick={onClose} ref={closeButton} type="button">
            ×
          </button>
        </header>
        {children}
      </aside>
    </div>
  );
}

function focusableElements(container: HTMLElement): readonly HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), summary, textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => element.getAttribute("aria-hidden") !== "true");
}
