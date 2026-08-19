export function TimelineNotice({ label }: { readonly label: string }) {
  return (
    <div className="timeline-notice" role="status">
      <span aria-hidden="true" className="spinner dark" />
      <p>{label}</p>
    </div>
  );
}

export function TimelineError({ message }: { readonly message: string }) {
  return (
    <div className="timeline-error" role="alert">
      <strong>Timeline unavailable</strong>
      <p>{message}</p>
    </div>
  );
}
