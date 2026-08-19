interface DefinitionItemProps {
  readonly label: string;
  readonly value: string;
}

export function DefinitionItem({ label, value }: DefinitionItemProps) {
  return (
    <div>
      <dt>{label}</dt>
      <dd title={value}>{value}</dd>
    </div>
  );
}
